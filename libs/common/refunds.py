"""Refund call shared by checkout and the order worker."""

import asyncio
import logging

import httpx
from prometheus_client import Counter

log = logging.getLogger("refunds")

REFUNDS = Counter("payment_refunds_total", "Refund attempts by outcome", ["outcome"])


async def refund_payment(payment: httpx.AsyncClient, order_id: str, timeout_s: float) -> str:
    """Returns `refunded`, `none` (provider never charged this order) or `error`."""
    try:
        async with asyncio.timeout(timeout_s):
            resp = await payment.post(f"/payments/{order_id}/refund")
    except Exception as exc:
        log.warning("refund failed", extra={"order_id": order_id, "error": type(exc).__name__})
        REFUNDS.labels("error").inc()
        return "error"
    if resp.status_code == 200:
        REFUNDS.labels("refunded").inc()
        return "refunded"
    if resp.status_code in (404, 409):  # unknown or declined: nothing was charged
        REFUNDS.labels("none").inc()
        return "none"
    log.warning("refund failed", extra={"order_id": order_id, "status": resp.status_code})
    REFUNDS.labels("error").inc()
    return "error"
