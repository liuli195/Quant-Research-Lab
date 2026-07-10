from __future__ import annotations

import subprocess
from pathlib import Path
from xml.etree import ElementTree

import pytest


def test_scheduler_xml_uses_beijing_0400_and_three_retries() -> None:
    from joinquant_sync.scheduler import scheduler_xml

    xml = scheduler_xml(Path("python.exe"), Path("jq_sync.py"), "JoinQuantArchiveSync")
    assert xml.startswith('<?xml version="1.0" encoding="UTF-16"?>')
    assert "T04:00:00" in xml
    assert "<Interval>PT30M</Interval>" in xml
    assert "<Count>3</Count>" in xml
    assert "sync-active-simulations" in xml
    root = ElementTree.fromstring(xml)
    namespace = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
    assert root.findtext(".//t:ScheduleByDay/t:DaysInterval", namespaces=namespace) == "1"


def test_install_rejects_non_china_timezone(monkeypatch: pytest.MonkeyPatch) -> None:
    import joinquant_sync.scheduler as scheduler

    monkeypatch.setattr(
        scheduler.subprocess,
        "check_output",
        lambda *args, **kwargs: "Pacific Standard Time",
    )
    with pytest.raises(scheduler.TimezoneError):
        scheduler.install_scheduler(
            "JoinQuantArchiveSync", ["python.exe", "jq_sync.py"]
        )


def test_install_uses_schtasks_xml_and_cleans_temp(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import joinquant_sync.scheduler as scheduler

    calls: list[list[str]] = []
    monkeypatch.setattr(
        scheduler.subprocess,
        "check_output",
        lambda *args, **kwargs: "China Standard Time\n",
    )

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        assert Path(command[command.index("/XML") + 1]).is_file()
        return subprocess.CompletedProcess(command, 0, "SUCCESS", "")

    monkeypatch.setattr(scheduler.subprocess, "run", fake_run)
    monkeypatch.setattr(scheduler.tempfile, "gettempdir", lambda: str(tmp_path))
    scheduler.install_scheduler(
        "JoinQuantArchiveSync", ["python.exe", "jq_sync.py"]
    )
    assert calls[0][:4] == ["schtasks.exe", "/Create", "/TN", "JoinQuantArchiveSync"]
    assert list(tmp_path.iterdir()) == []


def test_status_and_uninstall_are_non_interactive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import joinquant_sync.scheduler as scheduler

    calls: list[list[str]] = []

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, "<Task />", "")

    monkeypatch.setattr(scheduler.subprocess, "run", fake_run)
    assert scheduler.scheduler_status("JoinQuantArchiveSync")["installed"] is True
    scheduler.uninstall_scheduler("JoinQuantArchiveSync")
    assert calls[0] == ["schtasks.exe", "/Query", "/TN", "JoinQuantArchiveSync", "/XML"]
    assert calls[1] == ["schtasks.exe", "/Delete", "/TN", "JoinQuantArchiveSync", "/F"]


def test_self_test_command_uses_repository_venv() -> None:
    from joinquant_sync.scheduler import self_test_command

    command = self_test_command(Path("D:/repo"))
    assert command[0].replace("\\", "/").endswith("D:/repo/.venv/Scripts/python.exe")
    assert command[1].replace("\\", "/").endswith(
        ".agents/skills/joinquant-archive-sync/scripts/jq_sync.py"
    )
    assert command[2:] == ["self-test", "--repo-root", str(Path("D:/repo").resolve())]


def test_wait_for_task_result_accepts_hex_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    import joinquant_sync.scheduler as scheduler

    monkeypatch.setattr(
        scheduler.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0], 0, "Last Result: 0x0\n", ""
        ),
    )
    assert scheduler.wait_for_task_result("JoinQuantArchiveSync", 1) == 0


def test_cli_installs_statuses_and_uninstalls_scheduler(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    import jq_sync

    installed: list[tuple[str, list[str]]] = []
    removed: list[str] = []
    monkeypatch.setattr(
        jq_sync,
        "install_scheduler",
        lambda name, command: installed.append((name, command)),
    )
    monkeypatch.setattr(
        jq_sync,
        "scheduler_status",
        lambda name: {"task_name": name, "installed": True},
    )
    monkeypatch.setattr(
        jq_sync, "uninstall_scheduler", lambda name: removed.append(name)
    )

    assert (
        jq_sync.main(
            [
                "schedule-install",
                "--repo-root",
                str(tmp_path),
                "--task-name",
                "TestJoinQuant",
            ]
        )
        == 0
    )
    assert installed[0][0] == "TestJoinQuant"
    assert installed[0][1][2:] == ["--repository", str(tmp_path.resolve())]
    assert jq_sync.main(["schedule-status", "--task-name", "TestJoinQuant"]) == 0
    assert jq_sync.main(["schedule-uninstall", "--task-name", "TestJoinQuant"]) == 0
    assert removed == ["TestJoinQuant"]
    assert '"installed": true' in capsys.readouterr().out


def test_cli_exposes_active_simulation_command() -> None:
    from jq_sync import build_parser

    args = build_parser().parse_args(
        ["sync-active-simulations", "--repository", "D:/repo"]
    )
    assert args.command == "sync-active-simulations"
    assert args.repository == "D:/repo"
