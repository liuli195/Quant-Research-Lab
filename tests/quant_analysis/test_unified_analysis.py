from __future__ import annotations

import json
import shutil
from datetime import date
from pathlib import Path
import subprocess

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from quant_analysis import unified_analysis as analysis
from quant_analysis.unified_analysis import (
    ScenarioInput,
    UnifiedAnalysisError,
    _attribution,
    _deletion_sensitivity,
    _position_facts,
    _position_shocks,
    _risk_metrics,
    align_three_way_benchmarks,
    calculate_return_metrics,
    evaluate_metrics,
    load_registered_scenario,
    run_standard_analysis,
)
from quant_analysis.source_registry import load_source_registry
from scripts.research.market_data.benchmark_sets import (
    BENCHMARK_IDS as CONTRACT_BENCHMARK_IDS,
    SourcePayload,
    write_benchmark_set,
)
from tests.quant_analysis.test_source_registry import _prepared_sources, _registry


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, sort_keys=True), encoding="utf-8")


def _benchmark_sources() -> tuple[SourcePayload, ...]:
    identities = {
        "csi300_total_return": (
            "China Securities Index Co., Ltd.",
            "H00300",
            "https://www.csindex.com.cn/",
        ),
        "nasdaq100_total_return": (
            "Nasdaq, Inc.",
            "XNDX",
            "https://indexes.nasdaqomx.com/",
        ),
        "usd_cny": (
            "Board of Governors of the Federal Reserve System",
            "DEXCHUS",
            "https://www.federalreserve.gov/",
        ),
    }
    return tuple(
        SourcePayload(
            name=name,
            filename=f"{name}.source",
            provider=provider,
            source_id=source_id,
            url=f"{url}{name}",
            content_type="application/octet-stream",
            data=name.encode(),
        )
        for name, (provider, source_id, url) in identities.items()
    )


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


def test_registered_sources_share_the_four_common_facts(
    repo_root: Path, tmp_path: Path
) -> None:
    root, entries = _prepared_sources(repo_root, tmp_path)
    registry = load_source_registry(root, _registry(root, entries))

    scenarios = [
        load_registered_scenario(source, analysis_params={})
        for source in registry.sources
    ]

    assert [source.registration.source_type for source in registry.sources] == [
        "local_research",
        "joinquant_backtest",
        "joinquant_simulation",
    ]
    assert all(not scenario.returns.empty for scenario in scenarios)
    assert all(
        {"date", "total_value", "net_value", "cash", "aval_cash"}
        .issubset(scenario.balances.columns)
        for scenario in scenarios
    )
    assert scenarios[0].attribution_status == "missing_at_source"
    assert scenarios[0].events.empty
    for scenario in scenarios[1:]:
        assert scenario.attribution_status == "available"
        assert {"time", "event_time", "date", "event_type", "reason_code", "security"}.issubset(
            scenario.events.columns
        )
        assert not scenario.events.empty
        assert scenario.events["event_time"].notna().all()
        assert "run_start" in set(scenario.events["event_type"])

    attribution = _attribution(scenarios[1], pd.DataFrame())
    assert attribution["status"] == "available"
    assert attribution["method"] == "verified source-native event log"
    assert attribution["event_counts"]["rebalance_signals"] > 0
    assert attribution["pnl_contribution"]["status"] == "evidence_insufficient"


