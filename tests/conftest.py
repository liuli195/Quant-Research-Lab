from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
SKILL_SCRIPTS = REPO_ROOT / ".agents" / "skills" / "joinquant-archive-sync" / "scripts"
sys.path.insert(0, str(SKILL_SCRIPTS))
DOCS_SCRIPTS = REPO_ROOT / ".agents" / "skills" / "joinquant-docs-sync" / "scripts"
sys.path.insert(0, str(DOCS_SCRIPTS))
STANDARD_ANALYSIS_SKILL_SCRIPTS = (
    REPO_ROOT / ".agents" / "skills" / "analyze-quant-robustness" / "scripts"
)
sys.path.insert(0, str(STANDARD_ANALYSIS_SKILL_SCRIPTS))


@pytest.fixture
def repo_root() -> Path:
    return REPO_ROOT


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers", "real: requires real file, native database, process, or browser behavior"
    )


@pytest.fixture(scope="session")
def real_temp_directory(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.getbasetemp() / "native-temp"
    root.mkdir()
    return root


@pytest.fixture(autouse=True)
def isolate_real_temporary_files(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    if request.node.get_closest_marker("real") is None:
        return
    root = str(request.getfixturevalue("real_temp_directory"))
    for name in ("TEMP", "TMP", "TMPDIR"):
        monkeypatch.setenv(name, root)
    monkeypatch.setattr(tempfile, "tempdir", root)
