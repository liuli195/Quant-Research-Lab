from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest


def test_query_rejects_failed_manifest_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    import joinquant_sync.query as query

    monkeypatch.setattr(query, "verify_existing_manifest", lambda _root: {"gate": {"status": "fail"}})
    with pytest.raises(query.QueryError, match="manifest gate did not pass"):
        query.open_views(Path("unused-manifest.json"), object())


def test_cli_returns_immediately_after_matching_command(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import jq_sync

    class CommandProbe:
        matched = False

        def __eq__(self, other: object) -> bool:
            if self.matched:
                raise AssertionError("command dispatch continued after a successful match")
            if other == "query":
                self.matched = True
                return True
            return False

    arguments = SimpleNamespace(
        command=CommandProbe(),
        object="unused-archive/manifest.json",
        dataset="orders",
        limit=10,
    )
    parser = SimpleNamespace(parse_args=lambda _argv: arguments)
    monkeypatch.setattr(jq_sync, "build_parser", lambda: parser)
    monkeypatch.setattr(jq_sync, "query_rows", lambda *_args: [{"id": 1}])

    assert jq_sync.main([]) == 0
    assert json.loads(capsys.readouterr().out) == [{"id": 1}]
