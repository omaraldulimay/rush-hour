# NoPaidOrders

**What it means.** Checkout accepts orders but none became paid in 5 minutes: the shop earns nothing.

**How to check.** Order statuses on *Sale night*. Mostly `payment_pending`: payment problem. Mostly `failed`: look at the `failure_reason` column in `orders.orders`.

**What to do.** Payment provider problem: see PaymentBreakerOpen. Otherwise check checkout and inventory logs for the first error.

**Recovery.** Revenue per minute is above 0 again; the alert resolves.
