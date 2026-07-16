from __future__ import annotations

import builtins
import hashlib
import importlib
import json
import os
import shutil
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Iterator

import pyarrow as pa
import pytest

from scripts.research.analysis_data.manifest import open_analysis_source
from scripts.research.analysis_data.views import open_analysis_database
from scripts.research.local_quant_research.archive import (
    ArchiveResult,
    promote_archive,
)
from scripts.research.local_quant_research.contracts import (
    ExecutionBundle,
    ExecutionRun,
    ResultExtension,
)
from scripts.research.local_quant_research.result_package import (
    ResultPackageRequest,
    write_result_package,
)
from tests.local_quant_research.test_analysis_data_views import _AnalysisLedger


STRATEGY_ID = "strategy-003"
RUN_ID = "a" * 64
ANALYSIS_ID = "baseline-v2"


@pytest.fixture
def isolated_repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "joinquant/strategies" / STRATEGY_ID).mkdir(parents=True)
    return root


def _write_complete_package(
    repo_root: Path,
    *,
    manifest_strategy_id: str = STRATEGY_ID,
    manifest_run_id: str = RUN_ID,
    directory_run_id: str = RUN_ID,
) -> Path:
    code = repo_root / "fixture-strategy.py"
    code.write_bytes(b"VALUE = 1\r\n")
    ledger = _AnalysisLedger()
    run = ExecutionRun(ledger=ledger, trace={})
    package = write_result_package(
        ResultPackageRequest(
            strategy_id=manifest_strategy_id,
            scenario_id="baseline",
            run_id=manifest_run_id,
            output_dir=(
                repo_root
                / ".local/quant-research"
                / STRATEGY_ID
                / directory_run_id
            ),
            execution=ExecutionBundle(
                primary=run,
                final=run,
                stages=("primary",),
            ),
            extensions=(
                ResultExtension(
                    name="signals",
                    schema_version="signals/1",
                    table=pa.table(
                        {"event_id": ["signal-1"], "score": [0.5]}
                    ),
                    unique_key=("event_id",),
                    evidence={"status": "complete"},
                ),
            ),
            code_files={"strategy.py": code},
            config_documents={
                "scenario.json": {"scenario_id": "baseline"},
                "project-run.json": {"schema_version": 2},
                "code-identity.json": {"digest": "b" * 64},
            },
            evidence_documents={
                "market-snapshot.json": {"snapshot_id": "c" * 64},
                "runtime-lock.json": {"python": "3.12"},
                "performance.json": {"status": "pass"},
                "environment.json": {"platform": "windows"},
            },
        )
    )
    (package.path / "evidence/raw-bytes.bin").write_bytes(
        b"\x00\xffarchive-ready\r\n"
    )
    (package.path / "extensions/empty-evidence").mkdir()
    return package.path


@pytest.fixture
def complete_package(isolated_repo: Path) -> Path:
    return _write_complete_package(isolated_repo)


def _boom(*_args: object, **_kwargs: object) -> None:
    raise AssertionError("archive promotion must not recompute results")


def _guard_recomputation(monkeypatch: pytest.MonkeyPatch) -> None:
    import vectorbt as vbt

    from scripts.research.local_quant_research import (
        result_package,
        runner,
        strategy_loader,
    )

    monkeypatch.setattr(strategy_loader, "load_strategy", _boom)
    monkeypatch.setattr(vbt.Portfolio, "from_order_func", _boom)
    monkeypatch.setattr(result_package.pq, "write_table", _boom)
    monkeypatch.setattr(result_package, "write_result_package", _boom)
    monkeypatch.setattr(runner, "run_project", _boom)
    monkeypatch.setitem(
        sys.modules,
        "scripts.research.local_quant_research.vectorbt_runtime",
        SimpleNamespace(run_vectorbt=_boom),
    )


