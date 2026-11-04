"""Order events on the Redis Stream shared by checkout and the order worker."""

import logging

from redis.asyncio import Redis

STREAM = "orders"
GROUP = "order-workers"

log = logging.getLogger(__name__)


async def publish_order_event(redis: Redis, order_id: str, status: str) -> None:
    """Best effort: the order row is the source of truth, a lost event must not fail a request."""
    try:
        await redis.xadd(STREAM, {"order_id": order_id, "status": status}, maxlen=100_000)
    except Exception:
        log.exception("could not publish order event", extra={"order_id": order_id})
