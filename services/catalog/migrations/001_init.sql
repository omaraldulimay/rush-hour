CREATE SCHEMA IF NOT EXISTS catalog;

CREATE TABLE catalog.products (
    sku           text PRIMARY KEY,
    name          text NOT NULL,
    category      text NOT NULL,
    price_cents   integer NOT NULL CHECK (price_cents > 0),
    currency      char(3) NOT NULL DEFAULT 'EUR',
    limited_drop  boolean NOT NULL DEFAULT false,
    created_at    timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX products_category_sku_idx ON catalog.products (category, sku);
