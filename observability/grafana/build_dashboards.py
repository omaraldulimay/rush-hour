"""Generate the Grafana dashboards (dashboards/*.json) - dashboards as code.

    python observability/grafana/build_dashboards.py          # rewrite the JSON files
    python observability/grafana/build_dashboards.py --check  # exit 1 if they are out of date

Hand-edited dashboard JSON is hundreds of near-identical lines; here a panel is one function call
and units, colours and thresholds stay consistent. Every panel uses the `$datasource` variable
(no hard-coded datasource ids) and every dashboard has a stable uid.
"""

import json
import sys
from pathlib import Path

OUT = Path(__file__).parent / "dashboards"
DS = {"type": "prometheus", "uid": "${datasource}"}
BIZ = '{service=~"$service",route!~"/healthz|/readyz|/metrics"}'
R = "$__rate_interval"

# One colour per service/status everywhere, so a line means the same thing on every dashboard.
SERVICE_COLORS = {
    "catalog": "blue",
    "inventory": "purple",
    "checkout": "orange",
    "payment_mock": "teal",
    "order_worker": "text",
    "control_room": "dark-blue",
}
STATUS_COLORS = {
    "paid": "green",
    "payment_pending": "yellow",
    "failed": "red",
    "abandoned": "text",
}
GREEN_YELLOW_RED = [("green", None), ("yellow", 1), ("red", 5)]  # percent errors


class Board:
    def __init__(self, uid: str, title: str, description: str, tags: list[str] | None = None):
        self.uid, self.title, self.description = uid, title, description
        self.panels: list[dict] = []
        self.y = 0
        self.x = 0
        self.row_h = 0
        self.next_id = 1
        self.tags = ["rush-hour", *(tags or [])]
        self.templating: list[dict] = [
            {
                "name": "datasource",
                "label": "Data source",
                "type": "datasource",
                "query": "prometheus",
                "refresh": 1,
                "hide": 0,
                "current": {},
            }
        ]

    # --- layout: panels fill a 24-column grid left to right -----------------------------------
    def _place(self, w: int, h: int) -> dict:
        if self.x + w > 24:
            self.y += self.row_h
            self.x, self.row_h = 0, 0
        pos = {"x": self.x, "y": self.y, "w": w, "h": h}
        self.x += w
        self.row_h = max(self.row_h, h)
        return pos

    def row(self, title: str) -> None:
        self.y += self.row_h
        self.x, self.row_h = 0, 0
        self.panels.append(
            {
                "id": self._id(),
                "type": "row",
                "title": title,
                "collapsed": False,
                "gridPos": {"x": 0, "y": self.y, "w": 24, "h": 1},
                "panels": [],
            }
        )
        self.y += 1

    def _id(self) -> int:
        self.next_id += 1
        return self.next_id - 1

    def add(self, panel: dict, w: int, h: int) -> None:
        panel.update(id=self._id(), gridPos=self._place(w, h), datasource=DS)
        self.panels.append(panel)

    def variable(self, name: str, label: str, query: str, multi: bool = True) -> None:
        self.templating.append(
            {
                "name": name,
                "label": label,
                "type": "query",
                "datasource": DS,
                "query": {"query": query, "refId": f"var-{name}"},
                "definition": query,
                "refresh": 2,
                "multi": multi,
                "includeAll": multi,
                "allValue": ".+",
                "current": {"selected": True, "text": "All", "value": "$__all"},
                "sort": 1,
            }
        )

    def json(self) -> dict:
        return {
            "uid": self.uid,
            "title": self.title,
            "description": self.description,
            "tags": self.tags,
            "schemaVersion": 39,
            "version": 1,
            "editable": True,
            "graphTooltip": 1,
            "refresh": "5s",
            "time": {"from": "now-15m", "to": "now"},
            "timepicker": {"refresh_intervals": ["5s", "10s", "30s", "1m"]},
            "timezone": "browser",
            "fiscalYearStartMonth": 0,
            "liveNow": False,
            "links": [
                {
                    "title": "Rush Hour dashboards",
                    "type": "dashboards",
                    "tags": ["rush-hour"],
                    "asDropdown": True,
                    "includeVars": False,
                    "keepTime": True,
                }
            ],
            "annotations": {
                "list": [
                    {
                        "name": "Alerts firing",
                        "datasource": DS,
                        "enable": True,
                        "iconColor": "red",
                        "expr": 'ALERTS{alertstate="firing",severity=~"critical|warning"}',
                        "step": "15s",
                        "titleFormat": "{{alertname}}",
                        "textFormat": "{{severity}} {{slo}}",
                        "useValueForTime": False,
                    }
                ]
            },
            "templating": {"list": self.templating},
            "panels": self.panels,
        }


