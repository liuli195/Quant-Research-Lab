from __future__ import annotations

from collections import namedtuple
from dataclasses import dataclass
from typing import Mapping, Sequence

import numpy as np
import pandas as pd
from numba import njit

from scripts.research.local_quant_research.contracts import (
    FILL_ACCEPTED,
    FILL_REJECTED,
    SIDE_BUY,
    SIDE_SELL,
    LedgerInput,
    OrderBuffer,
    OrderProgram,
    PreparedStrategy,
    StrategyEvidenceError,
)
from scripts.research.market_data.contracts import (
    corporate_actions_digest as compute_corporate_actions_digest,
)

from scripts.research.market_data.economic_returns import (
    CorporateActionApplication,
    derive_continuous_prices,
)

import math


def _numeric_columns(
    frame: pd.DataFrame,
    fields: tuple[str, ...],
) -> dict[str, pd.Series]:
    missing = [field for field in fields if field not in frame.columns]
    if missing:
        raise ValueError(f"missing price fields: {', '.join(missing)}")
    return {
        field: pd.to_numeric(frame[field], errors="coerce").astype(float)
        for field in fields
    }


def true_range(frame: pd.DataFrame) -> pd.Series:
    columns = _numeric_columns(frame, ("high", "low", "pre_close"))
    components = pd.concat(
        (
            columns["high"] - columns["low"],
            (columns["high"] - columns["pre_close"]).abs(),
            (columns["low"] - columns["pre_close"]).abs(),
        ),
        axis=1,
    )
    result = components.max(axis=1, skipna=False)
    result.name = "tr"
    return result


def turtle_n(frame: pd.DataFrame, days: int = 20) -> pd.Series:
    if not isinstance(days, int) or days < 1:
        raise ValueError("days must be a positive integer")
    values: list[float] = []
    warmup: list[float] = []
    current: float | None = None
    for raw_value in true_range(frame):
        value = float(raw_value) if pd.notna(raw_value) else math.nan
        if not math.isfinite(value):
            warmup.clear()
            current = None
            values.append(math.nan)
            continue
        if current is None:
            warmup.append(value)
            if len(warmup) < days:
                values.append(math.nan)
                continue
            current = sum(warmup[-days:]) / days
        else:
            current = ((current * (days - 1)) + value) / days
        values.append(current)
    return pd.Series(values, index=frame.index, name="n", dtype=float)


def breakout_levels(
    frame: pd.DataFrame,
    entry_days: int,
    exit_days: int,
) -> pd.DataFrame:
    if not isinstance(entry_days, int) or entry_days < 1:
        raise ValueError("entry_days must be a positive integer")
    if not isinstance(exit_days, int) or exit_days < 1:
        raise ValueError("exit_days must be a positive integer")
    columns = _numeric_columns(frame, ("high", "low"))
    return pd.DataFrame(
        {
            "entry_high": columns["high"]
            .shift(1)
            .rolling(entry_days, min_periods=entry_days)
            .max(),
            "exit_low": columns["low"]
            .shift(1)
            .rolling(exit_days, min_periods=exit_days)
            .min(),
        },
        index=frame.index,
    )


@dataclass(frozen=True)
class SimulationInputs:
    dates: np.ndarray
    securities: tuple[str, ...]
    asset_groups: tuple[str, ...]
    asset_group_ids: np.ndarray
    raw_open: np.ndarray
    raw_high: np.ndarray
    raw_low: np.ndarray
    raw_close: np.ndarray
    raw_pre_close: np.ndarray
    continuous_open: np.ndarray
    continuous_high: np.ndarray
    continuous_low: np.ndarray
    continuous_close: np.ndarray
    continuous_pre_close: np.ndarray
    continuity_factor: np.ndarray
    corporate_action_applied: np.ndarray
    corporate_actions_digest: str
    corporate_action_applications: tuple[CorporateActionApplication, ...]
    paused: np.ndarray
    high_limit: np.ndarray
    low_limit: np.ndarray
    signal_source_index: np.ndarray
    signal_close: np.ndarray
    signal_entry_high: np.ndarray
    signal_exit_low: np.ndarray
    signal_n: np.ndarray

    @property
    def execution_open(self) -> np.ndarray:
        return self.continuous_open

    @property
    def close(self) -> np.ndarray:
        return self.continuous_close


def _section(config: Mapping[str, object], name: str) -> Mapping[str, object]:
    value = config.get(name)
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} config must be an object")
    return value


def _positive_int(value: object, name: str, *, minimum: int = 1) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be an integer")
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if result < minimum or result != value:
        raise ValueError(f"{name} must be at least {minimum}")
    return result


def _readonly(values: object, dtype: np.dtype[object] | str) -> np.ndarray:
    result = np.ascontiguousarray(values, dtype=dtype)
    result.setflags(write=False)
    return result


def _evidence_insufficient(message: str) -> ValueError:
    return ValueError(f"evidence_insufficient: {message}")


def _normalized_frame(
    frame: pd.DataFrame,
    *,
    security: str,
    signal: Mapping[str, object],
    actions: Sequence[Mapping[str, object]],
) -> tuple[pd.DataFrame, tuple[CorporateActionApplication, ...]]:
    continuous = derive_continuous_prices(
        frame,
        security=security,
        corporate_actions=actions,
    )
    result = continuous.frame
    result["n"] = turtle_n(result, days=_positive_int(signal.get("n_days"), "n_days"))
    levels = breakout_levels(
        result,
        entry_days=_positive_int(signal.get("entry_days"), "entry_days"),
        exit_days=_positive_int(signal.get("exit_days"), "exit_days"),
    )
    result["entry_high"] = levels["entry_high"]
    result["exit_low"] = levels["exit_low"]
    return result.set_index("date", drop=False), continuous.applications


