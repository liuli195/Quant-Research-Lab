from __future__ import annotations

from collections import defaultdict
from pathlib import Path

import pytest

from scripts.research.quant_analysis.attribution import calculate_attribution
from scripts.research.quant_analysis.benchmarks import (
    BenchmarkAlignmentError,
    calculate_benchmark_statistics,
)
from scripts.research.quant_analysis.contracts import (
    STANDARD_TABLES,
    validate_analysis_bundle,
    write_analysis_table,
)


def test_two_benchmark_statistics_match_golden_alpha_beta_and_capture() -> None:
    benchmark = {
        "2026-01-05": -0.01,
        "2026-01-06": 0.0,
        "2026-01-07": 0.01,
    }
    strategy = {date: 0.001 + 2.0 * value for date, value in benchmark.items()}

    statistics = calculate_benchmark_statistics(
        strategy,
        benchmark,
        annualization=252,
    )

    assert statistics["beta"] == pytest.approx(2.0)
    assert statistics["alpha"] == pytest.approx(0.252)
    assert statistics["correlation"] == pytest.approx(1.0)
    assert statistics["up_capture"] == pytest.approx(2.1)
    assert statistics["down_capture"] == pytest.approx(1.9)
    assert "information_ratio" in statistics
    assert "tracking_error" in statistics


def test_benchmark_statistics_reject_date_misalignment() -> None:
    with pytest.raises(BenchmarkAlignmentError, match="dates"):
        calculate_benchmark_statistics(
            {"2026-01-05": 0.01},
            {"2026-01-06": 0.01},
        )


def test_attribution_contains_required_dimensions_and_reconciles(
    tmp_path: Path,
    analysis_rows: dict[str, list[dict[str, object]]],
) -> None:
    for name in STANDARD_TABLES:
        write_analysis_table(name, analysis_rows[name], tmp_path)
    bundle = validate_analysis_bundle(tmp_path)

    rows = calculate_attribution(bundle)

    required_dimensions = {
        "security",
        "asset_group",
        "period",
        "trading_reason",
        "exposure",
        "cash",
        "trend_filter",
        "risk_constraint",
    }
    assert required_dimensions.issubset({row["dimension"] for row in rows})
    assert any(
        row["dimension"] == "cash" and row["key"] == "cash"
        for row in rows
    )
    totals: defaultdict[str, float] = defaultdict(float)
    targets: dict[str, float] = {}
    for row in rows:
        totals[str(row["dimension"])] += float(row["contribution"])
        targets[str(row["dimension"])] = float(row["portfolio_return"])
        assert abs(float(row["reconciliation_error"])) <= 1e-12
    for dimension in required_dimensions:
        assert totals[dimension] == pytest.approx(targets[dimension], abs=1e-12)
