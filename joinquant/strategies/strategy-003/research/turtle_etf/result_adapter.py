from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
import shutil
import uuid
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Mapping

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

from scripts.research.analysis_data import open_analysis_source

from .vectorbt_callbacks import (
    ACTION_ADDITION,
    ACTION_ENTRY,
    ACTION_FULL_EXIT,
    ACTION_NONE,
    ACTION_RISK_REDUCTION,
    REASON_ALLOCATION_CONSTRAINT,
    REASON_ENTRY_BREAKOUT,
    REASON_FIXED_ADDITION_LEVEL,
    REASON_HELD_RISK_INPUT_MISSING,
    REASON_HIGH_LIMIT,
    REASON_LOW_LIMIT,
    REASON_MISSING_OPEN,
    REASON_NONE,
    REASON_ORDER_REJECTED,
    REASON_PAUSED,
    REASON_PROTECTIVE_STOP,
    REASON_TARGET_VOLATILITY_REDUCTION,
    REASON_TREND_EXIT,
)


ATTRIBUTION_SCHEMA_VERSION = "turtle-etf-attribution/2"
ATTRIBUTION_FIELDS = (
    "time",
    "event_id",
    "scope",
    "security",
    "event_type",
    "reason_code",
    "requested_amount",
    "executed_amount",
    "reference_price",
    "risk_before",
    "risk_after",
    "details_json",
)

_REASON_CODES = {
    "signal_entry",
    "signal_add",
    "signal_exit",
    "protective_stop",
    "forced_risk_reduction",
    "risk_gate_block",
    "untradeable",
    "order_rejected",
    "state_update",
    "corporate_action_applied",
}
_EVENT_TYPES = {"decision", "state", "valuation", "corporate_action"}

_ACCOUNTING_CONTRACT = {
    "version": "turtle-etf-corporate-actions/1",
    "corporate_action_mode": "point_in_time_total_return_approximation",
    "continuity_factor_basis": "raw_previous_close_over_current_pre_close",
    "corporate_action_metadata_timing": "audit_only_may_be_retrospective",
    "price_basis": "continuous_economic_price",
    "quantity_basis": "economic_units",
    "cash_dividend_mode": "implicit_reinvestment_on_ex_date",
    "pay_date_cash_supported": False,
    "exact_joinquant_reconciliation": False,
}

_ACTION_NAMES = {
    ACTION_NONE: "none",
    ACTION_FULL_EXIT: "full_exit",
    ACTION_RISK_REDUCTION: "risk_reduction",
    ACTION_ENTRY: "entry",
    ACTION_ADDITION: "addition",
}
_REASON_NAMES = {
    REASON_NONE: "none",
    REASON_ENTRY_BREAKOUT: "entry_breakout",
    REASON_FIXED_ADDITION_LEVEL: "fixed_addition_level",
    REASON_PROTECTIVE_STOP: "protective_stop",
    REASON_TREND_EXIT: "trend_exit",
    REASON_TARGET_VOLATILITY_REDUCTION: "target_volatility_reduction",
    REASON_MISSING_OPEN: "missing_open",
    REASON_PAUSED: "paused",
    REASON_HIGH_LIMIT: "high_limit",
    REASON_LOW_LIMIT: "low_limit",
    REASON_ALLOCATION_CONSTRAINT: "allocation_constraint",
    REASON_HELD_RISK_INPUT_MISSING: "held_risk_input_missing",
    REASON_ORDER_REJECTED: "order_rejected",
}
_SELL_ACTIONS = {"full_exit", "risk_reduction"}

_RESULTS_SCHEMA = pa.schema(
    [
        pa.field("benchmark_returns", pa.float64()),
        pa.field("returns", pa.float64(), nullable=False),
        pa.field("time", pa.string(), nullable=False),
    ]
)
_BALANCES_SCHEMA = pa.schema(
    [
        pa.field("total_value", pa.float64(), nullable=False),
        pa.field("net_value", pa.float64(), nullable=False),
        pa.field("cash", pa.float64(), nullable=False),
        pa.field("aval_cash", pa.float64(), nullable=False),
        pa.field("time", pa.string(), nullable=False),
    ]
)
_POSITIONS_SCHEMA = pa.schema(
    [
        pa.field("pindex", pa.int64(), nullable=False),
        pa.field("avg_cost", pa.float64(), nullable=False),
        pa.field("margin", pa.float64(), nullable=False),
        pa.field("amount", pa.float64(), nullable=False),
        pa.field("today_amount", pa.int64(), nullable=False),
        pa.field("hold_cost", pa.float64(), nullable=False),
        pa.field("side", pa.string(), nullable=False),
        pa.field("price", pa.float64(), nullable=False),
        pa.field("gains", pa.float64(), nullable=False),
        pa.field("daily_gains", pa.float64(), nullable=False),
        pa.field("closeable_amount", pa.int64(), nullable=False),
        pa.field("time", pa.string(), nullable=False),
        pa.field("security_name", pa.string(), nullable=False),
        pa.field("security", pa.string(), nullable=False),
    ]
)
_ORDERS_SCHEMA = pa.schema(
    [
        pa.field("match_time", pa.string()),
        pa.field("pindex", pa.int64(), nullable=False),
        pa.field("cancel_time", pa.string()),
        pa.field("action", pa.string(), nullable=False),
        pa.field("limit_price", pa.float64(), nullable=False),
        pa.field("comment", pa.string(), nullable=False),
        pa.field("entrust_time", pa.string(), nullable=False),
        pa.field("finish_time", pa.string()),
        pa.field("side", pa.string(), nullable=False),
        pa.field("price", pa.float64(), nullable=False),
        pa.field("commission", pa.float64(), nullable=False),
        pa.field("gains", pa.float64(), nullable=False),
        pa.field("type", pa.string(), nullable=False),
        pa.field("time", pa.string(), nullable=False),
        pa.field("security_name", pa.string(), nullable=False),
        pa.field("security", pa.string(), nullable=False),
        pa.field("filled", pa.int64(), nullable=False),
        pa.field("amount", pa.int64(), nullable=False),
        pa.field("status", pa.string(), nullable=False),
    ]
)
_ATTRIBUTION_SCHEMA = pa.schema(
    [
        pa.field("time", pa.string(), nullable=False),
        pa.field("event_id", pa.string(), nullable=False),
        pa.field("scope", pa.string(), nullable=False),
        pa.field("security", pa.string()),
        pa.field("event_type", pa.string(), nullable=False),
        pa.field("reason_code", pa.string(), nullable=False),
        pa.field("requested_amount", pa.float64()),
        pa.field("executed_amount", pa.float64()),
        pa.field("reference_price", pa.float64()),
        pa.field("risk_before", pa.float64()),
        pa.field("risk_after", pa.float64()),
        pa.field("details_json", pa.string(), nullable=False),
    ]
)


