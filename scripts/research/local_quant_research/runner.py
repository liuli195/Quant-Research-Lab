from __future__ import annotations

import json
import hashlib
import os
import re
import shutil
import stat
import subprocess
import time
import uuid
from pathlib import Path
from typing import Mapping, Sequence

from scripts.research.market_data.query import open_snapshot
from scripts.research.market_data.storage import MarketDataError

from .contracts import RunConfig, RunResult, StageRecord
from .evidence import (
    EvidenceError,
    canonical_digest,
    compute_run_id,
    file_digest,
    record_attempt,
    write_manifest,
)


_STOP_STATES = ("complete", "evidence_insufficient", "failed")
_PROJECT_ID_PATTERN = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")
_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")
_REASON_PATTERN = re.compile(r"[a-z][a-z0-9_]{0,63}")
_SENSITIVE_KEYS = ("password", "token", "cookie", "secret", "credential", "api_key")
_PROJECT_EXECUTION_TIMEOUT_SECONDS = 3_600
_COMPLETE_STAGE_NAMES = (
    "snapshot_validation",
    "config_validation",
    "project_execution",
    "output_validation",
    "evidence_finalization",
)
_FILE_ATTRIBUTE_REPARSE_POINT = 0x0400


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
    attempt_id = uuid.uuid4().hex
    final_status = status if status in {"evidence_insufficient", "failed"} else "failed"
    try:
        project_root = _resolve_output_project_root(repo_root, project_id)
        attempts_root = _resolve_plain_output_directory(
            project_root / ".attempts",
            repo_root=repo_root,
            allow_missing=True,
        )
        if staging is not None and staging.exists():
            safe_staging = _resolve_plain_output_directory(
                staging,
                repo_root=repo_root,
                allow_missing=False,
            )
            if safe_staging.parent != project_root:
                raise ConfigurationError(
                    "unsafe_output_root",
                    "staging is outside the fixed project output root",
                )
            shutil.rmtree(safe_staging)
        record_attempt(
            attempts_root=attempts_root,
            attempt_id=attempt_id,
            project_id=project_id,
            run_id=run_id,
            status=final_status,
            stage=stage,
            reason_codes=(code,),
        )
    except ConfigurationError:
        attempt_id = None
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
        "NUMBA_DISABLE_JIT",
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
    ignored_names = {
        ".git",
        ".local",
        ".venv",
        ".pytest_cache",
        ".ruff_cache",
        "__pycache__",
    }
    state: dict[str, tuple[int, int]] = {}
    ignored = tuple(path.resolve() for path in ignored_roots)
    for directory, names, files in os.walk(repo_root, followlinks=False):
        root = Path(directory)
        names[:] = [
            name
            for name in names
            if name not in ignored_names
            and not any(_inside(root / name, ignored_root) for ignored_root in ignored)
        ]
        for name in files:
            if name in ignored_names:
                continue
            path = root / name
            if path.is_symlink():
                continue
            stat = path.stat()
            state[path.relative_to(repo_root).as_posix()] = (stat.st_size, stat.st_mtime_ns)
    return state


_V2_CONFIG_FIELDS = {
    "schema_version",
    "project_id",
    "strategy",
    "snapshot_id",
    "snapshot_requirements",
    "scenario_config",
    "declared_inputs",
}
_LEGACY_RUN_FIELDS = {
    "command",
    "project_entry",
    "code_identity",
    "required_outputs",
    "output_root",
    "stop_states",
}


def _is_directory_link(path: Path) -> bool:
    try:
        details = os.lstat(path)
    except OSError:
        return False
    return (
        stat.S_ISLNK(details.st_mode)
        or bool(
            getattr(details, "st_file_attributes", 0)
            & _FILE_ATTRIBUTE_REPARSE_POINT
        )
        or bool(getattr(os.path, "isjunction", lambda _value: False)(path))
    )


