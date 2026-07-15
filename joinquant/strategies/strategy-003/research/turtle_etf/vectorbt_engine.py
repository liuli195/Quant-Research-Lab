from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

import numpy as np
import pandas as pd
import vectorbt as vbt

from .vectorbt_callbacks import (
    CallbackInputs,
    CallbackParams,
    CallbackState,
    order_func_nb,
    post_order_func_nb,
    pre_segment_func_nb,
    pre_sim_func_nb,
)
from .vectorbt_inputs import SimulationInputs


@dataclass(frozen=True)
class VectorbtSimulationResult:
    initial_cash: float
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


def _section(config: Mapping[str, object], name: str) -> Mapping[str, object]:
    value = config.get(name)
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} config must be an object")
    return value


def _number(
    section: Mapping[str, object],
    name: str,
    default: float | None = None,
    *,
    positive: bool = True,
) -> float:
    value = section.get(name, default)
    if value is None:
        raise ValueError(f"missing config value: {name}")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be numeric") from exc
    if not np.isfinite(result) or (positive and result <= 0.0):
        raise ValueError(f"{name} must be finite and positive")
    return result


def _params(config: Mapping[str, object]) -> tuple[float, CallbackParams]:
    research = _section(config, "research")
    signal = _section(config, "signal")
    risk = _section(config, "risk")
    costs_value = config.get("costs", {})
    if not isinstance(costs_value, Mapping):
        raise ValueError("costs config must be an object")
    initial_cash = _number(research, "initial_cash")
    lot_size_value = risk.get("lot_size", 100)
    if isinstance(lot_size_value, bool) or int(lot_size_value) != lot_size_value:
        raise ValueError("lot_size must be a positive integer")
    lot_size = int(lot_size_value)
    if lot_size <= 0:
        raise ValueError("lot_size must be a positive integer")
    slippage = _number(
        costs_value, "one_way_slippage", 0.0, positive=False
    )
    if slippage < 0.0 or slippage >= 1.0:
        raise ValueError("one_way_slippage must be between zero and one")
    return initial_cash, CallbackParams(
        lot_size=lot_size,
        risk_per_unit=_number(risk, "risk_per_unit"),
        add_step_n=_number(signal, "add_step_n"),
        stop_n=_number(signal, "stop_n"),
        security_risk_cap=_number(risk, "security_risk_cap"),
        security_value_cap=_number(risk, "security_value_cap"),
        asset_group_risk_cap=_number(risk, "asset_group_risk_cap"),
        asset_group_value_cap=_number(risk, "asset_group_value_cap"),
        portfolio_risk_cap=_number(risk, "portfolio_risk_cap"),
        portfolio_value_cap=_number(risk, "portfolio_value_cap"),
        target_volatility=_number(risk, "target_volatility"),
        risk_reduction_target_volatility=_number(
            risk, "risk_reduction_target_volatility"
        ),
        commission_multiplier=_number(
            costs_value, "commission_multiplier", 1.0
        ),
        one_way_slippage=slippage,
    )


def _mutable_state(rows: int, columns: int) -> CallbackState:
    return CallbackState(
        standard_unit=np.zeros(columns, dtype=np.int64),
        signal_n=np.full(columns, np.nan, dtype=np.float64),
        initial_fill_price=np.full(columns, np.nan, dtype=np.float64),
        common_stop=np.full(columns, np.nan, dtype=np.float64),
        next_add_index=np.zeros(columns, dtype=np.int64),
        batch_count=np.zeros(columns, dtype=np.int64),
        batch_quantities=np.zeros((columns, rows + 1), dtype=np.int64),
        batch_prices=np.full((columns, rows + 1), np.nan, dtype=np.float64),
        action_codes=np.zeros((rows, columns), dtype=np.int16),
        reason_codes=np.zeros((rows, columns), dtype=np.int16),
        requested_quantities=np.zeros((rows, columns), dtype=np.int64),
        planned_quantities=np.zeros((rows, columns), dtype=np.int64),
        filled_quantities=np.zeros((rows, columns), dtype=np.int64),
        fill_prices=np.full((rows, columns), np.nan, dtype=np.float64),
        fees=np.zeros((rows, columns), dtype=np.float64),
        state_quantities=np.zeros((rows, columns), dtype=np.int64),
        state_common_stop=np.full((rows, columns), np.nan, dtype=np.float64),
        state_next_add_index=np.zeros((rows, columns), dtype=np.int64),
        day_equity=np.full(rows, np.nan, dtype=np.float64),
        allocation_ready=np.zeros(rows, dtype=np.bool_),
    )


