import asyncio
import time

from libs.common import orders_db
from libs.common.events import STREAM
from services.order_worker.app.worker import RETRY_KEY
from tests.conftest import Stack

LATER = time.time() + 10_000


async def crashed_checkout(stack: Stack, order_id: str, qty: int = 2, *, reserve: bool = True):
    """An order stuck in `created`, as if checkout died after (or before) reserving."""
    await orders_db.create_order(
        stack.pool,
        order_id=order_id,
        cart_id="c",
        idempotency_key=None,
        total_cents=100,
        items=[{"sku": "A", "qty": qty, "unit_price_cents": 50}],
    )
    if reserve:
        resp = await stack.inventory.post(
            "/reservations", json={"order_id": order_id, "items": [{"sku": "A", "qty": qty}]}
        )
        assert resp.status_code == 201


async def age(stack: Stack, order_id: str, seconds: int = 3600) -> None:
    await stack.pool.execute(
        "UPDATE orders.orders SET updated_at = now() - make_interval(secs => $2), "
        "created_at = now() - make_interval(secs => $2) WHERE order_id = $1",
        order_id,
        float(seconds),
    )


async def row(stack: Stack, order_id: str):
    return await stack.pool.fetchrow("SELECT * FROM orders.orders WHERE order_id=$1", order_id)


async def failed_events(stack: Stack) -> list[str]:
    msgs = await stack.redis.xrange(STREAM)
    return [f["order_id"] for _, f in msgs if f["status"] == "failed"]


async def test_stale_created_order_is_failed_and_stock_returns_once(stack: Stack) -> None:
    await stack.add_product("A", stock=10)
    await crashed_checkout(stack, "stale")
    await crashed_checkout(stack, "fresh")
    await age(stack, "stale")
    assert await stack.stock("A") == 6

    assert (await stack.worker.sweep())["abandoned"] == 1
    assert (await stack.worker.sweep())["abandoned"] == 0

    stale = await row(stack, "stale")
    assert (stale["status"], stale["failure_reason"]) == ("failed", "checkout_abandoned")
    assert (await row(stack, "fresh"))["status"] == "created"
    assert await stack.stock("A") == 8  # only the stale order's 2 pieces came back, once
    assert await failed_events(stack) == ["stale"]


async def test_never_reserved_stale_order_is_failed_without_error(stack: Stack) -> None:
    await stack.add_product("A", stock=10)
    await crashed_checkout(stack, "x", reserve=False)
    await age(stack, "x")
    assert (await stack.worker.sweep())["abandoned"] == 1
    assert (await row(stack, "x"))["status"] == "failed"
    assert await stack.stock("A") == 10


async def test_checkout_finishing_concurrently_wins(stack: Stack) -> None:
    await stack.add_product("A", stock=10)
    await crashed_checkout(stack, "slow")
    await age(stack, "slow")

    # Checkout holds the row lock while it finishes: the sweeper must skip it, not overwrite.
    async with stack.pool.acquire() as conn, conn.transaction():
        await conn.execute("SELECT 1 FROM orders.orders WHERE order_id='slow' FOR UPDATE")
        assert (await stack.worker.sweep())["abandoned"] == 0
        await conn.execute("UPDATE orders.orders SET status='paid', updated_at=now()")
    assert (await stack.worker.sweep())["abandoned"] == 0  # CAS: no longer `created`
    assert (await row(stack, "slow"))["status"] == "paid"
    assert await stack.stock("A") == 8  # still reserved for the paid order
    assert await failed_events(stack) == []


async def test_orphaned_pending_order_is_rescheduled_and_paid(stack: Stack) -> None:
    await stack.add_product("A", stock=10)
    await stack.set_fault(error_rate=1)
    order_id = (await stack.buy("A")).json()["order_id"]  # payment_pending, event not consumed
    await stack.worker.consume_once()
    assert await stack.redis.zrem(RETRY_KEY, order_id)  # worker "crashed" after ZREM
    assert (await stack.worker.sweep())["rescheduled"] == 0  # too fresh

    await age(stack, order_id)
    assert (await stack.worker.sweep())["rescheduled"] == 1
    assert await stack.redis.zscore(RETRY_KEY, order_id) is not None
    assert (await row(stack, order_id))["status"] == "payment_pending"  # sweep never pays

    await stack.set_fault()
    assert await stack.worker.process_due() == 1  # due immediately
    assert (await row(stack, order_id))["status"] == "paid"


async def test_pending_order_with_retry_entry_is_not_duplicated(stack: Stack) -> None:
    await stack.add_product("A", stock=10)
    await stack.set_fault(error_rate=1)
    order_id = (await stack.buy("A")).json()["order_id"]
    await stack.worker.consume_once()
    score = await stack.redis.zscore(RETRY_KEY, order_id)
    await age(stack, order_id)

    assert (await stack.worker.sweep())["rescheduled"] == 0
    assert await stack.redis.zscore(RETRY_KEY, order_id) == score  # untouched
    assert await stack.redis.zcard(RETRY_KEY) == 1


