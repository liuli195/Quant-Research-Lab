from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from collections.abc import Iterable
from pathlib import Path

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq

from .archive import IntegrityError, verify_existing_manifest


class QueryError(RuntimeError):
    """Raised when manifest-backed data cannot be queried safely."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_parquet(
    rows: Iterable[dict[str, object]],
    destination: Path,
    *,
    root: Path | None = None,
) -> dict[str, object]:
    materialized = list(rows)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
    try:
        pq.write_table(
            pa.Table.from_pylist(materialized), temporary, compression="zstd"
        )
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    archive_root = (root or destination.parent).resolve()
    try:
        relative = destination.resolve().relative_to(archive_root).as_posix()
    except ValueError:
        raise QueryError("Parquet destination is outside object root") from None
    return {
        "path": relative,
        "sha256": _sha256(destination),
        "bytes": destination.stat().st_size,
        "rows": len(materialized),
        "format": "parquet",
        "compression": "zstd",
    }


def _load_manifest(path: Path) -> dict[str, object]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError) as error:
        raise QueryError(f"invalid manifest: {path}") from error
    if not isinstance(data, dict):
        raise QueryError("manifest root must be an object")
    return data


def _safe_file(manifest_path: Path, relative: str) -> Path:
    candidate = Path(relative)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise QueryError(f"unsafe manifest path: {relative}")
    path = (manifest_path.parent / candidate).resolve()
    try:
        path.relative_to(manifest_path.parent.resolve())
    except ValueError:
        raise QueryError(f"unsafe manifest path: {relative}") from None
    return path


def _identifier(name: str) -> str:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
        raise QueryError(f"unsafe dataset or field name: {name}")
    return f'"{name}"'


def open_views(
    manifest_path: Path,
    connection: duckdb.DuckDBPyConnection,
    *,
    manifest: dict[str, object] | None = None,
) -> list[str]:
    try:
        verified = verify_existing_manifest(manifest_path.parent)
    except IntegrityError as error:
        raise QueryError(str(error)) from error
    if manifest is None:
        manifest = verified
    elif manifest != verified:
        raise QueryError("manifest changed during query")
    gate = manifest.get("gate")
    if not isinstance(gate, dict) or gate.get("status") != "pass":
        raise QueryError("manifest gate did not pass")
    datasets = manifest.get("datasets")
    if not isinstance(datasets, dict):
        raise QueryError("manifest datasets are missing")
    opened: list[str] = []
    for name, dataset in datasets.items():
        if not isinstance(dataset, dict) or dataset.get("status") != "complete":
            continue
        files = [
            item
            for item in dataset.get("files") or []
            if isinstance(item, dict) and item.get("format") == "parquet"
        ]
        if not files:
            continue
        paths: list[Path] = []
        for item in files:
            path = _safe_file(manifest_path, str(item["path"]))
            if not path.is_file() or _sha256(path) != item.get("sha256"):
                raise QueryError(f"dataset file failed hash check: {name}")
            paths.append(path)
        literals = ",".join(
            "'" + path.as_posix().replace("'", "''") + "'" for path in paths
        )
        connection.execute(
            f"CREATE OR REPLACE TEMP VIEW {_identifier(str(name))} AS "
            f"SELECT * FROM read_parquet([{literals}], union_by_name=true)"
        )
        actual = connection.execute(
            f"SELECT count(*) FROM {_identifier(str(name))}"
        ).fetchone()[0]
        if actual != dataset.get("rows"):
            raise QueryError(f"manifest row count mismatch: {name}")
        opened.append(str(name))
    return opened


def export_csv(
    manifest_path: Path,
    dataset: str,
    fields: list[str],
    start: str | None,
    end: str | None,
    destination: Path,
) -> dict[str, object]:
    if not fields:
        raise QueryError("at least one field is required")
    manifest = _load_manifest(manifest_path)
    connection = duckdb.connect(":memory:")
    try:
        if dataset not in open_views(manifest_path, connection, manifest=manifest):
            raise QueryError(f"dataset is not queryable: {dataset}")
        columns = {
            row[0]
            for row in connection.execute(f"DESCRIBE {_identifier(dataset)}").fetchall()
        }
        if any(field not in columns for field in fields):
            raise QueryError("requested field is missing")
        parameters: list[str] = []
        clauses: list[str] = []
        if start or end:
            if "time" not in columns:
                raise QueryError("time range requires a time field")
            if start:
                clauses.append('CAST("time" AS VARCHAR) >= ?')
                parameters.append(start)
            if end:
                clauses.append('CAST("time" AS VARCHAR) <= ?')
                parameters.append(end)
        select = ", ".join(_identifier(field) for field in fields)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        frame = connection.execute(
            f"SELECT {select} FROM {_identifier(dataset)}{where}", parameters
        ).fetchdf()
    finally:
        connection.close()
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
    try:
        frame.to_csv(temporary, index=False)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    source_files = manifest["datasets"][dataset]["files"]
    return {
        "path": str(destination),
        "bytes": destination.stat().st_size,
        "sha256": _sha256(destination),
        "rows": len(frame),
        "source_sha256": [item["sha256"] for item in source_files],
        "filters": {"fields": fields, "start": start, "end": end},
    }


def query_rows(
    manifest_path: Path, dataset: str, limit: int = 100
) -> list[dict[str, object]]:
    if limit < 1 or limit > 10_000:
        raise QueryError("limit must be between 1 and 10000")
    connection = duckdb.connect(":memory:")
    try:
        if dataset not in open_views(manifest_path, connection):
            raise QueryError(f"dataset is not queryable: {dataset}")
        cursor = connection.execute(
            f"SELECT * FROM {_identifier(dataset)} LIMIT ?", [limit]
        )
        columns = [item[0] for item in cursor.description]
        return [dict(zip(columns, row, strict=True)) for row in cursor.fetchall()]
    finally:
        connection.close()
