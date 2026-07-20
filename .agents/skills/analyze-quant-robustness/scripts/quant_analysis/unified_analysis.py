from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np
import pandas as pd
import pyarrow.parquet as pq

from scripts.research.analysis_data.views import open_analysis_database
from scripts.research.market_data.benchmark_sets import (
    BenchmarkSet,
    BenchmarkSetError,
    open_benchmark_set,
)

from .analysis_plan import expand_analysis_plan
from .benchmarks import calculate_benchmark_statistics
from .cvar import calculate_cvar, rolling_compound_returns
from .evidence import ScenarioResult, build_evidence_matrix, evidence_digest
from .robustness import block_bootstrap, summarize_bootstrap
from .package_source import PackageSource, PackageSourceError, open_package_sources


BENCHMARK_IDS = (
    "CSI300_CNY_TOTAL_RETURN",
    "NASDAQ100_CNY_TOTAL_RETURN",
)
_FORMULA_VERSION = "standard-strategy-analysis/1"
_SCRIPT_VERSION = "analyze-quant-robustness/1"
_SCRIPT_ENTRY = (
    ".agents/skills/analyze-quant-robustness/scripts/analyze_quant_robustness.py"
)


class UnifiedAnalysisError(ValueError):
    """Raised when standard result packages cannot support deterministic analysis."""


@dataclass(frozen=True)
class ScenarioInput:
    scenario_id: str
    returns: pd.Series
    balances: pd.DataFrame
    positions: pd.DataFrame
    orders: pd.DataFrame
    events: pd.DataFrame
    params: Mapping[str, object]
    capabilities: Mapping[str, Mapping[str, object]] = field(default_factory=dict)
    attribution_status: str = "available"


def _atomic_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
    finally:
        temporary = Path(temporary_name)
        if temporary.exists():
            temporary.unlink()


def _unavailable_cost_sensitivity(
    definitions: Sequence[Mapping[str, object]], reason: str
) -> tuple[list[dict[str, object]], list[ScenarioResult]]:
    pairs = [
        _unavailable_result(f"cost-{definition['id']}", "cost_execution", reason)
        for definition in definitions
    ]
    return [row for row, _ in pairs], [result for _, result in pairs]


