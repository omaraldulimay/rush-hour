"""Control Room API (Prometheus mocked, payment real in-process) and the oversold invariant."""

import asyncio
import random

import httpx
import pytest
import pytest_asyncio

from services.control_room.app import main as control_room
from services.control_room.app import store
from services.control_room.app.main import create_app
from services.control_room.app.settings import Settings
from services.payment_mock.app.main import create_app as create_payment
from services.payment_mock.app.settings import Settings as PaymentSettings
from tests.conftest import asgi_client


def prom_ok() -> httpx.MockTransport:
    def vector(value: str) -> dict:
        data = {"resultType": "vector", "result": [{"metric": {}, "value": [0, value]}]}
        return {"status": "success", "data": data}

    def handler(request: httpx.Request) -> httpx.Response:
        query = request.url.params["query"]
        if request.url.path.endswith("query_range"):
            result = [
                {"metric": {"service": "checkout"}, "values": [[1, "2"], [6, "NaN"]]},
                {"metric": {"status": "paid"}, "values": [[1, "4"]]},
            ]
            return httpx.Response(200, json={"status": "success", "data": {"result": result}})
        if "circuit_breaker_state" in query:
            return httpx.Response(200, json=vector("2"))
        if "histogram_quantile" in query:
            return httpx.Response(200, json=vector("0.42"))
        if "status=~" in query:
            return httpx.Response(200, json=vector("1.5"))
        return httpx.Response(200, json=vector("120"))

    return httpx.MockTransport(handler)


def prom_down() -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    return httpx.MockTransport(handler)


@pytest_asyncio.fixture
async def room(pool, monkeypatch):
    await pool.execute("DELETE FROM control_room.stock_epoch")
    payment = asgi_client(create_payment(PaymentSettings(decline_rate=0.02), rng=random.Random(1)))
    holder = {}

    def build(transport: httpx.AsyncBaseTransport):
        prom = httpx.AsyncClient(base_url="http://prom", transport=transport)
        app = create_app(Settings(""), pool=pool, prometheus=prom, payment=payment)
        holder["app"] = app
        return asgi_client(app, "http://localhost:8080")

    holder["build"] = build
    yield holder
    await payment.aclose()


async def seed_hot_stock(pool, spent: int = 0) -> None:
    for p in store.hot_items():
        await pool.execute(
            "INSERT INTO inventory.stock (sku, available) VALUES ($1, $2) "
            "ON CONFLICT (sku) DO UPDATE SET available = EXCLUDED.available",
            p.sku,
            p.stock - spent,
        )


async def reserve(pool, order_id: str, sku: str, qty: int, status: str = "reserved") -> None:
    await pool.execute(
        "INSERT INTO inventory.reservations (order_id, status) VALUES ($1, $2)", order_id, status
    )
    await pool.execute(
        "INSERT INTO inventory.reservation_items (order_id, sku, qty) VALUES ($1, $2, $3)",
        order_id,
        sku,
        qty,
    )


# --- the invariant against real Postgres -------------------------------------------------------
async def test_invariant_holds_when_reserved_units_match_stock(pool):
    hot = store.hot_items()[0]
    await seed_hot_stock(pool, spent=0)
    await pool.execute("UPDATE inventory.stock SET available = available - 3 WHERE sku=$1", hot.sku)
    await reserve(pool, "o1", hot.sku, 3, "committed")
    status = await store.drop_status(pool)
    item = next(i for i in status["items"] if i["sku"] == hot.sku)
    assert (item["available"], item["held"], item["initial"]) == (hot.stock - 3, 3, hot.stock)
    assert status["oversold"] == 0 and status["invariant_ok"]


async def test_released_reservations_do_not_count_and_oversell_is_detected(pool):
    hot = store.hot_items()[1]
    await seed_hot_stock(pool)
    await reserve(pool, "gone", hot.sku, 5, "released")
    assert (await store.drop_status(pool))["oversold"] == 0
    # A (hypothetical) bug that sold more than ever existed must show up as oversold units.
    await reserve(pool, "too-many", hot.sku, hot.stock + 4, "committed")
    status = await store.drop_status(pool)
    assert status["oversold"] == 4 and not status["invariant_ok"]


