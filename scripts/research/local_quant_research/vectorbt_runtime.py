from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from types import MappingProxyType
from typing import Callable

import numpy as np
import pandas as pd
import vectorbt as vbt
from numba import njit
from vectorbt.portfolio import nb
from vectorbt.portfolio.enums import Direction, OrderSide, OrderStatus, TradeStatus

from .contracts import (
    FILL_ACCEPTED,
    FILL_IGNORED,
    FILL_REJECTED,
    SIDE_BUY,
    SIDE_NONE,
    SIDE_SELL,
    ExecutionRun,
    FillEvent,
    LedgerInput,
    OrderProgram,
    SegmentView,
)


_ORDER_DTYPE = np.dtype(
    [
        ("match_time", "U32"),
        ("pindex", "i8"),
        ("cancel_time", "U32"),
        ("action", "U16"),
        ("limit_price", "f8"),
        ("comment", "U64"),
        ("entrust_time", "U32"),
        ("finish_time", "U32"),
        ("side", "U16"),
        ("price", "f8"),
        ("commission", "f8"),
        ("gains", "f8"),
        ("type", "U16"),
        ("time", "U32"),
        ("security_name", "U64"),
        ("security", "U64"),
        ("filled", "i8"),
        ("amount", "i8"),
        ("status", "U16"),
    ]
)
_ASSET_DTYPE = np.dtype(
    [
        ("pindex", "i8"),
        ("avg_cost", "f8"),
        ("margin", "f8"),
        ("amount", "f8"),
        ("today_amount", "i8"),
        ("hold_cost", "f8"),
        ("side", "U16"),
        ("price", "f8"),
        ("gains", "f8"),
        ("daily_gains", "f8"),
        ("closeable_amount", "i8"),
        ("time", "U32"),
        ("security_name", "U64"),
        ("security", "U64"),
    ]
)
_CASH_DTYPE = np.dtype([("time", "U32"), ("cash", "f8")])
_VALUE_DTYPE = np.dtype(
    [
        ("time", "U32"),
        ("total_value", "f8"),
        ("returns", "f8"),
        ("benchmark_returns", "f8"),
    ]
)
_RETURNS_DTYPE = np.dtype([("time", "U32"), ("returns", "f8")])


def _readonly(value: np.ndarray) -> np.ndarray:
    array = np.asarray(value)
    if not array.flags.owndata or not array.flags.c_contiguous:
        array = np.array(array, copy=True, order="C")
    array.setflags(write=False)
    return array


def _times(dates: np.ndarray, suffix: str) -> np.ndarray:
    days = np.datetime_as_string(
        np.asarray(dates).astype("datetime64[ns]"),
        unit="D",
    )
    return np.char.add(days, suffix)


def _record_array(records: object) -> np.ndarray:
    return np.asarray(getattr(records, "records_arr"))


def _matrix(value: np.ndarray, shape: tuple[int, int]) -> np.ndarray:
    array = np.asarray(value)
    if array.shape == (shape[0],) and shape[1] == 1:
        array = array.reshape(shape)
    if array.shape != shape:
        raise RuntimeError("vectorbt accessor shape does not match the ledger")
    return array


