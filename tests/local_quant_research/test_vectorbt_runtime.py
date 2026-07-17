from __future__ import annotations

import importlib
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from numba import njit

from scripts.research.local_quant_research.contracts import (
    FILL_ACCEPTED,
    FILL_REJECTED,
    SIDE_BUY,
    SIDE_NONE,
    SIDE_SELL,
    ExecutionRun,
    LedgerInput,
    OrderBuffer,
    OrderProgram,
)
from scripts.research.local_quant_research.strategy_loader import load_strategy
from scripts.research.market_data.query import SnapshotView


def _snapshot() -> SnapshotView:
    return SnapshotView(
        snapshot_id="a" * 64,
        fields=(),
        rows=(),
        digest="b" * 64,
        corporate_action_fields=(),
        corporate_actions=(),
        corporate_actions_digest="c" * 64,
    )


def _ledger_input(
    rows: int,
    columns: int,
    *,
    initial_cash: float = 100.0,
) -> LedgerInput:
    return LedgerInput(
        dates=np.arange(rows).astype("timedelta64[D]")
        + np.datetime64("2026-01-05"),
        symbols=tuple(f"S{column}" for column in range(columns)),
        close=np.full((rows, columns), 10.0),
        initial_cash=initial_cash,
        group_ids=np.zeros(columns, dtype=np.int64),
        cash_sharing=True,
        frequency="1d",
    )


def _order_buffer(columns: int) -> OrderBuffer:
    return OrderBuffer(
        enabled=np.zeros(columns, dtype=np.bool_),
        side=np.full(columns, SIDE_NONE, dtype=np.int8),
        size=np.zeros(columns, dtype=np.float64),
        price=np.full(columns, np.nan, dtype=np.float64),
        fixed_fees=np.zeros(columns, dtype=np.float64),
        size_granularity=np.ones(columns, dtype=np.float64),
        allow_partial=np.zeros(columns, dtype=np.bool_),
        priority=np.zeros(columns, dtype=np.int64),
    )


@njit
def _prepare_priority_program(view, inputs, params, state, trace, orders) -> None:
    priorities = inputs[0]
    enabled, side, size, price, fixed_fees, granularity, partial, priority = orders
    for column in range(view.from_col, view.to_col):
        enabled[column] = True
        side[column] = SIDE_BUY
        size[column] = 1.0
        price[column] = 10.0
        fixed_fees[column] = 0.0
        granularity[column] = 1.0
        partial[column] = False
        priority[column] = priorities[column]


@njit
def _prepare_priority_with_disabled(
    view,
    inputs,
    params,
    state,
    trace,
    orders,
) -> None:
    priorities, enabled_mask = inputs
    enabled, side, size, price, fixed_fees, granularity, partial, priority = orders
    for column in range(view.from_col, view.to_col):
        enabled[column] = enabled_mask[column]
        side[column] = SIDE_BUY
        size[column] = 1.0
        price[column] = 10.0
        fixed_fees[column] = 0.0
        granularity[column] = 1.0
        partial[column] = False
        priority[column] = priorities[column]


@njit
def _prepare_sell_then_buy(view, inputs, params, state, trace, orders) -> None:
    enabled, side, size, price, fixed_fees, granularity, partial, priority = orders
    if view.row == 0:
        enabled[1] = True
        side[1] = SIDE_BUY
        size[1] = 1.0
        price[1] = 10.0
        priority[1] = 0
    else:
        enabled[0] = True
        side[0] = SIDE_BUY
        size[0] = 1.0
        price[0] = 10.0
        priority[0] = 1
        enabled[1] = True
        side[1] = SIDE_SELL
        size[1] = 1.0
        price[1] = 10.0
        priority[1] = 0
    for column in range(view.from_col, view.to_col):
        fixed_fees[column] = 0.0
        granularity[column] = 1.0
        partial[column] = False


