"""Scheduled tasks: a series of commands run on a saved SSH session or on this computer, with the output saved
to a text file.

A task runs at the times its schedule gives, either while the application is open (TaskScheduler below) or
from Windows Task Scheduler, which starts the program with --run-task even when the window is closed.
Each line of a task is a command of its own: on a server each runs in a fresh channel of one connection, here
each in a fresh shell, so a `cd` on one line does not carry over to the next. A task can instead run its lines
as one script, in one shell, which keeps the folder and variables from line to line but has one exit status.
"""

import base64
import json
import re
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timedelta
from pathlib import Path

from PyQt6.QtCore import QObject, QThread, QTimer, pyqtSignal

from . import APP_NAME, threads
from .paths import CONFIG_DIR
from .sessions import PROTO_SSH, SessionStore, apply_command_line, write_atomically

TASKS_FILE = CONFIG_DIR / "tasks.json"
OUTPUT_DIR = CONFIG_DIR / "task-output"  # where output files with a relative name, or none, are written

TARGET_POWERSHELL = "local:powershell"
TARGET_CMD = "local:cmd"
LOCAL_TARGETS = [(TARGET_POWERSHELL, "This computer: PowerShell"), (TARGET_CMD, "This computer: Command Prompt")]

SCHEDULE_MANUAL = "manual"  # only when asked to
SCHEDULE_INTERVAL = "interval"  # every so many minutes
SCHEDULE_DAILY = "daily"  # every day at a time
SCHEDULES = [(SCHEDULE_MANUAL, "Only when run by hand"), (SCHEDULE_INTERVAL, "Every"), (SCHEDULE_DAILY, "Every day at")]
MAX_INTERVAL_MINUTES = 1439  # the longest repeat Windows Task Scheduler takes in minutes; longer is "daily"

RUNNER_APP = "app"  # the application's own timer, while it is open
RUNNER_WINDOWS = "windows"  # Windows Task Scheduler, also while it is closed
WINDOWS_FOLDER = APP_NAME.replace(" ", "")  # tasks are registered in a folder of this name
MAX_COMMAND_LINE = 32_000  # Windows does not start a program with a longer command line
MAX_WINDOWS_COMMAND = 261  # the longest command schtasks accepts for /TR

MAX_OUTPUT_BYTES = 10 * 1024 * 1024  # an output file that is added to is cut back, oldest runs first, past this
DEFAULT_TIMEOUT = 300  # seconds one command may run before it is stopped
EXEC_TIMEOUT = 15  # seconds to wait for a server to start a command
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)  # a console window must not flash up for each command
PLACEHOLDERS = ("{name}", "{date}", "{time}")


@dataclass
class Task:
    name: str = ""
    target: str = TARGET_POWERSHELL  # a saved session's id, or one of LOCAL_TARGETS
    commands: str = ""  # one command per line; empty lines and lines starting with # are skipped
    output_file: str = ""  # may hold {name}, {date} and {time}; empty for OUTPUT_DIR/{name}.txt
    append: bool = True  # each run is added to the end of the file; otherwise it replaces the file
    schedule: str = SCHEDULE_MANUAL
    every_minutes: int = 60
    daily_at: str = "09:00"  # HH:MM, local time
    runner: str = RUNNER_APP
    enabled: bool = True
    stop_on_error: bool = False  # skip the remaining commands once one fails
    as_script: bool = False  # all lines in one shell, so a cd or a variable carries over; one exit status for all
    notify_on_failure: bool = True  # a desktop notification when a run fails while no one is looking
    then_run: str = ""  # the id of a task to run once every command of this one succeeded, empty for none
    timeout: int = DEFAULT_TIMEOUT
    created: float = field(default_factory=time.time)
    last_run: float = 0.0  # when the last run started
    last_result: str = ""  # its summary, see TaskResult
    last_ok: bool = False  # whether every command of it succeeded
    registered_as: str = ""  # the Windows Task Scheduler name it was registered under, empty for none
    id: str = field(default_factory=lambda: uuid.uuid4().hex)

    @property
    def title(self) -> str:
        return self.name.strip() or "(unnamed task)"

    def command_lines(self) -> list[str]:
        lines = (line.strip() for line in self.commands.splitlines())
        return [line for line in lines if line and not line.startswith("#")]

    def is_local(self) -> bool:
        return self.target in dict(LOCAL_TARGETS)


