from __future__ import annotations

import math
import hashlib
import json
from dataclasses import dataclass
from typing import Mapping, Sequence

import numpy as np
import pandas as pd

from .indicators import breakout_levels, turtle_n


@dataclass(frozen=True)
class CorporateActionApplication:
    source_event_id: str
    security: str
    event_type: str
    effective_date: str
    application_date: str
    announcement_date: str
    knowledge_cutoff_date: str
    evidence_timing: str
    split_ratio: float | None
    cash_per_share: float | None
    cumulative_factor: float
    price_basis_changed: bool
    source: str
    source_record_sha256: str


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
    covariance: np.ndarray
    covariance_eligible: np.ndarray

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


def _numeric(frame: pd.DataFrame, field: str) -> pd.Series:
    if field not in frame.columns:
        raise ValueError(f"missing market field: {field}")
    return pd.to_numeric(frame[field], errors="coerce").astype(float)


_RECONCILIATION_RTOL = 1e-8
# 聚宽 ETF 日线价格按 0.001 最小价位导出，而官方每份现金可有四位小数；
# 两项权威事实勾稽时允许最多半个最小价位的舍入差。
_RECONCILIATION_ATOL = 0.000500001


def _evidence_insufficient(message: str) -> ValueError:
    return ValueError(f"evidence_insufficient: {message}")