class ExecutionLedger:
    def __init__(
        self,
        portfolio: object,
        dates: np.ndarray,
        symbols: tuple[str, ...],
        close: np.ndarray,
    ) -> None:
        self.__portfolio = portfolio
        self._dates = np.asarray(dates)
        self._symbols = symbols
        self._close = np.asarray(close)
        self._cache: dict[str, np.ndarray] = {}
        self._raw_cache: dict[str, np.ndarray] = {}

    def _cached(self, name: str, build: Callable[[], np.ndarray]) -> np.ndarray:
        if name not in self._cache:
            self._cache[name] = _readonly(build())
        return self._cache[name]

    def _raw(self, name: str, build: Callable[[], object]) -> np.ndarray:
        if name not in self._raw_cache:
            self._raw_cache[name] = np.asarray(build())
        return self._raw_cache[name]

    def _order_records(self) -> np.ndarray:
        return self._raw(
            "order_records",
            lambda: _record_array(self.__portfolio.orders),
        )

    def _trade_records(self) -> np.ndarray:
        return self._raw(
            "trades",
            lambda: _record_array(self.__portfolio.trades),
        )

    def _position_records(self) -> np.ndarray:
        return self._raw(
            "positions",
            lambda: _record_array(self.__portfolio.positions),
        )

    @property
    def orders(self) -> np.ndarray:
        def build() -> np.ndarray:
            records = self._order_records()
            result = np.empty(len(records), dtype=_ORDER_DTYPE)
            times = _times(self._dates, "T09:30:00")
            realized: dict[tuple[int, int], float] = {}
            for trade in self._trade_records():
                if int(trade["status"]) != TradeStatus.Closed:
                    continue
                key = (int(trade["exit_idx"]), int(trade["col"]))
                realized[key] = realized.get(key, 0.0) + float(trade["pnl"])
            for index, record in enumerate(records):
                row = int(record["idx"])
                column = int(record["col"])
                time = times[row]
                quantity = int(round(float(record["size"])))
                is_sell = int(record["side"]) == OrderSide.Sell
                price = float(record["price"])
                fees = float(record["fees"])
                gains = realized.get((row, column), 0.0) if is_sell else 0.0
                result[index] = (
                    time,
                    0,
                    "",
                    "close" if is_sell else "open",
                    0.0,
                    "",
                    time,
                    time,
                    "long",
                    price,
                    fees,
                    gains,
                    "market",
                    time,
                    self._symbols[column],
                    self._symbols[column],
                    quantity,
                    quantity,
                    "done",
                )
            return result

        return self._cached("orders", build)

    @property
    def assets(self) -> np.ndarray:
        def build() -> np.ndarray:
            shape = self._close.shape
            values = _matrix(
                self._raw("assets", self.__portfolio.assets),
                shape,
            )
            active_rows, active_columns = np.nonzero(np.abs(values) > 1e-12)
            result = np.empty(len(active_rows), dtype=_ASSET_DTYPE)
            times = _times(self._dates, "T16:00:00")
            asset_flow = _matrix(
                self._raw("asset_flow", self.__portfolio.asset_flow),
                shape,
            )
            asset_value = _matrix(
                self._raw(
                    "asset_value",
                    lambda: self.__portfolio.asset_value(group_by=False),
                ),
                shape,
            )
            cash_flow = _matrix(
                self._raw(
                    "cash_flow",
                    lambda: self.__portfolio.cash_flow(group_by=False),
                ),
                shape,
            )
            previous_value = np.vstack(
                (np.zeros((1, asset_value.shape[1])), asset_value[:-1])
            )
            daily_gains = asset_value - previous_value + cash_flow
            cost_by_row = np.full(values.shape, np.nan)
            order_records = self._order_records()
            for position in self._position_records():
                column = int(position["col"])
                entry_row = int(position["entry_idx"])
                exit_row = int(position["exit_idx"])
                buys = order_records[
                    (order_records["col"] == column)
                    & (order_records["idx"] >= entry_row)
                    & (order_records["idx"] <= exit_row)
                    & (order_records["side"] == OrderSide.Buy)
                ]
                average = 0.0
                for buy_index, buy in enumerate(buys):
                    row = int(buy["idx"])
                    quantity = float(buy["size"])
                    after = float(values[row, column])
                    before = after - quantity
                    average = (
                        before * average + quantity * float(buy["price"])
                    ) / after
                    stop = (
                        int(buys[buy_index + 1]["idx"])
                        if buy_index + 1 < len(buys)
                        else exit_row + 1
                    )
                    rows = np.arange(row, stop)
                    held = np.abs(values[rows, column]) > 1e-12
                    cost_by_row[rows[held], column] = average
            average_cost = cost_by_row[active_rows, active_columns]
            if np.any(~np.isfinite(average_cost)):
                raise RuntimeError(
                    "vectorbt order records do not cover active asset costs"
                )
            amount = values[active_rows, active_columns]
            price = self._close[active_rows, active_columns]
            today_bought = np.maximum(
                asset_flow[active_rows, active_columns],
                0.0,
            ).astype(np.int64)
            result["pindex"] = 0
            result["avg_cost"] = average_cost
            result["margin"] = 0.0
            result["amount"] = amount
            result["today_amount"] = today_bought
            result["hold_cost"] = average_cost
            result["side"] = "long"
            result["price"] = price
            result["gains"] = (price - average_cost) * amount
            result["daily_gains"] = daily_gains[active_rows, active_columns]
            result["closeable_amount"] = np.maximum(
                np.rint(amount).astype(np.int64) - today_bought,
                0,
            )
            result["time"] = times[active_rows]
            securities = np.asarray(self._symbols)
            result["security_name"] = securities[active_columns]
            result["security"] = securities[active_columns]
            return result

        return self._cached("assets", build)

    @property
    def cash(self) -> np.ndarray:
        def build() -> np.ndarray:
            values = self._raw("cash", self.__portfolio.cash).reshape(-1)
            result = np.empty(len(values), dtype=_CASH_DTYPE)
            result["time"] = _times(self._dates, "T16:00:00")
            result["cash"] = values
            return result

        return self._cached("cash", build)

    @property
    def value(self) -> np.ndarray:
        def build() -> np.ndarray:
            values = self._raw("value", self.__portfolio.value).reshape(-1)
            returns = self._raw(
                "returns",
                self.__portfolio.cumulative_returns,
            ).reshape(-1)
            result = np.empty(len(values), dtype=_VALUE_DTYPE)
            result["time"] = _times(self._dates, "T16:00:00")
            result["total_value"] = values
            result["returns"] = returns
            result["benchmark_returns"] = np.nan
            return result

        return self._cached("value", build)

    @property
    def trades(self) -> np.ndarray:
        return self._cached("trades", self._trade_records)

    @property
    def positions(self) -> np.ndarray:
        return self._cached("positions", self._position_records)

    @property
    def returns(self) -> np.ndarray:
        def build() -> np.ndarray:
            values = self._raw(
                "returns",
                self.__portfolio.cumulative_returns,
            ).reshape(-1)
            result = np.empty(len(values), dtype=_RETURNS_DTYPE)
            result["time"] = _times(self._dates, "T16:00:00")
            result["returns"] = values
            return result

        return self._cached("returns", build)