def _unavailable_deletion_sensitivity(
    universe: Mapping[str, str], reason: str
) -> tuple[list[dict[str, object]], list[ScenarioResult]]:
    pairs = [
        _unavailable_result(
            f"delete-security-{security.lower().replace('.', '-').replace('_', '-')}",
            "asset_delete_security",
            reason,
        )
        for security in sorted(universe)
    ]
    pairs.extend(
        _unavailable_result(
            f"delete-asset-group-{group.lower().replace('.', '-').replace('_', '-')}",
            "asset_delete_group",
            reason,
        )
        for group in sorted(set(universe.values()))
    )
    return [row for row, _ in pairs], [result for _, result in pairs]
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
    finally:
        temporary = Path(temporary_name)
        if temporary.exists():
            temporary.unlink()


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load_json(path: Path, *, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise UnifiedAnalysisError(f"cannot read {label}: {path}") from exc
    if not isinstance(value, dict):
        raise UnifiedAnalysisError(f"{label} must be a JSON object")
    return value


def _safe_number(value: object) -> float | int | None:
    if value is None:
        return None
    if isinstance(value, (np.integer, int)):
        return int(value)
    numeric = float(value)
    return numeric if math.isfinite(numeric) else None


def _numeric_metrics(values: Mapping[str, object]) -> dict[str, float | int | None]:
    return {str(key): _safe_number(value) for key, value in values.items()}


def calculate_return_metrics(returns: pd.Series, *, annualization: int = 252) -> dict[str, float | int | None]:
    values = np.asarray(returns, dtype=np.float64)
    if annualization <= 0 or values.ndim != 1 or values.size == 0:
        raise UnifiedAnalysisError("return series must be non-empty")
    if not np.isfinite(values).all() or np.any(values <= -1.0):
        raise UnifiedAnalysisError("return series contains invalid values")
    wealth = np.cumprod(1.0 + values)
    cumulative_return = float(wealth[-1] - 1.0)
    cagr = float((1.0 + cumulative_return) ** (annualization / values.size) - 1.0)
    volatility = float(np.std(values, ddof=1) * math.sqrt(annualization)) if values.size > 1 else 0.0
    mean_annual = float(np.mean(values) * annualization)
    downside_values = np.minimum(values, 0.0)
    downside = float(math.sqrt(float(np.mean(downside_values**2))) * math.sqrt(annualization))
    peaks = np.maximum(1.0, np.maximum.accumulate(wealth))
    drawdowns = wealth / peaks - 1.0
    maximum_drawdown = float(np.min(drawdowns))
    underwater = drawdowns < 0.0
    maximum_duration = 0
    current_duration = 0
    for value in underwater:
        current_duration = current_duration + 1 if value else 0
        maximum_duration = max(maximum_duration, current_duration)
    return {
        "observations": int(values.size),
        "cumulative_return": cumulative_return,
        "cagr": cagr,
        "annualized_volatility": volatility,
        "sharpe": None if volatility == 0.0 else mean_annual / volatility,
        "sortino": None if downside == 0.0 else mean_annual / downside,
        "max_drawdown": maximum_drawdown,
        "max_drawdown_duration": maximum_duration,
        "calmar": None if maximum_drawdown == 0.0 else cagr / abs(maximum_drawdown),
        "daily_hit_rate": float(np.mean(values[values != 0.0] > 0.0)) if np.any(values != 0.0) else None,
    }


def evaluate_metrics(
    metrics: Mapping[str, object], thresholds: Mapping[str, object]
) -> tuple[str, list[str]]:
    checks = (
        ("cagr_min_exclusive", "cagr", lambda value, limit: value > limit),
        (
            "max_drawdown_abs_max",
            "max_drawdown",
            lambda value, limit: abs(value) <= limit,
        ),
        ("calmar_min", "calmar", lambda value, limit: value >= limit),
    )
    reasons: list[str] = []
    for threshold_name, metric_name, predicate in checks:
        value = metrics.get(metric_name)
        if value is None or not predicate(float(value), float(thresholds[threshold_name])):
            reasons.append(threshold_name)
    return ("pass" if not reasons else "fail", reasons)


def align_three_way_benchmarks(
    strategy: pd.Series,
    benchmarks: Mapping[str, pd.Series],
) -> tuple[pd.DataFrame, dict[str, object]]:
    if tuple(benchmarks) != BENCHMARK_IDS:
        raise UnifiedAnalysisError("benchmark set must contain the two canonical benchmarks")
    common = set(pd.DatetimeIndex(strategy.index).normalize())
    for benchmark_id in BENCHMARK_IDS:
        common &= set(pd.DatetimeIndex(benchmarks[benchmark_id].index).normalize())
    common_index = pd.DatetimeIndex(sorted(common))
    if common_index.empty:
        raise UnifiedAnalysisError("strategy and benchmarks have no shared dates")
    aligned = pd.DataFrame(index=common_index)
    aligned["strategy"] = strategy.groupby(pd.DatetimeIndex(strategy.index).normalize()).last().reindex(common_index)
    for benchmark_id in BENCHMARK_IDS:
        normalized = benchmarks[benchmark_id].groupby(
            pd.DatetimeIndex(benchmarks[benchmark_id].index).normalize()
        ).last()
        aligned[benchmark_id] = normalized.reindex(common_index)
    if aligned.isna().any().any():
        raise UnifiedAnalysisError("benchmark alignment produced missing values")
    return aligned, {
        "alignment": "three-way exact-date inner join",
        "common_samples": len(aligned),
        "strategy_samples": len(strategy),
        "strategy_excluded_dates": len(strategy) - len(aligned),
        "benchmark_samples": {
            benchmark_id: len(benchmarks[benchmark_id]) for benchmark_id in BENCHMARK_IDS
        },
        "benchmark_excluded_dates": {
            benchmark_id: len(benchmarks[benchmark_id]) - len(aligned)
            for benchmark_id in BENCHMARK_IDS
        },
    }


def _load_common_facts(
    result_dir: Path, *, snapshot_id: str | None = None
) -> tuple[pd.Series, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    with open_analysis_database(result_dir, snapshot_id=snapshot_id) as database:
        returns_frame = database.connection.sql(
            "select trading_date, daily_returns from strategy_daily_returns "
            "where comparable order by trading_date"
        ).fetchdf()
        balances = database.connection.sql(
            "select time, total_value, net_value, cash, aval_cash from balances order by time"
        ).fetchdf()
        positions = database.connection.sql("select * from positions order by time, security").fetchdf()
        orders = database.connection.sql("select * from orders order by time, security").fetchdf()
    returns = pd.Series(
        returns_frame["daily_returns"].astype(float).to_numpy(),
        index=pd.to_datetime(returns_frame["trading_date"]).dt.normalize(),
        name="return",
    )
    for frame in (balances, positions, orders):
        if "time" in frame:
            frame["date"] = pd.to_datetime(frame["time"]).dt.normalize()
    return returns, balances, positions, orders


def _package_attribution_events(package: PackageSource) -> pd.DataFrame:
    capability = package.capabilities["attribution"]
    if capability.get("status") != "available":
        return pd.DataFrame()
    path = (package.root / str(capability["path"])).resolve()
    if not path.is_relative_to(package.root) or _sha256(path) != capability["sha256"]:
        raise UnifiedAnalysisError("result package attribution evidence changed")
    detail_fields = [str(field) for field in capability.get("detail_fields", [])]
    columns = ["time", "event_id", "event_type", *detail_fields]
    frame = pq.read_table(path, columns=list(dict.fromkeys(columns))).to_pandas()
    event_time = pd.to_datetime(frame["time"], errors="coerce", format="mixed")
    if event_time.isna().any():
        raise UnifiedAnalysisError("result package attribution evidence has invalid event times")
    events = frame.copy()
    events["event_time"] = event_time
    events["date"] = event_time.dt.normalize()
    events["event_type"] = events["event_type"].fillna("").astype(str)
    for field in ("reason_code", "security"):
        if field not in events:
            events[field] = ""
        else:
            events[field] = events[field].fillna("").astype(str)
    if events["event_type"].eq("").any():
        raise UnifiedAnalysisError("result package attribution evidence has invalid event identities")
    return events


def load_package_scenario(package: PackageSource) -> ScenarioInput:
    returns, balances, positions, orders = _load_common_facts(package.root)
    attribution_status = str(package.capabilities["attribution"]["status"])
    return ScenarioInput(
        scenario_id=package.scenario_id,
        returns=returns,
        balances=balances,
        positions=positions,
        orders=orders,
        events=_package_attribution_events(package),
        params=dict(package.params),
        capabilities=package.capabilities,
        attribution_status=attribution_status,
    )


def _immutable_json(path: Path, value: object) -> None:
    if path.exists():
        existing = _load_json(path, label=path.name)
        if existing != value:
            raise UnifiedAnalysisError(f"immutable analysis identity collision: {path}")
        return
    _atomic_json(path, value)


def _valuation_facts(scenario: ScenarioInput) -> pd.DataFrame:
    columns = [
        "date",
        "security",
        "reason_code",
        "source_reason",
        "security_daily_pnl",
        "common_stop_after",
        "stop_failure_loss",
    ]
    if scenario.events.empty:
        return pd.DataFrame(columns=columns)
    if "event_type" not in scenario.events or "details_json" not in scenario.events:
        return pd.DataFrame(columns=columns)
    events = scenario.events.loc[
        scenario.events["event_type"] == "valuation"
    ].copy()
    if events.empty:
        return pd.DataFrame(columns=columns)
    if "date" not in events:
        time_column = "event_time" if "event_time" in events else "time"
        events["date"] = pd.to_datetime(events[time_column]).dt.normalize()
    records: list[dict[str, object]] = []
    for event in events.to_dict("records"):
        try:
            details = json.loads(str(event["details_json"]))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise UnifiedAnalysisError("valuation details_json is invalid") from exc
        if not isinstance(details, Mapping):
            raise UnifiedAnalysisError("valuation details_json must be an object")
        try:
            security_pnl = float(details["security_daily_pnl"])
        except (KeyError, TypeError, ValueError) as exc:
            raise UnifiedAnalysisError(
                "valuation event has no security_daily_pnl"
            ) from exc
        if not np.isfinite(security_pnl):
            raise UnifiedAnalysisError("valuation security_daily_pnl is invalid")
        records.append(
            {
                "date": pd.Timestamp(event["date"]).normalize(),
                "security": str(event["security"]),
                "reason_code": str(event["reason_code"]),
                "source_reason": str(
                    details.get("source_reason", event["reason_code"])
                ),
                "security_daily_pnl": security_pnl,
                "common_stop_after": _safe_number(
                    details.get("common_stop_after")
                ),
                "stop_failure_loss": _safe_number(
                    details.get("stop_failure_loss")
                ),
            }
        )
    facts = pd.DataFrame.from_records(records, columns=columns)
    if facts.duplicated(["date", "security"]).any():
        raise UnifiedAnalysisError(
            "valuation facts contain duplicate security dates"
        )
    return facts.sort_values(["date", "security"]).reset_index(drop=True)


def _position_facts(
    scenario: ScenarioInput, universe: Mapping[str, str]
) -> pd.DataFrame:
    positions = scenario.positions.copy()
    if positions.empty:
        return positions
    positions["asset_group"] = positions["security"].map(universe)
    if positions["asset_group"].isna().any():
        raise UnifiedAnalysisError("position security is absent from analysis universe")
    equity = scenario.balances.set_index("date")["total_value"].astype(float)
    positions["equity"] = positions["date"].map(equity)
    positions["market_value"] = positions["amount"].astype(float) * positions["price"].astype(float)
    positions["weight"] = positions["market_value"] / positions["equity"]
    valuations = _valuation_facts(scenario).rename(
        columns={
            "source_reason": "attribution_reason",
            "common_stop_after": "common_stop",
        }
    )
    if valuations.empty:
        positions["attribution_reason"] = "unclassified"
        positions["common_stop"] = np.nan
        positions["stop_failure_loss"] = np.nan
        return positions
    annotated = positions.merge(
        valuations[
            [
                "date",
                "security",
                "attribution_reason",
                "common_stop",
                "stop_failure_loss",
            ]
        ],
        on=["date", "security"],
        how="left",
        validate="one_to_one",
    )
    annotated["attribution_reason"] = annotated["attribution_reason"].fillna(
        "unclassified"
    )
    return annotated


def _security_pnl_facts(
    scenario: ScenarioInput, universe: Mapping[str, str]
) -> pd.DataFrame:
    facts = _valuation_facts(scenario)
    if facts.empty:
        return pd.DataFrame(
            columns=[
                *facts.columns,
                "asset_group",
                "previous_equity",
                "return_contribution",
                "attribution_reason",
            ]
        )
    facts["asset_group"] = facts["security"].map(universe)
    if facts["asset_group"].isna().any():
        raise UnifiedAnalysisError(
            "valuation security is absent from analysis universe"
        )
    balances = scenario.balances.set_index("date")["total_value"].astype(float)
    previous_equity = balances.shift(1)
    first_date = balances.index.min()
    first_day_pnl = float(
        facts.loc[facts["date"] == first_date, "security_daily_pnl"].sum()
    )
    previous_equity.loc[first_date] = float(balances.loc[first_date]) - first_day_pnl
    facts["previous_equity"] = facts["date"].map(previous_equity)
    if facts["previous_equity"].isna().any() or (
        facts["previous_equity"] <= 0.0
    ).any():
        raise UnifiedAnalysisError(
            "valuation facts cannot resolve previous equity"
        )
    facts["return_contribution"] = (
        facts["security_daily_pnl"].astype(float)
        / facts["previous_equity"].astype(float)
    )
    facts["attribution_reason"] = facts["source_reason"]
    return facts


def _risk_metrics(scenario: ScenarioInput, positions: pd.DataFrame) -> dict[str, object]:
    balances = scenario.balances.copy()
    balances["invested_ratio"] = (
        (balances["total_value"].astype(float) - balances["cash"].astype(float))
        / balances["total_value"].astype(float)
    )
    risk = scenario.params.get("risk", {})
    if not isinstance(risk, Mapping):
        raise UnifiedAnalysisError("scenario risk config is invalid")
    if positions.empty:
        max_security_weight = max_group_weight = 0.0
        planned_coverage = 0.0
        max_planned_loss_ratio = None
    else:
        max_security_weight = float(positions["weight"].max())
        group_weights = positions.groupby(["date", "asset_group"])["weight"].sum()
        max_group_weight = float(group_weights.max())
        planned = positions.loc[positions["common_stop"].notna()].copy()
        planned_coverage = float(len(planned) / len(positions))
        if planned.empty:
            max_planned_loss_ratio = None
        else:
            planned["planned_loss"] = (
                (
                    planned["avg_cost"].astype(float)
                    - planned["common_stop"].astype(float)
                )
                .clip(lower=0.0)
                * planned["amount"].astype(float)
            )
            daily_risk = planned.groupby("date")["planned_loss"].sum()
            equity = balances.set_index("date")["total_value"].astype(float)
            loss_ratio = daily_risk / equity.reindex(daily_risk.index)
            max_planned_loss_ratio = float(loss_ratio.max())
    rolling_vol = scenario.returns.rolling(60, min_periods=60).std(ddof=1) * math.sqrt(252)
    filled_orders = scenario.orders.loc[
        (scenario.orders["status"] == "done") & (scenario.orders["filled"].astype(float) > 0)
    ]
    closed = filled_orders.loc[filled_orders["action"] == "close"]
    average_equity = float(balances["total_value"].mean())
    filled_notional = float(
        (filled_orders["filled"].astype(float) * filled_orders["price"].astype(float)).sum()
    )
    decision_events = (
        scenario.events.loc[scenario.events["event_type"] == "decision"]
        if "event_type" in scenario.events
        else scenario.events.iloc[0:0]
    )
    event_reason_codes = (
        decision_events["reason_code"]
        if "reason_code" in decision_events
        else pd.Series(dtype="object")
    )
    maximum_effective_risk_units: float | None = None
    maximum_portfolio_unit_utilization: float | None = None
    if not decision_events.empty and "details_json" in decision_events:
        for raw_details in decision_events["details_json"]:
            try:
                details = json.loads(str(raw_details))
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            if not isinstance(details, Mapping):
                continue
            effective = _safe_number(details.get("effective_risk_units"))
            if effective is None:
                continue
            effective_value = float(effective)
            maximum_effective_risk_units = (
                effective_value
                if maximum_effective_risk_units is None
                else max(maximum_effective_risk_units, effective_value)
            )
            cap = _safe_number(
                details.get(
                    "portfolio_unit_cap", risk.get("portfolio_unit_cap")
                )
            )
            if cap is not None and float(cap) > 0.0:
                utilization = effective_value / float(cap)
                maximum_portfolio_unit_utilization = (
                    utilization
                    if maximum_portfolio_unit_utilization is None
                    else max(
                        maximum_portfolio_unit_utilization, utilization
                    )
                )
    return {
        "average_invested_ratio": float(balances["invested_ratio"].mean()),
        "median_invested_ratio": float(balances["invested_ratio"].median()),
        "below_half_ratio": float((balances["invested_ratio"] < 0.5).mean()),
        "near_full_ratio": float((balances["invested_ratio"] >= 0.9).mean()),
        "average_cash_ratio": float((1.0 - balances["invested_ratio"]).mean()),
        "maximum_invested_ratio": float(balances["invested_ratio"].max()),
        "maximum_security_weight": max_security_weight,
        "maximum_asset_group_weight": max_group_weight,
        "planned_risk_coverage": planned_coverage,
        "maximum_planned_loss_ratio": max_planned_loss_ratio,
        "maximum_effective_risk_units": maximum_effective_risk_units,
        "maximum_portfolio_unit_utilization": (
            maximum_portfolio_unit_utilization
        ),
        "maximum_realized_60d_volatility": _safe_number(rolling_vol.max()),
        "filled_order_count": int(len(filled_orders)),
        "rejected_order_count": int((scenario.orders["status"] != "done").sum()),
        "turnover": None if average_equity == 0.0 else filled_notional / average_equity,
        "fees": float(filled_orders["commission"].astype(float).sum()),
        "closed_order_count": int(len(closed)),
        "closed_order_win_rate": float((closed["gains"].astype(float) > 0.0).mean()) if len(closed) else None,
        "closed_order_realized_gains": float(closed["gains"].astype(float).sum()),
        "protective_stop_events": int(
            (event_reason_codes == "protective_stop").sum()
        ),
        "redistribution_event_count": int(
            (event_reason_codes == "full_position_redistribution").sum()
        ),
    }


def _performance(scenario: ScenarioInput, positions: pd.DataFrame) -> dict[str, object]:
    return {**calculate_return_metrics(scenario.returns), **_risk_metrics(scenario, positions)}


def _group_contribution(rows: pd.DataFrame, key: str) -> list[dict[str, object]]:
    values = rows.groupby(key)["return_contribution"].sum().sort_values(key=lambda item: item.abs(), ascending=False)
    return [{"key": str(index), "contribution": float(value)} for index, value in values.items()]


def _source_native_attribution(scenario: ScenarioInput) -> dict[str, object]:
    event_counts = {
        str(key): int(value)
        for key, value in scenario.events["event_type"].value_counts().sort_index().items()
    }
    return {
        "status": "available",
        "method": "verified source-native event log",
        "security": [],
        "asset_group": [],
        "trading_reason": [],
        "period": [],
        "exposure": [],
        "event_counts": event_counts,
        "event_count_scope": "all verified source-native events",
        "reconciliation_error": None,
        "pnl_contribution": {
            "status": "evidence_insufficient",
            "reason": "security_daily_pnl_missing_at_source",
        },
        "limitation": "source-native events do not prove arithmetic security PnL contribution",
    }


def _available_attribution(
    scenario: ScenarioInput, pnl_facts: pd.DataFrame
) -> dict[str, object]:
    daily_asset = pnl_facts.groupby("date")["return_contribution"].sum() if not pnl_facts.empty else pd.Series(dtype=float)
    residual = scenario.returns.subtract(daily_asset, fill_value=0.0)
    total_target = float(scenario.returns.sum())
    residual_total = float(residual.sum())

    def with_residual(rows: list[dict[str, object]]) -> list[dict[str, object]]:
        completed = [*rows, {"key": "cash_fees_and_unclassified", "contribution": residual_total}]
        error = sum(float(row["contribution"]) for row in completed) - total_target
        if abs(error) > 1e-10:
            raise UnifiedAnalysisError("attribution does not reconcile")
        return completed

    security = with_residual(_group_contribution(pnl_facts, "security") if not pnl_facts.empty else [])
    groups = with_residual(_group_contribution(pnl_facts, "asset_group") if not pnl_facts.empty else [])
    reasons = with_residual(_group_contribution(pnl_facts, "attribution_reason") if not pnl_facts.empty else [])
    period_rows = pnl_facts.copy()
    if not period_rows.empty:
        period_rows["period"] = period_rows["date"].dt.strftime("%Y")
    periods = with_residual(_group_contribution(period_rows, "period") if not period_rows.empty else [])
    decision_events = (
        scenario.events.loc[scenario.events["event_type"] == "decision"]
        if "event_type" in scenario.events
        else scenario.events.iloc[0:0]
    )
    event_counts = {
        str(key): int(value)
        for key, value in decision_events.groupby("reason_code")
        .size()
        .sort_values(ascending=False)
        .items()
    }
    return {
        "method": "arithmetic daily PnL contribution divided by prior equity",
        "portfolio_arithmetic_return": total_target,
        "security": security,
        "asset_group": groups,
        "trading_reason": reasons,
        "period": periods,
        "exposure": with_residual(
            [{"key": "invested_assets", "contribution": float(daily_asset.sum())}]
        ),
        "event_counts": event_counts,
        "event_count_scope": "decision events only",
        "reconciliation_error": 0.0,
        "limitation": "arithmetic attribution is not geometric linking",
    }


def _attribution(scenario: ScenarioInput, pnl_facts: pd.DataFrame) -> dict[str, object]:
    if scenario.attribution_status != "available":
        return {
            "status": "evidence_insufficient",
            "reason": scenario.attribution_status,
            "method": None,
            "security": [],
            "asset_group": [],
            "trading_reason": [],
            "period": [],
            "exposure": [],
            "event_counts": {},
            "event_count_scope": None,
            "reconciliation_error": None,
            "pnl_contribution": {
                "status": "evidence_insufficient",
                "reason": scenario.attribution_status,
            },
        }
    if "details_json" not in scenario.events:
        return _source_native_attribution(scenario)
    result = _available_attribution(scenario, pnl_facts)
    result["status"] = "available"
    result["pnl_contribution"] = {"status": "available"}
    return result


def _benchmark_series(path: Path) -> dict[str, pd.Series]:
    frame = pq.read_table(path).to_pandas()
    frame["date"] = pd.to_datetime(frame["time"]).dt.normalize()
    output: dict[str, pd.Series] = {}
    for benchmark_id in BENCHMARK_IDS:
        rows = frame.loc[frame["benchmark_id"] == benchmark_id].sort_values("date")
        if rows.empty:
            raise UnifiedAnalysisError(f"benchmark is missing: {benchmark_id}")
        output[benchmark_id] = pd.Series(
            rows["returns"].astype(float).to_numpy(), index=rows["date"], name=benchmark_id
        )
    return output


def _period_result(
    scenario_id: str,
    dimension: str,
    returns: pd.Series,
    thresholds: Mapping[str, object],
) -> tuple[dict[str, object], ScenarioResult]:
    metrics = calculate_return_metrics(returns)
    status, reasons = evaluate_metrics(metrics, thresholds)
    document = {
        "scenario_id": scenario_id,
        "dimension": dimension,
        "status": status,
        "reasons": reasons,
        "metrics": metrics,
        "start": returns.index.min().date().isoformat(),
        "end": returns.index.max().date().isoformat(),
    }
    evidence = ScenarioResult(
        scenario_id=scenario_id,
        dimension=dimension,
        status=status,
        metrics=_numeric_metrics(metrics),
        input_sha256=evidence_digest(
            {"scenario_id": scenario_id, "returns": returns.astype(float).tolist()}
        ),
        reasons=tuple(reasons),
    )
    return document, evidence


def _fixed_and_rolling(
    baseline: ScenarioInput,
    analyses: Mapping[str, object],
    thresholds: Mapping[str, object],
) -> tuple[list[dict[str, object]], list[ScenarioResult]]:
    rows: list[dict[str, object]] = []
    evidence: list[ScenarioResult] = []
    for period in analyses["fixed_periods"]:
        selected = baseline.returns.loc[str(period["start"]): str(period["end"])]
        if selected.empty:
            row, result = _unavailable_result(
                f"period-{period['id']}",
                "fixed_period",
                "period_has_no_observations",
            )
        else:
            row, result = _period_result(
                f"period-{period['id']}", "fixed_period", selected, thresholds
            )
        rows.append(row)
        evidence.append(result)

    rolling = analyses["rolling"]
    start = baseline.returns.index.min()
    last = baseline.returns.index.max()
    rolling_count = 0
    while True:
        stop = start + pd.DateOffset(years=int(rolling["window_years"])) - pd.Timedelta(days=1)
        if stop > last:
            break
        selected = baseline.returns.loc[start:stop]
        scenario_id = f"rolling-{int(rolling['window_years'])}y-{start.date().isoformat()}"
        row, result = _period_result(scenario_id, "rolling_period", selected, thresholds)
        rows.append(row)
        evidence.append(result)
        rolling_count += 1
        start = start + pd.DateOffset(months=int(rolling["step_months"]))
    if rolling_count == 0:
        row, result = _unavailable_result(
            f"rolling-{int(rolling['window_years'])}y",
            "rolling_period",
            "insufficient_window_observations",
        )
        rows.append(row)
        evidence.append(result)
    return rows, evidence


def _deletion_sensitivity(
    baseline: ScenarioInput,
    security_pnl: pd.DataFrame,
    universe: Mapping[str, str],
    thresholds: Mapping[str, object],
) -> tuple[list[dict[str, object]], list[ScenarioResult]]:
    rows: list[dict[str, object]] = []
    evidence: list[ScenarioResult] = []
    definitions = (
        ("asset_delete_security", "security", sorted(universe)),
        ("asset_delete_group", "asset_group", sorted(set(universe.values()))),
    )
    for dimension, key, values in definitions:
        for value in values:
            contribution = (
                security_pnl.loc[security_pnl[key] == value]
                .groupby("date")["return_contribution"]
                .sum()
                .reindex(baseline.returns.index, fill_value=0.0)
            )
            adjusted = baseline.returns.subtract(contribution)
            scenario_id = f"delete-{key}-{str(value).lower().replace('.', '-').replace('_', '-')}"
            metrics = calculate_return_metrics(adjusted)
            status, reasons = evaluate_metrics(metrics, thresholds)
            row = {
                "scenario_id": scenario_id,
                "dimension": dimension,
                "removed": str(value),
                "method": "contribution-removal sensitivity; no capital reallocation",
                "status": status,
                "reasons": reasons,
                "metrics": metrics,
            }
            rows.append(row)
            evidence.append(
                ScenarioResult(
                    scenario_id=scenario_id,
                    dimension=dimension,
                    status=status,
                    metrics=_numeric_metrics(metrics),
                    input_sha256=evidence_digest(
                        {"scenario_id": scenario_id, "returns": adjusted.tolist()}
                    ),
                    reasons=tuple(reasons),
                )
            )
    return rows, evidence


def _bootstrap(
    returns: pd.Series, definition: Mapping[str, object]
) -> tuple[list[dict[str, object]], list[ScenarioResult]]:
    rows: list[dict[str, object]] = []
    evidence: list[ScenarioResult] = []
    thresholds = definition["thresholds"]
    for block_size in definition["block_sizes"]:
        paths = block_bootstrap(
            returns.to_numpy(dtype=np.float64),
            block_size=int(block_size),
            paths=int(definition["paths"]),
            horizon=int(definition["horizon_days"]),
            seed=int(definition["seed"]),
        )
        summary = summarize_bootstrap(paths)
        del paths
        reasons: list[str] = []
        if summary["probability_drawdown_over_20pct"] > float(thresholds["probability_drawdown_over_20pct_max"]):
            reasons.append("probability_drawdown_over_20pct")
        if summary["probability_drawdown_over_30pct"] > float(thresholds["probability_drawdown_over_30pct_max"]):
            reasons.append("probability_drawdown_over_30pct")
        if summary["median_terminal_return"] <= float(thresholds["median_terminal_return_min_exclusive"]):
            reasons.append("median_terminal_return")
        scenario_id = f"bootstrap-block-{block_size}"
        metrics = {
            **summary,
            "block_size": int(block_size),
            "paths": int(definition["paths"]),
            "horizon_days": int(definition["horizon_days"]),
            "seed": int(definition["seed"]),
        }
        status = "fail" if reasons else "pass"
        rows.append(
            {"scenario_id": scenario_id, "dimension": "block_bootstrap", "status": status, "reasons": reasons, "metrics": metrics}
        )
        evidence.append(
            ScenarioResult(
                scenario_id=scenario_id,
                dimension="block_bootstrap",
                status=status,
                metrics=_numeric_metrics(metrics),
                input_sha256=evidence_digest(
                    {"definition": dict(definition), "block_size": block_size, "returns": returns.tolist()}
                ),
                reasons=tuple(reasons),
            )
        )
    return rows, evidence


def _historical_stress(
    returns: pd.Series, definitions: Sequence[Mapping[str, object]]
) -> tuple[list[dict[str, object]], list[ScenarioResult]]:
    rows: list[dict[str, object]] = []
    evidence: list[ScenarioResult] = []
    for definition in definitions:
        selected = returns.loc[str(definition["start"]): str(definition["end"])]
        if selected.empty:
            row, result = _unavailable_result(
                str(definition["id"]),
                "historical_stress",
                "period_has_no_observations",
            )
            rows.append(row)
            evidence.append(result)
            continue
        metrics = calculate_return_metrics(selected)
        passed = abs(float(metrics["max_drawdown"])) <= float(definition["max_drawdown_abs_max"])
        status = "pass" if passed else "fail"
        reasons = [] if passed else ["max_drawdown_abs_max"]
        scenario_id = str(definition["id"])
        rows.append(
            {"scenario_id": scenario_id, "dimension": "historical_stress", "status": status, "reasons": reasons, "metrics": metrics}
        )
        evidence.append(
            ScenarioResult(
                scenario_id=scenario_id,
                dimension="historical_stress",
                status=status,
                metrics=_numeric_metrics(metrics),
                input_sha256=evidence_digest({"definition": dict(definition), "returns": selected.tolist()}),
                reasons=tuple(reasons),
            )
        )
    return rows, evidence


def _position_shocks(
    positions: pd.DataFrame, definitions: Sequence[Mapping[str, object]]
) -> tuple[list[dict[str, object]], list[ScenarioResult]]:
    rows: list[dict[str, object]] = []
    evidence: list[ScenarioResult] = []
    for definition in definitions:
        scenario_id = str(definition["id"])
        if definition.get("use_stop_failure_loss") is True:
            if (
                "stop_failure_loss" not in positions
                or "equity" not in positions
                or positions["stop_failure_loss"].isna().any()
            ):
                status = "evidence_insufficient"
                reasons = ["stop_failure_loss_missing_at_source"]
                metrics = {"evaluated_dates": 0, "worst_account_loss": None}
            else:
                daily_loss = positions.groupby("date")["stop_failure_loss"].sum()
                daily_equity = positions.groupby("date")["equity"].first()
                loss_ratio = daily_loss / daily_equity
                worst = float(loss_ratio.max()) if len(loss_ratio) else 0.0
                passed = worst <= float(definition["maximum_loss_abs_max"]) + 1e-12
                status = "pass" if passed else "fail"
                reasons = [] if passed else ["maximum_loss_abs_max"]
                metrics = {
                    "evaluated_dates": len(loss_ratio),
                    "worst_account_loss": worst,
                }
        else:
            losses: list[float] = []
            for _, daily in positions.groupby("date"):
                shock_return = 0.0
                for row in daily.to_dict("records"):
                    shock = definition.get("security_shocks", {}).get(
                        str(row["security"]),
                        definition["asset_group_shocks"].get(str(row["asset_group"])),
                    )
                    if shock is None:
                        raise UnifiedAnalysisError("position shock does not cover every asset group")
                    shock_return += float(row["weight"]) * float(shock)
                losses.append(max(0.0, -shock_return))
            worst = max(losses) if losses else 0.0
            passed = worst <= float(definition["maximum_loss_abs_max"]) + 1e-12
            status = "pass" if passed else "fail"
            reasons = [] if passed else ["maximum_loss_abs_max"]
            metrics = {"evaluated_dates": len(losses), "worst_account_loss": worst}
        rows.append(
            {"scenario_id": scenario_id, "dimension": "position_shock", "status": status, "reasons": reasons, "metrics": metrics}
        )
        evidence.append(
            ScenarioResult(
                scenario_id=scenario_id,
                dimension="position_shock",
                status=status,
                metrics=_numeric_metrics(metrics),
                input_sha256=evidence_digest({"definition": dict(definition), "positions": len(positions)}),
                reasons=tuple(reasons),
            )
        )
    return rows, evidence


def _cvar(
    returns: pd.Series, definitions: Sequence[Mapping[str, object]]
) -> tuple[list[dict[str, object]], list[ScenarioResult]]:
    rows: list[dict[str, object]] = []
    evidence: list[ScenarioResult] = []
    base = returns.to_numpy(dtype=np.float64)
    for definition in definitions:
        horizon = int(definition["horizon_days"])
        samples = base if horizon == 1 else rolling_compound_returns(base, window=horizon)
        tail = float(len(samples) * (1.0 - float(definition["confidence"])))
        sufficient = tail >= float(definition["minimum_tail_observations"])
        value = calculate_cvar(samples, float(definition["confidence"])) if sufficient else None
        passed = value is not None and value <= float(definition["maximum_loss_abs_max"])
        status = "evidence_insufficient" if not sufficient else ("pass" if passed else "fail")
        reasons = ["insufficient_tail_observations"] if not sufficient else ([] if passed else ["maximum_loss_abs_max"])
        metrics = {
            "cvar": value,
            "confidence": float(definition["confidence"]),
            "horizon_days": horizon,
            "samples": len(samples),
            "tail_observations": tail,
        }
        scenario_id = str(definition["id"])
        rows.append(
            {"scenario_id": scenario_id, "dimension": "cvar", "status": status, "reasons": reasons, "metrics": metrics}
        )
        evidence.append(
            ScenarioResult(
                scenario_id=scenario_id,
                dimension="cvar",
                status=status,
                metrics=_numeric_metrics(metrics),
                input_sha256=evidence_digest({"definition": dict(definition), "returns": base.tolist()}),
                reasons=tuple(reasons),
            )
        )
    return rows, evidence


def _unavailable_result(
    scenario_id: str, dimension: str, reason: str
) -> tuple[dict[str, object], ScenarioResult]:
    row = {
        "scenario_id": scenario_id,
        "dimension": dimension,
        "status": "evidence_insufficient",
        "reasons": [reason],
        "metrics": {},
    }
    return row, ScenarioResult(
        scenario_id=scenario_id,
        dimension=dimension,
        status="evidence_insufficient",
        metrics={},
        input_sha256=evidence_digest(row),
        reasons=(reason,),
    )


def _validated_benchmark(manifest_path: Path) -> tuple[BenchmarkSet, Path]:
    path = Path(manifest_path).resolve()
    if path.name != "manifest.json":
        raise UnifiedAnalysisError("benchmark manifest must be a benchmark-set manifest.json")
    try:
        benchmark_set = open_benchmark_set(path.parent)
    except BenchmarkSetError as exc:
        raise UnifiedAnalysisError(f"benchmark set validation failed: {exc}") from exc
    return benchmark_set, benchmark_set.root / "benchmark-returns.parquet"


def _standard_package_document(
    packages: Sequence[PackageSource],
) -> dict[str, object]:
    return {
        "script_version": _SCRIPT_VERSION,
        "packages": [
            {
                "scenario_id": package.scenario_id,
                "content_sha256": package.content_sha256,
                "manifest_sha256": package.manifest_sha256,
                "capabilities": dict(package.capabilities),
            }
            for package in packages
        ],
    }


def _analysis_input_identity(
    packages: Sequence[PackageSource],
    expanded: Mapping[str, object],
    benchmark_set: BenchmarkSet,
) -> dict[str, object]:
    data = benchmark_set.manifest["data"]
    assert isinstance(data, Mapping)
    return {
        "analysis_plan_sha256": expanded["analysis_plan_sha256"],
        "baseline_config_sha256": expanded["baseline_config_sha256"],
        "benchmark_set_id": benchmark_set.benchmark_set_id,
        "benchmark_manifest_sha256": _sha256(benchmark_set.root / "manifest.json"),
        "benchmark_data_sha256": data["sha256"],
        "result_packages": [
            {
                "scenario_id": package.scenario_id,
                "content_sha256": package.content_sha256,
                "manifest_sha256": package.manifest_sha256,
                "attribution": dict(package.capabilities["attribution"]),
            }
            for package in packages
        ],
    }


def run_standard_analysis(
    repo_root: Path,
    package_paths: Sequence[Path],
    analysis_plan_path: Path,
    benchmark_manifest_path: Path,
) -> dict[str, object]:
    root = Path(repo_root).resolve()
    try:
        packages = open_package_sources(package_paths)
    except PackageSourceError as exc:
        raise UnifiedAnalysisError(str(exc)) from exc
    expanded = expand_analysis_plan(root, analysis_plan_path)
    benchmark_set, benchmark_data = _validated_benchmark(benchmark_manifest_path)
    input_identity = _analysis_input_identity(packages, expanded, benchmark_set)
    analysis_id = evidence_digest(
        {
            "formula_version": _FORMULA_VERSION,
            **input_identity,
        }
    )
    configurations = {
        str(item["scenario_id"]): item
        for item in expanded["scenarios"]
    }
    package_ids = [package.scenario_id for package in packages]
    if any(package.strategy_id != expanded["strategy_id"] for package in packages):
        raise UnifiedAnalysisError("result package strategy differs from the analysis plan")
    if any(identifier not in configurations for identifier in package_ids):
        raise UnifiedAnalysisError("result package scenario is absent from the analysis plan")
    if "baseline" not in package_ids:
        raise UnifiedAnalysisError("result packages do not contain the baseline")
    for package in packages:
        expected = configurations[package.scenario_id]["params"]
        if dict(package.params) != expected:
            raise UnifiedAnalysisError(
                f"result package parameters differ from analysis plan: {package.scenario_id}"
            )
    scenarios = {
        package.scenario_id: load_package_scenario(package) for package in packages
    }
    analysis_root = root / ".local" / "standard-strategy-analysis" / analysis_id
    package_document = _standard_package_document(packages)
    package_document["benchmark"] = {
        "benchmark_set_id": benchmark_set.benchmark_set_id,
        "manifest_sha256": input_identity["benchmark_manifest_sha256"],
        "data_sha256": input_identity["benchmark_data_sha256"],
    }

    baseline = scenarios["baseline"]
    universe = expanded["universe"]
    thresholds = expanded["thresholds"]
    baseline_positions = _position_facts(baseline, universe)
    baseline_metrics = _performance(baseline, baseline_positions)
    baseline_status, baseline_reasons = evaluate_metrics(baseline_metrics, thresholds)
    evidence: list[ScenarioResult] = [
        ScenarioResult(
            scenario_id="baseline-performance",
            dimension="baseline_performance",
            status=baseline_status,
            metrics=_numeric_metrics(baseline_metrics),
            input_sha256=evidence_digest(
                {"scenario_id": baseline.scenario_id, "returns": baseline.returns.tolist()}
            ),
            reasons=tuple(baseline_reasons),
        )
    ]

    challenge_rows: list[dict[str, object]] = []
    for scenario_id in package_ids:
        if scenario_id == "baseline":
            continue
        scenario = scenarios[scenario_id]
        positions = _position_facts(scenario, universe)
        metrics = _performance(scenario, positions)
        status, reasons = evaluate_metrics(metrics, thresholds)
        challenge_rows.append(
            {
                "scenario_id": scenario_id,
                "dimension": configurations[scenario_id]["dimension"],
                "status": status,
                "reasons": reasons,
                "metrics": metrics,
            }
        )
        evidence.append(
            ScenarioResult(
                scenario_id=f"challenge-{scenario_id}",
                dimension=str(configurations[scenario_id]["dimension"]),
                status=status,
                metrics=_numeric_metrics(metrics),
                input_sha256=evidence_digest(
                    {"scenario_id": scenario_id, "returns": scenario.returns.tolist()}
                ),
                reasons=tuple(reasons),
            )
        )

    security_pnl = _security_pnl_facts(baseline, universe)
    attribution = _attribution(baseline, security_pnl)
    attribution_row, attribution_evidence = (
        _unavailable_result(
            "baseline-attribution", "deep_attribution", str(attribution.get("reason", "missing_at_source"))
        )
        if attribution["status"] == "evidence_insufficient"
        else (
            {
                "scenario_id": "baseline-attribution",
                "dimension": "deep_attribution",
                "status": "pass",
                "reasons": [],
                "metrics": {"event_count": int(len(baseline.events))},
            },
            ScenarioResult(
                scenario_id="baseline-attribution",
                dimension="deep_attribution",
                status="pass",
                metrics={"event_count": int(len(baseline.events))},
                input_sha256=evidence_digest(attribution),
            ),
        )
    )
    evidence.append(attribution_evidence)
    analyses = expanded["analyses"]
    period_rows, period_evidence = _fixed_and_rolling(baseline, analyses, thresholds)
    if baseline.attribution_status == "available" and "details_json" in baseline.events:
        deletion_rows, deletion_evidence = _deletion_sensitivity(
            baseline, security_pnl, universe, thresholds
        )
    else:
        deletion_rows, deletion_evidence = _unavailable_deletion_sensitivity(
            universe, "attribution_missing_at_source"
        )
    cost_rows, cost_evidence = _unavailable_cost_sensitivity(
        analyses["cost_execution"], "market_snapshot_missing_at_source"
    )
    bootstrap_rows, bootstrap_evidence = _bootstrap(baseline.returns, analyses["bootstrap"])
    stress_rows, stress_evidence = _historical_stress(
        baseline.returns, analyses["historical_stress"]
    )
    shock_rows, shock_evidence = _position_shocks(
        baseline_positions, analyses["position_shocks"]
    )
    cvar_rows, cvar_evidence = _cvar(baseline.returns, analyses["cvar"])
    evidence.extend(
        [
            *period_evidence,
            *deletion_evidence,
            *cost_evidence,
            *bootstrap_evidence,
            *stress_evidence,
            *shock_evidence,
            *cvar_evidence,
        ]
    )
    if len(scenarios) == 1:
        cross_scenario, cross_evidence = _unavailable_result(
            "cross-scenario", "cross_scenario", "single_result_package"
        )
    else:
        failed = sum(row["status"] == "fail" for row in challenge_rows)
        cross_scenario = {
            "scenario_id": "cross-scenario",
            "dimension": "cross_scenario",
            "status": "pass" if failed == 0 else "fail",
            "reasons": [] if failed == 0 else ["scenario_failure"],
            "metrics": {"package_count": len(scenarios), "failed_scenarios": failed},
        }
        cross_evidence = ScenarioResult(
            scenario_id="cross-scenario",
            dimension="cross_scenario",
            status=cross_scenario["status"],
            metrics=_numeric_metrics(cross_scenario["metrics"]),
            input_sha256=evidence_digest(cross_scenario),
            reasons=tuple(cross_scenario["reasons"]),
        )
    evidence.append(cross_evidence)

    aligned, alignment = align_three_way_benchmarks(
        baseline.returns, _benchmark_series(benchmark_data)
    )
    benchmark_statistics = {
        benchmark_id: calculate_benchmark_statistics(
            {date.date().isoformat(): float(value) for date, value in aligned["strategy"].items()},
            {date.date().isoformat(): float(value) for date, value in aligned[benchmark_id].items()},
        )
        for benchmark_id in BENCHMARK_IDS
    }
    evidence_rows = [item.to_document() for item in evidence]
    failures = [row for row in evidence_rows if row["status"] == "fail"]
    insufficient = [row for row in evidence_rows if row["status"] == "evidence_insufficient"]
    try:
        final_packages = open_package_sources(package_paths)
        final_expanded = expand_analysis_plan(root, analysis_plan_path)
        final_benchmark, _ = _validated_benchmark(benchmark_manifest_path)
        final_identity = _analysis_input_identity(
            final_packages, final_expanded, final_benchmark
        )
    except (PackageSourceError, ValueError) as exc:
        raise UnifiedAnalysisError("analysis inputs changed during analysis") from exc
    if final_identity != input_identity:
        raise UnifiedAnalysisError("analysis inputs changed during analysis")

    _immutable_json(analysis_root / "package-results.json", package_document)
    evidence_path = build_evidence_matrix(
        evidence, analysis_root / "evidence-matrix.parquet"
    )
    summary = {
        "schema_version": "standard-strategy-analysis/1",
        "formula_version": _FORMULA_VERSION,
        "script": {
            "version": _SCRIPT_VERSION,
            "entry": _SCRIPT_ENTRY,
        },
        "analysis_id": analysis_id,
        "strategy_id": expanded["strategy_id"],
        "authority": "read_only_standard_result_packages",
        "source_mutation": "forbidden",
        "sources": {"package_count": len(scenarios), **package_document},
        "analysis_configuration": {
            "analysis_plan": {
                "path": expanded["analysis_plan"],
                "sha256": expanded["analysis_plan_sha256"],
            },
            "baseline_config": {
                "path": expanded["baseline_config"],
                "sha256": expanded["baseline_config_sha256"],
            },
            "scenario_params": [
                {
                    "scenario_id": item["scenario_id"],
                    "dimension": item["dimension"],
                    "params_sha256": item["params_sha256"],
                }
                for item in expanded["scenarios"]
            ],
            "analyses": expanded["analyses"],
            "thresholds": expanded["thresholds"],
        },
        "baseline": {
            "scenario_id": baseline.scenario_id,
            "status": baseline_status,
            "reasons": baseline_reasons,
            "metrics": baseline_metrics,
        },
        "benchmarks": {"alignment": alignment, "statistics": benchmark_statistics},
        "attribution": attribution,
        "attribution_evidence": attribution_row,
        "challenge_results": challenge_rows,
        "cross_scenario": cross_scenario,
        "robustness": {
            "periods": period_rows,
            "asset_deletions": deletion_rows,
            "cost_execution": cost_rows,
            "bootstrap": bootstrap_rows,
            "historical_stress": stress_rows,
            "position_shocks": shock_rows,
            "cvar": cvar_rows,
        },
        "evidence_rows": evidence_rows,
        "evidence_matrix": {
            "path": evidence_path.relative_to(analysis_root).as_posix(),
            "sha256": _sha256(evidence_path),
            "rows": len(evidence_rows),
            "pass": sum(row["status"] == "pass" for row in evidence_rows),
            "fail": len(failures),
            "evidence_insufficient": len(insufficient),
        },
        "pre_vibe_recommendation": {
            "decision": (
                "recommend_joinquant_confirmation"
                if baseline_status == "pass" and not failures and not insufficient
                else "revise_before_joinquant"
            ),
            "baseline_status": baseline_status,
            "failure_count": len(failures),
            "evidence_insufficient_count": len(insufficient),
        },
        "next_action": "generate_standard_strategy_analysis_report",
    }
    analysis_path = analysis_root / "deterministic-analysis.json"
    _atomic_json(analysis_path, summary)
    return summary
