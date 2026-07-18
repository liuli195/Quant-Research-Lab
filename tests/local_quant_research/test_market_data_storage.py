from __future__ import annotations

import hashlib
import json
import multiprocessing
import queue
from pathlib import Path

import pyarrow.parquet as pq
import pytest

from scripts.research.market_data.contracts import (
    CORPORATE_ACTION_FIELDS,
    MARKET_DATA_FIELDS as FIELDS,
    SnapshotSelection,
)
from scripts.research.market_data.storage import (
    MarketDataConflict,
    MarketDataIntegrityError,
    _exclusive_storage_lock,
    audit_store,
    create_snapshot,
    import_batch,
    validate_snapshot,
)


def _import_in_process(
    csv_path: str,
    manifest: dict[str, object],
    root: str,
    ready: object,
    start: object,
    results: object,
) -> None:
    ready.set()
    start.wait()
    try:
        record = import_batch(
            csv_path=Path(csv_path),
            manifest=manifest,
            root=Path(root),
        )
        results.put(("ok", record.batch_id))
    except Exception as exc:  # pragma: no cover - returned to the parent assertion
        results.put(("error", type(exc).__name__, str(exc)))


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _canonical_digest(value: object) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _manifest(*, corporate_action_status: str = "verified_empty") -> dict[str, object]:
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
            "status": corporate_action_status,
        },
    }


def _write_corporate_actions(
    path: Path,
    *,
    split_ratio: str = "2",
    status: str = "active",
    announcement_date: str = "2026-06-30",
) -> Path:
    row = (
        "jq-000001-20260703-split",
        "000001.XSHG",
        "split",
        announcement_date,
        "2026-07-02",
        "2026-07-03",
        "2026-07-03",
        "",
        status,
        "2026-07-15",
        split_ratio,
        "",
        "joinquant.finance.FUND_DIVIDEND",
        "b" * 64,
    )
    path.write_text(
        ",".join(CORPORATE_ACTION_FIELDS) + "\n" + ",".join(row) + "\n",
        encoding="utf-8",
        newline="",
    )
    return path


def _write_empty_corporate_actions(path: Path) -> Path:
    path.write_text(
        ",".join(CORPORATE_ACTION_FIELDS) + "\n",
        encoding="utf-8",
        newline="",
    )
    return path


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


def test_import_batch_is_immutable_complete_and_deduplicated(
    repo_root: Path,
    tmp_path: Path,
) -> None:
    source = repo_root / "tests" / "local_quant_research" / "fixtures" / "daily-bars.csv"

    first = import_batch(csv_path=source, manifest=_manifest(), root=tmp_path)
    batch_dir = tmp_path / "batches" / first.batch_id
    before = {path.name: _sha256(path) for path in batch_dir.iterdir()}
    second = import_batch(csv_path=source, manifest=_manifest(), root=tmp_path)

    assert first == second
    assert {path.name for path in batch_dir.iterdir()} == {
        "manifest.json",
        "market-data.parquet",
        "corporate-actions.parquet",
        "validation.json",
    }
    assert (batch_dir / "market-data.parquet").stat().st_size > 0
    assert {path.name: _sha256(path) for path in batch_dir.iterdir()} == before
    assert [path.name for path in (tmp_path / "batches").iterdir()] == [first.batch_id]

    stored_manifest = json.loads((batch_dir / "manifest.json").read_text(encoding="utf-8"))
    assert stored_manifest["schema_version"] == 3
    assert stored_manifest["transport_csv"]["sha256"] == _sha256(source)
    assert stored_manifest["transport_csv"]["rows"] == 4
    assert stored_manifest["parquet"]["sha256"] == _sha256(
        batch_dir / "market-data.parquet"
    )
    assert stored_manifest["parquet"]["rows"] == 4
    assert len(stored_manifest["content_sha256"]) == 64
    assert stored_manifest["corporate_actions"]["status"] == "verified_empty"
    assert stored_manifest["corporate_actions"]["rows"] == 0
    assert stored_manifest["corporate_actions"]["parquet"]["sha256"] == _sha256(
        batch_dir / "corporate-actions.parquet"
    )
    assert stored_manifest["securities"] == [
        {
            "security": "000001.XSHG",
            "start_date": "2026-01-05",
            "end_date": "2026-01-06",
            "rows": 2,
        },
        {
            "security": "000002.XSHE",
            "start_date": "2026-01-05",
            "end_date": "2026-01-06",
            "rows": 2,
        },
    ]
    validation = json.loads((batch_dir / "validation.json").read_text(encoding="utf-8"))
    assert validation == {
        "schema_version": 3,
        "status": "complete",
        "checks": {
            "field_order": True,
            "nonempty": True,
            "unique_date_security": True,
            "parquet_roundtrip": True,
            "normalized_digest": True,
            "corporate_actions_field_order": True,
            "corporate_actions_primary_key": True,
            "corporate_actions_point_in_time": True,
            "corporate_actions_parquet_roundtrip": True,
            "corporate_actions_normalized_digest": True,
        },
    }