def thresholds(steps: list[tuple[str, float | None]]) -> dict:
    return {
        "mode": "absolute",
        "steps": [{"color": c, "value": v} for c, v in steps],
    }


def target(expr: str, legend: str = "", ref: str = "A", instant: bool = False, table=False) -> dict:
    t = {"expr": expr, "legendFormat": legend or "__auto", "refId": ref, "datasource": DS}
    if instant:
        t.update(instant=True, range=False)
    if table:
        t["format"] = "table"
    return t


def color_overrides(colors: dict[str, str]) -> list[dict]:
    return [
        {
            "matcher": {"id": "byName", "options": name},
            "properties": [{"id": "color", "value": {"mode": "fixed", "fixedColor": color}}],
        }
        for name, color in colors.items()
    ]


def timeseries(
    title: str,
    targets: list[dict],
    unit: str = "short",
    *,
    description: str = "",
    stack: bool = False,
    colors: dict[str, str] | None = None,
    steps: list[tuple[str, float | None]] | None = None,
    line_thresholds: bool = False,
    minimum: float | None = 0,
    maximum: float | None = None,
    decimals: int | None = None,
    legend_calcs: tuple[str, ...] = (),
    bars: bool = False,
) -> dict:
    custom = {
        "drawStyle": "bars" if bars else "line",
        "lineWidth": 2,
        "fillOpacity": 70 if bars else 14,
        "gradientMode": "opacity" if not bars else "none",
        "showPoints": "never",
        "spanNulls": True,
        "lineInterpolation": "linear",
        "stacking": {"mode": "normal" if stack else "none", "group": "A"},
        "thresholdsStyle": {"mode": "dashed" if line_thresholds else "off"},
    }
    defaults = {
        "unit": unit,
        "min": minimum,
        "color": {"mode": "palette-classic"},
        "custom": custom,
        "noValue": "No data",
        "thresholds": thresholds(steps or [("green", None)]),
    }
    if maximum is not None:
        defaults["max"] = maximum
    if decimals is not None:
        defaults["decimals"] = decimals
    return {
        "type": "timeseries",
        "title": title,
        "description": description,
        "targets": targets,
        "fieldConfig": {"defaults": defaults, "overrides": color_overrides(colors or {})},
        "options": {
            "legend": {
                "displayMode": "table" if legend_calcs else "list",
                "placement": "bottom",
                "calcs": list(legend_calcs),
            },
            "tooltip": {"mode": "multi", "sort": "desc"},
        },
    }


def stat(
    title: str,
    expr: str,
    unit: str = "short",
    *,
    steps: list[tuple[str, float | None]] | None = None,
    description: str = "",
    decimals: int | None = None,
    mappings: list[dict] | None = None,
    no_value: str = "No data",
    graph: bool = True,
    text_mode: str = "auto",
    minimum: float | None = None,
    maximum: float | None = None,
) -> dict:
    defaults: dict = {
        "unit": unit,
        "noValue": no_value,
        "color": {"mode": "thresholds"},
        "thresholds": thresholds(steps or [("blue", None)]),
        "mappings": mappings or [],
    }
    if decimals is not None:
        defaults["decimals"] = decimals
    if minimum is not None:
        defaults["min"] = minimum
    if maximum is not None:
        defaults["max"] = maximum
    return {
        "type": "stat",
        "title": title,
        "description": description,
        "targets": [target(expr)],
        "fieldConfig": {"defaults": defaults, "overrides": []},
        "options": {
            "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
            "colorMode": "background",
            "graphMode": "area" if graph else "none",
            "textMode": text_mode,
            "justifyMode": "center",
            "wideLayout": True,
        },
    }