@njit
def _reset_orders_nb(orders) -> None:
    enabled, side, size, price, fixed_fees, granularity, partial, priority = orders
    enabled[:] = False
    side[:] = SIDE_NONE
    size[:] = 0.0
    price[:] = np.nan
    fixed_fees[:] = 0.0
    granularity[:] = np.nan
    partial[:] = False
    priority[:] = 0


@njit
def _pre_sim_func_nb(c, orders):
    _reset_orders_nb(orders)
    return ()


@njit
def _order_func_nb(c, orders):
    enabled, side, size, price, fixed_fees, granularity, partial, _ = orders
    column = c.col
    if not enabled[column] or side[column] == SIDE_NONE or size[column] <= 0.0:
        return nb.NoOrder
    signed_size = size[column] if side[column] == SIDE_BUY else -size[column]
    return nb.order_nb(
        size=signed_size,
        price=price[column],
        direction=Direction.LongOnly,
        fixed_fees=fixed_fees[column],
        size_granularity=granularity[column],
        allow_partial=partial[column],
    )


def _as_numba_callback(callback: object) -> object:
    return callback if hasattr(callback, "py_func") else njit(callback)


@dataclass(frozen=True, slots=True)
class _CallbackFunctions:
    pre_segment_func_nb: object
    post_order_func_nb: object
    post_segment_func_nb: object


@lru_cache(maxsize=None)
def _specialized_functions(
    prepare_segment_nb: object,
    after_fill_nb: object,
    after_segment_nb: object | None,
) -> _CallbackFunctions:
    prepare_callback = _as_numba_callback(prepare_segment_nb)
    fill_callback = _as_numba_callback(after_fill_nb)

    @njit
    def pre_segment(c, inputs, params, state, trace, orders):
        _reset_orders_nb(orders)
        view = SegmentView(
            c.i,
            c.group,
            c.from_col,
            c.to_col,
            c.last_cash[c.group],
            c.last_value[c.group],
            c.last_position[c.from_col : c.to_col],
            c.last_val_price[c.from_col : c.to_col],
        )
        prepare_callback(view, inputs, params, state, trace, orders)
        for offset in range(c.to_col - c.from_col):
            c.call_seq_now[offset] = offset
        for offset in range(1, c.to_col - c.from_col):
            current = c.call_seq_now[offset]
            current_priority = orders[7][c.from_col + current]
            insertion = offset
            while insertion > 0:
                previous = c.call_seq_now[insertion - 1]
                previous_priority = orders[7][c.from_col + previous]
                if previous_priority <= current_priority:
                    break
                c.call_seq_now[insertion] = previous
                insertion -= 1
            c.call_seq_now[insertion] = current
        return ()

    @njit
    def post_order(c, inputs, params, state, trace, orders) -> None:
        if not orders[0][c.col]:
            return
        result = c.order_result
        status = FILL_IGNORED
        if result.status == OrderStatus.Filled:
            status = FILL_ACCEPTED
        elif result.status == OrderStatus.Rejected:
            status = FILL_REJECTED
        side = SIDE_NONE
        if result.side == OrderSide.Buy:
            side = SIDE_BUY
        elif result.side == OrderSide.Sell:
            side = SIDE_SELL
        event = FillEvent(
            c.i,
            c.col,
            status,
            side,
            result.size,
            result.price,
            result.fees,
            c.cash_now,
            c.position_now,
        )
        fill_callback(event, inputs, params, state, trace, orders)

    if after_segment_nb is None:

        @njit
        def post_segment(c, inputs, params, state, trace, orders) -> None:
            return None

    else:
        segment_callback = _as_numba_callback(after_segment_nb)

        @njit
        def post_segment(c, inputs, params, state, trace, orders) -> None:
            view = SegmentView(
                c.i,
                c.group,
                c.from_col,
                c.to_col,
                c.last_cash[c.group],
                c.last_value[c.group],
                c.last_position[c.from_col : c.to_col],
                c.last_val_price[c.from_col : c.to_col],
            )
            segment_callback(view, inputs, params, state, trace, orders)

    return _CallbackFunctions(pre_segment, post_order, post_segment)