def _standard_registry(repo_root: Path, tmp_path: Path, *, single_source: bool) -> tuple[Path, Path]:
    root, entries = _prepared_sources(repo_root, tmp_path)
    if single_source:
        entries = entries[:1]
    positions = pq.read_table(
        root / "sources/backtest/data/positions.parquet", columns=["security"]
    ).to_pandas()
    universe = [
        {"security": security, "asset_group": "etf"}
        for security in sorted(set(positions["security"].dropna().astype(str)))
    ]
    config = root / "config"
    _write_json(
        config / "baseline.json",
        {"project_id": "standard-fixture", "universe": universe, "risk": {}},
    )
    _write_json(
        config / "analysis-plan.json",
        {
            "schema_version": "strategy-analysis-plan/1",
            "strategy_id": "standard-fixture",
            "baseline_config": "config/baseline.json",
            "scenarios": [
                {
                    "scenario_id": entry["scenario_id"],
                    "dimension": "baseline" if index == 0 else "comparison",
                    "overrides": {},
                }
                for index, entry in enumerate(entries)
            ],
            "analyses": {
                "fixed_periods": [
                    {"id": "fixture", "start": "2024-01-01", "end": "2024-12-31"}
                ],
                "rolling": {"window_years": 1, "step_months": 3},
                "cost_execution": [
                    {"id": "fixture", "commission_multiplier": 1.0, "slippage": 0.0, "delay_days": 0}
                ],
                "bootstrap": {
                    "block_sizes": [1], "paths": 3, "horizon_days": 1, "seed": 7,
                    "thresholds": {
                        "probability_drawdown_over_20pct_max": 1.0,
                        "probability_drawdown_over_30pct_max": 1.0,
                        "median_terminal_return_min_exclusive": -1.0,
                    },
                },
                "historical_stress": [
                    {"id": "stress-fixture", "start": "2024-01-01", "end": "2024-12-31", "max_drawdown_abs_max": 1.0}
                ],
                "position_shocks": [
                    {"id": "shock-fixture", "asset_group_shocks": {"etf": -0.1}, "maximum_loss_abs_max": 1.0}
                ],
                "cvar": [
                    {"id": "cvar-fixture", "horizon_days": 1, "confidence": 0.95, "maximum_loss_abs_max": 1.0, "minimum_tail_observations": 1}
                ],
            },
            "thresholds": {"cagr_min_exclusive": 0.0, "max_drawdown_abs_max": 0.2, "calmar_min": 0.5},
        },
    )
    benchmark_set = write_benchmark_set(
        market_data_root=config,
        rows=[
            {
                "time": trading_date,
                "benchmark_id": benchmark_id,
                "returns": 0.0 if trading_date == "2024-01-02" else 0.01,
            }
            for trading_date in ("2024-01-02", "2024-01-03")
            for benchmark_id in CONTRACT_BENCHMARK_IDS
        ],
        sources=_benchmark_sources(),
        start_date=date(2024, 1, 2),
        end_date=date(2024, 1, 3),
    )
    registry = root / "standard-source-registry.json"
    _write_json(
        registry,
        {
            "schema_version": "standard-analysis-source-registry/1",
            "analysis_plan": "config/analysis-plan.json",
            "benchmark_manifest": (
                benchmark_set.root / "manifest.json"
            ).relative_to(root).as_posix(),
            "baseline_scenario_id": "baseline",
            "sources": entries,
        },
    )
    return root, registry


def test_standard_analysis_rejects_tampered_benchmark_data(
    repo_root: Path, tmp_path: Path
) -> None:
    root, registry = _standard_registry(repo_root, tmp_path, single_source=True)
    registry_document = json.loads(registry.read_text(encoding="utf-8"))
    benchmark_manifest = root / registry_document["benchmark_manifest"]
    benchmark_data = benchmark_manifest.parent / "benchmark-returns.parquet"
    table = pq.read_table(benchmark_data)
    returns = table["returns"].to_pylist()
    returns[-1] = float(returns[-1]) + 0.01
    pq.write_table(table.set_column(2, "returns", pa.array(returns)), benchmark_data)

    with pytest.raises(UnifiedAnalysisError, match="benchmark.*digest"):
        run_standard_analysis(root, registry)


def test_standard_analysis_is_byte_deterministic_after_fresh_output(
    repo_root: Path, tmp_path: Path
) -> None:
    root, registry = _standard_registry(repo_root, tmp_path, single_source=True)
    first = run_standard_analysis(root, registry)
    workspace = root / ".local/standard-strategy-analysis" / first["analysis_id"]
    first_bytes = (workspace / "deterministic-analysis.json").read_bytes()
    shutil.rmtree(workspace)

    second = run_standard_analysis(root, registry)

    assert second == first
    assert (workspace / "deterministic-analysis.json").read_bytes() == first_bytes


