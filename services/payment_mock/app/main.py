import asyncio
import random
from collections import OrderedDict

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from prometheus_client import Counter
from pydantic import BaseModel, Field

from libs.common.logging import configure_logging
from libs.common.observability import install_observability
from services.payment_mock.app.settings import Settings

SERVICE = "payment_mock"
MAX_REMEMBERED = 100_000

PAYMENTS = Counter("payment_mock_payments_total", "Payment outcomes", ["outcome"])


class PaymentIn(BaseModel):
    order_id: str = Field(min_length=1)
    amount_cents: int = Field(gt=0)


class Fault(BaseModel):
    latency_ms: int = Field(ge=0, le=60_000)
    error_rate: float = Field(ge=0, le=1)
    decline_rate: float = Field(ge=0, le=1)


def create_app(settings: Settings | None = None, *, rng: random.Random | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    configure_logging(SERVICE)
    rng = rng or random.Random()
    app = FastAPI(title="payment-mock")
    install_observability(app, SERVICE)

    fault = Fault(
        latency_ms=settings.latency_ms,
        error_rate=settings.error_rate,
        decline_rate=settings.decline_rate,
    )
    # Per-process memory is fine for a mock; a real provider dedupes by idempotency key too.
    results: OrderedDict[str, str] = OrderedDict()

    @app.post("/payments", response_model=None)
    async def pay(body: PaymentIn) -> JSONResponse:
        if body.order_id in results:
            return JSONResponse({"order_id": body.order_id, "status": results[body.order_id]})
        if fault.latency_ms:
            await asyncio.sleep(fault.latency_ms / 1000)
        if rng.random() < fault.error_rate:
            PAYMENTS.labels("error").inc()
            return JSONResponse({"error": "provider_unavailable"}, status_code=503)
        status = "declined" if rng.random() < fault.decline_rate else "paid"
        results[body.order_id] = status
        if len(results) > MAX_REMEMBERED:
            results.popitem(last=False)
        PAYMENTS.labels(status).inc()
        return JSONResponse({"order_id": body.order_id, "status": status})

    @app.post("/payments/{order_id}/refund", response_model=None)
    async def refund(order_id: str) -> JSONResponse:
        """Idempotent. Same fault injection as /payments."""
        if fault.latency_ms:
            await asyncio.sleep(fault.latency_ms / 1000)
        if rng.random() < fault.error_rate:
            PAYMENTS.labels("refund_error").inc()
            return JSONResponse({"error": "provider_unavailable"}, status_code=503)
        status = results.get(order_id)
        if status is None:
            return JSONResponse({"error": "unknown_payment"}, status_code=404)
        if status == "declined":
            return JSONResponse({"error": "not_charged"}, status_code=409)
        if status == "paid":
            results[order_id] = "refunded"
            PAYMENTS.labels("refunded").inc()
        return JSONResponse({"order_id": order_id, "status": "refunded"})

    @app.get("/admin/fault")
    async def get_fault() -> Fault:
        return fault

    @app.post("/admin/fault")
    async def set_fault(new: Fault) -> Fault:
        fault.latency_ms, fault.error_rate, fault.decline_rate = (
            new.latency_ms,
            new.error_rate,
            new.decline_rate,
        )
        return fault

    return app
