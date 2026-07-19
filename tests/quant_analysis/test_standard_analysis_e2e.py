from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess

from tests.quant_analysis.test_unified_analysis import _standard_registry


def _tree_sha(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        digest.update(path.relative_to(root).as_posix().encode("utf-8"))
        digest.update(path.read_bytes())
    return digest.hexdigest()


def test_standard_analysis_skill_runs_only_the_read_only_registered_flow(
    repo_root: Path, tmp_path: Path
) -> None:
    root, registry = _standard_registry(repo_root, tmp_path, single_source=False)
    sources = {
        "local": root / "sources/local",
        "backtest": root / "sources/backtest",
        "simulation": root / "sources/simulation",
    }
    before = {name: _tree_sha(path) for name, path in sources.items()}
    skill = (
        repo_root / ".agents/skills/analyze-quant-robustness/SKILL.md"
    ).read_text(encoding="utf-8")
    assert "scripts\\analyze_quant_robustness.py" in skill
    network_guard = tmp_path / "network-guard"
    network_guard.mkdir()
    (network_guard / "sitecustomize.py").write_text(
        "import socket\n"
        "class _OfflineSocket(socket.socket):\n"
        "    def connect(self, *args, **kwargs):\n"
        "        raise RuntimeError('network is forbidden by the standard analysis E2E')\n"
        "    def connect_ex(self, *args, **kwargs):\n"
        "        raise RuntimeError('network is forbidden by the standard analysis E2E')\n"
        "socket.socket = _OfflineSocket\n",
        encoding="utf-8",
    )
    environment = {
        **os.environ,
        "PYTHONUTF8": "1",
        "PYTHONPATH": os.pathsep.join(
            [str(network_guard), str(repo_root), os.environ.get("PYTHONPATH", "")]
        ),
    }
    command = [
        str(repo_root / ".venv/Scripts/python.exe"),
        str(
            repo_root
            / ".agents/skills/analyze-quant-robustness/scripts/analyze_quant_robustness.py"
        ),
        "run",
        "--repository",
        str(root),
        "--source-registry",
        registry.relative_to(root).as_posix(),
    ]

    analysis_process = subprocess.run(
        command,
        cwd=repo_root,
        env=environment,
        capture_output=True,
        text=True,
        shell=False,
        check=True,
    )
    analysis = json.loads(analysis_process.stdout)
    workspace = root / ".local/standard-strategy-analysis" / analysis["analysis_id"]
    report_process = subprocess.run(
        [
            str(repo_root / ".venv/Scripts/python.exe"),
            str(
                repo_root
                / ".agents/skills/analyze-quant-robustness/scripts/analyze_quant_robustness.py"
            ),
            "report",
            "--repository",
            str(root),
            "--workspace",
            str(workspace),
        ],
        cwd=repo_root,
        env=environment,
        capture_output=True,
        text=True,
        shell=False,
        check=True,
    )

    assert json.loads(report_process.stdout)["decision"] == "revise_before_joinquant"
    assert (workspace / "deterministic-analysis.json").is_file()
    assert (workspace / "standard-strategy-analysis-report.md").is_file()
    assert (workspace / "recommendation.json").is_file()
    assert {name: _tree_sha(path) for name, path in sources.items()} == before