def gauge(
    title: str,
    expr: str,
    unit: str,
    steps: list[tuple[str, float | None]],
    *,
    minimum: float,
    maximum: float,
    description: str = "",
    decimals: int | None = None,
    legend: str = "",
) -> dict:
    defaults: dict = {
        "unit": unit,
        "min": minimum,
        "max": maximum,
        "noValue": "No data",
        "color": {"mode": "thresholds"},
        "thresholds": thresholds(steps),
    }
    if decimals is not None:
        defaults["decimals"] = decimals
    return {
        "type": "gauge",
        "title": title,
        "description": description,
        "targets": [target(expr, legend)],
        "fieldConfig": {"defaults": defaults, "overrides": []},
        "options": {
            "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
            "showThresholdLabels": False,
            "showThresholdMarkers": True,
        },
    }


def bargauge(
    title: str,
    expr: str,
    legend: str,
    unit: str,
    steps: list[tuple[str, float | None]],
    *,
    minimum: float = 0,
    maximum: float | None = None,
    description: str = "",
) -> dict:
    defaults: dict = {
        "unit": unit,
        "min": minimum,
        "noValue": "No data",
        "color": {"mode": "thresholds"},
        "thresholds": thresholds(steps),
    }
    if maximum is not None:
        defaults["max"] = maximum
    return {
        "type": "bargauge",
        "title": title,
        "description": description,
        "targets": [target(expr, legend, instant=True)],
        "fieldConfig": {"defaults": defaults, "overrides": []},
        "options": {
            "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
            "orientation": "horizontal",
            "displayMode": "gradient",
            "showUnfilled": True,
            "valueMode": "color",
            "minVizHeight": 16,
        },
    }


def table(title: str, targets: list[dict], overrides: list[dict] | None = None, **kw) -> dict:
    return {
        "type": "table",
        "title": title,
        "description": kw.get("description", ""),
        "targets": targets,
        "fieldConfig": {
            "defaults": {"noValue": "No data", "custom": {"align": "auto"}},
            "overrides": overrides or [],
        },
        "options": {"showHeader": True, "cellHeight": "sm"},
        "transformations": kw.get("transformations", []),
    }


