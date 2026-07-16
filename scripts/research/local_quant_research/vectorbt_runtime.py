from __future__ import annotations

from types import MappingProxyType
from typing import Callable

import numpy as np
import pandas as pd
import vectorbt as vbt
from vectorbt.portfolio import nb

from .contracts import ExecutionRun, LedgerInput, OrderProgram


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
    array = np.ascontiguousarray(value)
    array.setflags(write=False)
    return array


def _times(dates: np.ndarray) -> np.ndarray:
    return np.datetime_as_string(
        np.asarray(dates).astype("datetime64[ns]"),
        unit="s",
    )


class _VectorbtLedger:
    def __init__(self, portfolio: object, dates: np.ndarray) -> None:
        self.__portfolio = portfolio
        self._dates = np.asarray(dates)
        self._cache: dict[str, np.ndarray] = {}

    def _cached(self, name: str, build: Callable[[], np.ndarray]) -> np.ndarray:
        if name not in self._cache:
            self._cache[name] = _readonly(build())
        return self._cache[name]

    @property
    def orders(self) -> np.ndarray:
        return self._cached("orders", lambda: np.empty(0, dtype=_ORDER_DTYPE))

    @property
    def assets(self) -> np.ndarray:
        return self._cached("assets", lambda: np.empty(0, dtype=_ASSET_DTYPE))

    @property
    def cash(self) -> np.ndarray:
        def build() -> np.ndarray:
            values = np.asarray(self.__portfolio.cash()).reshape(-1)
            result = np.empty(len(values), dtype=_CASH_DTYPE)
            result["time"] = _times(self._dates)
            result["cash"] = values
            return result

        return self._cached("cash", build)

    @property
    def value(self) -> np.ndarray:
        def build() -> np.ndarray:
            values = np.asarray(self.__portfolio.value()).reshape(-1)
            returns = np.asarray(self.__portfolio.returns()).reshape(-1)
            result = np.empty(len(values), dtype=_VALUE_DTYPE)
            result["time"] = _times(self._dates)
            result["total_value"] = values
            result["returns"] = returns
            result["benchmark_returns"] = np.nan
            return result

        return self._cached("value", build)

    @property
    def trades(self) -> np.ndarray:
        return self._cached("trades", lambda: np.empty(0, dtype=np.dtype([])))

    @property
    def positions(self) -> np.ndarray:
        return self.assets

    @property
    def returns(self) -> np.ndarray:
        def build() -> np.ndarray:
            values = np.asarray(self.__portfolio.returns()).reshape(-1)
            result = np.empty(len(values), dtype=_RETURNS_DTYPE)
            result["time"] = _times(self._dates)
            result["returns"] = values
            return result

        return self._cached("returns", build)


def _validate_minimal_input(
    ledger_input: LedgerInput,
    program: OrderProgram,
) -> None:
    close = np.asarray(ledger_input.close)
    if close.ndim != 2 or close.shape[0] == 0 or close.shape[1] == 0:
        raise ValueError("ledger close must be a non-empty two-dimensional array")
    if len(ledger_input.dates) != close.shape[0]:
        raise ValueError("ledger dates do not match close rows")
    if len(ledger_input.symbols) != close.shape[1]:
        raise ValueError("ledger symbols do not match close columns")
    if len(program.orders.enabled) != close.shape[1]:
        raise ValueError("order buffer does not match close columns")
    if np.any(program.orders.enabled):
        raise NotImplementedError("Task 5 runtime only accepts the no-order seam")


def run_vectorbt(
    ledger_input: LedgerInput,
    program: OrderProgram,
) -> ExecutionRun:
    _validate_minimal_input(ledger_input, program)
    close = pd.DataFrame(
        np.asarray(ledger_input.close),
        index=pd.DatetimeIndex(
            np.asarray(ledger_input.dates).astype("datetime64[ns]")
        ),
        columns=ledger_input.symbols,
    )
    portfolio = vbt.Portfolio.from_order_func(
        close,
        nb.no_order_func_nb,
        init_cash=float(ledger_input.initial_cash),
        cash_sharing=True,
        group_by=np.asarray(ledger_input.group_ids),
        call_pre_segment=True,
        update_value=True,
        ffill_val_price=True,
        use_numba=True,
        max_orders=close.shape[0] * close.shape[1],
        max_logs=0,
        freq=ledger_input.frequency,
    )
    if len(portfolio.order_records) != 0:
        raise RuntimeError("no-order program produced vectorbt orders")
    trace = {
        name: _readonly(np.array(value, copy=True))
        for name, value in program.trace.items()
    }
    return ExecutionRun(
        ledger=_VectorbtLedger(portfolio, ledger_input.dates),
        trace=MappingProxyType(trace),
    )
