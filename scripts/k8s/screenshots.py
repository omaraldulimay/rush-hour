"""Screenshots of the four Grafana dashboards and the Control Room (Playwright, Chromium).

    python scripts/k8s/screenshots.py --out shots [--tag load] [--only overview,slo]

Used by the Kubernetes CI job while the load runs (and again while an alert fires). Grafana only
loads panels that are in the viewport, so the viewport is made as tall as the dashboard.
"""

import argparse
import json
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[2]
DASHBOARDS = ROOT / "observability/grafana/dashboards"
# name -> (dashboard uid, json file)
BOARDS = {
    "overview": ("rh-overview", "shop-overview.json"),
    "slo": ("rh-slo", "slo-error-budget.json"),
    "sale-night": ("rh-sale-night", "sale-night.json"),
    "data-tier": ("rh-data-tier", "data-tier.json"),
}
WIDTH = 1600


def board_height(filename: str) -> int:
    panels = json.loads((DASHBOARDS / filename).read_text())["panels"]
    units = max(p["gridPos"]["y"] + p["gridPos"]["h"] for p in panels)
    return int(units * 38 + 140)  # a grid unit is 30 px plus 8 px gap


def shoot_grafana(browser, base: str, out: Path, tag: str, only: set[str]) -> list[str]:
    done = []
    for name, (uid, filename) in BOARDS.items():
        if name not in only:
            continue
        ctx = browser.new_context(
            viewport={"width": WIDTH, "height": board_height(filename)}, device_scale_factor=1.5
        )
        page = ctx.new_page()
        url = f"{base}/d/{uid}?orgId=1&from=now-15m&to=now&refresh=5s&kiosk"
        page.goto(url, wait_until="domcontentloaded", timeout=60_000)
        page.wait_for_selector(
            ".react-grid-layout, [data-testid='data-testid Panel header']", timeout=60_000
        )
        page.wait_for_timeout(10_000)  # queries are quick; let every panel draw
        target = out / f"grafana-{name}{'-' + tag if tag else ''}.png"
        page.screenshot(path=target)
        ctx.close()
        done.append(target.name)
        print(f"saved {target}", flush=True)
    return done


def shoot_control_room(browser, base: str, out: Path, tag: str) -> str:
    ctx = browser.new_context(viewport={"width": 1440, "height": 1150}, device_scale_factor=1.5)
    page = ctx.new_page()
    page.goto(base, wait_until="domcontentloaded", timeout=60_000)
    page.wait_for_function(
        "document.getElementById('metricsText').textContent.includes('metrics live')",
        timeout=30_000,
    )
    page.wait_for_timeout(8_000)  # charts poll every few seconds
    target = out / f"control-room{'-' + tag if tag else ''}.png"
    page.screenshot(path=target)
    ctx.close()
    print(f"saved {target}", flush=True)
    return target.name


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", required=True)
    parser.add_argument("--tag", default="")
    parser.add_argument("--grafana", default="http://127.0.0.1:3000")
    parser.add_argument("--control-room", default="http://127.0.0.1:8080")
    parser.add_argument(
        "--only", default="", help="comma list of: overview,slo,sale-night,data-tier,control-room"
    )
    args = parser.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    only = {x for x in args.only.split(",") if x} or {*BOARDS, "control-room"}
    failures = 0
    with sync_playwright() as pw:
        browser = pw.chromium.launch(args=["--no-sandbox"])
        if only & set(BOARDS):
            try:
                shoot_grafana(browser, args.grafana, out, args.tag, only)
            except Exception as exc:
                print(f"::error::Grafana screenshots failed: {exc}")
                failures += 1
        if "control-room" in only:
            try:
                shoot_control_room(browser, args.control_room, out, args.tag)
            except Exception as exc:
                print(f"::error::Control Room screenshot failed: {exc}")
                failures += 1
        browser.close()
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
