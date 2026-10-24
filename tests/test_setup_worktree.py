import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).parents[1]
SCRIPT = REPO_ROOT / "scripts" / "setup-worktree.ps1"
ANSI_ESCAPE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


def run(*args, cwd, env=None):
    return subprocess.run(args, cwd=cwd, env=env, text=True, encoding="utf-8", errors="replace", capture_output=True)


def output(result):
    return " ".join(re.sub(r"\s*\|\s*", " ", ANSI_ESCAPE.sub("", result.stdout + result.stderr)).split())


def make_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "scripts").mkdir()
    shutil.copy2(SCRIPT, repo / "scripts" / SCRIPT.name)
    (repo / "requirements.txt").write_text("base\n", encoding="utf-8")
    (repo / "requirements-dev.txt").write_text("dev\n", encoding="utf-8")
    nested = repo / ".agents/skills/joinquant-archive-sync/requirements.txt"
    nested.parent.mkdir(parents=True, exist_ok=True)
    nested.write_text("", encoding="utf-8")
    run("git", "init", cwd=repo)
    run("git", "config", "user.email", "test@example.com", cwd=repo)
    run("git", "config", "user.name", "Test", cwd=repo)
    run("git", "add", ".", cwd=repo)
    run("git", "commit", "-m", "fixture", cwd=repo)
    return repo


def test_repository_uses_root_worktrees_directory():
    ignored_paths = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()

    assert ".worktrees/" in ignored_paths
    assert ".claude/worktrees/" not in ignored_paths


def test_build_and_verify_runs_worktree_setup_checks():
    config = json.loads((REPO_ROOT / ".build-and-verify" / "config.json").read_text(encoding="utf-8"))

    check = next(item for item in config["verify"]["checks"] if item["id"] == "verify.worktree-setup")
    assert "tests/test_setup_worktree.py" in check["command"]
    assert set(check["paths"]) >= {
        ".gitignore",
        "scripts/setup-worktree.ps1",
        "tests/test_setup_worktree.py",
        "requirements*.txt",
    }


@pytest.mark.real
def test_main_worktree_update_and_verification_smoke(tmp_path):
    repo = make_repo(tmp_path)
    for name in ("requirements.txt", "requirements-dev.txt"):
        (repo / name).write_text("", encoding="utf-8")
    config = {"version": 2, "build": {"checks": []}, "verify": {
        "realChecks": ["verify.shared-venv-smoke"], "checks": [{
            "id": "verify.shared-venv-smoke", "paths": ["probe.txt"], "inputs": ["probe.txt"],
            "command": '.\\.venv\\Scripts\\python.exe -c "from pathlib import Path; Path(\'bav-marker\').write_text(\'ok\')"',
        }]}}
    (repo / ".build-and-verify").mkdir()
    (repo / ".build-and-verify/config.json").write_text(json.dumps(config), encoding="utf-8")
    assert run("git", "add", ".", cwd=repo).returncode == 0
    assert run("git", "commit", "-m", "empty dependency smoke", cwd=repo).returncode == 0
    env = os.environ | {
        "PIP_NO_INDEX": "1", "PIP_DISABLE_PIP_VERSION_CHECK": "1",
        "PIP_CACHE_DIR": str(tmp_path / "pip-cache"),
    }
    def setup(root):
        result = run("pwsh", "-NoProfile", "-File", str(root / "scripts" / SCRIPT.name), cwd=root, env=env)
        assert result.returncode == 0, output(result)
    setup(repo)
    fingerprint = repo / ".venv/.requirements.sha256"
    before = fingerprint.read_text(encoding="ascii")
    worktree = tmp_path / "worktree"
    assert run("git", "worktree", "add", str(worktree), cwd=repo).returncode == 0
    setup(worktree)
    assert (worktree / ".venv").resolve() == (repo / ".venv").resolve()
    for root in (repo, worktree):
        (root / "requirements.txt").write_text("# normal update\n", encoding="utf-8")
    setup(repo)
    assert fingerprint.read_text(encoding="ascii") != before
    setup(worktree)
    (worktree / "probe.txt").write_text("changed", encoding="utf-8")
    command = shutil.which("build-and-verify")
    assert command, "build-and-verify CLI is required"
    result = run(command, "verify", "--project", ".", "--execution-context", "local", cwd=worktree, env=env)
    assert result.returncode == 0, output(result)
    assert (worktree / "bav-marker").read_text(encoding="utf-8") == "ok"
