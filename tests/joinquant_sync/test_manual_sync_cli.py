from __future__ import annotations

from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.mark.parametrize(
    "argv",
    [
        ["scheduled-sync-pr", "--repository", "D:/repo"],
        ["schedule-install"],
        ["schedule-status"],
        ["schedule-uninstall"],
    ],
)
def test_cli_rejects_removed_scheduling_commands(argv: list[str]) -> None:
    import jq_sync

    assert jq_sync.main(argv) == 2


@pytest.mark.parametrize(
    ("status", "exit_code"), [("committed", 0), ("failed", 1)]
)
def test_manual_active_sync_returns_status(
    monkeypatch: pytest.MonkeyPatch, status: str, exit_code: int
) -> None:
    import jq_sync

    monkeypatch.setattr(
        jq_sync,
        "open_authenticated_context",
        lambda *_args, **_kwargs: nullcontext(SimpleNamespace(pages=[object()])),
    )
    monkeypatch.setattr(
        jq_sync,
        "sync_all_active_simulations",
        lambda _page, repository: [
            {"status": status, "repository": str(repository)}
        ],
    )

    assert (
        jq_sync.main(
            ["sync-active-simulations", "--repository", str(Path.cwd())]
        )
        == exit_code
    )
