"""Connection factories (asyncpg pool, Redis) and a lifespan helper that owns what it creates."""

from contextlib import asynccontextmanager

import asyncpg
from fastapi import FastAPI
from redis.asyncio import Redis

from libs.common.settings import env_int


async def create_pool(dsn: str) -> asyncpg.Pool:
    return await asyncpg.create_pool(
        dsn,
        min_size=env_int("DB_POOL_MIN", 2),
        max_size=env_int("DB_POOL_MAX", 10),
        command_timeout=env_int("DB_COMMAND_TIMEOUT_S", 10),
    )


def create_redis(url: str) -> Redis:
    return Redis.from_url(url, decode_responses=True)


@asynccontextmanager
async def open_resources(app: FastAPI, *, database_url: str | None, redis_url: str | None):
    """Create app.state.pool / app.state.redis unless they were injected (tests)."""
    owned: list = []
    if database_url and getattr(app.state, "pool", None) is None:
        app.state.pool = await create_pool(database_url)
        owned.append(app.state.pool)
    if redis_url and getattr(app.state, "redis", None) is None:
        app.state.redis = create_redis(redis_url)
        owned.append(app.state.redis)
    try:
        yield
    finally:
        for resource in owned:
            if isinstance(resource, Redis):
                await resource.aclose()
            else:
                await resource.close()