@njit
def _prepare_rejected_buy(view, inputs, params, state, trace, orders) -> None:
    enabled, side, size, price, fixed_fees, granularity, partial, priority = orders
    enabled[0] = True
    side[0] = SIDE_BUY
    size[0] = 1.0
    price[0] = 10.0
    fixed_fees[0] = 0.0
    granularity[0] = 1.0
    partial[0] = False
    priority[0] = 0


@njit
def _prepare_buy_once(view, inputs, params, state, trace, orders) -> None:
    if view.row != 0:
        return
    enabled, side, size, price, fixed_fees, granularity, partial, priority = orders
    enabled[0] = True
    side[0] = SIDE_BUY
    size[0] = 1.0
    price[0] = 10.0
    fixed_fees[0] = 0.0
    granularity[0] = 1.0
    partial[0] = False
    priority[0] = 0


@njit
def _prepare_partial_exit(view, inputs, params, state, trace, orders) -> None:
    enabled, side, size, price, fixed_fees, granularity, partial, priority = orders
    enabled[0] = True
    side[0] = SIDE_BUY if view.row == 0 else SIDE_SELL
    size[0] = 2.0 if view.row == 0 else 1.0
    price[0] = 10.0 if view.row == 0 else 12.0
    fixed_fees[0] = 0.0
    granularity[0] = 1.0
    partial[0] = False
    priority[0] = 0


@njit
def _prepare_two_buys(view, inputs, params, state, trace, orders) -> None:
    enabled, side, size, price, fixed_fees, granularity, partial, priority = orders
    enabled[0] = True
    side[0] = SIDE_BUY
    size[0] = 1.0
    price[0] = 10.0 if view.row == 0 else 20.0
    fixed_fees[0] = 0.0
    granularity[0] = 1.0
    partial[0] = False
    priority[0] = 0


@njit
def _record_order_event(event, inputs, params, state, trace, orders) -> None:
    accepted = state[0]
    sequence, statuses, count, _ = trace
    index = count[0]
    sequence[index] = event.column
    statuses[index] = event.status
    count[0] = index + 1
    if event.status == FILL_ACCEPTED:
        accepted[event.column] += 1


@njit
def _record_segment(view, inputs, params, state, trace, orders) -> None:
    trace[3][view.row] += 1


def _program(
    prepare: object,
    *,
    columns: int,
    rows: int,
    inputs: tuple[object, ...] = (),
) -> OrderProgram:
    return OrderProgram(
        program_id="runtime-test",
        prepare_segment_nb=prepare,
        after_fill_nb=_record_order_event,
        after_segment_nb=_record_segment,
        inputs=inputs,
        params=(),
        state=(np.zeros(columns, dtype=np.int64),),
        trace={
            "sequence": np.full(rows * columns, -1, dtype=np.int64),
            "statuses": np.full(rows * columns, -1, dtype=np.int64),
            "count": np.zeros(1, dtype=np.int64),
            "segments": np.zeros(rows, dtype=np.int64),
        },
        orders=_order_buffer(columns),
    )


@pytest.mark.parametrize(
    ("strategy_root", "strategy_module"),
    (
        ("tests/local_quant_research/fixtures/minimal_strategy", "strategy"),
        ("tests/local_quant_research/fixtures", "minimal_strategy_b.strategy"),
    ),
)
def test_minimal_no_order_strategy_runs_through_shared_vectorbt_runtime(
    strategy_root: str,
    strategy_module: str,
    repo_root: Path,
) -> None:
    try:
        runtime = importlib.import_module(
            "scripts.research.local_quant_research.vectorbt_runtime"
        )
    except ModuleNotFoundError:
        pytest.fail("shared vectorbt runtime is missing")
    loaded = load_strategy(
        repo_root,
        {"root": strategy_root, "module": strategy_module, "symbol": "MODULE"},
    )
    prepared = loaded.module.prepare(_snapshot(), {})

    result = runtime.run_vectorbt(
        prepared.ledger_input,
        prepared.primary_program,
    )

    assert isinstance(result, ExecutionRun)
    value = result.ledger.value
    assert value.dtype.names == (
        "time",
        "total_value",
        "returns",
        "benchmark_returns",
    )
    assert value.shape == (2,)
    assert value.flags.writeable is False
    assert result.ledger.value is value
    assert np.array_equal(value["total_value"], np.array([100_000.0, 100_000.0]))


