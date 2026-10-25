from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from scripts.research import result_contract
from quant_analysis.analysis_plan import (
    AnalysisPlanError,
    expand_analysis_plan,
)


PLAN_PATH = Path("joinquant/strategies/strategy-003/research/analysis-plan.json")


def _load_plan(repo_root: Path) -> dict[str, object]:
    return json.loads((repo_root / PLAN_PATH).read_text(encoding="utf-8"))


def test_expands_baseline_and_challenges_deterministically(repo_root: Path) -> None:
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
        "double-commission": {"costs": {"commission_multiplier": 2.0}},
        "high-slippage": {"costs": {"one_way_slippage": 0.001}},
        "delay-one-day": {"execution": {"additional_delay_days": 1}},
    }
    assert first["scenarios"][0]["params"]["scenario_id"] == "baseline"
    assert len({item["params_sha256"] for item in first["scenarios"]}) == 10


def test_scenario_identifier_contract_matches_analysis_schema() -> None:
    schema_path = (
        Path(__file__).parents[2]
        / ".agents/skills/analyze-quant-robustness/scripts/quant_analysis/schemas/analysis-plan.schema.json"
    )
    schema = json.loads(schema_path.read_text(encoding="utf-8"))

    assert getattr(result_contract, "SCENARIO_ID_PATTERN", None) == schema[
        "properties"
    ]["scenarios"]["items"]["properties"]["scenario_id"]["pattern"]


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
    monkeypatch: pytest.MonkeyPatch,
    mutation,
    message: str,
) -> None:
    plan = _load_plan(repo_root)
    mutation(plan)
    import quant_analysis.analysis_plan as module
    original_load = module._load_json
    monkeypatch.setattr(module, "_load_json", lambda path, *, label:
                        copy.deepcopy(plan) if label == "analysis plan" else original_load(path, label=label))

    with pytest.raises(AnalysisPlanError, match=message):
        expand_analysis_plan(repo_root, PLAN_PATH)

def test_rejects_baseline_path_outside_plan_bundle(
    repo_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = _load_plan(repo_root)
    plan["baseline_config"] = "../outside.json"
    import quant_analysis.analysis_plan as module
    original_load = module._load_json
    monkeypatch.setattr(module, "_load_json", lambda path, *, label:
                        copy.deepcopy(plan) if label == "analysis plan" else original_load(path, label=label))

    with pytest.raises(AnalysisPlanError, match="baseline_config"):
        expand_analysis_plan(repo_root, PLAN_PATH)


def test_rejects_false_stop_failure_flag_without_market_shocks(
    repo_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = _load_plan(repo_root)
    plan["analyses"]["position_shocks"][0] = {
        "id": "invalid-stop-failure",
        "use_stop_failure_loss": False,
        "maximum_loss_abs_max": 0.15,
    }
    import quant_analysis.analysis_plan as module
    original_load = module._load_json
    monkeypatch.setattr(module, "_load_json", lambda path, *, label:
                        copy.deepcopy(plan) if label == "analysis plan" else original_load(path, label=label))

    with pytest.raises(AnalysisPlanError, match="schema validation"):
        expand_analysis_plan(repo_root, PLAN_PATH)
