from __future__ import annotations

import json
from pathlib import Path


def test_strategy_003_production_config_uses_only_v2_contract(
    repo_root: Path,
) -> None:
    config = json.loads(
        (
            repo_root
            / "joinquant/strategies/strategy-003/research/project-run.json"
        ).read_text(encoding="utf-8")
    )

    assert set(config) == {
        "schema_version",
        "project_id",
        "strategy",
        "snapshot_id",
        "snapshot_requirements",
        "scenario_config",
        "declared_inputs",
    }
    assert config["schema_version"] == 2
    assert config["project_id"] == "strategy-003"
    assert config["strategy"] == {
        "root": "joinquant/strategies/strategy-003/research",
        "module": "turtle_etf.strategy",
        "symbol": "MODULE",
    }
    assert config["scenario_config"] == (
        "joinquant/strategies/strategy-003/research/baseline.json"
    )
    assert config["declared_inputs"] == [
        "joinquant/strategies/strategy-003/manifest.json"
    ]
    assert config["snapshot_requirements"]
