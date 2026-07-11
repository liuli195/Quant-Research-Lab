from __future__ import annotations

import concurrent.futures
import dataclasses
import fnmatch
import hashlib
import importlib.util
import json
import os
import platform
import shlex
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any
from collections.abc import Callable


FRAMEWORK_VERSION = "0.1.0"
CACHE_VERSION = "1"
DEFAULT_CHECK_TIMEOUT_SECONDS = 300
PYTEST_XDIST_AUTO_WORKERS_ENV = "PYTEST_XDIST_AUTO_NUM_WORKERS"
DEFAULT_PYTEST_XDIST_AUTO_NUM_WORKERS = "4"
Runner = Callable[..., subprocess.CompletedProcess[Any]]


class ConfigError(Exception):
    pass


@dataclasses.dataclass
class CheckResult:
    index: int
    check: dict[str, Any]
    returncode: int
    stdout: str = ""
    stderr: str = ""
    duration_seconds: float = 0.0
    cache_key: str | None = None


def _is_non_empty_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _is_non_empty_string_list(value: Any) -> bool:
    return isinstance(value, list) and all(
        _is_non_empty_string(item) for item in value
    )


def _command_tokens(command: Any) -> list[str]:
    if isinstance(command, str):
        try:
            return shlex.split(command)
        except ValueError:
            return command.split()
    if isinstance(command, list):
        return [str(item) for item in command]
    return []


def _uses_pytest(command: Any) -> bool:
    tokens = _command_tokens(command)
    has_pytest = any(token == "pytest" or token.endswith("/pytest") or token.endswith("\\pytest") for token in tokens)
    has_pytest_module = any(
        token == "-m" and index + 1 < len(tokens) and tokens[index + 1] == "pytest"
        for index, token in enumerate(tokens)
    )
    return has_pytest or has_pytest_module


def uses_pytest_xdist(command: Any) -> bool:
    tokens = _command_tokens(command)
    has_xdist_flag = any(
        token == "-n"
        or (token.startswith("-n") and len(token) > 2)
        or token == "--numprocesses"
        or token.startswith("--numprocesses=")
        for token in tokens
    )
    return has_xdist_flag and _uses_pytest(command)


def _pytest_xdist_workers(value: Any) -> str | None:
    if value is None:
        return None
    if value == "auto":
        return "auto"
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ConfigError('pytestXdistWorkers must be "auto" or positive integer')
    return str(value)


def _command_with_pytest_xdist_workers(command: Any, workers: str | None) -> Any:
    if workers is None or not _uses_pytest(command) or uses_pytest_xdist(command):
        return command
    if isinstance(command, list):
        tokens = [str(item) for item in command]
        for index, token in enumerate(tokens):
            if token == "pytest" or token.endswith("/pytest") or token.endswith("\\pytest"):
                return tokens[: index + 1] + ["-n", workers] + tokens[index + 1 :]
            if token == "-m" and index + 1 < len(tokens) and tokens[index + 1] == "pytest":
                insert_at = index + 2
                return tokens[:insert_at] + ["-n", workers] + tokens[insert_at:]
    if isinstance(command, str):
        tokens = _command_tokens(command)
        for index, token in enumerate(tokens):
            if token == "pytest" or token.endswith("/pytest") or token.endswith("\\pytest"):
                tokens = tokens[: index + 1] + ["-n", workers] + tokens[index + 1 :]
                return shlex.join(tokens)
            if token == "-m" and index + 1 < len(tokens) and tokens[index + 1] == "pytest":
                insert_at = index + 2
                tokens = tokens[:insert_at] + ["-n", workers] + tokens[insert_at:]
                return shlex.join(tokens)
    return command


def _pytest_xdist_env(workers: str | None) -> dict[str, str] | None:
    if workers != "auto" or os.environ.get(PYTEST_XDIST_AUTO_WORKERS_ENV):
        return None
    env = os.environ.copy()
    env[PYTEST_XDIST_AUTO_WORKERS_ENV] = DEFAULT_PYTEST_XDIST_AUTO_NUM_WORKERS
    return env


