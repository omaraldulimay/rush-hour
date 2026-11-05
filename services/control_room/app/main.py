import asyncio
import contextlib
import logging
import time
from collections import deque
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

import asyncpg
import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from libs.common.db import open_resources
from libs.common.logging import configure_logging
from libs.common.observability import install_observability, pg_check
from scripts.rush import Rush, RushConfig
from services.control_room.app import store
from services.control_room.app.metrics import MetricsOffline, Prometheus
from services.control_room.app.settings import Settings

SERVICE = "control_room"
log = logging.getLogger(SERVICE)
STATIC = Path(__file__).parent / "static"
SLOW_DEFAULT_MS = 3000  # above the checkout's 2 s payment timeout, so the breaker has work to do


class RushIn(BaseModel):
    users: int = Field(default=50, ge=1, le=200)
    minutes: float = Field(default=3, ge=0.25, le=30)


class FaultIn(BaseModel):
    kind: Literal["slow", "down", "reset"]
    latency_ms: int = Field(default=SLOW_DEFAULT_MS, ge=0, le=60_000)
    error_rate: float = Field(default=1.0, ge=0, le=1)
    duration_s: float | None = Field(default=None, ge=1, le=3600)  # revert automatically


def incident_of(fault: dict) -> dict:
    kind = "slow" if fault["latency_ms"] > 0 else "down" if fault["error_rate"] > 0 else "none"
    if fault["latency_ms"] > 0 and fault["error_rate"] >= 1:
        kind = "down"
    return {"kind": kind, **fault}


