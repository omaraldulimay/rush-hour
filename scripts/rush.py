"""Realistic sale-night load: python -m scripts.rush --users 50 --minutes 3

Every virtual user repeats: think, then pick one action - 90 % browse, 8 % add to cart, 2 % check
out. The run starts with a quiet warm-up (a fifth of the users), then everybody arrives at once.
Checkouts mostly go for the limited-drop items so the stock drains and sells out.

The Control Room starts and stops the same `Rush` object as a background task.
"""

import argparse
import asyncio
import contextlib
import random
import time
import uuid
from dataclasses import dataclass, field

import httpx

from scripts.seed import CATALOG, PRODUCTS_PER_CATEGORY, generate

MIX = {"browse": 0.90, "cart": 0.08, "checkout": 0.02}
HOT_SHARE = 0.7  # share of checkouts that go for a limited-drop item
WARMUP_SHARE = 0.2  # share of the run (and of the users) before the jump
CATEGORIES = list(CATALOG)
HOT_SKUS = [p.sku for p in generate() if p.limited_drop]


def pick_action(rng: random.Random) -> str:
    return rng.choices(list(MIX), weights=list(MIX.values()))[0]


def regular_sku(rng: random.Random) -> str:
    """Accessories are plentiful (50-600 pieces) and not limited, so stock never decides."""
    return f"ACC-{rng.randint(1, PRODUCTS_PER_CATEGORY):04d}"


@dataclass
class RushConfig:
    users: int = 50
    duration_s: float = 180.0
    catalog_url: str = "http://localhost:8001"
    checkout_url: str = "http://localhost:8003"
    think_s: float = 0.8  # mean think time
    seed: int | None = None
    transport: httpx.AsyncBaseTransport | None = None  # tests plug in a fake shop


