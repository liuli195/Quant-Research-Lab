from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Iterable, Mapping, Sequence

import duckdb
import pyarrow.parquet as pq

from .contracts import (
    CORPORATE_ACTION_FIELDS,
    MARKET_DATA_FIELDS,
    MarketDataContractError,
    corporate_actions_digest,
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
    corporate_action_fields: tuple[str, ...]
    corporate_actions: tuple[Mapping[str, object], ...]
    corporate_actions_digest: str


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


def _read_corporate_action_rows(
    connection,
    parquet_paths: Sequence[Path],
) -> list[dict[str, object]]:
    relation = connection.read_parquet([str(path) for path in parquet_paths])
    columns = tuple(relation.columns)
    if columns != CORPORATE_ACTION_FIELDS:
        raise MarketDataIntegrityError(
            "DuckDB corporate-actions field order does not match the contract"
        )
    return [dict(zip(columns, values)) for values in relation.fetchall()]


def _read_corporate_action_parquet_rows(
    parquet_paths: Sequence[Path],
) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for path in parquet_paths:
        table = pq.read_table(path)
        if tuple(table.column_names) != CORPORATE_ACTION_FIELDS:
            raise MarketDataIntegrityError(
                "corporate-actions Parquet field order does not match the contract"
            )
        rows.extend(table.to_pylist())
    return rows


def _select_corporate_actions(
    rows: Iterable[Mapping[str, object]],
    *,
    securities: Sequence[str],
    start_date: str,
    end_date: str,
) -> list[dict[str, object]]:
    selected = set(securities)
    by_id: dict[str, dict[str, object]] = {}
    for raw in rows:
        row = dict(raw)
        if row.get("security") not in selected:
            continue
        effective_date = str(row.get("effective_date") or "")
        if not start_date <= effective_date <= end_date:
            continue
        event_id = str(row.get("source_event_id") or "")
        existing = by_id.get(event_id)
        if existing is not None and existing != row:
            raise MarketDataIntegrityError(
                f"snapshot corporate-actions conflict at {event_id}"
            )
        by_id[event_id] = row
    return [by_id[key] for key in sorted(by_id)]


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
    corporate_action_paths = [
        Path(root) / "batches" / batch_id / "corporate-actions.parquet"
        for batch_id in batch_ids
    ]
    connection = duckdb.connect(":memory:")
    try:
        query_rows = _read_query_rows(connection, parquet_paths)
        query_action_rows = _read_corporate_action_rows(
            connection, corporate_action_paths
        )
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
    selected_query_actions = _select_corporate_actions(
        query_action_rows,
        **selection_args,
    )
    selected_parquet_actions = _select_corporate_actions(
        _read_corporate_action_parquet_rows(corporate_action_paths),
        **selection_args,
    )
    action_digest = corporate_actions_digest(selected_query_actions)
    if action_digest != corporate_actions_digest(selected_parquet_actions):
        raise MarketDataIntegrityError(
            "DuckDB and authoritative corporate-actions Parquet digest mismatch"
        )

    immutable_rows = tuple(
        MappingProxyType(dict(row)) for row in normalized_query_rows
    )
    immutable_actions = tuple(
        MappingProxyType(dict(row)) for row in selected_query_actions
    )
    return SnapshotView(
        snapshot_id=snapshot.snapshot_id,
        fields=fields,
        rows=immutable_rows,
        digest=query_digest,
        corporate_action_fields=CORPORATE_ACTION_FIELDS,
        corporate_actions=immutable_actions,
        corporate_actions_digest=action_digest,
    )
