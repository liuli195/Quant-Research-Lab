from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Sequence

import numpy as np
import pandas as pd

from scripts.research.market_data.economic_returns import (
    CorporateActionApplication,
    canonical_corporate_actions_digest,
    derive_continuous_prices,
)

from .indicators import breakout_levels, turtle_n


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
    computed_action_digest = canonical_corporate_actions_digest(corporate_actions)
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
