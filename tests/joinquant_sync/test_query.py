from __future__ import annotations

import json
from pathlib import Path

import duckdb
import pandas as pd
import pytest


def _archive_with_rows(
    tmp_path: Path, dataset: str, rows: list[dict[str, object]]
) -> Path:
    from joinquant_sync.archive import detect_attribution_writer
    from joinquant_sync.sync_pipeline import commit_simulation_evidence

    code = "def initialize(context):\n    pass\n"
    dates = sorted(
        {str(row.get("time"))[:10] for row in rows if row.get("time") not in {None, ""}}
    )
    if not dates:
        dates = ["2026-01-01"]
    bundle: dict[str, object] = {
        "metadata": {
            "schema_version": 1,
            "backtest_id": "query-source",
            "generated_at": "2026-01-01T00:00:00",
            "extraction_method": "joinquant_research_get_backtest",
            "incremental_after": {},
            "transfer_modes": {
                name: "full"
                for name in ("results", "positions", "orders", "records", "balances")
            },
        },
        "params": {"start_date": dates[0], "end_date": dates[-1]},
        "status": "running",
        "results": [{"time": date, "returns": 0.0} for date in dates],
        "balances": [{"time": date, "cash": 1.0} for date in dates],
        "positions": [],
        "orders": [],
        "records": [],
        "risk": {"sharpe": 1.0},
        "period_risks": {},
    }
    bundle[dataset] = rows
    log_record = {"offset": 0, "text": "query fixture"}
    browser = {
        "normal_log": (json.dumps(log_record, separators=(",", ":")) + "\n").encode(),
        "normal_log_records": [log_record],
        "normal_log_raw_pages": [
            {
                "cursor": 0,
                "response": {"data": {"logArr": [log_record["text"]]}},
            }
        ],
        "normal_log_status": "complete",
        "normal_log_rows": 1,
        "log_pages": [{"cursor": 0, "rows": 1}],
        "code": code,
        "params": {"start_date": dates[0], "end_date": dates[-1]},
        "source_backtest": "query-source",
        "source_raw": b'{"query_fixture":true}',
        "code_versions": [code],
    }
    commit_simulation_evidence(
        tmp_path,
        tmp_path.parent / f"{tmp_path.name}-stage",
        {
            "local_id": "simulation-001",
            "page_space_id": "query-space",
            "detail_url": "memory://query",
            "aliases": ["query-source"],
            "status": "active",
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
    )
    return tmp_path / "manifest.json"


def test_duckdb_view_matches_manifest_rows(tmp_path: Path) -> None:
    from joinquant_sync.query import open_views

    manifest = _archive_with_rows(tmp_path, "orders", [{"id": 1}, {"id": 2}])
    connection = duckdb.connect(":memory:")
    assert "orders" in open_views(manifest, connection)
    assert connection.execute("select count(*) from orders").fetchone()[0] == 2
    assert not list(tmp_path.glob("*.duckdb"))


def test_csv_exports_only_requested_columns_and_range(tmp_path: Path) -> None:
    from joinquant_sync.query import export_csv

    manifest = _archive_with_rows(
        tmp_path,
        "orders",
        [
            {"id": 1, "time": "2026-01-01", "price": 10.0},
            {"id": 2, "time": "2026-01-02", "price": 11.0},
        ],
    )
    destination = tmp_path / "out.csv"
    result = export_csv(
        manifest,
        "orders",
        ["id", "time"],
        "2026-01-02",
        "2026-01-02",
        destination,
    )

    assert result["filters"]["start"] == "2026-01-02"
    frame = pd.read_csv(destination)
    assert frame.columns.tolist() == ["id", "time"]
    assert frame["id"].tolist() == [2]


def test_manifest_row_mismatch_blocks_view(tmp_path: Path) -> None:
    from joinquant_sync.query import QueryError, open_views

    manifest = _archive_with_rows(tmp_path, "orders", [{"id": 1}])
    data = json.loads(manifest.read_text(encoding="utf-8"))
    data["datasets"]["orders"]["rows"] = 2
    manifest.write_text(json.dumps(data), encoding="utf-8")

    with pytest.raises(QueryError):
        open_views(manifest, duckdb.connect(":memory:"))


def test_query_rejects_failed_manifest_gate(tmp_path: Path) -> None:
    from joinquant_sync.query import QueryError, open_views

    manifest = _archive_with_rows(tmp_path, "orders", [{"id": 1}])
    data = json.loads(manifest.read_text(encoding="utf-8"))
    data["gate"]["status"] = "fail"
    manifest.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(QueryError):
        open_views(manifest, duckdb.connect(":memory:"))


def test_nested_parquet_path_is_relative_to_object_root(tmp_path: Path) -> None:
    from joinquant_sync.query import write_parquet

    record = write_parquet(
        [{"id": 1}], tmp_path / "data" / "orders.parquet", root=tmp_path
    )
    assert record["path"] == "data/orders.parquet"


def test_cli_query_and_csv_use_manifest_only(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from jq_sync import main

    manifest = _archive_with_rows(
        tmp_path,
        "orders",
        [{"id": 1, "time": "2026-01-01"}, {"id": 2, "time": "2026-01-02"}],
    )
    assert main(["query", "--object", str(manifest), "--dataset", "orders"]) == 0
    assert json.loads(capsys.readouterr().out) == [
        {"id": 1, "time": "2026-01-01"},
        {"id": 2, "time": "2026-01-02"},
    ]
    output = tmp_path / "orders.csv"
    assert (
        main(
            [
                "export-csv",
                "--object",
                str(manifest),
                "--dataset",
                "orders",
                "--fields",
                "id,time",
                "--start",
                "2026-01-02",
                "--end",
                "2026-01-02",
                "--destination",
                str(output),
            ]
        )
        == 0
    )
    assert pd.read_csv(output)["id"].tolist() == [2]


def test_csv_uses_one_manifest_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import joinquant_sync.query as query

    manifest = _archive_with_rows(tmp_path, "orders", [{"id": 1, "time": "2026-01-01"}])
    original = query._load_manifest
    calls = 0

    def counted(path: Path):
        nonlocal calls
        calls += 1
        return original(path)

    monkeypatch.setattr(query, "_load_manifest", counted)
    query.export_csv(
        manifest,
        "orders",
        ["id", "time"],
        "2026-01-01",
        "2026-01-01",
        tmp_path / "snapshot.csv",
    )
    assert calls == 1
