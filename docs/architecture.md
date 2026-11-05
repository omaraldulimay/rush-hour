# Rush Hour – Architecture

Rush Hour (named after the moment everybody arrives at once) is the platform behind a fictional fashion shop, **Nordkai Fashion**, built to prove one thing:
the shop survives its busiest night of the year – the Black Friday sale that starts at 00:00 –
including a bad deploy and a slow payment provider in the middle of it.

The shop is only the example. The same problems (sudden traffic peaks, risky releases, slow
dependencies) hit banks on pay day, ticketing on a concert drop and logistics before Christmas.

## Goals

1. **Behave like a real shop under load**: several services, a shared database as the real
   bottleneck, a cache, a queue and an external payment provider.
2. **Never oversell**: 500 customers buying the last 10 pairs of a limited sneaker must result in
   exactly 10 orders and stock 0 – never -3.
3. **Degrade instead of fail**: if payment slows down, browsing and cart keep working and orders are
   finished later.
4. **Ship safely during the peak**: canary releases with automatic rollback.
5. **Prove it with numbers**: a reproducible game day that ends in a readiness report.
6. **Cost 0 €**: everything runs in GitHub Actions and is thrown away afterwards.

## Services

| Service | Responsibility | Talks to |
|---|---|---|
| `catalog` | Product list and detail pages – the most called service | Postgres (`catalog` schema), Redis (cache-aside, 30 s TTL) |
| `inventory` | Stock per SKU, reservations, releases | Postgres (`inventory` schema) |
| `checkout` | Cart, placing orders, payment call | Redis (carts), Postgres (`orders` schema), `inventory`, `payment-mock`, Redis Stream `orders` |
| `payment-mock` | Stand-in for an external provider (Stripe/PayPal) with injectable latency and errors | – |
| `order-worker` | Background jobs: confirmation "e-mail", retrying pending payments, releasing reservations of failed orders | Redis Stream `orders`, Postgres, `inventory`, `payment-mock` |

One Postgres instance with a schema per service: each service owns its tables and never reads
another service's schema. This is the common real-world compromise between "one database per
service" and one shared database, and it keeps the database the shared bottleneck – which is exactly
what happens on Black Friday.

## Key flows

### Checkout

```
client ──POST /checkout──▶ checkout
                             ├─▶ inventory  POST /reservations   (all items or nothing)
                             ├─▶ payment    POST /payments       (timeout 2 s, circuit breaker)
                             │     ├─ paid      → order "paid"
                             │     ├─ declined  → order "failed", release reservation
                             │     └─ timeout / breaker open → order "payment_pending"
                             └─▶ Redis Stream "orders" (event)
order-worker ◀── stream: sends confirmation, retries "payment_pending" orders,
                 releases the reservation after the final failed attempt
```

#### Stale orders

A crash can leave an order half-done with its stock still reserved. Every `order-worker` replica
therefore runs a sweeper (every `SWEEP_INTERVAL_S`, 30 s): orders still `created` after
`STALE_CREATED_AFTER_S` (120 s) are failed with `checkout_abandoned` (compare-and-set) and their
reservation is released; `payment_pending` orders without a retry entry for `ORPHAN_PENDING_AFTER_S`
(120 s) are put back into the retry set; `failed` orders without `released_at` (crash between
"failed" and the release) are released. Rows are claimed in batches with `FOR UPDATE SKIP LOCKED`,
so replicas never handle the same order twice. A checkout that was merely slow and loses the
compare-and-set undoes its charge via `POST /payments/{id}/refund`, publishes nothing, keeps the
cart and answers `409 order_abandoned`. The same refund happens when the worker's retry pays an order
that was failed meanwhile. If the provider is down the order gets the reason `refund_required` and
the sweep retries until the refund succeeds (then the reason is `refunded`).

### No overselling

Reservation is one transaction with a conditional update per item:

```sql
UPDATE inventory.stock SET available = available - $qty
WHERE sku = $sku AND available >= $qty
```

If any item updates 0 rows, the whole transaction rolls back and the API answers `409 sold_out`.
No read-then-write, so no race. Reservations are **idempotent** by `order_id`: a retried request
returns the existing reservation instead of reserving twice.

