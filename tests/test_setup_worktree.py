import hashlib
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
    run("git", "init", cwd=repo)
    run("git", "config", "user.email", "test@example.com", cwd=repo)
    run("git", "config", "user.name", "Test", cwd=repo)
    run("git", "add", ".", cwd=repo)
    run("git", "commit", "-m", "fixture", cwd=repo)
    return repo


def write_fingerprint(repo):
    lines = []
    for name in ("requirements.txt", "requirements-dev.txt"):
        digest = hashlib.sha256((repo / name).read_bytes()).hexdigest().upper()
        lines.append(f"{digest} {name}")
    (repo / ".venv" / ".requirements.sha256").write_text("\n".join(lines) + "\n", encoding="ascii")


def prepare_shared_venv(repo):
    shared_venv = repo / ".venv"
    shared_python = shared_venv / "Scripts" / "python.exe"
    shared_python.parent.mkdir(parents=True)
    shared_python.write_text("fixture", encoding="utf-8")
    write_fingerprint(repo)
    return shared_venv


def test_repository_uses_root_worktrees_directory():
    ignored_paths = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()

    assert ".worktrees/" in ignored_paths
    assert ".claude/worktrees/" not in ignored_paths


def test_build_and_verify_runs_worktree_setup_checks():
    config = json.loads((REPO_ROOT / ".build-and-verify" / "config.json").read_text(encoding="utf-8"))

    check = next(item for item in config["verify"]["checks"] if item["id"] == "verify.worktree-setup")
    assert "tests\\test_setup_worktree.py" in check["command"]
    assert set(check["paths"]) >= {
        ".gitignore",
        "scripts/setup-worktree.ps1",
        "tests/test_setup_worktree.py",
        "requirements*.txt",
    }


def test_main_repository_initializes_and_updates_venv(tmp_path):
    repo = make_repo(tmp_path)
    (repo / "requirements.txt").write_text("", encoding="utf-8")
    (repo / "requirements-dev.txt").write_text("", encoding="utf-8")
    env = os.environ | {"PIP_NO_INDEX": "1"}

    first = run("pwsh", "-NoProfile", "-File", str(repo / "scripts" / SCRIPT.name), cwd=repo, env=env)
    second = run("pwsh", "-NoProfile", "-File", str(repo / "scripts" / SCRIPT.name), cwd=repo, env=env)

    assert first.returncode == 0, first.stderr
    assert second.returncode == 0, second.stderr
    assert (repo / ".venv" / "Scripts" / "python.exe").is_file()
    fingerprint = (repo / ".venv" / ".requirements.sha256").read_text(encoding="utf-8")
    assert "requirements.txt" in fingerprint
    assert "requirements-dev.txt" in fingerprint


def test_failed_main_repository_update_invalidates_fingerprint(tmp_path):
    repo = make_repo(tmp_path)
    (repo / "requirements.txt").write_text("package-that-cannot-exist==0\n", encoding="utf-8")
    result = run("py", "-3.12", "-m", "venv", ".venv", cwd=repo)
    assert result.returncode == 0, result.stderr
    write_fingerprint(repo)

    result = run(
        "pwsh",
        "-NoProfile",
        "-File",
        str(repo / "scripts" / SCRIPT.name),
        cwd=repo,
        env=os.environ | {"PIP_NO_INDEX": "1"},
    )

    assert result.returncode != 0
    assert not (repo / ".venv" / ".requirements.sha256").exists()


