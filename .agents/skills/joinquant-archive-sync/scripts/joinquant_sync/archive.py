from __future__ import annotations

import ast
import hashlib
import gzip
import json
import msvcrt
import os
import re
import uuid
import zipfile
from collections import Counter
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from pathlib import PurePosixPath
from urllib.parse import parse_qs, urlsplit

import pyarrow.parquet as pq


STATES = {
    "complete",
    "capped_free",
    "missing_at_source",
    "unsupported_api_version",
    "failed",
}


class TargetRequired(ValueError):
    """Raised when a history sync does not name one exact page target."""


class IntegrityError(RuntimeError):
    """Raised when staged evidence cannot safely become the active manifest."""


class ObjectLocked(RuntimeError):
    """Raised when another sync owns the same object lock."""


class IdentityConflict(RuntimeError):
    """Raised when one page ordinal resolves to different immutable evidence."""


class AttributionIncomplete(RuntimeError):
    """Raised when an attribution stream is malformed or not closed."""


class SimulationIncrementError(RuntimeError):
    """Raised when a simulation increment lacks verifiable stream evidence."""


def detect_attribution_writer(code: str) -> dict[str, object]:
    writer_present = "def audit_event" in code and "write_file" in code
    if not writer_present:
        return {
            "writer_present": False,
            "path": "",
            "evidence": {"code_writer": False},
        }
    constants: dict[str, str] = {}
    try:
        tree = ast.parse(code)
    except SyntaxError:
        tree = ast.Module(body=[], type_ignores=[])
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ):
            constants[node.targets[0].id] = node.value.value
    token = next(
        (value for name, value in constants.items() if name.endswith("AUDIT_TOKEN")),
        "",
    )
    directory = next(
        (value for name, value in constants.items() if name.endswith("AUDIT_DIR")),
        "",
    )
    direct_path = next(
        (value for name, value in constants.items() if name.endswith("AUDIT_PATH")),
        "",
    )
    path = direct_path or (
        f"{directory.rstrip('/')}/{token}.jsonl" if token and directory else ""
    )
    parsed = PurePosixPath(path)
    if parsed.is_absolute() or ".." in parsed.parts:
        path = ""
    return {
        "writer_present": True,
        "path": path,
        "evidence": {"token": token, "directory": directory},
    }


@dataclass(frozen=True)
class DatasetPolicy:
    required: bool = True


def validate_history_target(
    strategy_id: str | None, target: str | None
) -> tuple[str, str]:
    strategy = (strategy_id or "").strip()
    selected = (target or "").strip()
    if not strategy or not selected or selected.lower() in {"latest", "all"}:
        raise TargetRequired("explicit strategy and page target required")
    if re.fullmatch(r"[1-9]\d*", selected):
        return strategy, selected
    parsed = urlsplit(selected)
    query = parse_qs(parsed.query)
    if (
        parsed.scheme == "https"
        and parsed.hostname in {"joinquant.com", "www.joinquant.com"}
        and parsed.path == "/algorithm/backtest/detail"
        and query.get("backtestId", [""])[0]
    ):
        return strategy, selected
    raise TargetRequired("target must be a page ordinal or JoinQuant detail URL")


def resolve_local_id(index_path: Path, kind: str, page_identity: dict[str, str]) -> str:
    if kind not in {"strategy", "simulation", "build", "backtest"}:
        raise ValueError(f"unsupported object kind: {kind}")
    ordinal = str(page_identity.get("page_ordinal") or "").strip()
    if not re.fullmatch(r"[1-9]\d*", ordinal):
        raise ValueError("page_ordinal must be a positive integer")
    stable_identity = {"page_ordinal": ordinal}
    if page_identity.get("strategy_id"):
        stable_identity["strategy_id"] = str(page_identity["strategy_id"])

    data = (
        json.loads(index_path.read_text(encoding="utf-8"))
        if index_path.is_file()
        else {"schema_version": 1, "objects": []}
    )
    objects = data.setdefault("objects", [])
    item = next(
        (
            candidate
            for candidate in objects
            if candidate.get("kind") == kind
            and candidate.get("identity") == stable_identity
        ),
        None,
    )
    if item is None:
        if kind in {"build", "backtest"}:
            local_id = ordinal
        else:
            numbers = [
                int(match.group(1))
                for candidate in objects
                if candidate.get("kind") == kind
                and (
                    match := re.fullmatch(
                        rf"{re.escape(kind)}-(\d+)",
                        str(candidate.get("local_id") or ""),
                    )
                )
            ]
            local_id = f"{kind}-{max(numbers, default=0) + 1:03d}"
        item = {
            "kind": kind,
            "local_id": local_id,
            "identity": stable_identity,
            "aliases": [],
        }
        if kind in {"build", "backtest"} and page_identity.get("fingerprint"):
            item["fingerprint"] = str(page_identity["fingerprint"])
        objects.append(item)

    if kind in {"build", "backtest"} and page_identity.get("fingerprint"):
        incoming_fingerprint = str(page_identity["fingerprint"])
        existing_fingerprint = str(item.get("fingerprint") or "")
        if existing_fingerprint and existing_fingerprint != incoming_fingerprint:
            raise IdentityConflict(f"page identity conflict: {kind}/{ordinal}")
        item["fingerprint"] = incoming_fingerprint

    alias = {
        key: str(page_identity[key])
        for key in ("remote_id", "url", "name")
        if page_identity.get(key)
    }
    if alias and alias not in item["aliases"]:
        item["aliases"].append(alias)
    index_path.parent.mkdir(parents=True, exist_ok=True)
    index_path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return str(item["local_id"])


def expected_datasets(
    kind: str, run_status: str, has_attribution_writer: bool
) -> dict[str, dict[str, object]]:
    if kind not in {"backtest", "simulation"}:
        raise ValueError(f"unsupported run kind: {kind}")
    policies = {
        name: DatasetPolicy()
        for name in (
            "results",
            "balances",
            "positions",
            "orders",
            "records",
            "risk",
            "period_risks",
            "official_summary",
        )
    }
    policies.update(
        normal_log=DatasetPolicy(required=False),
        performance_profile=DatasetPolicy(required=False),
        error_log=DatasetPolicy(required=run_status == "failed"),
        attribution_log=DatasetPolicy(required=has_attribution_writer),
    )
    datasets = {
        name: {"required": policy.required, "status": "failed"}
        for name, policy in policies.items()
    }
    if run_status in {"failed", "cancelled"}:
        for name in (
            "results",
            "balances",
            "positions",
            "orders",
            "records",
            "risk",
            "period_risks",
        ):
            datasets[name].update(status="complete", rows=0, verified_empty=True)
    if not has_attribution_writer:
        datasets["attribution_log"].update(
            status="missing_at_source", evidence={"code_writer": False}
        )
    if not datasets["error_log"]["required"]:
        datasets["error_log"].update(status="complete", rows=0, verified_empty=True)
    return datasets


