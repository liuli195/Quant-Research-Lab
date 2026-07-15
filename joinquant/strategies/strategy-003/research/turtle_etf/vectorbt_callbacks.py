from __future__ import annotations

from collections import namedtuple

import numpy as np
from numba import njit
from vectorbt.portfolio import nb
from vectorbt.portfolio.enums import Direction, OrderSide, OrderStatus


ACTION_NONE = 0
ACTION_FULL_EXIT = 1
ACTION_RISK_REDUCTION = 2
ACTION_ENTRY = 3
ACTION_ADDITION = 4

REASON_NONE = 0
REASON_ENTRY_BREAKOUT = 1
REASON_FIXED_ADDITION_LEVEL = 2
REASON_PROTECTIVE_STOP = 3
REASON_TREND_EXIT = 4
REASON_TARGET_VOLATILITY_REDUCTION = 5
REASON_MISSING_OPEN = 6
REASON_PAUSED = 7
REASON_HIGH_LIMIT = 8
REASON_LOW_LIMIT = 9
REASON_ALLOCATION_CONSTRAINT = 10
REASON_HELD_RISK_INPUT_MISSING = 11
REASON_ORDER_REJECTED = 12


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
        "covariance",
        "covariance_eligible",
        "asset_group_ids",
    ),
)

CallbackParams = namedtuple(
    "CallbackParams",
    (
        "lot_size",
        "risk_per_unit",
        "add_step_n",
        "stop_n",
        "security_risk_cap",
        "security_value_cap",
        "asset_group_risk_cap",
        "asset_group_value_cap",
        "portfolio_risk_cap",
        "portfolio_value_cap",
        "target_volatility",
        "risk_reduction_target_volatility",
        "commission_multiplier",
        "one_way_slippage",
    ),
)

CallbackState = namedtuple(
    "CallbackState",
    (
        "standard_unit",
        "signal_n",
        "initial_fill_price",
        "common_stop",
        "next_add_index",
        "batch_count",
        "batch_quantities",
        "batch_prices",
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
        "day_equity",
        "allocation_ready",
    ),
)


