"""Game day: one sale night, 6 minutes of load, two incidents, one readiness report.

    python scripts/gameday/run.py --report docs/gameday/report.md

Timeline (seconds after the k6 load starts; load profile in scripts/k8s/load.js, PHASE=gameday):
    0    warm-up
    60   the "midnight" spike (220 browse/s, 12 checkouts/s)
    80   bad release of checkout (FAULT_ERROR_RATE) -> Argo Rollouts aborts the canary on its own
    200  payment provider answers in 3 s -> circuit breaker opens, orders become payment_pending
    260  payment recovers -> breaker closes, the worker pays the pending orders
    290  spike over, 360 end of load; then wait for the retry queue to drain

Needs: k6, kubectl, helm, a chart installed with rollout.enabled=true, and these URLs (defaults are the
kind NodePorts / port-forwards of CI): PROMETHEUS_URL, CATALOG_URL, CHECKOUT_URL, PAYMENT_URL,
CONTROL_ROOM_URL. Exit code 1 when a hard check fails (see `failures`), the report is written anyway.
"""

import argparse
import contextlib
import json
import os
import subprocess
import sys
import threading
import time
import urllib.parse
from datetime import UTC, datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts" / "k8s"))
import obs_proof as op  # noqa: E402  (reuses its HTTP/Prometheus helpers and the SLO objectives)

CONTROL_ROOM = os.environ.get("CONTROL_ROOM_URL", "http://127.0.0.1:8080")
CATALOG = os.environ.get("CATALOG_URL", "http://127.0.0.1:8001")
RELEASE = os.environ.get("RELEASE", "rush-hour")
ROLLOUT = os.environ.get("ROLLOUT", f"{RELEASE}-checkout")
CHART = os.environ.get("CHART", str(REPO / "deploy" / "helm" / "rush-hour"))
BAD_ERROR_RATE = "0.5"
T_BAD_RELEASE, T_PAYMENT_SLOW, T_PAYMENT_OK, LOAD_S = 80, 200, 260, 360
VCPU_HOUR_USD = 0.048  # assumption: AWS m6i.large on-demand (2 vCPU, $0.096/h, us-east-1)


def sh(*cmd: str, check: bool = True) -> str:
    out = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if check and out.returncode:
        raise RuntimeError(f"{' '.join(cmd)}: {out.stderr.strip()}")
    return out.stdout