def prepare_simulation_inputs(
    frames: Mapping[str, pd.DataFrame],
    config: Mapping[str, object],
    *,
    corporate_actions: Sequence[Mapping[str, object]] = (),
    corporate_actions_digest: str | None = None,
) -> SimulationInputs:
    universe_value = config.get("universe")
    if not isinstance(universe_value, list) or not universe_value:
        raise ValueError("universe must be a non-empty list")
    universe: dict[str, str] = {}
    for item in universe_value:
        if not isinstance(item, Mapping):
            raise ValueError("universe entries must be objects")
        security = str(item.get("security", ""))
        asset_group = str(item.get("asset_group", ""))
        if not security or not asset_group or security in universe:
            raise ValueError("universe identities must be non-empty and unique")
        universe[security] = asset_group
    if set(frames) != set(universe):
        raise ValueError("market frames must exactly match the configured universe")
    action_securities = {
        str(action.get("security", "")) for action in corporate_actions
    }
    unknown_action_securities = sorted(action_securities - set(universe))
    if unknown_action_securities:
        raise _evidence_insufficient(
            "corporate actions are outside the configured universe: "
            + ", ".join(unknown_action_securities)
        )
    computed_action_digest = compute_corporate_actions_digest(corporate_actions)
    if corporate_actions_digest is None:
        corporate_actions_digest = computed_action_digest
    elif (
        not isinstance(corporate_actions_digest, str)
        or len(corporate_actions_digest) != 64
        or any(character not in "0123456789abcdef" for character in corporate_actions_digest)
    ):
        raise _evidence_insufficient("invalid corporate-actions digest")
    elif corporate_actions_digest != computed_action_digest:
        raise _evidence_insufficient("corporate-actions digest mismatch")

    securities = tuple(sorted(universe))
    asset_groups = tuple(universe[security] for security in securities)
    group_labels = {name: index for index, name in enumerate(sorted(set(asset_groups)))}
    asset_group_ids = [group_labels[name] for name in asset_groups]
    signal = _section(config, "signal")
    normalized: dict[str, pd.DataFrame] = {}
    action_applications: list[CorporateActionApplication] = []
    for security in securities:
        normalized_frame, applications = _normalized_frame(
            frames[security],
            security=security,
            signal=signal,
            actions=corporate_actions,
        )
        normalized[security] = normalized_frame
        action_applications.extend(applications)
    calendar = pd.DatetimeIndex(
        sorted({date for frame in normalized.values() for date in frame.index})
    )
    if calendar.empty:
        raise ValueError("market calendar must not be empty")

    row_count = len(calendar)
    column_count = len(securities)
    shape = (row_count, column_count)
    raw_open = np.full(shape, np.nan, dtype=np.float64)
    raw_high = np.full(shape, np.nan, dtype=np.float64)
    raw_low = np.full(shape, np.nan, dtype=np.float64)
    raw_close = np.full(shape, np.nan, dtype=np.float64)
    raw_pre_close = np.full(shape, np.nan, dtype=np.float64)
    continuous_open = np.full(shape, np.nan, dtype=np.float64)
    continuous_high = np.full(shape, np.nan, dtype=np.float64)
    continuous_low = np.full(shape, np.nan, dtype=np.float64)
    continuous_close = np.full(shape, np.nan, dtype=np.float64)
    continuous_pre_close = np.full(shape, np.nan, dtype=np.float64)
    continuity_factor = np.full(shape, np.nan, dtype=np.float64)
    corporate_action_applied = np.zeros(shape, dtype=np.bool_)
    paused = np.ones(shape, dtype=np.bool_)
    high_limit = np.full(shape, np.nan, dtype=np.float64)
    low_limit = np.full(shape, np.nan, dtype=np.float64)
    raw_entry_high = np.full(shape, np.nan, dtype=np.float64)
    raw_exit_low = np.full(shape, np.nan, dtype=np.float64)
    raw_n = np.full(shape, np.nan, dtype=np.float64)
    for column, security in enumerate(securities):
        aligned = normalized[security].reindex(calendar)
        raw_open[:, column] = aligned["raw_open"].to_numpy(dtype=np.float64)
        raw_high[:, column] = aligned["raw_high"].to_numpy(dtype=np.float64)
        raw_low[:, column] = aligned["raw_low"].to_numpy(dtype=np.float64)
        raw_close[:, column] = aligned["raw_close"].to_numpy(dtype=np.float64)
        raw_pre_close[:, column] = aligned["raw_pre_close"].to_numpy(dtype=np.float64)
        continuous_open[:, column] = aligned["open"].to_numpy(dtype=np.float64)
        continuous_high[:, column] = aligned["high"].to_numpy(dtype=np.float64)
        continuous_low[:, column] = aligned["low"].to_numpy(dtype=np.float64)
        continuous_close[:, column] = aligned["close"].to_numpy(dtype=np.float64)
        continuous_pre_close[:, column] = aligned["pre_close"].to_numpy(
            dtype=np.float64
        )
        continuity_factor[:, column] = aligned["continuity_factor"].to_numpy(
            dtype=np.float64
        )
        corporate_action_applied[:, column] = aligned[
            "corporate_action_applied"
        ].fillna(False).to_numpy(dtype=np.bool_)
        paused[:, column] = aligned["paused"].fillna(True).to_numpy(dtype=np.bool_)
        high_limit[:, column] = aligned["high_limit"].to_numpy(dtype=np.float64)
        low_limit[:, column] = aligned["low_limit"].to_numpy(dtype=np.float64)
        raw_entry_high[:, column] = aligned["entry_high"].to_numpy(dtype=np.float64)
        raw_exit_low[:, column] = aligned["exit_low"].to_numpy(dtype=np.float64)
        raw_n[:, column] = aligned["n"].to_numpy(dtype=np.float64)

    shift = 1
    signal_source_index = np.full(row_count, -1, dtype=np.int64)
    signal_close = np.full(shape, np.nan, dtype=np.float64)
    signal_entry_high = np.full(shape, np.nan, dtype=np.float64)
    signal_exit_low = np.full(shape, np.nan, dtype=np.float64)
    signal_n = np.full(shape, np.nan, dtype=np.float64)
    for execution_row in range(shift, row_count):
        source_row = execution_row - shift
        signal_source_index[execution_row] = source_row
        signal_close[execution_row] = continuous_close[source_row]
        signal_entry_high[execution_row] = raw_entry_high[source_row]
        signal_exit_low[execution_row] = raw_exit_low[source_row]
        signal_n[execution_row] = raw_n[source_row]

    return SimulationInputs(
        dates=_readonly(calendar.to_numpy(dtype="datetime64[D]"), "datetime64[D]"),
        securities=securities,
        asset_groups=asset_groups,
        asset_group_ids=_readonly(asset_group_ids, "int64"),
        raw_open=_readonly(raw_open, "float64"),
        raw_high=_readonly(raw_high, "float64"),
        raw_low=_readonly(raw_low, "float64"),
        raw_close=_readonly(raw_close, "float64"),
        raw_pre_close=_readonly(raw_pre_close, "float64"),
        continuous_open=_readonly(continuous_open, "float64"),
        continuous_high=_readonly(continuous_high, "float64"),
        continuous_low=_readonly(continuous_low, "float64"),
        continuous_close=_readonly(continuous_close, "float64"),
        continuous_pre_close=_readonly(continuous_pre_close, "float64"),
        continuity_factor=_readonly(continuity_factor, "float64"),
        corporate_action_applied=_readonly(corporate_action_applied, "bool"),
        corporate_actions_digest=corporate_actions_digest,
        corporate_action_applications=tuple(
            sorted(
                action_applications,
                key=lambda item: (
                    item.effective_date,
                    item.security,
                    item.source_event_id,
                ),
            )
        ),
        paused=_readonly(paused, "bool"),
        high_limit=_readonly(high_limit, "float64"),
        low_limit=_readonly(low_limit, "float64"),
        signal_source_index=_readonly(signal_source_index, "int64"),
        signal_close=_readonly(signal_close, "float64"),
        signal_entry_high=_readonly(signal_entry_high, "float64"),
        signal_exit_low=_readonly(signal_exit_low, "float64"),
        signal_n=_readonly(signal_n, "float64"),
    )


