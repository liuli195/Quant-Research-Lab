from __future__ import annotations

import csv
import hashlib
import json
import os
import re
import shutil
import tempfile
import time
from collections import defaultdict
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .contracts import BatchRecord, SnapshotRecord, SnapshotSelection


_BATCH_FILES = {"manifest.json", "market-data.csv", "validation.json"}
_REQUIRED_MANIFEST_FIELDS = {
    "schema_version",
    "source",
    "asset_type",
    "frequency",
    "fields",
    "price_semantics",
    "export_code_sha256",
}
_STORED_MANIFEST_FIELDS = _REQUIRED_MANIFEST_FIELDS | {"csv", "securities"}
_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")


class MarketDataError(RuntimeError):
    """Base error for immutable market-data storage."""


class MarketDataConflict(MarketDataError):
    """Raised when authoritative batches disagree on an overlapping key."""


class MarketDataIntegrityError(MarketDataError):
    """Raised when stored evidence no longer matches its manifest."""


class UnsupportedMarketData(MarketDataError):
    """Raised for data outside the first supported capability."""


def _require_identifier(value: object, field: str) -> str:
    if not isinstance(value, str) or _SHA256_PATTERN.fullmatch(value) is None:
        raise MarketDataIntegrityError(f"{field} identifier must be a SHA256 digest")
    return value


