from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

from scripts.research.local_quant_research.contracts import FillEvent


RESEARCH_ROOT = (
    Path(__file__).resolve().parents[2]
    / "joinquant"
    / "strategies"
    / "strategy-003"
    / "research"
)
sys.path.insert(0, str(RESEARCH_ROOT))

from scripts.research.local_quant_research.vectorbt_runtime import run_vectorbt  # noqa: E402
from turtle_etf import _kernel as callbacks  # noqa: E402
from turtle_etf._kernel import (  # noqa: E402
    SimulationInputs,
    _prepare_turtle_inputs,
    _mutable_state,
    _params,
)


class _Result:
    def __init__(self, execution: object, inputs: SimulationInputs) -> None:
        self.execution = execution
        shape = inputs.close.shape
        rows = {
            np.datetime_as_string(value, unit="D"): index
            for index, value in enumerate(inputs.dates)
        }
        columns = {security: index for index, security in enumerate(inputs.securities)}
        self.filled_quantities = np.zeros(shape, dtype=np.int64)
        for order in execution.ledger.orders:
            self.filled_quantities[
                rows[str(order["time"])[:10]],
                columns[str(order["security"])],
            ] += int(order["filled"])
        self.state_quantities = np.zeros(shape, dtype=np.int64)
        for asset in execution.ledger.assets:
            self.state_quantities[
                rows[str(asset["time"])[:10]],
                columns[str(asset["security"])],
            ] = int(round(float(asset["amount"])))

    def __getattr__(self, name: str) -> object:
        return self.execution.trace[name]


def _run(inputs: SimulationInputs, config: dict[str, object]) -> _Result:
    config = {**config, "scenario_id": "callback-test"}
    prepared = _prepare_turtle_inputs(inputs, config)
    return _Result(
        run_vectorbt(prepared.ledger_input, prepared.primary_program),
        inputs,
    )


def _ro(values: object, dtype: str) -> np.ndarray:
    result = np.ascontiguousarray(values, dtype=dtype)
    result.setflags(write=False)
    return result


def _inputs(
    *,
    opens: list[list[float]],
    signal_close: list[list[float]],
    entry_high: list[list[float]],
    exit_low: list[list[float]] | None = None,
    signal_n: list[list[float]] | None = None,
    paused: list[list[bool]] | None = None,
    high_limit: list[list[float]] | None = None,
    low_limit: list[list[float]] | None = None,
    group_ids: list[int] | None = None,
) -> SimulationInputs:
    open_array = np.asarray(opens, dtype=np.float64)
    rows, columns = open_array.shape
    securities = tuple(f"ETF-{chr(65 + column)}" for column in range(columns))
    close = np.where(np.isfinite(open_array), open_array, 10.0)
    ids = np.arange(columns) if group_ids is None else np.asarray(group_ids)
    return SimulationInputs(
        dates=_ro(
            np.arange(
                np.datetime64("2026-01-05"),
                np.datetime64("2026-01-05") + np.timedelta64(rows, "D"),
            ),
            "datetime64[D]",
        ),
        securities=securities,
        asset_groups=tuple(f"group-{group}" for group in ids),
        asset_group_ids=_ro(ids, "int64"),
        raw_open=_ro(open_array, "float64"),
        raw_high=_ro(open_array, "float64"),
        raw_low=_ro(open_array, "float64"),
        raw_close=_ro(close, "float64"),
        raw_pre_close=_ro(close, "float64"),
        continuous_open=_ro(open_array, "float64"),
        continuous_high=_ro(open_array, "float64"),
        continuous_low=_ro(open_array, "float64"),
        continuous_close=_ro(close, "float64"),
        continuous_pre_close=_ro(close, "float64"),
        continuity_factor=_ro(np.ones((rows, columns)), "float64"),
        corporate_action_applied=_ro(
            np.zeros((rows, columns), dtype=np.bool_), "bool"
        ),
        corporate_actions_digest=(
            "4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e4d8e6f7a0f8f0d7c6b3f3b"
        ),
        corporate_action_applications=(),
        paused=_ro(
            np.zeros((rows, columns), dtype=np.bool_) if paused is None else paused,
            "bool",
        ),
        high_limit=_ro(
            np.full((rows, columns), np.nan) if high_limit is None else high_limit,
            "float64",
        ),
        low_limit=_ro(
            np.full((rows, columns), np.nan) if low_limit is None else low_limit,
            "float64",
        ),
        signal_source_index=_ro(np.arange(rows) - 1, "int64"),
        signal_close=_ro(signal_close, "float64"),
        signal_entry_high=_ro(entry_high, "float64"),
        signal_exit_low=_ro(
            np.full((rows, columns), np.nan) if exit_low is None else exit_low,
            "float64",
        ),
        signal_n=_ro(
            np.ones((rows, columns)) if signal_n is None else signal_n,
            "float64",
        ),
    )


