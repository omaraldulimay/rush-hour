# Peak Ready

Platform behind a fictional fashion shop (Nordkai Fashion) that is built to survive a Black Friday
night. Design and goals: [docs/architecture.md](docs/architecture.md). This repository currently
contains step 1: five services, migrations, seed data, docker compose and tests.

Services: `catalog`, `inventory`, `checkout`, `payment_mock`, `order_worker` (Python 3.12, FastAPI,
asyncpg, Redis).

## Run

```sh
make up      # postgres, redis, migrate + seed (one-shot), then all services
make down    # stop and delete volumes
```

| Service | Host port |
|---|---|
| catalog | 8001 |
| inventory | 8002 |
| checkout | 8003 |
| payment-mock | 8004 |
| order-worker (ops endpoints only) | 8005 |

Without Docker: set `DATABASE_URL` and `REDIS_URL`, then
`python -m libs.common.migrate && python -m scripts.seed` and
`uvicorn services.<name>.app.main:create_app --factory`.
`make seed` re-runs migrate + seed against a running stack (idempotent; live stock is kept,
`python -m scripts.seed --reset-stock` restores seeded levels).

## Test

```sh
make install
make lint
make test
```

Tests need no Docker. Without `DATABASE_URL` they start a throwaway Postgres cluster
(`initdb` + `pg_ctl` from `/Library/PostgreSQL/16/bin`, override with `PG_BIN`) on a free port and
delete it afterwards. Redis is `fakeredis` unless `REDIS_URL` is set.

## Endpoints

Every service: `GET /healthz`, `GET /readyz`, `GET /metrics`.

**catalog**
- `GET /products?category=&limit=&offset=`
- `GET /products/{sku}`

**inventory**
- `POST /reservations` `{order_id, items:[{sku, qty}]}` -> 201, 200 on replay, 409 `sold_out`
- `POST /reservations/{order_id}/release`, `POST /reservations/{order_id}/commit`
- `GET /stock/{sku}`

**checkout**
- `PUT /carts/{cart_id}/items` `{sku, qty}`, `GET /carts/{cart_id}`, `DELETE /carts/{cart_id}/items/{sku}`
- `POST /checkout` `{cart_id}` with optional `Idempotency-Key` header
- `GET /orders/{order_id}`

**payment-mock**
- `POST /payments` `{order_id, amount_cents}` -> `{status: paid|declined}` (503 on injected error)
- `POST /payments/{order_id}/refund` -> `{status: refunded}` (idempotent; 404 unknown, 409 declined, same fault injection)
- `GET|POST /admin/fault` `{latency_ms, error_rate, decline_rate}`

**order-worker** has no business API; it consumes the Redis Stream `orders`.

## Configuration

Environment variables only. The ones you will most likely touch: `DATABASE_URL`, `REDIS_URL`,
`CATALOG_URL`, `INVENTORY_URL`, `PAYMENT_URL`, `PAYMENT_TIMEOUT_S` (2), `BREAKER_FAILURE_THRESHOLD`
(5), `BREAKER_WINDOW_S` (30), `BREAKER_COOLDOWN_S` (10), `PAYMENT_LATENCY_MS`, `PAYMENT_ERROR_RATE`,
`PAYMENT_DECLINE_RATE`, `WORKER_MAX_ATTEMPTS` (5), `RETRY_BASE_DELAY_S` (5), `CACHE_TTL_S` (30), `SWEEP_INTERVAL_S` (30), `STALE_CREATED_AFTER_S` (120),
`ORPHAN_PENDING_AFTER_S` (120), `SWEEP_BATCH_SIZE` (100). The sweeper repairs stale `created`/`payment_pending`/unreleased
orders and retries refunds (`order_worker_swept_total{action}`; refunds: `payment_refunds_total`).
