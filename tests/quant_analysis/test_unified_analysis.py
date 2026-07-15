from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pandas as pd
import pytest

from scripts.research.quant_analysis import unified_analysis as analysis
from scripts.research.quant_analysis.unified_analysis import (
    ScenarioInput,
    UnifiedAnalysisError,
    _deletion_sensitivity,
    _position_facts,
    _position_shocks,
    _register_source_results,
    _risk_metrics,
    align_three_way_benchmarks,
    calculate_return_metrics,
    deterministic_next_action,
    evaluate_metrics,
)


SCENARIO_IDS = (
    "baseline",
    "entry-40",
    "entry-60",
    "stop-1-5n",
    "stop-2-5n",
    "covariance-120d",
    "covariance-ewma-30d",
)


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, sort_keys=True), encoding="utf-8")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _source_fixture(
    root: Path,
    workspace: Path,
    *,
    identity_override: dict[str, object] | None = None,
    run_prefix: str = "run",
    shared_identity: dict[str, object] | None = None,
) -> dict[str, str]:
    _write_json(
        workspace / "analysis-scenarios.json",
        {
            "strategy_id": "strategy-003",
            "scenarios": [
                {"scenario_id": scenario_id, "dimension": "baseline" if index == 0 else "parameter"}
                for index, scenario_id in enumerate(SCENARIO_IDS)
            ],
        },
    )
    registry: dict[str, str] = {}
    for index, scenario_id in enumerate(SCENARIO_IDS):
        params_path = workspace / "scenario-configs" / scenario_id / "params.json"
        _write_json(params_path, {"scenario_id": scenario_id})
        run_id = f"{run_prefix}-{index}"
        registry[scenario_id] = run_id
        run_root = root / ".local" / "quant-research" / "strategy-003" / run_id
        result_dir = run_root / "backtests" / f"local-{scenario_id}"
        performance = {
            "status": "pass",
            "result_match": True,
            "cold_seconds": 10.0,
            "warm_seconds": 1.0,
            "cleanup": {"verified": True},
        }
        _write_json(result_dir / "performance.json", performance)
        identity = {
            "snapshot_id": "snapshot-1",
            "code_identity_sha256": "code-identity-1",
            "code_sha256": "code-1",
            "execution": {
                "adapter_version": "adapter-1",
                "backend": "vectorbt.Portfolio.from_order_func",
                "callbacks_sha256": "callbacks-1",
            },
        }
        if shared_identity:
            identity.update(shared_identity)
        if index == len(SCENARIO_IDS) - 1 and identity_override:
            identity.update(identity_override)
        _write_json(
            result_dir / "manifest.json",
            {
                "run": {
                    "run_id": run_id,
                    "scenario_id": scenario_id,
                    "snapshot_id": identity["snapshot_id"],
                },
                "source": {
                    "engine": {
                        "adapter_version": identity["execution"]["adapter_version"],
                        "backend": identity["execution"]["backend"],
                        "numba": "0.66.0",
                        "vectorbt": "1.1.0",
                    }
                },
            },
        )
        run_manifest = {
            "schema_version": 1,
            "project_id": "strategy-003",
            "run_id": run_id,
            "status": "complete",
            "snapshot": {"snapshot_id": identity["snapshot_id"]},
            "inputs": {
                "project_config_sha256": _sha256(params_path),
                "code_identity_sha256": identity["code_identity_sha256"],
                "code_sha256": identity["code_sha256"],
                "code_identity": {
                    "execution": {
                        **identity["execution"],
                        "dependencies": {"numba": "0.66.0", "vectorbt": "1.1.0"},
                    }
                },
            },
            "output_set_sha256": f"outputs-{index}",
        }
        _write_json(run_root / "run-manifest.json", run_manifest)
    return registry


def test_calculate_return_metrics_compounds_and_measures_drawdown() -> None:
    series = pd.Series(
        [0.10, -0.20, 0.05],
        index=pd.to_datetime(["2024-01-02", "2024-01-03", "2024-01-04"]),
    )

    metrics = calculate_return_metrics(series)

    assert abs(metrics["cumulative_return"] - (1.10 * 0.80 * 1.05 - 1.0)) < 1e-12
    assert abs(metrics["max_drawdown"] + 0.20) < 1e-12
    assert metrics["observations"] == 3
    assert metrics["cagr"] is not None
    assert metrics["calmar"] is not None


