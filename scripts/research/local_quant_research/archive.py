from __future__ import annotations

import hashlib
import os
import re
import shutil
import stat
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from scripts.research import analysis_data

from .result_package import ResultContractError, validate_result_package


_IDENTIFIER = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")
_RUN_ID = re.compile(r"[0-9a-f]{64}")
_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x0400)
_WINDOWS_RESERVED_NAMES = {
    "aux",
    "con",
    "nul",
    "prn",
    *(f"com{number}" for number in range(1, 10)),
    *(f"lpt{number}" for number in range(1, 10)),
}


@dataclass(frozen=True, slots=True)
class ArchiveResult:
    status: Literal["complete", "failed", "conflict"]
    reused: bool
    source: Path | None
    target: Path | None
    reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _FileEntry:
    relative: Path
    size: int
    sha256: str
    metadata: _FileMetadata


@dataclass(frozen=True, slots=True)
class _FileMetadata:
    device: int
    inode: int
    mode: int
    links: int
    reparse: bool


@dataclass(frozen=True, slots=True)
class _TreeSnapshot:
    directories: tuple[Path, ...]
    files: tuple[_FileEntry, ...]
    digest: str


class _UnsafeTreeError(ValueError):
    pass


def _failed(
    reason: str,
    *,
    source: Path | None = None,
    target: Path | None = None,
) -> ArchiveResult:
    return ArchiveResult("failed", False, source, target, (reason,))


def _path_exists(path: Path) -> bool:
    return os.path.lexists(os.fspath(path))


def _is_reparse_point(metadata: os.stat_result) -> bool:
    attributes = getattr(metadata, "st_file_attributes", 0)
    return bool(attributes & _REPARSE_POINT)


def _plain_directory(path: Path) -> bool:
    try:
        metadata = path.lstat()
    except OSError:
        return False
    return stat.S_ISDIR(metadata.st_mode) and not _is_reparse_point(metadata)


def _safe_identifier(value: str) -> bool:
    return (
        _IDENTIFIER.fullmatch(value) is not None
        and not value.endswith((".", " "))
        and value.split(".", 1)[0] not in _WINDOWS_RESERVED_NAMES
    )


def _ordinary_child(parent: Path, name: str) -> Path | None:
    if not _plain_directory(parent):
        raise _UnsafeTreeError("path parent is not an ordinary directory")
    try:
        with os.scandir(parent) as iterator:
            entry = next((item for item in iterator if item.name == name), None)
    except OSError as exc:
        raise _UnsafeTreeError("path parent is unreadable") from exc
    if entry is None:
        return None
    child = Path(entry.path)
    if not _plain_directory(child):
        raise _UnsafeTreeError("path component is not an ordinary directory")
    return child


def _existing_directory(root: Path, *parts: str) -> Path | None:
    current = root
    if not _plain_directory(current):
        raise _UnsafeTreeError("repository root is not an ordinary directory")
    for part in parts:
        child = _ordinary_child(current, part)
        if child is None:
            return None
        current = child
    return current


def _optional_directory(root: Path, *parts: str) -> Path:
    current = root
    for index, part in enumerate(parts):
        child = _ordinary_child(current, part)
        if child is None:
            return current.joinpath(*parts[index:])
        current = child
    return current


def _metadata_identity(metadata: os.stat_result) -> _FileMetadata:
    return _FileMetadata(
        device=metadata.st_dev,
        inode=metadata.st_ino,
        mode=metadata.st_mode,
        links=metadata.st_nlink,
        reparse=_is_reparse_point(metadata),
    )


def _ordinary_file(metadata: _FileMetadata) -> bool:
    return (
        stat.S_ISREG(metadata.mode)
        and metadata.links == 1
        and not metadata.reparse
    )


