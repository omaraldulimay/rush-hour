# SLOFastBurn

**What it means.** The error budget of an SLO burns 14.4x too fast (1 h and 5 m windows). At this pace a 30-day budget is gone in about two days.

**How to check.** Open the *SLOs & error budget* dashboard and find the SLO in the alert label `slo`. Availability: look at 5xx per service on *Shop overview*. Latency: look at p95/p99 per route and at DB pool usage (*Data tier*).

**What to do.** Find the cause first: a bad deploy (roll back), a slow dependency (payment: see PaymentBreakerOpen), a saturated database (see DBPoolSaturated). Scale the affected service if CPU bound.

**Recovery.** Burn rate 5m drops below 14.4, the alert resolves within minutes.
