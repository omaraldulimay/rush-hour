"""Apply every service's migrations in order: python -m libs.common.migrate"""

import asyncio
import hashlib
import logging
from dataclasses import dataclass
from pathlib import Path

import asyncpg

from libs.common.logging import configure_logging
from libs.common.settings import database_url

SERVICES_DIR = Path(__file__).resolve().parents[2] / "services"
# Order matters only if a service references another's objects; today none does.
SERVICE_ORDER = ["catalog", "inventory", "checkout"]
LOCK_ID = 727_274_001

log = logging.getLogger("migrate")


@dataclass(frozen=True)
class Migration:
    service: str
    name: str
    sql: str

    @property
    def checksum(self) -> str:
        return hashlib.sha256(self.sql.encode()).hexdigest()


def discover(services_dir: Path = SERVICES_DIR) -> list[Migration]:
    found: dict[str, list[Migration]] = {}
    for path in sorted(services_dir.glob("*/migrations/*.sql")):
        service = path.parent.parent.name
        found.setdefault(service, []).append(Migration(service, path.name, path.read_text()))
    ordered = [s for s in SERVICE_ORDER if s in found] + sorted(set(found) - set(SERVICE_ORDER))
    return [m for s in ordered for m in found[s]]


async def apply_all(dsn: str, migrations: list[Migration] | None = None) -> list[str]:
    """Returns the names ("service/file") that were newly applied."""
    migrations = discover() if migrations is None else migrations
    conn = await asyncpg.connect(dsn)
    applied_now: list[str] = []
    try:
        await conn.execute("SELECT pg_advisory_lock($1)", LOCK_ID)  # parallel runners queue up
        await conn.execute(
            """CREATE TABLE IF NOT EXISTS public.schema_migrations (
                   service text NOT NULL,
                   name text NOT NULL,
                   checksum text NOT NULL,
                   applied_at timestamptz NOT NULL DEFAULT now(),
                   PRIMARY KEY (service, name))"""
        )
        done = {
            (r["service"], r["name"]): r["checksum"]
            for r in await conn.fetch(
                "SELECT service, name, checksum FROM public.schema_migrations"
            )
        }
        for m in migrations:
            key = (m.service, m.name)
            if key in done:
                if done[key] != m.checksum:
                    raise RuntimeError(
                        f"migration {m.service}/{m.name} changed after being applied"
                    )
                continue
            async with conn.transaction():
                await conn.execute(m.sql)
                await conn.execute(
                    "INSERT INTO public.schema_migrations (service, name, checksum) "
                    "VALUES ($1, $2, $3)",
                    m.service,
                    m.name,
                    m.checksum,
                )
            applied_now.append(f"{m.service}/{m.name}")
            log.info("applied migration", extra={"migration": f"{m.service}/{m.name}"})
        await conn.execute("SELECT pg_advisory_unlock($1)", LOCK_ID)
    finally:
        await conn.close()
    return applied_now


def main() -> None:
    configure_logging("migrate")
    applied = asyncio.run(apply_all(database_url()))
    log.info("migrations done", extra={"applied": len(applied)})


if __name__ == "__main__":
    main()
