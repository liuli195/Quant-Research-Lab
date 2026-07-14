from __future__ import annotations

import json
import hashlib
import os
import re
import shutil
import subprocess
import time
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Mapping, Sequence

from scripts.research.market_data.query import open_snapshot
from scripts.research.market_data.storage import MarketDataError

from .contracts import OutputSpec, RunConfig, RunResult, StageRecord
from .evidence import (
    EvidenceError,
    canonical_digest,
    collect_output_evidence,
    compute_run_id,
    file_digest,
    record_attempt,
    validate_complete_run,
    write_manifest,
)


_CONFIG_FIELDS = {
    "schema_version",
    "project_id",
    "snapshot_id",
    "snapshot_requirements",
    "project_entry",
    "command",
    "project_config",
    "code_identity",
    "declared_inputs",
    "required_outputs",
    "output_root",
    "stop_states",
}
_STOP_STATES = ("complete", "evidence_insufficient", "failed")
_PROJECT_ID_PATTERN = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")
_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")
_REASON_PATTERN = re.compile(r"[a-z][a-z0-9_]{0,63}")
_SENSITIVE_KEYS = ("password", "token", "cookie", "secret", "credential", "api_key")
_RESERVED_ARGUMENTS = {
    "--snapshot-manifest",
    "--market-data-root",
    "--project-config",
    "--output-dir",
    "--run-id",
    "--snapshot-id",
    "--code-sha256",
    "--config-sha256",
}
_COMPLETE_STAGE_NAMES = (
    "snapshot_validation",
    "config_validation",
    "project_execution",
    "output_validation",
    "evidence_finalization",
)


class ConfigurationError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class InputIntegrityError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


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


@dataclass(frozen=True)
class _FrozenExecutionInputs:
    root: Path
    repository: Path
    market_data: Path
    project_entry: Path
    project_config: Path


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


