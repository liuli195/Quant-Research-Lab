from __future__ import annotations

from collections import defaultdict
from typing import Iterable, Mapping, Sequence

from .contracts import AnalysisBundle
from .evidence import ScenarioResult, evidence_digest


HISTORICAL_WINDOWS = (
    {
        "scenario_id": "history-2015-a-share-volatility",
        "start_date": "2015-06-12",
        "end_date": "2015-09-30",
        "max_drawdown_abs_max": 0.15,
    },
    {
        "scenario_id": "history-2018-global-risk",
        "start_date": "2018-01-01",
        "end_date": "2018-12-31",
        "max_drawdown_abs_max": 0.15,
    },
    {
        "scenario_id": "history-2020-pandemic",
        "start_date": "2020-02-03",
        "end_date": "2020-04-30",
        "max_drawdown_abs_max": 0.15,
    },
    {
        "scenario_id": "history-2022-rate-hikes",
        "start_date": "2022-01-04",
        "end_date": "2022-10-31",
        "max_drawdown_abs_max": 0.15,
    },
    {
        "scenario_id": "history-2024-liquidity",
        "start_date": "2024-01-02",
        "end_date": "2024-02-08",
        "max_drawdown_abs_max": 0.15,
    },
)

_EQUITY_GROUPS = {
    "china_sync_equity",
    "cross_border_tech_equity",
    "china_dividend",
    "china_innovative_drug",
}
POSITION_SHOCKS = (
    {
        "scenario_id": "shock-synchronous-equity-crash",
        "asset_group_shocks": {
            **{group: -0.20 for group in _EQUITY_GROUPS},
            "gold": -0.05,
            "treasury_bond": 0.0,
        },
        "maximum_loss_abs_max": 0.15,
    },
    {
        "scenario_id": "shock-market-liquidity",
        "asset_group_shocks": {
            **{group: -0.15 for group in _EQUITY_GROUPS},
            "gold": -0.10,
            "treasury_bond": -0.08,
        },
        "maximum_loss_abs_max": 0.15,
    },
    {
        "scenario_id": "shock-cross-border-gap",
        "asset_group_shocks": {
            **{group: -0.08 for group in _EQUITY_GROUPS},
            "gold": 0.0,
            "treasury_bond": 0.0,
        },
        "security_shocks": {
            "513100.XSHG": -0.25,
            "513180.XSHG": -0.25,
        },
        "maximum_loss_abs_max": 0.15,
    },
    {
        "scenario_id": "shock-stop-failure",
        "use_stop_failure_loss": True,
        "maximum_loss_abs_max": 0.15,
    },
)


def _max_drawdown(
    equities: Sequence[float],
) -> tuple[float, float, int, int | None]:
    peak = equities[0]
    maximum = 0.0
    minimum_equity = equities[0]
    duration = 0
    current_duration = 0
    trough_index = 0
    recovery_peak = peak
    for index, value in enumerate(equities):
        minimum_equity = min(minimum_equity, value)
        if value >= peak:
            peak = value
            current_duration = 0
        else:
            current_duration += 1
            duration = max(duration, current_duration)
        drawdown = value / peak - 1.0
        if drawdown < maximum:
            maximum = drawdown
            trough_index = index
            recovery_peak = peak
    recovery_duration = next(
        (
            index - trough_index
            for index, value in enumerate(equities[trough_index + 1 :], trough_index + 1)
            if value >= recovery_peak
        ),
        None,
    )
    if maximum == 0.0:
        recovery_duration = 0
    return maximum, minimum_equity, duration, recovery_duration


