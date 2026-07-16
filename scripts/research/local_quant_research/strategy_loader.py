from __future__ import annotations

import importlib
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from types import ModuleType
from typing import Mapping

from .contracts import StrategyDescriptor, StrategyModule


_STRATEGY_FIELDS = {"root", "module", "symbol"}
_DOTTED_NAME = re.compile(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*")
_SYMBOL_NAME = re.compile(r"[A-Za-z_]\w*")


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


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


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
    resolved = (repo_root / candidate).resolve()
    if not _inside(resolved, repo_root):
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


def _module_in_root(module: ModuleType, strategy_root: Path) -> bool:
    module_file = getattr(module, "__file__", None)
    if not isinstance(module_file, (str, os.PathLike)):
        return False
    return _inside(Path(module_file), strategy_root)


def _namespace_member(name: str, top_level: str) -> bool:
    return name == top_level or name.startswith(f"{top_level}.")


def _require_module_file(module_name: str, strategy_root: Path) -> None:
    relative = Path(*module_name.split("."))
    candidates = (
        (strategy_root / relative).with_suffix(".py"),
        strategy_root / relative / "__init__.py",
    )
    if not any(
        candidate.is_file() and _inside(candidate, strategy_root)
        for candidate in candidates
    ):
        raise ConfigurationError(
            "missing_strategy_module",
            "strategy module file must exist inside strategy_root",
        )


def _import_isolated(module_name: str, strategy_root: Path) -> ModuleType:
    top_level = module_name.partition(".")[0]
    original_path = list(sys.path)
    original_modules = dict(sys.modules)
    for name in tuple(sys.modules):
        if _namespace_member(name, top_level):
            sys.modules.pop(name, None)
    sys.path.insert(0, str(strategy_root))
    importlib.invalidate_caches()
    try:
        return importlib.import_module(module_name)
    except ModuleNotFoundError as exc:
        if exc.name is not None and (
            exc.name == module_name or module_name.startswith(f"{exc.name}.")
        ):
            raise ConfigurationError(
                "missing_strategy_module",
                "strategy module could not be imported",
            ) from exc
        raise
    finally:
        for name, module in tuple(sys.modules.items()):
            if _namespace_member(name, top_level) or (
                isinstance(module, ModuleType) and _module_in_root(module, strategy_root)
            ):
                if name in original_modules:
                    sys.modules[name] = original_modules[name]
                else:
                    sys.modules.pop(name, None)
        for name, module in original_modules.items():
            if _namespace_member(name, top_level):
                sys.modules[name] = module
        sys.path[:] = original_path
        importlib.invalidate_caches()


def _validate_module_file(imported: ModuleType, strategy_root: Path) -> None:
    module_file = getattr(imported, "__file__", None)
    if not isinstance(module_file, (str, os.PathLike)):
        raise ConfigurationError(
            "invalid_strategy_module_file",
            "strategy module file is missing",
        )
    resolved = Path(module_file).resolve()
    if not resolved.is_file() or not _inside(resolved, strategy_root):
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


def _source_paths(
    descriptor: StrategyDescriptor,
    strategy_root: Path,
) -> tuple[Path, ...]:
    if not isinstance(descriptor.source_files, tuple) or not descriptor.source_files:
        raise ConfigurationError(
            "invalid_source_files",
            "source_files must be a non-empty tuple of relative paths",
        )
    paths: list[Path] = []
    seen: set[str] = set()
    for source_file in descriptor.source_files:
        if not isinstance(source_file, Path):
            raise ConfigurationError(
                "invalid_source_files",
                "source_files must contain Path values",
            )
        portable = PurePosixPath(source_file.as_posix())
        if source_file.is_absolute() or portable.is_absolute() or ".." in portable.parts:
            raise ConfigurationError(
                "unsafe_source_files",
                "source_files must stay inside strategy_root",
            )
        resolved = (strategy_root / Path(*portable.parts)).resolve()
        if not _inside(resolved, strategy_root):
            raise ConfigurationError(
                "unsafe_source_files",
                "source_files must stay inside strategy_root",
            )
        identity = os.path.normcase(str(resolved))
        if identity in seen:
            raise ConfigurationError(
                "duplicate_source_files",
                "source_files contain a duplicate path",
            )
        seen.add(identity)
        if not resolved.exists():
            raise ConfigurationError(
                "missing_source_file",
                "a declared source file is missing",
            )
        if not resolved.is_file():
            raise ConfigurationError(
                "invalid_source_file",
                "each declared source file must be a file",
            )
        paths.append(resolved)
    return tuple(paths)


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

    _require_module_file(module_name, root)
    imported = _import_isolated(module_name, root)
    _validate_module_file(imported, root)
    module = _validate_strategy_symbol(imported, symbol)
    descriptor = module.descriptor
    return LoadedStrategy(
        module=module,
        root=root,
        source_paths=_source_paths(descriptor, root),
        descriptor=descriptor,
    )
