"""Database access of the Control Room: limited-drop stock, the oversold invariant, revenue.

Reads only touch other services' tables (inventory, orders); the single write is the stock reset,
which does the same as `python -m scripts.seed --reset-stock` for the hot items.
"""

from datetime import UTC, datetime
from functools import cache

import asyncpg
from prometheus_client import Gauge

from scripts.seed import SeedProduct, generate

DROP_STOCK = Gauge("drop_stock_available", "Units left of a limited-drop item", ["sku", "item"])
DROP_OVERSOLD = Gauge("drop_oversold_units", "Units sold beyond stock (the invariant: always 0)")

EPOCH_START = datetime(1970, 1, 1, tzinfo=UTC)


@cache
def hot_items() -> tuple[SeedProduct, ...]:
    return tuple(p for p in generate() if p.limited_drop)


async def _epoch(conn: asyncpg.Connection) -> datetime:
    value = await conn.fetchval("SELECT reset_at FROM control_room.stock_epoch WHERE id = 1")
    return value or EPOCH_START


async def drop_status(pool: asyncpg.Pool) -> dict:
    """Stock of the hot items and the invariant, from one consistent snapshot.

    held = units in reservations that are still reserved or committed since the last reset.
    oversold = units held beyond what was ever in stock + units of negative stock. It must be 0.
    """
    items = hot_items()
    async with pool.acquire() as conn, conn.transaction(isolation="repeatable_read", readonly=True):
        epoch = await _epoch(conn)
        rows = await conn.fetch(
            """SELECT s.sku, s.available,
                      COALESCE((SELECT sum(ri.qty)
                                  FROM inventory.reservation_items ri
                                  JOIN inventory.reservations r USING (order_id)
                                 WHERE ri.sku = s.sku
                                   AND r.status IN ('reserved', 'committed')
                                   AND r.created_at >= $2), 0) AS held
                 FROM inventory.stock s WHERE s.sku = ANY($1)""",
            [p.sku for p in items],
            epoch,
        )
    by_sku = {r["sku"]: r for r in rows}
    out, oversold = [], 0
    for number, p in enumerate(items, start=1):
        row = by_sku.get(p.sku)
        available = row["available"] if row else p.stock
        held = int(row["held"]) if row else 0
        oversold += max(0, held - p.stock) + max(0, -available)
        out.append(
            {
                "sku": p.sku,
                "name": p.name.removeprefix("Nordkai ").split(" ", 3)[-1],
                "label": f"Drop {number:02d}",
                "initial": p.stock,
                "available": available,
                "held": held,
                "sold_out": available <= 0,
            }
        )
    return {"items": out, "oversold": oversold, "invariant_ok": oversold == 0}


async def revenue_cents(pool: asyncpg.Pool) -> int:
    async with pool.acquire() as conn:
        return int(
            await conn.fetchval(
                "SELECT COALESCE(sum(total_cents), 0) FROM orders.orders "
                "WHERE status = 'paid' AND created_at >= $1",
                await _epoch(conn),
            )
        )


async def reset_stock(pool: asyncpg.Pool) -> int:
    items = hot_items()
    async with pool.acquire() as conn, conn.transaction():
        await conn.executemany(
            "INSERT INTO inventory.stock (sku, available) VALUES ($1, $2) "
            "ON CONFLICT (sku) DO UPDATE SET available = EXCLUDED.available, updated_at = now()",
            [(p.sku, p.stock) for p in items],
        )
        await conn.execute(
            "INSERT INTO control_room.stock_epoch (id, reset_at) VALUES (1, now()) "
            "ON CONFLICT (id) DO UPDATE SET reset_at = EXCLUDED.reset_at"
        )
    return len(items)


async def publish_gauges(pool: asyncpg.Pool) -> dict:
    """Mirror the limited-drop state into gauges (5 items, cheap) for alerts and dashboards."""
    status = await drop_status(pool)
    for item in status["items"]:
        DROP_STOCK.labels(item["sku"], item["label"]).set(item["available"])
    DROP_OVERSOLD.set(status["oversold"])
    return status
