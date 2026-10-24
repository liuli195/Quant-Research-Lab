from __future__ import annotations

from pathlib import Path
import json

import pytest


def test_backtest_fence_changes_when_performance_profile_changes() -> None:
    from joinquant_sync.sync_pipeline import _backtest_browser_fingerprint

    browser = {
        "code": "enable_profile()\n",
        "normal_log": b"log",
        "official_summary": b"summary",
        "params": {},
        "performance_profile": b"profile-a",
        "performance_profile_surface_supported": True,
    }

    before = _backtest_browser_fingerprint(browser)
    browser["performance_profile"] = b"profile-b"

    assert _backtest_browser_fingerprint(browser) != before


def _datasets(kind: str = "simulation") -> dict[str, dict[str, object]]:
    from joinquant_sync.archive import expected_datasets

    return expected_datasets(kind, "active" if kind == "simulation" else "done", False)


def test_missing_structured_dataset_fails_closed() -> None:
    from joinquant_sync.archive import evaluate_gate
    from joinquant_sync.sync_pipeline import _stage_structured

    datasets = _datasets()
    _stage_structured(Path("unused-stage"), {}, datasets)
    assert datasets["results"]["status"] == "failed"
    assert evaluate_gate(datasets)["status"] == "fail"


def test_fact_date_outside_result_trading_dates_fails_closed() -> None:
    from joinquant_sync.archive import IntegrityError
    from joinquant_sync.sync_pipeline import _validate_fact_relations

    bundle = {
        "results": [{"time": "2026-01-02"}],
        "orders": [{"time": "2026-01-03"}],
    }
    with pytest.raises(IntegrityError, match="orders time is outside results"):
        _validate_fact_relations(
            bundle, _datasets(), {"start_date": "2026-01-01", "end_date": "2026-01-31"}
        )


def test_simulation_attribution_mismatch_reports_date_evidence_only() -> None:
    from joinquant_sync.archive import AttributionIncomplete
    from joinquant_sync.sync_pipeline import _validate_simulation_attribution

    raw = (
        b'{"audit_token":"run-1","seq":1,"event":"run_start",'
        b'"current_dt":"2021-01-01T00:00:00"}\n'
    )
    with pytest.raises(
        AttributionIncomplete,
        match="expected_start=2026-01-01 observed_start=2021-01-01",
    ):
        _validate_simulation_attribution(raw, "2026-01-01")


def test_research_fingerprint_ignores_generation_time_but_detects_data_change() -> None:
    from joinquant_sync.sync_pipeline import _research_remote_fingerprint

    first = {
        "bundle": {
            "metadata": {"generated_at": "2026-01-01T00:00:00"},
            "results": [{"time": "2026-01-01", "returns": 0.1}],
        },
        "attribution": b"one\n",
    }
    later = {
        "bundle": {
            "metadata": {"generated_at": "2026-01-01T00:01:00"},
            "results": [{"time": "2026-01-01", "returns": 0.1}],
        },
        "attribution": b"one\n",
    }
    changed = {
        **later,
        "bundle": {
            **later["bundle"],
            "results": [{"time": "2026-01-01", "returns": 0.2}],
        },
    }
    assert _research_remote_fingerprint(first) == _research_remote_fingerprint(later)
    assert _research_remote_fingerprint(first) != _research_remote_fingerprint(changed)


def test_simulation_fingerprint_uses_code_history_content_not_rotating_alias() -> None:
    from joinquant_sync.sync_pipeline import _simulation_remote_fingerprint

    code = "def initialize(context):\n    pass\n"
    browser = {
        "code": "# current\n",
        "code_versions": ["# old\n"],
        "code_history_total": 1,
        "code_history_versions": [
            {
                "history_ordinal": 1,
                "live_history_id": "h1",
                "source_backtest_id": "source-1",
                "add_time": "t1",
                "mod_time": "t1",
                "code": "# old\n",
            }
        ],
        "normal_log": b"",
        "normal_log_records": [],
        "params": {},
    }
    alias_changed = {
        **browser,
        "code_history_versions": [
            {**browser["code_history_versions"][0], "source_backtest_id": "source-2"}
        ],
    }
    code_changed = {
        **browser,
        "code_history_versions": [
            {**browser["code_history_versions"][0], "code": "# changed\n"}
        ],
    }

    assert _simulation_remote_fingerprint(browser) == _simulation_remote_fingerprint(
        alias_changed
    )
    assert _simulation_remote_fingerprint(browser) != _simulation_remote_fingerprint(
        code_changed
    )