def test_build_and_verify_uses_shared_venv_from_worktree(tmp_path):
    repo = make_repo(tmp_path)
    (repo / ".build-and-verify").mkdir()
    config = {
        "version": 1,
        "build": {"checks": []},
        "verify": {
            "checks": [
                {
                    "id": "verify.shared-venv-smoke",
                    "command": ".\\.venv\\Scripts\\python.exe -c \"from pathlib import Path; Path('bav-marker').write_text('ok')\"",
                    "paths": ["probe.txt"],
                    "inputs": ["probe.txt"],
                }
            ]
        },
    }
    (repo / ".build-and-verify" / "config.json").write_text(json.dumps(config), encoding="utf-8")
    run("git", "add", ".build-and-verify", cwd=repo)
    result = run("git", "commit", "-m", "add build verification", cwd=repo)
    assert result.returncode == 0, result.stderr
    result = run("py", "-3.12", "-m", "venv", ".venv", cwd=repo)
    assert result.returncode == 0, result.stderr
    write_fingerprint(repo)
    worktree = tmp_path / "worktree"
    result = run("git", "worktree", "add", str(worktree), cwd=repo)
    assert result.returncode == 0, result.stderr
    result = run("pwsh", "-NoProfile", "-File", str(worktree / "scripts" / SCRIPT.name), cwd=worktree)
    assert result.returncode == 0, result.stderr
    (worktree / "probe.txt").write_text("changed", encoding="utf-8")

    command = shutil.which("build-and-verify")
    assert command, "build-and-verify CLI is required"
    result = run(command, "verify", "--project", ".", cwd=worktree)

    assert result.returncode == 0, output(result)
    assert (worktree / "bav-marker").read_text(encoding="utf-8") == "ok"


@pytest.mark.parametrize("shell_name", ["powershell", "pwsh"])
def test_linked_worktree_reuses_main_repository_venv(tmp_path, shell_name):
    repo = make_repo(tmp_path)
    shared_venv = prepare_shared_venv(repo)
    (shared_venv / "shared-marker").write_text("shared", encoding="utf-8")
    worktree = tmp_path / "worktree"
    result = run("git", "worktree", "add", str(worktree), cwd=repo)
    assert result.returncode == 0, result.stderr

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    (fake_bin / "py.cmd").write_text("@exit /b 99\n", encoding="ascii")
    env = os.environ | {"PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}"}
    result = run(shell_name, "-NoProfile", "-File", str(worktree / "scripts" / SCRIPT.name), cwd=worktree, env=env)

    assert result.returncode == 0, result.stderr
    assert (worktree / ".venv" / "shared-marker").read_text(encoding="utf-8") == "shared"

    result = run(shell_name, "-NoProfile", "-File", str(worktree / "scripts" / SCRIPT.name), cwd=worktree, env=env)
    assert result.returncode == 0, result.stderr


def test_linked_worktree_stops_when_shared_venv_is_missing(tmp_path):
    repo = make_repo(tmp_path)
    worktree = tmp_path / "worktree"
    result = run("git", "worktree", "add", str(worktree), cwd=repo)
    assert result.returncode == 0, result.stderr

    result = run("pwsh", "-NoProfile", "-File", str(worktree / "scripts" / SCRIPT.name), cwd=worktree)

    assert result.returncode != 0
    assert "run scripts/setup-worktree.ps1 in the main repository first" in output(result)
    assert not (worktree / ".venv").exists()


def test_linked_worktree_stops_when_shared_venv_is_stale(tmp_path):
    repo = make_repo(tmp_path)
    prepare_shared_venv(repo)
    worktree = tmp_path / "worktree"
    result = run("git", "worktree", "add", str(worktree), cwd=repo)
    assert result.returncode == 0, result.stderr
    for root in (repo, worktree):
        (root / "requirements.txt").write_text("updated\n", encoding="utf-8")

    result = run("pwsh", "-NoProfile", "-File", str(worktree / "scripts" / SCRIPT.name), cwd=worktree)

    assert result.returncode != 0
    assert "Shared Python environment is stale" in (result.stdout + result.stderr)
    assert not (worktree / ".venv").exists()


def test_linked_worktree_stops_when_dependency_manifests_differ(tmp_path):
    repo = make_repo(tmp_path)
    prepare_shared_venv(repo)
    worktree = tmp_path / "worktree"
    result = run("git", "worktree", "add", str(worktree), cwd=repo)
    assert result.returncode == 0, result.stderr
    (worktree / "requirements.txt").write_text("different\n", encoding="utf-8")

    result = run("pwsh", "-NoProfile", "-File", str(worktree / "scripts" / SCRIPT.name), cwd=worktree)

    assert result.returncode != 0
    assert "dependency manifests differ" in output(result)
    assert not (worktree / ".venv").exists()