def task_from_dict(item) -> Task | None:
    """A task from saved data; a value of the wrong type falls back to the default."""
    if not isinstance(item, dict):
        return None
    defaults = Task()
    clean = {
        f.name: item[f.name]
        for f in fields(Task)
        if f.name in item and type(item[f.name]) is type(getattr(defaults, f.name))
    }
    return Task(**clean)


def _file_name(text: str) -> str:
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", text).strip(" ._") or "task"


def output_path(task: Task, when: datetime | None = None) -> Path:
    """The file a run writes to, with {name}, {date} and {time} filled in; a relative path is in OUTPUT_DIR."""
    when = when or datetime.now()
    text = task.output_file.strip() or "{name}.txt"
    text = (
        text.replace("{name}", _file_name(task.title))
        .replace("{date}", f"{when:%Y-%m-%d}")
        .replace("{time}", f"{when:%H%M%S}")
    )
    path = Path(text).expanduser()
    return path if path.is_absolute() else OUTPUT_DIR / path


def parse_time_of_day(text: str) -> tuple[int, int] | None:
    match = re.fullmatch(r"\s*(\d{1,2}):(\d{2})\s*", text)
    if not match or int(match[1]) > 23 or int(match[2]) > 59:
        return None
    return int(match[1]), int(match[2])


def next_run(task: Task) -> float | None:
    """When the application should run the task next (a time.time() value); None if it is not its to run.

    A run that was missed while the application was closed is made up for once, as soon as it opens.
    """
    if not task.enabled or task.runner != RUNNER_APP or not task.command_lines():
        return None
    since = max(task.last_run, task.created)
    if task.schedule == SCHEDULE_INTERVAL:
        return since + max(1, task.every_minutes) * 60
    if task.schedule == SCHEDULE_DAILY:
        at = parse_time_of_day(task.daily_at)
        if at is None:
            return None
        start = datetime.fromtimestamp(since)
        candidate = start.replace(hour=at[0], minute=at[1], second=0, microsecond=0)
        if candidate.timestamp() <= since:
            candidate += timedelta(days=1)
        return candidate.timestamp()
    return None


def is_due(task: Task, now: float | None = None) -> bool:
    due = next_run(task)
    return due is not None and (now if now is not None else time.time()) >= due


# ---- running -----------------------------------------------------------------------


@dataclass
class TaskResult:
    ok: bool
    summary: str  # one line for the status bar and the task list
    path: Path | None = None  # the output file, when one was written


class _Stopped(Exception):
    pass


RUN_HEADER = "===== "  # only the first line of a run starts so (its last line has -----); files are cut there


