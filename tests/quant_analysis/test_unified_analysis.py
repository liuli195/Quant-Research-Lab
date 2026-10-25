from __future__ import annotations

import json
import copy
import hashlib
from dataclasses import replace
from types import SimpleNamespace
from datetime import date
from pathlib import Path

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from quant_analysis import unified_analysis as analysis
from quant_analysis import package_source
from quant_analysis.unified_analysis import (
    ScenarioInput,
    UnifiedAnalysisError,
    _deletion_sensitivity,
    _position_facts,
    _position_shocks,
    _risk_metrics,
    align_three_way_benchmarks,
    calculate_return_metrics,
    evaluate_metrics,
    run_standard_analysis,
)
from scripts.research.market_data.benchmark_sets import (
    BENCHMARK_IDS as CONTRACT_BENCHMARK_IDS,
    SourcePayload,
    write_benchmark_set,
)
from scripts.research.result_contract import ResultExtension
from tests.local_quant_research.test_analysis_data_views import _write_result_package


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


def _standard_plan_document() -> dict[str, object]:
    return {
        "schema_version": "strategy-analysis-plan/2",
        "strategy_id": "minimal",
        "baseline_config": "baseline.json",
        "scenarios": [
            {"scenario_id": "baseline", "dimension": "baseline", "overrides": {}}
        ],
        "analyses": {
            "fixed_periods": [
                {"id": "fixture", "start": "2024-01-01", "end": "2024-12-31"}
            ],
            "rolling": {
                "window_years": 1,
                "step_months": 3,
                "minimum_positive_cagr_fraction": 0.7,
            },
            "bootstrap": {
                "block_sizes": [1],
                "paths": 3,
                "horizon_days": 1,
                "seed": 7,
                "thresholds": {
                    "probability_drawdown_over_20pct_max": 1.0,
                    "probability_drawdown_over_30pct_max": 1.0,
                    "median_terminal_return_min_exclusive": -1.0,
                },
            },
            "historical_stress": [
                {
                    "id": "stress-fixture",
                    "start": "2024-01-01",
                    "end": "2024-12-31",
                    "max_drawdown_abs_max": 1.0,
                }
            ],
            "position_shocks": [
                {
                    "id": "shock-fixture",
                    "asset_group_shocks": {"etf": -0.1},
                    "maximum_loss_abs_max": 1.0,
                }
            ],
            "cvar": [
                {
                    "id": "cvar-fixture",
                    "horizon_days": 1,
                    "confidence": 0.95,
                    "maximum_loss_abs_max": 1.0,
                    "minimum_tail_observations": 1,
                }
            ],
        },
        "thresholds": {
            "cagr_min_exclusive": 0.0,
            "max_drawdown_abs_max": 0.2,
            "calmar_min": 0.5,
        },
    }


def _standard_package_inputs(
    repo_root: Path,
    tmp_path: Path,
    *,
    extensions: tuple[ResultExtension, ...] | None = None,
    scenario: dict[str, object] | None = None,
) -> tuple[Path, list[Path], Path, Path]:
    root = tmp_path / "package-repository"
    root.mkdir()
    scenario = scenario or {
        "project_id": "minimal",
        "scenario_id": "baseline",
        "universe": [{"security": "TEST.X", "asset_group": "etf"}],
        "risk": {},
    }
    package = _write_result_package(
        root / "package",
        strategy_id="minimal",
        scenario=scenario,
        extensions=extensions,
    )
    config = root / "config"
    _write_json(config / "baseline.json", scenario)
    _write_json(config / "analysis-plan.json", _standard_plan_document())
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
    return root, [package], config / "analysis-plan.json", benchmark_set.root / "manifest.json"


