"""Test harness: real Postgres (temp cluster or DATABASE_URL), fakeredis, in-process apps."""

import getpass
import os
import random
import shutil
import socket
import subprocess
import tempfile
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from pathlib import Path

import asyncpg
import httpx
import pytest
import pytest_asyncio
from fakeredis import FakeServer
from fakeredis.aioredis import FakeRedis
from redis.asyncio import Redis

from libs.common import migrate
from libs.common.http import make_client
from services.catalog.app.main import create_app as create_catalog
from services.catalog.app.settings import Settings as CatalogSettings
from services.checkout.app.main import create_app as create_checkout
from services.checkout.app.settings import Settings as CheckoutSettings
from services.inventory.app.main import create_app as create_inventory
from services.inventory.app.settings import Settings as InventorySettings
from services.order_worker.app.settings import Settings as WorkerSettings
from services.order_worker.app.worker import OrderWorker
from services.payment_mock.app.main import create_app as create_payment
from services.payment_mock.app.settings import Settings as PaymentSettings

PG_BIN = Path(os.environ.get("PG_BIN", "/Library/PostgreSQL/16/bin"))


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="session")
def database_url() -> Iterator[str]:
    """DATABASE_URL if set (CI); otherwise initdb + pg_ctl in a temp dir on a free port."""
    if url := os.environ.get("DATABASE_URL"):
        yield url
        return

    user = getpass.getuser()
    root = Path(tempfile.mkdtemp(prefix="peak-ready-pg-"))
    data, port = root / "data", _free_port()
    subprocess.run(
        [PG_BIN / "initdb", "-D", data, "-U", user, "--auth=trust", "-E", "UTF8", "--no-locale"],
        check=True,
        capture_output=True,
    )
    options = (
        f"-p {port} -c listen_addresses=127.0.0.1 -c unix_socket_directories= "
        "-c fsync=off -c synchronous_commit=off -c full_page_writes=off "
        "-c shared_buffers=32MB -c max_connections=100"
    )
    subprocess.run(
        [PG_BIN / "pg_ctl", "-D", data, "-o", options, "-l", root / "pg.log", "-w", "start"],
        check=True,
        capture_output=True,
    )
    try:
        yield f"postgresql://{user}@127.0.0.1:{port}/postgres"
    finally:
        subprocess.run([PG_BIN / "pg_ctl", "-D", data, "-m", "immediate", "stop"], check=False)
        shutil.rmtree(root, ignore_errors=True)


@pytest_asyncio.fixture(scope="session")
async def pool(database_url: str) -> AsyncIterator[asyncpg.Pool]:
    await migrate.apply_all(database_url)
    pool = await asyncpg.create_pool(database_url, min_size=2, max_size=20)
    yield pool
    await pool.close()


@pytest_asyncio.fixture(autouse=True)
async def clean_db(pool: asyncpg.Pool) -> None:
    await pool.execute(
        "TRUNCATE catalog.products, inventory.stock, inventory.reservations, "
        "inventory.reservation_items, orders.orders, orders.order_items"
    )


@pytest_asyncio.fixture
async def redis() -> AsyncIterator[Redis]:
    if url := os.environ.get("REDIS_URL"):
        client = Redis.from_url(url, decode_responses=True)
        await client.flushdb()
    else:
        client = FakeRedis(server=FakeServer(), decode_responses=True)
    yield client
    await client.aclose()


def asgi_client(app, base_url: str = "http://test") -> httpx.AsyncClient:
    return make_client(base_url, 10, httpx.ASGITransport(app=app))


@dataclass
class Stack:
    pool: asyncpg.Pool
    redis: Redis
    catalog: httpx.AsyncClient
    inventory: httpx.AsyncClient
    payment: httpx.AsyncClient
    checkout: httpx.AsyncClient
    checkout_app: object
    worker: OrderWorker
    checkout_settings: CheckoutSettings
    worker_settings: WorkerSettings

    async def add_product(
        self, sku: str, stock: int, price_cents: int = 10_000, category: str = "sneakers"
    ) -> None:
        await self.pool.execute(
            "INSERT INTO catalog.products (sku, name, category, price_cents) VALUES ($1,$2,$3,$4)",
            sku,
            f"Product {sku}",
            category,
            price_cents,
        )
        await self.pool.execute(
            "INSERT INTO inventory.stock (sku, available) VALUES ($1, $2)", sku, stock
        )

    async def stock(self, sku: str) -> int:
        return await self.pool.fetchval("SELECT available FROM inventory.stock WHERE sku=$1", sku)

    async def set_fault(self, **fault: float) -> None:
        body = {"latency_ms": 0, "error_rate": 0, "decline_rate": 0} | fault
        resp = await self.payment.post("/admin/fault", json=body)
        assert resp.status_code == 200

    async def buy(self, sku: str, qty: int = 1, cart_id: str = "cart-1", **headers: str):
        await self.checkout.put(f"/carts/{cart_id}/items", json={"sku": sku, "qty": qty})
        return await self.checkout.post("/checkout", json={"cart_id": cart_id}, headers=headers)


@pytest_asyncio.fixture
async def stack(pool: asyncpg.Pool, redis: Redis) -> AsyncIterator[Stack]:
    catalog_app = create_catalog(CatalogSettings("", "", cache_ttl_s=30), pool=pool, redis=redis)
    inventory_app = create_inventory(InventorySettings(""), pool=pool)
    payment_app = create_payment(PaymentSettings(), rng=random.Random(1))

    catalog = asgi_client(catalog_app)
    inventory = asgi_client(inventory_app)
    payment = asgi_client(payment_app)

    checkout_settings = CheckoutSettings(
        "",
        "",
        payment_timeout_s=0.3,
        breaker_failure_threshold=3,
        breaker_window_s=30,
        breaker_cooldown_s=60,
    )
    checkout_app = create_checkout(
        checkout_settings,
        pool=pool,
        redis=redis,
        catalog=catalog,
        inventory=inventory,
        payment=payment,
    )
    worker_settings = WorkerSettings(
        "",
        "",
        payment_timeout_s=0.3,
        max_attempts=3,
        retry_base_delay_s=10,
        retry_max_delay_s=100,
    )
    worker = OrderWorker(worker_settings, pool, redis, inventory, payment)
    await worker.setup()

    checkout = asgi_client(checkout_app)
    yield Stack(
        pool,
        redis,
        catalog,
        inventory,
        payment,
        checkout,
        checkout_app,
        worker,
        checkout_settings,
        worker_settings,
    )
    for client in (catalog, inventory, payment, checkout):
        await client.aclose()