def _config(
    initial_cash: float = 100_000.0,
    *,
    unit_risk: float = 0.01,
    group_cap: float = 6.0,
    portfolio_cap: float = 12.0,
) -> dict[str, object]:
    return {
        "research": {"initial_cash": initial_cash},
        "signal": {"add_step_n": 0.5, "stop_n": 2.0, "max_units": 4},
        "risk": {
            "unit_risk_per_n": unit_risk,
            "asset_group_unit_cap": group_cap,
            "portfolio_unit_cap": portfolio_cap,
        },
        "costs": {"commission_multiplier": 1.0, "one_way_slippage": 0.0},
    }


def test_group_and_portfolio_unit_scales_follow_confirmed_formula() -> None:
    group_scales, portfolio_scale = callbacks._risk_scales_nb.py_func(
        np.asarray([4, 4, 4], dtype=np.int64),
        np.asarray([0, 0, 1], dtype=np.int64),
        2,
        6.0,
        12.0,
    )
    assert group_scales.tolist() == pytest.approx([0.75, 1.0])
    assert portfolio_scale == pytest.approx(1.0)

    group_scales, portfolio_scale = callbacks._risk_scales_nb.py_func(
        np.asarray([4, 4, 4, 4], dtype=np.int64),
        np.asarray([0, 1, 2, 3], dtype=np.int64),
        4,
        6.0,
        12.0,
    )
    assert group_scales.tolist() == pytest.approx([1.0, 1.0, 1.0, 1.0])
    assert portfolio_scale == pytest.approx(0.75)


def test_target_rounding_is_uniform_and_input_order_invariant() -> None:
    bases = np.asarray([[1000, 0, 0, 0], [2000, 0, 0, 0]], dtype=np.int64)
    counts = np.asarray([1, 1], dtype=np.int64)
    groups = np.asarray([0, 1], dtype=np.int64)
    scales = np.asarray([1.0, 1.0])
    locked = np.asarray([-1, -1], dtype=np.int64)

    targets = callbacks._targets_for_scale_nb.py_func(
        bases, counts, groups, scales, 1.0, 0.55, locked, 100
    )
    permuted = callbacks._targets_for_scale_nb.py_func(
        bases[::-1], counts[::-1], groups[::-1], scales, 1.0, 0.55, locked, 100
    )[::-1]

    assert targets.tolist() == [500, 1100]
    assert targets.tolist() == permuted.tolist()
    assert np.all(targets % 100 == 0)


def test_late_breakout_displaces_earlier_position_without_changing_its_unit() -> None:
    inputs = _inputs(
        opens=[[10.0, 10.0], [10.0, 10.0], [10.0, 10.0]],
        signal_close=[[11.0, np.nan], [10.0, 11.0], [10.0, 10.0]],
        entry_high=[[10.0, np.nan], [20.0, 10.0], [20.0, 20.0]],
    )
    result = _run(
        inputs, _config(initial_cash=100_005.0, portfolio_cap=1.0)
    )

    assert result.state_quantities[0].tolist() == [1000, 0]
    assert result.action_codes[1].tolist() == [
        callbacks.ACTION_REDISTRIBUTION_SELL,
        callbacks.ACTION_ENTRY,
    ]
    assert result.state_quantities[1].tolist() == [500, 500]
    assert result.state_unit_counts[1].tolist() == [1, 1]
    assert result.event_portfolio_scales[1] == pytest.approx(0.5)
    assert result.state_common_stop[1, 0] == result.state_common_stop[0, 0]
    assert result.action_codes[2].tolist() == [callbacks.ACTION_NONE] * 2
    assert result.filled_quantities[2].tolist() == [0, 0]


def test_same_group_units_scale_uniformly() -> None:
    inputs = _inputs(
        opens=[[10.0, 10.0], [10.0, 10.0]],
        signal_close=[[11.0, np.nan], [10.0, 11.0]],
        entry_high=[[10.0, np.nan], [20.0, 10.0]],
        group_ids=[0, 0],
    )
    result = _run(
        inputs, _config(initial_cash=100_005.0, group_cap=1.0)
    )

    assert result.state_quantities[1].tolist() == [500, 500]
    assert result.event_group_scales[1].tolist() == pytest.approx([0.5, 0.5])
    assert result.event_portfolio_scales[1] == pytest.approx(1.0)


def test_each_filled_unit_freezes_its_own_n_and_only_raises_common_stop() -> None:
    inputs = _inputs(
        opens=[[10.0], [13.0], [13.0]],
        signal_close=[[11.0], [10.6], [10.0]],
        entry_high=[[10.0], [20.0], [20.0]],
        signal_n=[[1.0], [2.0], [999.0]],
    )
    result = _run(inputs, _config())

    assert result.state_unit_counts[:, 0].tolist() == [1, 2, 2]
    assert result.state_common_stop[0, 0] == pytest.approx(8.0)
    assert result.state_common_stop[1, 0] == pytest.approx(9.0)
    assert result.state_common_stop[2, 0] == pytest.approx(9.0)
    assert result.state_next_add_index[:, 0].tolist() == [1, 2, 2]