class ResultContractError(ValueError):
    """Raised when vectorbt facts cannot prove the local result contract."""


@dataclass(frozen=True)
class LocalExecutionFacts:
    results: pa.Table
    balances: pa.Table
    positions: pa.Table
    orders: pa.Table
    attribution: pa.Table

    def with_attribution(self, document: Mapping[str, object]) -> LocalExecutionFacts:
        return replace(
            self,
            attribution=pa.Table.from_pydict(dict(document), schema=_ATTRIBUTION_SCHEMA),
        )


@dataclass(frozen=True)
class LocalResultPackage:
    root: Path
    params_sha256: str
    attribution_sha256: str


def _table(rows: list[dict[str, object]], schema: pa.Schema) -> pa.Table:
    if not rows:
        return pa.Table.from_pylist([], schema=schema)
    return pa.Table.from_pylist(rows, schema=schema)


def _vector(value: object, rows: int, field: str) -> np.ndarray:
    array = np.asarray(value, dtype=np.float64).reshape(-1)
    if array.shape != (rows,) or not np.all(np.isfinite(array)):
        raise ResultContractError(f"portfolio {field} is invalid")
    return array


def _matrix(value: object, shape: tuple[int, int], field: str) -> np.ndarray:
    array = np.asarray(value)
    if array.shape != shape:
        raise ResultContractError(f"simulation {field} shape is invalid")
    return array


def _date_text(value: object) -> str:
    return str(np.datetime64(value, "D"))


def _nullable(value: float) -> float | None:
    return float(value) if np.isfinite(value) else None


def _changed(left: float, right: float) -> bool:
    if np.isnan(left) and np.isnan(right):
        return False
    return bool(left != right)


def _event_id(
    scenario_id: str,
    date_text: str,
    security: str,
    event_type: str,
    reason_code: str,
) -> str:
    identity = "|".join(
        (
            ATTRIBUTION_SCHEMA_VERSION,
            scenario_id,
            date_text,
            security,
            event_type,
            reason_code,
        )
    )
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def _reason_code(action: str, source_reason: str) -> str:
    if source_reason == "order_rejected":
        return "order_rejected"
    if source_reason in {"missing_open", "paused", "high_limit", "low_limit"}:
        return "untradeable"
    if source_reason in {"allocation_constraint", "held_risk_input_missing"}:
        return "risk_gate_block"
    if source_reason == "protective_stop":
        return "protective_stop"
    if action == "risk_reduction" or source_reason == "target_volatility_reduction":
        return "forced_risk_reduction"
    if action == "full_exit" or source_reason == "trend_exit":
        return "signal_exit"
    if action == "entry":
        return "signal_entry"
    if action == "addition":
        return "signal_add"
    return "state_update"


def _planned_risk(quantity: int, average_cost: float, common_stop: float) -> float | None:
    if quantity <= 0:
        return 0.0
    if not np.isfinite(average_cost) or not np.isfinite(common_stop):
        return None
    return max(float(average_cost) - float(common_stop), 0.0) * int(quantity)


def _json_value(value: object) -> object:
    if isinstance(value, (np.integer, int)):
        return int(value)
    if isinstance(value, (np.floating, float)):
        return float(value) if np.isfinite(value) else None
    return value