# ======================================================================================
def shop_overview() -> Board:
    b = Board(
        "rh-overview",
        "Rush Hour / Shop overview",
        "RED metrics (rate, errors, duration) for every service of the Nordkai shop.",
        ["red"],
    )
    b.variable("service", "Service", "label_values(http_requests_total, service)")
    all_biz = (
        'service=~"catalog|inventory|checkout|payment_mock|order_worker",'
        'route!~"/healthz|/readyz|/metrics"'
    )
    b.row("Right now")
    b.add(
        stat(
            "Requests / s",
            f"sum(rate(http_requests_total{{{all_biz}}}[{R}]))",
            "reqps",
            decimals=1,
            description="All shop services together, health checks excluded.",
        ),
        4,
        4,
    )
    b.add(
        stat(
            "Error rate (5xx)",
            f'(sum(rate(http_requests_total{{{all_biz},status=~"5.."}}[{R}])) or vector(0)) / '
            f"sum(rate(http_requests_total{{{all_biz}}}[{R}])) * 100",
            "percent",
            steps=GREEN_YELLOW_RED,
            decimals=2,
            description="Share of requests answered with a 5xx. 4xx (e.g. 409 sold_out) is not an error.",
        ),
        4,
        4,
    )
    b.add(
        stat(
            "Checkout p95",
            f"histogram_quantile(0.95, sum by (le) (rate(http_request_duration_seconds_bucket"
            f'{{service="checkout",route="/checkout"}}[{R}])))',
            "s",
            steps=[("green", None), ("yellow", 0.5), ("red", 1)],
            decimals=3,
            description="SLO: 99 % of checkouts under 1 s.",
        ),
        4,
        4,
    )
    b.add(
        stat(
            "Catalog p95",
            f"histogram_quantile(0.95, sum by (le) (rate(http_request_duration_seconds_bucket"
            f'{{service="catalog",route!~"/healthz|/readyz|/metrics"}}[{R}])))',
            "s",
            steps=[("green", None), ("yellow", 0.15), ("red", 0.3)],
            decimals=3,
            description="SLO: 99 % of catalog requests under 300 ms.",
        ),
        4,
        4,
    )
    b.add(
        stat(
            "Orders / min",
            f"sum(rate(checkout_orders_total[{R}])) * 60",
            "short",
            decimals=0,
            steps=[("blue", None)],
        ),
        4,
        4,
    )
    b.add(
        stat(
            "Alerts firing",
            'count(ALERTS{alertstate="firing",severity=~"critical|warning"}) or vector(0)',
            "short",
            steps=[("green", None), ("red", 1)],
            graph=False,
            description="Critical and warning alerts. Details on the SLO dashboard.",
        ),
        4,
        4,
    )

    b.row("Rate")
    b.add(
        timeseries(
            "Requests per second",
            [target(f"sum by (service) (rate(http_requests_total{BIZ}[{R}]))", "{{service}}")],
            "reqps",
            stack=True,
            colors=SERVICE_COLORS,
            legend_calcs=("mean", "max"),
        ),
        12,
        10,
    )
    b.add(
        timeseries(
            "Errors (5xx) per second",
            [
                target(
                    f'(sum by (service) (rate(http_requests_total{BIZ[:-1]},status=~"5.."}}[{R}])) '
                    f"or sum by (service) (rate(http_requests_total{BIZ}[{R}])) * 0)",
                    "{{service}}",
                )
            ],
            "reqps",
            colors=SERVICE_COLORS,
            legend_calcs=("max",),
            description="Absolute 5xx per second. Empty is good.",
        ),
        12,
        10,
    )
    b.row("Errors")
    b.add(
        timeseries(
            "Error ratio by service",
            [
                target(
                    f'((sum by (service) (rate(http_requests_total{BIZ[:-1]},status=~"5.."}}[{R}])) '
                    f"or sum by (service) (rate(http_requests_total{BIZ}[{R}])) * 0) "
                    f"/ sum by (service) (rate(http_requests_total{BIZ}[{R}]))) * 100",
                    "{{service}}",
                )
            ],
            "percent",
            colors=SERVICE_COLORS,
            steps=[("green", None), ("yellow", 1), ("red", 5)],
            line_thresholds=True,
            decimals=2,
            description="5xx / all requests. Dashed lines: 1 % and 5 %.",
        ),
        12,
        8,
    )
    b.add(
        timeseries(
            "Responses by status class",
            [
                target(
                    f"sum by (status_class) (label_replace(rate(http_requests_total{BIZ}[{R}]), "
                    '"status_class", "${1}xx", "status", "(.)..")) ',
                    "{{status_class}}",
                )
            ],
            "reqps",
            stack=True,
            colors={"2xx": "green", "3xx": "blue", "4xx": "yellow", "5xx": "red"},
            description="409 sold_out and 404 are 4xx: correct answers, not failures.",
        ),
        12,
        8,
    )
    b.row("Duration")
    for q, pos in ((0.5, "p50"), (0.95, "p95"), (0.99, "p99")):
        b.add(
            timeseries(
                f"Latency {pos}",
                [
                    target(
                        f"histogram_quantile({q}, sum by (le, service) "
                        f"(rate(http_request_duration_seconds_bucket{BIZ}[{R}])))",
                        "{{service}}",
                    )
                ],
                "s",
                colors=SERVICE_COLORS,
                steps=[("green", None), ("red", 1)],
                line_thresholds=pos == "p99",
                decimals=3,
                legend_calcs=("max",),
                description="Payment-mock and checkout are slow when the payment provider is.",
            ),
            8,
            10,
        )
    b.add(
        table(
            "Slowest routes (p95, last 5 min)",
            [
                target(
                    "topk(10, histogram_quantile(0.95, sum by (le, service, route) "
                    f"(rate(http_request_duration_seconds_bucket{BIZ}[5m]))) >= 0)",
                    instant=True,
                    table=True,
                )
            ],
            [
                {
                    "matcher": {"id": "byName", "options": "Value"},
                    "properties": [
                        {"id": "displayName", "value": "p95"},
                        {"id": "unit", "value": "s"},
                        {"id": "decimals", "value": 3},
                        {"id": "custom.cellOptions", "value": {"type": "color-background"}},
                        {
                            "id": "thresholds",
                            "value": thresholds([("green", None), ("yellow", 0.3), ("red", 1)]),
                        },
                    ],
                }
            ],
            transformations=[
                {
                    "id": "organize",
                    "options": {
                        "excludeByName": {"Time": True},
                        "indexByName": {"service": 0, "route": 1, "Value": 2},
                    },
                }
            ],
        ),
        24,
        8,
    )
    return b


