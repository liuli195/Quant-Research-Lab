from __future__ import annotations

import importlib
import importlib.machinery
import importlib.util
import os
import re
import stat
import sys
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from types import ModuleType
from typing import Mapping

from .contracts import StrategyDescriptor, StrategyModule


_STRATEGY_FIELDS = {"root", "module", "symbol"}
_DOTTED_NAME = re.compile(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*")
_SYMBOL_NAME = re.compile(r"[A-Za-z_]\w*")
_IMPORT_LOCK = threading.RLock()
_PRIVATE_NAMESPACE_PREFIX = "_local_quant_strategy_"
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


@dataclass(frozen=True, slots=True)
class DiscoveredStrategySources:
    root: Path
    source_paths: tuple[Path, ...]


@dataclass(frozen=True, slots=True)
class _ImportTarget:
    fullname: str
    part: str
    origin: Path
    package_root: Path | None


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


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


def _plain_python_sources(strategy_root: Path) -> tuple[Path, ...]:
    sources: list[Path] = []
    pending = [strategy_root]
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
                if entry.name not in _IGNORED_SOURCE_DIRECTORIES:
                    pending.append(path)
                continue
            if stat.S_ISREG(metadata.st_mode) and path.suffix.lower() == ".py":
                sources.append(path.resolve())
    return tuple(
        sorted(sources, key=lambda path: path.relative_to(strategy_root).as_posix())
    )


def _import_error(*, package: bool, unsafe: bool) -> ConfigurationError:
    kind = "package" if package else "module"
    state = "unsafe" if unsafe else "missing"
    requirement = "be" if unsafe else "exist"
    if package and not unsafe:
        message = (
            "strategy module file requires each package file to exist "
            "inside strategy_root"
        )
    else:
        message = f"strategy {kind} file must {requirement} inside strategy_root"
    return ConfigurationError(
        f"{state}_strategy_{kind}_file",
        message,
    )


def _resolve_import_targets(
    module_name: str,
    strategy_root: Path,
    namespace: str,
) -> tuple[_ImportTarget, ...]:
    parts = module_name.split(".")
    search_root = strategy_root
    fullname = namespace
    targets: list[_ImportTarget] = []
    for index, part in enumerate(parts):
        fullname = f"{fullname}.{part}"
        parent_package = index < len(parts) - 1
        spec = importlib.machinery.PathFinder.find_spec(
            fullname,
            [str(search_root)],
        )
        if spec is None or not isinstance(spec.origin, (str, os.PathLike)):
            raise _import_error(package=parent_package, unsafe=False)
        origin = Path(spec.origin).resolve()
        if (
            not origin.is_file()
            or origin.suffix.lower() != ".py"
            or not _inside(origin, strategy_root)
        ):
            raise _import_error(package=parent_package, unsafe=True)

        package_root: Path | None = None
        if spec.submodule_search_locations is not None:
            locations = tuple(
                Path(location).resolve()
                for location in spec.submodule_search_locations
            )
            if (
                len(locations) != 1
                or not locations[0].is_dir()
                or not _inside(locations[0], strategy_root)
            ):
                raise _import_error(package=parent_package, unsafe=True)
            package_root = locations[0]
        if parent_package and package_root is None:
            raise _import_error(package=True, unsafe=False)

        targets.append(
            _ImportTarget(
                fullname=fullname,
                part=part,
                origin=origin,
                package_root=package_root,
            )
        )
        if parent_package:
            assert package_root is not None
            search_root = package_root
    return tuple(targets)


def _register_namespace(namespace: str, strategy_root: Path) -> ModuleType:
    spec = importlib.machinery.ModuleSpec(namespace, loader=None, is_package=True)
    spec.submodule_search_locations = [str(strategy_root)]
    module = ModuleType(namespace)
    module.__package__ = namespace
    module.__path__ = [str(strategy_root)]  # type: ignore[attr-defined]
    module.__spec__ = spec
    sys.modules[namespace] = module
    return module


def _discard_namespace(namespace: str) -> None:
    prefix = f"{namespace}."
    for name in tuple(sys.modules):
        if name == namespace or name.startswith(prefix):
            sys.modules.pop(name, None)


def _import_private(
    targets: tuple[_ImportTarget, ...],
    strategy_root: Path,
    namespace: str,
) -> ModuleType:
    parent = _register_namespace(namespace, strategy_root)
    imported: ModuleType = parent
    for target in targets:
        locations = (
            [str(target.package_root)]
            if target.package_root is not None
            else None
        )
        spec = importlib.util.spec_from_file_location(
            target.fullname,
            target.origin,
            submodule_search_locations=locations,
        )
        if spec is None or spec.loader is None:
            raise _import_error(package=target.package_root is not None, unsafe=True)
        imported = importlib.util.module_from_spec(spec)
        sys.modules[target.fullname] = imported
        spec.loader.exec_module(imported)
        if target.package_root is not None:
            imported.__package__ = target.fullname
            imported.__path__ = [str(target.package_root)]  # type: ignore[attr-defined]
            imported.__spec__ = spec
        setattr(parent, target.part, imported)
        parent = imported
    return imported


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


def discover_strategy_sources(
    repo_root: Path,
    config: Mapping[str, object],
) -> DiscoveredStrategySources:
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
    sources = _plain_python_sources(root)
    if not sources:
        raise ConfigurationError(
            "missing_strategy_source",
            "strategy_root must contain a Python source file",
        )
    namespace = f"{_PRIVATE_NAMESPACE_PREFIX}{uuid.uuid4().hex}"
    targets = _resolve_import_targets(module_name, root, namespace)
    source_identities = {os.path.normcase(str(path)) for path in sources}
    if any(
        os.path.normcase(str(target.origin)) not in source_identities
        for target in targets
    ):
        raise ConfigurationError(
            "unsafe_strategy_module_file",
            "strategy module file must be an ordinary source inside strategy_root",
        )
    return DiscoveredStrategySources(root=root, source_paths=sources)


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

    with _IMPORT_LOCK:
        namespace = f"{_PRIVATE_NAMESPACE_PREFIX}{uuid.uuid4().hex}"
        importlib.invalidate_caches()
        targets = _resolve_import_targets(module_name, root, namespace)
        try:
            imported = _import_private(targets, root, namespace)
            _validate_module_file(imported, root)
            module = _validate_strategy_symbol(imported, symbol)
            descriptor = module.descriptor
            source_paths = _source_paths(descriptor, root)
        except BaseException:
            _discard_namespace(namespace)
            raise
        return LoadedStrategy(
            module=module,
            root=root,
            source_paths=source_paths,
            descriptor=descriptor,
        )
