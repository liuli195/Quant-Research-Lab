from __future__ import annotations

import math
import statistics
from typing import Mapping


class BenchmarkAlignmentError(ValueError):
    """Raised when strategy and benchmark returns do not share exact dates."""


def _product_return(values: list[float]) -> float:
    wealth = 1.0
    for value in values:
        wealth *= 1.0 + value
    return wealth - 1.0


def _ratio(numerator: float, denominator: float) -> float | None:
    return None if denominator == 0 else numerator / denominator


def _annualized_conditional_return(values: list[float], annualization: int) -> float:
    if not values:
        return 0.0
    total = _product_return(values)
    return (1.0 + total) ** (annualization / len(values)) - 1.0


def calculate_benchmark_statistics(
    strategy_returns: Mapping[str, float],
    benchmark_returns: Mapping[str, float],
    *,
    annualization: int = 252,
) -> dict[str, float | None]:
    strategy_dates = set(strategy_returns)
    benchmark_dates = set(benchmark_returns)
    if strategy_dates != benchmark_dates or not strategy_dates:
        raise BenchmarkAlignmentError("strategy and benchmark dates must align exactly")
    dates = sorted(strategy_dates)
    strategy = [float(strategy_returns[current_date]) for current_date in dates]
    benchmark = [float(benchmark_returns[current_date]) for current_date in dates]
    if any(not math.isfinite(value) for value in (*strategy, *benchmark)):
        raise BenchmarkAlignmentError("strategy and benchmark returns must be finite")
    mean_strategy = statistics.fmean(strategy)
    mean_benchmark = statistics.fmean(benchmark)
    benchmark_variance = statistics.fmean(
        (value - mean_benchmark) ** 2 for value in benchmark
    )
    covariance = statistics.fmean(
        (left - mean_strategy) * (right - mean_benchmark)
        for left, right in zip(strategy, benchmark)
    )
    beta = _ratio(covariance, benchmark_variance)
    alpha = (
        None
        if beta is None
        else (mean_strategy - beta * mean_benchmark) * annualization
    )
    strategy_variance = statistics.fmean(
        (value - mean_strategy) ** 2 for value in strategy
    )
    correlation = _ratio(
        covariance, math.sqrt(strategy_variance * benchmark_variance)
    )
    active = [left - right for left, right in zip(strategy, benchmark)]
    tracking_error = (
        statistics.stdev(active) * math.sqrt(annualization) if len(active) > 1 else 0.0
    )
    information_ratio = _ratio(
        statistics.fmean(active) * annualization, tracking_error
    )
    up_strategy = [left for left, right in zip(strategy, benchmark) if right > 0]
    up_benchmark = [right for right in benchmark if right > 0]
    down_strategy = [left for left, right in zip(strategy, benchmark) if right < 0]
    down_benchmark = [right for right in benchmark if right < 0]
    strategy_total = _product_return(strategy)
    benchmark_total = _product_return(benchmark)
    return {
        "alpha": alpha,
        "beta": beta,
        "correlation": correlation,
        "tracking_error": tracking_error,
        "information_ratio": information_ratio,
        "up_capture": _ratio(
            _annualized_conditional_return(up_strategy, annualization),
            _annualized_conditional_return(up_benchmark, annualization),
        ),
        "down_capture": _ratio(
            _annualized_conditional_return(down_strategy, annualization),
            _annualized_conditional_return(down_benchmark, annualization),
        ),
        "strategy_return": strategy_total,
        "benchmark_return": benchmark_total,
        "active_return": strategy_total - benchmark_total,
        "relative_return": (1.0 + strategy_total) / (1.0 + benchmark_total) - 1.0,
    }
