from __future__ import annotations

from pathlib import Path

import numpy as np
import pyarrow.parquet as pq
import pytest

from scripts.research.quant_analysis.benchmarks import (
    BenchmarkAlignmentError,
    calculate_benchmark_statistics,
)
from scripts.research.quant_analysis.cvar import (
    calculate_cvar,
    rolling_compound_returns,
)
from scripts.research.quant_analysis.evidence import (
    ScenarioResult,
    build_evidence_matrix,
)
from scripts.research.quant_analysis.robustness import (
    block_bootstrap,
    summarize_bootstrap,
)


def test_benchmark_statistics_match_alpha_beta_and_capture() -> None:
    benchmark = {
        "2026-01-05": -0.01,
        "2026-01-06": 0.0,
        "2026-01-07": 0.01,
    }
    strategy = {date: 0.001 + 2.0 * value for date, value in benchmark.items()}

    statistics = calculate_benchmark_statistics(strategy, benchmark, annualization=252)

    assert statistics["beta"] == pytest.approx(2.0)
    assert statistics["alpha"] == pytest.approx(0.252)
    assert statistics["correlation"] == pytest.approx(1.0)
    assert "information_ratio" in statistics


def test_benchmark_statistics_reject_date_misalignment() -> None:
    with pytest.raises(BenchmarkAlignmentError, match="dates"):
        calculate_benchmark_statistics(
            {"2026-01-05": 0.01},
            {"2026-01-06": 0.01},
        )


def test_bootstrap_is_seeded_and_includes_loss_from_initial_capital() -> None:
    sample = np.array([0.01, -0.02, 0.03, -0.25, 0.02], dtype=np.float64)

    first = block_bootstrap(sample, block_size=3, paths=128, horizon=37, seed=20260714)
    second = block_bootstrap(sample, block_size=3, paths=128, horizon=37, seed=20260714)

    np.testing.assert_array_equal(first, second)
    assert first.shape == (128, 37)
    assert np.isin(first, sample).all()
    summary = summarize_bootstrap(np.array([[-0.25, 0.0]], dtype=np.float64))
    assert summary["probability_drawdown_over_20pct"] == 1.0


def test_cvar_uses_exact_tail_mass_and_compounds_windows() -> None:
    returns = np.array([-0.10, -0.10, -0.05, 0.0, 0.01], dtype=np.float64)

    assert calculate_cvar(returns, 0.8) == pytest.approx(0.10)
    compounded = rolling_compound_returns(
        np.array([0.1, -0.1, 0.1, -0.1, 0.1, 0.0], dtype=np.float64),
        window=5,
    )
    assert compounded[0] == pytest.approx(1.1 * 0.9 * 1.1 * 0.9 * 1.1 - 1.0)
    assert compounded.shape == (2,)


def test_evidence_matrix_is_deterministic_and_local_only(tmp_path: Path) -> None:
    results = (
        ScenarioResult(
            scenario_id="scenario-a",
            dimension="parameter",
            status="pass",
            metrics={"cagr": 0.1},
            input_sha256="a" * 64,
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
    path = tmp_path / "evidence-matrix.parquet"

    build_evidence_matrix(results, path)
    rows = pq.read_table(path).to_pylist()
    before = path.read_bytes()
    build_evidence_matrix(results, path)

    assert [row["scenario_id"] for row in rows] == ["scenario-a", "scenario-b"]
    assert all(row["authority"] == "local_exploratory" for row in rows)
    assert path.read_bytes() == before
