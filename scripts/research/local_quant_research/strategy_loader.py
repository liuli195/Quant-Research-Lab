from __future__ import annotations

import importlib
import os
import re
import stat
import sys
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Mapping

from .contracts import StrategyDescriptor, StrategyModule


_STRATEGY_FIELDS = {"root", "module", "symbol"}
_DOTTED_NAME = re.compile(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*")
_SYMBOL_NAME = re.compile(r"[A-Za-z_]\w*")
_IGNORED_SOURCE_DIRECTORIES = {
    ".git",
    ".local",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "__pycache__",
    "build",
    "dist",
}
_FILE_ATTRIBUTE_REPARSE_POINT = 0x0400


class ConfigurationError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class LoadedStrategy:
    module: StrategyModule
    root: Path
    source_paths: tuple[Path, ...]
    descriptor: StrategyDescriptor


def _is_reparse_point(path: Path, metadata: os.stat_result | None = None) -> bool:
    try:
        details = os.lstat(path) if metadata is None else metadata
    except OSError:
        return False
    return (
        stat.S_ISLNK(details.st_mode)
        or bool(
            getattr(details, "st_file_attributes", 0)
            & _FILE_ATTRIBUTE_REPARSE_POINT
        )
        or bool(getattr(os.path, "isjunction", lambda _path: False)(path))
    )


def _reject_reparse_components(path: Path, root: Path) -> None:
    current = root
    for part in path.relative_to(root).parts:
        current /= part
        if current.exists() and _is_reparse_point(current):
            raise ConfigurationError(
                "unsafe_strategy_root",
                "strategy_root must not contain a reparse point",
            )


def _resolve_strategy_root(repo_root: Path, value: object) -> Path:
    if not isinstance(value, str) or not value:
        raise ConfigurationError(
            "invalid_strategy_root",
            "strategy_root must be a repository-relative path",
        )
    candidate = Path(value)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise ConfigurationError(
            "unsafe_strategy_root",
            "strategy_root must be a repository-relative path without '..'",
        )
    unresolved = repo_root / candidate
    try:
        unresolved.relative_to(repo_root)
    except ValueError as exc:
        raise ConfigurationError(
            "unsafe_strategy_root",
            "strategy_root escapes the repository",
        ) from exc
    _reject_reparse_components(unresolved, repo_root)
    resolved = unresolved.resolve()
    if not resolved.is_relative_to(repo_root.resolve()):
        raise ConfigurationError(
            "unsafe_strategy_root",
            "strategy_root escapes the repository",
        )
    if not resolved.is_dir():
        raise ConfigurationError(
            "missing_strategy_root",
            "strategy_root must be an existing directory",
        )
    return resolved


def _module_source_error(*, unsafe: bool) -> ConfigurationError:
    state = "unsafe" if unsafe else "missing"
    requirement = "stay inside strategy_root" if unsafe else "exist inside strategy_root"
    return ConfigurationError(
        f"{state}_strategy_module_file",
        f"strategy module file and each package file must {requirement}",
    )


def _plain_file(path: Path, strategy_root: Path) -> Path | None:
    try:
        metadata = os.lstat(path)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise _module_source_error(unsafe=True) from exc
    if _is_reparse_point(path, metadata) or not stat.S_ISREG(metadata.st_mode):
        raise _module_source_error(unsafe=True)
    resolved = path.resolve()
    if not resolved.is_relative_to(strategy_root.resolve()):
        raise _module_source_error(unsafe=True)
    return resolved


def _plain_directory(path: Path, strategy_root: Path) -> bool:
    try:
        metadata = os.lstat(path)
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise _module_source_error(unsafe=True) from exc
    if _is_reparse_point(path, metadata) or not stat.S_ISDIR(metadata.st_mode):
        raise _module_source_error(unsafe=True)
    if not path.resolve().is_relative_to(strategy_root.resolve()):
        raise _module_source_error(unsafe=True)
    return True


def _module_source_and_boundary(
    strategy_root: Path,
    module_name: str,
) -> tuple[Path, Path]:
    parts = module_name.split(".")
    current = strategy_root
    top_package: Path | None = None
    for index, part in enumerate(parts):
        final = index == len(parts) - 1
        package = current / part
        package_init = (
            _plain_file(package / "__init__.py", strategy_root)
            if _plain_directory(package, strategy_root)
            else None
        )
        if package_init is not None:
            if top_package is None:
                top_package = package.resolve()
            if final:
                return package_init, top_package
            current = package.resolve()
            continue
        if not final:
            raise _module_source_error(unsafe=False)
        module_file = _plain_file(current / f"{part}.py", strategy_root)
        if module_file is None:
            raise _module_source_error(unsafe=False)
        return module_file, top_package or module_file.parent
    raise _module_source_error(unsafe=False)


def _is_archive_directory(path: Path, boundary: Path) -> bool:
    parts = path.relative_to(boundary).parts
    return len(parts) >= 2 and parts[-2:] == ("research", "archives")


def _inside_archive_path(path: Path) -> bool:
    parts = tuple(part.casefold() for part in path.parts)
    return any(
        parts[index : index + 2] == ("research", "archives")
        for index in range(len(parts) - 1)
    )


def _plain_python_sources(boundary: Path) -> tuple[Path, ...]:
    sources: list[Path] = []
    pending = [boundary]
    while pending:
        directory = pending.pop()
        try:
            entries = sorted(os.scandir(directory), key=lambda item: item.name)
        except OSError as exc:
            raise ConfigurationError(
                "invalid_strategy_source_tree",
                "strategy source tree cannot be inspected",
            ) from exc
        for entry in entries:
            path = Path(entry.path)
            try:
                metadata = entry.stat(follow_symlinks=False)
            except OSError as exc:
                raise ConfigurationError(
                    "invalid_strategy_source_tree",
                    "strategy source entry cannot be inspected",
                ) from exc
            if _is_reparse_point(path, metadata):
                raise ConfigurationError(
                    "unsafe_strategy_source_tree",
                    "strategy source tree must not contain a reparse point",
                )
            if stat.S_ISDIR(metadata.st_mode):
                if (
                    entry.name not in _IGNORED_SOURCE_DIRECTORIES
                    and not _is_archive_directory(path, boundary)
                ):
                    pending.append(path)
                continue
            if stat.S_ISREG(metadata.st_mode) and path.suffix.lower() == ".py":
                sources.append(path.resolve())
    return tuple(
        sorted(sources, key=lambda path: path.relative_to(boundary).as_posix())
    )


def discover_strategy_sources(
    strategy_root: Path,
    module: str,
) -> tuple[Path, ...]:
    root = Path(strategy_root).resolve()
    if not root.is_dir():
        raise ConfigurationError(
            "missing_strategy_root",
            "strategy_root must be an existing directory",
        )
    if not isinstance(module, str) or _DOTTED_NAME.fullmatch(module) is None:
        raise ConfigurationError("invalid_strategy_module", "strategy module is invalid")
    module_source, boundary = _module_source_and_boundary(root, module)
    if _inside_archive_path(boundary):
        raise ConfigurationError(
            "unsafe_strategy_source_tree",
            "strategy source boundary must not be inside research/archives",
        )
    sources = _plain_python_sources(boundary)
    if module_source not in sources:
        raise ConfigurationError(
            "unsafe_strategy_module_file",
            "strategy module file must be an ordinary Python source",
        )
    return sources


def _validate_module_file(imported: ModuleType, strategy_root: Path) -> None:
    module_file = getattr(imported, "__file__", None)
    if not isinstance(module_file, (str, os.PathLike)):
        raise ConfigurationError(
            "invalid_strategy_module_file",
            "strategy module file is missing",
        )
    resolved = Path(module_file).resolve()
    if not resolved.is_file() or not resolved.is_relative_to(strategy_root.resolve()):
        raise ConfigurationError(
            "unsafe_strategy_module_file",
            "strategy module file must be inside strategy_root",
        )


def _validate_strategy_symbol(imported: ModuleType, symbol: str) -> StrategyModule:
    try:
        module = getattr(imported, symbol)
    except AttributeError as exc:
        raise ConfigurationError(
            "missing_strategy_symbol",
            "strategy symbol is missing",
        ) from exc
    descriptor = getattr(module, "descriptor", None)
    if not isinstance(descriptor, StrategyDescriptor):
        raise ConfigurationError(
            "invalid_strategy_symbol",
            "strategy symbol descriptor is invalid",
        )
    for method_name in ("prepare", "followup_program", "build_extensions"):
        if not callable(getattr(module, method_name, None)):
            raise ConfigurationError(
                "invalid_strategy_symbol",
                f"strategy symbol method {method_name} is missing",
            )
    return module


def load_strategy(
    repo_root: Path,
    config: Mapping[str, object],
) -> LoadedStrategy:
    if not isinstance(config, Mapping) or set(config) != _STRATEGY_FIELDS:
        raise ConfigurationError(
            "invalid_strategy_fields",
            "strategy fields must be exactly root, module, and symbol",
        )
    root = _resolve_strategy_root(Path(repo_root).resolve(), config["root"])
    module_name = config["module"]
    symbol = config["symbol"]
    if not isinstance(module_name, str) or _DOTTED_NAME.fullmatch(module_name) is None:
        raise ConfigurationError("invalid_strategy_module", "strategy module is invalid")
    if not isinstance(symbol, str) or _SYMBOL_NAME.fullmatch(symbol) is None:
        raise ConfigurationError("invalid_strategy_symbol", "strategy symbol is invalid")

    source_paths = discover_strategy_sources(root, module_name)
    import_root = str(root)
    sys.path.insert(0, import_root)
    importlib.invalidate_caches()
    try:
        imported = importlib.import_module(module_name)
        _validate_module_file(imported, root)
        module = _validate_strategy_symbol(imported, symbol)
    finally:
        if sys.path and sys.path[0] == import_root:
            sys.path.pop(0)
        else:
            sys.path.remove(import_root)
    return LoadedStrategy(
        module=module,
        root=root,
        source_paths=source_paths,
        descriptor=module.descriptor,
    )
