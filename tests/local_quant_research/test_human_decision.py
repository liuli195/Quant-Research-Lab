from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from scripts.research.local_quant_research.decision import (
    DecisionError,
    record_human_decision,
    validate_human_decision,
)


def _run(tmp_path: Path) -> tuple[Path, str]:
    run_id = "a" * 64
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "local-research-report.md").write_text("report\n", encoding="utf-8")
    (run_dir / "recommendation.json").write_text(
        json.dumps({"identity": {"run_id": run_id}, "recommendation": "revise_and_reassess"}),
        encoding="utf-8",
    )
    return run_dir, run_id


def test_human_decision_is_append_only_outside_immutable_run(tmp_path: Path) -> None:
    run_dir, run_id = _run(tmp_path)
    decision_root = tmp_path / "decisions"
    decision = {
        "decision": "revise_and_reassess",
        "candidate_focus": ["entry-40"],
        "baseline_action": "retain_frozen_baseline",
        "reason": "等待人工复核稳健性反证",
        "confirmed_by": "research-owner",
        "confirmed_at": "2026-07-14T12:00:00+08:00",
    }

    path = record_human_decision(
        run_dir=run_dir,
        decision_root=decision_root,
        decision=decision,
    )
    document = validate_human_decision(run_dir=run_dir, decision_path=path)

    assert path.parent.parent.name == run_id
    assert not path.is_relative_to(run_dir)
    assert document["decision"] == "revise_and_reassess"
    assert document["report_sha256"] == hashlib.sha256(
        (run_dir / "local-research-report.md").read_bytes()
    ).hexdigest()
    assert record_human_decision(
        run_dir=run_dir,
        decision_root=decision_root,
        decision=decision,
    ) == path


def test_human_decision_rejects_changed_report_or_recommendation(tmp_path: Path) -> None:
    run_dir, _ = _run(tmp_path)
    path = record_human_decision(
        run_dir=run_dir,
        decision_root=tmp_path / "decisions",
        decision={
            "decision": "stop_evidence_insufficient",
            "candidate_focus": [],
            "baseline_action": "retain_frozen_baseline",
            "reason": "证据不足",
            "confirmed_by": "research-owner",
            "confirmed_at": "2026-07-14T12:00:00+08:00",
        },
    )
    (run_dir / "local-research-report.md").write_text("changed\n", encoding="utf-8")

    with pytest.raises(DecisionError, match="digest"):
        validate_human_decision(run_dir=run_dir, decision_path=path)