def test_standard_analysis_rejects_source_drift_before_publishing(
    repo_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, registry = _standard_registry(repo_root, tmp_path, single_source=False)
    source_manifest = root / "sources/simulation/manifest.json"
    real_benchmark_series = analysis._benchmark_series

    def mutate_source_after_reads(path: Path) -> dict[str, pd.Series]:
        result = real_benchmark_series(path)
        source_manifest.write_bytes(source_manifest.read_bytes() + b" ")
        return result

    monkeypatch.setattr(analysis, "_benchmark_series", mutate_source_after_reads)

    with pytest.raises(UnifiedAnalysisError, match="changed during analysis"):
        run_standard_analysis(root, registry)
    assert not list(
        (root / ".local/standard-strategy-analysis").glob(
            "*/deterministic-analysis.json"
        )
    )


def test_missing_period_observations_degrade_to_evidence_insufficient(
    repo_root: Path, tmp_path: Path
) -> None:
    root, registry = _standard_registry(repo_root, tmp_path, single_source=True)
    plan_path = root / "config/analysis-plan.json"
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    plan["analyses"]["fixed_periods"] = [
        {"id": "missing", "start": "1990-01-01", "end": "1990-12-31"}
    ]
    plan["analyses"]["rolling"] = {"window_years": 5, "step_months": 3}
    plan["analyses"]["historical_stress"] = [
        {
            "id": "missing-stress",
            "start": "1990-01-01",
            "end": "1990-12-31",
            "max_drawdown_abs_max": 1.0,
        }
    ]
    _write_json(plan_path, plan)

    result = run_standard_analysis(root, registry)

    period_rows = result["robustness"]["periods"]
    assert {row["dimension"] for row in period_rows} == {
        "fixed_period",
        "rolling_period",
    }
    assert all(row["status"] == "evidence_insufficient" for row in period_rows)
    assert result["robustness"]["historical_stress"][0]["status"] == (
        "evidence_insufficient"
    )


def test_standard_analysis_keeps_independent_results_and_evidence_gaps(
    repo_root: Path, tmp_path: Path
) -> None:
    root, registry = _standard_registry(repo_root, tmp_path, single_source=False)

    result = run_standard_analysis(root, registry)

    statuses = {row["status"] for row in result["evidence_rows"]}
    assert {"pass", "evidence_insufficient"}.issubset(statuses)
    assert result["attribution"]["status"] == "evidence_insufficient"
    assert result["robustness"]["cost_execution"][0]["status"] == "evidence_insufficient"
    assert result["sources"]["registered_count"] == 3
    assert result["analysis_configuration"]["analysis_plan"]["path"] == (
        "config/analysis-plan.json"
    )
    assert result["analysis_configuration"]["baseline_config"]["path"] == (
        "config/baseline.json"
    )
    assert result["analysis_configuration"]["analyses"]["bootstrap"]["seed"] == (
        7
    )
    assert result["analysis_configuration"]["scenario_params"]
    assert result["script"]["version"] == "analyze-quant-robustness/1"
    assert result["script"]["entry"].endswith("analyze_quant_robustness.py")
    assert (root / ".local/standard-strategy-analysis" / result["analysis_id"] / "deterministic-analysis.json").is_file()


def test_rolling_window_accepts_exact_calendar_year() -> None:
    index = pd.date_range("2024-01-01", "2024-12-31", freq="D")
    scenario = ScenarioInput(
        scenario_id="baseline",
        returns=pd.Series([0.0] * len(index), index=index),
        balances=pd.DataFrame(),
        positions=pd.DataFrame(),
        orders=pd.DataFrame(),
        events=pd.DataFrame(),
        params={},
    )
    rows, _ = analysis._fixed_and_rolling(
        scenario,
        {
            "fixed_periods": [],
            "rolling": {"window_years": 1, "step_months": 3},
        },
        {
            "cagr_min_exclusive": -1.0,
            "max_drawdown_abs_max": 1.0,
            "calmar_min": -1.0,
        },
    )

    assert rows[0]["scenario_id"] == "rolling-1y-2024-01-01"
    assert rows[0]["status"] != "evidence_insufficient"


def test_standard_analysis_runs_single_source_return_checks(
    repo_root: Path, tmp_path: Path
) -> None:
    root, registry = _standard_registry(repo_root, tmp_path, single_source=True)

    result = run_standard_analysis(root, registry)

    assert result["challenge_results"] == []
    assert result["robustness"]["bootstrap"]
    assert result["cross_scenario"]["status"] == "evidence_insufficient"


def test_standard_skill_script_requires_one_explicit_source_registry(repo_root: Path) -> None:
    completed = subprocess.run(
        [
            str(repo_root / ".venv/Scripts/python.exe"),
            str(
                repo_root
                / ".agents/skills/analyze-quant-robustness/scripts/analyze_quant_robustness.py"
            ),
            "run",
            "--repository",
            str(repo_root),
        ],
        cwd=repo_root,
        capture_output=True,
        text=True,
        shell=False,
        check=False,
    )

    assert completed.returncode == 2
    assert "--source-registry" in completed.stderr


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
    )

    facts = _position_facts(scenario, {"510300.XSHG": "equity"})

    assert facts.loc[0, "common_stop"] == 9.0
    assert facts.loc[0, "attribution_reason"] == "entry_breakout"