## Conventions for every service

- Python 3.12, FastAPI, `asyncpg` with plain SQL, `redis` (asyncio), `httpx` for service calls.
- Endpoints `/healthz` (process alive), `/readyz` (dependencies reachable) and `/metrics`
  (Prometheus: request count and latency by route and status, plus service-specific counters).
- JSON logs to stdout with a `request_id` that is passed on to downstream services
  (`X-Request-ID`).
- Configuration only through environment variables; no secrets in code.
- SQL migrations as numbered files per service, applied by a separate `migrate` command – in
  Kubernetes this becomes a Job that runs before the rollout.
- One Dockerfile for all services (build argument `SERVICE`), non-root user, small image.

## Kubernetes

Helm chart `deploy/helm/rush-hour` (one chart for the whole shop), tested on kind with 1 control plane and 2 workers.

| What | Where |
|---|---|
| catalog, inventory, checkout | Deployments with 2+ replicas, spread over nodes, PodDisruptionBudget `minAvailable: 1`, HPA on CPU |
| payment-mock, order-worker | Deployments with 1 replica (fault state in memory / no HTTP load), no HPA |
| PgBouncer | 2 replicas, transaction pooling; **every service connects to it**, not to Postgres |
| Postgres 16, Redis 7 | StatefulSets with PVCs, official images, **demo only** (RDS / ElastiCache in step 7: `postgres.enabled=false`) |
| migrations + seed | Helm hook Job (`pre-install,pre-upgrade`), connects to Postgres directly |

All pods run non-root with a read-only root filesystem (an `emptyDir` for `/tmp`), no capabilities,
no privilege escalation and the `RuntimeDefault` seccomp profile. Probes: `startupProbe` and
liveness on `/healthz`, readiness on `/readyz`. On shutdown a 5 s `preStop` sleep keeps the pod
serving while the Service endpoint is removed, then uvicorn drains on SIGTERM.

**Ordering.** Helm hooks run before normal resources: the DB Secret, then the in-cluster Postgres
(install only, Helm never touches it again) and then the migration Job (which waits for Postgres
with `pg_isready`). Services and PgBouncer are created after the Job succeeded, so nothing serves
traffic on an unmigrated schema, also not during an upgrade.

**Why PgBouncer.** Every replica holds a pool of up to 10 connections. With the HPA at 6+6+4
replicas that is well over 100 connections, which Postgres cannot serve (each is a process). PgBouncer
multiplexes them onto 20 server connections per pod. In `transaction` mode a server connection is
only held for one transaction, which is the mode that actually saves connections.
What this costs:

- asyncpg caches named prepared statements per connection. The next transaction may run on another
  server connection where the statement does not exist, so `DB_STATEMENT_CACHE_SIZE=0` is set by the
  chart (`statement_cache_size=0`). Compose and the tests keep asyncpg's default.
- Session state does not survive between transactions. Checked in the code: no `LISTEN/NOTIFY`, no
  `SET`/`set_config`, no temp tables, no cursors and no advisory locks in the services. The
  `FOR UPDATE SKIP LOCKED` claims in `libs/common/orders_db.py` are single statements (the
  `UPDATE ... WHERE order_id IN (SELECT ... FOR UPDATE SKIP LOCKED)` runs in its own implicit
  transaction) and the stock reservation is one explicit transaction, so both are safe with pooling.
  The only advisory lock is the migration lock in `libs/common/migrate.py`: session-level, so the
  migration Job bypasses PgBouncer.

**Why this HPA behaviour.** Scale up has no stabilisation window and may double the pods or add 2
every 15 s: the peak starts at 00:00 and each second of lag is lost orders. Scale down waits 5
minutes and removes at most 25 % per minute: a short dip between two waves must not remove capacity
that is needed again 30 s later (flapping means cold pods and reconnect storms on the database).
CPU utilisation is measured against the CPU *request* (100m), so the targets are small on purpose.

**Only for the demo / not done.** In-cluster Postgres and Redis (single replica, no backups, no
failover); the generated password lives in a Secret (a real cloud would use a secret manager); no
Ingress, TLS or NetworkPolicies; the HPAs scale on CPU only.