def _open_verified_descriptor(
    path: Path,
    expected: os.stat_result | _FileMetadata | None = None,
) -> tuple[int, _FileMetadata]:
    before = _metadata_identity(path.lstat())
    expected_identity = (
        _metadata_identity(expected)
        if isinstance(expected, os.stat_result)
        else expected
    )
    if not _ordinary_file(before) or (
        expected_identity is not None and before != expected_identity
    ):
        raise _UnsafeTreeError("tree file identity changed")
    flags = os.O_RDONLY
    flags |= getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(os.fspath(path), flags)
    try:
        opened = _metadata_identity(os.fstat(descriptor))
        current = _metadata_identity(path.lstat())
        if not _ordinary_file(opened) or opened != before or current != before:
            raise _UnsafeTreeError("tree file identity changed")
    except Exception:
        os.close(descriptor)
        raise
    return descriptor, opened


def _descriptor_identity(descriptor: int) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    while True:
        chunk = os.read(descriptor, 1024 * 1024)
        if not chunk:
            break
        size += len(chunk)
        digest.update(chunk)
    return size, digest.hexdigest()


def _verify_open_file(
    path: Path,
    descriptor: int,
    expected: _FileMetadata,
) -> None:
    opened = _metadata_identity(os.fstat(descriptor))
    current = _metadata_identity(path.lstat())
    if opened != expected or current != expected:
        raise _UnsafeTreeError("tree file changed while open")


def _file_identity(
    path: Path,
    expected: os.stat_result | _FileMetadata | None = None,
) -> tuple[int, str]:
    descriptor, opened = _open_verified_descriptor(path, expected)
    try:
        identity = _descriptor_identity(descriptor)
        _verify_open_file(path, descriptor, opened)
    finally:
        os.close(descriptor)
    return identity


def _tree_digest(
    directories: tuple[Path, ...], files: tuple[_FileEntry, ...]
) -> str:
    digest = hashlib.sha256()
    for relative in directories:
        digest.update(b"directory\0")
        digest.update(relative.as_posix().encode("utf-8"))
        digest.update(b"\0")
    for entry in files:
        digest.update(b"file\0")
        digest.update(entry.relative.as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(entry.size).encode("ascii"))
        digest.update(b"\0")
        digest.update(bytes.fromhex(entry.sha256))
    return digest.hexdigest()


def _scan_tree(root: Path) -> _TreeSnapshot:
    try:
        root_metadata = root.lstat()
    except OSError as exc:
        raise _UnsafeTreeError("tree root is unreadable") from exc
    root_identity = _metadata_identity(root_metadata)
    if not stat.S_ISDIR(root_identity.mode) or root_identity.reparse:
        raise _UnsafeTreeError("tree root is not an ordinary directory")

    directories: list[Path] = []
    files: list[_FileEntry] = []

    def visit(
        directory: Path,
        relative_root: Path,
        expected_directory: _FileMetadata,
    ) -> None:
        current_directory = _metadata_identity(directory.lstat())
        if current_directory != expected_directory or current_directory.reparse:
            raise _UnsafeTreeError("tree directory changed during inspection")
        with os.scandir(directory) as iterator:
            entries = sorted(iterator, key=lambda item: item.name)
        if _metadata_identity(directory.lstat()) != expected_directory:
            raise _UnsafeTreeError("tree directory changed during inspection")
        for entry in entries:
            relative = relative_root / entry.name
            entry_path = Path(entry.path)
            try:
                metadata = entry_path.lstat()
            except OSError as exc:
                raise _UnsafeTreeError("tree entry is unreadable") from exc
            if entry.is_symlink() or _is_reparse_point(metadata):
                raise _UnsafeTreeError("tree contains a link or reparse point")
            if stat.S_ISDIR(metadata.st_mode):
                directory_identity = _metadata_identity(metadata)
                if _metadata_identity(entry_path.lstat()) != directory_identity:
                    raise _UnsafeTreeError(
                        "tree directory changed during inspection"
                    )
                directories.append(relative)
                visit(entry_path, relative, directory_identity)
                continue
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                raise _UnsafeTreeError("tree contains a non-ordinary file")
            try:
                file_metadata = _metadata_identity(metadata)
                size, sha256 = _file_identity(entry_path, file_metadata)
            except (OSError, _UnsafeTreeError) as exc:
                raise _UnsafeTreeError("tree file is unreadable") from exc
            if size != metadata.st_size:
                raise _UnsafeTreeError("tree file changed during inspection")
            files.append(_FileEntry(relative, size, sha256, file_metadata))
        if _metadata_identity(directory.lstat()) != expected_directory:
            raise _UnsafeTreeError("tree directory changed during inspection")

    visit(root, Path(), root_identity)
    directory_entries = tuple(directories)
    file_entries = tuple(files)
    return _TreeSnapshot(
        directories=directory_entries,
        files=file_entries,
        digest=_tree_digest(directory_entries, file_entries),
    )


