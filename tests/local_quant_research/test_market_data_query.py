from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from types import MappingProxyType

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

CORPORATE_ACTION_FIELDS = (
    "source_event_id",
    "security",
    "event_type",
    "announcement_date",
    "record_date",
    "ex_date",
    "effective_date",
    "pay_date",
    "status",
    "knowledge_cutoff_date",
    "split_ratio",
    "cash_per_share",
    "source",
    "source_record_sha256",
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
        "corporate_actions": {
            "source": {
                "name": "joinquant",
                "dataset": "finance.FUND_DIVIDEND",
            },
            "knowledge_cutoff_date": "2026-07-15",
            "status": "verified_empty",
        },
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
    assert view.corporate_actions == ()
    assert view.corporate_actions_digest
    with pytest.raises(TypeError):
        view.rows[0]["close"] = 99.0
    assert not list(tmp_path.rglob("*.duckdb"))


def test_open_snapshot_returns_corporate_actions_from_the_same_memory_database(
    repo_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scripts.research.market_data import query

    source = repo_root / "tests/local_quant_research/fixtures/daily-bars.csv"
    action_path = tmp_path / "corporate-actions.csv"
    action_path.write_text(
        ",".join(CORPORATE_ACTION_FIELDS)
        + "\n"
        + ",".join(
            (
                "jq-000001-20260106-cash",
                "000001.XSHG",
                "cash_dividend",
                "2026-01-05",
                "2026-01-05",
                "2026-01-06",
                "2026-01-06",
                "2026-01-08",
                "active",
                "2026-07-15",
                "",
                "0.1",
                "joinquant.finance.FUND_DIVIDEND",
                "b" * 64,
            )
        )
        + "\n",
        encoding="utf-8",
    )
    manifest = _manifest()
    manifest["corporate_actions"]["status"] = "complete"
    batch = import_batch(
        csv_path=source,
        corporate_actions_csv_path=action_path,
        manifest=manifest,
        root=tmp_path / "store",
    )
    snapshot = create_snapshot(
        batch_ids=[batch.batch_id],
        selection=_selection("000001.XSHG", "000002.XSHE"),
        root=tmp_path / "store",
    )
    connections: list[str] = []
    real_connect = query.duckdb.connect

    def recording_connect(database: str):
        connections.append(database)
        return real_connect(database)

    monkeypatch.setattr(query.duckdb, "connect", recording_connect)

    view = query.open_snapshot(snapshot.snapshot_id, root=tmp_path / "store")

    assert connections == [":memory:"]
    assert view.corporate_action_fields == CORPORATE_ACTION_FIELDS
    assert len(view.corporate_actions) == 1
    assert view.corporate_actions[0]["source_event_id"] == "jq-000001-20260106-cash"
    assert view.corporate_actions[0]["cash_per_share"] == 0.1
    with pytest.raises(TypeError):
        view.corporate_actions[0]["cash_per_share"] = 1.0
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


def test_snapshot_overlap_accepts_exact_subset_with_shared_batch(
    repo_root: Path,
    tmp_path: Path,
) -> None:
    from scripts.research.market_data.query import validate_snapshot_overlap

    source = repo_root / "tests/local_quant_research/fixtures/daily-bars.csv"
    batch = import_batch(csv_path=source, manifest=_manifest(), root=tmp_path)
    baseline = create_snapshot(
        batch_ids=[batch.batch_id],
        selection=_selection("000001.XSHG"),
        root=tmp_path,
    )
    expanded = create_snapshot(
        batch_ids=[batch.batch_id],
        selection=_selection("000001.XSHG", "000002.XSHE"),
        root=tmp_path,
    )

    evidence = validate_snapshot_overlap(
        baseline.snapshot_id,
        expanded.snapshot_id,
        root=tmp_path,
    )

    assert evidence.securities == ("000001.XSHG",)
    assert evidence.market_digest
    assert evidence.corporate_actions_digest


def test_snapshot_overlap_rejects_corporate_action_drift(
    repo_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scripts.research.market_data import query

    source = repo_root / "tests/local_quant_research/fixtures/daily-bars.csv"
    batch = import_batch(csv_path=source, manifest=_manifest(), root=tmp_path)
    baseline = create_snapshot(
        batch_ids=[batch.batch_id],
        selection=_selection("000001.XSHG"),
        root=tmp_path,
    )
    expanded = create_snapshot(
        batch_ids=[batch.batch_id],
        selection=_selection("000001.XSHG", "000002.XSHE"),
        root=tmp_path,
    )
    left_view = query.open_snapshot(baseline.snapshot_id, root=tmp_path)
    right_view = query.open_snapshot(expanded.snapshot_id, root=tmp_path)
    changed_action = MappingProxyType(
        {
            field: value
            for field, value in zip(
                CORPORATE_ACTION_FIELDS,
                (
                    "drift",
                    "000001.XSHG",
                    "cash_dividend",
                    "2026-01-05",
                    "2026-01-05",
                    "2026-01-06",
                    "2026-01-06",
                    "2026-01-08",
                    "active",
                    "2026-07-15",
                    None,
                    0.1,
                    "fixture",
                    "b" * 64,
                ),
            )
        }
    )
    changed_right = replace(right_view, corporate_actions=(changed_action,))
    views = iter((left_view, changed_right))
    monkeypatch.setattr(query, "open_snapshot", lambda *_args, **_kwargs: next(views))

    with pytest.raises(MarketDataIntegrityError, match="corporate-actions"):
        query.validate_snapshot_overlap(
            baseline.snapshot_id,
            expanded.snapshot_id,
            root=tmp_path,
        )
