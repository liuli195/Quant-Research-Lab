from __future__ import annotations

import hashlib
import json

import numpy as np
import pyarrow as pa

from scripts.research.local_quant_research.contracts import (
    ExecutionBundle,
    PreparedStrategy,
    ResultExtension,
)

from ._delayed import (
    ADJUST_CASH_TRUNCATED,
    ADJUST_HOLDING_TRUNCATED,
    ADJUST_HORIZON_EXPIRED,
    ADJUST_NONE,
    ADJUST_RISK_TRUNCATED,
    ADJUST_UNTRADABLE,
    _filled_matrix,
)
from ._kernel import (
    ACTION_ADDITION,
    ACTION_ENTRY,
    ACTION_FULL_EXIT,
    ACTION_NONE,
    ACTION_REDISTRIBUTION_BUY,
    ACTION_REDISTRIBUTION_SELL,
    REASON_ALLOCATION_CONSTRAINT,
    REASON_ENTRY_BREAKOUT,
    REASON_FIXED_ADDITION_LEVEL,
    REASON_FULL_POSITION_REDISTRIBUTION,
    REASON_HIGH_LIMIT,
    REASON_LOW_LIMIT,
    REASON_MISSING_OPEN,
    REASON_NONE,
    REASON_ORDER_REJECTED,
    REASON_PAUSED,
    REASON_PROTECTIVE_STOP,
    REASON_TREND_EXIT,
    TurtleContext,
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
    ACTION_REDISTRIBUTION_SELL: "redistribution_sell",
    ACTION_ENTRY: "entry",
    ACTION_ADDITION: "addition",
    ACTION_REDISTRIBUTION_BUY: "redistribution_buy",
}
_REASON_NAMES = {
    REASON_NONE: "none",
    REASON_ENTRY_BREAKOUT: "entry_breakout",
    REASON_FIXED_ADDITION_LEVEL: "fixed_addition_level",
    REASON_PROTECTIVE_STOP: "protective_stop",
    REASON_TREND_EXIT: "trend_exit",
    REASON_FULL_POSITION_REDISTRIBUTION: "full_position_redistribution",
    REASON_MISSING_OPEN: "missing_open",
    REASON_PAUSED: "paused",
    REASON_HIGH_LIMIT: "high_limit",
    REASON_LOW_LIMIT: "low_limit",
    REASON_ALLOCATION_CONSTRAINT: "allocation_constraint",
    REASON_ORDER_REJECTED: "order_rejected",
}
_SELL_ACTIONS = {"full_exit", "redistribution_sell"}
_ADJUSTMENT_NAMES = {
    ADJUST_NONE: "none",
    ADJUST_CASH_TRUNCATED: "cash_truncated",
    ADJUST_HOLDING_TRUNCATED: "holding_truncated",
    ADJUST_UNTRADABLE: "untradable",
    ADJUST_HORIZON_EXPIRED: "horizon_expired",
    ADJUST_RISK_TRUNCATED: "risk_truncated",
}

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


def _safe_simulation_number(value: object) -> float | None:
    if value is None:
        return None
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    return numeric if np.isfinite(numeric) else None


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
    if source_reason == "allocation_constraint":
        return "allocation_constraint"
    if source_reason == "protective_stop":
        return "protective_stop"
    if action in {"redistribution_sell", "redistribution_buy"} or (
        source_reason == "full_position_redistribution"
    ):
        return "full_position_redistribution"
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
    if isinstance(value, (np.bool_, bool)):
        return bool(value)
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


