from __future__ import annotations

from collections import namedtuple

import numpy as np
from numba import njit

from scripts.research.local_quant_research.contracts import (
    FILL_ACCEPTED,
    FILL_REJECTED,
    SIDE_BUY,
    SIDE_SELL,
    ExecutionRun,
    OrderProgram,
    PreparedStrategy,
)

from ._kernel import (
    ACTION_ADDITION,
    ACTION_ENTRY,
    ACTION_FULL_EXIT,
    ACTION_NONE,
    ACTION_REDISTRIBUTION_SELL,
    REASON_NONE,
    REASON_ORDER_REJECTED,
    TurtleContext,
    _action_priority,
    _buy_price,
    _commission,
    _finite_positive,
    _order_buffer,
    _planned_loss_nb,
    _risk_capped_target_nb,
    _sell_price,
)


ADJUST_NONE = 0
ADJUST_CASH_TRUNCATED = 1
ADJUST_HOLDING_TRUNCATED = 2
ADJUST_UNTRADABLE = 3
ADJUST_HORIZON_EXPIRED = 4
ADJUST_RISK_TRUNCATED = 5


DelayedInputs = namedtuple(
    "DelayedInputs",
    (
        "execution_open",
        "paused",
        "high_limit",
        "low_limit",
        "plan_actions",
        "plan_reasons",
        "plan_requested",
        "plan_targets",
        "plan_signal_n",
        "plan_risk_budgets",
        "delay_days",
    ),
)

DelayedState = namedtuple(
    "DelayedState",
    (
        "common_stop",
        "next_add_index",
        "unit_count",
        "position_costs",
        "action_codes",
        "reason_codes",
        "requested_quantities",
        "planned_quantities",
        "state_common_stop",
        "state_next_add_index",
        "state_unit_counts",
        "planned_row_indices",
        "execution_adjustment_codes",
        "frozen_signal_n",
        "event_risk_budgets",
        "event_planned_losses",
        "event_risk_cap_applied",
    ),
)


@njit
def _tradable(inputs: DelayedInputs, row: int, column: int, action: int) -> bool:
    open_price = inputs.execution_open[row, column]
    if not _finite_positive(open_price) or inputs.paused[row, column]:
        return False
    if action == ACTION_FULL_EXIT or action == ACTION_REDISTRIBUTION_SELL:
        low_limit = inputs.low_limit[row, column]
        return not np.isfinite(low_limit) or open_price > low_limit
    high_limit = inputs.high_limit[row, column]
    return not np.isfinite(high_limit) or open_price < high_limit


