from contextlib import asynccontextmanager
from typing import Any

import asyncpg
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from libs.common.db import open_resources
from libs.common.logging import configure_logging
from libs.common.observability import install_observability, pg_check
from services.inventory.app import repo
from services.inventory.app.settings import Settings

SERVICE = "inventory"


class Item(BaseModel):
    sku: str = Field(min_length=1)
    qty: int = Field(gt=0, le=1000)


class ReservationIn(BaseModel):
    order_id: str = Field(min_length=1)
    items: list[Item] = Field(min_length=1)


def merge_items(items: list[Item]) -> dict[str, int]:
    merged: dict[str, int] = {}
    for item in items:
        merged[item.sku] = merged.get(item.sku, 0) + item.qty
    return merged


def create_app(settings: Settings | None = None, *, pool: asyncpg.Pool | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    configure_logging(SERVICE)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        async with open_resources(app, database_url=settings.database_url, redis_url=None):
            yield

    app = FastAPI(title="inventory", lifespan=lifespan)
    app.state.pool = pool
    install_observability(app, SERVICE, {"postgres": pg_check(app)})

    @app.post("/reservations", status_code=201)
    async def create_reservation(body: ReservationIn) -> JSONResponse:
        try:
            reservation, created = await repo.reserve(
                app.state.pool, body.order_id, merge_items(body.items)
            )
        except repo.SoldOut as exc:
            return JSONResponse({"error": "sold_out", "sku": exc.sku}, status_code=409)
        except repo.UnknownSku as exc:
            return JSONResponse({"error": "unknown_sku", "sku": exc.sku}, status_code=404)
        return JSONResponse(reservation, status_code=201 if created else 200)

    @app.post("/reservations/{order_id}/release")
    async def release_reservation(order_id: str) -> JSONResponse:
        reservation = await repo.release(app.state.pool, order_id)
        if reservation is None:
            raise HTTPException(404, {"error": "unknown_reservation"})
        if reservation["status"] == "committed":
            return JSONResponse({"error": "already_committed"}, status_code=409)
        return JSONResponse(reservation)

    @app.post("/reservations/{order_id}/commit")
    async def commit_reservation(order_id: str) -> JSONResponse:
        reservation = await repo.commit(app.state.pool, order_id)
        if reservation is None:
            raise HTTPException(404, {"error": "unknown_reservation"})
        if reservation["status"] == "released":
            return JSONResponse({"error": "already_released"}, status_code=409)
        return JSONResponse(reservation)

    @app.get("/stock/{sku}")
    async def stock(sku: str) -> dict[str, Any]:
        available = await repo.get_stock(app.state.pool, sku)
        if available is None:
            raise HTTPException(404, {"error": "unknown_sku", "sku": sku})
        return {"sku": sku, "available": available}

    return app