def test_ledger_and_trace_public_arrays_are_cached_readonly_and_hide_portfolio(
    repo_root: Path,
) -> None:
    runtime = importlib.import_module(
        "scripts.research.local_quant_research.vectorbt_runtime"
    )
    loaded = load_strategy(
        repo_root,
        {
            "root": "tests/local_quant_research/fixtures/minimal_strategy",
            "module": "strategy",
            "symbol": "MODULE",
        },
    )
    prepared = loaded.module.prepare(_snapshot(), {})
    program = replace(
        prepared.primary_program,
        trace={"state": np.array([1.0, 2.0])},
    )

    result = runtime.run_vectorbt(prepared.ledger_input, program)

    public_arrays = (
        "orders",
        "assets",
        "cash",
        "value",
        "trades",
        "positions",
        "returns",
    )
    for name in public_arrays:
        first = getattr(result.ledger, name)
        assert first.flags.writeable is False
        assert getattr(result.ledger, name) is first
    trace = result.trace["state"]
    assert trace.flags.writeable is False
    assert result.trace["state"] is trace
    assert {
        name for name in dir(result.ledger) if not name.startswith("_")
    } == set(public_arrays)
    assert not hasattr(result.ledger, "portfolio")


def test_runtime_uses_stable_priority_and_only_converts_enabled_slots() -> None:
    runtime = importlib.import_module(
        "scripts.research.local_quant_research.vectorbt_runtime"
    )
    program = _program(
        _prepare_priority_with_disabled,
        columns=4,
        rows=1,
        inputs=(
            np.array([2, 1, 1, 0], dtype=np.int64),
            np.array([True, True, True, False]),
        ),
    )

    result = runtime.run_vectorbt(_ledger_input(1, 4), program)

    assert result.trace["count"][0] == 3
    assert np.array_equal(result.trace["sequence"][:3], np.array([1, 2, 0]))
    assert np.array_equal(
        result.trace["statuses"][:3],
        np.full(3, FILL_ACCEPTED, dtype=np.int64),
    )
    assert result.trace["segments"].tolist() == [1]


def test_sell_priority_releases_shared_cash_before_buy() -> None:
    runtime = importlib.import_module(
        "scripts.research.local_quant_research.vectorbt_runtime"
    )
    program = _program(_prepare_sell_then_buy, columns=2, rows=2)

    result = runtime.run_vectorbt(
        _ledger_input(2, 2, initial_cash=10.0),
        program,
    )

    assert result.trace["count"][0] == 3
    assert result.trace["sequence"][:3].tolist() == [1, 1, 0]
    assert result.trace["statuses"][:3].tolist() == [
        FILL_ACCEPTED,
        FILL_ACCEPTED,
        FILL_ACCEPTED,
    ]
    assert program.state[0].tolist() == [1, 2]


def test_rejected_order_is_reported_without_advancing_fill_state() -> None:
    runtime = importlib.import_module(
        "scripts.research.local_quant_research.vectorbt_runtime"
    )
    program = _program(_prepare_rejected_buy, columns=1, rows=1)

    result = runtime.run_vectorbt(
        _ledger_input(1, 1, initial_cash=5.0),
        program,
    )

    assert result.trace["count"][0] == 1
    assert result.trace["statuses"][0] == FILL_REJECTED
    assert program.state[0].tolist() == [0]


