from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from statistics import median
from typing import Literal, Mapping, Sequence

import pandas as pd

from .contracts import (
    MarketDataContractError,
    corporate_actions_digest,
    normalize_market_rows,
)
from .economic_returns import EconomicReturnError, derive_continuous_prices
from .storage import MarketDataIntegrityError


@dataclass(frozen=True)
class CandidateScreenRule:
    min_valid_days: int = 750
    money_lookback_days: int = 20
    min_median_money: float = 100_000_000.0

    def __post_init__(self) -> None:
        if self.min_valid_days <= 0 or self.money_lookback_days <= 0:
            raise ValueError("candidate screen day thresholds must be positive")
        if not math.isfinite(self.min_median_money) or self.min_median_money < 0:
            raise ValueError("candidate screen money threshold must be non-negative")


@dataclass(frozen=True)
class SecurityScreenResult:
    security: str
    status: Literal["pass", "fail"]
    valid_days: int
    median_money_20d: float | None
    official_start_date: str | None
    first_market_date: str | None
    last_market_date: str | None
    market_rows: int
    corporate_actions_digest: str
    reason_codes: tuple[str, ...]
    instrument_risk_notes: tuple[str, ...]


@dataclass(frozen=True)
class CandidateScreenResult:
    screen_id: str
    as_of_date: str
    requested_securities: tuple[str, ...]
    passed_securities: tuple[str, ...]
    rule: CandidateScreenRule
    results: tuple[SecurityScreenResult, ...]


