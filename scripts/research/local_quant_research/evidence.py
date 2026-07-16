from __future__ import annotations

import csv
import hashlib
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Iterable, Mapping, Sequence

import pyarrow as pa
import pyarrow.parquet as pq
import numpy as np

from .contracts import ExecutionBundle, OutputSpec, ResultExtension


_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")
_REASON_PATTERN = re.compile(r"[a-z][a-z0-9_]{0,63}")
_COMPLETE_STAGES = (
    "snapshot_validation",
    "config_validation",
    "project_execution",
    "output_validation",
    "evidence_finalization",
)


class EvidenceError(RuntimeError):
    """Raised when immutable research evidence is invalid."""


def canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def canonical_digest(value: object) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def _array_digest(value: np.ndarray) -> dict[str, object]:
    array = np.ascontiguousarray(np.asarray(value))
    return {
        "dtype": array.dtype.descr if array.dtype.names else array.dtype.str,
        "shape": list(array.shape),
        "sha256": hashlib.sha256(array.tobytes()).hexdigest(),
    }


def execution_digest(
    execution: ExecutionBundle,
    extensions: Sequence[ResultExtension] = (),
) -> str:
    runs = {"primary": execution.primary, "final": execution.final}
    document: dict[str, object] = {"stages": list(execution.stages), "runs": {}}
    run_documents: dict[str, object] = {}
    for name, run in runs.items():
        run_documents[name] = {
            "ledger": {
                field: _array_digest(np.asarray(getattr(run.ledger, field)))
                for field in (
                    "orders",
                    "assets",
                    "cash",
                    "value",
                    "trades",
                    "positions",
                    "returns",
                )
            },
            "trace": {
                key: _array_digest(np.asarray(value))
                for key, value in sorted(run.trace.items())
            },
        }
    document["runs"] = run_documents
    document["extensions"] = [
        {
            "name": extension.name,
            "schema_version": extension.schema_version,
            "unique_key": list(extension.unique_key),
            "evidence": dict(extension.evidence),
            "rows": extension.table.to_pylist(),
        }
        for extension in extensions
    ]
    return canonical_digest(document)


