"""One tab: a terminal wired to a connection, with find, logging, file transfer and reconnect."""

import codecs
import re
import time
from datetime import datetime
from pathlib import Path

from PyQt6.QtCore import QSize, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QKeySequence, QShortcut
from PyQt6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSplitter,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from . import connections, responses
from .dialogs import ask_text
from .preferences import Preferences
from .sessions import (
    PROTO_RAW,
    PROTO_SERIAL,
    PROTO_SSH,
    PROTO_TELNET,
    PROTOCOLS,
    Session,
    SessionStore,
    apply_command_line,
)
from .sftp import FileBrowser, human_size
from .terminal import TerminalWidget
from .theme import line_icon, set_style_property

STATE_CONNECTING = "connecting"
STATE_CONNECTED = "connected"
STATE_DISCONNECTED = "disconnected"

# Automatic logon only reacts to prompts that show up shortly after connecting, so a
# saved password is never typed into some unrelated prompt later in the session.
LOGON_WINDOW_SECONDS = 30
_LOGIN_PROMPT = re.compile(r"(?<!last )(login|user ?name|user)\s*:\s*$", re.IGNORECASE)
_PASSWORD_PROMPT = re.compile(r"pass(word|phrase)[^:\n]*:\s*$", re.IGNORECASE)
_ESCAPES = re.compile(r"\x1b\[[0-9;?<>=]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[()][0-9A-Za-z]|\x1b[=>78DEHMNOZc]")
_CONTROLS = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")

# Seconds to wait before each automatic reconnect attempt.
RECONNECT_DELAYS = (3, 5, 10, 20, 30)
WATCH_WINDOW = 400  # characters of recent output searched for watched text


class SessionLog:
    """Writes a session's output to a text file with the terminal control codes removed."""

    def __init__(self, path: Path, timestamps: bool = False):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._timestamps = timestamps
        self._line_start = True  # the next character written begins a line
        self._file = open(path, "a", encoding="utf-8", newline="\n")
        self._decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self._carry = ""
        self._file.write(f"--- log started {datetime.now():%Y-%m-%d %H:%M:%S} ---\n")

    def write(self, data: bytes) -> None:
        text = self._carry + self._decoder.decode(data)
        # An escape sequence may be split across two packets; keep its start for next time.
        escape = text.rfind("\x1b")
        if escape >= 0 and len(text) - escape < 64 and not _ESCAPES.match(text, escape):
            text, self._carry = text[:escape], text[escape:]
        else:
            self._carry = ""
        text = _CONTROLS.sub("", _ESCAPES.sub("", text))
        if text:
            self._file.write(self._stamped(text) if self._timestamps else text)
            self._file.flush()

    def _stamped(self, text: str) -> str:
        """The text with the current time in front of every line that starts in it."""
        stamp = f"[{datetime.now():%H:%M:%S}] "
        out = []
        for piece in text.splitlines(keepends=True):
            out.append(stamp + piece if self._line_start else piece)
            self._line_start = piece.endswith("\n")
        return "".join(out)

    def close(self) -> None:
        try:
            self._file.write(f"\n--- log ended {datetime.now():%Y-%m-%d %H:%M:%S} ---\n")
            self._file.close()
        except (OSError, ValueError):
            pass