def _canonical_action_digest(actions: Sequence[Mapping[str, object]]) -> str:
    rows = [dict(action) for action in actions]
    rows.sort(key=lambda row: str(row.get("source_event_id", "")))
    payload = json.dumps(
        rows,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _action_date(value: object, field: str) -> pd.Timestamp:
    try:
        timestamp = pd.Timestamp(value).normalize()
    except (TypeError, ValueError) as exc:
        raise _evidence_insufficient(f"invalid corporate-action {field}") from exc
    if pd.isna(timestamp):
        raise _evidence_insufficient(f"missing corporate-action {field}")
    return timestamp


def _positive_action_number(value: object, field: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise _evidence_insufficient(f"invalid corporate-action {field}") from exc
    if not math.isfinite(number) or number <= 0.0:
        raise _evidence_insufficient(f"invalid corporate-action {field}")
    return number


def _actions_by_date(
    actions: Sequence[Mapping[str, object]],
    *,
    security: str,
) -> dict[pd.Timestamp, list[Mapping[str, object]]]:
    result: dict[pd.Timestamp, list[Mapping[str, object]]] = {}
    event_ids: set[str] = set()
    for action in actions:
        if str(action.get("security", "")) != security:
            continue
        event_id = str(action.get("source_event_id", ""))
        if not event_id or event_id in event_ids:
            raise _evidence_insufficient(
                f"duplicate or missing corporate-action identity for {security}"
            )
        event_ids.add(event_id)
        effective_date = _action_date(
            action.get("effective_date"), "effective_date"
        )
        result.setdefault(effective_date, []).append(action)
    return result


def _derive_continuous_prices(
    frame: pd.DataFrame,
    *,
    security: str,
    actions: Sequence[Mapping[str, object]],
) -> tuple[pd.DataFrame, tuple[CorporateActionApplication, ...]]:
    result = frame.copy()
    price_fields = (
        "open",
        "high",
        "low",
        "close",
        "pre_close",
        "high_limit",
        "low_limit",
    )
    for field in price_fields:
        result[f"raw_{field}"] = result[field]
    factor = np.ones(len(result), dtype=np.float64)
    applied = np.zeros(len(result), dtype=np.bool_)
    applications: list[CorporateActionApplication] = []
    events_by_date = _actions_by_date(actions, security=security)
    available_dates = set(result["date"])
    in_range_events = {
        event_date
        for event_date in events_by_date
        if result["date"].iloc[0] <= event_date <= result["date"].iloc[-1]
    }
    if not in_range_events.issubset(available_dates):
        raise _evidence_insufficient(
            f"corporate-action effective date is not a market row for {security}"
        )

    row_by_date = {
        current_date: row for row, current_date in enumerate(result["date"])
    }
    scheduled_events: dict[
        pd.Timestamp, list[tuple[pd.Timestamp, Mapping[str, object]]]
    ] = {}
    for effective_date, events in events_by_date.items():
        if effective_date not in in_range_events:
            continue
        effective_row = row_by_date[effective_date]
        for event in events:
            announcement_date = _action_date(
                event.get("announcement_date"), "announcement_date"
            )
            knowledge_cutoff = _action_date(
                event.get("knowledge_cutoff_date"), "knowledge_cutoff_date"
            )
            if knowledge_cutoff < announcement_date:
                raise _evidence_insufficient(
                    "corporate action was not known by the snapshot cutoff"
                )
            status = str(event.get("status", ""))
            if status == "cancelled":
                continue
            if status != "active":
                raise _evidence_insufficient("unknown corporate-action status")
            event_type = str(event.get("event_type", ""))
            if event_type == "split":
                _positive_action_number(event.get("split_ratio"), "split_ratio")
            elif event_type == "cash_dividend":
                _positive_action_number(
                    event.get("cash_per_share"), "cash_per_share"
                )
            else:
                raise _evidence_insufficient("unknown corporate-action type")

            application_row = effective_row
            if bool(result["paused"].iloc[application_row]):
                application_row += 1
                while application_row < len(result) and bool(
                    result["paused"].iloc[application_row]
                ):
                    application_row += 1
                if application_row >= len(result):
                    raise _evidence_insufficient(
                        f"corporate action has no resumed market row for {security}"
                    )
            application_date = result["date"].iloc[application_row]
            scheduled_events.setdefault(application_date, []).append(
                (effective_date, event)
            )

    for row in range(1, len(result)):
        factor[row] = factor[row - 1]
        current_date = result["date"].iloc[row]
        previous_close = float(result["raw_close"].iloc[row - 1])
        current_pre_close = float(result["raw_pre_close"].iloc[row])
        if (
            not math.isfinite(previous_close)
            or not math.isfinite(current_pre_close)
            or previous_close <= 0.0
            or current_pre_close <= 0.0
        ):
            raise _evidence_insufficient(
                f"invalid close/pre_close reconciliation input for {security} {current_date.date()}"
            )
        scheduled = scheduled_events.get(current_date, [])
        basis_changed = not np.isclose(
            previous_close,
            current_pre_close,
            rtol=_RECONCILIATION_RTOL,
            atol=_RECONCILIATION_ATOL,
        )
        if scheduled:
            if basis_changed:
                factor[row] *= previous_close / current_pre_close
            applied[row] = True
            for source_effective_date, event in scheduled:
                event_type = str(event["event_type"])
                applications.append(
                    CorporateActionApplication(
                        source_event_id=str(event.get("source_event_id", "")),
                        security=security,
                        event_type=event_type,
                        effective_date=source_effective_date.strftime("%Y-%m-%d"),
                        application_date=current_date.strftime("%Y-%m-%d"),
                        announcement_date=_action_date(
                            event.get("announcement_date"), "announcement_date"
                        ).strftime("%Y-%m-%d"),
                        knowledge_cutoff_date=_action_date(
                            event.get("knowledge_cutoff_date"),
                            "knowledge_cutoff_date",
                        ).strftime("%Y-%m-%d"),
                        evidence_timing=(
                            "point_in_time"
                            if _action_date(
                                event.get("announcement_date"),
                                "announcement_date",
                            )
                            <= source_effective_date
                            else "retrospective_reconciliation"
                        ),
                        split_ratio=(
                            float(event["split_ratio"])
                            if event_type == "split"
                            else None
                        ),
                        cash_per_share=(
                            float(event["cash_per_share"])
                            if event_type == "cash_dividend"
                            else None
                        ),
                        cumulative_factor=float(factor[row]),
                        price_basis_changed=bool(basis_changed),
                        source=str(event.get("source", "")),
                        source_record_sha256=str(
                            event.get("source_record_sha256", "")
                        ),
                    )
                )
        elif basis_changed:
            raise _evidence_insufficient(
                f"unexplained price-basis change for {security} {current_date.date()}"
            )

    first_date = result["date"].iloc[0]
    if scheduled_events.get(first_date):
        raise _evidence_insufficient(
            f"corporate action on the first market row cannot be reconciled for {security}"
        )
    result["continuity_factor"] = factor
    result["corporate_action_applied"] = applied
    for field in price_fields:
        result[field] = result[f"raw_{field}"] * factor
    return result, tuple(applications)


def _normalized_frame(
    frame: pd.DataFrame,
    *,
    security: str,
    signal: Mapping[str, object],
    actions: Sequence[Mapping[str, object]],
) -> tuple[pd.DataFrame, tuple[CorporateActionApplication, ...]]:
    required = {
        "date",
        "open",
        "high",
        "low",
        "close",
        "pre_close",
        "paused",
        "high_limit",
        "low_limit",
    }
    missing = sorted(required - set(frame.columns))
    if missing:
        raise ValueError(f"missing market fields for {security}: {', '.join(missing)}")
    if "security" in frame.columns and set(frame["security"].astype(str)) != {security}:
        raise ValueError(f"market frame identity mismatch: {security}")

    result = frame.copy()
    result["date"] = pd.to_datetime(result["date"], errors="raise").dt.normalize()
    result = result.sort_values("date", kind="stable")
    if result["date"].duplicated().any():
        raise ValueError(f"duplicate market date: {security}")
    for field in (
        "open",
        "high",
        "low",
        "close",
        "pre_close",
        "high_limit",
        "low_limit",
    ):
        result[field] = _numeric(result, field)
    result["paused"] = result["paused"].astype(bool)
    result, applications = _derive_continuous_prices(
        result,
        security=security,
        actions=actions,
    )
    result["n"] = turtle_n(result, days=_positive_int(signal.get("n_days"), "n_days"))
    levels = breakout_levels(
        result,
        entry_days=_positive_int(signal.get("entry_days"), "entry_days"),
        exit_days=_positive_int(signal.get("exit_days"), "exit_days"),
    )
    result["entry_high"] = levels["entry_high"]
    result["exit_low"] = levels["exit_low"]
    return result.set_index("date", drop=False), applications


def _covariance_matrix(
    values: np.ndarray,
    *,
    method: str,
    half_life_days: int | None,
) -> np.ndarray:
    if method == "sample":
        result = np.cov(values, rowvar=False, ddof=1)
        return np.atleast_2d(np.asarray(result, dtype=np.float64))
    if method != "ewma":
        raise ValueError("unsupported covariance method")
    if half_life_days is None or half_life_days <= 0:
        raise ValueError("ewma covariance requires a positive half_life_days")
    decay = math.exp(math.log(0.5) / half_life_days)
    weights = np.power(decay, np.arange(len(values) - 1, -1, -1))
    weights /= weights.sum()
    centered = values - np.average(values, axis=0, weights=weights)
    denominator = 1.0 - float(np.sum(weights**2))
    return (centered * weights[:, None]).T @ centered / denominator


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
    computed_action_digest = _canonical_action_digest(corporate_actions)
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

    execution = config.get("execution", {})
    if not isinstance(execution, Mapping):
        raise ValueError("execution config must be an object")
    additional_delay = _positive_int(
        execution.get("additional_delay_days", 0) + 1,
        "signal execution delay",
    ) - 1
    shift = 1 + additional_delay
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

    risk = _section(config, "risk")
    covariance_config = risk.get("covariance")
    if not isinstance(covariance_config, Mapping):
        raise ValueError("covariance config must be an object")
    window_days = _positive_int(
        covariance_config.get("window_days"), "covariance window", minimum=2
    )
    method = str(covariance_config.get("method", "sample"))
    half_life_value = covariance_config.get("half_life_days")
    half_life_days = (
        None
        if half_life_value is None
        else _positive_int(half_life_value, "half_life_days")
    )
    with np.errstate(divide="ignore", invalid="ignore"):
        return_values = continuous_close / continuous_pre_close - 1.0
    returns = pd.DataFrame(return_values, index=calendar, columns=securities)
    covariance = np.full(
        (row_count, column_count, column_count), np.nan, dtype=np.float64
    )
    covariance_eligible = np.zeros(shape, dtype=np.bool_)
    for execution_row in range(shift, row_count):
        source_row = execution_row - shift
        through = returns.iloc[: source_row + 1].replace([np.inf, -np.inf], np.nan)
        eligible = [
            column
            for column in range(column_count)
            if int(through.iloc[:, column].notna().sum()) >= window_days
        ]
        if not eligible:
            continue
        aligned = through.iloc[:, eligible].dropna(how="any")
        if len(aligned) < window_days:
            continue
        values = aligned.tail(window_days).to_numpy(dtype=np.float64)
        matrix = _covariance_matrix(
            values, method=method, half_life_days=half_life_days
        )
        if matrix.shape != (len(eligible), len(eligible)) or not np.isfinite(matrix).all():
            continue
        for left_position, left_column in enumerate(eligible):
            covariance_eligible[execution_row, left_column] = True
            for right_position, right_column in enumerate(eligible):
                covariance[execution_row, left_column, right_column] = matrix[
                    left_position, right_position
                ]

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
        covariance=_readonly(covariance, "float64"),
        covariance_eligible=_readonly(covariance_eligible, "bool"),
    )