def _dependency_error(check: dict[str, Any]) -> str | None:
    command = check.get("command")
    workers = _pytest_xdist_workers(check.get("pytestXdistWorkers"))
    needs_xdist = uses_pytest_xdist(command) or (workers is not None and _uses_pytest(command))
    if needs_xdist and importlib.util.find_spec("xdist") is None:
        return (
            f"missing_dependency: {check.get('id')}: pytest-xdist is required "
            "for pytest xdist workers; install requirements-dev.txt\n"
        )
    return None


def _load_config(project: Path) -> dict[str, Any]:
    config_path = project / ".build-and-verify" / "config.json"
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ConfigError("missing_config: .build-and-verify/config.json") from None
    except json.JSONDecodeError as error:
        raise ConfigError(
            f"invalid_config: .build-and-verify/config.json: {error.msg}"
        ) from None
    if not isinstance(config, dict):
        raise ConfigError(
            "invalid_config: .build-and-verify/config.json: root must be object"
        )
    for section in ("build", "verify"):
        section_config = config.get(section, {})
        if not isinstance(section_config, dict):
            raise ConfigError(
                f"invalid_config: .build-and-verify/config.json: {section} must be object"
            )
        if section == "verify":
            max_parallel = section_config.get("maxParallel")
            if max_parallel is not None and (
                isinstance(max_parallel, bool)
                or not isinstance(max_parallel, int)
                or max_parallel < 0
            ):
                raise ConfigError(
                    "invalid_config: .build-and-verify/config.json: "
                    "verify.maxParallel must be non-negative integer"
                )
        checks = section_config.get("checks", [])
        if not isinstance(checks, list):
            raise ConfigError(
                f"invalid_config: .build-and-verify/config.json: {section}.checks must be list"
            )
        seen_ids: set[str] = set()
        for index, check in enumerate(checks):
            if not isinstance(check, dict):
                raise ConfigError(
                    "invalid_config: .build-and-verify/config.json: "
                    f"{section}.checks[{index}] must be object"
                )
            check_id = check.get("id")
            if not _is_non_empty_string(check_id):
                raise ConfigError(
                    "invalid_config: .build-and-verify/config.json: "
                    f"{section}.checks[{index}].id must be non-empty string"
                )
            if check_id in seen_ids:
                raise ConfigError(
                    "invalid_config: .build-and-verify/config.json: "
                    f"{section}.checks[{index}].id must be unique"
                )
            seen_ids.add(check_id)
            command = check.get("command")
            if not (
                _is_non_empty_string(command) or _is_non_empty_string_list(command)
            ):
                raise ConfigError(
                    "invalid_config: .build-and-verify/config.json: "
                    f"{section}.checks[{index}].command must be non-empty string or list of non-empty strings"
                )
            if "parallel" in check:
                raise ConfigError(
                    "invalid_config: .build-and-verify/config.json: "
                    f"{section}.checks[{index}].parallel is no longer supported; use checkParallel"
                )
            if "checkParallel" in check and not isinstance(check.get("checkParallel"), bool):
                raise ConfigError(
                    "invalid_config: .build-and-verify/config.json: "
                    f"{section}.checks[{index}].checkParallel must be boolean"
                )
            try:
                workers = _pytest_xdist_workers(check.get("pytestXdistWorkers"))
            except ConfigError as error:
                raise ConfigError(
                    "invalid_config: .build-and-verify/config.json: "
                    f"{section}.checks[{index}].{error}"
                ) from None
            if workers is not None and not _uses_pytest(command):
                raise ConfigError(
                    "invalid_config: .build-and-verify/config.json: "
                    f"{section}.checks[{index}].pytestXdistWorkers requires pytest command"
                )
            for field in ("paths", "inputs"):
                value = check.get(field)
                if value is not None and not _is_non_empty_string_list(value):
                    raise ConfigError(
                        "invalid_config: .build-and-verify/config.json: "
                        f"{section}.checks[{index}].{field} must be list of non-empty strings"
                    )
    return config


def _normalize_path(path: str | Path) -> str:
    return Path(path).as_posix().strip("/")


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    deduped: list[str] = []
    for item in items:
        normalized = _normalize_path(item)
        if normalized and normalized not in seen:
            seen.add(normalized)
            deduped.append(normalized)
    return deduped


def _git_names(project: Path, *args: str) -> list[str] | None:
    result = subprocess.run(
        ["git", *args],
        cwd=project,
        check=False,
        text=True,
        capture_output=True,
    )
    if result.returncode != 0:
        return None
    return result.stdout.splitlines()