def test_import_batch_publishes_market_data_and_versioned_corporate_actions(
    repo_root: Path,
    tmp_path: Path,
) -> None:
    source = repo_root / "tests/local_quant_research/fixtures/daily-bars.csv"
    actions = _write_corporate_actions(tmp_path / "corporate-actions.csv")

    record = import_batch(
        csv_path=source,
        corporate_actions_csv_path=actions,
        manifest=_manifest(corporate_action_status="complete"),
        root=tmp_path / "store",
    )

    assert {path.name for path in record.path.iterdir()} == {
        "manifest.json",
        "market-data.parquet",
        "corporate-actions.parquet",
        "validation.json",
    }
    stored = json.loads((record.path / "manifest.json").read_text("utf-8"))
    assert stored["schema_version"] == 3
    assert stored["content_sha256"]
    assert stored["corporate_actions"]["content_sha256"]
    assert stored["corporate_actions"]["rows"] == 1
    assert stored["corporate_actions"]["knowledge_cutoff_date"] == "2026-07-15"
    assert stored["corporate_actions"]["parquet"]["sha256"] == _sha256(
        record.path / "corporate-actions.parquet"
    )


def test_import_batch_requires_an_explicit_valid_empty_corporate_action_set(
    repo_root: Path,
    tmp_path: Path,
) -> None:
    source = repo_root / "tests/local_quant_research/fixtures/daily-bars.csv"
    actions = _write_empty_corporate_actions(tmp_path / "corporate-actions.csv")

    record = import_batch(
        csv_path=source,
        corporate_actions_csv_path=actions,
        manifest=_manifest(corporate_action_status="verified_empty"),
        root=tmp_path / "store",
    )

    stored = json.loads((record.path / "manifest.json").read_text("utf-8"))
    assert stored["corporate_actions"]["rows"] == 0
    assert (record.path / "corporate-actions.parquet").is_file()


def test_corporate_actions_participate_in_batch_and_snapshot_identity(
    repo_root: Path,
    tmp_path: Path,
) -> None:
    source = repo_root / "tests/local_quant_research/fixtures/daily-bars.csv"
    first_actions = _write_corporate_actions(tmp_path / "first-actions.csv")
    second_actions = _write_corporate_actions(
        tmp_path / "second-actions.csv",
        split_ratio="3",
    )

    first = import_batch(
        csv_path=source,
        corporate_actions_csv_path=first_actions,
        manifest=_manifest(corporate_action_status="complete"),
        root=tmp_path / "first-store",
    )
    second = import_batch(
        csv_path=source,
        corporate_actions_csv_path=second_actions,
        manifest=_manifest(corporate_action_status="complete"),
        root=tmp_path / "second-store",
    )
    first_snapshot = create_snapshot(
        batch_ids=[first.batch_id],
        selection=_selection("000001.XSHG", "000002.XSHE"),
        root=tmp_path / "first-store",
    )
    second_snapshot = create_snapshot(
        batch_ids=[second.batch_id],
        selection=_selection("000001.XSHG", "000002.XSHE"),
        root=tmp_path / "second-store",
    )

    assert first.batch_id != second.batch_id
    assert first_snapshot.snapshot_id != second_snapshot.snapshot_id
    assert first_snapshot.document["batches"][0]["corporate_actions_sha256"]
    assert first_snapshot.document["batches"][0][
        "corporate_actions_content_sha256"
    ]


