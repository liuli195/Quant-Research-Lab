from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from types import MappingProxyType
from typing import Any, Iterable, Mapping, Sequence


MARKET_DATA_FIELDS = (
    "date",
    "security",
    "open",
    "high",
    "low",
    "close",
    "pre_close",
    "volume",
    "money",
    "factor",
    "paused",
    "high_limit",
    "low_limit",
)
_NUMERIC_FIELDS = frozenset(MARKET_DATA_FIELDS) - {"date", "security", "paused"}


class MarketDataContractError(ValueError):
    """Raised when a daily market-data row violates the shared contract."""


def _normalize_number(value: object, field: str) -> float | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    try:
        normalized = float(value)
    except (TypeError, ValueError) as exc:
        raise MarketDataContractError(f"invalid numeric value for {field}") from exc
    if not math.isfinite(normalized):
        raise MarketDataContractError(f"non-finite numeric value for {field}")
    return normalized


def _normalize_paused(value: object) -> bool | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "false"}:
            return lowered == "true"
    normalized = _normalize_number(value, "paused")
    if normalized not in {0.0, 1.0}:
        raise MarketDataContractError("paused must be 0, 1, false or true")
    return normalized == 1.0


def normalize_market_rows(
    rows: Iterable[Mapping[str, object]],
) -> list[dict[str, object]]:
    normalized_by_key: dict[tuple[str, str], dict[str, object]] = {}
    for raw in rows:
        if set(raw) != set(MARKET_DATA_FIELDS):
            raise MarketDataContractError(
                "market-data row does not match the fixed field contract"
            )
        row_date = str(raw["date"] or "").strip()
        security = str(raw["security"] or "").strip()
        try:
            date.fromisoformat(row_date)
        except ValueError as exc:
            raise MarketDataContractError(f"invalid date: {row_date}") from exc
        if not security:
            raise MarketDataContractError("security must be non-empty")
        normalized: dict[str, object] = {"date": row_date, "security": security}
        for field in MARKET_DATA_FIELDS[2:]:
            if field == "paused":
                normalized[field] = _normalize_paused(raw[field])
            elif field in _NUMERIC_FIELDS:
                normalized[field] = _normalize_number(raw[field], field)
        key = (row_date, security)
        existing = normalized_by_key.get(key)
        if existing is not None and existing != normalized:
            raise MarketDataContractError(
                f"conflicting normalized row for {security} {row_date}"
            )
        normalized_by_key[key] = normalized
    return [normalized_by_key[key] for key in sorted(normalized_by_key)]


def normalized_digest(rows: Iterable[Mapping[str, object]]) -> str:
    canonical_rows = [dict(row) for row in rows]
    canonical_rows.sort(key=lambda row: (str(row["date"]), str(row["security"])))
    payload = json.dumps(
        canonical_rows,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _deep_freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType(
            {str(key): _deep_freeze(item) for key, item in value.items()}
        )
    if isinstance(value, (list, tuple)):
        return tuple(_deep_freeze(item) for item in value)
    return value


@dataclass(frozen=True)
class BatchRecord:
    batch_id: str
    path: Path
    manifest: Mapping[str, Any]

    def __post_init__(self) -> None:
        object.__setattr__(self, "manifest", _deep_freeze(self.manifest))


@dataclass(frozen=True)
class SnapshotSelection:
    source: Mapping[str, object]
    asset_type: str
    frequency: str
    securities: Sequence[str]
    start_date: str
    end_date: str
    fields: Sequence[str]
    price_semantics: Mapping[str, object]

    def __post_init__(self) -> None:
        object.__setattr__(self, "source", _deep_freeze(self.source))
        object.__setattr__(self, "securities", tuple(self.securities))
        object.__setattr__(self, "fields", tuple(self.fields))
        object.__setattr__(
            self,
            "price_semantics",
            _deep_freeze(self.price_semantics),
        )

    def to_document(self) -> dict[str, object]:
        return {
            "source": dict(self.source),
            "asset_type": self.asset_type,
            "frequency": self.frequency,
            "securities": sorted(self.securities),
            "start_date": self.start_date,
            "end_date": self.end_date,
            "fields": list(self.fields),
            "price_semantics": dict(self.price_semantics),
        }


@dataclass(frozen=True)
class SnapshotRecord:
    snapshot_id: str
    path: Path
    document: Mapping[str, Any]

    def __post_init__(self) -> None:
        object.__setattr__(self, "document", _deep_freeze(self.document))
