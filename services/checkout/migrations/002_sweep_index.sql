-- Keeps the order worker's stale-order sweep cheap: it only looks at the two "unfinished"
-- statuses, ordered by age, and the index ignores the (vast majority of) finished orders.
CREATE INDEX orders_unfinished_idx ON orders.orders (status, updated_at)
    WHERE status IN ('created', 'payment_pending');
