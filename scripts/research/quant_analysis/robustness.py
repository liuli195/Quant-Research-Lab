from __future__ import annotations

import copy
import math
from datetime import date, timedelta
from typing import Callable, Mapping, Sequence

import numpy as np

from .contracts import AnalysisBundle
from .evidence import ScenarioResult, evidence_digest
from .metrics import calculate_performance


BOOTSTRAP_BLOCK_SIZES = (5, 20, 60)
BOOTSTRAP_PATHS = 10_000
BOOTSTRAP_HORIZON = 756
BOOTSTRAP_SEED = 20260714
_PATH_THRESHOLDS = {
    "cagr_min_exclusive": 0.0,
    "max_drawdown_abs_max": 0.20,
}


def _scenario(
    scenario_id: str,
    dimension: str,
    overrides: Mapping[str, object],
    *,
    thresholds: Mapping[str, float] | None = None,
) -> dict[str, object]:
    return {
        "scenario_id": scenario_id,
        "dimension": dimension,
        "overrides": dict(overrides),
        "thresholds": dict(_PATH_THRESHOLDS if thresholds is None else thresholds),
    }


def parameter_scenarios() -> tuple[dict[str, object], ...]:
    return (
        _scenario("entry-40", "parameter", {"signal": {"entry_days": 40}}),
        _scenario("entry-60", "parameter", {"signal": {"entry_days": 60}}),
        _scenario("stop-1.5n", "parameter", {"signal": {"stop_n": 1.5}}),
        _scenario("stop-2.5n", "parameter", {"signal": {"stop_n": 2.5}}),
        _scenario(
            "covariance-120d",
            "parameter",
            {"risk": {"covariance": {"method": "sample", "window_days": 120}}},
        ),
        _scenario(
            "covariance-ewma-30d",
            "parameter",
            {
                "risk": {
                    "covariance": {
                        "method": "ewma",
                        "half_life_days": 30,
                        "window_days": 120,
                    }
                }
            },
        ),
    )


def fixed_period_scenarios(end_date: str) -> tuple[dict[str, object], ...]:
    end = date.fromisoformat(end_date)
    periods = (
        ("period-2015-2018", date(2015, 1, 1), date(2018, 12, 31)),
        ("period-2019-2022", date(2019, 1, 1), date(2022, 12, 31)),
        ("period-2023-end", date(2023, 1, 1), end),
    )
    return tuple(
        _scenario(
            scenario_id,
            "fixed_period",
            {
                "research_window": {
                    "start_date": start.isoformat(),
                    "end_date": stop.isoformat(),
                }
            },
        )
        for scenario_id, start, stop in periods
    )


def _next_quarter(value: date) -> date:
    month = value.month + 3
    year = value.year + (month - 1) // 12
    return date(year, (month - 1) % 12 + 1, 1)


def rolling_three_year_scenarios(
    start_date: str,
    end_date: str,
) -> tuple[dict[str, object], ...]:
    current = date.fromisoformat(start_date)
    end = date.fromisoformat(end_date)
    scenarios: list[dict[str, object]] = []
    while True:
        stop = current.replace(year=current.year + 3) - timedelta(days=1)
        if stop > end:
            break
        scenarios.append(
            _scenario(
                f"rolling-3y-{current.isoformat()}",
                "rolling_period",
                {
                    "research_window": {
                        "start_date": current.isoformat(),
                        "end_date": stop.isoformat(),
                    }
                },
            )
        )
        current = _next_quarter(current)
    return tuple(scenarios)


def asset_deletion_scenarios(
    *,
    securities: Sequence[str],
    asset_groups: Sequence[str],
) -> tuple[dict[str, object], ...]:
    security_rows = tuple(
        _scenario(
            f"delete-etf-{security.lower().replace('.', '-')}",
            "asset_delete_etf",
            {"exclude_securities": [security]},
        )
        for security in securities
    )
    group_rows = tuple(
        _scenario(
            f"delete-group-{group.lower().replace('_', '-')}",
            "asset_delete_group",
            {"exclude_asset_groups": [group]},
        )
        for group in asset_groups
    )
    return (*security_rows, *group_rows)