def test_ledger_formats_vectorbt_orders_positions_and_cumulative_returns() -> None:
    runtime = importlib.import_module(
        "scripts.research.local_quant_research.vectorbt_runtime"
    )
    ledger_input = replace(
        _ledger_input(2, 1, initial_cash=10.0),
        close=np.array([[10.0], [12.0]]),
    )
    program = _program(_prepare_buy_once, columns=1, rows=2)

    result = runtime.run_vectorbt(ledger_input, program)

    assert result.trace["count"][0] == 1
    assert result.ledger.orders["action"].tolist() == ["open"]
    assert result.ledger.orders["security"].tolist() == ["S0"]
    assert result.ledger.orders["time"].tolist() == ["2026-01-05T09:30:00"]
    assert result.ledger.cash["cash"].tolist() == [0.0, 0.0]
    assert result.ledger.value["total_value"].tolist() == [10.0, 12.0]
    assert np.allclose(result.ledger.value["returns"], np.array([0.0, 0.2]))
    assert np.allclose(result.ledger.returns["returns"], np.array([0.0, 0.2]))
    assert result.ledger.assets["avg_cost"].tolist() == [10.0, 10.0]
    assert result.ledger.assets["hold_cost"].tolist() == [10.0, 10.0]
    assert result.ledger.assets["gains"].tolist() == [0.0, 2.0]
    assert result.ledger.assets["daily_gains"].tolist() == [0.0, 2.0]
    assert result.ledger.assets["today_amount"].tolist() == [1, 0]
    assert result.ledger.assets["closeable_amount"].tolist() == [0, 1]
    assert result.ledger.assets["time"].tolist() == [
        "2026-01-05T16:00:00",
        "2026-01-06T16:00:00",
    ]


def test_order_gains_only_include_closed_vectorbt_trades() -> None:
    runtime = importlib.import_module(
        "scripts.research.local_quant_research.vectorbt_runtime"
    )
    ledger_input = replace(
        _ledger_input(2, 1, initial_cash=20.0),
        close=np.array([[10.0], [12.0]]),
    )
    program = _program(_prepare_partial_exit, columns=1, rows=2)

    result = runtime.run_vectorbt(ledger_input, program)

    assert result.ledger.orders["action"].tolist() == ["open", "close"]
    assert result.ledger.orders["gains"].tolist() == [0.0, 2.0]


def test_later_buy_does_not_rewrite_earlier_daily_average_cost() -> None:
    runtime = importlib.import_module(
        "scripts.research.local_quant_research.vectorbt_runtime"
    )
    ledger_input = replace(
        _ledger_input(2, 1, initial_cash=30.0),
        close=np.array([[10.0], [20.0]]),
    )
    program = _program(_prepare_two_buys, columns=1, rows=2)

    result = runtime.run_vectorbt(ledger_input, program)

    assert result.ledger.assets["amount"].tolist() == [1.0, 2.0]
    assert result.ledger.assets["avg_cost"].tolist() == [10.0, 15.0]
    assert result.ledger.assets["hold_cost"].tolist() == [10.0, 15.0]
    assert result.ledger.assets["gains"].tolist() == [0.0, 10.0]


def test_same_callback_identity_reuses_specialized_functions() -> None:
    runtime = importlib.import_module(
        "scripts.research.local_quant_research.vectorbt_runtime"
    )
    first_program = _program(_prepare_priority_program, columns=1, rows=1)
    second_program = _program(_prepare_priority_program, columns=1, rows=1)

    first = runtime._specialize_program(first_program)
    second = runtime._specialize_program(second_program)

    assert first.pre_segment_func_nb is second.pre_segment_func_nb
    assert first.post_order_func_nb is second.post_order_func_nb
    assert first.post_segment_func_nb is second.post_segment_func_nb