async def test_batches_drain_backlog_and_concurrent_sweeps_split_it(stack: Stack) -> None:
    await stack.add_product("A", stock=100)
    stack.worker.settings = type(stack.worker.settings)(
        **{**stack.worker.settings.__dict__, "sweep_batch_size": 3}
    )
    ids = [f"o{i}" for i in range(10)]
    for order_id in ids:
        await crashed_checkout(stack, order_id, qty=1)
        await age(stack, order_id)

    results = await asyncio.gather(*(stack.worker.sweep() for _ in range(3)))

    assert sum(r["abandoned"] for r in results) == 10  # each order handled by one sweeper
    assert sorted(await failed_events(stack)) == sorted(ids)
    assert await stack.stock("A") == 100
    assert (
        await stack.pool.fetchval("SELECT count(*) FROM orders.orders WHERE status='created'") == 0
    )


def sweep_during_payment(monkeypatch, stack: Stack, after_pay=None) -> None:
    """Make checkout slow: the sweeper declares the order abandoned while payment is running."""
    from services.checkout.app.service import CheckoutService

    original = CheckoutService._pay

    async def slow_pay(self, order_id: str, amount_cents: int) -> str:
        outcome = await original(self, order_id, amount_cents)
        await age(stack, order_id)
        await stack.worker.sweep()
        if after_pay:
            await after_pay()
        return outcome

    monkeypatch.setattr(CheckoutService, "_pay", slow_pay)


async def test_checkout_losing_race_after_payment_refunds_and_answers_409(
    stack: Stack, monkeypatch
) -> None:
    await stack.add_product("A", stock=5)
    sweep_during_payment(monkeypatch, stack)

    resp = await stack.buy("A", qty=2)

    assert resp.status_code == 409
    body = resp.json()
    assert body["error"] == "order_abandoned"
    assert body["order"]["status"] == "failed"
    order = await row(stack, body["order"]["order_id"])
    assert order["failure_reason"] == "refunded"
    assert await stack.stock("A") == 5  # released by the sweeper, not double-released
    assert await failed_events(stack) == [order["order_id"]]  # no paid/pending event
    assert (await stack.checkout.get("/carts/cart-1")).json()["items"]  # cart kept


async def test_failed_refund_is_retried_by_the_sweep(stack: Stack, monkeypatch) -> None:
    await stack.add_product("A", stock=5)
    sweep_during_payment(monkeypatch, stack, after_pay=lambda: stack.set_fault(error_rate=1))

    resp = await stack.buy("A")
    order_id = resp.json()["order"]["order_id"]
    assert resp.status_code == 409
    assert (await row(stack, order_id))["failure_reason"] == "refund_required"

    assert (await stack.worker.sweep())["refunded"] == 0  # provider still down
    assert (await row(stack, order_id))["failure_reason"] == "refund_required"

    await stack.set_fault()
    assert (await stack.worker.sweep())["refunded"] == 1
    assert (await row(stack, order_id))["failure_reason"] == "refunded"
    assert (await stack.worker.sweep())["refunded"] == 0


async def test_late_payment_on_failed_order_is_refunded_by_worker(
    stack: Stack, monkeypatch
) -> None:
    await stack.add_product("A", stock=5)
    await stack.set_fault(error_rate=1)
    order_id = (await stack.buy("A")).json()["order_id"]
    await stack.worker.consume_once()  # retry scheduled

    original = orders_db.increment_attempts

    async def fail_meanwhile(pool, oid):  # order gets failed while the payment is in flight
        attempts = await original(pool, oid)
        await orders_db.set_status(
            pool, oid, "failed", expected="payment_pending", reason="checkout_abandoned"
        )
        return attempts

    monkeypatch.setattr(orders_db, "increment_attempts", fail_meanwhile)
    await stack.set_fault()  # provider recovered: the retry is charged
    assert await stack.worker.process_due(now=LATER) == 1

    order = await row(stack, order_id)
    assert (order["status"], order["failure_reason"]) == ("failed", "refunded")


async def test_failed_order_without_release_is_released_once(stack: Stack) -> None:
    await stack.add_product("A", stock=10)
    await crashed_checkout(stack, "half", qty=3)
    await stack.pool.execute("UPDATE orders.orders SET status='failed'")  # crash before release
    assert await stack.stock("A") == 7

    assert (await stack.worker.sweep())["released"] == 0  # too fresh
    await age(stack, "half")
    results = await asyncio.gather(stack.worker.sweep(), stack.worker.sweep())

    assert sum(r["released"] for r in results) == 1
    assert (await row(stack, "half"))["released_at"] is not None
    assert await stack.stock("A") == 10
    assert (await stack.worker.sweep())["released"] == 0
