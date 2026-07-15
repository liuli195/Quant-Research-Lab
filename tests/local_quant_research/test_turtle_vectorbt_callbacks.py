from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from vectorbt.portfolio.enums import OrderResult, OrderSide, OrderStatus


RESEARCH_ROOT = (
    Path(__file__).resolve().parents[2]
    / "joinquant"
    / "strategies"
    / "strategy-003"
    / "research"
)
sys.path.insert(0, str(RESEARCH_ROOT))

from turtle_etf.vectorbt_callbacks import (  # noqa: E402
    ACTION_ADDITION,
    ACTION_ENTRY,
    ACTION_RISK_REDUCTION,
    CallbackInputs,
    REASON_HIGH_LIMIT,
    REASON_HELD_RISK_INPUT_MISSING,
    REASON_LOW_LIMIT,
    REASON_ORDER_REJECTED,
    REASON_PAUSED,
    order_func_nb,
    post_order_func_nb,
    pre_segment_func_nb,
    pre_sim_func_nb,
)
from turtle_etf.vectorbt_engine import run_vectorbt_simulation  # noqa: E402
from turtle_etf.vectorbt_engine import (  # noqa: E402
    _mutable_state,
    _params,
)
from turtle_etf.vectorbt_inputs import SimulationInputs  # noqa: E402


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
) -> SimulationInputs:
    open_array = np.asarray(opens, dtype=np.float64)
    rows, columns = open_array.shape
    securities = tuple(f"ETF-{chr(65 + column)}" for column in range(columns))
    close = np.where(np.isfinite(open_array), open_array, 10.0)
    covariance = np.zeros((rows, columns, columns), dtype=np.float64)
    for row in range(rows):
        covariance[row] = np.eye(columns) * 0.000001
    return SimulationInputs(
        dates=_ro(
            np.arange(
                np.datetime64("2026-01-05"),
                np.datetime64("2026-01-05") + np.timedelta64(rows, "D"),
            ),
            "datetime64[D]",
        ),
        securities=securities,
        asset_groups=tuple(f"group-{column}" for column in range(columns)),
        asset_group_ids=_ro(np.arange(columns), "int64"),
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
        covariance=_ro(covariance, "float64"),
        covariance_eligible=_ro(np.ones((rows, columns), dtype=np.bool_), "bool"),
    )


def _config(initial_cash: float = 10_000.0) -> dict[str, object]:
    return {
        "research": {"initial_cash": initial_cash},
        "signal": {"add_step_n": 0.5, "stop_n": 2.0},
        "risk": {
            "risk_per_unit": 0.5,
            "security_risk_cap": 1.0,
            "security_value_cap": 1.0,
            "asset_group_risk_cap": 1.0,
            "asset_group_value_cap": 1.0,
            "portfolio_risk_cap": 1.0,
            "portfolio_value_cap": 1.0,
            "target_volatility": 10.0,
            "risk_reduction_target_volatility": 9.5,
            "minimum_aligned_samples": 2,
        },
        "costs": {"commission_multiplier": 1.0, "one_way_slippage": 0.0},
    }


def _with_asset_groups(
    inputs: SimulationInputs,
    group_ids: list[int],
) -> SimulationInputs:
    return SimulationInputs(
        **{
            **vars(inputs),
            "asset_groups": tuple(f"group-{group}" for group in group_ids),
            "asset_group_ids": _ro(group_ids, "int64"),
        }
    )


def test_official_callbacks_share_cash_and_a1_scales_all_breakouts() -> None:
    inputs = _inputs(
        opens=[[10.0, 10.0], [10.0, 10.0]],
        signal_close=[[11.0, 11.0], [np.nan, np.nan]],
        entry_high=[[10.0, 10.0], [np.nan, np.nan]],
    )

    result = run_vectorbt_simulation(inputs, _config())

    assert result.portfolio.wrapper.grouper.get_group_lens().tolist() == [2]
    assert result.action_codes[0].tolist() == [ACTION_ENTRY, ACTION_ENTRY]
    assert result.requested_quantities[0].tolist() == [2500, 2500]
    assert 0 < result.filled_quantities[0, 0]
    assert 0 < result.filled_quantities[0, 1]
    assert abs(result.filled_quantities[0, 0] - result.filled_quantities[0, 1]) <= 100
    assert float(result.portfolio.cash().iloc[-1]) >= 0.0
    assert result.state_quantities[0].tolist() == result.filled_quantities[0].tolist()
    for callback in (
        pre_sim_func_nb,
        pre_segment_func_nb,
        order_func_nb,
        post_order_func_nb,
    ):
        assert callback.nopython_signatures


