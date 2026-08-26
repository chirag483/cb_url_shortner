import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Optional

import asyncpg
from fastapi import FastAPI, HTTPException
from fastapi import Path as FPath
from fastapi.responses import RedirectResponse

from app.cache import get_cache, start_cache_listener, stop_cache_listener
from app.config import get_settings
from app.database import close_pool, get_pool, init_pool
from app.schemas import HealthResponse, ShortenRequest, ShortenResponse, StatsResponse
from app.utils import generate_short_code, is_valid_custom_code

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("urlshortener")

settings = get_settings()

# Hits are buffered in memory and flushed to Postgres periodically in a
# single batched UPDATE per code, instead of writing on every redirect.
# This keeps the hot redirect path to a single read (often cache-only)
# with no write on the critical path.
_hit_buffer: dict[str, int] = {}
_hit_buffer_lock = asyncio.Lock()
_flusher_task: Optional[asyncio.Task] = None


async def _record_hit(code: str) -> None:
    async with _hit_buffer_lock:
        _hit_buffer[code] = _hit_buffer.get(code, 0) + 1


async def _flush_hits() -> None:
    async with _hit_buffer_lock:
        if not _hit_buffer:
            return
        batch = dict(_hit_buffer)
        _hit_buffer.clear()
    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            for code, count in batch.items():
                await conn.execute(
                    "UPDATE urls SET hits = hits + $1, last_accessed_at = now() "
                    "WHERE short_code = $2",
                    count,
                    code,
                )


async def _flush_hits_periodically() -> None:
    while True:
        await asyncio.sleep(settings.hit_flush_interval_seconds)
        try:
            await _flush_hits()
        except Exception:
            logger.exception("Failed to flush hit counters")


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _flusher_task
    await init_pool()
    await start_cache_listener(settings.database_url)
    _flusher_task = asyncio.create_task(_flush_hits_periodically())
    logger.info("URL shortener ready (base_url=%s)", settings.base_url)
    yield
    if _flusher_task is not None:
        _flusher_task.cancel()
        try:
            await _flusher_task
        except asyncio.CancelledError:
            pass
    await _flush_hits()
    await stop_cache_listener()
    await close_pool()


app = FastAPI(title="PostgreSQL URL Shortener", lifespan=lifespan)


@app.get("/health", response_model=HealthResponse)
async def health():
    pool = get_pool()
    async with pool.acquire() as conn:
        await conn.execute("SELECT 1")
    return HealthResponse(status="ok", cache_size=get_cache().size())


@app.post("/api/shorten", response_model=ShortenResponse, status_code=201)
async def shorten(payload: ShortenRequest):
    pool = get_pool()
    long_url = str(payload.url)

    expires_at = None
    if payload.expires_in_days:
        expires_at = datetime.now(timezone.utc) + timedelta(days=payload.expires_in_days)
    elif settings.default_expiry_days:
        expires_at = datetime.now(timezone.utc) + timedelta(days=settings.default_expiry_days)

    if payload.custom_code:
        if not is_valid_custom_code(payload.custom_code):
            raise HTTPException(400, "Invalid custom code: use 3-32 chars of letters, digits, - or _")
        code = payload.custom_code
        async with pool.acquire() as conn:
            try:
                await conn.execute(
                    "INSERT INTO urls (short_code, long_url, expires_at) VALUES ($1, $2, $3)",
                    code,
                    long_url,
                    expires_at,
                )
            except asyncpg.UniqueViolationError:
                raise HTTPException(409, "Custom code already in use")
    else:
        code = None
        async with pool.acquire() as conn:
            for _ in range(5):
                candidate = generate_short_code(settings.short_code_length)
                try:
                    await conn.execute(
                        "INSERT INTO urls (short_code, long_url, expires_at) VALUES ($1, $2, $3)",
                        candidate,
                        long_url,
                        expires_at,
                    )
                    code = candidate
                    break
                except asyncpg.UniqueViolationError:
                    continue
        if code is None:
            raise HTTPException(500, "Could not generate a unique short code, please retry")

    return ShortenResponse(
        short_code=code,
        short_url=f"{settings.base_url.rstrip('/')}/{code}",
        long_url=long_url,
        expires_at=expires_at,
    )


@app.get("/api/stats/{code}", response_model=StatsResponse)
async def stats(code: str = FPath(..., min_length=1, max_length=32)):
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT short_code, long_url, created_at, expires_at, hits, last_accessed_at "
            "FROM urls WHERE short_code = $1",
            code,
        )
    if row is None:
        raise HTTPException(404, "Short code not found")
    return StatsResponse(**dict(row))


@app.delete("/api/{code}", status_code=204)
async def delete_code(code: str = FPath(..., min_length=1, max_length=32)):
    pool = get_pool()
    async with pool.acquire() as conn:
        result = await conn.execute("DELETE FROM urls WHERE short_code = $1", code)
    if result.endswith(" 0"):
        raise HTTPException(404, "Short code not found")
    # The DB trigger also broadcasts a NOTIFY, but we evict locally too so
    # the deleting instance doesn't serve a stale hit before the round trip.
    await get_cache().invalidate(code)


@app.get("/{code}")
async def resolve(code: str = FPath(..., min_length=1, max_length=32)):
    cache = get_cache()
    cached = await cache.get(code)

    if cached is not None:
        expires_at = cached.get("expires_at")
        if expires_at is not None and expires_at < datetime.now(timezone.utc):
            await cache.invalidate(code)
        else:
            await _record_hit(code)
            return RedirectResponse(cached["long_url"], status_code=307)

    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT long_url, expires_at FROM urls WHERE short_code = $1", code
        )

    if row is None:
        raise HTTPException(404, "Short URL not found")

    if row["expires_at"] is not None and row["expires_at"] < datetime.now(timezone.utc):
        raise HTTPException(410, "Short URL has expired")

    await cache.set(code, {"long_url": row["long_url"], "expires_at": row["expires_at"]})
    await _record_hit(code)
    return RedirectResponse(row["long_url"], status_code=307)
