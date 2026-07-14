from __future__ import annotations

import hashlib
import json
import multiprocessing
import queue
from pathlib import Path

import pytest

from scripts.research.market_data.contracts import SnapshotSelection
from scripts.research.market_data.storage import (
    MarketDataConflict,
    MarketDataIntegrityError,
    _exclusive_storage_lock,
    create_snapshot,
    import_batch,
    validate_snapshot,
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


def _write_legacy_batch(root: Path, source: Path) -> Path:
    csv_bytes = source.read_bytes()
    csv_sha256 = hashlib.sha256(csv_bytes).hexdigest()
    legacy_manifest = {
        **_manifest(),
        "csv": {"sha256": csv_sha256, "bytes": len(csv_bytes), "rows": 4},
        "securities": [
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
        ],
    }
    identity = {
        "source": legacy_manifest["source"],
        "asset_type": legacy_manifest["asset_type"],
        "frequency": legacy_manifest["frequency"],
        "fields": legacy_manifest["fields"],
        "price_semantics": legacy_manifest["price_semantics"],
        "export_code_sha256": legacy_manifest["export_code_sha256"],
        "csv_sha256": csv_sha256,
    }
    batch_dir = root / "batches" / _canonical_digest(identity)
    batch_dir.mkdir(parents=True)
    (batch_dir / "manifest.json").write_text(
        json.dumps(
            legacy_manifest,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n",
        encoding="utf-8",
    )
    (batch_dir / "market-data.csv").write_bytes(csv_bytes)
    (batch_dir / "validation.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "status": "complete",
                "checks": {
                    "field_order": True,
                    "nonempty": True,
                    "unique_date_security": True,
                },
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n",
        encoding="utf-8",
    )
    return batch_dir


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
        "validation.json",
    }
    assert (batch_dir / "market-data.parquet").stat().st_size > 0
    assert {path.name: _sha256(path) for path in batch_dir.iterdir()} == before
    assert [path.name for path in (tmp_path / "batches").iterdir()] == [first.batch_id]

    stored_manifest = json.loads((batch_dir / "manifest.json").read_text(encoding="utf-8"))
    assert stored_manifest["schema_version"] == 2
    assert stored_manifest["transport_csv"]["sha256"] == _sha256(source)
    assert stored_manifest["transport_csv"]["rows"] == 4
    assert stored_manifest["parquet"]["sha256"] == _sha256(
        batch_dir / "market-data.parquet"
    )
    assert stored_manifest["parquet"]["rows"] == 4
    assert len(stored_manifest["content_sha256"]) == 64
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
        "schema_version": 2,
        "status": "complete",
        "checks": {
            "field_order": True,
            "nonempty": True,
            "unique_date_security": True,
            "parquet_roundtrip": True,
            "normalized_digest": True,
        },
    }


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


def test_legacy_csv_batch_allows_non_overlapping_parquet_import(
    repo_root: Path,
    tmp_path: Path,
) -> None:
    source = repo_root / "tests/local_quant_research/fixtures/daily-bars.csv"
    legacy_dir = _write_legacy_batch(tmp_path, source)
    added = tmp_path / "added.csv"
    added.write_text(
        ",".join(FIELDS)
        + "\n2026-01-05,000003.XSHG,30,31,29,30.5,30,100,3050,1,0,33,27\n",
        encoding="utf-8",
    )

    record = import_batch(csv_path=added, manifest=_manifest(), root=tmp_path)

    assert (record.path / "market-data.parquet").is_file()
    assert (legacy_dir / "market-data.csv").is_file()


def test_legacy_csv_batch_still_rejects_conflicting_overlap(
    repo_root: Path,
    tmp_path: Path,
) -> None:
    source = repo_root / "tests/local_quant_research/fixtures/daily-bars.csv"
    _write_legacy_batch(tmp_path, source)
    conflicting = tmp_path / "conflicting.csv"
    conflicting.write_text(
        source.read_text(encoding="utf-8").replace(
            "2026-01-05,000001.XSHG,10.00,10.20,9.90,10.10,",
            "2026-01-05,000001.XSHG,10.00,10.20,9.90,10.15,",
        ),
        encoding="utf-8",
    )

    with pytest.raises(MarketDataConflict, match="000001.XSHG.*2026-01-05"):
        import_batch(csv_path=conflicting, manifest=_manifest(), root=tmp_path)


def test_new_snapshot_rejects_legacy_csv_batch_with_migration_message(
    repo_root: Path,
    tmp_path: Path,
) -> None:
    source = repo_root / "tests/local_quant_research/fixtures/daily-bars.csv"
    legacy_dir = _write_legacy_batch(tmp_path, source)

    with pytest.raises(
        MarketDataIntegrityError,
        match=f"legacy CSV batch requires migration: {legacy_dir.name}",
    ):
        create_snapshot(
            batch_ids=[legacy_dir.name],
            selection=_selection("000001.XSHG", "000002.XSHE"),
            root=tmp_path,
        )


def test_audit_store_reports_legacy_and_parquet_batches_without_mutation(
    repo_root: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from scripts.research.market_data.cli import main

    source = repo_root / "tests/local_quant_research/fixtures/daily-bars.csv"
    legacy_dir = _write_legacy_batch(tmp_path, source)
    added = tmp_path / "added.csv"
    added.write_text(
        ",".join(FIELDS)
        + "\n2026-01-05,000003.XSHG,30,31,29,30.5,30,100,3050,1,0,33,27\n",
        encoding="utf-8",
    )
    parquet = import_batch(csv_path=added, manifest=_manifest(), root=tmp_path)
    before = {
        path.relative_to(tmp_path).as_posix(): _sha256(path)
        for path in tmp_path.rglob("*")
        if path.is_file() and path.name != ".market-data.lock"
    }

    assert main(["audit", "--root", str(tmp_path)]) == 0

    document = json.loads(capsys.readouterr().out)
    assert document["status"] == "complete"
    assert document["legacy_batch_ids"] == [legacy_dir.name]
    assert document["parquet_batch_ids"] == [parquet.batch_id]
    after = {
        path.relative_to(tmp_path).as_posix(): _sha256(path)
        for path in tmp_path.rglob("*")
        if path.is_file() and path.name != ".market-data.lock"
    }
    assert after == before


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
