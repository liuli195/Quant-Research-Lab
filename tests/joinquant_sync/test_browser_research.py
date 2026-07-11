from __future__ import annotations

import hashlib
import gzip
import json
import os
from contextlib import contextmanager
from pathlib import Path

import pytest


class FakePage:
    url = "https://www.joinquant.com/user/login/index"


def test_login_redirect_is_auth_required() -> None:
    from joinquant_sync.browser import AuthRequired, ensure_authenticated

    with pytest.raises(AuthRequired):
        ensure_authenticated(FakePage())


def test_research_export_filters_time_series_after_saved_cursors() -> None:
    from joinquant_sync.research_cloud import build_research_export_script

    script = build_research_export_script(
        "backtest", "export.json", after_times={"results": "2026-07-10"}
    )
    assert '"results": "2026-07-10"' in script
    assert "_incremental" in script
    assert 'str(row.get("time")) >= cursor' in script
    assert '"params": _safe("get_params", gt.get_params)' in script
    assert '"status": _safe("get_status", gt.get_status)' in script


def test_research_transport_leaves_xsrf_to_jupyter_ajax() -> None:
    import joinquant_sync.research_cloud as research_cloud

    scripts = research_cloud._EXECUTE_JS + research_cloud._FILE_JS
    assert "document.cookie" not in scripts
    assert 'require(["base/js/utils"]' in scripts
    assert "utils.ajax" in scripts


def test_research_fetch_reads_all_requested_attribution_sources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import joinquant_sync.research_cloud as research_cloud

    class Frame:
        def evaluate(self, _script: str, _payload: dict[str, object]) -> dict[str, object]:
            return {"ok": True}

    def read_file(_frame: object, path: str, *, remove: bool) -> str:
        if remove:
            return json.dumps({"metadata": {}, "results": []})

    def read_files(_frame: object, paths: list[str]) -> dict[str, str]:
        return {
            "audit/run-old.jsonl": '{"audit_token":"run-old"}\n',
            "audit/run-new.jsonl": '{"audit_token":"run-new"}\n',
        }

    monkeypatch.setattr(research_cloud, "_research_frame", lambda _page: Frame())
    monkeypatch.setattr(research_cloud, "_read_research_file", read_file)
    monkeypatch.setattr(research_cloud, "_read_research_files", read_files)

    result = research_cloud.fetch_research_backtest(
        object(),
        "simulation",
        attribution_paths=["audit/run-old.jsonl", "audit/run-new.jsonl"],
    )

    assert result["attributions"] == {
        "audit/run-old.jsonl": b'{"audit_token":"run-old"}\n',
        "audit/run-new.jsonl": b'{"audit_token":"run-new"}\n',
    }


def test_simulation_history_fetches_each_distinct_source_code() -> None:
    from joinquant_sync.browser import fetch_simulation_code_versions

    class Page:
        def __init__(self) -> None:
            self.urls: list[str] = []

        def evaluate(self, _script: str, payload: dict[str, str]) -> dict[str, object]:
            self.urls.append(payload["url"])
            source_id = payload["url"].split("=")[-1]
            return {
                "ok": True,
                "value": {"data": {"source": f"# code {source_id}\n"}},
                "raw_text": "raw",
            }

    page = Page()
    result = fetch_simulation_code_versions(
        page,
        [
            {"sourceBacktestId": "new"},
            {"sourceBacktestId": "old"},
            {"sourceBacktestId": "new"},
        ],
    )

    assert result == ["# code new\n", "# code old\n"]
    assert page.urls == [
        "/algorithm/backtest/source?backtestId=new",
        "/algorithm/backtest/source?backtestId=old",
    ]


def test_log_transport_captures_original_response_text() -> None:
    import joinquant_sync.browser as browser

    assert "responseText" in browser._CY_AJAX_JS
    assert "document.cookie" not in browser._CY_AJAX_JS


