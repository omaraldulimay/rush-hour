import asyncio
import random
from collections.abc import AsyncIterator

import httpx
import pytest
import pytest_asyncio
from starlette.datastructures import Headers

from libs.common import migrate
from services.order_worker.app.main import create_app as create_worker_app
from services.order_worker.app.settings import Settings as WorkerSettings
from services.payment_mock.app.main import create_app as create_payment
from tests.conftest import Stack, asgi_client


@pytest_asyncio.fixture
async def worker_http(stack: Stack) -> AsyncIterator[httpx.AsyncClient]:
    app = create_worker_app(WorkerSettings("", ""))
    app.state.pool, app.state.redis = stack.pool, stack.redis
    app.state.worker_task = asyncio.create_task(asyncio.sleep(3600))
    yield asgi_client(app)
    app.state.worker_task.cancel()


@pytest.mark.parametrize("name", ["catalog", "inventory", "payment", "checkout", "worker"])
async def test_ops_endpoints_exist_on_every_service(
    stack: Stack, worker_http: httpx.AsyncClient, name: str
) -> None:
    client = worker_http if name == "worker" else getattr(stack, name)

    assert (await client.get("/healthz")).json() == {"status": "ok"}
    assert (await client.get("/readyz")).status_code == 200
    metrics = await client.get("/metrics")
    assert metrics.status_code == 200
    assert "http_requests_total" in metrics.text
    assert "http_request_duration_seconds_bucket" in metrics.text


async def test_readyz_is_503_when_a_dependency_is_down(stack: Stack) -> None:
    class BrokenRedis:
        async def ping(self) -> None:
            raise ConnectionError("redis down")

    stack.checkout_app.state.redis = BrokenRedis()
    resp = await stack.checkout.get("/readyz")
    assert resp.status_code == 503
    assert "redis" in resp.json()["failures"]
    assert (await stack.checkout.get("/healthz")).status_code == 200  # liveness unaffected


async def test_metrics_use_route_templates_and_status(stack: Stack) -> None:
    await stack.add_product("TPL-1", 1)
    await stack.catalog.get("/products/TPL-1")
    await stack.catalog.get("/products/does-not-exist")
    text = (await stack.catalog.get("/metrics")).text
    assert (
        'http_requests_total{method="GET",route="/products/{sku}",service="catalog",status="200"}'
        in text
    )
    assert 'route="/products/{sku}",service="catalog",status="404"' in text
    assert "TPL-1" not in text


async def test_request_id_is_returned_and_propagated_downstream(stack: Stack) -> None:
    await stack.add_product("A", 5)
    seen: list[str | None] = []
    real_inventory = stack.checkout_app.state.inventory._transport.app

    async def spy(scope, receive, send):
        seen.append(Headers(scope=scope).get("x-request-id"))
        await real_inventory(scope, receive, send)

    stack.checkout_app.state.inventory = asgi_client(spy)
    await stack.checkout.put("/carts/c1/items", json={"sku": "A", "qty": 1})

    resp = await stack.checkout.post(
        "/checkout", json={"cart_id": "c1"}, headers={"X-Request-ID": "req-abc"}
    )

    assert resp.headers["x-request-id"] == "req-abc"
    assert seen == ["req-abc"]
    generated = await stack.checkout.get("/healthz")
    assert len(generated.headers["x-request-id"]) == 32


async def test_migrations_are_tracked_and_idempotent(stack: Stack, database_url: str) -> None:
    rows = await stack.pool.fetch("SELECT service, name FROM public.schema_migrations")
    assert {(r["service"], r["name"]) for r in rows} >= {
        ("catalog", "001_init.sql"),
        ("inventory", "001_init.sql"),
        ("checkout", "001_init.sql"),
    }
    assert await migrate.apply_all(database_url) == []
    schemas = {
        r["schema_name"]
        for r in await stack.pool.fetch("SELECT schema_name FROM information_schema.schemata")
    }
    assert {"catalog", "inventory", "orders"} <= schemas


def test_payment_app_without_faults_needs_no_dependencies() -> None:
    assert create_payment(rng=random.Random(0)).title == "payment-mock"