class GameDay:
    def __init__(self) -> None:
        self.t0 = 0.0
        self.timeline: list[tuple[float, str]] = []
        self.failures: list[str] = []
        self.stop = threading.Event()
        self.max_oversold = 0
        self.bad_hash = ""
        self.t_release = self.t_abort = self.t_rolled_back = None  # seconds since t0

    def mark(self, label: str) -> float:
        t = time.time() - self.t0
        self.timeline.append((t, label))
        print(f"[{op.now()}] +{t:5.0f}s  {label}", flush=True)
        return t

    # --- background: watch the rollout and the oversold invariant -------------------------
    def monitor(self) -> None:
        last_oversold_check = 0.0
        while not self.stop.is_set():
            try:
                self.watch_rollout()
            except Exception as exc:  # a failed poll must not end the game day
                print(f"rollout poll failed: {exc}", flush=True)
            if time.time() - last_oversold_check >= 6:
                last_oversold_check = time.time()
                self.check_oversold()
            self.stop.wait(2)

    def check_oversold(self) -> None:
        try:
            stock = op.http("GET", f"{CONTROL_ROOM}/api/state")["stock"]
        except Exception:
            return
        if stock:
            self.max_oversold = max(self.max_oversold, stock["oversold"])

    def watch_rollout(self) -> None:
        if self.t_release is None or self.t_rolled_back is not None:
            return
        st = json.loads(sh("kubectl", "get", "rollout", ROLLOUT, "-o", "json"))["status"]
        if not self.bad_hash and st.get("currentPodHash") != st.get("stableRS"):
            self.bad_hash = st["currentPodHash"]
            self.mark(f"canary {self.bad_hash} started (20 % of checkout pods)")
        if self.bad_hash and st.get("abort") and self.t_abort is None:
            self.t_abort = self.mark("analysis failed: rollout aborted (Degraded)")
            self.diag()
        if self.bad_hash and (self.t_abort or st.get("stableRS") == self.bad_hash):
            pods = json.loads(
                sh(
                    "kubectl",
                    "get",
                    "pods",
                    "-l",
                    f"rollouts-pod-template-hash={self.bad_hash}",
                    "-o",
                    "json",
                )
            )["items"]
            if not [p for p in pods if not p["metadata"].get("deletionTimestamp")]:
                self.t_rolled_back = self.mark(
                    "bad canary pods gone: rolled back to the stable version"
                )

    def diag(self) -> None:
        """Why did the analysis fail? Kept in gameday-diag.log (uploaded with the report)."""
        series = op.query(
            'sum by (pod, status) (http_requests_total{service="checkout",route="/checkout"})'
        )
        with open("gameday-diag.log", "a") as fh:
            fh.write(sh("kubectl", "get", "analysisrun", "-o", "yaml", check=False))
            fh.write(sh("kubectl", "get", "pods", "-o", "wide", check=False))
            fh.write(json.dumps(series, indent=1))

    # --- scheduled actions ---------------------------------------------------------------
    def sleep_until(self, t: float) -> None:
        time.sleep(max(0.0, self.t0 + t - time.time()))

    def bad_release(self) -> None:
        self.t_release = self.mark(
            f"bad release: helm upgrade with FAULT_ERROR_RATE={BAD_ERROR_RATE} (same image)"
        )
        sh(
            "helm",
            "upgrade",
            RELEASE,
            CHART,
            "--reuse-values",
            "--set",
            f"services.checkout.env.FAULT_ERROR_RATE={BAD_ERROR_RATE}",
        )

    def payment(self, latency_ms: int) -> None:
        current = op.http("GET", f"{op.PAYMENT}/admin/fault")
        op.http(
            "POST",
            f"{op.PAYMENT}/admin/fault",
            {**current, "latency_ms": latency_ms, "error_rate": 0},
        )

    def run(self) -> dict:
        op.query("up")  # Prometheus reachable?
        sh("kubectl", "get", "rollout", ROLLOUT)  # chart installed with rollout.enabled=true?
        op.http("POST", f"{CONTROL_ROOM}/api/reset-stock")
        hot = [i["sku"] for i in op.http("GET", f"{CONTROL_ROOM}/api/state")["stock"]["items"]]
        k6_out = Path("gameday-k6.json")
        env = {
            **os.environ,
            "PHASE": "gameday",
            "HOT_SKUS": ",".join(hot),
            "SUMMARY": str(k6_out),
            "CATALOG_URL": CATALOG,
            "CHECKOUT_URL": op.CHECKOUT,
        }
        self.t0 = time.time()
        start_wall = self.t0
        self.mark("k6 started: warm-up (20 browse/s, 2 checkouts/s)")
        k6 = subprocess.Popen(
            ["k6", "run", "--quiet", str(REPO / "scripts" / "k8s" / "load.js")], env=env
        )
        monitor = threading.Thread(target=self.monitor, daemon=True)
        monitor.start()
        try:
            self.sleep_until(60)
            self.mark("midnight: spike to 220 browse/s and 12 checkouts/s")
            self.sleep_until(T_BAD_RELEASE)
            self.bad_release()
            self.sleep_until(T_PAYMENT_SLOW)
            self.payment(3000)
            self.mark("payment provider slow (3 s per call, checkout waits 2 s)")
            self.sleep_until(T_PAYMENT_OK)
            self.payment(0)
            self.mark("payment provider recovered")
            k6.wait()
            self.mark("k6 finished (spike over at 290 s, cool-down)")
            self.settle()
        finally:
            self.stop.set()
            if k6.poll() is None:
                k6.kill()
            with contextlib.suppress(Exception):
                self.payment(0)
            sh("helm", "rollback", RELEASE, check=False)  # leave the release as before the bad one
        self.check_oversold()
        end_wall = time.time()
        k6_summary = json.loads(k6_out.read_text()) if k6_out.exists() else {}
        return {"start": start_wall, "end": end_wall, "k6": k6_summary}

    def settle(self) -> None:
        """Wait (max 150 s) until the breaker is closed and the retry queue is empty."""
        deadline = time.time() + 150
        while time.time() < deadline:
            queue = op.value("sum(order_worker_payment_retry_queue)")
            breaker = op.value('max(circuit_breaker_state{name="payment"})')
            if queue == 0 and breaker == 0:
                self.mark("breaker closed, retry queue empty")
                return
            time.sleep(5)
        self.failures.append("retry queue not empty / breaker not closed 150 s after recovery")


# --- numbers from Prometheus ------------------------------------------------------------------
def rng(expr: str, start: float, end: float, step: int = 5) -> list[tuple[float, float]]:
    q = urllib.parse.urlencode({"query": expr, "start": start, "end": end, "step": step})
    body = op.http("GET", f"{op.PROM}/api/v1/query_range?{q}", timeout=30)
    if body["status"] != "success":
        raise RuntimeError(body)
    series = body["data"]["result"]
    return [(float(t), float(v)) for t, v in series[0]["values"]] if series else []


