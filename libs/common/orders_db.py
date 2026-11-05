"""SQL for the `orders` schema, shared by checkout (owner) and the order worker."""

from typing import Any

import asyncpg
from prometheus_client import Counter

# Incremented by whoever moves an order to `paid` (checkout or the worker retry); sum() over both.
REVENUE_CENTS = Counter("orders_revenue_cents_total", "Value of orders that became paid, in cents")

Order = dict[str, Any]


async def _load(conn: asyncpg.Connection, row: asyncpg.Record | None) -> Order | None:
    if row is None:
        return None
    items = await conn.fetch(
        "SELECT sku, qty, unit_price_cents FROM orders.order_items "
        "WHERE order_id = $1 ORDER BY sku",
        row["order_id"],
    )
    return {
        "order_id": row["order_id"],
        "status": row["status"],
        "total_cents": row["total_cents"],
        "currency": row["currency"],
        "payment_attempts": row["payment_attempts"],
        "failure_reason": row["failure_reason"],
        "created_at": row["created_at"].isoformat(),
        "items": [dict(i) for i in items],
    }


async def get_order(pool: asyncpg.Pool, order_id: str) -> Order | None:
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM orders.orders WHERE order_id = $1", order_id)
        return await _load(conn, row)


async def get_order_by_key(pool: asyncpg.Pool, key: str) -> Order | None:
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM orders.orders WHERE idempotency_key = $1", key)
        return await _load(conn, row)


async def create_order(
    pool: asyncpg.Pool,
    *,
    order_id: str,
    cart_id: str,
    idempotency_key: str | None,
    total_cents: int,
    items: list[dict[str, Any]],
) -> tuple[Order, bool]:
    """Insert an order in status `created`. Returns (order, created); created is False when the
    idempotency key already belongs to an order (also for concurrent duplicates: the unique
    index makes the second insert wait for the first and then do nothing)."""
    async with pool.acquire() as conn, conn.transaction():
        inserted = await conn.fetchval(
            """INSERT INTO orders.orders (order_id, cart_id, idempotency_key, status, total_cents)
               VALUES ($1, $2, $3, 'created', $4)
               ON CONFLICT (idempotency_key) DO NOTHING
               RETURNING order_id""",
            order_id,
            cart_id,
            idempotency_key,
            total_cents,
        )
        if inserted is None:
            row = await conn.fetchrow(
                "SELECT * FROM orders.orders WHERE idempotency_key = $1", idempotency_key
            )
            return await _load(conn, row), False  # type: ignore[return-value]
        await conn.executemany(
            "INSERT INTO orders.order_items (order_id, sku, qty, unit_price_cents) "
            "VALUES ($1, $2, $3, $4)",
            [(order_id, i["sku"], i["qty"], i["unit_price_cents"]) for i in items],
        )
        row = await conn.fetchrow("SELECT * FROM orders.orders WHERE order_id = $1", order_id)
        return await _load(conn, row), True  # type: ignore[return-value]


async def set_status(
    pool: asyncpg.Pool,
    order_id: str,
    new_status: str,
    *,
    expected: str,
    reason: str | None = None,
) -> bool:
    """Compare-and-set on the status; False means someone else already moved the order."""
    row = await pool.fetchval(
        """UPDATE orders.orders
           SET status = $2, updated_at = now(), failure_reason = COALESCE($4, failure_reason)
           WHERE order_id = $1 AND status = $3
           RETURNING 1""",
        order_id,
        new_status,
        expected,
        reason,
    )
    return row is not None


async def increment_attempts(pool: asyncpg.Pool, order_id: str) -> int | None:
    """Count a payment retry; None if the order is no longer payment_pending."""
    return await pool.fetchval(
        """UPDATE orders.orders SET payment_attempts = payment_attempts + 1, updated_at = now()
           WHERE order_id = $1 AND status = 'payment_pending'
           RETURNING payment_attempts""",
        order_id,
    )