def _prepare_archives_directory(
    strategy_root: Path,
    created: list[Path],
) -> Path:
    research = strategy_root / "research"
    archives = research / "archives"
    for directory in (research, archives):
        if not _path_exists(directory):
            directory.mkdir()
            created.append(directory)
        if not _plain_directory(directory):
            raise _UnsafeTreeError("archive parent is not an ordinary directory")
    return archives


def _cleanup_failed_promotion(
    staging: Path | None,
    created_parents: tuple[Path, ...],
) -> bool:
    cleanup_failed = False
    if staging is not None and _path_exists(staging):
        try:
            if not _plain_directory(staging):
                raise _UnsafeTreeError(
                    "archive staging is not an ordinary directory"
                )
            shutil.rmtree(staging)
        except Exception:
            cleanup_failed = True
    for directory in reversed(created_parents):
        if not _path_exists(directory):
            continue
        try:
            if not _plain_directory(directory):
                raise _UnsafeTreeError(
                    "created archive parent is not an ordinary directory"
                )
            directory.rmdir()
        except Exception:
            cleanup_failed = True
    return cleanup_failed


def _copy_verified_tree(
    source: Path,
    staging: Path,
    snapshot: _TreeSnapshot,
) -> None:
    staging.mkdir()
    for relative in snapshot.directories:
        (staging / relative).mkdir()
    expected = {entry.relative: entry for entry in snapshot.files}
    for relative, entry in expected.items():
        source_file = source / relative
        target_file = staging / relative
        descriptor, opened = _open_verified_descriptor(
            source_file, entry.metadata
        )
        try:
            duplicate = os.dup(descriptor)
            try:
                source_stream = os.fdopen(duplicate, "rb")
            except Exception:
                os.close(duplicate)
                raise
            with source_stream:
                with target_file.open("xb") as target_stream:
                    shutil.copyfileobj(
                        source_stream,
                        target_stream,
                        length=1024 * 1024,
                    )
            os.lseek(descriptor, 0, os.SEEK_SET)
            source_identity = _descriptor_identity(descriptor)
            _verify_open_file(source_file, descriptor, opened)
        finally:
            os.close(descriptor)
        target_identity = _file_identity(target_file)
        expected_identity = (entry.size, entry.sha256)
        if source_identity != expected_identity or target_identity != expected_identity:
            raise OSError("archive copy verification failed")


def _existing_result(
    source: Path,
    target: Path,
    source_snapshot: _TreeSnapshot,
) -> ArchiveResult:
    try:
        target_snapshot = _scan_tree(target)
    except (OSError, _UnsafeTreeError):
        target_snapshot = None
    if target_snapshot is not None and target_snapshot.digest == source_snapshot.digest:
        return ArchiveResult("complete", True, source, target, ())
    return ArchiveResult(
        "conflict",
        False,
        source,
        target,
        ("target_conflict",),
    )


