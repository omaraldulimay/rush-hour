# PaymentBreakerOpen

**What it means.** Checkout stopped calling the payment provider after repeated failures. Orders are saved as `payment_pending` and retried by the order worker; nobody is charged yet.

**How to check.** *Sale night* dashboard: breaker state and payment outcomes. `curl <payment-mock>:8000/admin/fault` shows an injected fault. Checkout logs show `payment failed`.

**What to do.** Fix or wait for the provider. In the demo reset the fault (Control Room: *Reset incident*). Retries continue automatically with backoff; do not restart checkout.

**Recovery.** Breaker goes closed, the retry queue shrinks, payment_pending orders turn paid. The alert resolves about a minute after the breaker closes.