def trim_output(path: Path, limit: int = MAX_OUTPUT_BYTES) -> bool:
    """Keep a file that each run is added to from growing for ever: past `limit`, the oldest runs are dropped,
    whole, until it is about half that. Returns whether anything was dropped."""
    try:
        if path.stat().st_size <= limit:
            return False
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    cut = text.find("\n" + RUN_HEADER, max(0, len(text) - limit // 2))
    kept = text[cut + 1 :] if cut >= 0 else ""
    try:
        write_atomically(path, f"(Older runs were removed to keep this file from growing without end.)\n\n{kept}")
    except OSError:
        return False
    return True


_POWERSHELL_WRAPPER = """$ProgressPreference = 'SilentlyContinue'
[Console]::OutputEncoding = [Text.Encoding]::UTF8
$global:LASTEXITCODE = 0
$failed = $false
try {
    $block = [ScriptBlock]::Create('{line}')
    & $block 2>&1 | ForEach-Object {
        if ($_ -is [System.Management.Automation.ErrorRecord]) { $script:failed = $true }
        $_
    } | Out-String -Width 250 -Stream
} catch {
    $_ | Out-String -Width 250 -Stream
    $failed = $true
}
if ($LASTEXITCODE) { exit $LASTEXITCODE }
if ($failed) { exit 1 }
"""


def _local_command(target: str, line: str) -> tuple[str, str]:
    """The command line that runs one line in the target's shell, and the encoding of what it prints."""
    if target == TARGET_CMD:
        # /s: cmd takes everything between the first and the last quote as written.
        return f'cmd.exe /d /s /c "{line}"', "oem"
    # Encoded, the line reaches PowerShell exactly as written, quotes and all.
    # PowerShell writes errors as XML when it is started this way, so they are turned into text here, with the
    # output, as a console shows them. The line goes in as a string, so even one that does not parse is reported.
    script = _POWERSHELL_WRAPPER.replace("{line}", line.replace("'", "''"))
    encoded = base64.b64encode(script.encode("utf-16-le")).decode()
    if len(encoded) > MAX_COMMAND_LINE:
        raise OSError("The commands are too long for one PowerShell command line; run them line by line")
    return (
        "powershell.exe -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -OutputFormat Text "
        f"-EncodedCommand {encoded}",
        "utf-8",
    )


def _kill_tree(process: subprocess.Popen) -> None:
    """End a command and whatever it started; killing only the shell would leave its children running."""
    subprocess.run(
        ["taskkill", "/T", "/F", "/PID", str(process.pid)], capture_output=True, creationflags=NO_WINDOW, check=False
    )
    process.kill()


def task_script(task: Task, shell: str) -> str:
    """A task's lines as one script for a shell ("ssh", or a local target), so that a cd on one line holds for
    the next. With stop_on_error the script ends at the first command that fails, with its exit status."""
    lines = task.command_lines()
    if shell == TARGET_CMD:
        stop = "\nif errorlevel 1 exit /b %errorlevel%" if task.stop_on_error else ""
        return "@echo off\n" + "".join(f"{line}{stop}\n" for line in lines) + "exit /b %errorlevel%\n"
    if shell == TARGET_POWERSHELL:
        if not task.stop_on_error:
            return "\n".join(lines)
        checked = "".join(f"{line}\nif ($LASTEXITCODE) {{ exit $LASTEXITCODE }}\n" for line in lines)
        return "$ErrorActionPreference = 'Stop'\n" + checked  # an error ends the script; the wrapper reports it
    return ("set -e\n" if task.stop_on_error else "") + "\n".join(lines)  # the server's own shell runs it


def run_local(target: str, script: str, timeout: int, should_stop=None) -> tuple[str, int | None]:
    """Run a line, or a script of several, on this computer: (what it printed, its exit code; None when stopped)."""
    batch = None
    if target == TARGET_CMD and "\n" in script:
        # cmd runs a script only from a file, which it reads in the console's code page.
        with tempfile.NamedTemporaryFile("w", suffix=".cmd", delete=False, encoding="oem", errors="replace") as handle:
            handle.write(script.replace("\n", "\r\n"))
        batch = Path(handle.name)
        command, encoding = f'cmd.exe /d /c "{batch}"', "oem"
    else:
        command, encoding = _local_command(target, script)
    try:
        return _run_process(command, encoding, timeout, should_stop)
    finally:
        if batch is not None:
            batch.unlink(missing_ok=True)


def _run_process(command: str, encoding: str, timeout: int, should_stop=None) -> tuple[str, int | None]:
    process = subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        cwd=str(Path.home()),
        creationflags=NO_WINDOW,
    )
    deadline = time.monotonic() + timeout
    while True:
        try:
            output, _ = process.communicate(timeout=0.5)
            break
        except subprocess.TimeoutExpired:
            stopped = should_stop is not None and should_stop()
            if stopped or time.monotonic() > deadline:
                _kill_tree(process)
                output, _ = process.communicate()
                text = output.decode(encoding, "replace")
                if stopped:
                    raise _Stopped(text) from None
                return text + f"\n(stopped after {timeout} s)\n", None
    return output.decode(encoding, "replace"), process.returncode


def _command_session_class():
    from .ssh import SSHWorker  # deferred: paramiko takes a moment to load, and local tasks do not need it

    class CommandSession(SSHWorker):
        """An SSH login with nobody at the keyboard, used to run commands one after another.

        Whatever the login would ask (an unknown host key, a password that is not saved, a one-time code) has
        no one to answer it, so it fails instead: connect once from a tab first, and save the password.
        """

        def ask(self, kind: str, message: str) -> str | None:
            return None

        def _open_shell(self, transport):
            return None  # no terminal: each command gets a channel of its own

        def _start_tunnels(self, transport) -> None:
            pass

        def connect(self) -> None:
            try:
                self._open()
            except Exception as exc:
                self._close()
                raise ConnectionError(self._describe(exc)) from None

        def execute(self, command: str, timeout: int, should_stop=None) -> tuple[str, int | None]:
            transport = self._client.get_transport()
            channel = transport.open_session(timeout=EXEC_TIMEOUT)
            try:
                channel.set_combine_stderr(True)
                channel.settimeout(0.5)
                channel.exec_command(command)
                chunks, deadline = [], time.monotonic() + timeout
                while True:
                    try:
                        data = channel.recv(65536)
                    except socket.timeout:
                        data = None
                    if data == b"":
                        break
                    if data:
                        chunks.append(data)
                        continue
                    if should_stop is not None and should_stop():
                        raise _Stopped(self._decode(chunks))
                    if time.monotonic() > deadline:
                        return self._decode(chunks) + f"\n(stopped after {timeout} s)\n", None
                channel.status_event.wait(EXEC_TIMEOUT)
                return self._decode(chunks), channel.exit_status if channel.exit_status != -1 else None
            finally:
                channel.close()

        def _decode(self, chunks: list[bytes]) -> str:
            return b"".join(chunks).decode(self.session.encoding or "utf-8", "replace").replace("\r\n", "\n")

        def disconnect(self) -> None:
            self._close()

    return CommandSession


def target_name(task: Task, sessions: SessionStore) -> str:
    if task.is_local():
        return dict(LOCAL_TARGETS)[task.target]
    session = sessions.get(task.target)
    return session.title if session else "(a deleted session)"


def run_task(task: Task, sessions: SessionStore, should_stop=None) -> TaskResult:
    """Run every command of a task and write what they print to its output file."""
    started, began = datetime.now(), time.monotonic()
    lines = task.command_lines()
    try:
        path = output_path(task, started)
        path.parent.mkdir(parents=True, exist_ok=True)
        if task.append:
            trim_output(path)
        out = open(path, "a" if task.append else "w", encoding="utf-8", newline="\n")  # noqa: SIM115 - closed below
    except OSError as exc:
        return TaskResult(False, f"Could not write the output file: {exc.strerror or exc}")
    failed = ran = 0
    note = ""
    runner = None
    try:
        out.write(f"{RUN_HEADER}{task.title} on {target_name(task, sessions)}, {started:%Y-%m-%d %H:%M:%S} =====\n")
        out.flush()
        if not lines:
            note = "There are no commands to run"
        elif not task.is_local():
            session = sessions.get(task.target)
            if session is None or session.protocol != PROTO_SSH:
                note = "The session this task runs on is gone, or is not an SSH session"
            else:
                options = apply_command_line(session)
                password = options.password or sessions.get_password(session)
                runner = _command_session_class()(options.session, password, 80, 24, compress=options.compress)
                runner.connect()
        # Each step is (the lines it shows, what is run): every line on its own, or all of them as one script.
        if task.as_script:
            steps = [(lines, task_script(task, "ssh" if runner is not None else task.target))]
        else:
            steps = [([line], line) for line in lines]
        last_status = None
        for shown, command in steps if not note else []:
            if should_stop is not None and should_stop():
                note = "Stopped"
                break
            out.write("\n" + "".join(f"$ {line}\n" for line in shown))
            out.flush()
            try:
                if runner is not None:
                    text, status = runner.execute(command, task.timeout, should_stop)
                else:
                    text, status = run_local(task.target, command, task.timeout, should_stop)
            except _Stopped as stopped:
                out.write(str(stopped))
                note = "Stopped"
                break
            ran += 1
            last_status = status
            out.write(text if text.endswith("\n") or not text else text + "\n")
            out.write(f"[exit status {status if status is not None else 'unknown'}]\n")
            out.flush()
            if status != 0:
                failed += 1
                if task.stop_on_error and not task.as_script:
                    note = f"Stopped at the first failure: {shown[0]}"
                    break
    except (ConnectionError, OSError) as exc:
        note = str(exc) or exc.__class__.__name__
    except Exception as exc:  # paramiko's own errors on a connection that went away mid-run
        note = f"{exc.__class__.__name__}: {exc}"
    finally:
        if runner is not None:
            runner.disconnect()
    seconds = time.monotonic() - began
    if note:
        summary, ok = note, False
    elif task.as_script:
        ok = not failed
        status_text = last_status if last_status is not None else "unknown"
        summary = "The script succeeded" if ok else f"The script failed (exit status {status_text})"
    elif failed:
        summary, ok = f"{failed} of {ran} command{'s' if ran != 1 else ''} failed", False
    else:
        summary, ok = f"{ran} command{'s' if ran != 1 else ''} succeeded", True
    try:
        out.write(f"\n----- {summary} ({seconds:.1f} s) -----\n\n")
        out.close()
    except OSError:
        pass
    return TaskResult(ok, summary, path)


# ---- keeping tasks -----------------------------------------------------------------


def chained_after(tasks: "TaskStore", task: Task, chain: tuple[str, ...] = ()) -> Task | None:
    """The task to run after this one succeeded, or None. `chain` holds the ids already run in this series,
    so that tasks that name each other run once each instead of for ever."""
    following = tasks.get(task.then_run) if task.then_run else None
    return following if following is not None and following.id not in (*chain, task.id) else None


class TaskStore:
    def __init__(self, path: Path = TASKS_FILE):
        self.path = path
        self.tasks: list[Task] = []
        self.load()

    def load(self) -> None:
        self.tasks = []
        try:
            items = json.loads(self.path.read_text(encoding="utf-8"))["tasks"]
        except (OSError, ValueError, KeyError, TypeError):
            return
        for item in items if isinstance(items, list) else []:
            task = task_from_dict(item)
            if task is not None:
                self.tasks.append(task)

    def save(self) -> None:
        write_atomically(self.path, json.dumps({"tasks": [asdict(task) for task in self.tasks]}, indent=2))

    def get(self, task_id: str) -> Task | None:
        return next((task for task in self.tasks if task.id == task_id), None)

    def find(self, wanted: str) -> Task | None:
        """A task by its id or by its name, in any case."""
        wanted = wanted.strip()
        return self.get(wanted) or next((t for t in self.tasks if t.title.lower() == wanted.lower()), None)

    def replace_all(self, tasks: list[Task]) -> None:
        """Keep these tasks. When each last ran comes from the file: a run from Windows may have been since."""
        self.load()
        ran = {task.id: (task.last_run, task.last_result, task.last_ok) for task in self.tasks}
        for task in tasks:
            if task.id in ran and ran[task.id][0] > task.last_run:
                task.last_run, task.last_result, task.last_ok = ran[task.id]
        self.tasks = list(tasks)
        self.save()

    def record(self, task_id: str, started: float, result: TaskResult) -> None:
        self.load()  # the file may have changed since: another run, or the window's own edits
        task = self.get(task_id)
        if task is not None:
            task.last_run, task.last_result, task.last_ok = started, result.summary, result.ok
            self.save()


# ---- Windows Task Scheduler ----------------------------------------------------------


def program_command(task: Task) -> str:
    """The command line Windows runs: this program, with --run-task and the task's id."""
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}" --run-task {task.id}'
    windowless = Path(sys.executable).with_name("pythonw.exe")  # no console window popping up for each run
    python = windowless if windowless.is_file() else Path(sys.executable)
    main = Path(__file__).resolve().parent.parent / "main.py"
    return f'"{python}" "{main}" --run-task {task.id}'


