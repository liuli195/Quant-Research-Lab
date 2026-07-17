from __future__ import annotations

import hashlib
from pathlib import Path
from types import MappingProxyType

import numpy as np
import pyarrow as pa
import pytest

from scripts.research.local_quant_research.contracts import (
    ExecutionBundle,
    ExecutionRun,
    OutputSpec,
    ResultExtension,
)
from scripts.research.local_quant_research.evidence import (
    EvidenceError,
    collect_output_evidence,
    compute_run_id,
    execution_digest,
)
from scripts.research.local_quant_research import evidence


def test_run_id_is_stable_and_binds_all_three_input_digests() -> None:
    snapshot = "1" * 64
    config = "2" * 64
    code = "3" * 64

    first = compute_run_id(snapshot, config, code)

    assert first == compute_run_id(snapshot, config, code)
    assert first != compute_run_id("4" * 64, config, code)
    assert first != compute_run_id(snapshot, "4" * 64, code)
    assert first != compute_run_id(snapshot, config, "4" * 64)
    assert len(first) == 64


@pytest.mark.parametrize(
    "content",
    [
        'a,b\n"unterminated\n',
        "a,b\n1,2,3\n",
    ],
    ids=["unterminated-quote", "wrong-column-count"],
)
def test_csv_evidence_rejects_malformed_data_rows(
    content: str,
    tmp_path: Path,
) -> None:
    output = tmp_path / "result.csv"
    output.write_text(content, encoding="utf-8")

    with pytest.raises(EvidenceError, match="CSV"):
        collect_output_evidence(
            tmp_path,
            (OutputSpec(path="result.csv", format="csv"),),
        )


def test_directory_evidence_binds_dynamic_result_files(tmp_path: Path) -> None:
    package = tmp_path / "backtests" / "local-baseline"
    (package / "data").mkdir(parents=True)
    manifest = package / "manifest.json"
    attribution = package / "data" / f"attribution_log-{'a' * 64}.parquet"
    manifest.write_text('{"status":"complete"}\n', encoding="utf-8")
    attribution.write_bytes(b"dynamic")

    evidence = collect_output_evidence(
        tmp_path,
        (OutputSpec(path="backtests/local-baseline", format="directory"),),
    )

    assert evidence[0]["path"] == "backtests/local-baseline"
    assert evidence[0]["format"] == "directory"
    assert evidence[0]["files"] == [
        {
            "path": "data/attribution_log-" + "a" * 64 + ".parquet",
            "bytes": attribution.stat().st_size,
            "sha256": hashlib.sha256(attribution.read_bytes()).hexdigest(),
        },
        {
            "path": "manifest.json",
            "bytes": manifest.stat().st_size,
            "sha256": hashlib.sha256(manifest.read_bytes()).hexdigest(),
        },
    ]
    assert len(evidence[0]["sha256"]) == 64


def test_execution_digest_scans_shared_run_and_arrays_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class CountingLedger:
        def __init__(self) -> None:
            self.calls: dict[str, int] = {}
            self.shared = np.array([(1,)], dtype=[("value", "i8")])

        def _value(self, name: str) -> np.ndarray:
            self.calls[name] = self.calls.get(name, 0) + 1
            return self.shared

        orders = property(lambda self: self._value("orders"))
        assets = property(lambda self: self._value("assets"))
        cash = property(lambda self: self._value("cash"))
        value = property(lambda self: self._value("value"))
        trades = property(lambda self: self._value("trades"))
        positions = property(lambda self: self._value("positions"))
        returns = property(lambda self: self._value("returns"))

    ledger = CountingLedger()
    run = ExecutionRun(
        ledger=ledger,
        trace=MappingProxyType({"trace": np.array([1.0, np.nan])}),
    )
    execution = ExecutionBundle(run, run, ("primary",))
    monkeypatch.setattr(
        "scripts.research.local_quant_research.evidence.np.ascontiguousarray",
        lambda _value: pytest.fail("digest must not create a full contiguous copy"),
    )

    digest = execution_digest(execution)

    assert len(digest) == 64
    assert ledger.calls == {
        name: 1
        for name in (
            "orders",
            "assets",
            "cash",
            "value",
            "trades",
            "positions",
            "returns",
        )
    }