def create_app(
    settings: Settings | None = None,
    *,
    pool: asyncpg.Pool | None = None,
    prometheus: httpx.AsyncClient | None = None,
    checkout: httpx.AsyncClient | None = None,
    payment: httpx.AsyncClient | None = None,
) -> FastAPI:
    """Injectable clients/pool so tests run against in-process fakes."""
    settings = settings or Settings.from_env()
    configure_logging(SERVICE)

    async def publish_gauges_forever() -> None:
        while True:
            try:
                await store.publish_gauges(app.state.pool)
            except Exception as exc:  # the DB may be briefly away; the gauges just go stale
                log.warning("could not publish drop gauges: %s", type(exc).__name__)
            await asyncio.sleep(settings.gauge_interval_s)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        async with open_resources(app, database_url=settings.database_url, redis_url=None):
            owned = []
            for attr, url in (
                ("prom_http", settings.prometheus_url),
                ("payment", settings.payment_url),
            ):
                if getattr(app.state, attr) is None:
                    client = httpx.AsyncClient(base_url=url, timeout=settings.http_timeout_s)
                    setattr(app.state, attr, client)
                    owned.append(client)
            gauges = asyncio.create_task(publish_gauges_forever(), name="drop-gauges")
            try:
                yield
            finally:
                gauges.cancel()
                run = app.state.run
                if run is not None and run.running:
                    run.stop()
                    with contextlib.suppress(Exception):
                        await app.state.run_task
                revert = app.state.revert_task
                if revert is not None:
                    revert.cancel()
                for client in owned:
                    await client.aclose()

    app = FastAPI(title="control-room", lifespan=lifespan)
    app.state.pool, app.state.prom_http, app.state.payment = pool, prometheus, payment
    app.state.run = None  # the Rush of the current or last run
    app.state.run_task = None
    app.state.revert_task = None
    app.state.incident_until = None
    app.state.events = deque(maxlen=200)
    app.state.seen = {"breaker": None, "sold_out": set(), "phase": None, "alerts": None}
    install_observability(app, SERVICE, {"postgres": pg_check(app)})

    def log_event(kind: str, message: str) -> None:
        app.state.events.appendleft({"t": time.time(), "kind": kind, "message": message})

    @app.middleware("http")
    async def same_origin_only(request: Request, call_next):
        """The panel is a demo admin: refuse state changes that a foreign web page triggered."""
        origin = request.headers.get("origin")
        if (
            request.method == "POST"
            and origin
            and urlsplit(origin).netloc != request.headers.get("host")
        ):
            return JSONResponse({"error": "cross_origin"}, status_code=403)
        return await call_next(request)

    # --- helpers -------------------------------------------------------------------------------
    async def get_fault() -> dict | None:
        try:
            resp = await app.state.payment.get("/admin/fault")
            resp.raise_for_status()
            return resp.json()
        except (httpx.HTTPError, ValueError):
            return None

    def run_view() -> dict:
        run: Rush | None = app.state.run
        if run is None:
            return {"phase": "idle", "running": False}
        return run.progress()

    def detect_alert_events(alerts: dict | None) -> None:
        """Alerts that start or stop firing become timeline events (the first poll is silent)."""
        if alerts is None:
            return
        now = {a["name"]: a for a in alerts["firing"]}
        before = app.state.seen["alerts"]
        if before is not None:
            for name, a in now.items():
                if name not in before:
                    log_event("alert", f"ALERT {a['severity']}: {name} - {a['summary']}")
            for name in before.keys() - now.keys():
                log_event("resolved", f"Alert {name} resolved")
        app.state.seen["alerts"] = now

    def detect_events(kpis: dict | None, stock: dict | None) -> None:
        seen = app.state.seen
        if kpis and kpis["breaker"] in ("closed", "half_open", "open"):
            state = kpis["breaker"]
            if seen["breaker"] not in (None, state):
                label = {"open": "OPENED", "half_open": "is half-open", "closed": "CLOSED again"}
                log_event(
                    "breaker" if state != "open" else "alert",
                    f"Payment circuit breaker {label[state]}",
                )
            seen["breaker"] = state
        if stock:
            for item in stock["items"]:
                if item["sold_out"] and item["sku"] not in seen["sold_out"]:
                    seen["sold_out"].add(item["sku"])
                    log_event("soldout", f"{item['label']} sold out ({item['name']})")
                elif not item["sold_out"]:
                    seen["sold_out"].discard(item["sku"])
        phase = run_view()["phase"]
        if seen["phase"] in ("warmup", "rush") and phase in ("done", "stopped"):
            counts = run_view()["counts"]
            log_event("run", f"Rush hour {phase}: {counts['paid']} paid orders")
        if seen["phase"] == "warmup" and phase == "rush":
            log_event("run", "The doors open: everybody arrives at once")
        seen["phase"] = phase

    # --- API -----------------------------------------------------------------------------------
    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})

    @app.get("/api/state")
    async def state() -> dict:
        prom = Prometheus(app.state.prom_http)
        metrics: dict = {"online": True, "error": None}
        kpis = None
        try:
            kpis = await prom.kpis()
        except MetricsOffline as exc:
            metrics = {"online": False, "error": str(exc)}
        alerts = None
        with contextlib.suppress(MetricsOffline):
            alerts = await prom.alerts()
        stock, revenue, db_error = None, None, None
        try:
            stock = await store.drop_status(app.state.pool)
            revenue = await store.revenue_cents(app.state.pool) / 100
        except (TimeoutError, asyncpg.PostgresError, OSError) as exc:
            db_error = type(exc).__name__
        detect_alert_events(alerts)
        detect_events(kpis, stock)
        fault = await get_fault()
        incident = incident_of(fault) if fault else {"kind": "unknown"}
        incident["until"] = app.state.incident_until if incident["kind"] != "none" else None
        return {
            "ts": time.time(),
            "metrics": metrics,
            "db": {"online": db_error is None, "error": db_error},
            "kpis": {**(kpis or {}), "revenue_eur": revenue},
            "stock": stock,
            "alerts": alerts,
            "run": run_view(),
            "incident": incident,
            "events": list(app.state.events)[:60],
        }

    @app.get("/api/series")
    async def series(window_s: int = 300) -> dict:
        try:
            data = await Prometheus(app.state.prom_http).series(min(max(window_s, 60), 3600))
        except MetricsOffline as exc:
            return {"online": False, "error": str(exc)}
        return {"online": True, **data}

    @app.post("/api/rush/start", status_code=202)
    async def rush_start(body: RushIn) -> dict:
        run: Rush | None = app.state.run
        if run is not None and run.running:
            raise HTTPException(409, {"error": "rush_already_running"})
        cfg = RushConfig(
            users=body.users,
            duration_s=body.minutes * 60,
            catalog_url=settings.catalog_url,
            checkout_url=settings.checkout_url,
        )
        app.state.run = Rush(cfg)
        app.state.run_task = asyncio.create_task(app.state.run.run(), name="rush")
        log_event("run", f"Rush hour started: {body.users} users, {body.minutes:g} min")
        return run_view()

    @app.post("/api/rush/stop")
    async def rush_stop() -> dict:
        run: Rush | None = app.state.run
        if run is None or not run.running:
            raise HTTPException(409, {"error": "no_rush_running"})
        run.stop()
        return run_view()

    async def apply_fault(latency_ms: int, error_rate: float) -> dict:
        current = await get_fault()
        if current is None:
            raise HTTPException(502, {"error": "payment_unreachable"})
        body = {**current, "latency_ms": latency_ms, "error_rate": error_rate}
        resp = await app.state.payment.post("/admin/fault", json=body)
        resp.raise_for_status()
        return resp.json()

    async def revert_later(delay_s: float) -> None:
        await asyncio.sleep(delay_s)
        with contextlib.suppress(Exception):
            await apply_fault(0, 0.0)
            app.state.incident_until = None
            log_event("incident", "Incident ended: payment back to normal")

    @app.post("/api/fault")
    async def fault(body: FaultIn) -> dict:
        latency, errors = {
            "slow": (body.latency_ms, 0.0),
            "down": (0, body.error_rate),
            "reset": (0, 0.0),
        }[body.kind]
        try:
            applied = await apply_fault(latency, errors)
        except httpx.HTTPError as exc:
            raise HTTPException(502, {"error": "payment_unreachable"}) from exc
        if app.state.revert_task is not None:
            app.state.revert_task.cancel()
            app.state.revert_task = None
        app.state.incident_until = None
        if body.kind == "reset":
            log_event("incident", "Incident cleared: payment back to normal")
        else:
            what = f"Payment slow: {latency} ms" if body.kind == "slow" else "Payment down"
            if body.kind == "down" and errors < 1:
                what = f"Payment flaky: {errors:.0%} errors"
            log_event("incident", f"Incident injected - {what}")
            if body.duration_s:
                app.state.incident_until = time.time() + body.duration_s
                app.state.revert_task = asyncio.create_task(revert_later(body.duration_s))
        return incident_of(applied)

    @app.post("/api/reset-stock")
    async def reset_stock() -> dict:
        run: Rush | None = app.state.run
        if run is not None and run.running:
            raise HTTPException(409, {"error": "rush_running"})
        count = await store.reset_stock(app.state.pool)
        app.state.seen["sold_out"].clear()
        log_event("stock", f"Stock of the {count} limited-drop items reset")
        return {"reset": count}

    return app
