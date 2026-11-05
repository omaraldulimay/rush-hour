"""Observability proof for the kind CI job (standard library only).

    python scripts/k8s/obs_proof.py slo     # SLO recording rules have data and are within target
    python scripts/k8s/obs_proof.py fault   # payment down -> PaymentBreakerOpen fires -> recovers

Environment: PROMETHEUS_URL (http://127.0.0.1:9090), CHECKOUT_URL (http://127.0.0.1:8003),
PAYMENT_URL (http://127.0.0.1:8004), CONTROL_ROOM_URL (optional, e.g. http://127.0.0.1:8080),
OBS_REPORT (markdown file the results are appended to) and GITHUB_STEP_SUMMARY.
The results are printed to the log and appended to both files.
"""

import argparse
import json
import math
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime

PROM = os.environ.get("PROMETHEUS_URL", "http://127.0.0.1:9090")
CHECKOUT = os.environ.get("CHECKOUT_URL", "http://127.0.0.1:8003")
PAYMENT = os.environ.get("PAYMENT_URL", "http://127.0.0.1:8004")
CONTROL_ROOM = os.environ.get("CONTROL_ROOM_URL", "")
ALERT = "PaymentBreakerOpen"

SLOS = {  # name -> objective (keep in sync with observability/prometheus/gen_slo_rules.py)
    "checkout_availability": 0.995,
    "checkout_latency": 0.99,
    "catalog_availability": 0.999,
    "catalog_latency": 0.99,
}


