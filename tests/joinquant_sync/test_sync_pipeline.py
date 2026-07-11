from __future__ import annotations

from pathlib import Path
import gzip
import json
import hashlib

import pytest


def test_atomic_replace_retries_transient_windows_permission_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import joinquant_sync.sync_pipeline as pipeline

    source = tmp_path / "source"
    destination = tmp_path / "destination"
    source.write_text("new", encoding="utf-8")
    destination.write_text("old", encoding="utf-8")
    calls = 0
    real_replace = pipeline.os.replace

    def flaky_replace(src: Path, dst: Path) -> None:
        nonlocal calls
        calls += 1
        if calls < 3:
            raise PermissionError(5, "transient lock")
        real_replace(src, dst)

    monkeypatch.setattr(pipeline.os, "replace", flaky_replace)
    monkeypatch.setattr(pipeline.time, "sleep", lambda _seconds: None)

    pipeline._replace_with_retry(source, destination)

    assert calls == 3
    assert destination.read_text(encoding="utf-8") == "new"


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


def test_simulation_log_increment_uses_last_verified_offset_not_row_count(
    tmp_path: Path,
) -> None:
    from joinquant_sync.sync_pipeline import _simulation_browser_incremental_state

    log_path = tmp_path / "raw" / "normal-log.jsonl.gz"
    log_path.parent.mkdir(parents=True)
    with gzip.open(log_path, "wt", encoding="utf-8") as stream:
        for offset in range(100, 1100):
            stream.write(json.dumps({"offset": offset, "text": "line"}) + "\n")
    manifest = {
        "datasets": {
            "normal_log": {
                "rows": 1000,
                "files": [
                    {
                        "path": "raw/normal-log.jsonl.gz",
                        "format": "jsonl.gz",
                    }
                ],
            }
        },
        "code": {"versions": [], "history": {}},
    }

    state = _simulation_browser_incremental_state(tmp_path, manifest)

    assert state is not None
    assert state["normal_log_stop_offset"] == 1100


def test_capped_simulation_log_remains_capped_after_incremental_merge(
    tmp_path: Path,
) -> None:
    from joinquant_sync.sync_pipeline import _merge_simulation_log

    log_path = tmp_path / "raw" / "normal-log.jsonl.gz"
    log_path.parent.mkdir(parents=True)
    with gzip.open(log_path, "wt", encoding="utf-8") as stream:
        for offset in range(100, 1100):
            stream.write(json.dumps({"offset": offset, "text": "old"}) + "\n")
    pages_path = tmp_path / "raw" / "normal-log-pages.json.gz"
    with gzip.open(pages_path, "wt", encoding="utf-8") as stream:
        json.dump([{"cursor": 0, "blocked_free": True}], stream)
    previous = {
        "datasets": {
            "normal_log": {
                "status": "capped_free",
                "rows": 1000,
                "files": [
                    {
                        "path": "raw/normal-log.jsonl.gz",
                        "format": "jsonl.gz",
                    },
                    {
                        "path": "raw/normal-log-pages.json.gz",
                        "format": "json.gz",
                    },
                ],
            }
        }
    }
    browser = {
        "normal_log": b'{"offset":1100,"text":"new"}\n',
        "normal_log_records": [{"offset": 1100, "text": "new"}],
        "normal_log_status": "incremental",
        "normal_log_raw_pages": [],
    }

    _merge_simulation_log(browser, previous, tmp_path)

    assert browser["normal_log_status"] == "capped_free"
    assert browser["normal_log_rows"] == 1001
    assert any(
        page.get("blocked_free") is True
        for page in browser["normal_log_raw_pages"]
    )


def test_simulation_code_history_cursor_uses_remote_history_count(
    tmp_path: Path,
) -> None:
    from joinquant_sync.sync_pipeline import _simulation_browser_incremental_state

    history_path = tmp_path / "raw" / "code-history.json.gz"
    history_path.parent.mkdir(parents=True)
    with gzip.open(history_path, "wt", encoding="utf-8") as stream:
        json.dump(
            [{"data": {"list": [{"id": 1}, {"id": 2}, {"id": 3}]}}], stream
        )
    manifest = {
        "datasets": {"normal_log": {"rows": 0, "files": []}},
        "code": {
            "versions": [{"path": "one.py"}],
            "history": {"path": "raw/code-history.json.gz", "rows": 3},
        },
    }

    state = _simulation_browser_incremental_state(tmp_path, manifest)

    assert state is not None
    assert state["code_history_total"] == 3


def test_simulation_incremental_state_reuses_verified_code_sources(
    tmp_path: Path,
) -> None:
    from joinquant_sync.sync_pipeline import _simulation_browser_incremental_state

    code_text = "# historical code\n"
    digest = hashlib.sha256(code_text.encode()).hexdigest()
    code_path = tmp_path / "code_versions" / f"{digest}.py"
    code_path.parent.mkdir(parents=True)
    code_path.write_bytes(code_text.encode())
    history_path = tmp_path / "raw" / "code-history.json.gz"
    history_path.parent.mkdir(parents=True)
    with gzip.open(history_path, "wt", encoding="utf-8") as stream:
        json.dump(
            [
                {
                    "data": {
                        "list": [
                            {
                                "liveHistoryId": "h1",
                                "sourceBacktestId": "source-1",
                                "addTime": "t1",
                                "modTime": "t1",
                                "code": 0,
                            }
                        ]
                    }
                }
            ],
            stream,
        )
    manifest = {
        "datasets": {"normal_log": {"rows": 0, "files": []}},
        "code": {
            "versions": [
                {
                    "path": f"code_versions/{digest}.py",
                    "sha256": digest,
                }
            ],
            "history": {"path": "raw/code-history.json.gz", "rows": 1},
            "history_versions": [
                {
                    "history_ordinal": 1,
                    "live_history_id": "h1",
                    "source_backtest_id": "source-1",
                    "add_time": "t1",
                    "mod_time": "t1",
                    "code_sha256": digest,
                    "path": f"code_versions/{digest}.py",
                }
            ],
        },
    }

    state = _simulation_browser_incremental_state(tmp_path, manifest)

    assert state is not None
    assert state["code_version_cache"] == {"source-1": code_text}
    assert list(state["code_history_cache"].values()) == [code_text]


