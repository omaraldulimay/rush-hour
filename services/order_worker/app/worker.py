"""Consumes the `orders` stream. Handlers are idempotent because streams deliver at-least-once."""

import asyncio
import contextlib
import logging
import time
from typing import Any

import asyncpg
import httpx
from prometheus_client import Counter
from redis.asyncio import Redis
from redis.exceptions import ResponseError

from libs.common import orders_db
from libs.common.events import GROUP, STREAM, publish_order_event
from libs.common.refunds import refund_payment
from services.order_worker.app.settings import Settings

log = logging.getLogger("order_worker")

RETRY_KEY = "orders:payment-retries"  # sorted set: order_id -> due time (epoch seconds)

EVENTS = Counter("worker_events_total", "Stream events handled", ["status"])
RETRIES = Counter("worker_payment_retries_total", "Payment retries", ["outcome"])
EMAILS = Counter("worker_confirmation_emails_total", "Confirmation e-mails sent")
SWEPT = Counter("order_worker_swept_total", "Stale orders repaired by the sweeper", ["action"])


class OrderWorker:
    def __init__(
        self,
        settings: Settings,
        pool: asyncpg.Pool,
        redis: Redis,
        inventory: httpx.AsyncClient,
        payment: httpx.AsyncClient,
    ) -> None:
        self.settings, self.pool, self.redis = settings, pool, redis
        self.inventory, self.payment = inventory, payment

    # --- stream consumption -------------------------------------------------------------

    async def setup(self) -> None:
        try:
            await self.redis.xgroup_create(STREAM, GROUP, id="0", mkstream=True)
        except ResponseError as exc:
            if "BUSYGROUP" not in str(exc):
                raise

    async def consume_once(self, block_ms: int | None = None, count: int = 20) -> int:
        """Read new events, handle them, ack after success. Returns the number handled."""
        return await self._read(">", block_ms, count)

    async def recover_pending(self) -> int:
        """Re-handle events this consumer read but never acked (e.g. it crashed)."""
        return await self._read("0", None, 100)

    async def _read(self, start: str, block_ms: int | None, count: int) -> int:
        response = await self.redis.xreadgroup(
            GROUP, self.settings.consumer_name, {STREAM: start}, count=count, block=block_ms
        )
        handled = 0
        for _stream, messages in response or []:
            for message_id, fields in messages:
                try:
                    await self.handle_event(fields)
                except Exception:
                    # No ack: the event stays pending and is retried by recover_pending().
                    log.exception("event handling failed", extra={"message_id": message_id})
                    continue
                await self.redis.xack(STREAM, GROUP, message_id)
                handled += 1
        return handled

    async def run(self) -> None:
        await self.setup()
        await self.recover_pending()
        sweeper = asyncio.create_task(self.run_sweeper(), name="order-sweeper")
        try:
            await self._loop()
        finally:
            sweeper.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await sweeper

    async def run_sweeper(self) -> None:
        while True:
            try:
                await self.sweep()
            except Exception:
                log.exception("sweep failed")
            await asyncio.sleep(self.settings.sweep_interval_s)

    async def _loop(self) -> None:
        while True:
            try:
                await self.consume_once(block_ms=1000)
                await self.process_due()
            except Exception:
                log.exception("worker loop error, backing off")
                await asyncio.sleep(1)

    # --- event handling -----------------------------------------------------------------

    async def handle_event(self, fields: dict[str, Any]) -> None:
        """Acts on the order's *current* state, not the event payload, so duplicates and
        out-of-order delivery are harmless."""
        order_id = fields["order_id"]
        order = await orders_db.get_order(self.pool, order_id)
        if order is None:
            return
        EVENTS.labels(order["status"]).inc()
        match order["status"]:
            case "paid":
                await self._confirm(order_id)
            case "payment_pending":
                await self._schedule_retry(order_id, order["payment_attempts"])
            case "failed":
                await self._release(order_id)  # idempotent safety net

    async def _confirm(self, order_id: str) -> None:
        resp = await self.inventory.post(f"/reservations/{order_id}/commit")
        resp.raise_for_status()
        if await orders_db.mark_confirmed(self.pool, order_id):
            EMAILS.inc()
            log.info("confirmation e-mail sent", extra={"order_id": order_id})

    async def _release(self, order_id: str) -> bool:
        """True if this call recorded the release (False: already recorded)."""
        resp = await self.inventory.post(f"/reservations/{order_id}/release")
        if resp.status_code not in (200, 404, 409):  # 404: nothing was reserved
            resp.raise_for_status()
        return await orders_db.mark_released(self.pool, order_id)

    # --- payment retries ----------------------------------------------------------------

    def backoff(self, attempts_done: int) -> float:
        s = self.settings
        return min(s.retry_base_delay_s * 2**attempts_done, s.retry_max_delay_s)

    async def _schedule_retry(self, order_id: str, attempts_done: int, *, force: bool = False):
        due = time.time() + self.backoff(attempts_done)
        # nx: a duplicate event must not push an already scheduled retry further out.
        await self.redis.zadd(RETRY_KEY, {order_id: due}, nx=not force)

    async def process_due(self, now: float | None = None) -> int:
        """Retry every payment_pending order whose backoff has elapsed."""
        now = time.time() if now is None else now
        due = await self.redis.zrangebyscore(RETRY_KEY, "-inf", now)
        processed = 0
        for order_id in due:
            # ZREM succeeds for exactly one worker, so a retry is never run twice in parallel.
            if not await self.redis.zrem(RETRY_KEY, order_id):
                continue
            try:
                await self._retry(order_id)
            except Exception:
                log.exception("payment retry crashed", extra={"order_id": order_id})
                await self._schedule_retry(order_id, 0, force=True)
            processed += 1
        return processed

    async def _retry(self, order_id: str) -> None:
        attempts = await orders_db.increment_attempts(self.pool, order_id)
        if attempts is None:  # no longer payment_pending
            return
        order = await orders_db.get_order(self.pool, order_id)
        try:
            async with asyncio.timeout(self.settings.payment_timeout_s):
                resp = await self.payment.post(
                    "/payments",
                    json={"order_id": order_id, "amount_cents": order["total_cents"]},  # type: ignore[index]
                )
            resp.raise_for_status()
            result = resp.json()["status"]
        except Exception as exc:
            log.warning(
                "payment retry failed",
                extra={"order_id": order_id, "attempt": attempts, "error": type(exc).__name__},
            )
            RETRIES.labels("error").inc()
            if attempts >= self.settings.max_attempts:
                await self._give_up(order_id, "payment_unavailable")
            else:
                await self._schedule_retry(order_id, attempts)
            return

        RETRIES.labels(result).inc()
        if result == "paid":
            await self._finish(order_id, "paid", None)
        else:
            await self._give_up(order_id, "payment_declined")

    async def _give_up(self, order_id: str, reason: str) -> None:
        await self._finish(order_id, "failed", reason)
        await self._release(order_id)

    async def _finish(self, order_id: str, status: str, reason: str | None) -> None:
        moved = await orders_db.set_status(
            self.pool, order_id, status, expected="payment_pending", reason=reason
        )
        if moved:
            await publish_order_event(self.redis, order_id, status)
        elif status == "paid":
            order = await orders_db.get_order(self.pool, order_id)
            if order and order["status"] == "failed":
                # The customer was charged for an order that was failed in the meantime.
                await self._refund(order_id)

    async def _refund(self, order_id: str) -> bool:
        """True if this call settled the refund (so concurrent sweepers count it once)."""
        result = await refund_payment(self.payment, order_id, self.settings.payment_timeout_s)
        if result == "error":
            await orders_db.set_refund_required(self.pool, order_id)  # the sweep keeps trying
            return False
        return await orders_db.mark_refunded(self.pool, order_id)

    # --- sweeper: repairs orders a crashed checkout or worker left behind -----------------

    async def sweep(self) -> dict[str, int]:
        """Safe to run in several replicas at once. Returns what this call repaired."""
        return {
            "abandoned": await self._sweep_abandoned(),
            "released": await self._sweep_unreleased(),
            "refunded": await self._sweep_refunds(),
            "rescheduled": await self._sweep_orphans(),
        }

    async def _sweep_abandoned(self) -> int:
        """Checkout died after reserving (or before): fail the order, give the stock back."""
        s, total = self.settings, 0
        while ids := await orders_db.claim_abandoned(
            self.pool, older_than_s=s.stale_created_after_s, limit=s.sweep_batch_size
        ):
            for order_id in ids:
                try:
                    await self._release(order_id)  # 404 = never reserved, harmless
                except Exception:
                    # The "failed" event below makes handle_event() release it again.
                    log.exception("release failed", extra={"order_id": order_id})
                await publish_order_event(self.redis, order_id, "failed")
                SWEPT.labels("abandoned").inc()
                log.warning("abandoned order failed", extra={"order_id": order_id})
            total += len(ids)
            if len(ids) < s.sweep_batch_size:
                break
        return total

    async def _sweep_unreleased(self) -> int:
        """Failed orders whose release was never recorded (a sweeper/checkout died in between)."""
        s, total = self.settings, 0
        while ids := await orders_db.claim_unreleased(
            self.pool, older_than_s=s.orphan_pending_after_s, limit=s.sweep_batch_size
        ):
            for order_id in ids:
                try:
                    if await self._release(order_id):
                        SWEPT.labels("released").inc()
                        total += 1
                except Exception:
                    log.exception("release failed", extra={"order_id": order_id})
            if len(ids) < s.sweep_batch_size:
                break
        return total

    async def _sweep_refunds(self) -> int:
        """Charged customers of failed orders: retry the refund until the provider confirms."""
        s, after, total = self.settings, "", 0
        while ids := await orders_db.refund_required_after(
            self.pool, after=after, limit=s.sweep_batch_size
        ):
            after = ids[-1]
            for order_id in ids:
                if await self._refund(order_id):
                    SWEPT.labels("refunded").inc()
                    total += 1
            if len(ids) < s.sweep_batch_size:
                break
        return total

    async def _sweep_orphans(self) -> int:
        """A worker died after ZREM and before the attempt: put the order back into the retry
        set. Only schedules; the normal retry path does the payment. Pending orders waiting for
        a backoff are old too, so we page through all of them and skip those with an entry."""
        s, after, total = self.settings, "", 0
        while ids := await orders_db.pending_older_than(
            self.pool,
            older_than_s=s.orphan_pending_after_s,
            after=after,
            limit=s.sweep_batch_size,
        ):
            after = ids[-1]
            async with self.redis.pipeline(transaction=False) as pipe:
                for order_id in ids:
                    pipe.zscore(RETRY_KEY, order_id)
                scores = await pipe.execute()
            for order_id, score in zip(ids, scores, strict=True):
                if score is None and await self.redis.zadd(
                    RETRY_KEY, {order_id: time.time()}, nx=True
                ):  # nx: with concurrent sweepers exactly one adds (and counts) it
                    SWEPT.labels("rescheduled").inc()
                    total += 1
                    log.warning("orphaned payment retry rescheduled", extra={"order_id": order_id})
            if len(ids) < s.sweep_batch_size:
                break
        return total
