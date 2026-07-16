from __future__ import annotations

import builtins
import hashlib
import importlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Callable

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


@pytest.mark.parametrize(
    "analysis_id", ("Upper", "../escape", "a/b", "", "x" * 65)
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
    ("Upper", "../escape", "a/b", "", "x" * 65, "strategy-999"),
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

    monkeypatch.setattr(archive.shutil, "copyfile", _boom)
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

    monkeypatch.setattr(archive.shutil, "copyfile", _boom)
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

    monkeypatch.setattr(archive.shutil, "copyfile", _boom)
    monkeypatch.setattr(archive.os, "replace", _boom)

    result = promote_archive(isolated_repo, STRATEGY_ID, RUN_ID, ANALYSIS_ID)

    assert result.status == "conflict"
    assert marker.read_bytes() == b"keep"
    assert tuple(target.iterdir()) == (marker,)


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

    real_copyfile = shutil.copyfile
    copies = 0

    def interrupted_copy(source: Path, target: Path) -> str:
        nonlocal copies
        copies += 1
        if copies == 2:
            raise OSError("simulated copy interruption")
        return real_copyfile(source, target)

    monkeypatch.setattr(archive.shutil, "copyfile", interrupted_copy)

    result = promote_archive(isolated_repo, STRATEGY_ID, RUN_ID, ANALYSIS_ID)

    assert result.status == "failed"
    assert result.reasons == ("copy_failed",)
    assert not (archives / ANALYSIS_ID).exists()
    assert list(archives.glob(f".{ANALYSIS_ID}.*.tmp")) == []
    assert neighbor.read_bytes() == b"keep"
    assert _tree_digests(complete_package) == source_before


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
