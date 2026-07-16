from __future__ import annotations

import argparse
import os
import runpy
import shutil
import sys
from pathlib import Path
from typing import Sequence


_WRITE_FLAGS = (
    os.O_WRONLY
    | os.O_RDWR
    | os.O_APPEND
    | os.O_CREAT
    | os.O_TRUNC
    | getattr(os, "O_TEMPORARY", 0)
)
_SINGLE_PATH_MUTATIONS = {
    "os.remove",
    "os.rmdir",
    "os.mkdir",
    "os.chmod",
    "os.chown",
    "os.truncate",
    "os.utime",
}
_TWO_PATH_MUTATIONS = {"os.rename", "os.replace", "os.link", "os.symlink"}
_PROCESS_EVENTS = {
    "subprocess.Popen",
    "os.system",
    "os.posix_spawn",
    "os.spawn",
    "os.startfile",
}


def _path_from_event(value: object) -> Path | None:
    if isinstance(value, int):
        return None
    try:
        return Path(os.fsdecode(os.fspath(value))).resolve()
    except (TypeError, ValueError, OSError):
        return None


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _require_write_path(value: object, write_roots: tuple[Path, ...]) -> None:
    path = _path_from_event(value)
    if path is not None and not any(_inside(path, root) for root in write_roots):
        raise PermissionError("adapter write outside staging is forbidden")


def _open_is_write(args: Sequence[object]) -> bool:
    mode = args[1] if len(args) > 1 else None
    flags = args[2] if len(args) > 2 else 0
    mode_writes = isinstance(mode, str) and any(marker in mode for marker in "wax+")
    flag_writes = isinstance(flags, int) and bool(flags & _WRITE_FLAGS)
    return mode_writes or flag_writes


def _is_devnull(value: object) -> bool:
    if isinstance(value, int):
        return False
    try:
        return os.path.normcase(os.fsdecode(os.fspath(value))) == os.path.normcase(
            os.devnull
        )
    except (TypeError, ValueError, OSError):
        return False


def install_access_guard(
    output_dir: Path,
    *,
    execution_root: Path,
    repository_root: Path,
    venv_root: Path,
    runtime_cache_root: Path | None = None,
) -> None:
    output_root = Path(output_dir).resolve()
    execution_root = Path(execution_root).resolve()
    repository_root = Path(repository_root).resolve()
    venv_root = Path(venv_root).resolve()
    runtime_cache_root = (
        output_root / ".runtime-cache"
        if runtime_cache_root is None
        else Path(runtime_cache_root).resolve()
    )
    write_roots = (output_root, runtime_cache_root)

    def audit(event: str, args: tuple[object, ...]) -> None:
        if (
            event == "open"
            and args
            and _open_is_write(args)
            and not _is_devnull(args[0])
        ):
            _require_write_path(args[0], write_roots)
        elif event == "open" and args:
            path = _path_from_event(args[0])
            if (
                path is not None
                and _inside(path, repository_root)
                and not any(
                    _inside(path, allowed)
                    for allowed in (output_root, execution_root, venv_root)
                )
            ):
                raise PermissionError("adapter read from live repository is forbidden")
        elif event in _SINGLE_PATH_MUTATIONS and args:
            _require_write_path(args[0], write_roots)
        elif event in _TWO_PATH_MUTATIONS and len(args) >= 2:
            _require_write_path(args[0], write_roots)
            _require_write_path(args[1], write_roots)
        elif event in _PROCESS_EVENTS:
            raise PermissionError("adapter child processes are forbidden")

    sys.addaudithook(audit)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--staging-root", type=Path, required=True)
    parser.add_argument("--execution-root", type=Path, required=True)
    parser.add_argument("--repository-root", type=Path, required=True)
    parser.add_argument("--venv-root", type=Path, required=True)
    parser.add_argument("--entry", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args, adapter_args = _parser().parse_known_args(argv)
    if adapter_args and adapter_args[0] == "--":
        adapter_args = adapter_args[1:]
    output_dir = args.staging_root.resolve()
    execution_root = args.execution_root.resolve()
    entry = args.entry.resolve()
    if (
        not output_dir.is_dir()
        or not execution_root.is_dir()
        or not entry.is_file()
        or not _inside(entry, execution_root)
    ):
        return 2
    runtime_cache = output_dir / ".runtime-cache"
    numba_cache = runtime_cache / "numba"
    matplotlib_cache = runtime_cache / "matplotlib"
    numba_cache.mkdir(parents=True)
    matplotlib_cache.mkdir()
    os.environ["NUMBA_CACHE_DIR"] = str(numba_cache)
    os.environ["MPLCONFIGDIR"] = str(matplotlib_cache)
    os.environ["XDG_CACHE_HOME"] = str(runtime_cache)
    install_access_guard(
        output_dir,
        execution_root=execution_root,
        repository_root=args.repository_root,
        venv_root=args.venv_root,
    )
    sys.path.insert(0, str(execution_root / "repository"))
    sys.path.insert(0, str(entry.parent))
    sys.argv = [str(entry), *adapter_args]
    try:
        runpy.run_path(str(entry), run_name="__main__")
    finally:
        shutil.rmtree(runtime_cache)
        if runtime_cache.exists():
            raise RuntimeError("adapter runtime cache cleanup failed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
