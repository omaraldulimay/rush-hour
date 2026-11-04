-- released_at: set once the inventory reservation of a failed order was given back, so a crash
-- between "failed" and the release is found (and repaired) by the order worker's sweep.
-- Existing failed orders stay NULL on purpose: the sweep releases them once (a no-op if done).
ALTER TABLE orders.orders ADD COLUMN released_at timestamptz;

CREATE INDEX orders_unreleased_idx ON orders.orders (updated_at)
    WHERE status = 'failed' AND released_at IS NULL;

-- Orders whose customer still has to be refunded (failure_reason = 'refund_required').
CREATE INDEX orders_refund_required_idx ON orders.orders (order_id)
    WHERE status = 'failed' AND failure_reason = 'refund_required';
