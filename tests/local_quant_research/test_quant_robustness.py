from __future__ import annotations

from pathlib import Path

import numpy as np

from scripts.research.quant_analysis.evidence import (
    ScenarioResult,
    build_evidence_matrix,
    validate_evidence_matrix,
)
from scripts.research.quant_analysis.robustness import (
    BOOTSTRAP_BLOCK_SIZES,
    BOOTSTRAP_HORIZON,
    BOOTSTRAP_PATHS,
    asset_deletion_scenarios,
    block_bootstrap,
    calculate_bootstrap_scenarios,
    cost_execution_scenarios,
    fixed_period_scenarios,
    parameter_scenarios,
    rolling_three_year_scenarios,
    run_path_scenarios,
    summarize_bootstrap,
)


def test_fixed_path_scenario_catalog_has_exact_required_counts() -> None:
    parameters = parameter_scenarios()
    periods = fixed_period_scenarios("2026-07-13")
    rolling = rolling_three_year_scenarios("2015-01-01", "2026-07-13")
    deletions = asset_deletion_scenarios(
        securities=tuple(f"ETF-{index}" for index in range(11)),
        asset_groups=tuple(f"group-{index}" for index in range(6)),
    )
    costs = cost_execution_scenarios()

    assert len(parameters) == 6
    assert len(periods) == 3
    assert len(rolling) == 35
    assert sum(row["dimension"] == "asset_delete_etf" for row in deletions) == 11
    assert sum(row["dimension"] == "asset_delete_group" for row in deletions) == 6
    assert len(costs) == 5
    assert BOOTSTRAP_BLOCK_SIZES == (5, 20, 60)
    assert BOOTSTRAP_PATHS == 10_000
    assert BOOTSTRAP_HORIZON == 756
    all_rows = (*parameters, *periods, *rolling, *deletions, *costs)
    assert len({row["scenario_id"] for row in all_rows}) == len(all_rows)


def test_path_changing_scenarios_invoke_runner_once_each() -> None:
    calls: list[dict[str, object]] = []
    scenarios = parameter_scenarios()[:3]

    def runner(config: dict[str, object]) -> dict[str, float]:
        calls.append(config)
        return {"cagr": 0.1, "max_drawdown": -0.1, "calmar": 1.0}

    results = run_path_scenarios(
        {"signal": {"entry_days": 55, "stop_n": 2.0}},
        scenarios,
        runner,
    )

    assert len(calls) == len(scenarios)
    assert {row.scenario_id for row in results} == {
        str(row["scenario_id"]) for row in scenarios
    }
    assert all(row.status == "pass" for row in results)
    assert len({row.input_sha256 for row in results}) == len(results)


def test_path_scenario_threshold_failure_cannot_be_compensated_by_other_metrics() -> None:
    scenario = {
        "scenario_id": "bad-drawdown",
        "dimension": "parameter",
        "overrides": {},
        "thresholds": {"cagr_min_exclusive": 0.0, "max_drawdown_abs_max": 0.2},
    }

    result = run_path_scenarios(
        {},
        (scenario,),
        lambda _: {"cagr": 1.0, "max_drawdown": -0.21, "calmar": 4.0},
    )[0]

    assert result.status == "fail"
    assert "max_drawdown_abs_max" in result.reasons


def test_path_scenario_failure_is_recorded_without_stopping_remaining_scenarios() -> None:
    scenarios = (
        {"scenario_id": "broken", "dimension": "parameter", "overrides": {}},
        {"scenario_id": "healthy", "dimension": "parameter", "overrides": {}},
    )

    def runner(config: dict[str, object]) -> dict[str, float]:
        if config["scenario_id"] == "broken":
            raise RuntimeError("deliberate scenario failure")
        return {"cagr": 0.1, "max_drawdown": -0.1, "calmar": 1.0}

    broken, healthy = run_path_scenarios({}, scenarios, runner)

    assert broken.status == "evidence_insufficient"
    assert broken.reasons == ("scenario_execution_failed:RuntimeError",)
    assert healthy.status == "pass"


def test_bootstrap_is_seeded_has_exact_shape_and_keeps_extreme_days() -> None:
    sample = np.array([0.01, -0.02, 0.03, -0.25, 0.02], dtype=np.float64)

    first = block_bootstrap(sample, block_size=3, paths=128, horizon=37, seed=20260714)
    second = block_bootstrap(sample, block_size=3, paths=128, horizon=37, seed=20260714)

    assert first.shape == (128, 37)
    np.testing.assert_array_equal(first, second)
    assert np.isin(first, sample).all()
    assert np.any(first == -0.25)
    summary = summarize_bootstrap(first)
    assert set(summary) == {
        "probability_drawdown_over_20pct",
        "probability_drawdown_over_30pct",
        "median_terminal_return",
    }


def test_bootstrap_scenarios_cover_all_three_blocks_with_hard_gates() -> None:
    results = calculate_bootstrap_scenarios(
        np.full(80, 0.001, dtype=np.float64),
        paths=64,
        horizon=30,
        seed=20260714,
    )

    assert [row.scenario_id for row in results] == [
        "bootstrap-block-5",
        "bootstrap-block-20",
        "bootstrap-block-60",
    ]
    assert all(row.status == "pass" for row in results)


def test_bootstrap_drawdown_includes_loss_from_initial_capital() -> None:
    summary = summarize_bootstrap(np.array([[-0.25, 0.0]], dtype=np.float64))

    assert summary["probability_drawdown_over_20pct"] == 1.0


def test_evidence_matrix_is_parquet_deterministic_and_local_only(tmp_path: Path) -> None:
    results = (
        ScenarioResult(
            scenario_id="scenario-a",
            dimension="parameter",
            status="pass",
            metrics={"cagr": 0.1},
            input_sha256="a" * 64,
            reasons=(),
        ),
        ScenarioResult(
            scenario_id="scenario-b",
            dimension="history",
            status="evidence_insufficient",
            metrics={},
            input_sha256="b" * 64,
            reasons=("missing_window",),
        ),
    )
    path = tmp_path / "local-evidence-matrix.parquet"

    build_evidence_matrix(results, path)
    rows = validate_evidence_matrix(path)

    assert [row.scenario_id for row in rows] == ["scenario-a", "scenario-b"]
    assert all(row.authority == "local_exploratory" for row in rows)
    assert all(row.formula_version == "quant-analysis-v1" for row in rows)
    before = path.read_bytes()
    build_evidence_matrix(results, path)
    assert path.read_bytes() == before
