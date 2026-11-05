import random
from contextlib import asynccontextmanager
from typing import Annotated

import asyncpg
import httpx
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from redis.asyncio import Redis

from libs.common import orders_db
from libs.common.db import open_resources
from libs.common.http import make_client
from libs.common.logging import configure_logging
from libs.common.observability import install_observability, pg_check, redis_check
from services.checkout.app.breaker import CircuitBreaker
from services.checkout.app.service import CheckoutError, CheckoutService, cart_key
from services.checkout.app.settings import Settings

SERVICE = "checkout"


class CartItemIn(BaseModel):
    sku: str = Field(min_length=1)
    qty: int = Field(ge=1, le=10)


class CheckoutIn(BaseModel):
    cart_id: str = Field(min_length=1)


def create_app(
    settings: Settings | None = None,
    *,
    pool: asyncpg.Pool | None = None,
    redis: Redis | None = None,
    catalog: httpx.AsyncClient | None = None,
    inventory: httpx.AsyncClient | None = None,
    payment: httpx.AsyncClient | None = None,
) -> FastAPI:
    """Downstream clients can be injected (tests wire them to in-process apps)."""
    settings = settings or Settings.from_env()
    configure_logging(SERVICE)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        async with open_resources(
            app, database_url=settings.database_url, redis_url=settings.redis_url
        ):
            owned = []
            for attr, url in (
                ("catalog", settings.catalog_url),
                ("inventory", settings.inventory_url),
                ("payment", settings.payment_url),
            ):
                if getattr(app.state, attr) is None:
                    client = make_client(url, settings.http_timeout_s)
                    setattr(app.state, attr, client)
                    owned.append(client)
            try:
                yield
            finally:
                for client in owned:
                    await client.aclose()

    app = FastAPI(title="checkout", lifespan=lifespan)
    app.state.pool, app.state.redis = pool, redis
    app.state.catalog, app.state.inventory, app.state.payment = catalog, inventory, payment
    app.state.breaker = CircuitBreaker(
        "payment",
        threshold=settings.breaker_failure_threshold,
        window_s=settings.breaker_window_s,
        cooldown_s=settings.breaker_cooldown_s,
    )
    install_observability(app, SERVICE, {"postgres": pg_check(app), "redis": redis_check(app)})

    def service() -> CheckoutService:
        s = app.state
        return CheckoutService(
            settings, s.pool, s.redis, s.catalog, s.inventory, s.payment, s.breaker
        )

    async def cart_view(cart_id: str) -> dict:
        raw = await app.state.redis.hgetall(cart_key(cart_id))
        items = [{"sku": sku, "qty": int(qty)} for sku, qty in sorted(raw.items())]
        return {"cart_id": cart_id, "items": items}

    @app.put("/carts/{cart_id}/items")
    async def put_item(cart_id: str, item: CartItemIn) -> dict:
        key = cart_key(cart_id)
        redis_ = app.state.redis
        await redis_.hset(key, item.sku, item.qty)
        await redis_.expire(key, settings.cart_ttl_s)
        return await cart_view(cart_id)

    @app.get("/carts/{cart_id}")
    async def get_cart(cart_id: str) -> dict:
        return await cart_view(cart_id)

    @app.delete("/carts/{cart_id}/items/{sku}")
    async def delete_item(cart_id: str, sku: str) -> dict:
        await app.state.redis.hdel(cart_key(cart_id), sku)
        return await cart_view(cart_id)

    @app.post("/checkout")
    async def checkout(
        body: CheckoutIn,
        idempotency_key: Annotated[str | None, Header(max_length=128)] = None,
    ) -> JSONResponse:
        if settings.fault_error_rate and random.random() < settings.fault_error_rate:
            # the "bad release" of the game day: fails before touching stock, payment or orders
            return JSONResponse({"error": "injected_fault"}, status_code=500)
        try:
            order, created = await service().checkout(body.cart_id, idempotency_key)
        except CheckoutError as exc:
            return JSONResponse(exc.body, status_code=exc.status_code)
        return JSONResponse(order, status_code=201 if created else 200)

    @app.get("/orders/{order_id}")
    async def get_order(order_id: str) -> dict:
        order = await orders_db.get_order(app.state.pool, order_id)
        if order is None:
            raise HTTPException(404, {"error": "not_found"})
        return order

    return app