def cost_execution_scenarios() -> tuple[dict[str, object], ...]:
    values = (
        ("cost-double-commission", 2.0, 0.0005, 0),
        ("cost-high-slippage", 1.0, 0.0010, 0),
        ("cost-double-high", 2.0, 0.0010, 0),
        ("execution-delay-one-day", 1.0, 0.0005, 1),
        ("execution-delay-double-high", 2.0, 0.0010, 1),
    )
    return tuple(
        _scenario(
            scenario_id,
            "cost_execution",
            {
                "costs": {
                    "commission_multiplier": commission,
                    "one_way_slippage": slippage,
                },
                "execution": {"additional_delay_days": delay},
            },
        )
        for scenario_id, commission, slippage, delay in values
    )


def _deep_merge(
    base: Mapping[str, object],
    overrides: Mapping[str, object],
) -> dict[str, object]:
    merged = copy.deepcopy(dict(base))
    for key, value in overrides.items():
        if isinstance(value, Mapping) and isinstance(merged.get(key), Mapping):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def _scenario_metrics(value: object) -> dict[str, float | int | None]:
    if isinstance(value, AnalysisBundle):
        return {
            key: item
            for key, item in calculate_performance(value).items()
            if item is None or isinstance(item, (int, float))
        }
    if isinstance(value, Mapping):
        metrics = dict(value)
        if all(item is None or isinstance(item, (int, float)) for item in metrics.values()):
            return metrics
    raise TypeError("scenario runner must return an AnalysisBundle or numeric metrics")


def _evaluate(
    metrics: Mapping[str, float | int | None],
    thresholds: Mapping[str, object],
) -> tuple[str, tuple[str, ...]]:
    reasons: list[str] = []
    missing: list[str] = []
    for threshold, raw_limit in thresholds.items():
        limit = float(raw_limit)
        metric_name = {
            "cagr_min_exclusive": "cagr",
            "max_drawdown_abs_max": "max_drawdown",
            "calmar_min": "calmar",
        }.get(str(threshold))
        if metric_name is None:
            raise ValueError(f"unsupported scenario threshold: {threshold}")
        value = metrics.get(metric_name)
        if value is None:
            missing.append(str(threshold))
            continue
        numeric = float(value)
        passed = {
            "cagr_min_exclusive": numeric > limit,
            "max_drawdown_abs_max": abs(numeric) <= limit,
            "calmar_min": numeric >= limit,
        }[str(threshold)]
        if not passed:
            reasons.append(str(threshold))
    if missing:
        return "evidence_insufficient", tuple(f"missing_{item}" for item in missing)
    return ("fail", tuple(reasons)) if reasons else ("pass", ())


def run_path_scenarios(
    base_config: Mapping[str, object],
    scenario_configs: Sequence[Mapping[str, object]],
    run_turtle: Callable[[dict[str, object]], object],
) -> tuple[ScenarioResult, ...]:
    results: list[ScenarioResult] = []
    seen: set[str] = set()
    for scenario in scenario_configs:
        scenario_id = str(scenario["scenario_id"])
        if scenario_id in seen:
            raise ValueError(f"duplicate scenario_id: {scenario_id}")
        seen.add(scenario_id)
        dimension = str(scenario["dimension"])
        overrides = scenario.get("overrides", {})
        thresholds = scenario.get("thresholds", _PATH_THRESHOLDS)
        if not isinstance(overrides, Mapping) or not isinstance(thresholds, Mapping):
            raise ValueError("scenario overrides and thresholds must be mappings")
        config = _deep_merge(base_config, overrides)
        config["scenario_id"] = scenario_id
        input_sha256 = evidence_digest(
            {"config": config, "scenario": dict(scenario)}
        )
        try:
            metrics = _scenario_metrics(run_turtle(config))
        except Exception as exc:
            results.append(
                ScenarioResult(
                    scenario_id=scenario_id,
                    dimension=dimension,
                    status="evidence_insufficient",
                    metrics={},
                    input_sha256=input_sha256,
                    reasons=(f"scenario_execution_failed:{type(exc).__name__}",),
                )
            )
            continue
        status, reasons = _evaluate(metrics, thresholds)
        results.append(
            ScenarioResult(
                scenario_id=scenario_id,
                dimension=dimension,
                status=status,
                metrics=metrics,
                input_sha256=input_sha256,
                reasons=reasons,
            )
        )
    return tuple(results)