def test_aligns_strategy_and_both_benchmarks_on_one_shared_calendar() -> None:
    strategy = pd.Series(
        [0.01, 0.02, 0.03],
        index=pd.to_datetime(["2024-01-02", "2024-01-03", "2024-01-04"]),
    )
    benchmarks = {
        "CSI300_CNY_TOTAL_RETURN": pd.Series(
            [0.001, 0.002],
            index=pd.to_datetime(["2024-01-02", "2024-01-03"]),
        ),
        "NASDAQ100_CNY_TOTAL_RETURN": pd.Series(
            [0.004, 0.005],
            index=pd.to_datetime(["2024-01-03", "2024-01-04"]),
        ),
    }

    aligned, evidence = align_three_way_benchmarks(strategy, benchmarks)

    assert list(aligned.index) == [pd.Timestamp("2024-01-03")]
    assert list(aligned.columns) == [
        "strategy",
        "CSI300_CNY_TOTAL_RETURN",
        "NASDAQ100_CNY_TOTAL_RETURN",
    ]
    assert evidence["common_samples"] == 1
    assert evidence["strategy_excluded_dates"] == 2


def test_evaluates_all_declared_strategy_thresholds() -> None:
    thresholds = {
        "cagr_min_exclusive": 0.0,
        "max_drawdown_abs_max": 0.2,
        "calmar_min": 0.5,
    }

    assert evaluate_metrics(
        {"cagr": 0.10, "max_drawdown": -0.10, "calmar": 1.0},
        thresholds,
    ) == ("pass", [])
    assert evaluate_metrics(
        {"cagr": -0.01, "max_drawdown": -0.25, "calmar": -0.04},
        thresholds,
    ) == (
        "fail",
        ["cagr_min_exclusive", "max_drawdown_abs_max", "calmar_min"],
    )


def test_position_facts_use_same_day_state_before_end_of_day_valuation() -> None:
    scenario = ScenarioInput(
        scenario_id="baseline",
        run_id="run-1",
        result_dir=Path("."),
        returns=pd.Series([0.0], index=pd.to_datetime(["2024-01-02"])),
        balances=pd.DataFrame(
            {
                "date": pd.to_datetime(["2024-01-02"]),
                "total_value": [1000.0],
                "cash": [500.0],
            }
        ),
        positions=pd.DataFrame(
            {
                "date": pd.to_datetime(["2024-01-02"]),
                "security": ["510300.XSHG"],
                "amount": [50.0],
                "price": [10.0],
                "daily_gains": [0.0],
            }
        ),
        orders=pd.DataFrame(),
        events=pd.DataFrame(
            {
                "time": ["2024-01-02 16:00:00"],
                "event_id": ["event-1"],
                "scope": ["security"],
                "security": ["510300.XSHG"],
                "event_type": ["valuation"],
                "reason_code": ["signal_entry"],
                "requested_amount": [None],
                "executed_amount": [None],
                "reference_price": [10.0],
                "risk_before": [0.0],
                "risk_after": [50.0],
                "details_json": [
                    '{"average_cost_after":10.0,"close":10.0,'
                    '"common_stop_after":9.0,"position_after":50,'
                    '"security_daily_pnl":0.0,"source_reason":"entry_breakout",'
                    '"stop_failure_loss":150.0}'
                ],
            }
        ),
        params={},
        performance={},
    )

    facts = _position_facts(scenario, {"510300.XSHG": "equity"})

    assert facts.loc[0, "common_stop"] == 9.0
    assert facts.loc[0, "attribution_reason"] == "entry_breakout"


