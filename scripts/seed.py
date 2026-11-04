"""Seed the catalog and stock: python -m scripts.seed

Deterministic (fixed random seed) and idempotent. Re-running refreshes product data but keeps
live stock untouched; pass --reset-stock to restore the seeded stock levels.
"""

import argparse
import asyncio
import logging
import random
from dataclasses import dataclass

import asyncpg

from libs.common.logging import configure_logging
from libs.common.settings import database_url

SEED = 20_241_129
PRODUCTS_PER_CATEGORY = 500
HOT_ITEMS = 5

log = logging.getLogger("seed")

KINDS = {
    "sneakers": ["Runner", "Court", "Trail", "Retro", "Low", "High-Top"],
    "jackets": ["Parka", "Bomber", "Windbreaker", "Trench", "Puffer", "Field"],
    "knitwear": ["Pullover", "Cardigan", "Turtleneck", "Zip Knit", "Vest", "Beanie"],
    "accessories": ["Scarf", "Tote", "Belt", "Cap", "Gloves", "Wallet"],
}
# category -> (sku prefix, name parts, price range in EUR, stock range)
CATALOG = {
    "sneakers": ("SNK", KINDS["sneakers"], (79, 189), (30, 300)),
    "jackets": ("JKT", KINDS["jackets"], (99, 349), (20, 200)),
    "knitwear": ("KNT", KINDS["knitwear"], (39, 169), (40, 400)),
    "accessories": ("ACC", KINDS["accessories"], (15, 99), (50, 600)),
}
MATERIALS = ["Merino", "Organic Cotton", "Recycled Nylon", "Suede", "Canvas", "Leather", "Wool"]
COLORS = ["Fjord Blue", "Sand", "Charcoal", "Moss", "Off-White", "Black", "Rust", "Slate"]


@dataclass(frozen=True)
class SeedProduct:
    sku: str
    name: str
    category: str
    price_cents: int
    limited_drop: bool
    stock: int


def generate(seed: int = SEED) -> list[SeedProduct]:
    rng = random.Random(seed)
    products: list[SeedProduct] = []
    for category, (prefix, kinds, (low, high), (stock_low, stock_high)) in CATALOG.items():
        for n in range(1, PRODUCTS_PER_CATEGORY + 1):
            name = f"{rng.choice(COLORS)} {rng.choice(MATERIALS)} {rng.choice(kinds)}"
            # Retail-style price: whole euros ending in .95 or .99
            price_cents = rng.randint(low, high) * 100 - rng.choice([5, 1])
            stock = rng.randint(stock_low, stock_high)
            products.append(
                SeedProduct(f"{prefix}-{n:04d}", name, category, price_cents, False, stock)
            )
    # Hot items: the first sneakers at fixed positions become "limited drop" with 10-50 pieces.
    sneakers = [p for p in products if p.category == "sneakers"]
    for i in range(HOT_ITEMS):
        base = sneakers[i * 7]
        index = products.index(base)
        products[index] = SeedProduct(
            base.sku,
            f"Nordkai Limited Drop {i + 1:02d} {base.name}",
            base.category,
            base.price_cents + 5000,
            True,
            rng.randint(10, 50),
        )
    return products


async def seed(pool: asyncpg.Pool, *, reset_stock: bool = False) -> int:
    products = generate()
    async with pool.acquire() as conn, conn.transaction():
        await conn.executemany(
            """INSERT INTO catalog.products (sku, name, category, price_cents, limited_drop)
               VALUES ($1, $2, $3, $4, $5)
               ON CONFLICT (sku) DO UPDATE SET name = EXCLUDED.name, category = EXCLUDED.category,
                   price_cents = EXCLUDED.price_cents, limited_drop = EXCLUDED.limited_drop""",
            [(p.sku, p.name, p.category, p.price_cents, p.limited_drop) for p in products],
        )
        conflict = (
            "DO UPDATE SET available = EXCLUDED.available, updated_at = now()"
            if reset_stock
            else "DO NOTHING"
        )
        await conn.executemany(
            f"INSERT INTO inventory.stock (sku, available) VALUES ($1, $2) "
            f"ON CONFLICT (sku) {conflict}",
            [(p.sku, p.stock) for p in products],
        )
    return len(products)


async def main(reset_stock: bool) -> None:
    pool = await asyncpg.create_pool(database_url(), min_size=1, max_size=2)
    try:
        count = await seed(pool, reset_stock=reset_stock)
    finally:
        await pool.close()
    log.info("seeded", extra={"products": count, "hot_items": HOT_ITEMS})


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--reset-stock", action="store_true")
    args = parser.parse_args()
    configure_logging("seed")
    asyncio.run(main(args.reset_stock))