async def mark_confirmed(pool: asyncpg.Pool, order_id: str) -> bool:
    """True exactly once per order: used to send the confirmation e-mail only once."""
    row = await pool.fetchval(
        "UPDATE orders.orders SET confirmed_at = now() "
        "WHERE order_id = $1 AND confirmed_at IS NULL RETURNING 1",
        order_id,
    )
    return row is not None


async def claim_abandoned(pool: asyncpg.Pool, *, older_than_s: float, limit: int) -> list[str]:
    """Compare-and-set `created` -> `failed (checkout_abandoned)` for up to `limit` stale orders.

    One statement does claim and CAS: rows locked by a concurrent sweeper or by a checkout that
    is finishing right now are skipped (SKIP LOCKED), and a checkout that already moved the order
    no longer matches `status = 'created'`. Returns the ids this call failed."""
    rows = await pool.fetch(
        """UPDATE orders.orders SET status = 'failed', failure_reason = 'checkout_abandoned',
                                    updated_at = now()
           WHERE order_id IN (
               SELECT order_id FROM orders.orders
               WHERE status = 'created' AND updated_at < now() - make_interval(secs => $1)
               ORDER BY updated_at LIMIT $2
               FOR UPDATE SKIP LOCKED)
             AND status = 'created'
           RETURNING order_id""",
        older_than_s,
        limit,
    )
    return [r["order_id"] for r in rows]


async def pending_older_than(
    pool: asyncpg.Pool, *, older_than_s: float, after: str, limit: int
) -> list[str]:
    """One keyset page (ordered by order_id) of `payment_pending` orders not updated recently."""
    rows = await pool.fetch(
        """SELECT order_id FROM orders.orders
           WHERE status = 'payment_pending' AND order_id > $3
             AND updated_at < now() - make_interval(secs => $1)
           ORDER BY order_id LIMIT $2""",
        older_than_s,
        limit,
        after,
    )
    return [r["order_id"] for r in rows]


async def mark_released(pool: asyncpg.Pool, order_id: str) -> bool:
    """Record that the reservation was given back. True only for the call that set it."""
    row = await pool.fetchval(
        "UPDATE orders.orders SET released_at = now() "
        "WHERE order_id = $1 AND released_at IS NULL RETURNING 1",
        order_id,
    )
    return row is not None


async def claim_unreleased(pool: asyncpg.Pool, *, older_than_s: float, limit: int) -> list[str]:
    """Failed orders whose release was never recorded. Claiming bumps `updated_at` (a lease), so
    concurrent sweepers skip them (SKIP LOCKED) and a crashed one is retried after the delay."""
    rows = await pool.fetch(
        """UPDATE orders.orders SET updated_at = now()
           WHERE order_id IN (
               SELECT order_id FROM orders.orders
               WHERE status = 'failed' AND released_at IS NULL
                 AND updated_at < now() - make_interval(secs => $1)
               ORDER BY updated_at LIMIT $2
               FOR UPDATE SKIP LOCKED)
           RETURNING order_id""",
        older_than_s,
        limit,
    )
    return [r["order_id"] for r in rows]


async def set_refund_required(pool: asyncpg.Pool, order_id: str) -> None:
    await pool.execute(
        "UPDATE orders.orders SET failure_reason = 'refund_required', updated_at = now() "
        "WHERE order_id = $1 AND status = 'failed'",
        order_id,
    )


async def mark_refunded(pool: asyncpg.Pool, order_id: str) -> bool:
    row = await pool.fetchval(
        "UPDATE orders.orders SET failure_reason = 'refunded', updated_at = now() "
        "WHERE order_id = $1 AND status = 'failed' "
        "AND failure_reason IS DISTINCT FROM 'refunded' RETURNING 1",
        order_id,
    )
    return row is not None


async def refund_required_after(pool: asyncpg.Pool, *, after: str, limit: int) -> list[str]:
    """One keyset page of failed orders whose customer still needs a refund."""
    rows = await pool.fetch(
        "SELECT order_id FROM orders.orders "
        "WHERE status = 'failed' AND failure_reason = 'refund_required' AND order_id > $1 "
        "ORDER BY order_id LIMIT $2",
        after,
        limit,
    )
    return [r["order_id"] for r in rows]
