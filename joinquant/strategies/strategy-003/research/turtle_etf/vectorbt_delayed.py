from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd
import vectorbt as vbt

from .vectorbt_callbacks import (
    ACTION_ADDITION,
    ACTION_ENTRY,
    ACTION_FULL_EXIT,
    ACTION_NONE,
    ACTION_REDISTRIBUTION_BUY,
    ACTION_REDISTRIBUTION_SELL,
    REASON_NONE,
)


ADJUST_NONE = 0
ADJUST_CASH_TRUNCATED = 1
ADJUST_HOLDING_TRUNCATED = 2
ADJUST_UNTRADABLE = 3
ADJUST_HORIZON_EXPIRED = 4


@dataclass(frozen=True)
class FrozenOrderPlan:
    planned_row_indices: np.ndarray
    action_codes: np.ndarray
    reason_codes: np.ndarray
    requested_quantities: np.ndarray
    target_quantities: np.ndarray
    signal_n: np.ndarray


@dataclass(frozen=True)
class HorizonExpiredOrder:
    planned_row_index: int
    column: int
    action_code: int
    reason_code: int
    requested_quantity: int
    target_quantity: int
    signal_n: float
    delay_days: int


@dataclass(frozen=True)
class DelayedExecutionResult:
    portfolio: object
    action_codes: np.ndarray
    reason_codes: np.ndarray
    requested_quantities: np.ndarray
    planned_quantities: np.ndarray
    filled_quantities: np.ndarray
    fill_prices: np.ndarray
    fees: np.ndarray
    state_quantities: np.ndarray
    state_common_stop: np.ndarray
    state_next_add_index: np.ndarray
    state_unit_counts: np.ndarray
    day_equity: np.ndarray
    planned_row_indices: np.ndarray
    execution_adjustment_codes: np.ndarray
    frozen_signal_n: np.ndarray
    execution_sequence: tuple[tuple[str, ...], ...]
    horizon_expired_orders: tuple[HorizonExpiredOrder, ...]


def _readonly(values: np.ndarray, dtype: str) -> np.ndarray:
    result = np.ascontiguousarray(values, dtype=dtype)
    result.setflags(write=False)
    return result


def _matrix(value: object, shape: tuple[int, int], name: str) -> np.ndarray:
    result = np.asarray(value)
    if result.shape != shape:
        raise ValueError(f"invalid frozen order source shape: {name}")
    return result


def freeze_order_plan(inputs: object, immediate: object) -> FrozenOrderPlan:
    signal_n = np.asarray(getattr(inputs, "signal_n"), dtype=np.float64)
    shape = signal_n.shape
    actions = _matrix(immediate.action_codes, shape, "action_codes")
    reasons = _matrix(immediate.reason_codes, shape, "reason_codes")
    requested = _matrix(
        immediate.requested_quantities, shape, "requested_quantities"
    )
    planned = _matrix(immediate.planned_quantities, shape, "planned_quantities")
    filled = _matrix(immediate.filled_quantities, shape, "filled_quantities")
    valid = filled > 0
    row_indices = np.broadcast_to(
        np.arange(shape[0], dtype=np.int64)[:, None], shape
    )
    targets = np.where(valid, np.where(planned > 0, planned, filled), 0)
    return FrozenOrderPlan(
        planned_row_indices=_readonly(
            np.where(valid, row_indices, -1), "int64"
        ),
        action_codes=_readonly(np.where(valid, actions, ACTION_NONE), "int16"),
        reason_codes=_readonly(np.where(valid, reasons, REASON_NONE), "int16"),
        requested_quantities=_readonly(np.where(valid, requested, 0), "int64"),
        target_quantities=_readonly(targets, "int64"),
        signal_n=_readonly(np.where(valid, signal_n, np.nan), "float64"),
    )


def _commission(price: float, quantity: int, multiplier: float) -> float:
    return max(5.0, price * quantity * 0.000085) * multiplier


def _priority(action: int) -> int:
    if action == ACTION_FULL_EXIT:
        return 0
    if action == ACTION_REDISTRIBUTION_SELL:
        return 1
    if action in (ACTION_ENTRY, ACTION_ADDITION):
        return 2
    if action == ACTION_REDISTRIBUTION_BUY:
        return 3
    return 4


def _is_tradable(inputs: object, row: int, column: int, action: int) -> bool:
    open_price = float(inputs.execution_open[row, column])
    if not np.isfinite(open_price) or open_price <= 0.0:
        return False
    if bool(inputs.paused[row, column]):
        return False
    if action in (ACTION_FULL_EXIT, ACTION_REDISTRIBUTION_SELL):
        low_limit = float(inputs.low_limit[row, column])
        return not np.isfinite(low_limit) or open_price > low_limit
    high_limit = float(inputs.high_limit[row, column])
    return not np.isfinite(high_limit) or open_price < high_limit