def _commit_test_simulation(
    object_dir: Path,
    *,
    writer: bool = False,
    capped: bool = False,
    sparse_attribution: bool = False,
    browser_start_date: str = "2026-01-01",
    history_includes_current: bool = True,
    profile: bool = False,
    rotated_writer: bool = False,
    rotated_tail_event: str = "schedule_reset_after_code_changed",
) -> dict[str, object]:
    from joinquant_sync.archive import (
        commit_manifest,
        detect_attribution_writer,
        detect_attribution_writers,
    )
    from joinquant_sync.sync_pipeline import (
        _build_simulation_batch,
        _update_simulation_pointers,
    )

    token = "run-new" if rotated_writer else "run-1"
    code = (
        f'JQ_AUTO_AUDIT_TOKEN = "{token}"\n'
        'JQ_AUTO_AUDIT_DIR = "audit"\n'
        "def audit_event(event):\n    write_file(g.audit_path, event, append=True)\n"
        if writer
        else "def initialize(context):\n    pass\n"
    )
    if profile:
        code = "enable_profile()\n" + code
    bundle = {
        "metadata": {
            "schema_version": 1,
            "backtest_id": "source",
            "generated_at": "2026-01-01T00:00:00",
            "extraction_method": "joinquant_research_get_backtest",
            "incremental_after": {},
            "transfer_modes": {
                name: "full"
                for name in ("results", "positions", "orders", "records", "balances")
            },
        },
        "params": {"start_date": "2026-01-01"},
        "status": "running",
        "results": [{"time": "2026-01-01", "returns": 0.1}],
        "balances": [{"time": "2026-01-01", "cash": 1.0}],
        "positions": [],
        "orders": [],
        "records": [],
        "risk": {"sharpe": 1.0},
        "period_risks": {},
    }
    log_rows = 1000 if capped else 1
    log_start = 100 if capped else 0
    log_records = [
        {"offset": offset, "text": f"line-{offset}"}
        for offset in range(log_start, log_start + log_rows)
    ]
    raw_pages = [
        {
            "cursor": log_start,
            "response": {"data": {"logArr": [row["text"] for row in log_records]}},
        }
    ]
    if capped:
        raw_pages.append(
            {
                "cursor": 0,
                "response": {"status": "error", "msg": "free limit"},
                "blocked_free": True,
            }
        )
    browser = {
        "normal_log": (
            "".join(
                json.dumps(row, separators=(",", ":")) + "\n" for row in log_records
            )
        ).encode(),
        "normal_log_records": log_records,
        "normal_log_status": "capped_free" if capped else "complete",
        "normal_log_rows": log_rows,
        "log_pages": [
            {"cursor": log_start, "rows": log_rows},
            *([{"cursor": 0, "rows": 0, "blocked_free": True}] if capped else []),
        ],
        "normal_log_raw_pages": raw_pages,
        "code": code,
        "params": {"start_date": browser_start_date},
        "source_backtest": "source",
        "code_versions": [code] if history_includes_current else [],
        "performance_profile": (
            b"Timer unit: 1e-06 s\nTotal time: 1 s\n"
            b"Line # Hits Time Per Hit % Time Line Contents\n"
            if profile
            else b""
        ),
        "performance_profile_surface_supported": profile,
    }
    old_code = code.replace("run-new", "run-old") if rotated_writer else code
    attribution = (
        detect_attribution_writers([code, old_code])
        if rotated_writer
        else detect_attribution_writer(code)
    )
    attribution_rows = [
        {
            "audit_token": token,
            "seq": seq,
            "event": event,
            "current_dt": current_dt,
        }
        for seq, event, current_dt in (
            (1, "run_start", "2026-01-01T00:00:00"),
            (2, "run_end", "2026-01-01T00:01:00"),
        )
    ]
    if sparse_attribution:
        attribution_rows[0]["start_date"] = "2026-01-01"
        attribution_rows[1]["reason"] = "completed"
    raw = (
        b"\n".join(
            json.dumps(row, separators=(",", ":")).encode()
            for row in attribution_rows
        )
        + b"\n"
        if writer
        else b""
    )
    attributions = None
    if rotated_writer:
        old_rows = [
            {
                "audit_token": "run-old",
                "seq": 1,
                "event": "run_start",
                "current_dt": "2026-01-01T00:00:00",
            }
        ]
        new_rows = [
            {
                "audit_token": "run-new",
                "seq": 1,
                "event": "run_start",
                "current_dt": "2025-01-01T00:00:00",
            },
            {
                "audit_token": "run-new",
                "seq": 2,
                "event": "run_end",
                "current_dt": "2025-12-31T23:59:59",
            },
            {
                "audit_token": "run-new",
                "seq": 3,
                "event": rotated_tail_event,
                "current_dt": "2026-01-01T00:01:00",
            },
        ]
        attributions = {
            "audit/run-new.jsonl": (
                "".join(json.dumps(row, separators=(",", ":")) + "\n" for row in new_rows)
            ).encode(),
            "audit/run-old.jsonl": (
                "".join(json.dumps(row, separators=(",", ":")) + "\n" for row in old_rows)
            ).encode(),
        }
        raw = b""
        browser["code_versions"] = [code, old_code]
    browser["code_history_versions"] = [
        {
            "history_ordinal": ordinal,
            "live_history_id": f"history-{ordinal}",
            "source_backtest_id": f"source-{ordinal}",
            "add_time": f"2026-01-0{ordinal} 00:00:00",
            "mod_time": f"2026-01-0{ordinal} 00:00:00",
            "code": version,
        }
        for ordinal, version in enumerate(browser["code_versions"], start=1)
    ]
    browser["code_history_total"] = len(browser["code_history_versions"])
    browser["code_history_pages"] = [
        {
            "data": {
                "totalCount": len(browser["code_history_versions"]),
                "list": [
                    {
                        "liveHistoryId": item["live_history_id"],
                        "sourceBacktestId": item["source_backtest_id"],
                        "addTime": item["add_time"],
                        "modTime": item["mod_time"],
                        "code": 0,
                    }
                    for item in browser["code_history_versions"]
                ],
            }
        }
    ]
    stage = object_dir.parent / "stage"
    manifest, staged = _build_simulation_batch(
        stage,
        {
            "local_id": "simulation-001",
            "page_space_id": "space-1",
            "detail_url": "https://www.joinquant.com/algorithm/live/index?backtestId=x",
            "aliases": ["x"],
            "collection_fence": {
                "collection_before_sha256": "0" * 64,
                "collection_after_sha256": "0" * 64,
            },
        },
        browser,
        {
            "bundle": bundle,
            "raw": json.dumps(bundle, separators=(",", ":")).encode(),
            "attribution": raw,
            "attributions": attributions or {},
        },
        attribution,
    )
    commit_manifest(object_dir, manifest, staged)
    _update_simulation_pointers(object_dir, manifest)
    return manifest


