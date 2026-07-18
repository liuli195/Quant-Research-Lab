"""Build and Verify Plugin（构建与验证插件）命令入口。"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shutil
import sys
from datetime import datetime
from pathlib import Path
from types import ModuleType


_RUNNER_MODULE: ModuleType | None = None
RUNTIME_FILES = ("build_and_verify.py", "build_and_verify_runner.py")
VERSION_FILE = "version.json"
DEFAULT_CONFIG = {"version": 1, "build": {"checks": []}, "verify": {"checks": []}}
DEFAULT_GITIGNORE = "/cache/\n/runs/\n/backups/\n"


def _skill_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _templates_root() -> Path:
    return _skill_root() / "assets" / "templates"


def _runtime_root() -> Path:
    return Path(__file__).resolve().parent


def _runtime_target(project: Path) -> Path:
    return project / ".build-and-verify" / "runtime"


def _plugin_root() -> Path:
    return _skill_root().parents[1]


def _runtime_metadata() -> dict[str, str]:
    version_path = _runtime_root() / VERSION_FILE
    if version_path.is_file():
        try:
            data = json.loads(version_path.read_text(encoding="utf-8"))
            version = str(data.get("runtime_version") or data.get("plugin_version") or "unknown")
            return {
                "plugin": "build-and-verify",
                "plugin_version": str(data.get("plugin_version") or version),
                "runtime_version": version,
            }
        except json.JSONDecodeError:
            pass
    manifest_path = _plugin_root() / ".codex-plugin" / "plugin.json"
    if manifest_path.is_file():
        try:
            version = str(json.loads(manifest_path.read_text(encoding="utf-8")).get("version"))
            return {
                "plugin": "build-and-verify",
                "plugin_version": version,
                "runtime_version": version,
            }
        except json.JSONDecodeError:
            pass
    return {
        "plugin": "build-and-verify",
        "plugin_version": "unknown",
        "runtime_version": "unknown",
    }


def _version_key(version: str) -> tuple[int, ...]:
    parts: list[int] = []
    for part in version.split("."):
        if not part.isdigit():
            break
        parts.append(int(part))
    return tuple(parts)


def _user_runtime_roots() -> list[Path]:
    home = os.environ.get("USERPROFILE") or os.environ.get("HOME")
    if not home:
        return []
    root = Path(home)
    return [
        root / ".codex" / "plugins" / "cache",
        root / ".claude" / "plugins" / "cache",
        root / ".claude" / "plugins",
    ]


def _newer_user_runtime() -> tuple[Path, str, str] | None:
    current = _runtime_metadata().get("runtime_version", "unknown")
    current_key = _version_key(current)
    if not current_key:
        return None
    best: tuple[Path, str, str] | None = None
    for root in _user_runtime_roots():
        if not root.is_dir():
            continue
        for version_path in root.glob("**/skills/build-and-verify/scripts/version.json"):
            script = version_path.with_name("build_and_verify.py")
            if not script.is_file() or script.resolve() == Path(__file__).resolve():
                continue
            try:
                data = json.loads(version_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                continue
            if data.get("plugin") != "build-and-verify":
                continue
            installed = str(data.get("runtime_version") or data.get("plugin_version") or "")
            installed_key = _version_key(installed)
            if installed_key <= current_key:
                continue
            if best is None or installed_key > _version_key(best[2]):
                best = (script, current, installed)
    return best


def _print_runtime_update_hint(project: Path) -> None:
    newer = _newer_user_runtime()
    if newer is None:
        return
    script, current, installed = newer
    print(f"runtime_outdated: repository={current} installed={installed}")
    print(f"run: python {script} update-runtime --project {project}")


def _copy_runtime(project: Path) -> None:
    target = _runtime_target(project)
    target.mkdir(parents=True, exist_ok=True)
    for filename in RUNTIME_FILES:
        source = _runtime_root() / filename
        destination = target / filename
        if source.resolve() != destination.resolve():
            shutil.copyfile(source, destination)
    (target / VERSION_FILE).write_text(
        json.dumps(_runtime_metadata(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _load_config_file(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ValueError(f"missing_config_file: {path}") from None
    except json.JSONDecodeError as error:
        raise ValueError(f"invalid_config_file: {path}: {error.msg}") from None
    if not isinstance(data, dict):
        raise ValueError(f"invalid_config_file: {path}: root must be object")
    return data


def _merge_gitignore(path: Path) -> None:
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    for entry in DEFAULT_GITIGNORE.splitlines():
        if entry not in lines:
            lines.append(entry)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _backup_config(config_target: Path, project: Path) -> Path:
    backup_dir = project / ".build-and-verify" / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup = backup_dir / f"config-{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
    shutil.copy2(config_target, backup)
    return backup


def _runner() -> ModuleType:
    global _RUNNER_MODULE
    if _RUNNER_MODULE is not None:
        return _RUNNER_MODULE
    runner_path = Path(__file__).resolve().with_name("build_and_verify_runner.py")
    spec = importlib.util.spec_from_file_location("build_and_verify_runner", runner_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"missing_runner: {runner_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    _RUNNER_MODULE = module
    return module


def _init_project(
    project: Path,
    *,
    config: Path | None = None,
    overwrite: bool = False,
) -> int:
    config_target = project / ".build-and-verify" / "config.json"
    gitignore_target = project / ".build-and-verify" / ".gitignore"
    runtime_target = _runtime_target(project)
    try:
        confirmed_config = DEFAULT_CONFIG if config is None else _load_config_file(config)
    except ValueError as error:
        print(str(error), file=sys.stderr)
        return 1

    # Preflight before mkdir/copy so a failed init does not create framework artifacts.
    if not overwrite or config is None:
        for target in [config_target, gitignore_target, runtime_target]:
            if target.exists():
                print(f"existing_file: {target.relative_to(project).as_posix()}", file=sys.stderr)
                return 1

    project.mkdir(parents=True, exist_ok=True)
    config_target.parent.mkdir(parents=True, exist_ok=True)
    backup = _backup_config(config_target, project) if config_target.exists() else None
    _merge_gitignore(gitignore_target)
    config_target.write_text(
        json.dumps(confirmed_config, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    _copy_runtime(project)
    (project / ".build-and-verify" / "cache").mkdir(parents=True, exist_ok=True)

    if backup is not None:
        print(f"backup: {backup.relative_to(project).as_posix()}")
    print("status: initialized")
    return 0


def _update_runtime(project: Path) -> int:
    _copy_runtime(project)
    print("status: runtime-updated")
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="build_and_verify.py")
    subparsers = parser.add_subparsers(dest="command", required=True)
    init_parser = subparsers.add_parser("init")
    init_parser.add_argument("--project", required=True)
    init_parser.add_argument("--config")
    init_parser.add_argument("--overwrite", action="store_true")
    update_parser = subparsers.add_parser("update-runtime")
    update_parser.add_argument("--project", required=True)
    build_parser = subparsers.add_parser("build")
    build_parser.add_argument("--project", default=".")
    verify_parser = subparsers.add_parser("verify")
    verify_parser.add_argument("--project", default=".")
    verify_parser.add_argument("--full", action="store_true")
    verify_parser.add_argument("--performance-report", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    try:
        args = parser.parse_args(sys.argv[1:] if argv is None else argv)
        if (
            args.command == "verify"
            and args.performance_report
            and not args.full
        ):
            parser.error("--performance-report requires --full")
    except SystemExit as error:
        return int(error.code) if isinstance(error.code, int) else 2
    if args.command == "init":
        return _init_project(
            Path(args.project).resolve(),
            config=Path(args.config).resolve() if args.config else None,
            overwrite=bool(args.overwrite),
        )
    if args.command == "update-runtime":
        return _update_runtime(Path(args.project).resolve())
    if args.command == "build":
        project = Path(args.project).resolve()
        _print_runtime_update_hint(project)
        return int(_runner().run_build(project))
    if args.command == "verify":
        project = Path(args.project).resolve()
        _print_runtime_update_hint(project)
        return int(
            _runner().run_verify(
                project,
                full=args.full,
                performance_report=args.performance_report,
                runtime_version=_runtime_metadata()["runtime_version"],
            )
        )
    parser.error(f"unsupported command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
