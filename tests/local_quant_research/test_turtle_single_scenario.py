from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


RESEARCH_ROOT = (
    Path(__file__).resolve().parents[2]
    / "joinquant"
    / "strategies"
    / "strategy-003"
    / "research"
)
sys.path.insert(0, str(RESEARCH_ROOT))

from turtle_etf import single_scenario  # noqa: E402
from turtle_etf.result_adapter import LocalResultPackage  # noqa: E402


def _config() -> dict[str, object]:
    return {
        "schema_version": 1,
        "project_id": "strategy-003",
        "scenario_id": "baseline",
        "research": {"initial_cash": 1_500_000},
    }


def test_one_call_executes_exactly_one_scenario_and_returns_to_caller(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[object] = []
    facts = SimpleNamespace(name="facts")
    def fake_benchmark(**kwargs: object) -> SimpleNamespace:
        calls.append(kwargs)
        return SimpleNamespace(facts=facts, performance={"status": "pass"})

    monkeypatch.setattr(single_scenario, "benchmark_scenario", fake_benchmark)

    def fake_write(target: Path, **kwargs: object) -> LocalResultPackage:
        calls.append((target, kwargs))
        target.mkdir(parents=True)
        return LocalResultPackage(
            root=target.resolve(), params_sha256="a" * 64, attribution_sha256="b" * 64
        )

    monkeypatch.setattr(single_scenario, "write_local_result", fake_write)
    code = tmp_path / "cli.py"
    code.write_text("pass\n", encoding="utf-8")

    outcome = single_scenario.execute_prepared_scenario(
        prepared_inputs=SimpleNamespace(
            name="prepared", corporate_actions_digest="f" * 64
        ),
        config=_config(),
        output_dir=tmp_path / "output",
        run_id="run-1",
        snapshot_id="c" * 64,
        code_sha256="e" * 64,
        config_sha256="d" * 64,
        code_path=code,
    )

    benchmark_calls = [call for call in calls if isinstance(call, dict)]
    assert len(benchmark_calls) == 1
    assert benchmark_calls[0]["prepared_inputs"].name == "prepared"
    assert benchmark_calls[0]["scenario_id"] == "baseline"
    write_calls = [call for call in calls if isinstance(call, tuple)]
    assert write_calls[0][1]["corporate_actions_sha256"] == "f" * 64
    assert outcome.scenario_id == "baseline"
    assert outcome.local_backtest_id == "local-baseline"
    assert outcome.next_action == "return_to_caller"
    assert outcome.result_path == (
        tmp_path / "output" / "backtests" / "local-baseline"
    ).resolve()
    assert len(list((tmp_path / "output" / "backtests").iterdir())) == 1


@pytest.mark.parametrize("forbidden", ["candidates", "scenarios", "analysis_plan"])
def test_single_scenario_rejects_batch_or_analysis_inputs(
    forbidden: str,
) -> None:
    config = _config()
    config[forbidden] = []

    with pytest.raises(single_scenario.SingleScenarioError, match="single scenario"):
        single_scenario.validate_single_scenario_config(config)


def test_project_status_is_minimal_and_returns_to_caller(tmp_path: Path) -> None:
    single_scenario.write_project_status(
        tmp_path,
        status="complete",
        reason_codes=(),
        next_action="return_to_caller",
    )

    assert json.loads((tmp_path / "project-status.json").read_text(encoding="utf-8")) == {
        "schema_version": 1,
        "status": "complete",
        "reason_codes": [],
        "next_action": "return_to_caller",
    }


def test_repository_entry_and_run_config_expose_only_one_result(
    repo_root: Path,
) -> None:
    research_root = repo_root / "joinquant/strategies/strategy-003/research"
    baseline = json.loads((research_root / "baseline.json").read_text(encoding="utf-8"))
    run_config = json.loads(
        (research_root / "project-run.json").read_text(encoding="utf-8")
    )
    entry = (
        research_root / "turtle_etf/vectorbt_cli.py"
    ).read_text(encoding="utf-8")

    assert baseline["scenario_id"] == "baseline"
    assert run_config["project_entry"].endswith("/vectorbt_cli.py")
    assert run_config["required_outputs"] == [
        {"path": "backtests/local-baseline", "format": "directory"}
    ]
    assert "benchmark_input" not in run_config
    assert "candidates.json" not in run_config["declared_inputs"]
    assert "corporate_actions=snapshot.corporate_actions" in entry
    assert "corporate_actions_digest=snapshot.corporate_actions_digest" in entry
    for forbidden in (
        "run_candidate_set",
        "quant_analysis",
        "analysis-plan.json",
        "candidate-strategies.json",
        "Vibe-Trading",
    ):
        assert forbidden not in entry
