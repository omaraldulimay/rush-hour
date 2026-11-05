#!/usr/bin/env bash
# Port-forward in the background, log to port-forward-<name>.log, wait until the port answers.
#   port-forward.sh <name> <svc> <local>:<remote> [namespace] [health path]
# Prints the log and fails when the port is not healthy after 60 s.
set -euo pipefail
name=$1 svc=$2 ports=$3 ns=${4:-default} path=${5:-/healthz}
log="port-forward-${name}.log"
nohup kubectl -n "$ns" port-forward "svc/$svc" "$ports" > "$log" 2>&1 &
for _ in $(seq 1 30); do
  if curl -fsS -o /dev/null "http://127.0.0.1:${ports%%:*}${path}" 2>>"$log"; then
    echo "port-forward $name ready on ${ports%%:*}"
    exit 0
  fi
  sleep 2
done
echo "::error::port-forward $name never became healthy"
cat "$log"
exit 1
