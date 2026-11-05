import asyncio
import time

from tests.conftest import Stack


async def order(stack: Stack, order_id: str) -> dict:
    return (await stack.checkout.get(f"/orders/{order_id}")).json()


async def test_cart_crud(stack: Stack) -> None:
    await stack.checkout.put("/carts/c1/items", json={"sku": "A", "qty": 2})
    await stack.checkout.put("/carts/c1/items", json={"sku": "B", "qty": 1})
    cart = (await stack.checkout.get("/carts/c1")).json()
    assert cart["items"] == [{"sku": "A", "qty": 2}, {"sku": "B", "qty": 1}]
    cart = (await stack.checkout.delete("/carts/c1/items/A")).json()
    assert cart["items"] == [{"sku": "B", "qty": 1}]
    assert (await stack.checkout.get("/carts/none")).json()["items"] == []
    bad = await stack.checkout.put("/carts/c1/items", json={"sku": "A", "qty": 0})
    assert bad.status_code == 422


async def test_happy_path(stack: Stack) -> None:
    await stack.add_product("A", stock=5, price_cents=12_000)
    await stack.add_product("B", stock=5, price_cents=2_500)
    await stack.checkout.put("/carts/c1/items", json={"sku": "A", "qty": 2})
    await stack.checkout.put("/carts/c1/items", json={"sku": "B", "qty": 1})

    resp = await stack.checkout.post("/checkout", json={"cart_id": "c1"})

    assert resp.status_code == 201
    body = resp.json()
    assert body["status"] == "paid"
    assert body["total_cents"] == 2 * 12_000 + 2_500  # prices come from the catalog
    assert await stack.stock("A") == 3
    assert (await order(stack, body["order_id"]))["status"] == "paid"
    assert (await stack.checkout.get("/carts/c1")).json()["items"] == []
    events = await stack.redis.xrange("orders")
    assert [f for _, f in events] == [{"order_id": body["order_id"], "status": "paid"}]


async def test_client_cannot_set_prices(stack: Stack) -> None:
    await stack.add_product("A", stock=5, price_cents=9_900)
    await stack.checkout.put("/carts/c1/items", json={"sku": "A", "qty": 1, "price_cents": 1})
    body = (await stack.checkout.post("/checkout", json={"cart_id": "c1"})).json()
    assert body["total_cents"] == 9_900


async def test_empty_cart_and_unknown_sku(stack: Stack) -> None:
    assert (await stack.checkout.post("/checkout", json={"cart_id": "none"})).status_code == 400
    resp = await stack.buy("GHOST")
    assert resp.status_code == 422
    assert resp.json() == {"error": "unknown_sku", "sku": "GHOST"}


async def test_sold_out_returns_409_and_marks_order_failed(stack: Stack) -> None:
    await stack.add_product("A", stock=1)
    assert (await stack.buy("A", cart_id="c1")).status_code == 201

    resp = await stack.buy("A", cart_id="c2")

    assert resp.status_code == 409
    assert resp.json() == {"error": "sold_out", "sku": "A"}
    assert await stack.stock("A") == 0
    statuses = await stack.pool.fetch("SELECT status FROM orders.orders ORDER BY created_at")
    assert [r["status"] for r in statuses] == ["paid", "failed"]


async def test_declined_payment_releases_stock(stack: Stack) -> None:
    await stack.add_product("A", stock=5)
    await stack.set_fault(decline_rate=1)

    resp = await stack.buy("A", qty=2)

    assert resp.status_code == 201
    assert resp.json()["status"] == "failed"
    assert resp.json()["failure_reason"] == "payment_declined"
    assert await stack.stock("A") == 5


async def test_payment_timeout_gives_payment_pending(stack: Stack) -> None:
    await stack.add_product("A", stock=5)
    await stack.set_fault(latency_ms=3000)

    started = time.perf_counter()
    resp = await stack.buy("A")

    assert time.perf_counter() - started < 2
    assert resp.status_code == 201
    assert resp.json()["status"] == "payment_pending"
    assert await stack.stock("A") == 4  # reservation is held while payment is pending


async def test_payment_503_gives_payment_pending(stack: Stack) -> None:
    await stack.add_product("A", stock=5)
    await stack.set_fault(error_rate=1)
    assert (await stack.buy("A")).json()["status"] == "payment_pending"


async def test_circuit_breaker_opens_and_checkout_answers_fast(stack: Stack) -> None:
    await stack.add_product("A", stock=20)
    await stack.set_fault(latency_ms=5000)  # every call hits the 0.3 s test timeout

    slow = []
    for i in range(3):  # threshold in the test settings is 3
        started = time.perf_counter()
        resp = await stack.buy("A", cart_id=f"c{i}")
        slow.append(time.perf_counter() - started)
        assert resp.json()["status"] == "payment_pending"
    assert min(slow) >= 0.25
    assert stack.checkout_app.state.breaker.state == "open"

    started = time.perf_counter()
    resp = await stack.buy("A", cart_id="c-fast")
    fast = time.perf_counter() - started

    assert resp.json()["status"] == "payment_pending"
    assert fast < 0.15
    metrics = (await stack.checkout.get("/metrics")).text
    assert 'circuit_breaker_state{name="payment"} 2.0' in metrics


async def test_idempotency_key_returns_same_order(stack: Stack) -> None:
    await stack.add_product("A", stock=5)
    first = await stack.buy("A", **{"Idempotency-Key": "key-1"})
    second = await stack.checkout.post(
        "/checkout", json={"cart_id": "cart-1"}, headers={"Idempotency-Key": "key-1"}
    )

    assert first.status_code == 201
    assert second.status_code == 200
    assert first.json()["order_id"] == second.json()["order_id"]
    assert await stack.stock("A") == 4


async def test_concurrent_requests_with_same_idempotency_key_create_one_order(
    stack: Stack,
) -> None:
    await stack.add_product("A", stock=5)
    await stack.checkout.put("/carts/c1/items", json={"sku": "A", "qty": 1})

    responses = await asyncio.gather(
        *(
            stack.checkout.post(
                "/checkout", json={"cart_id": "c1"}, headers={"Idempotency-Key": "k"}
            )
            for _ in range(10)
        )
    )

    assert len({r.json()["order_id"] for r in responses if r.status_code in (200, 201)}) == 1
    assert await stack.pool.fetchval("SELECT count(*) FROM orders.orders") == 1
    assert await stack.stock("A") == 4


async def test_unknown_order_is_404(stack: Stack) -> None:
    assert (await stack.checkout.get("/orders/nope")).status_code == 404


async def test_fault_error_rate_returns_500_without_side_effects(stack: Stack) -> None:
    await stack.add_product("A", stock=5)
    await stack.checkout.put("/carts/c1/items", json={"sku": "A", "qty": 1})
    object.__setattr__(stack.checkout_settings, "fault_error_rate", 1.0)
    try:
        resp = await stack.checkout.post("/checkout", json={"cart_id": "c1"})
    finally:
        object.__setattr__(stack.checkout_settings, "fault_error_rate", 0.0)
    assert resp.status_code == 500
    assert resp.json() == {"error": "injected_fault"}
    assert await stack.stock("A") == 5
    assert (await stack.checkout.post("/checkout", json={"cart_id": "c1"})).status_code == 201