def _affordable_quantity(
    *,
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


def run_delayed_execution(
    inputs: object,
    plan: FrozenOrderPlan,
    *,
    initial_cash: float,
    lot_size: int,
    stop_n: float,
    commission_multiplier: float,
    one_way_slippage: float,
    delay_days: int,
) -> DelayedExecutionResult:
    if delay_days <= 0:
        raise ValueError("delayed execution requires a positive delay")
    if lot_size <= 0 or initial_cash <= 0.0:
        raise ValueError("delayed execution cash and lot size must be positive")
    opens = np.asarray(inputs.execution_open, dtype=np.float64)
    close = np.asarray(inputs.close, dtype=np.float64)
    if opens.shape != close.shape or opens.shape != plan.action_codes.shape:
        raise ValueError("delayed execution input shapes differ")
    rows, columns = opens.shape
    securities = tuple(str(value) for value in inputs.securities)
    if len(securities) != columns:
        raise ValueError("delayed execution securities do not match columns")

    actions = np.zeros((rows, columns), dtype=np.int16)
    reasons = np.zeros((rows, columns), dtype=np.int16)
    requested = np.zeros((rows, columns), dtype=np.int64)
    planned = np.zeros((rows, columns), dtype=np.int64)
    filled = np.zeros((rows, columns), dtype=np.int64)
    fill_prices = np.full((rows, columns), np.nan, dtype=np.float64)
    fees = np.zeros((rows, columns), dtype=np.float64)
    state_quantities = np.zeros((rows, columns), dtype=np.int64)
    state_common_stop = np.full((rows, columns), np.nan, dtype=np.float64)
    state_next_add = np.zeros((rows, columns), dtype=np.int64)
    state_unit_counts = np.zeros((rows, columns), dtype=np.int64)
    planned_rows = np.full((rows, columns), -1, dtype=np.int64)
    adjustments = np.zeros((rows, columns), dtype=np.int16)
    frozen_n = np.full((rows, columns), np.nan, dtype=np.float64)
    sequences: list[tuple[str, ...]] = []
    expired: list[HorizonExpiredOrder] = []

    cash_now = float(initial_cash)
    positions = np.zeros(columns, dtype=np.int64)
    common_stop = np.full(columns, np.nan, dtype=np.float64)
    next_add = np.zeros(columns, dtype=np.int64)
    unit_counts = np.zeros(columns, dtype=np.int64)
    last_close = np.full(columns, np.nan, dtype=np.float64)
    values = np.full(rows, np.nan, dtype=np.float64)
    cash_values = np.full(rows, np.nan, dtype=np.float64)
    call_seq = np.empty((rows, columns), dtype=np.int64)

    for planned_row, column in zip(*np.nonzero(plan.action_codes != ACTION_NONE)):
        if planned_row + delay_days >= rows:
            expired.append(
                HorizonExpiredOrder(
                    planned_row_index=int(planned_row),
                    column=int(column),
                    action_code=int(plan.action_codes[planned_row, column]),
                    reason_code=int(plan.reason_codes[planned_row, column]),
                    requested_quantity=int(
                        plan.requested_quantities[planned_row, column]
                    ),
                    target_quantity=int(plan.target_quantities[planned_row, column]),
                    signal_n=float(plan.signal_n[planned_row, column]),
                    delay_days=delay_days,
                )
            )

    for execution_row in range(rows):
        source_row = execution_row - delay_days
        queue: list[int] = []
        if source_row >= 0:
            queue = [
                column
                for column in range(columns)
                if plan.action_codes[source_row, column] != ACTION_NONE
            ]
            queue.sort(
                key=lambda column: (
                    _priority(int(plan.action_codes[source_row, column])),
                    securities[column],
                )
            )
        sequences.append(
            tuple(
                f"queued-from-row-{source_row}:{securities[column]}"
                for column in queue
            )
        )
        ordered_columns = queue + sorted(
            (column for column in range(columns) if column not in queue),
            key=lambda column: securities[column],
        )
        for rank, column in enumerate(ordered_columns):
            call_seq[execution_row, rank] = column
        for column in queue:
            action = int(plan.action_codes[source_row, column])
            target = int(plan.target_quantities[source_row, column])
            actions[execution_row, column] = action
            reasons[execution_row, column] = int(
                plan.reason_codes[source_row, column]
            )
            requested[execution_row, column] = int(
                plan.requested_quantities[source_row, column]
            )
            planned[execution_row, column] = target
            planned_rows[execution_row, column] = source_row
            frozen_n[execution_row, column] = float(plan.signal_n[source_row, column])
            if not _is_tradable(inputs, execution_row, column, action):
                adjustments[execution_row, column] = ADJUST_UNTRADABLE
                continue

            open_price = float(opens[execution_row, column])
            if action in (ACTION_FULL_EXIT, ACTION_REDISTRIBUTION_SELL):
                quantity = min(target, int(positions[column]))
                if quantity < target:
                    adjustments[execution_row, column] = ADJUST_HOLDING_TRUNCATED
                if quantity <= 0:
                    continue
                price = open_price * (1.0 - one_way_slippage)
                fee = _commission(price, quantity, commission_multiplier)
                cash_now += price * quantity - fee
                positions[column] -= quantity
                if positions[column] == 0:
                    common_stop[column] = np.nan
                    next_add[column] = 0
                    if action == ACTION_FULL_EXIT:
                        unit_counts[column] = 0
            else:
                price = open_price * (1.0 + one_way_slippage)
                quantity = _affordable_quantity(
                    cash=cash_now,
                    target=target,
                    lot_size=lot_size,
                    price=price,
                    commission_multiplier=commission_multiplier,
                )
                if quantity < target:
                    adjustments[execution_row, column] = ADJUST_CASH_TRUNCATED
                if quantity <= 0:
                    continue
                fee = _commission(price, quantity, commission_multiplier)
                cash_now -= price * quantity + fee
                positions[column] += quantity
                if action in (ACTION_ENTRY, ACTION_ADDITION):
                    signal_n = float(plan.signal_n[source_row, column])
                    candidate_stop = price - stop_n * signal_n
                    if action == ACTION_ENTRY or not np.isfinite(common_stop[column]):
                        common_stop[column] = candidate_stop
                        next_add[column] = 1
                        unit_counts[column] = 1
                    else:
                        common_stop[column] = max(common_stop[column], candidate_stop)
                        next_add[column] += 1
                        unit_counts[column] += 1
            filled[execution_row, column] = quantity
            fill_prices[execution_row, column] = price
            fees[execution_row, column] = fee

        state_quantities[execution_row] = positions
        state_common_stop[execution_row] = common_stop
        state_next_add[execution_row] = next_add
        state_unit_counts[execution_row] = unit_counts
        for column in range(columns):
            price = float(close[execution_row, column])
            if np.isfinite(price) and price > 0.0:
                last_close[column] = price
        held_values = np.where(positions > 0, positions * last_close, 0.0)
        if np.any(~np.isfinite(held_values)):
            raise ValueError("delayed position has no valid valuation price")
        values[execution_row] = cash_now + float(held_values.sum())
        cash_values[execution_row] = cash_now

    order_sizes = np.full((rows, columns), np.nan, dtype=np.float64)
    for row, column in zip(*np.nonzero(filled > 0)):
        direction = -1.0 if actions[row, column] in (
            ACTION_FULL_EXIT,
            ACTION_REDISTRIBUTION_SELL,
        ) else 1.0
        order_sizes[row, column] = direction * float(filled[row, column])
    close_frame = pd.DataFrame(
        close,
        index=pd.DatetimeIndex(np.asarray(inputs.dates, dtype="datetime64[ns]")),
        columns=securities,
    )
    portfolio = vbt.Portfolio.from_orders(
        close_frame,
        size=order_sizes,
        price=fill_prices,
        fixed_fees=fees,
        direction="longonly",
        init_cash=initial_cash,
        cash_sharing=True,
        group_by=True,
        call_seq=call_seq,
        update_value=True,
        ffill_val_price=True,
        max_orders=rows * columns,
        freq="1D",
    )
    vectorbt_values = np.asarray(portfolio.value(), dtype=np.float64).reshape(-1)
    vectorbt_cash = np.asarray(portfolio.cash(), dtype=np.float64).reshape(-1)
    if not np.allclose(vectorbt_values, values, rtol=0.0, atol=0.02):
        differences = np.abs(vectorbt_values - values)
        row = int(np.nanargmax(differences))
        raise ValueError(
            "vectorbt delayed portfolio value does not reconcile: "
            f"row={row} expected={values[row]} actual={vectorbt_values[row]} "
            f"difference={differences[row]} "
            f"expected_orders={int(np.count_nonzero(filled))} "
            f"actual_orders={int(portfolio.orders.count())}"
        )
    if not np.allclose(vectorbt_cash, cash_values, rtol=0.0, atol=0.02):
        raise ValueError("vectorbt delayed portfolio cash does not reconcile")
    return DelayedExecutionResult(
        portfolio=portfolio,
        action_codes=_readonly(actions, "int16"),
        reason_codes=_readonly(reasons, "int16"),
        requested_quantities=_readonly(requested, "int64"),
        planned_quantities=_readonly(planned, "int64"),
        filled_quantities=_readonly(filled, "int64"),
        fill_prices=_readonly(fill_prices, "float64"),
        fees=_readonly(fees, "float64"),
        state_quantities=_readonly(state_quantities, "int64"),
        state_common_stop=_readonly(state_common_stop, "float64"),
        state_next_add_index=_readonly(state_next_add, "int64"),
        state_unit_counts=_readonly(state_unit_counts, "int64"),
        day_equity=_readonly(values, "float64"),
        planned_row_indices=_readonly(planned_rows, "int64"),
        execution_adjustment_codes=_readonly(adjustments, "int16"),
        frozen_signal_n=_readonly(frozen_n, "float64"),
        execution_sequence=tuple(sequences),
        horizon_expired_orders=tuple(expired),
    )