def test_capped_normal_log_cannot_prove_failed_error_log_complete() -> None:
    from joinquant_sync.sync_pipeline import _stage_error_log

    datasets = _datasets("backtest")
    assert (
        _stage_error_log(
            Path("unused-stage"),
            "failed",
            b"2026-01-01 - ERROR - boom\n",
            "capped_free",
            datasets,
        )
        == []
    )
    assert datasets["error_log"]["status"] == "failed"
    assert datasets["error_log"]["evidence"] == {"normal_log_status": "capped_free"}


def test_done_run_rejects_all_empty_core_tables() -> None:
    from joinquant_sync.archive import IntegrityError
    from joinquant_sync.sync_pipeline import _validate_run_semantics

    datasets = _datasets("backtest")
    for name in ("results", "balances", "risk"):
        datasets[name].update(status="complete", rows=0, verified_empty=True)
    with pytest.raises(IntegrityError, match="results is empty"):
        _validate_run_semantics("backtest", "done", datasets)


def test_incremental_same_timestamp_replaces_old_and_keeps_new_orders() -> None:
    from joinquant_sync.sync_pipeline import _merge_fact_rows

    previous = [
        {
            "time": "2026-01-01 09:30:00",
            "entrust_time": "2026-01-01 09:30:00",
            "pindex": 0,
            "security": "A",
            "side": "long",
            "amount": 1,
            "price": 10,
            "status": "open",
        }
    ]
    current = [
        {**previous[0], "status": "filled"},
        {**previous[0], "amount": 2, "status": "open"},
    ]
    merged = _merge_fact_rows("orders", previous, current)
    assert len(merged) == 2
    assert [row["status"] for row in merged] == ["filled", "open"]


def test_incremental_orders_preserve_exact_duplicate_multiplicity() -> None:
    from joinquant_sync.sync_pipeline import _merge_fact_rows

    order = {
        "time": "2026-01-01 09:30:00",
        "entrust_time": "2026-01-01 09:30:00",
        "pindex": 0,
        "security": "A",
        "side": "long",
        "amount": 1,
        "price": 10,
        "status": "filled",
    }
    merged = _merge_fact_rows("orders", [dict(order)], [dict(order), dict(order)])
    assert merged == [order, order]


def test_bundle_time_cursors_never_move_backwards() -> None:
    from joinquant_sync.sync_pipeline import _bundle_time_cursors

    assert _bundle_time_cursors(
        {"results": [{"time": "2026-07-11"}], "orders": []},
        {"results": "2026-07-10", "orders": "2026-07-09"},
    ) == {"results": "2026-07-11", "orders": "2026-07-09"}


