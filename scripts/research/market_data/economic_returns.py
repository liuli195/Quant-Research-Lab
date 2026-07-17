from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Mapping, Sequence

import numpy as np
import pandas as pd

from .contracts import corporate_actions_digest
from .query import SnapshotView


_RECONCILIATION_RTOL = 1e-8
# 聚宽 ETF 日线价格按 0.001 最小价位导出，而官方每份现金可有四位小数。
_RECONCILIATION_ATOL = 0.000500001


class EconomicReturnError(ValueError):
    """Raised when point-in-time economic returns cannot be reconciled."""


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
class ContinuousPriceResult:
    frame: pd.DataFrame
    returns: pd.Series
    applications: tuple[CorporateActionApplication, ...]


def _evidence_insufficient(message: str) -> EconomicReturnError:
    return EconomicReturnError(f"evidence_insufficient: {message}")


def _numeric(frame: pd.DataFrame, field: str) -> pd.Series:
    if field not in frame.columns:
        raise ValueError(f"missing market field: {field}")
    return pd.to_numeric(frame[field], errors="coerce").astype(float)


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
        effective_date = _action_date(action.get("effective_date"), "effective_date")
        result.setdefault(effective_date, []).append(action)
    return result


def _normalize_frame(frame: pd.DataFrame, *, security: str) -> pd.DataFrame:
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
    result = result.sort_values("date", kind="stable").reset_index(drop=True)
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
    return result


def _derive_forward_only_continuity(
    frame: pd.DataFrame,
    *,
    security: str,
    corporate_actions: Sequence[Mapping[str, object]],
) -> tuple[pd.DataFrame, tuple[CorporateActionApplication, ...]]:
    result = _normalize_frame(frame, security=security)
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
    events_by_date = _actions_by_date(corporate_actions, security=security)
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

    first_date = result["date"].iloc[0]
    if scheduled_events.get(first_date):
        raise _evidence_insufficient(
            f"corporate action on the first market row cannot be reconciled for {security}"
        )

    dates = result["date"].to_numpy(copy=False)
    raw_close = result["raw_close"].to_numpy(dtype=np.float64, copy=False)
    raw_pre_close = result["raw_pre_close"].to_numpy(
        dtype=np.float64, copy=False
    )
    previous_close = raw_close[:-1]
    current_pre_close = raw_pre_close[1:]
    invalid = (
        ~np.isfinite(previous_close)
        | ~np.isfinite(current_pre_close)
        | (previous_close <= 0.0)
        | (current_pre_close <= 0.0)
    )
    if np.any(invalid):
        row = int(np.flatnonzero(invalid)[0]) + 1
        current_date = pd.Timestamp(dates[row])
        raise _evidence_insufficient(
            f"invalid close/pre_close reconciliation input for {security} "
            f"{current_date.date()}"
        )

    basis_changed = np.zeros(len(result), dtype=np.bool_)
    basis_changed[1:] = ~np.isclose(
        previous_close,
        current_pre_close,
        rtol=_RECONCILIATION_RTOL,
        atol=_RECONCILIATION_ATOL,
    )
    scheduled_rows = np.asarray(
        sorted(row_by_date[date] for date in scheduled_events), dtype=np.int64
    )
    scheduled_mask = np.zeros(len(result), dtype=np.bool_)
    scheduled_mask[scheduled_rows] = True
    unexplained = basis_changed & ~scheduled_mask
    if np.any(unexplained):
        row = int(np.flatnonzero(unexplained)[0])
        current_date = pd.Timestamp(dates[row])
        raise _evidence_insufficient(
            f"unexplained price-basis change for {security} {current_date.date()}"
        )

    factor_steps = np.ones(len(result), dtype=np.float64)
    changed_rows = np.flatnonzero(basis_changed)
    factor_steps[changed_rows] = (
        raw_close[changed_rows - 1] / raw_pre_close[changed_rows]
    )
    factor = np.cumprod(factor_steps)
    applied[scheduled_rows] = True
    for row in scheduled_rows:
        current_date = pd.Timestamp(dates[row])
        for source_effective_date, event in scheduled_events[current_date]:
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
                    price_basis_changed=bool(basis_changed[row]),
                    source=str(event.get("source", "")),
                    source_record_sha256=str(
                        event.get("source_record_sha256", "")
                    ),
                )
            )
    result["continuity_factor"] = factor
    result["corporate_action_applied"] = applied
    for field in price_fields:
        result[field] = result[f"raw_{field}"] * factor
    return result, tuple(applications)


def derive_continuous_prices(
    frame: pd.DataFrame,
    *,
    security: str,
    corporate_actions: Sequence[Mapping[str, object]],
) -> ContinuousPriceResult:
    normalized, applications = _derive_forward_only_continuity(
        frame,
        security=security,
        corporate_actions=corporate_actions,
    )
    returns = normalized["close"] / normalized["pre_close"] - 1.0
    return ContinuousPriceResult(
        frame=normalized,
        returns=returns.astype("float64"),
        applications=applications,
    )


def snapshot_return_panel(
    snapshot: SnapshotView,
    securities: Sequence[str] | None = None,
) -> pd.DataFrame:
    available = {str(row["security"]) for row in snapshot.rows}
    selected = tuple(sorted(securities or available))
    unknown = sorted(set(selected) - available)
    if unknown:
        raise EconomicReturnError(
            "snapshot is missing requested securities: " + ", ".join(unknown)
        )
    columns: dict[str, pd.Series] = {}
    for security in selected:
        frame = pd.DataFrame(
            dict(row) for row in snapshot.rows if row["security"] == security
        )
        actions = [
            row
            for row in snapshot.corporate_actions
            if row["security"] == security
        ]
        result = derive_continuous_prices(
            frame,
            security=security,
            corporate_actions=actions,
        )
        columns[security] = pd.Series(
            result.returns.to_numpy(),
            index=pd.to_datetime(result.frame["date"]).dt.normalize(),
            dtype="float64",
        )
    return pd.DataFrame(columns).sort_index()


def canonical_corporate_actions_digest(
    actions: Sequence[Mapping[str, object]],
) -> str:
    return corporate_actions_digest(actions)
