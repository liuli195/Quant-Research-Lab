from __future__ import annotations

from collections import defaultdict
from typing import Iterable, Mapping

from .contracts import AnalysisBundle, AnalysisContractError


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
    error = sum(completed.values()) - target
    if not completed or abs(error) > _TOLERANCE:
        raise AnalysisContractError(
            f"{dimension} attribution does not reconcile to portfolio return"
        )
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
    cash_by_date = {
        str(row["date"]): float(row["cash_return_contribution"])
        for row in bundle.rows("returns")
    }
    cash_total = sum(cash_by_date.values())
    invested_total = sum(float(row["return_contribution"]) for row in positions)
    security = _group_contributions(positions, "security")
    security["cash"] = cash_total
    asset_group = _group_contributions(positions, "asset_group")
    asset_group["cash"] = cash_total
    values_by_dimension: dict[str, dict[str, float]] = {
        "security": security,
        "asset_group": asset_group,
        "period": {},
        "trading_reason": {},
        "exposure": {
            "invested": invested_total,
            "cash": cash_total,
        },
        "cash": {
            "invested_assets": invested_total,
            "cash": cash_total,
        },
        "trend_filter": {
            "breakout_path": invested_total,
            "cash": cash_total,
        },
        "risk_constraint": {
            "risk_budgeted_path": invested_total,
            "cash": cash_total,
        },
    }
    period: defaultdict[str, float] = defaultdict(float)
    for row in positions:
        period[str(row["date"])[:7]] += float(row["return_contribution"])
    for current_date, contribution in cash_by_date.items():
        period[current_date[:7]] += contribution
    values_by_dimension["period"] = dict(period)

    trading_reason: defaultdict[str, float] = defaultdict(float)
    for row in positions:
        trading_reason[str(row["attribution_reason"])] += float(
            row["return_contribution"]
        )
    trading_reason["cash"] += cash_total
    values_by_dimension["trading_reason"] = dict(trading_reason)

    output: list[dict[str, object]] = []
    for dimension in _DIMENSIONS:
        output.extend(
            _reconciled_rows(dimension, values_by_dimension[dimension], target)
        )
    return tuple(output)