def test_simulation_rotated_writer_archives_all_source_logs(tmp_path: Path) -> None:
    from joinquant_sync.archive import verify_existing_manifest

    object_dir = tmp_path / "simulation"
    manifest = _commit_test_simulation(
        object_dir,
        writer=True,
        rotated_writer=True,
    )

    dataset = manifest["datasets"]["attribution_log"]
    assert dataset["rows"] == 2
    assert dataset["evidence"]["tokens"] == ["run-new", "run-old"]
    assert len(
        [
            item
            for item in dataset["files"]
            if item.get("format") == "attribution-source-jsonl.gz"
        ]
    ) == 2
    assert [
        (item["history_ordinal"], item["source_backtest_id"])
        for item in manifest["code"]["history_versions"]
    ] == [(1, "source-1"), (2, "source-2")]
    assert all(item["path"].startswith("code_versions/") for item in manifest["code"]["history_versions"])
    assert verify_existing_manifest(object_dir)["gate"]["status"] == "pass"


def test_simulation_fence_changes_when_only_attribution_source_changes(
    tmp_path: Path,
) -> None:
    first = _commit_test_simulation(
        tmp_path / "one",
        writer=True,
        rotated_writer=True,
        rotated_tail_event="schedule_reset_after_code_changed",
    )
    second = _commit_test_simulation(
        tmp_path / "two",
        writer=True,
        rotated_writer=True,
        rotated_tail_event="code_changed",
    )

    assert first["fence"] != second["fence"]


