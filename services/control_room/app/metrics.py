"""Thin Prometheus HTTP API client + the PromQL behind the Control Room."""

import math
import time

import httpx

SERVICES = ("catalog", "inventory", "checkout", "payment_mock", "order_worker")
_SVC = "|".join(SERVICES)
_BUSINESS = f'{{service=~"{_SVC}",route!~"/healthz|/readyz|/metrics"}}'
WINDOW = "30s"  # rate() window; scrapes come every 5 s

KPI_QUERIES = {
    "orders_per_min": f"sum(rate(checkout_orders_total[{WINDOW}])) * 60",
    "error_rate": (
        f'sum(rate(http_requests_total{{service=~"{_SVC}",route!~"/healthz|/readyz|/metrics",'
        f'status=~"5.."}}[{WINDOW}])) / '
        f"sum(rate(http_requests_total{_BUSINESS}[{WINDOW}])) * 100"
    ),
    "checkout_p95_s": (
        "histogram_quantile(0.95, sum by (le) (rate(http_request_duration_seconds_bucket"
        f'{{service="checkout",route="/checkout"}}[{WINDOW}])))'
    ),
    "breaker": 'max(circuit_breaker_state{name="payment"})',
}
SERIES_QUERIES = {
    "rps": f"sum by (service) (rate(http_requests_total{_BUSINESS}[{WINDOW}]))",
    "p95_ms": (
        "histogram_quantile(0.95, sum by (le, service) (rate(http_request_duration_seconds_bucket"
        f"{_BUSINESS}[{WINDOW}]))) * 1000"
    ),
    "orders": f"sum by (status) (rate(checkout_orders_total[{WINDOW}])) * 60",
}
BREAKER_STATES = {0: "closed", 1: "half_open", 2: "open"}


class MetricsOffline(Exception):
    pass


def _number(raw: str) -> float | None:
    value = float(raw)
    return None if math.isnan(value) or math.isinf(value) else value


class Prometheus:
    def __init__(self, client: httpx.AsyncClient) -> None:
        self.client = client

    async def _get(self, path: str, params: dict) -> list[dict]:
        try:
            resp = await self.client.get(path, params=params)
            resp.raise_for_status()
            body = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise MetricsOffline(type(exc).__name__) from exc
        if body.get("status") != "success":
            raise MetricsOffline(body.get("error", "query failed"))
        return body["data"]["result"]

    async def instant(self, query: str) -> float | None:
        result = await self._get("/api/v1/query", {"query": query})
        return _number(result[0]["value"][1]) if result else None

    async def kpis(self) -> dict:
        values = {name: await self.instant(q) for name, q in KPI_QUERIES.items()}
        breaker = values["breaker"]
        p95 = values["checkout_p95_s"]
        return {
            "orders_per_min": values["orders_per_min"],
            "error_rate_pct": values["error_rate"],
            "checkout_p95_ms": None if p95 is None else p95 * 1000,
            "breaker": "unknown" if breaker is None else BREAKER_STATES.get(round(breaker)),
        }

    async def series(self, window_s: int = 300, step_s: int = 5) -> dict:
        end = time.time()
        out: dict[str, dict[str, list]] = {}
        for name, query in SERIES_QUERIES.items():
            label = "status" if name == "orders" else "service"
            result = await self._get(
                "/api/v1/query_range",
                {"query": query, "start": end - window_s, "end": end, "step": step_s},
            )
            out[name] = {
                r["metric"].get(label, "?"): [[float(t), _number(v)] for t, v in r["values"]]
                for r in result
            }
        return out