def test_snapshot_validation_rejects_tampered_corporate_actions(
    repo_root: Path,
    tmp_path: Path,
) -> None:
    source = repo_root / "tests/local_quant_research/fixtures/daily-bars.csv"
    actions = _write_corporate_actions(tmp_path / "corporate-actions.csv")
    batch = import_batch(
        csv_path=source,
        corporate_actions_csv_path=actions,
        manifest=_manifest(corporate_action_status="complete"),
        root=tmp_path,
    )
    snapshot = create_snapshot(
        batch_ids=[batch.batch_id],
        selection=_selection("000001.XSHG", "000002.XSHE"),
        root=tmp_path,
    )
    path = batch.path / "corporate-actions.parquet"
    path.write_bytes(path.read_bytes() + b"tampered")

    with pytest.raises(MarketDataIntegrityError, match="corporate-actions.*SHA256"):
        validate_snapshot(snapshot.snapshot_id, root=tmp_path)


def test_audit_store_validates_current_batches_and_snapshots(
    repo_root: Path,
    tmp_path: Path,
) -> None:
    source = repo_root / "tests/local_quant_research/fixtures/daily-bars.csv"
    batch = import_batch(csv_path=source, manifest=_manifest(), root=tmp_path)
    snapshot = create_snapshot(
        batch_ids=[batch.batch_id],
        selection=_selection("000001.XSHG", "000002.XSHE"),
        root=tmp_path,
    )

    assert audit_store(root=tmp_path) == {
        "schema_version": 1,
        "status": "complete",
        "parquet_batch_ids": [batch.batch_id],
        "snapshot_ids": [snapshot.snapshot_id],
    }


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"status": "unknown"}, "status"),
        ({"split_ratio": "0"}, "split_ratio"),
    ],
)
def test_import_rejects_invalid_corporate_action_evidence(
    repo_root: Path,
    tmp_path: Path,
    overrides: dict[str, str],
    message: str,
) -> None:
    source = repo_root / "tests/local_quant_research/fixtures/daily-bars.csv"
    actions = _write_corporate_actions(
        tmp_path / "corporate-actions.csv",
        **overrides,
    )

    with pytest.raises(MarketDataIntegrityError, match=message):
        import_batch(
            csv_path=source,
            corporate_actions_csv_path=actions,
            manifest=_manifest(corporate_action_status="complete"),
            root=tmp_path / "store",
        )


def test_import_retains_action_metadata_published_after_effective_date(
    repo_root: Path,
    tmp_path: Path,
) -> None:
    source = repo_root / "tests/local_quant_research/fixtures/daily-bars.csv"
    actions = _write_corporate_actions(
        tmp_path / "corporate-actions.csv",
        announcement_date="2026-07-04",
    )

    record = import_batch(
        csv_path=source,
        corporate_actions_csv_path=actions,
        manifest=_manifest(corporate_action_status="complete"),
        root=tmp_path / "store",
    )

    stored = pq.read_table(record.path / "corporate-actions.parquet").to_pylist()
    assert stored[0]["effective_date"] == "2026-07-03"
    assert stored[0]["announcement_date"] == "2026-07-04"


def test_batch_identity_uses_logical_content_not_csv_line_endings(
    repo_root: Path,
    tmp_path: Path,
) -> None:
    source = repo_root / "tests/local_quant_research/fixtures/daily-bars.csv"
    lf = tmp_path / "lf.csv"
    crlf = tmp_path / "crlf.csv"
    text = source.read_text(encoding="utf-8").replace("\r\n", "\n")
    lf.write_text(text, encoding="utf-8", newline="")
    crlf.write_bytes(text.replace("\n", "\r\n").encode("utf-8"))

    first = import_batch(csv_path=lf, manifest=_manifest(), root=tmp_path / "store")
    second = import_batch(csv_path=crlf, manifest=_manifest(), root=tmp_path / "store")

    assert first.batch_id == second.batch_id
    assert first.manifest["transport_csv"]["sha256"] != _sha256(crlf)
    assert len(list((tmp_path / "store" / "batches").iterdir())) == 1


