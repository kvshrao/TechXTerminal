"""Local shells: PowerShell, the Command Prompt, WSL or any other console program, in a tab of its own.

The program runs in a Windows pseudo console (ConPTY), so it sees a real terminal of the tab's size and its
output arrives with the same escape codes an SSH server would send.
"""

import os
import shlex
import shutil
import time
from pathlib import Path

from .connections import ConnectionWorker
from .sessions import PROTO_LOCAL, Session

READ_PAUSE = 0.01  # seconds between reads while the program is quiet


def _git_bash() -> str | None:
    for folder in (os.environ.get("ProgramFiles", ""), os.environ.get("ProgramFiles(x86)", "")):
        candidate = Path(folder) / "Git" / "bin" / "bash.exe"
        if folder and candidate.is_file():
            return str(candidate)
    return None


def available_shells() -> list[tuple[str, str]]:
    """(name, command line) for the shells found on this computer, the usual first."""
    found = []
    for name, program in (
        ("PowerShell", "pwsh.exe"),
        ("Windows PowerShell", "powershell.exe"),
        ("Command Prompt", "cmd.exe"),
        ("WSL", "wsl.exe"),
    ):
        if shutil.which(program):
            found.append((name, program))
    bash = _git_bash()
    if bash:
        found.append(("Git Bash", f'"{bash}" --login -i'))
    return found


def default_shell() -> str:
    shells = available_shells()
    return shells[0][1] if shells else "cmd.exe"


def shell_name(command_line: str) -> str:
    """What to call a shell in a tab: "PowerShell" for pwsh.exe, otherwise the program's own name."""
    for name, known in available_shells():
        if known.lower() == command_line.strip().lower():
            return name
    program = split_command_line(command_line)[0] if command_line.strip() else ""
    return Path(program).stem or "Shell"


def split_command_line(command_line: str) -> list[str]:
    """The program and its arguments, as Windows reads them; quotes keep a path with spaces together."""
    try:
        return [part.strip('"') for part in shlex.split(command_line, posix=False)] or [""]
    except ValueError:
        return [command_line.strip()]


def find_program(command_line: str) -> str | None:
    """The full path of the program a command line starts, or None if there is no such program."""
    program = split_command_line(command_line)[0]
    if not program:
        return None
    if Path(program).is_file():
        return str(Path(program))
    return shutil.which(program)


def start_folder(session: Session) -> Path:
    """The folder a local shell starts in: the session's own, with %VARIABLES% and ~ filled in, or the user's."""
    text = os.path.expandvars(session.start_folder.strip().strip('"'))
    return Path(text).expanduser() if text else Path.home()


def local_session(command_line: str = "", name: str = "", folder: str = "") -> Session:
    """An unsaved session that opens a shell on this computer, in the given folder or the user's own."""
    command_line = command_line.strip() or default_shell()
    return Session(
        name=name or shell_name(command_line), host=command_line, protocol=PROTO_LOCAL, port=22, start_folder=folder
    )


class LocalWorker(ConnectionWorker):
    """Runs a console program in a pseudo console; its output is the tab's input, and the other way round."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._pty = None

    def _open(self) -> None:
        try:
            from winpty import PTY
        except ImportError:
            raise RuntimeError("Local shells need the 'pywinpty' package (pip install pywinpty)") from None
        command_line = self.session.host
        program = find_program(command_line)
        if program is None:
            raise FileNotFoundError(f"Program not found: {split_command_line(command_line)[0]}")
        folder = start_folder(self.session)
        if not folder.is_dir():
            raise FileNotFoundError(f"The start folder is not there: {folder}")
        self.status.emit(f"Starting {command_line} in {folder} ...")
        pty = PTY(max(2, self._cols), max(2, self._rows))
        if not pty.spawn(program, command_line, str(folder)):
            raise RuntimeError(f"Could not start {command_line}")
        self._pty = pty

    def _read_loop(self) -> None:
        pty = self._pty
        while not self._stop:
            data = pty.read(blocking=False)
            if data:
                self.data_received.emit(data.encode("utf-8", "replace"))
                continue
            if not pty.isalive() or pty.iseof():
                break
            time.sleep(READ_PAUSE)
        if not self._stop:
            status = pty.get_exitstatus()
            if status:
                self.status.emit(f"{shell_name(self.session.host)} ended with exit code {status}")

    def send(self, data: bytes) -> None:
        pty = self._pty
        if pty is None:
            return
        try:
            pty.write(data.decode("utf-8", "replace"))
        except Exception:
            pass  # the program has just ended

    def resize(self, cols: int, rows: int) -> None:
        super().resize(cols, rows)
        pty = self._pty
        if pty is not None:
            try:
                pty.set_size(max(2, cols), max(2, rows))
            except Exception:
                pass

    def details(self) -> list[tuple[str, str]]:
        pty = self._pty
        return [
            ("Program", self.session.host),
            ("Started in", str(start_folder(self.session))),
            ("Process ID", str(pty.pid) if pty is not None and pty.pid else ""),
        ]

    def _interrupt(self) -> None:
        pass  # reads do not block, so the loop notices the stop flag by itself; _close ends the program

    def _close(self) -> None:
        pty, self._pty = self._pty, None
        if pty is None:
            return
        try:
            if pty.isalive() and pty.pid:
                os.kill(pty.pid, 9)  # TerminateProcess; dropping the pseudo console then closes it
        except Exception:
            pass

    def _describe(self, exc: Exception) -> str:
        return str(exc) or exc.__class__.__name__