def _finite_number(value: object) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _integer(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        result = int(value)
    except (TypeError, ValueError):
        return None
    return result if result == value else None


def _valid_non_paused_row(row: Mapping[str, object]) -> tuple[bool, str | None]:
    if row.get("paused") is True:
        return False, None
    values = {
        field: _finite_number(row.get(field))
        for field in (
            "open",
            "high",
            "low",
            "close",
            "pre_close",
            "volume",
            "money",
            "factor",
            "high_limit",
            "low_limit",
        )
    }
    if any(value is None for value in values.values()):
        return False, "incomplete_non_paused_fields"
    open_price = float(values["open"])
    high = float(values["high"])
    low = float(values["low"])
    close = float(values["close"])
    pre_close = float(values["pre_close"])
    volume = float(values["volume"])
    money = float(values["money"])
    factor = float(values["factor"])
    high_limit = float(values["high_limit"])
    low_limit = float(values["low_limit"])
    if (
        min(open_price, high, low, close, pre_close, factor) <= 0.0
        or volume < 0.0
        or money < 0.0
        or high < max(open_price, low, close)
        or low > min(open_price, high, close)
        or high_limit < low_limit
    ):
        return False, "illegal_ohlc"
    return True, None


def _canonical_document(result: CandidateScreenResult) -> dict[str, object]:
    document = asdict(result)
    document["requested_securities"] = list(result.requested_securities)
    document["passed_securities"] = list(result.passed_securities)
    document["results"] = [
        {
            **asdict(item),
            "reason_codes": list(item.reason_codes),
            "instrument_risk_notes": list(item.instrument_risk_notes),
        }
        for item in result.results
    ]
    return document


def _screen_id(result: CandidateScreenResult) -> str:
    document = _canonical_document(replace(result, screen_id=""))
    document.pop("screen_id", None)
    payload = json.dumps(
        document,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _screen_security(
    *,
    security: str,
    raw_rows: Sequence[Mapping[str, object]],
    corporate_actions: Sequence[Mapping[str, object]],
    metadata: Mapping[str, object] | None,
    as_of_date: str,
    rule: CandidateScreenRule,
    risk_notes: Sequence[str],
) -> SecurityScreenResult:
    reasons: set[str] = set()
    try:
        rows = normalize_market_rows(raw_rows)
    except MarketDataContractError:
        rows = []
        reasons.add("invalid_market_contract")
    if len(rows) != len(raw_rows):
        reasons.add("duplicate_market_rows")
    if any(str(row["date"]) > as_of_date for row in rows):
        reasons.add("market_rows_after_cutoff")
        rows = [row for row in rows if str(row["date"]) <= as_of_date]

    valid_rows: list[dict[str, object]] = []
    for row in rows:
        is_valid, reason = _valid_non_paused_row(row)
        if reason:
            reasons.add(reason)
        if is_valid:
            valid_rows.append(row)
    if len(valid_rows) < rule.min_valid_days:
        reasons.add(f"valid_days_below_{rule.min_valid_days}")

    median_money: float | None = None
    if len(valid_rows) >= rule.money_lookback_days:
        money_values = [
            float(row["money"])
            for row in valid_rows[-rule.money_lookback_days :]
        ]
        median_money = float(median(money_values))
        if median_money < rule.min_median_money:
            reasons.add(
                "median_money_below_" + str(int(rule.min_median_money))
            )
    else:
        reasons.add("insufficient_money_lookback")

    first_date = str(rows[0]["date"]) if rows else None
    last_date = str(rows[-1]["date"]) if rows else None
    official_start: str | None = None
    if metadata is None:
        reasons.add("official_metadata_missing")
    else:
        if str(metadata.get("security", "")) != security:
            reasons.add("official_security_mismatch")
        official_start = str(metadata.get("official_start_date") or "") or None
        if (
            official_start is None
            or first_date is None
            or official_start > first_date
            or str(metadata.get("first_market_date") or "") != first_date
            or str(metadata.get("last_market_date") or "") != last_date
            or _integer(metadata.get("market_rows")) != len(rows)
        ):
            reasons.add("official_coverage_mismatch")

    selected_actions = tuple(
        action
        for action in corporate_actions
        if str(action.get("security", "")) == security
    )
    action_digest = corporate_actions_digest(selected_actions)
    if rows:
        try:
            derive_continuous_prices(
                pd.DataFrame(rows),
                security=security,
                corporate_actions=selected_actions,
            )
        except (EconomicReturnError, ValueError):
            reasons.add("corporate_action_evidence_unreconciled")

    ordered_reasons = tuple(sorted(reasons))
    return SecurityScreenResult(
        security=security,
        status="fail" if ordered_reasons else "pass",
        valid_days=len(valid_rows),
        median_money_20d=median_money,
        official_start_date=official_start,
        first_market_date=first_date,
        last_market_date=last_date,
        market_rows=len(rows),
        corporate_actions_digest=action_digest,
        reason_codes=ordered_reasons,
        instrument_risk_notes=tuple(str(note) for note in risk_notes),
    )


def screen_candidates(
    *,
    rows: Sequence[Mapping[str, object]],
    corporate_actions: Sequence[Mapping[str, object]],
    official_security_metadata: Mapping[str, Mapping[str, object]],
    requested_securities: Sequence[str],
    as_of_date: str,
    rule: CandidateScreenRule,
    instrument_risk_notes: Mapping[str, Sequence[str]] | None = None,
) -> CandidateScreenResult:
    try:
        pd.Timestamp(as_of_date)
    except (TypeError, ValueError) as exc:
        raise ValueError("as_of_date must be a valid date") from exc
    requested = tuple(sorted(str(value) for value in requested_securities))
    if not requested or len(set(requested)) != len(requested):
        raise ValueError("requested securities must be non-empty and unique")
    notes = instrument_risk_notes or {}
    results = tuple(
        _screen_security(
            security=security,
            raw_rows=tuple(
                row for row in rows if str(row.get("security", "")) == security
            ),
            corporate_actions=corporate_actions,
            metadata=official_security_metadata.get(security),
            as_of_date=as_of_date,
            rule=rule,
            risk_notes=notes.get(security, ()),
        )
        for security in requested
    )
    unknown_rows = sorted(
        {str(row.get("security", "")) for row in rows} - set(requested)
    )
    if unknown_rows:
        raise ValueError(
            "candidate rows include unrequested securities: " + ", ".join(unknown_rows)
        )
    provisional = CandidateScreenResult(
        screen_id="",
        as_of_date=as_of_date,
        requested_securities=requested,
        passed_securities=tuple(
            item.security for item in results if item.status == "pass"
        ),
        rule=rule,
        results=results,
    )
    return replace(provisional, screen_id=_screen_id(provisional))


def write_candidate_screen(result: CandidateScreenResult, *, root: Path) -> Path:
    document = _canonical_document(result)
    payload = (
        json.dumps(
            document,
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")
    target = Path(root) / "screens" / f"{result.screen_id}.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        if target.read_bytes() != payload:
            raise MarketDataIntegrityError("candidate screen content conflict")
        return target
    try:
        with target.open("xb") as handle:
            handle.write(payload)
    except FileExistsError:
        if target.read_bytes() != payload:
            raise MarketDataIntegrityError("candidate screen content conflict")
    return target
