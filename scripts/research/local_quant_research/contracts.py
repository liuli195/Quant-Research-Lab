from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Literal, Mapping, NamedTuple, Protocol

import numpy as np
import pyarrow as pa

if TYPE_CHECKING:
    from scripts.research.market_data.query import SnapshotView


RunStatus = Literal["complete", "evidence_insufficient", "failed"]
RUN_OUTPUT_ROOT = Path(".local/quant-research")
RUN_STATUSES: tuple[RunStatus, ...] = (
    "complete",
    "evidence_insufficient",
    "failed",
)

SIDE_NONE = 0
SIDE_BUY = 1
SIDE_SELL = -1

FILL_IGNORED = 0
FILL_ACCEPTED = 1
FILL_REJECTED = 2


class StrategyEvidenceError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class SegmentView(NamedTuple):
    row: int
    group: int
    from_col: int
    to_col: int
    cash: float
    value: float
    positions: np.ndarray
    valuation_prices: np.ndarray


class FillEvent(NamedTuple):
    row: int
    column: int
    status: int
    side: int
    size: float
    price: float
    fees: float
    cash_after: float
    position_after: float


@dataclass(frozen=True, slots=True)
class StrategyDescriptor:
    strategy_id: str
    contract_version: str
    extension_names: tuple[str, ...]
    accounting: Mapping[str, object]

    def __post_init__(self) -> None:
        object.__setattr__(self, "accounting", MappingProxyType(dict(self.accounting)))


@dataclass(frozen=True, slots=True)
class LedgerInput:
    dates: np.ndarray
    symbols: tuple[str, ...]
    close: np.ndarray
    initial_cash: float
    group_ids: np.ndarray
    cash_sharing: bool
    frequency: str


@dataclass(frozen=True, slots=True)
class OrderBuffer:
    enabled: np.ndarray
    side: np.ndarray
    size: np.ndarray
    price: np.ndarray
    fixed_fees: np.ndarray
    size_granularity: np.ndarray
    allow_partial: np.ndarray
    priority: np.ndarray

    def __post_init__(self) -> None:
        arrays = (
            self.enabled,
            self.side,
            self.size,
            self.price,
            self.fixed_fees,
            self.size_granularity,
            self.allow_partial,
            self.priority,
        )
        try:
            lengths = {len(array) for array in arrays}
        except TypeError as exc:
            raise ValueError("OrderBuffer arrays must have the same length") from exc
        if len(lengths) != 1:
            raise ValueError("OrderBuffer arrays must have the same length")


@dataclass(frozen=True, slots=True)
class OrderProgram:
    program_id: str
    prepare_segment_nb: object
    after_fill_nb: object
    after_segment_nb: object | None
    inputs: tuple[object, ...]
    params: tuple[object, ...]
    state: tuple[object, ...]
    trace: Mapping[str, np.ndarray]
    orders: OrderBuffer

    def __post_init__(self) -> None:
        object.__setattr__(self, "trace", MappingProxyType(dict(self.trace)))


@dataclass(frozen=True, slots=True)
class PreparedStrategy:
    ledger_input: LedgerInput
    primary_program: OrderProgram
    context: object


class ExecutionLedger(Protocol):
    @property
    def orders(self) -> np.ndarray: ...

    @property
    def assets(self) -> np.ndarray: ...

    @property
    def cash(self) -> np.ndarray: ...

    @property
    def value(self) -> np.ndarray: ...

    @property
    def trades(self) -> np.ndarray: ...

    @property
    def positions(self) -> np.ndarray: ...

    @property
    def returns(self) -> np.ndarray: ...


@dataclass(frozen=True, slots=True)
class ExecutionRun:
    ledger: ExecutionLedger
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


class StrategyModule(Protocol):
    descriptor: StrategyDescriptor

    def prepare(
        self,
        snapshot: "SnapshotView",
        config: Mapping[str, object],
    ) -> PreparedStrategy: ...

    def followup_program(
        self,
        prepared: PreparedStrategy,
        primary_run: ExecutionRun,
    ) -> OrderProgram | None: ...

    def build_extensions(
        self,
        prepared: PreparedStrategy,
        execution: ExecutionBundle,
    ) -> tuple[ResultExtension, ...]: ...


@dataclass(frozen=True)
class OutputSpec:
    path: str
    format: Literal["json", "csv", "markdown", "text", "parquet", "directory"]


@dataclass(frozen=True, slots=True)
class RunConfig:
    project_id: str
    strategy_root: Path
    strategy_module: str
    strategy_symbol: str
    snapshot_id: str
    snapshot_requirements: Mapping[str, object]
    scenario_config: Path
    declared_inputs: tuple[Path, ...]
    document: Mapping[str, object]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "snapshot_requirements",
            MappingProxyType(dict(self.snapshot_requirements)),
        )
        object.__setattr__(self, "document", MappingProxyType(dict(self.document)))


@dataclass(frozen=True)
class StageRecord:
    name: str
    status: str

    def to_document(self) -> dict[str, str]:
        return {"name": self.name, "status": self.status}


@dataclass(frozen=True)
class RunResult:
    status: RunStatus
    project_id: str
    run_id: str | None
    run_path: Path | None
    attempt_id: str | None
    reused: bool
    reasons: tuple[str, ...]
    stages: tuple[StageRecord, ...]
    next_action: str | None = None

    def to_document(self) -> dict[str, object]:
        return {
            "status": self.status,
            "project_id": self.project_id,
            "run_id": self.run_id,
            "run_path": str(self.run_path) if self.run_path is not None else None,
            "attempt_id": self.attempt_id,
            "reused": self.reused,
            "reasons": list(self.reasons),
            "stages": [stage.to_document() for stage in self.stages],
            "next_action": self.next_action,
        }
