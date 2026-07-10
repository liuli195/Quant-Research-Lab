from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, urlsplit


STATES = {
    "complete",
    "capped_free",
    "missing_at_source",
    "unsupported_api_version",
    "failed",
}


class TargetRequired(ValueError):
    """Raised when a history sync does not name one exact page target."""


@dataclass(frozen=True)
class DatasetPolicy:
    required: bool = True


def validate_history_target(
    strategy_id: str | None, target: str | None
) -> tuple[str, str]:
    strategy = (strategy_id or "").strip()
    selected = (target or "").strip()
    if not strategy or not selected or selected.lower() in {"latest", "all"}:
        raise TargetRequired("explicit strategy and page target required")
    if re.fullmatch(r"[1-9]\d*", selected):
        return strategy, selected
    parsed = urlsplit(selected)
    query = parse_qs(parsed.query)
    if (
        parsed.scheme == "https"
        and parsed.hostname in {"joinquant.com", "www.joinquant.com"}
        and parsed.path == "/algorithm/backtest/detail"
        and query.get("backtestId", [""])[0]
    ):
        return strategy, selected
    raise TargetRequired("target must be a page ordinal or JoinQuant detail URL")


def resolve_local_id(
    index_path: Path, kind: str, page_identity: dict[str, str]
) -> str:
    if kind not in {"strategy", "simulation", "build", "backtest"}:
        raise ValueError(f"unsupported object kind: {kind}")
    ordinal = str(page_identity.get("page_ordinal") or "").strip()
    if not re.fullmatch(r"[1-9]\d*", ordinal):
        raise ValueError("page_ordinal must be a positive integer")
    stable_identity = {"page_ordinal": ordinal}
    if page_identity.get("strategy_id"):
        stable_identity["strategy_id"] = str(page_identity["strategy_id"])

    data = (
        json.loads(index_path.read_text(encoding="utf-8"))
        if index_path.is_file()
        else {"schema_version": 1, "objects": []}
    )
    objects = data.setdefault("objects", [])
    item = next(
        (
            candidate
            for candidate in objects
            if candidate.get("kind") == kind
            and candidate.get("identity") == stable_identity
        ),
        None,
    )
    if item is None:
        if kind in {"build", "backtest"}:
            local_id = ordinal
        else:
            numbers = [
                int(match.group(1))
                for candidate in objects
                if candidate.get("kind") == kind
                and (
                    match := re.fullmatch(
                        rf"{re.escape(kind)}-(\d+)",
                        str(candidate.get("local_id") or ""),
                    )
                )
            ]
            local_id = f"{kind}-{max(numbers, default=0) + 1:03d}"
        item = {
            "kind": kind,
            "local_id": local_id,
            "identity": stable_identity,
            "aliases": [],
        }
        objects.append(item)

    alias = {
        key: str(page_identity[key])
        for key in ("remote_id", "url", "name")
        if page_identity.get(key)
    }
    if alias and alias not in item["aliases"]:
        item["aliases"].append(alias)
    index_path.parent.mkdir(parents=True, exist_ok=True)
    index_path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return str(item["local_id"])


def expected_datasets(
    kind: str, run_status: str, has_attribution_writer: bool
) -> dict[str, dict[str, object]]:
    if kind not in {"backtest", "simulation"}:
        raise ValueError(f"unsupported run kind: {kind}")
    policies = {
        name: DatasetPolicy()
        for name in (
            "results",
            "balances",
            "positions",
            "orders",
            "records",
            "risk",
            "period_risks",
            "official_summary",
        )
    }
    policies.update(
        normal_log=DatasetPolicy(required=False),
        performance_profile=DatasetPolicy(required=False),
        error_log=DatasetPolicy(required=run_status in {"failed", "cancelled"}),
        attribution_log=DatasetPolicy(required=has_attribution_writer),
    )
    datasets = {
        name: {"required": policy.required, "status": "complete"}
        for name, policy in policies.items()
    }
    if run_status in {"failed", "cancelled"}:
        for name in (
            "results",
            "balances",
            "positions",
            "orders",
            "records",
            "risk",
            "period_risks",
        ):
            datasets[name].update(rows=0, verified_empty=True)
    if not has_attribution_writer:
        datasets["attribution_log"].update(
            status="missing_at_source", evidence={"code_writer": False}
        )
    if not datasets["error_log"]["required"]:
        datasets["error_log"].update(rows=0, verified_empty=True)
    return datasets


def evaluate_gate(
    datasets: dict[str, dict[str, object]],
) -> dict[str, object]:
    failed = False
    exceptions: list[str] = []
    for name, item in datasets.items():
        status = item.get("status")
        required = bool(item.get("required"))
        if status not in STATES or status == "failed":
            failed = True
            continue
        if status == "complete":
            if item.get("rows") == 0 and not item.get("verified_empty"):
                failed = True
            continue
        accepted = False
        if status == "capped_free":
            accepted = name == "normal_log" and bool(item.get("pagination"))
        elif status in {"missing_at_source", "unsupported_api_version"}:
            accepted = not required and bool(item.get("evidence"))
        if required or not accepted:
            failed = True
        else:
            exceptions.append(f"{name}:{status}")
    return {"status": "fail" if failed else "pass", "exceptions": exceptions}


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
