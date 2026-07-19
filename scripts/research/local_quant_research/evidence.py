from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Iterable, Mapping

import numpy as np

from scripts.research.result_contract import (
    EvidenceError,
    ExecutionBundle,
    validate_extension_table as validate_extension_table,
)


_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")
_REASON_PATTERN = re.compile(r"[a-z][a-z0-9_]{0,63}")


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
    array = np.asarray(value)
    if array.dtype.hasobject:
        raise EvidenceError("execution digest does not accept object arrays")
    digest = hashlib.sha256()
    if array.flags.c_contiguous:
        digest.update(memoryview(array).cast("B"))
    else:
        iterator = np.nditer(
            array,
            flags=("external_loop", "buffered"),
            order="C",
            buffersize=65_536,
        )
        for chunk in iterator:
            digest.update(memoryview(np.asarray(chunk)).cast("B"))
    return {
        "dtype": array.dtype.descr if array.dtype.names else array.dtype.str,
        "shape": list(array.shape),
        "sha256": digest.hexdigest(),
    }


def _run_digest_document(run: object) -> dict[str, object]:
    ledger_document: dict[str, object] = {}
    seen_arrays: dict[int, str] = {}
    for field in (
        "orders",
        "assets",
        "cash",
        "value",
        "trades",
        "positions",
        "returns",
    ):
        array = np.asarray(getattr(run.ledger, field))
        previous = seen_arrays.get(id(array))
        if previous is None:
            ledger_document[field] = _array_digest(array)
            seen_arrays[id(array)] = field
        else:
            ledger_document[field] = {"same_as": previous}
    return {
        "ledger": ledger_document,
        "trace": {
            key: _array_digest(np.asarray(value))
            for key, value in sorted(run.trace.items())
        },
    }


def execution_digest(
    execution: ExecutionBundle,
) -> str:
    primary_document = _run_digest_document(execution.primary)
    run_documents: dict[str, object] = {"primary": primary_document}
    if execution.final is execution.primary:
        run_documents["final"] = {"same_as": "primary"}
    else:
        run_documents["final"] = _run_digest_document(execution.final)
    document: dict[str, object] = {
        "stages": list(execution.stages),
        "runs": run_documents,
    }
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