def http(method: str, url: str, body: dict | None = None, timeout: float = 5) -> dict:
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(
        url, data=data, method=method, headers={"content-type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.load(resp)


def query(expr: str) -> list[dict]:
    url = f"{PROM}/api/v1/query?" + urllib.parse.urlencode({"query": expr})
    body = http("GET", url)
    if body["status"] != "success":
        raise RuntimeError(f"query failed: {expr}: {body}")
    return body["data"]["result"]


def value(expr: str) -> float | None:
    """First sample of an instant query, None when there is none (or it is NaN)."""
    result = query(expr)
    if not result:
        return None
    number = float(result[0]["value"][1])
    return None if math.isnan(number) else number


def now() -> str:
    return datetime.now(UTC).strftime("%H:%M:%S")


def emit(markdown: str) -> None:
    print(markdown, flush=True)
    for path in (os.environ.get("OBS_REPORT"), os.environ.get("GITHUB_STEP_SUMMARY")):
        if path:
            with open(path, "a") as fh:
                fh.write(markdown + "\n")


def firing(name: str) -> dict[str, str]:
    """state of alert `name` ('pending' / 'firing') or {} when it is not active."""
    alerts = http("GET", f"{PROM}/api/v1/alerts")["data"]["alerts"]
    return {a["state"]: a["activeAt"] for a in alerts if a["labels"].get("alertname") == name}


def pct(v: float | None) -> str:
    return "no data" if v is None else f"{v * 100:.3f} %"


def one_minus(v: float | None) -> float | None:
    return None if v is None else 1 - v


def fmt(v: float | None, spec: str) -> str:
    return "no data" if v is None else format(v, spec)


# --- 1. SLO recording rules during the load run -------------------------------------------
def cmd_slo() -> int:
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:  # rules are evaluated every 15 s
        if all(value(f'slo:sli_error_ratio:rate5m{{slo="{s}"}}') is not None for s in SLOS):
            break
        time.sleep(5)

    rows, failures = [], []
    for slo, objective in SLOS.items():
        sel = f'{{slo="{slo}"}}'
        err5, err30 = (value(f"slo:sli_error_ratio:rate{w}{sel}") for w in ("5m", "30m"))
        rps = value(f"slo:requests:rate5m{sel}")
        burn = value(f"slo:burn_rate:rate5m{sel}")
        remaining = value(f"slo:error_budget_remaining:ratio{sel}")
        if err5 is None or err30 is None:
            failures.append(f"{slo}: recording rules have no data")
            met = "NO DATA"
        else:
            met = "yes" if 1 - err30 >= objective else "NO"
            if met == "NO":
                failures.append(
                    f"{slo}: SLI {(1 - err30) * 100:.3f} % < target {objective * 100} %"
                )
        rows.append(
            f"| {slo} | {objective * 100:g} % | {pct(one_minus(err5))} | {pct(one_minus(err30))} | "
            f"{fmt(rps, '.1f')} | {fmt(burn, '.2f')} | "
            f"{pct(remaining)} | {met} |"
        )

    up = value('count(up{job="rush-hour"} == 1)')
    if up is None or up < 6:
        failures.append(f"expected at least 6 healthy shop targets, got {up}")
    burning = [a for a in ("SLOFastBurn", "SLOSlowBurn") if firing(a).get("firing")]
    if burning:
        failures.append(f"burn-rate alerts fired during a healthy load run: {burning}")

    emit("\n### SLOs measured during the load run\n")
    emit("| SLO | target | SLI 5m | SLI 30m | req/s | burn 5m | budget left (6h) | within target |")
    emit("|---|---|---|---|---|---|---|---|")
    for row in rows:
        emit(row)
    emit(
        f"\nHealthy shop targets: {fmt(up, '.0f')} (6+ expected). Burn alerts: {burning or 'none'}."
    )
    emit(exporter_inventory())
    for failure in failures:
        print(f"::error::{failure}")
    return 1 if failures else 0


def exporter_inventory() -> str:
    """Which exporter metrics exist (informational: the dashboards rely on these names)."""
    wanted = {
        "pgbouncer": "pgbouncer_pools_client_waiting_connections",
        "postgres connections": "pg_stat_database_numbackends",
        "postgres up": "pg_up",
        "redis memory": "redis_memory_used_bytes",
        "app pool": 'db_pool_connections{state="used"}',
        "order stream lag": "order_worker_stream_lag",
        "hot-item stock": "drop_stock_available",
    }
    lines = ["\nMetrics behind the dashboards (series count):", ""]
    for label, expr in wanted.items():
        lines.append(f"- {label}: `{expr}` -> {len(query(expr))}")
    return "\n".join(lines)


# --- 2. fault injection: PaymentBreakerOpen must fire, then recover -----------------------
class Traffic:
    """A few threads that keep placing small orders, so the breaker sees real calls."""

    def __init__(self, workers: int = 6) -> None:
        self.stop = threading.Event()
        self.threads = [
            threading.Thread(target=self.run, args=(i,), daemon=True) for i in range(workers)
        ]
        self.sent = 0

    def run(self, worker: int) -> None:
        n = 0
        while not self.stop.is_set():
            n += 1
            cart, sku = (
                f"proof-{worker}-{n}-{int(time.time())}",
                f"ACC-{1 + (worker * 7 + n) % 500:04d}",
            )
            try:
                http("PUT", f"{CHECKOUT}/carts/{cart}/items", {"sku": sku, "qty": 1})
                http("POST", f"{CHECKOUT}/checkout", {"cart_id": cart}, timeout=10)
                self.sent += 1
            except (urllib.error.URLError, TimeoutError, OSError):
                pass
            time.sleep(0.15)

    def __enter__(self) -> "Traffic":
        for t in self.threads:
            t.start()
        return self

    def __exit__(self, *exc) -> None:
        self.stop.set()
        for t in self.threads:
            t.join(timeout=15)


def set_fault(error_rate: float) -> None:
    current = http("GET", f"{PAYMENT}/admin/fault")
    http("POST", f"{PAYMENT}/admin/fault", {**current, "latency_ms": 0, "error_rate": error_rate})


def control_room_view() -> dict | None:
    if not CONTROL_ROOM:
        return None
    try:
        return http("GET", f"{CONTROL_ROOM}/api/state")
    except (urllib.error.URLError, TimeoutError, OSError):
        return None


def cmd_fault(on_firing: str | None, firing_timeout: int, resolve_timeout: int) -> int:
    timeline: list[tuple[str, float, str]] = []
    failures: list[str] = []
    t0 = time.monotonic()

    def mark(label: str) -> None:
        timeline.append((f"{now()} UTC", time.monotonic() - t0, label))
        print(f"[{now()}] +{time.monotonic() - t0:5.0f}s  {label}", flush=True)

    if firing(ALERT):
        failures.append(f"{ALERT} was already active before the injection")
    control_room_view()  # baseline: the Control Room only reports changes it has seen

    seen_pending = seen_firing = False
    badge = timeline_event = None
    with Traffic() as traffic:
        time.sleep(5)
        set_fault(1.0)
        mark("injected: payment provider down (100 % errors)")
        deadline = time.monotonic() + firing_timeout
        while time.monotonic() < deadline and not seen_firing:
            states = firing(ALERT)
            if "pending" in states and not seen_pending:
                seen_pending = True
                mark(f"{ALERT} pending")
            if "firing" in states:
                seen_firing = True
                mark(f"{ALERT} FIRING")
            time.sleep(2)
        if not seen_firing:
            failures.append(f"{ALERT} did not fire within {firing_timeout}s")

        if seen_firing:
            time.sleep(16)  # give Prometheus, the Control Room's poll and Grafana a refresh
            view = control_room_view()
            if view and view.get("alerts"):
                badge = view["alerts"]["counts"]
                timeline_event = any(
                    e["kind"] == "alert" and ALERT in e["message"] for e in view["events"]
                )
                mark(f"Control Room badge {badge}, timeline event present: {timeline_event}")
                if badge["critical"] < 1 or not timeline_event:
                    failures.append("Control Room did not show the firing alert")
            elif CONTROL_ROOM:
                failures.append("Control Room returned no alerts")
            if on_firing:
                mark(f"running: {on_firing}")
                rc = subprocess.run(on_firing, shell=True, check=False).returncode
                if rc:
                    failures.append(f"on-firing command exited {rc}")

        set_fault(0.0)
        mark("fault reset: payment provider healthy again")
        deadline = time.monotonic() + resolve_timeout
        resolved = False
        while time.monotonic() < deadline:
            if not firing(ALERT):
                resolved = True
                mark(f"{ALERT} resolved (gone from /api/v1/alerts)")
                break
            time.sleep(2)
        if not resolved:
            failures.append(f"{ALERT} still active {resolve_timeout}s after the reset")
        sent = traffic.sent
        set_fault(0.0)

    emit("\n### Fault injection: payment provider down\n")
    emit(f"`{ALERT}` uses `for: 20s` in CI (real: 2m, covered by the promtool unit tests).\n")
    emit("| time | since injection | event |")
    emit("|---|---|---|")
    for clock, since, label in timeline:
        emit(f"| {clock} | +{since:.0f} s | {label} |")
    emit(f"\nOrders placed by the proof traffic: {sent}.")
    for failure in failures:
        print(f"::error::{failure}")
    return 1 if failures else 0


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("slo")
    fault = sub.add_parser("fault")
    fault.add_argument("--on-firing", help="shell command to run once the alert is firing")
    fault.add_argument("--firing-timeout", type=int, default=240)
    fault.add_argument("--resolve-timeout", type=int, default=300)
    args = parser.parse_args()
    if args.cmd == "slo":
        return cmd_slo()
    return cmd_fault(args.on_firing, args.firing_timeout, args.resolve_timeout)


if __name__ == "__main__":
    sys.exit(main())