def _git_status_names(project: Path) -> list[str] | None:
    result = subprocess.run(
        ["git", "status", "--porcelain=v1", "-z", "--untracked-files=all"],
        cwd=project,
        check=False,
        text=True,
        capture_output=True,
    )
    if result.returncode != 0:
        return None
    entries = result.stdout.split("\0")
    names: list[str] = []
    index = 0
    while index < len(entries):
        entry = entries[index]
        index += 1
        if not entry:
            continue
        status = entry[:2]
        path = entry[3:]
        if status[0] in {"R", "C"}:
            # In porcelain v1 -z mode, rename/copy entries are destination then source.
            if path:
                names.append(path)
            index += 1
            continue
        if path:
            names.append(path)
    return names


def _is_excluded_relative(relative: str) -> bool:
    parts = relative.split("/")
    return (
        ".git" in parts
        or "__pycache__" in parts
        or relative == ".build-and-verify/cache"
        or relative.startswith(".build-and-verify/cache/")
    )


def _all_project_files(project: Path) -> list[str]:
    files: list[str] = []
    for root, dirs, names in os.walk(project):
        root_path = Path(root)
        kept_dirs: list[str] = []
        for name in dirs:
            relative = (root_path / name).relative_to(project).as_posix()
            if not _is_excluded_relative(relative):
                kept_dirs.append(name)
        dirs[:] = kept_dirs
        for name in names:
            path = root_path / name
            relative = path.relative_to(project).as_posix()
            if not _is_excluded_relative(relative):
                files.append(relative)
    return sorted(files)


def _changed_files(project: Path) -> list[str]:
    status_names = _git_status_names(project)
    if status_names is not None:
        return _dedupe(status_names)
    commands = [
        ("diff", "--name-only", "--cached"),
        ("diff", "--name-only"),
        ("ls-files", "--others", "--exclude-standard"),
    ]
    names: list[str] = []
    any_git_command_succeeded = False
    for command in commands:
        result = _git_names(project, *command)
        if result is None:
            continue
        any_git_command_succeeded = True
        names.extend(result)
    if not any_git_command_succeeded:
        names = _all_project_files(project)
    return _dedupe(names)


def _path_matches(pattern: str, changed_file: str) -> bool:
    raw_pattern = str(pattern).replace("\\", "/").strip()
    directory_pattern = raw_pattern.endswith("/")
    pattern = raw_pattern.strip("/")
    changed_file = _normalize_path(changed_file)
    if not pattern:
        return False
    if pattern.endswith("/**"):
        prefix = pattern[:-3].rstrip("/")
        return changed_file == prefix or changed_file.startswith(prefix + "/")
    if directory_pattern:
        prefix = pattern.rstrip("/")
        return changed_file == prefix or changed_file.startswith(prefix + "/")
    if any(char in pattern for char in "*?["):
        return fnmatch.fnmatch(changed_file, pattern)
    if "/" in pattern:
        return changed_file == pattern or changed_file.startswith(pattern.rstrip("/") + "/")
    return changed_file == pattern


def _selected_checks(
    checks: list[dict[str, Any]], changed_files: list[str]
) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    for check in checks:
        paths = check.get("paths") or []
        if not paths:
            # Pathless checks are global checks in default verify mode.
            if changed_files:
                selected.append(check)
            continue
        if any(_path_matches(pattern, changed) for pattern in paths for changed in changed_files):
            selected.append(check)
    return selected