def test_execution_digest_streams_arrow_extension_without_to_pylist() -> None:
    class StreamingTable:
        def __init__(self) -> None:
            self._table = pa.table(
                {"value": pa.array([1.0, None, float("nan")])}
            )
            self.schema = self._table.schema
            self.num_rows = self._table.num_rows

        def to_pylist(self) -> list[object]:
            pytest.fail("extension digest must not Python-materialize all rows")

        def slice(self, offset: int, length: int) -> pa.Table:
            return self._table.slice(offset, length)

    empty = np.empty(0, dtype=[("value", "i8")])

    class Ledger:
        orders = assets = cash = value = trades = positions = returns = empty

    run = ExecutionRun(ledger=Ledger(), trace={})
    extension = ResultExtension(
        name="streamed",
        schema_version="streamed/1",
        table=StreamingTable(),  # type: ignore[arg-type]
        unique_key=("value",),
        evidence={},
    )

    assert len(execution_digest(ExecutionBundle(run, run, ("primary",)), (extension,))) == 64


def test_arrow_digest_distinguishes_equal_length_slices_with_different_values() -> None:
    base = pa.table({"value": pa.array([1, 2, 3], type=pa.int64())})

    first = evidence._arrow_table_digest(base.slice(0, 2))
    second = evidence._arrow_table_digest(base.slice(1, 2))

    assert first["sha256"] != second["sha256"]


def test_arrow_digest_binds_dictionary_values() -> None:
    indices = pa.array([0, 1, 0], type=pa.int8())
    first = pa.table(
        {
            "value": pa.DictionaryArray.from_arrays(
                indices,
                pa.array(["a", "b"]),
            )
        }
    )
    second = pa.table(
        {
            "value": pa.DictionaryArray.from_arrays(
                indices,
                pa.array(["x", "y"]),
            )
        }
    )

    assert evidence._arrow_table_digest(first)["sha256"] != evidence._arrow_table_digest(second)["sha256"]


def test_arrow_digest_is_chunk_independent_for_nested_null_and_nan_values() -> None:
    nested_type = pa.struct(
        [
            pa.field("items", pa.list_(pa.float64())),
            pa.field("label", pa.string()),
        ]
    )
    values = [
        {"items": [1.0, None, float("nan")], "label": "a"},
        None,
        {"items": [], "label": "b"},
    ]
    single = pa.table({"nested": pa.array(values, type=nested_type)})
    chunked = pa.Table.from_arrays(
        [
            pa.chunked_array(
                [
                    pa.array(values[:1], type=nested_type),
                    pa.array(values[1:], type=nested_type),
                ]
            )
        ],
        names=["nested"],
    )

    assert evidence._arrow_table_digest(single)["sha256"] == evidence._arrow_table_digest(chunked)["sha256"]


def test_arrow_digest_binds_field_order_and_null_nan_distinction() -> None:
    first = pa.table(
        {
            "left": pa.array([None], type=pa.float64()),
            "right": pa.array([float("nan")], type=pa.float64()),
        }
    )
    reordered = pa.table(
        {
            "right": pa.array([float("nan")], type=pa.float64()),
            "left": pa.array([None], type=pa.float64()),
        }
    )
    swapped = pa.table(
        {
            "left": pa.array([float("nan")], type=pa.float64()),
            "right": pa.array([None], type=pa.float64()),
        }
    )

    first_digest = evidence._arrow_table_digest(first)["sha256"]
    assert first_digest != evidence._arrow_table_digest(reordered)["sha256"]
    assert first_digest != evidence._arrow_table_digest(swapped)["sha256"]
