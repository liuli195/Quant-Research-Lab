from __future__ import annotations

from dataclasses import dataclass
import re
from types import MappingProxyType
from typing import Mapping

import numpy as np
import pyarrow as pa
import pyarrow.compute as pc


SCENARIO_ID_PATTERN = r"^[a-z0-9][a-z0-9-]{0,63}$"


class EvidenceError(RuntimeError):
    """Raised when immutable result evidence is invalid."""


def is_valid_scenario_id(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(SCENARIO_ID_PATTERN, value) is not None


def validate_extension_table(table: object) -> None:
    if not isinstance(table, pa.Table):
        raise EvidenceError("extension table must be an Arrow table")
    try:
        table.validate(full=True)
    except pa.ArrowException as exc:
        raise EvidenceError("extension table is invalid") from exc
    for field in table.schema:
        if field.type not in (pa.string(), pa.bool_(), pa.int64(), pa.float64()):
            raise EvidenceError(
                "extension fields must use flat string/bool/int64/float64 types"
            )
        if pa.types.is_float64(field.type) and bool(
            pc.any(pc.is_nan(table[field.name])).as_py()
        ):
            raise EvidenceError(
                "extension float values must use Arrow null instead of NaN"
            )


@dataclass(frozen=True, slots=True)
class ExecutionRun:
    ledger: object
    trace: Mapping[str, np.ndarray]

    def __post_init__(self) -> None:
        object.__setattr__(self, "trace", MappingProxyType(dict(self.trace)))


@dataclass(frozen=True, slots=True)
class ExecutionBundle:
    primary: ExecutionRun
    final: ExecutionRun
    stages: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ResultExtension:
    name: str
    schema_version: str
    table: pa.Table
    unique_key: tuple[str, ...]
    evidence: Mapping[str, object]

    def __post_init__(self) -> None:
        object.__setattr__(self, "evidence", MappingProxyType(dict(self.evidence)))
