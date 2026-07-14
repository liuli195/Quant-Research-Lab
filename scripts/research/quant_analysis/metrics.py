from __future__ import annotations

import math
import statistics
from collections import defaultdict

from .contracts import AnalysisBundle


def _product_return(values: list[float]) -> float:
    wealth = 1.0
    for value in values:
        wealth *= 1.0 + value
    return wealth - 1.0


def _ratio(numerator: float, denominator: float) -> float | None:
    return None if denominator == 0 else numerator / denominator


def _drawdown(equities: list[float]) -> tuple[float, int, int]:
    peak = equities[0]
    max_drawdown = 0.0
    current_duration = 0
    max_duration = 0
    recovery_duration = 0
    trough_index = 0
    peak_index = 0
    for index, value in enumerate(equities):
        if value >= peak:
            peak = value
            peak_index = index
            current_duration = 0
        else:
            current_duration += 1
            max_duration = max(max_duration, current_duration)
        drawdown = value / peak - 1.0
        if drawdown < max_drawdown:
            max_drawdown = drawdown
            trough_index = index
            recovery_duration = 0
        elif index > trough_index and value >= equities[peak_index]:
            recovery_duration = index - trough_index
    return max_drawdown, max_duration, recovery_duration


def calculate_performance(
    bundle: AnalysisBundle,
    annualization: int = 252,
) -> dict[str, object]:
    if annualization <= 0:
        raise ValueError("annualization must be positive")
    return_rows = sorted(bundle.rows("returns"), key=lambda row: str(row["date"]))
    daily_returns = [float(row["return"]) for row in return_rows]
    period_returns = daily_returns[1:] if daily_returns and daily_returns[0] == 0 else daily_returns
    periods = max(1, len(period_returns))
    cumulative_return = _product_return(period_returns)
    cagr = (1.0 + cumulative_return) ** (annualization / periods) - 1.0
    mean_return = statistics.fmean(period_returns) if period_returns else 0.0
    volatility = (
        statistics.stdev(period_returns) * math.sqrt(annualization)
        if len(period_returns) > 1
        else 0.0
    )
    downside = math.sqrt(
        statistics.fmean(min(value, 0.0) ** 2 for value in period_returns)
    ) * math.sqrt(annualization) if period_returns else 0.0
    sharpe = _ratio(mean_return * annualization, volatility)
    sortino = _ratio(mean_return * annualization, downside)

    equity_rows = sorted(bundle.rows("equity"), key=lambda row: str(row["date"]))
    equities = [float(row["equity"]) for row in equity_rows]
    max_drawdown, max_duration, recovery_duration = _drawdown(equities)
    calmar = _ratio(cagr, abs(max_drawdown))

    trades = list(bundle.rows("trades"))
    wins = [float(row["pnl"]) for row in trades if float(row["pnl"]) > 0]
    losses = [float(row["pnl"]) for row in trades if float(row["pnl"]) < 0]
    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))
    average_win = statistics.fmean(wins) if wins else 0.0
    average_loss = abs(statistics.fmean(losses)) if losses else 0.0
    trade_pnls = [float(row["pnl"]) for row in trades]

    orders = list(bundle.rows("orders"))
    filled_notional = sum(
        float(row["filled_quantity"]) * float(row["fill_price"] or 0.0)
        for row in orders
        if row["status"] == "filled"
    )
    average_equity = statistics.fmean(equities)
    risk_rows = list(bundle.rows("risk"))
    invested = [float(row["invested_ratio"]) for row in risk_rows]
    cash = [float(row["cash_ratio"]) for row in risk_rows]

    monthly: dict[str, list[float]] = defaultdict(list)
    annual: dict[str, list[float]] = defaultdict(list)
    for row in return_rows:
        current_date = str(row["date"])
        monthly[current_date[:7]].append(float(row["return"]))
        annual[current_date[:4]].append(float(row["return"]))
    rolling = {
        str(return_rows[index]["date"]): _product_return(
            [float(row["return"]) for row in return_rows[index - annualization + 1 : index + 1]]
        )
        for index in range(annualization - 1, len(return_rows))
    }

    nonzero_returns = [value for value in period_returns if value != 0]
    return {
        "cumulative_return": cumulative_return,
        "cagr": cagr,
        "annual_returns": {key: _product_return(values) for key, values in sorted(annual.items())},
        "monthly_returns": {key: _product_return(values) for key, values in sorted(monthly.items())},
        "rolling_annual_returns": rolling,
        "annualized_volatility": volatility,
        "downside_deviation": downside,
        "sharpe": sharpe,
        "sortino": sortino,
        "calmar": calmar,
        "max_drawdown": max_drawdown,
        "max_drawdown_duration": max_duration,
        "drawdown_recovery_duration": recovery_duration,
        "daily_hit_rate": (
            sum(value > 0 for value in nonzero_returns) / len(nonzero_returns)
            if nonzero_returns
            else None
        ),
        "trade_count": len(trades),
        "win_rate": len(wins) / len(trades) if trades else None,
        "average_win": average_win,
        "average_loss": average_loss,
        "payoff_ratio": _ratio(average_win, average_loss),
        "expectancy": statistics.fmean(trade_pnls) if trade_pnls else None,
        "profit_factor": _ratio(gross_profit, gross_loss),
        "turnover": _ratio(filled_notional, average_equity),
        "fees": sum(float(row["fee"]) for row in orders),
        "average_invested_ratio": statistics.fmean(invested),
        "median_invested_ratio": statistics.median(invested),
        "below_half_ratio": sum(value < 0.5 for value in invested) / len(invested),
        "near_full_ratio": sum(value >= 0.9 for value in invested) / len(invested),
        "average_cash_ratio": statistics.fmean(cash),
        "maximum_portfolio_risk_usage": max(
            float(row["portfolio_risk_usage"]) for row in risk_rows
        ),
        "maximum_target_volatility_usage": max(
            float(row["target_volatility_usage"] or 0.0) for row in risk_rows
        ),
        "rejected_order_count": sum(row["status"] != "filled" for row in orders),
        "protective_stop_count": sum(
            row["exit_reason"] == "protective_stop" for row in trades
        ),
    }
