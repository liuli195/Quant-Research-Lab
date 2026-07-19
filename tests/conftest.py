from __future__ import annotations

import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
SKILL_SCRIPTS = REPO_ROOT / ".agents" / "skills" / "joinquant-archive-sync" / "scripts"
sys.path.insert(0, str(SKILL_SCRIPTS))
STANDARD_ANALYSIS_SKILL_SCRIPTS = (
    REPO_ROOT / ".agents" / "skills" / "analyze-quant-robustness" / "scripts"
)
sys.path.insert(0, str(STANDARD_ANALYSIS_SKILL_SCRIPTS))


@pytest.fixture
def repo_root() -> Path:
    return REPO_ROOT
