CREATE SCHEMA IF NOT EXISTS orders;

CREATE TABLE orders.orders (
    order_id         text PRIMARY KEY,
    cart_id          text NOT NULL,
    idempotency_key  text UNIQUE,
    status           text NOT NULL
                     CHECK (status IN ('created', 'paid', 'failed', 'payment_pending')),
    total_cents      bigint NOT NULL,
    currency         char(3) NOT NULL DEFAULT 'EUR',
    payment_attempts integer NOT NULL DEFAULT 0,
    failure_reason   text,
    confirmed_at     timestamptz,
    created_at       timestamptz NOT NULL DEFAULT now(),
    updated_at       timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX orders_status_idx ON orders.orders (status);

CREATE TABLE orders.order_items (
    order_id          text NOT NULL REFERENCES orders.orders (order_id) ON DELETE CASCADE,
    sku               text NOT NULL,
    qty               integer NOT NULL CHECK (qty > 0),
    unit_price_cents  integer NOT NULL,
    PRIMARY KEY (order_id, sku)
);
