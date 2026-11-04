from tests.conftest import Stack


async def pay(stack: Stack, order_id: str = "o-1"):
    return await stack.payment.post("/payments", json={"order_id": order_id, "amount_cents": 500})


async def test_default_is_paid_and_idempotent(stack: Stack) -> None:
    assert (await pay(stack)).json()["status"] == "paid"
    await stack.set_fault(decline_rate=1)
    assert (await pay(stack)).json()["status"] == "paid"  # same order_id: remembered
    assert (await pay(stack, "o-2")).json()["status"] == "declined"


async def test_error_rate_returns_503_and_is_not_remembered(stack: Stack) -> None:
    await stack.set_fault(error_rate=1)
    assert (await pay(stack)).status_code == 503
    await stack.set_fault()
    assert (await pay(stack)).json()["status"] == "paid"


async def test_fault_admin_roundtrip_and_validation(stack: Stack) -> None:
    await stack.set_fault(latency_ms=5, error_rate=0.25, decline_rate=0.5)
    assert (await stack.payment.get("/admin/fault")).json() == {
        "latency_ms": 5,
        "error_rate": 0.25,
        "decline_rate": 0.5,
    }
    bad = await stack.payment.post(
        "/admin/fault", json={"latency_ms": 0, "error_rate": 2, "decline_rate": 0}
    )
    assert bad.status_code == 422