def _ledger_matrices(
    context: TurtleContext,
    execution: ExecutionBundle,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    inputs = context.inputs
    shape = inputs.close.shape
    rows = {
        np.datetime_as_string(value, unit="D"): index
        for index, value in enumerate(np.asarray(inputs.dates, dtype="datetime64[D]"))
    }
    columns = {security: index for index, security in enumerate(inputs.securities)}
    filled = np.zeros(shape, dtype=np.int64)
    fill_prices = np.full(shape, np.nan, dtype=np.float64)
    fees = np.zeros(shape, dtype=np.float64)
    for order in execution.final.ledger.orders:
        row = rows.get(str(order["time"])[:10])
        column = columns.get(str(order["security"]))
        if row is None or column is None or filled[row, column] != 0:
            raise ResultContractError("vectorbt order identity is invalid")
        filled[row, column] = int(order["filled"])
        fill_prices[row, column] = float(order["price"])
        fees[row, column] = float(order["commission"])
    quantities = np.zeros(shape, dtype=np.int64)
    for asset in execution.final.ledger.assets:
        row = rows.get(str(asset["time"])[:10])
        column = columns.get(str(asset["security"]))
        if row is None or column is None or quantities[row, column] != 0:
            raise ResultContractError("vectorbt position identity is invalid")
        quantities[row, column] = int(round(float(asset["amount"])))
    values = np.asarray(
        execution.final.ledger.value["total_value"],
        dtype=np.float64,
    )
    return filled, fill_prices, fees, quantities, values


def _strategy_evidence(
    context: TurtleContext,
    execution: ExecutionBundle,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    rows, columns = context.inputs.close.shape
    final_trace = execution.final.trace
    if context.delay_days == 0:
        return (
            _matrix(
                final_trace["candidate_base_quantities"],
                (rows, columns),
                "candidate_base_quantities",
            ),
            _matrix(
                final_trace["event_group_scales"],
                (rows, columns),
                "event_group_scales",
            ),
            _vector(
                final_trace["event_portfolio_scales"],
                rows,
                "event_portfolio_scales",
            ),
            _vector(
                final_trace["event_cash_scales"],
                rows,
                "event_cash_scales",
            ),
        )
    primary_trace = execution.primary.trace
    candidate = np.zeros((rows, columns), dtype=np.int64)
    group_scales = np.ones((rows, columns), dtype=np.float64)
    portfolio_scales = np.ones(rows, dtype=np.float64)
    cash_scales = np.ones(rows, dtype=np.float64)
    for row in range(context.delay_days, rows):
        source = row - context.delay_days
        candidate[row] = primary_trace["candidate_base_quantities"][source]
        group_scales[row] = primary_trace["event_group_scales"][source]
        portfolio_scales[row] = primary_trace["event_portfolio_scales"][source]
        cash_scales[row] = primary_trace["event_cash_scales"][source]
    return candidate, group_scales, portfolio_scales, cash_scales


def _build_attribution_table(
    context: TurtleContext,
    execution: ExecutionBundle,
) -> pa.Table:
    scenario_id = context.scenario_id
    if not scenario_id:
        raise ResultContractError("scenario_id is required")
    inputs = context.inputs
    dates = np.asarray(inputs.dates, dtype="datetime64[D]")
    securities = inputs.securities
    close = np.asarray(inputs.close, dtype=np.float64)
    rows, columns = close.shape
    if dates.shape != (rows,) or len(securities) != columns:
        raise ResultContractError("input identities are inconsistent")
    shape = (rows, columns)
    trace = execution.final.trace
    actions = _matrix(trace["action_codes"], shape, "action_codes")
    reasons = _matrix(trace["reason_codes"], shape, "reason_codes")
    requested = _matrix(trace["requested_quantities"], shape, "requested_quantities")
    planned = _matrix(trace["planned_quantities"], shape, "planned_quantities")
    filled, fill_prices, fees, state_quantities, values = _ledger_matrices(
        context,
        execution,
    )
    common_stops = _matrix(
        trace["state_common_stop"], shape, "state_common_stop"
    )
    next_add = _matrix(
        trace["state_next_add_index"], shape, "state_next_add_index"
    )
    unit_counts = _matrix(
        trace["state_unit_counts"],
        shape,
        "state_unit_counts",
    )
    (
        candidate_base_quantities,
        event_group_scales,
        event_portfolio_scales,
        event_cash_scales,
    ) = _strategy_evidence(context, execution)
    portfolio_unit_cap = _safe_simulation_number(
        context.params.portfolio_unit_cap
    )
    signal_n = _matrix(inputs.signal_n, shape, "signal_n")
    planned_row_indices = _matrix(
        trace["planned_row_indices"],
        shape,
        "planned_row_indices",
    )
    adjustments = _matrix(
        trace["execution_adjustment_codes"],
        shape,
        "execution_adjustment_codes",
    )
    frozen_signal_n = _matrix(
        trace["frozen_signal_n"],
        shape,
        "frozen_signal_n",
    )
    risk_budgets = _matrix(
        trace["event_risk_budgets"],
        shape,
        "event_risk_budgets",
    ).astype(np.float64)
    planned_losses = _matrix(
        trace["event_planned_losses"],
        shape,
        "event_planned_losses",
    ).astype(np.float64)
    risk_cap_applied = _matrix(
        trace["event_risk_cap_applied"],
        shape,
        "event_risk_cap_applied",
    ).astype(np.bool_)
    execution_delay_days = context.delay_days
    if np.any(requested < 0) or np.any(planned < 0) or np.any(filled < 0):
        raise ResultContractError("simulation quantities must be non-negative")
    if not np.all(np.isfinite(fees)) or np.any(fees < 0.0):
        raise ResultContractError("simulation commissions must be finite and non-negative")
    if any(int(value) not in _ACTION_NAMES for value in np.unique(actions)):
        raise ResultContractError("simulation action code is unknown")
    if any(int(value) not in _REASON_NAMES for value in np.unique(reasons)):
        raise ResultContractError("simulation reason code is unknown")
    if any(int(value) not in _ADJUSTMENT_NAMES for value in np.unique(adjustments)):
        raise ResultContractError("simulation execution adjustment code is unknown")
    if execution_delay_days < 0:
        raise ResultContractError("simulation execution delay is invalid")
    active_plan_rows = planned_row_indices[actions != ACTION_NONE]
    if np.any(active_plan_rows < 0) or np.any(active_plan_rows >= rows):
        raise ResultContractError("simulation planned row is invalid")

    values = _vector(values, rows, "value")
    initial_cash = context.initial_cash
    if initial_cash <= 0.0 or not np.isfinite(initial_cash):
        raise ResultContractError("initial cash could not be reconciled")
    average_cost = np.zeros(columns, dtype=np.float64)
    previous_quantity = np.zeros(columns, dtype=np.int64)
    previous_close = np.full(columns, np.nan, dtype=np.float64)
    previous_stop = np.full(columns, np.nan, dtype=np.float64)
    previous_next_add = np.zeros(columns, dtype=np.int64)
    previous_unit_count = np.zeros(columns, dtype=np.int64)
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

    for expired in _horizon_expired(context, execution):
        planned_row = int(expired["planned_row_index"])
        column = int(expired["column"])
        if not 0 <= planned_row < rows or not 0 <= column < columns:
            raise ResultContractError("horizon-expired order identity is invalid")
        date_text = _date_text(dates[planned_row])
        security = securities[column]
        action = _ACTION_NAMES[int(expired["action_code"])]
        source_reason = _REASON_NAMES[int(expired["reason_code"])]
        reason_code = _reason_code(action, source_reason)
        target = int(expired["target_quantity"])
        requested_quantity = int(expired["requested_quantity"])
        delay_days = int(expired["delay_days"])
        attribution_rows.append(
            {
                "time": f"{date_text} 09:30:00",
                "event_id": _event_id(
                    scenario_id,
                    date_text,
                    security,
                    "decision",
                    reason_code + ":horizon_expired",
                ),
                "scope": "security",
                "security": security,
                "event_type": "decision",
                "reason_code": reason_code,
                "requested_amount": float(requested_quantity),
                "executed_amount": 0.0,
                "reference_price": _nullable(float(close[planned_row, column])),
                "risk_before": None,
                "risk_after": None,
                "details_json": _details(
                    action=action,
                    source_reason=source_reason,
                    planned_date=date_text,
                    execution_date=None,
                    delay_days=delay_days,
                    frozen_reason=source_reason,
                    frozen_target_amount=target,
                    frozen_signal_n=float(expired["signal_n"]),
                    execution_adjustment="horizon_expired",
                    planned_amount=target,
                    state_changed=False,
                ),
            }
        )

    for row in range(rows):
        date_text = _date_text(dates[row])
        execution_time = f"{date_text} 09:30:00"
        balance_time = f"{date_text} 16:00:00"
        valuation_rows: list[tuple[dict[str, object], dict[str, object]]] = []
        daily_security_pnl_total = 0.0
        for column, security in enumerate(securities):
            action = _ACTION_NAMES[int(actions[row, column])]
            source_reason = _REASON_NAMES[int(reasons[row, column])]
            reason_code = _reason_code(action, source_reason)
            adjustment = _ADJUSTMENT_NAMES[int(adjustments[row, column])]
            planned_row = int(planned_row_indices[row, column])
            planned_date = (
                _date_text(dates[planned_row]) if planned_row >= 0 else date_text
            )
            quantity = int(filled[row, column])
            frozen_target = max(int(planned[row, column]), quantity)
            before = int(previous_quantity[column])
            before_cost = float(average_cost[column])
            fee = float(fees[row, column])
            close_price = float(close[row, column])
            if quantity > 0 and action == "none":
                raise ResultContractError("filled order has no action")
            is_sell = action in _SELL_ACTIONS
            if quantity > 0:
                price = float(fill_prices[row, column])
                if not np.isfinite(price) or price <= 0.0 or fee < 0.0:
                    raise ResultContractError("filled order price or commission is invalid")
                if is_sell:
                    if quantity > before:
                        raise ResultContractError("sell order exceeds the held position")
                    expected_after = before - quantity
                    if expected_after == 0:
                        average_cost[column] = 0.0
                else:
                    expected_after = before + quantity
                    average_cost[column] = (
                        before * before_cost + quantity * price
                    ) / expected_after
                if int(state_quantities[row, column]) != expected_after:
                    raise ResultContractError("filled order and position state do not reconcile")
            elif int(state_quantities[row, column]) != before:
                raise ResultContractError("position changed without a filled order")

            after = int(state_quantities[row, column])
            stop_after = float(common_stops[row, column])
            next_after = int(next_add[row, column])
            units_after = int(unit_counts[row, column])
            state_changed = (
                before != after
                or _changed(float(previous_stop[column]), stop_after)
                or int(previous_next_add[column]) != next_after
            )
            logical_state_changed = (
                _changed(float(previous_stop[column]), stop_after)
                or int(previous_next_add[column]) != next_after
                or int(previous_unit_count[column]) != units_after
            )
            effective_risk_units = float(
                np.sum(
                    unit_counts[row].astype(np.float64)
                    * event_group_scales[row]
                )
                * event_portfolio_scales[row]
            )
            risk_before = _planned_risk(
                before, before_cost, float(previous_stop[column])
            )
            risk_after = _planned_risk(
                after, float(average_cost[column]), stop_after
            )
            group = inputs.asset_group_ids[column]
            group_mask = inputs.asset_group_ids == group
            group_risk_budget = float(np.nansum(risk_budgets[row, group_mask]))
            group_planned_loss = float(np.nansum(planned_losses[row, group_mask]))
            portfolio_risk_budget = float(np.nansum(risk_budgets[row]))
            portfolio_planned_loss = float(np.nansum(planned_losses[row]))
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
                            unit_count_before=int(previous_unit_count[column]),
                            unit_count_after=units_after,
                            candidate_base_quantity=int(
                                candidate_base_quantities[row, column]
                            ),
                            frozen_signal_n=float(
                                frozen_signal_n[row, column]
                            ),
                            actual_fill_price=(
                                float(fill_prices[row, column])
                                if quantity > 0
                                else np.nan
                            ),
                            group_scale=float(
                                event_group_scales[row, column]
                            ),
                            portfolio_scale=float(
                                event_portfolio_scales[row]
                            ),
                            cash_scale=float(event_cash_scales[row]),
                            effective_risk_units=effective_risk_units,
                            portfolio_unit_cap=portfolio_unit_cap,
                            risk_budget_amount=risk_budgets[row, column],
                            projected_planned_loss=planned_losses[row, column],
                            group_risk_budget_amount=group_risk_budget,
                            group_projected_planned_loss=group_planned_loss,
                            portfolio_risk_budget_amount=portfolio_risk_budget,
                            portfolio_projected_planned_loss=portfolio_planned_loss,
                            risk_cap_applied=risk_cap_applied[row, column],
                            redistribution_state_changed=(
                                logical_state_changed
                                if action
                                in {
                                    "redistribution_sell",
                                    "redistribution_buy",
                                }
                                else None
                            ),
                            state_changed=state_changed,
                            **(
                                {
                                    "planned_date": planned_date,
                                    "execution_date": date_text,
                                    "delay_days": execution_delay_days,
                                    "frozen_reason": source_reason,
                                    "frozen_target_amount": frozen_target,
                                    "execution_adjustment": adjustment,
                                }
                                if execution_delay_days > 0
                                or adjustment != "none"
                                else {}
                            ),
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

            previous_quantity[column] = after
            previous_stop[column] = stop_after
            previous_next_add[column] = next_after
            previous_unit_count[column] = units_after

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

    return _table(attribution_rows, _ATTRIBUTION_SCHEMA)


def _horizon_expired(
    context: TurtleContext,
    execution: ExecutionBundle,
) -> tuple[dict[str, object], ...]:
    delay_days = context.delay_days
    if delay_days <= 0:
        return ()
    trace = execution.primary.trace
    actions = np.asarray(trace["action_codes"])
    filled = _filled_matrix(
        execution.primary,
        context.inputs.dates,
        context.inputs.securities,
    )
    planned = np.asarray(trace["planned_quantities"])
    rows = actions.shape[0]
    expired: list[dict[str, object]] = []
    for row, column in zip(*np.nonzero((actions != ACTION_NONE) & (filled > 0))):
        if row + delay_days < rows:
            continue
        target = int(planned[row, column])
        if target <= 0:
            target = int(filled[row, column])
        expired.append(
            {
                "planned_row_index": int(row),
                "column": int(column),
                "action_code": int(actions[row, column]),
                "reason_code": int(trace["reason_codes"][row, column]),
                "requested_quantity": int(
                    trace["requested_quantities"][row, column]
                ),
                "target_quantity": target,
                "signal_n": float(context.inputs.signal_n[row, column]),
                "delay_days": delay_days,
            }
        )
    return tuple(expired)


def build_turtle_attribution(
    prepared: PreparedStrategy,
    execution: ExecutionBundle,
) -> ResultExtension:
    context = prepared.context
    if not isinstance(context, TurtleContext):
        raise TypeError("prepared turtle context is invalid")
    table = _build_attribution_table(context, execution)
    return ResultExtension(
        name="turtle_etf",
        schema_version=ATTRIBUTION_SCHEMA_VERSION,
        table=table,
        unique_key=("event_id",),
        evidence={
            "accounting": dict(_ACCOUNTING_CONTRACT),
            "execution_stages": list(execution.stages),
        },
    )
