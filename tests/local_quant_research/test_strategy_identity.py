from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path
from urllib.parse import parse_qs, urlparse


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_strategy_003_is_bound_to_one_real_joinquant_strategy(repo_root: Path) -> None:
    index_path = repo_root / "joinquant" / "strategies" / "strategy_index.csv"
    with index_path.open(encoding="utf-8", newline="") as handle:
        matches = [
            row
            for row in csv.DictReader(handle)
            if row["strategy_id"] == "strategy-003"
        ]

    assert len(matches) == 1
    row = matches[0]
    parsed = urlparse(row["joinquant_strategy_url"])
    assert parsed.netloc == "www.joinquant.com"
    assert parsed.path == "/algorithm/index/edit"
    assert len(parse_qs(parsed.query).get("algorithmId", [])) == 1
    assert row["status"] == "active"
    assert row["latest_backtest_id"] == ""
    assert row["latest_simulation_id"] == ""

    strategy_dir = repo_root / "joinquant" / "strategies" / "strategy-003"
    code_path = repo_root / row["current_default_code"]
    manifest = json.loads((strategy_dir / "manifest.json").read_text(encoding="utf-8"))
    assert code_path == strategy_dir / "default_code.py"
    assert manifest["object"] == {
        "kind": "strategy",
        "local_id": "strategy-003",
        "status": "active",
        "name": "turtle_etf_local_research",
    }
    assert manifest["source"]["url"] == row["joinquant_strategy_url"]
    assert manifest["code"]["sha256"] == _sha256(code_path)
    assert manifest["fence"]["before_sha256"] == manifest["fence"]["after_sha256"]


def test_strategy_003_has_no_backtest_or_simulation_archive(repo_root: Path) -> None:
    strategy_dir = repo_root / "joinquant" / "strategies" / "strategy-003"
    assert not (strategy_dir / "backtests").exists()
    assert not (strategy_dir / "simulations").exists()
