CREATE SCHEMA IF NOT EXISTS inventory;

CREATE TABLE inventory.stock (
    sku         text PRIMARY KEY,
    -- Safety net: even a buggy query can never drive stock below zero.
    available   integer NOT NULL CHECK (available >= 0),
    updated_at  timestamptz NOT NULL DEFAULT now()
);

-- order_id as primary key is what makes reservations idempotent.
CREATE TABLE inventory.reservations (
    order_id    text PRIMARY KEY,
    status      text NOT NULL DEFAULT 'reserved'
                CHECK (status IN ('reserved', 'committed', 'released')),
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE inventory.reservation_items (
    order_id  text NOT NULL REFERENCES inventory.reservations (order_id) ON DELETE CASCADE,
    sku       text NOT NULL,
    qty       integer NOT NULL CHECK (qty > 0),
    PRIMARY KEY (order_id, sku)
);
