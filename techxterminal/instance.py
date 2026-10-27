"""One copy of the program at a time.

The first copy listens on a local socket named after its data folder. A second one, started from the Start
menu, a shortcut or with sessions named on its command line, finds it there, hands over those sessions and
asks for the window to come to the front, then ends. The name is per data folder, which is per user (and per
portable copy), so two copies never write the same saved sessions at once.

The options that open no window (--list, --tasks, --run-task, --version) are not affected: Windows Task
Scheduler runs tasks with --run-task while the window may well be open.
"""

import hashlib
import json
import sys

from PyQt6.QtCore import QObject, pyqtSignal
from PyQt6.QtNetwork import QLocalServer, QLocalSocket

from .paths import CONFIG_DIR

TIMEOUT_MS = 1500


def server_name() -> str:
    folder = str(CONFIG_DIR).lower().encode("utf-8")
    return f"TechXTerminal-{hashlib.sha1(folder).hexdigest()[:16]}"


def hand_over(targets: list[str]) -> bool:
    """Pass the targets to a copy already running, which comes to the front; False if there is none."""
    socket = QLocalSocket()
    socket.connectToServer(server_name())
    if not socket.waitForConnected(TIMEOUT_MS):
        return False
    if sys.platform == "win32":
        import ctypes

        # Windows lets only the program in front give another the front; this one is, having just started.
        ctypes.windll.user32.AllowSetForegroundWindow(-1)  # ASFW_ANY
    socket.write(json.dumps({"open": targets}).encode("utf-8") + b"\n")
    socket.waitForBytesWritten(TIMEOUT_MS)
    socket.disconnectFromServer()
    return True


class InstanceServer(QObject):
    """Listens for later copies; `activated` carries the sessions each one was asked to open."""

    activated = pyqtSignal(list)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.server = QLocalServer(self)
        if not self.server.listen(server_name()):
            # On some systems a copy that crashed leaves its name behind; that one is gone, so take the name.
            QLocalServer.removeServer(server_name())
            self.server.listen(server_name())
        self.server.newConnection.connect(self._accept)

    def is_listening(self) -> bool:
        return self.server.isListening()

    def _accept(self) -> None:
        while (socket := self.server.nextPendingConnection()) is not None:
            buffer = bytearray()
            socket.readyRead.connect(lambda s=socket, b=buffer: self._read(s, b))
            socket.disconnected.connect(socket.deleteLater)

    def _read(self, socket: QLocalSocket, buffer: bytearray) -> None:
        buffer.extend(bytes(socket.readAll()))
        if b"\n" not in buffer:
            return
        line = bytes(buffer).split(b"\n", 1)[0]
        buffer.clear()
        try:
            targets = json.loads(line).get("open", [])
        except (ValueError, AttributeError):
            targets = []  # from something else on this computer that knows the name: just come to the front
        self.activated.emit([str(target) for target in targets if isinstance(target, str)])