@pytest.mark.real
def test_public_simulation_archive_query_smoke(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from contextlib import contextmanager
    from types import SimpleNamespace
    import jq_sync
    import joinquant_sync.sync_pipeline as pipeline

    candidate = {
        "page_ordinal": "1",
        "name": "strategy",
        "page_space_id": "space-1",
        "status": "active",
        "detail_url": "https://www.joinquant.com/algorithm/live/index?backtestId=alias",
        "aliases": ["alias"],
    }
    browser = {
        "normal_log": b'{"offset":0,"text":"line"}\n',
        "normal_log_records": [{"offset": 0, "text": "line"}],
        "normal_log_raw_pages": [
            {"cursor": 0, "response": {"data": {"logArr": ["line"]}}}
        ],
        "normal_log_status": "complete",
        "normal_log_rows": 1,
        "log_pages": [{"cursor": 0, "rows": 1}],
        "code": "def initialize(context):\n    pass\n",
        "params": {"start_date": "2026-01-01"},
        "source_backtest": "source-alias",
        "research_id": "research-alias",
        "code_versions": ["def initialize(context):\n    pass\n"],
        "code_history_versions": [
            {
                "history_ordinal": 1,
                "live_history_id": "history-1",
                "source_backtest_id": "source-1",
                "add_time": "2026-01-01 00:00:00",
                "mod_time": "2026-01-01 00:00:00",
                "code": "def initialize(context):\n    pass\n",
            }
        ],
        "code_version_cache": {
            "source-1": "def initialize(context):\n    pass\n"
        },
        "code_history_pages": [
            {
                "data": {
                    "totalCount": 1,
                    "list": [
                        {
                            "liveHistoryId": "history-1",
                            "sourceBacktestId": "source-1",
                            "addTime": "2026-01-01 00:00:00",
                            "modTime": "2026-01-01 00:00:00",
                            "code": 0,
                        }
                    ],
                }
            }
        ],
        "code_history_total": 1,
    }
    calls: list[dict[str, str]] = []
    object_dir = (
        tmp_path
        / "joinquant"
        / "strategies"
        / "strategy-001"
        / "simulations"
        / "simulation-001"
    )
    research_status = "running"

    def research(
        _page: object,
        _backtest_id: str,
        *,
        attribution_path: str = "",
        after_times: dict[str, str] | None = None,
    ) -> dict[str, object]:
        results = [
            {"time": "2026-01-01 16:00:00", "returns": 0.1},
            {"time": "2026-01-01 17:00:00", "returns": 0.1},
        ]
        rows = {
            "params": {"start_date": "2026-01-01"},
            "status": research_status,
            "results": results,
            "balances": [{"time": "2026-01-01", "cash": 1.0}],
            "positions": [
                {
                    "time": "2026-01-01",
                    "pindex": 0,
                    "security": "A",
                    "side": "long",
                }
            ],
            "orders": [
                {
                    "time": "2026-01-01",
                    "pindex": 0,
                    "security": "A",
                    "side": "long",
                }
            ],
            "records": [],
            "risk": {"sharpe": 1.0},
            "period_risks": {},
            "metadata": {
                "schema_version": 1,
                "backtest_id": "research-alias",
                "generated_at": "2026-01-01T00:00:00",
                "extraction_method": "joinquant_research_get_backtest",
                "incremental_after": {},
                "transfer_modes": {
                    name: "full"
                    for name in (
                        "results",
                        "positions",
                        "orders",
                        "records",
                        "balances",
                    )
                },
            },
        }
        return {
            "bundle": rows,
            "raw": json.dumps(rows, separators=(",", ":")).encode(),
            "attribution": b"",
            "attributions": {},
        }

    monkeypatch.setattr(
        pipeline, "discover_active_simulations", lambda _page: [dict(candidate)]
    )
    monkeypatch.setattr(
        pipeline, "discover_all_simulations", lambda _page: [dict(candidate)]
    )
    monkeypatch.setattr(
        pipeline,
        "fetch_strategy_default_code",
        lambda _page, _name: {
            "name": "strategy",
            "edit_url": "https://www.joinquant.com/algorithm/index/edit?algorithmId=s",
            "code": browser["code"],
        },
    )
    browser_states: list[dict[str, object] | None] = []

    def browser_evidence(
        _page: object,
        _candidate: dict[str, object],
        incremental: dict[str, object] | None = None,
    ) -> dict[str, object]:
        browser_states.append(incremental)
        return dict(browser)

    monkeypatch.setattr(pipeline, "fetch_simulation_browser_evidence", browser_evidence)
    monkeypatch.setattr(pipeline, "fetch_research_backtest", research)

    @contextmanager
    def offline_context(_profile: Path, *, headless: bool):
        assert headless is True
        yield SimpleNamespace(pages=[object()])

    monkeypatch.setattr(jq_sync, "open_authenticated_context", offline_context)
    command = ["sync-active-simulations", "--repository", str(tmp_path),
               "--profile", str(tmp_path.parent / "unused-sync-profile")]
    for expected in ("committed", "unchanged"):
        assert jq_sync.main(command) == 0
        assert json.loads(capsys.readouterr().out)["results"][0]["status"] == expected
    assert jq_sync.main(["verify", "--object", str(object_dir)]) == 0
    capsys.readouterr()
    manifest = str(object_dir / "manifest.json")
    assert jq_sync.main(["query", "--object", manifest, "--dataset", "results"]) == 0
    rows = json.loads(capsys.readouterr().out)
    assert [row["time"] for row in rows] == ["2026-01-01 16:00:00", "2026-01-01 17:00:00"]
    destination = tmp_path / "results.csv"
    assert jq_sync.main(["export-csv", "--object", manifest, "--dataset", "results",
                         "--fields", "time,returns", "--destination", str(destination)]) == 0
    assert destination.read_text(encoding="utf-8").splitlines()[0] == "time,returns"
    assert len(destination.read_text(encoding="utf-8").splitlines()) == 3
