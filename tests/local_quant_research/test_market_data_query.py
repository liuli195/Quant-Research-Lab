from __future__ import annotations

from pathlib import Path

import pytest

from scripts.research.market_data.contracts import SnapshotSelection
from scripts.research.market_data.storage import (
    MarketDataIntegrityError,
    create_snapshot,
    import_batch,
)


FIELDS = (
    "date",
    "security",
    "open",
    "high",
    "low",
    "close",
    "pre_close",
    "volume",
    "money",
    "factor",
    "paused",
    "high_limit",
    "low_limit",
)


def _manifest() -> dict[str, object]:
    return {
        "schema_version": 1,
        "source": {"name": "joinquant", "environment": "research"},
        "asset_type": "etf",
        "frequency": "1d",
        "fields": list(FIELDS),
        "price_semantics": {"fq": None, "skip_paused": False},
        "export_code_sha256": "a" * 64,
    }


def _selection(*securities: str) -> SnapshotSelection:
    return SnapshotSelection(
        source={"name": "joinquant", "environment": "research"},
        asset_type="etf",
        frequency="1d",
        securities=securities,
        start_date="2026-01-05",
        end_date="2026-01-06",
        fields=FIELDS,
        price_semantics={"fq": None, "skip_paused": False},
    )


def _snapshot(repo_root: Path, root: Path):
    source = repo_root / "tests" / "local_quant_research" / "fixtures" / "daily-bars.csv"
    batch = import_batch(csv_path=source, manifest=_manifest(), root=root)
    return create_snapshot(
        batch_ids=[batch.batch_id],
        selection=_selection("000001.XSHG", "000002.XSHE"),
        root=root,
    )


def test_open_snapshot_uses_only_memory_and_returns_normalized_read_only_rows(
    repo_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scripts.research.market_data import query

    snapshot = _snapshot(repo_root, tmp_path)
    real_connect = query.duckdb.connect
    connections: list[str] = []
    queried_paths: list[tuple[Path, ...]] = []
    real_reader = query._read_query_rows

    def recording_connect(database: str):
        connections.append(database)
        return real_connect(database)

    def recording_reader(connection, parquet_paths):
        queried_paths.append(tuple(parquet_paths))
        return real_reader(connection, parquet_paths)

    monkeypatch.setattr(query.duckdb, "connect", recording_connect)
    monkeypatch.setattr(query, "_read_query_rows", recording_reader)

    view = query.open_snapshot(snapshot.snapshot_id, root=tmp_path)

    assert connections == [":memory:"]
    assert queried_paths
    assert all(path.name == "market-data.parquet" for path in queried_paths[0])
    assert view.snapshot_id == snapshot.snapshot_id
    assert view.fields == FIELDS
    assert [(row["date"], row["security"]) for row in view.rows] == [
        ("2026-01-05", "000001.XSHG"),
        ("2026-01-05", "000002.XSHE"),
        ("2026-01-06", "000001.XSHG"),
        ("2026-01-06", "000002.XSHE"),
    ]
    assert view.rows[0]["open"] == 10.0
    assert view.rows[0]["paused"] is False
    assert view.digest == query.normalized_digest(view.rows)
    with pytest.raises(TypeError):
        view.rows[0]["close"] = 99.0
    assert not list(tmp_path.rglob("*.duckdb"))


def test_open_snapshot_normalizes_nulls_and_numeric_paused(tmp_path: Path) -> None:
    from scripts.research.market_data.query import open_snapshot

    source = tmp_path / "nullable.csv"
    source.write_text(
        ",".join(FIELDS)
        + "\n2026-01-05,000003.XSHG,30.00,30.20,29.90,30.10,30.00,,"
        + "9030,1.0000,1.0,33.00,27.00"
        + "\n2026-01-06,000003.XSHG,30.10,30.30,30.00,30.20,30.10,1000,"
        + "30200,1.0000,0,33.11,27.09\n",
        encoding="utf-8",
    )
    batch = import_batch(csv_path=source, manifest=_manifest(), root=tmp_path / "store")
    snapshot = create_snapshot(
        batch_ids=[batch.batch_id],
        selection=_selection("000003.XSHG"),
        root=tmp_path / "store",
    )

    view = open_snapshot(snapshot.snapshot_id, root=tmp_path / "store")

    assert view.rows[0]["volume"] is None
    assert view.rows[0]["paused"] is True
    assert view.rows[1]["paused"] is False


def test_snapshot_query_supports_multiple_batches_without_duplicate_rows(
    repo_root: Path,
    tmp_path: Path,
) -> None:
    from scripts.research.market_data.query import open_snapshot

    source = repo_root / "tests" / "local_quant_research" / "fixtures" / "daily-bars.csv"
    first = import_batch(csv_path=source, manifest=_manifest(), root=tmp_path)
    extra = tmp_path / "extra.csv"
    extra.write_text(
        ",".join(FIELDS)
        + "\n2026-01-05,000003.XSHG,30,31,29,30.5,30,100,3050,1,0,33,27"
        + "\n2026-01-06,000003.XSHG,30.5,32,30,31,30.5,110,3410,1,0,33.55,27.45\n",
        encoding="utf-8",
    )
    second = import_batch(csv_path=extra, manifest=_manifest(), root=tmp_path)
    snapshot = create_snapshot(
        batch_ids=[first.batch_id, second.batch_id],
        selection=_selection("000001.XSHG", "000003.XSHG"),
        root=tmp_path,
    )

    view = open_snapshot(snapshot.snapshot_id, root=tmp_path)

    assert len(view.rows) == 4
    assert {row["security"] for row in view.rows} == {
        "000001.XSHG",
        "000003.XSHG",
    }


def test_normalized_digest_is_independent_of_input_order() -> None:
    from scripts.research.market_data.query import normalized_digest

    rows = [
        {"date": "2026-01-06", "security": "000001.XSHG", "close": 10.2},
        {"date": "2026-01-05", "security": "000001.XSHG", "close": 10.1},
    ]

    assert normalized_digest(rows) == normalized_digest(reversed(rows))


def test_open_snapshot_rejects_query_and_parquet_content_drift(
    repo_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scripts.research.market_data import query

    snapshot = _snapshot(repo_root, tmp_path)
    real_reader = query._read_query_rows

    def changed_reader(*args, **kwargs):
        rows = real_reader(*args, **kwargs)
        rows[0]["close"] = "999"
        return rows

    monkeypatch.setattr(query, "_read_query_rows", changed_reader)

    with pytest.raises(MarketDataIntegrityError, match="normalized digest"):
        query.open_snapshot(snapshot.snapshot_id, root=tmp_path)
