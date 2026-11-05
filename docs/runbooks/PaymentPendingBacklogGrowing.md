# PaymentPendingBacklogGrowing

**What it means.** Orders waiting for a payment retry keep growing for 5 minutes.

**How to check.** *Sale night*: retry queue, payment retries by outcome. Is the breaker open or the provider failing?

**What to do.** Fix the payment problem first. If retries succeed but the queue still grows, add order-worker replicas or raise the worker's batch.

**Recovery.** The retry queue falls; orders become paid or fail with a reason after the maximum attempts.