@dataclass
class Rush:
    config: RushConfig
    counts: dict[str, int] = field(
        default_factory=lambda: {
            k: 0
            for k in (
                "browse",
                "cart",
                "checkout",
                "requests",
                "errors",
                "paid",
                "payment_pending",
                "failed",
                "sold_out",
            )
        }
    )
    phase: str = "idle"  # idle | warmup | rush | done | stopped
    started_at: float = 0.0
    finished_at: float = 0.0
    active_users: int = 0

    def __post_init__(self) -> None:
        self._stop = asyncio.Event()
        self._rng = random.Random(self.config.seed)

    # --- control -------------------------------------------------------------------------------
    def stop(self) -> None:
        self._stop.set()

    @property
    def running(self) -> bool:
        return self.phase in ("warmup", "rush")

    def progress(self) -> dict:
        end = self.finished_at or time.monotonic()
        elapsed = min(end - self.started_at, self.config.duration_s) if self.started_at else 0.0
        return {
            "phase": self.phase,
            "running": self.running,
            "users": self.config.users,
            "active_users": self.active_users,
            "duration_s": self.config.duration_s,
            "elapsed_s": round(elapsed, 1),
            "counts": dict(self.counts),
        }

    # --- the run -------------------------------------------------------------------------------
    async def run(self) -> None:
        cfg = self.config
        self.started_at = time.monotonic()
        deadline = self.started_at + cfg.duration_s
        self.phase = "warmup"
        warm_users = max(1, round(cfg.users * WARMUP_SHARE))
        limits = httpx.Limits(max_connections=cfg.users * 2 + 10)
        tasks: list[asyncio.Task] = []
        stopped = False
        try:
            async with httpx.AsyncClient(
                timeout=10, limits=limits, transport=cfg.transport
            ) as client:

                def spawn(count: int) -> None:
                    for _ in range(count):
                        seed = self._rng.random()
                        tasks.append(asyncio.create_task(self._user(client, deadline, seed)))

                spawn(warm_users)
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._stop.wait(), cfg.duration_s * WARMUP_SHARE)
                if not self._stop.is_set():
                    self.phase = "rush"
                    spawn(cfg.users - warm_users)  # the jump
                    remaining = deadline - time.monotonic()
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(self._stop.wait(), max(0.0, remaining))
                stopped = self._stop.is_set()
                self._stop.set()
                await asyncio.gather(*tasks, return_exceptions=True)
        finally:
            self.finished_at = time.monotonic()
            self.phase = "stopped" if stopped else "done"
            self.active_users = 0

    async def _user(self, client: httpx.AsyncClient, deadline: float, seed: float) -> None:
        rng = random.Random(seed)
        cart_id = f"rush-{uuid.uuid4().hex[:12]}"
        self.active_users += 1
        try:
            while not self._stop.is_set() and time.monotonic() < deadline:
                try:
                    await asyncio.wait_for(
                        self._stop.wait(), rng.expovariate(1 / self.config.think_s)
                    )
                    return
                except TimeoutError:
                    pass
                action = pick_action(rng)
                self.counts[action] += 1
                try:
                    if action == "browse":
                        await self._browse(client, rng)
                    elif action == "cart":
                        await self._put(client, cart_id, self._pick_sku(rng, HOT_SHARE / 2), rng)
                    else:
                        cart_id = await self._checkout(client, cart_id, rng)
                except httpx.HTTPError:
                    self.counts["errors"] += 1
        finally:
            self.active_users -= 1

    @staticmethod
    def _pick_sku(rng: random.Random, hot_share: float) -> str:
        return rng.choice(HOT_SKUS) if rng.random() < hot_share else regular_sku(rng)

    def _ok(self, resp: httpx.Response) -> bool:
        self.counts["requests"] += 1
        if resp.status_code >= 400:
            self.counts["errors"] += 1
            return False
        return True

    async def _browse(self, client: httpx.AsyncClient, rng: random.Random) -> None:
        base = self.config.catalog_url
        if rng.random() < 0.3:
            self._ok(await client.get(f"{base}/products/{self._pick_sku(rng, 0.2)}"))
        else:
            params = {"category": rng.choice(CATEGORIES), "limit": 20}
            params["offset"] = rng.randint(0, PRODUCTS_PER_CATEGORY - 20)
            self._ok(await client.get(f"{base}/products", params=params))

    async def _put(self, client: httpx.AsyncClient, cart_id: str, sku: str, rng: random.Random):
        qty = 1 if sku in HOT_SKUS else rng.randint(1, 2)
        resp = await client.put(
            f"{self.config.checkout_url}/carts/{cart_id}/items", json={"sku": sku, "qty": qty}
        )
        return self._ok(resp)

    async def _checkout(self, client: httpx.AsyncClient, cart_id: str, rng: random.Random) -> str:
        """Fill a cart, pay it. Returns the cart id for the user's next purchase."""
        sku = self._pick_sku(rng, HOT_SHARE)
        if not await self._put(client, cart_id, sku, rng):
            return cart_id
        resp = await client.post(
            f"{self.config.checkout_url}/checkout",
            json={"cart_id": cart_id},
            headers={"Idempotency-Key": uuid.uuid4().hex},
        )
        self.counts["requests"] += 1
        if resp.status_code in (200, 201):
            status = resp.json().get("status", "failed")
            self.counts[status if status in self.counts else "failed"] += 1
            return f"rush-{uuid.uuid4().hex[:12]}"
        if resp.status_code == 409 and resp.json().get("error") == "sold_out":
            self.counts["sold_out"] += 1
            return cart_id
        self.counts["errors"] += 1
        return cart_id


async def _cli(args: argparse.Namespace) -> None:
    rush = Rush(
        RushConfig(
            users=args.users,
            duration_s=args.minutes * 60,
            catalog_url=args.catalog_url,
            checkout_url=args.checkout_url,
            seed=args.seed,
        )
    )
    task = asyncio.create_task(rush.run())
    try:
        while not task.done():
            await asyncio.sleep(5)
            p = rush.progress()
            print(
                f"[{p['phase']:>6}] {p['elapsed_s']:>5.0f}s users={p['active_users']:>3}",
                p["counts"],
            )
    except (KeyboardInterrupt, asyncio.CancelledError):
        rush.stop()
    await task
    print("finished:", rush.progress())


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--users", type=int, default=50)
    parser.add_argument("--minutes", type=float, default=3.0)
    parser.add_argument("--catalog-url", default="http://localhost:8001")
    parser.add_argument("--checkout-url", default="http://localhost:8003")
    parser.add_argument("--seed", type=int, default=None)
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(_cli(parser.parse_args()))


if __name__ == "__main__":
    main()
