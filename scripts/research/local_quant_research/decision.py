from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import uuid
from pathlib import Path
from typing import Mapping


_SHA256 = re.compile(r"[0-9a-f]{64}")
_DECISIONS = {
    "proceed_to_joinquant",
    "revise_and_reassess",
    "stop_evidence_insufficient",
}
_REQUIRED = {
    "decision",
    "candidate_focus",
    "baseline_action",
    "reason",
    "confirmed_by",
    "confirmed_at",
}


class DecisionError(ValueError):
    """Raised when a human decision does not match immutable research evidence."""


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _file_digest(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as exc:
        raise DecisionError("research evidence is missing") from exc


def _recommendation(run_dir: Path) -> tuple[dict[str, object], str]:
    path = Path(run_dir) / "recommendation.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DecisionError("recommendation evidence is invalid") from exc
    if not isinstance(value, dict):
        raise DecisionError("recommendation evidence is invalid")
    identity = value.get("identity")
    run_id = identity.get("run_id") if isinstance(identity, Mapping) else None
    if not isinstance(run_id, str) or _SHA256.fullmatch(run_id) is None:
        raise DecisionError("recommendation run identity is invalid")
    return value, run_id


def _document(run_dir: Path, decision: Mapping[str, object]) -> dict[str, object]:
    if set(decision) != _REQUIRED:
        raise DecisionError("human decision fields are incomplete or unknown")
    if decision["decision"] not in _DECISIONS:
        raise DecisionError("human decision value is invalid")
    if not isinstance(decision["candidate_focus"], list) or any(
        not isinstance(item, str) or not item for item in decision["candidate_focus"]
    ):
        raise DecisionError("candidate_focus must be a string array")
    for field in ("baseline_action", "reason", "confirmed_by", "confirmed_at"):
        if not isinstance(decision[field], str) or not str(decision[field]).strip():
            raise DecisionError(f"{field} must be non-empty")
    _, run_id = _recommendation(run_dir)
    payload = {
        "schema_version": 1,
        "project_id": "strategy-003",
        "run_id": run_id,
        "report_sha256": _file_digest(Path(run_dir) / "local-research-report.md"),
        "recommendation_sha256": _file_digest(
            Path(run_dir) / "recommendation.json"
        ),
        **dict(decision),
    }
    payload["decision_id"] = _digest(payload)
    payload["document_sha256"] = _digest(payload)
    return payload


def record_human_decision(
    *,
    run_dir: Path,
    decision_root: Path,
    decision: Mapping[str, object],
) -> Path:
    document = _document(Path(run_dir), decision)
    target = (
        Path(decision_root)
        / "strategy-003"
        / str(document["run_id"])
        / str(document["decision_id"])
    )
    output = target / "human-decision.json"
    if output.exists():
        if validate_human_decision(run_dir=run_dir, decision_path=output) != document:
            raise DecisionError("existing human decision conflicts with requested decision")
        return output
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.parent / f".{target.name}.tmp-{uuid.uuid4().hex}"
    temporary.mkdir()
    try:
        (temporary / "human-decision.json").write_text(
            json.dumps(document, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, target)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return output


def validate_human_decision(
    *,
    run_dir: Path,
    decision_path: Path,
) -> dict[str, object]:
    try:
        document = json.loads(Path(decision_path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DecisionError("human decision is invalid") from exc
    if not isinstance(document, dict):
        raise DecisionError("human decision is invalid")
    semantic = {key: value for key, value in document.items() if key != "document_sha256"}
    if document.get("document_sha256") != _digest(semantic):
        raise DecisionError("human decision document digest mismatch")
    _, run_id = _recommendation(Path(run_dir))
    if document.get("run_id") != run_id:
        raise DecisionError("human decision run identity mismatch")
    if document.get("report_sha256") != _file_digest(
        Path(run_dir) / "local-research-report.md"
    ) or document.get("recommendation_sha256") != _file_digest(
        Path(run_dir) / "recommendation.json"
    ):
        raise DecisionError("human decision evidence digest mismatch")
    expected_parent = (
        Path(decision_path).parents[2]
        / run_id
        / str(document.get("decision_id"))
    )
    if Path(decision_path).parent.resolve() != expected_parent.resolve():
        raise DecisionError("human decision path does not match its identity")
    return document
