from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping, Sequence


def _immutable_mapping(value: Mapping[str, object]) -> Mapping[str, object]:
    return MappingProxyType(dict(value))


@dataclass(frozen=True)
class BatchRecord:
    batch_id: str
    path: Path
    manifest: Mapping[str, Any]


@dataclass(frozen=True)
class SnapshotSelection:
    source: str
    asset_type: str
    frequency: str
    securities: Sequence[str]
    start_date: str
    end_date: str
    fields: Sequence[str]
    price_semantics: Mapping[str, object]

    def __post_init__(self) -> None:
        object.__setattr__(self, "securities", tuple(self.securities))
        object.__setattr__(self, "fields", tuple(self.fields))
        object.__setattr__(
            self,
            "price_semantics",
            _immutable_mapping(self.price_semantics),
        )

    def to_document(self) -> dict[str, object]:
        return {
            "source": self.source,
            "asset_type": self.asset_type,
            "frequency": self.frequency,
            "securities": sorted(self.securities),
            "start_date": self.start_date,
            "end_date": self.end_date,
            "fields": list(self.fields),
            "price_semantics": dict(self.price_semantics),
        }


@dataclass(frozen=True)
class SnapshotRecord:
    snapshot_id: str
    path: Path
    document: Mapping[str, Any]
