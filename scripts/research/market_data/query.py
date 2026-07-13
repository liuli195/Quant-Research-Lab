from __future__ import annotations

import csv
import hashlib
import json
import math
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from types import MappingProxyType
from typing import Iterable, Mapping, Sequence

import duckdb

from .storage import MarketDataIntegrityError, validate_snapshot


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


@dataclass(frozen=True)
class SnapshotView:
    snapshot_id: str
    fields: tuple[str, ...]
    rows: tuple[Mapping[str, object], ...]
    digest: str


def normalized_digest(rows: Iterable[Mapping[str, object]]) -> str:
    canonical_rows = [dict(row) for row in rows]
    canonical_rows.sort(
        key=lambda row: (
            str(row.get("date", "")),
            str(row.get("security", "")),
            json.dumps(
                row,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ),
        )
    )
    payload = json.dumps(
        canonical_rows,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _read_query_rows(connection, csv_paths: Sequence[Path]) -> list[dict[str, object]]:
    relation = connection.read_csv(
        [str(path) for path in csv_paths],
        header=True,
        all_varchar=True,
    )
    columns = tuple(relation.columns)
    if columns != MARKET_DATA_FIELDS:
        raise MarketDataIntegrityError(
            "DuckDB field order does not match the fixed market-data contract"
        )
    return [dict(zip(columns, values)) for values in relation.fetchall()]


def _read_csv_rows(csv_paths: Sequence[Path]) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for path in csv_paths:
        with path.open(encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            if tuple(reader.fieldnames or ()) != MARKET_DATA_FIELDS:
                raise MarketDataIntegrityError(
                    "CSV field order does not match the fixed market-data contract"
                )
            rows.extend(dict(row) for row in reader)
    return rows


def _normalize_number(value: object, field: str) -> float | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    try:
        normalized = float(value)
    except (TypeError, ValueError) as exc:
        raise MarketDataIntegrityError(f"invalid numeric value for {field}") from exc
    if not math.isfinite(normalized):
        raise MarketDataIntegrityError(f"non-finite numeric value for {field}")
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
        raise MarketDataIntegrityError("paused must be 0, 1, false or true")
    return normalized == 1.0


def _normalize_selected_rows(
    rows: Iterable[Mapping[str, object]],
    *,
    securities: Sequence[str],
    start_date: str,
    end_date: str,
) -> list[dict[str, object]]:
    selected = set(securities)
    normalized_by_key: dict[tuple[str, str], dict[str, object]] = {}
    for raw in rows:
        if set(raw) != set(MARKET_DATA_FIELDS):
            raise MarketDataIntegrityError(
                "market-data row does not match the fixed field contract"
            )
        row_date = str(raw["date"] or "").strip()
        security = str(raw["security"] or "").strip()
        try:
            date.fromisoformat(row_date)
        except ValueError as exc:
            raise MarketDataIntegrityError(f"invalid date: {row_date}") from exc
        if security not in selected or not start_date <= row_date <= end_date:
            continue
        normalized: dict[str, object] = {
            "date": row_date,
            "security": security,
        }
        for field in MARKET_DATA_FIELDS[2:]:
            if field == "paused":
                normalized[field] = _normalize_paused(raw[field])
            elif field in _NUMERIC_FIELDS:
                normalized[field] = _normalize_number(raw[field], field)
        key = (row_date, security)
        existing = normalized_by_key.get(key)
        if existing is not None and existing != normalized:
            raise MarketDataIntegrityError(
                f"conflicting normalized row for {security} {row_date}"
            )
        normalized_by_key[key] = normalized
    return [normalized_by_key[key] for key in sorted(normalized_by_key)]


def open_snapshot(snapshot_id: str, *, root: Path) -> SnapshotView:
    snapshot = validate_snapshot(snapshot_id, root=root)
    selection = snapshot.document["selection"]
    fields = tuple(str(field) for field in selection["fields"])
    if fields != MARKET_DATA_FIELDS:
        raise MarketDataIntegrityError(
            "snapshot fields do not match the fixed daily market-data contract"
        )

    batch_ids = tuple(str(batch_id) for batch_id in snapshot.document["batch_ids"])
    csv_paths = [Path(root) / "batches" / batch_id / "market-data.csv" for batch_id in batch_ids]
    connection = duckdb.connect(":memory:")
    try:
        query_rows = _read_query_rows(connection, csv_paths)
    finally:
        connection.close()

    selection_args = {
        "securities": tuple(str(value) for value in selection["securities"]),
        "start_date": str(selection["start_date"]),
        "end_date": str(selection["end_date"]),
    }
    normalized_query_rows = _normalize_selected_rows(query_rows, **selection_args)
    normalized_csv_rows = _normalize_selected_rows(
        _read_csv_rows(csv_paths),
        **selection_args,
    )
    query_digest = normalized_digest(normalized_query_rows)
    if query_digest != normalized_digest(normalized_csv_rows):
        raise MarketDataIntegrityError(
            "DuckDB and authoritative CSV normalized digest mismatch"
        )

    immutable_rows = tuple(
        MappingProxyType(dict(row)) for row in normalized_query_rows
    )
    return SnapshotView(
        snapshot_id=snapshot.snapshot_id,
        fields=fields,
        rows=immutable_rows,
        digest=query_digest,
    )
