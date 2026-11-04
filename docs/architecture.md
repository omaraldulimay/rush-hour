# Peak Ready – Architecture

Peak Ready is the platform behind a fictional fashion shop, **Nordkai Fashion**, built to prove one thing:
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
| 4 | Prometheus, Grafana, alerts, SLOs | Dashboards |
| 5 | Argo Rollouts canary with automatic rollback | Video: bad release stopped |
| 6 | k6 game day with fault injection, readiness report | Report with numbers |
| 7 | Terraform for AWS (EKS, RDS, ElastiCache), validated only | "Could run in a real cloud" |

## Honest limits

Everything runs on one GitHub Actions machine (4 CPUs, 16 GB). Absolute numbers are small; the
behaviour – what breaks first and how the system reacts – is what the project demonstrates. The
payment provider is simulated. The cloud setup is described in Terraform but not applied.
