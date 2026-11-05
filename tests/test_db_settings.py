import pytest

from libs.common import db


@pytest.mark.parametrize(("env", "expected"), [(None, 100), ("0", 0)])
async def test_statement_cache_size_is_configurable(
    monkeypatch: pytest.MonkeyPatch, env: str | None, expected: int
) -> None:
    """PgBouncer (transaction mode) needs DB_STATEMENT_CACHE_SIZE=0; the default stays asyncpg's."""
    seen: dict = {}

    async def fake_create_pool(dsn: str, **kwargs: object) -> str:
        seen.update(kwargs)
        return "pool"

    monkeypatch.setattr(db.asyncpg, "create_pool", fake_create_pool)
    if env is None:
        monkeypatch.delenv("DB_STATEMENT_CACHE_SIZE", raising=False)
    else:
        monkeypatch.setenv("DB_STATEMENT_CACHE_SIZE", env)

    await db.create_pool("postgresql://x")

    assert seen["statement_cache_size"] == expected
