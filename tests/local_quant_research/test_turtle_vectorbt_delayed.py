from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np


RESEARCH_ROOT = (
    Path(__file__).resolve().parents[2]
    / "joinquant"
    / "strategies"
    / "strategy-003"
    / "research"
)
sys.path.insert(0, str(RESEARCH_ROOT))

from turtle_etf.vectorbt_callbacks import (  # noqa: E402
    ACTION_ENTRY,
    ACTION_FULL_EXIT,
    ACTION_REDISTRIBUTION_BUY,
    ACTION_REDISTRIBUTION_SELL,
    REASON_ENTRY_BREAKOUT,
    REASON_FULL_POSITION_REDISTRIBUTION,
    REASON_PROTECTIVE_STOP,
)
from turtle_etf.vectorbt_delayed import (  # noqa: E402
    ADJUST_CASH_TRUNCATED,
    ADJUST_HOLDING_TRUNCATED,
    ADJUST_NONE,
    freeze_order_plan,
    run_delayed_execution,
)


def _matrix(values: object, dtype: str) -> np.ndarray:
    return np.asarray(values, dtype=dtype)


def _inputs(opens: list[list[float]]) -> SimpleNamespace:
    open_values = _matrix(opens, "float64")
    rows, columns = open_values.shape
    return SimpleNamespace(
        dates=np.arange(
            np.datetime64("2026-01-05"),
            np.datetime64("2026-01-05") + np.timedelta64(rows, "D"),
        ).astype("datetime64[D]"),
        securities=tuple(f"ETF-{chr(65 + column)}" for column in range(columns)),
        execution_open=open_values,
        close=np.where(np.isfinite(open_values), open_values, 10.0),
        paused=np.zeros((rows, columns), dtype=np.bool_),
        high_limit=np.full((rows, columns), np.nan),
        low_limit=np.full((rows, columns), np.nan),
        signal_n=np.ones((rows, columns), dtype=np.float64),
    )


def _immediate(
    *,
    rows: int,
    columns: int,
    orders: list[tuple[int, int, int, int, int]],
) -> SimpleNamespace:
    action = np.zeros((rows, columns), dtype=np.int16)
    reason = np.zeros((rows, columns), dtype=np.int16)
    requested = np.zeros((rows, columns), dtype=np.int64)
    planned = np.zeros((rows, columns), dtype=np.int64)
    filled = np.zeros((rows, columns), dtype=np.int64)
    for row, column, action_code, reason_code, quantity in orders:
        action[row, column] = action_code
        reason[row, column] = reason_code
        requested[row, column] = quantity
        planned[row, column] = quantity
        filled[row, column] = quantity
    return SimpleNamespace(
        action_codes=action,
        reason_codes=reason,
        requested_quantities=requested,
        planned_quantities=planned,
        filled_quantities=filled,
    )


def _run(
    inputs: SimpleNamespace,
    immediate: SimpleNamespace,
    *,
    initial_cash: float = 100_000.0,
):
    plan = freeze_order_plan(inputs, immediate)
    return plan, run_delayed_execution(
        inputs,
        plan,
        initial_cash=initial_cash,
        lot_size=100,
        stop_n=2.0,
        commission_multiplier=1.0,
        one_way_slippage=0.0,
        delay_days=1,
    )


def test_delayed_execution_freezes_original_action_target_reason_and_signal_n() -> None:
    inputs = _inputs([[10.0], [11.0], [12.0], [13.0]])
    inputs.signal_n[1, 0] = 1.5
    inputs.signal_n[2, 0] = 999.0
    immediate = _immediate(
        rows=4,
        columns=1,
        orders=[(1, 0, ACTION_ENTRY, REASON_ENTRY_BREAKOUT, 200)],
    )

    plan, delayed = _run(inputs, immediate)

    assert plan.signal_n[1, 0] == 1.5
    assert delayed.action_codes[2, 0] == ACTION_ENTRY
    assert delayed.reason_codes[2, 0] == REASON_ENTRY_BREAKOUT
    assert delayed.planned_quantities[2, 0] == 200
    assert delayed.planned_row_indices[2, 0] == 1
    assert delayed.frozen_signal_n[2, 0] == 1.5
    assert delayed.state_common_stop[2, 0] == 12.0 - 2.0 * 1.5
    assert delayed.execution_adjustment_codes[2, 0] == ADJUST_NONE


def test_delayed_buy_only_uses_lot_cash_truncation() -> None:
    inputs = _inputs([[10.0], [10.0], [200.0], [200.0]])
    immediate = _immediate(
        rows=4,
        columns=1,
        orders=[(1, 0, ACTION_ENTRY, REASON_ENTRY_BREAKOUT, 200)],
    )

    _, delayed = _run(inputs, immediate, initial_cash=25_000.0)

    assert delayed.execution_adjustment_codes[2, 0] == ADJUST_CASH_TRUNCATED
    assert delayed.filled_quantities[2, 0] == 100
    assert delayed.filled_quantities[2, 0] % 100 == 0
    assert delayed.filled_quantities[2, 0] < delayed.planned_quantities[2, 0]