ACTION_NONE = 0
ACTION_FULL_EXIT = 1
ACTION_REDISTRIBUTION_SELL = 2
ACTION_ENTRY = 3
ACTION_ADDITION = 4
ACTION_REDISTRIBUTION_BUY = 5

REASON_NONE = 0
REASON_ENTRY_BREAKOUT = 1
REASON_FIXED_ADDITION_LEVEL = 2
REASON_PROTECTIVE_STOP = 3
REASON_TREND_EXIT = 4
REASON_MISSING_OPEN = 5
REASON_PAUSED = 6
REASON_HIGH_LIMIT = 7
REASON_LOW_LIMIT = 8
REASON_ALLOCATION_CONSTRAINT = 9
REASON_ORDER_REJECTED = 10
REASON_FULL_POSITION_REDISTRIBUTION = 11


CallbackInputs = namedtuple(
    "CallbackInputs",
    (
        "execution_open",
        "signal_close",
        "signal_entry_high",
        "signal_exit_low",
        "signal_n",
        "paused",
        "high_limit",
        "low_limit",
        "asset_group_ids",
    ),
)

CallbackParams = namedtuple(
    "CallbackParams",
    (
        "lot_size",
        "unit_risk_per_n",
        "add_step_n",
        "stop_n",
        "max_units",
        "asset_group_unit_cap",
        "portfolio_unit_cap",
        "commission_multiplier",
        "one_way_slippage",
    ),
)

CallbackState = namedtuple(
    "CallbackState",
    (
        "unit_count",
        "unit_signal_n",
        "unit_base_quantities",
        "position_costs",
        "initial_fill_price",
        "initial_signal_n",
        "common_stop",
        "next_add_index",
        "candidate_signal_n",
        "candidate_base_quantity",
        "action_codes",
        "reason_codes",
        "requested_quantities",
        "planned_quantities",
        "state_common_stop",
        "state_next_add_index",
        "state_unit_counts",
        "event_group_scales",
        "event_portfolio_scales",
        "event_cash_scales",
        "event_risk_budgets",
        "event_planned_losses",
        "event_risk_cap_applied",
        "planned_row_indices",
        "execution_adjustment_codes",
        "frozen_signal_n",
        "scratch_exit_active",
        "scratch_candidate_active",
        "scratch_locked_quantities",
        "scratch_targets",
        "scratch_candidate_targets",
        "scratch_unit_counts",
        "scratch_unit_bases",
        "scratch_group_units",
        "scratch_group_scales",
        "scratch_buy_prices",
        "scratch_risk_stops",
        "scratch_risk_budgets",
        "scratch_risk_allowances",
        "scratch_risk_cap_applied",
    ),
)


@njit
def _finite_positive(value: float) -> bool:
    return np.isfinite(value) and value > 0.0


@njit
def _commission(price: float, quantity: int, multiplier: float) -> float:
    return max(5.0, price * quantity * 0.000085) * multiplier


@njit
def _buy_price(open_price: float, slippage: float) -> float:
    return open_price * (1.0 + slippage)


@njit
def _sell_price(open_price: float, slippage: float) -> float:
    return open_price * (1.0 - slippage)


@njit
def _buy_tradability_reason_nb(
    row: int, column: int, inputs: CallbackInputs
) -> int:
    open_price = inputs.execution_open[row, column]
    if not _finite_positive(open_price):
        return REASON_MISSING_OPEN
    if inputs.paused[row, column]:
        return REASON_PAUSED
    high_limit = inputs.high_limit[row, column]
    if np.isfinite(high_limit) and open_price >= high_limit:
        return REASON_HIGH_LIMIT
    return REASON_NONE


@njit
def _sell_tradability_reason_nb(
    row: int, column: int, inputs: CallbackInputs
) -> int:
    open_price = inputs.execution_open[row, column]
    if not _finite_positive(open_price):
        return REASON_MISSING_OPEN
    if inputs.paused[row, column]:
        return REASON_PAUSED
    low_limit = inputs.low_limit[row, column]
    if np.isfinite(low_limit) and open_price <= low_limit:
        return REASON_LOW_LIMIT
    return REASON_NONE


@njit
def _risk_scales_into_nb(
    unit_counts: np.ndarray,
    asset_group_ids: np.ndarray,
    from_column: int,
    group_count: int,
    asset_group_unit_cap: float,
    portfolio_unit_cap: float,
    group_units: np.ndarray,
    group_scales: np.ndarray,
) -> float:
    for group in range(group_count):
        group_units[group] = 0.0
        group_scales[group] = 1.0
    for column in range(unit_counts.shape[0]):
        group_units[asset_group_ids[from_column + column]] += unit_counts[column]
    for group in range(group_count):
        if group_units[group] > asset_group_unit_cap:
            group_scales[group] = asset_group_unit_cap / group_units[group]
    effective_units = 0.0
    for column in range(unit_counts.shape[0]):
        effective_units += (
            unit_counts[column]
            * group_scales[asset_group_ids[from_column + column]]
        )
    portfolio_scale = 1.0
    if effective_units > portfolio_unit_cap:
        portfolio_scale = portfolio_unit_cap / effective_units
    return portfolio_scale