# ======================================================================================
SLO_ROWS = [
    (
        "checkout_availability",
        "Checkout availability",
        99.5,
        "non-5xx answers (409 sold_out is fine)",
    ),
    ("checkout_latency", "Checkout latency", 99.0, "requests answered in under 1 s"),
    ("catalog_availability", "Catalog availability", 99.9, "non-5xx answers"),
    ("catalog_latency", "Catalog latency", 99.0, "requests answered in under 300 ms"),
]
BUDGET_STEPS = [("red", None), ("orange", 0), ("yellow", 0.25), ("green", 0.5)]


def slo_board() -> Board:
    b = Board(
        "rh-slo",
        "Rush Hour / SLOs and error budget",
        "SLI against target, error budget remaining and multi-window burn rates "
        "(recording rules from observability/prometheus/rules/slo.rules.yml).",
        ["slo"],
    )
    b.row("Service level indicators (last hour)")
    for slo, title, objective, what in SLO_ROWS:
        budget = 1 - objective / 100
        b.add(
            stat(
                f"{title} - target {objective:g} %",
                f'(1 - slo:sli_error_ratio:rate1h{{slo="{slo}"}}) * 100',
                "percent",
                steps=[("red", None), ("yellow", 100 - budget * 100 * 4), ("green", objective)],
                decimals=3,
                description=f"SLI over the last hour: {what}. Green = target met.",
            ),
            6,
            4,
        )
    b.row("Error budget remaining (demo window: 6 h; production would use 30 days)")
    for slo, title, _objective, _ in SLO_ROWS:
        b.add(
            gauge(
                title,
                f'slo:error_budget_remaining:ratio{{slo="{slo}"}}',
                "percentunit",
                BUDGET_STEPS,
                minimum=0,
                maximum=1,
                decimals=1,
                description="100 % = no errors, 0 % = the 6 h budget is used up, "
                "negative values are shown as 0.",
            ),
            6,
            5,
        )
    for slo, title, objective, what in SLO_ROWS:
        b.row(f"{title} ({objective:g} %: {what})")
        b.add(
            timeseries(
                "SLI vs target",
                [
                    target(f'(1 - slo:sli_error_ratio:rate5m{{slo="{slo}"}}) * 100', "SLI 5m"),
                    target(f'(1 - slo:sli_error_ratio:rate1h{{slo="{slo}"}}) * 100', "SLI 1h", "B"),
                    target(f'slo:objective:ratio{{slo="{slo}"}} * 100', "target", "C"),
                ],
                "percent",
                minimum=None,
                decimals=3,
                colors={"SLI 5m": "blue", "SLI 1h": "purple", "target": "red"},
            ),
            10,
            7,
        )
        b.add(
            timeseries(
                "Burn rate",
                [
                    target(f'slo:burn_rate:rate{w}{{slo="{slo}"}}', w, ref)
                    for w, ref in (("5m", "A"), ("30m", "B"), ("1h", "C"), ("6h", "D"))
                ],
                "none",
                steps=[("transparent", None), ("orange", 6), ("red", 14.4)],
                line_thresholds=True,
                decimals=2,
                description="1x = budget used up exactly at the end of the SLO window. "
                "Page at 14.4x (1 h and 5 m), ticket at 6x (6 h and 30 m).",
                colors={"5m": "light-blue", "30m": "blue", "1h": "purple", "6h": "dark-purple"},
            ),
            10,
            7,
        )
        b.add(
            stat(
                "Requests / s",
                f'slo:requests:rate5m{{slo="{slo}"}}',
                "reqps",
                decimals=2,
                description="Traffic behind the SLI. Alerts need more than 0.1 req/s.",
            ),
            4,
            7,
        )
    b.row("Alerts")
    b.add(
        table(
            "Active alerts",
            [
                target(
                    'ALERTS{alertstate=~"firing|pending"}',
                    instant=True,
                    table=True,
                )
            ],
            transformations=[
                {
                    "id": "organize",
                    "options": {
                        "excludeByName": {
                            "Time": True,
                            "Value": True,
                            "__name__": True,
                            "instance": True,
                            "job": True,
                            "namespace": True,
                            "pod": True,
                            "container": True,
                            "endpoint": True,
                        },
                        "indexByName": {"alertname": 0, "severity": 1, "alertstate": 2},
                    },
                }
            ],
            description="Pending = condition true but `for:` not over yet.",
        ),
        24,
        7,
    )
    return b


# ======================================================================================
BREAKER_MAP = [
    {
        "type": "value",
        "options": {
            "0": {"text": "closed", "color": "green", "index": 0},
            "1": {"text": "half-open", "color": "yellow", "index": 1},
            "2": {"text": "OPEN", "color": "red", "index": 2},
        },
    }
]