def counter_increase(selector: str, at: float) -> float:
    """Exact count for counters of pods that lived through the whole run (worker, stable checkout)."""
    q = urllib.parse.urlencode({"query": f"sum(increase({selector}))", "time": at})
    result = op.http("GET", f"{op.PROM}/api/v1/query?{q}")["data"]["result"]
    return float(result[0]["value"][1]) if result else 0.0


def total(rate_expr: str, start: float, end: float) -> float:
    """Events in a window: sum(rate(...[30s])) sampled every 5 s and summed (survives pod churn)."""
    return sum(v for _, v in rng(rate_expr, start, end)) * 5


def peak(expr: str, start: float, end: float) -> float:
    return max((v for _, v in rng(expr, start, end)), default=0.0)


CO = 'service="checkout",route="/checkout"'


def checkout_counts(a: float, b: float) -> tuple[float, float]:
    bad = total(f'sum(rate(http_requests_total{{{CO},status=~"5.."}}[30s])) or vector(0)', a, b)
    all_ = total(f"sum(rate(http_requests_total{{{CO}}}[30s]))", a, b)
    return bad, all_


def slo_rows(a: float, b: float) -> list[tuple[str, str, float, bool]]:
    rows = []
    cat = 'service="catalog",route="/products"'
    for name, num, den in (
        (
            "checkout_availability",
            f'sum(rate(http_requests_total{{{CO},status!~"5.."}}[30s]))',
            f"sum(rate(http_requests_total{{{CO}}}[30s]))",
        ),
        (
            "checkout_latency (< 1 s)",
            f'sum(rate(http_request_duration_seconds_bucket{{{CO},le="1.0"}}[30s]))',
            f"sum(rate(http_request_duration_seconds_count{{{CO}}}[30s]))",
        ),
        (
            "catalog_availability",
            f'sum(rate(http_requests_total{{{cat},status!~"5.."}}[30s]))',
            f"sum(rate(http_requests_total{{{cat}}}[30s]))",
        ),
        (
            "catalog_latency (< 300 ms)",
            f'sum(rate(http_request_duration_seconds_bucket{{{cat},le="0.3"}}[30s]))',
            f"sum(rate(http_request_duration_seconds_count{{{cat}}}[30s]))",
        ),
    ):
        good, all_ = total(num, a, b), total(den, a, b)
        sli = good / all_ if all_ else 0.0
        target = op.SLOS[name.split(" ")[0]]
        rows.append((name, f"{target * 100:g} %", sli, sli >= target))
    return rows


def hms(t: float) -> str:
    return f"{int(t) // 60}:{int(t) % 60:02d}"


