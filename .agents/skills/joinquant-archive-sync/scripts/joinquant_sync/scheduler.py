from __future__ import annotations

import re
import subprocess
import tempfile
import time
import uuid
from pathlib import Path
from xml.etree import ElementTree


TASK_NAMESPACE = "http://schemas.microsoft.com/windows/2004/02/mit/task"


class SchedulerError(RuntimeError):
    """Raised when Windows Task Scheduler cannot complete an operation."""


class TimezoneError(SchedulerError):
    """Raised when 04:00 local time would not be Beijing time."""


def _task_name(name: str) -> str:
    selected = name.strip()
    if not selected or len(selected) > 128 or any(ch in selected for ch in "\r\n\0"):
        raise SchedulerError("task name is invalid")
    return selected


def _element(parent: ElementTree.Element, name: str, text: str | None = None) -> ElementTree.Element:
    node = ElementTree.SubElement(parent, f"{{{TASK_NAMESPACE}}}{name}")
    if text is not None:
        node.text = text
    return node


def scheduler_xml(
    python_exe: Path,
    cli: Path,
    task_name: str,
    extra_arguments: list[str] | None = None,
) -> str:
    name = _task_name(task_name)
    ElementTree.register_namespace("", TASK_NAMESPACE)
    task = ElementTree.Element(
        f"{{{TASK_NAMESPACE}}}Task", {"version": "1.4"}
    )
    registration = _element(task, "RegistrationInfo")
    _element(registration, "Description", f"{name}: JoinQuant active simulation archive sync")

    triggers = _element(task, "Triggers")
    calendar = _element(triggers, "CalendarTrigger")
    _element(calendar, "StartBoundary", "2000-01-01T04:00:00")
    _element(calendar, "Enabled", "true")
    daily = _element(calendar, "ScheduleByDay")
    _element(daily, "DaysInterval", "1")

    principals = _element(task, "Principals")
    principal = _element(principals, "Principal")
    principal.set("id", "Author")
    _element(principal, "LogonType", "InteractiveToken")
    _element(principal, "RunLevel", "LeastPrivilege")

    settings = _element(task, "Settings")
    _element(settings, "MultipleInstancesPolicy", "IgnoreNew")
    _element(settings, "DisallowStartIfOnBatteries", "false")
    _element(settings, "StopIfGoingOnBatteries", "false")
    _element(settings, "StartWhenAvailable", "true")
    retry = _element(settings, "RestartOnFailure")
    _element(retry, "Interval", "PT30M")
    _element(retry, "Count", "3")
    _element(settings, "ExecutionTimeLimit", "PT2H")
    _element(settings, "Enabled", "true")

    actions = _element(task, "Actions")
    actions.set("Context", "Author")
    execute = _element(actions, "Exec")
    _element(execute, "Command", str(python_exe.resolve()))
    arguments = [str(cli.resolve()), "sync-active-simulations", *(extra_arguments or [])]
    _element(execute, "Arguments", subprocess.list2cmdline(arguments))
    _element(execute, "WorkingDirectory", str(cli.resolve().parent))
    body = ElementTree.tostring(task, encoding="unicode")
    return '<?xml version="1.0" encoding="UTF-16"?>\n' + body


def _run(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        errors="replace",
    )


def install_scheduler(task_name: str, command: list[str]) -> None:
    name = _task_name(task_name)
    if len(command) < 2:
        raise SchedulerError("scheduler command requires Python and CLI paths")
    timezone = subprocess.check_output(
        ["tzutil.exe", "/g"], text=True, errors="replace"
    ).strip()
    if timezone != "China Standard Time":
        raise TimezoneError(
            f"expected China Standard Time before installing 04:00 task, got {timezone}"
        )
    xml = scheduler_xml(
        Path(command[0]), Path(command[1]), name, extra_arguments=command[2:]
    )
    temporary = Path(tempfile.gettempdir()) / f"joinquant-scheduler-{uuid.uuid4().hex}.xml"
    try:
        temporary.write_text(xml, encoding="utf-16", newline="")
        result = _run(
            ["schtasks.exe", "/Create", "/TN", name, "/XML", str(temporary), "/F"]
        )
        if result.returncode != 0:
            raise SchedulerError(result.stderr.strip() or result.stdout.strip())
    finally:
        temporary.unlink(missing_ok=True)


def scheduler_status(task_name: str) -> dict[str, object]:
    name = _task_name(task_name)
    result = _run(["schtasks.exe", "/Query", "/TN", name, "/XML"])
    if result.returncode != 0:
        return {
            "task_name": name,
            "installed": False,
            "message": result.stderr.strip() or result.stdout.strip(),
        }
    return {"task_name": name, "installed": True, "xml": result.stdout}


def uninstall_scheduler(task_name: str) -> None:
    name = _task_name(task_name)
    result = _run(["schtasks.exe", "/Delete", "/TN", name, "/F"])
    if result.returncode != 0:
        raise SchedulerError(result.stderr.strip() or result.stdout.strip())


def self_test_command(repo_root: Path) -> list[str]:
    root = repo_root.resolve()
    return [
        str(root / ".venv" / "Scripts" / "python.exe"),
        str(
            root
            / ".agents"
            / "skills"
            / "joinquant-archive-sync"
            / "scripts"
            / "jq_sync.py"
        ),
        "self-test",
        "--repo-root",
        str(root),
    ]


def _last_result(output: str) -> int | None:
    match = re.search(r"(?:Last Result|上次运行结果)\s*:\s*(0x[0-9a-fA-F]+|\d+)", output)
    if match is None:
        return None
    return int(match.group(1), 0)


def wait_for_task_result(task_name: str, timeout_seconds: int) -> int:
    name = _task_name(task_name)
    deadline = time.monotonic() + max(0, timeout_seconds)
    while True:
        result = _run(["schtasks.exe", "/Query", "/TN", name, "/FO", "LIST", "/V"])
        if result.returncode != 0:
            raise SchedulerError(result.stderr.strip() or result.stdout.strip())
        last_result = _last_result(result.stdout)
        if last_result is not None:
            return last_result
        if time.monotonic() >= deadline:
            raise SchedulerError("scheduled task result timed out")
        time.sleep(0.25)
