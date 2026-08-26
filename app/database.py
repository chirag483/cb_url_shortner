import logging
from pathlib import Path
from typing import Optional

import asyncpg

from app.config import get_settings

logger = logging.getLogger("urlshortener.db")

_pool: Optional[asyncpg.Pool] = None


async def init_pool() -> asyncpg.Pool:
    global _pool
    settings = get_settings()
    _pool = await asyncpg.create_pool(
        dsn=settings.database_url,
        min_size=settings.db_pool_min_size,
        max_size=settings.db_pool_max_size,
        command_timeout=10,
    )
    await _run_migrations(_pool)
    logger.info(
        "Postgres pool ready (min=%s, max=%s, unlogged_table=%s)",
        settings.db_pool_min_size,
        settings.db_pool_max_size,
        settings.unlogged_table,
    )
    return _pool


async def _run_migrations(pool: asyncpg.Pool) -> None:
    settings = get_settings()
    table_kind = "UNLOGGED TABLE" if settings.unlogged_table else "TABLE"
    schema_path = Path(__file__).resolve().parent.parent / "sql" / "init.sql"
    sql = schema_path.read_text().replace("__TABLE_KIND__", table_kind)
    async with pool.acquire() as conn:
        await conn.execute(sql)


def get_pool() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError("Database pool is not initialized yet")
    return _pool


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None