def windows_task_name(task: Task) -> str:
    return f"\\{WINDOWS_FOLDER}\\{_file_name(task.title)}"


def schtasks_create_arguments(task: Task) -> list[str]:
    """The arguments to schtasks.exe that register the task; ValueError when its schedule cannot be."""
    command = program_command(task)
    if len(command) > MAX_WINDOWS_COMMAND:
        raise ValueError("The path to the program is too long for Windows Task Scheduler")
    arguments = ["/Create", "/F", "/TN", windows_task_name(task), "/TR", command]
    if task.schedule == SCHEDULE_INTERVAL:
        minutes = task.every_minutes
        if not 1 <= minutes <= MAX_INTERVAL_MINUTES:
            raise ValueError(f"Windows repeats a task every 1 to {MAX_INTERVAL_MINUTES} minutes; use a daily time for longer")
        return arguments + ["/SC", "MINUTE", "/MO", str(minutes)]
    if task.schedule == SCHEDULE_DAILY:
        at = parse_time_of_day(task.daily_at)
        if at is None:
            raise ValueError("The time of day is not in the form HH:MM")
        return arguments + ["/SC", "DAILY", "/ST", f"{at[0]:02d}:{at[1]:02d}"]
    raise ValueError("Windows needs a schedule: every so many minutes, or every day at a time")


