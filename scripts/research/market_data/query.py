from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Iterable, Mapping, Sequence

import duckdb
import pyarrow.parquet as pq

from .contracts import (
    MARKET_DATA_FIELDS,
    MarketDataContractError,
    normalize_market_rows,
    normalized_digest,
)
from .storage import MarketDataIntegrityError, validate_snapshot


@dataclass(frozen=True)
class SnapshotView:
    snapshot_id: str
    fields: tuple[str, ...]
    rows: tuple[Mapping[str, object], ...]
    digest: str


def _read_query_rows(connection, parquet_paths: Sequence[Path]) -> list[dict[str, object]]:
    relation = connection.read_parquet([str(path) for path in parquet_paths])
    columns = tuple(relation.columns)
    if columns != MARKET_DATA_FIELDS:
        raise MarketDataIntegrityError(
            "DuckDB field order does not match the fixed market-data contract"
        )
    return [dict(zip(columns, values)) for values in relation.fetchall()]


def _read_parquet_rows(parquet_paths: Sequence[Path]) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for path in parquet_paths:
        table = pq.read_table(path)
        if tuple(table.column_names) != MARKET_DATA_FIELDS:
            raise MarketDataIntegrityError(
                "Parquet field order does not match the fixed market-data contract"
            )
        rows.extend(table.to_pylist())
    return rows


def _normalize_selected_rows(
    rows: Iterable[Mapping[str, object]],
    *,
    securities: Sequence[str],
    start_date: str,
    end_date: str,
) -> list[dict[str, object]]:
    selected = set(securities)
    try:
        normalized = normalize_market_rows(rows)
    except MarketDataContractError as exc:
        raise MarketDataIntegrityError(str(exc)) from exc
    return [
        row
        for row in normalized
        if row["security"] in selected and start_date <= str(row["date"]) <= end_date
    ]


def open_snapshot(snapshot_id: str, *, root: Path) -> SnapshotView:
    snapshot = validate_snapshot(snapshot_id, root=root)
    selection = snapshot.document["selection"]
    fields = tuple(str(field) for field in selection["fields"])
    if fields != MARKET_DATA_FIELDS:
        raise MarketDataIntegrityError(
            "snapshot fields do not match the fixed daily market-data contract"
        )

    batch_ids = tuple(str(batch_id) for batch_id in snapshot.document["batch_ids"])
    parquet_paths = [
        Path(root) / "batches" / batch_id / "market-data.parquet"
        for batch_id in batch_ids
    ]
    connection = duckdb.connect(":memory:")
    try:
        query_rows = _read_query_rows(connection, parquet_paths)
    finally:
        connection.close()

    selection_args = {
        "securities": tuple(str(value) for value in selection["securities"]),
        "start_date": str(selection["start_date"]),
        "end_date": str(selection["end_date"]),
    }
    normalized_query_rows = _normalize_selected_rows(query_rows, **selection_args)
    normalized_parquet_rows = _normalize_selected_rows(
        _read_parquet_rows(parquet_paths),
        **selection_args,
    )
    query_digest = normalized_digest(normalized_query_rows)
    if query_digest != normalized_digest(normalized_parquet_rows):
        raise MarketDataIntegrityError(
            "DuckDB and authoritative Parquet normalized digest mismatch"
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
