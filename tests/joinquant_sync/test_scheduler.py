from __future__ import annotations

import os
import subprocess
import sys
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
    assert (
        root.findtext(".//t:ScheduleByDay/t:DaysInterval", namespaces=namespace) == "1"
    )


def test_scheduler_xml_preserves_explicit_self_test_command() -> None:
    from joinquant_sync.scheduler import scheduler_xml

    xml = scheduler_xml(
        Path("python.exe"),
        Path("jq_sync.py"),
        "JoinQuantArchiveSync-SelfTest",
        extra_arguments=["self-test", "--repo-root", "D:/repo"],
    )
    assert "self-test --repo-root D:/repo" in xml
    assert "sync-active-simulations self-test" not in xml


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

    def fake_run(
        command: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        assert Path(command[command.index("/XML") + 1]).is_file()
        return subprocess.CompletedProcess(command, 0, "SUCCESS", "")

    monkeypatch.setattr(scheduler.subprocess, "run", fake_run)
    monkeypatch.setattr(scheduler.tempfile, "gettempdir", lambda: str(tmp_path))
    scheduler.install_scheduler("JoinQuantArchiveSync", ["python.exe", "jq_sync.py"])
    assert calls[0][:4] == ["schtasks.exe", "/Create", "/TN", "JoinQuantArchiveSync"]
    assert "/F" not in calls[0]
    assert list(tmp_path.iterdir()) == []


def test_status_and_uninstall_are_non_interactive(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import joinquant_sync.scheduler as scheduler

    calls: list[list[str]] = []

    def fake_run(
        command: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        return subprocess.CompletedProcess(
            command,
            0,
            _scheduled_xml(tmp_path),
            "",
        )

    monkeypatch.setattr(scheduler.subprocess, "run", fake_run)
    assert scheduler.scheduler_status("JoinQuantArchiveSync")["installed"] is True
    scheduler.uninstall_scheduler("JoinQuantArchiveSync")
    assert calls[0] == ["schtasks.exe", "/Query", "/TN", "JoinQuantArchiveSync", "/XML"]
    assert calls[1] == ["schtasks.exe", "/Query", "/TN", "JoinQuantArchiveSync", "/XML"]
    assert calls[2] == ["schtasks.exe", "/Delete", "/TN", "JoinQuantArchiveSync", "/F"]


def test_self_test_command_uses_repository_venv() -> None:
    from joinquant_sync.scheduler import self_test_command

    command = self_test_command(Path("D:/repo"))
    assert command[0].replace("\\", "/").endswith("D:/repo/.venv/Scripts/python.exe")
    assert (
        command[1]
        .replace("\\", "/")
        .endswith(".agents/skills/joinquant-archive-sync/scripts/jq_sync.py")
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
                "JoinQuantArchiveSync-Test",
            ]
        )
        == 0
    )
    assert installed[0][0] == "JoinQuantArchiveSync-Test"
    assert installed[0][1][2:] == [
        "sync-active-simulations",
        "--repository",
        str(tmp_path.resolve()),
    ]
    assert (
        jq_sync.main(["schedule-status", "--task-name", "JoinQuantArchiveSync-Test"])
        == 0
    )
    assert (
        jq_sync.main(["schedule-uninstall", "--task-name", "JoinQuantArchiveSync-Test"])
        == 0
    )
    assert removed == ["JoinQuantArchiveSync-Test"]
    assert '"installed": true' in capsys.readouterr().out


def test_cli_exposes_active_simulation_command() -> None:
    from jq_sync import build_parser

    args = build_parser().parse_args(
        ["sync-active-simulations", "--repository", "D:/repo"]
    )
    assert args.command == "sync-active-simulations"
    assert args.repository == "D:/repo"


def test_scheduler_rejects_unowned_task_name() -> None:
    from joinquant_sync.scheduler import SchedulerError, scheduler_status

    with pytest.raises(SchedulerError, match="namespace"):
        scheduler_status("UnrelatedTask")


def test_scheduler_marker_in_arguments_does_not_grant_ownership(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import joinquant_sync.scheduler as scheduler

    xml = (
        "<Task><RegistrationInfo><Description>Other task</Description></RegistrationInfo>"
        "<Actions><Exec><Arguments>JoinQuant active simulation archive sync</Arguments>"
        "</Exec></Actions></Task>"
    )
    monkeypatch.setattr(
        scheduler.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0, xml, ""),
    )
    assert scheduler.scheduler_status("JoinQuantArchiveSync")["owned"] is False


def _scheduled_xml(tmp_path: Path) -> str:
    from joinquant_sync.scheduler import scheduler_xml

    root = tmp_path / "repo"
    return scheduler_xml(
        root / ".venv" / "Scripts" / "python.exe",
        root
        / ".agents"
        / "skills"
        / "joinquant-archive-sync"
        / "scripts"
        / "jq_sync.py",
        "JoinQuantArchiveSync",
        ["sync-active-simulations", "--repository", str(root.resolve())],
    )


def test_scheduler_status_accepts_exact_production_contract(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import joinquant_sync.scheduler as scheduler

    xml = _scheduled_xml(tmp_path)
    monkeypatch.setattr(
        scheduler.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0, xml, ""),
    )
    assert scheduler.scheduler_status("JoinQuantArchiveSync")["owned"] is True


@pytest.mark.parametrize(
    ("tag", "replacement"),
    [
        ("Description", "JoinQuantArchiveSync: forged"),
        ("Command", "C:/Python/python.exe"),
        ("Arguments", "sync-active-simulations"),
        ("WorkingDirectory", "C:/Temp"),
        ("StartBoundary", "2000-01-01T05:00:00"),
        ("Enabled", "false"),
        ("Interval", "PT20M"),
        ("Count", "4"),
    ],
)
def test_scheduler_status_rejects_any_forged_contract_field(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    tag: str,
    replacement: str,
) -> None:
    import joinquant_sync.scheduler as scheduler

    root = ElementTree.fromstring(_scheduled_xml(tmp_path))
    node = next(item for item in root.iter() if item.tag.rsplit("}", 1)[-1] == tag)
    node.text = replacement
    xml = ElementTree.tostring(root, encoding="unicode")
    calls: list[list[str]] = []

    def fake_run(
        command: list[str], **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, xml, "")

    monkeypatch.setattr(scheduler.subprocess, "run", fake_run)
    assert scheduler.scheduler_status("JoinQuantArchiveSync")["owned"] is False
    with pytest.raises(scheduler.SchedulerError, match="not owned"):
        scheduler.uninstall_scheduler("JoinQuantArchiveSync")
    assert all("/Delete" not in call for call in calls)


def test_scheduler_status_rejects_an_extra_trigger(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import joinquant_sync.scheduler as scheduler

    root = ElementTree.fromstring(_scheduled_xml(tmp_path))
    triggers = next(
        item for item in root.iter() if item.tag.rsplit("}", 1)[-1] == "Triggers"
    )
    ElementTree.SubElement(
        triggers, "{http://schemas.microsoft.com/windows/2004/02/mit/task}EventTrigger"
    )
    xml = ElementTree.tostring(root, encoding="unicode")
    monkeypatch.setattr(
        scheduler.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0, xml, ""),
    )
    assert scheduler.scheduler_status("JoinQuantArchiveSync")["owned"] is False


def test_wait_ignores_running_scheduler_code(monkeypatch: pytest.MonkeyPatch) -> None:
    import joinquant_sync.scheduler as scheduler

    outputs = iter(["Last Result: 0x41301\n", "Last Result: 0x0\n"])
    monkeypatch.setattr(
        scheduler.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0], 0, next(outputs), ""
        ),
    )
    monkeypatch.setattr(scheduler.time, "sleep", lambda _: None)
    assert scheduler.wait_for_task_result("JoinQuantArchiveSync", 1) == 0


@pytest.mark.skipif(sys.platform != "win32", reason="Windows Task Scheduler only")
def test_schtasks_runs_self_test(repo_root: Path) -> None:
    from joinquant_sync.scheduler import (
        install_scheduler,
        scheduler_status,
        self_test_command,
        uninstall_scheduler,
        wait_for_task_result,
    )

    task_name = f"JoinQuantArchiveSync-SelfTest-{os.getpid()}"
    try:
        install_scheduler(task_name, self_test_command(repo_root))
        subprocess.run(
            ["schtasks.exe", "/Run", "/TN", task_name],
            check=True,
            capture_output=True,
            text=True,
        )
        assert wait_for_task_result(task_name, timeout_seconds=60) == 0
    finally:
        if scheduler_status(task_name)["installed"]:
            uninstall_scheduler(task_name)
    assert scheduler_status(task_name)["installed"] is False