def test_security_pnl_facts_include_full_exit_without_fake_source_position() -> None:
    dates = pd.to_datetime(["2024-01-02", "2024-01-03"])
    scenario = ScenarioInput(
        scenario_id="baseline",
        run_id="run-1",
        result_dir=Path("."),
        returns=pd.Series([99.0 / 999.0], index=dates[1:]),
        balances=pd.DataFrame(
            {
                "date": dates,
                "total_value": [999.0, 1098.0],
                "cash": [499.0, 1098.0],
            }
        ),
        positions=pd.DataFrame(
            {
                "date": [dates[0]],
                "security": ["ETF-A"],
                "amount": [50.0],
                "price": [10.0],
                "avg_cost": [10.0],
                "daily_gains": [-1.0],
            }
        ),
        orders=pd.DataFrame(),
        events=pd.DataFrame(
            {
                "time": ["2024-01-02 16:00:00", "2024-01-03 16:00:00"],
                "event_id": ["valuation-1", "valuation-2"],
                "scope": ["security", "security"],
                "security": ["ETF-A", "ETF-A"],
                "event_type": ["valuation", "valuation"],
                "reason_code": ["signal_entry", "protective_stop"],
                "requested_amount": [None, None],
                "executed_amount": [None, None],
                "reference_price": [10.0, 12.0],
                "risk_before": [0.0, 50.0],
                "risk_after": [50.0, 0.0],
                "details_json": [
                    '{"security_daily_pnl":-1.0,"source_reason":"entry_breakout"}',
                    '{"security_daily_pnl":99.0,"source_reason":"protective_stop"}',
                ],
            }
        ),
        params={},
        performance={},
    )

    pnl = analysis._security_pnl_facts(scenario, {"ETF-A": "equity"})

    assert scenario.positions["date"].tolist() == [dates[0]]
    assert pnl["date"].tolist() == list(dates)
    assert pnl["security_daily_pnl"].tolist() == pytest.approx([-1.0, 99.0])
    assert pnl["return_contribution"].tolist() == pytest.approx(
        [-0.001, 99.0 / 999.0]
    )
    assert pnl["attribution_reason"].tolist() == [
        "entry_breakout",
        "protective_stop",
    ]


def test_stop_failure_shock_uses_reproducible_loss_from_valuation_details() -> None:
    date = pd.Timestamp("2024-01-02")
    positions = pd.DataFrame(
        {
            "date": [date, date],
            "security": ["ETF-A", "ETF-B"],
            "asset_group": ["equity", "bond"],
            "weight": [0.4, 0.2],
            "equity": [1000.0, 1000.0],
            "stop_failure_loss": [100.0, 50.0],
        }
    )
    definitions = [
        {
            "id": "shock-stop-failure",
            "use_stop_failure_loss": True,
            "maximum_loss_abs_max": 0.20,
        }
    ]

    rows, _ = _position_shocks(positions, definitions)

    assert rows[0]["status"] == "pass"
    assert rows[0]["reasons"] == []
    assert rows[0]["metrics"]["evaluated_dates"] == 1
    assert rows[0]["metrics"]["worst_account_loss"] == pytest.approx(0.15)


def test_deterministic_analysis_continues_to_local_report_not_vibe() -> None:
    assert deterministic_next_action() == "generate_deterministic_local_report"


def test_analysis_seconds_are_recorded_once_and_preserved_on_rerun(
    tmp_path: Path,
) -> None:
    output = tmp_path / "deterministic-analysis.json"
    summary = {"analysis_id": "analysis-1"}

    first = analysis._with_analysis_seconds(summary, 7.25, output)
    _write_json(output, first)
    second = analysis._with_analysis_seconds(summary, 9.5, output)

    assert first["analysis_seconds"] == 7.25
    assert second["analysis_seconds"] == 7.25


def test_risk_diagnostics_do_not_mislabel_mark_to_market_rows_as_gate_breaches() -> None:
    date = pd.Timestamp("2024-01-02")
    scenario = ScenarioInput(
        scenario_id="baseline",
        run_id="run-1",
        result_dir=Path("."),
        returns=pd.Series([0.0] * 60, index=pd.date_range(date, periods=60)),
        balances=pd.DataFrame(
            {"date": [date], "total_value": [1000.0], "cash": [500.0]}
        ),
        positions=pd.DataFrame(),
        orders=pd.DataFrame(
            {
                "status": ["done"],
                "filled": [10.0],
                "action": ["open"],
                "price": [10.0],
                "commission": [1.0],
                "gains": [0.0],
            }
        ),
        events=pd.DataFrame(
            {
                "event_type": ["decision", "valuation", "decision", "valuation"],
                "reason_code": [
                    "risk_gate_block",
                    "risk_gate_block",
                    "protective_stop",
                    "protective_stop",
                ],
            }
        ),
        params={
            "risk": {
                "security_value_cap": 0.3,
                "asset_group_value_cap": 0.5,
                "portfolio_value_cap": 0.9,
                "portfolio_risk_cap": 0.1,
                "target_volatility": 0.1,
            }
        },
        performance={},
    )
    positions = pd.DataFrame(
        {
            "date": [date],
            "asset_group": ["equity"],
            "weight": [0.4],
            "common_stop": [9.0],
            "avg_cost": [10.0],
            "amount": [10.0],
        }
    )

    metrics = _risk_metrics(scenario, positions)

    assert metrics["mark_to_market_security_weight_above_entry_cap_rows"] == 1
    assert metrics["mark_to_market_group_weight_above_entry_cap_rows"] == 0
    assert metrics["mark_to_market_portfolio_weight_above_cap_days"] == 0
    assert metrics["realized_60d_volatility_above_target_days"] == 0
    assert metrics["risk_constraint_events"] == 1
    assert metrics["protective_stop_events"] == 1
    assert "security_value_cap_breaches" not in metrics