def _resolve_plain_output_directory(
    path: Path,
    *,
    repo_root: Path,
    allow_missing: bool,
) -> Path:
    repository = Path(repo_root).resolve()
    unresolved = Path(os.path.abspath(path))
    try:
        relative = unresolved.relative_to(repository)
    except ValueError as exc:
        raise ConfigurationError(
            "unsafe_output_root",
            "output path must remain inside the repository",
        ) from exc
    current = repository
    for index, part in enumerate(relative.parts):
        current /= part
        final = index == len(relative.parts) - 1
        try:
            details = os.lstat(current)
        except FileNotFoundError:
            if allow_missing:
                continue
            raise ConfigurationError(
                "unsafe_output_root",
                "required output directory is missing",
            ) from None
        except OSError as exc:
            raise ConfigurationError(
                "unsafe_output_root",
                "output directory cannot be inspected",
            ) from exc
        if _is_directory_link(current) or not stat.S_ISDIR(details.st_mode):
            raise ConfigurationError(
                "unsafe_output_root",
                "output path must contain only ordinary directories",
            )
        if final and not stat.S_ISDIR(details.st_mode):
            raise ConfigurationError(
                "unsafe_output_root",
                "output path must be an ordinary directory",
            )
    return unresolved


def _resolve_output_project_root(repo_root: Path, project_id: str) -> Path:
    from .contracts import RUN_OUTPUT_ROOT

    repository = Path(repo_root).resolve()
    if project_id != "_invalid" and _PROJECT_ID_PATTERN.fullmatch(project_id) is None:
        raise ConfigurationError(
            "unsafe_output_root",
            "project output identity is unsafe",
        )
    output_root = _resolve_plain_output_directory(
        repository / RUN_OUTPUT_ROOT,
        repo_root=repository,
        allow_missing=True,
    )
    return _resolve_plain_output_directory(
        output_root / project_id,
        repo_root=repository,
        allow_missing=True,
    )


def _resolve_output_run_dir(
    repo_root: Path,
    project_id: str,
    run_id: str,
) -> Path:
    if _SHA256_PATTERN.fullmatch(run_id) is None:
        raise ConfigurationError("unsafe_output_root", "run output identity is unsafe")
    project_root = _resolve_output_project_root(repo_root, project_id)
    return _resolve_plain_output_directory(
        project_root / run_id,
        repo_root=repo_root,
        allow_missing=True,
    )


