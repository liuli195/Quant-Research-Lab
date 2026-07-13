from __future__ import annotations

from pathlib import Path

import pytest

from scripts.research.local_quant_research.contracts import OutputSpec
from scripts.research.local_quant_research.evidence import (
    EvidenceError,
    collect_output_evidence,
    compute_run_id,
)


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
