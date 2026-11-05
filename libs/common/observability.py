"""Request-ID + Prometheus middleware and the /healthz, /readyz, /metrics endpoints."""

import logging
import time
import uuid
from collections.abc import Awaitable, Callable

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest
from starlette.datastructures import Headers, MutableHeaders
from starlette.responses import Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from libs.common.logging import request_id_var

HTTP_REQUESTS = Counter(
    "http_requests_total", "HTTP requests", ["service", "method", "route", "status"]
)
HTTP_LATENCY = Histogram(
    "http_request_duration_seconds",
    "HTTP request latency",
    ["service", "method", "route", "status"],
    # 0.3 and 1 are the SLO thresholds (catalog 300 ms, checkout 1 s): the SLI needs an edge there.
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.3, 0.5, 1, 2.5, 5, 10),
)
DB_POOL = Gauge(
    "db_pool_connections",
    "asyncpg pool connections: state = used | idle | max (saturation = used / max)",
    ["service", "state"],
)

OPS_PATHS = {"/healthz", "/readyz", "/metrics"}
log = logging.getLogger("access")

Check = Callable[[], Awaitable[None]]


class ObservabilityMiddleware:
    """Pure ASGI middleware so the request_id contextvar is visible inside the endpoint."""

    def __init__(self, app: ASGIApp, service: str) -> None:
        self.app = app
        self.service = service

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_id = (Headers(scope=scope).get("x-request-id") or uuid.uuid4().hex)[:64]
        token = request_id_var.set(request_id)
        status = 500
        started = time.perf_counter()

        async def send_wrapper(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                MutableHeaders(scope=message)["X-Request-ID"] = request_id
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            elapsed = time.perf_counter() - started
            # Route *template* (not the raw path) keeps label cardinality bounded.
            route = getattr(scope.get("route"), "path", None) or "unmatched"
            labels = (self.service, scope["method"], route, str(status))
            HTTP_REQUESTS.labels(*labels).inc()
            HTTP_LATENCY.labels(*labels).observe(elapsed)
            if scope["path"] not in OPS_PATHS:
                log.info(
                    "request",
                    extra={
                        "method": scope["method"],
                        "path": scope["path"],
                        "route": route,
                        "status": status,
                        "duration_ms": round(elapsed * 1000, 2),
                    },
                )
            request_id_var.reset(token)


def install_observability(app: FastAPI, service: str, checks: dict[str, Check] | None = None):
    """Add middleware and the three ops endpoints. `checks` are readiness probes."""
    checks = checks or {}
    app.add_middleware(ObservabilityMiddleware, service=service)

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz", include_in_schema=False)
    async def readyz() -> JSONResponse:
        failures: dict[str, str] = {}
        for name, check in checks.items():
            try:
                await check()
            except Exception as exc:
                failures[name] = f"{type(exc).__name__}: {exc}"
        if failures:
            return JSONResponse({"status": "unavailable", "failures": failures}, status_code=503)
        return JSONResponse({"status": "ready"})

    @app.get("/metrics", include_in_schema=False)
    async def metrics() -> Response:
        update_pool_gauges(service, getattr(app.state, "pool", None))
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


def update_pool_gauges(service: str, pool) -> None:
    """Pool usage is read at scrape time: three integers, no hot-path cost."""
    if pool is None:
        return
    size, idle = pool.get_size(), pool.get_idle_size()
    DB_POOL.labels(service, "used").set(size - idle)
    DB_POOL.labels(service, "idle").set(idle)
    DB_POOL.labels(service, "max").set(pool.get_max_size())


def pg_check(app: FastAPI) -> Check:
    async def check() -> None:
        async with app.state.pool.acquire(timeout=2) as conn:
            await conn.fetchval("SELECT 1")

    return check


def redis_check(app: FastAPI) -> Check:
    async def check() -> None:
        await app.state.redis.ping()

    return check