def test_security_pnl_facts_include_full_exit_without_fake_source_position() -> None:
    dates = pd.to_datetime(["2024-01-02", "2024-01-03"])
    scenario = ScenarioInput(
        scenario_id="baseline",
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


def test_risk_diagnostics_use_actual_exposure_and_optional_turtle_units() -> None:
    date = pd.Timestamp("2024-01-02")
    scenario = ScenarioInput(
        scenario_id="baseline",
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
                    "full_position_redistribution",
                    "full_position_redistribution",
                    "protective_stop",
                    "protective_stop",
                ],
                "details_json": [
                    json.dumps(
                        {
                            "effective_risk_units": 12.0,
                            "portfolio_unit_cap": 12.0,
                        }
                    ),
                    "{}",
                    "{}",
                    "{}",
                ],
            }
        ),
        params={
            "risk": {
                "unit_risk_per_n": 0.01,
                "asset_group_unit_cap": 6.0,
                "portfolio_unit_cap": 12.0,
            }
        },
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

    assert metrics["maximum_security_weight"] == pytest.approx(0.4)
    assert metrics["maximum_asset_group_weight"] == pytest.approx(0.4)
    assert metrics["maximum_planned_loss_ratio"] == pytest.approx(0.01)
    assert metrics["maximum_effective_risk_units"] == pytest.approx(12.0)
    assert metrics["maximum_portfolio_unit_utilization"] == pytest.approx(1.0)
    assert metrics["redistribution_event_count"] == 1
    assert metrics["protective_stop_events"] == 1
    assert "mark_to_market_security_weight_above_entry_cap_rows" not in metrics
    assert "realized_60d_volatility_above_target_days" not in metrics


def test_risk_diagnostics_accept_joinquant_results_without_turtle_evidence() -> None:
    date = pd.Timestamp("2024-01-02")
    scenario = ScenarioInput(
        scenario_id="joinquant",
        returns=pd.Series([0.0], index=[date]),
        balances=pd.DataFrame(
            {"date": [date], "total_value": [1000.0], "cash": [1000.0]}
        ),
        positions=pd.DataFrame(),
        orders=pd.DataFrame(
            columns=["status", "filled", "action", "price", "commission", "gains"]
        ),
        events=pd.DataFrame(),
        params={},
    )

    metrics = _risk_metrics(scenario, pd.DataFrame())

    assert metrics["maximum_effective_risk_units"] is None
    assert metrics["maximum_portfolio_unit_utilization"] is None
    assert metrics["redistribution_event_count"] == 0


def test_deletion_sensitivity_keeps_unheld_securities_and_groups_in_matrix() -> None:
    dates = pd.to_datetime(["2024-01-02", "2024-01-03"])
    scenario = ScenarioInput(
        scenario_id="baseline",
        returns=pd.Series([0.01, -0.01], index=dates),
        balances=pd.DataFrame(),
        positions=pd.DataFrame(),
        orders=pd.DataFrame(),
        events=pd.DataFrame(),
        params={},
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
