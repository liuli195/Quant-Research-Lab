from __future__ import annotations

from types import MappingProxyType

import numpy as np
import pyarrow as pa
import pytest

from scripts.research.local_quant_research.contracts import (
    ExecutionBundle,
    ExecutionRun,
)
from scripts.research.local_quant_research.evidence import (
    EvidenceError,
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


@pytest.mark.parametrize(
    "table",
    (
        pa.table({"value": pa.array([["nested"]])}),
        pa.table({"value": pa.array([float("nan")], type=pa.float64())}),
        pa.table({"value": pa.array([1], type=pa.int32())}),
        pa.table(
            {
                "value": pa.DictionaryArray.from_arrays(
                    pa.array([0], type=pa.int8()), pa.array(["value"])
                )
            }
        ),
        pa.table(
            {
                "value": pa.array(
                    [{"child": 1}],
                    type=pa.struct([("child", pa.int64())]),
                )
            }
        ),
        pa.table(
            {
                "value": pa.array(
                    [[("key", 1)]],
                    type=pa.map_(pa.string(), pa.int64()),
                )
            }
        ),
        pa.table(
            {
                "value": pa.UnionArray.from_sparse(
                    pa.array([5], type=pa.int8()),
                    [pa.array(["value"]), pa.array([1])],
                    field_names=["text", "number"],
                    type_codes=[5, 7],
                )
            }
        ),
        pa.table(
            {
                "value": pa.RunEndEncodedArray.from_arrays(
                    pa.array([1], type=pa.int16()),
                    pa.array(["value"]),
                )
            }
        ),
    ),
)
def test_extension_table_rejects_non_flat_or_nan_values(table: pa.Table) -> None:
    with pytest.raises(EvidenceError, match="extension"):
        evidence.validate_extension_table(table)


def test_extension_table_accepts_flat_values_and_arrow_nulls() -> None:
    table = pa.table(
        {
            "text": pa.array(["value", None], type=pa.string()),
            "flag": pa.array([True, None], type=pa.bool_()),
            "count": pa.array([1, None], type=pa.int64()),
            "score": pa.array([1.0, None], type=pa.float64()),
        }
    )

    evidence.validate_extension_table(table)
