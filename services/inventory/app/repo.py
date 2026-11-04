"""All stock mutations. Every operation is one short transaction, no read-then-write."""

from typing import Any

import asyncpg

Reservation = dict[str, Any]


class SoldOut(Exception):
    def __init__(self, sku: str) -> None:
        super().__init__(sku)
        self.sku = sku


class UnknownSku(Exception):
    def __init__(self, sku: str) -> None:
        super().__init__(sku)
        self.sku = sku


async def fetch_reservation(conn: asyncpg.Connection, order_id: str) -> Reservation | None:
    row = await conn.fetchrow(
        "SELECT order_id, status FROM inventory.reservations WHERE order_id = $1", order_id
    )
    if row is None:
        return None
    items = await conn.fetch(
        "SELECT sku, qty FROM inventory.reservation_items WHERE order_id = $1 ORDER BY sku",
        order_id,
    )
    return {"order_id": row["order_id"], "status": row["status"], "items": [dict(i) for i in items]}


async def reserve(
    pool: asyncpg.Pool, order_id: str, items: dict[str, int]
) -> tuple[Reservation, bool]:
    """Reserve all items or none. Returns (reservation, created).

    created=False means this order_id was already reserved (idempotent replay).
    Raises SoldOut / UnknownSku after rolling back everything.
    """
    async with pool.acquire() as conn:
        try:
            async with conn.transaction():
                # The primary key on order_id is the idempotency guard. For two concurrent requests
                # with the same order_id the second INSERT blocks until the first transaction ends:
                # if it committed we get no row back (replay), if it rolled back we proceed.
                inserted = await conn.fetchval(
                    "INSERT INTO inventory.reservations (order_id) VALUES ($1) "
                    "ON CONFLICT (order_id) DO NOTHING RETURNING order_id",
                    order_id,
                )
                if inserted is None:
                    return await fetch_reservation(conn, order_id), False  # type: ignore[return-value]

                # Sorted SKUs give every transaction the same lock order, so no deadlocks.
                for sku, qty in sorted(items.items()):
                    # The check and the decrement are ONE atomic statement. Postgres locks the row
                    # and re-evaluates `available >= qty` against the latest committed value, so
                    # two buyers of the last pair cannot both win (a SELECT followed by an UPDATE
                    # could).
                    updated = await conn.fetchval(
                        "UPDATE inventory.stock SET available = available - $2, updated_at = now() "
                        "WHERE sku = $1 AND available >= $2 RETURNING sku",
                        sku,
                        qty,
                    )
                    if updated is None:
                        raise SoldOut(sku)
                await conn.executemany(
                    "INSERT INTO inventory.reservation_items (order_id, sku, qty) "
                    "VALUES ($1, $2, $3)",
                    [(order_id, sku, qty) for sku, qty in items.items()],
                )
                return await fetch_reservation(conn, order_id), True  # type: ignore[return-value]
        except SoldOut as exc:
            # Transaction is rolled back; tell "no such product" apart from "sold out".
            exists = await conn.fetchval("SELECT 1 FROM inventory.stock WHERE sku = $1", exc.sku)
            if exists is None:
                raise UnknownSku(exc.sku) from exc
            raise


async def release(pool: asyncpg.Pool, order_id: str) -> Reservation | None:
    """Give stock back exactly once. Returns the current reservation, None if unknown."""
    async with pool.acquire() as conn, conn.transaction():
        # Only the transaction that flips reserved -> released returns stock; a concurrent
        # duplicate waits on the row lock, then matches 0 rows.
        flipped = await conn.fetchval(
            "UPDATE inventory.reservations SET status = 'released', updated_at = now() "
            "WHERE order_id = $1 AND status = 'reserved' RETURNING order_id",
            order_id,
        )
        if flipped is not None:
            await conn.execute(
                "UPDATE inventory.stock s SET available = s.available + i.qty, updated_at = now() "
                "FROM inventory.reservation_items i WHERE i.order_id = $1 AND s.sku = i.sku",
                order_id,
            )
        return await fetch_reservation(conn, order_id)


async def commit(pool: asyncpg.Pool, order_id: str) -> Reservation | None:
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute(
            "UPDATE inventory.reservations SET status = 'committed', updated_at = now() "
            "WHERE order_id = $1 AND status = 'reserved'",
            order_id,
        )
        return await fetch_reservation(conn, order_id)


async def get_stock(pool: asyncpg.Pool, sku: str) -> int | None:
    return await pool.fetchval("SELECT available FROM inventory.stock WHERE sku = $1", sku)
