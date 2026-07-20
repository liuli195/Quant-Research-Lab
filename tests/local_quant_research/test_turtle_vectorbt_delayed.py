from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from scripts.research.local_quant_research.contracts import (  # noqa: E402
    ExecutionRun,
    LedgerInput,
    PreparedStrategy,
)
from scripts.research.local_quant_research.vectorbt_runtime import run_vectorbt  # noqa: E402

RESEARCH_ROOT = (
    Path(__file__).resolve().parents[2]
    / "joinquant"
    / "strategies"
    / "strategy-003"
    / "research"
)
sys.path.insert(0, str(RESEARCH_ROOT))

from turtle_etf._kernel import (  # noqa: E402
    ACTION_ENTRY,
    ACTION_FULL_EXIT,
    ACTION_REDISTRIBUTION_BUY,
    ACTION_REDISTRIBUTION_SELL,
    REASON_ENTRY_BREAKOUT,
    REASON_FULL_POSITION_REDISTRIBUTION,
    REASON_PROTECTIVE_STOP,
    TurtleContext,
    _params,
)
from turtle_etf._delayed import (  # noqa: E402
    ADJUST_CASH_TRUNCATED,
    ADJUST_HOLDING_TRUNCATED,
    ADJUST_NONE,
    build_delayed_program,
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
    risk_budgets: np.ndarray | None = None,
):
    config = {
        "research": {"initial_cash": initial_cash},
        "signal": {"add_step_n": 0.5, "stop_n": 2.0, "max_units": 4},
        "risk": {
            "lot_size": 100,
            "unit_risk_per_n": 0.01,
            "asset_group_unit_cap": 6.0,
            "portfolio_unit_cap": 12.0,
        },
        "costs": {"commission_multiplier": 1.0, "one_way_slippage": 0.0},
    }
    _, params = _params(config)
    rows, columns = inputs.close.shape
    trace = {
        **{
            name: value
            for name, value in vars(immediate).items()
            if name != "filled_quantities"
        },
        "candidate_base_quantities": np.zeros((rows, columns), dtype=np.int64),
        "event_group_scales": np.ones((rows, columns), dtype=np.float64),
        "event_portfolio_scales": np.ones(rows, dtype=np.float64),
        "event_cash_scales": np.ones(rows, dtype=np.float64),
        "event_risk_budgets": (
            np.full((rows, columns), 1e30, dtype=np.float64)
            if risk_budgets is None
            else risk_budgets
        ),
    }
    ledger_input = LedgerInput(
        dates=inputs.dates,
        symbols=inputs.securities,
        close=inputs.close,
        initial_cash=initial_cash,
        group_ids=np.zeros(columns, dtype=np.int64),
        cash_sharing=True,
        frequency="1D",
    )
    prepared = PreparedStrategy(
        ledger_input=ledger_input,
        primary_program=SimpleNamespace(),
        context=TurtleContext(inputs, params, "delayed-test", 1, initial_cash),
    )
    primary_orders = []
    for row, column in zip(*np.nonzero(immediate.filled_quantities > 0)):
        primary_orders.append(
            (
                f"{np.datetime_as_string(inputs.dates[row], unit='D')}T09:30:00",
                inputs.securities[column],
                int(immediate.filled_quantities[row, column]),
            )
        )
    primary = ExecutionRun(
        ledger=SimpleNamespace(
            orders=np.asarray(
                primary_orders,
                dtype=[("time", "U32"), ("security", "U64"), ("filled", "i8")],
            )
        ),
        trace=trace,
    )
    program = build_delayed_program(prepared, primary)
    assert program is not None
    execution = run_vectorbt(ledger_input, program)
    date_rows = {
        np.datetime_as_string(value, unit="D"): index
        for index, value in enumerate(inputs.dates)
    }
    security_columns = {
        security: index for index, security in enumerate(inputs.securities)
    }
    filled = np.zeros((rows, columns), dtype=np.int64)
    for order in execution.ledger.orders:
        filled[
            date_rows[str(order["time"])[:10]],
            security_columns[str(order["security"])],
        ] += int(order["filled"])
    quantities = np.zeros((rows, columns), dtype=np.int64)
    for asset in execution.ledger.assets:
        quantities[
            date_rows[str(asset["time"])[:10]],
            security_columns[str(asset["security"])],
        ] = int(round(float(asset["amount"])))
    result = SimpleNamespace(
        **execution.trace,
        execution=execution,
        filled_quantities=filled,
        state_quantities=quantities,
    )
    return program.inputs, result


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

    assert plan.plan_signal_n[1, 0] == 1.5
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