def sale_night() -> Board:
    b = Board(
        "rh-sale-night",
        "Rush Hour / Sale night",
        "What the business sees at 00:00: orders, revenue, the limited drop, "
        "the payment breaker and the machinery that repairs things.",
        ["business"],
    )
    b.row("The night in numbers")
    b.add(
        stat(
            "Orders / min",
            f"sum(rate(checkout_orders_total[{R}])) * 60",
            "short",
            decimals=0,
            description="Orders created per minute, whatever their status.",
        ),
        4,
        4,
    )
    b.add(
        stat(
            "Revenue (selected range)",
            "sum(increase(orders_revenue_cents_total[$__range])) / 100",
            "currencyEUR",
            decimals=0,
            steps=[("green", None)],
            graph=False,
            description="Value of orders that became paid (also those paid later by the worker).",
        ),
        4,
        4,
    )
    b.add(
        stat(
            "Paid share",
            f'sum(rate(checkout_orders_total{{status="paid"}}[{R}])) / sum(rate(checkout_orders_total[{R}])) * 100',
            "percent",
            decimals=1,
            steps=[("red", None), ("yellow", 70), ("green", 90)],
            description="Orders that were paid straight away. The rest waits for a retry.",
        ),
        4,
        4,
    )
    b.add(
        stat(
            "Payment breaker",
            'max(circuit_breaker_state{name="payment"})',
            "none",
            mappings=BREAKER_MAP,
            steps=[("green", None), ("yellow", 1), ("red", 2)],
            graph=False,
            description="Open = checkout stopped calling the provider; orders wait as payment_pending.",
        ),
        4,
        4,
    )
    b.add(
        stat(
            "Orders waiting for payment",
            "max(order_worker_payment_retry_queue)",
            "short",
            decimals=0,
            steps=[("green", None), ("yellow", 10), ("red", 50)],
        ),
        4,
        4,
    )
    b.add(
        stat(
            "Oversold units",
            "max(drop_oversold_units)",
            "short",
            decimals=0,
            steps=[("green", None), ("red", 1)],
            graph=False,
            description="The invariant of the project. Must be 0, always.",
        ),
        4,
        4,
    )
    b.row("Orders and revenue")
    b.add(
        timeseries(
            "Orders per minute by status",
            [target(f"sum by (status) (rate(checkout_orders_total[{R}])) * 60", "{{status}}")],
            "short",
            stack=True,
            colors=STATUS_COLORS,
            legend_calcs=("sum",),
            decimals=0,
            description="paid / payment_pending (retried later) / failed / abandoned (swept).",
        ),
        12,
        8,
    )
    b.add(
        timeseries(
            "Revenue per minute",
            [target(f"sum(rate(orders_revenue_cents_total[{R}])) * 60 / 100", "revenue")],
            "currencyEUR",
            colors={"revenue": "green"},
            legend_calcs=("mean", "max"),
            decimals=0,
        ),
        12,
        8,
    )
    b.row("Limited drop")
    b.add(
        bargauge(
            "Stock left per hot item",
            "drop_stock_available",
            "{{item}}",
            "short",
            [("red", None), ("orange", 1), ("yellow", 8), ("green", 20)],
            description="Five limited sneakers with 10-50 pieces. Red = sold out.",
        ),
        8,
        8,
    )
    b.add(
        timeseries(
            "Hot-item stock over time",
            [target("drop_stock_available", "{{item}}")],
            "short",
            decimals=0,
            legend_calcs=("min",),
        ),
        16,
        8,
    )
    b.row("Payment provider and the breaker")
    b.add(
        {
            "type": "state-timeline",
            "title": "Payment circuit breaker",
            "description": "Per checkout replica: closed / half-open / open.",
            "targets": [target('circuit_breaker_state{name="payment"}', "{{pod}}{{instance}}")],
            "fieldConfig": {
                "defaults": {
                    "mappings": BREAKER_MAP,
                    "color": {"mode": "thresholds"},
                    "thresholds": thresholds([("green", None), ("yellow", 1), ("red", 2)]),
                    "custom": {"fillOpacity": 80, "lineWidth": 0},
                    "noValue": "No data",
                },
                "overrides": [],
            },
            "options": {
                "mergeValues": True,
                "showValue": "never",
                "legend": {"showLegend": False},
                "rowHeight": 0.8,
            },
        },
        12,
        6,
    )
    b.add(
        timeseries(
            "Payment provider outcomes per minute",
            [
                target(
                    f"sum by (outcome) (rate(payment_mock_payments_total[{R}])) * 60", "{{outcome}}"
                )
            ],
            "short",
            stack=True,
            colors={
                "paid": "green",
                "declined": "yellow",
                "error": "red",
                "refunded": "blue",
                "refund_error": "orange",
            },
            decimals=0,
        ),
        12,
        6,
    )
    b.row("Self-healing")
    b.add(
        timeseries(
            "Payment retries per minute",
            [
                target(
                    f"sum by (outcome) (rate(worker_payment_retries_total[{R}])) * 60",
                    "{{outcome}}",
                )
            ],
            "short",
            colors={"paid": "green", "error": "red", "declined": "yellow"},
            decimals=1,
            description="The order worker retries payment_pending orders with backoff.",
        ),
        6,
        7,
    )
    b.add(
        timeseries(
            "Refunds per minute",
            [target(f"sum by (outcome) (rate(payment_refunds_total[{R}])) * 60", "{{outcome}}")],
            "short",
            colors={"refunded": "green", "error": "red", "none": "text"},
            decimals=1,
            description="Charges undone for orders that were failed meanwhile.",
        ),
        6,
        7,
    )
    b.add(
        timeseries(
            "Sweeper actions per minute",
            [target(f"sum by (action) (rate(order_worker_swept_total[{R}])) * 60", "{{action}}")],
            "short",
            decimals=1,
            description="abandoned = stale checkouts failed; released / refunded / rescheduled = repairs.",
        ),
        6,
        7,
    )
    b.add(
        timeseries(
            "Retry queue and stream backlog",
            [
                target("max(order_worker_payment_retry_queue)", "retry queue", "A"),
                target("max(order_worker_stream_lag)", "stream lag", "B"),
                target("max(order_worker_stream_pending)", "unacknowledged", "C"),
            ],
            "short",
            decimals=0,
            colors={"retry queue": "yellow", "stream lag": "red", "unacknowledged": "purple"},
        ),
        6,
        7,
    )
    return b