def test_deletion_sensitivity_keeps_unheld_securities_and_groups_in_matrix() -> None:
    dates = pd.to_datetime(["2024-01-02", "2024-01-03"])
    scenario = ScenarioInput(
        scenario_id="baseline",
        run_id="run-1",
        result_dir=Path("."),
        returns=pd.Series([0.01, -0.01], index=dates),
        balances=pd.DataFrame(),
        positions=pd.DataFrame(),
        orders=pd.DataFrame(),
        events=pd.DataFrame(),
        params={},
        performance={},
    )
    positions = pd.DataFrame(
        {
            "date": dates,
            "security": ["ETF-A", "ETF-A"],
            "asset_group": ["equity", "equity"],
            "return_contribution": [0.01, -0.01],
        }
    )
    thresholds = {
        "cagr_min_exclusive": 0.0,
        "max_drawdown_abs_max": 0.2,
        "calmar_min": 0.5,
    }

    rows, _ = _deletion_sensitivity(
        scenario,
        positions,
        {"ETF-A": "equity", "ETF-B": "bond"},
        thresholds,
    )

    assert {row["removed"] for row in rows} == {"ETF-A", "ETF-B", "equity", "bond"}


def test_source_registration_uses_only_explicit_scenario_run_mapping(tmp_path: Path) -> None:
    root = tmp_path
    workspace = root / ".local" / "strategy-analysis-preparations" / "preparation-1"
    _write_json(workspace / "preparation.json", {"preparation_id": "preparation-1"})
    registry = _source_fixture(root, workspace)
    duplicate = root / ".local" / "quant-research" / "strategy-003" / "old-run"
    _write_json(
        duplicate / "run-manifest.json",
        {
            "run_id": "old-run",
            "status": "complete",
            "inputs": {
                "project_config_sha256": _sha256(
                    workspace / "scenario-configs" / "baseline" / "params.json"
                )
            },
        },
    )

    document = _register_source_results(root, workspace, registry)

    assert [source["run_id"] for source in document["sources"]] == list(registry.values())
    assert document["source_registry"] == {
        "explicit": True,
        "scenario_count": 7,
        "run_id_count": 7,
        "sha256": document["source_registry"]["sha256"],
    }
    assert document["shared_identity"] == {
        "snapshot_id": "snapshot-1",
        "code_identity_sha256": "code-identity-1",
        "code_sha256": "code-1",
            "execution_backend": {
                "adapter_version": "adapter-1",
                "backend": "vectorbt.Portfolio.from_order_func",
                "callbacks_sha256": "callbacks-1",
                "dependencies": {"numba": "0.66.0", "vectorbt": "1.1.0"},
            },
        "execution_backend_sha256": document["shared_identity"][
            "execution_backend_sha256"
        ],
    }
    assert "old-run" not in json.dumps(document)
    final_workspace = root / ".local" / "strategy-analysis" / document["analysis_id"]
    assert json.loads(
        (final_workspace / "source-results.json").read_text(encoding="utf-8")
    ) == document