def load_run_config(path: Path, *, repo_root: Path) -> RunConfig:
    from .contracts import RUN_STATUSES

    repo_root = Path(repo_root).resolve()
    document = _load_raw_config(path, repo_root=repo_root)
    if _contains_sensitive_key(document):
        raise ConfigurationError("credential_field", "credential fields are forbidden")
    if set(document) & _LEGACY_RUN_FIELDS:
        raise ConfigurationError(
            "legacy_run_field",
            "legacy command and output fields are forbidden in configuration v2",
        )
    if set(document) != _V2_CONFIG_FIELDS or document.get("schema_version") != 2:
        raise ConfigurationError(
            "invalid_config",
            "configuration v2 fields are incomplete or unknown",
        )
    project_id = document["project_id"]
    if not isinstance(project_id, str) or _PROJECT_ID_PATTERN.fullmatch(project_id) is None:
        raise ConfigurationError("invalid_project_id", "project_id is invalid")
    snapshot_id = document["snapshot_id"]
    if not isinstance(snapshot_id, str) or _SHA256_PATTERN.fullmatch(snapshot_id) is None:
        raise ConfigurationError("missing_snapshot", "snapshot_id is missing or invalid")
    requirements = document["snapshot_requirements"]
    if not isinstance(requirements, Mapping) or _contains_sensitive_key(requirements):
        raise ConfigurationError(
            "missing_snapshot_requirements",
            "snapshot requirements are missing or invalid",
        )
    strategy = document["strategy"]
    if not isinstance(strategy, Mapping) or set(strategy) != {"root", "module", "symbol"}:
        raise ConfigurationError("invalid_strategy_fields", "strategy fields are invalid")
    strategy_root = _resolve_repo_path(
        strategy["root"], repo_root=repo_root, field="strategy.root"
    )
    if not strategy_root.is_dir():
        raise ConfigurationError("missing_strategy_root", "strategy root is missing")
    strategy_module = strategy["module"]
    strategy_symbol = strategy["symbol"]
    if not isinstance(strategy_module, str) or not strategy_module:
        raise ConfigurationError("invalid_strategy_module", "strategy module is invalid")
    if not isinstance(strategy_symbol, str) or not strategy_symbol:
        raise ConfigurationError("invalid_strategy_symbol", "strategy symbol is invalid")
    scenario_config = _resolve_repo_path(
        document["scenario_config"],
        repo_root=repo_root,
        field="scenario_config",
    )
    if not scenario_config.is_file():
        raise ConfigurationError("missing_scenario_config", "scenario config is missing")
    try:
        scenario_document = json.loads(scenario_config.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ConfigurationError(
            "invalid_scenario_config",
            "scenario config is not valid UTF-8 JSON",
        ) from exc
    if not isinstance(scenario_document, Mapping) or _contains_sensitive_key(
        scenario_document
    ):
        raise ConfigurationError("invalid_scenario_config", "scenario config is invalid")
    scenario_id = scenario_document.get("scenario_id")
    if not isinstance(scenario_id, str) or not scenario_id.strip():
        raise ConfigurationError(
            "missing_scenario_id",
            "scenario_id is missing or invalid",
        )
    declared = document["declared_inputs"]
    if not isinstance(declared, list) or any(not isinstance(item, str) for item in declared):
        raise ConfigurationError("invalid_inputs", "declared_inputs must be a path array")
    declared_inputs = tuple(
        _resolve_repo_path(item, repo_root=repo_root, field="declared_inputs")
        for item in declared
    )
    if len(declared_inputs) != len(set(declared_inputs)):
        raise ConfigurationError("invalid_inputs", "declared_inputs must be unique")
    if any(not item.is_file() for item in declared_inputs):
        raise ConfigurationError("missing_declared_input", "a declared input is missing")
    if tuple(RUN_STATUSES) != _STOP_STATES:
        raise RuntimeError("shared run statuses are inconsistent")
    return RunConfig(
        project_id=project_id,
        strategy_root=strategy_root,
        strategy_module=strategy_module,
        strategy_symbol=strategy_symbol,
        snapshot_id=snapshot_id,
        snapshot_requirements=dict(requirements),
        scenario_config=scenario_config,
        declared_inputs=declared_inputs,
        document=document,
    )


def _execute_command(
    *,
    repo_root: Path,
    execution_root: Path,
    staging: Path,
) -> tuple[Path | str, ...]:
    return (
        Path(repo_root) / ".venv/Scripts/python.exe",
        Path(repo_root) / "scripts/research/local_quant_research/cli.py",
        "_execute",
        "--frozen-inputs",
        Path(execution_root) / "request.json",
        "--staging",
        Path(staging),
    )


def _runtime_lock(repo_root: Path) -> tuple[dict[str, object], tuple[Path, ...]]:
    import importlib.metadata
    import platform
    import sys

    source_roots = (
        repo_root / "scripts/research/local_quant_research",
        repo_root / "scripts/research/market_data",
    )
    sources = tuple(
        sorted(
            (
                path
                for path in (
                    repo_root / "scripts/__init__.py",
                    repo_root / "scripts/research/__init__.py",
                    *(path for root in source_roots for path in root.glob("*.py")),
                )
                if path.is_file()
            ),
            key=lambda path: path.relative_to(repo_root).as_posix(),
        )
    )
    dependencies = {}
    for name in ("duckdb", "numba", "numpy", "pandas", "pyarrow", "vectorbt"):
        try:
            dependencies[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError as exc:
            raise ConfigurationError(
                "missing_runtime_dependency",
                f"required dependency is missing: {name}",
            ) from exc
    return (
        {
            "schema_version": 1,
            "python": sys.version.split()[0],
            "platform": platform.system(),
            "dependencies": dependencies,
        },
        sources,
    )


def _v2_identity(
    config: RunConfig,
    *,
    repo_root: Path,
    config_path: Path,
) -> tuple[tuple[Path, ...], str, str, dict[str, object], dict[str, object], dict[str, object]]:
    from .strategy_loader import ConfigurationError as StrategyConfigurationError
    from .strategy_loader import discover_strategy_sources

    try:
        strategy_sources = discover_strategy_sources(
            config.strategy_root,
            config.strategy_module,
        )
    except StrategyConfigurationError as exc:
        raise ConfigurationError(exc.code, str(exc)) from exc
    runtime_lock, runtime_sources = _runtime_lock(repo_root)
    code_sources = tuple(
        sorted(
            {*strategy_sources, *runtime_sources},
            key=lambda item: item.relative_to(repo_root).as_posix(),
        )
    )
    code_files = [
            {
                "path": source.relative_to(repo_root).as_posix(),
                "sha256": file_digest(source),
            }
            for source in code_sources
        ]
    declared = [
        {
            "path": item.relative_to(repo_root).as_posix(),
            "sha256": file_digest(item),
        }
        for item in config.declared_inputs
    ]
    try:
        scenario_payload = config.scenario_config.read_bytes()
        scenario_document = json.loads(scenario_payload.decode("utf-8"))
        project_run_payload = Path(config_path).resolve().read_bytes()
        project_run_document = json.loads(project_run_payload.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ConfigurationError(
            "run_input_changed",
            "configuration inputs changed during identity capture",
        ) from exc
    if (
        not isinstance(scenario_document, Mapping)
        or project_run_document != dict(config.document)
    ):
        raise ConfigurationError(
            "run_input_changed",
            "configuration inputs changed during identity capture",
        )
    scenario = {
        "path": config.scenario_config.relative_to(repo_root).as_posix(),
        "sha256": hashlib.sha256(scenario_payload).hexdigest(),
    }
    project_run_source = {
        "path": Path(config_path).resolve().relative_to(repo_root).as_posix(),
        "sha256": hashlib.sha256(project_run_payload).hexdigest(),
    }
    code_identity = {
        "schema_version": 1,
        "files": code_files,
        "inputs": {
            "project_run": project_run_source,
            "scenario": scenario,
            "declared_inputs": declared,
        },
    }
    config_identity = {
        "project_run": dict(config.document),
        "scenario": dict(scenario_document),
        "declared_inputs": declared,
    }
    config_digest = canonical_digest(config_identity)
    code_digest = canonical_digest(
        {
            "code_identity": {"schema_version": 1, "files": code_files},
            "runtime_lock": runtime_lock,
        }
    )
    evidence = {
        "config_sha256": config_digest,
        "code_sha256": code_digest,
        "project_run_source": project_run_source,
        "scenario": scenario,
        "declared_inputs": declared,
        "code_identity": code_identity,
        "runtime_lock": runtime_lock,
        "project_run": dict(config.document),
        "scenario_document": dict(scenario_document),
    }
    return strategy_sources, config_digest, code_digest, evidence, code_identity, runtime_lock


def _copy_v2_inputs(
    *,
    config_path: Path,
    config: RunConfig,
    repo_root: Path,
    market_root: Path,
    snapshot_document: Mapping[str, object],
    snapshot_digest: str,
    identity: Mapping[str, object],
    code_identity: Mapping[str, object],
    runtime_lock: Mapping[str, object],
    execution_root: Path,
    staging: Path,
    attempt_id: str,
) -> None:
    frozen_repo = execution_root / "repository"
    frozen_market = execution_root / "market-data"
    try:
        execution_root.mkdir()
        expected_files: dict[Path, str] = {}
        for item in (
            identity["project_run_source"],
            identity["scenario"],
            *identity["declared_inputs"],
            *code_identity["files"],
        ):
            if not isinstance(item, Mapping):
                raise InputIntegrityError(
                    "run_input_changed",
                    "captured run identity is invalid",
                )
            relative = Path(str(item.get("path", "")))
            expected = str(item.get("sha256", ""))
            source = (repo_root / relative).resolve()
            if not _inside(source, repo_root) or _SHA256_PATTERN.fullmatch(expected) is None:
                raise InputIntegrityError(
                    "run_input_changed",
                    "captured run identity is invalid",
                )
            previous = expected_files.setdefault(source, expected)
            if previous != expected:
                raise InputIntegrityError(
                    "run_input_changed",
                    "captured run identity is inconsistent",
                )
        for source, expected in sorted(
            expected_files.items(),
            key=lambda item: item[0].relative_to(repo_root).as_posix(),
        ):
            relative = source.relative_to(repo_root)
            _copy_verified_file(source, frozen_repo / relative, expected)
        snapshot_path = market_root / "snapshots" / f"{config.snapshot_id}.json"
        _copy_verified_file(
            snapshot_path,
            frozen_market / "snapshots" / snapshot_path.name,
            snapshot_digest,
        )
        for batch in snapshot_document["batches"]:
            batch_id = str(batch["batch_id"])
            for name, digest_field in (
                ("manifest.json", "manifest_sha256"),
                ("market-data.parquet", "parquet_sha256"),
                ("corporate-actions.parquet", "corporate_actions_sha256"),
                ("validation.json", "validation_sha256"),
            ):
                _copy_verified_file(
                    market_root / "batches" / batch_id / name,
                    frozen_market / "batches" / batch_id / name,
                    str(batch[digest_field]),
                )
        request = {
            "schema_version": 2,
            "project_id": str(identity["project_id"]),
            "run_id": str(identity["run_id"]),
            "attempt_id": attempt_id,
            "output_root": str((repo_root / ".local/quant-research").resolve()),
            "repository": str(frozen_repo.resolve()),
            "market_data": str(frozen_market.resolve()),
            "live_repository": str(repo_root.resolve()),
            "runtime_cache": str((execution_root / "runtime-cache").resolve()),
            "staging": str(Path(staging).resolve()),
            "config": config_path.resolve().relative_to(repo_root).as_posix(),
            "code_identity": dict(code_identity),
            "runtime_lock": dict(runtime_lock),
            "market_snapshot": dict(snapshot_document),
            "environment": {
                "schema_version": 1,
                "python": runtime_lock["python"],
                "platform": runtime_lock["platform"],
            },
        }
        write_manifest(execution_root / "request.json", request)
        frozen = open_snapshot(config.snapshot_id, root=frozen_market)
        if frozen.digest != identity["snapshot_normalized_sha256"]:
            raise InputIntegrityError(
                "run_input_changed",
                "frozen snapshot differs from the run identity",
            )
    except (KeyError, OSError, MarketDataError, ValueError, InputIntegrityError) as exc:
        shutil.rmtree(execution_root, ignore_errors=True)
        if isinstance(exc, InputIntegrityError):
            raise
        raise InputIntegrityError(
            "run_input_changed",
            "run inputs could not be frozen",
        ) from exc


def _v2_environment(repo_root: Path, execution_root: Path) -> dict[str, str]:
    environment = _sanitized_environment(repo_root)
    cache = execution_root / "runtime-cache"
    cache.mkdir()
    environment["NUMBA_CACHE_DIR"] = str(cache)
    environment["MPLCONFIGDIR"] = str(cache / "matplotlib")
    environment["XDG_CACHE_HOME"] = str(cache)
    environment["TEMP"] = str(cache)
    environment["TMP"] = str(cache)
    return environment


def _package_identity(
    path: Path,
    *,
    expected: Mapping[str, object],
) -> Mapping[str, object]:
    from .result_package import ResultContractError, validate_result_package

    try:
        document = validate_result_package(path)
    except ResultContractError as exc:
        raise EvidenceError("completed result package is invalid") from exc
    identity = document.get("object")
    project_id = expected.get("project_id")
    run_id = expected.get("run_id")
    expected_scenario = expected.get("scenario_document")
    expected_scenario_id = (
        expected_scenario.get("scenario_id")
        if isinstance(expected_scenario, Mapping)
        else None
    )
    if not isinstance(identity, Mapping) or (
        identity.get("kind") != "local_research"
        or identity.get("status") != "complete"
        or identity.get("strategy_id") != project_id
        or identity.get("scenario_id") != expected_scenario_id
        or identity.get("run_id") != run_id
    ):
        raise EvidenceError("completed result package identity is invalid")
    package_root = Path(path)
    package_documents: dict[str, object] = {}
    for name, relative in (
        ("project_run", "config/project-run.json"),
        ("scenario_document", "config/scenario.json"),
        ("code_identity", "config/code-identity.json"),
        ("market_snapshot", "evidence/market-snapshot.json"),
        ("runtime_lock", "evidence/runtime-lock.json"),
    ):
        try:
            package_documents[name] = json.loads(
                (package_root / relative).read_text(encoding="utf-8")
            )
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise EvidenceError("completed result package identity is invalid") from exc
        if package_documents[name] != expected.get(name):
            raise EvidenceError("completed result package identity is invalid")
    code_identity = package_documents["code_identity"]
    if not isinstance(code_identity, Mapping):
        raise EvidenceError("completed result package identity is invalid")
    inputs = code_identity.get("inputs")
    if not isinstance(inputs, Mapping) or not isinstance(
        inputs.get("declared_inputs"), list
    ):
        raise EvidenceError("completed result package identity is invalid")
    config_digest = canonical_digest(
        {
            "project_run": package_documents["project_run"],
            "scenario": package_documents["scenario_document"],
            "declared_inputs": inputs["declared_inputs"],
        }
    )
    code_digest = canonical_digest(
        {
            "code_identity": {
                "schema_version": code_identity.get("schema_version"),
                "files": code_identity.get("files"),
            },
            "runtime_lock": package_documents["runtime_lock"],
        }
    )
    recalculated_run_id = compute_run_id(
        canonical_digest(package_documents["market_snapshot"]),
        config_digest,
        code_digest,
    )
    if (
        config_digest != expected.get("config_sha256")
        or code_digest != expected.get("code_sha256")
        or recalculated_run_id != run_id
    ):
        raise EvidenceError("completed result package identity is invalid")
    return document


def _child_result(completed: subprocess.CompletedProcess[str]) -> tuple[str, str]:
    try:
        document = json.loads(completed.stdout)
    except (TypeError, json.JSONDecodeError):
        return "failed", "project_process_failed"
    if not isinstance(document, Mapping):
        return "failed", "project_process_failed"
    status = document.get("status")
    reasons = document.get("reasons")
    code = reasons[0] if isinstance(reasons, list) and reasons else "project_process_failed"
    if not isinstance(code, str) or _REASON_PATTERN.fullmatch(code) is None:
        code = "project_process_failed"
    if status not in {"complete", "evidence_insufficient", "failed"}:
        status = "failed"
    return str(status), code


def _v2_inputs_unchanged(
    *,
    config_path: Path,
    repo_root: Path,
    market_root: Path,
    config_digest: str,
    code_digest: str,
    snapshot_digest: str,
    snapshot_normalized_digest: str,
) -> bool:
    try:
        current_config = load_run_config(config_path, repo_root=repo_root)
        _, current_config_digest, current_code_digest, _, _, _ = _v2_identity(
            current_config,
            repo_root=repo_root,
            config_path=config_path,
        )
        current_snapshot_path = (
            market_root / "snapshots" / f"{current_config.snapshot_id}.json"
        )
        current_snapshot = open_snapshot(
            current_config.snapshot_id,
            root=market_root,
        )
    except (ConfigurationError, MarketDataError, OSError):
        return False
    return (
        current_config_digest == config_digest
        and current_code_digest == code_digest
        and file_digest(current_snapshot_path) == snapshot_digest
        and current_snapshot.digest == snapshot_normalized_digest
    )


def execute_frozen_inputs(frozen_inputs: Path, staging: Path) -> dict[str, object]:
    from .scenario import ScenarioRequest, execute_scenario
    from .strategy_loader import load_strategy

    request_path = Path(frozen_inputs).resolve()
    execution_root = request_path.parent
    try:
        request = json.loads(request_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ConfigurationError("invalid_frozen_inputs", "frozen inputs are invalid") from exc
    if not isinstance(request, Mapping) or request.get("schema_version") != 2:
        raise ConfigurationError("invalid_frozen_inputs", "frozen inputs are invalid")
    repository = Path(str(request.get("repository", ""))).resolve()
    market_data = Path(str(request.get("market_data", ""))).resolve()
    runtime_cache = Path(str(request.get("runtime_cache", ""))).resolve()
    expected_staging = Path(str(request.get("staging", ""))).resolve()
    if Path(staging).resolve() != expected_staging:
        raise ConfigurationError("staging_mismatch", "staging does not match frozen inputs")
    if (
        repository != (execution_root / "repository").resolve()
        or market_data != (execution_root / "market-data").resolve()
        or runtime_cache != (execution_root / "runtime-cache").resolve()
    ):
        raise ConfigurationError("unsafe_frozen_inputs", "frozen input roots are unsafe")
    config_path = repository / str(request.get("config", ""))
    config = load_run_config(config_path, repo_root=repository)
    started = time.perf_counter()
    loaded = load_strategy(repository, config.document["strategy"])
    if loaded.descriptor.strategy_id != config.project_id:
        raise ConfigurationError(
            "strategy_identity_mismatch",
            "project_id must match the loaded strategy descriptor",
        )
    strategy_load_seconds = time.perf_counter() - started
    snapshot = open_snapshot(config.snapshot_id, root=market_data)
    try:
        scenario_document = json.loads(config.scenario_config.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ConfigurationError("invalid_scenario_config", "scenario config is invalid") from exc
    if not isinstance(scenario_document, Mapping):
        raise ConfigurationError("invalid_scenario_config", "scenario config is invalid")
    outcome = execute_scenario(
        ScenarioRequest(
            loaded_strategy=loaded,
            snapshot=snapshot,
            scenario=scenario_document,
            project_document=config.document,
            run_id=str(request["run_id"]),
            output_dir=Path(staging),
            code_identity=request["code_identity"],
            market_snapshot=request["market_snapshot"],
            runtime_lock=request["runtime_lock"],
            environment=request["environment"],
            strategy_load_seconds=strategy_load_seconds,
        )
    )
    return {
        "status": "complete",
        "reasons": [],
        "package_sha256": outcome.package.package_sha256,
    }


def run_project(config_path: Path, *, repo_root: Path) -> RunResult:
    repo_root = Path(repo_root).resolve()
    stages: list[StageRecord] = []
    try:
        raw = _load_raw_config(config_path, repo_root=repo_root)
        config = load_run_config(config_path, repo_root=repo_root)
    except ConfigurationError as exc:
        project_id = "_invalid"
        try:
            project_id = _safe_project_id(raw)  # type: ignore[possibly-undefined]
        except UnboundLocalError:
            pass
        return _attempt_result(
            repo_root=repo_root,
            project_id=project_id,
            status="evidence_insufficient",
            stage="config_validation",
            code=exc.code,
            message=str(exc),
            run_id=None,
            stages=(StageRecord("config_validation", "evidence_insufficient"),),
        )
    project_id = config.project_id
    market_root = repo_root / ".local/market-data"
    snapshot_path = market_root / "snapshots" / f"{config.snapshot_id}.json"
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
        snapshot = open_snapshot(config.snapshot_id, root=market_root)
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
    if not _snapshot_requirements_match(
        snapshot_document.get("selection"), config.snapshot_requirements
    ):
        return _attempt_result(
            repo_root=repo_root,
            project_id=project_id,
            status="evidence_insufficient",
            stage="snapshot_validation",
            code="snapshot_requirements_unmet",
            message="snapshot does not exactly cover the declared requirements",
            run_id=None,
            stages=(StageRecord("snapshot_validation", "evidence_insufficient"),),
        )
    stages.append(StageRecord("snapshot_validation", "complete"))
    try:
        _, config_digest, code_digest, inputs, code_identity, runtime_lock = (
            _v2_identity(
                config,
                repo_root=repo_root,
                config_path=Path(config_path).resolve(),
            )
        )
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
    stages.append(StageRecord("config_validation", "complete"))
    snapshot_identity_digest = canonical_digest(snapshot_document)
    run_id = compute_run_id(snapshot_identity_digest, config_digest, code_digest)
    identity = {
        **inputs,
        "project_id": project_id,
        "run_id": run_id,
        "market_snapshot": dict(snapshot_document),
        "snapshot_manifest_sha256": snapshot_digest,
        "snapshot_identity_sha256": snapshot_identity_digest,
        "snapshot_normalized_sha256": snapshot.digest,
    }
    try:
        project_root = _resolve_output_project_root(repo_root, project_id)
    except ConfigurationError as exc:
        return RunResult(
            status="evidence_insufficient",
            project_id=project_id,
            run_id=None,
            run_path=None,
            attempt_id=None,
            reused=False,
            reasons=(exc.code,),
            stages=(StageRecord("config_validation", "evidence_insufficient"),),
            next_action=None,
        )
    try:
        run_dir = _resolve_output_run_dir(repo_root, project_id, run_id)
    except ConfigurationError as exc:
        return RunResult(
            status="evidence_insufficient",
            project_id=project_id,
            run_id=run_id,
            run_path=None,
            attempt_id=None,
            reused=False,
            reasons=(exc.code,),
            stages=(*stages, StageRecord("config_validation", "evidence_insufficient")),
            next_action=None,
        )
    if run_dir.exists():
        try:
            _package_identity(run_dir, expected=identity)
        except EvidenceError:
            return _attempt_result(
                repo_root=repo_root,
                project_id=project_id,
                status="failed",
                stage="evidence_finalization",
                code="completed_evidence_mismatch",
                message="existing complete run failed revalidation",
                run_id=run_id,
                stages=(*stages, StageRecord("evidence_finalization", "failed")),
            )
        return RunResult(
            "complete",
            project_id,
            run_id,
            run_dir,
            None,
            True,
            (),
            tuple(StageRecord(name, "complete") for name in _COMPLETE_STAGE_NAMES),
            "return_to_caller",
        )
    project_root.mkdir(parents=True, exist_ok=True)
    attempt_id = uuid.uuid4().hex
    staging = project_root / f".{run_id}.{attempt_id}.tmp"
    execution_root = project_root / f".{run_id}.{attempt_id}.inputs"
    try:
        _copy_v2_inputs(
            config_path=Path(config_path).resolve(),
            config=config,
            repo_root=repo_root,
            market_root=market_root,
            snapshot_document=snapshot_document,
            snapshot_digest=snapshot_digest,
            identity=identity,
            code_identity=code_identity,
            runtime_lock=runtime_lock,
            execution_root=execution_root,
            staging=staging,
            attempt_id=attempt_id,
        )
    except InputIntegrityError as exc:
        return _attempt_result(
            repo_root=repo_root,
            project_id=project_id,
            status="failed",
            stage="project_execution",
            code=exc.code,
            message=str(exc),
            run_id=run_id,
            stages=(*stages, StageRecord("project_execution", "failed")),
            staging=staging,
        )
    before_state = _repo_state(repo_root, ignored_roots=(staging, execution_root))
    command = _execute_command(
        repo_root=repo_root,
        execution_root=execution_root,
        staging=staging,
    )
    completed: subprocess.CompletedProcess[str] | None = None
    try:
        completed = subprocess.run(
            command,
            cwd=repo_root,
            shell=False,
            env=_v2_environment(repo_root, execution_root),
            capture_output=True,
            text=True,
            timeout=_PROJECT_EXECUTION_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        shutil.rmtree(execution_root)
    except OSError:
        return _attempt_result(
            repo_root=repo_root,
            project_id=project_id,
            status="failed",
            stage="project_execution",
            code="input_cleanup_failed",
            message="frozen run inputs could not be cleaned up",
            run_id=run_id,
            stages=(*stages, StageRecord("project_execution", "failed")),
            staging=staging,
        )
    after_state = _repo_state(repo_root, ignored_roots=(staging, execution_root))
    if after_state != before_state:
        return _attempt_result(
            repo_root=repo_root,
            project_id=project_id,
            status="failed",
            stage="project_execution",
            code="write_outside_staging",
            message="project wrote outside staging",
            run_id=run_id,
            stages=(*stages, StageRecord("project_execution", "failed")),
            staging=staging,
        )
    if not _v2_inputs_unchanged(
        config_path=Path(config_path).resolve(),
        repo_root=repo_root,
        market_root=market_root,
        config_digest=config_digest,
        code_digest=code_digest,
        snapshot_digest=snapshot_digest,
        snapshot_normalized_digest=snapshot.digest,
    ):
        return _attempt_result(
            repo_root=repo_root,
            project_id=project_id,
            status="failed",
            stage="project_execution",
            code="run_input_changed",
            message="run input changed during shared scenario execution",
            run_id=run_id,
            stages=(*stages, StageRecord("project_execution", "failed")),
            staging=staging,
        )
    if completed is None:
        return _attempt_result(
            repo_root=repo_root,
            project_id=project_id,
            status="failed",
            stage="project_execution",
            code="project_process_failed",
            message="project process could not complete",
            run_id=run_id,
            stages=(*stages, StageRecord("project_execution", "failed")),
            staging=staging,
        )
    child_status, child_code = _child_result(completed)
    if completed.returncode != 0 or child_status != "complete":
        status = child_status if child_status == "evidence_insufficient" else "failed"
        return _attempt_result(
            repo_root=repo_root,
            project_id=project_id,
            status=status,
            stage="project_execution",
            code=child_code,
            message=child_code,
            run_id=run_id,
            stages=(*stages, StageRecord("project_execution", status)),
            staging=staging,
        )
    stages.append(StageRecord("project_execution", "complete"))
    try:
        _package_identity(staging, expected=identity)
    except EvidenceError:
        return _attempt_result(
            repo_root=repo_root,
            project_id=project_id,
            status="failed",
            stage="output_validation",
            code="output_validation_failed",
            message="archive-ready result package failed validation",
            run_id=run_id,
            stages=(*stages, StageRecord("output_validation", "failed")),
            staging=staging,
        )
    stages.append(StageRecord("output_validation", "complete"))
    published = False
    try:
        _publish_directory(staging, run_dir)
        published = True
        _package_identity(run_dir, expected=identity)
    except (OSError, EvidenceError):
        if published:
            shutil.rmtree(run_dir, ignore_errors=True)
        return _attempt_result(
            repo_root=repo_root,
            project_id=project_id,
            status="failed",
            stage="evidence_finalization",
            code="evidence_finalization_failed",
            message="complete evidence could not be atomically finalized",
            run_id=run_id,
            stages=(*stages, StageRecord("evidence_finalization", "failed")),
            staging=staging if staging.exists() else None,
        )
    return RunResult(
        "complete",
        project_id,
        run_id,
        run_dir,
        None,
        False,
        (),
        tuple(StageRecord(name, "complete") for name in _COMPLETE_STAGE_NAMES),
        "return_to_caller",
    )