def test_delayed_redistribution_buy_is_capped_by_frozen_risk_budget() -> None:
    inputs = _inputs([[10.0], [10.0], [10.0], [20.0], [20.0]])
    immediate = _immediate(
        rows=5,
        columns=1,
        orders=[
            (0, 0, ACTION_ENTRY, REASON_ENTRY_BREAKOUT, 100),
            (
                2,
                0,
                ACTION_REDISTRIBUTION_BUY,
                REASON_FULL_POSITION_REDISTRIBUTION,
                200,
            ),
        ],
    )
    budgets = np.full((5, 1), np.inf, dtype=np.float64)
    budgets[0, 0] = 200.0
    budgets[2, 0] = 1400.0

    _, delayed = _run(inputs, immediate, risk_budgets=budgets)

    assert delayed.filled_quantities[3, 0] == 100
    assert delayed.execution_adjustment_codes[3, 0] == 5
    assert delayed.state_unit_counts[3, 0] == 1
    assert delayed.state_common_stop[3, 0] == pytest.approx(8.0)


def test_delayed_untradeable_over_budget_sell_blocks_redistribution_buy() -> None:
    inputs = _inputs(
        [[10.0, 10.0], [10.0, 10.0], [10.0, 10.0], [10.0, 20.0], [10.0, 20.0]]
    )
    inputs.low_limit[3, 0] = 10.0
    immediate = _immediate(
        rows=5,
        columns=2,
        orders=[
            (0, 0, ACTION_ENTRY, REASON_ENTRY_BREAKOUT, 100),
            (0, 1, ACTION_ENTRY, REASON_ENTRY_BREAKOUT, 100),
            (
                2,
                0,
                ACTION_REDISTRIBUTION_SELL,
                REASON_FULL_POSITION_REDISTRIBUTION,
                100,
            ),
            (
                2,
                1,
                ACTION_REDISTRIBUTION_BUY,
                REASON_FULL_POSITION_REDISTRIBUTION,
                100,
            ),
        ],
    )
    budgets = np.full((5, 2), 1e30, dtype=np.float64)
    budgets[0] = [200.0, 200.0]
    budgets[2] = [0.0, 1400.0]

    _, delayed = _run(inputs, immediate, risk_budgets=budgets)

    assert delayed.filled_quantities[3].tolist() == [0, 0]
    assert delayed.execution_adjustment_codes[3].tolist() == [3, 5]


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

    day = delayed.execution.ledger.orders
    assert day["security"].tolist() == ["ETF-A", "ETF-B"]


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

    day = delayed.execution.ledger.orders[
        np.char.startswith(delayed.execution.ledger.orders["time"], "2026-01-07")
    ]
    assert day["security"].tolist() == ["ETF-C", "ETF-A", "ETF-B"]
    assert delayed.filled_quantities[2].tolist() == [100, 100, 100]
    assert len(delayed.execution.ledger.orders) == 4


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

    day = delayed.execution.ledger.orders[
        np.char.startswith(delayed.execution.ledger.orders["time"], "2026-01-07")
    ]
    assert day["security"].tolist() == ["ETF-A", "ETF-B", "ETF-C"]
    assert delayed.state_quantities[2].tolist() == [100, 100, 300]
    assert delayed.state_unit_counts[2].tolist() == [1, 1, 1]
    assert delayed.state_common_stop[2].tolist() == [8.0, 9.0, 8.0]
    assert delayed.state_next_add_index[2].tolist() == [1, 1, 1]