def _validate_analysis_views(staging: Path) -> None:
    with analysis_data.open_analysis_database(staging) as database:
        source = database.source
        if (
            source.kind != "local_research"
            or source.authority != "local_research"
            or source.backend != "vectorbt"
            or source.formula_version != "unified-strategy-analysis/1"
        ):
            raise ValueError("archive analysis identity is invalid")
        if (
            database.table_names != analysis_data.LOCAL_PHYSICAL_DATASETS
            or len(database.table_names) != 4
        ):
            raise ValueError("archive core analysis tables are incomplete")
        for name in database.table_names:
            database.connection.table(name).limit(1).fetchall()
        extensions = source.manifest.get("extensions")
        if not isinstance(extensions, Mapping):
            raise ValueError("archive extensions are invalid")
        for name in sorted(extensions):
            if not isinstance(name, str):
                raise ValueError("archive extension name is invalid")
            database.extension(name).limit(1).fetchall()


def promote_archive(
    repo_root: Path,
    strategy_id: str,
    run_id: str,
    analysis_id: str,
) -> ArchiveResult:
    root = Path(repo_root).absolute()
    if not _safe_identifier(strategy_id):
        return _failed("invalid_strategy_id")
    try:
        strategy_root = _existing_directory(
            root, "joinquant", "strategies", strategy_id
        )
    except _UnsafeTreeError:
        return _failed("invalid_strategy_id")
    if strategy_root is None:
        return _failed("invalid_strategy_id")
    if _RUN_ID.fullmatch(run_id) is None:
        return _failed("invalid_run_id")
    if not _safe_identifier(analysis_id):
        return _failed("invalid_analysis_id")

    try:
        source = _existing_directory(
            root, ".local", "quant-research", strategy_id, run_id
        )
    except _UnsafeTreeError:
        source = root / ".local" / "quant-research" / strategy_id / run_id
        target = strategy_root / "research" / "archives" / analysis_id
        return _failed("unsafe_source_entry", source=source, target=target)
    try:
        archives_path = _optional_directory(strategy_root, "research", "archives")
    except _UnsafeTreeError:
        source_path = (
            source
            if source is not None
            else root / ".local" / "quant-research" / strategy_id / run_id
        )
        target = strategy_root / "research" / "archives" / analysis_id
        return _failed("unsafe_archive_parent", source=source_path, target=target)
    target = archives_path / analysis_id
    if source is None:
        source = root / ".local" / "quant-research" / strategy_id / run_id
        return _failed("source_incomplete", source=source, target=target)
    try:
        manifest = validate_result_package(source)
    except (OSError, ResultContractError):
        return _failed("source_incomplete", source=source, target=target)
    identity = manifest.get("object")
    if not isinstance(identity, Mapping) or (
        identity.get("strategy_id") != strategy_id
        or identity.get("run_id") != run_id
    ):
        return _failed("source_identity_mismatch", source=source, target=target)
    try:
        source_snapshot = _scan_tree(source)
    except (OSError, _UnsafeTreeError):
        return _failed("unsafe_source_entry", source=source, target=target)

    if _path_exists(target):
        return _existing_result(source, target, source_snapshot)

    staging: Path | None = None
    created_parents: list[Path] = []
    try:
        archives = _prepare_archives_directory(strategy_root, created_parents)
        if _path_exists(target):
            return _existing_result(source, target, source_snapshot)
        staging = archives / f".{analysis_id}.{uuid.uuid4().hex}.tmp"
        _copy_verified_tree(source, staging, source_snapshot)
        current_source_snapshot = _scan_tree(source)
        if current_source_snapshot != source_snapshot:
            raise OSError("archive source changed during copy")
        staging_snapshot = _scan_tree(staging)
        if staging_snapshot.digest != source_snapshot.digest:
            raise OSError("archive tree verification failed")
        _validate_analysis_views(staging)
        if _path_exists(target):
            existing = _existing_result(source, target, source_snapshot)
            if _cleanup_failed_promotion(staging, ()):
                return _failed(
                    "cleanup_failed",
                    source=source,
                    target=target,
                )
            return existing
        os.replace(staging, target)
        return ArchiveResult("complete", False, source, target, ())
    except Exception:
        cleanup_failed = _cleanup_failed_promotion(
            staging,
            tuple(created_parents),
        )
        return _failed(
            "cleanup_failed" if cleanup_failed else "copy_failed",
            source=source,
            target=target,
        )