def file_digest(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def compute_run_id(
    snapshot_digest: str,
    config_digest: str,
    code_digest: str,
) -> str:
    digests = {
        "snapshot": snapshot_digest,
        "config": config_digest,
        "code": code_digest,
    }
    if any(_SHA256_PATTERN.fullmatch(value) is None for value in digests.values()):
        raise ValueError("run identity inputs must be lowercase SHA256 digests")
    return canonical_digest(digests)


def _load_json(path: Path) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise EvidenceError(f"invalid JSON output: {path.name}") from exc


def _validate_output(path: Path, output_format: str) -> None:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise EvidenceError(f"required output is missing: {path.name}") from exc
    if not raw:
        raise EvidenceError(f"required output is empty: {path.name}")
    if path.is_symlink():
        raise EvidenceError(f"required output must not be a symlink: {path.name}")
    if output_format == "json":
        value = _load_json(path)
        if not isinstance(value, (dict, list)):
            raise EvidenceError(f"JSON output must be an object or array: {path.name}")
    elif output_format == "csv":
        try:
            with path.open(encoding="utf-8-sig", newline="") as handle:
                reader = csv.reader(handle, strict=True)
                header = next(reader)
                if (
                    not header
                    or any(not column for column in header)
                    or len(header) != len(set(header))
                ):
                    raise EvidenceError(f"CSV header is invalid: {path.name}")
                for row in reader:
                    if len(row) != len(header):
                        raise EvidenceError(
                            f"CSV row has the wrong column count: {path.name}"
                        )
        except (OSError, UnicodeDecodeError, StopIteration, csv.Error) as exc:
            raise EvidenceError(f"invalid CSV output: {path.name}") from exc
    elif output_format == "parquet":
        try:
            schema = pq.read_schema(path)
        except (OSError, pa.ArrowException) as exc:
            raise EvidenceError(f"invalid Parquet output: {path.name}") from exc
        if not schema.names or len(schema.names) != len(set(schema.names)):
            raise EvidenceError(f"Parquet schema is invalid: {path.name}")
    else:
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise EvidenceError(f"text output must use UTF-8: {path.name}") from exc
        if not text.strip():
            raise EvidenceError(f"text output is blank: {path.name}")


def collect_output_evidence(
    root: Path,
    specs: Sequence[OutputSpec],
) -> list[dict[str, object]]:
    evidence: list[dict[str, object]] = []
    resolved_root = Path(root).resolve()
    for spec in sorted(specs, key=lambda item: item.path):
        path = (resolved_root / spec.path).resolve()
        try:
            path.relative_to(resolved_root)
        except ValueError as exc:
            raise EvidenceError("required output escapes the staging directory") from exc
        if spec.format == "directory":
            if not path.is_dir() or path.is_symlink():
                raise EvidenceError(f"required output directory is missing: {path.name}")
            files: list[dict[str, object]] = []
            for item in sorted(path.rglob("*")):
                if item.is_symlink():
                    raise EvidenceError("required output directory contains a symlink")
                if not item.is_file():
                    continue
                relative = item.relative_to(path).as_posix()
                files.append(
                    {
                        "path": relative,
                        "bytes": item.stat().st_size,
                        "sha256": file_digest(item),
                    }
                )
            if not files:
                raise EvidenceError("required output directory is empty")
            evidence.append(
                {
                    "path": spec.path,
                    "format": spec.format,
                    "bytes": sum(int(item["bytes"]) for item in files),
                    "sha256": canonical_digest(files),
                    "files": files,
                }
            )
        else:
            _validate_output(path, spec.format)
            evidence.append(
                {
                    "path": spec.path,
                    "format": spec.format,
                    "bytes": path.stat().st_size,
                    "sha256": file_digest(path),
                }
            )
    return evidence


def _write_json_exclusive(path: Path, document: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = canonical_bytes(document) + b"\n"
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.stem}.",
        suffix=".tmp",
        dir=path.parent,
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        temporary.write_bytes(payload)
        os.link(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def write_manifest(path: Path, document: Mapping[str, object]) -> None:
    payload = canonical_bytes(document) + b"\n"
    Path(path).write_bytes(payload)


def record_attempt(
    *,
    attempts_root: Path,
    attempt_id: str,
    project_id: str,
    run_id: str | None,
    status: str,
    stage: str,
    reason_codes: Iterable[str],
) -> Path:
    sanitized = tuple(dict.fromkeys(str(code) for code in reason_codes))[:10]
    if not sanitized or any(_REASON_PATTERN.fullmatch(code) is None for code in sanitized):
        sanitized = ("unspecified_failure",)
    document = {
        "schema_version": 1,
        "attempt_id": attempt_id,
        "project_id": project_id,
        "run_id": run_id,
        "status": status,
        "stage": stage,
        "reason_codes": list(sanitized),
    }
    path = Path(attempts_root) / f"{attempt_id}.json"
    _write_json_exclusive(path, document)
    return path


def validate_complete_run(
    run_dir: Path,
    *,
    project_id: str,
    run_id: str,
    snapshot: Mapping[str, object],
    inputs: Mapping[str, object],
    command: Sequence[str],
    required_outputs: Sequence[OutputSpec],
) -> None:
    manifest_path = Path(run_dir) / "run-manifest.json"
    manifest = _load_json(manifest_path)
    if not isinstance(manifest, dict):
        raise EvidenceError("run manifest must be an object")
    required_keys = {
        "schema_version",
        "project_id",
        "run_id",
        "status",
        "snapshot",
        "inputs",
        "command",
        "stages",
        "outputs",
        "output_set_sha256",
    }
    if set(manifest) != required_keys:
        raise EvidenceError("run manifest structure is invalid")
    if (
        manifest["schema_version"] != 1
        or manifest["project_id"] != project_id
        or manifest["run_id"] != run_id
        or manifest["status"] != "complete"
        or manifest["snapshot"] != dict(snapshot)
        or manifest["inputs"] != dict(inputs)
        or manifest["command"] != list(command)
    ):
        raise EvidenceError("run manifest identity is invalid")
    expected_stages = [
        {"name": name, "status": "complete"} for name in _COMPLETE_STAGES
    ]
    if manifest["stages"] != expected_stages:
        raise EvidenceError("run stage evidence is invalid")
    outputs = manifest["outputs"]
    if not isinstance(outputs, list) or not outputs:
        raise EvidenceError("run output evidence is missing")
    declared_contract = [
        {"path": item.get("path"), "format": item.get("format")}
        for item in outputs
        if isinstance(item, dict)
    ]
    expected_contract = [
        {"path": spec.path, "format": spec.format}
        for spec in sorted(required_outputs, key=lambda item: item.path)
    ]
    if declared_contract != expected_contract:
        raise EvidenceError("run output contract differs from the current config")
    try:
        specs = tuple(
            OutputSpec(path=item["path"], format=item["format"])
            for item in outputs
            if isinstance(item, dict)
        )
    except (KeyError, TypeError) as exc:
        raise EvidenceError("run output evidence is malformed") from exc
    if len(specs) != len(outputs):
        raise EvidenceError("run output evidence is malformed")
    recalculated = collect_output_evidence(run_dir, specs)
    if recalculated != outputs or canonical_digest(outputs) != manifest["output_set_sha256"]:
        raise EvidenceError("completed output digest mismatch")

    status = _load_json(Path(run_dir) / "project-status.json")
    expected_status = {
        "schema_version": 1,
        "status": "complete",
        "reason_codes": [],
        "next_action": "return_to_caller",
    }
    if status != expected_status:
        raise EvidenceError("completed project status is invalid")
    expected_files = {"run-manifest.json", "project-status.json"}
    for item in outputs:
        if item["format"] == "directory":
            expected_files.update(
                f"{item['path']}/{nested['path']}" for nested in item["files"]
            )
        else:
            expected_files.add(item["path"])
    actual_files = {
        path.relative_to(run_dir).as_posix()
        for path in Path(run_dir).rglob("*")
        if path.is_file() or path.is_symlink()
    }
    if actual_files != expected_files:
        raise EvidenceError("completed run contains an unexpected file set")
