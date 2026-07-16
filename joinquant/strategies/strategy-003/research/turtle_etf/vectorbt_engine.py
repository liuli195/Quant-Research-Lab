from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

import numpy as np
import pandas as pd
import vectorbt as vbt

from .vectorbt_callbacks import (
    ACTION_NONE,
    CallbackInputs,
    CallbackParams,
    CallbackState,
    order_func_nb,
    post_order_func_nb,
    post_segment_func_nb,
    pre_segment_func_nb,
    pre_sim_func_nb,
)
from .vectorbt_delayed import freeze_order_plan, run_delayed_execution
from .vectorbt_inputs import SimulationInputs


@dataclass(frozen=True)
class VectorbtSimulationResult:
    initial_cash: float
    asset_group_unit_cap: float
    portfolio_unit_cap: float
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
    candidate_base_quantities: np.ndarray
    event_group_scales: np.ndarray
    event_portfolio_scales: np.ndarray
    event_cash_scales: np.ndarray
    day_equity: np.ndarray
    planned_row_indices: np.ndarray
    execution_adjustment_codes: np.ndarray
    frozen_signal_n: np.ndarray
    execution_delay_days: int
    execution_sequence: tuple[tuple[str, ...], ...]
    horizon_expired_orders: tuple[object, ...]


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


_LEGACY_RISK_FIELDS = frozenset(
    {
        "security_risk_cap",
        "security_value_cap",
        "asset_group_risk_cap",
        "asset_group_value_cap",
        "portfolio_risk_cap",
        "portfolio_value_cap",
        "covariance",
        "target_volatility",
        "risk_reduction_target_volatility",
        "minimum_aligned_samples",
    }
)


def _reject_legacy_risk_fields(risk: Mapping[str, object]) -> None:
    found = sorted(set(risk) & _LEGACY_RISK_FIELDS)
    if found:
        raise ValueError(
            "legacy risk fields are not supported: " + ", ".join(found)
        )


def _params(config: Mapping[str, object]) -> tuple[float, CallbackParams]:
    research = _section(config, "research")
    signal = _section(config, "signal")
    risk = _section(config, "risk")
    costs_value = config.get("costs", {})
    if not isinstance(costs_value, Mapping):
        raise ValueError("costs config must be an object")
    initial_cash = _number(research, "initial_cash")
    _reject_legacy_risk_fields(risk)
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
    if "max_units" not in signal:
        raise ValueError("missing config value: max_units")
    max_units_value = signal["max_units"]
    if (
        isinstance(max_units_value, bool)
        or not isinstance(max_units_value, int)
        or max_units_value != 4
    ):
        raise ValueError("max_units must equal four")
    max_units = max_units_value
    return initial_cash, CallbackParams(
        lot_size=lot_size,
        unit_risk_per_n=_number(risk, "unit_risk_per_n"),
        add_step_n=_number(signal, "add_step_n"),
        stop_n=_number(signal, "stop_n"),
        max_units=max_units,
        asset_group_unit_cap=_number(risk, "asset_group_unit_cap"),
        portfolio_unit_cap=_number(risk, "portfolio_unit_cap"),
        commission_multiplier=_number(
            costs_value, "commission_multiplier", 1.0
        ),
        one_way_slippage=slippage,
    )


def _mutable_state(
    rows: int, columns: int, group_count: int, max_units: int
) -> CallbackState:
    if rows <= 0 or columns <= 0 or group_count <= 0 or max_units != 4:
        raise ValueError("invalid callback state dimensions")
    return CallbackState(
        unit_count=np.zeros(columns, dtype=np.int64),
        unit_signal_n=np.full(
            (columns, max_units), np.nan, dtype=np.float64
        ),
        unit_base_quantities=np.zeros(
            (columns, max_units), dtype=np.int64
        ),
        unit_fill_prices=np.full(
            (columns, max_units), np.nan, dtype=np.float64
        ),
        initial_fill_price=np.full(columns, np.nan, dtype=np.float64),
        initial_signal_n=np.full(columns, np.nan, dtype=np.float64),
        common_stop=np.full(columns, np.nan, dtype=np.float64),
        next_add_index=np.zeros(columns, dtype=np.int64),
        candidate_signal_n=np.full(
            (rows, columns), np.nan, dtype=np.float64
        ),
        candidate_base_quantity=np.zeros((rows, columns), dtype=np.int64),
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
        state_unit_counts=np.zeros((rows, columns), dtype=np.int64),
        event_group_scales=np.ones((rows, columns), dtype=np.float64),
        event_portfolio_scales=np.ones(rows, dtype=np.float64),
        event_cash_scales=np.ones(rows, dtype=np.float64),
        day_equity=np.full(rows, np.nan, dtype=np.float64),
        allocation_ready=np.zeros(rows, dtype=np.bool_),
    )


def _readonly_copy(values: np.ndarray) -> np.ndarray:
    result = np.ascontiguousarray(values.copy())
    result.setflags(write=False)
    return result


def _delay_days(config: Mapping[str, object]) -> int:
    execution = config.get("execution", {})
    if not isinstance(execution, Mapping):
        raise ValueError("execution config must be an object")
    value = execution.get("additional_delay_days", 0)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("additional_delay_days must be a non-negative integer")
    return value