def _readonly_copy(values: np.ndarray) -> np.ndarray:
    result = np.ascontiguousarray(values.copy())
    result.setflags(write=False)
    return result


def run_vectorbt_simulation(
    inputs: SimulationInputs,
    config: Mapping[str, object],
) -> VectorbtSimulationResult:
    rows, columns = inputs.close.shape
    if rows == 0 or columns == 0:
        raise ValueError("simulation inputs must not be empty")
    expected_shape = (rows, columns)
    for name in (
        "execution_open",
        "paused",
        "high_limit",
        "low_limit",
        "signal_close",
        "signal_entry_high",
        "signal_exit_low",
        "signal_n",
        "covariance_eligible",
    ):
        if getattr(inputs, name).shape != expected_shape:
            raise ValueError(f"invalid simulation input shape: {name}")
    if inputs.covariance.shape != (rows, columns, columns):
        raise ValueError("invalid covariance shape")
    if len(inputs.securities) != columns or len(inputs.asset_groups) != columns:
        raise ValueError("simulation input identities do not match columns")

    initial_cash, params = _params(config)
    if params.risk_reduction_target_volatility >= params.target_volatility:
        raise ValueError("risk reduction target must be below target volatility")
    state = _mutable_state(rows, columns)
    callback_inputs = CallbackInputs(
        execution_open=inputs.execution_open,
        signal_close=inputs.signal_close,
        signal_entry_high=inputs.signal_entry_high,
        signal_exit_low=inputs.signal_exit_low,
        signal_n=inputs.signal_n,
        paused=inputs.paused,
        high_limit=inputs.high_limit,
        low_limit=inputs.low_limit,
        covariance=inputs.covariance,
        covariance_eligible=inputs.covariance_eligible,
        asset_group_ids=inputs.asset_group_ids,
    )
    close = pd.DataFrame(
        inputs.close,
        index=pd.DatetimeIndex(inputs.dates.astype("datetime64[ns]")),
        columns=inputs.securities,
    )
    portfolio = vbt.Portfolio.from_order_func(
        close,
        order_func_nb,
        pre_sim_func_nb=pre_sim_func_nb,
        pre_sim_args=(state, callback_inputs, params),
        pre_segment_func_nb=pre_segment_func_nb,
        post_order_func_nb=post_order_func_nb,
        init_cash=initial_cash,
        cash_sharing=True,
        group_by=True,
        call_pre_segment=True,
        update_value=True,
        ffill_val_price=True,
        max_orders=rows * columns,
        use_numba=True,
        freq="1D",
    )
    return VectorbtSimulationResult(
        initial_cash=initial_cash,
        portfolio=portfolio,
        action_codes=_readonly_copy(state.action_codes),
        reason_codes=_readonly_copy(state.reason_codes),
        requested_quantities=_readonly_copy(state.requested_quantities),
        planned_quantities=_readonly_copy(state.planned_quantities),
        filled_quantities=_readonly_copy(state.filled_quantities),
        fill_prices=_readonly_copy(state.fill_prices),
        fees=_readonly_copy(state.fees),
        state_quantities=_readonly_copy(state.state_quantities),
        state_common_stop=_readonly_copy(state.state_common_stop),
        state_next_add_index=_readonly_copy(state.state_next_add_index),
    )
