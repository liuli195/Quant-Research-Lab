from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Literal, Mapping


RunStatus = Literal["complete", "evidence_insufficient", "failed"]


@dataclass(frozen=True)
class OutputSpec:
    path: str
    format: Literal["json", "csv", "markdown", "text", "parquet"]


@dataclass(frozen=True)
class RunConfig:
    project_id: str
    snapshot_id: str
    snapshot_requirements: Mapping[str, object]
    project_entry: Path
    command: tuple[str, ...]
    project_config: Path
    code_identity: Path
    benchmark_input: Path | None
    declared_inputs: tuple[Path, ...]
    required_outputs: tuple[OutputSpec, ...]
    output_root: Path
    stop_states: tuple[RunStatus, ...]
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
        }