def evaluate_gate(
    datasets: dict[str, dict[str, object]],
) -> dict[str, object]:
    failed = not datasets
    exceptions: list[str] = []
    for name, item in datasets.items():
        status = item.get("status")
        required = bool(item.get("required"))
        if status not in STATES or status == "failed":
            failed = True
            continue
        if status == "complete":
            verified_empty = (
                item.get("verified_empty") is True and item.get("rows") == 0
            )
            if not item.get("files") and not verified_empty:
                failed = True
            continue
        accepted = False
        if status == "capped_free":
            accepted = (
                name == "normal_log"
                and bool(item.get("pagination"))
                and bool(item.get("files"))
            )
        elif status in {"missing_at_source", "unsupported_api_version"}:
            accepted = not required and bool(item.get("evidence"))
        if required or not accepted:
            failed = True
        else:
            exceptions.append(f"{name}:{status}")
    return {"status": "fail" if failed else "pass", "exceptions": exceptions}


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_sha256(value: object) -> str:
    payload = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def write_raw_gzip(raw: bytes, destination: Path) -> dict[str, object]:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("wb") as output:
            with gzip.GzipFile(
                filename="", mode="wb", fileobj=output, compresslevel=9, mtime=0
            ) as compressed:
                compressed.write(raw)
            output.flush()
            os.fsync(output.fileno())
        compressed_sha256 = _file_sha256(temporary)
        if destination.exists():
            if _file_sha256(destination) != compressed_sha256:
                raise IntegrityError(f"immutable file conflict: {destination}")
        else:
            os.replace(temporary, destination)
        return {
            "path": str(destination),
            "bytes": destination.stat().st_size,
            "raw_bytes": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest(),
            "compressed_sha256": compressed_sha256,
        }
    finally:
        temporary.unlink(missing_ok=True)


@contextmanager
def object_lock(object_dir: Path) -> Iterator[None]:
    object_dir.mkdir(parents=True, exist_ok=True)
    lock_path = object_dir / ".sync.lock"
    with lock_path.open("a+b") as lock_file:
        lock_file.seek(0, os.SEEK_END)
        if lock_file.tell() == 0:
            lock_file.write(b"\0")
            lock_file.flush()
        lock_file.seek(0)
        try:
            msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as error:
            raise ObjectLocked(f"object_locked: {object_dir}") from error
        try:
            yield
        finally:
            lock_file.seek(0)
            msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)


def _manifest_references(manifest: dict[str, object]) -> dict[str, str]:
    references: dict[str, str] = {}
    code = manifest.get("code")
    if isinstance(code, dict) and code.get("path") and code.get("sha256"):
        references[str(code["path"])] = str(code["sha256"])
        for item in [
            code.get("params"),
            code.get("source"),
            code.get("history"),
            code.get("source_response"),
            *(code.get("versions") or []),
        ]:
            if (
                not isinstance(item, dict)
                or not item.get("path")
                or not item.get("sha256")
            ):
                continue
            references[str(item["path"])] = str(item["sha256"])
    research_response = manifest.get("research_response")
    if isinstance(research_response, dict):
        if not research_response.get("path") or not research_response.get("sha256"):
            raise IntegrityError("research response reference requires path and sha256")
        references[str(research_response["path"])] = str(research_response["sha256"])
    for item in manifest.get("research_lineage") or []:
        if not isinstance(item, dict) or not item.get("path") or not item.get("sha256"):
            raise IntegrityError("research lineage reference requires path and sha256")
        references[str(item["path"])] = str(item["sha256"])
    datasets = manifest.get("datasets")
    if isinstance(datasets, dict):
        for dataset in datasets.values():
            if not isinstance(dataset, dict):
                continue
            for item in dataset.get("files") or []:
                if (
                    not isinstance(item, dict)
                    or not item.get("path")
                    or not item.get("sha256")
                ):
                    raise IntegrityError(
                        "manifest file reference requires path and sha256"
                    )
                path = str(item["path"])
                digest = str(item["sha256"])
                if path in references and references[path] != digest:
                    raise IntegrityError(f"conflicting manifest reference: {path}")
                references[path] = digest
    return references


def _object_path(object_dir: Path, relative: str) -> Path:
    path = Path(relative)
    if path.is_absolute() or ".." in path.parts:
        raise IntegrityError(f"unsafe manifest path: {relative}")
    destination = (object_dir / path).resolve()
    try:
        destination.relative_to(object_dir.resolve())
    except ValueError:
        raise IntegrityError(f"unsafe manifest path: {relative}") from None
    return destination


def _staged_candidates(
    staged_files: list[Path],
    used: set[Path],
    relative: str,
    *,
    allow_basename: bool,
) -> list[Path]:
    parts = Path(relative).parts
    exact = [
        path
        for path in staged_files
        if path not in used
        and len(path.parts) >= len(parts)
        and path.parts[-len(parts) :] == parts
    ]
    if exact or not allow_basename:
        return exact
    return [
        path
        for path in staged_files
        if path not in used and path.name == Path(relative).name
    ]


def _verify_manifest_document(
    object_dir: Path,
    manifest: dict[str, object],
    *,
    verify_pointers: bool = True,
    validate_contract: bool = True,
) -> None:
    if validate_contract:
        _validate_manifest_contract(manifest)
    if manifest.get("schema_version") != 1:
        raise IntegrityError("unsupported manifest schema")
    gate = manifest.get("gate")
    if not isinstance(gate, dict) or gate.get("status") != "pass":
        raise IntegrityError("manifest gate did not pass")
    datasets = manifest.get("datasets")
    if not isinstance(datasets, dict) or evaluate_gate(datasets)["status"] != "pass":
        raise IntegrityError("dataset gate did not pass")
    if gate != evaluate_gate(datasets):
        raise IntegrityError("manifest gate does not match dataset evidence")
    for relative, expected_sha256 in _manifest_references(manifest).items():
        path = _object_path(object_dir, relative)
        if not path.is_file():
            raise IntegrityError(f"missing manifest file: {relative}")
        if _file_sha256(path) != expected_sha256:
            raise IntegrityError(f"manifest file hash mismatch: {relative}")
    if verify_pointers:
        _verify_convenience_pointers(object_dir, manifest)
    if validate_contract:
        _verify_run_contents(object_dir, manifest)


def _read_json_gzip(path: Path) -> object:
    try:
        with gzip.open(path, "rt", encoding="utf-8") as stream:
            return json.load(stream)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise IntegrityError(
            f"invalid compressed JSON evidence: {path.name}"
        ) from error


def _raw_fact_rows(value: object, dataset: str) -> list[dict[str, object]]:
    if dataset == "risk" and isinstance(value, dict):
        value = [value] if value else []
    elif dataset == "period_risks" and isinstance(value, dict):
        value = [
            {
                "metric": str(key),
                "payload_json": json.dumps(item, ensure_ascii=False, sort_keys=True),
            }
            for key, item in value.items()
        ]
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise IntegrityError(f"raw dataset has invalid row shape: {dataset}")
    return [
        {
            str(key): (
                json.dumps(cell, ensure_ascii=False, sort_keys=True)
                if isinstance(cell, (dict, list, tuple))
                else cell
            )
            for key, cell in row.items()
        }
        for row in value
    ]


