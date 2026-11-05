# OrderWorkerNotConsuming

**What it means.** The order stream grows and the worker handles nothing (or it does not report at all): no confirmations, retries or releases.

**How to check.** `kubectl get pods -l app.kubernetes.io/name=order-worker`, then its logs. *Data tier*: stream lag.

**What to do.** Restart the worker pod if it is stuck (`kubectl rollout restart`). If Redis or Postgres is down, fix that first. Events are not lost: the stream is durable.

**Recovery.** Stream lag falls to about 0 and worker events resume.