def test_simulation_code_history_mapping_is_verified_against_raw_pages(
    tmp_path: Path,
) -> None:
    from joinquant_sync.archive import IntegrityError, verify_existing_manifest

    object_dir = tmp_path / "simulation"
    manifest = _commit_test_simulation(
        object_dir,
        writer=True,
        rotated_writer=True,
    )
    manifest["code"]["history_versions"][0]["source_backtest_id"] = "forged"
    (object_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(IntegrityError, match="code history mapping"):
        verify_existing_manifest(object_dir)


def _datasets(kind: str = "simulation") -> dict[str, dict[str, object]]:
    from joinquant_sync.archive import expected_datasets

    return expected_datasets(kind, "active" if kind == "simulation" else "done", False)


def test_missing_structured_dataset_fails_closed(tmp_path: Path) -> None:
    from joinquant_sync.archive import evaluate_gate
    from joinquant_sync.sync_pipeline import _stage_structured

    datasets = _datasets()
    _stage_structured(tmp_path, {}, datasets)
    assert datasets["results"]["status"] == "failed"
    assert evaluate_gate(datasets)["status"] == "fail"


def test_primitive_structured_rows_fail_closed(tmp_path: Path) -> None:
    from joinquant_sync.archive import IntegrityError
    from joinquant_sync.sync_pipeline import _stage_structured

    bundle = {
        name: [] for name in ("results", "balances", "positions", "orders", "records")
    }
    bundle.update(risk={}, period_risks={})
    bundle["orders"] = ["not-an-object"]
    with pytest.raises(IntegrityError, match="orders"):
        _stage_structured(tmp_path, bundle, _datasets())


def test_duplicate_fact_key_fails_closed(tmp_path: Path) -> None:
    from joinquant_sync.archive import IntegrityError
    from joinquant_sync.sync_pipeline import _stage_structured

    bundle = {
        "results": [{"time": "2026-01-01"}, {"time": "2026-01-01"}],
        "balances": [],
        "positions": [],
        "orders": [],
        "records": [],
        "risk": {},
        "period_risks": {},
    }
    with pytest.raises(IntegrityError, match="results unique key"):
        _stage_structured(tmp_path, bundle, _datasets())


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


def test_attribution_is_saved_as_raw_and_queryable_parquet(tmp_path: Path) -> None:
    from joinquant_sync.sync_pipeline import _stage_attribution

    raw = b"{" + b'"seq":1,"event":"run_start","token":"t"}' + b"\n"
    datasets = _datasets()
    datasets["attribution_log"]["required"] = True
    staged = _stage_attribution(tmp_path, raw, datasets, {"rows": 1})
    assert {path.suffix for path in staged} == {".gz", ".parquet"}
    assert len(datasets["attribution_log"]["files"]) == 2
    paths = {item["path"] for item in datasets["attribution_log"]["files"]}
    assert any(path.startswith("raw/attribution-log-") for path in paths)
    assert any(path.startswith("data/attribution_log-") for path in paths)


def test_research_response_path_is_content_addressed(tmp_path: Path) -> None:
    from joinquant_sync.sync_pipeline import _stage_research_response

    first_datasets = _datasets()
    second_datasets = _datasets()
    for datasets in (first_datasets, second_datasets):
        for name in (
            "results",
            "balances",
            "positions",
            "orders",
            "records",
            "risk",
            "period_risks",
        ):
            datasets[name]["pagination"] = {}

    first_record, _ = _stage_research_response(
        tmp_path / "first", {"raw": b'{"revision":1}'}, first_datasets
    )
    second_record, _ = _stage_research_response(
        tmp_path / "second", {"raw": b'{"revision":2}'}, second_datasets
    )

    assert first_record["path"].startswith("raw/research-response-")
    assert first_record["path"] != second_record["path"]


def test_simulation_raw_code_evidence_is_content_addressed(tmp_path: Path) -> None:
    manifest = _commit_test_simulation(tmp_path / "simulation")

    assert manifest["code"]["history"]["path"].startswith("raw/code-history-")
    assert manifest["code"]["source_response"]["path"].startswith(
        "raw/code-source-response-"
    )


def test_snapshot_cleanup_keeps_only_manifest_referenced_lineage_file(
    tmp_path: Path,
) -> None:
    from joinquant_sync.sync_pipeline import _cleanup_unreferenced_snapshots

    old = tmp_path / "snapshots" / "old"
    response = old / "raw" / "research-response.json.gz"
    stale = old / "data" / "results.parquet"
    response.parent.mkdir(parents=True)
    stale.parent.mkdir(parents=True)
    response.write_bytes(b"response")
    stale.write_bytes(b"stale")
    manifest = {
        "datasets": {},
        "research_lineage": [
            {"path": "snapshots/old/raw/research-response.json.gz"}
        ],
    }

    _cleanup_unreferenced_snapshots(tmp_path, manifest)

    assert response.is_file()
    assert not stale.exists()


def test_performance_profile_payload_is_archived_and_referenced(tmp_path: Path) -> None:
    object_dir = tmp_path / "simulation-001"

    manifest = _commit_test_simulation(object_dir, profile=True)

    dataset = manifest["datasets"]["performance_profile"]
    assert dataset["status"] == "complete"
    assert dataset["rows"] == 3
    assert len(dataset["files"]) == 1
    path = object_dir / dataset["files"][0]["path"]
    assert gzip.open(path, "rb").read() == (
        b"Timer unit: 1e-06 s\nTotal time: 1 s\n"
        b"Line # Hits Time Per Hit % Time Line Contents\n"
    )


def test_verify_rejects_placeholder_performance_profile_with_matching_hash(
    tmp_path: Path,
) -> None:
    from joinquant_sync.archive import IntegrityError, verify_existing_manifest

    object_dir = tmp_path / "simulation-001"
    manifest = _commit_test_simulation(object_dir, profile=True)
    dataset = manifest["datasets"]["performance_profile"]
    record = dataset["files"][0]
    path = object_dir / record["path"]
    payload = b"No performance data available"
    with gzip.open(path, "wb") as stream:
        stream.write(payload)
    compressed = path.read_bytes()
    record.update(
        sha256=hashlib.sha256(compressed).hexdigest(),
        bytes=len(compressed),
        raw_sha256=hashlib.sha256(payload).hexdigest(),
        raw_bytes=len(payload),
    )
    dataset["rows"] = 1
    dataset["evidence"].update(
        payload_sha256=hashlib.sha256(payload).hexdigest(),
        payload_bytes=len(payload),
    )
    (object_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(IntegrityError, match="performance profile"):
        verify_existing_manifest(object_dir)


def test_verify_rejects_enabled_profile_mislabeled_missing_at_source(
    tmp_path: Path,
) -> None:
    from joinquant_sync.archive import (
        IntegrityError,
        evaluate_gate,
        verify_existing_manifest,
    )

    object_dir = tmp_path / "simulation-001"
    manifest = _commit_test_simulation(object_dir, profile=True)
    dataset = manifest["datasets"]["performance_profile"]
    dataset.update(
        status="missing_at_source",
        rows=0,
        files=[],
        evidence={"enable_profile_call": False},
    )
    manifest["gate"] = evaluate_gate(manifest["datasets"])
    (object_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(IntegrityError, match="performance profile"):
        verify_existing_manifest(object_dir)


def test_simulation_attribution_uses_first_result_date_as_run_start(
    tmp_path: Path,
) -> None:
    from joinquant_sync.archive import verify_existing_manifest

    object_dir = tmp_path / "simulation"
    _commit_test_simulation(
        object_dir,
        writer=True,
        browser_start_date="2025-12-31",
    )

    assert verify_existing_manifest(object_dir)["gate"]["status"] == "pass"


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


def test_simulation_code_digest_includes_current_code_missing_from_history(
    tmp_path: Path,
) -> None:
    from joinquant_sync.archive import verify_existing_manifest

    object_dir = tmp_path / "simulation"
    _commit_test_simulation(object_dir, history_includes_current=False)

    assert verify_existing_manifest(object_dir)["gate"]["status"] == "pass"


def test_simulation_snapshot_id_is_content_addressed_not_date_based(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from joinquant_sync.sync_pipeline import _build_simulation_batch

    bundle = {
        "metadata": {
            "schema_version": 1,
            "backtest_id": "source",
            "generated_at": "2026-01-01T00:00:00",
            "extraction_method": "joinquant_research_get_backtest",
            "incremental_after": {},
            "transfer_modes": {
                name: "full"
                for name in ("results", "positions", "orders", "records", "balances")
            },
        },
        "results": [{"time": "2026-01-01", "returns": 0.0}],
        "balances": [{"time": "2026-01-01", "cash": 1.0}],
        "positions": [],
        "orders": [],
        "records": [],
        "risk": {"sharpe": 1.0},
        "period_risks": {},
    }
    candidate = {
        "local_id": "simulation-001",
        "detail_url": "https://www.joinquant.com/algorithm/live/index?backtestId=x",
        "aliases": ["x"],
        "fence": {"before_sha256": "0" * 64, "after_sha256": "0" * 64},
    }
    browser = {
        "normal_log": b"line\n",
        "normal_log_status": "complete",
        "normal_log_rows": 1,
        "log_pages": [{"cursor": 0, "rows": 1}],
        "code": "def initialize(context):\n    pass\n",
        "params": {},
        "source_backtest": "source",
        "code_versions": [],
    }
    research = {
        "bundle": bundle,
        "raw": json.dumps(bundle, separators=(",", ":")).encode(),
        "attribution": b"",
    }
    attribution = {"writer_present": False, "path": ""}
    first, _ = _build_simulation_batch(
        tmp_path / "one", candidate, browser, research, attribution
    )
    second, _ = _build_simulation_batch(
        tmp_path / "two", candidate, browser, research, attribution
    )
    assert (
        first["streams"]["snapshots"]["cursor"]
        == second["streams"]["snapshots"]["cursor"]
    )
    assert not first["streams"]["snapshots"]["cursor"].startswith("20")


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


def test_simulation_manifest_fence_includes_research_data(tmp_path: Path) -> None:
    from joinquant_sync.sync_pipeline import _build_simulation_batch

    base_bundle = {
        "metadata": {
            "schema_version": 1,
            "backtest_id": "source",
            "generated_at": "2026-01-01T00:00:00",
            "extraction_method": "joinquant_research_get_backtest",
            "incremental_after": {},
            "transfer_modes": {
                name: "full"
                for name in ("results", "positions", "orders", "records", "balances")
            },
        },
        "results": [{"time": "2026-01-01", "returns": 0.1}],
        "balances": [{"time": "2026-01-01", "cash": 1.0}],
        "positions": [],
        "orders": [],
        "records": [],
        "risk": {"sharpe": 1.0},
        "period_risks": {},
    }
    candidate = {
        "local_id": "simulation-001",
        "detail_url": "https://www.joinquant.com/algorithm/live/index?backtestId=x",
        "aliases": ["x"],
        "fence": {"before_sha256": "0" * 64, "after_sha256": "0" * 64},
    }
    browser = {
        "normal_log": b"line\n",
        "normal_log_status": "complete",
        "normal_log_rows": 1,
        "log_pages": [{"cursor": 0, "rows": 1}],
        "code": "def initialize(context):\n    pass\n",
        "params": {},
        "source_backtest": "source",
        "code_versions": [],
    }
    attribution = {"writer_present": False, "path": ""}
    first, _ = _build_simulation_batch(
        tmp_path / "one",
        candidate,
        dict(browser),
        {
            "bundle": base_bundle,
            "raw": json.dumps(base_bundle, separators=(",", ":")).encode(),
            "attribution": b"",
        },
        attribution,
    )
    changed_bundle = {**base_bundle, "risk": {"sharpe": 2.0}}
    second, _ = _build_simulation_batch(
        tmp_path / "two",
        candidate,
        dict(browser),
        {
            "bundle": changed_bundle,
            "raw": json.dumps(changed_bundle, separators=(",", ":")).encode(),
            "attribution": b"",
        },
        attribution,
    )
    assert first["fence"] != second["fence"]


def test_simulation_code_and_params_use_immutable_version_paths(tmp_path: Path) -> None:
    from joinquant_sync.sync_pipeline import _version_simulation_code_context

    code = b"print('new')\n"
    params = b'{"x":2}\n'
    source = b'{"source_backtest":"b"}\n'
    stage = tmp_path
    (stage / "current_code.py").write_bytes(code)
    (stage / "params.json").write_bytes(params)
    (stage / "source.json").write_bytes(source)
    context = {
        "path": "current_code.py",
        "sha256": __import__("hashlib").sha256(code).hexdigest(),
        "bytes": len(code),
        "params": {
            "path": "params.json",
            "sha256": __import__("hashlib").sha256(params).hexdigest(),
            "bytes": len(params),
        },
        "source": {
            "path": "source.json",
            "sha256": __import__("hashlib").sha256(source).hexdigest(),
            "bytes": len(source),
        },
        "versions": [],
    }
    staged = _version_simulation_code_context(stage, context)
    assert context["path"].startswith("code_versions/")
    assert context["params"]["path"].startswith("params_versions/")
    assert context["source"]["path"].startswith("source_versions/")
    assert all(path.is_file() for path in staged)


def test_failed_backtest_requires_and_stores_error_log(tmp_path: Path) -> None:
    from joinquant_sync.sync_pipeline import _stage_error_log

    datasets = _datasets("backtest")
    datasets["error_log"]["required"] = True
    raw = b"2026-01-01 - ERROR - Traceback: boom\n"
    staged = _stage_error_log(tmp_path, "failed", raw, "complete", datasets)
    assert datasets["error_log"]["status"] == "complete"
    assert datasets["error_log"]["rows"] == 1
    assert staged[0].name.startswith("error-log-")


def test_capped_normal_log_cannot_prove_failed_error_log_complete(
    tmp_path: Path,
) -> None:
    from joinquant_sync.archive import IntegrityError
    from joinquant_sync.sync_pipeline import _stage_error_log

    with pytest.raises(IntegrityError, match="complete normal log"):
        _stage_error_log(
            tmp_path,
            "failed",
            b"2026-01-01 - ERROR - boom\n",
            "capped_free",
            _datasets("backtest"),
        )


def test_done_run_rejects_all_empty_core_tables() -> None:
    from joinquant_sync.archive import IntegrityError
    from joinquant_sync.sync_pipeline import _validate_run_semantics

    datasets = _datasets("backtest")
    for name in ("results", "balances", "risk"):
        datasets[name].update(status="complete", rows=0, verified_empty=True)
    with pytest.raises(IntegrityError, match="results is empty"):
        _validate_run_semantics("backtest", "done", datasets)


def test_strategy_manifest_uses_immutable_code_version(tmp_path: Path) -> None:
    from joinquant_sync.archive import IntegrityError, verify_existing_manifest
    from joinquant_sync.sync_pipeline import _write_strategy

    _write_strategy(
        tmp_path,
        {
            "name": "strategy",
            "edit_url": "https://www.joinquant.com/algorithm/index/edit?algorithmId=x",
            "code": "print('ok')\n",
        },
    )
    manifest = verify_existing_manifest(tmp_path)
    assert manifest["code"]["path"].startswith("code_versions/")
    assert (tmp_path / "default_code.py").read_text(encoding="utf-8") == "print('ok')\n"
    (tmp_path / "default_code.py").unlink()
    with pytest.raises(IntegrityError, match="default_code.py"):
        verify_existing_manifest(tmp_path)


def test_verify_recomputes_parquet_rows_and_gate(tmp_path: Path) -> None:
    from joinquant_sync.archive import (
        IntegrityError,
        evaluate_gate,
        verify_existing_manifest,
    )

    object_dir = tmp_path / "simulation"
    manifest = _commit_test_simulation(object_dir)
    manifest["datasets"]["results"]["rows"] += 1
    manifest["gate"] = evaluate_gate(manifest["datasets"])
    (object_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(IntegrityError, match="row count"):
        verify_existing_manifest(object_dir)


def test_verify_recomputes_gate_exceptions(tmp_path: Path) -> None:
    from joinquant_sync.archive import IntegrityError, verify_existing_manifest

    object_dir = tmp_path / "simulation"
    manifest = _commit_test_simulation(object_dir)
    manifest["gate"] = {"status": "pass", "exceptions": ["forged:complete"]}
    (object_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(IntegrityError, match="gate does not match"):
        verify_existing_manifest(object_dir)


def test_verify_compares_raw_and_parquet_values(tmp_path: Path) -> None:
    import pyarrow as pa
    import pyarrow.parquet as pq

    from joinquant_sync.archive import IntegrityError, verify_existing_manifest

    object_dir = tmp_path / "simulation"
    manifest = _commit_test_simulation(object_dir)
    record = next(
        item
        for item in manifest["datasets"]["results"]["files"]
        if item["format"] == "parquet"
    )
    path = object_dir / record["path"]
    pq.write_table(
        pa.Table.from_pylist([{"time": "2026-01-01", "returns": 9.9}]),
        path,
        compression="zstd",
    )
    record["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    record["bytes"] = path.stat().st_size
    (object_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(IntegrityError, match="raw and parquet contents differ"):
        verify_existing_manifest(object_dir)


def test_verify_compares_attribution_raw_and_parquet_values(tmp_path: Path) -> None:
    import pyarrow as pa
    import pyarrow.parquet as pq

    from joinquant_sync.archive import IntegrityError, verify_existing_manifest

    object_dir = tmp_path / "simulation"
    manifest = _commit_test_simulation(object_dir, writer=True)
    record = next(
        item
        for item in manifest["datasets"]["attribution_log"]["files"]
        if item["format"] == "parquet"
    )
    path = object_dir / record["path"]
    pq.write_table(
        pa.Table.from_pylist(
            [
                {
                    "audit_token": "forged",
                    "seq": 1,
                    "event": "run_start",
                    "current_dt": "2026-01-01T00:00:00",
                },
                {
                    "audit_token": "forged",
                    "seq": 2,
                    "event": "run_end",
                    "current_dt": "2026-01-01T00:01:00",
                },
            ]
        ),
        path,
        compression="zstd",
    )
    record["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    record["bytes"] = path.stat().st_size
    (object_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(IntegrityError, match="attribution raw and parquet"):
        verify_existing_manifest(object_dir)


def test_sparse_attribution_events_round_trip_through_parquet(tmp_path: Path) -> None:
    from joinquant_sync.archive import verify_existing_manifest

    object_dir = tmp_path / "simulation"
    _commit_test_simulation(object_dir, writer=True, sparse_attribution=True)

    assert verify_existing_manifest(object_dir)["gate"]["status"] == "pass"


def test_verify_rejects_tampered_original_research_response(tmp_path: Path) -> None:
    from joinquant_sync.archive import IntegrityError, verify_existing_manifest

    object_dir = tmp_path / "simulation"
    manifest = _commit_test_simulation(object_dir)
    record = manifest["research_response"]
    path = object_dir / record["path"]
    with gzip.open(path, "rt", encoding="utf-8") as stream:
        payload = json.load(stream)
    payload["results"][0]["returns"] = 999.0
    with gzip.open(path, "wt", encoding="utf-8") as stream:
        json.dump(payload, stream, separators=(",", ":"))
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    record["sha256"] = digest
    record["bytes"] = path.stat().st_size
    for name in (
        "results",
        "balances",
        "positions",
        "orders",
        "records",
        "risk",
        "period_risks",
    ):
        manifest["datasets"][name]["pagination"]["research_response_sha256"] = digest
    (object_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(IntegrityError, match="research lineage does not cover"):
        verify_existing_manifest(object_dir)


def test_verify_recomputes_normal_log_rows(tmp_path: Path) -> None:
    from joinquant_sync.archive import IntegrityError, verify_existing_manifest

    object_dir = tmp_path / "simulation"
    manifest = _commit_test_simulation(object_dir)
    manifest["datasets"]["normal_log"]["rows"] = 999
    (object_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(IntegrityError, match="normal log row count"):
        verify_existing_manifest(object_dir)


def test_verify_recomputes_normal_log_pagination_from_raw_pages(tmp_path: Path) -> None:
    from joinquant_sync.archive import IntegrityError, verify_existing_manifest

    object_dir = tmp_path / "simulation"
    manifest = _commit_test_simulation(object_dir)
    manifest["datasets"]["normal_log"]["pagination"] = {
        "pages": 1,
        "cumulative_rows": 1,
        "terminal": False,
        "capped": False,
    }
    (object_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(IntegrityError, match="pagination evidence"):
        verify_existing_manifest(object_dir)


def test_verify_compares_normal_log_raw_pages_and_jsonl_values(tmp_path: Path) -> None:
    from joinquant_sync.archive import IntegrityError, verify_existing_manifest

    object_dir = tmp_path / "simulation"
    manifest = _commit_test_simulation(object_dir)
    record = next(
        item
        for item in manifest["datasets"]["normal_log"]["files"]
        if item.get("format") == "jsonl.gz"
    )
    path = object_dir / record["path"]
    with gzip.open(path, "wt", encoding="utf-8") as stream:
        stream.write('{"offset":0,"text":"forged"}\n')
    record["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    record["bytes"] = path.stat().st_size
    (object_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(IntegrityError, match="log stream digest"):
        verify_existing_manifest(object_dir)


def test_verify_rejects_unstable_research_collection_fence(tmp_path: Path) -> None:
    from joinquant_sync.archive import IntegrityError, verify_existing_manifest

    object_dir = tmp_path / "simulation"
    manifest = _commit_test_simulation(object_dir)
    manifest["collection_fence"] = {
        "collection_before_sha256": "0" * 64,
        "collection_after_sha256": "1" * 64,
    }
    (object_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(IntegrityError, match="collection fence"):
        verify_existing_manifest(object_dir)


def test_verify_rejects_nonexistent_build_object_kind(tmp_path: Path) -> None:
    from joinquant_sync.archive import IntegrityError, verify_existing_manifest

    object_dir = tmp_path / "simulation"
    manifest = _commit_test_simulation(object_dir)
    manifest["object"]["kind"] = "build"
    (object_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(IntegrityError, match="kind"):
        verify_existing_manifest(object_dir)


def test_verify_derives_attribution_requirement_from_code(tmp_path: Path) -> None:
    from joinquant_sync.archive import (
        IntegrityError,
        evaluate_gate,
        verify_existing_manifest,
    )

    object_dir = tmp_path / "simulation"
    manifest = _commit_test_simulation(object_dir, writer=True)
    attribution = manifest["datasets"]["attribution_log"]
    attribution.update(
        required=False,
        status="missing_at_source",
        rows=0,
        files=[],
        evidence={"code_writer": False},
    )
    manifest["gate"] = evaluate_gate(manifest["datasets"])
    (object_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(IntegrityError, match="attribution requirement"):
        verify_existing_manifest(object_dir)


def test_simulation_current_pointers_follow_committed_versions(tmp_path: Path) -> None:
    from joinquant_sync.sync_pipeline import _update_simulation_pointers

    code = tmp_path / "code_versions" / "a.py"
    params = tmp_path / "params_versions" / "b.json"
    source = tmp_path / "source_versions" / "c.json"
    for path, payload in ((code, b"code"), (params, b"params"), (source, b"source")):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
    manifest = {
        "code": {
            "path": "code_versions/a.py",
            "params": {"path": "params_versions/b.json"},
            "source": {"path": "source_versions/c.json"},
        }
    }
    _update_simulation_pointers(tmp_path, manifest)
    assert (tmp_path / "current_code.py").read_bytes() == b"code"
    assert (tmp_path / "params.json").read_bytes() == b"params"
    assert (tmp_path / "source.json").read_bytes() == b"source"


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


def test_simulation_logs_merge_by_offset_and_preserve_order(tmp_path: Path) -> None:
    from joinquant_sync.archive import write_raw_gzip
    from joinquant_sync.sync_pipeline import _merge_simulation_log

    raw = b'{"offset":0,"text":"zero"}\n{"offset":1,"text":"old"}\n'
    evidence = write_raw_gzip(
        raw, tmp_path / "snapshots" / "old" / "raw" / "normal-log.jsonl.gz"
    )
    previous = {
        "datasets": {
            "normal_log": {
                "files": [
                    {
                        "path": "snapshots/old/raw/normal-log.jsonl.gz",
                        "format": "jsonl.gz",
                        "sha256": evidence["compressed_sha256"],
                    }
                ]
            }
        }
    }
    browser = {
        "normal_log": b"",
        "normal_log_status": "complete",
        "normal_log_rows": 2,
        "normal_log_records": [
            {"offset": 1, "text": "new"},
            {"offset": 2, "text": "two"},
        ],
    }
    _merge_simulation_log(browser, previous, tmp_path)
    assert [item["text"] for item in browser["normal_log_records"]] == [
        "zero",
        "new",
        "two",
    ]
    assert browser["normal_log_status"] == "complete"


def test_bundle_time_cursors_never_move_backwards() -> None:
    from joinquant_sync.sync_pipeline import _bundle_time_cursors

    assert _bundle_time_cursors(
        {"results": [{"time": "2026-07-11"}], "orders": []},
        {"results": "2026-07-10", "orders": "2026-07-09"},
    ) == {"results": "2026-07-11", "orders": "2026-07-09"}


def test_legacy_simulation_without_research_lineage_forces_full_refresh() -> None:
    from joinquant_sync.sync_pipeline import _research_after_times

    cursors = {
        "streams": {
            "data": {"cursors": {"results": "2026-07-11"}}
        }
    }
    assert _research_after_times(cursors) == {}

    response = {"path": "raw/response.json.gz", "sha256": "a" * 64}
    current = {
        **cursors,
        "research_response": response,
        "research_lineage": [response],
    }
    assert _research_after_times(current) == {"results": "2026-07-11"}


def test_existing_attribution_raw_is_upgraded_to_queryable_parquet(
    tmp_path: Path,
) -> None:
    from joinquant_sync.query import query_rows
    from joinquant_sync.sync_pipeline import _ensure_queryable_attribution

    manifest = _commit_test_simulation(tmp_path, writer=True)
    dataset = manifest["datasets"]["attribution_log"]
    parquet = next(item for item in dataset["files"] if item.get("format") == "parquet")
    (tmp_path / parquet["path"]).unlink()
    dataset["files"] = [
        item for item in dataset["files"] if item.get("format") != "parquet"
    ]
    (tmp_path / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    upgraded = _ensure_queryable_attribution(tmp_path, manifest)
    assert len(query_rows(tmp_path / "manifest.json", "attribution_log")) == 2
    assert any(
        item.get("format") == "parquet"
        for item in upgraded["datasets"]["attribution_log"]["files"]
    )


def test_paid_supplement_is_recorded_in_manifest(tmp_path: Path) -> None:
    from joinquant_sync.archive import write_raw_gzip
    from joinquant_sync.sync_pipeline import commit_paid_log_supplement

    _commit_test_simulation(tmp_path, capped=True)
    source = tmp_path / "supplements" / "paid" / "source" / "p.zip"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"zip evidence")
    selected_path = tmp_path / "supplements" / "paid" / "selected.jsonl.gz"
    selected_evidence = write_raw_gzip(b"selected\n", selected_path)
    updated = commit_paid_log_supplement(
        tmp_path,
        {"preview_id": "p", "quote": {"credits": 3}},
        source,
        {
            "path": str(selected_path),
            "requested_range": "1000:1001",
            "actual_range": "1000:1001",
            "rows": 1,
            "bytes": selected_evidence["bytes"],
            "sha256": selected_evidence["compressed_sha256"],
        },
        {
            "bytes": source.stat().st_size,
            "sha256": __import__("hashlib").sha256(source.read_bytes()).hexdigest(),
        },
    )
    evidence = updated["datasets"]["normal_log"]["evidence"]
    assert evidence["paid_supplements"][0]["confirmed"] is True


def test_rolling_capped_log_keeps_prior_raw_pages_and_commits(tmp_path: Path) -> None:
    from joinquant_sync.archive import (
        detect_attribution_writer,
        verify_existing_manifest,
    )
    from joinquant_sync.sync_pipeline import commit_simulation_evidence

    object_dir = tmp_path / "simulation"
    previous = _commit_test_simulation(object_dir)
    code = "def initialize(context):\n    pass\n"
    records = [
        {"offset": offset, "text": f"line-{offset}"} for offset in range(1, 1001)
    ]
    browser = {
        "normal_log": (
            "".join(json.dumps(row, separators=(",", ":")) + "\n" for row in records)
        ).encode(),
        "normal_log_records": records,
        "normal_log_raw_pages": [
            {
                "cursor": 1,
                "response": {"data": {"logArr": [row["text"] for row in records]}},
            },
            {"cursor": 0, "response": {"code": 403}, "blocked_free": True},
        ],
        "normal_log_status": "capped_free",
        "normal_log_rows": 1000,
        "log_pages": [],
        "code": code,
        "params": {"start_date": "2026-01-01"},
        "source_backtest": "source",
        "source_raw": b"",
        "code_versions": [code],
    }
    bundle = {
        "metadata": {
            "schema_version": 1,
            "backtest_id": "source",
            "generated_at": "2026-01-01T00:01:00",
            "extraction_method": "joinquant_research_get_backtest",
            "incremental_after": {
                "results": "2026-01-01",
                "balances": "2026-01-01",
            },
            "transfer_modes": {
                "results": "after_time_overlap",
                "balances": "after_time_overlap",
                "positions": "full",
                "orders": "full",
                "records": "full",
            },
        },
        "params": {"start_date": "2026-01-01"},
        "status": "running",
        "results": [{"time": "2026-01-01", "returns": 0.1}],
        "balances": [{"time": "2026-01-01", "cash": 1.0}],
        "positions": [],
        "orders": [],
        "records": [],
        "risk": {"sharpe": 1.0},
        "period_risks": {},
    }
    result = commit_simulation_evidence(
        object_dir,
        tmp_path / "next-stage",
        {
            "local_id": "simulation-001",
            "page_space_id": "space-1",
            "status": "active",
            "detail_url": "https://www.joinquant.com/algorithm/live/index?backtestId=x",
            "aliases": ["x"],
            "collection_fence": {
                "collection_before_sha256": "0" * 64,
                "collection_after_sha256": "0" * 64,
            },
        },
        browser,
        {
            "bundle": bundle,
            "raw": json.dumps(bundle, separators=(",", ":")).encode(),
            "attribution": b"",
        },
        detect_attribution_writer(code),
        previous=previous,
    )
    assert result["status"] == "committed"
    verified = verify_existing_manifest(object_dir)
    assert verified["datasets"]["normal_log"]["status"] == "complete"
    assert verified["datasets"]["normal_log"]["rows"] == 1001
    assert len(verified["research_lineage"]) == 2


def test_production_simulation_core_runs_incrementally_in_memory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import joinquant_sync.sync_pipeline as pipeline
    from joinquant_sync.query import query_rows

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

    def research(
        _page: object,
        _backtest_id: str,
        *,
        attribution_path: str = "",
        attribution_paths: list[str] | None = None,
        after_times: dict[str, str] | None = None,
    ) -> dict[str, object]:
        after = dict(after_times or {})
        calls.append(after)
        full = not after

        def include(name: str) -> bool:
            return full or name in after

        rows = {
            "params": {"start_date": "2026-01-01"},
            "status": "running",
            "results": (
                [{"time": "2026-01-01", "returns": 0.1}] if include("results") else []
            ),
            "balances": (
                [{"time": "2026-01-01", "cash": 1.0}] if include("balances") else []
            ),
            "positions": [
                {
                    "time": "2026-01-01",
                    "pindex": 0,
                    "security": "A",
                    "side": "long",
                }
            ]
            if include("positions")
            else [],
            "orders": [
                {
                    "time": "2026-01-01",
                    "pindex": 0,
                    "security": "A",
                    "side": "long",
                }
            ]
            if include("orders")
            else [],
            "records": [],
            "risk": {"sharpe": 1.0},
            "period_risks": {},
            "metadata": {
                "schema_version": 1,
                "backtest_id": "research-alias",
                "generated_at": "2026-01-01T00:00:00",
                "extraction_method": "joinquant_research_get_backtest",
                "incremental_after": after,
                "transfer_modes": {
                    name: ("after_time_overlap" if name in after else "full")
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

    object_dir = (
        tmp_path
        / "joinquant"
        / "strategies"
        / "strategy-001"
        / "simulations"
        / "simulation-001"
    )
    first = pipeline.sync_all_active_simulations(object(), tmp_path)
    (object_dir / "current_code.py").unlink()
    second = pipeline.sync_all_active_simulations(object(), tmp_path)
    third = pipeline.sync_all_active_simulations(object(), tmp_path)
    assert [first[0]["status"], second[0]["status"], third[0]["status"]] == [
        "committed",
        "unchanged",
        "unchanged",
    ]
    assert calls[:2] == [{}, {}]
    assert calls[2]["results"] == "2026-01-01"
    assert browser_states[0] is None
    assert all(
        state is not None
        and state["code_version_cache"] == browser["code_version_cache"]
        for state in browser_states[1:3]
    )
    assert all(
        state is not None
        and state["normal_log_stop_offset"] == 1
        and state["code_history_total"] == 1
        for state in browser_states[3:9]
    )
    assert len(query_rows(object_dir / "manifest.json", "results")) == 1
    assert len(list((object_dir / "snapshots").iterdir())) == 1
    monkeypatch.setattr(pipeline, "discover_active_simulations", lambda _page: [])
    monkeypatch.setattr(pipeline, "discover_all_simulations", lambda _page: [])
    monkeypatch.setattr(pipeline, "inspect_simulation_status", lambda *_: "closed")
    closed = pipeline.sync_all_active_simulations(object(), tmp_path)
    assert closed[0]["status"] == "committed"
    stopped = json.loads((object_dir / "manifest.json").read_text(encoding="utf-8"))
    assert stopped["tracking"] == "stopped"
    assert stopped["final_sync"] == "complete"
    assert pipeline.sync_all_active_simulations(object(), tmp_path) == []


def test_active_simulation_persists_malformed_log_before_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import joinquant_sync.sync_pipeline as pipeline
    from joinquant_sync.browser import FreeLogIncomplete

    candidate = {
        "page_ordinal": "1",
        "name": "strategy",
        "page_space_id": "space-1",
        "status": "active",
        "detail_url": "https://www.joinquant.com/algorithm/live/index?backtestId=alias",
        "aliases": ["alias"],
    }
    monkeypatch.setattr(
        pipeline, "discover_all_simulations", lambda _page: [dict(candidate)]
    )
    monkeypatch.setattr(
        pipeline,
        "fetch_strategy_default_code",
        lambda _page, _name: {
            "name": "strategy",
            "edit_url": "https://www.joinquant.com/algorithm/index/edit?algorithmId=s",
            "code": "def initialize(context):\n    pass\n",
        },
    )
    monkeypatch.setattr(
        pipeline,
        "fetch_simulation_browser_evidence",
        lambda *_: (_ for _ in ()).throw(
            FreeLogIncomplete(
                "malformed",
                raw_pages=[{"cursor": 100, "raw_text": '{"ok":1}\nBROKEN'}],
            )
        ),
    )
    result = pipeline.sync_all_active_simulations(object(), tmp_path)
    assert result[0]["status"] == "failed"
    evidence = result[0]["failure_evidence"]
    assert Path(evidence["path"]).is_file()