## Observability

Prometheus scrapes every service (5 s). Rules live once in `observability/prometheus/rules/`; compose and
`make local` load the files, the Helm chart renders the same files into a `PrometheusRule`
(`.Files.Get`), so nothing is copied. `slo.rules.yml` is generated by `gen_slo_rules.py`
(4 SLOs x windows 5m/30m/1h/6h). `promtool test rules` proves each alert fires and stays silent where it should.
Dashboards are generated by `observability/grafana/build_dashboards.py` (JSON, datasource variable, stable uids).

| SLO | Target | SLI |
|---|---|---|
| checkout availability | 99.5 % | non-5xx answers (409 `sold_out` is a success) |
| checkout latency | 99 % under 1 s | share of served requests faster than 1 s |
| catalog availability | 99.9 % | non-5xx answers |
| catalog latency | 99 % under 300 ms | share of served requests faster than 300 ms |

**Why multi-window, multi-burn-rate.** A single threshold either pages for every blip (short window) or notices
an outage after hours (long window). Burn rate = error ratio / error budget. Fast burn (14.4x over 1 h *and* 5 m)
pages: 2 % of a 30-day budget gone in an hour. Slow burn (6x over 6 h *and* 30 m) opens a ticket. The long
window shows the problem is real, the short one that it still happens, so alerts also clear quickly. A traffic
guard (0.1 req/s) ignores tiny samples. "Budget remaining" uses a 6 h demo window because Prometheus keeps hours, not 30 days.

| Alert | Severity | Fires when |
|---|---|---|
| SLOFastBurn | critical (page) | burn rate above 14.4 over 1 h and 5 m |
| SLOSlowBurn | warning (ticket) | burn rate above 6 over 6 h and 30 m |
| PaymentBreakerOpen | critical | payment breaker open for 2 min |
| PaymentPendingBacklogGrowing | warning | retry queue above 10 and growing for 5 min |
| NoPaidOrders | critical | orders accepted but none paid for 5 min |
| OrderWorkerNotConsuming | critical | stream lag with no events handled, or worker missing, 2 min |
| DBPoolSaturated | warning | a pool above 90 % for 3 min |
| PgBouncerClientsWaiting | warning | more than 5 clients wait for 2 min (needs pgbouncer_exporter, not installed by default) |
| HotItemSoldOut | info | a limited-drop item has stock 0 (timeline marker) |
| StockOversold | critical | oversold units above 0 |

Every alert links to a short runbook in `docs/runbooks/`. The Control Room shows firing alerts in its header and timeline.
Not done: Postgres/PgBouncer/Redis exporters (the data-tier dashboard shows "No data" for those panels), real
notification receivers in Alertmanager.

## Seed data

2,000 products across categories (sneakers, jackets, knitwear, accessories) with realistic prices
and stock. Five **hot items** ("limited drop") have only 10–50 pieces – those are what the load test
fights over.

## Load model (step 6)

Real customers, not a flat line: 90 % browse, 8 % add to cart, 2 % check out, with think time
between clicks. Traffic warms up before midnight and jumps at 00:00.

## Roadmap

| Step | Delivers | Visible result |
|---|---|---|
| 1 | Services, migrations, seed data, docker compose, tests incl. oversell test | Green tests, stack runs |
| 2 | CI: lint, tests, image build, Trivy scan, SBOM, signed images | Green check on every commit |
| 3 | Kubernetes (kind in CI), Helm, autoscaling, PgBouncer | Pods scale with load |
| 4 | Prometheus, Grafana, alerts, SLOs (done) | Dashboards |
| 5 | Argo Rollouts canary with automatic rollback | Video: bad release stopped |
| 6 | k6 game day with fault injection, readiness report | Report with numbers |
| 7 | Terraform for AWS (EKS, RDS, ElastiCache), validated only | "Could run in a real cloud" |

## Honest limits

Everything runs on one GitHub Actions machine (4 CPUs, 16 GB). Absolute numbers are small; the
behaviour – what breaks first and how the system reacts – is what the project demonstrates. The
payment provider is simulated. The cloud setup is described in Terraform but not applied.
