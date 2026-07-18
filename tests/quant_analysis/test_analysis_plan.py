from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from scripts.research.local_quant_research.runner import load_run_config
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
    assert first["universe"] == {
        "159819.XSHE": "china_sync_equity",
        "510300.XSHG": "china_sync_equity",
        "511010.XSHG": "treasury_bond",
        "512100.XSHG": "china_sync_equity",
        "512480.XSHG": "china_sync_equity",
        "513100.XSHG": "cross_border_tech_equity",
        "513180.XSHG": "cross_border_tech_equity",
        "515180.XSHG": "china_dividend",
        "516080.XSHG": "china_innovative_drug",
        "516160.XSHG": "china_sync_equity",
        "518880.XSHG": "gold",
    }
    assert {
        item["scenario_id"]: item["overrides"] for item in first["scenarios"]
    } == {
        "baseline": {},
        "entry-40": {"signal": {"entry_days": 40}},
        "entry-60": {"signal": {"entry_days": 60}},
        "stop-1-5n": {"signal": {"stop_n": 1.5}},
        "stop-2-5n": {"signal": {"stop_n": 2.5}},
        "group-unit-cap-5": {"risk": {"asset_group_unit_cap": 5.0}},
        "portfolio-unit-cap-10": {"risk": {"portfolio_unit_cap": 10.0}},
    }
    assert first["scenarios"][0]["params"]["scenario_id"] == "baseline"
    assert len({item["params_sha256"] for item in first["scenarios"]}) == 7


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (
            lambda plan: plan["scenarios"].append(copy.deepcopy(plan["scenarios"][0])),
            "unique",
        ),
        (
            lambda plan: plan["scenarios"][0].update(
                {"overrides": {"signal": {"entry_days": 1}}}
            ),
            "baseline",
        ),
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


def test_rejects_baseline_path_outside_repository(
    repo_root: Path, tmp_path: Path
) -> None:
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


def test_builds_seven_independent_public_runner_configs(
    repo_root: Path,
    tmp_path: Path,
) -> None:
    expanded = expand_analysis_plan(repo_root, PLAN_PATH)
    template = json.loads(
        (
            repo_root / "joinquant/strategies/strategy-003/research/project-run.json"
        ).read_text(encoding="utf-8")
    )
    (tmp_path / "strategy").mkdir()
    (tmp_path / "manifest.json").write_text("{}", encoding="utf-8")
    template["strategy"]["root"] = "strategy"
    template["declared_inputs"] = ["manifest.json"]

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
        expected_scenario_config = (
            f".local/strategy-analysis-preparations/{'a' * 64}/scenario-configs/"
            f"{scenario_id}/params.json"
        )
        assert item["run_config"]["scenario_config"] == expected_scenario_config
        assert set(item["run_config"]) == set(template)
        scenario_path = tmp_path / expected_scenario_config
        scenario_path.parent.mkdir(parents=True, exist_ok=True)
        scenario_path.write_text(json.dumps(item["params"]), encoding="utf-8")
        run_path = scenario_path.with_name("run.json")
        run_path.write_text(json.dumps(item["run_config"]), encoding="utf-8")
        assert load_run_config(run_path, repo_root=tmp_path).scenario_config == scenario_path
        encoded = json.dumps(item["run_config"], sort_keys=True)
        assert "analysis-plan" not in encoded
        assert "candidates" not in encoded