@contextmanager
def _exclusive_storage_lock(root: Path, *, timeout_seconds: float = 30.0):
    storage_root = Path(root)
    storage_root.mkdir(parents=True, exist_ok=True)
    lock_path = storage_root / ".market-data.lock"
    handle = lock_path.open("a+b")
    if lock_path.stat().st_size == 0:
        handle.write(b"\0")
        handle.flush()
    deadline = time.monotonic() + timeout_seconds
    locked = False
    try:
        if os.name == "nt":
            import msvcrt

            while not locked:
                handle.seek(0)
                try:
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    locked = True
                except OSError:
                    if time.monotonic() >= deadline:
                        raise MarketDataIntegrityError(
                            "timed out waiting for the market-data storage lock"
                        )
                    time.sleep(0.01)
        else:
            import fcntl

            while not locked:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    locked = True
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise MarketDataIntegrityError(
                            "timed out waiting for the market-data storage lock"
                        )
                    time.sleep(0.01)
        yield
    finally:
        if locked:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _json_file_bytes(value: object) -> bytes:
    return _canonical_bytes(value) + b"\n"


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_path(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


def _require_mapping(value: object, field: str) -> dict[str, object]:
    if not isinstance(value, Mapping) or not value:
        raise MarketDataIntegrityError(f"{field} must be a non-empty mapping")
    return dict(value)


def _validate_manifest_input(manifest: Mapping[str, object]) -> dict[str, object]:
    missing = sorted(_REQUIRED_MANIFEST_FIELDS - set(manifest))
    if missing:
        raise MarketDataIntegrityError(
            f"manifest is missing required fields: {', '.join(missing)}"
        )
    if manifest["schema_version"] != 1:
        raise MarketDataIntegrityError("manifest schema_version must be 1")

    source = _require_mapping(manifest["source"], "source")
    if source.get("name") != "joinquant":
        raise UnsupportedMarketData("only source=joinquant is supported")
    if manifest["asset_type"] != "etf":
        raise UnsupportedMarketData("only asset_type=etf is supported")
    if manifest["frequency"] != "1d":
        raise UnsupportedMarketData("only frequency=1d is supported")

    fields = manifest["fields"]
    if not isinstance(fields, Sequence) or isinstance(fields, (str, bytes)):
        raise MarketDataIntegrityError("fields must be an ordered sequence")
    field_list = [str(item) for item in fields]
    if len(field_list) != len(set(field_list)):
        raise MarketDataIntegrityError("fields must be unique")
    if not {"date", "security"}.issubset(field_list):
        raise MarketDataIntegrityError("fields must include date and security")

    price_semantics = _require_mapping(manifest["price_semantics"], "price_semantics")
    export_digest = manifest["export_code_sha256"]
    if (
        not isinstance(export_digest, str)
        or len(export_digest) != 64
        or any(char not in "0123456789abcdefABCDEF" for char in export_digest)
    ):
        raise MarketDataIntegrityError("export_code_sha256 must be a SHA256 digest")

    return {
        "schema_version": 1,
        "source": source,
        "asset_type": "etf",
        "frequency": "1d",
        "fields": field_list,
        "price_semantics": price_semantics,
        "export_code_sha256": export_digest.lower(),
    }


def _read_csv(
    path: Path,
    expected_fields: Sequence[str],
) -> tuple[list[dict[str, str]], list[dict[str, object]]]:
    try:
        with path.open(encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            if reader.fieldnames != list(expected_fields):
                raise MarketDataIntegrityError(
                    "CSV field order does not match the declared fields"
                )
            rows = [dict(row) for row in reader]
    except UnicodeDecodeError as exc:
        raise MarketDataIntegrityError("CSV must use UTF-8 encoding") from exc

    if not rows:
        raise MarketDataIntegrityError("CSV must contain at least one data row")

    keys: set[tuple[str, str]] = set()
    security_dates: dict[str, list[str]] = defaultdict(list)
    for row in rows:
        if None in row or any(value is None for value in row.values()):
            raise MarketDataIntegrityError(
                "CSV row column count does not match the declared fields"
            )
        security = row.get("security", "").strip()
        row_date = row.get("date", "").strip()
        if not security or not row_date:
            raise MarketDataIntegrityError("date and security must be non-empty")
        try:
            date.fromisoformat(row_date)
        except ValueError as exc:
            raise MarketDataIntegrityError(f"invalid date: {row_date}") from exc
        key = (security, row_date)
        if key in keys:
            raise MarketDataIntegrityError(
                f"duplicate date/security key: {security} {row_date}"
            )
        keys.add(key)
        security_dates[security].append(row_date)

    securities = [
        {
            "security": security,
            "start_date": min(dates),
            "end_date": max(dates),
            "rows": len(dates),
        }
        for security, dates in sorted(security_dates.items())
    ]
    return rows, securities


def _batch_identity(manifest: Mapping[str, object], csv_sha256: str) -> dict[str, object]:
    return {
        "source": manifest["source"],
        "asset_type": manifest["asset_type"],
        "frequency": manifest["frequency"],
        "fields": manifest["fields"],
        "price_semantics": manifest["price_semantics"],
        "export_code_sha256": manifest["export_code_sha256"],
        "csv_sha256": csv_sha256,
    }


def _dataset_identity(manifest: Mapping[str, object]) -> tuple[bytes, object, object]:
    return (
        _canonical_bytes(manifest["source"]),
        manifest["asset_type"],
        manifest["frequency"],
    )


def _validation_document() -> dict[str, object]:
    return {
        "schema_version": 1,
        "status": "complete",
        "checks": {
            "field_order": True,
            "nonempty": True,
            "unique_date_security": True,
        },
    }


def _load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MarketDataIntegrityError(f"invalid JSON evidence: {path}") from exc
    if not isinstance(value, dict):
        raise MarketDataIntegrityError(f"JSON evidence must be an object: {path}")
    return value


def _validate_batch_dir(batch_dir: Path) -> dict[str, Any]:
    _require_identifier(batch_dir.name, "batch")
    if not batch_dir.is_dir():
        raise MarketDataIntegrityError(f"batch does not exist: {batch_dir.name}")
    names = {path.name for path in batch_dir.iterdir()}
    if names != _BATCH_FILES:
        raise MarketDataIntegrityError(
            f"batch file set is invalid: {batch_dir.name}"
        )
    manifest = _load_json(batch_dir / "manifest.json")
    if set(manifest) != _STORED_MANIFEST_FIELDS:
        raise MarketDataIntegrityError("batch manifest structure is invalid")
    declared = _validate_manifest_input(manifest)
    csv_evidence = manifest.get("csv")
    if not isinstance(csv_evidence, Mapping) or set(csv_evidence) != {
        "sha256",
        "bytes",
        "rows",
    }:
        raise MarketDataIntegrityError("batch manifest is missing CSV evidence")
    expected_sha = csv_evidence.get("sha256")
    csv_path = batch_dir / "market-data.csv"
    csv_bytes = csv_path.read_bytes()
    actual_sha = _sha256_bytes(csv_bytes)
    if actual_sha != expected_sha:
        raise MarketDataIntegrityError(
            f"CSV SHA256 mismatch for batch {batch_dir.name}"
        )
    if len(csv_bytes) != csv_evidence.get("bytes"):
        raise MarketDataIntegrityError(
            f"CSV byte count mismatch for batch {batch_dir.name}"
        )
    validation = _load_json(batch_dir / "validation.json")
    if validation != _validation_document():
        raise MarketDataIntegrityError(
            f"batch validation evidence is invalid: {batch_dir.name}"
        )
    rows, securities = _read_csv(csv_path, declared["fields"])
    if len(rows) != csv_evidence.get("rows"):
        raise MarketDataIntegrityError(
            f"CSV row count mismatch for batch {batch_dir.name}"
        )
    if securities != manifest.get("securities"):
        raise MarketDataIntegrityError(
            f"batch security coverage mismatch: {batch_dir.name}"
        )
    expected_batch_id = _sha256_bytes(
        _canonical_bytes(_batch_identity(declared, actual_sha))
    )
    if batch_dir.name != expected_batch_id:
        raise MarketDataIntegrityError(
            f"batch identity mismatch: {batch_dir.name}"
        )
    return manifest


def _assert_existing_batch_matches(
    batch_dir: Path,
    expected: Mapping[str, bytes],
) -> None:
    _validate_batch_dir(batch_dir)
    for name, expected_bytes in expected.items():
        if (batch_dir / name).read_bytes() != expected_bytes:
            raise MarketDataIntegrityError(
                f"immutable batch collision for {batch_dir.name}/{name}"
            )


def _existing_rows(batch_dir: Path, manifest: Mapping[str, object]) -> dict[tuple[str, str], dict[str, str]]:
    rows, _ = _read_csv(batch_dir / "market-data.csv", manifest["fields"])
    return {(row["security"], row["date"]): row for row in rows}


def _reject_conflicting_overlap(
    *,
    batches_dir: Path,
    incoming_manifest: Mapping[str, object],
    incoming_rows: Iterable[dict[str, str]],
) -> None:
    incoming_by_key = {
        (row["security"], row["date"]): row for row in incoming_rows
    }
    if not batches_dir.exists():
        return
    for batch_dir in sorted(batches_dir.iterdir()):
        if not batch_dir.is_dir() or batch_dir.name.startswith("."):
            continue
        existing_manifest = _validate_batch_dir(batch_dir)
        if _dataset_identity(existing_manifest) != _dataset_identity(incoming_manifest):
            continue
        existing_by_key = _existing_rows(batch_dir, existing_manifest)
        overlap = sorted(incoming_by_key.keys() & existing_by_key.keys())
        if not overlap:
            continue
        if existing_manifest.get("price_semantics") != incoming_manifest.get(
            "price_semantics"
        ):
            security, row_date = overlap[0]
            raise MarketDataConflict(
                f"price semantics conflict at {security} {row_date}"
            )
        for key in overlap:
            if incoming_by_key[key] != existing_by_key[key]:
                security, row_date = key
                raise MarketDataConflict(
                    f"market data conflict at {security} {row_date}"
                )


def _atomic_directory_write(
    target: Path,
    files: Mapping[str, bytes],
) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(
        tempfile.mkdtemp(prefix=f".{target.name}.tmp-", dir=target.parent)
    )
    try:
        for name, content in files.items():
            (temporary / name).write_bytes(content)
        try:
            _publish_directory(temporary, target)
        except OSError:
            if not target.exists():
                raise
            _assert_existing_batch_matches(target, files)
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)


def _publish_directory(source: Path, target: Path) -> None:
    """Atomically publish once a transient Windows directory lock is released."""
    for delay_seconds in (0.02, 0.05, 0.1, 0.2, 0.4):
        try:
            os.replace(source, target)
            return
        except PermissionError:
            if target.exists() or not source.exists():
                raise
            time.sleep(delay_seconds)
    os.replace(source, target)


def _atomic_file_write(target: Path, content: bytes) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{target.stem}.tmp-",
        suffix=target.suffix,
        dir=target.parent,
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        temporary.write_bytes(content)
        try:
            os.link(temporary, target)
        except FileExistsError:
            if target.read_bytes() != content:
                raise MarketDataIntegrityError(
                    f"immutable snapshot collision for {target.stem}"
                )
    finally:
        temporary.unlink(missing_ok=True)


def _import_batch_locked(
    *,
    csv_path: Path,
    manifest: Mapping[str, object],
    root: Path,
) -> BatchRecord:
    declared = _validate_manifest_input(manifest)
    csv_bytes = csv_path.read_bytes()
    csv_sha256 = _sha256_bytes(csv_bytes)
    rows, securities = _read_csv(csv_path, declared["fields"])
    stored_manifest = {
        **declared,
        "csv": {
            "sha256": csv_sha256,
            "bytes": len(csv_bytes),
            "rows": len(rows),
        },
        "securities": securities,
    }
    validation = _validation_document()
    batch_id = _sha256_bytes(_canonical_bytes(_batch_identity(declared, csv_sha256)))
    batch_dir = Path(root) / "batches" / batch_id
    files = {
        "manifest.json": _json_file_bytes(stored_manifest),
        "market-data.csv": csv_bytes,
        "validation.json": _json_file_bytes(validation),
    }

    if batch_dir.exists():
        _assert_existing_batch_matches(batch_dir, files)
    else:
        _reject_conflicting_overlap(
            batches_dir=Path(root) / "batches",
            incoming_manifest=declared,
            incoming_rows=rows,
        )
        _atomic_directory_write(batch_dir, files)
    return BatchRecord(batch_id=batch_id, path=batch_dir, manifest=stored_manifest)


def import_batch(
    *,
    csv_path: Path,
    manifest: Mapping[str, object],
    root: Path,
) -> BatchRecord:
    with _exclusive_storage_lock(root):
        return _import_batch_locked(
            csv_path=csv_path,
            manifest=manifest,
            root=root,
        )


def _selection_rows(
    *,
    manifests: Sequence[tuple[str, Mapping[str, object]]],
    selection: SnapshotSelection,
    root: Path,
) -> list[dict[str, object]]:
    selected_securities = set(selection.securities)
    if not selected_securities or len(selected_securities) != len(selection.securities):
        raise MarketDataIntegrityError("snapshot securities must be non-empty and unique")
    if not selection.fields or len(set(selection.fields)) != len(selection.fields):
        raise MarketDataIntegrityError("snapshot fields must be non-empty and unique")
    try:
        start = date.fromisoformat(selection.start_date)
        end = date.fromisoformat(selection.end_date)
    except ValueError as exc:
        raise MarketDataIntegrityError("snapshot dates must use YYYY-MM-DD") from exc
    if start > end:
        raise MarketDataIntegrityError("snapshot start_date must not exceed end_date")

    selected_rows: dict[str, dict[str, dict[str, str]]] = {
        security: {} for security in selected_securities
    }
    for batch_id, manifest in manifests:
        source = manifest.get("source")
        if not isinstance(source, Mapping) or source != dict(selection.source):
            raise MarketDataIntegrityError("snapshot source does not match its batch")
        for field, expected in (
            ("asset_type", selection.asset_type),
            ("frequency", selection.frequency),
        ):
            if manifest.get(field) != expected:
                raise MarketDataIntegrityError(
                    f"snapshot {field} does not match its batch"
                )
        if manifest.get("price_semantics") != dict(selection.price_semantics):
            raise MarketDataIntegrityError(
                "snapshot price semantics do not match its batch"
            )
        manifest_fields = manifest.get("fields")
        if not isinstance(manifest_fields, list) or not set(selection.fields).issubset(
            manifest_fields
        ):
            raise MarketDataIntegrityError("snapshot fields are not covered")
        rows, _ = _read_csv(
            Path(root) / "batches" / batch_id / "market-data.csv",
            manifest_fields,
        )
        for row in rows:
            row_date = date.fromisoformat(row["date"])
            if row["security"] in selected_securities and start <= row_date <= end:
                existing = selected_rows[row["security"]].get(row["date"])
                if existing is not None and existing != row:
                    raise MarketDataConflict(
                        f"snapshot batches conflict at {row['security']} {row['date']}"
                    )
                selected_rows[row["security"]][row["date"]] = row
    missing = sorted(
        security for security, rows in selected_rows.items() if not rows
    )
    if missing:
        raise MarketDataIntegrityError(
            f"snapshot selection is missing securities: {', '.join(missing)}"
        )
    coverage: list[dict[str, object]] = []
    for security in sorted(selected_rows):
        dates = sorted(selected_rows[security])
        if dates[-1] != selection.end_date:
            raise MarketDataIntegrityError(
                f"snapshot end_date coverage is incomplete for {security}: "
                f"expected {selection.end_date}, found {dates[-1]}"
            )
        coverage.append(
            {
                "security": security,
                "start_date": dates[0],
                "end_date": dates[-1],
                "rows": len(dates),
            }
        )
    return coverage


def _create_snapshot_locked(
    *,
    batch_ids: Sequence[str],
    selection: SnapshotSelection,
    root: Path,
) -> SnapshotRecord:
    unique_batch_ids = sorted(set(batch_ids))
    if not unique_batch_ids or len(unique_batch_ids) != len(batch_ids):
        raise MarketDataIntegrityError("batch_ids must be non-empty and unique")
    manifests: list[tuple[str, Mapping[str, object]]] = []
    batch_evidence: list[dict[str, object]] = []
    for batch_id in unique_batch_ids:
        _require_identifier(batch_id, "batch")
        batch_dir = Path(root) / "batches" / batch_id
        manifest = _validate_batch_dir(batch_dir)
        manifests.append((batch_id, manifest))
        batch_evidence.append(
            {
                "batch_id": batch_id,
                "manifest_sha256": _sha256_path(batch_dir / "manifest.json"),
                "csv_sha256": _sha256_path(batch_dir / "market-data.csv"),
                "validation_sha256": _sha256_path(
                    batch_dir / "validation.json"
                ),
                "export_code_sha256": manifest["export_code_sha256"],
            }
        )
    coverage = _selection_rows(
        manifests=manifests,
        selection=selection,
        root=Path(root),
    )
    payload = {
        "schema_version": 1,
        "batch_ids": unique_batch_ids,
        "batches": batch_evidence,
        "selection": selection.to_document(),
        "coverage": coverage,
    }
    snapshot_id = _sha256_bytes(_canonical_bytes(payload))
    document = {**payload, "snapshot_id": snapshot_id}
    snapshot_path = Path(root) / "snapshots" / f"{snapshot_id}.json"
    _atomic_file_write(snapshot_path, _json_file_bytes(document))
    return SnapshotRecord(
        snapshot_id=snapshot_id,
        path=snapshot_path,
        document=document,
    )


def create_snapshot(
    *,
    batch_ids: Sequence[str],
    selection: SnapshotSelection,
    root: Path,
) -> SnapshotRecord:
    with _exclusive_storage_lock(root):
        return _create_snapshot_locked(
            batch_ids=batch_ids,
            selection=selection,
            root=root,
        )


def validate_snapshot(snapshot_id: str, *, root: Path) -> SnapshotRecord:
    _require_identifier(snapshot_id, "snapshot")
    snapshot_path = Path(root) / "snapshots" / f"{snapshot_id}.json"
    document = _load_json(snapshot_path)
    if document.get("snapshot_id") != snapshot_id:
        raise MarketDataIntegrityError("snapshot identity does not match its path")
    payload = {key: value for key, value in document.items() if key != "snapshot_id"}
    if _sha256_bytes(_canonical_bytes(payload)) != snapshot_id:
        raise MarketDataIntegrityError("snapshot identity digest mismatch")

    batch_ids = document.get("batch_ids")
    evidence_rows = document.get("batches")
    if not isinstance(batch_ids, list) or not isinstance(evidence_rows, list):
        raise MarketDataIntegrityError("snapshot batch evidence is missing")
    evidence_ids = [
        item.get("batch_id") if isinstance(item, Mapping) else None
        for item in evidence_rows
    ]
    if (
        batch_ids != sorted(set(batch_ids))
        or evidence_ids != batch_ids
        or len(evidence_ids) != len(set(evidence_ids))
    ):
        raise MarketDataIntegrityError(
            "snapshot canonical batch evidence is invalid"
        )
    evidence_by_id = {
        item.get("batch_id"): item
        for item in evidence_rows
        if isinstance(item, Mapping)
    }
    manifests: list[tuple[str, Mapping[str, object]]] = []
    for batch_id in batch_ids:
        if not isinstance(batch_id, str) or batch_id not in evidence_by_id:
            raise MarketDataIntegrityError("snapshot batch evidence is incomplete")
        _require_identifier(batch_id, "batch")
        batch_dir = Path(root) / "batches" / batch_id
        manifest = _validate_batch_dir(batch_dir)
        evidence = evidence_by_id[batch_id]
        if _sha256_path(batch_dir / "manifest.json") != evidence.get(
            "manifest_sha256"
        ):
            raise MarketDataIntegrityError(
                f"manifest SHA256 mismatch for batch {batch_id}"
            )
        if _sha256_path(batch_dir / "market-data.csv") != evidence.get("csv_sha256"):
            raise MarketDataIntegrityError(f"CSV SHA256 mismatch for batch {batch_id}")
        if _sha256_path(batch_dir / "validation.json") != evidence.get(
            "validation_sha256"
        ):
            raise MarketDataIntegrityError(
                f"validation SHA256 mismatch for batch {batch_id}"
            )
        if manifest.get("export_code_sha256") != evidence.get(
            "export_code_sha256"
        ):
            raise MarketDataIntegrityError(
                f"export code SHA256 mismatch for batch {batch_id}"
            )
        manifests.append((batch_id, manifest))

    selection_document = document.get("selection")
    if not isinstance(selection_document, Mapping):
        raise MarketDataIntegrityError("snapshot selection is missing")
    try:
        source_identity = selection_document["source"]
        if not isinstance(source_identity, Mapping):
            raise TypeError("source must be a mapping")
        selection = SnapshotSelection(
            source=source_identity,
            asset_type=str(selection_document["asset_type"]),
            frequency=str(selection_document["frequency"]),
            securities=selection_document["securities"],
            start_date=str(selection_document["start_date"]),
            end_date=str(selection_document["end_date"]),
            fields=selection_document["fields"],
            price_semantics=selection_document["price_semantics"],
        )
    except (KeyError, TypeError) as exc:
        raise MarketDataIntegrityError("snapshot selection is incomplete") from exc
    coverage = _selection_rows(
        manifests=manifests,
        selection=selection,
        root=Path(root),
    )
    if document.get("coverage") != coverage:
        raise MarketDataIntegrityError("snapshot coverage evidence is invalid")
    return SnapshotRecord(
        snapshot_id=snapshot_id,
        path=snapshot_path,
        document=document,
    )
