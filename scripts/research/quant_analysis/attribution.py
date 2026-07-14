from __future__ import annotations

from collections import defaultdict
from typing import Iterable, Mapping

from .contracts import AnalysisBundle


_DIMENSIONS = (
    "security",
    "asset_group",
    "period",
    "trading_reason",
    "exposure",
    "cash",
    "trend_filter",
    "risk_constraint",
)
_TOLERANCE = 1e-12


def _group_contributions(
    rows: Iterable[Mapping[str, object]],
    key: str,
) -> dict[str, float]:
    grouped: defaultdict[str, float] = defaultdict(float)
    for row in rows:
        grouped[str(row[key])] += float(row["return_contribution"])
    return dict(sorted(grouped.items()))


def _reconciled_rows(
    dimension: str,
    values: Mapping[str, float],
    target: float,
) -> list[dict[str, object]]:
    completed = dict(values)
    residual = target - sum(completed.values())
    if abs(residual) > _TOLERANCE or not completed:
        completed["unattributed"] = residual
    error = sum(completed.values()) - target
    return [
        {
            "dimension": dimension,
            "key": key,
            "contribution": value,
            "portfolio_return": target,
            "reconciliation_error": error,
        }
        for key, value in sorted(completed.items())
    ]


def calculate_attribution(bundle: AnalysisBundle) -> tuple[dict[str, object], ...]:
    target = sum(float(row["return"]) for row in bundle.rows("returns"))
    positions = list(bundle.rows("positions"))
    values_by_dimension: dict[str, dict[str, float]] = {
        "security": _group_contributions(positions, "security"),
        "asset_group": _group_contributions(positions, "asset_group"),
        "period": {},
        "trading_reason": {},
        "exposure": {
            "invested": sum(float(row["return_contribution"]) for row in positions),
            "cash": sum(
                float(row["cash_return_contribution"])
                for row in bundle.rows("returns")
            ),
        },
        "cash": {
            "cash": sum(
                float(row["cash_return_contribution"])
                for row in bundle.rows("returns")
            )
        },
        "trend_filter": {},
        "risk_constraint": {},
    }
    period: defaultdict[str, float] = defaultdict(float)
    for row in positions:
        period[str(row["date"])[:7]] += float(row["return_contribution"])
    values_by_dimension["period"] = dict(period)

    initial_equity = float(bundle.rows("equity")[0]["equity"])
    trading_reason: defaultdict[str, float] = defaultdict(float)
    for row in bundle.rows("trades"):
        trading_reason[str(row["exit_reason"])] += float(row["pnl"]) / initial_equity
    values_by_dimension["trading_reason"] = dict(trading_reason)

    trend_filter: defaultdict[str, float] = defaultdict(float)
    risk_constraint: defaultdict[str, float] = defaultdict(float)
    for row in bundle.rows("events"):
        reason = str(row["reason"])
        if "trend" in reason or "breakout" in reason:
            trend_filter[reason] += 0.0
        if "risk" in reason or "volatility" in reason:
            risk_constraint[reason] += 0.0
    values_by_dimension["trend_filter"] = dict(trend_filter)
    values_by_dimension["risk_constraint"] = dict(risk_constraint)

    output: list[dict[str, object]] = []
    for dimension in _DIMENSIONS:
        output.extend(
            _reconciled_rows(dimension, values_by_dimension[dimension], target)
        )
    return tuple(output)