def _stable_json(data: Any) -> str:
    return json.dumps(data, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _is_relative_to_project(project: Path, path: Path) -> bool:
    try:
        path.resolve().relative_to(project.resolve())
    except ValueError:
        return False
    return True


def _validate_project_relative_input(project: Path, input_path: str) -> tuple[str, Path]:
    raw_path = Path(input_path)
    relative = _normalize_path(input_path)
    if (
        raw_path.is_absolute()
        or str(input_path).replace("\\", "/").startswith("/")
        or raw_path.anchor
        or ".." in Path(relative).parts
    ):
        raise ValueError(f"invalid_input_path: {input_path}")
    path = project / relative
    if not _is_relative_to_project(project, path):
        raise ValueError(f"invalid_input_path: {input_path}")
    return relative, path


def _validate_check_inputs(project: Path, check: dict[str, Any]) -> None:
    for input_path in check.get("inputs") or []:
        _validate_project_relative_input(project, input_path)


def _hash_input(project: Path, input_path: str) -> dict[str, Any]:
    relative, path = _validate_project_relative_input(project, input_path)
    if not path.exists():
        return {"path": relative, "missing": True}
    if path.is_file():
        return {"path": relative, "type": "file", "sha256": _hash_file(path)}
    if path.is_dir():
        files: list[dict[str, str]] = []
        for root, dirs, names in os.walk(path):
            root_path = Path(root)
            kept_dirs: list[str] = []
            for name in dirs:
                child_relative = (root_path / name).relative_to(project).as_posix()
                if not _is_excluded_relative(child_relative):
                    kept_dirs.append(name)
            dirs[:] = kept_dirs
            for name in sorted(names):
                file_path = root_path / name
                child_relative = file_path.relative_to(project).as_posix()
                if _is_excluded_relative(child_relative):
                    continue
                if not _is_relative_to_project(project, file_path):
                    raise ValueError(f"invalid_input_path: {child_relative}")
                files.append({"path": child_relative, "sha256": _hash_file(file_path)})
        return {"path": relative, "type": "directory", "files": sorted(files, key=lambda item: item["path"])}
    return {"path": relative, "type": "other"}


def _default_cache_inputs(project: Path, paths: list[str]) -> list[str]:
    matched: list[str] = []
    for pattern in paths:
        normalized = _normalize_path(pattern)
        if Path(pattern).anchor or ".." in Path(normalized).parts:
            raise ValueError(f"invalid_input_path: {pattern}")
        matched.extend(
            relative
            for relative in _all_project_files(project)
            if _path_matches(normalized, relative)
        )
    return _dedupe(matched)


def _cache_key(
    project: Path,
    config: dict[str, Any],
    check: dict[str, Any],
    changed_files: list[str] | None = None,
) -> str:
    if "inputs" in check and check.get("inputs") is not None:
        inputs = check.get("inputs") or []
    else:
        paths = check.get("paths") or []
        if paths:
            inputs = _default_cache_inputs(project, paths)
        else:
            inputs = changed_files if changed_files is not None else _changed_files(project)
    payload = {
        "cache_version": CACHE_VERSION,
        "framework_version": FRAMEWORK_VERSION,
        "python_version": platform.python_version(),
        "check_id": check.get("id"),
        "command": check.get("command"),
        "inputs": [_hash_input(project, item) for item in inputs],
        "config": hashlib.sha256(_stable_json(config).encode("utf-8")).hexdigest(),
    }
    return hashlib.sha256(_stable_json(payload).encode("utf-8")).hexdigest()


def _cache_path(project: Path, key: str) -> Path:
    return project / ".build-and-verify" / "cache" / f"{key}.json"


def _cache_load(project: Path, key: str) -> bool:
    path = _cache_path(project, key)
    if not path.is_file():
        return False
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return False
    return data.get("status") == "passed"


def _cache_store(project: Path, key: str, check: dict[str, Any]) -> None:
    path = _cache_path(project, key)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        temp.write_text(
            _stable_json({"status": "passed", "id": check.get("id")}) + "\n",
            encoding="utf-8",
        )
        temp.replace(path)
    except Exception:
        temp.unlink(missing_ok=True)
        raise


def _format_seconds(value: float) -> str:
    return str(int(value)) if value == int(value) else str(value)


def _check_timeout_seconds(config: dict[str, Any], check: dict[str, Any]) -> float | None:
    timeout = check.get("timeoutSeconds")
    verify_config = config.get("verify")
    if timeout is None and isinstance(verify_config, dict):
        timeout = verify_config.get("timeoutSeconds")
    if timeout is None:
        return float(DEFAULT_CHECK_TIMEOUT_SECONDS)
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
        raise ValueError(f"invalid_timeoutSeconds: {check.get('id')}")
    return float(timeout)


def _run_check(project: Path, check: dict[str, Any], runner: Runner) -> int:
    command = check.get("command")
    if not command:
        print(f"missing_command: {check.get('id')}", file=sys.stderr)
        return 1
    dependency_error = _dependency_error(check)
    if dependency_error is not None:
        print(dependency_error, end="", file=sys.stderr)
        return 1
    workers = _pytest_xdist_workers(check.get("pytestXdistWorkers"))
    command = _command_with_pytest_xdist_workers(command, workers)
    use_shell = isinstance(command, str)
    run_kwargs = {
        "cwd": project,
        "check": False,
        "text": True,
        "capture_output": True,
        "shell": use_shell,
    }
    env = _pytest_xdist_env(workers)
    if env is not None:
        run_kwargs["env"] = env
    try:
        result = runner(command, **run_kwargs)
    except FileNotFoundError:
        executable = command[0] if isinstance(command, list) else str(command)
        print(f"command_not_found: {check.get('id')}: {executable}", file=sys.stderr)
        return 1
    if result.stdout:
        print(result.stdout, end="")
    if result.stderr:
        print(result.stderr, end="", file=sys.stderr)
    return int(result.returncode)


def _run_check_result(
    index: int,
    project: Path,
    check: dict[str, Any],
    config: dict[str, Any],
    changed_files: list[str],
    runner: Runner,
) -> CheckResult:
    started_at = time.monotonic()
    try:
        key = _cache_key(project, config, check, changed_files)
        timeout_seconds = _check_timeout_seconds(config, check)
    except ValueError as error:
        return CheckResult(
            index,
            check,
            1,
            stderr=f"{error}\n",
            duration_seconds=time.monotonic() - started_at,
        )

    command = check.get("command")
    if not command:
        return CheckResult(
            index,
            check,
            1,
            stderr=f"missing_command: {check.get('id')}\n",
            duration_seconds=time.monotonic() - started_at,
            cache_key=key,
        )
    dependency_error = _dependency_error(check)
    if dependency_error is not None:
        return CheckResult(
            index,
            check,
            1,
            stderr=dependency_error,
            duration_seconds=time.monotonic() - started_at,
            cache_key=key,
        )
    workers = _pytest_xdist_workers(check.get("pytestXdistWorkers"))
    command = _command_with_pytest_xdist_workers(command, workers)
    use_shell = isinstance(command, str)
    run_kwargs = {
        "cwd": project,
        "check": False,
        "text": True,
        "capture_output": True,
        "shell": use_shell,
    }
    env = _pytest_xdist_env(workers)
    if env is not None:
        run_kwargs["env"] = env
    if timeout_seconds is not None:
        run_kwargs["timeout"] = timeout_seconds
    try:
        result = runner(command, **run_kwargs)
    except FileNotFoundError:
        executable = command[0] if isinstance(command, list) else str(command)
        return CheckResult(
            index,
            check,
            1,
            stderr=f"command_not_found: {check.get('id')}: {executable}\n",
            duration_seconds=time.monotonic() - started_at,
            cache_key=key,
        )
    except subprocess.TimeoutExpired:
        return CheckResult(
            index,
            check,
            1,
            stderr=f"check_timeout: {check.get('id')} exceeded {_format_seconds(timeout_seconds or 0)}s\n",
            duration_seconds=time.monotonic() - started_at,
            cache_key=key,
        )
    except Exception as error:
        return CheckResult(
            index,
            check,
            1,
            stderr=(
                f"parallel_check_exception: {check.get('id')}: "
                f"{type(error).__name__}: {error}\n"
            ),
            duration_seconds=time.monotonic() - started_at,
            cache_key=key,
        )
    return CheckResult(
        index,
        check,
        int(result.returncode),
        stdout=result.stdout or "",
        stderr=result.stderr or "",
        duration_seconds=time.monotonic() - started_at,
        cache_key=key,
    )


def _checks(config: dict[str, Any], section: str) -> list[dict[str, Any]]:
    return list(config.get(section, {}).get("checks", []))


def _max_parallel_checks(config: dict[str, Any], parallel_count: int) -> int:
    verify_config = config.get("verify")
    configured = verify_config.get("maxParallel") if isinstance(verify_config, dict) else None
    if isinstance(configured, bool):
        return parallel_count
    if configured == 0:
        return parallel_count
    if isinstance(configured, int) and configured > 0:
        return min(parallel_count, configured)
    return min(parallel_count, os.cpu_count() or 1)


def _check_ids(checks: list[dict[str, Any]]) -> str:
    return ", ".join(str(check.get("id")) for check in checks)


def _config_error(error: ConfigError) -> int:
    print(str(error), file=sys.stderr)
    print("status: failed")
    return 1


def _run_scheduled_checks(
    project: Path,
    config: dict[str, Any],
    selected: list[dict[str, Any]],
    changed_files: list[str],
    runner: Runner,
) -> tuple[int, list[str]]:
    indexed_selected = list(enumerate(selected))
    parallel_checks = [(index, check) for index, check in indexed_selected if check.get("checkParallel") is True]
    serial_checks = [(index, check) for index, check in indexed_selected if check.get("checkParallel") is not True]
    results: list[CheckResult] = []
    if parallel_checks:
        max_workers = _max_parallel_checks(config, len(parallel_checks))
        executor = concurrent.futures.ThreadPoolExecutor(max_workers=max_workers)
        interrupted = False
        try:
            futures = {
                executor.submit(_run_check_result, index, project, check, config, changed_files, runner): (
                    index,
                    check,
                )
                for index, check in parallel_checks
            }
            for future in futures:
                index, check = futures[future]
                try:
                    results.append(future.result())
                except KeyboardInterrupt as error:
                    interrupted = True
                    results.append(
                        CheckResult(
                            index,
                            check,
                            1,
                            stderr=(
                                f"parallel_check_interrupted: {check.get('id')}: "
                                f"KeyboardInterrupt: {error}\n"
                            ),
                        )
                    )
                    break
        finally:
            executor.shutdown(wait=not interrupted, cancel_futures=interrupted)
        if not interrupted:
            results.extend(
                _run_check_result(index, project, check, config, changed_files, runner)
                for index, check in serial_checks
            )
    else:
        results.extend(
            _run_check_result(index, project, check, config, changed_files, runner)
            for index, check in serial_checks
        )

    failures = 0
    failed_ids: list[str] = []
    for result in sorted(results, key=lambda item: item.index):
        if result.stdout:
            print(result.stdout, end="")
        if result.stderr:
            print(result.stderr, end="", file=sys.stderr)
        print(f"duration: {result.check.get('id')} seconds={result.duration_seconds:.2f}")
        if result.returncode == 0:
            if result.cache_key is not None:
                _cache_store(project, result.cache_key, result.check)
        else:
            failures += 1
            failed_ids.append(str(result.check.get("id")))
    return failures, failed_ids


def run_build(project: Path, runner: Runner = subprocess.run) -> int:
    try:
        config = _load_config(project)
    except ConfigError as error:
        return _config_error(error)
    checks = _checks(config, "build")
    failures = 0
    for check in checks:
        try:
            _validate_check_inputs(project, check)
        except ValueError as error:
            print(str(error), file=sys.stderr)
            failures += 1
            continue
        if _run_check(project, check, runner) != 0:
            failures += 1
    print(f"checked: {_check_ids(checks)}")
    if failures:
        print("status: failed")
        return 1
    print("status: passed")
    return 0


def run_verify(
    project: Path,
    runner: Runner = subprocess.run,
    *,
    full: bool = False,
) -> int:
    try:
        config = _load_config(project)
    except ConfigError as error:
        return _config_error(error)
    checks = _checks(config, "verify")
    changed_files = _changed_files(project)
    selected = checks if full else _selected_checks(checks, changed_files)
    failures = 0
    if full:
        failures, failed_ids = _run_scheduled_checks(project, config, selected, changed_files, runner)
        if failed_ids:
            print(f"failed: {', '.join(failed_ids)}")
        print(f"checked: {_check_ids(selected)}")
        print(f"full-not-run: {str(not full).lower()}")
        if failures:
            print("status: failed")
            return 1
        print("status: passed")
        return 0
    cache_misses: list[dict[str, Any]] = []
    for check in selected:
        try:
            key = _cache_key(project, config, check, changed_files)
        except ValueError as error:
            print(str(error), file=sys.stderr)
            failures += 1
            continue
        if _cache_load(project, key):
            print(f"cache-hit: {check.get('id')}")
            continue
        cache_misses.append(check)
    scheduled_failures, failed_ids = _run_scheduled_checks(project, config, cache_misses, changed_files, runner)
    failures += scheduled_failures
    if failed_ids:
        print(f"failed: {', '.join(failed_ids)}")
    print(f"checked: {_check_ids(selected)}")
    print(f"full-not-run: {str(not full).lower()}")
    if failures:
        print("status: failed")
        return 1
    print("status: passed")
    return 0