@pytest.fixture
def memory_analysis_inputs(repo_root: Path, monkeypatch: pytest.MonkeyPatch):
    from scripts.research import result_package
    from scripts.research.result_contract import ExecutionBundle, ExecutionRun
    from scripts.research.market_data.benchmark_sets import BenchmarkSet
    from tests.local_quant_research.test_analysis_data_views import _AnalysisLedger
    from quant_analysis import evidence as matrix

    root = repo_root
    params = {"project_id": "minimal", "scenario_id": "baseline",
              "universe": [{"security": "TEST.X", "asset_group": "etf"}], "risk": {}}
    plan = _standard_plan_document()
    expanded = {"schema_version": "analysis-scenarios/1", "strategy_id": "minimal",
                "analysis_plan": "config/analysis-plan.json", "analysis_plan_sha256": analysis.evidence_digest(plan),
                "baseline_config": "baseline.json", "baseline_config_sha256": analysis.evidence_digest(params),
                "universe": {"TEST.X": "etf"}, "analyses": copy.deepcopy(plan["analyses"]),
                "thresholds": copy.deepcopy(plan["thresholds"]), "scenarios": [{
                    "scenario_id": "baseline", "dimension": "baseline", "overrides": {},
                    "params": copy.deepcopy(params), "params_sha256": analysis.evidence_digest(params)}]}
    run = ExecutionRun(_AnalysisLedger(), {})
    core = result_package._materialize_core(ExecutionBundle(run, run, ("primary",))).tables()
    tables, sources, source_sha, artifacts = {}, {}, {}, {}

    def read_table(path, columns=None, **_kwargs):
        table = tables[Path(path)]
        return table if columns is None else table.select(columns)

    monkeypatch.setattr(analysis.pq, "read_table", read_table)
    def add_source(path, scenario, extensions=()):
        path = Path(path).resolve()
        tables.update({path / "data" / f"{name}.parquet": table for name, table in core.items()})
        manifest = {"extensions": {}}
        for extension in extensions:
            relative = "strategy.py"  # Existing static path; actual input is the Arrow data seam.
            tables[path / relative] = extension.table
            digest = analysis.evidence_digest(extension.table.to_pylist())
            source_sha[path / relative] = digest
            manifest["extensions"][extension.name] = {"status": "complete",
                "schema": [{"name": field.name} for field in extension.table.schema],
                "files": [{"path": relative, "sha256": digest}]}
        sources[path] = package_source.PackageSource(root=path,
            manifest_sha256=analysis.evidence_digest(manifest),
            content_sha256=analysis.evidence_digest({"params": scenario, "extensions": manifest["extensions"]}),
            strategy_id=str(scenario["project_id"]), scenario_id=str(scenario["scenario_id"]),
            params=copy.deepcopy(scenario), capabilities={"common_facts": {"status": "available"},
                "official_risk": {"status": "missing_at_source"},
                "attribution": package_source._attribution_capability(path, manifest)})
        return path

    packages = [add_source(repo_root / "tests/local_quant_research/fixtures/minimal_strategy", params)]
    rows = [{"time": day, "benchmark_id": identity, "returns": 0.0 if day == "2024-01-02" else 0.01}
            for day in ("2024-01-02", "2024-01-03") for identity in CONTRACT_BENCHMARK_IDS]
    benchmark = BenchmarkSet(root=repo_root / "memory-benchmark", benchmark_set_id=analysis.evidence_digest(rows),
                             manifest={"data": {"sha256": analysis.evidence_digest(rows)}})
    tables[benchmark.root / "benchmark-returns.parquet"] = pa.Table.from_pylist(rows)
    source_sha[benchmark.root / "manifest.json"] = analysis.evidence_digest(dict(benchmark.manifest))
    monkeypatch.setattr(analysis, "open_package_sources", lambda paths: tuple(sources[Path(path).resolve()] for path in paths))
    monkeypatch.setattr(analysis, "expand_analysis_plan", lambda _root, _plan: copy.deepcopy(expanded))
    monkeypatch.setattr(analysis, "open_benchmark_set", lambda _root: benchmark)

    def capture_json(path, value):
        artifacts[Path(path)] = (json.dumps(value, ensure_ascii=False, sort_keys=True,
                                           separators=(",", ":"), allow_nan=False) + "\n").encode()
    def capture_matrix(results, path):
        rows = matrix._rows(results)
        table = pa.Table.from_pylist(rows, schema=matrix._SCHEMA).replace_schema_metadata(matrix._metadata(rows))
        sink = pa.BufferOutputStream()
        pq.write_table(table, sink, compression="zstd", use_dictionary=False, write_statistics=True)
        artifacts[Path(path)] = sink.getvalue().to_pybytes()
        return Path(path)
    monkeypatch.setattr(analysis, "_immutable_json", capture_json)
    monkeypatch.setattr(analysis, "_atomic_json", capture_json)
    monkeypatch.setattr(analysis, "build_evidence_matrix", capture_matrix)
    monkeypatch.setattr(analysis, "_sha256", lambda path: hashlib.sha256(artifacts[Path(path)]).hexdigest()
                        if Path(path) in artifacts else source_sha[Path(path)])
    return SimpleNamespace(inputs=(root, packages, root / "config/analysis-plan.json", benchmark.root / "manifest.json"),
                           params=params, expanded=expanded, packages=packages, sources=sources,
                           artifacts=artifacts, add_source=add_source)