def test_delayed_sell_is_mechanically_truncated_to_actual_holding() -> None:
    inputs = _inputs([[10.0], [10.0], [10.0], [10.0], [10.0]])
    immediate = _immediate(
        rows=5,
        columns=1,
        orders=[
            (0, 0, ACTION_ENTRY, REASON_ENTRY_BREAKOUT, 100),
            (2, 0, ACTION_FULL_EXIT, REASON_PROTECTIVE_STOP, 200),
        ],
    )

    _, delayed = _run(inputs, immediate)

    assert delayed.filled_quantities[1, 0] == 100
    assert delayed.planned_quantities[3, 0] == 200
    assert delayed.filled_quantities[3, 0] == 100
    assert delayed.execution_adjustment_codes[3, 0] == ADJUST_HOLDING_TRUNCATED
    assert delayed.state_quantities[3, 0] == 0


def test_delayed_queue_is_executed_in_original_priority_then_security_order() -> None:
    inputs = _inputs(
        [
            [10.0, 10.0],
            [10.0, 10.0],
            [10.0, 10.0],
            [10.0, 10.0],
        ]
    )
    immediate = _immediate(
        rows=4,
        columns=2,
        orders=[
            (1, 1, ACTION_ENTRY, REASON_ENTRY_BREAKOUT, 100),
            (1, 0, ACTION_ENTRY, REASON_ENTRY_BREAKOUT, 100),
        ],
    )

    _, delayed = _run(inputs, immediate)

    assert delayed.execution_sequence[2] == (
        "queued-from-row-1:ETF-A",
        "queued-from-row-1:ETF-B",
    )


def test_vectorbt_ledger_uses_priority_sequence_not_inverse_column_ranks() -> None:
    inputs = _inputs(
        [
            [10.0, 10.0, 100.0],
            [10.0, 10.0, 100.0],
            [10.0, 10.0, 100.0],
            [10.0, 10.0, 100.0],
        ]
    )
    immediate = _immediate(
        rows=4,
        columns=3,
        orders=[
            (0, 2, ACTION_ENTRY, REASON_ENTRY_BREAKOUT, 100),
            (1, 2, ACTION_FULL_EXIT, REASON_PROTECTIVE_STOP, 100),
            (1, 0, ACTION_ENTRY, REASON_ENTRY_BREAKOUT, 100),
            (1, 1, ACTION_ENTRY, REASON_ENTRY_BREAKOUT, 100),
        ],
    )

    _, delayed = _run(inputs, immediate, initial_cash=10_005.0)

    assert delayed.execution_sequence[2] == (
        "queued-from-row-1:ETF-C",
        "queued-from-row-1:ETF-A",
        "queued-from-row-1:ETF-B",
    )
    assert delayed.filled_quantities[2].tolist() == [100, 100, 100]
    assert delayed.portfolio.orders.count() == 4


def test_delayed_redistribution_keeps_units_and_stops_and_uses_priority() -> None:
    inputs = _inputs(
        [
            [10.0, 10.0, 10.0],
            [10.0, 10.0, 10.0],
            [11.0, 11.0, 11.0],
            [11.0, 11.0, 11.0],
        ]
    )
    immediate = _immediate(
        rows=4,
        columns=3,
        orders=[
            (0, 0, ACTION_ENTRY, REASON_ENTRY_BREAKOUT, 200),
            (0, 2, ACTION_ENTRY, REASON_ENTRY_BREAKOUT, 200),
            (
                1,
                0,
                ACTION_REDISTRIBUTION_SELL,
                REASON_FULL_POSITION_REDISTRIBUTION,
                100,
            ),
            (1, 1, ACTION_ENTRY, REASON_ENTRY_BREAKOUT, 100),
            (
                1,
                2,
                ACTION_REDISTRIBUTION_BUY,
                REASON_FULL_POSITION_REDISTRIBUTION,
                100,
            ),
        ],
    )

    _, delayed = _run(inputs, immediate)

    assert delayed.execution_sequence[2] == (
        "queued-from-row-1:ETF-A",
        "queued-from-row-1:ETF-B",
        "queued-from-row-1:ETF-C",
    )
    assert delayed.state_quantities[2].tolist() == [100, 100, 300]
    assert delayed.state_unit_counts[2].tolist() == [1, 1, 1]
    assert delayed.state_common_stop[2].tolist() == [8.0, 9.0, 8.0]
    assert delayed.state_next_add_index[2].tolist() == [1, 1, 1]
