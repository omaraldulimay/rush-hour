"""httpx client factory that forwards the current X-Request-ID to downstream services."""

import httpx

from libs.common.logging import request_id_var


async def _add_request_id(request: httpx.Request) -> None:
    request_id = request_id_var.get()
    if request_id != "-":
        request.headers["X-Request-ID"] = request_id


def make_client(
    base_url: str,
    timeout: float = 5.0,
    transport: httpx.AsyncBaseTransport | None = None,
) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=base_url,
        timeout=timeout,
        transport=transport,
        limits=httpx.Limits(max_connections=200, max_keepalive_connections=50),
        event_hooks={"request": [_add_request_id]},
    )