def block_bootstrap(
    returns: np.ndarray,
    block_size: int,
    paths: int,
    horizon: int,
    seed: int,
) -> np.ndarray:
    values = np.asarray(returns, dtype=np.float64)
    if values.ndim != 1 or values.size == 0 or not np.isfinite(values).all():
        raise ValueError("returns must be a finite one-dimensional array")
    if np.any(values <= -1.0):
        raise ValueError("returns must be greater than -100%")
    if block_size <= 0 or paths <= 0 or horizon <= 0:
        raise ValueError("block_size, paths and horizon must be positive")
    output = np.empty((paths, horizon), dtype=np.float64)
    generator = np.random.default_rng(seed)
    blocks = math.ceil(horizon / block_size)
    offsets = np.arange(block_size, dtype=np.int64)
    batch_size = min(256, paths)
    for first in range(0, paths, batch_size):
        count = min(batch_size, paths - first)
        starts = generator.integers(0, values.size, size=(count, blocks))
        indices = (starts[:, :, None] + offsets) % values.size
        output[first : first + count] = values[indices].reshape(count, -1)[:, :horizon]
    return output


def summarize_bootstrap(paths: np.ndarray) -> dict[str, float]:
    values = np.asarray(paths, dtype=np.float64)
    if values.ndim != 2 or values.shape[0] == 0 or values.shape[1] == 0:
        raise ValueError("bootstrap paths must be a non-empty matrix")
    wealth = np.cumprod(1.0 + values, axis=1)
    peaks = np.maximum(1.0, np.maximum.accumulate(wealth, axis=1))
    max_drawdown = np.min(wealth / peaks - 1.0, axis=1)
    terminal = wealth[:, -1] - 1.0
    return {
        "probability_drawdown_over_20pct": float(np.mean(max_drawdown < -0.20)),
        "probability_drawdown_over_30pct": float(np.mean(max_drawdown < -0.30)),
        "median_terminal_return": float(np.median(terminal)),
    }


def calculate_bootstrap_scenarios(
    returns: np.ndarray,
    *,
    paths: int = BOOTSTRAP_PATHS,
    horizon: int = BOOTSTRAP_HORIZON,
    seed: int = BOOTSTRAP_SEED,
) -> tuple[ScenarioResult, ...]:
    values = np.asarray(returns, dtype=np.float64)
    results: list[ScenarioResult] = []
    for block_size in BOOTSTRAP_BLOCK_SIZES:
        scenario_id = f"bootstrap-block-{block_size}"
        summary = summarize_bootstrap(
            block_bootstrap(
                values,
                block_size=block_size,
                paths=paths,
                horizon=horizon,
                seed=seed,
            )
        )
        reasons: list[str] = []
        if summary["probability_drawdown_over_20pct"] > 0.05:
            reasons.append("probability_drawdown_over_20pct")
        if summary["probability_drawdown_over_30pct"] > 0.01:
            reasons.append("probability_drawdown_over_30pct")
        if summary["median_terminal_return"] <= 0:
            reasons.append("median_terminal_return")
        results.append(
            ScenarioResult(
                scenario_id=scenario_id,
                dimension="block_bootstrap",
                status="fail" if reasons else "pass",
                metrics={
                    **summary,
                    "block_size": block_size,
                    "paths": paths,
                    "horizon": horizon,
                    "seed": seed,
                },
                input_sha256=evidence_digest(
                    {
                        "returns": values.tolist(),
                        "block_size": block_size,
                        "paths": paths,
                        "horizon": horizon,
                        "seed": seed,
                    }
                ),
                reasons=tuple(reasons),
            )
        )
    return tuple(results)
