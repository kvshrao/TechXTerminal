import os
import sys
from pathlib import Path

from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import QApplication

from . import APP_NAME, __version__, errors, instance, titlebar
from .mainwindow import MainWindow
from .preferences import Preferences
from .theme import app_icon, apply_appearance, load_fonts


# Kept under this name for callers that look here; see errors.py.
unhandled_errors = errors.unhandled


def create_app(argv: list[str]) -> QApplication:
    errors.install()
    app = QApplication(argv)
    app.setApplicationName(APP_NAME)
    app.setStyle("Fusion")
    load_fonts()
    prefs = Preferences.load()
    apply_appearance(prefs.theme, prefs.accent, prefs.ui_font, prefs.ui_size, prefs.terminal_scheme)
    app.setWindowIcon(app_icon())
    titlebar.install(app)
    return app


def usage() -> str:
    program = Path(sys.argv[0]).name if getattr(sys, "frozen", False) else "python main.py"
    return f"""Usage: {program} [SESSION ...]

Each SESSION is opened in a tab at start. It can be the name of a saved session,
or an address: user@host[:port], telnet://host, rlogin://user@host, raw://host:port,
serial://COM3:115200. A shell on this computer: local (PowerShell, or else the first
shell found), local://cmd.exe, local://wsl.exe and so on.

  --list             print the names of the saved sessions and exit
  --tasks            print the scheduled tasks and exit
  --run-task TASK    run a scheduled task, by name or id, without opening the window;
                     its output goes to the task's output file. The exit status is 0
                     when every command succeeded, 1 when one failed, 2 for no such task
  --version          print the version and exit
  --help             show this text and exit
"""


def say(text: str) -> None:
    """Print for whoever started the program from a command line.

    A Windows program built without a console has nowhere to print, so its output is lost. Started from a
    console window, it borrows that console for the purpose.
    """
    if sys.stdout is None and os.name == "nt":
        import ctypes

        if ctypes.windll.kernel32.AttachConsole(-1):  # the console of the program that started this one
            sys.stdout = open("CONOUT$", "w", encoding="utf-8", errors="replace")  # noqa: SIM115 - kept for the run
    print(text)


def list_tasks() -> None:
    from .sessions import SessionStore
    from .tasks import SCHEDULE_DAILY, SCHEDULE_INTERVAL, TaskStore, target_name

    sessions = SessionStore()
    for task in TaskStore().tasks:
        when = {SCHEDULE_INTERVAL: f"every {task.every_minutes} min", SCHEDULE_DAILY: f"daily at {task.daily_at}"}
        schedule = when.get(task.schedule, "by hand") + ("" if task.enabled else " (paused)")
        say(f"{task.title}\t{target_name(task, sessions)}\t{schedule}\t{task.id}")


def run_task_by_name(wanted: str) -> int:
    """Run a scheduled task from the command line, as Windows Task Scheduler does; returns the exit status."""
    import time

    from .sessions import SessionStore
    from .tasks import TaskStore, run_task

    errors.install()  # a failure is written to the error log: there may be no one watching
    tasks = TaskStore()
    task = tasks.find(wanted) if wanted else None
    if task is None:
        say(f"No scheduled task named {wanted!r}. --tasks lists them." if wanted else "--run-task needs the name of a task")
        return 2
    started = time.time()
    result = run_task(task, SessionStore())
    tasks.record(task.id, started, result)
    say(f"{task.title}: {result.summary}" + (f"\nOutput: {result.path}" if result.path else ""))
    if not result.ok and task.notify_on_failure:
        notify_failure(task.title, result)
    return 0 if result.ok else 1


def notify_failure(title: str, result) -> None:
    """Tell whoever is at the desktop that a task run by Windows failed; a click on the message opens its output.

    Nothing else of the application is on screen in this case, so it waits a little for that click.
    """
    from PyQt6.QtCore import QUrl
    from PyQt6.QtGui import QDesktopServices

    from .notices import SHOW_MS, DesktopNotifier

    app = QApplication.instance() or QApplication(sys.argv[:1])
    app.setApplicationName(APP_NAME)

    def clicked():
        if result.path is not None:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(result.path)))
        app.quit()

    notifier = DesktopNotifier()
    if notifier.show(f"Task failed: {title}", result.summary, clicked):
        QTimer.singleShot(SHOW_MS + 2000, app.quit)
        app.exec()
    notifier.close()


def main() -> None:
    arguments = sys.argv[1:]
    if "--help" in arguments or "-h" in arguments:
        say(usage().rstrip("\n"))
        return
    if "--version" in arguments or "-V" in arguments:
        say(f"{APP_NAME} {__version__}")
        return
    if "--list" in arguments:
        from .sessions import SessionStore

        for session in sorted(SessionStore().sessions, key=lambda s: s.title.lower()):
            say(f"{session.title}\t{session.address}")
        return
    if "--tasks" in arguments:
        list_tasks()
        return
    if "--run-task" in arguments:
        position = arguments.index("--run-task") + 1
        sys.exit(run_task_by_name(arguments[position] if position < len(arguments) else ""))
    app = create_app(sys.argv)
    # Qt removes the options it understands from its own list; what is left names sessions to open.
    targets = [argument for argument in app.arguments()[1:] if not argument.startswith("-")]
    # One copy at a time: a copy already running opens these sessions and comes to the front instead.
    if instance.hand_over(targets):
        return
    server = instance.InstanceServer(app)
    if not server.is_listening() and instance.hand_over(targets):
        return  # another copy started at the same moment, and was first
    window = MainWindow()
    server.activated.connect(window.come_forward)
    window.show()
    if targets:
        QTimer.singleShot(0, lambda: window.open_targets(targets))
    sys.exit(app.exec())
