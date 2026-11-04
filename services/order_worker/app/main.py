import asyncio
import contextlib
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from libs.common.db import open_resources
from libs.common.http import make_client
from libs.common.logging import configure_logging
from libs.common.observability import install_observability, pg_check, redis_check
from services.order_worker.app.settings import Settings
from services.order_worker.app.worker import OrderWorker

SERVICE = "order_worker"
log = logging.getLogger(SERVICE)


def create_app(settings: Settings | None = None) -> FastAPI:
    """The worker is a background task; the HTTP app only serves /healthz, /readyz, /metrics."""
    settings = settings or Settings.from_env()
    configure_logging(SERVICE)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        async with open_resources(
            app, database_url=settings.database_url, redis_url=settings.redis_url
        ):
            inventory = make_client(settings.inventory_url, settings.http_timeout_s)
            payment = make_client(settings.payment_url, settings.http_timeout_s)
            worker = OrderWorker(settings, app.state.pool, app.state.redis, inventory, payment)
            task = asyncio.create_task(worker.run(), name="order-worker")
            app.state.worker_task = task
            try:
                yield
            finally:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
                await inventory.aclose()
                await payment.aclose()

    app = FastAPI(title="order-worker", lifespan=lifespan)
    app.state.pool = app.state.redis = app.state.worker_task = None

    async def worker_alive() -> None:
        task = app.state.worker_task
        if task is None or task.done():
            raise RuntimeError("consumer loop is not running")

    install_observability(
        app,
        SERVICE,
        {"postgres": pg_check(app), "redis": redis_check(app), "consumer": worker_alive},
    )
    return app
