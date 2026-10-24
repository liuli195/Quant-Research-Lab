from __future__ import annotations

import os
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
    if config.getoption("markexpr") == "not real":
        import socket

        # Ordinary calculations do not verify optional CCXT/IPv6 host capabilities.
        optional_host = pytest.MonkeyPatch()
        config.add_cleanup(optional_host.undo)
        optional_host.setitem(sys.modules, "ccxt", None)
        optional_host.setattr(socket, "has_ipv6", False)

    # Cache only a known path: gettempdir() would otherwise probe with real writes.
    temp_root = Path(os.environ.get("TMPDIR") or REPO_ROOT).resolve()
    if not temp_root.is_relative_to(REPO_ROOT) or not temp_root.is_dir():
        temp_root = REPO_ROOT
    tempfile.tempdir = str(temp_root)

    basetemp = config.getoption("basetemp")
    if config.getoption("markexpr") == "real" and basetemp:
        parent = Path(basetemp).resolve().parent
        if not parent.is_relative_to(REPO_ROOT):
            raise pytest.UsageError("real check basetemp must be inside the repository")
        parent.mkdir(parents=True, exist_ok=True)


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