def test_additions_use_fixed_initial_levels_one_per_day_and_stop_at_four() -> None:
    inputs = _inputs(
        opens=[[10.0], [11.0], [11.5], [12.0], [12.5]],
        signal_close=[[11.0], [12.0], [12.0], [12.0], [12.0]],
        entry_high=[[10.0], [20.0], [20.0], [20.0], [20.0]],
        signal_n=[[1.0], [8.0], [8.0], [8.0], [8.0]],
    )
    result = _run(inputs, _config())

    assert result.action_codes[:, 0].tolist() == [
        callbacks.ACTION_ENTRY,
        callbacks.ACTION_ADDITION,
        callbacks.ACTION_ADDITION,
        callbacks.ACTION_ADDITION,
        callbacks.ACTION_NONE,
    ]
    assert result.state_unit_counts[:, 0].tolist() == [1, 2, 3, 4, 4]


def test_untradeable_candidate_does_not_advance_unit_stop_or_add_level() -> None:
    inputs = _inputs(
        opens=[[10.0], [10.0], [11.0]],
        signal_close=[[11.0], [11.0], [11.0]],
        entry_high=[[10.0], [10.0], [10.0]],
        paused=[[True], [False], [False]],
        high_limit=[[np.nan], [np.nan], [11.0]],
    )
    result = _run(inputs, _config())

    assert result.reason_codes[0, 0] == callbacks.REASON_PAUSED
    assert result.state_unit_counts[0, 0] == 0
    assert result.state_unit_counts[1, 0] == 1
    assert result.reason_codes[2, 0] == callbacks.REASON_HIGH_LIMIT
    assert result.state_unit_counts[2, 0] == 1
    assert result.state_next_add_index[2, 0] == 1
    assert result.state_common_stop[2, 0] == result.state_common_stop[1, 0]


def test_exit_sells_before_same_day_entry_uses_released_cash() -> None:
    inputs = _inputs(
        opens=[[10.0, 20.0], [12.0, 20.0], [12.0, 20.0]],
        signal_close=[[11.0, np.nan], [5.0, 21.0], [np.nan, np.nan]],
        entry_high=[[10.0, np.nan], [np.nan, 20.0], [np.nan, np.nan]],
        exit_low=[[np.nan, np.nan], [6.0, np.nan], [np.nan, np.nan]],
    )
    result = _run(inputs, _config(initial_cash=10_005.0))
    day_two = result.execution.ledger.orders[
        np.char.startswith(
            result.execution.ledger.orders["time"],
            "2026-01-06",
        )
    ]

    assert day_two["action"].tolist() == ["close", "open"]
    assert result.state_quantities[1, 0] == 0
    assert result.state_unit_counts[1, 0] == 0
    assert result.state_quantities[1, 1] > 0


def test_low_limit_blocks_full_exit_and_preserves_unit_state() -> None:
    inputs = _inputs(
        opens=[[10.0], [8.0]],
        signal_close=[[11.0], [5.0]],
        entry_high=[[10.0], [20.0]],
        exit_low=[[np.nan], [6.0]],
        low_limit=[[np.nan], [8.0]],
    )
    result = _run(inputs, _config())

    assert result.reason_codes[1, 0] == callbacks.REASON_LOW_LIMIT
    assert result.filled_quantities[1, 0] == 0
    assert result.state_quantities[1, 0] == result.state_quantities[0, 0]
    assert result.state_unit_counts[1, 0] == 1
    assert result.state_common_stop[1, 0] == result.state_common_stop[0, 0]


def test_rejected_official_order_does_not_establish_candidate() -> None:
    inputs = _inputs(
        opens=[[10.0]], signal_close=[[11.0]], entry_high=[[10.0]]
    )
    _, params = _params(_config())
    state = _mutable_state(1, 1, 1, 4)
    state.action_codes[0, 0] = callbacks.ACTION_ENTRY
    state.candidate_signal_n[0, 0] = 1.0
    state.candidate_base_quantity[0, 0] = 1000
    callback_inputs = callbacks.CallbackInputs(
        inputs.execution_open,
        inputs.signal_close,
        inputs.signal_entry_high,
        inputs.signal_exit_low,
        inputs.signal_n,
        inputs.paused,
        inputs.high_limit,
        inputs.low_limit,
        inputs.asset_group_ids,
    )
    event = FillEvent(
        row=0,
        column=0,
        status=callbacks.FILL_REJECTED,
        side=callbacks.SIDE_BUY,
        size=100.0,
        price=10.0,
        fees=5.0,
        cash_after=100_000.0,
        position_after=0.0,
    )

    callbacks.after_fill_nb.py_func(
        event,
        callback_inputs,
        params,
        state,
        (),
        (),
    )

    assert state.reason_codes[0, 0] == callbacks.REASON_ORDER_REJECTED
    assert state.unit_count[0] == 0
