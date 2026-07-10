from __future__ import annotations

import json
from pathlib import Path

import duckdb
import pandas as pd
import pytest


def _archive_with_rows(
    tmp_path: Path, dataset: str, rows: list[dict[str, object]]
) -> Path:
    from joinquant_sync.query import write_parquet

    data_path = tmp_path / f"{dataset}.parquet"
    file_record = write_parquet(rows, data_path)
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "datasets": {
                    dataset: {
                        "status": "complete",
                        "rows": len(rows),
                        "files": [file_record],
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    return manifest


def test_duckdb_view_matches_manifest_rows(tmp_path: Path) -> None:
    from joinquant_sync.query import open_views

    manifest = _archive_with_rows(tmp_path, "orders", [{"id": 1}, {"id": 2}])
    connection = duckdb.connect(":memory:")
    assert open_views(manifest, connection) == ["orders"]
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