def _details(**values: object) -> str:
    return json.dumps(
        {key: _json_value(value) for key, value in values.items()},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def to_joinquant_facts(
    inputs: object,
    simulation: object,
    scenario_id: str,
) -> LocalExecutionFacts:
    if not isinstance(scenario_id, str) or not scenario_id:
        raise ResultContractError("scenario_id is required")
    dates = np.asarray(getattr(inputs, "dates"), dtype="datetime64[D]")
    securities = tuple(str(item) for item in getattr(inputs, "securities"))
    close = np.asarray(getattr(inputs, "close"), dtype=np.float64)
    rows, columns = close.shape
    if dates.shape != (rows,) or len(securities) != columns:
        raise ResultContractError("input identities are inconsistent")
    shape = (rows, columns)
    actions = _matrix(simulation.action_codes, shape, "action_codes").astype(np.int64)
    reasons = _matrix(simulation.reason_codes, shape, "reason_codes").astype(np.int64)
    requested = _matrix(
        simulation.requested_quantities, shape, "requested_quantities"
    ).astype(np.int64)
    planned = _matrix(simulation.planned_quantities, shape, "planned_quantities").astype(
        np.int64
    )
    filled = _matrix(simulation.filled_quantities, shape, "filled_quantities").astype(
        np.int64
    )
    fill_prices = _matrix(simulation.fill_prices, shape, "fill_prices").astype(
        np.float64
    )
    fees = _matrix(simulation.fees, shape, "fees").astype(np.float64)
    state_quantities = _matrix(
        simulation.state_quantities, shape, "state_quantities"
    ).astype(np.int64)
    common_stops = _matrix(
        simulation.state_common_stop, shape, "state_common_stop"
    ).astype(np.float64)
    next_add = _matrix(
        simulation.state_next_add_index, shape, "state_next_add_index"
    ).astype(np.int64)
    raw_signal_n = getattr(inputs, "signal_n", None)
    signal_n = (
        np.full(shape, np.nan, dtype=np.float64)
        if raw_signal_n is None
        else _matrix(raw_signal_n, shape, "signal_n").astype(np.float64)
    )
    if np.any(requested < 0) or np.any(planned < 0) or np.any(filled < 0):
        raise ResultContractError("simulation quantities must be non-negative")
    if not np.all(np.isfinite(fees)) or np.any(fees < 0.0):
        raise ResultContractError("simulation commissions must be finite and non-negative")
    if any(int(value) not in _ACTION_NAMES for value in np.unique(actions)):
        raise ResultContractError("simulation action code is unknown")
    if any(int(value) not in _REASON_NAMES for value in np.unique(reasons)):
        raise ResultContractError("simulation reason code is unknown")

    values = _vector(simulation.portfolio.value(), rows, "value")
    cash = _vector(simulation.portfolio.cash(), rows, "cash")
    initial_cash = float(getattr(simulation, "initial_cash", np.nan))
    if initial_cash <= 0.0 or not np.isfinite(initial_cash):
        raise ResultContractError("initial cash could not be reconciled")
    average_cost = np.zeros(columns, dtype=np.float64)
    previous_quantity = np.zeros(columns, dtype=np.int64)
    previous_close = np.full(columns, np.nan, dtype=np.float64)
    previous_stop = np.full(columns, np.nan, dtype=np.float64)
    previous_next_add = np.zeros(columns, dtype=np.int64)
    order_rows: list[dict[str, object]] = []
    position_rows: list[dict[str, object]] = []
    attribution_rows: list[dict[str, object]] = []
    valid_dates = {_date_text(value) for value in dates}
    for application in getattr(inputs, "corporate_action_applications", ()):
        effective_date = str(getattr(application, "effective_date", ""))
        application_date = str(
            getattr(application, "application_date", effective_date)
        )
        security = str(getattr(application, "security", ""))
        source_event_id = str(getattr(application, "source_event_id", ""))
        if (
            effective_date not in valid_dates
            or application_date not in valid_dates
            or security not in securities
            or not source_event_id
        ):
            raise ResultContractError(
                "corporate-action attribution identity is invalid"
            )
        attribution_rows.append(
            {
                "time": f"{application_date} 00:00:00",
                "event_id": _event_id(
                    scenario_id,
                    application_date,
                    security,
                    "corporate_action",
                    f"corporate_action_applied:{source_event_id}",
                ),
                "scope": "security",
                "security": security,
                "event_type": "corporate_action",
                "reason_code": "corporate_action_applied",
                "requested_amount": None,
                "executed_amount": None,
                "reference_price": None,
                "risk_before": None,
                "risk_after": None,
                "details_json": _details(
                    source_event_id=source_event_id,
                    event_type=str(getattr(application, "event_type", "")),
                    effective_date=effective_date,
                    application_date=application_date,
                    announcement_date=str(
                        getattr(application, "announcement_date", "")
                    ),
                    knowledge_cutoff_date=str(
                        getattr(application, "knowledge_cutoff_date", "")
                    ),
                    evidence_timing=str(
                        getattr(
                            application,
                            "evidence_timing",
                            "retrospective_reconciliation"
                            if str(getattr(application, "announcement_date", ""))
                            > effective_date
                            else "point_in_time",
                        )
                    ),
                    split_ratio=getattr(application, "split_ratio", None),
                    cash_per_share=getattr(application, "cash_per_share", None),
                    cumulative_factor=float(
                        getattr(application, "cumulative_factor", np.nan)
                    ),
                    price_basis_changed=bool(
                        getattr(application, "price_basis_changed", True)
                    ),
                    source=str(getattr(application, "source", "")),
                    source_record_sha256=str(
                        getattr(application, "source_record_sha256", "")
                    ),
                    corporate_action_mode=(
                        "point_in_time_total_return_approximation"
                    ),
                ),
            }
        )

    for row in range(rows):
        date_text = _date_text(dates[row])
        execution_time = f"{date_text} 09:30:00"
        balance_time = f"{date_text} 16:00:00"
        today_buys = np.zeros(columns, dtype=np.int64)
        valuation_rows: list[tuple[dict[str, object], dict[str, object]]] = []
        daily_security_pnl_total = 0.0
        for column, security in enumerate(securities):
            action = _ACTION_NAMES[int(actions[row, column])]
            source_reason = _REASON_NAMES[int(reasons[row, column])]
            reason_code = _reason_code(action, source_reason)
            quantity = int(filled[row, column])
            before = int(previous_quantity[column])
            before_cost = float(average_cost[column])
            fee = float(fees[row, column])
            close_price = float(close[row, column])
            if quantity > 0 and action == "none":
                raise ResultContractError("filled order has no action")
            is_sell = action in _SELL_ACTIONS
            realized_gains = 0.0
            if quantity > 0:
                price = float(fill_prices[row, column])
                if not np.isfinite(price) or price <= 0.0 or fee < 0.0:
                    raise ResultContractError("filled order price or commission is invalid")
                if is_sell:
                    if quantity > before:
                        raise ResultContractError("sell order exceeds the held position")
                    realized_gains = (price - before_cost) * quantity - fee
                    expected_after = before - quantity
                    if expected_after == 0:
                        average_cost[column] = 0.0
                else:
                    expected_after = before + quantity
                    average_cost[column] = (
                        before * before_cost + quantity * price
                    ) / expected_after
                    today_buys[column] = quantity
                if int(state_quantities[row, column]) != expected_after:
                    raise ResultContractError("filled order and position state do not reconcile")
                order_rows.append(
                    {
                        "match_time": execution_time,
                        "pindex": 0,
                        "cancel_time": None,
                        "action": "close" if is_sell else "open",
                        "limit_price": 0.0,
                        "comment": "",
                        "entrust_time": execution_time,
                        "finish_time": execution_time,
                        "side": "long",
                        "price": price,
                        "commission": fee,
                        "gains": realized_gains,
                        "type": "market",
                        "time": execution_time,
                        "security_name": security,
                        "security": security,
                        "filled": quantity,
                        "amount": quantity,
                        "status": "done",
                    }
                )
            elif int(state_quantities[row, column]) != before:
                raise ResultContractError("position changed without a filled order")
            elif source_reason == "order_rejected" and action != "none":
                amount = max(
                    int(requested[row, column]), int(planned[row, column])
                )
                if amount <= 0:
                    raise ResultContractError("rejected order has no requested amount")
                order_rows.append(
                    {
                        "match_time": None,
                        "pindex": 0,
                        "cancel_time": execution_time,
                        "action": "close" if is_sell else "open",
                        "limit_price": 0.0,
                        "comment": source_reason,
                        "entrust_time": execution_time,
                        "finish_time": None,
                        "side": "long",
                        "price": 0.0,
                        "commission": 0.0,
                        "gains": 0.0,
                        "type": "market",
                        "time": execution_time,
                        "security_name": security,
                        "security": security,
                        "filled": 0,
                        "amount": amount,
                        "status": "canceled",
                    }
                )

            after = int(state_quantities[row, column])
            stop_after = float(common_stops[row, column])
            next_after = int(next_add[row, column])
            state_changed = (
                before != after
                or _changed(float(previous_stop[column]), stop_after)
                or int(previous_next_add[column]) != next_after
            )
            risk_before = _planned_risk(
                before, before_cost, float(previous_stop[column])
            )
            risk_after = _planned_risk(
                after, float(average_cost[column]), stop_after
            )
            if action != "none" or source_reason != "none" or state_changed:
                event_type = (
                    "decision"
                    if action != "none" or source_reason != "none"
                    else "state"
                )
                attribution_rows.append(
                    {
                        "time": execution_time,
                        "event_id": _event_id(
                            scenario_id, date_text, security, event_type, reason_code
                        ),
                        "scope": "security",
                        "security": security,
                        "event_type": event_type,
                        "reason_code": reason_code,
                        "requested_amount": float(requested[row, column]),
                        "executed_amount": float(quantity),
                        "reference_price": _nullable(
                            float(fill_prices[row, column])
                            if quantity > 0
                            else close_price
                        ),
                        "risk_before": risk_before,
                        "risk_after": risk_after,
                        "details_json": _details(
                            action=action,
                            source_reason=source_reason,
                            planned_amount=int(planned[row, column]),
                            commission=fee,
                            position_before=before,
                            position_after=after,
                            average_cost_before=before_cost,
                            average_cost_after=float(average_cost[column]),
                            common_stop_before=float(previous_stop[column]),
                            common_stop_after=stop_after,
                            next_add_before=int(previous_next_add[column]),
                            next_add_after=next_after,
                            state_changed=state_changed,
                        ),
                    }
                )

            active = before > 0 or after > 0 or quantity > 0 or fee > 0.0
            security_daily_pnl = 0.0
            if active:
                if not np.isfinite(close_price) or close_price <= 0.0:
                    raise ResultContractError("active position has no valid close price")
                if before > 0 and (
                    not np.isfinite(previous_close[column])
                    or float(previous_close[column]) <= 0.0
                ):
                    raise ResultContractError("held position has no previous close price")
                previous_price = (
                    float(previous_close[column]) if before > 0 else close_price
                )
                if quantity > 0 and is_sell:
                    security_daily_pnl = (
                        after * (close_price - previous_price)
                        + quantity
                        * (float(fill_prices[row, column]) - previous_price)
                        - fee
                    )
                elif quantity > 0:
                    security_daily_pnl = (
                        before * (close_price - previous_price)
                        + quantity
                        * (close_price - float(fill_prices[row, column]))
                        - fee
                    )
                else:
                    security_daily_pnl = before * (close_price - previous_price) - fee
                daily_security_pnl_total += security_daily_pnl

                n_value = float(signal_n[row, column])
                stop_failure_price = (
                    stop_after - 2.0 * n_value
                    if after > 0 and np.isfinite(stop_after) and np.isfinite(n_value)
                    else np.nan
                )
                stop_failure_loss = (
                    max(close_price - stop_failure_price, 0.0) * after
                    if np.isfinite(stop_failure_price)
                    else np.nan
                )
                valuation_rows.append(
                    (
                        {
                            "time": balance_time,
                            "event_id": _event_id(
                                scenario_id,
                                date_text,
                                security,
                                "valuation",
                                reason_code,
                            ),
                            "scope": "security",
                            "security": security,
                            "event_type": "valuation",
                            "reason_code": reason_code,
                            "requested_amount": None,
                            "executed_amount": None,
                            "reference_price": close_price,
                            "risk_before": risk_before,
                            "risk_after": risk_after,
                        },
                        {
                            "source_reason": source_reason,
                            "action": action,
                            "position_before": before,
                            "position_after": after,
                            "previous_close": previous_price,
                            "close": close_price,
                            "fill_price": (
                                float(fill_prices[row, column])
                                if quantity > 0
                                else np.nan
                            ),
                            "filled_amount": quantity,
                            "commission": fee,
                            "average_cost_before": before_cost,
                            "average_cost_after": float(average_cost[column]),
                            "common_stop_before": float(previous_stop[column]),
                            "common_stop_after": stop_after,
                            "n": n_value,
                            "stop_failure_price": stop_failure_price,
                            "stop_failure_loss": stop_failure_loss,
                            "security_daily_pnl": security_daily_pnl,
                        },
                    )
                )

            if after > 0:
                position_rows.append(
                    {
                        "pindex": 0,
                        "avg_cost": float(average_cost[column]),
                        "margin": 0.0,
                        "amount": float(after),
                        "today_amount": int(today_buys[column]),
                        "hold_cost": float(average_cost[column]),
                        "side": "long",
                        "price": close_price,
                        "gains": (close_price - float(average_cost[column])) * after,
                        "daily_gains": security_daily_pnl,
                        "closeable_amount": max(
                            after - int(today_buys[column]), 0
                        ),
                        "time": balance_time,
                        "security_name": security,
                        "security": security,
                    }
                )
            previous_quantity[column] = after
            previous_stop[column] = stop_after
            previous_next_add[column] = next_after

        portfolio_daily_pnl = float(values[row]) - (
            initial_cash if row == 0 else float(values[row - 1])
        )
        reconciliation_difference = daily_security_pnl_total - portfolio_daily_pnl
        if abs(reconciliation_difference) > 0.02:
            raise ResultContractError(
                "security daily PnL does not reconcile with portfolio change"
            )
        for event, valuation in valuation_rows:
            event["details_json"] = _details(
                **valuation,
                daily_security_pnl_total=daily_security_pnl_total,
                portfolio_daily_pnl=portfolio_daily_pnl,
                reconciliation_difference=reconciliation_difference,
            )
            attribution_rows.append(event)
        previous_close = close[row].copy()

    result_rows = []
    balance_rows = []
    for row in range(rows):
        time_text = f"{_date_text(dates[row])} 16:00:00"
        result_rows.append(
            {
                "benchmark_returns": None,
                "returns": float(values[row] / initial_cash - 1.0),
                "time": time_text,
            }
        )
        balance_rows.append(
            {
                "total_value": float(values[row]),
                "net_value": float(values[row]),
                "cash": float(cash[row]),
                "aval_cash": float(cash[row]),
                "time": time_text,
            }
        )

    facts = LocalExecutionFacts(
        results=_table(result_rows, _RESULTS_SCHEMA),
        balances=_table(balance_rows, _BALANCES_SCHEMA),
        positions=_table(position_rows, _POSITIONS_SCHEMA),
        orders=_table(order_rows, _ORDERS_SCHEMA),
        attribution=_table(attribution_rows, _ATTRIBUTION_SCHEMA),
    )
    validate_turtle_attribution(facts)
    _validate_common_facts(facts)
    return facts


def _validate_common_facts(facts: LocalExecutionFacts) -> None:
    expected = {
        "results": _RESULTS_SCHEMA,
        "balances": _BALANCES_SCHEMA,
        "positions": _POSITIONS_SCHEMA,
        "orders": _ORDERS_SCHEMA,
    }
    for name, schema in expected.items():
        table = getattr(facts, name)
        if table.schema != schema:
            raise ResultContractError(f"{name} fields do not match the contract")
    if facts.results.num_rows != facts.balances.num_rows:
        raise ResultContractError("results and balances do not reconcile")
    if facts.results["benchmark_returns"].null_count != facts.results.num_rows:
        raise ResultContractError("source benchmark returns must remain null")
    result_rows = facts.results.to_pylist()
    balance_rows = facts.balances.to_pylist()
    result_times = [str(item["time"]) for item in result_rows]
    if result_times != [str(item["time"]) for item in balance_rows]:
        raise ResultContractError("results and balances times do not reconcile")
    implied_initial_cash = []
    for result, balance in zip(result_rows, balance_rows, strict=True):
        denominator = 1.0 + float(result["returns"])
        if denominator <= 0.0:
            raise ResultContractError("cumulative return is invalid")
        implied_initial_cash.append(float(balance["total_value"]) / denominator)
    if implied_initial_cash and max(implied_initial_cash) - min(implied_initial_cash) > 0.01:
        raise ResultContractError("returns do not use one configured initial cash value")

    position_rows = facts.positions.to_pylist()
    position_keys = [
        (item["time"], item["pindex"], item["security"], item["side"])
        for item in position_rows
    ]
    if len(position_keys) != len(set(position_keys)):
        raise ResultContractError("position identity is not unique")
    if any(str(item["time"]) not in set(result_times) for item in position_rows):
        raise ResultContractError("position time is absent from results")
    position_value_by_time: dict[str, float] = {}
    for item in position_rows:
        time_text = str(item["time"])
        position_value_by_time[time_text] = position_value_by_time.get(
            time_text, 0.0
        ) + float(item["amount"]) * float(item["price"])
    for balance in balance_rows:
        time_text = str(balance["time"])
        reconciled = float(balance["cash"]) + position_value_by_time.get(time_text, 0.0)
        if abs(float(balance["total_value"]) - reconciled) > 0.02:
            raise ResultContractError("balance does not reconcile with cash and positions")

    result_dates = {time_text[:10] for time_text in result_times}
    for item in facts.orders.to_pylist():
        if str(item["time"])[:10] not in result_dates:
            raise ResultContractError("order date is absent from results")
        if not 0 <= int(item["filled"]) <= int(item["amount"]):
            raise ResultContractError("order filled amount is invalid")
    order_keys = list(
        zip(
            facts.orders["time"].to_pylist(),
            facts.orders["pindex"].to_pylist(),
            facts.orders["security"].to_pylist(),
        )
    )
    if len(order_keys) != len(set(order_keys)):
        raise ResultContractError("each security may have at most one order per day")


def validate_turtle_attribution(facts: LocalExecutionFacts) -> None:
    table = facts.attribution
    if table.schema != _ATTRIBUTION_SCHEMA:
        raise ResultContractError("attribution fields do not match the contract")
    rows = table.to_pylist()
    event_ids = [item["event_id"] for item in rows]
    if any(not isinstance(value, str) or not value for value in event_ids):
        raise ResultContractError("attribution event_id must be non-empty")
    if len(event_ids) != len(set(event_ids)):
        raise ResultContractError("attribution event_id must be unique")
    parsed_details: dict[str, dict[str, object]] = {}
    for item in rows:
        if item["scope"] != "security":
            raise ResultContractError("attribution scope is unknown")
        if item["event_type"] not in _EVENT_TYPES:
            raise ResultContractError("attribution event type is unknown")
        if item["reason_code"] not in _REASON_CODES:
            raise ResultContractError("attribution reason code is unknown")
        for name in (
            "requested_amount",
            "executed_amount",
            "reference_price",
            "risk_before",
            "risk_after",
        ):
            value = item[name]
            if value is not None and not np.isfinite(float(value)):
                raise ResultContractError(f"attribution {name} is invalid")
        for name in ("requested_amount", "executed_amount", "risk_before", "risk_after"):
            value = item[name]
            if value is not None and float(value) < 0.0:
                raise ResultContractError(f"attribution {name} is negative")
        try:
            details = json.loads(str(item["details_json"]))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ResultContractError("attribution details_json is invalid") from exc
        if not isinstance(details, dict):
            raise ResultContractError("attribution details_json must be an object")
        parsed_details[str(item["event_id"])] = details
        if item["event_type"] == "valuation":
            required = {
                "source_reason",
                "position_before",
                "position_after",
                "previous_close",
                "close",
                "filled_amount",
                "commission",
                "average_cost_before",
                "average_cost_after",
                "common_stop_before",
                "common_stop_after",
                "n",
                "stop_failure_price",
                "stop_failure_loss",
                "security_daily_pnl",
                "daily_security_pnl_total",
                "portfolio_daily_pnl",
                "reconciliation_difference",
            }
            if not required.issubset(details):
                raise ResultContractError("valuation attribution evidence is incomplete")
        if item["event_type"] == "corporate_action":
            required = {
                "source_event_id",
                "event_type",
                "effective_date",
                "application_date",
                "announcement_date",
                "knowledge_cutoff_date",
                "evidence_timing",
                "split_ratio",
                "cash_per_share",
                "cumulative_factor",
                "price_basis_changed",
                "source",
                "source_record_sha256",
                "corporate_action_mode",
            }
            if not required.issubset(details):
                raise ResultContractError(
                    "corporate-action attribution evidence is incomplete"
                )
    coverage = {
        (str(item["time"])[:10], str(item["security"]))
        for item in rows
        if item["event_type"] == "decision"
        and item["executed_amount"] is not None
        and float(item["executed_amount"]) > 0.0
    }
    filled_orders = {
        (str(item["time"])[:10], str(item["security"]))
        for item in facts.orders.to_pylist()
        if int(item["filled"]) > 0
    }
    if coverage != filled_orders:
        raise ResultContractError("attribution does not cover every order")
    rejected_coverage = {
        (str(item["time"])[:10], str(item["security"]))
        for item in rows
        if item["event_type"] == "decision"
        and item["reason_code"] == "order_rejected"
    }
    canceled_orders = {
        (str(item["time"])[:10], str(item["security"]))
        for item in facts.orders.to_pylist()
        if item["status"] == "canceled"
    }
    if rejected_coverage != canceled_orders:
        raise ResultContractError("attribution does not cover every canceled order")

    valuation_totals: dict[str, float] = {}
    for item in rows:
        if item["event_type"] != "valuation":
            continue
        date_text = str(item["time"])[:10]
        details = parsed_details[str(item["event_id"])]
        try:
            pnl = float(details["security_daily_pnl"])
            declared_total = float(details["daily_security_pnl_total"])
            declared_portfolio = float(details["portfolio_daily_pnl"])
            declared_difference = float(details["reconciliation_difference"])
        except (TypeError, ValueError) as exc:
            raise ResultContractError("valuation daily PnL evidence is invalid") from exc
        if not all(
            np.isfinite(value)
            for value in (pnl, declared_total, declared_portfolio, declared_difference)
        ):
            raise ResultContractError("valuation daily PnL evidence is invalid")
        if abs((declared_total - declared_portfolio) - declared_difference) > 1e-9:
            raise ResultContractError("valuation daily PnL evidence is inconsistent")
        valuation_totals[date_text] = valuation_totals.get(date_text, 0.0) + pnl

    result_rows = facts.results.to_pylist()
    balance_rows = facts.balances.to_pylist()
    if result_rows and len(result_rows) != len(balance_rows):
        raise ResultContractError("daily PnL cannot reconcile unmatched result rows")
    initial_cash = None
    if result_rows:
        denominator = 1.0 + float(result_rows[0]["returns"])
        if denominator <= 0.0:
            raise ResultContractError("daily PnL initial equity is invalid")
        initial_cash = float(balance_rows[0]["total_value"]) / denominator
    previous_value = initial_cash
    for balance in balance_rows:
        date_text = str(balance["time"])[:10]
        current_value = float(balance["total_value"])
        portfolio_pnl = current_value - float(previous_value)
        if abs(valuation_totals.get(date_text, 0.0) - portfolio_pnl) > 0.02:
            raise ResultContractError(
                "attribution security daily PnL does not reconcile with portfolio change"
            )
        previous_value = current_value


def validate_turtle_result(result_dir: Path) -> None:
    root = Path(result_dir).resolve()
    try:
        source = open_analysis_source(root)
    except Exception as exc:
        raise ResultContractError("local result failed the common contract") from exc
    if source.kind != "local_backtest":
        raise ResultContractError("turtle result must be a local backtest")
    try:
        extensions = source.manifest["extensions"]
        turtle = extensions["turtle_etf"]
        entry = turtle["attribution_log"]
        reference = entry["files"][0]
    except (KeyError, IndexError, TypeError) as exc:
        raise ResultContractError("turtle attribution declaration is missing") from exc
    expected_entry_fields = {
        "required",
        "status",
        "schema_version",
        "reason_code_version",
        "rows",
        "verified_empty",
        "time_range",
        "files",
        "evidence",
    }
    if (
        not isinstance(extensions, Mapping)
        or set(extensions) != {"turtle_etf"}
        or not isinstance(turtle, Mapping)
        or set(turtle) != {"attribution_log"}
        or not isinstance(entry, Mapping)
        or set(entry) != expected_entry_fields
        or entry["required"] is not True
        or entry["status"] != "complete"
        or entry["schema_version"] != ATTRIBUTION_SCHEMA_VERSION
        or entry["reason_code_version"] != ATTRIBUTION_SCHEMA_VERSION
        or not isinstance(reference, Mapping)
    ):
        raise ResultContractError("turtle attribution declaration is invalid")
    path_text = reference.get("path")
    digest = reference.get("sha256")
    if (
        not isinstance(path_text, str)
        or not isinstance(digest, str)
        or path_text != f"data/attribution_log-{digest}.parquet"
        or len(digest) != 64
        or any(character not in "0123456789abcdef" for character in digest)
    ):
        raise ResultContractError("turtle attribution file identity is invalid")
    path = root / path_text
    if (
        not path.is_file()
        or path.stat().st_size != reference.get("bytes")
        or _sha256_file(path) != digest
        or reference.get("rows") != entry["rows"]
        or reference.get("format") != "parquet"
        or reference.get("compression") != "zstd"
    ):
        raise ResultContractError("turtle attribution file evidence is invalid")
    evidence = entry.get("evidence")
    if (
        not isinstance(evidence, Mapping)
        or evidence.get("fields") != list(ATTRIBUTION_FIELDS)
        or evidence.get("unique_key") != ["event_id"]
        or evidence.get("reason_codes") != sorted(_REASON_CODES)
    ):
        raise ResultContractError("turtle attribution evidence is invalid")
    try:
        facts = LocalExecutionFacts(
            results=pq.read_table(root / "data/results.parquet"),
            balances=pq.read_table(root / "data/balances.parquet"),
            positions=pq.read_table(root / "data/positions.parquet"),
            orders=pq.read_table(root / "data/orders.parquet"),
            attribution=pq.read_table(path),
        )
    except Exception as exc:
        raise ResultContractError("turtle result Parquet is unreadable") from exc
    if facts.attribution.num_rows != entry["rows"]:
        raise ResultContractError("turtle attribution row count is invalid")
    if entry["verified_empty"] is not (facts.attribution.num_rows == 0):
        raise ResultContractError("turtle attribution empty evidence is invalid")
    if entry["time_range"] != _time_range(facts.attribution):
        raise ResultContractError("turtle attribution time range is invalid")
    _validate_common_facts(facts)
    validate_turtle_attribution(facts)


def _json_bytes(document: Mapping[str, object]) -> bytes:
    return (
        json.dumps(
            dict(document), ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False
        )
        + "\n"
    ).encode("utf-8")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def parameter_document_digest(document: Mapping[str, object]) -> str:
    return _sha256_bytes(_json_bytes(document))


def _sha256_file(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


def _file_ref(root: Path, path: Path) -> dict[str, object]:
    return {
        "path": path.relative_to(root).as_posix(),
        "sha256": _sha256_file(path),
        "bytes": path.stat().st_size,
    }


def _parquet_ref(root: Path, path: Path, rows: int) -> dict[str, object]:
    return {
        **_file_ref(root, path),
        "rows": rows,
        "format": "parquet",
        "compression": "zstd",
    }


def _time_range(table: pa.Table) -> dict[str, str | None]:
    if table.num_rows == 0:
        return {"start": None, "end": None}
    dates = [str(value)[:10] for value in table["time"].to_pylist()]
    return {"start": min(dates), "end": max(dates)}


def _dataset_entry(
    root: Path,
    name: str,
    table: pa.Table,
    path: Path,
    unique_key: list[str],
) -> dict[str, object]:
    return {
        "required": True,
        "status": "complete",
        "rows": table.num_rows,
        "verified_empty": table.num_rows == 0,
        "time_range": _time_range(table),
        "files": [_parquet_ref(root, path, table.num_rows)],
        "evidence": {"fields": table.schema.names, "unique_key": unique_key},
    }


def _engine() -> dict[str, str]:
    return {
        "backend": "vectorbt.Portfolio.from_order_func",
        "adapter_version": "local-vectorbt-adapter/1",
        "vectorbt": importlib.metadata.version("vectorbt"),
        "numba": importlib.metadata.version("numba"),
        "numpy": importlib.metadata.version("numpy"),
        "pandas": importlib.metadata.version("pandas"),
    }


def execution_facts_digest(facts: LocalExecutionFacts) -> str:
    document: dict[str, object] = {}
    for name in ("results", "balances", "positions", "orders", "attribution"):
        table = getattr(facts, name)
        document[name] = {
            "fields": [
                {
                    "name": field.name,
                    "type": str(field.type),
                    "nullable": field.nullable,
                }
                for field in table.schema
            ],
            "rows": table.to_pylist(),
        }
    return hashlib.sha256(
        json.dumps(
            document,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


def _read_materialized_facts(data_dir: Path) -> LocalExecutionFacts:
    attribution_paths = sorted(Path(data_dir).glob("attribution_log-*.parquet"))
    expected_files = {
        "results.parquet",
        "balances.parquet",
        "positions.parquet",
        "orders.parquet",
    }
    actual_files = {path.name for path in Path(data_dir).iterdir() if path.is_file()}
    if len(attribution_paths) != 1 or actual_files != {
        *expected_files,
        attribution_paths[0].name,
    }:
        raise ResultContractError("materialized execution fact file set is invalid")
    attribution_path = attribution_paths[0]
    digest = _sha256_file(attribution_path)
    if attribution_path.name != f"attribution_log-{digest}.parquet":
        raise ResultContractError("materialized attribution filename is invalid")
    try:
        return LocalExecutionFacts(
            results=pq.read_table(Path(data_dir) / "results.parquet"),
            balances=pq.read_table(Path(data_dir) / "balances.parquet"),
            positions=pq.read_table(Path(data_dir) / "positions.parquet"),
            orders=pq.read_table(Path(data_dir) / "orders.parquet"),
            attribution=pq.read_table(attribution_path),
        )
    except Exception as exc:
        raise ResultContractError("materialized execution facts are unreadable") from exc


def materialize_execution_facts(data_dir: Path, facts: LocalExecutionFacts) -> str:
    target = Path(data_dir)
    if target.exists():
        raise ResultContractError("execution fact directory already exists")
    _validate_common_facts(facts)
    validate_turtle_attribution(facts)
    try:
        target.mkdir(parents=True)
        for name in ("results", "balances", "positions", "orders"):
            pq.write_table(
                getattr(facts, name), target / f"{name}.parquet", compression="zstd"
            )
        temporary = target / ".attribution.parquet"
        pq.write_table(facts.attribution, temporary, compression="zstd")
        attribution_digest = _sha256_file(temporary)
        os.replace(
            temporary,
            target / f"attribution_log-{attribution_digest}.parquet",
        )
        written = _read_materialized_facts(target)
        _validate_common_facts(written)
        validate_turtle_attribution(written)
        expected_digest = execution_facts_digest(facts)
        if execution_facts_digest(written) != expected_digest:
            raise ResultContractError("materialized execution fact digest changed")
        return expected_digest
    except Exception:
        if target.exists():
            shutil.rmtree(target)
        raise


def write_local_result(
    backtest_dir: Path,
    *,
    facts: LocalExecutionFacts,
    run_id: str,
    local_backtest_id: str,
    scenario_id: str,
    snapshot_id: str,
    corporate_actions_sha256: str,
    code_path: Path,
    params: Mapping[str, object],
    performance: Mapping[str, object],
) -> LocalResultPackage:
    target = Path(backtest_dir).resolve()
    if target.exists():
        raise ResultContractError("local backtest directory already exists")
    if not all(isinstance(value, str) and value for value in (run_id, local_backtest_id, scenario_id)):
        raise ResultContractError("local result identity is incomplete")
    if len(snapshot_id) != 64 or any(character not in "0123456789abcdef" for character in snapshot_id):
        raise ResultContractError("snapshot_id must be a lowercase SHA256")
    if len(corporate_actions_sha256) != 64 or any(
        character not in "0123456789abcdef"
        for character in corporate_actions_sha256
    ):
        raise ResultContractError(
            "corporate_actions_sha256 must be a lowercase SHA256"
        )
    source_code = Path(code_path)
    if not source_code.is_file():
        raise ResultContractError("code source is missing")
    _validate_common_facts(facts)
    validate_turtle_attribution(facts)

    target.parent.mkdir(parents=True, exist_ok=True)
    staging = target.parent / f".{target.name}.{uuid.uuid4().hex}.tmp"
    try:
        data_dir = staging / "data"
        params_dir = staging / "params_versions"
        staging.mkdir()
        params_dir.mkdir()
        (staging / "code.py").write_bytes(source_code.read_bytes())
        params_bytes = _json_bytes(params)
        params_sha256 = parameter_document_digest(params)
        (staging / "params.json").write_bytes(params_bytes)
        (params_dir / f"{params_sha256}.json").write_bytes(params_bytes)
        (staging / "performance.json").write_bytes(_json_bytes(performance))

        materialize_execution_facts(data_dir, facts)
        paths = {
            name: data_dir / f"{name}.parquet"
            for name in ("results", "balances", "positions", "orders")
        }
        attribution_path = next(data_dir.glob("attribution_log-*.parquet"))
        attribution_sha256 = _sha256_file(attribution_path)

        datasets = {
            "results": _dataset_entry(
                staging, "results", facts.results, paths["results"], ["time"]
            ),
            "balances": _dataset_entry(
                staging, "balances", facts.balances, paths["balances"], ["time"]
            ),
            "positions": _dataset_entry(
                staging,
                "positions",
                facts.positions,
                paths["positions"],
                ["time", "pindex", "security", "side"],
            ),
            "orders": _dataset_entry(
                staging,
                "orders",
                facts.orders,
                paths["orders"],
                ["time", "pindex", "security"],
            ),
            "risk": {
                "required": False,
                "status": "missing_at_source",
                "reason": "computed_by_strategy_analysis",
                "rows": 0,
                "verified_empty": True,
                "files": [],
            },
            "period_risks": {
                "required": False,
                "status": "missing_at_source",
                "reason": "computed_by_strategy_analysis",
                "rows": 0,
                "verified_empty": True,
                "files": [],
            },
        }
        attribution_entry = {
            "required": True,
            "status": "complete",
            "schema_version": ATTRIBUTION_SCHEMA_VERSION,
            "reason_code_version": ATTRIBUTION_SCHEMA_VERSION,
            "rows": facts.attribution.num_rows,
            "verified_empty": facts.attribution.num_rows == 0,
            "time_range": _time_range(facts.attribution),
            "files": [
                _parquet_ref(
                    staging,
                    attribution_path,
                    facts.attribution.num_rows,
                )
            ],
            "evidence": {
                "fields": list(ATTRIBUTION_FIELDS),
                "unique_key": ["event_id"],
                "reason_codes": sorted(_REASON_CODES),
            },
        }
        code_ref = _file_ref(staging, staging / "code.py")
        current_params_ref = _file_ref(staging, staging / "params.json")
        version_params_ref = _file_ref(
            staging, params_dir / f"{params_sha256}.json"
        )
        manifest = {
            "schema_version": "local-backtest/1",
            "object": {
                "kind": "local_backtest",
                "local_id": local_backtest_id,
                "status": "complete",
            },
            "source": {
                "kind": "local_vectorbt",
                "engine": _engine(),
                "accounting": {
                    **_ACCOUNTING_CONTRACT,
                    "corporate_actions_sha256": corporate_actions_sha256,
                },
            },
            "authority": "local_research",
            "run": {
                "run_id": run_id,
                "scenario_id": scenario_id,
                "snapshot_id": snapshot_id,
            },
            "code": code_ref,
            "params": {"current": current_params_ref, "version": version_params_ref},
            "performance": _file_ref(staging, staging / "performance.json"),
            "datasets": datasets,
            "source_benchmark_returns": {
                "status": "missing_at_source",
                "reason": "independent_benchmark_set",
                "null_rows": facts.results.num_rows,
            },
            "gate": {
                "status": "pass",
                "exceptions": [],
                "checks": [
                    "local_schema",
                    "common_fact_fields",
                    "cross_table_reconciliation",
                    "turtle_attribution_coverage",
                ],
            },
            "extensions": {"turtle_etf": {"attribution_log": attribution_entry}},
        }
        (staging / "manifest.json").write_bytes(_json_bytes(manifest))
        validate_turtle_result(staging)
        if _sha256_file(attribution_path) != attribution_sha256:
            raise ResultContractError("attribution digest changed after writing")
        os.replace(staging, target)
        return LocalResultPackage(
            root=target,
            params_sha256=params_sha256,
            attribution_sha256=attribution_sha256,
        )
    except Exception:
        if staging.exists():
            shutil.rmtree(staging)
        raise