class _CountingPortfolio:
    def __init__(self) -> None:
        self.calls = {
            name: 0
            for name in (
                "orders",
                "assets",
                "asset_flow",
                "asset_value",
                "cash",
                "cash_flow",
                "value",
                "trades",
                "positions",
                "returns",
            )
        }
        self.order_records = np.zeros(
            1,
            dtype=[
                ("id", "i8"),
                ("col", "i8"),
                ("idx", "i8"),
                ("size", "f8"),
                ("price", "f8"),
                ("fees", "f8"),
                ("side", "i8"),
            ],
        )
        self.order_records[0] = (0, 0, 1, 1.0, 10.0, 0.0, 0)
        self.trade_records = np.zeros(
            1,
            dtype=[
                ("col", "i8"),
                ("exit_idx", "i8"),
                ("pnl", "f8"),
                ("status", "i8"),
            ],
        )
        self.trade_records["status"] = 1
        position_dtype = [
            ("col", "i8"),
            ("entry_idx", "i8"),
            ("exit_idx", "i8"),
            ("entry_price", "f8"),
        ]
        self._position_base = np.zeros(2, dtype=position_dtype)
        self._position_base[0] = (0, 1, 1, 10.0)
        self.position_records = self._position_base[::2]

    @property
    def orders(self) -> object:
        self.calls["orders"] += 1
        return SimpleNamespace(records_arr=self.order_records)

    @property
    def trades(self) -> object:
        self.calls["trades"] += 1
        return SimpleNamespace(records_arr=self.trade_records)

    @property
    def positions(self) -> object:
        self.calls["positions"] += 1
        return SimpleNamespace(records_arr=self.position_records)

    def assets(self) -> np.ndarray:
        self.calls["assets"] += 1
        return np.array([[0.0], [1.0]])

    def asset_flow(self) -> np.ndarray:
        self.calls["asset_flow"] += 1
        return np.array([[0.0], [1.0]])

    def asset_value(self, *, group_by: bool) -> np.ndarray:
        assert group_by is False
        self.calls["asset_value"] += 1
        return np.array([[0.0], [10.0]])

    def cash(self) -> np.ndarray:
        self.calls["cash"] += 1
        return np.array([100.0, 90.0])

    def cash_flow(self, *, group_by: bool) -> np.ndarray:
        assert group_by is False
        self.calls["cash_flow"] += 1
        return np.array([[0.0], [-10.0]])

    def value(self) -> np.ndarray:
        self.calls["value"] += 1
        return np.array([100.0, 100.0])

    def cumulative_returns(self) -> np.ndarray:
        self.calls["returns"] += 1
        return np.array([0.0, 0.0])


def test_ledger_computes_each_accessor_once_and_avoids_unneeded_copies() -> None:
    runtime = importlib.import_module(
        "scripts.research.local_quant_research.vectorbt_runtime"
    )
    portfolio = _CountingPortfolio()
    ledger = runtime.ExecutionLedger(
        portfolio,
        np.array(["2026-01-05", "2026-01-06"], dtype="datetime64[D]"),
        ("S0",),
        np.full((2, 1), 10.0),
    )

    views = {}
    for name in (
        "orders",
        "assets",
        "cash",
        "value",
        "trades",
        "positions",
        "returns",
    ):
        first = getattr(ledger, name)
        second = getattr(ledger, name)
        assert second is first
        assert first.flags.writeable is False
        views[name] = first

    assert portfolio.calls == {name: 1 for name in portfolio.calls}
    assert np.shares_memory(views["trades"], portfolio.trade_records)
    assert not np.shares_memory(views["positions"], portfolio.position_records)


def test_primary_and_followup_programs_use_the_same_runtime_entry() -> None:
    runtime = importlib.import_module(
        "scripts.research.local_quant_research.vectorbt_runtime"
    )
    ledger_input = _ledger_input(1, 1)
    primary_program = _program(
        _prepare_priority_program,
        columns=1,
        rows=1,
        inputs=(np.array([0], dtype=np.int64),),
    )
    followup_program = _program(
        _prepare_priority_program,
        columns=1,
        rows=1,
        inputs=(np.array([0], dtype=np.int64),),
    )

    primary = runtime.run_vectorbt(ledger_input, primary_program)
    final = runtime.run_vectorbt(ledger_input, followup_program)

    assert isinstance(primary, ExecutionRun)
    assert isinstance(final, ExecutionRun)
    assert primary.trace["count"][0] == 1
    assert final.trace["count"][0] == 1