def calculate_historical_stress(
    bundle: AnalysisBundle,
    windows: Sequence[Mapping[str, object]] = HISTORICAL_WINDOWS,
) -> tuple[ScenarioResult, ...]:
    equity_rows = sorted(bundle.rows("equity"), key=lambda row: str(row["date"]))
    first_date = str(equity_rows[0]["date"])
    last_date = str(equity_rows[-1]["date"])
    results: list[ScenarioResult] = []
    for window in windows:
        scenario_id = str(window["scenario_id"])
        start = str(window["start_date"])
        end = str(window["end_date"])
        threshold = float(window.get("max_drawdown_abs_max", 0.15))
        input_sha256 = evidence_digest(
            {"bundle": bundle.digest, "window": dict(window)}
        )
        selected = [
            float(row["equity"])
            for row in equity_rows
            if start <= str(row["date"]) <= end
        ]
        if first_date > start or last_date < end or len(selected) < 2:
            results.append(
                ScenarioResult(
                    scenario_id=scenario_id,
                    dimension="historical_stress",
                    status="evidence_insufficient",
                    metrics={"samples": len(selected)},
                    input_sha256=input_sha256,
                    reasons=("missing_window",),
                )
            )
            continue
        drawdown, minimum_equity, duration, recovery_duration = _max_drawdown(selected)
        window_events = [
            row
            for row in bundle.rows("events")
            if start <= str(row["date"]) <= end
        ]
        passed = abs(drawdown) <= threshold
        results.append(
            ScenarioResult(
                scenario_id=scenario_id,
                dimension="historical_stress",
                status="pass" if passed else "fail",
                metrics={
                    "max_drawdown": drawdown,
                    "minimum_equity": minimum_equity,
                    "drawdown_duration": duration,
                    "recovery_duration": recovery_duration,
                    "unfilled_events": sum(
                        row["status"] != "filled" for row in window_events
                    ),
                    "risk_rule_events": sum(
                        "risk" in str(row["reason"])
                        or "volatility" in str(row["reason"])
                        for row in window_events
                    ),
                    "threshold": threshold,
                    "samples": len(selected),
                },
                input_sha256=input_sha256,
                reasons=() if passed else ("max_drawdown_abs_max",),
            )
        )
    return tuple(results)


def _date_equity(rows: Sequence[Mapping[str, object]]) -> float | None:
    implied = [
        float(row["market_value"]) / float(row["weight"])
        for row in rows
        if float(row["weight"]) > 0
    ]
    if not implied:
        return None
    reference = implied[0]
    if any(abs(value - reference) > max(1.0, abs(reference)) * 1e-8 for value in implied):
        return None
    return reference


def calculate_position_shocks(
    positions: Iterable[Mapping[str, object]],
    shocks: Sequence[Mapping[str, object]],
) -> tuple[ScenarioResult, ...]:
    by_date: defaultdict[str, list[Mapping[str, object]]] = defaultdict(list)
    materialized = [dict(row) for row in positions]
    for row in materialized:
        by_date[str(row["date"])].append(row)
    results: list[ScenarioResult] = []
    for shock in shocks:
        scenario_id = str(shock["scenario_id"])
        threshold = float(shock.get("maximum_loss_abs_max", 0.15))
        group_shocks = shock.get("asset_group_shocks", {})
        security_shocks = shock.get("security_shocks", {})
        if not isinstance(group_shocks, Mapping) or not isinstance(
            security_shocks, Mapping
        ):
            raise ValueError("position shock mappings are invalid")
        losses: list[float] = []
        missing = False
        for rows in by_date.values():
            if shock.get("use_stop_failure_loss") is True:
                equity = _date_equity(rows)
                if equity is None or any("stop_failure_loss" not in row for row in rows):
                    missing = True
                    continue
                losses.append(
                    sum(float(row["stop_failure_loss"]) for row in rows) / equity
                )
            else:
                account_return = 0.0
                for row in rows:
                    security = str(row["security"])
                    group = str(row["asset_group"])
                    shock_return = security_shocks.get(
                        security,
                        group_shocks.get(group),
                    )
                    if shock_return is None:
                        missing = True
                        continue
                    account_return += float(row["weight"]) * float(shock_return)
                losses.append(max(0.0, -account_return))
        input_sha256 = evidence_digest(
            {"positions": materialized, "shock": dict(shock)}
        )
        if missing or not losses:
            results.append(
                ScenarioResult(
                    scenario_id=scenario_id,
                    dimension="position_shock",
                    status="evidence_insufficient",
                    metrics={"evaluated_dates": len(losses)},
                    input_sha256=input_sha256,
                    reasons=("missing_position_shock_input",),
                )
            )
            continue
        worst = max(losses)
        passed = worst <= threshold + 1e-12
        results.append(
            ScenarioResult(
                scenario_id=scenario_id,
                dimension="position_shock",
                status="pass" if passed else "fail",
                metrics={
                    "worst_account_loss": worst,
                    "threshold": threshold,
                    "evaluated_dates": len(losses),
                },
                input_sha256=input_sha256,
                reasons=() if passed else ("maximum_loss_abs_max",),
            )
        )
    return tuple(results)