def test_import_batch_retries_transient_directory_publish_lock(
    repo_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scripts.research.market_data import storage

    source = repo_root / "tests/local_quant_research/fixtures/daily-bars.csv"
    real_replace = storage.os.replace
    calls = 0

    def transient_replace(source_path: Path, target_path: Path) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise PermissionError(
                13,
                "directory is temporarily in use",
                str(source_path),
            )
        real_replace(source_path, target_path)

    monkeypatch.setattr(storage.os, "replace", transient_replace)

    batch = import_batch(csv_path=source, manifest=_manifest(), root=tmp_path)

    assert calls == 2
    assert (tmp_path / "batches" / batch.batch_id / "manifest.json").is_file()


def test_conflicting_overlap_is_rejected_without_changing_old_batch(
    repo_root: Path,
    tmp_path: Path,
) -> None:
    source = repo_root / "tests" / "local_quant_research" / "fixtures" / "daily-bars.csv"
    first = import_batch(csv_path=source, manifest=_manifest(), root=tmp_path)
    old_dir = tmp_path / "batches" / first.batch_id
    old_digest = {path.name: _sha256(path) for path in old_dir.iterdir()}
    conflicting = tmp_path / "conflicting.csv"
    conflicting.write_text(
        source.read_text(encoding="utf-8").replace(
            "2026-01-05,000001.XSHG,10.00,10.20,9.90,10.10,",
            "2026-01-05,000001.XSHG,10.00,10.20,9.90,10.15,",
        ),
        encoding="utf-8",
        newline="",
    )

    with pytest.raises(MarketDataConflict, match="000001.XSHG.*2026-01-05"):
        import_batch(csv_path=conflicting, manifest=_manifest(), root=tmp_path)

    assert {path.name: _sha256(path) for path in old_dir.iterdir()} == old_digest
    assert not list(tmp_path.rglob("*.tmp*"))


def test_new_security_batch_does_not_change_existing_batch(
    repo_root: Path,
    tmp_path: Path,
) -> None:
    source = repo_root / "tests" / "local_quant_research" / "fixtures" / "daily-bars.csv"
    first = import_batch(csv_path=source, manifest=_manifest(), root=tmp_path)
    old_dir = tmp_path / "batches" / first.batch_id
    old_digest = {path.name: _sha256(path) for path in old_dir.iterdir()}
    added = tmp_path / "added.csv"
    header = source.read_text(encoding="utf-8").splitlines()[0]
    added.write_text(
        header
        + "\n2026-01-05,000003.XSHG,30.00,30.30,29.90,30.10,30.00,700,21070,1.0000,0.0,33.00,27.00\n",
        encoding="utf-8",
        newline="",
    )

    second = import_batch(csv_path=added, manifest=_manifest(), root=tmp_path)

    assert second.batch_id != first.batch_id
    assert {path.name: _sha256(path) for path in old_dir.iterdir()} == old_digest
    assert len(list((tmp_path / "batches").iterdir())) == 2


def test_snapshot_only_references_batches_and_remains_stable_after_append(
    repo_root: Path,
    tmp_path: Path,
) -> None:
    source = repo_root / "tests" / "local_quant_research" / "fixtures" / "daily-bars.csv"
    first = import_batch(csv_path=source, manifest=_manifest(), root=tmp_path)
    snapshot = create_snapshot(
        batch_ids=[first.batch_id],
        selection=_selection("000001.XSHG", "000002.XSHE"),
        root=tmp_path,
    )
    snapshot_path = tmp_path / "snapshots" / f"{snapshot.snapshot_id}.json"
    before = _sha256(snapshot_path)

    added = tmp_path / "added.csv"
    header = source.read_text(encoding="utf-8").splitlines()[0]
    added.write_text(
        header
        + "\n2026-01-05,000003.XSHG,30.00,30.30,29.90,30.10,30.00,700,21070,1.0000,0.0,33.00,27.00\n",
        encoding="utf-8",
        newline="",
    )
    import_batch(csv_path=added, manifest=_manifest(), root=tmp_path)

    validated = validate_snapshot(snapshot.snapshot_id, root=tmp_path)
    document = json.loads(snapshot_path.read_text(encoding="utf-8"))
    assert validated == snapshot
    assert _sha256(snapshot_path) == before
    assert document["batch_ids"] == [first.batch_id]
    assert "rows" not in document
    assert not list(tmp_path.rglob("*.duckdb"))
    assert not list(tmp_path.rglob("*.tmp*"))


def test_snapshot_validation_rejects_tampered_authoritative_parquet(
    repo_root: Path,
    tmp_path: Path,
) -> None:
    source = repo_root / "tests" / "local_quant_research" / "fixtures" / "daily-bars.csv"
    batch = import_batch(csv_path=source, manifest=_manifest(), root=tmp_path)
    snapshot = create_snapshot(
        batch_ids=[batch.batch_id],
        selection=_selection("000001.XSHG", "000002.XSHE"),
        root=tmp_path,
    )
    parquet_path = tmp_path / "batches" / batch.batch_id / "market-data.parquet"
    parquet_path.write_bytes(parquet_path.read_bytes() + b"tampered")

    with pytest.raises(MarketDataIntegrityError, match="SHA256"):
        validate_snapshot(snapshot.snapshot_id, root=tmp_path)


def test_tampered_batch_identity_cannot_be_repackaged_as_a_new_snapshot(
    repo_root: Path,
    tmp_path: Path,
) -> None:
    source = repo_root / "tests" / "local_quant_research" / "fixtures" / "daily-bars.csv"
    batch = import_batch(csv_path=source, manifest=_manifest(), root=tmp_path)
    manifest_path = tmp_path / "batches" / batch.batch_id / "manifest.json"
    document = json.loads(manifest_path.read_text(encoding="utf-8"))
    document["source"]["environment"] = "tampered"
    manifest_path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(MarketDataIntegrityError, match="batch identity"):
        create_snapshot(
            batch_ids=[batch.batch_id],
            selection=_selection("000001.XSHG", "000002.XSHE"),
            root=tmp_path,
        )


def test_tampered_validation_evidence_cannot_be_reused(
    repo_root: Path,
    tmp_path: Path,
) -> None:
    source = repo_root / "tests" / "local_quant_research" / "fixtures" / "daily-bars.csv"
    batch = import_batch(csv_path=source, manifest=_manifest(), root=tmp_path)
    validation_path = tmp_path / "batches" / batch.batch_id / "validation.json"
    document = json.loads(validation_path.read_text(encoding="utf-8"))
    document["checks"]["unique_date_security"] = False
    validation_path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(MarketDataIntegrityError, match="validation evidence"):
        create_snapshot(
            batch_ids=[batch.batch_id],
            selection=_selection("000001.XSHG", "000002.XSHE"),
            root=tmp_path,
        )


@pytest.mark.parametrize(
    "invalid_id",
    ["..\\..\\outside", "../../outside", "g" * 64, "a" * 63],
)
def test_storage_identifiers_are_strict_sha256_values(
    invalid_id: str,
    tmp_path: Path,
) -> None:
    with pytest.raises(MarketDataIntegrityError, match="identifier"):
        create_snapshot(
            batch_ids=[invalid_id],
            selection=_selection("000001.XSHG"),
            root=tmp_path,
        )
    with pytest.raises(MarketDataIntegrityError, match="identifier"):
        validate_snapshot(invalid_id, root=tmp_path)


def test_snapshot_rejects_incomplete_end_date_coverage(
    repo_root: Path,
    tmp_path: Path,
) -> None:
    source = repo_root / "tests" / "local_quant_research" / "fixtures" / "daily-bars.csv"
    batch = import_batch(csv_path=source, manifest=_manifest(), root=tmp_path)
    selection = SnapshotSelection(
        source={"name": "joinquant", "environment": "research"},
        asset_type="etf",
        frequency="1d",
        securities=("000001.XSHG", "000002.XSHE"),
        start_date="2026-01-05",
        end_date="2026-12-31",
        fields=FIELDS,
        price_semantics={"fq": None, "skip_paused": False},
    )

    with pytest.raises(MarketDataIntegrityError, match="end_date coverage"):
        create_snapshot(batch_ids=[batch.batch_id], selection=selection, root=tmp_path)


def test_import_rejects_rows_with_missing_or_extra_columns(
    repo_root: Path,
    tmp_path: Path,
) -> None:
    source = repo_root / "tests" / "local_quant_research" / "fixtures" / "daily-bars.csv"
    lines = source.read_text(encoding="utf-8").splitlines()
    malformed_rows = {
        "missing": ",".join(lines[1].split(",")[:-1]),
        "extra": lines[1] + ",unexpected",
    }

    for name, row in malformed_rows.items():
        malformed = tmp_path / f"{name}.csv"
        malformed.write_text(lines[0] + "\n" + row + "\n", encoding="utf-8")
        with pytest.raises(MarketDataIntegrityError, match="column count"):
            import_batch(csv_path=malformed, manifest=_manifest(), root=tmp_path)


def test_snapshot_can_reference_multiple_verified_batches(
    repo_root: Path,
    tmp_path: Path,
) -> None:
    source = repo_root / "tests" / "local_quant_research" / "fixtures" / "daily-bars.csv"
    first = import_batch(csv_path=source, manifest=_manifest(), root=tmp_path)
    added = tmp_path / "added.csv"
    header = source.read_text(encoding="utf-8").splitlines()[0]
    added.write_text(
        header
        + "\n2026-01-05,000003.XSHG,30.00,30.30,29.90,30.10,30.00,700,21070,1.0000,0.0,33.00,27.00"
        + "\n2026-01-06,000003.XSHG,30.10,30.40,30.00,30.20,30.10,750,22650,1.0000,0.0,33.11,27.09\n",
        encoding="utf-8",
    )
    second = import_batch(csv_path=added, manifest=_manifest(), root=tmp_path)

    snapshot = create_snapshot(
        batch_ids=[second.batch_id, first.batch_id],
        selection=_selection("000001.XSHG", "000003.XSHG"),
        root=tmp_path,
    )

    assert list(snapshot.document["batch_ids"]) == sorted(
        [first.batch_id, second.batch_id]
    )
    assert list(snapshot.document["coverage"]) == [
        {
            "security": "000001.XSHG",
            "start_date": "2026-01-05",
            "end_date": "2026-01-06",
            "rows": 2,
        },
        {
            "security": "000003.XSHG",
            "start_date": "2026-01-05",
            "end_date": "2026-01-06",
            "rows": 2,
        },
    ]
    assert all("validation_sha256" in item for item in snapshot.document["batches"])
    assert all("parquet_sha256" in item for item in snapshot.document["batches"])
    assert validate_snapshot(snapshot.snapshot_id, root=tmp_path) == snapshot


def test_snapshot_requires_the_complete_source_identity(
    repo_root: Path,
    tmp_path: Path,
) -> None:
    source = repo_root / "tests" / "local_quant_research" / "fixtures" / "daily-bars.csv"
    batch = import_batch(csv_path=source, manifest=_manifest(), root=tmp_path)
    selection = SnapshotSelection(
        source={"name": "joinquant", "environment": "different"},
        asset_type="etf",
        frequency="1d",
        securities=("000001.XSHG",),
        start_date="2026-01-05",
        end_date="2026-01-06",
        fields=FIELDS,
        price_semantics={"fq": None, "skip_paused": False},
    )

    with pytest.raises(MarketDataIntegrityError, match="source"):
        create_snapshot(batch_ids=[batch.batch_id], selection=selection, root=tmp_path)


def test_storage_lock_serializes_cross_process_batch_publication(
    repo_root: Path,
    tmp_path: Path,
) -> None:
    source = repo_root / "tests" / "local_quant_research" / "fixtures" / "daily-bars.csv"
    context = multiprocessing.get_context("spawn")
    ready = context.Event()
    start = context.Event()
    results = context.Queue()
    process = context.Process(
        target=_import_in_process,
        args=(str(source), _manifest(), str(tmp_path), ready, start, results),
    )

    with _exclusive_storage_lock(tmp_path):
        process.start()
        assert ready.wait(timeout=5)
        start.set()
        with pytest.raises(queue.Empty):
            results.get(timeout=0.5)

    result = results.get(timeout=5)
    process.join(timeout=5)
    assert result[0] == "ok", result
    assert process.exitcode == 0


def test_records_expose_deeply_immutable_evidence(
    repo_root: Path,
    tmp_path: Path,
) -> None:
    source = repo_root / "tests" / "local_quant_research" / "fixtures" / "daily-bars.csv"
    batch = import_batch(csv_path=source, manifest=_manifest(), root=tmp_path)
    snapshot = create_snapshot(
        batch_ids=[batch.batch_id],
        selection=_selection("000001.XSHG", "000002.XSHE"),
        root=tmp_path,
    )

    with pytest.raises(TypeError):
        batch.manifest["source"]["environment"] = "mutated"
    with pytest.raises(TypeError):
        snapshot.document["selection"]["source"]["environment"] = "mutated"


def test_snapshot_validation_rejects_duplicate_or_extra_batch_evidence(
    repo_root: Path,
    tmp_path: Path,
) -> None:
    source = repo_root / "tests" / "local_quant_research" / "fixtures" / "daily-bars.csv"
    batch = import_batch(csv_path=source, manifest=_manifest(), root=tmp_path)
    snapshot = create_snapshot(
        batch_ids=[batch.batch_id],
        selection=_selection("000001.XSHG", "000002.XSHE"),
        root=tmp_path,
    )
    document = json.loads(snapshot.path.read_text(encoding="utf-8"))
    document["batch_ids"].append(batch.batch_id)
    document["batches"].append(dict(document["batches"][0]))
    payload = {key: value for key, value in document.items() if key != "snapshot_id"}
    malformed_id = _canonical_digest(payload)
    document["snapshot_id"] = malformed_id
    malformed_path = tmp_path / "snapshots" / f"{malformed_id}.json"
    malformed_path.write_text(
        json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(MarketDataIntegrityError, match="canonical batch evidence"):
        validate_snapshot(malformed_id, root=tmp_path)


def test_snapshot_validation_rejects_embedded_path_traversal_batch_id(
    repo_root: Path,
    tmp_path: Path,
) -> None:
    source = repo_root / "tests" / "local_quant_research" / "fixtures" / "daily-bars.csv"
    batch = import_batch(csv_path=source, manifest=_manifest(), root=tmp_path)
    snapshot = create_snapshot(
        batch_ids=[batch.batch_id],
        selection=_selection("000001.XSHG", "000002.XSHE"),
        root=tmp_path,
    )
    document = json.loads(snapshot.path.read_text(encoding="utf-8"))
    malicious_batch_id = "../../" + "a" * 64
    document["batch_ids"][0] = malicious_batch_id
    document["batches"][0]["batch_id"] = malicious_batch_id
    payload = {key: value for key, value in document.items() if key != "snapshot_id"}
    malformed_id = _canonical_digest(payload)
    document["snapshot_id"] = malformed_id
    malformed_path = tmp_path / "snapshots" / f"{malformed_id}.json"
    malformed_path.write_text(
        json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(MarketDataIntegrityError, match="batch identifier"):
        validate_snapshot(malformed_id, root=tmp_path)


def test_missing_batch_and_deleted_snapshot_evidence_are_rejected(
    repo_root: Path,
    tmp_path: Path,
) -> None:
    missing_batch_id = "f" * 64
    with pytest.raises(MarketDataIntegrityError, match="batch does not exist"):
        create_snapshot(
            batch_ids=[missing_batch_id],
            selection=_selection("000001.XSHG"),
            root=tmp_path,
        )

    source = repo_root / "tests" / "local_quant_research" / "fixtures" / "daily-bars.csv"
    batch = import_batch(csv_path=source, manifest=_manifest(), root=tmp_path)
    snapshot = create_snapshot(
        batch_ids=[batch.batch_id],
        selection=_selection("000001.XSHG", "000002.XSHE"),
        root=tmp_path,
    )
    snapshot.path.unlink()
    with pytest.raises(MarketDataIntegrityError, match="invalid JSON evidence"):
        validate_snapshot(snapshot.snapshot_id, root=tmp_path)