class FindBar(QFrame):
    """Search field shown above the terminal."""

    def __init__(self, terminal: TerminalWidget, parent=None):
        super().__init__(parent)
        self.setObjectName("FindBar")
        self.terminal = terminal
        self._themed: list = []

        self.query = QLineEdit()
        self.query.setPlaceholderText("Find in output")
        self.query.setClearButtonEnabled(True)
        self.count = QLabel()
        self.count.setObjectName("Muted")
        self.count.setMinimumWidth(70)
        self.case = QToolButton()
        self.case.setObjectName("Icon")
        self.case.setText("Aa")
        self.case.setCheckable(True)
        self.case.setToolTip("Match case")
        self.case.setAccessibleName("Match case")
        self.regex = QToolButton()
        self.regex.setObjectName("Icon")
        self.regex.setText(".*")
        self.regex.setCheckable(True)
        self.regex.setToolTip("Use a regular expression")
        self.regex.setAccessibleName("Use a regular expression")

        row = QHBoxLayout(self)
        row.setContentsMargins(10, 6, 8, 6)
        row.setSpacing(4)
        row.addWidget(self.query, 1)
        row.addWidget(self.count)
        row.addWidget(self.case)
        row.addWidget(self.regex)
        row.addWidget(self._button("chevron-up", "Previous match (Shift+Enter)", lambda: self.step(False)))
        row.addWidget(self._button("chevron-down", "Next match (Enter)", lambda: self.step(True)))
        row.addWidget(self._button("close", "Close (Esc)", self.close_bar))
        self.retheme()

        # Searching thousands of lines on every keystroke would stutter; wait for a pause.
        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(180)
        self._debounce.timeout.connect(self.search)
        self.query.textChanged.connect(lambda: self._debounce.start())
        self.case.toggled.connect(self.search)
        self.regex.toggled.connect(self.search)
        self.query.returnPressed.connect(lambda: self.step(True))
        for keys, handler in (("Shift+Return", lambda: self.step(False)), ("Escape", self.close_bar)):
            shortcut = QShortcut(QKeySequence(keys), self.query, activated=handler)
            shortcut.setContext(Qt.ShortcutContext.WidgetShortcut)
        self.hide()

    def _button(self, icon: str, tooltip: str, handler) -> QToolButton:
        button = QToolButton()
        button.setObjectName("Icon")
        button.setIconSize(QSize(16, 16))
        button.setToolTip(tooltip)
        button.setAccessibleName(tooltip.split(" (")[0])
        button.clicked.connect(handler)
        self._themed.append(lambda: button.setIcon(line_icon(icon)))
        return button

    def retheme(self) -> None:
        for refresh in self._themed:
            refresh()

    def open_bar(self) -> None:
        self.show()
        selected = self.terminal.selected_text()
        if selected and "\n" not in selected:
            self.query.setText(selected)
        self.query.setFocus()
        self.query.selectAll()
        self.search()

    def close_bar(self) -> None:
        self.hide()
        self.terminal.clear_find()
        self.terminal.setFocus()

    def search(self) -> None:
        self._debounce.stop()
        self.terminal.find(self.query.text(), self.case.isChecked(), self.regex.isChecked())
        self._show_count()

    def step(self, forward: bool) -> None:
        if self._debounce.isActive():
            self.search()
        self.terminal.find_step(forward)
        self._show_count()

    def _show_count(self) -> None:
        current, total = self.terminal.find_status()
        # The field turns red when what was typed finds nothing, as in most editors.
        nomatch = bool(self.terminal.find_error) or (bool(self.query.text()) and not total)
        set_style_property(self.query, "nomatch", nomatch)
        if self.terminal.find_error:
            self.count.setText("Invalid pattern")
            self.count.setToolTip(self.terminal.find_error)
            return
        self.count.setToolTip("")
        if not self.query.text():
            self.count.setText("")
        else:
            self.count.setText(f"{current} of {total}" if total else "No matches")


