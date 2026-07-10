from __future__ import annotations

import hashlib
from pathlib import Path


def stage_external_file(source: Path, stage_dir: Path) -> dict[str, object]:
    if not source.is_file():
        raise FileNotFoundError(source)

    stage_dir.mkdir(parents=True, exist_ok=True)
    destination = stage_dir / source.name
    digest = hashlib.sha256()
    with source.open("rb") as source_file, destination.open("wb") as target_file:
        while chunk := source_file.read(1024 * 1024):
            target_file.write(chunk)
            digest.update(chunk)

    return {
        "path": str(destination),
        "bytes": destination.stat().st_size,
        "sha256": digest.hexdigest(),
    }
