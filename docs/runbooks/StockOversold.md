# StockOversold

**What it means.** The core invariant is broken: more units reserved or sold than ever existed.

**How to check.** Control Room *oversold* counter; `inventory.stock` for negative `available`, reservations vs initial stock.

**What to do.** Stop the sale, find the code path that bypassed the conditional UPDATE in `libs`/inventory, correct stock and refund affected orders.

**Recovery.** `drop_oversold_units` is 0 again.
