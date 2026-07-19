from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Iterable, Literal, Mapping

import pyarrow as pa
import pyarrow.parquet as pq


ScenarioStatus = Literal["pass", "fail", "evidence_insufficient"]
_SHA256 = re.compile(r"[0-9a-f]{64}")
_AUTHORITY = "local_exploratory"
_FORMULA_VERSION = "quant-analysis-v1"
_SCHEMA = pa.schema(
    [
        pa.field("scenario_id", pa.string(), nullable=False),
        pa.field("dimension", pa.string(), nullable=False),
        pa.field("status", pa.string(), nullable=False),
        pa.field("authority", pa.string(), nullable=False),
        pa.field("formula_version", pa.string(), nullable=False),
        pa.field("input_sha256", pa.string(), nullable=False),
        pa.field("metrics_json", pa.string(), nullable=False),
        pa.field("reasons_json", pa.string(), nullable=False),
    ]
)


def canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def evidence_digest(value: object) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


@dataclass(frozen=True)
class ScenarioResult:
    scenario_id: str
    dimension: str
    status: ScenarioStatus
    metrics: Mapping[str, float | int | None]
    input_sha256: str
    reasons: tuple[str, ...] = ()
    authority: str = _AUTHORITY
    formula_version: str = _FORMULA_VERSION

    def __post_init__(self) -> None:
        if not self.scenario_id or not self.dimension:
            raise ValueError("scenario identity must be non-empty")
        if self.status not in {"pass", "fail", "evidence_insufficient"}:
            raise ValueError("scenario status is invalid")
        if _SHA256.fullmatch(self.input_sha256) is None:
            raise ValueError("scenario input_sha256 is invalid")
        if self.authority != _AUTHORITY or self.formula_version != _FORMULA_VERSION:
            raise ValueError("scenario authority or formula version is invalid")
        normalized: dict[str, float | int | None] = {}
        for key, value in self.metrics.items():
            if not key:
                raise ValueError("scenario metric name must be non-empty")
            if isinstance(value, float) and not math.isfinite(value):
                raise ValueError("scenario metrics must be finite")
            if value is not None and not isinstance(value, (int, float)):
                raise ValueError("scenario metrics must be numeric or null")
            normalized[str(key)] = value
        reasons = tuple(dict.fromkeys(str(reason) for reason in self.reasons))
        if self.status == "evidence_insufficient" and not reasons:
            raise ValueError("evidence-insufficient scenario requires a reason")
        object.__setattr__(self, "metrics", MappingProxyType(normalized))
        object.__setattr__(self, "reasons", reasons)

    def to_document(self) -> dict[str, object]:
        return {
            "scenario_id": self.scenario_id,
            "dimension": self.dimension,
            "status": self.status,
            "authority": self.authority,
            "formula_version": self.formula_version,
            "input_sha256": self.input_sha256,
            "metrics": dict(self.metrics),
            "reasons": list(self.reasons),
        }


def _rows(results: Iterable[ScenarioResult]) -> list[dict[str, object]]:
    ordered = sorted(results, key=lambda row: row.scenario_id)
    if len({row.scenario_id for row in ordered}) != len(ordered):
        raise ValueError("scenario IDs must be unique")
    return [
        {
            "scenario_id": row.scenario_id,
            "dimension": row.dimension,
            "status": row.status,
            "authority": row.authority,
            "formula_version": row.formula_version,
            "input_sha256": row.input_sha256,
            "metrics_json": canonical_bytes(dict(row.metrics)).decode("utf-8"),
            "reasons_json": canonical_bytes(list(row.reasons)).decode("utf-8"),
        }
        for row in ordered
    ]


def _metadata(rows: list[dict[str, object]]) -> dict[bytes, bytes]:
    return {
        b"schema_version": b"1",
        b"table_name": b"local-evidence-matrix",
        b"primary_key": b'["scenario_id"]',
        b"authority": _AUTHORITY.encode("ascii"),
        b"formula_version": _FORMULA_VERSION.encode("ascii"),
        b"content_sha256": evidence_digest(rows).encode("ascii"),
    }


def build_evidence_matrix(
    results: Iterable[ScenarioResult],
    output: Path,
) -> Path:
    rows = _rows(results)
    if not rows:
        raise ValueError("evidence matrix must contain scenarios")
    table = pa.Table.from_pylist(rows, schema=_SCHEMA)
    table = table.replace_schema_metadata(_metadata(rows))
    target = Path(output)
    target.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(
        table,
        target,
        compression="zstd",
        use_dictionary=False,
        write_statistics=True,
    )
    return target
