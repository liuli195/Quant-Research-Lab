from __future__ import annotations

import hashlib
import gzip
import json
import msvcrt
import os
import re
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, urlsplit


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


def resolve_local_id(
    index_path: Path, kind: str, page_identity: dict[str, str]
) -> str:
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
            raise IdentityConflict(
                f"page identity conflict: {kind}/{ordinal}"
            )
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
        error_log=DatasetPolicy(required=run_status in {"failed", "cancelled"}),
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
        datasets["error_log"].update(
            status="complete", rows=0, verified_empty=True
        )
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
            verified_empty = item.get("verified_empty") is True and item.get("rows") == 0
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
    datasets = manifest.get("datasets")
    if isinstance(datasets, dict):
        for dataset in datasets.values():
            if not isinstance(dataset, dict):
                continue
            for item in dataset.get("files") or []:
                if not isinstance(item, dict) or not item.get("path") or not item.get(
                    "sha256"
                ):
                    raise IntegrityError("manifest file reference requires path and sha256")
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


def _verify_manifest_document(
    object_dir: Path, manifest: dict[str, object]
) -> None:
    if manifest.get("schema_version") != 1:
        raise IntegrityError("unsupported manifest schema")
    gate = manifest.get("gate")
    if not isinstance(gate, dict) or gate.get("status") != "pass":
        raise IntegrityError("manifest gate did not pass")
    datasets = manifest.get("datasets")
    if not isinstance(datasets, dict) or evaluate_gate(datasets)["status"] != "pass":
        raise IntegrityError("dataset gate did not pass")
    for relative, expected_sha256 in _manifest_references(manifest).items():
        path = _object_path(object_dir, relative)
        if not path.is_file():
            raise IntegrityError(f"missing manifest file: {relative}")
        if _file_sha256(path) != expected_sha256:
            raise IntegrityError(f"manifest file hash mismatch: {relative}")


def commit_manifest(
    object_dir: Path,
    manifest: dict[str, object],
    staged_files: list[Path],
) -> None:
    gate = manifest.get("gate")
    if not isinstance(gate, dict) or gate.get("status") != "pass":
        raise IntegrityError("refusing to commit failed manifest")
    references = _manifest_references(manifest)
    with object_lock(object_dir):
        planned: list[tuple[Path, Path]] = []
        redundant: list[Path] = []
        used: set[Path] = set()
        for relative, expected_sha256 in references.items():
            destination = _object_path(object_dir, relative)
            if destination.is_file():
                if _file_sha256(destination) != expected_sha256:
                    raise IntegrityError(f"immutable file conflict: {relative}")
                candidates = [
                    path
                    for path in staged_files
                    if path not in used and path.name == Path(relative).name
                ]
                if candidates:
                    if len(candidates) != 1 or _file_sha256(candidates[0]) != expected_sha256:
                        raise IntegrityError(f"staged file hash mismatch: {relative}")
                    used.add(candidates[0])
                    redundant.append(candidates[0])
                continue
            candidates = [
                path
                for path in staged_files
                if path not in used and path.name == Path(relative).name
            ]
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
        _verify_manifest_document(object_dir, manifest)

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
