from __future__ import annotations

from collections import defaultdict
from pathlib import Path
import sys

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

RESEARCH_ROOT = (
    Path(__file__).resolve().parents[2]
    / "joinquant"
    / "strategies"
    / "strategy-003"
    / "research"
)

sys.path.insert(0, str(RESEARCH_ROOT))
from turtle_etf.cli import _standard_analysis_rows  # noqa: E402


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
    expected_up = ((1.021**252) - 1.0) / ((1.01**252) - 1.0)
    expected_down = ((0.981**252) - 1.0) / ((0.99**252) - 1.0)
    assert statistics["up_capture"] == pytest.approx(expected_up)
    assert statistics["down_capture"] == pytest.approx(expected_down)
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
    assert not any(row["key"] == "unattributed" for row in rows)


def test_security_attribution_uses_actual_opposite_etf_pnl_not_end_weight() -> None:
    dates = ("2026-01-05", "2026-01-06")
    positions = (
        {
            "date": dates[0], "security": "ETF-A", "asset_group": "equity",
            "quantity": 5, "close": "10", "market_value": "50",
            "common_stop": "8", "signal_n": "1", "planned_loss": "10",
            "stop_failure_loss": "20",
        },
        {
            "date": dates[0], "security": "ETF-B", "asset_group": "equity",
            "quantity": 5, "close": "10", "market_value": "50",
            "common_stop": "8", "signal_n": "1", "planned_loss": "10",
            "stop_failure_loss": "20",
        },
        {
            "date": dates[1], "security": "ETF-A", "asset_group": "equity",
            "quantity": 5, "close": "12", "market_value": "60",
            "common_stop": "8", "signal_n": "1", "planned_loss": "10",
            "stop_failure_loss": "30",
        },
        {
            "date": dates[1], "security": "ETF-B", "asset_group": "equity",
            "quantity": 5, "close": "8", "market_value": "40",
            "common_stop": "8", "signal_n": "1", "planned_loss": "0",
            "stop_failure_loss": "10",
        },
    )
    risk = tuple(
        {
            "date": current_date, "equity": "100", "cash": "0",
            "invested_ratio": "1", "cash_ratio": "0",
            "portfolio_planned_risk": "20", "portfolio_risk_usage": "0.2",
            "portfolio_volatility": "0.1", "target_volatility_usage": "1",
        }
        for current_date in dates
    )

    rows = _standard_analysis_rows(
        dates=dates,
        groups={"ETF-A": "equity", "ETF-B": "equity"},
        audit_rows=(),
        trade_rows=(),
        position_rows=positions,
        risk_rows=risk,
        benchmark_rows=(),
    )
    day_two = {
        row["security"]: row
        for row in rows["positions"]
        if row["date"] == dates[1]
    }

    assert day_two["ETF-A"]["pnl_contribution"] == pytest.approx(10.0)
    assert day_two["ETF-A"]["return_contribution"] == pytest.approx(0.1)
    assert day_two["ETF-B"]["pnl_contribution"] == pytest.approx(-10.0)
    assert day_two["ETF-B"]["return_contribution"] == pytest.approx(-0.1)
    assert rows["returns"][1]["cash_return_contribution"] == 0.0
