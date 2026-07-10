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


def test_full_page_without_end_evidence_is_not_complete() -> None:
    from joinquant_sync.research import PaginationIncomplete, collect_pages

    pages = iter([{"rows": [{"id": 1}], "next": None, "page_full": True}])
    with pytest.raises(PaginationIncomplete):
        collect_pages(lambda _cursor: next(pages))


def test_collect_pages_matches_real_1000_289_empty_shape() -> None:
    from joinquant_sync.research import collect_pages

    pages = {
        None: {"rows": [{"id": index} for index in range(1000)], "next": "1000"},
        "1000": {
            "rows": [{"id": index} for index in range(1000, 1289)],
            "next": "1289",
        },
        "1289": {"rows": [], "next": None},
    }

    rows, pagination = collect_pages(lambda cursor: pages[cursor])

    assert len(rows) == 1289
    assert pagination == {
        "complete": True,
        "end": "empty_page",
        "pages": 3,
        "rows": 1289,
        "cursors": [None, "1000", "1289"],
    }


def test_empty_page_cannot_override_unmet_declared_total() -> None:
    from joinquant_sync.research import PaginationIncomplete, collect_pages

    pages = {
        None: {"rows": [{"id": index} for index in range(1000)], "next": "1000", "total": 1289},
        "1000": {"rows": [], "next": None, "total": 1289},
    }
    with pytest.raises(PaginationIncomplete):
        collect_pages(lambda cursor: pages[cursor])


def test_failed_run_accepts_verified_empty_table() -> None:
    from joinquant_sync.research import validate_fact_table

    result = validate_fact_table("risk", [], "failed", {"end": "empty_page"})
    assert result["status"] == "complete"
    assert result["verified_empty"] is True


def test_done_run_rejects_unexplained_empty_required_table() -> None:
    from joinquant_sync.research import FactValidationError, validate_fact_table

    with pytest.raises(FactValidationError):
        validate_fact_table("results", [], "done", {"end": "empty_page"})


def test_fact_table_rejects_duplicate_or_unsorted_rows() -> None:
    from joinquant_sync.research import FactValidationError, validate_fact_table

    pagination = {"end": "empty_page"}
    duplicate = [
        {"time": "2026-01-01", "returns": 0.1, "benchmark_returns": 0.0},
        {"time": "2026-01-01", "returns": 0.1, "benchmark_returns": 0.0},
    ]
    with pytest.raises(FactValidationError):
        validate_fact_table("results", duplicate, "done", pagination)
    unsorted = [
        {"time": "2026-01-02", "returns": 0.1, "benchmark_returns": 0.0},
        {"time": "2026-01-01", "returns": 0.2, "benchmark_returns": 0.0},
    ]
    with pytest.raises(FactValidationError):
        validate_fact_table("results", unsorted, "done", pagination)


def test_inventory_drift_blocks_second_unstable_batch() -> None:
    from joinquant_sync.research import InventoryChanged, sync_with_fence

    inventories = iter([{"rev": 1}, {"rev": 2}, {"rev": 2}, {"rev": 3}])
    with pytest.raises(InventoryChanged):
        sync_with_fence(lambda: next(inventories), lambda: object())


def test_inventory_drift_retries_once_and_returns_stable_result() -> None:
    from joinquant_sync.research import sync_with_fence

    inventories = iter([{"rev": 1}, {"rev": 2}, {"rev": 2}, {"rev": 2}])
    collected = iter(["first", "second"])
    assert sync_with_fence(lambda: next(inventories), lambda: next(collected)) == "second"


def _make_log_fetcher(count: int, probe: str):
    rows = [{"seq": index} for index in range(count)]

    def fetch(offset: int):
        if offset < len(rows):
            return {"rows": rows[offset : offset + 1000], "end": False}
        if probe == "empty":
            return {"rows": [], "end": True}
        return {"rows": [], "end": False, "blocked_free": True}

    return fetch


@pytest.mark.parametrize(
    ("count", "probe", "expected"),
    [(999, "empty", "complete"), (1000, "empty", "complete"), (1000, "blocked", "capped_free")],
)
def test_free_log_boundary(count: int, probe: str, expected: str) -> None:
    from joinquant_sync.browser import collect_free_logs

    _, status = collect_free_logs(_make_log_fetcher(count=count, probe=probe))
    assert status == expected


def test_free_page_after_1000_continues() -> None:
    from joinquant_sync.browser import collect_free_logs

    rows, status = collect_free_logs(_make_log_fetcher(count=1001, probe="empty"))
    assert len(rows) == 1001
    assert status == "complete"


def test_paid_preview_is_bound_and_one_time(tmp_path: Path) -> None:
    from joinquant_sync.browser import (
        PaidConfirmationRequired,
        consume_paid_preview,
        create_paid_preview,
    )

    quote = {"credits": 3, "rows": 1200}
    preview = create_paid_preview(
        "run-1", "normal_log", "1000:1200", quote, store_dir=tmp_path
    )
    used: set[str] = set()
    with pytest.raises(PaidConfirmationRequired):
        consume_paid_preview(
            preview, "run-1", "normal_log", "1000:1200", quote, False, used, store_dir=tmp_path
        )
    with pytest.raises(PaidConfirmationRequired):
        consume_paid_preview(
            preview,
            "run-1",
            "normal_log",
            "1000:1200",
            {"credits": 4, "rows": 1200},
            True,
            used,
            store_dir=tmp_path,
        )
    assert consume_paid_preview(
        preview,
        "run-1",
        "normal_log",
        "1000:1200",
        quote,
        True,
        used,
        store_dir=tmp_path,
    )["preview_id"] == preview["preview_id"]
    with pytest.raises(PaidConfirmationRequired):
        consume_paid_preview(
            preview,
            "run-1",
            "normal_log",
            "1000:1200",
            quote,
            True,
            set(),
            store_dir=tmp_path,
        )
    forged = dict(preview, run_id="run-2")
    with pytest.raises(PaidConfirmationRequired):
        consume_paid_preview(
            forged,
            "run-2",
            "normal_log",
            "1000:1200",
            quote,
            True,
            set(),
            store_dir=tmp_path,
        )


def test_active_simulation_rows_separate_stable_identity_from_aliases() -> None:
    from joinquant_sync.browser import parse_active_simulation_rows

    rows = [
        {
            "status": "1",
            "name": "etf_factor_rotation",
            "page_space_id": "2901335",
            "detail_url": "/algorithm/live/index?backtestId=rotating-detail",
            "transport_id": "rotating-result",
        },
        {
            "status": "2",
            "name": "closed",
            "page_space_id": "10",
            "detail_url": "/algorithm/live/index?backtestId=closed",
            "transport_id": "closed",
        },
    ]
    candidates = parse_active_simulation_rows(rows)
    assert candidates == [
        {
            "page_ordinal": "1",
            "name": "etf_factor_rotation",
            "page_space_id": "2901335",
            "status": "active",
            "detail_url": "https://www.joinquant.com/algorithm/live/index?backtestId=rotating-detail",
            "aliases": ["rotating-detail", "rotating-result"],
        }
    ]