def test_passive_security_cap_breach_allows_another_security_entry() -> None:
    inputs = _inputs(
        opens=[[10.0, 10.0], [40.0, 10.0], [40.0, 10.0]],
        signal_close=[[11.0, np.nan], [10.0, 11.0], [np.nan, np.nan]],
        entry_high=[[10.0, np.nan], [20.0, 10.0], [np.nan, np.nan]],
    )
    config = _config()
    config["risk"]["risk_per_unit"] = 0.021
    config["risk"]["security_value_cap"] = 0.30

    result = run_vectorbt_simulation(inputs, config)

    assert result.state_quantities[0].tolist() == [100, 0]
    assert result.filled_quantities[1, 0] == 0
    assert result.filled_quantities[1, 1] == 100


def test_passive_security_cap_breach_blocks_same_security_addition() -> None:
    inputs = _inputs(
        opens=[[10.0], [40.0], [40.0]],
        signal_close=[[11.0], [11.0], [np.nan]],
        entry_high=[[10.0], [20.0], [np.nan]],
    )
    config = _config()
    config["risk"]["risk_per_unit"] = 0.021
    config["risk"]["security_value_cap"] = 0.30

    result = run_vectorbt_simulation(inputs, config)

    assert result.action_codes[1, 0] == ACTION_ADDITION
    assert result.filled_quantities[1, 0] == 0
    assert result.state_quantities[1, 0] == result.state_quantities[0, 0]


def test_passive_group_cap_breach_blocks_same_group_only() -> None:
    inputs = _with_asset_groups(
        _inputs(
            opens=[
                [10.0, 10.0, 10.0],
                [60.0, 10.0, 10.0],
                [60.0, 10.0, 10.0],
            ],
            signal_close=[
                [11.0, np.nan, np.nan],
                [10.0, 11.0, 11.0],
                [np.nan, np.nan, np.nan],
            ],
            entry_high=[
                [10.0, np.nan, np.nan],
                [20.0, 10.0, 10.0],
                [np.nan, np.nan, np.nan],
            ],
        ),
        [0, 0, 1],
    )
    config = _config()
    config["risk"]["risk_per_unit"] = 0.021
    config["risk"]["asset_group_value_cap"] = 0.50

    result = run_vectorbt_simulation(inputs, config)

    assert result.state_quantities[0].tolist() == [100, 0, 0]
    assert result.filled_quantities[1, 1] == 0
    assert result.filled_quantities[1, 2] == 100


def test_passive_value_cap_breach_does_not_block_trend_exit() -> None:
    inputs = _inputs(
        opens=[[10.0], [40.0], [40.0]],
        signal_close=[[11.0], [5.0], [np.nan]],
        entry_high=[[10.0], [20.0], [np.nan]],
        exit_low=[[np.nan], [6.0], [np.nan]],
    )
    config = _config()
    config["risk"]["risk_per_unit"] = 0.021
    config["risk"]["security_value_cap"] = 0.30

    result = run_vectorbt_simulation(inputs, config)

    assert result.filled_quantities[1, 0] == 100
    assert result.state_quantities[1, 0] == 0


@pytest.mark.parametrize(
    "local_cap",
    ["security_risk_cap", "asset_group_risk_cap"],
)
def test_local_risk_cap_candidate_does_not_freeze_unrelated_entry(
    local_cap: str,
) -> None:
    inputs = _inputs(
        opens=[[10.0, 10.0], [10.0, 10.0], [10.0, 10.0]],
        signal_close=[[11.0, np.nan], [11.0, 11.0], [np.nan, np.nan]],
        entry_high=[[10.0, np.nan], [20.0, 10.0], [np.nan, np.nan]],
    )
    config = _config()
    config["risk"]["risk_per_unit"] = 0.021
    config["risk"][local_cap] = 0.025

    result = run_vectorbt_simulation(inputs, config)

    assert result.state_quantities[0].tolist() == [100, 0]
    assert result.action_codes[1].tolist() == [ACTION_ADDITION, ACTION_ENTRY]
    assert result.filled_quantities[1, 0] == 0
    assert result.filled_quantities[1, 1] == 100