async def test_reset_stock_restarts_the_count_without_touching_history(pool):
    hot = store.hot_items()[2]
    await seed_hot_stock(pool, spent=hot.stock)
    await reserve(pool, "old", hot.sku, hot.stock, "committed")
    before = await store.drop_status(pool)
    assert next(i for i in before["items"] if i["sku"] == hot.sku)["sold_out"]
    assert await store.reset_stock(pool) == 5
    after = await store.drop_status(pool)
    item = next(i for i in after["items"] if i["sku"] == hot.sku)
    assert item["available"] == hot.stock and item["held"] == 0 and after["oversold"] == 0
    assert await pool.fetchval("SELECT count(*) FROM inventory.reservations") == 1


async def test_revenue_counts_paid_orders_only(pool):
    for oid, status, cents in [("a", "paid", 10_000), ("b", "failed", 5_000), ("c", "paid", 2_550)]:
        await pool.execute(
            "INSERT INTO orders.orders (order_id, cart_id, status, total_cents) "
            "VALUES ($1, $1, $2, $3)",
            oid,
            status,
            cents,
        )
    assert await store.revenue_cents(pool) == 12_550


# --- API ---------------------------------------------------------------------------------------
async def test_state_combines_metrics_stock_and_incident(pool, room):
    await seed_hot_stock(pool)
    await pool.execute(
        "INSERT INTO orders.orders (order_id, cart_id, status, total_cents) "
        "VALUES ('p', 'p', 'paid', 9900)"
    )
    async with room["build"](prom_ok()) as client:
        state = (await client.get("/api/state")).json()
    assert state["metrics"]["online"] is True
    kpis = state["kpis"]
    assert kpis["orders_per_min"] == 120 and kpis["error_rate_pct"] == 1.5
    assert kpis["checkout_p95_ms"] == pytest.approx(420) and kpis["breaker"] == "open"
    assert kpis["revenue_eur"] == 99.0
    assert len(state["stock"]["items"]) == 5 and state["stock"]["oversold"] == 0
    assert state["incident"]["kind"] == "none" and state["run"]["phase"] == "idle"


async def test_metrics_offline_does_not_break_the_state(pool, room):
    await seed_hot_stock(pool)
    async with room["build"](prom_down()) as client:
        state = (await client.get("/api/state")).json()
        series = (await client.get("/api/series")).json()
    assert state["metrics"]["online"] is False
    assert "orders_per_min" not in state["kpis"] and state["stock"] is not None
    assert series["online"] is False


async def test_series_turns_nan_into_gaps(room):
    async with room["build"](prom_ok()) as client:
        series = (await client.get("/api/series")).json()
    assert series["online"] and series["p95_ms"]["checkout"] == [[1, 2], [6, None]]
    assert series["orders"]["paid"] == [[1, 4]]


async def test_incidents_set_the_payment_fault_and_keep_the_decline_rate(room):
    async with room["build"](prom_ok()) as client:
        slow = (await client.post("/api/fault", json={"kind": "slow", "latency_ms": 2500})).json()
        assert (
            slow["kind"] == "slow" and slow["latency_ms"] == 2500 and slow["decline_rate"] == 0.02
        )
        assert (await client.get("/api/state")).json()["incident"]["kind"] == "slow"
        down = (await client.post("/api/fault", json={"kind": "down"})).json()
        assert down["kind"] == "down" and down["error_rate"] == 1 and down["latency_ms"] == 0
        reset = (await client.post("/api/fault", json={"kind": "reset"})).json()
        assert reset["kind"] == "none" and reset["decline_rate"] == 0.02
        kinds = [e["kind"] for e in (await client.get("/api/state")).json()["events"]]
        assert kinds.count("incident") == 3


async def test_incident_with_duration_reverts_by_itself(room):
    async with room["build"](prom_ok()) as client:
        resp = await client.post("/api/fault", json={"kind": "down", "duration_s": 1})
        assert resp.status_code == 200
        assert (await client.get("/api/state")).json()["incident"]["until"] is not None
        await asyncio.sleep(1.4)
        assert (await client.get("/api/state")).json()["incident"]["kind"] == "none"
    task = room["app"].state.revert_task
    assert task is None or task.done()


