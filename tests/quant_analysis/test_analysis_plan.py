from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from scripts.research.quant_analysis.analysis_plan import (
    AnalysisPlanError,
    expand_analysis_plan,
)
from scripts.research.quant_analysis.orchestration import (
    build_scenario_run_documents,
)


PLAN_PATH = Path("joinquant/strategies/strategy-003/research/analysis-plan.json")


def _load_plan(repo_root: Path) -> dict[str, object]:
    return json.loads((repo_root / PLAN_PATH).read_text(encoding="utf-8"))


def test_expands_baseline_and_six_challenges_deterministically(repo_root: Path) -> None:
    first = expand_analysis_plan(repo_root, PLAN_PATH)
    second = expand_analysis_plan(repo_root, PLAN_PATH)

    assert first == second
    assert first["schema_version"] == "analysis-scenarios/1"
    assert first["strategy_id"] == "strategy-003"
    assert first["expected"] == {
        "scenario_runs": 7,
        "benchmarks": 2,
        "bootstrap_paths": 10000,
        "seed": 20260714,
    }
    assert [item["scenario_id"] for item in first["scenarios"]] == [
        "baseline",
        "entry-40",
        "entry-60",
        "stop-1-5n",
        "stop-2-5n",
        "group-unit-cap-5",
        "portfolio-unit-cap-10",
    ]
    assert first["scenarios"][0]["params"]["scenario_id"] == "baseline"
    assert first["scenarios"][1]["params"]["signal"]["entry_days"] == 40
    assert first["scenarios"][5]["params"]["risk"][
        "asset_group_unit_cap"
    ] == 5.0
    assert first["scenarios"][6]["params"]["risk"][
        "portfolio_unit_cap"
    ] == 10.0
    assert len({item["params_sha256"] for item in first["scenarios"]}) == 7


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda plan: plan["scenarios"].append(copy.deepcopy(plan["scenarios"][0])), "unique"),
        (lambda plan: plan["expected"].update({"scenario_runs": 8}), "scenario_runs"),
        (lambda plan: plan["expected"].update({"bootstrap_paths": 9999}), "bootstrap_paths"),
        (lambda plan: plan["expected"].update({"seed": 1}), "seed"),
        (lambda plan: plan["scenarios"][0].update({"overrides": {"signal": {"entry_days": 1}}}), "baseline"),
    ],
)
def test_rejects_inconsistent_plan(
    repo_root: Path,
    tmp_path: Path,
    mutation,
    message: str,
) -> None:
    plan = _load_plan(repo_root)
    mutation(plan)
    path = tmp_path / "analysis-plan.json"
    path.write_text(json.dumps(plan), encoding="utf-8")

    with pytest.raises(AnalysisPlanError, match=message):
        expand_analysis_plan(repo_root, path)


def test_rejects_baseline_path_outside_repository(repo_root: Path, tmp_path: Path) -> None:
    plan = _load_plan(repo_root)
    plan["baseline_config"] = str(tmp_path / "outside.json")
    path = tmp_path / "analysis-plan.json"
    path.write_text(json.dumps(plan), encoding="utf-8")

    with pytest.raises(AnalysisPlanError, match="baseline_config"):
        expand_analysis_plan(repo_root, path)


def test_rejects_false_stop_failure_flag_without_market_shocks(
    repo_root: Path,
    tmp_path: Path,
) -> None:
    plan = _load_plan(repo_root)
    plan["analyses"]["position_shocks"][0] = {
        "id": "invalid-stop-failure",
        "use_stop_failure_loss": False,
        "maximum_loss_abs_max": 0.15,
    }
    path = tmp_path / "analysis-plan.json"
    path.write_text(json.dumps(plan), encoding="utf-8")

    with pytest.raises(AnalysisPlanError, match="schema validation"):
        expand_analysis_plan(repo_root, path)


def test_builds_seven_independent_public_runner_configs(repo_root: Path) -> None:
    expanded = expand_analysis_plan(repo_root, PLAN_PATH)
    template = json.loads(
        (
            repo_root
            / "joinquant/strategies/strategy-003/research/project-run.json"
        ).read_text(encoding="utf-8")
    )

    documents = build_scenario_run_documents(
        expanded,
        template,
        preparation_id="a" * 64,
    )

    assert len(documents) == 7
    assert [item["scenario_id"] for item in documents] == [
        item["scenario_id"] for item in expanded["scenarios"]
    ]
    for item in documents:
        scenario_id = item["scenario_id"]
        assert item["params"]["scenario_id"] == scenario_id
        assert item["run_config"]["project_config"] == (
            f".local/strategy-analysis-preparations/{'a' * 64}/scenario-configs/"
            f"{scenario_id}/params.json"
        )
        assert item["run_config"]["required_outputs"] == [
            {"path": f"backtests/local-{scenario_id}", "format": "directory"}
        ]
        encoded = json.dumps(item["run_config"], sort_keys=True)
        assert "analysis-plan" not in encoded
        assert "candidates" not in encoded
