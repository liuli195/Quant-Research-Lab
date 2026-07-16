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

from .result_package import ResultContractError, validate_result_package


_IDENTIFIER = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")
_RUN_ID = re.compile(r"[0-9a-f]{64}")
_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x0400)


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


def _file_identity(path: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            size += len(chunk)
            digest.update(chunk)
    return size, digest.hexdigest()


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
    if not _plain_directory(root):
        raise _UnsafeTreeError("tree root is not an ordinary directory")

    directories: list[Path] = []
    files: list[_FileEntry] = []

    def visit(directory: Path, relative_root: Path) -> None:
        with os.scandir(directory) as iterator:
            entries = sorted(iterator, key=lambda item: item.name)
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
                directories.append(relative)
                visit(entry_path, relative)
                continue
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                raise _UnsafeTreeError("tree contains a non-ordinary file")
            try:
                size, sha256 = _file_identity(entry_path)
                current = entry_path.lstat()
            except OSError as exc:
                raise _UnsafeTreeError("tree file is unreadable") from exc
            if (
                size != metadata.st_size
                or current.st_size != metadata.st_size
                or current.st_nlink != 1
                or _is_reparse_point(current)
            ):
                raise _UnsafeTreeError("tree file changed during inspection")
            files.append(_FileEntry(relative, size, sha256))

    visit(root, Path())
    directory_entries = tuple(directories)
    file_entries = tuple(files)
    return _TreeSnapshot(
        directories=directory_entries,
        files=file_entries,
        digest=_tree_digest(directory_entries, file_entries),
    )


def _prepare_archives_directory(strategy_root: Path) -> Path:
    research = strategy_root / "research"
    archives = research / "archives"
    for directory in (research, archives):
        if not _path_exists(directory):
            directory.mkdir()
        if not _plain_directory(directory):
            raise _UnsafeTreeError("archive parent is not an ordinary directory")
    return archives


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
        shutil.copyfile(source_file, target_file)
        source_identity = _file_identity(source_file)
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


def promote_archive(
    repo_root: Path,
    strategy_id: str,
    run_id: str,
    analysis_id: str,
) -> ArchiveResult:
    root = Path(repo_root).resolve()
    if _IDENTIFIER.fullmatch(strategy_id) is None:
        return _failed("invalid_strategy_id")
    strategy_root = root / "joinquant" / "strategies" / strategy_id
    if not _plain_directory(strategy_root):
        return _failed("invalid_strategy_id")
    if _RUN_ID.fullmatch(run_id) is None:
        return _failed("invalid_run_id")
    if _IDENTIFIER.fullmatch(analysis_id) is None:
        return _failed("invalid_analysis_id")

    source = root / ".local" / "quant-research" / strategy_id / run_id
    target = strategy_root / "research" / "archives" / analysis_id
    if not _plain_directory(source):
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
    try:
        archives = _prepare_archives_directory(strategy_root)
        if _path_exists(target):
            return _existing_result(source, target, source_snapshot)
        staging = archives / f".{analysis_id}.{uuid.uuid4().hex}.tmp"
        _copy_verified_tree(source, staging, source_snapshot)
        staging_snapshot = _scan_tree(staging)
        if staging_snapshot.digest != source_snapshot.digest:
            raise OSError("archive tree verification failed")
        validate_result_package(staging)
        if _path_exists(target):
            return _existing_result(source, target, source_snapshot)
        os.replace(staging, target)
        return ArchiveResult("complete", False, source, target, ())
    except Exception:
        return _failed("copy_failed", source=source, target=target)
    finally:
        if staging is not None and _path_exists(staging):
            shutil.rmtree(staging, ignore_errors=True)
