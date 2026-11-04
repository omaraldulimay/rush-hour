import logging
import time

import pytest

from libs.common.events import GROUP, STREAM
from services.order_worker.app.worker import RETRY_KEY
from tests.conftest import Stack

LATER = time.time() + 10_000  # "now" far in the future so every backoff has elapsed


def emails(caplog: pytest.LogCaptureFixture, order_id: str) -> int:
    return sum(
        1
        for r in caplog.records
        if r.getMessage() == "confirmation e-mail sent" and r.order_id == order_id  # type: ignore[attr-defined]
    )


async def pending_order(stack: Stack, qty: int = 1) -> str:
    await stack.add_product("A", stock=10)
    await stack.set_fault(error_rate=1)
    body = (await stack.buy("A", qty=qty)).json()
    assert body["status"] == "payment_pending"
    return body["order_id"]


async def test_paid_order_event_confirms_and_commits(
    stack: Stack, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    await stack.add_product("A", stock=5)
    order_id = (await stack.buy("A")).json()["order_id"]

    assert await stack.worker.consume_once() == 1

    assert emails(caplog, order_id) == 1
    status = await stack.pool.fetchval(
        "SELECT status FROM inventory.reservations WHERE order_id=$1", order_id
    )
    assert status == "committed"
    assert await stack.redis.xpending(STREAM, GROUP) == {
        "pending": 0, "min": None, "max": None, "consumers": [],
    }  # fmt: skip


async def test_pending_order_is_paid_on_retry(
    stack: Stack, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    order_id = await pending_order(stack)
    await stack.worker.consume_once()  # schedules the retry
    assert await stack.redis.zscore(RETRY_KEY, order_id) is not None
    assert await stack.worker.process_due() == 0  # backoff has not elapsed yet

    await stack.set_fault()  # provider recovered
    assert await stack.worker.process_due(now=LATER) == 1
    assert (await stack.checkout.get(f"/orders/{order_id}")).json()["status"] == "paid"

    await stack.worker.consume_once()  # the "paid" event published by the retry
    assert emails(caplog, order_id) == 1
    assert await stack.redis.zscore(RETRY_KEY, order_id) is None


async def test_final_failure_marks_failed_and_releases_stock(stack: Stack) -> None:
    order_id = await pending_order(stack, qty=3)
    assert await stack.stock("A") == 7
    await stack.worker.consume_once()

    for attempt in (1, 2, 3):  # max_attempts is 3 in the test settings
        assert await stack.worker.process_due(now=LATER) == 1
        order = (await stack.checkout.get(f"/orders/{order_id}")).json()
        assert order["payment_attempts"] == attempt

    assert order["status"] == "failed"
    assert order["failure_reason"] == "payment_unavailable"
    assert await stack.stock("A") == 10
    assert await stack.redis.zscore(RETRY_KEY, order_id) is None
    assert await stack.worker.process_due(now=LATER) == 0

    await stack.worker.consume_once()  # the "failed" event: releasing again changes nothing
    assert await stack.stock("A") == 10


async def test_retries_back_off_exponentially(stack: Stack) -> None:
    order_id = await pending_order(stack)
    await stack.worker.consume_once()
    delays = []
    for _ in range(2):
        before = time.time()
        await stack.worker.process_due(now=LATER)
        delays.append(round(await stack.redis.zscore(RETRY_KEY, order_id) - before))
    # base 10 s: 10 -> (after attempt 1) 20 -> (after attempt 2) 40
    assert [stack.worker.backoff(n) for n in range(5)] == [10, 20, 40, 80, 100]
    assert delays == [20, 40]


async def test_declined_on_retry_fails_order_and_releases(stack: Stack) -> None:
    order_id = await pending_order(stack)
    await stack.worker.consume_once()
    await stack.set_fault(decline_rate=1)

    await stack.worker.process_due(now=LATER)

    order = (await stack.checkout.get(f"/orders/{order_id}")).json()
    assert (order["status"], order["failure_reason"]) == ("failed", "payment_declined")
    assert await stack.stock("A") == 10


async def test_duplicate_event_is_handled_once(
    stack: Stack, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    await stack.add_product("A", stock=5)
    order_id = (await stack.buy("A")).json()["order_id"]
    await stack.redis.xadd(STREAM, {"order_id": order_id, "status": "paid"})  # duplicate delivery

    assert await stack.worker.consume_once() == 2
    await stack.worker.handle_event({"order_id": order_id, "status": "paid"})

    assert emails(caplog, order_id) == 1
    assert await stack.stock("A") == 4


async def test_duplicate_pending_event_does_not_push_retry_out(stack: Stack) -> None:
    order_id = await pending_order(stack)
    await stack.worker.handle_event({"order_id": order_id})
    first = await stack.redis.zscore(RETRY_KEY, order_id)
    await stack.worker.handle_event({"order_id": order_id})
    assert await stack.redis.zscore(RETRY_KEY, order_id) == first


async def test_unacked_event_is_redelivered_after_failure(stack: Stack) -> None:
    await stack.add_product("A", stock=5)
    order_id = (await stack.buy("A")).json()["order_id"]
    real = stack.worker.inventory
    stack.worker.inventory = None  # type: ignore[assignment]  # makes handling blow up
    assert await stack.worker.consume_once() == 0  # failed -> not acked
    stack.worker.inventory = real

    assert await stack.worker.recover_pending() == 1
    status = await stack.pool.fetchval(
        "SELECT status FROM inventory.reservations WHERE order_id=$1", order_id
    )
    assert status == "committed"


async def test_event_for_unknown_order_is_ignored(stack: Stack) -> None:
    await stack.redis.xadd(STREAM, {"order_id": "ghost", "status": "paid"})
    assert await stack.worker.consume_once() == 1