def _verify_fact_dataset(
    object_dir: Path, name: str, dataset: dict[str, object]
) -> list[dict[str, object]]:
    files = dataset.get("files")
    if not isinstance(files, list):
        raise IntegrityError(f"dataset file list is invalid: {name}")
    raw_items = [
        item
        for item in files
        if isinstance(item, dict) and item.get("format") == "json.gz"
    ]
    parquet_items = [
        item
        for item in files
        if isinstance(item, dict) and item.get("format") == "parquet"
    ]
    if len(raw_items) != 1:
        raise IntegrityError(f"dataset raw evidence is incomplete: {name}")
    raw_rows = _raw_fact_rows(
        _read_json_gzip(_object_path(object_dir, str(raw_items[0]["path"]))), name
    )
    expected_rows = int(dataset.get("rows") or 0)
    if len(raw_rows) != expected_rows:
        raise IntegrityError(f"dataset raw row count mismatch: {name}")
    actual_rows = 0
    actual_fields: set[str] = set()
    parquet_rows: list[dict[str, object]] = []
    for item in parquet_items:
        table = pq.read_table(_object_path(object_dir, str(item["path"])))
        actual_rows += table.num_rows
        actual_fields.update(table.column_names)
        parquet_rows.extend(table.to_pylist())
    if actual_rows != expected_rows:
        raise IntegrityError(f"dataset parquet row count mismatch: {name}")
    if expected_rows and not parquet_items:
        raise IntegrityError(f"dataset parquet evidence is missing: {name}")
    if json.dumps(
        raw_rows, ensure_ascii=False, sort_keys=True, default=str
    ) != json.dumps(parquet_rows, ensure_ascii=False, sort_keys=True, default=str):
        raise IntegrityError(f"dataset raw and parquet contents differ: {name}")
    evidence = dataset.get("evidence")
    if not isinstance(evidence, dict):
        raise IntegrityError(f"dataset evidence is missing: {name}")
    declared_fields = evidence.get("fields")
    if not isinstance(declared_fields, list) or set(declared_fields) != actual_fields:
        raise IntegrityError(f"dataset field evidence mismatch: {name}")
    unique_key = evidence.get("unique_key")
    if (
        evidence.get("unique") is True
        and expected_rows
        and name in {"results", "balances", "positions", "records", "period_risks"}
    ):
        if (
            not isinstance(unique_key, list)
            or not unique_key
            or not set(unique_key) <= actual_fields
        ):
            raise IntegrityError(f"dataset unique-key evidence mismatch: {name}")
        keys = [tuple(row.get(field) for field in unique_key) for row in parquet_rows]
        if len(keys) != len(set(keys)):
            raise IntegrityError(f"dataset business key is duplicated: {name}")
    time_values = [
        str(row["time"]) for row in parquet_rows if row.get("time") not in {None, ""}
    ]
    if time_values != sorted(time_values):
        raise IntegrityError(f"dataset time order mismatch: {name}")
    actual_range = (
        {"start": min(time_values), "end": max(time_values)} if time_values else None
    )
    if dataset.get("time_range") != actual_range:
        raise IntegrityError(f"dataset time range mismatch: {name}")
    pagination = dataset.get("pagination")
    if (
        not isinstance(pagination, dict)
        or pagination.get("terminal") is not True
        or pagination.get("source") != f"get_backtest.{name}"
    ):
        raise IntegrityError(f"dataset pagination evidence mismatch: {name}")
    return parquet_rows


def _verify_research_response(
    object_dir: Path,
    manifest: dict[str, object],
    fact_rows: dict[str, list[dict[str, object]]],
) -> None:
    record = manifest.get("research_response")
    if not isinstance(record, dict):
        raise IntegrityError("research response evidence is missing")
    payload = _read_json_gzip(_object_path(object_dir, str(record.get("path") or "")))
    if not isinstance(payload, dict):
        raise IntegrityError("research response root is invalid")
    for name in ("params", "status"):
        value = payload.get(name)
        if name not in payload or (isinstance(value, dict) and value.get("__error__")):
            raise IntegrityError(f"research response {name} is invalid")
    metadata = payload.get("metadata")
    if (
        not isinstance(metadata, dict)
        or metadata.get("schema_version") != 1
        or metadata.get("extraction_method") != "joinquant_research_get_backtest"
        or not isinstance(metadata.get("incremental_after"), dict)
        or not isinstance(metadata.get("transfer_modes"), dict)
    ):
        raise IntegrityError("research response metadata is invalid")
    after = metadata["incremental_after"]
    modes = metadata["transfer_modes"]
    response_sha256 = str(record.get("sha256") or "")
    lineage = manifest.get("research_lineage")
    if (
        not isinstance(lineage, list)
        or not lineage
        or not isinstance(lineage[-1], dict)
        or lineage[-1].get("path") != record.get("path")
        or lineage[-1].get("sha256") != record.get("sha256")
    ):
        raise IntegrityError("research response lineage is invalid")
    lineage_coverage = {
        name: Counter()
        for name in ("results", "positions", "orders", "records", "balances")
    }
    for lineage_record in lineage:
        if not isinstance(lineage_record, dict):
            raise IntegrityError("research response lineage record is invalid")
        lineage_payload = _read_json_gzip(
            _object_path(object_dir, str(lineage_record.get("path") or ""))
        )
        lineage_metadata = (
            lineage_payload.get("metadata")
            if isinstance(lineage_payload, dict)
            else None
        )
        if (
            not isinstance(lineage_metadata, dict)
            or lineage_metadata.get("schema_version") != 1
            or lineage_metadata.get("extraction_method")
            != "joinquant_research_get_backtest"
        ):
            raise IntegrityError("research response lineage metadata is invalid")
        for name, coverage in lineage_coverage.items():
            source_rows = _raw_fact_rows(lineage_payload.get(name), name)
            counts = Counter(
                json.dumps(row, ensure_ascii=False, sort_keys=True, default=str)
                for row in source_rows
            )
            for key, count in counts.items():
                coverage[key] = max(coverage[key], count)
    for name, archived_rows in fact_rows.items():
        if name not in payload:
            raise IntegrityError(f"research response dataset is missing: {name}")
        source_rows = _raw_fact_rows(payload[name], name)
        pagination = manifest["datasets"][name].get("pagination")
        if (
            not isinstance(pagination, dict)
            or pagination.get("research_response_sha256") != response_sha256
        ):
            raise IntegrityError(f"dataset research response link mismatch: {name}")
        source_counts = Counter(
            json.dumps(row, ensure_ascii=False, sort_keys=True, default=str)
            for row in source_rows
        )
        archived_counts = Counter(
            json.dumps(row, ensure_ascii=False, sort_keys=True, default=str)
            for row in archived_rows
        )
        if name in lineage_coverage and any(
            archived_counts[key] > lineage_coverage[name][key]
            for key in archived_counts
        ):
            raise IntegrityError(f"research lineage does not cover archive: {name}")
        if any(source_counts[key] > archived_counts[key] for key in source_counts):
            raise IntegrityError(f"research response rows are not archived: {name}")
        if name in {"results", "positions", "orders", "records", "balances"}:
            mode = modes.get(name)
            if mode not in {"full", "after_time_overlap", "full_no_time_contract"}:
                raise IntegrityError(f"research transfer mode is invalid: {name}")
            if not after.get(name) and mode != "full":
                raise IntegrityError(
                    f"research full transfer evidence is invalid: {name}"
                )
            if (
                mode in {"full", "full_no_time_contract"}
                and source_counts != archived_counts
            ):
                raise IntegrityError(f"research full response is incomplete: {name}")
            if mode == "after_time_overlap":
                cursor = str(after.get(name) or "")
                times = [
                    str(row.get("time") or "")
                    for row in source_rows
                    if row.get("time") not in {None, ""}
                ]
                if not cursor or not times or min(times) != cursor:
                    raise IntegrityError(
                        f"research overlap evidence is incomplete: {name}"
                    )
        elif source_counts != archived_counts:
            raise IntegrityError(f"research summary response is incomplete: {name}")


