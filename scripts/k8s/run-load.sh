#!/usr/bin/env bash
# Load burst + HPA proof + pod-kill resilience check for the kind cluster. Needs kubectl, k6, jq.
# Phase 1 ("ramp", 100 s): traffic jumps, HPAs must scale up. Phase 2 ("steady", 50 s): one catalog pod
# is deleted under load, requests must keep succeeding. Writes a markdown report to $REPORT.
set -euo pipefail

NS=${NS:-default}
REPORT=${REPORT:-load-report.md}
OUT=${OUT:-load-out}
mkdir -p "$OUT"
export CATALOG_URL=${CATALOG_URL:-http://127.0.0.1:8001} CHECKOUT_URL=${CHECKOUT_URL:-http://127.0.0.1:8003}
HPAS=$(kubectl -n "$NS" get hpa -o jsonpath='{.items[*].metadata.name}')
START=$(date +%s)
echo "time_s,hpa,current,desired,cpu_pct,min,max" > "$OUT/hpa.csv"

sample() {
  kubectl -n "$NS" get hpa -o json | jq -r --arg t "$(( $(date +%s) - START ))" '.items[] |
    [$t, .metadata.name, (.status.currentReplicas // 0), (.status.desiredReplicas // 0),
     ((.status.currentMetrics // [])[0].resource.current.averageUtilization // "n/a"),
     .spec.minReplicas, .spec.maxReplicas] | @csv' | tr -d '"' >> "$OUT/hpa.csv" || true
}
sampler() { while true; do sample; sleep 10; done; }

sampler & SAMPLER=$!
trap 'kill $SAMPLER 2>/dev/null || true' EXIT

echo "== phase 1: ramp"
PHASE=ramp SUMMARY="$OUT/ramp.json" k6 run --quiet "$(dirname "$0")/load.js" || true

echo "== phase 2: steady load, delete one catalog pod after 15 s"
PHASE=steady SUMMARY="$OUT/steady.json" k6 run --quiet "$(dirname "$0")/load.js" &
K6=$!
sleep 15
VICTIM=$(kubectl -n "$NS" get pods -l app.kubernetes.io/name=catalog --field-selector=status.phase=Running \
  -o jsonpath='{.items[0].metadata.name}')
echo "deleting $VICTIM"
kubectl -n "$NS" delete pod "$VICTIM" --wait=false
wait $K6 || true
sample
kubectl -n "$NS" rollout status deploy -l app.kubernetes.io/name=catalog --timeout=120s || true
echo "$VICTIM" > "$OUT/victim"

python3 - "$OUT" "$REPORT" <<'PY'
import csv, json, sys
out, report = sys.argv[1], sys.argv[2]
rows = list(csv.DictReader(open(f"{out}/hpa.csv")))
times = sorted({int(r["time_s"]) for r in rows})
names = sorted({r["hpa"] for r in rows})
by = {(int(r["time_s"]), r["hpa"]): r for r in rows}
lines = ["## Kubernetes autoscaling under load (kind: 1 control plane + 2 workers)", "",
         "Replicas are `current/desired`, CPU is the average utilisation against the pods' CPU request.", ""]
header = "| time (s) | " + " | ".join(f"{n.split('rush-hour-')[-1]} replicas | CPU" for n in names) + " |"
lines += [header, "|---|" + "---|---|" * len(names)]
for t in times:
    cells = []
    for n in names:
        r = by.get((t, n))
        cells += [f"{r['current']}/{r['desired']}", f"{r['cpu_pct']}%"] if r else ["", ""]
    lines.append(f"| {t} | " + " | ".join(cells) + " |")
peak = {}
for n in names:
    rs = [r for r in rows if r["hpa"] == n]
    peak[n] = (int(rs[0]["min"]), max(max(int(r["current"]), int(r["desired"])) for r in rs))
lines += ["", "| HPA | min | peak replicas |", "|---|---|---|"]
lines += [f"| {n} | {lo} | {hi} |" for n, (lo, hi) in peak.items()]
ramp, steady = (json.load(open(f"{out}/{p}.json")) for p in ("ramp", "steady"))
def row(label, d):
    return (f"| {label} | {d['requests']:.0f} | {d['rps']:.0f} | {d['catalog_error_rate']*100:.2f}% | "
            f"{d['catalog_p95_ms']:.0f} ms | {d['checkout_error_rate']*100:.2f}% | {d['checkout_p95_ms']:.0f} ms |")
victim = open(f"{out}/victim").read().strip()
lines += ["", "### Load results", "",
          "| phase | requests | req/s | catalog errors | catalog p95 | checkout errors | checkout p95 |",
          "|---|---|---|---|---|---|---|",
          row("ramp (HPA scale-up)", ramp), row(f"steady, pod `{victim}` deleted at +15 s", steady)]
open(report, "w").write("\n".join(lines) + "\n")
cat = next(n for n in names if n.endswith("-catalog"))
print(f"catalog min={peak[cat][0]} peak={peak[cat][1]}")
sys.exit(0 if peak[cat][1] > peak[cat][0] else 3)
PY
