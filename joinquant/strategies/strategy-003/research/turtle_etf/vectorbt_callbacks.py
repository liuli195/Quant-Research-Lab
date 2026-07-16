from __future__ import annotations

from collections import namedtuple

import numpy as np
from numba import njit
from vectorbt.portfolio import nb
from vectorbt.portfolio.enums import Direction, OrderSide, OrderStatus


ACTION_NONE = 0
ACTION_FULL_EXIT = 1
ACTION_REDISTRIBUTION_SELL = 2
ACTION_ENTRY = 3
ACTION_ADDITION = 4
ACTION_REDISTRIBUTION_BUY = 5

REASON_NONE = 0
REASON_ENTRY_BREAKOUT = 1
REASON_FIXED_ADDITION_LEVEL = 2
REASON_PROTECTIVE_STOP = 3
REASON_TREND_EXIT = 4
REASON_MISSING_OPEN = 5
REASON_PAUSED = 6
REASON_HIGH_LIMIT = 7
REASON_LOW_LIMIT = 8
REASON_ALLOCATION_CONSTRAINT = 9
REASON_ORDER_REJECTED = 10
REASON_FULL_POSITION_REDISTRIBUTION = 11


CallbackInputs = namedtuple(
    "CallbackInputs",
    (
        "execution_open",
        "signal_close",
        "signal_entry_high",
        "signal_exit_low",
        "signal_n",
        "paused",
        "high_limit",
        "low_limit",
        "asset_group_ids",
    ),
)

CallbackParams = namedtuple(
    "CallbackParams",
    (
        "lot_size",
        "unit_risk_per_n",
        "add_step_n",
        "stop_n",
        "max_units",
        "asset_group_unit_cap",
        "portfolio_unit_cap",
        "commission_multiplier",
        "one_way_slippage",
    ),
)

CallbackState = namedtuple(
    "CallbackState",
    (
        "unit_count",
        "unit_signal_n",
        "unit_base_quantities",
        "unit_fill_prices",
        "initial_fill_price",
        "initial_signal_n",
        "common_stop",
        "next_add_index",
        "candidate_signal_n",
        "candidate_base_quantity",
        "action_codes",
        "reason_codes",
        "requested_quantities",
        "planned_quantities",
        "filled_quantities",
        "fill_prices",
        "fees",
        "state_quantities",
        "state_common_stop",
        "state_next_add_index",
        "state_unit_counts",
        "event_group_scales",
        "event_portfolio_scales",
        "event_cash_scales",
        "day_equity",
        "allocation_ready",
    ),
)


@njit
def _finite_positive(value: float) -> bool:
    return np.isfinite(value) and value > 0.0


@njit
def _commission(price: float, quantity: int, multiplier: float) -> float:
    return max(5.0, price * quantity * 0.000085) * multiplier


@njit
def _buy_price(open_price: float, slippage: float) -> float:
    return open_price * (1.0 + slippage)


@njit
def _sell_price(open_price: float, slippage: float) -> float:
    return open_price * (1.0 - slippage)


@njit
def _buy_tradability_reason_nb(
    row: int, column: int, inputs: CallbackInputs
) -> int:
    open_price = inputs.execution_open[row, column]
    if not _finite_positive(open_price):
        return REASON_MISSING_OPEN
    if inputs.paused[row, column]:
        return REASON_PAUSED
    high_limit = inputs.high_limit[row, column]
    if np.isfinite(high_limit) and open_price >= high_limit:
        return REASON_HIGH_LIMIT
    return REASON_NONE


@njit
def _sell_tradability_reason_nb(
    row: int, column: int, inputs: CallbackInputs
) -> int:
    open_price = inputs.execution_open[row, column]
    if not _finite_positive(open_price):
        return REASON_MISSING_OPEN
    if inputs.paused[row, column]:
        return REASON_PAUSED
    low_limit = inputs.low_limit[row, column]
    if np.isfinite(low_limit) and open_price <= low_limit:
        return REASON_LOW_LIMIT
    return REASON_NONE