# ======================================================================================
def data_tier() -> Board:
    b = Board(
        "rh-data-tier",
        "Rush Hour / Data tier",
        "Postgres, PgBouncer, Redis and the order stream. Exporter panels (Postgres, PgBouncer, "
        'Redis) need the Kubernetes setup and show "No data" without it.',
        ["data"],
    )
    b.row("Application connection pools (every service reports its own)")
    b.add(
        timeseries(
            "Pool usage by service",
            [
                target(
                    'max by (service) (db_pool_connections{state="used"} '
                    '/ ignoring (state) db_pool_connections{state="max"}) * 100',
                    "{{service}}",
                )
            ],
            "percent",
            colors=SERVICE_COLORS,
            steps=[("green", None), ("red", 90)],
            line_thresholds=True,
            maximum=100,
            decimals=0,
            description="Connections in use / pool size (10). Alert DBPoolSaturated at 90 % for 3 min.",
        ),
        12,
        7,
    )
    b.add(
        timeseries(
            "Connections in use",
            [target('sum by (service) (db_pool_connections{state="used"})', "{{service}}")],
            "short",
            stack=True,
            colors=SERVICE_COLORS,
            decimals=0,
        ),
        12,
        7,
    )
    b.row("PgBouncer (needs pgbouncer_exporter, not installed by default)")
    b.add(
        stat(
            "Clients waiting",
            "sum(pgbouncer_pools_client_waiting_connections)",
            "short",
            steps=[("green", None), ("yellow", 1), ("red", 5)],
            no_value="No exporter",
            decimals=0,
            description="Clients that have a transaction ready but no server connection yet.",
        ),
        4,
        6,
    )
    b.add(
        timeseries(
            "Client connections",
            [
                target("sum(pgbouncer_pools_client_active_connections)", "active", "A"),
                target("sum(pgbouncer_pools_client_waiting_connections)", "waiting", "B"),
            ],
            "short",
            colors={"active": "blue", "waiting": "red"},
            decimals=0,
        ),
        10,
        6,
    )
    b.add(
        timeseries(
            "Server connections to Postgres",
            [
                target("sum(pgbouncer_pools_server_active_connections)", "active", "A"),
                target("sum(pgbouncer_pools_server_idle_connections)", "idle", "B"),
            ],
            "short",
            colors={"active": "blue", "idle": "text"},
            decimals=0,
            description="Transaction pooling: many clients share these few connections.",
        ),
        10,
        6,
    )
    b.row("Postgres (needs postgres_exporter, not installed by default)")
    b.add(
        stat(
            "Postgres up",
            "min(pg_up)",
            "none",
            mappings=[
                {
                    "type": "value",
                    "options": {
                        "0": {"text": "DOWN", "color": "red", "index": 0},
                        "1": {"text": "up", "color": "green", "index": 1},
                    },
                }
            ],
            steps=[("red", None), ("green", 1)],
            no_value="No exporter",
            graph=False,
        ),
        4,
        6,
    )
    b.add(
        timeseries(
            "Connections by state",
            [
                target(
                    'sum by (state) (pg_stat_activity_count{datname="rushhour"})',
                    "{{state}}",
                    "A",
                ),
                target("max(pg_settings_max_connections)", "max_connections", "B"),
            ],
            "short",
            colors={
                "active": "blue",
                "idle": "text",
                "idle in transaction": "orange",
                "max_connections": "red",
            },
            decimals=0,
            description="PgBouncer keeps this far below max_connections (100).",
        ),
        10,
        6,
    )
    b.add(
        timeseries(
            "Transactions per second",
            [
                target(
                    f'sum(rate(pg_stat_database_xact_commit{{datname="rushhour"}}[{R}]))',
                    "commits",
                    "A",
                ),
                target(
                    f'sum(rate(pg_stat_database_xact_rollback{{datname="rushhour"}}[{R}]))',
                    "rollbacks",
                    "B",
                ),
            ],
            "ops",
            colors={"commits": "green", "rollbacks": "red"},
            decimals=0,
        ),
        10,
        6,
    )
    b.row("Redis (needs redis_exporter, not installed by default) and the order stream")
    b.add(
        timeseries(
            "Redis memory",
            [
                target("sum(redis_memory_used_bytes)", "used", "A"),
                target("sum(redis_memory_max_bytes) > 0", "limit", "B"),
            ],
            "bytes",
            colors={"used": "blue", "limit": "red"},
        ),
        6,
        6,
    )
    b.add(
        timeseries(
            "Redis commands per second",
            [target(f"sum(rate(redis_commands_processed_total[{R}]))", "commands")],
            "ops",
            colors={"commands": "blue"},
            decimals=0,
        ),
        6,
        6,
    )
    b.add(
        timeseries(
            "Catalog cache hit ratio",
            [
                target(
                    f'sum(rate(catalog_cache_requests_total{{result="hit"}}[{R}])) / '
                    f"sum(rate(catalog_cache_requests_total[{R}])) * 100",
                    "hit ratio",
                )
            ],
            "percent",
            maximum=100,
            decimals=1,
            colors={"hit ratio": "green"},
            description="From the application, so it works without the Redis exporter.",
        ),
        6,
        6,
    )
    b.add(
        timeseries(
            "Order stream lag",
            [
                target("max(order_worker_stream_lag)", "not yet delivered", "A"),
                target("max(order_worker_stream_pending)", "unacknowledged", "B"),
                target("max(order_worker_payment_retry_queue)", "payment retry queue", "C"),
            ],
            "short",
            decimals=0,
            colors={
                "not yet delivered": "red",
                "unacknowledged": "purple",
                "payment retry queue": "yellow",
            },
            description="Reported by the order worker. Lag stays near 0 while it keeps up.",
        ),
        6,
        6,
    )
    return b


BOARDS = {
    "shop-overview.json": shop_overview,
    "slo-error-budget.json": slo_board,
    "sale-night.json": sale_night,
    "data-tier.json": data_tier,
}


def render() -> dict[str, str]:
    return {
        name: json.dumps(build().json(), indent=2, sort_keys=False) + "\n"
        for name, build in BOARDS.items()
    }


if __name__ == "__main__":
    files = render()
    if "--check" in sys.argv:
        stale = [
            n
            for n, text in files.items()
            if not (OUT / n).exists() or (OUT / n).read_text() != text
        ]
        sys.exit(1 if stale else 0)
    OUT.mkdir(exist_ok=True)
    for name, text in files.items():
        (OUT / name).write_text(text)
        print(f"wrote {OUT / name}")