@njit
def _affordable_quantity(
    cash: float,
    target: int,
    lot_size: int,
    price: float,
    commission_multiplier: float,
) -> int:
    quantity = (target // lot_size) * lot_size
    while quantity > 0:
        fee = _commission(price, quantity, commission_multiplier)
        if price * quantity + fee <= cash + 1e-9:
            return quantity
        quantity -= lot_size
    return 0


@njit
def prepare_delayed_segment_nb(view, inputs, params, state, trace, orders) -> None:
    row = view.row
    source_row = row - inputs.delay_days
    if source_row < 0:
        return
    projected_cash = view.cash
    for offset in range(view.to_col - view.from_col):
        column = view.from_col + offset
        held = int(round(view.positions[offset]))
        state.event_risk_budgets[row, column] = inputs.plan_risk_budgets[
            source_row, column
        ]
        state.event_planned_losses[row, column] = _planned_loss_nb(
            held,
            held,
            state.position_costs[column],
            np.nan,
            state.common_stop[column],
        )
    for category in range(4):
        for offset in range(view.to_col - view.from_col):
            column = view.from_col + offset
            action = inputs.plan_actions[source_row, column]
            if action == ACTION_NONE or _action_priority(action) != category:
                continue
            target = inputs.plan_targets[source_row, column]
            state.action_codes[row, column] = action
            state.reason_codes[row, column] = inputs.plan_reasons[source_row, column]
            state.requested_quantities[row, column] = inputs.plan_requested[
                source_row, column
            ]
            state.planned_quantities[row, column] = target
            state.planned_row_indices[row, column] = source_row
            state.frozen_signal_n[row, column] = inputs.plan_signal_n[
                source_row, column
            ]
            orders[7][column] = category
            if not _tradable(inputs, row, column, action):
                state.execution_adjustment_codes[row, column] = ADJUST_UNTRADABLE
                continue
            open_price = inputs.execution_open[row, column]
            quantity = target
            if action == ACTION_FULL_EXIT or action == ACTION_REDISTRIBUTION_SELL:
                held = int(round(view.positions[offset]))
                quantity = min(target, held)
                if quantity < target:
                    state.execution_adjustment_codes[row, column] = (
                        ADJUST_HOLDING_TRUNCATED
                    )
                if quantity <= 0:
                    continue
                price = _sell_price(open_price, params.one_way_slippage)
                fee = _commission(price, quantity, params.commission_multiplier)
                projected_cash += price * quantity - fee
                target_after = held - quantity
                state.event_planned_losses[row, column] = _planned_loss_nb(
                    target_after,
                    held,
                    state.position_costs[column],
                    price,
                    state.common_stop[column],
                )
                orders[1][column] = SIDE_SELL
                orders[5][column] = np.nan
            else:
                price = _buy_price(open_price, params.one_way_slippage)
                signal_n = inputs.plan_signal_n[source_row, column]
                risk_stop = state.common_stop[column]
                if action == ACTION_ENTRY or action == ACTION_ADDITION:
                    candidate_stop = price - params.stop_n * signal_n
                    if action == ACTION_ENTRY or not np.isfinite(risk_stop):
                        risk_stop = candidate_stop
                    else:
                        risk_stop = max(risk_stop, candidate_stop)
                held = int(round(view.positions[offset]))
                risk_target = held
                risk_increase_allowed = True
                # ponytail: block all delayed buys on unresolved excess; add
                # group-local recovery only if delayed fill rates become material.
                for other_offset in range(view.to_col - view.from_col):
                    other_column = view.from_col + other_offset
                    other_budget = state.event_risk_budgets[row, other_column]
                    other_loss = state.event_planned_losses[row, other_column]
                    if (
                        np.isfinite(other_budget)
                        and np.isfinite(other_loss)
                        and other_loss > other_budget + 1e-9
                    ):
                        risk_increase_allowed = False
                        break
                if risk_increase_allowed:
                    risk_target = _risk_capped_target_nb(
                        held + target,
                        held,
                        state.position_costs[column],
                        price,
                        risk_stop,
                        inputs.plan_risk_budgets[source_row, column],
                        params.lot_size,
                    )
                risk_quantity = max(risk_target - held, 0)
                quantity = _affordable_quantity(
                    projected_cash,
                    min(target, risk_quantity),
                    params.lot_size,
                    price,
                    params.commission_multiplier,
                )
                if risk_quantity < target:
                    state.execution_adjustment_codes[row, column] = (
                        ADJUST_RISK_TRUNCATED
                    )
                    state.event_risk_cap_applied[row, column] = True
                if quantity < risk_quantity:
                    state.execution_adjustment_codes[row, column] = ADJUST_CASH_TRUNCATED
                if quantity <= 0:
                    continue
                fee = _commission(price, quantity, params.commission_multiplier)
                projected_cash -= price * quantity + fee
                state.event_planned_losses[row, column] = _planned_loss_nb(
                    held + quantity,
                    held,
                    state.position_costs[column],
                    price,
                    risk_stop,
                )
                orders[1][column] = SIDE_BUY
                orders[5][column] = float(params.lot_size)
            orders[0][column] = True
            orders[2][column] = float(quantity)
            orders[3][column] = price
            orders[4][column] = fee
            orders[6][column] = False


@njit
def after_delayed_fill_nb(event, inputs, params, state, trace, orders) -> None:
    row = event.row
    column = event.column
    action = state.action_codes[row, column]
    if event.status == FILL_REJECTED:
        state.reason_codes[row, column] = REASON_ORDER_REJECTED
        return
    if event.status != FILL_ACCEPTED:
        return
    if event.side == SIDE_SELL:
        position_before = event.position_after + event.size
        if event.position_after <= 1e-9 or position_before <= 1e-9:
            state.position_costs[column] = 0.0
        else:
            state.position_costs[column] *= event.position_after / position_before
        if action == ACTION_FULL_EXIT and event.position_after <= 1e-9:
            state.common_stop[column] = np.nan
            state.next_add_index[column] = 0
            state.unit_count[column] = 0
        return
    state.position_costs[column] += event.size * event.price
    if action != ACTION_ENTRY and action != ACTION_ADDITION:
        return
    signal_n = state.frozen_signal_n[row, column]
    candidate_stop = event.price - params.stop_n * signal_n
    if action == ACTION_ENTRY or not np.isfinite(state.common_stop[column]):
        state.common_stop[column] = candidate_stop
        state.next_add_index[column] = 1
        state.unit_count[column] = 1
    else:
        state.common_stop[column] = max(state.common_stop[column], candidate_stop)
        state.next_add_index[column] += 1
        state.unit_count[column] += 1


@njit
def after_delayed_segment_nb(view, inputs, params, state, trace, orders) -> None:
    row = view.row
    for offset in range(view.to_col - view.from_col):
        column = view.from_col + offset
        state.state_common_stop[row, column] = state.common_stop[column]
        state.state_next_add_index[row, column] = state.next_add_index[column]
        state.state_unit_counts[row, column] = state.unit_count[column]


def _readonly_matrix(value: object, shape: tuple[int, int], name: str) -> np.ndarray:
    result = np.asarray(value)
    if result.shape != shape:
        raise ValueError(f"primary trace shape is invalid: {name}")
    return result


def _state(
    rows: int,
    columns: int,
) -> DelayedState:
    return DelayedState(
        common_stop=np.full(columns, np.nan, dtype=np.float64),
        next_add_index=np.zeros(columns, dtype=np.int64),
        unit_count=np.zeros(columns, dtype=np.int64),
        position_costs=np.zeros(columns, dtype=np.float64),
        action_codes=np.zeros((rows, columns), dtype=np.int16),
        reason_codes=np.zeros((rows, columns), dtype=np.int16),
        requested_quantities=np.zeros((rows, columns), dtype=np.int64),
        planned_quantities=np.zeros((rows, columns), dtype=np.int64),
        state_common_stop=np.full((rows, columns), np.nan, dtype=np.float64),
        state_next_add_index=np.zeros((rows, columns), dtype=np.int64),
        state_unit_counts=np.zeros((rows, columns), dtype=np.int64),
        planned_row_indices=np.full((rows, columns), -1, dtype=np.int64),
        execution_adjustment_codes=np.zeros((rows, columns), dtype=np.int16),
        frozen_signal_n=np.full((rows, columns), np.nan, dtype=np.float64),
        event_risk_budgets=np.full((rows, columns), np.nan, dtype=np.float64),
        event_planned_losses=np.full((rows, columns), np.nan, dtype=np.float64),
        event_risk_cap_applied=np.zeros((rows, columns), dtype=np.bool_),
    )


def _trace(state: DelayedState) -> dict[str, np.ndarray]:
    return {
        name: getattr(state, name)
        for name in DelayedState._fields
        if name not in {"common_stop", "next_add_index", "unit_count", "position_costs"}
    }


def _filled_matrix(
    primary_run: ExecutionRun,
    dates: np.ndarray,
    securities: tuple[str, ...],
) -> np.ndarray:
    rows = {
        np.datetime_as_string(value, unit="D"): index
        for index, value in enumerate(np.asarray(dates, dtype="datetime64[D]"))
    }
    columns = {security: index for index, security in enumerate(securities)}
    filled = np.zeros((len(rows), len(columns)), dtype=np.int64)
    for order in primary_run.ledger.orders:
        row = rows.get(str(order["time"])[:10])
        column = columns.get(str(order["security"]))
        if row is None or column is None:
            raise ValueError("primary order identity is absent from turtle inputs")
        filled[row, column] += int(order["filled"])
    filled.setflags(write=False)
    return filled


def build_delayed_program(
    prepared: PreparedStrategy,
    primary_run: ExecutionRun,
) -> OrderProgram | None:
    context = prepared.context
    if not isinstance(context, TurtleContext):
        raise TypeError("prepared turtle context is invalid")
    if context.delay_days == 0:
        return None
    inputs = context.inputs
    rows, columns = inputs.close.shape
    trace = primary_run.trace
    shape = (rows, columns)
    actions = _readonly_matrix(trace["action_codes"], shape, "action_codes")
    filled = _filled_matrix(primary_run, inputs.dates, inputs.securities)
    planned = _readonly_matrix(trace["planned_quantities"], shape, "planned_quantities")
    valid = filled > 0
    plan_actions = np.where(valid, actions, ACTION_NONE).astype(np.int16)
    plan_reasons = np.where(valid, trace["reason_codes"], REASON_NONE).astype(np.int16)
    plan_requested = np.where(valid, trace["requested_quantities"], 0).astype(
        np.int64
    )
    plan_targets = np.where(valid, np.where(planned > 0, planned, filled), 0).astype(
        np.int64
    )
    plan_signal_n = np.where(valid, inputs.signal_n, np.nan).astype(np.float64)
    plan_risk_budgets = np.asarray(
        trace["event_risk_budgets"], dtype=np.float64
    )
    state = _state(rows, columns)
    delayed_inputs = DelayedInputs(
        inputs.execution_open,
        inputs.paused,
        inputs.high_limit,
        inputs.low_limit,
        plan_actions,
        plan_reasons,
        plan_requested,
        plan_targets,
        plan_signal_n,
        plan_risk_budgets,
        context.delay_days,
    )
    return OrderProgram(
        program_id=f"turtle-etf-delayed/{context.delay_days}",
        prepare_segment_nb=prepare_delayed_segment_nb,
        after_fill_nb=after_delayed_fill_nb,
        after_segment_nb=after_delayed_segment_nb,
        inputs=delayed_inputs,
        params=context.params,
        state=state,
        trace=_trace(state),
        orders=_order_buffer(columns),
    )
