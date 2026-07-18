from __future__ import annotations

import os
import re
import shlex
import subprocess
import tempfile
import time
import uuid
from pathlib import Path
from xml.etree import ElementTree


TASK_NAMESPACE = "http://schemas.microsoft.com/windows/2004/02/mit/task"
TASK_PREFIX = "JoinQuantArchiveSync"
TASK_MARKER = "JoinQuant active simulation archive sync"


class SchedulerError(RuntimeError):
    """Raised when Windows Task Scheduler cannot complete an operation."""


class TimezoneError(SchedulerError):
    """Raised when 04:00 local time would not be Beijing time."""


def _task_name(name: str) -> str:
    selected = name.strip()
    if not selected or len(selected) > 128 or any(ch in selected for ch in "\r\n\0"):
        raise SchedulerError("task name is invalid")
    if selected != TASK_PREFIX and not selected.startswith(f"{TASK_PREFIX}-"):
        raise SchedulerError(f"task name is outside the {TASK_PREFIX} namespace")
    return selected


def _element(
    parent: ElementTree.Element, name: str, text: str | None = None
) -> ElementTree.Element:
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
    task = ElementTree.Element(f"{{{TASK_NAMESPACE}}}Task", {"version": "1.4"})
    registration = _element(task, "RegistrationInfo")
    _element(registration, "Description", f"{name}: {TASK_MARKER}")

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
    selected_arguments = extra_arguments or ["sync-active-simulations"]
    arguments = [str(cli.resolve()), *selected_arguments]
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


def _windows_arguments(value: str) -> list[str]:
    return [
        token[1:-1] if len(token) >= 2 and token[0] == token[-1] == '"' else token
        for token in shlex.split(value, posix=False)
    ]


def _same_path(left: Path, right: Path) -> bool:
    return os.path.normcase(str(left.resolve())) == os.path.normcase(
        str(right.resolve())
    )


def _owned_task(root: ElementTree.Element, name: str) -> bool:
    namespace = {"t": TASK_NAMESPACE}
    executions = root.findall("./t:Actions/t:Exec", namespace)
    calendars = root.findall("./t:Triggers/t:CalendarTrigger", namespace)
    retries = root.findall("./t:Settings/t:RestartOnFailure", namespace)
    actions = root.findall("./t:Actions/*", namespace)
    triggers = root.findall("./t:Triggers/*", namespace)
    if not (
        len(executions) == len(actions) == 1
        and len(calendars) == len(triggers) == 1
        and len(retries) == 1
    ):
        return False
    execution, calendar, retry = executions[0], calendars[0], retries[0]
    description = root.findtext(
        "./t:RegistrationInfo/t:Description", namespaces=namespace
    )
    command_text = execution.findtext("t:Command", namespaces=namespace)
    arguments_text = execution.findtext("t:Arguments", namespaces=namespace)
    working_text = execution.findtext("t:WorkingDirectory", namespaces=namespace)
    if None in {description, command_text, arguments_text, working_text}:
        return False
    arguments = _windows_arguments(str(arguments_text))
    if len(arguments) != 4:
        return False
    cli = Path(arguments[0])
    if not cli.is_absolute() or len(cli.parents) < 5:
        return False
    repository = cli.parents[4]
    expected_cli = (
        repository
        / ".agents"
        / "skills"
        / "joinquant-archive-sync"
        / "scripts"
        / "jq_sync.py"
    )
    expected_actions = (
        {("self-test", "--repo-root")}
        if name.startswith(f"{TASK_PREFIX}-SelfTest-")
        else {
            ("sync-active-simulations", "--repository"),
            ("scheduled-sync-pr", "--repository"),
        }
    )
    paths_match = (
        _same_path(cli, expected_cli)
        and _same_path(
            Path(str(command_text)), repository / ".venv" / "Scripts" / "python.exe"
        )
        and _same_path(Path(str(working_text)), cli.parent)
        and _same_path(Path(arguments[3]), repository)
    )
    return bool(
        description == f"{name}: {TASK_MARKER}"
        and paths_match
        and tuple(arguments[1:3]) in expected_actions
        and calendar.findtext("t:StartBoundary", namespaces=namespace)
        == "2000-01-01T04:00:00"
        and calendar.findtext("t:Enabled", namespaces=namespace) in {None, "true"}
        and calendar.findtext("t:ScheduleByDay/t:DaysInterval", namespaces=namespace)
        == "1"
        and retry.findtext("t:Interval", namespaces=namespace) == "PT30M"
        and retry.findtext("t:Count", namespaces=namespace) == "3"
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
    temporary = (
        Path(tempfile.gettempdir()) / f"joinquant-scheduler-{uuid.uuid4().hex}.xml"
    )
    try:
        temporary.write_text(xml, encoding="utf-16", newline="")
        result = _run(["schtasks.exe", "/Create", "/TN", name, "/XML", str(temporary)])
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
    try:
        root = ElementTree.fromstring(result.stdout)
    except ElementTree.ParseError as error:
        raise SchedulerError("scheduled task XML is invalid") from error
    return {
        "task_name": name,
        "installed": True,
        "owned": _owned_task(root, name),
        "xml": result.stdout,
    }


def uninstall_scheduler(task_name: str) -> None:
    name = _task_name(task_name)
    status = scheduler_status(name)
    if not status.get("installed"):
        raise SchedulerError("scheduled task is not installed")
    if not status.get("owned"):
        raise SchedulerError("refusing to delete a task not owned by this skill")
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
    match = re.search(
        r"(?:Last Result|上次运行结果)\s*:\s*(0x[0-9a-fA-F]+|\d+)", output
    )
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
        if last_result is not None and last_result not in {0x41300, 0x41301, 0x41303}:
            return last_result
        if time.monotonic() >= deadline:
            raise SchedulerError("scheduled task result timed out")
        time.sleep(0.25)
