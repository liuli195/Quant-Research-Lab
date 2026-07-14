from __future__ import annotations

from pathlib import Path

import pyarrow.parquet as pq
import pytest

from scripts.research.quant_analysis.contracts import (
    STANDARD_TABLES,
    AnalysisContractError,
    validate_analysis_bundle,
    write_analysis_table,
)


def _write_bundle(
    output_dir: Path,
    rows: dict[str, list[dict[str, object]]],
) -> None:
    for name in STANDARD_TABLES:
        write_analysis_table(name, rows[name], output_dir)


def test_bundle_requires_all_eight_parquet_tables(
    tmp_path: Path,
    analysis_rows: dict[str, list[dict[str, object]]],
) -> None:
    write_analysis_table("equity", analysis_rows["equity"], tmp_path)

    with pytest.raises(AnalysisContractError, match="missing tables"):
        validate_analysis_bundle(tmp_path)


def test_bundle_roundtrip_records_schema_units_keys_and_digest(
    tmp_path: Path,
    analysis_rows: dict[str, list[dict[str, object]]],
) -> None:
    _write_bundle(tmp_path, analysis_rows)

    bundle = validate_analysis_bundle(tmp_path)

    assert tuple(bundle.tables) == STANDARD_TABLES
    assert bundle.rows("equity")[0]["currency"] == "CNY"
    metadata = pq.read_schema(tmp_path / "equity.parquet").metadata
    assert metadata is not None
    assert metadata[b"schema_version"] == b"1"
    assert metadata[b"table_name"] == b"equity"
    assert metadata[b"primary_key"] == b'["date","portfolio_id"]'
    assert metadata[b"content_sha256"]
    assert metadata[b"units"]


def test_bundle_rejects_duplicate_primary_key(
    tmp_path: Path,
    analysis_rows: dict[str, list[dict[str, object]]],
) -> None:
    analysis_rows["equity"].append(dict(analysis_rows["equity"][0]))

    with pytest.raises(AnalysisContractError, match="duplicate primary key"):
        write_analysis_table("equity", analysis_rows["equity"], tmp_path)


def test_bundle_rejects_cash_position_equity_mismatch(
    tmp_path: Path,
    analysis_rows: dict[str, list[dict[str, object]]],
) -> None:
    analysis_rows["equity"][1]["cash"] = 1.0
    _write_bundle(tmp_path, analysis_rows)

    with pytest.raises(AnalysisContractError, match="cash.*positions.*equity"):
        validate_analysis_bundle(tmp_path)


def test_bundle_requires_closed_round_trips(
    tmp_path: Path,
    analysis_rows: dict[str, list[dict[str, object]]],
) -> None:
    analysis_rows["trades"][0]["exit_date"] = None

    with pytest.raises(AnalysisContractError, match="exit_date"):
        write_analysis_table("trades", analysis_rows["trades"], tmp_path)


def test_bundle_rejects_missing_two_benchmark_date_alignment(
    tmp_path: Path,
    analysis_rows: dict[str, list[dict[str, object]]],
) -> None:
    analysis_rows["benchmarks"] = analysis_rows["benchmarks"][:-1]
    _write_bundle(tmp_path, analysis_rows)

    with pytest.raises(AnalysisContractError, match="benchmark dates"):
        validate_analysis_bundle(tmp_path)


def test_bundle_rejects_benchmark_total_return_index_drift(
    tmp_path: Path,
    analysis_rows: dict[str, list[dict[str, object]]],
) -> None:
    analysis_rows["benchmarks"][2]["total_return_index"] = 999.0
    _write_bundle(tmp_path, analysis_rows)

    with pytest.raises(AnalysisContractError, match="benchmark total return index"):
        validate_analysis_bundle(tmp_path)


def test_bundle_rejects_unit_metadata_tampering(
    tmp_path: Path,
    analysis_rows: dict[str, list[dict[str, object]]],
) -> None:
    _write_bundle(tmp_path, analysis_rows)
    path = tmp_path / "equity.parquet"
    table = pq.read_table(path)
    metadata = dict(table.schema.metadata or {})
    metadata[b"units"] = b"{}"
    pq.write_table(table.replace_schema_metadata(metadata), path)

    with pytest.raises(AnalysisContractError, match="units"):
        validate_analysis_bundle(tmp_path)