@njit
def _targets_for_scale_into_nb(
    unit_base_quantities: np.ndarray,
    unit_counts: np.ndarray,
    asset_group_ids: np.ndarray,
    from_column: int,
    group_scales: np.ndarray,
    portfolio_scale: float,
    cash_scale: float,
    locked_quantities: np.ndarray,
    lot_size: int,
    targets: np.ndarray,
) -> None:
    for column in range(unit_counts.shape[0]):
        if locked_quantities[column] >= 0:
            targets[column] = locked_quantities[column]
            continue
        raw_quantity = 0
        for unit in range(unit_counts[column]):
            raw_quantity += unit_base_quantities[column, unit]
        scaled = (
            raw_quantity
            * group_scales[asset_group_ids[from_column + column]]
            * portfolio_scale
            * cash_scale
        )
        targets[column] = int(scaled // lot_size) * lot_size


@njit
def _planned_loss_nb(
    target: int,
    current: int,
    position_cost: float,
    buy_price: float,
    stop: float,
) -> float:
    if target <= 0:
        return 0.0
    if not _finite_positive(stop) or current < 0:
        return np.inf
    if current > 0 and (not np.isfinite(position_cost) or position_cost < 0.0):
        return np.inf
    if target <= current:
        projected_cost = position_cost * target / current
    else:
        if not _finite_positive(buy_price):
            return np.inf
        projected_cost = position_cost + (target - current) * buy_price
    return max(projected_cost - target * stop, 0.0)


@njit
def _risk_capped_target_nb(
    target: int,
    current: int,
    position_cost: float,
    buy_price: float,
    stop: float,
    allowance: float,
    lot_size: int,
) -> int:
    if (
        target <= 0
        or not np.isfinite(allowance)
        or allowance < 0.0
        or not _finite_positive(stop)
    ):
        return 0
    if (
        current > 0
        and (not np.isfinite(position_cost) or position_cost < 0.0)
    ):
        return 0
    if (
        _planned_loss_nb(target, current, position_cost, buy_price, stop)
        <= allowance + 1e-9
    ):
        return target
    average_cost = position_cost / current if current > 0 else buy_price
    existing_distance = max(average_cost - stop, 0.0)
    safe_existing = current
    if existing_distance > 0.0:
        safe_existing = int((allowance / existing_distance) // lot_size) * lot_size
    if safe_existing < current:
        return min(target, safe_existing)
    current_loss = _planned_loss_nb(
        current, current, position_cost, buy_price, stop
    )
    added_distance = max(buy_price - stop, 0.0)
    if added_distance <= 0.0:
        return target
    safe_added = int(
        ((allowance - current_loss) / added_distance) // lot_size
    ) * lot_size
    return min(target, current + max(safe_added, 0))


@njit
def _risk_cap_targets_nb(
    targets: np.ndarray,
    positions: np.ndarray,
    position_costs: np.ndarray,
    buy_prices: np.ndarray,
    stops: np.ndarray,
    risk_budgets: np.ndarray,
    asset_group_ids: np.ndarray,
    from_column: int,
    group_count: int,
    locked_quantities: np.ndarray,
    lot_size: int,
    allowances: np.ndarray,
    capped: np.ndarray,
) -> None:
    for offset in range(targets.shape[0]):
        budget = risk_budgets[offset]
        allowances[offset] = budget if np.isfinite(budget) and budget >= 0.0 else 0.0
        capped[offset] = False

    for group in range(group_count):
        group_budget = 0.0
        locked_loss = 0.0
        adjustable_budget = 0.0
        for offset in range(targets.shape[0]):
            if asset_group_ids[from_column + offset] != group:
                continue
            group_budget += allowances[offset]
            if locked_quantities[offset] >= 0:
                locked_loss += _planned_loss_nb(
                    int(targets[offset]),
                    int(round(positions[offset])),
                    position_costs[offset],
                    buy_prices[offset],
                    stops[offset],
                )
            else:
                adjustable_budget += allowances[offset]
        remaining = max(group_budget - locked_loss, 0.0)
        scale = min(remaining / adjustable_budget, 1.0) if adjustable_budget > 0.0 else 0.0
        for offset in range(targets.shape[0]):
            if (
                asset_group_ids[from_column + offset] == group
                and locked_quantities[offset] < 0
            ):
                allowances[offset] *= scale

    portfolio_budget = 0.0
    locked_loss = 0.0
    adjustable_budget = 0.0
    for offset in range(targets.shape[0]):
        budget = risk_budgets[offset]
        if np.isfinite(budget) and budget > 0.0:
            portfolio_budget += budget
        if locked_quantities[offset] >= 0:
            locked_loss += _planned_loss_nb(
                int(targets[offset]),
                int(round(positions[offset])),
                position_costs[offset],
                buy_prices[offset],
                stops[offset],
            )
        else:
            adjustable_budget += allowances[offset]
    remaining = max(portfolio_budget - locked_loss, 0.0)
    scale = min(remaining / adjustable_budget, 1.0) if adjustable_budget > 0.0 else 0.0
    for offset in range(targets.shape[0]):
        if locked_quantities[offset] >= 0:
            continue
        allowances[offset] *= scale
        before = int(targets[offset])
        targets[offset] = _risk_capped_target_nb(
            before,
            int(round(positions[offset])),
            position_costs[offset],
            buy_prices[offset],
            stops[offset],
            allowances[offset],
            lot_size,
        )
        capped[offset] = targets[offset] < before


@njit
def _cash_after_targets_nb(
    row: int,
    from_column: int,
    targets: np.ndarray,
    positions: np.ndarray,
    cash: float,
    inputs: CallbackInputs,
    params: CallbackParams,
) -> float:
    projected_cash = cash
    for offset in range(targets.shape[0]):
        column = from_column + offset
        current = int(round(positions[offset]))
        if targets[offset] >= current:
            continue
        quantity = current - targets[offset]
        price = _sell_price(
            inputs.execution_open[row, column], params.one_way_slippage
        )
        projected_cash += price * quantity - _commission(
            price, quantity, params.commission_multiplier
        )
    for offset in range(targets.shape[0]):
        column = from_column + offset
        current = int(round(positions[offset]))
        if targets[offset] <= current:
            continue
        quantity = targets[offset] - current
        price = _buy_price(
            inputs.execution_open[row, column], params.one_way_slippage
        )
        projected_cash -= price * quantity + _commission(
            price, quantity, params.commission_multiplier
        )
    return projected_cash


@njit
def _cash_feasible_targets_nb(
    row: int,
    from_column: int,
    unit_base_quantities: np.ndarray,
    unit_counts: np.ndarray,
    positions: np.ndarray,
    cash: float,
    group_scales: np.ndarray,
    portfolio_scale: float,
    locked_quantities: np.ndarray,
    position_costs: np.ndarray,
    buy_prices: np.ndarray,
    risk_stops: np.ndarray,
    risk_budgets: np.ndarray,
    risk_allowances: np.ndarray,
    risk_cap_applied: np.ndarray,
    group_count: int,
    inputs: CallbackInputs,
    params: CallbackParams,
    targets: np.ndarray,
    candidate_targets: np.ndarray,
) -> float:
    _targets_for_scale_into_nb(
        unit_base_quantities,
        unit_counts,
        inputs.asset_group_ids,
        from_column,
        group_scales,
        portfolio_scale,
        1.0,
        locked_quantities,
        params.lot_size,
        targets,
    )
    _risk_cap_targets_nb(
        targets,
        positions,
        position_costs,
        buy_prices,
        risk_stops,
        risk_budgets,
        inputs.asset_group_ids,
        from_column,
        group_count,
        locked_quantities,
        params.lot_size,
        risk_allowances,
        risk_cap_applied,
    )
    if _cash_after_targets_nb(
        row, from_column, targets, positions, cash, inputs, params
    ) >= -1e-9:
        return 1.0
    lower = 0.0
    upper = 1.0
    _targets_for_scale_into_nb(
        unit_base_quantities,
        unit_counts,
        inputs.asset_group_ids,
        from_column,
        group_scales,
        portfolio_scale,
        lower,
        locked_quantities,
        params.lot_size,
        targets,
    )
    _risk_cap_targets_nb(
        targets,
        positions,
        position_costs,
        buy_prices,
        risk_stops,
        risk_budgets,
        inputs.asset_group_ids,
        from_column,
        group_count,
        locked_quantities,
        params.lot_size,
        risk_allowances,
        risk_cap_applied,
    )
    for _ in range(64):
        candidate_scale = (lower + upper) / 2.0
        if candidate_scale == lower or candidate_scale == upper:
            break
        _targets_for_scale_into_nb(
            unit_base_quantities,
            unit_counts,
            inputs.asset_group_ids,
            from_column,
            group_scales,
            portfolio_scale,
            candidate_scale,
            locked_quantities,
            params.lot_size,
            candidate_targets,
        )
        _risk_cap_targets_nb(
            candidate_targets,
            positions,
            position_costs,
            buy_prices,
            risk_stops,
            risk_budgets,
            inputs.asset_group_ids,
            from_column,
            group_count,
            locked_quantities,
            params.lot_size,
            risk_allowances,
            risk_cap_applied,
        )
        matches_feasible_targets = True
        for column in range(targets.shape[0]):
            if candidate_targets[column] != targets[column]:
                matches_feasible_targets = False
                break
        if matches_feasible_targets:
            lower = candidate_scale
        elif _cash_after_targets_nb(
            row,
            from_column,
            candidate_targets,
            positions,
            cash,
            inputs,
            params,
        ) >= -1e-9:
            lower = candidate_scale
            for column in range(targets.shape[0]):
                targets[column] = candidate_targets[column]
        else:
            upper = candidate_scale
    _targets_for_scale_into_nb(
        unit_base_quantities,
        unit_counts,
        inputs.asset_group_ids,
        from_column,
        group_scales,
        portfolio_scale,
        lower,
        locked_quantities,
        params.lot_size,
        targets,
    )
    _risk_cap_targets_nb(
        targets,
        positions,
        position_costs,
        buy_prices,
        risk_stops,
        risk_budgets,
        inputs.asset_group_ids,
        from_column,
        group_count,
        locked_quantities,
        params.lot_size,
        risk_allowances,
        risk_cap_applied,
    )
    return lower


@njit
def _clear_position_state_nb(column: int, state: CallbackState) -> None:
    state.unit_count[column] = 0
    for unit in range(state.unit_signal_n.shape[1]):
        state.unit_signal_n[column, unit] = np.nan
        state.unit_base_quantities[column, unit] = 0
    state.initial_fill_price[column] = np.nan
    state.initial_signal_n[column] = np.nan
    state.common_stop[column] = np.nan
    state.next_add_index[column] = 0
    state.position_costs[column] = 0.0


@njit
def _action_priority(action: int) -> int:
    if action == ACTION_FULL_EXIT:
        return 0
    if action == ACTION_REDISTRIBUTION_SELL:
        return 1
    if action == ACTION_ENTRY or action == ACTION_ADDITION:
        return 2
    if action == ACTION_REDISTRIBUTION_BUY:
        return 3
    return 4


@njit
def prepare_segment_nb(view, inputs, params, state, trace, orders) -> None:
    row = view.row
    column_count = view.to_col - view.from_col
    equity = view.value
    for offset in range(column_count):
        column = view.from_col + offset
        state.action_codes[row, column] = ACTION_NONE
        state.reason_codes[row, column] = REASON_NONE
        state.requested_quantities[row, column] = 0
        state.planned_quantities[row, column] = 0
        state.candidate_signal_n[row, column] = np.nan
        state.candidate_base_quantity[row, column] = 0

    exit_active = state.scratch_exit_active[:column_count]
    candidate_active = state.scratch_candidate_active[:column_count]
    for offset in range(column_count):
        exit_active[offset] = False
        candidate_active[offset] = False
    any_decision = False

    for offset in range(column_count):
        column = view.from_col + offset
        position = view.positions[offset]
        close = inputs.signal_close[row, column]
        if position <= 0.0 or not _finite_positive(close):
            continue
        reason = REASON_NONE
        if np.isfinite(state.common_stop[column]) and close <= state.common_stop[column]:
            reason = REASON_PROTECTIVE_STOP
        elif (
            np.isfinite(inputs.signal_exit_low[row, column])
            and close < inputs.signal_exit_low[row, column]
        ):
            reason = REASON_TREND_EXIT
        if reason != REASON_NONE:
            state.action_codes[row, column] = ACTION_FULL_EXIT
            state.reason_codes[row, column] = reason
            state.requested_quantities[row, column] = int(round(position))
            tradability = _sell_tradability_reason_nb(row, column, inputs)
            if tradability == REASON_NONE:
                exit_active[offset] = True
            else:
                state.reason_codes[row, column] = tradability
            any_decision = True

    for offset in range(column_count):
        column = view.from_col + offset
        if state.action_codes[row, column] == ACTION_FULL_EXIT:
            continue
        close = inputs.signal_close[row, column]
        signal_n = inputs.signal_n[row, column]
        if not _finite_positive(close) or not _finite_positive(signal_n):
            continue
        position = view.positions[offset]
        action = ACTION_NONE
        reason = REASON_NONE
        if position > 0.0 and state.unit_count[column] > 0:
            next_index = state.next_add_index[column]
            if (
                next_index < params.max_units
                and close
                >= state.initial_fill_price[column]
                + next_index * params.add_step_n * state.initial_signal_n[column]
            ):
                action = ACTION_ADDITION
                reason = REASON_FIXED_ADDITION_LEVEL
        elif (
            position <= 0.0
            and np.isfinite(inputs.signal_entry_high[row, column])
            and close > inputs.signal_entry_high[row, column]
        ):
            action = ACTION_ENTRY
            reason = REASON_ENTRY_BREAKOUT
        if action == ACTION_NONE:
            continue
        quantity = int(equity * params.unit_risk_per_n / signal_n)
        quantity = (quantity // params.lot_size) * params.lot_size
        if quantity <= 0:
            continue
        state.action_codes[row, column] = action
        state.reason_codes[row, column] = reason
        state.requested_quantities[row, column] = quantity
        state.candidate_signal_n[row, column] = signal_n
        state.candidate_base_quantity[row, column] = quantity
        tradability = _buy_tradability_reason_nb(row, column, inputs)
        if tradability == REASON_NONE:
            candidate_active[offset] = True
        else:
            state.reason_codes[row, column] = tradability
        any_decision = True

    if any_decision:
        locked = state.scratch_locked_quantities[:column_count]
        targets = state.scratch_targets[:column_count]
        candidate_targets = state.scratch_candidate_targets[:column_count]
        counts = state.scratch_unit_counts[:column_count]
        bases = state.scratch_unit_bases[:column_count]
        group_units = state.scratch_group_units
        group_scales = state.scratch_group_scales
        buy_prices = state.scratch_buy_prices[:column_count]
        risk_stops = state.scratch_risk_stops[:column_count]
        risk_budgets = state.scratch_risk_budgets[:column_count]
        risk_allowances = state.scratch_risk_allowances[:column_count]
        risk_cap_applied = state.scratch_risk_cap_applied[:column_count]
        for offset in range(column_count):
            column = view.from_col + offset
            locked[offset] = -1
            targets[offset] = int(round(view.positions[offset]))
            open_price = inputs.execution_open[row, column]
            buy_prices[offset] = (
                _buy_price(open_price, params.one_way_slippage)
                if _finite_positive(open_price)
                else np.nan
            )
            if (
                not _finite_positive(inputs.execution_open[row, column])
                or inputs.paused[row, column]
                or (
                    state.action_codes[row, column] == ACTION_FULL_EXIT
                    and not exit_active[offset]
                )
            ):
                locked[offset] = int(round(view.positions[offset]))

        group_count = group_scales.shape[0]
        portfolio_scale = 1.0
        cash_scale = 1.0
        for _ in range(column_count * 3 + 1):
            for offset in range(column_count):
                column = view.from_col + offset
                counts[offset] = state.unit_count[column]
                for unit in range(params.max_units):
                    bases[offset, unit] = state.unit_base_quantities[column, unit]
                if exit_active[offset]:
                    counts[offset] = 0
                    for unit in range(params.max_units):
                        bases[offset, unit] = 0
                elif candidate_active[offset]:
                    slot = counts[offset]
                    bases[offset, slot] = state.candidate_base_quantity[row, column]
                    counts[offset] = slot + 1
            portfolio_scale = _risk_scales_into_nb(
                counts,
                inputs.asset_group_ids,
                view.from_col,
                group_count,
                params.asset_group_unit_cap,
                params.portfolio_unit_cap,
                group_units,
                group_scales,
            )
            for offset in range(column_count):
                column = view.from_col + offset
                group = inputs.asset_group_ids[column]
                stop = state.common_stop[column]
                unit_budget = 0.0
                if not exit_active[offset]:
                    for unit in range(state.unit_count[column]):
                        unit_budget += (
                            state.unit_base_quantities[column, unit]
                            * state.unit_signal_n[column, unit]
                        )
                    if candidate_active[offset]:
                        signal_n = state.candidate_signal_n[row, column]
                        unit_budget += (
                            state.candidate_base_quantity[row, column] * signal_n
                        )
                        candidate_stop = (
                            buy_prices[offset] - params.stop_n * signal_n
                        )
                        if _finite_positive(candidate_stop):
                            if not _finite_positive(stop):
                                stop = candidate_stop
                            else:
                                stop = max(stop, candidate_stop)
                risk_stops[offset] = stop
                risk_budgets[offset] = (
                    params.stop_n
                    * unit_budget
                    * group_scales[group]
                    * portfolio_scale
                )
            cash_scale = _cash_feasible_targets_nb(
                row,
                view.from_col,
                bases,
                counts,
                view.positions,
                view.cash,
                group_scales,
                portfolio_scale,
                locked,
                state.position_costs[view.from_col : view.to_col],
                buy_prices,
                risk_stops,
                risk_budgets,
                risk_allowances,
                risk_cap_applied,
                group_count,
                inputs,
                params,
                targets,
                candidate_targets,
            )
            changed = False
            for offset in range(column_count):
                column = view.from_col + offset
                current = int(round(view.positions[offset]))
                if locked[offset] < 0 and targets[offset] < current:
                    if _sell_tradability_reason_nb(row, column, inputs) != REASON_NONE:
                        locked[offset] = current
                        changed = True
                elif locked[offset] < 0 and targets[offset] > current:
                    if _buy_tradability_reason_nb(row, column, inputs) != REASON_NONE:
                        locked[offset] = current
                        changed = True
                if (
                    candidate_active[offset]
                    and targets[offset] - current < params.lot_size
                ):
                    candidate_active[offset] = False
                    state.reason_codes[row, column] = REASON_ALLOCATION_CONSTRAINT
                    changed = True
            if not changed:
                break

        has_effective_event = False
        for offset in range(column_count):
            if exit_active[offset] or candidate_active[offset]:
                has_effective_event = True
        if has_effective_event:
            for offset in range(column_count):
                column = view.from_col + offset
                group = inputs.asset_group_ids[column]
                state.event_group_scales[row, column] = group_scales[group]
                state.event_risk_budgets[row, column] = risk_budgets[offset]
                state.event_planned_losses[row, column] = _planned_loss_nb(
                    int(targets[offset]),
                    int(round(view.positions[offset])),
                    state.position_costs[column],
                    buy_prices[offset],
                    risk_stops[offset],
                )
                state.event_risk_cap_applied[row, column] = risk_cap_applied[
                    offset
                ]
            state.event_portfolio_scales[row] = portfolio_scale
            state.event_cash_scales[row] = cash_scale
            for offset in range(column_count):
                column = view.from_col + offset
                current = int(round(view.positions[offset]))
                action = state.action_codes[row, column]
                if action == ACTION_FULL_EXIT:
                    if exit_active[offset]:
                        state.planned_quantities[row, column] = current
                    continue
                delta = targets[offset] - current
                if action == ACTION_ENTRY or action == ACTION_ADDITION:
                    if candidate_active[offset] and delta >= params.lot_size:
                        state.planned_quantities[row, column] = delta
                    continue
                if delta <= -params.lot_size:
                    state.action_codes[row, column] = ACTION_REDISTRIBUTION_SELL
                    state.reason_codes[row, column] = REASON_FULL_POSITION_REDISTRIBUTION
                    state.requested_quantities[row, column] = -delta
                    state.planned_quantities[row, column] = -delta
                elif delta >= params.lot_size:
                    state.action_codes[row, column] = ACTION_REDISTRIBUTION_BUY
                    state.reason_codes[row, column] = REASON_FULL_POSITION_REDISTRIBUTION
                    state.requested_quantities[row, column] = delta
                    state.planned_quantities[row, column] = delta

    for offset in range(column_count):
        column = view.from_col + offset
        action = state.action_codes[row, column]
        reason = state.reason_codes[row, column]
        quantity = state.planned_quantities[row, column]
        open_price = inputs.execution_open[row, column]
        if action != ACTION_NONE:
            state.planned_row_indices[row, column] = row
            state.frozen_signal_n[row, column] = inputs.signal_n[row, column]
        if _finite_positive(open_price):
            view.valuation_prices[offset] = open_price
        orders[7][column] = _action_priority(action)
        if (
            action == ACTION_NONE
            or reason == REASON_MISSING_OPEN
            or reason == REASON_PAUSED
            or reason == REASON_HIGH_LIMIT
            or reason == REASON_LOW_LIMIT
            or reason == REASON_ALLOCATION_CONSTRAINT
            or quantity <= 0
        ):
            continue
        if action == ACTION_FULL_EXIT or action == ACTION_REDISTRIBUTION_SELL:
            quantity = min(quantity, int(round(view.positions[offset])))
            if quantity <= 0:
                continue
            price = _sell_price(open_price, params.one_way_slippage)
            orders[2][column] = float(quantity)
            orders[1][column] = SIDE_SELL
            orders[5][column] = np.nan
        else:
            price = _buy_price(open_price, params.one_way_slippage)
            orders[2][column] = float(quantity)
            orders[1][column] = SIDE_BUY
            orders[5][column] = float(params.lot_size)
        orders[0][column] = True
        orders[3][column] = price
        orders[4][column] = _commission(
            price,
            quantity,
            params.commission_multiplier,
        )
        orders[6][column] = False


@njit
def _record_candidate_unit_nb(
    row: int,
    column: int,
    fill_price: float,
    state: CallbackState,
    params: CallbackParams,
) -> None:
    slot = state.unit_count[column]
    if slot >= params.max_units:
        return
    signal_n = state.candidate_signal_n[row, column]
    state.unit_signal_n[column, slot] = signal_n
    state.unit_base_quantities[column, slot] = state.candidate_base_quantity[
        row, column
    ]
    if slot == 0:
        state.initial_fill_price[column] = fill_price
        state.initial_signal_n[column] = signal_n
        state.common_stop[column] = fill_price - params.stop_n * signal_n
    else:
        candidate_stop = fill_price - params.stop_n * signal_n
        state.common_stop[column] = max(state.common_stop[column], candidate_stop)
    state.unit_count[column] = slot + 1
    state.next_add_index[column] = slot + 1


@njit
def after_fill_nb(event, inputs, params, state, trace, orders) -> None:
    row = event.row
    column = event.column
    action = state.action_codes[row, column]
    if event.status == FILL_ACCEPTED:
        if event.side == SIDE_SELL:
            position_before = event.position_after + event.size
            if event.position_after <= 1e-9 or position_before <= 1e-9:
                state.position_costs[column] = 0.0
            else:
                state.position_costs[column] *= (
                    event.position_after / position_before
                )
            if action == ACTION_FULL_EXIT and event.position_after <= 1e-9:
                _clear_position_state_nb(column, state)
        else:
            if action == ACTION_ENTRY:
                _clear_position_state_nb(column, state)
                _record_candidate_unit_nb(row, column, event.price, state, params)
            elif action == ACTION_ADDITION:
                _record_candidate_unit_nb(row, column, event.price, state, params)
            state.position_costs[column] += event.size * event.price
    elif event.status == FILL_REJECTED:
        state.reason_codes[row, column] = REASON_ORDER_REJECTED


@njit
def after_segment_nb(view, inputs, params, state, trace, orders) -> None:
    row = view.row
    for offset in range(view.to_col - view.from_col):
        column = view.from_col + offset
        state.state_common_stop[row, column] = state.common_stop[column]
        state.state_next_add_index[row, column] = state.next_add_index[column]
        state.state_unit_counts[row, column] = state.unit_count[column]


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
    legacy = sorted(set(risk) & _LEGACY_RISK_FIELDS)
    if legacy:
        raise ValueError("legacy risk fields are not supported: " + ", ".join(legacy))
    lot_size_value = risk.get("lot_size", 100)
    if isinstance(lot_size_value, bool) or int(lot_size_value) != lot_size_value:
        raise ValueError("lot_size must be a positive integer")
    lot_size = int(lot_size_value)
    if lot_size <= 0:
        raise ValueError("lot_size must be a positive integer")
    slippage = _number(costs_value, "one_way_slippage", 0.0, positive=False)
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
    return initial_cash, CallbackParams(
        lot_size=lot_size,
        unit_risk_per_n=_number(risk, "unit_risk_per_n"),
        add_step_n=_number(signal, "add_step_n"),
        stop_n=_number(signal, "stop_n"),
        max_units=max_units_value,
        asset_group_unit_cap=_number(risk, "asset_group_unit_cap"),
        portfolio_unit_cap=_number(risk, "portfolio_unit_cap"),
        commission_multiplier=_number(costs_value, "commission_multiplier", 1.0),
        one_way_slippage=slippage,
    )


def _delay_days(config: Mapping[str, object]) -> int:
    execution = config.get("execution", {})
    if not isinstance(execution, Mapping):
        raise ValueError("execution config must be an object")
    value = execution.get("additional_delay_days", 0)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("additional_delay_days must be a non-negative integer")
    return value


def _mutable_state(
    rows: int,
    columns: int,
    group_count: int,
    max_units: int,
) -> CallbackState:
    if rows <= 0 or columns <= 0 or group_count <= 0 or max_units != 4:
        raise ValueError("invalid callback state dimensions")
    return CallbackState(
        unit_count=np.zeros(columns, dtype=np.int64),
        unit_signal_n=np.full((columns, max_units), np.nan, dtype=np.float64),
        unit_base_quantities=np.zeros((columns, max_units), dtype=np.int64),
        position_costs=np.zeros(columns, dtype=np.float64),
        initial_fill_price=np.full(columns, np.nan, dtype=np.float64),
        initial_signal_n=np.full(columns, np.nan, dtype=np.float64),
        common_stop=np.full(columns, np.nan, dtype=np.float64),
        next_add_index=np.zeros(columns, dtype=np.int64),
        candidate_signal_n=np.full((rows, columns), np.nan, dtype=np.float64),
        candidate_base_quantity=np.zeros((rows, columns), dtype=np.int64),
        action_codes=np.zeros((rows, columns), dtype=np.int16),
        reason_codes=np.zeros((rows, columns), dtype=np.int16),
        requested_quantities=np.zeros((rows, columns), dtype=np.int64),
        planned_quantities=np.zeros((rows, columns), dtype=np.int64),
        state_common_stop=np.full((rows, columns), np.nan, dtype=np.float64),
        state_next_add_index=np.zeros((rows, columns), dtype=np.int64),
        state_unit_counts=np.zeros((rows, columns), dtype=np.int64),
        event_group_scales=np.ones((rows, columns), dtype=np.float64),
        event_portfolio_scales=np.ones(rows, dtype=np.float64),
        event_cash_scales=np.ones(rows, dtype=np.float64),
        event_risk_budgets=np.full((rows, columns), np.nan, dtype=np.float64),
        event_planned_losses=np.full((rows, columns), np.nan, dtype=np.float64),
        event_risk_cap_applied=np.zeros((rows, columns), dtype=np.bool_),
        planned_row_indices=np.full((rows, columns), -1, dtype=np.int64),
        execution_adjustment_codes=np.zeros((rows, columns), dtype=np.int16),
        frozen_signal_n=np.full((rows, columns), np.nan, dtype=np.float64),
        scratch_exit_active=np.zeros(columns, dtype=np.bool_),
        scratch_candidate_active=np.zeros(columns, dtype=np.bool_),
        scratch_locked_quantities=np.full(columns, -1, dtype=np.int64),
        scratch_targets=np.zeros(columns, dtype=np.int64),
        scratch_candidate_targets=np.zeros(columns, dtype=np.int64),
        scratch_unit_counts=np.zeros(columns, dtype=np.int64),
        scratch_unit_bases=np.zeros((columns, max_units), dtype=np.int64),
        scratch_group_units=np.zeros(group_count, dtype=np.float64),
        scratch_group_scales=np.ones(group_count, dtype=np.float64),
        scratch_buy_prices=np.full(columns, np.nan, dtype=np.float64),
        scratch_risk_stops=np.full(columns, np.nan, dtype=np.float64),
        scratch_risk_budgets=np.zeros(columns, dtype=np.float64),
        scratch_risk_allowances=np.zeros(columns, dtype=np.float64),
        scratch_risk_cap_applied=np.zeros(columns, dtype=np.bool_),
    )


def _order_buffer(columns: int) -> OrderBuffer:
    return OrderBuffer(
        enabled=np.zeros(columns, dtype=np.bool_),
        side=np.zeros(columns, dtype=np.int8),
        size=np.zeros(columns, dtype=np.float64),
        price=np.full(columns, np.nan, dtype=np.float64),
        fixed_fees=np.zeros(columns, dtype=np.float64),
        size_granularity=np.full(columns, np.nan, dtype=np.float64),
        allow_partial=np.zeros(columns, dtype=np.bool_),
        priority=np.zeros(columns, dtype=np.int64),
    )


def _trace(state: CallbackState, rows: int, columns: int) -> dict[str, np.ndarray]:
    return {
        "action_codes": state.action_codes,
        "reason_codes": state.reason_codes,
        "requested_quantities": state.requested_quantities,
        "planned_quantities": state.planned_quantities,
        "state_common_stop": state.state_common_stop,
        "state_next_add_index": state.state_next_add_index,
        "state_unit_counts": state.state_unit_counts,
        "candidate_base_quantities": state.candidate_base_quantity,
        "event_group_scales": state.event_group_scales,
        "event_portfolio_scales": state.event_portfolio_scales,
        "event_cash_scales": state.event_cash_scales,
        "event_risk_budgets": state.event_risk_budgets,
        "event_planned_losses": state.event_planned_losses,
        "event_risk_cap_applied": state.event_risk_cap_applied,
        "planned_row_indices": state.planned_row_indices,
        "execution_adjustment_codes": state.execution_adjustment_codes,
        "frozen_signal_n": state.frozen_signal_n,
    }


@dataclass(frozen=True, slots=True)
class TurtleContext:
    inputs: SimulationInputs
    params: CallbackParams
    scenario_id: str
    delay_days: int
    initial_cash: float


def _market_frames(
    rows: Sequence[Mapping[str, object]],
    config: Mapping[str, object],
) -> dict[str, pd.DataFrame]:
    universe = config.get("universe")
    if not isinstance(universe, list) or not universe:
        raise ValueError("universe must be a non-empty list")
    securities = tuple(
        str(item.get("security", ""))
        for item in universe
        if isinstance(item, Mapping)
    )
    if len(securities) != len(universe) or any(not security for security in securities):
        raise ValueError("universe identities must be non-empty and unique")
    frame = pd.DataFrame([dict(row) for row in rows])
    if frame.empty or "security" not in frame:
        raise StrategyEvidenceError("snapshot_universe_mismatch", "snapshot has no market rows")
    if set(frame["security"].astype(str)) != set(securities):
        raise StrategyEvidenceError(
            "snapshot_universe_mismatch",
            "snapshot universe does not match the scenario",
        )
    return {
        security: frame.loc[frame["security"].astype(str) == security].copy()
        for security in securities
    }


def prepare_turtle_strategy(snapshot: object, config: Mapping[str, object]) -> PreparedStrategy:
    initial_cash, params = _params(config)
    delay_days = _delay_days(config)
    frames = _market_frames(getattr(snapshot, "rows"), config)
    try:
        simulation_inputs = prepare_simulation_inputs(
            frames,
            config,
            corporate_actions=getattr(snapshot, "corporate_actions"),
            corporate_actions_digest=getattr(snapshot, "corporate_actions_digest"),
        )
    except ValueError as exc:
        if str(exc).startswith("evidence_insufficient:"):
            raise StrategyEvidenceError("market_evidence_insufficient", str(exc)) from exc
        raise
    return _prepare_turtle_inputs(
        simulation_inputs,
        config,
        initial_cash=initial_cash,
        params=params,
        delay_days=delay_days,
    )


def _prepare_turtle_inputs(
    simulation_inputs: SimulationInputs,
    config: Mapping[str, object],
    *,
    initial_cash: float | None = None,
    params: CallbackParams | None = None,
    delay_days: int | None = None,
) -> PreparedStrategy:
    if initial_cash is None or params is None:
        initial_cash, params = _params(config)
    if delay_days is None:
        delay_days = _delay_days(config)
    rows, columns = simulation_inputs.close.shape
    group_count = int(np.max(simulation_inputs.asset_group_ids)) + 1
    state = _mutable_state(rows, columns, group_count, params.max_units)
    callback_inputs = CallbackInputs(
        simulation_inputs.execution_open,
        simulation_inputs.signal_close,
        simulation_inputs.signal_entry_high,
        simulation_inputs.signal_exit_low,
        simulation_inputs.signal_n,
        simulation_inputs.paused,
        simulation_inputs.high_limit,
        simulation_inputs.low_limit,
        simulation_inputs.asset_group_ids,
    )
    program = OrderProgram(
        program_id="turtle-etf-immediate/1",
        prepare_segment_nb=prepare_segment_nb,
        after_fill_nb=after_fill_nb,
        after_segment_nb=after_segment_nb,
        inputs=callback_inputs,
        params=params,
        state=state,
        trace=_trace(state, rows, columns),
        orders=_order_buffer(columns),
    )
    scenario_id = config.get("scenario_id")
    if not isinstance(scenario_id, str) or not scenario_id.strip():
        raise ValueError("scenario_id is missing or invalid")
    return PreparedStrategy(
        ledger_input=LedgerInput(
            dates=simulation_inputs.dates,
            symbols=simulation_inputs.securities,
            close=simulation_inputs.close,
            initial_cash=initial_cash,
            group_ids=np.zeros(columns, dtype=np.int64),
            cash_sharing=True,
            frequency="1D",
        ),
        primary_program=program,
        context=TurtleContext(
            simulation_inputs,
            params,
            scenario_id,
            delay_days,
            initial_cash,
        ),
    )