def test_malformed_production_log_response_is_persisted_before_failure(
    tmp_path: Path,
) -> None:
    from joinquant_sync.browser import FreeLogIncomplete
    from joinquant_sync.sync_pipeline import persist_failure_evidence

    error = FreeLogIncomplete(
        "malformed",
        raw_pages=[{"offset": 1000, "raw_text": '{"ok":1}\nBROKEN'}],
    )
    evidence = persist_failure_evidence(tmp_path, error, identity="run-1")
    assert evidence is not None
    with gzip.open(Path(evidence["path"]), "rt", encoding="utf-8") as stream:
        payload = json.load(stream)
    assert payload["raw_pages"][0]["raw_text"] == '{"ok":1}\nBROKEN'
    assert payload["recovery"][0]["rows"] == [{"ok": 1}]
    assert payload["recovery"][0]["errors"]


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


def test_persistent_context_ignores_exported_storage_state(tmp_path: Path) -> None:
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

    assert cookies == []


@pytest.mark.skipif(os.name != "nt", reason="Windows DPAPI only")
def test_authenticated_session_is_encrypted_and_restored(tmp_path: Path) -> None:
    from joinquant_sync.browser import (
        persist_authenticated_session,
        restore_authenticated_session,
    )

    secret = "temporary-session-secret"
    joinquant_cookie = {
        "name": "session",
        "value": secret,
        "domain": ".joinquant.com",
        "path": "/",
    }

    class FakeContext:
        def __init__(self, cookies: list[dict[str, object]] | None = None) -> None:
            self._cookies = list(cookies or [])
            self.restored: list[dict[str, object]] = []

        def cookies(self) -> list[dict[str, object]]:
            return self._cookies

        def add_cookies(self, cookies: list[dict[str, object]]) -> None:
            self.restored.extend(cookies)

    profile = tmp_path / "profile"
    persist_authenticated_session(
        FakeContext(
            [
                joinquant_cookie,
                {
                    "name": "unrelated",
                    "value": "do-not-save",
                    "domain": "example.com",
                    "path": "/",
                },
            ]
        ),
        profile,
    )

    encrypted = (profile / "joinquant-session.dpapi").read_bytes()
    assert secret.encode() not in encrypted
    restored = FakeContext()
    assert restore_authenticated_session(restored, profile) is True
    assert restored.restored == [joinquant_cookie]


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

        def cookies(self) -> list[dict[str, object]]:
            return [
                {
                    "name": "session",
                    "value": "test-only",
                    "domain": ".joinquant.com",
                    "path": "/",
                }
            ]

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
    }
    assert (
        tmp_path
        / "QuantResearchLab"
        / "joinquant-playwright"
        / "joinquant-session.dpapi"
    ).is_file()
    assert json.loads(capsys.readouterr().out)["status"] == "authenticated"