def _verify_attribution_dataset(
    object_dir: Path,
    manifest: dict[str, object],
    dataset: dict[str, object],
    writer: dict[str, object],
) -> None:
    writer_present = bool(writer.get("writer_present"))
    if dataset.get("required") is not writer_present:
        raise IntegrityError("attribution requirement does not match archived code")
    if not writer_present:
        if dataset.get("status") != "missing_at_source":
            raise IntegrityError("missing attribution writer evidence is inconsistent")
        return
    code = manifest["code"]
    params_record = code.get("params") if isinstance(code, dict) else None
    if not isinstance(params_record, dict):
        raise IntegrityError("attribution parameters are missing")
    params_path = _object_path(object_dir, str(params_record.get("path") or ""))
    try:
        params = json.loads(params_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise IntegrityError("attribution parameters are invalid") from error
    if not isinstance(params, dict):
        raise IntegrityError("attribution parameters are invalid")
    files = dataset.get("files")
    if not isinstance(files, list):
        raise IntegrityError("attribution files are missing")
    raw_items = [
        item
        for item in files
        if isinstance(item, dict) and item.get("format") == "jsonl.gz"
    ]
    parquet_items = [
        item
        for item in files
        if isinstance(item, dict) and item.get("format") == "parquet"
    ]
    if len(raw_items) != 1 or not parquet_items:
        raise IntegrityError("attribution raw or parquet evidence is incomplete")
    raw_path = _object_path(object_dir, str(raw_items[0]["path"]))
    try:
        with gzip.open(raw_path, "rb") as stream:
            raw = stream.read()
    except OSError as error:
        raise IntegrityError("attribution raw evidence is invalid") from error
    status = str((manifest.get("object") or {}).get("status") or "active")
    expected_token = str((writer.get("evidence") or {}).get("token") or "")
    checked = validate_attribution(
        raw.splitlines(),
        "done" if status == "closed" else status,
        True,
        expected_token=expected_token,
        expected_path=str(writer.get("path") or ""),
        expected_start=str(params.get("start_date") or ""),
        expected_end=str(params.get("end_date") or ""),
    )
    raw_rows = [json.loads(line) for line in raw.splitlines() if line.strip()]
    parquet_rows: list[dict[str, object]] = []
    for item in parquet_items:
        parquet_rows.extend(
            pq.read_table(_object_path(object_dir, str(item["path"]))).to_pylist()
        )
    if len(parquet_rows) != len(raw_rows) or len(parquet_rows) != dataset.get("rows"):
        raise IntegrityError("attribution row count mismatch")
    normalized_raw = _raw_fact_rows(raw_rows, "attribution_log")
    if json.dumps(
        normalized_raw, ensure_ascii=False, sort_keys=True, default=str
    ) != json.dumps(parquet_rows, ensure_ascii=False, sort_keys=True, default=str):
        raise IntegrityError("attribution raw and parquet contents differ")
    evidence = dataset.get("evidence")
    if not isinstance(evidence, dict) or evidence != checked:
        raise IntegrityError("attribution identity evidence mismatch")


def _verify_normal_log(object_dir: Path, dataset: dict[str, object]) -> None:
    files = dataset.get("files")
    if not isinstance(files, list):
        raise IntegrityError("normal log files are missing")
    raw_items = [
        item
        for item in files
        if isinstance(item, dict)
        and item.get("format") == "jsonl.gz"
        and "normal-log" in PurePosixPath(str(item.get("path") or "")).name
    ]
    if len(raw_items) != 1:
        raise IntegrityError("normal log raw evidence is incomplete")
    page_items = [
        item
        for item in files
        if isinstance(item, dict)
        and item.get("format") == "json.gz"
        and "normal-log-pages" in PurePosixPath(str(item.get("path") or "")).name
    ]
    if len(page_items) != 1:
        raise IntegrityError("normal log raw page evidence is incomplete")
    try:
        with gzip.open(
            _object_path(object_dir, str(raw_items[0]["path"])),
            "rt",
            encoding="utf-8",
        ) as stream:
            rows = [json.loads(line) for line in stream if line.strip()]
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise IntegrityError("normal log JSONL is invalid") from error
    if not all(
        isinstance(row, dict) and type(row.get("offset")) is int for row in rows
    ):
        raise IntegrityError("normal log offsets are invalid")
    offsets = [int(row["offset"]) for row in rows]
    if offsets != sorted(offsets) or len(offsets) != len(set(offsets)):
        raise IntegrityError("normal log offsets are unordered or duplicated")
    if offsets and offsets != list(range(offsets[0], offsets[-1] + 1)):
        raise IntegrityError("normal log offsets are not contiguous")
    if len(rows) != int(dataset.get("rows") or 0):
        raise IntegrityError("normal log row count mismatch")
    status = dataset.get("status")
    if status == "complete" and offsets and offsets[0] != 0:
        raise IntegrityError("complete normal log does not start at offset zero")
    if status == "capped_free" and len(rows) < 1000:
        raise IntegrityError("capped normal log is below the free 1000-row boundary")
    if status not in {"complete", "capped_free"}:
        raise IntegrityError("normal log status is invalid")
    raw_pages = _read_json_gzip(_object_path(object_dir, str(page_items[0]["path"])))
    if not isinstance(raw_pages, list) or not raw_pages:
        raise IntegrityError("normal log raw pages are invalid")
    blocked = False
    covered_offsets: set[int] = set()
    cursors: list[int] = []
    archived_by_offset = {
        int(row["offset"]): str(row.get("text") or "") for row in rows
    }
    for page in raw_pages:
        if not isinstance(page, dict):
            raise IntegrityError("normal log raw page is invalid")
        cursor = page.get("offset", page.get("cursor"))
        if type(cursor) is not int:
            raise IntegrityError("normal log raw page cursor is invalid")
        cursors.append(cursor)
        if page.get("blocked_free") is True:
            blocked = True
            continue
        response = page.get("response")
        raw_text = page.get("raw_text")
        if isinstance(raw_text, str) and raw_text:
            try:
                raw_response = json.loads(raw_text)
            except json.JSONDecodeError as error:
                raise IntegrityError(
                    "normal log original response is invalid JSON"
                ) from error
            if raw_response != response:
                raise IntegrityError(
                    "normal log original response differs from parsed page"
                )
        data = response.get("data") if isinstance(response, dict) else None
        lines = data.get("logArr") if isinstance(data, dict) else None
        if not isinstance(lines, list):
            raise IntegrityError("normal log raw page rows are invalid")
        for index, line in enumerate(lines):
            offset = cursor + index
            if archived_by_offset.get(offset) != str(line):
                raise IntegrityError("normal log raw pages differ from archived rows")
            covered_offsets.add(offset)
    if covered_offsets != set(archived_by_offset):
        raise IntegrityError("normal log raw pages do not match archived rows")
    pagination = dataset.get("pagination")
    if not isinstance(pagination, dict):
        raise IntegrityError("normal log pagination evidence is missing")
    page_summary = pagination.get("pages")
    if isinstance(page_summary, int):
        page_count_matches = page_summary == len(raw_pages)
    elif isinstance(page_summary, list):
        page_count_matches = len(page_summary) == len(raw_pages)
    else:
        page_count_matches = False
    if (
        not page_count_matches
        or pagination.get("cumulative_rows") != len(rows)
        or pagination.get("terminal") is not (status == "complete")
        or pagination.get("capped") is not (status == "capped_free")
        or (status == "complete" and (blocked or min(cursors) != 0))
        or (status == "capped_free" and not blocked)
    ):
        raise IntegrityError("normal log pagination evidence mismatch")


def _verify_run_contents(object_dir: Path, manifest: dict[str, object]) -> None:
    object_state = manifest.get("object")
    if not isinstance(object_state, dict) or object_state.get("kind") not in {
        "backtest",
        "simulation",
    }:
        return
    code = manifest.get("code")
    if not isinstance(code, dict):
        raise IntegrityError("run code record is invalid")
    code_path = _object_path(object_dir, str(code.get("path") or ""))
    try:
        code_text = code_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        raise IntegrityError("run code is not valid UTF-8") from error
    writer = detect_attribution_writer(code_text)
    datasets = manifest.get("datasets")
    if not isinstance(datasets, dict):
        raise IntegrityError("run datasets are missing")
    fact_rows: dict[str, list[dict[str, object]]] = {}
    raw_values: dict[str, object] = {}
    for name in (
        "results",
        "balances",
        "positions",
        "orders",
        "records",
        "risk",
        "period_risks",
    ):
        dataset = datasets.get(name)
        if not isinstance(dataset, dict) or dataset.get("status") != "complete":
            raise IntegrityError(f"structured dataset is incomplete: {name}")
        fact_rows[name] = _verify_fact_dataset(object_dir, name, dataset)
        raw_records = [
            item
            for item in dataset.get("files") or []
            if isinstance(item, dict) and item.get("format") == "json.gz"
        ]
        if len(raw_records) != 1:
            raise IntegrityError(f"dataset raw evidence is incomplete: {name}")
        raw_values[name] = _read_json_gzip(
            _object_path(object_dir, str(raw_records[0]["path"]))
        )
    _verify_research_response(object_dir, manifest, fact_rows)
    if object_state.get("kind") == "simulation":
        streams = manifest.get("streams")
        data_stream = streams.get("data") if isinstance(streams, dict) else None
        code_stream = streams.get("code") if isinstance(streams, dict) else None
        log_stream = streams.get("logs") if isinstance(streams, dict) else None
        if not isinstance(data_stream, dict) or data_stream.get(
            "sha256"
        ) != _canonical_sha256(raw_values):
            raise IntegrityError("simulation data stream digest mismatch")
        versions = code.get("versions") or []
        code_digest = _canonical_sha256(
            {
                "current": code_text,
                "version_sha256": sorted(
                    str(item.get("sha256"))
                    for item in versions
                    if isinstance(item, dict) and item.get("sha256")
                ),
            }
        )
        if (
            not isinstance(code_stream, dict)
            or code_stream.get("sha256") != code_digest
        ):
            raise IntegrityError("simulation code stream digest mismatch")
        normal = datasets.get("normal_log")
        log_files = normal.get("files") if isinstance(normal, dict) else None
        log_records = [
            item
            for item in log_files or []
            if isinstance(item, dict)
            and item.get("format") == "jsonl.gz"
            and "normal-log" in PurePosixPath(str(item.get("path") or "")).name
        ]
        if len(log_records) != 1:
            raise IntegrityError("simulation log stream evidence is incomplete")
        with gzip.open(
            _object_path(object_dir, str(log_records[0]["path"])), "rb"
        ) as stream:
            log_digest = hashlib.sha256(stream.read()).hexdigest()
        if not isinstance(log_stream, dict) or log_stream.get("sha256") != log_digest:
            raise IntegrityError("simulation log stream digest mismatch")
    params_record = code.get("params")
    if not isinstance(params_record, dict):
        raise IntegrityError("run parameters are missing")
    try:
        params = json.loads(
            _object_path(object_dir, str(params_record.get("path") or "")).read_text(
                encoding="utf-8"
            )
        )
    except (OSError, json.JSONDecodeError) as error:
        raise IntegrityError("run parameters are invalid") from error
    if not isinstance(params, dict):
        raise IntegrityError("run parameters are invalid")
    result_dates = {
        str(row["time"])[:10]
        for row in fact_rows["results"]
        if row.get("time") not in {None, ""}
    }
    start = str(params.get("start_date") or "")[:10]
    end = str(params.get("end_date") or "")[:10]
    for name, rows in fact_rows.items():
        dates = {
            str(row["time"])[:10] for row in rows if row.get("time") not in {None, ""}
        }
        if dates and start and min(dates) < start:
            raise IntegrityError(f"dataset starts before configured range: {name}")
        if dates and end and max(dates) > end:
            raise IntegrityError(f"dataset ends after configured range: {name}")
        if name != "results" and dates and result_dates and not dates <= result_dates:
            raise IntegrityError(f"dataset trading dates do not match results: {name}")
    attribution = datasets.get("attribution_log")
    if not isinstance(attribution, dict):
        raise IntegrityError("attribution dataset is missing")
    _verify_attribution_dataset(object_dir, manifest, attribution, writer)
    normal_log = datasets.get("normal_log")
    if not isinstance(normal_log, dict):
        raise IntegrityError("normal log dataset is missing")
    _verify_normal_log(object_dir, normal_log)


def _validate_manifest_contract(manifest: dict[str, object]) -> None:
    required = (
        "schema_version",
        "object",
        "source",
        "fence",
        "code",
        "datasets",
        "gate",
    )
    for name in required:
        if name not in manifest:
            raise IntegrityError(f"manifest required field is missing: {name}")
    if manifest.get("schema_version") != 1:
        raise IntegrityError("unsupported manifest schema")
    object_state = manifest.get("object")
    if not isinstance(object_state, dict):
        raise IntegrityError("manifest object is invalid")
    kind = object_state.get("kind")
    status = object_state.get("status")
    if kind not in {"strategy", "backtest", "simulation"}:
        raise IntegrityError("manifest object kind is invalid")
    if (
        not isinstance(object_state.get("local_id"), str)
        or not object_state["local_id"]
    ):
        raise IntegrityError("manifest object local_id is invalid")
    if not isinstance(status, str) or not status:
        raise IntegrityError("manifest object status is invalid")
    source = manifest.get("source")
    if (
        not isinstance(source, dict)
        or not isinstance(source.get("url"), str)
        or not isinstance(source.get("aliases"), list)
        or not isinstance(source.get("observed_at"), str)
    ):
        raise IntegrityError("manifest source is invalid")
    fence = manifest.get("fence")
    if not isinstance(fence, dict) or any(
        not isinstance(fence.get(name), str)
        or not re.fullmatch(r"[0-9a-f]{64}", str(fence.get(name)))
        for name in ("before_sha256", "after_sha256")
    ):
        raise IntegrityError("manifest fence is invalid")
    code = manifest.get("code")
    if (
        not isinstance(code, dict)
        or not isinstance(code.get("path"), str)
        or not re.fullmatch(r"[0-9a-f]{64}", str(code.get("sha256") or ""))
    ):
        raise IntegrityError("manifest code is invalid")
    if kind in {"backtest", "simulation"} and not isinstance(code.get("params"), dict):
        raise IntegrityError("manifest code parameters are missing")
    if kind in {"backtest", "simulation"}:
        research_response = manifest.get("research_response")
        if (
            not isinstance(research_response, dict)
            or not isinstance(research_response.get("path"), str)
            or not re.fullmatch(
                r"[0-9a-f]{64}", str(research_response.get("sha256") or "")
            )
            or research_response.get("format") != "json.gz"
        ):
            raise IntegrityError("research response evidence is invalid")
        research_lineage = manifest.get("research_lineage")
        if not isinstance(research_lineage, list) or not research_lineage:
            raise IntegrityError("research response lineage is missing")
        collection_fence = manifest.get("collection_fence")
        if (
            not isinstance(collection_fence, dict)
            or any(
                not re.fullmatch(r"[0-9a-f]{64}", str(collection_fence.get(name) or ""))
                for name in (
                    "collection_before_sha256",
                    "collection_after_sha256",
                )
            )
            or collection_fence.get("collection_before_sha256")
            != collection_fence.get("collection_after_sha256")
        ):
            raise IntegrityError("collection fence is unstable")
    if kind == "simulation":
        if not isinstance(code.get("source"), dict) or not isinstance(
            code.get("versions"), list
        ):
            raise IntegrityError("simulation code history is missing")
        if manifest.get("tracking") not in {"active", "stopped"}:
            raise IntegrityError("simulation tracking state is invalid")
        streams = manifest.get("streams")
        if not isinstance(streams, dict) or set(streams) != set(SIMULATION_STREAMS):
            raise IntegrityError("simulation streams are incomplete")
        _verified_simulation_streams(manifest)
        if status == "closed" and (
            manifest.get("tracking") != "stopped"
            or manifest.get("final_sync") != "complete"
        ):
            raise IntegrityError("closed simulation final sync is incomplete")
    datasets = manifest.get("datasets")
    if not isinstance(datasets, dict) or not datasets:
        raise IntegrityError("manifest datasets are missing")
    for name, dataset in datasets.items():
        if not isinstance(dataset, dict) or not isinstance(
            dataset.get("required"), bool
        ):
            raise IntegrityError(f"manifest dataset is invalid: {name}")
        if dataset.get("status") not in STATES:
            raise IntegrityError(f"manifest dataset status is invalid: {name}")
    if kind == "strategy":
        if set(datasets) != {"page_metadata"}:
            raise IntegrityError("strategy manifest datasets are invalid")
    elif kind in {"backtest", "simulation"}:
        attribution = datasets.get("attribution_log")
        has_writer = bool(isinstance(attribution, dict) and attribution.get("required"))
        policy_status = "done" if status == "closed" else str(status)
        expected = expected_datasets(str(kind), policy_status, has_writer)
        if set(datasets) != set(expected):
            raise IntegrityError("manifest expected datasets are incomplete")
        for name, policy in expected.items():
            if datasets[name].get("required") != policy.get("required"):
                raise IntegrityError(f"manifest dataset requirement drift: {name}")
        if status in {"done", "active", "closed"}:
            for name in ("results", "balances", "risk"):
                if int(datasets[name].get("rows") or 0) < 1:
                    raise IntegrityError(f"manifest core dataset is empty: {name}")
            for name in (
                "results",
                "balances",
                "positions",
                "orders",
                "records",
                "risk",
                "period_risks",
            ):
                pagination = datasets[name].get("pagination")
                if (
                    not isinstance(pagination, dict)
                    or pagination.get("terminal") is not True
                ):
                    raise IntegrityError(
                        f"manifest dataset lacks terminal evidence: {name}"
                    )
        if (
            kind == "backtest"
            and status == "done"
            and (
                datasets["official_summary"].get("rows")
                != datasets["results"].get("rows")
            )
        ):
            raise IntegrityError("official summary row count drift")


def _verify_convenience_pointers(object_dir: Path, manifest: dict[str, object]) -> None:
    object_state = manifest["object"]
    code = manifest["code"]
    pointers: list[tuple[str, dict[str, object]]] = []
    if object_state["kind"] == "strategy":
        pointers.append(("default_code.py", code))
    elif object_state["kind"] == "backtest":
        if isinstance(code.get("params"), dict):
            pointers.append(("params.json", code["params"]))
    elif object_state["kind"] == "simulation":
        pointers.append(("current_code.py", code))
        if isinstance(code.get("params"), dict):
            pointers.append(("params.json", code["params"]))
        if isinstance(code.get("source"), dict):
            pointers.append(("source.json", code["source"]))
    for relative, record in pointers:
        path = object_dir / relative
        if not path.is_file() or _file_sha256(path) != record.get("sha256"):
            raise IntegrityError(f"manifest convenience pointer mismatch: {relative}")


def commit_manifest(
    object_dir: Path,
    manifest: dict[str, object],
    staged_files: list[Path],
) -> None:
    gate = manifest.get("gate")
    if not isinstance(gate, dict) or gate.get("status") != "pass":
        raise IntegrityError("refusing to commit failed manifest")
    _validate_manifest_contract(manifest)
    references = _manifest_references(manifest)
    basename_counts = Counter(Path(relative).name for relative in references)
    with object_lock(object_dir):
        planned: list[tuple[Path, Path]] = []
        redundant: list[Path] = []
        used: set[Path] = set()
        for relative, expected_sha256 in references.items():
            destination = _object_path(object_dir, relative)
            if destination.is_file():
                if _file_sha256(destination) != expected_sha256:
                    raise IntegrityError(f"immutable file conflict: {relative}")
                candidates = _staged_candidates(
                    staged_files,
                    used,
                    relative,
                    allow_basename=basename_counts[Path(relative).name] == 1,
                )
                if candidates:
                    if (
                        len(candidates) != 1
                        or _file_sha256(candidates[0]) != expected_sha256
                    ):
                        raise IntegrityError(f"staged file hash mismatch: {relative}")
                    used.add(candidates[0])
                    redundant.append(candidates[0])
                continue
            candidates = _staged_candidates(
                staged_files,
                used,
                relative,
                allow_basename=basename_counts[Path(relative).name] == 1,
            )
            if len(candidates) != 1:
                raise IntegrityError(f"missing or ambiguous staged file: {relative}")
            staged = candidates[0]
            if not staged.is_file() or _file_sha256(staged) != expected_sha256:
                raise IntegrityError(f"staged file hash mismatch: {relative}")
            used.add(staged)
            planned.append((staged, destination))
        if set(staged_files) != used:
            raise IntegrityError("staged file is not referenced by manifest")

        for staged, destination in planned:
            destination.parent.mkdir(parents=True, exist_ok=True)
            os.replace(staged, destination)
        for staged in redundant:
            staged.unlink()
        _verify_manifest_document(object_dir, manifest, verify_pointers=False)

        manifest_path = object_dir / "manifest.json"
        temporary = object_dir / f"manifest.json.{uuid.uuid4().hex}.tmp"
        try:
            with temporary.open("w", encoding="utf-8", newline="\n") as stream:
                json.dump(manifest, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, manifest_path)
        finally:
            temporary.unlink(missing_ok=True)


def verify_existing_manifest(object_dir: Path) -> dict[str, object]:
    manifest_path = object_dir / "manifest.json"
    if not manifest_path.is_file():
        raise IntegrityError(f"missing manifest: {manifest_path}")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise IntegrityError(f"invalid manifest: {error.msg}") from error
    if not isinstance(manifest, dict):
        raise IntegrityError("manifest root must be an object")
    _verify_manifest_document(object_dir, manifest)
    return manifest


def verify_existing_manifest_files(object_dir: Path) -> dict[str, object]:
    manifest_path = object_dir / "manifest.json"
    if not manifest_path.is_file():
        raise IntegrityError(f"missing manifest: {manifest_path}")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise IntegrityError(f"invalid manifest: {error.msg}") from error
    if not isinstance(manifest, dict):
        raise IntegrityError("manifest root must be an object")
    _verify_manifest_document(object_dir, manifest, validate_contract=False)
    return manifest


def _write_staged_bytes(destination: Path, payload: bytes) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def write_code_context(
    stage_dir: Path,
    kind: str,
    code: str,
    params: dict[str, object],
    *,
    source_backtest: str | None = None,
    versions: list[str] | None = None,
) -> dict[str, object]:
    filenames = {
        "strategy": "default_code.py",
        "backtest": "code.py",
        "simulation": "current_code.py",
    }
    if kind not in filenames:
        raise ValueError(f"unsupported code context kind: {kind}")
    if kind == "simulation" and not source_backtest:
        raise ValueError("simulation source_backtest is required")

    payload = code.encode("utf-8")
    code_path = stage_dir / filenames[kind]
    _write_staged_bytes(code_path, payload)
    params_path = stage_dir / "params.json"
    params_payload = (
        json.dumps(params, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    _write_staged_bytes(params_path, params_payload)

    version_entries: list[dict[str, object]] = []
    source_entry: dict[str, object] | None = None
    if kind == "simulation":
        source_path = stage_dir / "source.json"
        source_payload = (
            json.dumps({"backtest_id": source_backtest}, indent=2) + "\n"
        ).encode("utf-8")
        _write_staged_bytes(source_path, source_payload)
        source_entry = {
            "path": source_path.relative_to(stage_dir).as_posix(),
            "sha256": hashlib.sha256(source_payload).hexdigest(),
            "bytes": len(source_payload),
        }
        for version in [*(versions or []), code]:
            version_payload = version.encode("utf-8")
            digest = hashlib.sha256(version_payload).hexdigest()
            version_path = stage_dir / "code_versions" / f"{digest}.py"
            if not version_path.exists():
                _write_staged_bytes(version_path, version_payload)
            entry = {
                "path": version_path.relative_to(stage_dir).as_posix(),
                "sha256": digest,
                "bytes": len(version_payload),
            }
            if entry not in version_entries:
                version_entries.append(entry)

    return {
        "path": code_path.relative_to(stage_dir).as_posix(),
        "sha256": hashlib.sha256(payload).hexdigest(),
        "bytes": len(payload),
        "params_path": params_path.relative_to(stage_dir).as_posix(),
        "params": {
            "path": params_path.relative_to(stage_dir).as_posix(),
            "sha256": hashlib.sha256(params_payload).hexdigest(),
            "bytes": len(params_payload),
        },
        "source": source_entry,
        "versions": version_entries,
    }


def recover_malformed_json(
    raw: bytes,
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    rows: list[dict[str, object]] = []
    errors: list[dict[str, object]] = []
    offset = 0
    for raw_line in raw.splitlines(keepends=True):
        line = raw_line.rstrip(b"\r\n")
        if line.strip():
            try:
                value = json.loads(line)
            except (json.JSONDecodeError, UnicodeDecodeError):
                errors.append(
                    {"offset": offset, "bytes": len(line), "error": "invalid_json"}
                )
            else:
                if isinstance(value, dict):
                    rows.append(value)
                else:
                    errors.append(
                        {"offset": offset, "bytes": len(line), "error": "invalid_json"}
                    )
        offset += len(raw_line)
    return rows, errors


def archive_log_response(raw: bytes, destination: Path) -> dict[str, object]:
    evidence = write_raw_gzip(raw, destination)
    rows, errors = recover_malformed_json(raw)
    return {
        "raw": evidence,
        "rows": rows,
        "recovery": {
            "source_lines": len(raw.splitlines()),
            "recovered_rows": len(rows),
            "errors": errors,
        },
    }


def validate_attribution(
    lines: Iterable[bytes],
    run_status: str,
    writer_present: bool,
    *,
    expected_token: str = "",
    expected_path: str = "",
    expected_start: str = "",
    expected_end: str = "",
) -> dict[str, object]:
    if not writer_present:
        return {
            "required": False,
            "status": "missing_at_source",
            "rows": 0,
            "evidence": {"code_writer": False},
        }
    raw_lines = [line.rstrip(b"\r\n") for line in lines if line.strip()]
    try:
        rows = [json.loads(line) for line in raw_lines]
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise AttributionIncomplete("attribution JSONL is malformed") from error
    if not rows or not all(isinstance(row, dict) for row in rows):
        raise AttributionIncomplete("attribution JSONL is empty or non-object")
    tokens = {row.get("token") or row.get("audit_token") for row in rows}
    if len(tokens) != 1 or None in tokens:
        raise AttributionIncomplete("attribution token mismatch")
    token = str(next(iter(tokens)) or "")
    if expected_token and token != expected_token:
        raise AttributionIncomplete("attribution does not match expected token")
    if expected_path and PurePosixPath(expected_path).stem != (expected_token or token):
        raise AttributionIncomplete("attribution path token mismatch")
    sequence = [row.get("seq") for row in rows]
    if not all(type(value) is int for value in sequence) or sequence != list(
        range(1, len(rows) + 1)
    ):
        raise AttributionIncomplete("attribution sequence is not contiguous")
    events = [row.get("event") for row in rows]
    if events[0] != "run_start":
        raise AttributionIncomplete("run_start is missing")
    if run_status in {"done", "failed", "cancelled"} and events[-1] != "run_end":
        raise AttributionIncomplete("run_end is missing")
    observed = [
        str(row.get("current_dt") or "")[:10]
        for row in rows
        if str(row.get("current_dt") or "")
    ]
    if observed != sorted(observed):
        raise AttributionIncomplete("attribution time order is not monotonic")
    if expected_start and (not observed or observed[0] != expected_start[:10]):
        raise AttributionIncomplete("attribution start time does not match target run")
    if expected_end:
        end = expected_end[:10]
        if not observed or any(value > end for value in observed):
            raise AttributionIncomplete("attribution time is outside target run")
        if run_status == "done" and observed[-1] != end:
            raise AttributionIncomplete(
                "attribution end time does not match target run"
            )
    payload = b"\n".join(raw_lines) + b"\n"
    return {
        "required": True,
        "status": "complete",
        "rows": len(rows),
        "sha256": hashlib.sha256(payload).hexdigest(),
        "token": token,
        "first_seq": sequence[0],
        "last_seq": sequence[-1],
        "run_start": True,
        "run_end": events[-1] == "run_end",
        "evidence": {
            "expected_token": expected_token or token,
            "source_path": expected_path,
            "expected_start": expected_start,
            "expected_end": expected_end,
        },
    }


SIMULATION_STREAMS = ("code", "snapshots", "data", "logs")


def _verified_simulation_streams(
    document: dict[str, object],
) -> dict[str, dict[str, object]]:
    streams = document.get("streams")
    if not isinstance(streams, dict):
        raise SimulationIncrementError("simulation streams are missing")
    verified: dict[str, dict[str, object]] = {}
    for name in SIMULATION_STREAMS:
        state = streams.get(name)
        if not isinstance(state, dict):
            raise SimulationIncrementError(f"simulation stream is missing: {name}")
        cursor = state.get("cursor")
        digest = state.get("sha256")
        if not isinstance(cursor, str) or not cursor:
            raise SimulationIncrementError(f"simulation cursor is invalid: {name}")
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise SimulationIncrementError(f"simulation digest is invalid: {name}")
        verified[name] = deepcopy(state)
    return verified


def _optional_current_streams(
    manifest: dict[str, object],
) -> dict[str, dict[str, object]]:
    if "streams" not in manifest:
        return {}
    return _verified_simulation_streams(manifest)


def _remote_attribution(remote: dict[str, object], status: str) -> dict[str, object]:
    writer_present = remote.get("writer_present")
    lines = remote.get("attribution_lines")
    if (
        not isinstance(writer_present, bool)
        or not isinstance(lines, list)
        or not all(isinstance(line, bytes) for line in lines)
    ):
        raise SimulationIncrementError("simulation attribution evidence is missing")
    attribution_status = "done" if status == "closed" else status
    return validate_attribution(lines, attribution_status, writer_present)


def next_increment(
    manifest: dict[str, object], remote: dict[str, object]
) -> dict[str, object]:
    status = remote.get("status")
    if status not in {"active", "closed"}:
        raise SimulationIncrementError("simulation status is invalid")
    current = _optional_current_streams(manifest)
    offered = _verified_simulation_streams(remote)
    requests: dict[str, dict[str, object]] = {}
    for name in SIMULATION_STREAMS:
        previous = current.get(name)
        if previous == offered[name]:
            continue
        requests[name] = {
            "after": previous.get("cursor") if previous else None,
            "through": offered[name]["cursor"],
            "sha256": offered[name]["sha256"],
        }
    return {
        "status": status,
        "changed": list(requests),
        "requests": requests,
    }


def _accept_simulation_remote(
    manifest: dict[str, object], remote: dict[str, object]
) -> dict[str, object]:
    accepted = deepcopy(manifest)
    object_state = accepted.get("object")
    if not isinstance(object_state, dict):
        object_state = {}
        accepted["object"] = object_state
    object_state["status"] = remote["status"]
    accepted["streams"] = _verified_simulation_streams(remote)
    accepted["attribution"] = _remote_attribution(remote, str(remote["status"]))
    return accepted


def finalize_closed_simulation(
    manifest: dict[str, object], remote: dict[str, object]
) -> dict[str, object]:
    if (
        manifest.get("tracking") == "stopped"
        and manifest.get("final_sync") == "complete"
    ):
        return deepcopy(manifest)
    if remote.get("status") != "closed":
        raise SimulationIncrementError("final sync requires closed remote status")
    next_increment(manifest, remote)
    accepted = _accept_simulation_remote(manifest, remote)
    accepted["tracking"] = "stopped"
    accepted["final_sync"] = "complete"
    return accepted


def _resume_cursors(manifest: dict[str, object]) -> dict[str, object]:
    try:
        streams = _optional_current_streams(manifest)
    except SimulationIncrementError:
        return {}
    return {name: state["cursor"] for name, state in streams.items()}


def sync_active_simulations(
    simulations: Iterable[dict[str, object]],
    fetch_remote: Callable[[dict[str, object]], dict[str, object]],
) -> list[dict[str, object]]:
    results: list[dict[str, object]] = []
    for simulation in simulations:
        original = deepcopy(simulation)
        simulation_id = str(original.get("id") or "")
        if original.get("tracking") != "active":
            results.append(
                {
                    "id": simulation_id,
                    "committed": False,
                    "status": "not_active",
                    "manifest": original,
                    "resume": _resume_cursors(original),
                }
            )
            continue
        try:
            remote = fetch_remote(deepcopy(original))
            increment = next_increment(original, remote)
            if remote.get("status") == "closed":
                accepted = finalize_closed_simulation(original, remote)
            else:
                accepted = _accept_simulation_remote(original, remote)
                accepted["tracking"] = "active"
                accepted.pop("final_sync", None)
            results.append(
                {
                    "id": simulation_id,
                    "committed": True,
                    "status": str(remote["status"]),
                    "increment": increment,
                    "manifest": accepted,
                }
            )
        except Exception as error:
            results.append(
                {
                    "id": simulation_id,
                    "committed": False,
                    "status": "failed",
                    "error": {
                        "type": type(error).__name__,
                        "message": str(error),
                    },
                    "manifest": original,
                    "resume": _resume_cursors(original),
                }
            )
    return results


def extract_paid_log_range(
    archive_path: Path, range_: str, destination: Path
) -> dict[str, object]:
    match = re.fullmatch(r"(\d+):(\d+)", range_.strip())
    if match is None:
        raise IntegrityError("paid log range must be START:END")
    start, end = (int(value) for value in match.groups())
    if start >= end:
        raise IntegrityError("paid log range must be non-empty")
    try:
        with zipfile.ZipFile(archive_path) as archive:
            names = sorted(
                item.filename for item in archive.infolist() if not item.is_dir()
            )
            if not names:
                raise IntegrityError("paid log archive is empty")
            lines: list[bytes] = []
            for name in names:
                lines.extend(archive.read(name).splitlines())
    except zipfile.BadZipFile as error:
        raise IntegrityError("paid log download is not a ZIP archive") from error
    if start >= len(lines):
        raise IntegrityError(
            f"paid log range starts after the downloaded log: {len(lines)} rows"
        )
    selected = lines[start : min(end, len(lines))]
    raw = b"\n".join(selected) + (b"\n" if selected else b"")
    evidence = write_raw_gzip(raw, destination)
    return {
        "path": str(destination),
        "requested_range": range_,
        "actual_range": f"{start}:{start + len(selected)}",
        "rows": len(selected),
        "source_rows": len(lines),
        "bytes": evidence["bytes"],
        "sha256": evidence["compressed_sha256"],
        "raw_sha256": evidence["sha256"],
    }


def stage_external_file(source: Path, stage_dir: Path) -> dict[str, object]:
    if not source.is_file():
        raise FileNotFoundError(source)

    stage_dir.mkdir(parents=True, exist_ok=True)
    destination = stage_dir / source.name
    digest = hashlib.sha256()
    with source.open("rb") as source_file, destination.open("wb") as target_file:
        while chunk := source_file.read(1024 * 1024):
            target_file.write(chunk)
            digest.update(chunk)

    return {
        "path": str(destination),
        "bytes": destination.stat().st_size,
        "sha256": digest.hexdigest(),
    }
