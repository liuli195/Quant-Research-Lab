from __future__ import annotations

import hashlib
import json
from contextlib import contextmanager
from pathlib import Path

import pytest


class FakePage:
    url = "https://www.joinquant.com/user/login/index"


def test_login_redirect_is_auth_required() -> None:
    from joinquant_sync.browser import AuthRequired, ensure_authenticated

    with pytest.raises(AuthRequired):
        ensure_authenticated(FakePage())


def test_stage_external_file_preserves_bytes_and_sha256(tmp_path: Path) -> None:
    from joinquant_sync.archive import stage_external_file

    source = tmp_path / "export.json"
    source.write_bytes(b'{"ok":true}')

    item = stage_external_file(source, tmp_path / "stage")

    assert Path(str(item["path"])).read_bytes() == source.read_bytes()
    assert item["sha256"] == hashlib.sha256(source.read_bytes()).hexdigest()


def test_stage_only_sync_requires_explicit_target() -> None:
    from jq_sync import main

    assert (
        main(
            [
                "sync-backtest",
                "--strategy",
                "strategy-001",
                "--stage-only",
                ".local/poc",
            ]
        )
        == 2
    )


def test_persistent_context_and_download_capture(tmp_path: Path) -> None:
    from joinquant_sync.browser import capture_download, open_authenticated_context

    profile = tmp_path / "profile"
    destination = tmp_path / "export.json"
    with open_authenticated_context(profile, headless=True) as context:
        page = context.pages[0]
        page.set_content(
            '<a id="download" download="export.json" '
            'href="data:application/json,%7B%22ok%22%3Atrue%7D">download</a>'
        )
        captured = capture_download(
            page,
            lambda: page.click("#download"),
            destination,
        )

    assert captured == destination
    assert destination.read_bytes() == b'{"ok":true}'


def test_persistent_context_loads_external_storage_state(tmp_path: Path) -> None:
    from joinquant_sync.browser import open_authenticated_context

    profile = tmp_path / "profile"
    profile.mkdir()
    (profile / "storage-state.json").write_text(
        json.dumps(
            {
                "cookies": [
                    {
                        "name": "session",
                        "value": "temporary-test-value",
                        "domain": "example.com",
                        "path": "/",
                        "expires": -1,
                        "httpOnly": True,
                        "secure": True,
                        "sameSite": "Lax",
                    }
                ],
                "origins": [],
            }
        ),
        encoding="utf-8",
    )

    with open_authenticated_context(profile, headless=True) as context:
        cookies = context.cookies("https://example.com")

    assert [(cookie["name"], cookie["value"]) for cookie in cookies] == [
        ("session", "temporary-test-value")
    ]


def test_verify_import_stages_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from jq_sync import main

    source = tmp_path / "manual.json"
    source.write_bytes(b'{"source":"manual"}')
    stage = tmp_path / "stage"

    assert (
        main(
            [
                "verify",
                "--import-file",
                str(source),
                "--stage-only",
                str(stage),
            ]
        )
        == 0
    )
    item = json.loads(capsys.readouterr().out)
    assert Path(item["path"]).read_bytes() == source.read_bytes()
    assert item["sha256"] == hashlib.sha256(source.read_bytes()).hexdigest()


def test_export_structured_backtest_stages_download(tmp_path: Path) -> None:
    from joinquant_sync.browser import open_authenticated_context
    from joinquant_sync.research import export_structured_backtest

    with open_authenticated_context(tmp_path / "profile", headless=True) as context:
        page = context.pages[0]
        page.set_content("<body></body>")
        items = export_structured_backtest(
            page,
            "data:application/json,%7B%22status%22%3A%22done%22%7D",
            tmp_path / "stage",
        )

    assert len(items) == 1
    assert Path(items[0]["path"]).read_bytes() == b'{"status":"done"}'


def test_auth_uses_external_persistent_profile(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import jq_sync

    class FakePage:
        url = "about:blank"

        def goto(self, url: str) -> None:
            self.url = url

    class FakeContext:
        pages = [FakePage()]

        def storage_state(self, *, path: Path) -> None:
            opened["state_path"] = path

    opened: dict[str, object] = {}

    @contextmanager
    def fake_context(profile_dir: Path, *, headless: bool):
        opened.update(profile_dir=profile_dir, headless=headless)
        yield FakeContext()

    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setattr(jq_sync, "open_authenticated_context", fake_context)

    assert jq_sync.main(["auth", "--headless", "--timeout-seconds", "0"]) == 0
    assert opened == {
        "profile_dir": tmp_path / "QuantResearchLab" / "joinquant-playwright",
        "headless": True,
        "state_path": tmp_path
        / "QuantResearchLab"
        / "joinquant-playwright"
        / "storage-state.json",
    }
    assert json.loads(capsys.readouterr().out)["status"] == "authenticated"
