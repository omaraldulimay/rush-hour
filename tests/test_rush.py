"""The load generator: action mix, the warm-up/jump shape and stopping."""

import asyncio
import random
from collections import Counter

import httpx

from scripts.rush import HOT_SKUS, MIX, Rush, RushConfig, pick_action


def test_action_mix_is_90_8_2():
    rng = random.Random(42)
    n = 100_000
    counts = Counter(pick_action(rng) for _ in range(n))
    assert set(counts) == set(MIX)
    assert abs(counts["browse"] / n - 0.90) < 0.01
    assert abs(counts["cart"] / n - 0.08) < 0.01
    assert abs(counts["checkout"] / n - 0.02) < 0.005


def test_there_are_five_hot_items():
    assert len(HOT_SKUS) == 5


def fake_shop() -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/checkout":
            return httpx.Response(201, json={"status": "paid", "order_id": "o"})
        return httpx.Response(200, json={})

    return httpx.MockTransport(handler)


async def test_simulated_users_keep_the_mix_and_pay_orders():
    rush = Rush(RushConfig(users=40, duration_s=2.5, think_s=0.01, seed=1, transport=fake_shop()))
    await rush.run()
    c = rush.counts
    steps = c["browse"] + c["cart"] + c["checkout"]
    assert steps > 1000
    assert abs(c["browse"] / steps - 0.90) < 0.03
    assert abs(c["cart"] / steps - 0.08) < 0.03
    assert abs(c["checkout"] / steps - 0.02) < 0.015
    assert c["paid"] == c["checkout"] and c["errors"] == 0
    assert rush.phase == "done" and rush.active_users == 0


async def test_warmup_starts_with_a_fifth_of_the_users_then_everyone_arrives():
    rush = Rush(RushConfig(users=50, duration_s=3, think_s=0.05, seed=2, transport=fake_shop()))
    task = asyncio.create_task(rush.run())
    await asyncio.sleep(0.3)
    assert rush.phase == "warmup" and rush.active_users == 10
    await asyncio.sleep(0.6)
    assert rush.phase == "rush" and rush.active_users == 50
    await task


async def test_stop_ends_the_run_early():
    rush = Rush(RushConfig(users=5, duration_s=60, think_s=0.01, transport=fake_shop()))
    task = asyncio.create_task(rush.run())
    await asyncio.sleep(0.2)
    rush.stop()
    await asyncio.wait_for(task, 3)
    assert rush.phase == "stopped" and not rush.running


async def test_sold_out_and_server_errors_are_counted_apart():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/checkout":
            return httpx.Response(409, json={"error": "sold_out"})
        if request.url.path.startswith("/products"):
            return httpx.Response(503)
        return httpx.Response(200, json={})

    rush = Rush(
        RushConfig(
            users=10, duration_s=1.5, think_s=0.01, seed=3, transport=httpx.MockTransport(handler)
        )
    )
    await rush.run()
    assert rush.counts["sold_out"] > 0 and rush.counts["errors"] > 0
    assert rush.counts["paid"] == 0
