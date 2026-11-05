"""The checkout flow: price -> reserve -> pay -> order status -> event."""

import asyncio
import logging
import uuid
from typing import Any

import asyncpg
import httpx
from prometheus_client import Counter
from redis.asyncio import Redis

from libs.common import orders_db
from libs.common.events import publish_order_event
from libs.common.refunds import refund_payment
from services.checkout.app.breaker import CircuitBreaker
from services.checkout.app.settings import Settings

log = logging.getLogger("checkout")

ORDERS = Counter("checkout_orders_total", "Orders by resulting status", ["status"])


class CheckoutError(Exception):
    def __init__(self, status_code: int, body: dict[str, Any]) -> None:
        super().__init__(body)
        self.status_code, self.body = status_code, body


def cart_key(cart_id: str) -> str:
    return f"cart:{cart_id}"


class CheckoutService:
    def __init__(
        self,
        settings: Settings,
        pool: asyncpg.Pool,
        redis: Redis,
        catalog: httpx.AsyncClient,
        inventory: httpx.AsyncClient,
        payment: httpx.AsyncClient,
        breaker: CircuitBreaker,
    ) -> None:
        self.settings, self.pool, self.redis = settings, pool, redis
        self.catalog, self.inventory, self.payment, self.breaker = (
            catalog,
            inventory,
            payment,
            breaker,
        )

    async def checkout(self, cart_id: str, idempotency_key: str | None) -> tuple[dict, bool]:
        """Returns (order, created). created=False for an idempotent replay."""
        if idempotency_key and (
            existing := await orders_db.get_order_by_key(self.pool, idempotency_key)
        ):
            return existing, False

        cart = await self.redis.hgetall(cart_key(cart_id))
        if not cart:
            raise CheckoutError(400, {"error": "empty_cart"})
        lines = await self._price(cart)

        order, created = await orders_db.create_order(
            self.pool,
            order_id=str(uuid.uuid4()),
            cart_id=cart_id,
            idempotency_key=idempotency_key,
            total_cents=sum(line["qty"] * line["unit_price_cents"] for line in lines),
            items=lines,
        )
        if not created:
            return order, False
        order_id = order["order_id"]

        await self._reserve(order_id, lines)
        outcome = await self._pay(order_id, order["total_cents"])

        if outcome == "paid":
            status, reason = "paid", None
        elif outcome == "declined":
            status, reason = "failed", "payment_declined"
        else:
            status, reason = "payment_pending", None
        moved = await orders_db.set_status(
            self.pool, order_id, status, expected="created", reason=reason
        )
        if not moved:
            # The sweeper declared this checkout abandoned (and released the stock) while we were
            # slow. Undo any charge; publish nothing and keep the cart.
            await self._undo_charge(order_id, outcome)
            ORDERS.labels("abandoned").inc()
            raise CheckoutError(
                409,
                {
                    "error": "order_abandoned",
                    "order": await orders_db.get_order(self.pool, order_id),
                },
            )
        if status == "failed":
            await self._release(order_id)
        else:
            await self.redis.delete(cart_key(cart_id))

        ORDERS.labels(status).inc()
        if status == "paid":
            orders_db.REVENUE_CENTS.inc(order["total_cents"])
        await publish_order_event(self.redis, order_id, status)
        return await orders_db.get_order(self.pool, order_id), True  # type: ignore[return-value]

    async def _price(self, cart: dict[str, str]) -> list[dict[str, Any]]:
        """Prices always come from the catalog, never from the client."""

        async def fetch(sku: str, qty: str) -> dict[str, Any]:
            try:
                resp = await self.catalog.get(f"/products/{sku}")
            except httpx.HTTPError as exc:
                raise CheckoutError(502, {"error": "catalog_unavailable"}) from exc
            if resp.status_code == 404:
                raise CheckoutError(422, {"error": "unknown_sku", "sku": sku})
            if resp.status_code != 200:
                raise CheckoutError(502, {"error": "catalog_unavailable"})
            return {"sku": sku, "qty": int(qty), "unit_price_cents": resp.json()["price_cents"]}

        return list(await asyncio.gather(*(fetch(sku, qty) for sku, qty in sorted(cart.items()))))

    async def _undo_charge(self, order_id: str, outcome: str) -> None:
        """`pending` may still have been charged (a timeout does not mean "not processed")."""
        if outcome == "declined":
            return
        result = await refund_payment(self.payment, order_id, self.settings.payment_timeout_s)
        if result == "refunded":
            await orders_db.mark_refunded(self.pool, order_id)
        elif result == "error":
            # The worker's sweep retries until the provider confirms the refund.
            await orders_db.set_refund_required(self.pool, order_id)

    async def _fail(self, order_id: str, reason: str) -> None:
        await orders_db.set_status(self.pool, order_id, "failed", expected="created", reason=reason)
        if reason == "sold_out":  # all-or-nothing reservation: nothing to give back
            await orders_db.mark_released(self.pool, order_id)
        ORDERS.labels("failed").inc()
        await publish_order_event(self.redis, order_id, "failed")

    async def _reserve(self, order_id: str, lines: list[dict[str, Any]]) -> None:
        payload = {
            "order_id": order_id,
            "items": [{"sku": line["sku"], "qty": line["qty"]} for line in lines],
        }
        try:
            resp = await self.inventory.post("/reservations", json=payload)
        except httpx.HTTPError as exc:
            await self._fail(order_id, "inventory_unavailable")
            raise CheckoutError(503, {"error": "inventory_unavailable"}) from exc
        if resp.status_code == 409:
            await self._fail(order_id, "sold_out")
            raise CheckoutError(409, resp.json())
        if resp.status_code not in (200, 201):
            await self._fail(order_id, "inventory_error")
            raise CheckoutError(503, {"error": "inventory_unavailable"})

    async def _pay(self, order_id: str, amount_cents: int) -> str:
        """Returns paid | declined | pending. Never raises for provider trouble."""
        if not self.breaker.allow():
            log.warning("payment skipped, circuit open", extra={"order_id": order_id})
            return "pending"
        try:
            # asyncio.timeout is the hard 2 s budget (httpx's own timeout only covers I/O phases).
            async with asyncio.timeout(self.settings.payment_timeout_s):
                resp = await self.payment.post(
                    "/payments", json={"order_id": order_id, "amount_cents": amount_cents}
                )
            resp.raise_for_status()
            status = resp.json()["status"]
        except Exception as exc:
            self.breaker.record_failure()
            log.warning(
                "payment failed",
                extra={"order_id": order_id, "error": type(exc).__name__},
            )
            return "pending"
        self.breaker.record_success()  # a decline is a healthy answer from the provider
        return status

    async def _release(self, order_id: str) -> None:
        try:
            resp = await self.inventory.post(f"/reservations/{order_id}/release")
            if resp.status_code in (200, 404, 409):
                await orders_db.mark_released(self.pool, order_id)
        except httpx.HTTPError:
            # The "failed" event makes the order worker release it again (release is idempotent).
            log.exception("release failed", extra={"order_id": order_id})
