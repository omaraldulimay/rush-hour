from prometheus_client import REGISTRY

from scripts import seed
from tests.conftest import Stack


def cache_count(result: str) -> float:
    return REGISTRY.get_sample_value("catalog_cache_requests_total", {"result": result}) or 0.0


async def test_product_cache_miss_then_hit(stack: Stack) -> None:
    await stack.add_product("A", stock=1, price_cents=5_000)
    hits, misses = cache_count("hit"), cache_count("miss")

    first = await stack.catalog.get("/products/A")
    second = await stack.catalog.get("/products/A")

    assert first.json() == second.json()
    assert first.json()["price_cents"] == 5_000
    assert cache_count("miss") - misses == 1
    assert cache_count("hit") - hits == 1
    assert 0 < await stack.redis.ttl("catalog:product:A") <= 30


async def test_cache_is_actually_used(stack: Stack) -> None:
    await stack.add_product("A", stock=1, price_cents=5_000)
    await stack.catalog.get("/products/A")
    await stack.pool.execute("UPDATE catalog.products SET price_cents = 1 WHERE sku = 'A'")
    assert (await stack.catalog.get("/products/A")).json()["price_cents"] == 5_000
    await stack.redis.flushall()
    assert (await stack.catalog.get("/products/A")).json()["price_cents"] == 1


async def test_list_filter_pagination_and_404(stack: Stack) -> None:
    for i in range(5):
        await stack.add_product(f"S{i}", 1, category="sneakers")
    await stack.add_product("J1", 1, category="jackets")

    sneakers = (await stack.catalog.get("/products?category=sneakers&limit=2&offset=1")).json()
    assert [p["sku"] for p in sneakers["items"]] == ["S1", "S2"]
    assert len((await stack.catalog.get("/products")).json()["items"]) == 6
    assert (await stack.catalog.get("/products/missing")).status_code == 404
    assert (await stack.catalog.get("/products?limit=1000")).status_code == 422


async def test_list_is_cached(stack: Stack) -> None:
    await stack.add_product("A", 1)
    hits = cache_count("hit")
    await stack.catalog.get("/products?category=sneakers")
    await stack.catalog.get("/products?category=sneakers")
    assert cache_count("hit") - hits == 1


def test_seed_data_is_deterministic_and_shaped() -> None:
    first, second = seed.generate(), seed.generate()
    assert first == second
    assert len(first) == 2000 and len({p.sku for p in first}) == 2000
    assert {p.category for p in first} == {"sneakers", "jackets", "knitwear", "accessories"}
    hot = [p for p in first if p.limited_drop]
    assert len(hot) == 5 and all(10 <= p.stock <= 50 for p in hot)
    assert all(p.stock >= 20 for p in first if not p.limited_drop)
    assert all(1_000 <= p.price_cents <= 40_000 for p in first)


async def snapshot(pool) -> tuple:
    products = await pool.fetch(
        "SELECT sku, name, category, price_cents, currency, limited_drop "
        "FROM catalog.products ORDER BY sku"
    )
    stock = await pool.fetch("SELECT sku, available FROM inventory.stock ORDER BY sku")
    return [tuple(r) for r in products], [tuple(r) for r in stock]


async def test_seed_is_idempotent(stack: Stack) -> None:
    assert await seed.seed(stack.pool) == 2000
    once = await snapshot(stack.pool)
    await seed.seed(stack.pool)
    assert await snapshot(stack.pool) == once
    assert len(once[0]) == 2000


async def test_reseed_keeps_live_stock_unless_reset(stack: Stack) -> None:
    await seed.seed(stack.pool)
    hot_sku = await stack.pool.fetchval(
        "SELECT sku FROM catalog.products WHERE limited_drop LIMIT 1"
    )
    seeded = await stack.stock(hot_sku)
    await stack.pool.execute("UPDATE inventory.stock SET available = 0 WHERE sku = $1", hot_sku)

    await seed.seed(stack.pool)
    assert await stack.stock(hot_sku) == 0
    await seed.seed(stack.pool, reset_stock=True)
    assert await stack.stock(hot_sku) == seeded