def build_report(gd: GameDay, r: dict) -> str:
    a, b = r["start"], r["end"]
    k6 = r["k6"]
    peak_rps = peak(
        'sum(rate(http_requests_total{service=~"catalog|checkout",route!~"/healthz|/readyz|/metrics"}[30s]))',
        a,
        b,
    )
    peak_opm = 60 * peak(
        'sum(rate(checkout_orders_total{status=~"paid|payment_pending"}[30s]))', a, b
    )
    orders = total('sum(rate(checkout_orders_total{status=~"paid|payment_pending"}[30s]))', a, b)
    replicas = {}
    q = 'count by (service) (up{job="rush-hour"} == 1)'
    url = f"{op.PROM}/api/v1/query_range?" + urllib.parse.urlencode(
        {"query": q, "start": a, "end": b, "step": 5}
    )
    for s in op.http("GET", url, timeout=30)["data"]["result"]:
        replicas[s["metric"]["service"].removeprefix(f"{RELEASE}-")] = max(
            int(float(v)) for _, v in s["values"]
        )

    # bad release: share of checkouts in the window from release to rollback that failed
    t_rel, t_back = gd.t_release, gd.t_rolled_back
    if t_rel is not None and t_back is not None:
        bad_w, all_w = checkout_counts(a + t_rel, a + t_back + 10)
        share = (
            f"{bad_w / all_w * 100:.1f} % ({bad_w:.0f} of {all_w:.0f} checkouts in that window)"
            if all_w
            else "n/a"
        )
        back = f"{t_back - t_rel:.0f} s"
        detect = f"{gd.t_abort - t_rel:.0f} s" if gd.t_abort is not None else "n/a"
    else:
        share = back = detect = "n/a"
        gd.failures.append("the bad release was not rolled back")
    # payment incident
    t_slow = a + T_PAYMENT_SLOW - 10
    open_s = sum(
        5
        for _, v in rng('max(max_over_time(circuit_breaker_state{name="payment"}[5s]))', t_slow, b)
        if v >= 2
    )
    window = f"{int(b - a) + 10}s"
    pending = counter_increase(f'checkout_orders_total{{status="payment_pending"}}[{window}]', b)
    paid_later = counter_increase(f'worker_payment_retries_total{{outcome="paid"}}[{window}]', b)
    if open_s == 0:
        gd.failures.append("the circuit breaker never opened")
    if gd.max_oversold:
        gd.failures.append(f"OVERSOLD {gd.max_oversold} units")
    cpu_s = total('sum(rate(process_cpu_seconds_total{job="rush-hour"}[30s]))', a, b)
    cost = cpu_s / 3600 * VCPU_HOUR_USD / orders * 1000 if orders and cpu_s else None

    slos = slo_rows(a, b)
    lines = [
        "# Game day report: sale night",
        "",
        f"Run `{datetime.fromtimestamp(a, UTC):%Y-%m-%d %H:%M} UTC`"
        + (f", commit `{os.environ['GITHUB_SHA'][:7]}`" if os.environ.get("GITHUB_SHA") else "")
        + ". Generated by `scripts/gameday/run.py` from k6 output and Prometheus.",
        "",
        "| Measure | Result |",
        "|---|---|",
        f"| Peak load | {peak_rps:.0f} requests/s (catalog + checkout), {peak_opm:.0f} orders/min |",
        f"| Orders placed | {orders:.0f} ({pending:.0f} went through `payment_pending`) |",
        f"| Checkout p95 (k6, whole run) | {k6.get('checkout_p95_ms', 0):.0f} ms |",
        f"| Checkout errors (k6, 5xx and timeouts; sold out is not an error) | {k6.get('checkout_error_rate', 0) * 100:.2f} % |",
        f"| Oversold units | **{gd.max_oversold}** (must be 0) |",
        "| Max replicas | " + ", ".join(f"{k} {v}" for k, v in sorted(replicas.items())) + " |",
        f"| Bad release: aborted after / back on stable after | {detect} / {back} |",
        f"| Bad release: checkouts that failed meanwhile | {share} |",
        f"| Circuit breaker open | {open_s} s |",
        f"| Pending orders paid afterwards | {paid_later:.0f} of {pending:.0f} |",
        f"| Cost per 1,000 orders | {f'${cost:.4f}' if cost else 'n/a'} (see assumption below) |",
        "",
        "## SLOs over the whole run (the bad release and the slow payment count against them)",
        "",
        "| SLO | target | measured | met |",
        "|---|---|---|---|",
        *[f"| {n} | {t} | {s * 100:.2f} % | {'yes' if ok else 'no'} |" for n, t, s, ok in slos],
        "",
        "## Timeline",
        "",
        "| t | what happened |",
        "|---|---|",
        *[f"| {hms(t)} | {label} |" for t, label in gd.timeline],
        "",
        "## Cost assumption",
        "",
        f"CPU-seconds of the six shop services (Prometheus `process_cpu_seconds_total`): {cpu_s:.0f} s over the run.",
        f"Priced at **${VCPU_HOUR_USD}/vCPU-hour** (assumption: AWS m6i.large on-demand, us-east-1). "
        "Only CPU actually used, no idle capacity, Postgres, Redis, PgBouncer or monitoring.",
        "",
        "## Honest limits",
        "",
        "- One machine (kind with 3 nodes on a CI runner): the cluster, k6 and Prometheus compete for 4 CPUs.",
        "- The payment provider is simulated, so its failure is clean and its recovery instant.",
        "- Absolute numbers are small (about 12 orders/s); read the behaviour, not the throughput.",
        "",
    ]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", default="docs/gameday/report.md")
    args = parser.parse_args()
    gd = GameDay()
    try:
        result = gd.run()
    except Exception as exc:
        print(f"::error::game day aborted: {exc}", flush=True)
        return 1
    time.sleep(10)  # let the last scrapes land
    result["end"] = time.time()
    report = build_report(gd, result)
    Path(args.report).parent.mkdir(parents=True, exist_ok=True)
    Path(args.report).write_text(report)
    print(report)
    for failure in gd.failures:
        print(f"::error::{failure}")
    return 1 if gd.failures else 0


if __name__ == "__main__":
    sys.exit(main())
