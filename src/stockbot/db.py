"""Async Postgres connection pool, shared by the bot and market services."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

_pool: AsyncConnectionPool | None = None


async def init_pool(conninfo: str, *, min_size: int = 1, max_size: int = 10) -> AsyncConnectionPool:
    """Create and open the global connection pool. Call once at startup."""
    global _pool
    if _pool is not None:
        return _pool
    _pool = AsyncConnectionPool(conninfo, min_size=min_size, max_size=max_size, open=False)
    await _pool.open(wait=True)
    return _pool


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


def get_pool() -> AsyncConnectionPool:
    if _pool is None:
        raise RuntimeError("Connection pool is not initialized; call init_pool() first.")
    return _pool


def pool_stats() -> dict[str, int]:
    """psycopg_pool's own metrics (size/available/waiting), empty before
    init or if the pool doesn't expose stats. Surfaced by /admin health."""
    if _pool is None:
        return {}
    try:
        raw = _pool.get_stats()
    except Exception:
        return {}
    return {str(k): int(v) for k, v in raw.items() if isinstance(v, int)}


@asynccontextmanager
async def connection() -> AsyncIterator[AsyncConnection]:
    """Yield a pooled connection. Callers manage their own transactions."""
    pool = get_pool()
    async with pool.connection() as conn:
        yield conn
