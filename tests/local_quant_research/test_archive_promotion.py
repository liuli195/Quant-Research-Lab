from __future__ import annotations

import builtins
import importlib
import json
import sys
from types import SimpleNamespace
from pathlib import Path
from typing import Callable

import pytest

from scripts.research.local_quant_research.archive import (
    ArchiveResult,
    promote_archive,
)


STRATEGY_ID = "strategy-003"
RUN_ID = "a" * 64
ANALYSIS_ID = "baseline-v2"


def _boom(*_args: object, **_kwargs: object) -> None:
    raise AssertionError("archive promotion must not recompute results")


def _guard_recomputation(monkeypatch: pytest.MonkeyPatch) -> None:
    import vectorbt as vbt
    from scripts.research import result_package
    from scripts.research.local_quant_research import runner, strategy_loader

    monkeypatch.setattr(strategy_loader, "load_strategy", _boom)
    monkeypatch.setattr(vbt.Portfolio, "from_order_func", _boom)
    monkeypatch.setattr(result_package.pq, "write_table", _boom)
    monkeypatch.setattr(result_package, "write_result_package", _boom)
    monkeypatch.setattr(runner, "run_project", _boom)
    monkeypatch.setitem(sys.modules, "scripts.research.local_quant_research.vectorbt_runtime",
                        SimpleNamespace(run_vectorbt=_boom))


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
    repo_root: Path,
    analysis_id: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _guard_recomputation(monkeypatch)

    result = promote_archive(repo_root, STRATEGY_ID, RUN_ID, analysis_id)

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
    repo_root: Path,
    strategy_id: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _guard_recomputation(monkeypatch)

    result = promote_archive(repo_root, strategy_id, RUN_ID, ANALYSIS_ID)

    assert result.status == "failed"
    assert result.reasons == ("invalid_strategy_id",)


@pytest.mark.parametrize(
    "run_id", ("A" * 64, "a" * 63, "a" * 65, "../escape", "a/b", "")
)
def test_promote_rejects_invalid_run_identity(
    repo_root: Path,
    run_id: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _guard_recomputation(monkeypatch)

    result = promote_archive(repo_root, STRATEGY_ID, run_id, ANALYSIS_ID)

    assert result.status == "failed"
    assert result.reasons == ("invalid_run_id",)


@pytest.mark.parametrize(
    ("status", "exit_code"),
    (("complete", 0), ("conflict", 1), ("failed", 2)),
)
def test_promote_cli_uses_exact_arguments_sorted_json_and_exit_codes(
    repo_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    status: str,
    exit_code: int,
) -> None:
    from scripts.research.local_quant_research import archive, cli

    source = repo_root / ".local/quant-research/strategy-003" / RUN_ID
    target = (
        repo_root
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

    monkeypatch.setattr(cli, "REPO_ROOT", repo_root)
    monkeypatch.setattr(archive, "promote_archive", fake_promote)

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
    assert calls == [(repo_root, STRATEGY_ID, RUN_ID, ANALYSIS_ID)]
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
    repo_root: Path,
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
        context.setattr(cli, "REPO_ROOT", repo_root)

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