def test_source_registration_rejects_duplicate_run_ids(tmp_path: Path) -> None:
    root = tmp_path
    workspace = root / ".local" / "strategy-analysis-preparations" / "preparation-1"
    _write_json(workspace / "preparation.json", {"preparation_id": "preparation-1"})
    registry = _source_fixture(root, workspace)
    registry["entry-40"] = registry["baseline"]

    with pytest.raises(UnifiedAnalysisError, match="run_id values must be unique"):
        _register_source_results(root, workspace, registry)


def test_source_registration_requires_exactly_seven_scenarios(tmp_path: Path) -> None:
    root = tmp_path
    workspace = root / ".local" / "strategy-analysis-preparations" / "preparation-1"
    _write_json(workspace / "preparation.json", {"preparation_id": "preparation-1"})
    registry = _source_fixture(root, workspace)
    scenarios = json.loads(
        (workspace / "analysis-scenarios.json").read_text(encoding="utf-8")
    )
    removed = scenarios["scenarios"].pop()
    registry.pop(removed["scenario_id"])
    _write_json(workspace / "analysis-scenarios.json", scenarios)

    with pytest.raises(UnifiedAnalysisError, match="exactly seven"):
        _register_source_results(root, workspace, registry)


def test_source_registration_rejects_result_manifest_with_another_backend(
    tmp_path: Path,
) -> None:
    root = tmp_path
    workspace = root / ".local" / "strategy-analysis-preparations" / "preparation-1"
    _write_json(workspace / "preparation.json", {"preparation_id": "preparation-1"})
    registry = _source_fixture(root, workspace)
    result_manifest = (
        root
        / ".local"
        / "quant-research"
        / "strategy-003"
        / registry["baseline"]
        / "backtests"
        / "local-baseline"
        / "manifest.json"
    )
    document = json.loads(result_manifest.read_text(encoding="utf-8"))
    document["source"]["engine"]["backend"] = "forged.Backend"
    _write_json(result_manifest, document)

    with pytest.raises(
        UnifiedAnalysisError, match="local result execution backend identity"
    ):
        _register_source_results(root, workspace, registry)


@pytest.mark.parametrize(
    ("identity_override", "message"),
    [
        ({"snapshot_id": "snapshot-2"}, "snapshot_id"),
        ({"code_identity_sha256": "code-identity-2"}, "code_identity_sha256"),
        ({"code_sha256": "code-2"}, "code_sha256"),
        (
            {
                "execution": {
                    "adapter_version": "adapter-2",
                    "backend": "vectorbt.Portfolio.from_order_func",
                    "callbacks_sha256": "callbacks-1",
                }
            },
            "execution backend identity",
        ),
    ],
)
def test_source_registration_requires_one_shared_execution_identity(
    tmp_path: Path,
    identity_override: dict[str, object],
    message: str,
) -> None:
    root = tmp_path
    workspace = root / ".local" / "strategy-analysis-preparations" / "preparation-1"
    _write_json(workspace / "preparation.json", {"preparation_id": "preparation-1"})
    registry = _source_fixture(root, workspace, identity_override=identity_override)

    with pytest.raises(UnifiedAnalysisError, match=message):
        _register_source_results(root, workspace, registry)


def test_analysis_identity_changes_when_registered_source_code_changes(
    tmp_path: Path,
) -> None:
    root = tmp_path
    preparation = (
        root / ".local" / "strategy-analysis-preparations" / "preparation-1"
    )
    _write_json(preparation / "preparation.json", {"preparation_id": "preparation-1"})
    first_registry = _source_fixture(root, preparation, run_prefix="first")
    first = _register_source_results(root, preparation, first_registry)
    first_path = root / ".local" / "strategy-analysis" / first["analysis_id"]
    first_bytes = (first_path / "source-results.json").read_bytes()

    second_registry = _source_fixture(
        root,
        preparation,
        run_prefix="second",
        shared_identity={
            "code_identity_sha256": "code-identity-2",
            "code_sha256": "code-2",
            "execution": {
                "adapter_version": "adapter-2",
                "backend": "vectorbt.Portfolio.from_order_func",
                "callbacks_sha256": "callbacks-2",
            },
        },
    )
    second = _register_source_results(root, preparation, second_registry)

    assert first["analysis_id"] != second["analysis_id"]
    assert (first_path / "source-results.json").read_bytes() == first_bytes
    assert (
        root / ".local" / "strategy-analysis" / second["analysis_id"] / "source-results.json"
    ).is_file()