def test_auth_rejects_profile_inside_repository(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import jq_sync

    @contextmanager
    def unexpected_context(*_args: object, **_kwargs: object):
        raise AssertionError("browser must not open for an unsafe profile")
        yield

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(jq_sync, "open_authenticated_context", unexpected_context)
    assert jq_sync.main(["auth", "--profile", str(tmp_path / ".browser")]) == 2
    assert json.loads(capsys.readouterr().out) == {
        "status": "invalid_profile",
        "message": "--profile must be outside the repository",
    }


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
        None: {
            "rows": [{"id": index} for index in range(1000)],
            "next": "1000",
            "total": 1289,
        },
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
    assert (
        sync_with_fence(lambda: next(inventories), lambda: next(collected)) == "second"
    )


def test_research_change_diagnostics_name_only_stable_sections() -> None:
    from joinquant_sync.sync_pipeline import _research_changed_sections

    before = {
        "bundle": {
            "metadata": {"generated_at": "first", "schema_version": 1},
            "results": [{"time": "2026-01-01", "value": 1}],
        },
        "attribution": b"same",
    }
    after = {
        "bundle": {
            "metadata": {"generated_at": "second", "schema_version": 1},
            "results": [{"time": "2026-01-01", "value": 2}],
        },
        "attribution": b"same",
    }

    assert _research_changed_sections(before, after) == ["results"]


def test_research_change_diagnostics_name_metadata_field() -> None:
    from joinquant_sync.sync_pipeline import _research_changed_sections

    before = {"bundle": {"metadata": {"schema_version": 1}}}
    after = {"bundle": {"metadata": {"schema_version": 2}}}

    assert _research_changed_sections(before, after) == ["metadata.schema_version"]


def test_research_fence_ignores_rotating_backtest_alias() -> None:
    from joinquant_sync.sync_pipeline import (
        _research_changed_sections,
        _research_remote_fingerprint,
    )

    before = {
        "bundle": {
            "metadata": {"backtest_id": "alias-one", "schema_version": 1},
            "results": [{"time": "2026-01-01", "value": 1}],
        }
    }
    after = {
        "bundle": {
            "metadata": {"backtest_id": "alias-two", "schema_version": 1},
            "results": [{"time": "2026-01-01", "value": 1}],
        }
    }

    assert _research_remote_fingerprint(before) == _research_remote_fingerprint(after)
    assert _research_changed_sections(before, after) == []


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
    [
        (999, "empty", "complete"),
        (1000, "empty", "complete"),
        (1000, "blocked", "capped_free"),
    ],
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


def test_simulation_log_probes_and_keeps_free_rows_after_1000() -> None:
    from joinquant_sync.browser import collect_simulation_logs

    calls: list[tuple[int, int]] = []

    def fetch_older(offset: int, limit: int) -> dict[str, object]:
        calls.append((offset, limit))
        return {"rows": [f"older-{index}" for index in range(limit)]}

    records, status = collect_simulation_logs(
        100,
        [f"latest-{index}" for index in range(1000)],
        fetch_older,
    )

    assert calls == [(0, 100)]
    assert len(records) == 1100
    assert status == "complete"


def test_simulation_log_marks_cap_only_after_blocked_probe() -> None:
    from joinquant_sync.browser import collect_simulation_logs

    calls: list[tuple[int, int]] = []

    def blocked(offset: int, limit: int) -> dict[str, object]:
        calls.append((offset, limit))
        return {"rows": [], "blocked_free": True}

    records, status = collect_simulation_logs(
        100,
        [f"latest-{index}" for index in range(1000)],
        blocked,
    )

    assert calls == [(0, 100)]
    assert len(records) == 1000
    assert status == "capped_free"


def test_simulation_log_stops_at_verified_previous_offset() -> None:
    from joinquant_sync.browser import collect_simulation_logs

    calls: list[tuple[int, int]] = []

    def fetch_older(offset: int, limit: int) -> dict[str, object]:
        calls.append((offset, limit))
        return {"rows": [f"older-{index}" for index in range(limit)]}

    records, status = collect_simulation_logs(
        1200,
        [f"latest-{index}" for index in range(100)],
        fetch_older,
        stop_offset=1230,
    )

    assert calls == []
    assert [record["offset"] for record in records] == list(range(1200, 1300))
    assert status == "incremental"


def test_simulation_log_fetches_only_gap_after_verified_offset() -> None:
    from joinquant_sync.browser import collect_simulation_logs

    calls: list[tuple[int, int]] = []

    def fetch_older(offset: int, limit: int) -> dict[str, object]:
        calls.append((offset, limit))
        return {"rows": [f"older-{index}" for index in range(limit)]}

    records, status = collect_simulation_logs(
        1300,
        [f"latest-{index}" for index in range(100)],
        fetch_older,
        stop_offset=1230,
    )

    assert calls == [(1230, 70)]
    assert [record["offset"] for record in records] == list(range(1230, 1400))
    assert status == "incremental"


def test_unchanged_simulation_code_history_reuses_verified_old_pages() -> None:
    from joinquant_sync.browser import reuse_simulation_code_history

    first = {"data": {"list": [{"id": "newest", "code": "v2"}]}}
    previous = [
        first,
        {"data": {"list": [{"id": "oldest", "code": "v1"}]}},
    ]

    history, pages, reused = reuse_simulation_code_history(first, previous, 2, 2)

    assert reused is True
    assert history == [
        {"id": "newest", "code": "v2"},
        {"id": "oldest", "code": "v1"},
    ]
    assert pages == previous


def test_paid_preview_is_bound_and_one_time(tmp_path: Path) -> None:
    from joinquant_sync.browser import (
        PaidConfirmationRequired,
        consume_paid_preview,
        create_paid_preview,
        load_paid_preview,
    )

    quote = {"credits": 3, "rows": 1200}
    preview = create_paid_preview(
        "run-1", "normal_log", "1000:1200", quote, store_dir=tmp_path
    )
    assert load_paid_preview(preview["preview_id"], store_dir=tmp_path) == preview
    used: set[str] = set()
    with pytest.raises(PaidConfirmationRequired):
        consume_paid_preview(
            preview,
            "run-1",
            "normal_log",
            "1000:1200",
            quote,
            False,
            used,
            store_dir=tmp_path,
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
    assert (
        consume_paid_preview(
            preview,
            "run-1",
            "normal_log",
            "1000:1200",
            quote,
            True,
            used,
            store_dir=tmp_path,
        )["preview_id"]
        == preview["preview_id"]
    )
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
    from joinquant_sync.browser import (
        parse_active_simulation_rows,
        parse_simulation_rows,
    )

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
    assert parse_simulation_rows(rows)[1]["status"] == "closed"


@pytest.mark.parametrize("status", ["0", "1", "3", "5"])
def test_simulation_list_accepts_only_verified_active_statuses(status: str) -> None:
    from joinquant_sync.browser import parse_simulation_rows

    candidate = parse_simulation_rows(
        [
            {
                "status": status,
                "name": "active",
                "page_space_id": "10",
                "detail_url": "/algorithm/live/index?backtestId=active",
            }
        ]
    )
    assert candidate[0]["status"] == "active"


def test_simulation_list_blocks_unknown_status_instead_of_assuming_closed() -> None:
    from joinquant_sync.browser import SimulationDiscoveryError, parse_simulation_rows

    with pytest.raises(SimulationDiscoveryError, match="unknown simulation status: 6"):
        parse_simulation_rows(
            [
                {
                    "status": "6",
                    "name": "unknown",
                    "page_space_id": "10",
                    "detail_url": "/algorithm/live/index?backtestId=unknown",
                }
            ]
        )


def test_history_rows_deduplicate_mobile_copy_by_page_ordinal() -> None:
    from joinquant_sync.browser import parse_history_rows

    row = {
        "page_ordinal": "115",
        "name": "etf_factor_rotation",
        "status_text": "完成",
        "created_at": "2026-07-09 17:35:51",
        "date_range": "2021-01-01 - 2026-04-30",
        "detail_id": "rotating-detail",
        "result_id": "rotating-result",
        "source_id": "rotating-source",
    }
    assert parse_history_rows([row, dict(row)]) == [
        {
            "page_ordinal": "115",
            "name": "etf_factor_rotation",
            "status": "done",
            "created_at": "2026-07-09 17:35:51",
            "date_range": "2021-01-01 - 2026-04-30",
            "detail_url": "https://www.joinquant.com/algorithm/backtest/detail?backtestId=rotating-detail",
            "aliases": ["rotating-detail", "rotating-result", "rotating-source"],
        }
    ]


def test_backtest_log_network_error_is_not_reported_as_free_cap() -> None:
    """Transport failure must fail closed instead of becoming a paid-log exception."""
    from joinquant_sync.browser import FreeLogIncomplete

    # Exercise the production collector through its injectable fetch contract.
    from joinquant_sync.browser import collect_free_logs

    def failed_page(_offset: int) -> dict[str, object]:
        raise FreeLogIncomplete("offline")

    with pytest.raises(FreeLogIncomplete):
        collect_free_logs(failed_page)


def test_simulation_terminal_status_requires_explicit_page_evidence() -> None:
    from joinquant_sync.browser import parse_simulation_page_status

    assert parse_simulation_page_status("3", "运行中") == "active"
    assert parse_simulation_page_status("2", "模拟交易已关闭") == "closed"
    assert parse_simulation_page_status("6", "模拟交易已关闭") == "unknown"
    assert parse_simulation_page_status("", "页面不可用") == "unknown"
