# DBPoolSaturated

**What it means.** A service uses over 90 % of its asyncpg pool for 3 minutes; requests queue for a connection.

**How to check.** *Data tier*: pool usage by service. Slow queries or a slow downstream call that holds a connection?

**What to do.** Find the slow transaction. Scale the service out (each replica has its own pool) before raising the pool size; check Postgres max_connections / PgBouncer.

**Recovery.** Pool usage stays below 90 %.