def _schtasks(arguments: list[str]) -> str:
    """Run schtasks.exe; returns its complaint, empty when it succeeded."""
    try:
        done = subprocess.run(
            ["schtasks", *arguments], capture_output=True, text=True, creationflags=NO_WINDOW, timeout=30, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"Could not run schtasks: {exc}"
    if done.returncode == 0:
        return ""
    return (done.stderr or done.stdout).strip().removeprefix("ERROR:").strip() or f"schtasks failed ({done.returncode})"


def sync_with_windows(task: Task) -> str:
    """Register the task with Windows, or remove it there, to match its settings; returns a problem or ""."""
    wanted = task.enabled and task.runner == RUNNER_WINDOWS
    name = windows_task_name(task) if wanted else ""
    if task.registered_as and task.registered_as != name:
        _schtasks(["/Delete", "/F", "/TN", task.registered_as])  # renamed, moved back to the app or turned off
        task.registered_as = ""
    if not wanted:
        return ""
    try:
        arguments = schtasks_create_arguments(task)
    except ValueError as exc:
        return str(exc)
    problem = _schtasks(arguments)
    if not problem:
        task.registered_as = name
    return problem


def unregister(task: Task) -> None:
    if task.registered_as:
        _schtasks(["/Delete", "/F", "/TN", task.registered_as])
        task.registered_as = ""


# ---- running while the application is open --------------------------------------------


class TaskRun(QThread):
    """One run of a task, off the UI thread."""

    done = pyqtSignal(object, object)  # Task, TaskResult

    def __init__(self, task: Task, sessions: SessionStore, parent=None):
        super().__init__(parent)
        self.task = task
        self._sessions = sessions
        self._stop = False

    def stop(self) -> None:
        self._stop = True

    def run(self) -> None:
        try:
            result = run_task(self.task, self._sessions, lambda: self._stop)
        except Exception as exc:  # never lose the window over one task
            result = TaskResult(False, f"{exc.__class__.__name__}: {exc}")
        self.done.emit(self.task, result)


class TaskScheduler(QObject):
    """Starts each task of the store when its time comes, and reports how runs went."""

    started = pyqtSignal(object)  # Task
    finished = pyqtSignal(object, object)  # Task, TaskResult
    CHECK_MS = 15_000

    def __init__(self, tasks: TaskStore, sessions: SessionStore, parent=None):
        super().__init__(parent)
        self.tasks = tasks
        self.sessions = sessions
        self._runs: dict[str, TaskRun] = {}
        self._chains: dict[str, tuple[str, ...]] = {}  # per running task: the ids that ran before it in its series
        self._timer = QTimer(self)
        self._timer.setInterval(self.CHECK_MS)
        self._timer.timeout.connect(self.check)
        self._timer.start()

    def check(self, now: float | None = None) -> list[Task]:
        """Start the tasks that are due; returns them."""
        due = [task for task in self.tasks.tasks if task.id not in self._runs and is_due(task, now)]
        for task in due:
            self.run(task)
        return due

    def is_running(self, task_id: str) -> bool:
        return task_id in self._runs

    def run(self, task: Task, chain: tuple[str, ...] = ()) -> bool:
        """Run a task now, whatever its schedule; False if it is running already."""
        if task.id in self._runs:
            return False
        self._chains[task.id] = chain
        started = time.time()
        task.last_run = started  # at once, so that the next check does not start it again
        run = TaskRun(task, self.sessions)
        run.done.connect(lambda done_task, result: self._finished(done_task, result, started))
        self._runs[task.id] = run
        run.start()
        self.started.emit(task)
        return True

    def _finished(self, task: Task, result: TaskResult, started: float) -> None:
        run = self._runs.pop(task.id, None)
        if run is not None:
            threads.park(run)
        task.last_result, task.last_ok = result.summary, result.ok
        self.tasks.record(task.id, started, result)
        chain = self._chains.pop(task.id, ())
        self.finished.emit(task, result)
        following = chained_after(self.tasks, task, chain) if result.ok else None
        if following is not None:
            self.run(following, (*chain, task.id))

    def shutdown(self) -> None:
        self._timer.stop()
        for run in self._runs.values():
            run.done.disconnect()
            run.stop()
            threads.park(run)
        self._runs.clear()