_MASK_INVALID_LOT = 1 << 0
_MASK_STANDARD_UNIT = 1 << 1
_MASK_TRANSITION = 1 << 2
_MASK_CASH = 1 << 3
_MASK_COVARIANCE = 1 << 4
_MASK_SECURITY_VALUE = 1 << 5
_MASK_GROUP_VALUE = 1 << 6
_MASK_PORTFOLIO_VALUE = 1 << 7
_MASK_SECURITY_RISK = 1 << 8
_MASK_GROUP_RISK = 1 << 9
_MASK_PORTFOLIO_RISK = 1 << 10
_MASK_TARGET_VOLATILITY = 1 << 11
_NON_MONOTONIC_MASK = (
    _MASK_PORTFOLIO_RISK | _MASK_TARGET_VOLATILITY
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
def _portfolio_volatility_nb(
    row: int,
    positions: np.ndarray,
    prices: np.ndarray,
    equity: float,
    inputs: CallbackInputs,
) -> float:
    if not _finite_positive(equity):
        return np.nan
    any_position = False
    variance = 0.0
    for left in range(positions.shape[0]):
        if positions[left] <= 0.0:
            continue
        any_position = True
        if (
            not inputs.covariance_eligible[row, left]
            or not _finite_positive(prices[left])
        ):
            return np.nan
        left_weight = positions[left] * prices[left] / equity
        for right in range(positions.shape[0]):
            if positions[right] <= 0.0:
                continue
            covariance = inputs.covariance[row, left, right]
            if (
                not inputs.covariance_eligible[row, right]
                or not _finite_positive(prices[right])
                or not np.isfinite(covariance)
            ):
                return np.nan
            right_weight = positions[right] * prices[right] / equity
            variance += left_weight * covariance * right_weight
    if not any_position:
        return 0.0
    if variance < -1e-15:
        return np.nan
    return np.sqrt(max(0.0, variance)) * np.sqrt(252.0)


@njit
def _tradability_reason_nb(
    row: int,
    column: int,
    action: int,
    inputs: CallbackInputs,
) -> int:
    open_price = inputs.execution_open[row, column]
    if not _finite_positive(open_price):
        return REASON_MISSING_OPEN
    if inputs.paused[row, column]:
        return REASON_PAUSED
    if action == ACTION_FULL_EXIT or action == ACTION_RISK_REDUCTION:
        low_limit = inputs.low_limit[row, column]
        if np.isfinite(low_limit) and open_price <= low_limit:
            return REASON_LOW_LIMIT
    elif action == ACTION_ENTRY or action == ACTION_ADDITION:
        high_limit = inputs.high_limit[row, column]
        if np.isfinite(high_limit) and open_price >= high_limit:
            return REASON_HIGH_LIMIT
    return REASON_NONE


@njit
def _clear_position_state_nb(column: int, state: CallbackState) -> None:
    state.standard_unit[column] = 0
    state.signal_n[column] = np.nan
    state.initial_fill_price[column] = np.nan
    state.common_stop[column] = np.nan
    state.next_add_index[column] = 0
    count = state.batch_count[column]
    for batch in range(count):
        state.batch_quantities[column, batch] = 0
        state.batch_prices[column, batch] = np.nan
    state.batch_count[column] = 0


@njit
def _reduce_batches_nb(column: int, quantity: int, state: CallbackState) -> None:
    remaining = quantity
    batch = state.batch_count[column] - 1
    while batch >= 0 and remaining > 0:
        available = state.batch_quantities[column, batch]
        removed = min(available, remaining)
        state.batch_quantities[column, batch] = available - removed
        remaining -= removed
        if state.batch_quantities[column, batch] == 0:
            state.batch_prices[column, batch] = np.nan
            state.batch_count[column] -= 1
        batch -= 1


@njit
def _allocation_spend_nb(
    row: int,
    quantities: np.ndarray,
    inputs: CallbackInputs,
    params: CallbackParams,
) -> float:
    spent = 0.0
    for column in range(quantities.shape[0]):
        quantity = quantities[column]
        if quantity <= 0:
            continue
        price = _buy_price(
            inputs.execution_open[row, column], params.one_way_slippage
        )
        spent += price * quantity + _commission(
            price, quantity, params.commission_multiplier
        )
    return spent


@njit
def _feasibility_mask_nb(
    row: int,
    quantities: np.ndarray,
    positions: np.ndarray,
    cash: float,
    state: CallbackState,
    inputs: CallbackInputs,
    params: CallbackParams,
) -> int:
    equity = state.day_equity[row]
    if not _finite_positive(equity):
        return _MASK_CASH
    if _allocation_spend_nb(row, quantities, inputs, params) > cash + 1e-9:
        return _MASK_CASH

    mask = 0
    column_count = quantities.shape[0]
    group_values = np.zeros(column_count, dtype=np.float64)
    group_risks = np.zeros(column_count, dtype=np.float64)
    group_increases = np.zeros(column_count, dtype=np.bool_)
    projected_quantities = np.zeros(column_count, dtype=np.float64)
    prices = np.empty(column_count, dtype=np.float64)
    total_value = 0.0
    total_risk = 0.0

    for column in range(column_count):
        allocated = quantities[column]
        action = state.action_codes[row, column]
        position = positions[column]
        if allocated > 0:
            if allocated % params.lot_size != 0:
                mask |= _MASK_INVALID_LOT
            if allocated > state.requested_quantities[row, column]:
                mask |= _MASK_STANDARD_UNIT
            if action == ACTION_ENTRY and position > 0.0:
                mask |= _MASK_TRANSITION
            if action == ACTION_ADDITION and position <= 0.0:
                mask |= _MASK_TRANSITION

        projected = position + allocated
        projected_quantities[column] = projected
        if projected <= 0.0:
            prices[column] = np.nan
            continue
        open_price = inputs.execution_open[row, column]
        if not _finite_positive(open_price):
            mask |= _MASK_COVARIANCE
            prices[column] = np.nan
            continue
        price = _buy_price(open_price, params.one_way_slippage)
        prices[column] = price
        stop = state.common_stop[column]
        if allocated > 0:
            candidate_stop = price - params.stop_n * inputs.signal_n[row, column]
            if position <= 0.0 or not np.isfinite(stop):
                stop = candidate_stop
            else:
                stop = max(stop, candidate_stop)
        if not np.isfinite(stop):
            mask |= _MASK_TRANSITION
            continue
        planned_risk = 0.0
        for batch in range(state.batch_count[column]):
            planned_risk += (
                state.batch_prices[column, batch] - stop
            ) * state.batch_quantities[column, batch]
        if allocated > 0:
            planned_risk += (price - stop) * allocated
        planned_risk = max(0.0, planned_risk)
        value = price * projected
        group = inputs.asset_group_ids[column]
        group_values[group] += value
        group_risks[group] += planned_risk
        if allocated > 0:
            group_increases[group] = True
        total_value += value
        total_risk += planned_risk
        if allocated > 0 and value > equity * params.security_value_cap + 1e-9:
            mask |= _MASK_SECURITY_VALUE
        if (
            allocated > 0
            and planned_risk > equity * params.security_risk_cap + 1e-9
        ):
            mask |= _MASK_SECURITY_RISK

    for group in range(column_count):
        if (
            group_increases[group]
            and group_values[group] > equity * params.asset_group_value_cap + 1e-9
        ):
            mask |= _MASK_GROUP_VALUE
        if (
            group_increases[group]
            and group_risks[group] > equity * params.asset_group_risk_cap + 1e-9
        ):
            mask |= _MASK_GROUP_RISK
    if total_value > equity * params.portfolio_value_cap + 1e-9:
        mask |= _MASK_PORTFOLIO_VALUE
    if total_risk > equity * params.portfolio_risk_cap + 1e-9:
        mask |= _MASK_PORTFOLIO_RISK

    for left in range(column_count):
        if projected_quantities[left] <= 0.0:
            continue
        if not inputs.covariance_eligible[row, left]:
            mask |= _MASK_COVARIANCE
            continue
        for right in range(column_count):
            if projected_quantities[right] <= 0.0:
                continue
            if (
                not inputs.covariance_eligible[row, right]
                or not np.isfinite(inputs.covariance[row, left, right])
            ):
                mask |= _MASK_COVARIANCE

    if mask & _MASK_COVARIANCE == 0:
        volatility = _portfolio_volatility_nb(
            row, projected_quantities, prices, equity, inputs
        )
        if not np.isfinite(volatility):
            mask |= _MASK_COVARIANCE
        elif volatility > params.target_volatility + 1e-12:
            mask |= _MASK_TARGET_VOLATILITY
    return mask


@njit
def _hamilton_quantities_nb(
    base: np.ndarray,
    active: np.ndarray,
    extra_lots: int,
    requested: np.ndarray,
    lot: int,
) -> np.ndarray:
    result = base.copy()
    column_count = base.shape[0]
    remaining_lots = np.zeros(column_count, dtype=np.int64)
    total_remaining = 0
    for column in range(column_count):
        if active[column]:
            remaining_lots[column] = max(
                0, (requested[column] - base[column]) // lot
            )
            total_remaining += remaining_lots[column]
    if extra_lots <= 0 or total_remaining <= 0:
        return result

    floor_lots = np.zeros(column_count, dtype=np.int64)
    remainders = np.full(column_count, -1, dtype=np.int64)
    allocated_lots = 0
    for column in range(column_count):
        if not active[column]:
            continue
        numerator = extra_lots * remaining_lots[column]
        floor_lots[column] = numerator // total_remaining
        remainders[column] = numerator % total_remaining
        allocated_lots += floor_lots[column]
    remainder_lots = extra_lots - allocated_lots
    selected = np.zeros(column_count, dtype=np.bool_)
    while remainder_lots > 0:
        best = -1
        best_remainder = -1
        for column in range(column_count):
            if (
                active[column]
                and not selected[column]
                and floor_lots[column] < remaining_lots[column]
                and remainders[column] > best_remainder
            ):
                best = column
                best_remainder = remainders[column]
        if best < 0:
            break
        floor_lots[best] += 1
        selected[best] = True
        remainder_lots -= 1
    for column in range(column_count):
        result[column] += floor_lots[column] * lot
    return result


@njit
def _maximum_hamilton_allocation_nb(
    row: int,
    base: np.ndarray,
    active: np.ndarray,
    requested: np.ndarray,
    positions: np.ndarray,
    cash: float,
    state: CallbackState,
    inputs: CallbackInputs,
    params: CallbackParams,
) -> np.ndarray:
    maximum_lots = 0
    cheapest_lot = np.inf
    for column in range(active.shape[0]):
        if not active[column]:
            continue
        maximum_lots += max(0, requested[column] - base[column]) // params.lot_size
        price = _buy_price(
            inputs.execution_open[row, column], params.one_way_slippage
        )
        cheapest_lot = min(cheapest_lot, price * params.lot_size)
    available_cash = max(
        0.0, cash - _allocation_spend_nb(row, base, inputs, params)
    )
    if _finite_positive(cheapest_lot):
        maximum_lots = min(maximum_lots, int(available_cash // cheapest_lot))
    for candidate_lots in range(maximum_lots, 0, -1):
        proposed = _hamilton_quantities_nb(
            base, active, candidate_lots, requested, params.lot_size
        )
        if _allocation_spend_nb(row, proposed, inputs, params) > cash + 1e-9:
            continue
        if (
            _feasibility_mask_nb(
                row, proposed, positions, cash, state, inputs, params
            )
            == 0
        ):
            return proposed
    return base.copy()


@njit
def _allocate_a1_nb(
    c,
    state: CallbackState,
    inputs: CallbackInputs,
    params: CallbackParams,
) -> None:
    row = c.i
    column_count = c.target_shape[1]
    active = np.zeros(column_count, dtype=np.bool_)
    requested = state.requested_quantities[row].copy()
    any_candidate = False
    for column in range(column_count):
        action = state.action_codes[row, column]
        reason = state.reason_codes[row, column]
        if (
            (action == ACTION_ENTRY or action == ACTION_ADDITION)
            and reason
            in (
                REASON_NONE,
                REASON_ENTRY_BREAKOUT,
                REASON_FIXED_ADDITION_LEVEL,
            )
            and requested[column] >= params.lot_size
        ):
            active[column] = True
            any_candidate = True
    if not any_candidate:
        state.allocation_ready[row] = True
        return

    for column in range(column_count):
        if c.last_position[column] > 0.0 and (
            not _finite_positive(inputs.execution_open[row, column])
            or not _finite_positive(inputs.signal_close[row, column])
            or not inputs.covariance_eligible[row, column]
        ):
            for candidate in range(column_count):
                if active[candidate]:
                    state.reason_codes[row, candidate] = (
                        REASON_HELD_RISK_INPUT_MISSING
                    )
            state.allocation_ready[row] = True
            return

    quantities = np.zeros(column_count, dtype=np.int64)
    while True:
        remaining_active = False
        for column in range(column_count):
            if active[column] and quantities[column] < requested[column]:
                remaining_active = True
            else:
                active[column] = False
        if not remaining_active:
            break
        quantities = _maximum_hamilton_allocation_nb(
            row,
            quantities,
            active,
            requested,
            c.last_position,
            c.cash_now,
            state,
            inputs,
            params,
        )
        remaining_lots = 0
        for column in range(column_count):
            if active[column]:
                remaining_lots += max(
                    0, requested[column] - quantities[column]
                ) // params.lot_size
        if remaining_lots == 0:
            break
        blocked = False
        for column in range(column_count):
            if not active[column]:
                continue
            next_quantities = quantities.copy()
            next_quantities[column] += params.lot_size
            mask = _feasibility_mask_nb(
                row,
                next_quantities,
                c.last_position,
                c.cash_now,
                state,
                inputs,
                params,
            )
            if mask & ~_NON_MONOTONIC_MASK:
                active[column] = False
                blocked = True
        if not blocked:
            break

    for column in range(column_count):
        state.planned_quantities[row, column] = quantities[column]
        if active[column] or state.action_codes[row, column] in (
            ACTION_ENTRY,
            ACTION_ADDITION,
        ):
            if quantities[column] == 0 and state.reason_codes[row, column] in (
                REASON_NONE,
                REASON_ENTRY_BREAKOUT,
                REASON_FIXED_ADDITION_LEVEL,
            ):
                state.reason_codes[row, column] = REASON_ALLOCATION_CONSTRAINT
    state.allocation_ready[row] = True


@njit
def pre_sim_func_nb(
    c,
    state: CallbackState,
    inputs: CallbackInputs,
    params: CallbackParams,
):
    state.standard_unit[:] = 0
    state.signal_n[:] = np.nan
    state.initial_fill_price[:] = np.nan
    state.common_stop[:] = np.nan
    state.next_add_index[:] = 0
    state.batch_count[:] = 0
    state.batch_quantities[:, :] = 0
    state.batch_prices[:, :] = np.nan
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
    state.day_equity[:] = np.nan
    state.allocation_ready[:] = False
    return state, inputs, params


@njit
def pre_segment_func_nb(
    c,
    state: CallbackState,
    inputs: CallbackInputs,
    params: CallbackParams,
):
    row = c.i
    equity = c.last_value[c.group]
    state.day_equity[row] = equity
    state.allocation_ready[row] = False
    for column in range(c.from_col, c.to_col):
        state.action_codes[row, column] = ACTION_NONE
        state.reason_codes[row, column] = REASON_NONE
        state.requested_quantities[row, column] = 0
        state.planned_quantities[row, column] = 0

    for column in range(c.from_col, c.to_col):
        position = c.last_position[column]
        close = inputs.signal_close[row, column]
        if position <= 0.0 or not _finite_positive(close):
            continue
        if np.isfinite(state.common_stop[column]) and close <= state.common_stop[column]:
            state.action_codes[row, column] = ACTION_FULL_EXIT
            state.reason_codes[row, column] = REASON_PROTECTIVE_STOP
            state.requested_quantities[row, column] = int(round(position))
        elif (
            np.isfinite(inputs.signal_exit_low[row, column])
            and close < inputs.signal_exit_low[row, column]
        ):
            state.action_codes[row, column] = ACTION_FULL_EXIT
            state.reason_codes[row, column] = REASON_TREND_EXIT
            state.requested_quantities[row, column] = int(round(position))

    volatility = _portfolio_volatility_nb(
        row, c.last_position, inputs.signal_close[row], equity, inputs
    )
    if np.isfinite(volatility) and volatility > params.target_volatility:
        scale = params.risk_reduction_target_volatility / volatility
        for column in range(c.from_col, c.to_col):
            position = c.last_position[column]
            if (
                position <= 0.0
                or state.action_codes[row, column] == ACTION_FULL_EXIT
            ):
                continue
            target = (
                int((position * scale) // params.lot_size) * params.lot_size
            )
            reduction = int(round(position)) - target
            if reduction > 0:
                state.action_codes[row, column] = ACTION_RISK_REDUCTION
                state.reason_codes[row, column] = (
                    REASON_TARGET_VOLATILITY_REDUCTION
                )
                state.requested_quantities[row, column] = reduction

    for column in range(c.from_col, c.to_col):
        if state.action_codes[row, column] != ACTION_NONE:
            continue
        close = inputs.signal_close[row, column]
        if not _finite_positive(close):
            continue
        position = c.last_position[column]
        if position > 0.0:
            if (
                _finite_positive(state.signal_n[column])
                and state.standard_unit[column] > 0
                and close
                >= state.initial_fill_price[column]
                + state.next_add_index[column]
                * params.add_step_n
                * state.signal_n[column]
            ):
                state.action_codes[row, column] = ACTION_ADDITION
                state.reason_codes[row, column] = REASON_FIXED_ADDITION_LEVEL
                state.requested_quantities[row, column] = state.standard_unit[column]
        elif (
            _finite_positive(inputs.signal_n[row, column])
            and np.isfinite(inputs.signal_entry_high[row, column])
            and close > inputs.signal_entry_high[row, column]
        ):
            quantity = int(
                equity
                * params.risk_per_unit
                / (2.0 * inputs.signal_n[row, column])
            )
            if quantity > 0:
                state.action_codes[row, column] = ACTION_ENTRY
                state.reason_codes[row, column] = REASON_ENTRY_BREAKOUT
                state.requested_quantities[row, column] = quantity

    for column in range(c.from_col, c.to_col):
        action = state.action_codes[row, column]
        if action != ACTION_NONE:
            tradability = _tradability_reason_nb(row, column, action, inputs)
            if tradability != REASON_NONE:
                state.reason_codes[row, column] = tradability

    call_index = 0
    for category in range(4):
        for column in range(c.from_col, c.to_col):
            action = state.action_codes[row, column]
            actual_category = 3
            if action == ACTION_FULL_EXIT:
                actual_category = 0
            elif action == ACTION_RISK_REDUCTION:
                actual_category = 1
            elif action == ACTION_ENTRY or action == ACTION_ADDITION:
                actual_category = 2
            if actual_category == category:
                c.call_seq_now[call_index] = column - c.from_col
                call_index += 1

    for column in range(c.from_col, c.to_col):
        open_price = inputs.execution_open[row, column]
        if _finite_positive(open_price):
            c.last_val_price[column] = open_price
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
    if action == ACTION_NONE:
        return nb.NoOrder
    if reason in (
        REASON_MISSING_OPEN,
        REASON_PAUSED,
        REASON_HIGH_LIMIT,
        REASON_LOW_LIMIT,
        REASON_HELD_RISK_INPUT_MISSING,
    ):
        return nb.NoOrder

    open_price = inputs.execution_open[row, column]
    if action == ACTION_FULL_EXIT or action == ACTION_RISK_REDUCTION:
        quantity = min(
            state.requested_quantities[row, column], int(round(c.position_now))
        )
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

    if not state.allocation_ready[row]:
        _allocate_a1_nb(c, state, inputs, params)
    quantity = state.planned_quantities[row, column]
    if quantity <= 0:
        if state.reason_codes[row, column] == REASON_NONE:
            state.reason_codes[row, column] = REASON_ALLOCATION_CONSTRAINT
        return nb.NoOrder
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
            _reduce_batches_nb(column, quantity, state)
            if c.position_now <= 1e-9 or action == ACTION_FULL_EXIT:
                _clear_position_state_nb(column, state)
        elif action == ACTION_ENTRY:
            _clear_position_state_nb(column, state)
            state.standard_unit[column] = state.requested_quantities[row, column]
            state.signal_n[column] = inputs.signal_n[row, column]
            state.initial_fill_price[column] = result.price
            state.common_stop[column] = result.price - params.stop_n * state.signal_n[column]
            state.next_add_index[column] = 1
            state.batch_quantities[column, 0] = quantity
            state.batch_prices[column, 0] = result.price
            state.batch_count[column] = 1
        elif action == ACTION_ADDITION:
            batch = state.batch_count[column]
            state.batch_quantities[column, batch] = quantity
            state.batch_prices[column, batch] = result.price
            state.batch_count[column] = batch + 1
            candidate_stop = result.price - params.stop_n * state.signal_n[column]
            state.common_stop[column] = max(
                state.common_stop[column], candidate_stop
            )
            state.next_add_index[column] += 1
    elif result.status == OrderStatus.Rejected:
        state.reason_codes[row, column] = REASON_ORDER_REJECTED

    if c.call_idx == c.group_len - 1:
        for tracked in range(c.from_col, c.to_col):
            state.state_quantities[row, tracked] = int(
                round(c.last_position[tracked])
            )
            state.state_common_stop[row, tracked] = state.common_stop[tracked]
            state.state_next_add_index[row, tracked] = state.next_add_index[tracked]