@njit
def _risk_scales_nb(
    unit_counts: np.ndarray,
    asset_group_ids: np.ndarray,
    group_count: int,
    asset_group_unit_cap: float,
    portfolio_unit_cap: float,
):
    group_units = np.zeros(group_count, dtype=np.float64)
    for column in range(unit_counts.shape[0]):
        group_units[asset_group_ids[column]] += unit_counts[column]
    group_scales = np.ones(group_count, dtype=np.float64)
    for group in range(group_count):
        if group_units[group] > asset_group_unit_cap:
            group_scales[group] = asset_group_unit_cap / group_units[group]
    effective_units = 0.0
    for column in range(unit_counts.shape[0]):
        effective_units += (
            unit_counts[column] * group_scales[asset_group_ids[column]]
        )
    portfolio_scale = 1.0
    if effective_units > portfolio_unit_cap:
        portfolio_scale = portfolio_unit_cap / effective_units
    return group_scales, portfolio_scale


@njit
def _targets_for_scale_nb(
    unit_base_quantities: np.ndarray,
    unit_counts: np.ndarray,
    asset_group_ids: np.ndarray,
    group_scales: np.ndarray,
    portfolio_scale: float,
    cash_scale: float,
    locked_quantities: np.ndarray,
    lot_size: int,
) -> np.ndarray:
    targets = np.zeros(unit_counts.shape[0], dtype=np.int64)
    for column in range(unit_counts.shape[0]):
        if locked_quantities[column] >= 0:
            targets[column] = locked_quantities[column]
            continue
        raw_quantity = 0
        for unit in range(unit_counts[column]):
            raw_quantity += unit_base_quantities[column, unit]
        scaled = (
            raw_quantity
            * group_scales[asset_group_ids[column]]
            * portfolio_scale
            * cash_scale
        )
        targets[column] = int(scaled // lot_size) * lot_size
    return targets


@njit
def _cash_after_targets_nb(
    row: int,
    targets: np.ndarray,
    positions: np.ndarray,
    cash: float,
    inputs: CallbackInputs,
    params: CallbackParams,
) -> float:
    projected_cash = cash
    for column in range(targets.shape[0]):
        current = int(round(positions[column]))
        if targets[column] >= current:
            continue
        quantity = current - targets[column]
        price = _sell_price(
            inputs.execution_open[row, column], params.one_way_slippage
        )
        projected_cash += price * quantity - _commission(
            price, quantity, params.commission_multiplier
        )
    for column in range(targets.shape[0]):
        current = int(round(positions[column]))
        if targets[column] <= current:
            continue
        quantity = targets[column] - current
        price = _buy_price(
            inputs.execution_open[row, column], params.one_way_slippage
        )
        projected_cash -= price * quantity + _commission(
            price, quantity, params.commission_multiplier
        )
    return projected_cash


@njit
def _cash_feasible_targets_nb(
    row: int,
    unit_base_quantities: np.ndarray,
    unit_counts: np.ndarray,
    positions: np.ndarray,
    cash: float,
    group_scales: np.ndarray,
    portfolio_scale: float,
    locked_quantities: np.ndarray,
    inputs: CallbackInputs,
    params: CallbackParams,
):
    full_targets = _targets_for_scale_nb(
        unit_base_quantities,
        unit_counts,
        inputs.asset_group_ids,
        group_scales,
        portfolio_scale,
        1.0,
        locked_quantities,
        params.lot_size,
    )
    if _cash_after_targets_nb(
        row, full_targets, positions, cash, inputs, params
    ) >= -1e-9:
        return full_targets, 1.0
    lower = 0.0
    upper = 1.0
    best = _targets_for_scale_nb(
        unit_base_quantities,
        unit_counts,
        inputs.asset_group_ids,
        group_scales,
        portfolio_scale,
        lower,
        locked_quantities,
        params.lot_size,
    )
    for _ in range(64):
        candidate_scale = (lower + upper) / 2.0
        candidate = _targets_for_scale_nb(
            unit_base_quantities,
            unit_counts,
            inputs.asset_group_ids,
            group_scales,
            portfolio_scale,
            candidate_scale,
            locked_quantities,
            params.lot_size,
        )
        if _cash_after_targets_nb(
            row, candidate, positions, cash, inputs, params
        ) >= -1e-9:
            lower = candidate_scale
            best = candidate
        else:
            upper = candidate_scale
    return best, lower


@njit
def _clear_position_state_nb(column: int, state: CallbackState) -> None:
    state.unit_count[column] = 0
    for unit in range(state.unit_signal_n.shape[1]):
        state.unit_signal_n[column, unit] = np.nan
        state.unit_base_quantities[column, unit] = 0
        state.unit_fill_prices[column, unit] = np.nan
    state.initial_fill_price[column] = np.nan
    state.initial_signal_n[column] = np.nan
    state.common_stop[column] = np.nan
    state.next_add_index[column] = 0


@njit
def pre_sim_func_nb(
    c,
    state: CallbackState,
    inputs: CallbackInputs,
    params: CallbackParams,
):
    state.unit_count[:] = 0
    state.unit_signal_n[:, :] = np.nan
    state.unit_base_quantities[:, :] = 0
    state.unit_fill_prices[:, :] = np.nan
    state.initial_fill_price[:] = np.nan
    state.initial_signal_n[:] = np.nan
    state.common_stop[:] = np.nan
    state.next_add_index[:] = 0
    state.candidate_signal_n[:, :] = np.nan
    state.candidate_base_quantity[:, :] = 0
    state.action_codes[:, :] = ACTION_NONE
    state.reason_codes[:, :] = REASON_NONE
    state.requested_quantities[:, :] = 0
    state.planned_quantities[:, :] = 0
    state.filled_quantities[:, :] = 0
    state.fill_prices[:, :] = np.nan
    state.fees[:, :] = 0.0
    state.state_quantities[:, :] = 0
    state.state_common_stop[:, :] = np.nan
    state.state_next_add_index[:, :] = 0
    state.state_unit_counts[:, :] = 0
    state.event_group_scales[:, :] = 1.0
    state.event_portfolio_scales[:] = 1.0
    state.event_cash_scales[:] = 1.0
    state.day_equity[:] = np.nan
    state.allocation_ready[:] = False
    return state, inputs, params


@njit
def _set_call_sequence_nb(c, state: CallbackState) -> None:
    row = c.i
    call_index = 0
    for category in range(5):
        for column in range(c.from_col, c.to_col):
            action = state.action_codes[row, column]
            actual_category = 4
            if action == ACTION_FULL_EXIT:
                actual_category = 0
            elif action == ACTION_REDISTRIBUTION_SELL:
                actual_category = 1
            elif action == ACTION_ENTRY or action == ACTION_ADDITION:
                actual_category = 2
            elif action == ACTION_REDISTRIBUTION_BUY:
                actual_category = 3
            if actual_category == category:
                c.call_seq_now[call_index] = column - c.from_col
                call_index += 1


@njit
def pre_segment_func_nb(
    c,
    state: CallbackState,
    inputs: CallbackInputs,
    params: CallbackParams,
):
    row = c.i
    column_count = c.to_col - c.from_col
    equity = c.last_value[c.group]
    state.day_equity[row] = equity
    state.allocation_ready[row] = False
    for column in range(c.from_col, c.to_col):
        state.action_codes[row, column] = ACTION_NONE
        state.reason_codes[row, column] = REASON_NONE
        state.requested_quantities[row, column] = 0
        state.planned_quantities[row, column] = 0
        state.candidate_signal_n[row, column] = np.nan
        state.candidate_base_quantity[row, column] = 0

    exit_active = np.zeros(column_count, dtype=np.bool_)
    candidate_active = np.zeros(column_count, dtype=np.bool_)
    any_decision = False

    for offset in range(column_count):
        column = c.from_col + offset
        position = c.last_position[column]
        close = inputs.signal_close[row, column]
        if position <= 0.0 or not _finite_positive(close):
            continue
        reason = REASON_NONE
        if np.isfinite(state.common_stop[column]) and close <= state.common_stop[column]:
            reason = REASON_PROTECTIVE_STOP
        elif (
            np.isfinite(inputs.signal_exit_low[row, column])
            and close < inputs.signal_exit_low[row, column]
        ):
            reason = REASON_TREND_EXIT
        if reason != REASON_NONE:
            state.action_codes[row, column] = ACTION_FULL_EXIT
            state.reason_codes[row, column] = reason
            state.requested_quantities[row, column] = int(round(position))
            tradability = _sell_tradability_reason_nb(row, column, inputs)
            if tradability == REASON_NONE:
                exit_active[offset] = True
            else:
                state.reason_codes[row, column] = tradability
            any_decision = True

    for offset in range(column_count):
        column = c.from_col + offset
        if state.action_codes[row, column] == ACTION_FULL_EXIT:
            continue
        close = inputs.signal_close[row, column]
        signal_n = inputs.signal_n[row, column]
        if not _finite_positive(close) or not _finite_positive(signal_n):
            continue
        position = c.last_position[column]
        action = ACTION_NONE
        reason = REASON_NONE
        if position > 0.0 and state.unit_count[column] > 0:
            next_index = state.next_add_index[column]
            if (
                next_index < params.max_units
                and close
                >= state.initial_fill_price[column]
                + next_index * params.add_step_n * state.initial_signal_n[column]
            ):
                action = ACTION_ADDITION
                reason = REASON_FIXED_ADDITION_LEVEL
        elif (
            position <= 0.0
            and np.isfinite(inputs.signal_entry_high[row, column])
            and close > inputs.signal_entry_high[row, column]
        ):
            action = ACTION_ENTRY
            reason = REASON_ENTRY_BREAKOUT
        if action == ACTION_NONE:
            continue
        quantity = int(equity * params.unit_risk_per_n / signal_n)
        quantity = (quantity // params.lot_size) * params.lot_size
        if quantity <= 0:
            continue
        state.action_codes[row, column] = action
        state.reason_codes[row, column] = reason
        state.requested_quantities[row, column] = quantity
        state.candidate_signal_n[row, column] = signal_n
        state.candidate_base_quantity[row, column] = quantity
        tradability = _buy_tradability_reason_nb(row, column, inputs)
        if tradability == REASON_NONE:
            candidate_active[offset] = True
        else:
            state.reason_codes[row, column] = tradability
        any_decision = True

    if not any_decision:
        _set_call_sequence_nb(c, state)
        for column in range(c.from_col, c.to_col):
            open_price = inputs.execution_open[row, column]
            if _finite_positive(open_price):
                c.last_val_price[column] = open_price
        state.allocation_ready[row] = True
        return state, inputs, params

    locked = np.full(column_count, -1, dtype=np.int64)
    for offset in range(column_count):
        column = c.from_col + offset
        if (
            not _finite_positive(inputs.execution_open[row, column])
            or inputs.paused[row, column]
            or (
                state.action_codes[row, column] == ACTION_FULL_EXIT
                and not exit_active[offset]
            )
        ):
            locked[offset] = int(round(c.last_position[column]))

    targets = np.asarray(c.last_position[c.from_col : c.to_col], dtype=np.int64)
    group_count = 1
    for offset in range(column_count):
        group_count = max(
            group_count, int(inputs.asset_group_ids[c.from_col + offset]) + 1
        )
    group_scales = np.ones(group_count, dtype=np.float64)
    portfolio_scale = 1.0
    cash_scale = 1.0
    for _ in range(column_count * 3 + 1):
        counts = state.unit_count[c.from_col : c.to_col].copy()
        bases = state.unit_base_quantities[c.from_col : c.to_col].copy()
        for offset in range(column_count):
            column = c.from_col + offset
            if exit_active[offset]:
                counts[offset] = 0
                for unit in range(params.max_units):
                    bases[offset, unit] = 0
            elif candidate_active[offset]:
                slot = counts[offset]
                bases[offset, slot] = state.candidate_base_quantity[row, column]
                counts[offset] = slot + 1
        group_scales, portfolio_scale = _risk_scales_nb(
            counts,
            inputs.asset_group_ids[c.from_col : c.to_col],
            group_count,
            params.asset_group_unit_cap,
            params.portfolio_unit_cap,
        )
        local_inputs = CallbackInputs(
            inputs.execution_open[:, c.from_col : c.to_col],
            inputs.signal_close[:, c.from_col : c.to_col],
            inputs.signal_entry_high[:, c.from_col : c.to_col],
            inputs.signal_exit_low[:, c.from_col : c.to_col],
            inputs.signal_n[:, c.from_col : c.to_col],
            inputs.paused[:, c.from_col : c.to_col],
            inputs.high_limit[:, c.from_col : c.to_col],
            inputs.low_limit[:, c.from_col : c.to_col],
            inputs.asset_group_ids[c.from_col : c.to_col],
        )
        targets, cash_scale = _cash_feasible_targets_nb(
            row,
            bases,
            counts,
            c.last_position[c.from_col : c.to_col],
            c.last_cash[c.group],
            group_scales,
            portfolio_scale,
            locked,
            local_inputs,
            params,
        )
        changed = False
        for offset in range(column_count):
            column = c.from_col + offset
            current = int(round(c.last_position[column]))
            if locked[offset] < 0 and targets[offset] < current:
                if _sell_tradability_reason_nb(row, column, inputs) != REASON_NONE:
                    locked[offset] = current
                    changed = True
            elif locked[offset] < 0 and targets[offset] > current:
                if _buy_tradability_reason_nb(row, column, inputs) != REASON_NONE:
                    locked[offset] = current
                    changed = True
            if (
                candidate_active[offset]
                and targets[offset] - current < params.lot_size
            ):
                candidate_active[offset] = False
                state.reason_codes[row, column] = REASON_ALLOCATION_CONSTRAINT
                changed = True
        if not changed:
            break

    has_effective_event = False
    for offset in range(column_count):
        if exit_active[offset] or candidate_active[offset]:
            has_effective_event = True
    if has_effective_event:
        for offset in range(column_count):
            column = c.from_col + offset
            group = inputs.asset_group_ids[column]
            state.event_group_scales[row, column] = group_scales[group]
        state.event_portfolio_scales[row] = portfolio_scale
        state.event_cash_scales[row] = cash_scale
        for offset in range(column_count):
            column = c.from_col + offset
            current = int(round(c.last_position[column]))
            action = state.action_codes[row, column]
            if action == ACTION_FULL_EXIT:
                if exit_active[offset]:
                    state.planned_quantities[row, column] = current
                continue
            delta = targets[offset] - current
            if action == ACTION_ENTRY or action == ACTION_ADDITION:
                if candidate_active[offset] and delta >= params.lot_size:
                    state.planned_quantities[row, column] = delta
                continue
            if delta <= -params.lot_size:
                state.action_codes[row, column] = ACTION_REDISTRIBUTION_SELL
                state.reason_codes[row, column] = (
                    REASON_FULL_POSITION_REDISTRIBUTION
                )
                state.requested_quantities[row, column] = -delta
                state.planned_quantities[row, column] = -delta
            elif delta >= params.lot_size:
                state.action_codes[row, column] = ACTION_REDISTRIBUTION_BUY
                state.reason_codes[row, column] = (
                    REASON_FULL_POSITION_REDISTRIBUTION
                )
                state.requested_quantities[row, column] = delta
                state.planned_quantities[row, column] = delta

    _set_call_sequence_nb(c, state)
    for column in range(c.from_col, c.to_col):
        open_price = inputs.execution_open[row, column]
        if _finite_positive(open_price):
            c.last_val_price[column] = open_price
    state.allocation_ready[row] = True
    return state, inputs, params


@njit
def order_func_nb(
    c,
    state: CallbackState,
    inputs: CallbackInputs,
    params: CallbackParams,
):
    row = c.i
    column = c.col
    action = state.action_codes[row, column]
    reason = state.reason_codes[row, column]
    if action == ACTION_NONE or reason in (
        REASON_MISSING_OPEN,
        REASON_PAUSED,
        REASON_HIGH_LIMIT,
        REASON_LOW_LIMIT,
        REASON_ALLOCATION_CONSTRAINT,
    ):
        return nb.NoOrder
    quantity = state.planned_quantities[row, column]
    if quantity <= 0:
        return nb.NoOrder
    open_price = inputs.execution_open[row, column]
    if action == ACTION_FULL_EXIT or action == ACTION_REDISTRIBUTION_SELL:
        quantity = min(quantity, int(round(c.position_now)))
        if quantity <= 0:
            return nb.NoOrder
        price = _sell_price(open_price, params.one_way_slippage)
        return nb.order_nb(
            size=-float(quantity),
            price=price,
            direction=Direction.LongOnly,
            fixed_fees=_commission(price, quantity, params.commission_multiplier),
            allow_partial=False,
        )
    price = _buy_price(open_price, params.one_way_slippage)
    return nb.order_nb(
        size=float(quantity),
        price=price,
        direction=Direction.LongOnly,
        fixed_fees=_commission(price, quantity, params.commission_multiplier),
        size_granularity=float(params.lot_size),
        allow_partial=False,
    )


@njit
def _record_candidate_unit_nb(
    row: int,
    column: int,
    fill_price: float,
    state: CallbackState,
    params: CallbackParams,
) -> None:
    slot = state.unit_count[column]
    if slot >= params.max_units:
        return
    signal_n = state.candidate_signal_n[row, column]
    state.unit_signal_n[column, slot] = signal_n
    state.unit_base_quantities[column, slot] = state.candidate_base_quantity[
        row, column
    ]
    state.unit_fill_prices[column, slot] = fill_price
    if slot == 0:
        state.initial_fill_price[column] = fill_price
        state.initial_signal_n[column] = signal_n
        state.common_stop[column] = fill_price - params.stop_n * signal_n
    else:
        candidate_stop = fill_price - params.stop_n * signal_n
        state.common_stop[column] = max(
            state.common_stop[column], candidate_stop
        )
    state.unit_count[column] = slot + 1
    state.next_add_index[column] = slot + 1


@njit
def post_order_func_nb(
    c,
    state: CallbackState,
    inputs: CallbackInputs,
    params: CallbackParams,
) -> None:
    row = c.i
    column = c.col
    action = state.action_codes[row, column]
    result = c.order_result
    if result.status == OrderStatus.Filled:
        quantity = int(round(result.size))
        state.filled_quantities[row, column] = quantity
        state.fill_prices[row, column] = result.price
        state.fees[row, column] = result.fees
        if result.side == OrderSide.Sell:
            if action == ACTION_FULL_EXIT and c.position_now <= 1e-9:
                _clear_position_state_nb(column, state)
        elif action == ACTION_ENTRY:
            _clear_position_state_nb(column, state)
            _record_candidate_unit_nb(
                row, column, result.price, state, params
            )
        elif action == ACTION_ADDITION:
            _record_candidate_unit_nb(
                row, column, result.price, state, params
            )
    elif result.status == OrderStatus.Rejected:
        state.reason_codes[row, column] = REASON_ORDER_REJECTED



@njit
def post_segment_func_nb(
    c,
    state: CallbackState,
    inputs: CallbackInputs,
    params: CallbackParams,
) -> None:
    row = c.i
    for column in range(c.from_col, c.to_col):
        state.state_quantities[row, column] = int(
            round(c.last_position[column])
        )
        state.state_common_stop[row, column] = state.common_stop[column]
        state.state_next_add_index[row, column] = state.next_add_index[column]
        state.state_unit_counts[row, column] = state.unit_count[column]