def test_exit_fills_before_same_day_a1_buy_uses_released_cash() -> None:
    inputs = _inputs(
        opens=[[10.0, 20.0], [12.0, 20.0], [12.0, 20.0]],
        signal_close=[[11.0, np.nan], [5.0, 21.0], [np.nan, np.nan]],
        entry_high=[[10.0, np.nan], [np.nan, 20.0], [np.nan, np.nan]],
        exit_low=[[np.nan, np.nan], [6.0, np.nan], [np.nan, np.nan]],
    )

    result = run_vectorbt_simulation(inputs, _config())
    orders = result.portfolio.orders.records_readable

    day_two = orders.loc[orders["Timestamp"] == np.datetime64("2026-01-06")]
    assert day_two["Side"].tolist() == ["Sell", "Buy"]
    assert result.state_quantities[1, 0] == 0
    assert result.state_quantities[1, 1] > 0
    assert result.filled_quantities[1, 1] > 0


def test_untradeable_orders_do_not_advance_turtle_state() -> None:
    inputs = _inputs(
        opens=[[10.0, 10.0], [10.0, 10.0], [11.0, 11.0], [11.0, 11.0]],
        signal_close=[[11.0, 11.0], [11.0, 11.0], [12.0, 12.0], [12.0, 12.0]],
        entry_high=[[10.0, 10.0], [10.0, 10.0], [10.0, 10.0], [10.0, 10.0]],
        paused=[[True, False], [False, False], [False, False], [False, False]],
        high_limit=[[np.nan, 10.0], [np.nan, np.nan], [11.0, 11.0], [np.nan, np.nan]],
    )

    config = _config()
    config["risk"]["risk_per_unit"] = 0.05
    result = run_vectorbt_simulation(inputs, config)

    assert result.reason_codes[0].tolist() == [REASON_PAUSED, REASON_HIGH_LIMIT]
    assert result.state_quantities[0].tolist() == [0, 0]
    assert result.state_quantities[1, 0] > 0
    assert result.state_quantities[1, 1] > 0
    before_add = result.state_next_add_index[2].copy()
    assert result.action_codes[2].tolist() == [ACTION_ADDITION, ACTION_ADDITION]
    assert result.state_quantities[2].tolist() == result.state_quantities[1].tolist()
    assert result.state_next_add_index[2].tolist() == before_add.tolist()
    assert result.state_quantities[3, 0] > result.state_quantities[2, 0]
    assert result.state_quantities[3, 1] > result.state_quantities[2, 1]
    assert result.state_next_add_index[3].tolist() == [2, 2]


def test_high_close_risk_reduces_positions_before_any_new_risk() -> None:
    inputs = _inputs(
        opens=[[10.0, 10.0], [10.0, 10.0], [10.0, 10.0]],
        signal_close=[[11.0, 11.0], [10.0, 10.0], [10.0, 10.0]],
        entry_high=[[10.0, 10.0], [20.0, 20.0], [20.0, 20.0]],
    )
    covariance = inputs.covariance.copy()
    covariance[1] = np.eye(2) * 0.25
    covariance.setflags(write=False)
    inputs = SimulationInputs(**{**vars(inputs), "covariance": covariance})
    config = _config()
    config["risk"]["risk_per_unit"] = 0.05
    config["risk"]["target_volatility"] = 0.20
    config["risk"]["risk_reduction_target_volatility"] = 0.15

    result = run_vectorbt_simulation(inputs, config)

    assert result.state_quantities[0].min() > 0
    assert result.action_codes[1].tolist() == [
        ACTION_RISK_REDUCTION,
        ACTION_RISK_REDUCTION,
    ]
    assert result.filled_quantities[1].min() > 0
    assert np.all(result.state_quantities[1] < result.state_quantities[0])
    order_counts = (
        result.portfolio.orders.records_readable.groupby(["Timestamp", "Column"])
        .size()
        .to_numpy()
    )
    assert np.all(order_counts <= 1)


def test_gap_open_applies_slippage_fee_and_fill_based_common_stop() -> None:
    inputs = _inputs(
        opens=[[12.0], [12.0]],
        signal_close=[[11.0], [np.nan]],
        entry_high=[[10.0], [np.nan]],
    )
    config = _config()
    config["risk"]["risk_per_unit"] = 0.05
    config["costs"]["one_way_slippage"] = 0.01

    result = run_vectorbt_simulation(inputs, config)

    assert result.filled_quantities[0, 0] == 200
    assert result.fill_prices[0, 0] == pytest.approx(12.12)
    assert result.fees[0, 0] == 5.0
    assert result.state_common_stop[0, 0] == pytest.approx(10.12)