class TerminalTab(QWidget):
    stateChanged = pyqtSignal(str)
    titleChanged = pyqtSignal(str)
    transferLimitChanged = pyqtSignal(int)  # chosen in the file browser, KB/s
    transferSettingsRequested = pyqtSignal()  # the file browser's settings button was pressed
    activity = pyqtSignal()  # output arrived while this tab was in the background
    saveRequested = pyqtSignal()  # "Save Output..." was chosen in the terminal's menu
    saveCommandRequested = pyqtSignal(str)  # "Save Selection as Saved Command..." was chosen, with the text
    bell = pyqtSignal()  # the remote side rang the bell
    silenceDetected = pyqtSignal()  # output stopped while the tab was being watched for that
    textSeen = pyqtSignal(str)  # output contained the text the tab was being watched for
    responsesStopped = pyqtSignal()  # automatic responses answered too often and were turned off

    def __init__(self, session: Session, store: SessionStore, prefs: Preferences, password: str | None = None, parent=None):
        super().__init__(parent)
        self.session = session
        self.store = store
        self.prefs = prefs
        # Kept in memory only, so reconnecting this tab does not ask again.
        self._password = password
        self.state = STATE_DISCONNECTED
        self._worker: connections.ConnectionWorker | None = None
        self._effective = session  # the session after its command line options
        self._logon_user = ""
        self._logon_password = ""
        self._logon_tail = ""
        self._logon_deadline = 0.0
        self._log: SessionLog | None = None
        self.custom_title = ""  # a name given to this tab only; the session keeps its own
        self.in_broadcast = False  # marked to receive what the send bar broadcasts
        self.pinned = False  # kept at the left of its pane, and not closed by the bulk closes
        self.pinned = False  # kept at the left of its pane, and not closed by the bulk closes
        self.unseen_output = False  # cleared by the window when the tab is looked at
        self._files_remote_dir = "."
        self.bytes_in = 0  # traffic of the current connection
        self.bytes_out = 0
        self.connected_at = 0.0  # time.monotonic() when the connection came up, 0 while down
        self._files: FileBrowser | None = None
        self._was_connected = False
        self._retry = 0
        self._retry_at = 0.0
        self._retry_reason = ""
        self._closed = False  # set once the tab has been shut down for good
        self._retry_timer = QTimer(self)
        self._retry_timer.setInterval(500)
        self._retry_timer.timeout.connect(self._retry_tick)
        self._silence_timer = QTimer(self)
        self._silence_timer.setSingleShot(True)
        self._silence_timer.timeout.connect(self._silence)
        self._watching_silence = False
        self._watch_pattern: re.Pattern | None = None  # see watch_for_text()
        self._watch_text = ""
        self._watch_tail = ""
        self._responder: responses.AutoResponder | None = None  # built on each connect
        self.responses_paused = False  # chosen in the tab menu; kept across reconnects

        self.color_strip = QFrame()
        self.color_strip.setFixedHeight(3)

        self.banner = QFrame()
        self.banner.setObjectName("Banner")
        row = QHBoxLayout(self.banner)
        row.setContentsMargins(14, 8, 10, 8)
        self.banner_label = QLabel()
        self.banner_label.setTextFormat(Qt.TextFormat.PlainText)  # it can show what a server said when it hung up
        self.banner_label.setWordWrap(True)
        self.cancel_retry = QPushButton("Cancel")
        self.cancel_retry.clicked.connect(self._cancel_retry)
        self.cancel_retry.hide()
        reconnect = QPushButton("Reconnect")
        reconnect.setObjectName("Primary")
        reconnect.clicked.connect(self.connect_to_host)
        self.enter_hint = QLabel("or press Enter")  # Enter in a closed tab reconnects, which few would guess
        self.enter_hint.setObjectName("Muted")
        row.addWidget(self.banner_label, 1)
        row.addWidget(self.cancel_retry)
        row.addWidget(self.enter_hint)
        row.addWidget(reconnect)
        self.banner.hide()

        self.terminal = TerminalWidget(prefs)
        self.terminal.send.connect(self._on_input)
        self.terminal.sizeChanged.connect(self._on_resize)
        self.terminal.titleChanged.connect(self.titleChanged)
        self.terminal.findRequested.connect(self.show_find)
        self.terminal.saveRequested.connect(self.saveRequested)
        self.terminal.saveCommandRequested.connect(self.saveCommandRequested)
        self.terminal.bell.connect(self.bell)
        self.setFocusProxy(self.terminal)
        self.find_bar = FindBar(self.terminal)

        column = QWidget()
        column_layout = QVBoxLayout(column)
        column_layout.setContentsMargins(0, 0, 0, 0)
        column_layout.setSpacing(0)
        column_layout.addWidget(self.find_bar)
        column_layout.addWidget(self.terminal, 1)
        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        self.splitter.setHandleWidth(1)
        self.splitter.setChildrenCollapsible(False)
        self.splitter.addWidget(column)
        self.splitter.setStretchFactor(0, 1)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self.color_strip)
        layout.addWidget(self.banner)
        layout.addWidget(self.splitter, 1)
        self.set_session(session)

    def set_session(self, session: Session) -> None:
        """Adopt an edited session; connection settings apply from the next (re)connect."""
        self.session = session
        self.color_strip.setVisible(bool(session.color))
        self.color_strip.setStyleSheet(f"background: {session.color};" if session.color else "")

    def apply_preferences(self, prefs: Preferences) -> None:
        self.prefs = prefs
        self.terminal.apply_preferences(prefs)
        if self._files:
            self._files.apply_preferences(prefs)

    def retheme(self) -> None:
        self.terminal.retheme()
        self.find_bar.retheme()
        if self._files:
            self._files.retheme()

    @property
    def title(self) -> str:
        return self.custom_title or self.session.title

    @property
    def password(self) -> str | None:
        return self._password

    # ---- find, files, logging -------------------------------------------------

    def show_find(self) -> None:
        self.find_bar.open_bar()

    def sftp_opener(self):
        """A callable giving a file-transfer channel on this connection, or None if there is none."""
        return self._worker.open_sftp if self.can_browse_files() else None

    def can_browse_files(self) -> bool:
        return self.state == STATE_CONNECTED and hasattr(self._worker, "open_sftp")

    def command_runner(self):
        """A callable that runs a command on this connection out of sight, or None if there is none."""
        if self.state == STATE_CONNECTED and hasattr(self._worker, "run_command"):
            return self._worker.run_command
        return None

    def latency_meter(self):
        """A callable giving the connection's round trip in seconds, or None if it cannot be measured."""
        if self.state == STATE_CONNECTED and hasattr(self._worker, "round_trip"):
            return self._worker.round_trip
        return None

    def can_manage_tunnels(self) -> bool:
        return self.state == STATE_CONNECTED and hasattr(self._worker, "add_tunnels")

    def running_tunnels(self) -> list[str]:
        return list(self._worker.tunnels_running) if self.can_manage_tunnels() else []

    def add_tunnels(self, text: str) -> tuple[list[str], list[str]]:
        """Start tunnels on the live connection; returns those that started and the problems."""
        if not self.can_manage_tunnels():
            return [], ["Tunnels can be started on a connected SSH tab only"]
        return self._worker.add_tunnels(text)

    def stop_tunnel(self, description: str) -> bool:
        return self.can_manage_tunnels() and self._worker.stop_tunnel(description)

    def can_send_break(self) -> bool:
        return self.state == STATE_CONNECTED and self._worker is not None and self._worker.can_send_break()

    def send_break(self) -> bool:
        """Send a break signal, which routers and serial consoles use to interrupt or enter a boot menu."""
        return self.can_send_break() and self._worker.send_break()

    def files_visible(self) -> bool:
        return self._files is not None

    def toggle_files(self) -> None:
        """Open the file browser window for this session, or close it if it is open."""
        if self._files:
            self._close_files()
        elif self.can_browse_files():
            self._files = FileBrowser(
                self._worker.open_sftp,
                self.title,
                self.prefs.transfer_limit_kbps,
                self.window(),
                start_remote=self._files_remote_dir,
                bookmark_key=self.session.id if self.store.get(self.session.id) else "",
                prefs=self.prefs,
            )
            self._files.closed.connect(self._close_files)
            self._files.speedLimitChanged.connect(self.transferLimitChanged)
            self._files.settingsRequested.connect(self.transferSettingsRequested)
            self._files.terminalRequested.connect(self.cd_to)
            self._files.show()
            self._files.raise_()
            self._files.activateWindow()

    def cd_to(self, folder: str) -> bool:
        """Type `cd` to a remote folder at the shell prompt of this tab and bring the tab's window forward.

        The folder name comes from the server, so it is quoted for the shell, and one with a control
        character in it (a newline would run whatever follows) is refused.
        """
        if self.state != STATE_CONNECTED:
            return False
        if any(ord(ch) < 32 or ord(ch) == 127 for ch in folder):
            self.terminal.write_notice("Not changing to a folder whose name has control characters", color=91)
            return False
        self.terminal.send_text("cd -- '" + folder.replace("'", "'\\''") + "'\n")
        window = self.window()
        window.raise_()
        window.activateWindow()
        self.terminal.setFocus()
        return True

    def _close_files(self) -> None:
        browser, self._files = self._files, None
        if browser:
            self._files_remote_dir = browser.directory or "."  # reopen in the same remote folder
            browser.shutdown()
            browser.hide()
            browser.deleteLater()

    def is_logging(self) -> bool:
        return self._log is not None

    def log_path(self) -> Path | None:
        return self._log.path if self._log else None

    def default_log_path(self) -> Path:
        name = re.sub(r"[^\w.-]+", "_", self.title).strip("_") or "session"
        return self.prefs.log_directory() / f"{name}-{datetime.now():%Y%m%d-%H%M%S}.log"

    def start_logging(self, path: Path | None = None) -> bool:
        self.stop_logging()
        try:
            self._log = SessionLog(path or self.default_log_path(), self.prefs.log_timestamps)
        except OSError as exc:
            self.terminal.write_notice(f"Could not open the log file: {exc.strerror or exc}", color=91)
            return False
        self.terminal.write_notice(f"Logging to {self._log.path}")
        return True

    def stop_logging(self) -> None:
        log, self._log = self._log, None
        if log:
            log.close()
            self.terminal.write_notice(f"Stopped logging to {log.path}")

    # ---- connection lifecycle ---------------------------------------------

    def connect_to_host(self) -> None:
        if self._closed:
            return  # the tab was closed before a connect that was still waiting to start
        self._stop_retry_timer()
        self._drop_worker()
        self.banner.hide()
        self._set_state(STATE_CONNECTING)

        options = apply_command_line(self.session)
        self._effective = session = options.session
        if options.ignored:
            self.terminal.write_notice("Ignored command line options: " + " ".join(options.ignored), color=93)
        password = options.password or self._password or self.store.get_password(self.session)

        # Either the password is part of the SSH login, or it is typed at a prompt.
        in_login = session.protocol == PROTO_SSH and session.password_in_command_line
        self._logon_password = "" if in_login else (password or "")
        self._logon_user = session.username if session.protocol in (PROTO_RAW, PROTO_TELNET, PROTO_SERIAL) else ""
        self._logon_tail = ""
        self.terminal.configure_session(session.encoding, session.backspace, session.enter)

        worker = connections.create_worker(
            session,
            password if in_login else None,
            self.terminal.cols,
            self.terminal.rows,
            options.compress,
            keepalive=self.prefs.keepalive_seconds,
        )
        worker.status.connect(self.terminal.write_notice)
        worker.prompt.connect(self._on_prompt)
        worker.connected.connect(self._on_connected)
        worker.data_received.connect(self._on_data)
        worker.closed.connect(self._on_closed)
        self._worker = worker
        worker.start()
        self.terminal.setFocus()

    def disconnect_from_host(self) -> None:
        self._stop_retry_timer()
        self._retry = 0
        self._was_connected = False
        if self._worker is None:
            return
        self._drop_worker()
        self.terminal.write_notice("Disconnected.")
        self._show_banner("Disconnected.")
        self._set_state(STATE_DISCONNECTED)

    def shutdown(self) -> None:
        self._closed = True
        self._stop_retry_timer()
        self._drop_worker()
        log, self._log = self._log, None
        if log:
            log.close()

    def send_text(self, text: str) -> None:
        if self.state == STATE_CONNECTED:
            self.terminal.send_text(text)

    def _drop_worker(self) -> None:
        self._close_files()  # the file browser lives on this connection
        worker, self._worker = self._worker, None
        if worker is None:
            return
        for signal in (worker.status, worker.prompt, worker.connected, worker.data_received, worker.closed):
            try:
                signal.disconnect()
            except TypeError:
                pass
        connections.retire(worker)

    def _set_state(self, state: str) -> None:
        if state != STATE_CONNECTED:
            self.connected_at = 0.0
        self.state = state
        self.stateChanged.emit(state)

    def _show_banner(self, text: str) -> None:
        self.banner_label.setText(text)
        self.banner.show()

    # ---- automatic reconnect ------------------------------------------------

    def _schedule_retry(self, reason: str) -> bool:
        if not self.prefs.auto_reconnect or not self._was_connected or self._retry >= len(RECONNECT_DELAYS):
            return False
        self._retry_reason = reason
        self._retry_at = time.monotonic() + RECONNECT_DELAYS[self._retry]
        self._retry += 1
        self.cancel_retry.show()
        self._retry_timer.start()
        self._retry_tick()
        return True

    def _retry_tick(self) -> None:
        remaining = self._retry_at - time.monotonic()
        if remaining <= 0:
            self.connect_to_host()
            return
        self._show_banner(
            f"{self._retry_reason}  Reconnecting in {int(remaining) + 1} s "
            f"(attempt {self._retry} of {len(RECONNECT_DELAYS)})."
        )

    def _stop_retry_timer(self) -> None:
        self._retry_timer.stop()
        self.cancel_retry.hide()

    def _cancel_retry(self) -> None:
        self._stop_retry_timer()
        self._retry = 0
        self._was_connected = False
        self._show_banner(f"{self._retry_reason}  Automatic reconnect cancelled.")

    # ---- automatic logon ----------------------------------------------------

    def watch_for_silence(self, seconds: float = 5.0) -> None:
        """Report, once, when output has arrived and then stopped for this long.

        Useful for a long command: start it, turn this on, and get told when it is done.
        """
        self._watching_silence = True
        self._silence_timer.setInterval(int(seconds * 1000))
        self._silence_timer.start()

    def stop_watching(self) -> None:
        self._watching_silence = False
        self._silence_timer.stop()

    def is_watching(self) -> bool:
        return self._watching_silence

    def watch_for_text(self, wanted: str, regex: bool = False) -> bool:
        """Report, once, when output containing `wanted` arrives; case does not matter.

        Returns False if `wanted` is empty or not a valid regular expression.
        """
        if not wanted.strip():
            return False
        try:
            self._watch_pattern = re.compile(wanted if regex else re.escape(wanted), re.IGNORECASE)
        except re.error:
            return False
        self._watch_text = wanted
        self._watch_tail = ""
        return True

    def stop_watching_text(self) -> None:
        self._watch_pattern = None
        self._watch_text = ""

    def watched_text(self) -> str:
        """What output is being watched for, empty if nothing."""
        return self._watch_text

    def _check_watched(self, data: bytes) -> None:
        # Searched in a short window of recent text, so a word split between two packets still counts.
        text = _CONTROLS.sub("", _ESCAPES.sub("", data.decode(self._effective.encoding or "utf-8", "replace")))
        self._watch_tail = (self._watch_tail + text)[-WATCH_WINDOW:]
        match = self._watch_pattern.search(self._watch_tail)
        if match:
            self.stop_watching_text()
            self.textSeen.emit(match.group(0))

    def _silence(self) -> None:
        if self._watching_silence:
            self._watching_silence = False
            self.silenceDetected.emit()

    def _on_data(self, data: bytes) -> None:
        self.bytes_in += len(data)
        if self._watching_silence:
            self._silence_timer.start()  # still busy: the quiet period starts again
        self.terminal.feed(data)
        if not self.unseen_output and not self.isVisible():
            self.unseen_output = True
            self.activity.emit()
        if self._log:
            self._log.write(data)
        if self._watch_pattern is not None:
            self._check_watched(data)
        if self._responder is not None and not self.responses_paused:
            self._respond(data)
        if not (self._logon_user or self._logon_password):
            return
        if time.monotonic() > self._logon_deadline:
            self._logon_user = self._logon_password = ""
            return
        text = _ESCAPES.sub("", data.decode("utf-8", "replace"))
        self._logon_tail = (self._logon_tail + text)[-200:]
        if self._logon_user and _LOGIN_PROMPT.search(self._logon_tail):
            answer, self._logon_user = self._logon_user, ""
            self._type_after_wait(answer)
        elif self._logon_password and _PASSWORD_PROMPT.search(self._logon_tail):
            answer, self._logon_password = self._logon_password, ""
            self._type_after_wait(answer)

    # ---- automatic responses ------------------------------------------------------

    def has_responses(self) -> bool:
        """Whether this tab's session has automatic responses to give."""
        return bool(responses.parse_rules(self._effective.auto_responses)[0])

    def set_responses_paused(self, paused: bool) -> None:
        self.responses_paused = paused

    def _respond(self, data: bytes) -> None:
        encoding = self._effective.encoding or "utf-8"
        text = _ESCAPES.sub("", data.decode(encoding, "replace"))
        responder = self._responder
        for reply in responder.feed(text):
            self._on_input(reply.encode(encoding, "replace"))
        if responder.stopped:
            self._responder = None
            self.terminal.write_notice(
                "Automatic responses turned off: they were answering over and over. Reconnect to turn them on again.",
                color=93,
            )
            self.responsesStopped.emit()

    def _type_after_wait(self, text: str) -> None:
        self._logon_tail = ""
        worker = self._worker
        last_step = not (self._logon_user or self._logon_password)

        def type_it():
            if worker is not self._worker:  # the tab reconnected or closed meanwhile
                return
            worker.send(text.encode() + b"\r")
            if last_step:
                QTimer.singleShot(400, lambda: self._send_startup(worker))

        QTimer.singleShot(max(0, self._effective.wait_ms), type_it)

    def _send_startup(self, worker) -> None:
        command = self._effective.startup_command
        if command and worker is self._worker:
            worker.send(command.encode() + b"\r")

    # ---- worker signals ---------------------------------------------------

    def _on_connected(self) -> None:
        worker = self._worker
        if self._effective.protocol == PROTO_SSH and self._effective.password_in_command_line:
            self._password = worker.password
            if self.session.save_password and worker.password:
                self.store.set_password(self.session, worker.password)
        # The window may have been resized while the connection was being set up.
        worker.resize(self.terminal.cols, self.terminal.rows)
        self._logon_deadline = time.monotonic() + LOGON_WINDOW_SECONDS
        rules, _bad = responses.parse_rules(self._effective.auto_responses)
        self._responder = responses.AutoResponder(rules) if rules else None
        self._was_connected = True
        self._retry = 0
        self.bytes_in = self.bytes_out = 0
        self.connected_at = time.monotonic()
        if self.prefs.auto_log and not self._log:
            self.start_logging()
        self._set_state(STATE_CONNECTED)
        if not (self._logon_user or self._logon_password):
            self._send_startup(worker)

    def _on_closed(self, error: str) -> None:
        self._drop_worker()
        if error:
            self.terminal.write_notice(error, color=91)
            if not self._schedule_retry(error):
                self._show_banner(error)
        else:
            self._was_connected = False  # the remote side ended the session on purpose
            self.terminal.write_notice("Connection closed.")
            self._show_banner(f"Connection to {self.session.host} closed.")
        self._set_state(STATE_DISCONNECTED)

    def _on_prompt(self, kind: str, message: str) -> None:
        worker = self._worker
        if worker is None:
            return
        if kind == connections.PROMPT_HOSTKEY:
            box = QMessageBox(self.window())
            box.setIcon(QMessageBox.Icon.Warning)
            box.setWindowTitle("Unknown Host Key")
            box.setTextFormat(Qt.TextFormat.PlainText)
            box.setText(message)
            trust = box.addButton("Trust and Connect", QMessageBox.ButtonRole.AcceptRole)
            box.addButton(QMessageBox.StandardButton.Cancel)
            box.exec()
            answer = "yes" if box.clickedButton() is trust else None
        else:
            title = {connections.PROMPT_PASSPHRASE: "Key Passphrase", connections.PROMPT_INPUT: "Verification"}.get(
                kind, "Password"
            )
            text, ok = ask_text(self.window(), title, message, hidden=kind != connections.PROMPT_INPUT)
            answer = text if ok else None
        # The tab may have been closed or reconnected while the dialog was open. The old
        # connection still needs an answer so its thread can end, but nothing else is ours.
        if worker is not self._worker:
            worker.answer(None)
            return
        worker.answer(answer)
        self.terminal.setFocus()

    def _on_input(self, data: bytes) -> None:
        if self._worker is not None and self.state == STATE_CONNECTED:
            self.bytes_out += len(data)
            self._worker.send(data)
            if self._effective.local_echo:
                self._echo(data)
        elif self.state == STATE_DISCONNECTED and data in (b"\r", b"\r\n"):
            self.connect_to_host()  # Enter on a dead tab reconnects

    def _echo(self, data: bytes) -> None:
        """Show what was typed, for a host that does not echo. Control sequences are not shown."""
        if data.startswith(b"\x1b"):
            return
        shown = bytearray()
        for byte in data.replace(b"\r\n", b"\r"):
            if byte == 0x0D:
                shown += b"\r\n"
            elif byte in (0x08, 0x7F):
                shown += b"\b \b"
            elif byte >= 0x20:
                shown.append(byte)
        if shown:
            self.terminal.feed(bytes(shown))

    def connection_details(self) -> list[tuple[str, str]]:
        """Everything worth knowing about this tab's connection, as (label, value) rows."""
        session = self._effective
        rows = [
            ("Session", self.title),
            ("Protocol", dict(PROTOCOLS)[session.protocol]),
            ("Address", session.address),
            ("State", self.state.capitalize()),
        ]
        if self.connected_at:
            seconds = int(self.duration())
            since = datetime.now().timestamp() - seconds
            rows.append(
                (
                    "Connected for",
                    f"{seconds // 3600:02d}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"
                    f"  (since {datetime.fromtimestamp(since):%H:%M:%S})",
                )
            )
            rows.append(("Received / sent", f"{human_size(self.bytes_in)} / {human_size(self.bytes_out)}"))
        rows.append(("Terminal", f"{self.terminal.cols} × {self.terminal.rows}, {self.terminal.encoding().upper()}"))
        rows.append(("Log file", str(self._log.path) if self._log else "Not logging"))
        if self._worker is not None and self.state == STATE_CONNECTED:
            rows.extend(self._worker.details())
        return rows

    def duration(self) -> float:
        """Seconds the current connection has been up; 0 when not connected."""
        return time.monotonic() - self.connected_at if self.connected_at else 0.0

    def _on_resize(self, cols: int, rows: int) -> None:
        if self._worker is not None:
            self._worker.resize(cols, rows)