async def test_fault_validation(room):
    async with room["build"](prom_ok()) as client:
        assert (await client.post("/api/fault", json={"kind": "boom"})).status_code == 422
        resp = await client.post("/api/fault", json={"kind": "down", "error_rate": 3})
        assert resp.status_code == 422


class FakeRush:
    def __init__(self, config):
        self.config, self.running, self._release = config, False, asyncio.Event()
        self.counts = dict.fromkeys(
            ("browse", "cart", "checkout", "requests", "errors", "paid", "payment_pending",
             "failed", "sold_out"), 0
        )  # fmt: skip

    async def run(self):
        self.running = True
        await self._release.wait()
        self.running = False

    def stop(self):
        self._release.set()

    def progress(self):
        phase = "rush" if self.running else "stopped"
        return {"phase": phase, "running": self.running, "users": self.config.users,
                "active_users": 3, "duration_s": self.config.duration_s, "elapsed_s": 1.0,
                "counts": self.counts}  # fmt: skip


async def test_only_one_rush_at_a_time(room, monkeypatch):
    monkeypatch.setattr(control_room, "Rush", FakeRush)
    async with room["build"](prom_ok()) as client:
        started = await client.post("/api/rush/start", json={"users": 20, "minutes": 1})
        await asyncio.sleep(0.05)
        assert started.status_code == 202 and started.json()["users"] == 20
        assert started.json()["duration_s"] == 60
        again = await client.post("/api/rush/start", json={})
        assert again.status_code == 409
        assert (await client.post("/api/reset-stock")).status_code == 409
        assert (await client.get("/api/state")).json()["run"]["running"] is True
        assert (await client.post("/api/rush/stop")).status_code == 200
        await asyncio.sleep(0.05)
        assert (await client.post("/api/rush/stop")).status_code == 409
        assert (await client.get("/api/state")).json()["run"]["phase"] == "stopped"


async def test_rush_start_validates_input(room):
    async with room["build"](prom_ok()) as client:
        assert (await client.post("/api/rush/start", json={"users": 0})).status_code == 422
        assert (await client.post("/api/rush/start", json={"users": 100_000})).status_code == 422


async def test_reset_stock_and_sold_out_events(pool, room):
    await seed_hot_stock(pool)
    await pool.execute("UPDATE inventory.stock SET available = 0 WHERE sku LIKE 'SNK-%'")
    async with room["build"](prom_ok()) as client:
        state = (await client.get("/api/state")).json()
        assert all(i["sold_out"] for i in state["stock"]["items"])
        assert sum(e["kind"] == "soldout" for e in state["events"]) == 5
        assert (await client.post("/api/reset-stock")).json() == {"reset": 5}
        state = (await client.get("/api/state")).json()
        assert not any(i["sold_out"] for i in state["stock"]["items"])


async def test_breaker_transitions_become_events(room):
    transport_state = {"value": "0"}

    def handler(request: httpx.Request) -> httpx.Response:
        value = (
            transport_state["value"]
            if "circuit_breaker_state" in request.url.params["query"]
            else "1"
        )
        body = {"resultType": "vector", "result": [{"metric": {}, "value": [0, value]}]}
        return httpx.Response(200, json={"status": "success", "data": body})

    async with room["build"](httpx.MockTransport(handler)) as client:
        await client.get("/api/state")
        transport_state["value"] = "2"
        events = (await client.get("/api/state")).json()["events"]
    assert any(e["kind"] == "alert" and "OPENED" in e["message"] for e in events)


async def test_foreign_origin_cannot_post_but_same_origin_can(room):
    async with room["build"](prom_ok()) as client:
        evil = await client.post(
            "/api/fault", json={"kind": "down"}, headers={"Origin": "http://evil.example"}
        )
        assert evil.status_code == 403
        ok = await client.post(
            "/api/fault", json={"kind": "reset"}, headers={"Origin": "http://localhost:8080"}
        )
        assert ok.status_code == 200


async def test_index_page_is_served(room):
    async with room["build"](prom_ok()) as client:
        resp = await client.get("/")
    assert resp.status_code == 200 and "Rush Hour" in resp.text
