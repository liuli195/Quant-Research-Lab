from __future__ import annotations

import csv
import io
from dataclasses import replace
from pathlib import Path

import pytest

from scripts.research.market_data import storage
from scripts.research.market_data.contracts import (
    BatchRecord, CORPORATE_ACTION_FIELDS, MARKET_DATA_FIELDS as FIELDS,
    MarketDataContractError, SnapshotRecord, SnapshotSelection,
    normalize_market_rows, normalized_digest,
)


def _manifest() -> dict[str, object]:
    return {"schema_version": 1, "source": {"name": "joinquant", "environment": "research"},
            "asset_type": "etf", "frequency": "1d", "fields": list(FIELDS),
            "price_semantics": {"fq": None, "skip_paused": False}, "export_code_sha256": "a" * 64,
            "corporate_actions": {"source": {"name": "joinquant", "dataset": "finance.FUND_DIVIDEND"},
                                  "knowledge_cutoff_date": "2026-07-15", "status": "complete"}}


def _action(**overrides: str) -> dict[str, str]:
    values = ("jq-000001-20260703-split", "000001.XSHG", "split", "2026-06-30",
              "2026-07-02", "2026-07-03", "2026-07-03", "", "active", "2026-07-15",
              "2", "", "joinquant.finance.FUND_DIVIDEND", "b" * 64)
    return {**dict(zip(CORPORATE_ACTION_FIELDS, values)), **overrides}


def _selection() -> SnapshotSelection:
    return SnapshotSelection(source={"name": "joinquant", "environment": "research"},
        asset_type="etf", frequency="1d", securities=("000001.XSHG", "000002.XSHE"),
        start_date="2026-01-05", end_date="2026-01-06", fields=FIELDS,
        price_semantics={"fq": None, "skip_paused": False})


def _rows(repo_root: Path):
    text = (repo_root / "tests/local_quant_research/fixtures/daily-bars.csv").read_text(encoding="utf-8")
    return list(csv.DictReader(io.StringIO(text)))


@pytest.mark.parametrize(("overrides", "message"), [({"status": "unknown"}, "status"), ({"split_ratio": "0"}, "split_ratio")])
def test_corporate_action_normalization_rejects_invalid_evidence(overrides, message):
    with pytest.raises(storage.MarketDataIntegrityError, match=message):
        storage._normalize_corporate_action_rows([_action(**overrides)], declared=_manifest())


def test_corporate_action_metadata_retains_late_announcement():
    rows = storage._normalize_corporate_action_rows([_action(announcement_date="2026-07-04")], declared=_manifest())
    assert rows[0]["effective_date"] == "2026-07-03"
    assert rows[0]["announcement_date"] == "2026-07-04"


def test_normalized_digest_ignores_csv_line_endings(repo_root: Path):
    rows = _rows(repo_root)
    text = io.StringIO()
    writer = csv.DictWriter(text, fieldnames=FIELDS, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    lf = list(csv.DictReader(io.StringIO(text.getvalue())))
    crlf = list(csv.DictReader(io.StringIO(text.getvalue().replace("\n", "\r\n"))))
    assert normalized_digest(normalize_market_rows(lf)) == normalized_digest(normalize_market_rows(crlf))


@pytest.mark.parametrize("invalid_id", ["..\\..\\outside", "../../outside", "g" * 64, "a" * 63])
def test_storage_identifier_rule_requires_strict_sha256(invalid_id):
    with pytest.raises(storage.MarketDataIntegrityError, match="identifier"):
        storage._require_identifier(invalid_id, "batch")


def test_selection_rejects_incomplete_end_date_coverage(repo_root: Path, monkeypatch: pytest.MonkeyPatch):
    rows = normalize_market_rows(_rows(repo_root))
    monkeypatch.setattr(storage, "_read_parquet", lambda _path: rows)
    with pytest.raises(storage.MarketDataIntegrityError, match="end_date coverage"):
        storage._selection_rows(manifests=[("a" * 64, _manifest())],
            selection=replace(_selection(), end_date="2026-12-31"), root=repo_root)


def test_market_rows_reject_missing_or_extra_columns(repo_root: Path):
    row = _rows(repo_root)[0]
    missing = dict(row)
    missing.pop(FIELDS[-1])
    for malformed in (missing, {**row, "unexpected": "extra"}):
        with pytest.raises(MarketDataContractError, match="fixed field contract"):
            normalize_market_rows([malformed])


def test_selection_requires_complete_source_identity():
    with pytest.raises(storage.MarketDataIntegrityError, match="source"):
        storage._selection_rows(manifests=[("a" * 64, _manifest())],
            selection=replace(_selection(), source={"name": "joinquant", "environment": "different"}),
            root=Path("unused-market-root"))


def test_records_expose_deeply_immutable_evidence():
    batch = BatchRecord(batch_id="a" * 64, path=Path("unused-batch"), manifest=_manifest())
    snapshot = SnapshotRecord(snapshot_id="b" * 64, path=Path("unused-snapshot"),
                              document={"selection": _selection().to_document()})
    with pytest.raises(TypeError):
        batch.manifest["source"]["environment"] = "mutated"
    with pytest.raises(TypeError):
        snapshot.document["selection"]["source"]["environment"] = "mutated"
