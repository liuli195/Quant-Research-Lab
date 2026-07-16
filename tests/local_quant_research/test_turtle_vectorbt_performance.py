from __future__ import annotations

import hashlib
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

from turtle_etf import vectorbt_benchmark  # noqa: E402
from turtle_etf.vectorbt_inputs import CorporateActionApplication  # noqa: E402


REFERENCE_SCENARIOS = (
    "immediate-11-etf",
    "immediate-17-etf",
    "delayed-11-etf-1d",
)


def test_benchmark_runs_cold_and_warm_once_then_cleans_temp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    simulations = iter(("cold-simulation", "warm-simulation"))
    facts = SimpleNamespace(name="same-facts")
    run_calls: list[str] = []

    def run(inputs: object, config: object) -> str:
        value = next(simulations)
        run_calls.append(value)
        return value

    monkeypatch.setattr(vectorbt_benchmark, "run_vectorbt_simulation", run)
    monkeypatch.setattr(
        vectorbt_benchmark,
        "to_joinquant_facts",
        lambda inputs, simulation, scenario_id: facts,
    )

    def materialize(path: Path, value: object) -> str:
        path.mkdir(parents=True)
        (path / "evidence.bin").write_bytes(b"same")
        return "e" * 64

    monkeypatch.setattr(
        vectorbt_benchmark, "materialize_execution_facts", materialize
    )
    ticks = iter((10.0, 11.5, 20.0, 20.4))
    monkeypatch.setattr(vectorbt_benchmark.time, "perf_counter", lambda: next(ticks))
    work = tmp_path / "benchmark-work"

    result = vectorbt_benchmark.benchmark_scenario(
        prepared_inputs=SimpleNamespace(
            identity="prepared",
            corporate_action_applications=(
                CorporateActionApplication(
                    source_event_id="FUND_DIVIDEND:101",
                    security="ETF-A",
                    event_type="split",
                    effective_date="2026-01-05",
                    application_date="2026-01-06",
                    announcement_date="2026-01-05",
                    knowledge_cutoff_date="2026-01-10",
                    evidence_timing="point_in_time",
                    split_ratio=2.0,
                    cash_per_share=None,
                    cumulative_factor=2.0,
                    price_basis_changed=True,
                    source="joinquant.finance.FUND_DIVIDEND",
                    source_record_sha256="b" * 64,
                ),
            ),
        ),
        config={"scenario_id": "baseline", "research": {"initial_cash": 1}},
        scenario_id="baseline",
        work_dir=work,
        code_sha256="a" * 64,
        config_sha256="b" * 64,
    )

    assert run_calls == ["cold-simulation", "warm-simulation"]
    assert result.facts is facts
    assert result.performance["cold_seconds"] == 1.5
    assert result.performance["warm_seconds"] == pytest.approx(0.4)
    assert result.performance["cold_result_sha256"] == "e" * 64
    assert result.performance["warm_result_sha256"] == "e" * 64
    assert result.performance["result_match"] is True
    assert result.performance["limit_seconds"] == 180.0
    assert result.performance["cleanup"] == {
        "cold_temporary_result_removed": True,
        "warm_temporary_result_removed": True,
        "work_directory_removed": True,
        "verified": True,
    }
    assert not work.exists()


def test_benchmark_rejects_nondeterminism_and_cleans_temp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        vectorbt_benchmark,
        "run_vectorbt_simulation",
        lambda inputs, config: object(),
    )
    monkeypatch.setattr(
        vectorbt_benchmark,
        "to_joinquant_facts",
        lambda inputs, simulation, scenario_id: object(),
    )
    digests = iter(("c" * 64, "d" * 64))

    def materialize(path: Path, facts: object) -> str:
        path.mkdir(parents=True)
        return next(digests)

    monkeypatch.setattr(
        vectorbt_benchmark, "materialize_execution_facts", materialize
    )
    work = tmp_path / "benchmark-work"

    with pytest.raises(vectorbt_benchmark.PerformanceGateError, match="deterministic"):
        vectorbt_benchmark.benchmark_scenario(
            prepared_inputs=SimpleNamespace(identity="prepared"),
            config={"scenario_id": "baseline"},
            scenario_id="baseline",
            work_dir=work,
            code_sha256="a" * 64,
            config_sha256="b" * 64,
        )

    assert not work.exists()


def test_benchmark_rejects_either_run_over_180_seconds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        vectorbt_benchmark, "run_vectorbt_simulation", lambda inputs, config: object()
    )
    monkeypatch.setattr(
        vectorbt_benchmark,
        "to_joinquant_facts",
        lambda inputs, simulation, scenario_id: object(),
    )

    def materialize(path: Path, facts: object) -> str:
        path.mkdir(parents=True)
        return "e" * 64

    monkeypatch.setattr(
        vectorbt_benchmark, "materialize_execution_facts", materialize
    )
    ticks = iter((0.0, 180.1, 200.0, 200.2))
    monkeypatch.setattr(vectorbt_benchmark.time, "perf_counter", lambda: next(ticks))

    with pytest.raises(vectorbt_benchmark.PerformanceGateError, match="180"):
        vectorbt_benchmark.benchmark_scenario(
            prepared_inputs=SimpleNamespace(identity="prepared"),
            config={"scenario_id": "baseline"},
            scenario_id="baseline",
            work_dir=tmp_path / "benchmark-work",
            code_sha256="a" * 64,
            config_sha256="b" * 64,
        )


def test_release_performance_baseline_has_real_three_by_five_samples(
    repo_root: Path,
) -> None:
    fixture_root = repo_root / "tests/local_quant_research/fixtures"
    performance = json.loads(
        (fixture_root / "performance-baseline.json").read_text(encoding="utf-8")
    )
    behavior = json.loads(
        (fixture_root / "local-research-v1-baseline.json").read_text(
            encoding="utf-8"
        )
    )
    behavior_by_scenario = {
        item["scenario"]: item for item in behavior["scenarios"]
    }

    assert tuple(performance["scenarios"]) == REFERENCE_SCENARIOS
    for scenario in REFERENCE_SCENARIOS:
        metrics = performance["scenarios"][scenario]
        expected_digest = hashlib.sha256(
            json.dumps(
                behavior_by_scenario[scenario],
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        assert set(metrics) == {
            "snapshot_id",
            "summary_sha256",
            "cold_process",
            "warm",
        }
        assert metrics["summary_sha256"] == expected_digest
        assert set(metrics["cold_process"]) == {
            "median_seconds",
            "median_peak_memory_mib",
            "median_package_bytes",
        }
        assert set(metrics["warm"]) == {
            "median_seconds",
            "median_peak_memory_mib",
            "phase_medians_seconds",
        }
        assert set(metrics["warm"]["phase_medians_seconds"]) == {
            "snapshot",
            "prepare",
            "simulate",
            "adapt",
            "materialize",
        }
        assert 0.0 < metrics["cold_process"]["median_seconds"] <= 180.0
        assert metrics["cold_process"]["median_peak_memory_mib"] > 0.0
        assert metrics["cold_process"]["median_package_bytes"] > 0
        assert 0.0 < metrics["warm"]["median_seconds"] <= 180.0
        assert metrics["warm"]["median_peak_memory_mib"] > 0.0
        assert all(
            value > 0.0
            for value in metrics["warm"]["phase_medians_seconds"].values()
        )
