# PgBouncerClientsWaiting

**What it means.** More than 5 clients wait for a free server connection in PgBouncer. Needs pgbouncer_exporter (not installed by default).

**How to check.** `SHOW POOLS;` on the PgBouncer admin console: `cl_waiting`. Long transactions in Postgres (`pg_stat_activity`).

**What to do.** Kill or fix the long transaction; raise `defaultPoolSize` only if Postgres has headroom.

**Recovery.** `cl_waiting` is 0 again.