def _run_immediate(
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
    ):
        if getattr(inputs, name).shape != expected_shape:
            raise ValueError(f"invalid simulation input shape: {name}")
    if len(inputs.securities) != columns or len(inputs.asset_groups) != columns:
        raise ValueError("simulation input identities do not match columns")

    initial_cash, params = _params(config)
    group_count = int(np.max(inputs.asset_group_ids)) + 1
    state = _mutable_state(rows, columns, group_count, params.max_units)
    callback_inputs = CallbackInputs(
        execution_open=inputs.execution_open,
        signal_close=inputs.signal_close,
        signal_entry_high=inputs.signal_entry_high,
        signal_exit_low=inputs.signal_exit_low,
        signal_n=inputs.signal_n,
        paused=inputs.paused,
        high_limit=inputs.high_limit,
        low_limit=inputs.low_limit,
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
        post_segment_func_nb=post_segment_func_nb,
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
    valid_orders = state.action_codes != ACTION_NONE
    row_indices = np.broadcast_to(
        np.arange(rows, dtype=np.int64)[:, None], (rows, columns)
    )
    return VectorbtSimulationResult(
        initial_cash=initial_cash,
        asset_group_unit_cap=params.asset_group_unit_cap,
        portfolio_unit_cap=params.portfolio_unit_cap,
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
        state_unit_counts=_readonly_copy(state.state_unit_counts),
        candidate_base_quantities=_readonly_copy(
            state.candidate_base_quantity
        ),
        event_group_scales=_readonly_copy(state.event_group_scales),
        event_portfolio_scales=_readonly_copy(
            state.event_portfolio_scales
        ),
        event_cash_scales=_readonly_copy(state.event_cash_scales),
        day_equity=_readonly_copy(state.day_equity),
        planned_row_indices=_readonly_copy(
            np.where(valid_orders, row_indices, -1).astype(np.int64)
        ),
        execution_adjustment_codes=_readonly_copy(
            np.zeros((rows, columns), dtype=np.int16)
        ),
        frozen_signal_n=_readonly_copy(
            np.where(valid_orders, inputs.signal_n, np.nan).astype(np.float64)
        ),
        execution_delay_days=0,
        execution_sequence=tuple(
            tuple(
                f"immediate-row-{row}:{inputs.securities[column]}"
                for column in range(columns)
                if state.action_codes[row, column] != ACTION_NONE
            )
            for row in range(rows)
        ),
        horizon_expired_orders=(),
    )


def run_vectorbt_simulation(
    inputs: SimulationInputs,
    config: Mapping[str, object],
) -> VectorbtSimulationResult:
    delay_days = _delay_days(config)
    immediate = _run_immediate(inputs, config)
    if delay_days == 0:
        return immediate
    initial_cash, params = _params(config)
    plan = freeze_order_plan(inputs, immediate)
    delayed = run_delayed_execution(
        inputs,
        plan,
        initial_cash=initial_cash,
        lot_size=params.lot_size,
        stop_n=params.stop_n,
        commission_multiplier=params.commission_multiplier,
        one_way_slippage=params.one_way_slippage,
        delay_days=delay_days,
    )
    rows, columns = inputs.close.shape
    delayed_candidate_bases = np.zeros((rows, columns), dtype=np.int64)
    delayed_group_scales = np.ones((rows, columns), dtype=np.float64)
    delayed_portfolio_scales = np.ones(rows, dtype=np.float64)
    delayed_cash_scales = np.ones(rows, dtype=np.float64)
    for execution_row in range(delay_days, rows):
        source_row = execution_row - delay_days
        delayed_candidate_bases[execution_row] = (
            immediate.candidate_base_quantities[source_row]
        )
        delayed_group_scales[execution_row] = (
            immediate.event_group_scales[source_row]
        )
        delayed_portfolio_scales[execution_row] = (
            immediate.event_portfolio_scales[source_row]
        )
        delayed_cash_scales[execution_row] = (
            immediate.event_cash_scales[source_row]
        )
    return VectorbtSimulationResult(
        initial_cash=initial_cash,
        asset_group_unit_cap=params.asset_group_unit_cap,
        portfolio_unit_cap=params.portfolio_unit_cap,
        portfolio=delayed.portfolio,
        action_codes=delayed.action_codes,
        reason_codes=delayed.reason_codes,
        requested_quantities=delayed.requested_quantities,
        planned_quantities=delayed.planned_quantities,
        filled_quantities=delayed.filled_quantities,
        fill_prices=delayed.fill_prices,
        fees=delayed.fees,
        state_quantities=delayed.state_quantities,
        state_common_stop=delayed.state_common_stop,
        state_next_add_index=delayed.state_next_add_index,
        state_unit_counts=delayed.state_unit_counts,
        candidate_base_quantities=_readonly_copy(delayed_candidate_bases),
        event_group_scales=_readonly_copy(delayed_group_scales),
        event_portfolio_scales=_readonly_copy(
            delayed_portfolio_scales
        ),
        event_cash_scales=_readonly_copy(delayed_cash_scales),
        day_equity=delayed.day_equity,
        planned_row_indices=delayed.planned_row_indices,
        execution_adjustment_codes=delayed.execution_adjustment_codes,
        frozen_signal_n=delayed.frozen_signal_n,
        execution_delay_days=delay_days,
        execution_sequence=delayed.execution_sequence,
        horizon_expired_orders=delayed.horizon_expired_orders,
    )