def test_standard_analysis_accepts_explicit_package_without_registry(
    memory_analysis_inputs, repo_root: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, packages, plan, benchmark = memory_analysis_inputs.inputs

    result = run_standard_analysis(root, packages, plan, benchmark)

    assert result["sources"]["package_count"] == 1
    assert result["baseline"]["scenario_id"] == "baseline"


def test_standard_analysis_identity_does_not_depend_on_package_location(
    memory_analysis_inputs, repo_root: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, packages, plan, benchmark = memory_analysis_inputs.inputs
    first = run_standard_analysis(root, packages, plan, benchmark)
    relocated = root / "memory-relocated-package"
    memory_analysis_inputs.add_source(relocated, memory_analysis_inputs.params)

    second = run_standard_analysis(root, [relocated], plan, benchmark)

    assert second["analysis_id"] == first["analysis_id"]
    assert second == first


def test_standard_analysis_does_not_call_legacy_source_readers(
    memory_analysis_inputs, repo_root: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, packages, plan, benchmark = memory_analysis_inputs.inputs

    def reject_legacy_reader(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("standard analysis must not use a legacy source reader")

    monkeypatch.setattr(analysis, "open_analysis_database", reject_legacy_reader, raising=False)
    monkeypatch.setattr(package_source, "open_analysis_source", reject_legacy_reader, raising=False)

    assert run_standard_analysis(root, packages, plan, benchmark)["analysis_id"]


def test_standard_analysis_resolves_baseline_from_the_plan_bundle(
    memory_analysis_inputs, repo_root: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from quant_analysis.analysis_plan import expand_analysis_plan
    actual = expand_analysis_plan(repo_root, repo_root / "joinquant/strategies/strategy-003/research/analysis-plan.json")
    assert actual["baseline_config"] == "baseline.json"
    memory_analysis_inputs.expanded["baseline_config"] = actual["baseline_config"]
    result = run_standard_analysis(*memory_analysis_inputs.inputs)
    assert result["analysis_configuration"]["baseline_config"]["path"] == "baseline.json"


def test_standard_analysis_rejects_missing_plan_scenario_package(
    memory_analysis_inputs, repo_root: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, packages, plan_path, benchmark = memory_analysis_inputs.inputs
    plan = memory_analysis_inputs.expanded
    plan["scenarios"].append(
        {"scenario_id": "alternate", "dimension": "parameter", "overrides": {}}
    )

    with pytest.raises(UnifiedAnalysisError, match="scenarios differ"):
        run_standard_analysis(root, packages, plan_path, benchmark)


def test_standard_analysis_rejects_package_params_that_differ_from_plan(
    memory_analysis_inputs, repo_root: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, packages, plan_path, benchmark = memory_analysis_inputs.inputs
    memory_analysis_inputs.expanded["scenarios"][0]["params"]["risk"] = {"unit": 0.01}

    with pytest.raises(UnifiedAnalysisError, match="parameters differ"):
        run_standard_analysis(root, packages, plan_path, benchmark)


def test_standard_analysis_discovers_attribution_by_fields_not_extension_name(
    memory_analysis_inputs, repo_root: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    extension = ResultExtension(
        name="turtle_etf",
        schema_version="turtle-etf-attribution/2",
        table=pa.table(
            {
                "time": ["2024-01-02 16:00:00"],
                "event_id": ["decision-1"],
                "event_type": ["decision"],
                "security": ["TEST.X"],
                "reason_code": ["breakout_entry"],
                "details_json": ["{}"],
            }
        ),
        unique_key=("event_id",),
        evidence={"status": "complete"},
    )
    root, packages, plan, benchmark = memory_analysis_inputs.inputs
    scenario = memory_analysis_inputs.params
    memory_analysis_inputs.add_source(packages[0], scenario, (extension,))

    result = run_standard_analysis(root, packages, plan, benchmark)

    assert result["attribution"]["status"] == "available"
    assert result["attribution"]["event_counts"] == {"breakout_entry": 1}


def test_standard_analysis_preserves_source_loss_events_and_daily_reconciliation(
    memory_analysis_inputs, repo_root: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    extension = ResultExtension(
        name="audit_events",
        schema_version="attribution/1",
        table=pa.table(
            {
                "time": ["2024-01-03 16:00:00", "2024-01-03 16:00:00"],
                "event_id": ["loss-exit", "offsetting-gain"],
                "event_type": ["valuation", "valuation"],
                "security": ["TEST.X", "OFFSET.X"],
                "reason_code": ["protective_stop", "mark_to_market"],
                "details_json": [
                    json.dumps(
                        {
                            "action": "full_exit",
                            "position_before": 10,
                            "position_after": 0,
                            "source_reason": "protective_stop",
                            "security_daily_pnl": -5.0,
                            "common_stop_before": 9.5,
                            "fill_price": 9.0,
                            "daily_security_pnl_total": 10.0,
                            "portfolio_daily_pnl": 10.0,
                            "reconciliation_difference": 0.0,
                        }
                    ),
                    json.dumps(
                        {
                            "security_daily_pnl": 15.0,
                            "source_reason": "mark_to_market",
                            "daily_security_pnl_total": 10.0,
                            "portfolio_daily_pnl": 10.0,
                            "reconciliation_difference": 0.0,
                        }
                    ),
                ],
            }
        ),
        unique_key=("event_id",),
        evidence={"status": "complete"},
    )
    root, packages, plan, benchmark = memory_analysis_inputs.inputs
    scenario = copy.deepcopy(memory_analysis_inputs.params)
    scenario["universe"].append({"security": "OFFSET.X", "asset_group": "etf"})
    memory_analysis_inputs.expanded["universe"]["OFFSET.X"] = "etf"
    memory_analysis_inputs.expanded["scenarios"][0]["params"] = copy.deepcopy(scenario)
    memory_analysis_inputs.expanded["scenarios"][0]["params_sha256"] = analysis.evidence_digest(scenario)
    memory_analysis_inputs.expanded["baseline_config_sha256"] = analysis.evidence_digest(scenario)
    memory_analysis_inputs.add_source(packages[0], scenario, (extension,))

    result = run_standard_analysis(root, packages, plan, benchmark)

    assert result["attribution"]["loss_events"] == [
        {
            "event_id": "loss-exit",
            "security": "TEST.X",
            "date": "2024-01-03",
            "security_daily_pnl": -5.0,
            "reason_code": "protective_stop",
            "source_reason": "protective_stop",
            "is_exit": True,
            "evidence": {
                "entry": {"status": "evidence_insufficient", "reason": "missing_at_source"},
                "common_stop_before": {"status": "available", "value": 9.5},
                "previous_trading_day_signal": {
                    "status": "evidence_insufficient",
                    "reason": "missing_at_source",
                },
                "fill_price": {"status": "available", "value": 9.0},
                "stop_failure_loss": {
                    "status": "evidence_insufficient",
                    "reason": "missing_at_source",
                },
            },
        }
    ]
    assert result["attribution"]["loss_reconciliation"] == [
        {
            "date": "2024-01-03",
            "daily_security_pnl_total": 10.0,
            "portfolio_daily_pnl": 10.0,
            "reconciliation_difference": 0.0,
            "tolerance": 0.02,
            "status": "reconciled",
        }
    ]


def test_standard_analysis_rejects_invalid_benchmark_evidence(
    memory_analysis_inputs, repo_root: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, packages, plan, benchmark = memory_analysis_inputs.inputs
    from scripts.research.market_data.benchmark_sets import BenchmarkSetError
    def invalid_benchmark(_root):
        raise BenchmarkSetError("benchmark data digest mismatch")
    monkeypatch.setattr(analysis, "open_benchmark_set", invalid_benchmark)

    with pytest.raises(UnifiedAnalysisError, match="benchmark.*digest"):
        run_standard_analysis(root, packages, plan, benchmark)


def test_standard_analysis_is_byte_deterministic_after_fresh_output(
    memory_analysis_inputs, repo_root: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, packages, plan, benchmark = memory_analysis_inputs.inputs
    first = run_standard_analysis(root, packages, plan, benchmark)
    workspace = root / ".local/standard-strategy-analysis" / first["analysis_id"]
    first_bytes = memory_analysis_inputs.artifacts[workspace / "deterministic-analysis.json"]
    memory_analysis_inputs.artifacts.clear()

    second = run_standard_analysis(root, packages, plan, benchmark)

    assert second == first
    assert memory_analysis_inputs.artifacts[workspace / "deterministic-analysis.json"] == first_bytes


def test_standard_analysis_rejects_source_drift_before_publishing(
    memory_analysis_inputs, repo_root: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, packages, plan, benchmark = memory_analysis_inputs.inputs
    calls = 0
    def changing_sources(paths):
        nonlocal calls
        calls += 1
        values = tuple(memory_analysis_inputs.sources[Path(path).resolve()] for path in paths)
        return values if calls == 1 else tuple(replace(value, manifest_sha256="f" * 64) for value in values)
    monkeypatch.setattr(analysis, "open_package_sources", changing_sources)
    with pytest.raises(UnifiedAnalysisError, match="changed during analysis"):
        run_standard_analysis(root, packages, plan, benchmark)
    assert calls == 2
    assert memory_analysis_inputs.artifacts == {}


def test_missing_period_observations_degrade_to_evidence_insufficient(
    memory_analysis_inputs, repo_root: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, packages, plan_path, benchmark = memory_analysis_inputs.inputs
    plan = memory_analysis_inputs.expanded
    plan["analyses"]["fixed_periods"] = [
        {"id": "missing", "start": "1990-01-01", "end": "1990-12-31"}
    ]
    plan["analyses"]["rolling"] = {
        "window_years": 5,
        "step_months": 3,
        "minimum_positive_cagr_fraction": 0.7,
    }
    plan["analyses"]["historical_stress"] = [
        {
            "id": "missing-stress",
            "start": "1990-01-01",
            "end": "1990-12-31",
            "max_drawdown_abs_max": 1.0,
        }
    ]

    result = run_standard_analysis(root, packages, plan_path, benchmark)

    period_rows = result["robustness"]["periods"]
    assert {row["dimension"] for row in period_rows} == {
        "fixed_period",
        "rolling_gate",
    }
    assert all(row["status"] == "evidence_insufficient" for row in period_rows)
    assert result["robustness"]["historical_stress"][0]["status"] == (
        "evidence_insufficient"
    )


def test_cost_execution_scenario_uses_standard_package_metrics(
    memory_analysis_inputs, repo_root: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, packages, plan_path, benchmark = memory_analysis_inputs.inputs
    scenario = copy.deepcopy(memory_analysis_inputs.params)
    scenario["scenario_id"] = "double-commission"
    scenario["costs"] = {"commission_multiplier": 2.0}
    packages.append(memory_analysis_inputs.add_source(root / "memory-cost-package", scenario))
    memory_analysis_inputs.expanded["scenarios"].append({
        "scenario_id": "double-commission", "dimension": "cost_execution",
        "overrides": {"costs": {"commission_multiplier": 2.0}}, "params": scenario,
        "params_sha256": analysis.evidence_digest(scenario)})

    result = run_standard_analysis(root, packages, plan_path, benchmark)

    cost = result["robustness"]["cost_execution"]
    assert len(cost) == 1
    assert cost[0]["scenario_id"] == "double-commission"
    assert cost[0]["status"] != "evidence_insufficient"
    assert cost[0]["metrics"]["cagr"] > 0


def test_standard_analysis_keeps_independent_results_and_evidence_gaps(
    memory_analysis_inputs, repo_root: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, packages, plan, benchmark = memory_analysis_inputs.inputs

    result = run_standard_analysis(root, packages, plan, benchmark)

    statuses = {row["status"] for row in result["evidence_rows"]}
    assert {"pass", "evidence_insufficient"}.issubset(statuses)
    assert result["attribution"]["status"] == "evidence_insufficient"
    assert result["robustness"]["cost_execution"] == []
    assert result["sources"]["package_count"] == 1
    assert result["analysis_configuration"]["analysis_plan"]["path"] == (
        "config/analysis-plan.json"
    )
    assert result["analysis_configuration"]["baseline_config"]["path"] == "baseline.json"
    assert result["analysis_configuration"]["analyses"]["bootstrap"]["seed"] == (
        7
    )
    assert result["analysis_configuration"]["scenario_params"]
    assert result["script"]["version"] == "analyze-quant-robustness/2"
    assert result["script"]["entry"].endswith("analyze_quant_robustness.py")
    assert root / ".local/standard-strategy-analysis" / result["analysis_id"] / "deterministic-analysis.json" in memory_analysis_inputs.artifacts


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
            "rolling": {
                "window_years": 1,
                "step_months": 3,
                "minimum_positive_cagr_fraction": 0.7,
            },
        },
        {
            "cagr_min_exclusive": -1.0,
            "max_drawdown_abs_max": 1.0,
            "calmar_min": -1.0,
        },
    )

    assert rows[0]["scenario_id"] == "rolling-1y-2024-01-01"
    assert rows[0]["status"] != "evidence_insufficient"
    assert rows[-1]["dimension"] == "rolling_gate"


def test_standard_analysis_runs_single_source_return_checks(
    memory_analysis_inputs, repo_root: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, packages, plan, benchmark = memory_analysis_inputs.inputs

    result = run_standard_analysis(root, packages, plan, benchmark)

    assert result["challenge_results"] == []
    assert result["robustness"]["bootstrap"]
    assert result["cross_scenario"]["status"] == "evidence_insufficient"


def test_standard_skill_script_requires_explicit_package_plan_and_benchmark(
    repo_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from analyze_quant_robustness import main

    with pytest.raises(SystemExit) as error:
        main(["run", "--repository", str(repo_root)])

    stderr = capsys.readouterr().err
    assert error.value.code == 2
    assert "--package" in stderr
    assert "--analysis-plan" in stderr
    assert "--benchmark-manifest" in stderr
    assert "--source-registry" not in stderr


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
