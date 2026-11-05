"""Generated files are current, every alert is documented, dashboards are well-formed."""

import json
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
RULES = sorted((ROOT / "observability/prometheus/rules").glob("*.rules.yml"))
URL = "https://github.com/omaraldulimay/rush-hour/blob/main/docs/runbooks/"


def alerts() -> list[dict]:
    return [
        r
        for f in RULES
        for g in yaml.safe_load(f.read_text())["groups"]
        for r in g["rules"]
        if "alert" in r
    ]


def test_generated_files_are_current() -> None:
    for script in ("prometheus/gen_slo_rules.py", "grafana/build_dashboards.py"):
        done = subprocess.run([sys.executable, str(ROOT / "observability" / script), "--check"])
        assert done.returncode == 0, f"{script} output is stale: run it without --check"


def test_every_alert_has_metadata_and_a_runbook() -> None:
    found = alerts()
    assert len(found) >= 10
    for rule in found:
        name = rule["alert"]
        assert rule["labels"]["severity"] in {"critical", "warning", "info"}, name
        for key in ("summary", "description", "runbook_url"):
            assert rule["annotations"].get(key), (name, key)
        assert rule["annotations"]["runbook_url"] == f"{URL}{name}.md"
        assert (ROOT / "docs/runbooks" / f"{name}.md").exists(), name


def test_dashboards_have_stable_uids_and_use_the_datasource_variable() -> None:
    for path in (ROOT / "observability/grafana/dashboards").glob("*.json"):
        text = path.read_text()
        board = json.loads(text)
        assert board["uid"].startswith("rh-")
        assert any(v["name"] == "datasource" for v in board["templating"]["list"])
        assert '"uid": "prometheus"' not in text  # no hard-coded datasource