def test_low_limit_blocks_exit_and_preserves_batches() -> None:
    inputs = _inputs(
        opens=[[10.0], [8.0], [8.0]],
        signal_close=[[11.0], [5.0], [np.nan]],
        entry_high=[[10.0], [np.nan], [np.nan]],
        exit_low=[[np.nan], [6.0], [np.nan]],
        low_limit=[[np.nan], [8.0], [np.nan]],
    )
    config = _config()
    config["risk"]["risk_per_unit"] = 0.05

    result = run_vectorbt_simulation(inputs, config)

    assert result.state_quantities[0, 0] > 0
    assert result.reason_codes[1, 0] == REASON_LOW_LIMIT
    assert result.filled_quantities[1, 0] == 0
    assert result.state_quantities[1, 0] == result.state_quantities[0, 0]
    assert result.state_common_stop[1, 0] == result.state_common_stop[0, 0]


def test_missing_held_open_stops_new_risk_but_keeps_existing_state() -> None:
    inputs = _inputs(
        opens=[[10.0, 20.0], [np.nan, 20.0], [10.0, 20.0]],
        signal_close=[[11.0, np.nan], [10.0, 21.0], [np.nan, np.nan]],
        entry_high=[[10.0, np.nan], [20.0, 20.0], [np.nan, np.nan]],
    )
    config = _config()
    config["risk"]["risk_per_unit"] = 0.05

    result = run_vectorbt_simulation(inputs, config)

    assert result.state_quantities[0, 0] > 0
    assert result.reason_codes[1, 1] == REASON_HELD_RISK_INPUT_MISSING
    assert result.filled_quantities[1, 1] == 0
    assert result.state_quantities[1, 0] == result.state_quantities[0, 0]
    assert result.state_quantities[1, 1] == 0


def test_missing_held_signal_close_stops_all_new_risk() -> None:
    inputs = _inputs(
        opens=[[10.0, 20.0], [10.0, 20.0], [10.0, 20.0]],
        signal_close=[[11.0, np.nan], [np.nan, 21.0], [np.nan, np.nan]],
        entry_high=[[10.0, np.nan], [20.0, 20.0], [np.nan, np.nan]],
    )
    config = _config()
    config["risk"]["risk_per_unit"] = 0.05

    result = run_vectorbt_simulation(inputs, config)

    assert result.state_quantities[0, 0] > 0
    assert result.reason_codes[1, 1] == REASON_HELD_RISK_INPUT_MISSING
    assert result.filled_quantities[1, 1] == 0
    assert result.state_quantities[1, 0] == result.state_quantities[0, 0]
    assert result.state_quantities[1, 1] == 0


def test_rejected_official_order_result_does_not_advance_state() -> None:
    inputs = _inputs(
        opens=[[10.0]],
        signal_close=[[11.0]],
        entry_high=[[10.0]],
    )
    _, params = _params(_config())
    state = _mutable_state(1, 1)
    state.action_codes[0, 0] = ACTION_ENTRY
    callback_inputs = CallbackInputs(
        inputs.execution_open,
        inputs.signal_close,
        inputs.signal_entry_high,
        inputs.signal_exit_low,
        inputs.signal_n,
        inputs.paused,
        inputs.high_limit,
        inputs.low_limit,
        inputs.covariance,
        inputs.covariance_eligible,
        inputs.asset_group_ids,
    )
    context = SimpleNamespace(
        i=0,
        col=0,
        call_idx=0,
        group_len=1,
        from_col=0,
        to_col=1,
        position_now=0.0,
        last_position=np.zeros(1, dtype=np.float64),
        order_result=OrderResult(
            size=100.0,
            price=10.0,
            fees=5.0,
            side=OrderSide.Buy,
            status=OrderStatus.Rejected,
            status_info=0,
        ),
    )

    post_order_func_nb.py_func(context, state, callback_inputs, params)

    assert state.reason_codes[0, 0] == REASON_ORDER_REJECTED
    assert state.state_quantities[0, 0] == 0
    assert state.batch_count[0] == 0
