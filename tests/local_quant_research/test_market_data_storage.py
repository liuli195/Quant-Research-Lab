from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from scripts.research.market_data.contracts import SnapshotSelection
from scripts.research.market_data.storage import (
    MarketDataConflict,
    MarketDataIntegrityError,
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


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


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
        source="joinquant",
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
        "market-data.csv",
        "validation.json",
    }
    assert (batch_dir / "market-data.csv").read_bytes() == source.read_bytes()
    assert {path.name: _sha256(path) for path in batch_dir.iterdir()} == before
    assert [path.name for path in (tmp_path / "batches").iterdir()] == [first.batch_id]

    stored_manifest = json.loads((batch_dir / "manifest.json").read_text(encoding="utf-8"))
    assert stored_manifest["csv"]["sha256"] == _sha256(source)
    assert stored_manifest["csv"]["rows"] == 4
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
        "schema_version": 1,
        "status": "complete",
        "checks": {
            "field_order": True,
            "nonempty": True,
            "unique_date_security": True,
        },
    }


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


def test_snapshot_validation_rejects_tampered_authoritative_csv(
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
    csv_path = tmp_path / "batches" / batch.batch_id / "market-data.csv"
    csv_path.write_bytes(csv_path.read_bytes() + b"\n")

    with pytest.raises(MarketDataIntegrityError, match="SHA256"):
        validate_snapshot(snapshot.snapshot_id, root=tmp_path)