def _tree_digests(root: Path) -> dict[str, tuple[int, str]]:
    return {
        path.relative_to(root).as_posix(): (
            path.stat().st_size,
            hashlib.sha256(path.read_bytes()).hexdigest(),
        )
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _tree_directories(root: Path) -> tuple[str, ...]:
    return tuple(
        path.relative_to(root).as_posix()
        for path in sorted(root.rglob("*"))
        if path.is_dir()
    )


def _path_exists_for_test(path: Path) -> bool:
    return os.path.lexists(os.fspath(path))


@pytest.mark.parametrize(
    "analysis_id",
    (
        "Upper",
        "../escape",
        "a/b",
        "",
        "x" * 65,
        "con",
        "aux.txt",
        "baseline-v2.",
        "baseline-v2 ",
    ),
)
def test_promote_rejects_invalid_analysis_id(
    isolated_repo: Path,
    analysis_id: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _guard_recomputation(monkeypatch)

    result = promote_archive(isolated_repo, STRATEGY_ID, RUN_ID, analysis_id)

    assert result.status == "failed"
    assert result.reasons == ("invalid_analysis_id",)


@pytest.mark.parametrize(
    "strategy_id",
    (
        "Upper",
        "../escape",
        "a/b",
        "",
        "x" * 65,
        "strategy-999",
        "con",
        "aux.txt",
        "strategy-003.",
        "strategy-003 ",
    ),
)
def test_promote_rejects_invalid_strategy_identity(
    isolated_repo: Path,
    strategy_id: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _guard_recomputation(monkeypatch)

    result = promote_archive(isolated_repo, strategy_id, RUN_ID, ANALYSIS_ID)

    assert result.status == "failed"
    assert result.reasons == ("invalid_strategy_id",)


@pytest.mark.parametrize(
    "run_id", ("A" * 64, "a" * 63, "a" * 65, "../escape", "a/b", "")
)
def test_promote_rejects_invalid_run_identity(
    isolated_repo: Path,
    run_id: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _guard_recomputation(monkeypatch)

    result = promote_archive(isolated_repo, STRATEGY_ID, run_id, ANALYSIS_ID)

    assert result.status == "failed"
    assert result.reasons == ("invalid_run_id",)


@pytest.mark.skipif(os.name != "nt", reason="Windows name matching is required")
def test_promote_rejects_case_insensitive_strategy_directory_alias(
    complete_package: Path,
    isolated_repo: Path,
) -> None:
    strategies = isolated_repo / "joinquant/strategies"
    exact = strategies / STRATEGY_ID
    temporary = strategies / "strategy-rename"
    alias = strategies / "Strategy-003"
    exact.rename(temporary)
    temporary.rename(alias)

    result = promote_archive(isolated_repo, STRATEGY_ID, RUN_ID, ANALYSIS_ID)

    assert result.status == "failed"
    assert result.reasons == ("invalid_strategy_id",)
    assert not (alias / "research/archives" / ANALYSIS_ID).exists()


def _replace_directory_with_link(
    directory: Path,
    target: Path,
    kind: str,
) -> None:
    directory.rename(target)
    if kind == "symlink":
        try:
            directory.symlink_to(target, target_is_directory=True)
        except OSError as exc:
            target.rename(directory)
            pytest.skip(f"directory symlink is unavailable: {exc}")
        return
    if os.name != "nt":
        target.rename(directory)
        pytest.skip("junction coverage is Windows-specific")
    completed = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(directory), str(target)],
        capture_output=True,
        check=False,
        text=True,
    )
    if completed.returncode != 0:
        target.rename(directory)
        pytest.skip(f"junction is unavailable: {completed.stderr}")


def _restore_linked_directory(directory: Path, target: Path) -> None:
    if directory.is_symlink():
        directory.unlink()
    else:
        os.rmdir(directory)
    target.rename(directory)


@pytest.mark.parametrize("link_kind", ("symlink", "junction"))
@pytest.mark.parametrize(
    "relative_parent",
    (
        ".local",
        ".local/quant-research",
        f".local/quant-research/{STRATEGY_ID}",
        f".local/quant-research/{STRATEGY_ID}/{RUN_ID}",
        "joinquant",
        "joinquant/strategies",
        f"joinquant/strategies/{STRATEGY_ID}",
        f"joinquant/strategies/{STRATEGY_ID}/research",
        f"joinquant/strategies/{STRATEGY_ID}/research/archives",
    ),
)
def test_promote_rejects_linked_fixed_parent_without_crossing_boundary(
    complete_package: Path,
    isolated_repo: Path,
    relative_parent: str,
    link_kind: str,
) -> None:
    (isolated_repo / f"joinquant/strategies/{STRATEGY_ID}/research/archives").mkdir(
        parents=True
    )
    linked_parent = isolated_repo / relative_parent
    outside = isolated_repo.parent / "outside-parent"
    _replace_directory_with_link(linked_parent, outside, link_kind)
    try:
        result = promote_archive(isolated_repo, STRATEGY_ID, RUN_ID, ANALYSIS_ID)

        assert result.status == "failed"
        assert not (
            isolated_repo
            / f"joinquant/strategies/{STRATEGY_ID}/research/archives"
            / ANALYSIS_ID
        ).exists()
    finally:
        _restore_linked_directory(linked_parent, outside)


def test_promote_rejects_incomplete_source_package(
    complete_package: Path,
    isolated_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (complete_package / "data/results.parquet").unlink()
    _guard_recomputation(monkeypatch)

    result = promote_archive(isolated_repo, STRATEGY_ID, RUN_ID, ANALYSIS_ID)

    assert result.status == "failed"
    assert result.reasons == ("source_incomplete",)
    assert not (isolated_repo / "joinquant/strategies/strategy-003/research").exists()


@pytest.mark.parametrize(
    ("manifest_strategy_id", "manifest_run_id"),
    (("strategy-004", RUN_ID), (STRATEGY_ID, "b" * 64)),
)
def test_promote_rejects_source_identity_mismatch(
    isolated_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    manifest_strategy_id: str,
    manifest_run_id: str,
) -> None:
    _write_complete_package(
        isolated_repo,
        manifest_strategy_id=manifest_strategy_id,
        manifest_run_id=manifest_run_id,
    )
    _guard_recomputation(monkeypatch)

    result = promote_archive(isolated_repo, STRATEGY_ID, RUN_ID, ANALYSIS_ID)

    assert result.status == "failed"
    assert result.reasons == ("source_identity_mismatch",)


def test_promote_preserves_layout_and_every_source_byte(
    complete_package: Path,
    isolated_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest_before = (complete_package / "manifest.json").read_bytes()
    _guard_recomputation(monkeypatch)

    result = promote_archive(isolated_repo, STRATEGY_ID, RUN_ID, ANALYSIS_ID)

    assert result == ArchiveResult(
        status="complete",
        reused=False,
        source=complete_package,
        target=(
            isolated_repo
            / "joinquant/strategies/strategy-003/research/archives/baseline-v2"
        ),
        reasons=(),
    )
    assert _tree_digests(result.source) == _tree_digests(result.target)
    assert _tree_directories(result.source) == _tree_directories(result.target)
    assert (complete_package / "manifest.json").read_bytes() == manifest_before
    manifest = json.loads((result.target / "manifest.json").read_text("utf-8"))
    assert manifest["object"]["run_id"] == RUN_ID
    assert "analysis_id" not in manifest


@pytest.mark.parametrize("unsafe_kind", ("symlink", "hardlink", "junction"))
def test_promote_rejects_links_and_reparse_points(
    complete_package: Path,
    isolated_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    unsafe_kind: str,
) -> None:
    if unsafe_kind == "symlink":
        try:
            (complete_package / "unsafe-link").symlink_to(
                complete_package / "manifest.json"
            )
        except OSError as exc:
            pytest.skip(f"symlink is unavailable: {exc}")
    elif unsafe_kind == "hardlink":
        os.link(
            complete_package / "manifest.json",
            complete_package / "manifest-hardlink.json",
        )
    else:
        if os.name != "nt":
            pytest.skip("junction coverage is Windows-specific")
        outside = isolated_repo.parent / "junction-target"
        outside.mkdir()
        completed = subprocess.run(
            [
                "cmd",
                "/c",
                "mklink",
                "/J",
                str(complete_package / "unsafe-junction"),
                str(outside),
            ],
            capture_output=True,
            check=False,
            text=True,
        )
        if completed.returncode != 0:
            pytest.skip(f"junction is unavailable: {completed.stderr}")
    _guard_recomputation(monkeypatch)

    result = promote_archive(isolated_repo, STRATEGY_ID, RUN_ID, ANALYSIS_ID)

    assert result.status == "failed"
    assert result.reasons == ("unsafe_source_entry",)


def test_promote_reuses_identical_target_without_writing(
    complete_package: Path,
    isolated_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = promote_archive(isolated_repo, STRATEGY_ID, RUN_ID, ANALYSIS_ID)
    before = _tree_digests(first.target)
    _guard_recomputation(monkeypatch)
    from scripts.research.local_quant_research import archive

    monkeypatch.setattr(archive.shutil, "copyfileobj", _boom)
    monkeypatch.setattr(archive.os, "replace", _boom)

    second = promote_archive(isolated_repo, STRATEGY_ID, RUN_ID, ANALYSIS_ID)

    assert second.status == "complete"
    assert second.reused is True
    assert second.reasons == ()
    assert _tree_digests(second.target) == before


def test_promote_reports_conflict_without_changing_existing_target(
    complete_package: Path,
    isolated_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = promote_archive(isolated_repo, STRATEGY_ID, RUN_ID, ANALYSIS_ID)
    before = _tree_digests(first.target)
    (complete_package / "new-evidence.bin").write_bytes(b"different")
    _guard_recomputation(monkeypatch)
    from scripts.research.local_quant_research import archive

    monkeypatch.setattr(archive.shutil, "copyfileobj", _boom)
    monkeypatch.setattr(archive.os, "replace", _boom)

    result = promote_archive(isolated_repo, STRATEGY_ID, RUN_ID, ANALYSIS_ID)

    assert result.status == "conflict"
    assert result.reused is False
    assert result.reasons == ("target_conflict",)
    assert _tree_digests(first.target) == before


def test_existing_target_is_never_written(
    complete_package: Path,
    isolated_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = (
        isolated_repo
        / "joinquant/strategies/strategy-003/research/archives/baseline-v2"
    )
    target.mkdir(parents=True)
    marker = target / "user-owned.txt"
    marker.write_bytes(b"keep")
    _guard_recomputation(monkeypatch)
    from scripts.research.local_quant_research import archive

    monkeypatch.setattr(archive.shutil, "copyfileobj", _boom)
    monkeypatch.setattr(archive.os, "replace", _boom)

    result = promote_archive(isolated_repo, STRATEGY_ID, RUN_ID, ANALYSIS_ID)

    assert result.status == "conflict"
    assert marker.read_bytes() == b"keep"
    assert tuple(target.iterdir()) == (marker,)


def test_target_appearing_before_publish_does_not_leave_staging(
    complete_package: Path,
    isolated_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scripts.research.local_quant_research import archive

    target = (
        isolated_repo
        / f"joinquant/strategies/{STRATEGY_ID}/research/archives/{ANALYSIS_ID}"
    )
    real_validate_views = archive._validate_analysis_views

    def publish_competing_target(staging: Path) -> None:
        real_validate_views(staging)
        shutil.copytree(complete_package, target)

    monkeypatch.setattr(
        archive,
        "_validate_analysis_views",
        publish_competing_target,
    )

    result = promote_archive(isolated_repo, STRATEGY_ID, RUN_ID, ANALYSIS_ID)

    assert result.status == "complete"
    assert result.reused is True
    assert list(target.parent.glob(f".{ANALYSIS_ID}.*.tmp")) == []


def test_copy_interruption_cleans_only_its_staging_directory(
    complete_package: Path,
    isolated_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archives = (
        isolated_repo / "joinquant/strategies/strategy-003/research/archives"
    )
    archives.mkdir(parents=True)
    neighbor = archives / "neighbor.keep"
    neighbor.write_bytes(b"keep")
    source_before = _tree_digests(complete_package)
    _guard_recomputation(monkeypatch)
    from scripts.research.local_quant_research import archive

    real_copyfileobj = shutil.copyfileobj
    copies = 0

    def interrupted_copy(
        source: object,
        target: object,
        length: int = 0,
    ) -> None:
        nonlocal copies
        copies += 1
        if copies == 2:
            raise OSError("simulated copy interruption")
        real_copyfileobj(source, target, length)

    monkeypatch.setattr(archive.shutil, "copyfileobj", interrupted_copy)

    result = promote_archive(isolated_repo, STRATEGY_ID, RUN_ID, ANALYSIS_ID)

    assert result.status == "failed"
    assert result.reasons == ("copy_failed",)
    assert not (archives / ANALYSIS_ID).exists()
    assert list(archives.glob(f".{ANALYSIS_ID}.*.tmp")) == []
    assert neighbor.read_bytes() == b"keep"
    assert _tree_digests(complete_package) == source_before


def test_fdopen_failure_closes_duplicate_and_original_descriptors(
    complete_package: Path,
    isolated_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scripts.research.local_quant_research import archive

    snapshot = archive._scan_tree(complete_package)
    staging = isolated_repo / "staging"
    original_descriptors: list[int] = []
    duplicate_descriptors: list[int] = []
    real_open = os.open
    real_dup = os.dup
    real_close = os.close

    def record_open(*args: object, **kwargs: object) -> int:
        descriptor = real_open(*args, **kwargs)
        original_descriptors.append(descriptor)
        return descriptor

    def record_dup(descriptor: int) -> int:
        duplicate = real_dup(descriptor)
        duplicate_descriptors.append(duplicate)
        return duplicate

    def fail_fdopen(*_args: object, **_kwargs: object) -> object:
        raise OSError("simulated fdopen failure")

    monkeypatch.setattr(archive.os, "open", record_open)
    monkeypatch.setattr(archive.os, "dup", record_dup)
    monkeypatch.setattr(archive.os, "fdopen", fail_fdopen)
    try:
        with pytest.raises(OSError, match="simulated fdopen failure"):
            archive._copy_verified_tree(complete_package, staging, snapshot)

        assert len(original_descriptors) == 1
        assert len(duplicate_descriptors) == 1
        for descriptor in (*original_descriptors, *duplicate_descriptors):
            with pytest.raises(OSError):
                os.fstat(descriptor)
    finally:
        for descriptor in (*original_descriptors, *duplicate_descriptors):
            try:
                real_close(descriptor)
            except OSError:
                pass


def test_copy_failure_rolls_back_new_empty_archive_parents(
    complete_package: Path,
    isolated_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scripts.research.local_quant_research import archive

    strategy = isolated_repo / f"joinquant/strategies/{STRATEGY_ID}"
    assert not (strategy / "research").exists()
    _guard_recomputation(monkeypatch)

    def fail_copy(*_args: object, **_kwargs: object) -> None:
        raise OSError("simulated copy failure")

    monkeypatch.setattr(archive.shutil, "copyfileobj", fail_copy)

    result = promote_archive(isolated_repo, STRATEGY_ID, RUN_ID, ANALYSIS_ID)

    assert result.status == "failed"
    assert result.reasons == ("copy_failed",)
    assert not (strategy / "research").exists()


def test_cleanup_failure_returns_stable_reason_and_preserves_existing_parent(
    complete_package: Path,
    isolated_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scripts.research.local_quant_research import archive

    archives = isolated_repo / f"joinquant/strategies/{STRATEGY_ID}/research/archives"
    archives.mkdir(parents=True)
    neighbor = archives / "neighbor.keep"
    neighbor.write_bytes(b"keep")
    _guard_recomputation(monkeypatch)

    def fail_copy(*_args: object, **_kwargs: object) -> None:
        raise OSError("simulated copy failure")

    def fail_real_cleanup(
        _path: Path,
        *,
        ignore_errors: bool = False,
    ) -> None:
        if not ignore_errors:
            raise OSError("simulated cleanup failure")

    monkeypatch.setattr(archive.shutil, "copyfileobj", fail_copy)
    monkeypatch.setattr(archive.shutil, "rmtree", fail_real_cleanup)

    result = promote_archive(isolated_repo, STRATEGY_ID, RUN_ID, ANALYSIS_ID)

    assert result.status == "failed"
    assert result.reasons == ("cleanup_failed",)
    assert neighbor.read_bytes() == b"keep"
    assert archives.is_dir()


def test_source_change_after_validation_is_not_published(
    complete_package: Path,
    isolated_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scripts.research.local_quant_research import archive

    real_validate = archive.validate_result_package

    def mutate_after_validation(path: Path) -> object:
        manifest = real_validate(path)
        if Path(path) == complete_package:
            results = complete_package / "data/results.parquet"
            results.write_bytes(results.read_bytes() + b"changed-after-validation")
        return manifest

    monkeypatch.setattr(archive, "validate_result_package", mutate_after_validation)

    result = promote_archive(isolated_repo, STRATEGY_ID, RUN_ID, ANALYSIS_ID)

    assert result.status == "failed"
    assert result.reasons == ("copy_failed",)
    assert not result.target.exists()
    archives = result.target.parent
    assert list(archives.glob(f".{ANALYSIS_ID}.*.tmp")) == []


@pytest.mark.parametrize(
    "mutation", ("added_file", "added_empty_directory", "deleted_file")
)
def test_source_tree_change_after_initial_scan_is_not_published(
    complete_package: Path,
    isolated_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    from scripts.research.local_quant_research import archive

    real_scan = archive._scan_tree
    mutated = False

    def mutate_after_scan(root: Path) -> object:
        nonlocal mutated
        snapshot = real_scan(root)
        if Path(root) == complete_package and not mutated:
            mutated = True
            if mutation == "added_file":
                (complete_package / "added-after-scan.bin").write_bytes(b"added")
            elif mutation == "added_empty_directory":
                (complete_package / "added-after-scan").mkdir()
            else:
                (complete_package / "evidence/raw-bytes.bin").unlink()
        return snapshot

    _guard_recomputation(monkeypatch)
    monkeypatch.setattr(archive, "_scan_tree", mutate_after_scan)

    result = promote_archive(isolated_repo, STRATEGY_ID, RUN_ID, ANALYSIS_ID)

    assert result.status == "failed"
    assert not result.target.exists()


@pytest.mark.parametrize("mutation", ("changed", "deleted"))
def test_source_file_change_after_it_was_copied_is_not_published(
    complete_package: Path,
    isolated_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    from scripts.research.local_quant_research import archive

    first_source = complete_package / sorted(_tree_digests(complete_package))[0]
    real_copyfileobj = shutil.copyfileobj
    copies = 0

    def mutate_after_first_copy(
        source: object,
        target: object,
        length: int = 0,
    ) -> None:
        nonlocal copies
        copies += 1
        if copies == 2:
            if mutation == "changed":
                first_source.write_bytes(first_source.read_bytes() + b"changed")
            else:
                first_source.unlink()
        real_copyfileobj(source, target, length)

    _guard_recomputation(monkeypatch)
    monkeypatch.setattr(archive.shutil, "copyfileobj", mutate_after_first_copy)

    result = promote_archive(isolated_repo, STRATEGY_ID, RUN_ID, ANALYSIS_ID)

    assert result.status == "failed"
    assert not result.target.exists()


def test_source_file_inode_replacement_between_lstat_and_read_is_rejected(
    complete_package: Path,
    isolated_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scripts.research.local_quant_research import archive

    victim = complete_package / "evidence/raw-bytes.bin"
    replacement = isolated_repo.parent / "replacement.bin"
    replacement.write_bytes(victim.read_bytes())
    real_identity = archive._file_identity
    replaced = False

    def replace_before_read(path: Path, *args: object, **kwargs: object) -> object:
        nonlocal replaced
        if Path(path) == victim and not replaced:
            replaced = True
            os.replace(replacement, victim)
        return real_identity(path, *args, **kwargs)

    _guard_recomputation(monkeypatch)
    monkeypatch.setattr(archive, "_file_identity", replace_before_read)

    result = promote_archive(isolated_repo, STRATEGY_ID, RUN_ID, ANALYSIS_ID)

    assert result.status == "failed"
    assert result.reasons == ("unsafe_source_entry",)
    assert not result.target.exists()


@pytest.mark.skipif(os.name != "nt", reason="junction coverage is Windows-specific")
def test_source_directory_junction_replacement_after_lstat_is_rejected(
    complete_package: Path,
    isolated_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    victim = complete_package / "extensions/empty-evidence"
    outside = isolated_repo.parent / "replacement-directory"
    real_lstat = Path.lstat
    replaced = False

    def replace_after_lstat(path: Path) -> os.stat_result:
        nonlocal replaced
        metadata = real_lstat(path)
        if path == victim and not replaced:
            replaced = True
            _replace_directory_with_link(victim, outside, "junction")
        return metadata

    _guard_recomputation(monkeypatch)
    monkeypatch.setattr(Path, "lstat", replace_after_lstat)
    try:
        result = promote_archive(isolated_repo, STRATEGY_ID, RUN_ID, ANALYSIS_ID)

        assert result.status == "failed"
        assert result.reasons == ("unsafe_source_entry",)
        assert not result.target.exists()
    finally:
        if replaced:
            _restore_linked_directory(victim, outside)


@pytest.mark.parametrize("replacement_kind", ("symlink", "hardlink", "junction"))
def test_source_entry_replaced_with_link_before_copy_is_not_published(
    complete_package: Path,
    isolated_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    replacement_kind: str,
) -> None:
    from scripts.research.local_quant_research import archive

    victim = (
        complete_package / "extensions/empty-evidence"
        if replacement_kind == "junction"
        else complete_package / "evidence/raw-bytes.bin"
    )
    outside = isolated_repo.parent / "copy-replacement"
    real_scan = archive._scan_tree
    replaced = False

    def replace_after_scan(root: Path) -> object:
        nonlocal replaced
        snapshot = real_scan(root)
        if Path(root) == complete_package and not replaced:
            replaced = True
            if replacement_kind == "junction":
                _replace_directory_with_link(victim, outside, "junction")
            else:
                victim.rename(outside)
                if replacement_kind == "symlink":
                    try:
                        victim.symlink_to(outside)
                    except OSError as exc:
                        outside.rename(victim)
                        pytest.skip(f"file symlink is unavailable: {exc}")
                else:
                    os.link(outside, victim)
        return snapshot

    _guard_recomputation(monkeypatch)
    monkeypatch.setattr(archive, "_scan_tree", replace_after_scan)
    try:
        result = promote_archive(isolated_repo, STRATEGY_ID, RUN_ID, ANALYSIS_ID)

        assert result.status == "failed"
        assert not result.target.exists()
    finally:
        if replaced and _path_exists_for_test(victim):
            if replacement_kind == "junction":
                _restore_linked_directory(victim, outside)
            else:
                victim.unlink()
                outside.rename(victim)


def test_promoted_archive_remains_queryable_after_local_source_is_deleted(
    complete_package: Path,
    isolated_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _guard_recomputation(monkeypatch)
    result = promote_archive(isolated_repo, STRATEGY_ID, RUN_ID, ANALYSIS_ID)
    shutil.rmtree(isolated_repo / ".local")

    source = open_analysis_source(result.target)
    with open_analysis_database(result.target) as database:
        assert database.table_names == (
            "results",
            "balances",
            "positions",
            "orders",
        )
        assert database.connection.sql("select count(*) from results").fetchone() == (
            2,
        )
        assert database.extension("signals").fetchall() == [("signal-1", 0.5)]
    assert source.backend == "vectorbt"
    assert source.formula_version == "unified-strategy-analysis/1"


def test_unified_analysis_validation_failure_prevents_publish(
    complete_package: Path,
    isolated_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scripts.research import analysis_data

    def reject_staging(_path: Path) -> object:
        raise analysis_data.AnalysisManifestError(
            "simulated unified analysis failure"
        )

    _guard_recomputation(monkeypatch)
    monkeypatch.setattr(analysis_data, "open_analysis_database", reject_staging)

    result = promote_archive(isolated_repo, STRATEGY_ID, RUN_ID, ANALYSIS_ID)

    assert result.status == "failed"
    assert not result.target.exists()


def test_complete_staging_passes_the_real_unified_analysis_entry(
    complete_package: Path,
    isolated_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scripts.research import analysis_data

    real_open = analysis_data.open_analysis_database
    validated: list[Path] = []

    def record_real_validation(path: Path) -> object:
        validated.append(Path(path))
        return real_open(path)

    _guard_recomputation(monkeypatch)
    monkeypatch.setattr(
        analysis_data,
        "open_analysis_database",
        record_real_validation,
    )

    result = promote_archive(isolated_repo, STRATEGY_ID, RUN_ID, ANALYSIS_ID)

    assert result.status == "complete"
    assert len(validated) == 1
    assert validated[0].parent == result.target.parent
    assert validated[0].name.startswith(f".{ANALYSIS_ID}.")
    assert validated[0].name.endswith(".tmp")


def test_prepublish_uses_authoritative_validator_once_per_tree(
    complete_package: Path,
    isolated_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scripts.research.analysis_data import manifest as analysis_manifest
    from scripts.research.local_quant_research import archive

    real_validate = archive.validate_result_package
    validated: list[Path] = []

    def record_validation(path: Path) -> object:
        validated.append(Path(path))
        return real_validate(path)

    monkeypatch.setattr(archive, "validate_result_package", record_validation)
    monkeypatch.setattr(
        analysis_manifest,
        "validate_result_package",
        record_validation,
    )

    result = promote_archive(isolated_repo, STRATEGY_ID, RUN_ID, ANALYSIS_ID)

    assert result.status == "complete"
    assert validated.count(complete_package) == 1
    staging_validations = [path for path in validated if path != complete_package]
    assert len(staging_validations) == 1
    assert staging_validations[0].parent == result.target.parent
    assert staging_validations[0].name.startswith(f".{ANALYSIS_ID}.")


def test_prepublish_analysis_queries_one_row_per_relation(
    complete_package: Path,
    isolated_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scripts.research import analysis_data

    real_open = analysis_data.open_analysis_database
    fetched: list[tuple[str, int | None]] = []

    def probe_relation(
        relation: Any,
        name: str,
        *,
        limit_count: int | None = None,
    ) -> SimpleNamespace:
        def limit(count: int) -> SimpleNamespace:
            return probe_relation(
                relation.limit(count),
                name,
                limit_count=count,
            )

        def fetchall() -> object:
            fetched.append((name, limit_count))
            return relation.fetchall()

        return SimpleNamespace(limit=limit, fetchall=fetchall)

    @contextmanager
    def record_bounded_queries(path: Path) -> Iterator[object]:
        with real_open(path) as database:
            connection = SimpleNamespace(
                table=lambda name: probe_relation(
                    database.connection.table(name),
                    f"core:{name}",
                )
            )
            yield SimpleNamespace(
                source=database.source,
                table_names=database.table_names,
                connection=connection,
                extension=lambda name: probe_relation(
                    database.extension(name),
                    f"extension:{name}",
                ),
            )

    monkeypatch.setattr(
        analysis_data,
        "open_analysis_database",
        record_bounded_queries,
    )

    result = promote_archive(isolated_repo, STRATEGY_ID, RUN_ID, ANALYSIS_ID)

    assert result.status == "complete"
    assert fetched == [
        (f"core:{name}", 1)
        for name in analysis_data.LOCAL_PHYSICAL_DATASETS
    ] + [("extension:signals", 1)]


@pytest.mark.parametrize(
    ("status", "exit_code"),
    (("complete", 0), ("conflict", 1), ("failed", 2)),
)
def test_promote_cli_uses_exact_arguments_sorted_json_and_exit_codes(
    isolated_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    status: str,
    exit_code: int,
) -> None:
    from scripts.research.local_quant_research import cli

    source = isolated_repo / ".local/quant-research/strategy-003" / RUN_ID
    target = (
        isolated_repo
        / "joinquant/strategies/strategy-003/research/archives/baseline-v2"
    )
    calls: list[tuple[Path, str, str, str]] = []

    def fake_promote(
        repo_root: Path,
        strategy_id: str,
        run_id: str,
        analysis_id: str,
    ) -> ArchiveResult:
        calls.append((repo_root, strategy_id, run_id, analysis_id))
        return ArchiveResult(
            status=status,
            reused=status == "complete",
            source=source,
            target=target,
            reasons=() if status == "complete" else (f"{status}_reason",),
        )

    monkeypatch.setattr(cli, "REPO_ROOT", isolated_repo)
    monkeypatch.setattr(cli, "promote_archive", fake_promote)

    actual = cli.main(
        [
            "promote",
            "--strategy-id",
            STRATEGY_ID,
            "--run-id",
            RUN_ID,
            "--analysis-id",
            ANALYSIS_ID,
        ]
    )

    assert actual == exit_code
    assert calls == [(isolated_repo, STRATEGY_ID, RUN_ID, ANALYSIS_ID)]
    expected = {
        "reasons": [] if status == "complete" else [f"{status}_reason"],
        "reused": status == "complete",
        "source": str(source),
        "status": status,
        "target": str(target),
    }
    assert capsys.readouterr().out == (
        json.dumps(expected, ensure_ascii=False, sort_keys=True) + "\n"
    )


@pytest.mark.parametrize(
    "arguments",
    (
        ["promote", "--strategy-id", STRATEGY_ID, "--run-id", RUN_ID],
        [
            "promote",
            "--strategy-id",
            STRATEGY_ID,
            "--run-id",
            RUN_ID,
            "--analysis-id",
            ANALYSIS_ID,
            "--target",
            "elsewhere",
        ],
    ),
)
def test_promote_cli_rejects_missing_or_extra_public_arguments(
    arguments: list[str],
) -> None:
    from scripts.research.local_quant_research.cli import _parser

    with pytest.raises(SystemExit) as error:
        _parser().parse_args(arguments)

    assert error.value.code == 2


def test_promote_cli_does_not_import_the_run_or_recompute_paths(
    isolated_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cli_name = "scripts.research.local_quant_research.cli"
    archive_module = importlib.import_module(
        "scripts.research.local_quant_research.archive"
    )
    result = ArchiveResult(
        status="complete",
        reused=False,
        source=None,
        target=None,
        reasons=(),
    )
    real_import: Callable[..., object] = builtins.__import__

    def guarded_import(
        name: str,
        globals_: object = None,
        locals_: object = None,
        fromlist: object = (),
        level: int = 0,
    ) -> object:
        if name.rsplit(".", 1)[-1] in {
            "runner",
            "strategy_loader",
            "vectorbt_runtime",
        }:
            raise AssertionError(f"promote imported forbidden module: {name}")
        return real_import(name, globals_, locals_, fromlist, level)

    with monkeypatch.context() as context:
        context.setattr(archive_module, "promote_archive", lambda *_args: result)
        context.setattr(builtins, "__import__", guarded_import)
        sys.modules.pop(cli_name, None)
        cli = importlib.import_module(cli_name)
        context.setattr(cli, "REPO_ROOT", isolated_repo)

        assert cli.main(
            [
                "promote",
                "--strategy-id",
                STRATEGY_ID,
                "--run-id",
                RUN_ID,
                "--analysis-id",
                ANALYSIS_ID,
            ]
        ) == 0
    sys.modules.pop(cli_name, None)
    importlib.import_module(cli_name)