def _resolve_repo_path(value: object, *, repo_root: Path, field: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ConfigurationError("invalid_path", f"{field} must be a repository path")
    candidate = Path(value)
    if candidate.is_absolute():
        raise ConfigurationError("unsafe_path", f"{field} must be repository-relative")
    resolved = (repo_root / candidate).resolve()
    if not _inside(resolved, repo_root):
        raise ConfigurationError("unsafe_path", f"{field} escapes the repository")
    return resolved


def _contains_sensitive_key(value: object) -> bool:
    if isinstance(value, Mapping):
        for key, item in value.items():
            normalized = str(key).lower().replace("-", "_")
            if any(marker in normalized for marker in _SENSITIVE_KEYS):
                return True
            if _contains_sensitive_key(item):
                return True
    elif isinstance(value, list):
        return any(_contains_sensitive_key(item) for item in value)
    return False


def _load_raw_config(path: Path, *, repo_root: Path) -> dict[str, object]:
    config_path = Path(path).resolve()
    if not _inside(config_path, repo_root):
        raise ConfigurationError("unsafe_config_path", "config path is outside repository")
    try:
        value = json.loads(config_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigurationError("missing_config", "run config is missing") from exc
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ConfigurationError("invalid_config", "run config is not valid UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise ConfigurationError("invalid_config", "run config must be an object")
    return value


def _output_path(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise ConfigurationError("invalid_output", "required output path is invalid")
    if "\\" in value:
        raise ConfigurationError("invalid_output", "required output must use forward slashes")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or path.name in {
        "run-manifest.json",
        "project-status.json",
    }:
        raise ConfigurationError("unsafe_output", "required output escapes its contract")
    return path.as_posix()


def load_run_config(path: Path, *, repo_root: Path) -> RunConfig:
    repo_root = Path(repo_root).resolve()
    document = _load_raw_config(path, repo_root=repo_root)
    if _contains_sensitive_key(document):
        raise ConfigurationError("credential_field", "credential fields are forbidden")
    missing = sorted(_CONFIG_FIELDS - set(document))
    if missing or set(document) != _CONFIG_FIELDS:
        raise ConfigurationError("invalid_config", "run config fields are incomplete or unknown")
    if document["schema_version"] != 1:
        raise ConfigurationError("invalid_config", "schema_version must be 1")

    project_id = document["project_id"]
    if not isinstance(project_id, str) or _PROJECT_ID_PATTERN.fullmatch(project_id) is None:
        raise ConfigurationError("invalid_project_id", "project_id is invalid")
    snapshot_id = document["snapshot_id"]
    if not isinstance(snapshot_id, str) or _SHA256_PATTERN.fullmatch(snapshot_id) is None:
        raise ConfigurationError("missing_snapshot", "snapshot_id is missing or invalid")
    requirements = document["snapshot_requirements"]
    if not isinstance(requirements, Mapping) or not requirements:
        raise ConfigurationError("missing_snapshot_requirements", "snapshot requirements are missing")

    project_entry = _resolve_repo_path(
        document["project_entry"], repo_root=repo_root, field="project_entry"
    )
    if project_entry.suffix.lower() != ".py" or not project_entry.is_file():
        raise ConfigurationError("missing_project_entry", "project entry is missing")
    project_config = _resolve_repo_path(
        document["project_config"], repo_root=repo_root, field="project_config"
    )
    code_identity = _resolve_repo_path(
        document["code_identity"], repo_root=repo_root, field="code_identity"
    )

    command = document["command"]
    if (
        not isinstance(command, list)
        or len(command) < 2
        or any(not isinstance(item, str) or not item for item in command)
    ):
        raise ConfigurationError("unsafe_command", "command must be a non-empty argument array")
    expected_python = (repo_root / ".venv" / "Scripts" / "python.exe").resolve()
    command_python = _resolve_repo_path(command[0], repo_root=repo_root, field="command[0]")
    command_entry = _resolve_repo_path(command[1], repo_root=repo_root, field="command[1]")
    if command_python != expected_python or not expected_python.is_file():
        raise ConfigurationError("system_python", "command must use the project .venv Python")
    if command_entry != project_entry:
        raise ConfigurationError("unsafe_command", "command entry does not match project_entry")
    lowered_args = [item.lower() for item in command[1:]]
    if "-m" in lowered_args or "pip" in lowered_args or "install" in lowered_args:
        raise ConfigurationError("implicit_install", "implicit dependency installation is forbidden")
    if any(argument in _RESERVED_ARGUMENTS for argument in command[2:]):
        raise ConfigurationError("reserved_argument", "runner arguments must not be supplied by config")
    if any(
        any(marker in argument.lower().replace("-", "_") for marker in _SENSITIVE_KEYS)
        for argument in command[2:]
    ):
        raise ConfigurationError("credential_argument", "credential command arguments are forbidden")

    declared = document["declared_inputs"]
    if not isinstance(declared, list) or any(not isinstance(item, str) for item in declared):
        raise ConfigurationError("invalid_inputs", "declared_inputs must be a path array")
    declared_inputs = tuple(
        _resolve_repo_path(item, repo_root=repo_root, field="declared_inputs")
        for item in declared
    )
    if len(declared_inputs) != len(set(declared_inputs)):
        raise ConfigurationError("invalid_inputs", "declared_inputs must be unique")
    for input_path in (project_config, code_identity, *declared_inputs):
        if not input_path.is_file():
            raise ConfigurationError("missing_declared_input", "a declared input is missing")

    try:
        project_document = json.loads(project_config.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ConfigurationError("invalid_project_config", "project config is invalid") from exc
    if not isinstance(project_document, dict) or _contains_sensitive_key(project_document):
        raise ConfigurationError("invalid_project_config", "project config is unsafe or invalid")

    output_documents = document["required_outputs"]
    if not isinstance(output_documents, list) or not output_documents:
        raise ConfigurationError("missing_outputs", "required_outputs must be non-empty")
    output_specs: list[OutputSpec] = []
    for item in output_documents:
        if not isinstance(item, dict) or set(item) != {"path", "format"}:
            raise ConfigurationError("invalid_output", "required output structure is invalid")
        output_format = item["format"]
        if output_format not in {"json", "csv", "markdown", "text"}:
            raise ConfigurationError("invalid_output", "required output format is invalid")
        output_specs.append(OutputSpec(path=_output_path(item["path"]), format=output_format))
    if len({spec.path for spec in output_specs}) != len(output_specs):
        raise ConfigurationError("invalid_output", "required output paths must be unique")

    output_root = _resolve_repo_path(
        document["output_root"], repo_root=repo_root, field="output_root"
    )
    if output_root != (repo_root / ".local" / "quant-research").resolve():
        raise ConfigurationError("unsafe_output_root", "output_root must be .local/quant-research")
    stop_states = document["stop_states"]
    if stop_states != list(_STOP_STATES):
        raise ConfigurationError("unknown_state", "stop_states must contain the fixed three states")

    return RunConfig(
        project_id=project_id,
        snapshot_id=snapshot_id,
        snapshot_requirements=dict(requirements),
        project_entry=project_entry,
        command=tuple(command),
        project_config=project_config,
        code_identity=code_identity,
        declared_inputs=declared_inputs,
        required_outputs=tuple(output_specs),
        output_root=output_root,
        stop_states=tuple(stop_states),
        document=document,
    )


def _safe_project_id(raw: Mapping[str, object]) -> str:
    value = raw.get("project_id")
    if isinstance(value, str) and _PROJECT_ID_PATTERN.fullmatch(value):
        return value
    return "_invalid"


def _attempt_result(
    *,
    repo_root: Path,
    project_id: str,
    status: str,
    stage: str,
    code: str,
    message: str,
    run_id: str | None,
    stages: Sequence[StageRecord],
    staging: Path | None = None,
) -> RunResult:
    if staging is not None and staging.exists():
        shutil.rmtree(staging)
    attempt_id = uuid.uuid4().hex
    attempts_root = (
        repo_root / ".local" / "quant-research" / project_id / ".attempts"
    )
    final_status = status if status in {"evidence_insufficient", "failed"} else "failed"
    try:
        record_attempt(
            attempts_root=attempts_root,
            attempt_id=attempt_id,
            project_id=project_id,
            run_id=run_id,
            status=final_status,
            stage=stage,
            reason_codes=(code,),
        )
    except (OSError, EvidenceError):
        final_status = "failed"
        code = "attempt_record_failed"
        message = "attempt evidence could not be recorded"
        attempt_id = None
    return RunResult(
        status=final_status,
        project_id=project_id,
        run_id=run_id,
        run_path=None,
        attempt_id=attempt_id,
        reused=False,
        reasons=(message,),
        stages=tuple(stages),
    )


def _snapshot_requirements_match(actual: object, expected: Mapping[str, object]) -> bool:
    return actual == dict(expected)


def _code_identity(config: RunConfig, *, repo_root: Path) -> tuple[str, dict[str, object]]:
    try:
        document = json.loads(config.code_identity.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InputIntegrityError("invalid_code_identity", "code identity is invalid") from exc
    if not isinstance(document, dict) or set(document) != {"schema_version", "files"}:
        raise InputIntegrityError("invalid_code_identity", "code identity structure is invalid")
    files = document["files"]
    if document["schema_version"] != 1 or not isinstance(files, list) or not files:
        raise InputIntegrityError("invalid_code_identity", "code identity files are invalid")
    normalized: list[dict[str, str]] = []
    for item in files:
        if not isinstance(item, dict) or set(item) != {"path", "sha256"}:
            raise InputIntegrityError("invalid_code_identity", "code identity entry is invalid")
        try:
            code_path = _resolve_repo_path(item["path"], repo_root=repo_root, field="code path")
        except ConfigurationError as exc:
            raise InputIntegrityError("invalid_code_identity", "code identity path is unsafe") from exc
        expected = item["sha256"]
        if not code_path.is_file() or not isinstance(expected, str) or _SHA256_PATTERN.fullmatch(expected) is None:
            raise InputIntegrityError("invalid_code_identity", "code identity file is missing")
        if file_digest(code_path) != expected:
            raise InputIntegrityError("code_digest_mismatch", "declared code digest mismatch")
        normalized.append(
            {"path": code_path.relative_to(repo_root).as_posix(), "sha256": expected}
        )
    if normalized != sorted(normalized, key=lambda item: item["path"]) or len(
        {item["path"] for item in normalized}
    ) != len(normalized):
        raise InputIntegrityError("invalid_code_identity", "code identity must be sorted and unique")
    entry_path = config.project_entry.relative_to(repo_root).as_posix()
    if entry_path not in {item["path"] for item in normalized}:
        raise InputIntegrityError("missing_entry_identity", "project entry is absent from code identity")
    normalized_document = {"schema_version": 1, "files": normalized}
    return canonical_digest(normalized_document), normalized_document


def _input_evidence(config: RunConfig, *, repo_root: Path) -> tuple[str, str, dict[str, object]]:
    code_digest, code_document = _code_identity(config, repo_root=repo_root)
    project_config_digest = file_digest(config.project_config)
    code_identity_digest = file_digest(config.code_identity)
    declared = [
        {
            "path": path.relative_to(repo_root).as_posix(),
            "sha256": file_digest(path),
        }
        for path in config.declared_inputs
    ]
    config_identity = {
        "run_config": dict(config.document),
        "project_config_sha256": project_config_digest,
        "code_identity_sha256": code_identity_digest,
        "declared_inputs": declared,
    }
    config_digest = canonical_digest(config_identity)
    evidence = {
        "config_sha256": config_digest,
        "project_config_sha256": project_config_digest,
        "code_identity_sha256": code_identity_digest,
        "code_sha256": code_digest,
        "code_identity": code_document,
        "declared_inputs": declared,
    }
    return config_digest, code_digest, evidence


def _copy_verified_file(source: Path, target: Path, expected_sha256: str) -> None:
    try:
        content = source.read_bytes()
    except OSError as exc:
        raise InputIntegrityError(
            "run_input_changed",
            "a run input could not be frozen",
        ) from exc
    if hashlib.sha256(content).hexdigest() != expected_sha256:
        raise InputIntegrityError(
            "run_input_changed",
            "a run input changed before it could be frozen",
        )
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)


def _freeze_execution_inputs(
    *,
    config: RunConfig,
    repo_root: Path,
    market_root: Path,
    snapshot_document: Mapping[str, object],
    snapshot_digest: str,
    snapshot_normalized_digest: str,
    inputs: Mapping[str, object],
    execution_root: Path,
) -> _FrozenExecutionInputs:
    frozen_repo = execution_root / "repository"
    frozen_market = execution_root / "market-data"
    try:
        execution_root.mkdir()
        project_config_relative = config.project_config.relative_to(repo_root)
        code_identity_relative = config.code_identity.relative_to(repo_root)
        _copy_verified_file(
            config.project_config,
            frozen_repo / project_config_relative,
            str(inputs["project_config_sha256"]),
        )
        _copy_verified_file(
            config.code_identity,
            frozen_repo / code_identity_relative,
            str(inputs["code_identity_sha256"]),
        )
        for item in inputs["code_identity"]["files"]:
            relative = Path(str(item["path"]))
            _copy_verified_file(
                repo_root / relative,
                frozen_repo / relative,
                str(item["sha256"]),
            )
        for item in inputs["declared_inputs"]:
            relative = Path(str(item["path"]))
            _copy_verified_file(
                repo_root / relative,
                frozen_repo / relative,
                str(item["sha256"]),
            )

        snapshot_id = config.snapshot_id
        _copy_verified_file(
            market_root / "snapshots" / f"{snapshot_id}.json",
            frozen_market / "snapshots" / f"{snapshot_id}.json",
            snapshot_digest,
        )
        for batch in snapshot_document["batches"]:
            batch_id = str(batch["batch_id"])
            source_dir = market_root / "batches" / batch_id
            target_dir = frozen_market / "batches" / batch_id
            for name, digest_field in (
                ("manifest.json", "manifest_sha256"),
                ("market-data.parquet", "parquet_sha256"),
                ("validation.json", "validation_sha256"),
            ):
                _copy_verified_file(
                    source_dir / name,
                    target_dir / name,
                    str(batch[digest_field]),
                )
        frozen_view = open_snapshot(snapshot_id, root=frozen_market)
        if frozen_view.digest != snapshot_normalized_digest:
            raise InputIntegrityError(
                "run_input_changed",
                "the frozen market-data snapshot differs from the run identity",
            )
        return _FrozenExecutionInputs(
            root=execution_root,
            repository=frozen_repo,
            market_data=frozen_market,
            project_entry=frozen_repo / config.project_entry.relative_to(repo_root),
            project_config=frozen_repo / project_config_relative,
        )
    except (KeyError, TypeError, OSError, MarketDataError, InputIntegrityError) as exc:
        if execution_root.exists():
            shutil.rmtree(execution_root, ignore_errors=True)
        if isinstance(exc, InputIntegrityError):
            raise
        raise InputIntegrityError(
            "run_input_changed",
            "run inputs could not be frozen",
        ) from exc


def _run_inputs_unchanged(
    *,
    config_path: Path,
    repo_root: Path,
    market_root: Path,
    snapshot_id: str,
    snapshot_path: Path,
    snapshot_digest: str,
    snapshot_normalized_digest: str,
    config_digest: str,
    code_digest: str,
    inputs: Mapping[str, object],
) -> bool:
    try:
        current_snapshot = open_snapshot(snapshot_id, root=market_root)
        current_snapshot_document = json.loads(
            snapshot_path.read_text(encoding="utf-8")
        )
        current_snapshot_digest = file_digest(snapshot_path)
        current_config = load_run_config(config_path, repo_root=repo_root)
        current_config_digest, current_code_digest, current_inputs = _input_evidence(
            current_config,
            repo_root=repo_root,
        )
    except (
        ConfigurationError,
        InputIntegrityError,
        MarketDataError,
        OSError,
        UnicodeDecodeError,
        json.JSONDecodeError,
    ):
        return False
    return (
        current_snapshot_digest == snapshot_digest
        and current_snapshot.digest == snapshot_normalized_digest
        and _snapshot_requirements_match(
            current_snapshot_document.get("selection"),
            current_config.snapshot_requirements,
        )
        and current_config_digest == config_digest
        and current_code_digest == code_digest
        and current_inputs == dict(inputs)
    )


def _sanitized_environment(python_path: Path) -> dict[str, str]:
    allowed = (
        "SYSTEMROOT",
        "WINDIR",
        "COMSPEC",
        "PATHEXT",
        "PATH",
        "TEMP",
        "TMP",
        "HOME",
        "USERPROFILE",
        "LOCALAPPDATA",
    )
    environment = {key: os.environ[key] for key in allowed if key in os.environ}
    environment.update(
        {
            "PYTHONPATH": str(python_path),
            "PYTHONNOUSERSITE": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONUTF8": "1",
        }
    )
    return environment


def _repo_state(
    repo_root: Path,
    *,
    ignored_roots: Sequence[Path],
) -> dict[str, tuple[int, int]]:
    ignored_names = {".git", ".venv", ".pytest_cache", ".ruff_cache", "__pycache__"}
    state: dict[str, tuple[int, int]] = {}
    ignored = tuple(path.resolve() for path in ignored_roots)
    for path in repo_root.rglob("*"):
        if any(part in ignored_names for part in path.relative_to(repo_root).parts):
            continue
        if any(_inside(path, root) for root in ignored):
            continue
        if path.is_file() and not path.is_symlink():
            stat = path.stat()
            state[path.relative_to(repo_root).as_posix()] = (stat.st_size, stat.st_mtime_ns)
    return state


def _project_status(staging: Path) -> tuple[str, tuple[str, ...]]:
    path = staging / "project-status.json"
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise EvidenceError("project status is missing or invalid") from exc
    if not isinstance(document, dict) or set(document) != {
        "schema_version",
        "status",
        "reason_codes",
    }:
        raise EvidenceError("project status structure is invalid")
    status = document["status"]
    reasons = document["reason_codes"]
    if (
        document["schema_version"] != 1
        or status not in _STOP_STATES
        or not isinstance(reasons, list)
        or len(reasons) > 10
        or any(not isinstance(code, str) or _REASON_PATTERN.fullmatch(code) is None for code in reasons)
    ):
        raise EvidenceError("project status value is invalid")
    if status == "complete" and reasons:
        raise EvidenceError("complete project status must not contain reasons")
    return status, tuple(reasons)


def _actual_staging_files(staging: Path) -> set[str]:
    files: set[str] = set()
    for path in staging.rglob("*"):
        if path.is_symlink():
            raise EvidenceError("project output must not contain symlinks")
        if path.is_file():
            files.add(path.relative_to(staging).as_posix())
    return files


def run_project(config_path: Path, *, repo_root: Path) -> RunResult:
    repo_root = Path(repo_root).resolve()
    stages: list[StageRecord] = []
    try:
        raw = _load_raw_config(config_path, repo_root=repo_root)
    except ConfigurationError as exc:
        return _attempt_result(
            repo_root=repo_root,
            project_id="_invalid",
            status="evidence_insufficient",
            stage="snapshot_validation",
            code=exc.code,
            message=str(exc),
            run_id=None,
            stages=(StageRecord("snapshot_validation", "evidence_insufficient"),),
        )
    project_id = _safe_project_id(raw)
    snapshot_id = raw.get("snapshot_id")
    if not isinstance(snapshot_id, str) or _SHA256_PATTERN.fullmatch(snapshot_id) is None:
        return _attempt_result(
            repo_root=repo_root,
            project_id=project_id,
            status="evidence_insufficient",
            stage="snapshot_validation",
            code="missing_snapshot",
            message="snapshot_id is missing or invalid",
            run_id=None,
            stages=(StageRecord("snapshot_validation", "evidence_insufficient"),),
        )

    market_root = repo_root / ".local" / "market-data"
    snapshot_path = market_root / "snapshots" / f"{snapshot_id}.json"
    if not snapshot_path.is_file():
        return _attempt_result(
            repo_root=repo_root,
            project_id=project_id,
            status="evidence_insufficient",
            stage="snapshot_validation",
            code="missing_snapshot",
            message="declared snapshot is missing",
            run_id=None,
            stages=(StageRecord("snapshot_validation", "evidence_insufficient"),),
        )
    try:
        snapshot_view = open_snapshot(snapshot_id, root=market_root)
        snapshot_document = json.loads(snapshot_path.read_text(encoding="utf-8"))
        snapshot_digest = file_digest(snapshot_path)
    except (MarketDataError, OSError, UnicodeDecodeError, json.JSONDecodeError):
        return _attempt_result(
            repo_root=repo_root,
            project_id=project_id,
            status="failed",
            stage="snapshot_validation",
            code="snapshot_integrity_failed",
            message="declared snapshot failed integrity validation",
            run_id=None,
            stages=(StageRecord("snapshot_validation", "failed"),),
        )
    stages.append(StageRecord("snapshot_validation", "complete"))

    try:
        config = load_run_config(config_path, repo_root=repo_root)
        if not _snapshot_requirements_match(
            snapshot_document.get("selection"), config.snapshot_requirements
        ):
            raise ConfigurationError(
                "snapshot_requirements_unmet",
                "snapshot does not exactly cover the declared requirements",
            )
        config_digest, code_digest, inputs = _input_evidence(config, repo_root=repo_root)
    except ConfigurationError as exc:
        return _attempt_result(
            repo_root=repo_root,
            project_id=project_id,
            status="evidence_insufficient",
            stage="config_validation",
            code=exc.code,
            message=str(exc),
            run_id=None,
            stages=(*stages, StageRecord("config_validation", "evidence_insufficient")),
        )
    except InputIntegrityError as exc:
        return _attempt_result(
            repo_root=repo_root,
            project_id=project_id,
            status="failed",
            stage="config_validation",
            code=exc.code,
            message=str(exc),
            run_id=None,
            stages=(*stages, StageRecord("config_validation", "failed")),
        )
    stages.append(StageRecord("config_validation", "complete"))

    run_id = compute_run_id(snapshot_digest, config_digest, code_digest)
    snapshot_evidence = {
        "snapshot_id": snapshot_id,
        "manifest_sha256": snapshot_digest,
        "normalized_sha256": snapshot_view.digest,
    }
    project_root = config.output_root / config.project_id
    run_dir = project_root / run_id
    if run_dir.exists():
        try:
            validate_complete_run(
                run_dir,
                project_id=config.project_id,
                run_id=run_id,
                snapshot=snapshot_evidence,
                inputs=inputs,
                command=config.command,
                required_outputs=config.required_outputs,
            )
        except EvidenceError:
            return _attempt_result(
                repo_root=repo_root,
                project_id=config.project_id,
                status="failed",
                stage="evidence_finalization",
                code="completed_evidence_mismatch",
                message="existing complete run failed revalidation",
                run_id=run_id,
                stages=(*stages, StageRecord("evidence_finalization", "failed")),
            )
        complete_stages = tuple(StageRecord(name, "complete") for name in _COMPLETE_STAGE_NAMES)
        return RunResult(
            status="complete",
            project_id=config.project_id,
            run_id=run_id,
            run_path=run_dir,
            attempt_id=None,
            reused=True,
            reasons=(),
            stages=complete_stages,
        )

    project_root.mkdir(parents=True, exist_ok=True)
    attempt_id = uuid.uuid4().hex
    staging = project_root / f".{run_id}.{attempt_id}.tmp"
    execution_root = project_root / f".{run_id}.{attempt_id}.inputs"
    staging.mkdir()
    try:
        frozen = _freeze_execution_inputs(
            config=config,
            repo_root=repo_root,
            market_root=market_root,
            snapshot_document=snapshot_document,
            snapshot_digest=snapshot_digest,
            snapshot_normalized_digest=snapshot_view.digest,
            inputs=inputs,
            execution_root=execution_root,
        )
    except InputIntegrityError:
        return _attempt_result(
            repo_root=repo_root,
            project_id=config.project_id,
            status="failed",
            stage="project_execution",
            code="run_input_changed",
            message="run inputs could not be frozen for execution",
            run_id=run_id,
            stages=(*stages, StageRecord("project_execution", "failed")),
            staging=staging,
        )
    before_state = _repo_state(
        repo_root,
        ignored_roots=(staging, execution_root),
    )
    command = [
        str((repo_root / config.command[0]).resolve()),
        str(
            (
                repo_root
                / "scripts/research/local_quant_research/adapter_guard.py"
            ).resolve()
        ),
        "--staging-root",
        str(staging.resolve()),
        "--execution-root",
        str(frozen.root.resolve()),
        "--repository-root",
        str(repo_root),
        "--venv-root",
        str((repo_root / ".venv").resolve()),
        "--entry",
        str(frozen.project_entry.resolve()),
        "--",
        *config.command[2:],
        "--snapshot-manifest",
        str((frozen.market_data / "snapshots" / f"{snapshot_id}.json").resolve()),
        "--market-data-root",
        str(frozen.market_data.resolve()),
        "--project-config",
        str(frozen.project_config.resolve()),
        "--output-dir",
        str(staging.resolve()),
        "--run-id",
        run_id,
        "--snapshot-id",
        snapshot_id,
        "--code-sha256",
        code_digest,
        "--config-sha256",
        config_digest,
    ]
    completed = None
    try:
        completed = subprocess.run(
            command,
            cwd=staging,
            shell=False,
            env=_sanitized_environment(frozen.repository),
            capture_output=True,
            text=True,
            timeout=600,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        shutil.rmtree(execution_root)
    except OSError:
        return _attempt_result(
            repo_root=repo_root,
            project_id=config.project_id,
            status="failed",
            stage="project_execution",
            code="input_cleanup_failed",
            message="frozen run inputs could not be cleaned up",
            run_id=run_id,
            stages=(*stages, StageRecord("project_execution", "failed")),
            staging=staging,
        )
    if completed is None:
        return _attempt_result(
            repo_root=repo_root,
            project_id=config.project_id,
            status="failed",
            stage="project_execution",
            code="project_process_failed",
            message="project process could not complete",
            run_id=run_id,
            stages=(*stages, StageRecord("project_execution", "failed")),
            staging=staging,
        )
    after_state = _repo_state(
        repo_root,
        ignored_roots=(staging, execution_root),
    )
    if after_state != before_state:
        return _attempt_result(
            repo_root=repo_root,
            project_id=config.project_id,
            status="failed",
            stage="project_execution",
            code="write_outside_staging",
            message="project wrote outside staging",
            run_id=run_id,
            stages=(*stages, StageRecord("project_execution", "failed")),
            staging=staging,
        )
    if not _run_inputs_unchanged(
        config_path=config_path,
        repo_root=repo_root,
        market_root=market_root,
        snapshot_id=snapshot_id,
        snapshot_path=snapshot_path,
        snapshot_digest=snapshot_digest,
        snapshot_normalized_digest=snapshot_view.digest,
        config_digest=config_digest,
        code_digest=code_digest,
        inputs=inputs,
    ):
        return _attempt_result(
            repo_root=repo_root,
            project_id=config.project_id,
            status="failed",
            stage="project_execution",
            code="run_input_changed",
            message="run input changed during project execution",
            run_id=run_id,
            stages=(*stages, StageRecord("project_execution", "failed")),
            staging=staging,
        )
    if completed.returncode != 0:
        return _attempt_result(
            repo_root=repo_root,
            project_id=config.project_id,
            status="failed",
            stage="project_execution",
            code="project_process_failed",
            message="project process returned a non-zero exit code",
            run_id=run_id,
            stages=(*stages, StageRecord("project_execution", "failed")),
            staging=staging,
        )
    stages.append(StageRecord("project_execution", "complete"))

    try:
        project_status, reason_codes = _project_status(staging)
    except EvidenceError:
        return _attempt_result(
            repo_root=repo_root,
            project_id=config.project_id,
            status="failed",
            stage="output_validation",
            code="invalid_project_status",
            message="project status is missing, unknown or invalid",
            run_id=run_id,
            stages=(*stages, StageRecord("output_validation", "failed")),
            staging=staging,
        )
    if project_status != "complete":
        status = project_status if project_status in {"evidence_insufficient", "failed"} else "failed"
        code = reason_codes[0] if reason_codes else "project_reported_failure"
        return _attempt_result(
            repo_root=repo_root,
            project_id=config.project_id,
            status=status,
            stage="output_validation",
            code=code,
            message="project reported that research could not complete",
            run_id=run_id,
            stages=(*stages, StageRecord("output_validation", status)),
            staging=staging,
        )
    try:
        output_evidence = collect_output_evidence(staging, config.required_outputs)
        expected_files = {"project-status.json", *(spec.path for spec in config.required_outputs)}
        if _actual_staging_files(staging) != expected_files:
            raise EvidenceError("project output file set differs from its declaration")
    except EvidenceError:
        return _attempt_result(
            repo_root=repo_root,
            project_id=config.project_id,
            status="failed",
            stage="output_validation",
            code="output_validation_failed",
            message="required project outputs failed validation",
            run_id=run_id,
            stages=(*stages, StageRecord("output_validation", "failed")),
            staging=staging,
        )
    stages.append(StageRecord("output_validation", "complete"))

    if not _run_inputs_unchanged(
        config_path=config_path,
        repo_root=repo_root,
        market_root=market_root,
        snapshot_id=snapshot_id,
        snapshot_path=snapshot_path,
        snapshot_digest=snapshot_digest,
        snapshot_normalized_digest=snapshot_view.digest,
        config_digest=config_digest,
        code_digest=code_digest,
        inputs=inputs,
    ):
        return _attempt_result(
            repo_root=repo_root,
            project_id=config.project_id,
            status="failed",
            stage="evidence_finalization",
            code="run_input_changed",
            message="run input changed before evidence finalization",
            run_id=run_id,
            stages=(*stages, StageRecord("evidence_finalization", "failed")),
            staging=staging,
        )

    complete_stages = [
        {"name": name, "status": "complete"} for name in _COMPLETE_STAGE_NAMES
    ]
    manifest = {
        "schema_version": 1,
        "project_id": config.project_id,
        "run_id": run_id,
        "status": "complete",
        "snapshot": snapshot_evidence,
        "inputs": inputs,
        "command": list(config.command),
        "stages": complete_stages,
        "outputs": output_evidence,
        "output_set_sha256": canonical_digest(output_evidence),
    }
    published = False
    try:
        write_manifest(staging / "run-manifest.json", manifest)
        _publish_directory(staging, run_dir)
        published = True
        validate_complete_run(
            run_dir,
            project_id=config.project_id,
            run_id=run_id,
            snapshot=snapshot_evidence,
            inputs=inputs,
            command=config.command,
            required_outputs=config.required_outputs,
        )
    except (OSError, EvidenceError):
        if published and run_dir.exists():
            shutil.rmtree(run_dir)
        return _attempt_result(
            repo_root=repo_root,
            project_id=config.project_id,
            status="failed",
            stage="evidence_finalization",
            code="evidence_finalization_failed",
            message="complete evidence could not be atomically finalized",
            run_id=run_id,
            stages=(*stages, StageRecord("evidence_finalization", "failed")),
            staging=staging if staging.exists() else None,
        )
    return RunResult(
        status="complete",
        project_id=config.project_id,
        run_id=run_id,
        run_path=run_dir,
        attempt_id=None,
        reused=False,
        reasons=(),
        stages=tuple(StageRecord(name, "complete") for name in _COMPLETE_STAGE_NAMES),
    )
