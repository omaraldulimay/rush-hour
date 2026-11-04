import json
import logging
from contextlib import asynccontextmanager
from typing import Annotated, Any

import asyncpg
from fastapi import FastAPI, HTTPException, Query
from prometheus_client import Counter
from redis.asyncio import Redis
from redis.exceptions import RedisError

from libs.common.db import open_resources
from libs.common.logging import configure_logging
from libs.common.observability import install_observability, pg_check, redis_check
from services.catalog.app.settings import Settings

SERVICE = "catalog"
log = logging.getLogger(SERVICE)

CACHE_REQUESTS = Counter(
    "catalog_cache_requests_total", "Catalog cache lookups", ["result"]
)  # result = hit | miss

COLUMNS = "sku, name, category, price_cents, currency, limited_drop"


class Repository:
    """Cache-aside reads. Redis trouble is treated as a miss: browsing must keep working."""

    def __init__(self, pool: asyncpg.Pool, redis: Redis, ttl_s: int) -> None:
        self.pool, self.redis, self.ttl_s = pool, redis, ttl_s

    async def _cached(self, key: str) -> Any | None:
        try:
            raw = await self.redis.get(key)
        except RedisError:
            log.warning("cache read failed", extra={"key": key})
            raw = None
        CACHE_REQUESTS.labels("hit" if raw is not None else "miss").inc()
        return json.loads(raw) if raw is not None else None

    async def _store(self, key: str, value: Any) -> None:
        try:
            await self.redis.set(key, json.dumps(value), ex=self.ttl_s)
        except RedisError:
            log.warning("cache write failed", extra={"key": key})

    async def get_product(self, sku: str) -> dict[str, Any] | None:
        key = f"catalog:product:{sku}"
        if (cached := await self._cached(key)) is not None:
            return cached
        row = await self.pool.fetchrow(
            f"SELECT {COLUMNS} FROM catalog.products WHERE sku = $1", sku
        )
        if row is None:
            return None
        product = dict(row)
        await self._store(key, product)
        return product

    async def list_products(self, category: str | None, limit: int, offset: int) -> list[dict]:
        key = f"catalog:list:{category or '*'}:{limit}:{offset}"
        if (cached := await self._cached(key)) is not None:
            return cached
        rows = await self.pool.fetch(
            f"""SELECT {COLUMNS} FROM catalog.products
                WHERE ($1::text IS NULL OR category = $1)
                ORDER BY sku LIMIT $2 OFFSET $3""",
            category,
            limit,
            offset,
        )
        products = [dict(r) for r in rows]
        await self._store(key, products)
        return products


def create_app(
    settings: Settings | None = None,
    *,
    pool: asyncpg.Pool | None = None,
    redis: Redis | None = None,
) -> FastAPI:
    settings = settings or Settings.from_env()
    configure_logging(SERVICE)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        async with open_resources(
            app, database_url=settings.database_url, redis_url=settings.redis_url
        ):
            yield

    app = FastAPI(title="catalog", lifespan=lifespan)
    app.state.pool, app.state.redis = pool, redis
    install_observability(app, SERVICE, {"postgres": pg_check(app), "redis": redis_check(app)})

    def repo() -> Repository:
        return Repository(app.state.pool, app.state.redis, settings.cache_ttl_s)

    @app.get("/products")
    async def list_products(
        category: str | None = None,
        limit: Annotated[int, Query(ge=1, le=100)] = 20,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> dict[str, Any]:
        items = await repo().list_products(category, limit, offset)
        return {"items": items, "limit": limit, "offset": offset}

    @app.get("/products/{sku}")
    async def get_product(sku: str) -> dict[str, Any]:
        product = await repo().get_product(sku)
        if product is None:
            raise HTTPException(404, {"error": "not_found", "sku": sku})
        return product

    return app