@dataclass(frozen=True, slots=True)
class _SpecializedCallbacks:
    order_func_nb: object
    order_args: tuple[object, ...]
    pre_sim_func_nb: object
    pre_sim_args: tuple[object, ...]
    pre_segment_func_nb: object
    pre_segment_args: tuple[object, ...]
    post_order_func_nb: object
    post_order_args: tuple[object, ...]
    post_segment_func_nb: object
    post_segment_args: tuple[object, ...]


def _order_arrays(program: OrderProgram) -> tuple[np.ndarray, ...]:
    orders = program.orders
    return (
        orders.enabled,
        orders.side,
        orders.size,
        orders.price,
        orders.fixed_fees,
        orders.size_granularity,
        orders.allow_partial,
        orders.priority,
    )


def _specialize_program(program: OrderProgram) -> _SpecializedCallbacks:
    functions = _specialized_functions(
        program.prepare_segment_nb,
        program.after_fill_nb,
        program.after_segment_nb,
    )
    orders = _order_arrays(program)
    trace = tuple(program.trace.values())
    program_args = (program.inputs, program.params, program.state, trace, orders)
    return _SpecializedCallbacks(
        order_func_nb=_order_func_nb,
        order_args=(orders,),
        pre_sim_func_nb=_pre_sim_func_nb,
        pre_sim_args=(orders,),
        pre_segment_func_nb=functions.pre_segment_func_nb,
        pre_segment_args=program_args,
        post_order_func_nb=functions.post_order_func_nb,
        post_order_args=program_args,
        post_segment_func_nb=functions.post_segment_func_nb,
        post_segment_args=program_args,
    )


def _validate_input(ledger_input: LedgerInput, program: OrderProgram) -> None:
    close = np.asarray(ledger_input.close)
    if close.ndim != 2 or close.shape[0] == 0 or close.shape[1] == 0:
        raise ValueError("ledger close must be a non-empty two-dimensional array")
    if np.asarray(ledger_input.dates).shape != (close.shape[0],):
        raise ValueError("ledger dates do not match close rows")
    if len(ledger_input.symbols) != close.shape[1]:
        raise ValueError("ledger symbols do not match close columns")
    groups = np.asarray(ledger_input.group_ids)
    if groups.shape != (close.shape[1],):
        raise ValueError("ledger groups do not match close columns")
    if not ledger_input.cash_sharing:
        raise ValueError("vectorbt runtime requires shared cash")
    if not np.isfinite(ledger_input.initial_cash) or ledger_input.initial_cash <= 0:
        raise ValueError("ledger initial cash must be positive")
    arrays = _order_arrays(program)
    if any(array.shape != (close.shape[1],) for array in arrays):
        raise ValueError("order buffer does not match close columns")
    if any(not array.flags.writeable for array in arrays):
        raise ValueError("order buffer must be writeable")


def run_vectorbt(
    ledger_input: LedgerInput,
    program: OrderProgram,
) -> ExecutionRun:
    _validate_input(ledger_input, program)
    callbacks = _specialize_program(program)
    close_values = np.asarray(ledger_input.close)
    close = pd.DataFrame(
        close_values,
        index=pd.DatetimeIndex(
            np.asarray(ledger_input.dates).astype("datetime64[ns]")
        ),
        columns=ledger_input.symbols,
    )
    portfolio = vbt.Portfolio.from_order_func(
        close,
        callbacks.order_func_nb,
        *callbacks.order_args,
        pre_sim_func_nb=callbacks.pre_sim_func_nb,
        pre_sim_args=callbacks.pre_sim_args,
        pre_segment_func_nb=callbacks.pre_segment_func_nb,
        pre_segment_args=callbacks.pre_segment_args,
        post_order_func_nb=callbacks.post_order_func_nb,
        post_order_args=callbacks.post_order_args,
        post_segment_func_nb=callbacks.post_segment_func_nb,
        post_segment_args=callbacks.post_segment_args,
        init_cash=float(ledger_input.initial_cash),
        cash_sharing=True,
        group_by=np.asarray(ledger_input.group_ids),
        call_pre_segment=True,
        call_post_segment=True,
        update_value=True,
        ffill_val_price=True,
        use_numba=True,
        max_orders=close.shape[0] * close.shape[1],
        max_logs=0,
        freq=ledger_input.frequency,
    )
    trace = {
        name: _readonly(value)
        for name, value in program.trace.items()
    }
    return ExecutionRun(
        ledger=ExecutionLedger(
            portfolio,
            ledger_input.dates,
            ledger_input.symbols,
            close_values,
        ),
        trace=MappingProxyType(trace),
    )
