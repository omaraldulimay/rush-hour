# SLOSlowBurn

**What it means.** An SLO burns its budget 6x too fast over 6 h (and 30 m). Not urgent, but it will become an incident.

**How to check.** Same dashboards as SLOFastBurn. Compare the 6 h burn rate with deploys and traffic changes.

**What to do.** Create a ticket, find the slow leak (a flaky dependency, one slow route, a too small pool) and fix it in working hours.

**Recovery.** Burn rate 6h falls below 6 and the alert resolves.
