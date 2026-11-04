import asyncio

from tests.conftest import Stack


async def reserve(stack: Stack, order_id: str, items: list[tuple[str, int]]):
    body = {"order_id": order_id, "items": [{"sku": s, "qty": q} for s, q in items]}
    return await stack.inventory.post("/reservations", json=body)


async def test_oversell_500_buyers_for_10_pieces(stack: Stack) -> None:
    await stack.add_product("HOT-1", stock=10)

    responses = await asyncio.gather(
        *(reserve(stack, f"o-{i}", [("HOT-1", 1)]) for i in range(500))
    )

    codes = [r.status_code for r in responses]
    assert codes.count(201) == 10
    assert codes.count(409) == 490
    assert {r.json()["error"] for r in responses if r.status_code == 409} == {"sold_out"}
    assert await stack.stock("HOT-1") == 0
    reserved = await stack.pool.fetchval("SELECT count(*) FROM inventory.reservations")
    assert reserved == 10


async def test_multi_item_reservation_is_all_or_nothing(stack: Stack) -> None:
    await stack.add_product("A", stock=5)
    await stack.add_product("B", stock=3)

    responses = await asyncio.gather(
        *(reserve(stack, f"o-{i}", [("A", 1), ("B", 1)]) for i in range(50))
    )

    assert [r.status_code for r in responses].count(201) == 3
    # A was decremented only by the 3 complete reservations; failed attempts rolled back.
    assert await stack.stock("A") == 2
    assert await stack.stock("B") == 0
    sold_out = [r.json() for r in responses if r.status_code == 409]
    assert {j["sku"] for j in sold_out} == {"B"}


async def test_single_failing_item_leaves_other_items_untouched(stack: Stack) -> None:
    await stack.add_product("A", stock=5)
    await stack.add_product("B", stock=1)

    resp = await reserve(stack, "o-1", [("A", 2), ("B", 2)])

    assert resp.status_code == 409
    assert resp.json() == {"error": "sold_out", "sku": "B"}
    assert await stack.stock("A") == 5
    assert await stack.pool.fetchval("SELECT count(*) FROM inventory.reservations") == 0


async def test_duplicate_skus_in_one_request_are_summed(stack: Stack) -> None:
    await stack.add_product("A", stock=3)
    resp = await reserve(stack, "o-1", [("A", 2), ("A", 2)])
    assert resp.status_code == 409
    assert await stack.stock("A") == 3


async def test_unknown_sku_and_validation(stack: Stack) -> None:
    assert (await reserve(stack, "o-1", [("NOPE", 1)])).status_code == 404
    assert (await reserve(stack, "o-1", [("NOPE", 0)])).status_code == 422
    assert (await stack.inventory.get("/stock/NOPE")).status_code == 404


async def test_reservation_is_idempotent_by_order_id(stack: Stack) -> None:
    await stack.add_product("A", stock=5)

    first = await reserve(stack, "o-1", [("A", 2)])
    second = await reserve(stack, "o-1", [("A", 2)])

    assert (first.status_code, second.status_code) == (201, 200)
    assert first.json() == second.json()
    assert await stack.stock("A") == 3


async def test_concurrent_duplicate_order_id_reserves_once(stack: Stack) -> None:
    await stack.add_product("A", stock=10)

    responses = await asyncio.gather(*(reserve(stack, "same", [("A", 4)]) for _ in range(30)))

    codes = [r.status_code for r in responses]
    assert codes.count(201) == 1
    assert codes.count(200) == 29
    assert await stack.stock("A") == 6


async def test_release_returns_stock_exactly_once(stack: Stack) -> None:
    await stack.add_product("A", stock=5)
    await reserve(stack, "o-1", [("A", 2)])
    assert await stack.stock("A") == 3

    responses = await asyncio.gather(
        *(stack.inventory.post("/reservations/o-1/release") for _ in range(10))
    )

    assert {r.status_code for r in responses} == {200}
    assert await stack.stock("A") == 5
    again = await stack.inventory.post("/reservations/o-1/release")
    assert again.json()["status"] == "released"
    assert await stack.stock("A") == 5


async def test_commit_and_state_conflicts(stack: Stack) -> None:
    await stack.add_product("A", stock=5)
    await reserve(stack, "o-1", [("A", 1)])
    await reserve(stack, "o-2", [("A", 1)])

    assert (await stack.inventory.post("/reservations/o-1/commit")).json()["status"] == "committed"
    assert (await stack.inventory.post("/reservations/o-1/commit")).status_code == 200
    assert (await stack.inventory.post("/reservations/o-1/release")).status_code == 409
    assert await stack.stock("A") == 3  # committed stock is not returned

    await stack.inventory.post("/reservations/o-2/release")
    assert (await stack.inventory.post("/reservations/o-2/commit")).status_code == 409
    assert (await stack.inventory.post("/reservations/missing/commit")).status_code == 404
    assert (await stack.inventory.post("/reservations/missing/release")).status_code == 404


async def test_stock_endpoint(stack: Stack) -> None:
    await stack.add_product("A", stock=7)
    assert (await stack.inventory.get("/stock/A")).json() == {"sku": "A", "available": 7}
