from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping, Sequence


def _deep_freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType(
            {str(key): _deep_freeze(item) for key, item in value.items()}
        )
    if isinstance(value, (list, tuple)):
        return tuple(_deep_freeze(item) for item in value)
    return value


@dataclass(frozen=True)
class BatchRecord:
    batch_id: str
    path: Path
    manifest: Mapping[str, Any]

    def __post_init__(self) -> None:
        object.__setattr__(self, "manifest", _deep_freeze(self.manifest))


@dataclass(frozen=True)
class SnapshotSelection:
    source: Mapping[str, object]
    asset_type: str
    frequency: str
    securities: Sequence[str]
    start_date: str
    end_date: str
    fields: Sequence[str]
    price_semantics: Mapping[str, object]

    def __post_init__(self) -> None:
        object.__setattr__(self, "source", _deep_freeze(self.source))
        object.__setattr__(self, "securities", tuple(self.securities))
        object.__setattr__(self, "fields", tuple(self.fields))
        object.__setattr__(
            self,
            "price_semantics",
            _deep_freeze(self.price_semantics),
        )

    def to_document(self) -> dict[str, object]:
        return {
            "source": dict(self.source),
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

    def __post_init__(self) -> None:
        object.__setattr__(self, "document", _deep_freeze(self.document))
