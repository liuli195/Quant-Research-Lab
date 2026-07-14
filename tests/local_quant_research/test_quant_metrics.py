from __future__ import annotations

from pathlib import Path

import pytest

from scripts.research.quant_analysis.contracts import (
    STANDARD_TABLES,
    validate_analysis_bundle,
    write_analysis_table,
)
from scripts.research.quant_analysis.metrics import calculate_performance


def test_performance_matches_golden_returns_drawdown_trades_and_exposure(
    tmp_path: Path,
    analysis_rows: dict[str, list[dict[str, object]]],
) -> None:
    for name in STANDARD_TABLES:
        write_analysis_table(name, analysis_rows[name], tmp_path)
    bundle = validate_analysis_bundle(tmp_path)

    metrics = calculate_performance(bundle, annualization=3)

    assert metrics["cumulative_return"] == pytest.approx(0.21)
    assert metrics["cagr"] == pytest.approx(0.21)
    assert metrics["max_drawdown"] == pytest.approx(-0.10)
    assert metrics["max_drawdown_duration"] == 1
    assert metrics["calmar"] == pytest.approx(2.1)
    assert metrics["trade_count"] == 2
    assert metrics["win_rate"] == pytest.approx(0.5)
    assert metrics["profit_factor"] == pytest.approx(2.0)
    assert metrics["payoff_ratio"] == pytest.approx(2.0)
    assert metrics["expectancy"] == pytest.approx(2.5)
    assert metrics["fees"] == pytest.approx(3.0)
    assert metrics["average_invested_ratio"] == pytest.approx(0.55)
    assert metrics["average_cash_ratio"] == pytest.approx(0.45)
    assert metrics["maximum_portfolio_risk_usage"] == pytest.approx(0.2)
    for required in (
        "annualized_volatility",
        "downside_deviation",
        "sharpe",
        "sortino",
        "turnover",
        "monthly_returns",
        "rolling_annual_returns",
    ):
        assert required in metrics
