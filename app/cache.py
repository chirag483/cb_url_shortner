"""
Fast-resolution strategy (PostgreSQL only, no Redis/Memcached):

1. The `urls` table has a unique index on short_code, so PostgreSQL's own
   shared_buffers keep hot rows in memory -- repeated lookups of popular
   codes are served from RAM by Postgres itself.
2. On top of that, each app instance keeps a small in-process LRU cache of
   recently resolved codes, so the very hottest codes avoid a DB round trip
   entirely.
3. Correctness is preserved with zero extra infrastructure by using
   PostgreSQL's LISTEN/NOTIFY: a trigger on the urls table fires
   pg_notify() on every UPDATE/DELETE, and every app instance listens on
   that channel and evicts the matching key. This means a delete/edit is
   reflected across all replicas within milliseconds, with Postgres itself
   acting as the invalidation bus.
"""
import asyncio
import logging
from collections import OrderedDict
from typing import Optional

import asyncpg

from app.config import get_settings

logger = logging.getLogger("urlshortener.cache")


class LRUCache:
    def __init__(self, max_size: int):
        self.max_size = max_size
        self._data: "OrderedDict[str, dict]" = OrderedDict()
        self._lock = asyncio.Lock()

    async def get(self, key: str) -> Optional[dict]:
        async with self._lock:
            value = self._data.get(key)
            if value is not None:
                self._data.move_to_end(key)
            return value

    async def set(self, key: str, value: dict) -> None:
        async with self._lock:
            self._data[key] = value
            self._data.move_to_end(key)
            if len(self._data) > self.max_size:
                self._data.popitem(last=False)

    async def invalidate(self, key: str) -> None:
        async with self._lock:
            self._data.pop(key, None)

    async def clear(self) -> None:
        async with self._lock:
            self._data.clear()

    def size(self) -> int:
        return len(self._data)


_cache: Optional[LRUCache] = None
_listener_conn: Optional[asyncpg.Connection] = None


def get_cache() -> LRUCache:
    global _cache
    if _cache is None:
        _cache = LRUCache(get_settings().cache_max_size)
    return _cache


async def start_cache_listener(dsn: str) -> None:
    """Open a dedicated connection (required by asyncpg for LISTEN) and
    subscribe to invalidation events published by the Postgres trigger."""
    global _listener_conn
    _listener_conn = await asyncpg.connect(dsn=dsn)

    def _on_notify(connection, pid, channel, payload):
        cache = get_cache()
        asyncio.create_task(cache.invalidate(payload))
        logger.debug("Evicted cache entry for short_code=%s (NOTIFY)", payload)

    await _listener_conn.add_listener("url_cache_invalidate", _on_notify)
    logger.info("Subscribed to Postgres channel 'url_cache_invalidate' for cache invalidation")


async def stop_cache_listener() -> None:
    global _listener_conn
    if _listener_conn is not None:
        await _listener_conn.close()
        _listener_conn = None
