"""Main window: session sidebar, tabs in up to four panes, quick connect and send bar."""

import getpass
import itertools
import re
import time
from datetime import datetime
from html import escape
from dataclasses import replace
from pathlib import Path

from PyQt6 import sip
from PyQt6.QtCore import QEvent, QPoint, QSize, QStringListModel, Qt, QTimer, QUrl
from PyQt6.QtGui import QCursor, QDesktopServices, QGuiApplication, QIcon, QKeySequence, QPixmap, QShortcut
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QCompleter,
    QDialog,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSplitter,
    QStackedWidget,
    QTabBar,
    QTabWidget,
    QToolButton,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
    QWidgetAction,
)

from . import APP_NAME, CAPTION, __version__, backup, connections, errors, importers, keys, threads
from .dialogs import (
    BackupPassphraseDialog,
    CommandValuesDialog,
    ConnectionInfoDialog,
    HostKeysDialog,
    KeyDialog,
    SessionDialog,
    SettingsDialog,
    ShortcutsDialog,
    SnippetsDialog,
    TasksDialog,
    TunnelsDialog,
    ask_text,
)
from . import monitor
from .hostkeys import approved_keys, forget_host
from .local import available_shells, local_session
from .monitor import StatsProbe
from .monitorview import Sample, ServerMonitorWindow, new_history
from .notices import DesktopNotifier
from .preferences import Preferences, open_settings
from .screens import fit_to_screen, keep_on_screen
from .sftp import human_size
from .sessions import KNOWN_HOSTS_FILE, AUTH_AGENT, DEFAULT_PORTS, PROTO_LOCAL, PROTO_SERIAL, PROTO_SSH, PROTOCOLS, Session, SessionStore
from .snippets import Snippet, SnippetStore, fill_placeholders, fill_prompts, prompts_in
from .tasks import (
    SCHEDULE_MANUAL,
    Task,
    TaskResult,
    TaskScheduler,
    TaskStore,
    next_run,
    sync_with_windows,
    unregister,
)
from .workspaces import WorkspaceStore
from .switcher import KIND_ACTION, KIND_PINNED, KIND_SESSION, KIND_SNIPPET, KIND_TAB, KIND_WORKSPACE, QuickSwitcher
from .sidebar import (
    FAVOURITES,
    ROLE_COLOR,
    ROLE_FAVOURITE,
    ROLE_PINNED,
    ROLE_IS_GROUP,
    ROLE_SESSION_ID,
    ROLE_SUBTITLE,
    SessionDelegate,
    SessionTree,
)
from .tab import STATE_CONNECTED, STATE_CONNECTING, STATE_DISCONNECTED, TerminalTab
from .theme import (
    COLORS,
    THEME_DARK,
    THEME_LIGHT,
    THEME_SYSTEM,
    active_theme,
    app_icon,
    apply_appearance,
    badged_icon,
    icon_pixmap,
    line_icon,
    session_avatar,
    set_style_property,
    tab_icon,
)
from .untrusted import literal

_LOCAL_TARGET = re.compile(r"^local(?:://(?P<program>.*))?$", re.IGNORECASE)  # local, local://pwsh.exe
_QUICK_CONNECT = re.compile(
    r"^(?:ssh\s+)?(?:(?P<protocol>ssh|telnet|rlogin|raw|serial)://)?"
    r"(?:(?P<user>[^@\s]+)@)?(?P<host>[^:\s]+)(?::(?P<port>\d{1,7}))?$",
    re.IGNORECASE,
)
# Palette keys, looked up at use so that a theme switch takes effect at once.
MAX_SEND_FILE_BYTES = 1 << 20  # Send Text File: more than this is not something to type
_STATE_COLOR_KEYS = {STATE_CONNECTING: "amber", STATE_CONNECTED: "green", STATE_DISCONNECTED: "red"}
_STATE_RANK = {STATE_CONNECTED: 2, STATE_CONNECTING: 1, STATE_DISCONNECTED: 0}
# Every shortcut of the main window, in one place. The key bindings, menu hints, command
# palette, welcome page and shortcut reference all read from here, so they cannot disagree.
SHORTCUTS = {
    "new_session": ("Ctrl+Shift+N", "New session"),
    "switcher": ("Ctrl+Shift+P", "Go to a tab or session, or run a command"),
    "quick_connect": ("Ctrl+Shift+K", "Quick connect"),
    "search_sessions": ("Ctrl+Shift+S", "Search saved sessions"),
    "duplicate_tab": ("Ctrl+Shift+T", "Duplicate tab"),
    "pin_tab": ("Ctrl+Shift+I", "Pin or unpin the tab"),
    "tab_list": ("Ctrl+Shift+J", "List all open tabs"),
    "close_tab": ("Ctrl+Shift+W", "Close tab"),
    "reconnect": ("Ctrl+Shift+R", "Reconnect"),
    "next_tab": ("Ctrl+Tab", "Next tab"),
    "previous_tab": ("Ctrl+Shift+Tab", "Previous tab"),
    "find": ("Ctrl+Shift+F", "Find in output"),
    "files": ("Ctrl+Shift+E", "File browser"),
    "logging": ("Ctrl+Shift+L", "Start or stop logging to a file"),
    "move_pane": ("Ctrl+Shift+D", "Split / move to other pane"),
    "stack_panes": ("Ctrl+Shift+O", "Stack panes / side by side"),
    "sidebar": ("Ctrl+Shift+B", "Toggle sidebar"),
    "full_screen": ("F11", "Full screen"),
    "settings": ("Ctrl+,", "Settings"),
    "shortcuts": ("Ctrl+Shift+H", "Keyboard shortcuts"),
}
# The few shown on the welcome page; the rest are one shortcut away.
_WELCOME_SHORTCUTS = (
    "new_session", "switcher", "quick_connect", "duplicate_tab", "close_tab",
    "find", "files", "move_pane", "settings", "shortcuts",
)  # fmt: skip
# Grouping for the shortcut reference. Terminal keys are handled by the terminal itself.
_SHORTCUT_SECTIONS = (
    ("Sessions and tabs", ("new_session", "quick_connect", "search_sessions", "duplicate_tab", "pin_tab", "tab_list", "close_tab", "reconnect", "next_tab", "previous_tab")),
    ("Tools", ("switcher", "find", "files", "logging")),
    ("Layout", ("move_pane", "stack_panes", "sidebar", "full_screen")),
    ("Application", ("settings", "shortcuts")),
)
_EXTRA_TAB_KEYS = [("Alt+1 ... Alt+9", "Jump to a tab")]
_TERMINAL_KEYS = [
    ("Ctrl+Shift+C", "Copy"),
    ("Ctrl+Shift+V / Shift+Insert", "Paste"),
    ("Shift+PageUp / Shift+PageDown", "Scroll back and forward"),
    ("Shift+Home / Shift+End", "Start or end of the scrollback"),
    ("Ctrl+Plus / Ctrl+Minus / Ctrl+0", "Font size (also Ctrl+mouse wheel)"),
    ("Enter", "Reconnect a closed tab"),
]


def _spoken(tooltip: str) -> str:
    """A button's name for screen readers: its tooltip without the shortcut in brackets."""
    return tooltip.split(" (")[0].splitlines()[0]


def time_ago(seconds: float) -> str:
    """How long ago, in words: just now, 5 minutes ago, 3 hours ago, 2 days ago."""
    for size, unit in ((86400, "day"), (3600, "hour"), (60, "minute")):
        if seconds >= size:
            count = int(seconds // size)
            return f"{count} {unit}{'' if count == 1 else 's'} ago"
    return "just now"


def keys_for(action: str) -> str:
    return SHORTCUTS[action][0]


def with_keys(label: str, action: str) -> str:
    """A menu label with its shortcut shown on the right."""
    return f"{label}\t{keys_for(action)}"


# How long a status bar message stays, by kind; errors stay longer so they can be read.
NOTICE_MS = {"info": 5000, "success": 5000, "error": 8000}

# Window widths (in device-independent pixels) at which the layout adapts.
COMPACT_WIDTH = 900  # below: the sidebar collapses and is opened on demand
NARROW_WIDTH = 700  # below: labels in the top bar and send bar are dropped
MAX_PANES = 4  # tab panes that can sit side by side
NEW_TAB_MENU_ALL = 15  # the + menu lists every saved session up to this many
NEW_TAB_MENU_SHORTLIST = 8  # beyond that: favourites and recent sessions, at most this many
# A split pane smaller than this gets the other arrangement, if that one leaves more room.
MIN_PANE_COLUMNS, MIN_PANE_ROWS = 40, 8
SEND_HISTORY_LENGTH = 50
QUICK_HISTORY_LENGTH = 20  # addresses quick connect remembers and suggests
CLOSED_TABS_KEPT = 10  # closes that can be undone with Reopen Closed Tab
QUIT_NAMES_SHOWN = 4  # open sessions named when quitting; the rest are counted
SORT_NAME, SORT_RECENT = "name", "recent"  # orders of the session list
RECENT_SESSIONS = 6  # shown on the welcome page
PIN_MARK = "\u25C6 "  # before the name of a pinned tab
UNSEEN_MARK = " \u2022"  # shown after the name of a background tab with new output
CRAMPED_HEIGHT = 520  # below: the send bar is tucked away and the bars are made thinner
TOP_CONTROL_HEIGHT = 34  # quick connect, the tool tray and New Session in the top bar
TOP_CONTROL_HEIGHT_DENSE = 28
RAIL_WIDTH = 56  # the sidebar collapsed: a column of session tiles


class HistoryLineEdit(QLineEdit):
    """A line edit that recalls earlier entries with the Up and Down keys, like a shell."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.history: list[str] = []
        self._position = 0  # index into history; len(history) means "the line being typed"
        self._draft = ""

    def remember(self, text: str) -> None:
        if text and (not self.history or self.history[-1] != text):
            self.history.append(text)
            del self.history[:-SEND_HISTORY_LENGTH]
        self._position = len(self.history)
        self._draft = ""

    def set_history(self, entries: list[str]) -> None:
        self.history = [entry for entry in entries if entry][-SEND_HISTORY_LENGTH:]
        self._position = len(self.history)

    def keyPressEvent(self, event):
        step = {Qt.Key.Key_Up: -1, Qt.Key.Key_Down: 1}.get(event.key())
        if step is None or not self.history:
            return super().keyPressEvent(event)
        if self._position == len(self.history):
            self._draft = self.text()
        self._position = max(0, min(len(self.history), self._position + step))
        self.setText(self._draft if self._position == len(self.history) else self.history[self._position])


class QuickSuggestions(QStringListModel):
    """Quick connect's suggestions, each marked as an address typed before or a saved session."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.typed: set[str] = set()
        self._icons: dict[tuple[str, str], QIcon] = {}

    def set_suggestions(self, typed: list[str], saved: list[str]) -> None:
        self.typed = set(typed)
        self.setStringList(typed + [name for name in saved if name not in self.typed])

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if role != Qt.ItemDataRole.DecorationRole:
            return super().data(index, role)
        name = "clock" if super().data(index, Qt.ItemDataRole.DisplayRole) in self.typed else "server"
        key = (name, COLORS["muted"])  # by colour, so a change of theme draws them afresh
        if key not in self._icons:
            self._icons[key] = line_icon(name, COLORS["muted"])
        return self._icons[key]


class KeyHintEdit(QLineEdit):
    """A field with its keyboard shortcut shown as a key at the right while it is idle, as quick connect and
    the session search have."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.key_hint = QLabel(self)
        self.key_hint.setObjectName("KeyHint")
        self.key_hint.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.textChanged.connect(self._place_hint)

    def set_key_hint(self, keys: str) -> None:
        self.key_hint.setText(keys)
        self.key_hint.adjustSize()
        self._place_hint()

    def _place_hint(self) -> None:
        # Not while typing: the hint is for reaching the field, and the clear button needs the place.
        shown = bool(self.key_hint.text()) and not self.text() and not self.hasFocus()
        self.key_hint.setVisible(shown)
        hint = self.key_hint
        self.setTextMargins(0, 0, hint.width() + 8 if shown else 0, 0)
        hint.move(self.width() - hint.width() - 10, (self.height() - hint.height()) // 2)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._place_hint()

    def focusInEvent(self, event):
        super().focusInEvent(event)
        self._place_hint()

    def focusOutEvent(self, event):
        super().focusOutEvent(event)
        self._place_hint()


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.store = SessionStore()
        self.settings = open_settings()  # window size and layout
        self.prefs = Preferences.load()
        self.snippets = SnippetStore()
        self.workspaces = WorkspaceStore()
        self.tasks = TaskStore()
        self.scheduler = TaskScheduler(self.tasks, self.store, self)
        self.scheduler.finished.connect(self._task_finished)
        self.notifier = DesktopNotifier(self)
        self._reachable: dict[str, tuple[bool, str]] = {}  # results of the last connection check
        self._probe_many = None
        # Callables that repaint icons and coloured text; run again whenever the theme changes.
        self._themed: list = []
        self._tab_icons: dict[tuple[str, str, bool], object] = {}
        self._close_icon_cache: QIcon | None = None
        self._blank_icon: QIcon | None = None
        self._closed_tabs: list[list[tuple]] = []  # groups of (session, tab name, password, pinned), oldest first
        self._reordering = False  # a tab is being put in place by the window, not dragged
        self._closing_batch: list | None = None  # collects the tabs of a close-several in progress
        self._tab_counter = itertools.count()
        self._collapsed_groups: set[str] = set()
        self._compact = False
        self._sidebar_wanted = True  # the user's choice for wide windows
        self._was_maximized = False
        self._dense = False

        self.setWindowTitle(APP_NAME)
        self.setWindowIcon(app_icon())
        self.setMinimumSize(460, 320)
        fit_to_screen(self, 1240, 780)  # smaller on a small screen

        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        self.splitter.setHandleWidth(1)
        self.splitter.setChildrenCollapsible(False)
        self.splitter.addWidget(self._build_sidebar())
        self.splitter.addWidget(self._build_content())
        self.splitter.setStretchFactor(0, 0)
        self.splitter.setStretchFactor(1, 1)
        self.splitter.setSizes([270, 970])

        central = QWidget()
        layout = QVBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self._build_top_bar())
        body = QHBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)
        body.addWidget(self._build_rail())
        body.addWidget(self.splitter, 1)
        layout.addLayout(body, 1)
        self.setCentralWidget(central)

        self.status_left = QLabel()
        self.status_left.setTextFormat(Qt.TextFormat.RichText)
        self.status_left.installEventFilter(self)  # a click on it opens Connection Info
        self.status_right = QLabel()
        self.status_right.setTextFormat(Qt.TextFormat.RichText)  # the server monitor's figures are coloured
        self.status_right.installEventFilter(self)  # a click on the figures opens the server monitor
        self.statusBar().setSizeGripEnabled(False)
        self.statusBar().addWidget(self.status_left, 1)
        # A notice can offer one thing to do about it, such as reopening the tab just closed.
        self.notice_action = QPushButton()
        self.notice_action.setObjectName("NoticeAction")
        self.notice_action.setCursor(Qt.CursorShape.PointingHandCursor)
        self.notice_action.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self.notice_action.hide()
        self.notice_action.clicked.connect(self._run_notice_action)
        self._notice_callback = None
        self.statusBar().addPermanentWidget(self.notice_action)
        self.statusBar().addPermanentWidget(self.status_right)
        self.statusBar().messageChanged.connect(self._notice_changed)

        self._build_shortcuts()
        errors.listeners.append(self._internal_error)
        if self.store.load_error:
            QTimer.singleShot(0, lambda: self.notify(self.store.load_error, "error", 15000))
        # The status bar shows connection time and traffic, so it is refreshed every second.
        self._status_timer = QTimer(self)
        self._status_timer.setInterval(1000)
        self._status_timer.timeout.connect(self._update_status)
        self._status_timer.start()
        # The server monitor: the figures of the active SSH tab's server, asked for every few seconds.
        self._stats_probes: dict[int, StatsProbe] = {}  # by id() of the tab being measured
        self._background_timer = QTimer(self)
        self._background_timer.setInterval(monitor.BACKGROUND_INTERVAL_MS)
        self._background_timer.timeout.connect(self._probe_background)
        self._background_timer.start()
        self._monitor_window: ServerMonitorWindow | None = None
        self._stats_timer = QTimer(self)
        self._stats_timer.setInterval(monitor.INTERVAL_MS)
        self._stats_timer.timeout.connect(self._probe_server)
        self._stats_timer.start()
        QApplication.instance().focusChanged.connect(self._on_focus_changed)
        self._reload_tree()
        self._update_status()
        self._apply_send_bar()
        QGuiApplication.styleHints().colorSchemeChanged.connect(self._on_system_theme_changed)

        geometry = self.settings.value("geometry")
        if geometry:
            self.restoreGeometry(geometry)
            keep_on_screen(self)  # the monitor it was last on may be gone or smaller
        splitter_state = self.settings.value("splitter")
        if splitter_state:
            self.splitter.restoreState(splitter_state)
        self._apply_responsive()
        # Start where typing makes sense: the session list, or quick connect when there is none yet.
        (self.tree if self.store.sessions else self.quick).setFocus()
        if self.prefs.restore_tabs:
            QTimer.singleShot(0, self.restore_tabs)

    # ---- UI construction ----------------------------------------------------

    def notify(self, text: str, level: str = "info", duration: int | None = None, action=None) -> None:
        """Show a message in the status bar: "info", "success" or "error", each in its own colour.

        `duration` is in milliseconds; 0 keeps the message until the next one. `action` is an
        optional (label, callable) offered as a button for as long as the message shows.
        """
        self._set_notice_level(level)
        if duration is None:
            duration = NOTICE_MS.get(level, NOTICE_MS["info"]) * (2 if action else 1)  # time to reach the button
        self.statusBar().showMessage(text, duration)
        # After showMessage, which clears the previous message's button through _notice_changed.
        self._notice_callback = action[1] if action else None
        self.notice_action.setText(action[0] if action else "")
        self.notice_action.setVisible(bool(action))

    def _notice_changed(self, text: str) -> None:
        # The bar hides the connection state behind a message only if the state is on screen when
        # the message comes; one that comes while the window is hidden would be drawn over it.
        self.status_left.setVisible(not text)
        if not text:
            self._set_notice_level("info")
        self._notice_callback = None
        self.notice_action.hide()

    def _run_notice_action(self) -> None:
        callback, self._notice_callback = self._notice_callback, None
        self.statusBar().clearMessage()
        if callback:
            callback()

    def confirm(self, title: str, text: str, setting: str) -> bool:
        """Ask a question that a setting in Settings, Behaviour can turn off, offering to stop asking.

        "Don't ask again" takes effect only with Yes: No is not something to remember.
        """
        box = QMessageBox(
            QMessageBox.Icon.Question,
            title,
            text,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            self,
        )
        box.setDefaultButton(QMessageBox.StandardButton.Yes)
        stop_asking = QCheckBox("Don't ask again", box)  # parented, or Python frees it under the box
        box.setCheckBox(stop_asking)
        if box.exec() != QMessageBox.StandardButton.Yes:
            return False
        if stop_asking.isChecked():
            self.prefs = replace(self.prefs, **{setting: False})
            self.prefs.save()
            self.notify(
                "No longer asking; Settings, Behaviour turns it back on",
                action=("Settings", lambda: self.open_settings("behaviour")),
            )
        return True

    def _set_notice_level(self, level: str) -> None:
        set_style_property(self.statusBar(), "level", level)

    def _themed_call(self, refresh) -> None:
        """Run a paint-in-theme-colours step now and again after every theme change."""
        self._themed.append(refresh)
        refresh()

    def _icon_button(self, icon: str, tooltip: str, handler) -> QToolButton:
        button = QToolButton()
        button.setObjectName("Icon")
        self._themed_call(lambda: button.setIcon(line_icon(icon)))
        button.setIconSize(QSize(18, 18))
        button.setToolTip(tooltip)
        button.setAccessibleName(_spoken(tooltip))
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.clicked.connect(handler)
        return button

    def _build_top_bar(self) -> QWidget:
        bar = QFrame()
        bar.setObjectName("TopBar")
        row = self._top_row = QHBoxLayout(bar)
        row.setContentsMargins(12, 10, 12, 10)
        row.setSpacing(8)

        toggle = self._icon_button("sidebar", f"Toggle sidebar ({keys_for('sidebar')})", self.toggle_sidebar)
        # The same tile as the taskbar and the start page, so the window is recognisably the app.
        self.logo = QLabel()
        self.logo.setToolTip(f"About {APP_NAME} {__version__}")
        self.logo.setCursor(Qt.CursorShape.PointingHandCursor)
        self.logo.installEventFilter(self)  # a click on it opens About
        self._size_logo(28)
        self.brand_text = QLabel(APP_NAME)
        self.brand_text.setObjectName("Brand")
        self.caption = QLabel(CAPTION)
        self.caption.setObjectName("Caption")
        self.caption.setToolTip(f"{APP_NAME} {__version__}")
        names = QVBoxLayout()  # the caption sits just below the name
        names.setSpacing(0)
        names.addStretch(1)
        names.addWidget(self.brand_text)
        names.addWidget(self.caption)
        names.addStretch(1)
        brand = QHBoxLayout()
        brand.setSpacing(10)
        brand.addWidget(self.logo, 0, Qt.AlignmentFlag.AlignVCenter)
        brand.addLayout(names)

        self.quick = KeyHintEdit()
        self.quick.setObjectName("QuickConnect")
        self.quick.setPlaceholderText("Quick connect   user@host[:port]")
        self.quick.setToolTip(
            "user@host or user@host:port for SSH.\n"
            "Other protocols: telnet://host, rlogin://user@host, raw://host:port, serial://COM3:115200\n"
            "A shell on this computer: local, or local://cmd.exe, local://wsl.exe and so on"
        )
        self.quick.setClearButtonEnabled(True)
        quick_icon = self.quick.addAction(QIcon(), QLineEdit.ActionPosition.LeadingPosition)
        self._themed_call(lambda: quick_icon.setIcon(line_icon("send", COLORS["muted"])))
        quick_icon.triggered.connect(self._quick_connect)
        self.quick.setMinimumWidth(120)
        self.quick.setMaximumWidth(460)
        self.quick.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.quick.returnPressed.connect(self._quick_connect)
        self._quick_model = QuickSuggestions(self)
        completer = QCompleter(self._quick_model, self.quick)
        completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        completer.setFilterMode(Qt.MatchFlag.MatchContains)
        completer.popup().setObjectName("CompleterPopup")
        completer.popup().setIconSize(QSize(16, 16))
        self.quick.setCompleter(completer)

        self.shell_button = self._icon_button("terminal", "Open a shell on this computer", self._local_shell_menu)
        self.tasks_button = self._icon_button("clock", "Scheduled tasks", self.edit_tasks)
        self._themed_call(self._paint_tasks_button)
        self.scheduler.started.connect(lambda _task: self._paint_tasks_button())
        self.scheduler.finished.connect(lambda _task, _result: self._paint_tasks_button())
        self.theme_button = self._icon_button("moon", "Switch theme", self.toggle_theme)  # see _paint_theme_button
        self._themed_call(self._paint_theme_button)
        settings = self._icon_button("settings", f"Settings ({keys_for('settings')})", self.open_settings)
        self.new_button = QPushButton("New Session")
        self.new_button.setObjectName("Primary")
        self._themed_call(lambda: self.new_button.setIcon(line_icon("plus", COLORS["on_accent"])))
        self.new_button.setIconSize(QSize(14, 14))
        self.new_button.setToolTip(f"New session ({keys_for('new_session')})")
        self.new_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.new_button.setFocusPolicy(Qt.FocusPolicy.TabFocus)  # a click must not take focus from the terminal
        self.new_button.clicked.connect(self.new_session)

        # The tools share one rounded tray, which sets them apart from New Session, the main action.
        tools = QFrame()
        tools.setObjectName("TopTools")
        tray = QHBoxLayout(tools)
        tray.setContentsMargins(3, 0, 3, 0)
        tray.setSpacing(2)
        for button in (self.shell_button, self.tasks_button, self.theme_button, settings):
            button.setIconSize(QSize(17, 17))
            tray.addWidget(button)
        # Quick connect, the tray and New Session are one height, so the bar reads as a single line.
        self._top_controls = (self.quick, tools, self.new_button)
        self._size_top_controls(TOP_CONTROL_HEIGHT)

        # The two sides share the room left over equally, so quick connect stays in the middle of
        # the window until one side needs more than half of it.
        left = QHBoxLayout()
        left.setSpacing(8)
        left.addWidget(toggle)
        left.addSpacing(6)
        left.addLayout(brand)
        left.addStretch(1)
        right = QHBoxLayout()
        right.setSpacing(8)
        right.addStretch(1)
        right.addWidget(tools)
        right.addSpacing(2)
        right.addWidget(self.new_button)

        row.addLayout(left, 1)
        row.addSpacing(12)
        row.addWidget(self.quick, 2)
        row.addSpacing(12)
        row.addLayout(right, 1)
        self.top_bar = bar
        return bar

    def _size_top_controls(self, height: int) -> None:
        for widget in self._top_controls:
            widget.setFixedHeight(height)
        for button in self._top_controls[1].findChildren(QToolButton):
            button.setFixedSize(height - 6, height - 6)

    def _paint_tasks_button(self) -> None:
        """The clock says at a glance whether tasks are scheduled, one is running, or one last failed."""
        scheduled = [task for task in self.tasks.tasks if task.enabled and task.schedule != SCHEDULE_MANUAL]
        running = [task for task in self.tasks.tasks if self.scheduler.is_running(task.id)]
        failed = [task for task in scheduled if task.last_run and not task.last_ok]
        if running:
            badge, status = COLORS["amber"], f"“{running[0].title}” is running"
        elif failed:
            badge, status = COLORS["red"], f"The last run of “{failed[0].title}” failed: {failed[0].last_result}"
        elif scheduled:
            badge, status = COLORS["accent"], f"{len(scheduled)} scheduled"
            upcoming = sorted((when, task.title) for task in scheduled if (when := next_run(task)) is not None)
            if upcoming:
                when, title = upcoming[0]
                today = datetime.fromtimestamp(when).date() == datetime.now().date()
                status += f"; next, “{title}” at {datetime.fromtimestamp(when).strftime('%H:%M' if today else '%a %H:%M')}"
        else:
            badge, status = "", ""
        self.tasks_button.setIcon(badged_icon("clock", badge) if badge else line_icon("clock"))
        tooltip = f"Scheduled tasks\n{status}" if status else "Scheduled tasks"
        self.tasks_button.setToolTip(tooltip)
        self.tasks_button.setAccessibleName(f"Scheduled tasks. {status}" if status else "Scheduled tasks")

    def _paint_theme_button(self) -> None:
        light = active_theme() == THEME_LIGHT
        self.theme_button.setIcon(line_icon("moon" if light else "sun"))
        tooltip = "Switch to the dark theme" if light else "Switch to the light theme"
        self.theme_button.setToolTip(tooltip)
        self.theme_button.setAccessibleName(_spoken(tooltip))

    def _size_logo(self, size: int) -> None:
        self.logo.setPixmap(app_icon().pixmap(size, size))
        self.logo.setFixedSize(size, size)

    def _build_sidebar(self) -> QWidget:
        self.sidebar = QFrame()
        self.sidebar.setObjectName("Sidebar")
        self.sidebar.setMinimumWidth(200)
        self.sidebar.setMaximumWidth(460)
        layout = QVBoxLayout(self.sidebar)
        layout.setContentsMargins(10, 14, 10, 10)
        layout.setSpacing(10)

        header = QHBoxLayout()
        header.setContentsMargins(6, 0, 0, 0)
        title = QLabel("SESSIONS")
        title.setObjectName("SectionTitle")
        header.addWidget(title, 1)
        self.import_button = self._icon_button("import", "Import, export and back up", self._import_menu)
        header.addWidget(self.import_button)
        self.sort_button = self._icon_button("sort", "Order of the sessions", self._sort_menu)
        header.addWidget(self.sort_button)
        header.addWidget(self._icon_button("plus", f"New session ({keys_for('new_session')})", self.new_session))

        self.search = KeyHintEdit()
        self.search.setObjectName("SessionSearch")
        self.search.setPlaceholderText("Search sessions")
        self.search.setClearButtonEnabled(True)
        search_icon = self.search.addAction(QIcon(), QLineEdit.ActionPosition.LeadingPosition)
        self._themed_call(lambda: search_icon.setIcon(line_icon("search", COLORS["muted"])))
        self.search.set_key_hint(keys_for("search_sessions"))
        self.search.setToolTip(
            f"{keys_for('search_sessions')}\nEnter opens the first match, Down moves into the list, Esc clears"
        )
        self.search.textChanged.connect(self._reload_tree)
        self.search.installEventFilter(self)

        self.tree = SessionTree()
        self.tree.sessionDropped.connect(self._session_dropped)
        self.tree.setHeaderHidden(True)
        self.tree.setRootIsDecorated(False)
        self.tree.setIndentation(0)
        self.tree.setExpandsOnDoubleClick(False)
        self.tree.setMouseTracking(True)
        self.tree.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.tree.setItemDelegate(SessionDelegate(self.tree, self._session_state))
        self.tree.itemDelegate().toggle_favourite = self._toggle_favourite
        self.tree.itemDelegate().reachable_for = lambda session_id: (self._reachable.get(session_id) or (None,))[0]
        self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._tree_menu)
        self.tree.itemClicked.connect(self._toggle_group)
        self.tree.itemDoubleClicked.connect(lambda item, _col: self._open_item(item))
        # The same keys as in the file browser: Enter opens, F2 edits, Delete deletes.
        tree_keys = (
            (Qt.Key.Key_Return, lambda: self._open_item(self.tree.currentItem())),
            (Qt.Key.Key_Enter, lambda: self._open_item(self.tree.currentItem())),
            (Qt.Key.Key_F2, lambda: self._with_current_session(self.edit_session)),
            (Qt.Key.Key_Delete, lambda: self._with_current_session(self._delete_session)),
        )
        for key, handler in tree_keys:
            shortcut = QShortcut(QKeySequence(key), self.tree, activated=handler)
            shortcut.setContext(Qt.ShortcutContext.WidgetShortcut)

        self.empty_hint = QLabel()
        self.empty_hint.setObjectName("Muted")
        self.empty_hint.setWordWrap(True)
        self.empty_hint.setContentsMargins(6, 4, 6, 4)

        layout.addLayout(header)
        layout.addWidget(self.search)
        layout.addWidget(self.empty_hint)
        layout.addWidget(self.tree, 1)
        return self.sidebar

    def _build_rail(self) -> QWidget:
        """What the sidebar collapses to: a slim column of the saved sessions, one tile each."""
        self.rail = QFrame()
        self.rail.setObjectName("SessionRail")
        self.rail.setFixedWidth(RAIL_WIDTH)
        self.rail.hide()  # shown in place of the sidebar, see _set_sidebar_visible
        column = QVBoxLayout(self.rail)
        column.setContentsMargins(0, 8, 0, 8)
        column.setSpacing(6)
        search = self._icon_button("search", f"Search sessions ({keys_for('search_sessions')})", self.focus_session_search)
        column.addWidget(search, 0, Qt.AlignmentFlag.AlignHCenter)
        tiles = QWidget()
        tiles.setObjectName("RailTiles")
        self._rail_column = QVBoxLayout(tiles)
        self._rail_column.setContentsMargins(0, 0, 0, 0)
        self._rail_column.setSpacing(4)
        self._rail_column.addStretch(1)
        scroll = QScrollArea()
        scroll.setObjectName("RailScroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)  # the wheel scrolls it
        scroll.setWidget(tiles)
        column.addWidget(scroll, 1)
        column.addWidget(self._icon_button("plus", f"New session ({keys_for('new_session')})", self.new_session), 0, Qt.AlignmentFlag.AlignHCenter)
        return self.rail

    def _rail_sessions(self) -> list[Session]:
        """The saved sessions in the sidebar's order, each once: favourites first, then by folder, or by recent use."""
        if self.sidebar_sort() == SORT_RECENT:
            return sorted(self.store.sessions, key=lambda s: (-self.last_connected(s), s.title.lower()))
        return sorted(self.store.sessions, key=lambda s: (not s.favourite, s.group == "", s.group.lower(), s.title.lower()))

    def _fill_rail(self) -> None:
        while self._rail_column.count() > 1:  # all but the closing stretch
            old = self._rail_column.takeAt(0).widget()
            old.setParent(None)  # off the rail at once, not when the event loop comes round to deleting it
            old.deleteLater()
        for number, session in enumerate(self._rail_sessions()):
            state = self._session_state(session.id)
            tile = QToolButton()
            tile.setObjectName("RailSession")
            tile.setIcon(
                session_avatar(session.title, session.color, COLORS[_STATE_COLOR_KEYS[state]] if state else "", session.pinned)
            )
            tile.setIconSize(QSize(34, 34))
            tile.setFixedSize(44, 44)
            tile.setCursor(Qt.CursorShape.PointingHandCursor)
            tile.setFocusPolicy(Qt.FocusPolicy.TabFocus)
            note = "\nOpens its tab pinned" if session.pinned else ""
            tile.setToolTip(literal(f"{session.title}\n{session.address}{note}"))  # names can come from an imported file
            tile.setAccessibleName(f"Open {session.title}")
            tile.clicked.connect(lambda _checked=False, s=session: self._rail_open(s))
            self._rail_column.insertWidget(number, tile, 0, Qt.AlignmentFlag.AlignHCenter)

    def _rail_open(self, session: Session) -> TerminalTab:
        """Go to the session's tab when one is open, else open it: the rail is for getting to a server."""
        tab = next((tab for tab in self.open_tabs() if tab.session.id == session.id), None)
        if tab is None:
            return self.open_session(session)
        pane = self._pane_of(tab)
        self.stack.setCurrentIndex(1)
        pane.setCurrentWidget(tab)
        self._set_active_pane(pane)
        return tab

    def _refresh_rail(self) -> None:
        if hasattr(self, "rail") and not self.rail.isHidden():
            self._fill_rail()

    def _set_sidebar_visible(self, show: bool) -> None:
        """Show the sidebar, or the rail of sessions in its place."""
        self.sidebar.setVisible(show)
        self.rail.setVisible(not show)
        if not show:
            self._fill_rail()

    def _build_content(self) -> QWidget:
        # Tab panes side by side. There is always the first one; splitting adds more,
        # and a pane goes away again when its last tab leaves.
        self.pane_splitter = QSplitter(
            Qt.Orientation.Vertical if self.prefs.stack_panes else Qt.Orientation.Horizontal
        )
        self.pane_splitter.setHandleWidth(1)
        self.pane_splitter.setChildrenCollapsible(False)
        self.pane_splitter.installEventFilter(self)  # its own resizes decide how split panes are arranged
        self.panes: list[QTabWidget] = []
        self._pane_columns: list[QSplitter] = []  # the two columns of the grid, while it is used
        self._active_pane: QTabWidget | None = None
        self.tabs = self._new_pane()
        self._active_pane = self.tabs

        corner = QWidget()
        corner.setObjectName("TabCorner")
        corner_row = QHBoxLayout(corner)
        corner_row.setContentsMargins(4, 6, 8, 0)
        self.add_tab_button = self._icon_button("plus", "Open a session in a new tab", self._new_tab_menu)
        corner_row.addWidget(self.add_tab_button)
        self.tab_list_button = self._icon_button(
            "chevron-down", f"List all open tabs ({keys_for('tab_list')})", self._tab_list_menu
        )
        corner_row.addWidget(self.tab_list_button)
        self.tabs.setCornerWidget(corner, Qt.Corner.TopRightCorner)

        self.command_bar = command_bar = QFrame()
        command_bar.setObjectName("CommandBar")
        row = QHBoxLayout(command_bar)
        row.setContentsMargins(12, 7, 12, 7)
        row.setSpacing(10)
        self.send_icon = QLabel()
        self.send_icon.setScaledContents(True)
        self.send_icon.setFixedSize(18, 18)
        self.command = HistoryLineEdit()
        saved_history = self.settings.value("send_history") or []
        self.command.set_history([saved_history] if isinstance(saved_history, str) else list(saved_history))
        self.command.setToolTip("Up and Down recall earlier commands")
        self.command.returnPressed.connect(self._send_command)
        self.all_tabs = QCheckBox("All tabs")
        self.all_tabs.setToolTip("Broadcast the command to every connected tab")
        self.all_tabs.toggled.connect(self._show_broadcast)
        self.all_tabs.setFocusPolicy(Qt.FocusPolicy.TabFocus)  # clicking it leaves the focus in the terminal
        self._themed_call(self._paint_send_icon)
        row.addWidget(self.send_icon)
        row.addWidget(self.command, 1)
        row.addWidget(self.all_tabs)
        self.snippet_button = self._icon_button("list", "Saved commands", self._snippet_menu)
        row.addWidget(self.snippet_button)

        tabs_page = QWidget()
        layout = QVBoxLayout(tabs_page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self.pane_splitter, 1)
        layout.addWidget(command_bar)

        self.stack = QStackedWidget()
        self.stack.setMinimumWidth(240)
        self.stack.addWidget(self._build_welcome())
        self.stack.addWidget(tabs_page)
        return self.stack

    def _make_pane(self) -> QTabWidget:
        pane = QTabWidget()
        pane.setDocumentMode(True)
        pane.setMovable(True)
        pane.setUsesScrollButtons(True)
        pane.setElideMode(Qt.TextElideMode.ElideRight)
        pane.setIconSize(QSize(22, 16))
        pane.currentChanged.connect(lambda _index: self._on_current_changed(pane))
        pane.tabBarClicked.connect(lambda _index: self._set_active_pane(pane))
        pane.tabBarDoubleClicked.connect(lambda index: self.rename_tab(pane.widget(index)) if index >= 0 else None)
        tab_bar = pane.tabBar()
        tab_bar.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        tab_bar.customContextMenuRequested.connect(lambda pos: self._tab_menu(pane, pos))
        tab_bar.installEventFilter(self)
        tab_bar.setAttribute(Qt.WidgetAttribute.WA_Hover)  # for the close button of the tab pointed at
        tab_bar.tabMoved.connect(lambda _from, _to: self._keep_pins_first(pane))
        if self._dense:
            set_style_property(tab_bar, "dense", True, children=True)
        return pane

    def _new_pane(self, after: QTabWidget | None = None) -> QTabWidget:
        pane = self._make_pane()
        index = self.panes.index(after) + 1 if after in self.panes else len(self.panes)
        self.panes.insert(index, pane)
        self._lay_out_panes()
        self._mark_active_pane()
        return pane

    def _grid_wanted(self) -> bool:
        return self.prefs.pane_grid and len(self.panes) >= 3

    def _lay_out_panes(self) -> None:
        """Put the panes, in their order, into a row or a column, or into a grid of two columns.

        In the grid the order reads left to right, then down: with three panes the first fills
        the left column and the others are stacked on the right; with four, two above two.
        """
        if self._grid_wanted():
            if not self._pane_columns:
                for _ in range(2):
                    column = QSplitter(Qt.Orientation.Vertical)
                    column.setHandleWidth(1)
                    column.setChildrenCollapsible(False)
                    self._pane_columns.append(column)
            left = [self.panes[0]] if len(self.panes) == 3 else [self.panes[0], self.panes[2]]
            right = [pane for pane in self.panes if pane not in left]
            self.pane_splitter.setOrientation(Qt.Orientation.Horizontal)
            for position, (column, members) in enumerate(zip(self._pane_columns, (left, right))):
                self.pane_splitter.insertWidget(position, column)
                for row, pane in enumerate(members):
                    column.insertWidget(row, pane)  # moves it out of wherever it was
                    pane.show()
                column.show()
            return
        for index, pane in enumerate(self.panes):
            self.pane_splitter.insertWidget(index, pane)
            pane.show()
        if self._pane_columns:
            for column in self._pane_columns:
                column.hide()
                column.setParent(None)
                column.deleteLater()
            self._pane_columns = []
            self.pane_splitter.setOrientation(
                Qt.Orientation.Vertical if self.prefs.stack_panes else Qt.Orientation.Horizontal
            )

    def panes_in_grid(self) -> bool:
        return bool(self._pane_columns)

    def _remove_pane(self, pane: QTabWidget) -> None:
        self.panes.remove(pane)
        pane.hide()
        pane.setParent(None)
        pane.deleteLater()

    def _balance_panes(self) -> None:
        if self._pane_columns:
            self.pane_splitter.setSizes([max(1, self.pane_splitter.width() // 2)] * 2)
            for column in self._pane_columns:
                column.setSizes([max(1, self.pane_splitter.height() // column.count())] * column.count())
            return
        stacked = self.pane_splitter.orientation() == Qt.Orientation.Vertical
        extent = self.pane_splitter.height() if stacked else self.pane_splitter.width()
        share = max(1, extent // len(self.panes))
        self.pane_splitter.setSizes([share] * len(self.panes))

    def _pane_room(self, orientation) -> tuple[int, int]:
        """Columns and rows each pane's terminal would get with the panes arranged this way."""
        tab = self.current_tab()
        if tab is None:
            return 0, 0
        cell_width, cell_height = tab.terminal.cell_size()
        count = len(self.panes)
        width, height = self.pane_splitter.width(), self.pane_splitter.height()
        tabs_height = self.tabs.tabBar().sizeHint().height()
        if orientation == Qt.Orientation.Horizontal:
            width //= count
        else:
            height //= count
        return int(width // cell_width), int((height - tabs_height) // cell_height)

    def _arrange_panes(self) -> None:
        """Let split panes follow the window's shape.

        The arrangement chosen in Settings is kept while it gives every pane room; when it
        squeezes them (side by side on a portrait screen, stacked on a wide short one) and the
        other arrangement does not, the other one is used until the window changes again.
        """
        if len(self.panes) < 2 or self._pane_columns:
            return  # the grid is already both ways at once
        chosen = Qt.Orientation.Vertical if self.prefs.stack_panes else Qt.Orientation.Horizontal
        other = Qt.Orientation.Horizontal if chosen == Qt.Orientation.Vertical else Qt.Orientation.Vertical

        def roomy(orientation) -> bool:
            columns, rows = self._pane_room(orientation)
            return columns >= MIN_PANE_COLUMNS and rows >= MIN_PANE_ROWS

        wanted = other if not roomy(chosen) and roomy(other) else chosen
        if self.pane_splitter.orientation() != wanted:
            self.pane_splitter.setOrientation(wanted)
            self._balance_panes()

    def _build_welcome(self) -> QWidget:
        page = QFrame()
        page.setObjectName("Welcome")
        layout = QVBoxLayout(page)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(8)

        icon = self.welcome_icon = QLabel()
        icon.setPixmap(app_icon().pixmap(72, 72))
        title = QLabel(APP_NAME)
        title.setObjectName("WelcomeTitle")
        hint = self.start_hint = QLabel()  # its words depend on whether there are saved sessions
        hint.setObjectName("WelcomeHint")
        hint.setWordWrap(True)
        hint.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        button = QPushButton("New Session")
        button.setObjectName("Primary")
        self._themed_call(lambda: button.setIcon(line_icon("plus", COLORS["on_accent"])))
        button.setIconSize(QSize(14, 14))
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        button.clicked.connect(self.new_session)
        # Most people arrive with sessions somewhere else, so with none saved the start page offers
        # to bring them in, instead of leaving that to the small icon above the session list.
        self.import_start = QPushButton("Import Sessions...")
        self._themed_call(lambda: self.import_start.setIcon(line_icon("import")))
        self.import_start.setIconSize(QSize(14, 14))
        self.import_start.setToolTip("Bring in sessions from an OpenSSH config, saved sessions or a file")
        self.import_start.setCursor(Qt.CursorShape.PointingHandCursor)
        self.import_start.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self.import_start.clicked.connect(lambda: self._import_menu(self.import_start))
        start_buttons = QWidget()
        start_buttons_layout = QHBoxLayout(start_buttons)
        start_buttons_layout.setContentsMargins(0, 0, 0, 0)
        start_buttons_layout.setSpacing(10)
        start_buttons_layout.addWidget(button)
        start_buttons_layout.addWidget(self.import_start)

        # The keys look like the keys in Keyboard Shortcuts, so the two read as one list.
        shortcuts = QGridLayout()
        shortcuts.setHorizontalSpacing(18)
        shortcuts.setVerticalSpacing(8)
        for row, name in enumerate(_WELCOME_SHORTCUTS):
            cap = QLabel(keys_for(name))
            cap.setObjectName("KeyCap")
            what = QLabel(SHORTCUTS[name][1])
            what.setObjectName("Muted")
            shortcuts.addWidget(cap, row, 0, Qt.AlignmentFlag.AlignLeft)
            shortcuts.addWidget(what, row, 1)
        shortcuts.setColumnStretch(1, 1)
        self.shortcut_card = QFrame()
        self.shortcut_card.setObjectName("Card")
        card_layout = QVBoxLayout(self.shortcut_card)
        card_layout.setContentsMargins(22, 18, 22, 18)
        card_layout.addLayout(shortcuts)

        layout.addStretch(1)
        layout.addWidget(icon, 0, Qt.AlignmentFlag.AlignHCenter)
        layout.addSpacing(10)
        layout.addWidget(title, 0, Qt.AlignmentFlag.AlignHCenter)
        layout.addWidget(hint)
        layout.addSpacing(14)
        layout.addWidget(start_buttons, 0, Qt.AlignmentFlag.AlignHCenter)
        layout.addSpacing(14)
        # One click back to what was used last: recent sessions, then saved sets of tabs.
        self.recent_bar, self.recent_row = self._start_row("RECENT")
        self.workspace_bar, self.workspace_row = self._start_row("WORKSPACES")
        layout.addWidget(self.recent_bar, 0, Qt.AlignmentFlag.AlignHCenter)
        layout.addSpacing(6)
        layout.addWidget(self.workspace_bar, 0, Qt.AlignmentFlag.AlignHCenter)
        layout.addSpacing(14)
        layout.addWidget(self.shortcut_card, 0, Qt.AlignmentFlag.AlignHCenter)
        layout.addStretch(1)
        return page

    def _build_shortcuts(self) -> None:
        handlers = self._shortcut_handlers()
        bindings = {keys_for(name): handlers[name] for name in SHORTCUTS}
        for number in range(1, 10):
            bindings[f"Alt+{number}"] = lambda n=number: self._active_pane.setCurrentIndex(n - 1)
        for sequence, handler in bindings.items():
            QShortcut(QKeySequence(sequence), self, activated=handler)

    def _shortcut_handlers(self) -> dict:
        return {
            "new_session": self.new_session,
            "switcher": self.show_switcher,
            "quick_connect": self._focus_quick_connect,
            "search_sessions": self.focus_session_search,
            "duplicate_tab": self.duplicate_tab,
            "pin_tab": self.pin_tab,
            "tab_list": self._tab_list_menu,
            "close_tab": self.close_tab,
            "reconnect": self.reconnect_tab,
            "next_tab": lambda: self._cycle_tab(1),
            "previous_tab": lambda: self._cycle_tab(-1),
            "find": self.find_in_tab,
            "files": self.toggle_files,
            "logging": self.toggle_logging,
            "move_pane": self.move_to_other_pane,
            "stack_panes": self.toggle_pane_stacking,
            "sidebar": self.toggle_sidebar,
            "full_screen": self.toggle_full_screen,
            "settings": self.open_settings,
            "shortcuts": self.show_shortcuts,
        }

    def shortcut_sections(self) -> list[tuple[str, list[tuple[str, str]]]]:
        """Every shortcut, grouped for the reference window."""
        sections = [(title, [SHORTCUTS[name] for name in names]) for title, names in _SHORTCUT_SECTIONS]
        sections[0][1].extend(_EXTRA_TAB_KEYS)
        sections.insert(2, ("Terminal", list(_TERMINAL_KEYS)))
        return sections

    def show_shortcuts(self) -> ShortcutsDialog:
        dialog = ShortcutsDialog(self.shortcut_sections(), self)
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        dialog.show()
        return dialog

    # ---- responsive layout ----------------------------------------------------

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, "shortcut_card"):  # Qt may deliver a resize before the UI is built
            self._apply_responsive()

    def _apply_responsive(self) -> None:
        width, height = self.width(), self.height()
        compact = width < COMPACT_WIDTH
        if compact != self._compact:
            self._compact = compact
            # Entering compact mode tucks the sidebar away; leaving it restores the user's choice.
            self._set_sidebar_visible(False if compact else self._sidebar_wanted)
        narrow = width < NARROW_WIDTH
        # Quick connect needs the room more than the name does, which the logo stands in for.
        self.brand_text.setVisible(not compact)
        self.caption.setVisible(not compact and height >= CRAMPED_HEIGHT)
        self.new_button.setText("" if narrow else "New Session")
        self.send_icon.setVisible(not narrow)
        self._label_broadcast()
        self.quick.setPlaceholderText("user@host[:port]" if compact else "Quick connect: user@host[:port]")
        self.quick.set_key_hint("" if compact else keys_for("quick_connect"))
        QTimer.singleShot(0, self._fit_welcome)  # once the page has its new size
        self.welcome_icon.setVisible(height >= CRAMPED_HEIGHT)
        self._set_dense(height < CRAMPED_HEIGHT)
        self._apply_send_bar()
        self._update_command_placeholder()

    def _fit_welcome(self) -> None:
        """Show the shortcut list on the start page only if it fits beside everything else there."""
        page = self.stack.widget(0)
        self.shortcut_card.setVisible(True)
        wanted = page.layout().sizeHint().height()
        self.shortcut_card.setVisible(wanted <= page.height() and page.width() >= 520)

    def _set_dense(self, dense: bool) -> None:
        """On a short window the bars give up some padding, so the terminal keeps its rows."""
        if dense == self._dense:
            return
        self._dense = dense
        vertical = 4 if dense else 10
        self._top_row.setContentsMargins(12, vertical, 12, vertical)
        self._size_logo(24 if dense else 28)
        self._size_top_controls(TOP_CONTROL_HEIGHT_DENSE if dense else TOP_CONTROL_HEIGHT)
        for widget in [self.top_bar] + [pane.tabBar() for pane in self.panes]:
            set_style_property(widget, "dense", dense, children=True)

    def _apply_send_bar(self) -> None:
        """The send bar follows the setting, except that a short window tucks it away.

        It stays while it broadcasts to every tab, so that the warning colour stays in sight.
        """
        cramped = self.height() < CRAMPED_HEIGHT and not self.all_tabs.isChecked()
        self.command_bar.setVisible(self.prefs.show_send_bar and not cramped)

    def toggle_sidebar(self) -> None:
        show = self.sidebar.isHidden()
        self._set_sidebar_visible(show)
        if not self._compact:
            self._sidebar_wanted = show
        elif show:
            # Never let the sidebar swallow a small window.
            width = min(260, max(200, self.width() // 2))
            self.splitter.setSizes([width, max(1, self.width() - width)])

    def _update_command_placeholder(self) -> None:
        marked = any(tab.in_broadcast for tab in self.open_tabs())
        if self.all_tabs.isChecked():
            targets = self.broadcast_targets()
            live = sum(1 for tab in targets if tab.state == STATE_CONNECTED)
            if marked:
                target = f"the {len(targets)} marked tab{'' if len(targets) == 1 else 's'} ({live} connected)"
            else:
                target = f"all {live} connected tab{'' if live == 1 else 's'}"
        else:
            target = "the active tab"
        if self.width() < NARROW_WIDTH:
            broadcast = "Send to marked tabs" if marked else "Send to all tabs"
            self.command.setPlaceholderText(broadcast if self.all_tabs.isChecked() else "Send a command")
        else:
            self.command.setPlaceholderText(f"Type a command and press Enter to send it to {target}")

    def _show_broadcast(self) -> None:
        """Make it plain that what is typed goes to every tab: broadcasting by mistake is costly."""
        set_style_property(self.command_bar, "broadcast", self.all_tabs.isChecked(), children=True)
        self._paint_send_icon()
        self._update_command_placeholder()
        self._apply_send_bar()

    def _paint_send_icon(self) -> None:
        color = COLORS["amber"] if self.all_tabs.isChecked() else COLORS["accent"]
        self.send_icon.setPixmap(icon_pixmap("send", color, 36))

    # ---- session tree -------------------------------------------------------

    def _session_state(self, session_id: str) -> str | None:
        """Best state among the open tabs of a saved session, for the sidebar dot."""
        best = None
        for tab in self.open_tabs():
            if tab.session.id == session_id and tab.state != STATE_DISCONNECTED:
                if best is None or _STATE_RANK[tab.state] > _STATE_RANK[best]:
                    best = tab.state
        return best

    def _reload_tree(self) -> None:
        # Every word typed must appear somewhere, in any order, as in the command palette: "prod web".
        words = self.search.text().lower().split()
        self.tree.clear()
        groups: dict[str, QTreeWidgetItem] = {}

        def add_row(parent, session: Session) -> None:
            item = QTreeWidgetItem(parent, [session.title])
            tip = [session.address]
            if session.pinned:
                tip.append("Opens its tab pinned")
            if session.notes:
                tip.append(session.notes)
            if session.id in self._reachable:
                tip.append(self._reachable[session.id][1])
            used = self.last_connected(session)
            tip.append(f"Last connected {time_ago(time.time() - used)}" if used else "Never connected")
            item.setToolTip(0, literal("\n".join(tip)))  # names and notes can come from an imported file
            item.setData(0, ROLE_SESSION_ID, session.id)
            item.setData(0, ROLE_SUBTITLE, match_reason(session) or session.address)
            item.setData(0, ROLE_COLOR, session.color)
            item.setData(0, ROLE_FAVOURITE, session.favourite)
            item.setData(0, ROLE_PINNED, session.pinned)

        def heading(name: str) -> QTreeWidgetItem:
            item = QTreeWidgetItem(self.tree, [name])
            item.setFlags(Qt.ItemFlag.ItemIsEnabled)
            item.setData(0, ROLE_IS_GROUP, True)
            groups[name] = item
            return item

        def matches(session: Session) -> bool:
            haystack = f"{session.title} {session.address} {session.group} {session.notes} {session.jump_host} {session.tunnels}".lower()
            return all(word in haystack for word in words)

        def match_reason(session: Session) -> str:
            """What to show under a row found by something its name and address do not say: a note or a jump host."""
            if not words:
                return ""
            shown = f"{session.title} {session.address} {session.group}".lower()
            hidden = [word for word in words if word not in shown]
            if not hidden:
                return ""
            for label, text in (("Note", session.notes), ("Via", session.jump_host), ("Tunnels", session.tunnels)):
                for line in text.splitlines():
                    if any(word in line.lower() for word in hidden):
                        line = line.strip()
                        return f"{label}: {line[:80]}{chr(0x2026) if len(line) > 80 else ''}"
            return ""

        by_recent = self.sidebar_sort() == SORT_RECENT
        # Moving sessions between folders by dragging needs the folders, which this order leaves out.
        self.tree.setDragEnabled(not by_recent)
        if by_recent:
            # One list, the session used last on top; those never used follow by name.
            ordered = sorted(self.store.sessions, key=lambda s: (-self.last_connected(s), s.title.lower()))
            for session in ordered:
                if matches(session):
                    add_row(self.tree.invisibleRootItem(), session)
        else:
            # Favourites are listed first, under a heading of their own, and again in their folders.
            favourites = sorted((s for s in self.store.sessions if s.favourite and matches(s)), key=lambda s: s.title.lower())
            if favourites:
                top = heading(FAVOURITES)
                for session in favourites:
                    add_row(top, session)
            sessions = sorted(self.store.sessions, key=lambda s: (s.group == "", s.group.lower(), s.title.lower()))
            for session in sessions:
                if not matches(session):
                    continue
                parent = self.tree.invisibleRootItem()
                if session.group:
                    parent = groups.get(session.group) or heading(session.group)
                add_row(parent, session)
        for name, item in groups.items():
            # A search shows every match, even inside folders the user collapsed.
            item.setExpanded(bool(words) or name not in self._collapsed_groups)
        if not self.store.sessions:
            self.empty_hint.setText("No saved sessions yet.\nUse the + button to add one.")
        else:
            self.empty_hint.setText(f"No sessions match “{self.search.text().strip()}”.")
        self.empty_hint.setVisible(not self.store.sessions or self.tree.topLevelItemCount() == 0)
        self._fill_recent()
        self._refresh_rail()
        self._update_status()  # the count of saved sessions
        self._refresh_quick_suggestions()

    def sidebar_sort(self) -> str:
        return SORT_RECENT if self.settings.value("sidebar_sort") == SORT_RECENT else SORT_NAME

    def set_sidebar_sort(self, order: str) -> None:
        """List the sessions in folders by name, or in one list with the most recently used first."""
        self.settings.setValue("sidebar_sort", order)
        self._reload_tree()

    def _sort_menu(self) -> None:
        menu = QMenu(self)
        current = self.sidebar_sort()
        for order, label in ((SORT_NAME, "In Folders, by Name"), (SORT_RECENT, "Recently Used First")):
            action = menu.addAction(label, lambda o=order: self.set_sidebar_sort(o))
            action.setCheckable(True)
            action.setChecked(order == current)
        button = self.sort_button
        menu.exec(button.mapToGlobal(button.rect().bottomLeft()))

    def last_connected(self, session: Session) -> float:
        """When a saved session last connected (seconds since the epoch), 0 if never."""
        try:
            return float(self.settings.value(f"last_connected/{session.id}") or 0)
        except (TypeError, ValueError):
            return 0.0

    def _session_rows(self) -> list[QTreeWidgetItem]:
        """The session rows of the sidebar, top to bottom, skipping folder headings."""
        rows = []
        for top in range(self.tree.topLevelItemCount()):
            item = self.tree.topLevelItem(top)
            children = [item.child(i) for i in range(item.childCount())] if item.data(0, ROLE_IS_GROUP) else [item]
            rows.extend(child for child in children if child.data(0, ROLE_SESSION_ID))
        return rows

    def _with_current_session(self, action) -> None:
        session = self._item_session(self.tree.currentItem())
        if session:
            action(session)

    def focus_session_search(self) -> None:
        if self.sidebar.isHidden():
            self.toggle_sidebar()
        self.search.setFocus()
        self.search.selectAll()

    def _search_key(self, key) -> bool:
        """Keys in the sidebar search: Enter opens the first match, Down goes to the list, Esc clears."""
        rows = self._session_rows()
        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and rows and self.search.text().strip():
            self.open_session(self._item_session(rows[0]))
            self.search.clear()
            return True
        if key == Qt.Key.Key_Down and rows:
            self.tree.setFocus()
            self.tree.setCurrentItem(rows[0])
            return True
        if key == Qt.Key.Key_Escape and self.search.text():
            self.search.clear()
            return True
        return False

    def _toggle_group(self, item: QTreeWidgetItem, _column: int = 0) -> None:
        if not item.data(0, ROLE_IS_GROUP):
            return
        item.setExpanded(not item.isExpanded())
        if item.isExpanded():
            self._collapsed_groups.discard(item.text(0))
        else:
            self._collapsed_groups.add(item.text(0))

    def _item_session(self, item: QTreeWidgetItem | None) -> Session | None:
        session_id = item.data(0, ROLE_SESSION_ID) if item else None
        return self.store.get(session_id) if session_id else None

    def _open_item(self, item: QTreeWidgetItem | None) -> None:
        session = self._item_session(item)
        if session:
            self.open_session(session)

    def _tree_menu(self, pos) -> None:
        item = self.tree.itemAt(pos)
        session = self._item_session(item)
        menu = QMenu(self)
        if session:
            open_already = [tab for tab in self.open_tabs() if tab.session.id == session.id]
            if open_already:
                # Usually wanted rather than a second connection; the first of them if there are several.
                shown = open_already[0]
                menu.addAction(f"Go to {self._tab_name(shown)}", lambda: self._switch_to(KIND_TAB, shown))
                menu.addAction("Connect in a New Tab\tEnter", lambda: self.open_session(session))
            else:
                menu.addAction("Connect\tEnter", lambda: self.open_session(session))
            menu.addSeparator()
            menu.addAction("Edit...", lambda: self.edit_session(session))
            menu.addAction("Duplicate", lambda: self._duplicate_session(session))
            move = menu.addMenu("Move to Folder")
            for group in self.store.groups():
                action = move.addAction(group, lambda g=group: self.move_session(session, g))
                action.setEnabled(group != session.group)
            if self.store.groups():
                move.addSeparator()
            top = move.addAction("(No folder)", lambda: self.move_session(session, ""))
            top.setEnabled(bool(session.group))
            move.addAction("New Folder...", lambda: self._move_to_new_folder(session))
            menu.addAction(
                "Remove from Favourites" if session.favourite else "Add to Favourites",
                lambda: self.set_favourite(session, not session.favourite),
            )
            menu.addAction(
                "Don’t Open Pinned" if session.pinned else "Open Its Tab Pinned",
                lambda: self.set_session_pinned(session, not session.pinned),
            )
            menu.addAction("Delete", lambda: self._delete_session(session))
            menu.addSeparator()
        elif item is not None and item.data(0, ROLE_IS_GROUP):
            group = item.text(0)
            count = len(self.sessions_in(group))
            menu.addAction(f"Connect All ({count})", lambda: self.connect_folder(group))
            menu.addAction("Check Which Answer", lambda: self.check_connections(self.sessions_in(group)))
            if group != FAVOURITES:  # the favourites heading is not a folder
                menu.addAction("Rename Folder...", lambda: self._ask_rename_folder(group))
            menu.addSeparator()
        menu.addAction(with_keys("New Session...", "new_session"), self.new_session)
        menu.exec(self.tree.viewport().mapToGlobal(pos))

    def _session_dropped(self, session_id: str, group: str) -> None:
        """A session was dragged onto a folder (or out of all folders) in the sidebar."""
        session = self.store.get(session_id)
        if session is None:
            return
        if group == FAVOURITES:  # dropped on the favourites heading or on a favourite
            self.set_favourite(session, True)
        elif session.group != group:
            self.move_session(session, group)

    def sessions_in(self, group: str) -> list[Session]:
        if group == FAVOURITES:
            chosen = (s for s in self.store.sessions if s.favourite)
        else:
            chosen = (s for s in self.store.sessions if s.group == group)
        return sorted(chosen, key=lambda s: s.title.lower())

    def _toggle_favourite(self, session_id: str) -> None:
        session = self.store.get(session_id)
        if session is not None:
            self.set_favourite(session, not session.favourite)

    def set_favourite(self, session: Session, favourite: bool) -> None:
        self.store.upsert(replace(session, favourite=favourite))
        self._adopt_saved_sessions()

    def set_session_pinned(self, session: Session, pinned: bool) -> None:
        """Make a saved session open its tab pinned, or not; tabs already open stay as they are."""
        self.store.upsert(replace(session, pinned=pinned))
        self._adopt_saved_sessions()

    def connect_folder(self, group: str) -> list[TerminalTab]:
        """Open a tab for every session in a folder."""
        return [self.open_session(session) for session in self.sessions_in(group)]

    def move_session(self, session: Session, group: str) -> None:
        self.store.upsert(replace(session, group=group.strip()))
        self._adopt_saved_sessions()

    def _move_to_new_folder(self, session: Session) -> None:
        name, ok = QInputDialog.getText(self, "New Folder", "Folder name")
        if ok and name.strip():
            self.move_session(session, name)

    def rename_folder(self, group: str, name: str) -> None:
        """Rename a folder; renaming it to an existing folder's name merges the two."""
        name = name.strip()
        if not name or name == group:
            return
        for session in self.sessions_in(group):
            self.store.upsert(replace(session, group=name))
        if group in self._collapsed_groups:
            self._collapsed_groups.discard(group)
            self._collapsed_groups.add(name)
        self._adopt_saved_sessions()

    def _ask_rename_folder(self, group: str) -> None:
        name, ok = QInputDialog.getText(self, "Rename Folder", "Folder name", text=group)
        if ok:
            self.rename_folder(group, name)

    def _adopt_saved_sessions(self) -> None:
        """After sessions changed in the store: refresh the sidebar and the open tabs that use them."""
        self._reload_tree()
        for tab in self.open_tabs():
            saved = self.store.get(tab.session.id)
            if saved is not None:
                tab.set_session(saved)

    # ---- recent sessions ------------------------------------------------------------

    def recent_sessions(self) -> list[Session]:
        """Saved sessions in the order they were last opened, most recent first."""
        stored = self.settings.value("recent_sessions") or []
        ids = [stored] if isinstance(stored, str) else list(stored)
        found = (self.store.get(session_id) for session_id in ids)
        return [session for session in found if session is not None][:RECENT_SESSIONS]

    def _note_recent(self, session: Session) -> None:
        if self.store.get(session.id) is None:
            return  # quick connections are not saved, so there is nothing to come back to
        stored = self.settings.value("recent_sessions") or []
        ids = [stored] if isinstance(stored, str) else list(stored)
        ids = [session.id] + [session_id for session_id in ids if session_id != session.id]
        self.settings.setValue("recent_sessions", ids[: RECENT_SESSIONS * 2])
        self._fill_recent()

    @staticmethod
    def _start_row(title: str) -> tuple[QWidget, QHBoxLayout]:
        """A titled row of buttons for the start page; the buttons go in the returned layout."""
        bar = QWidget()
        column = QVBoxLayout(bar)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(6)
        heading = QLabel(title)
        heading.setObjectName("SectionTitle")
        column.addWidget(heading, 0, Qt.AlignmentFlag.AlignHCenter)
        row = QHBoxLayout()
        row.setSpacing(8)
        column.addLayout(row)
        return bar, row

    @staticmethod
    def _start_button(text: str, tooltip: str, handler) -> QPushButton:
        button = QPushButton(text)
        button.setObjectName("RecentButton")
        button.setToolTip(tooltip)
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        button.clicked.connect(lambda _checked=False: handler())
        return button

    def _update_start_hint(self) -> None:
        """With no saved sessions the start page says how to begin and offers to import some."""
        empty = not self.store.sessions
        self.start_hint.setText(
            "Add a session, import the ones you already have, or type user@host in Quick connect."
            if empty
            else "Double-click a saved session, or type user@host in Quick connect."
        )
        self.import_start.setVisible(empty)

    def _fill_recent(self) -> None:
        if not hasattr(self, "recent_row"):
            return  # the welcome page is built after the sidebar
        self._update_start_hint()
        for row in (self.recent_row, self.workspace_row):
            while row.count():
                widget = row.takeAt(0).widget()
                if widget:
                    widget.deleteLater()
        recent = self.recent_sessions()
        for session in recent:
            self.recent_row.addWidget(self._start_button(session.title, session.address, lambda s=session: self.open_session(s)))
        self.recent_bar.setVisible(bool(recent))
        names = self.workspaces.names()[:RECENT_SESSIONS]
        for name in names:
            members = [self.store.get(session_id) for session_id in self.workspaces.get(name)]
            titles = ", ".join(session.title for session in members if session is not None)
            self.workspace_row.addWidget(self._start_button(name, f"Open {titles}", lambda n=name: self.open_workspace(n)))
        self.workspace_bar.setVisible(bool(names))
        QTimer.singleShot(0, self._fit_welcome)

    # ---- saved commands -----------------------------------------------------------------

    def _snippet_menu(self) -> None:
        menu = QMenu(self)
        menu.setToolTipsVisible(True)
        for snippet in self.snippets.snippets:
            action = menu.addAction(snippet.title, lambda s=snippet: self.send_snippet(s))
            action.setToolTip(snippet.command)
        if self.snippets.snippets:
            menu.addSeparator()
        menu.addAction("Manage Saved Commands...", self.edit_snippets)
        button = self.snippet_button
        menu.exec(button.mapToGlobal(button.rect().topLeft()))

    def save_as_snippet(self, text: str, name: str | None = None) -> Snippet | None:
        """Keep text from the terminal as a saved command; asks for a name unless given one. Returns it, or None."""
        text = "\n".join(line.rstrip() for line in text.strip().splitlines())
        if not text:
            return None
        if name is None:
            name, ok = ask_text(self, "Save as Saved Command", "Name shown in the menu (may be empty)")
            if not ok:
                return None
        snippet = Snippet(name.strip(), text)
        self.snippets.replace_all([*self.snippets.snippets, snippet])
        self.notify(f"Saved as “{snippet.title}” in the saved commands", "success")
        return snippet

    def edit_snippets(self) -> None:
        dialog = SnippetsDialog(self.snippets.snippets, self)
        if dialog.exec():
            self.snippets.replace_all(dialog.snippets())

    # ---- scheduled tasks and local shells ---------------------------------------

    def edit_tasks(self) -> None:
        dialog = TasksDialog(self.tasks.tasks, self.store.sessions, self.scheduler, self)
        if dialog.exec():
            self.save_tasks(dialog.tasks())

    def save_tasks(self, tasks: list[Task]) -> list[str]:
        """Keep these tasks, and register those for Windows with it; returns what could not be registered."""
        kept = {task.id for task in tasks}
        for gone in (task for task in self.tasks.tasks if task.id not in kept):
            unregister(gone)
        problems = []
        for task in tasks:
            problem = sync_with_windows(task)
            if problem:
                problems.append(f"{task.title}: {problem}")
        self.tasks.replace_all(tasks)
        self._paint_tasks_button()
        if problems:
            self.notify("Not registered with Windows Task Scheduler. " + "; ".join(problems), "error", 15000)
        return problems

    def run_task_now(self, task: Task) -> bool:
        """Run a saved task at once, whatever its schedule; the status bar says how it went."""
        if not self.scheduler.run(task):
            self.notify(f"Task “{task.title}” is running already", "error")
            return False
        self.notify(f"Task “{task.title}” started")
        return True

    def _adopt_restored_tasks(self) -> None:
        """Bring Windows Task Scheduler in line with tasks just restored from a backup.

        What a backup says was registered may be from another computer, so each task is registered afresh, and
        the tasks of before that are not in the backup are taken off Windows' list.
        """
        restored = TaskStore(self.tasks.path).tasks
        registered_here = {task.id: task.registered_as for task in self.tasks.tasks}
        for task in restored:
            task.registered_as = registered_here.get(task.id, "")
        self.save_tasks(restored)

    def _task_finished(self, task: Task, result: TaskResult) -> None:
        path = result.path
        open_output = (lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))) if path else None
        action = ("Open Output", open_output) if path else None
        self.notify(f"Task “{task.title}”: {result.summary}", "success" if result.ok else "error", action=action)
        if not result.ok and task.notify_on_failure and not self.isActiveWindow():
            # The status bar is enough for someone looking at the window; anyone else is told by the desktop.
            def clicked():
                if self.isMinimized():
                    self.showNormal()
                self.activateWindow()
                if open_output is not None:
                    open_output()

            self.notifier.show(f"Task failed: {task.title}", result.summary, clicked)
            QApplication.alert(self)

    def open_local_shell(self, command_line: str = "") -> TerminalTab:
        """A tab with a shell on this computer: the one given, or else the first one found (PowerShell)."""
        return self.open_session(local_session(command_line))

    def _local_shell_menu(self) -> None:
        menu = QMenu(self)
        self._add_shells(menu)
        button = self.shell_button
        menu.exec(button.mapToGlobal(button.rect().bottomLeft() + QPoint(0, 8)))  # clear of the tray
        menu.deleteLater()

    def _add_shells(self, menu: QMenu) -> None:
        shells = available_shells()
        for name, command_line in shells:
            action = menu.addAction(line_icon("terminal", COLORS["muted"]), name, lambda c=command_line: self.open_local_shell(c))
            action.setToolTip(command_line)
        if not shells:
            menu.addAction(line_icon("terminal", COLORS["muted"]), "Command Prompt", lambda: self.open_local_shell("cmd.exe"))

    # ---- quick switcher, full screen, restoring tabs -------------------------------------

    def switcher_entries(self) -> list[tuple[str, object, str, str]]:
        """Open tabs, saved sessions, commands, saved commands and workspaces, as (kind, target, title, detail)."""
        tabs = sorted(self.open_tabs(), key=lambda tab: not tab.pinned)  # pinned ones first, the order kept
        entries = [
            (KIND_PINNED if tab.pinned else KIND_TAB, tab, self._tab_name(tab), tab.session.address) for tab in tabs
        ]
        for session in sorted(self.store.sessions, key=lambda s: s.title.lower()):
            detail = session.address + (f"   [{session.group}]" if session.group else "")
            entries.append((KIND_SESSION, session, session.title, detail))
        entries.extend((KIND_ACTION, handler, title, keys_) for title, keys_, handler in self.commands())
        for task in self.tasks.tasks:
            entries.append((KIND_ACTION, lambda t=task: self.run_task_now(t), f"Run Task: {task.title}", ""))
        for snippet in self.snippets.snippets:
            entries.append((KIND_SNIPPET, snippet, snippet.title, " ; ".join(snippet.command.strip().splitlines())))
        for name in self.workspaces.names():
            count = len(self.workspaces.get(name))
            entries.append((KIND_WORKSPACE, name, name, f"{count} session{'' if count == 1 else 's'}"))
        return entries

    def commands(self) -> list[tuple[str, str, object]]:
        """What the switcher can run: (name, shortcut, callable)."""
        light = active_theme() == THEME_LIGHT
        return [
            ("New Session", keys_for("new_session"), self.new_session),
            ("Settings", keys_for("settings"), self.open_settings),
            ("Keyboard Shortcuts", keys_for("shortcuts"), self.show_shortcuts),
            ("Switch to Dark Theme" if light else "Switch to Light Theme", "", self.toggle_theme),
            ("Toggle Sidebar", keys_for("sidebar"), self.toggle_sidebar),
            ("Server Monitor", "", self.show_server_monitor),
            ("Sort Sessions in Folders by Name", "", lambda: self.set_sidebar_sort(SORT_NAME)),
            ("Sort Sessions by Recent Use", "", lambda: self.set_sidebar_sort(SORT_RECENT)),
            ("Search Saved Sessions", keys_for("search_sessions"), self.focus_session_search),
            ("Toggle Send Bar", "", self.toggle_send_bar),
            ("Full Screen", keys_for("full_screen"), self.toggle_full_screen),
            ("Find in Output", keys_for("find"), self.find_in_tab),
            ("File Browser", keys_for("files"), self.toggle_files),
            ("Split or Move Tab to Next Pane", keys_for("move_pane"), self.move_to_other_pane),
            ("Stack Panes / Side by Side", keys_for("stack_panes"), self.toggle_pane_stacking),
            ("Grid of Panes / One Row", "", self.toggle_pane_grid),
            ("Duplicate Tab", keys_for("duplicate_tab"), self.duplicate_tab),
            ("Rename Tab", "", self.rename_tab),
            ("Pin Tab / Unpin Tab", keys_for("pin_tab"), self.pin_tab),
            ("List All Open Tabs", keys_for("tab_list"), self._tab_list_menu),
            ("Close All Unpinned Tabs", "", self.close_unpinned),
            ("Pin All Tabs", "", lambda: self.pin_all(True)),
            ("Unpin All Tabs", "", lambda: self.pin_all(False)),
            ("Include Tab in Broadcast / Leave It Out", "", self.set_in_broadcast),
            ("Clear Broadcast Marks", "", self.clear_broadcast_marks),
            ("Reconnect Tab", keys_for("reconnect"), self.reconnect_tab),
            ("Disconnect Tab", "", lambda: self.current_tab() and self.current_tab().disconnect_from_host()),
            ("Close Tab", keys_for("close_tab"), self.close_tab),
            ("Reopen Closed Tab", "", self.reopen_closed_tab),
            ("Close Disconnected Tabs", "", self.close_disconnected),
            ("Reconnect Disconnected Tabs", "", self.reconnect_disconnected),
            ("Save Output to a File", "", self.save_output),
            ("Log Tab to a File", keys_for("logging"), self.toggle_logging),
            ("Manage Saved Commands", "", self.edit_snippets),
            ("Scheduled Tasks", "", self.edit_tasks),
            ("Open a Shell on This Computer", "", self.open_local_shell),
            ("Generate SSH Key Pair", "", self.generate_key),
            ("Install Public Key on Server", "", self.install_public_key),
            ("Export Sessions to a File", "", self._export_sessions_file),
            ("Back Up Everything", "", self._ask_backup),
            ("Restore from a Backup", "", self._ask_restore),
            ("Clear History", "", self.clear_history),
            ("Clear Logs", "", self.clear_logs),
            ("Check Which Sessions Answer", "", self.check_connections),
            ("Save Open Tabs as a Workspace", "", self.save_workspace),
            ("Notify When Output Stops", "", lambda: self.current_tab() and self.current_tab().watch_for_silence()),
            ("Notify When Output Contains...", "", self.watch_for_text),
            ("Connection Info", "", self.show_connection_info),
            ("Send Break", "", self.send_break),
            ("Send Text File", "", self.send_text_file),
            ("Tunnels", "", self.show_tunnels),
            ("Approved Host Keys", "", self.show_host_keys),
            ("Pause / Resume Automatic Responses", "", self.toggle_responses),
        ]

    def toggle_send_bar(self) -> None:
        self.apply_preferences(replace(self.prefs, show_send_bar=not self.prefs.show_send_bar))

    def toggle_theme(self) -> None:
        self.apply_preferences(
            # From what is on screen, which with "system" may be either.
            replace(self.prefs, theme=THEME_DARK if active_theme() == THEME_LIGHT else THEME_LIGHT)
        )

    def show_switcher(self) -> QuickSwitcher:
        switcher = QuickSwitcher(self.switcher_entries(), self)
        switcher.chosen.connect(self._switch_to)
        # Centred near the top of the window, where the eye already is, and never wider or
        # taller than the window itself.
        top = 70 if self.height() >= CRAMPED_HEIGHT else 12
        switcher.resize(min(switcher.width(), self.width() - 24), min(switcher.height(), self.height() - top - 12))
        origin = self.mapToGlobal(self.rect().topLeft())
        switcher.move(origin.x() + (self.width() - switcher.width()) // 2, origin.y() + top)
        switcher.show()
        switcher.query.setFocus()
        return switcher

    def _switch_to(self, kind: str, target) -> None:
        if kind == KIND_ACTION:
            target()
            return
        if kind == KIND_SNIPPET:
            self.send_snippet(target)
            return
        if kind == KIND_WORKSPACE:
            self.open_workspace(target)
            return
        if kind == KIND_SESSION:
            self.open_session(target)
            return
        pane = self._pane_of(target)
        if pane is not None:
            pane.setCurrentWidget(target)
            self._set_active_pane(pane)
            target.terminal.setFocus()

    def toggle_full_screen(self) -> None:
        if self.isFullScreen():
            self.showMaximized() if self._was_maximized else self.showNormal()
        else:
            self._was_maximized = self.isMaximized()
            self.showFullScreen()

    def _tab_layout(self, local_shells: bool = True) -> dict[str, list[str]]:
        """The open tabs and how they are arranged, as lists of text to keep: the saved sessions (and local
        shells, when asked) in tab order under "entries", and under the other keys positions in that list."""
        entries, pinned, panes, place_of = [], [], [], {}
        for tab in self.open_tabs():
            if self.store.get(tab.session.id) is not None:
                entries.append(tab.session.id)
            elif local_shells and tab.session.protocol == PROTO_LOCAL:
                entries.append(tab.session.address)  # local://program: a shell opened without a saved session
            else:
                continue
            if tab.pinned:
                pinned.append(str(len(entries) - 1))
            panes.append(str(self.panes.index(self._pane_of(tab))))  # the pane of each
            place_of[tab] = len(entries) - 1
        in_front = [place_of[pane.currentWidget()] for pane in self.panes if pane.currentWidget() in place_of]
        sizes = self.pane_splitter.sizes()
        total = sum(sizes)
        # The share of the window each pane has, in thousandths; not kept for the grid, which sizes its own.
        shares = [str(round(size * 1000 / total)) for size in sizes] if total and not self._pane_columns else []
        active = self.panes.index(self._active_pane) if self._active_pane in self.panes else 0
        return {
            "entries": entries, "pinned": pinned, "panes": panes, "front": [str(place) for place in in_front],
            "sizes": shares, "active": [str(active)],
        }  # fmt: skip

    def _open_layout(self, layout: dict) -> list[TerminalTab]:
        """Open the tabs of a _tab_layout() and arrange them as they were; returns the tabs opened."""

        def texts(key: str) -> list[str]:
            value = layout.get(key) or []
            return [value] if isinstance(value, str) else [str(item) for item in value]

        entries, pinned, in_panes = texts("entries"), set(texts("pinned")), texts("panes")
        opened, at_place = [], {}
        for position, entry in enumerate(entries):
            if entry.startswith("local://"):
                tab = self.open_local_shell(entry.removeprefix("local://"))
            elif (session := self.store.get(entry)) is not None:
                tab = self.open_session(session)
            else:
                continue
            if str(position) in pinned:
                self.pin_tab(tab, True, announce=False)
            opened.append(tab)
            at_place[position] = tab
            wanted = int(in_panes[position]) if position < len(in_panes) and in_panes[position].isdigit() else 0
            self._put_in_pane(tab, wanted)
        self._restore_pane_sizes(texts("sizes"))
        for place in texts("front"):
            front = at_place.get(int(place)) if place.isdigit() else None
            if front is not None and self._pane_of(front) is not None:
                self._pane_of(front).setCurrentWidget(front)
        active = texts("active")
        if opened and active and active[0].isdigit() and int(active[0]) < len(self.panes):
            # After the focus changes the new tabs queue for themselves have been handled.
            QTimer.singleShot(0, lambda pane=self.panes[int(active[0])]: self._focus_pane(pane))
        return opened

    def _focus_pane(self, pane: QTabWidget) -> None:
        if pane in self.panes:
            self._set_active_pane(pane)
            if pane.currentWidget() is not None:
                pane.currentWidget().terminal.setFocus()

    _LAYOUT_SETTINGS = {
        "entries": "open_sessions", "pinned": "pinned_tabs", "panes": "tab_panes", "front": "pane_current",
        "sizes": "pane_sizes", "active": "active_pane",
    }  # fmt: skip

    def remember_open_tabs(self) -> None:
        """Note which saved sessions and local shells are open, and how they are arranged, for "Reopen tabs at start"."""
        for key, value in self._tab_layout().items():
            self.settings.setValue(self._LAYOUT_SETTINGS[key], value)

    def restore_tabs(self) -> list[TerminalTab]:
        return self._open_layout({key: self.settings.value(name) for key, name in self._LAYOUT_SETTINGS.items()})

    def _restore_pane_sizes(self, shares: list[str]) -> None:
        try:
            shares = [int(share) for share in shares]
        except (TypeError, ValueError):
            return
        if len(self.panes) < 2 or len(shares) != len(self.panes) or self._pane_columns or min(shares) < 100:
            return  # nothing saved for this many panes, or a pane too thin to be intended
        stacked = self.pane_splitter.orientation() == Qt.Orientation.Vertical
        extent = self.pane_splitter.height() if stacked else self.pane_splitter.width()
        self.pane_splitter.setSizes([max(1, extent * share // 1000) for share in shares])

    def _put_in_pane(self, tab: TerminalTab, number: int) -> None:
        """Move a tab to the pane of that number, making panes as far as needed (up to the most there can be)."""
        number = min(number, MAX_PANES - 1)
        made = False
        while len(self.panes) <= number:
            self._new_pane(after=self.panes[-1])
            made = True
        if self._pane_of(tab) is not self.panes[number]:
            if made:
                self._balance_panes()
            self._place(tab, self.panes[number])
            self._arrange_panes()

    # ---- SSH keys ---------------------------------------------------------------------------

    def generate_key(self) -> None:
        KeyDialog(self).exec()

    def install_public_key(self, target=None, key_path=None) -> None:
        """Add a public key to the server of an SSH tab, so that key can log in there."""
        tab = self._resolve(target)
        opener = tab.sftp_opener() if tab else None
        if opener is None:
            self.notify("Installing a key needs a connected SSH session", "error")
            return
        if key_path is None:
            start = keys.default_key_path().parent
            key_path, _ = QFileDialog.getOpenFileName(
                self, "Install Public Key on Server", str(start), "Public keys (*.pub);;All files (*)"
            )
            if not key_path:
                return
        try:
            line = keys.public_key_line(key_path)
        except keys.KeyError_ as exc:
            self.notify(str(exc), "error")
            tab.terminal.write_notice(str(exc), color=91)
            return
        installer = keys.KeyInstaller(opener, line)
        name = line.split()[2] if len(line.split()) > 2 else "the key"

        def finished(message: str, color: int = 92) -> None:
            self.notify(message, "success" if color == 92 else "error")
            tab.terminal.write_notice(message, color=color)

        installer.done.connect(
            lambda added: finished(
                f"Installed the public key {name} on {tab.session.host}"
                if added
                else f"The public key {name} was already installed on {tab.session.host}"
            )
        )
        installer.failed.connect(lambda error: finished(f"Could not install the key: {error}", 91))
        installer.start()
        threads.park(installer)

    def save_output(self, target=None, path=None) -> bool:
        """Write everything a tab has shown, scrollback included, to a file.

        A name ending in .html or .htm gives a web page that keeps the colours; anything else plain text.
        """
        tab = self._resolve(target)
        if tab is None:
            return False
        if path is None:
            suggested = tab.default_log_path().with_suffix(".txt")
            path, _ = QFileDialog.getSaveFileName(
                self, "Save Output", str(suggested), "Text files (*.txt *.log);;Web page with colours (*.html)"
            )
            if not path:
                return False
        as_html = str(path).lower().endswith((".html", ".htm"))
        try:
            with open(path, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(tab.terminal.all_html(tab.title) if as_html else tab.terminal.all_text())
        except OSError as exc:
            self.notify(f"Could not save the output: {exc.strerror or exc}", "error")
            return False
        self.notify(f"Saved the output of {tab.title} to {path}", "success")
        return True

    def _import_menu(self, anchor: QWidget | None = None) -> None:
        menu = QMenu(self)
        menu.addSection("Import")
        menu.addAction("From OpenSSH Config (~/.ssh/config)", lambda: self.import_sessions(importers.from_openssh_config()))
        menu.addAction("From Another OpenSSH Config File...", self._import_openssh_file)
        menu.addAction("From Saved Sessions", lambda: self.import_sessions(importers.from_saved_sessions()))
        menu.addAction("From a Sessions File...", self._import_sessions_file)
        menu.addSeparator()
        export = menu.addAction("Export Sessions to a File...", self._export_sessions_file)
        export.setEnabled(bool(self.store.sessions))
        menu.addSeparator()
        menu.addAction("Back Up Everything to a File...", self._ask_backup)
        menu.addAction("Restore from a Backup...", self._ask_restore)
        menu.addSeparator()
        menu.addAction("Approved Host Keys...", self.show_host_keys)
        menu.addAction("Clear History...", self.clear_history)
        menu.addAction("Clear Logs...", self.clear_logs)
        button = anchor if isinstance(anchor, QWidget) else self.import_button  # a click passes a bool
        menu.exec(button.mapToGlobal(button.rect().bottomLeft()))

    def _import_sessions_file(self) -> None:
        chosen, _ = QFileDialog.getOpenFileName(self, "Import Sessions", "", "Sessions (*.json);;All files (*)")
        if chosen:
            self.import_sessions(importers.from_sessions_file(chosen))

    def _export_sessions_file(self) -> None:
        chosen, _ = QFileDialog.getSaveFileName(self, "Export Sessions", "techx-sessions.json", "Sessions (*.json)")
        if chosen:
            self.export_sessions(chosen)

    def export_sessions(self, path) -> int:
        """Write every saved session to a file; passwords are not included."""
        try:
            count = importers.export_sessions(self.store.sessions, path)
        except OSError as exc:
            self.notify(f"Could not write the file: {exc.strerror or exc}", "error")
            return 0
        self.notify(f"Exported {count} session{'' if count == 1 else 's'} (without passwords)", "success")
        return count

    def _ask_backup(self) -> None:
        asking = BackupPassphraseDialog(self)
        if asking.exec() != QDialog.DialogCode.Accepted:
            return
        passphrase = asking.chosen_passphrase()
        if passphrase:
            name, kinds = f"techx-backup{backup.ENCRYPTED_SUFFIX}", f"Encrypted backups (*{backup.ENCRYPTED_SUFFIX})"
        else:
            name, kinds = "techx-backup.zip", "Backups (*.zip)"
        chosen, _ = QFileDialog.getSaveFileName(self, "Back Up Everything", name, kinds)
        if chosen:
            self.backup_to(chosen, passphrase)

    def backup_to(self, path, passphrase: str = "") -> bool:
        """Save sessions, saved commands, scheduled tasks, approved host keys and preferences in one file,
        encrypted when there is a passphrase."""
        try:
            parts = backup.create_backup(
                path, self.store.path, self.snippets.path, KNOWN_HOSTS_FILE, self.prefs, self.workspaces.path,
                self.tasks.path, passphrase=passphrase,
            )
        except backup.BackupError as exc:
            self.notify(str(exc), "error")
            return False
        protected = "encrypted, " if passphrase else ""
        self.notify(f"Backed up {len(parts)} items to {path} ({protected}passwords are not included)", "success")
        return True

    def _ask_restore(self) -> None:
        kinds = f"Backups (*{backup.ENCRYPTED_SUFFIX} *.zip);;All files (*)"
        chosen, _ = QFileDialog.getOpenFileName(self, "Restore from a Backup", "", kinds)
        if not chosen:
            return
        answer = QMessageBox.question(
            self,
            "Restore from a Backup",
            "Restoring replaces your saved sessions, saved commands, scheduled tasks, approved host keys and settings with "
            "those in the backup. The current files are kept beside them with the ending .bak.\n\nContinue?",
        )
        if answer == QMessageBox.StandardButton.Yes:
            self.restore_from(chosen)

    def restore_from(self, path, passphrase: str | None = None) -> bool:
        """Replace the stored sessions, commands, tasks, host keys and preferences with a backup's. An encrypted
        backup needs its passphrase, which is asked for when it is not given."""
        if passphrase is None and backup.is_encrypted(path):
            passphrase, given = ask_text(self, "Restore from a Backup", "The passphrase of this backup:", hidden=True)
            if not given:
                return False
        try:
            parts = backup.read_backup(path, passphrase)
            backup.restore_files(
                parts, self.store.path, self.snippets.path, KNOWN_HOSTS_FILE, self.workspaces.path, self.tasks.path
            )
        except backup.BackupError as exc:
            self.notify(str(exc), "error")
            return False
        self.store.load()
        self.snippets.load()
        self.workspaces.load()
        if backup.TASKS_NAME in parts:
            self._adopt_restored_tasks()
        if backup.PREFERENCES_NAME in parts:
            self.apply_preferences(backup.preferences_from(parts[backup.PREFERENCES_NAME]))
        self._adopt_saved_sessions()
        self._update_status()
        self.notify(f"Restored {len(self.store.sessions)} sessions and the settings from {path}", "success")
        return True

    def _import_openssh_file(self) -> None:
        chosen, _ = QFileDialog.getOpenFileName(self, "Import OpenSSH Config", str(importers.OPENSSH_CONFIG.parent))
        if chosen:
            from pathlib import Path

            self.import_sessions(importers.from_openssh_config(Path(chosen)))

    def import_sessions(self, found: list[Session]) -> int:
        """Add sessions that are not saved yet; reports the outcome in the status bar."""
        added = importers.merge(self.store, found)
        if added:
            self._reload_tree()
            self._update_status()
            message, level = f"Imported {added} session{'' if added == 1 else 's'}", "success"
        elif found:
            message, level = "Nothing to import: those sessions are already saved", "info"
        else:
            message, level = "No sessions found to import", "info"
        self.notify(message, level)
        return added

    def new_session(self) -> None:
        self.edit_session(None)

    def edit_session(self, session: Session | None) -> None:
        dialog = SessionDialog(self.store, session, self)
        if not dialog.exec():
            return
        saved = dialog.session()
        self.store.upsert(saved)
        if not saved.save_password:
            self.store.set_password(saved, None)
        elif dialog.password:
            self.store.set_password(saved, dialog.password)
        self._reload_tree()
        # Open tabs of this session pick up the new name and colour straight away.
        for tab in self.open_tabs():
            if tab.session.id == saved.id:
                tab.set_session(saved)
                pane = self._pane_of(tab)
                pane.setTabIcon(pane.indexOf(tab), self._tab_icon(tab))
        self._refresh_tab_labels()  # a renamed session may now share, or no longer share, a name
        self._update_status()
        if dialog.connect_after:
            self.open_session(saved, dialog.password)

    def save_tab_as_session(self, tab: TerminalTab) -> Session | None:
        """Keep the session of a tab opened by quick connect; the tab goes on as that saved session."""
        if self.store.get(tab.session.id) is not None:
            return None
        # Its id is the tab's, so the sidebar shows the tab's state on it at once. No password is kept.
        session = replace(tab.session, name=tab.session.title, save_password=False)
        tab.set_session(session)
        self.store.upsert(session)
        self._reload_tree()
        self.notify(f"Saved “{session.title}”", "success", action=("Edit", lambda: self.edit_session(session)))
        return session

    def _duplicate_session(self, session: Session) -> None:
        copy = replace(session, id=Session().id, name=f"{session.title} (copy)", save_password=False)
        self.store.upsert(copy)
        self._reload_tree()
        # A copy is nearly always made to be changed, so the next step is one click away.
        self.notify(f"Saved a copy as “{copy.title}”", "success", action=("Edit", lambda: self.edit_session(copy)))

    def _delete_session(self, session: Session) -> None:
        answer = QMessageBox.question(self, "Delete Session", f"Delete the saved session “{session.title}”?")
        if answer != QMessageBox.StandardButton.Yes:
            return
        password = self.store.get_password(session)  # removing it also forgets the saved password
        self.store.remove(session)
        self._reload_tree()
        self.notify(f"Deleted “{session.title}”", action=("Undo", lambda: self._undelete_session(session, password)))

    def _undelete_session(self, session: Session, password: str | None) -> None:
        if self.store.get(session.id) is not None:
            return
        self.store.upsert(session)
        if password:
            self.store.set_password(session, password)
        self._reload_tree()
        self.notify(f"“{session.title}” is back", "success")

    # ---- tabs -----------------------------------------------------------------

    def open_tabs(self) -> list[TerminalTab]:
        return [pane.widget(index) for pane in self.panes for index in range(pane.count())]

    def _pane_of(self, tab: TerminalTab) -> QTabWidget | None:
        return next((pane for pane in self.panes if pane.indexOf(tab) >= 0), None)

    def _resolve(self, target) -> TerminalTab | None:
        """Accepts a tab, an index into the first pane, or None for the current tab."""
        if target is None or isinstance(target, bool):
            return self.current_tab()
        if isinstance(target, int):
            return self.tabs.widget(target)
        return target

    def _add_to_pane(self, pane: QTabWidget, tab: TerminalTab) -> int:
        if not hasattr(tab, "opened_order"):
            tab.opened_order = next(self._tab_counter)  # numbers tabs of the same name, oldest first
        index = pane.addTab(tab, self._tab_icon(tab), self._tab_label(tab))
        pane.setTabToolTip(index, self._tab_tooltip(tab))
        self._settle_pins(pane)  # a pinned tab arriving from another pane joins the pinned ones
        index = pane.indexOf(tab)
        close = QToolButton()
        close.setObjectName("TabClose")
        close.setAutoRaise(True)  # so the hover look of the icon is used
        close.setIcon(self._close_icon())
        close.setIconSize(QSize(10, 10))
        close.setFixedSize(18, 24)  # an 18 x 18 face below the 6 px margin the tabs have too
        close.setToolTip(f"Close tab ({keys_for('close_tab')})")
        close.setAccessibleName(f"Close {tab.title}")
        close.setCursor(Qt.CursorShape.ArrowCursor)
        close.clicked.connect(lambda: self.close_tab(tab))
        pane.tabBar().setTabButton(index, QTabBar.ButtonPosition.RightSide, close)
        self._refresh_tab_labels()  # a second tab of a name numbers itself
        self._paint_close_buttons(pane)
        return index

    def open_session(self, session: Session, password: str | None = None) -> TerminalTab:
        tab = TerminalTab(session, self.store, self.prefs, password)
        tab.state = STATE_CONNECTING  # shown on the tab until the first real state arrives
        pane = self._active_pane if self._active_pane in self.panes else self.tabs
        index = self._add_to_pane(pane, tab)

        tab.stateChanged.connect(lambda state: self._on_tab_state(tab, state))
        tab.terminal.sizeChanged.connect(lambda *_: self._update_status())
        tab.terminal.fontSizeChanged.connect(self._set_font_size)
        tab.transferLimitChanged.connect(self._set_transfer_limit)
        tab.transferSettingsRequested.connect(lambda: self.open_settings("transfers"))
        tab.activity.connect(lambda: self._refresh_tab_label(tab))
        tab.saveRequested.connect(lambda: self.save_output(tab))
        tab.saveCommandRequested.connect(self.save_as_snippet)
        tab.bell.connect(lambda: self._tab_rang(tab))
        tab.silenceDetected.connect(lambda: self._tab_went_quiet(tab))
        tab.textSeen.connect(lambda seen: self._tab_saw_text(tab, seen))
        tab.responsesStopped.connect(
            lambda: self.notify(
                f"{self._tab_name(tab)}: automatic responses turned off, they kept answering", "error", 10000
            )
        )
        self._note_recent(session)

        self.stack.setCurrentIndex(1)
        pane.setCurrentIndex(index)
        self._set_active_pane(pane)
        if session.pinned:
            self.pin_tab(tab, True, announce=False)
        if self._compact:
            self._set_sidebar_visible(False)  # give the terminal the room on small screens
        # Connect once the tab is laid out so the remote PTY gets the real size.
        QTimer.singleShot(0, tab.connect_to_host)
        return tab

    def _close_icon(self) -> QIcon:
        """The tab close cross: muted at rest, light on the red it gets when pointed at."""
        if self._close_icon_cache is None:
            icon = QIcon(icon_pixmap("close", COLORS["muted"], 40))
            icon.addPixmap(icon_pixmap("close", COLORS["bg"], 40), QIcon.Mode.Active)
            self._close_icon_cache = icon
        return self._close_icon_cache

    def _paint_close_buttons(self, pane: QTabWidget, pointed: int | None = None) -> None:
        """Show the close cross on the current tab and the one pointed at only, so a row of tabs stays calm.

        The others keep their button, drawn empty, so that no tab changes width as the pointer moves.
        """
        bar = pane.tabBar()
        if pointed is None:
            pointed = bar.tabAt(bar.mapFromGlobal(QCursor.pos())) if bar.underMouse() else -1
        if self._blank_icon is None:
            blank = QPixmap(40, 40)
            blank.fill(Qt.GlobalColor.transparent)
            self._blank_icon = QIcon(blank)
        for index in range(bar.count()):
            button = bar.tabButton(index, QTabBar.ButtonPosition.RightSide)
            if button is not None:
                shown = index in (pane.currentIndex(), pointed) and not pane.widget(index).pinned
                button.setIcon(self._close_icon() if shown else self._blank_icon)

    def _tab_icon(self, tab: TerminalTab, state: str | None = None):
        key = (state or tab.state, tab.session.color, tab.in_broadcast)
        if key not in self._tab_icons:
            ring = COLORS["amber"] if key[2] else ""
            self._tab_icons[key] = tab_icon(COLORS[_STATE_COLOR_KEYS[key[0]]], key[1], ring)
        return self._tab_icons[key]

    def current_tab(self) -> TerminalTab | None:
        if self._active_pane in self.panes and self._active_pane.currentWidget():
            return self._active_pane.currentWidget()
        return next((pane.currentWidget() for pane in self.panes if pane.currentWidget()), None)

    def _set_active_pane(self, pane: QTabWidget) -> None:
        if pane is not self._active_pane:
            self._active_pane = pane
            self._mark_active_pane()
            self._update_status()

    def _mark_active_pane(self) -> None:
        """With several panes, only the active one's tab keeps the accent, so it is clear where typing goes."""
        several = len(self.panes) > 1
        for pane in self.panes:
            set_style_property(pane.tabBar(), "inactive", several and pane is not self._active_pane)

    def _on_focus_changed(self, _old, new) -> None:
        widget = new
        while widget is not None:
            if widget in self.panes:
                self._set_active_pane(widget)
                return
            widget = widget.parentWidget()

    def _normalize_panes(self) -> None:
        """Drop panes that have become empty; the first pane always stays."""
        before = len(self.panes)
        # The first pane carries the "+" button, so when it empties its neighbour's tabs move in.
        while self.tabs.count() == 0 and len(self.panes) > 1:
            donor = self.panes[1]
            for tab in [donor.widget(index) for index in range(donor.count())]:
                self._add_to_pane(self.tabs, tab)
            self._remove_pane(donor)
        for pane in [pane for pane in self.panes[1:] if pane.count() == 0]:
            self._remove_pane(pane)
        if len(self.panes) != before:
            self._lay_out_panes()
            self._balance_panes()
            self._arrange_panes()
        if self._active_pane not in self.panes or not self._active_pane.count():
            self._active_pane = self.tabs
        self._mark_active_pane()
        if not self.open_tabs():
            self.stack.setCurrentIndex(0)
        self._refresh_tab_labels()  # a closed tab's namesakes renumber
        self.tree.viewport().update()
        self._refresh_rail()
        self._update_status()
        self._update_command_placeholder()

    def close_tab(self, target=None, force: bool = False) -> None:
        tab = self._resolve(target)
        if tab is None:
            return
        if tab.pinned and not force:
            answer = QMessageBox.question(self, "Close Tab", f"\u201c{tab.title}\u201d is pinned. Close it anyway?")
            if answer != QMessageBox.StandardButton.Yes:
                return
        elif self.prefs.confirm_close_tab and not force and tab.state == STATE_CONNECTED:
            question = f"\u201c{tab.title}\u201d is still connected. Close it anyway?"
            if not self.confirm("Close Tab", question, "confirm_close_tab"):
                return
        pane = self._pane_of(tab)
        closed = (tab.session, tab.custom_title, tab.password, tab.pinned)
        tab.shutdown()
        pane.removeTab(pane.indexOf(tab))
        tab.deleteLater()
        self._normalize_panes()
        self._label_broadcast()
        if self._closing_batch is not None:
            self._closing_batch.append(closed)  # several tabs closed at once come back together
        else:
            self._remember_closed([closed])

    def _remember_closed(self, batch: list) -> None:
        """Keep closed tabs so they can be reopened, and offer that at once."""
        if not batch:
            return
        self._closed_tabs.append(batch)
        del self._closed_tabs[:-CLOSED_TABS_KEPT]
        what = f"“{batch[0][1] or batch[0][0].title}”" if len(batch) == 1 else f"{len(batch)} tabs"
        self.notify(f"Closed {what}", action=("Reopen", self.reopen_closed_tab))

    def _close_several(self, tabs: list[TerminalTab]) -> int:
        """Close tabs without asking, remembering them as one group to reopen."""
        tabs = [tab for tab in tabs if not tab.pinned]  # a pinned tab is closed on its own only
        self._closing_batch = []
        try:
            for tab in tabs:
                self.close_tab(tab, force=True)
        finally:
            batch, self._closing_batch = self._closing_batch, None
        self._remember_closed(batch)
        return len(batch)

    def reopen_closed_tab(self) -> list[TerminalTab]:
        """Open again the tab, or the group of tabs, closed most recently."""
        if not self._closed_tabs:
            self.notify("There is no closed tab to reopen")
            return []
        reopened = []
        for session, custom_title, password, pinned in self._closed_tabs.pop():
            tab = self.open_session(self.store.get(session.id) or session, password)
            if pinned:
                self.pin_tab(tab, True, announce=False)
            if custom_title:
                tab.custom_title = custom_title
                self._refresh_tab_labels()
            reopened.append(tab)
        return reopened

    def duplicate_tab(self, target=None) -> None:
        tab = self._resolve(target)
        if tab:
            self.open_session(tab.session, tab.password)

    def reconnect_tab(self, target=None) -> None:
        tab = self._resolve(target)
        if tab:
            tab.connect_to_host()

    def _place(self, tab: TerminalTab, destination: QTabWidget) -> None:
        index = self._add_to_pane(destination, tab)  # re-parenting takes it out of its old pane
        destination.setCurrentIndex(index)
        self._normalize_panes()
        self._set_active_pane(self._pane_of(tab))
        tab.terminal.setFocus()

    def split_tab(self, target=None) -> bool:
        """Open a new pane to the right of the tab's pane and move the tab into it."""
        tab = self._resolve(target)
        source = self._pane_of(tab) if tab else None
        if source is None or source.count() < 2 or len(self.panes) >= MAX_PANES:
            return False  # a pane's only tab has nothing to be split from
        destination = self._new_pane(after=source)
        self._balance_panes()
        self._place(tab, destination)
        self._arrange_panes()
        return True

    def move_tab(self, target=None, step: int = 1) -> bool:
        """Move a tab to the pane on its right (step 1) or left (step -1), if there is one."""
        tab = self._resolve(target)
        source = self._pane_of(tab) if tab else None
        if source is None:
            return False
        index = self.panes.index(source) + step
        if not 0 <= index < len(self.panes):
            return False
        self._place(tab, self.panes[index])
        return True

    def panes_stacked(self) -> bool:
        return not self._pane_columns and self.pane_splitter.orientation() == Qt.Orientation.Vertical

    def set_panes_stacked(self, stacked: bool) -> None:
        """Arrange split panes one above the other, or side by side; the choice is remembered.

        Choosing either leaves the grid.
        """
        if self.prefs.stack_panes != stacked or self.prefs.pane_grid:
            self.prefs.stack_panes = stacked
            self.prefs.pane_grid = False
            self.prefs.save()
        was_grid = self.panes_in_grid()
        self._lay_out_panes()
        if stacked != self.panes_stacked() or was_grid:
            self.pane_splitter.setOrientation(Qt.Orientation.Vertical if stacked else Qt.Orientation.Horizontal)
            self._balance_panes()

    def set_pane_grid(self, grid: bool) -> None:
        """Place three or four panes in two columns, or go back to one row or column; remembered."""
        if self.prefs.pane_grid != grid:
            self.prefs.pane_grid = grid
            self.prefs.save()
        self._lay_out_panes()
        self._balance_panes()
        self._arrange_panes()
        if grid and len(self.panes) < 3:
            self.notify("The grid is used once there are three or four panes")

    def toggle_pane_grid(self) -> None:
        self.set_pane_grid(not self.prefs.pane_grid)

    def _apply_pane_arrangement(self) -> None:
        """Follow the arrangement in the preferences, after Settings has changed them."""
        was_grid = self.panes_in_grid()
        self._lay_out_panes()
        if self.panes_in_grid():
            if not was_grid:
                self._balance_panes()
            return
        if self.prefs.stack_panes != self.panes_stacked() or was_grid:
            self.pane_splitter.setOrientation(
                Qt.Orientation.Vertical if self.prefs.stack_panes else Qt.Orientation.Horizontal
            )
            self._balance_panes()

    def toggle_pane_stacking(self) -> None:
        self.set_panes_stacked(not self.panes_stacked())

    def move_to_other_pane(self, target=None) -> None:
        """Ctrl+Shift+D: to the pane on the right; else split; else back to the first pane."""
        tab = self._resolve(target)
        if tab is None or self.move_tab(tab, 1) or self.split_tab(tab):
            return
        if len(self.panes) > 1:
            self._place(tab, self.tabs)

    def find_in_tab(self, target=None) -> None:
        tab = self._resolve(target)
        if tab:
            tab.show_find()

    def toggle_files(self, target=None) -> None:
        tab = self._resolve(target)
        if tab is None:
            return
        if tab.files_visible() or tab.can_browse_files():
            tab.toggle_files()
        else:
            self.notify("The file browser needs a connected SSH session", "error")

    def toggle_logging(self, target=None) -> None:
        tab = self._resolve(target)
        if tab is None:
            return
        if tab.is_logging():
            tab.stop_logging()
        else:
            suggested = tab.default_log_path()
            chosen, _ = QFileDialog.getSaveFileName(self, "Log to File", str(suggested), "Log files (*.log *.txt)")
            if chosen:
                from pathlib import Path

                tab.start_logging(Path(chosen))
        self._refresh_tab_label(tab)  # the tooltip says where the tab is logged to

    def _cycle_tab(self, step: int) -> None:
        pane = self._active_pane
        if pane.count():
            pane.setCurrentIndex((pane.currentIndex() + step) % pane.count())

    def _tab_list_menu(self) -> None:
        button = self.tab_list_button
        menu = self.tab_list()
        menu.exec(button.mapToGlobal(button.rect().bottomLeft()))
        menu.deleteLater()

    def tab_list(self) -> QMenu:
        """Every open tab of every pane, for when too many are open to see in the tab bar."""
        menu = QMenu(self)
        current = self.current_tab()
        tabs = self.open_tabs()
        rows = []
        for tab in tabs:
            action = menu.addAction(self._tab_icon(tab), self._tab_title(tab).replace("&", "&&"))
            action.setCheckable(True)
            action.setChecked(tab is current)
            action.triggered.connect(lambda _checked=False, t=tab: self._switch_to(KIND_TAB, t))
            rows.append((action, f"{self._tab_name(tab)} {tab.session.address}".lower()))
        if not tabs:
            menu.addAction("No tabs are open").setEnabled(False)
        if len(tabs) > self.prefs.tab_list_filter_at:
            # A long list gets a box to narrow it: every word typed must be in the tab's name or address.
            box = QLineEdit()
            box.setPlaceholderText("Filter tabs")
            box.setClearButtonEnabled(True)
            box.setAccessibleName("Filter tabs")
            holder = QWidgetAction(menu)
            holder.setDefaultWidget(box)

            def narrow(text: str) -> None:
                words = text.lower().split()
                for action, haystack in rows:
                    action.setVisible(all(word in haystack for word in words))

            def first_shown() -> None:
                shown = [action for action, _ in rows if action.isVisible()]
                if shown:
                    menu.close()
                    shown[0].trigger()

            box.textChanged.connect(narrow)
            box.returnPressed.connect(first_shown)
            menu.insertAction(menu.actions()[0], holder)
            menu.insertSeparator(menu.actions()[1])
            menu.aboutToShow.connect(box.setFocus)
            menu.filter_box = box  # for tests and callers that want to type into it
        return menu

    def _new_tab_menu(self) -> None:
        button = self.add_tab_button
        menu = self.new_tab_menu()
        menu.exec(button.mapToGlobal(button.rect().bottomLeft()))
        menu.deleteLater()

    def new_tab_menu(self) -> QMenu:
        """The + next to the tabs: one click to open a saved session.

        A short list shows every session. A long one would wrap into columns, so then only the
        favourites and the sessions used last are listed, and All Sessions has a submenu per folder.
        """
        menu = QMenu(self)
        sessions = sorted(self.store.sessions, key=lambda s: s.title.lower())
        if len(sessions) <= NEW_TAB_MENU_ALL:
            for session in sessions:
                menu.addAction(session.title, lambda s=session: self.open_session(s))
        else:
            favourites = [session for session in sessions if session.favourite]
            used = sorted((s for s in sessions if self.last_connected(s) and not s.favourite), key=self.last_connected, reverse=True)
            if favourites:
                menu.addSection("Favourites")
                for session in favourites[:NEW_TAB_MENU_SHORTLIST]:
                    menu.addAction(session.title, lambda s=session: self.open_session(s))
            if used:
                menu.addSection("Recent")
                for session in used[: max(3, NEW_TAB_MENU_SHORTLIST - len(favourites))]:
                    menu.addAction(session.title, lambda s=session: self.open_session(s))
            everything = menu.addMenu(f"All Sessions ({len(sessions)})")
            for group in self.store.groups():
                folder = everything.addMenu(group)
                for session in (s for s in sessions if s.group == group):
                    folder.addAction(session.title, lambda s=session: self.open_session(s))
            loose = [session for session in sessions if not session.group]
            if loose and self.store.groups():
                everything.addSeparator()
            for session in loose:
                everything.addAction(session.title, lambda s=session: self.open_session(s))
        if sessions:
            menu.addSeparator()
        self._add_shells(menu.addMenu("This Computer"))
        spaces = menu.addMenu("Workspaces")
        for name in self.workspaces.names():
            spaces.addAction(f"{name}  ({len(self.workspaces.get(name))})", lambda n=name: self.open_workspace(n))
        if self.workspaces.names():
            spaces.addSeparator()
        save = spaces.addAction("Save Open Tabs as a Workspace...", self.save_workspace)
        save.setEnabled(bool(self._saved_open_ids()))
        if self.workspaces.names():
            remove = spaces.addMenu("Delete Workspace")
            for name in self.workspaces.names():
                remove.addAction(name, lambda n=name: self.delete_workspace(n))
        menu.addAction(with_keys("New Session...", "new_session"), self.new_session)
        reopen = menu.addAction("Reopen Closed Tab", self.reopen_closed_tab)
        reopen.setEnabled(bool(self._closed_tabs))
        return menu

    def _tab_menu(self, pane: QTabWidget, pos) -> None:
        index = pane.tabBar().tabAt(pos)
        if index >= 0:
            menu = self.tab_menu(pane.widget(index))
            menu.exec(pane.tabBar().mapToGlobal(pos))
            menu.deleteLater()  # a new one is built each time

    def tab_menu(self, tab: TerminalTab) -> QMenu:
        """The menu of one tab. Everyday actions sit at the top; the rest are grouped in
        submenus, so the menu stays one short column even on a small screen."""
        pane = self._pane_of(tab)
        connected = tab.state == STATE_CONNECTED
        menu = QMenu(self)
        menu.setToolTipsVisible(True)
        menu.addAction(with_keys("Reconnect", "reconnect"), lambda: self.reconnect_tab(tab))
        menu.addAction("Disconnect", tab.disconnect_from_host).setEnabled(tab.state != STATE_DISCONNECTED)
        menu.addAction(with_keys("Duplicate Tab", "duplicate_tab"), lambda: self.duplicate_tab(tab))
        menu.addAction("Rename Tab...", lambda: self.rename_tab(tab))
        menu.addAction(with_keys("Unpin Tab" if tab.pinned else "Pin Tab", "pin_tab"), lambda: self.pin_tab(tab))
        if self.store.get(tab.session.id) is None:  # opened by quick connect or from the command line
            menu.addAction("Save as Session", lambda: self.save_tab_as_session(tab))
        menu.addSeparator()
        menu.addAction(with_keys("Find...", "find"), lambda: self.find_in_tab(tab))
        files = menu.addAction(
            with_keys("Close File Browser" if tab.files_visible() else "File Browser...", "files"),
            lambda: self.toggle_files(tab),
        )
        files.setEnabled(tab.files_visible() or tab.can_browse_files())
        menu.addAction("Connection Info...", lambda: self.show_connection_info(tab))
        menu.addSeparator()

        send = menu.addMenu("Send")
        include = send.addAction("Include in Broadcast", lambda: self.set_in_broadcast(tab))
        include.setCheckable(True)
        include.setChecked(tab.in_broadcast)
        send.addAction("Clear Broadcast Marks", self.clear_broadcast_marks).setEnabled(
            any(other.in_broadcast for other in self.open_tabs())
        )
        send.addSeparator()
        send.addAction("Send Text File...", lambda: self.send_text_file(tab)).setEnabled(connected)
        brk = send.addAction("Send Break", lambda: self.send_break(tab))
        brk.setEnabled(tab.can_send_break())
        brk.setToolTip("Telnet, Serial and SSH: what routers and consoles use to interrupt or open a boot menu")
        if tab.has_responses():
            responding = send.addAction("Automatic Responses", lambda: self.toggle_responses(tab))
            responding.setCheckable(True)
            responding.setChecked(not tab.responses_paused)

        output = menu.addMenu("Output")
        output.addAction(
            with_keys("Stop Logging" if tab.is_logging() else "Log to File...", "logging"), lambda: self.toggle_logging(tab)
        )
        output.addAction("Save Output...", lambda: self.save_output(tab))
        output.addSeparator()
        output.addAction(
            "Stop Watching for Silence" if tab.is_watching() else "Notify When Output Stops",
            lambda: tab.stop_watching() if tab.is_watching() else tab.watch_for_silence(),
        )
        if tab.watched_text():
            output.addAction(f"Stop Watching for “{tab.watched_text()}”", tab.stop_watching_text)
        else:
            output.addAction("Notify When Output Contains...", lambda: self.watch_for_text(tab))

        ssh = menu.addMenu("SSH")
        ssh.addAction("Tunnels...", lambda: self.show_tunnels(tab)).setEnabled(tab.can_manage_tunnels())
        ssh.addAction("Install Public Key on Server...", lambda: self.install_public_key(tab)).setEnabled(
            tab.can_browse_files()
        )
        ssh.addAction("Generate SSH Key Pair...", self.generate_key)

        panes = menu.addMenu("Panes")
        position = self.panes.index(pane)
        stacked, grid = self.panes_stacked(), self.panes_in_grid()
        split_name = "Split to a New Pane" if grid or self.prefs.pane_grid else "Split Down" if stacked else "Split Right"
        panes.addAction(split_name, lambda: self.split_tab(tab)).setEnabled(pane.count() > 1 and len(self.panes) < MAX_PANES)
        earlier, later = ("Previous", "Next") if grid else ("Upper", "Lower") if stacked else ("Left", "Right")
        panes.addAction(f"Move to {earlier} Pane", lambda: self.move_tab(tab, -1)).setEnabled(position > 0)
        panes.addAction(with_keys(f"Move to {later} Pane", "move_pane"), lambda: self.move_tab(tab, 1)).setEnabled(
            position < len(self.panes) - 1
        )
        panes.addSeparator()
        panes.addAction(
            with_keys("Place Panes Side by Side" if stacked else "Stack Panes", "stack_panes"), self.toggle_pane_stacking
        )
        in_grid = panes.addAction("Grid of Panes", self.toggle_pane_grid)
        in_grid.setCheckable(True)
        in_grid.setChecked(self.prefs.pane_grid)
        menu.addSeparator()

        menu.addAction(with_keys("Close", "close_tab"), lambda: self.close_tab(tab))
        others = menu.addMenu("Other Tabs")
        others.addAction("Close Other Tabs", lambda: self._close_others(pane, tab)).setEnabled(pane.count() > 1)
        everyone = self.open_tabs()
        others.addAction("Pin All Tabs", lambda: self.pin_all(True)).setEnabled(any(not t.pinned for t in everyone))
        others.addAction("Unpin All Tabs", lambda: self.pin_all(False)).setEnabled(any(t.pinned for t in everyone))
        others.addAction("Close All Unpinned Tabs", self.close_unpinned).setEnabled(
            any(not other.pinned for other in self.open_tabs())
        )
        others.addAction("Close Tabs to the Right", lambda: self.close_tabs_right(tab)).setEnabled(
            pane.indexOf(tab) < pane.count() - 1
        )
        dead = [other for other in self.open_tabs() if other.state == STATE_DISCONNECTED]
        others.addSeparator()
        others.addAction(f"Close Disconnected Tabs ({len(dead)})", self.close_disconnected).setEnabled(bool(dead))
        others.addAction(f"Reconnect Disconnected Tabs ({len(dead)})", self.reconnect_disconnected).setEnabled(bool(dead))
        others.addSeparator()
        others.addAction("Reopen Closed Tab", self.reopen_closed_tab).setEnabled(bool(self._closed_tabs))
        return menu

    def _close_others(self, pane: QTabWidget, keep: TerminalTab) -> None:
        self._close_several([pane.widget(index) for index in range(pane.count()) if pane.widget(index) is not keep])

    def close_tabs_right(self, target=None) -> None:
        tab = self._resolve(target)
        pane = self._pane_of(tab) if tab else None
        if pane is None:
            return
        self._close_several([pane.widget(index) for index in range(pane.indexOf(tab) + 1, pane.count())])

    def pin_all(self, pinned: bool = True) -> int:
        """Pin every open tab, or unpin them all; returns how many changed."""
        changing = [tab for tab in self.open_tabs() if tab.pinned != pinned]
        for tab in changing:
            tab.pinned = pinned
        for pane in self.panes:
            self._settle_pins(pane)  # the order each tab had is kept
        self._refresh_tab_labels()
        if changing:
            self.notify(f"{'Pinned' if pinned else 'Unpinned'} {len(changing)} tab{'' if len(changing) == 1 else 's'}")
        return len(changing)

    def close_unpinned(self) -> int:
        """Close every tab that is not pinned, in all panes; Reopen Closed Tab brings them back together."""
        return self._close_several([tab for tab in self.open_tabs() if not tab.pinned])

    def close_disconnected(self) -> int:
        """Close every tab whose connection has ended; returns how many were closed."""
        return self._close_several([tab for tab in self.open_tabs() if tab.state == STATE_DISCONNECTED])

    def reconnect_disconnected(self) -> int:
        dead = [tab for tab in self.open_tabs() if tab.state == STATE_DISCONNECTED]
        for tab in dead:
            tab.connect_to_host()
        return len(dead)

    def eventFilter(self, obj, event):
        if (
            obj is getattr(self, "status_left", None)
            and event.type() == QEvent.Type.MouseButtonRelease
            and event.button() == Qt.MouseButton.LeftButton
            and self.current_tab() is not None
        ):
            self.show_connection_info()
            return True
        if (
            obj is getattr(self, "status_right", None)
            and event.type() == QEvent.Type.MouseButtonRelease
            and event.button() == Qt.MouseButton.LeftButton
            and self.status_right.toolTip()  # it has the monitor's figures
        ):
            self.show_server_monitor()
            return True
        if (
            obj is getattr(self, "logo", None)
            and event.type() == QEvent.Type.MouseButtonRelease
            and event.button() == Qt.MouseButton.LeftButton
        ):
            self.open_settings("about")
            return True
        if obj is getattr(self, "pane_splitter", None) and event.type() == QEvent.Type.Resize:
            # After the resize has been handled, so that the panes are measured at their new size.
            QTimer.singleShot(0, self._arrange_panes)
        if obj is getattr(self, "search", None) and event.type() == QEvent.Type.KeyPress:
            if self._search_key(event.key()):
                return True
        if event.type() in (QEvent.Type.HoverMove, QEvent.Type.HoverLeave) and isinstance(obj, QTabBar):
            for pane in self.panes:
                if obj is pane.tabBar():
                    moved = event.type() == QEvent.Type.HoverMove
                    self._paint_close_buttons(pane, obj.tabAt(event.position().toPoint()) if moved else -1)
        if event.type() == QEvent.Type.MouseButtonRelease and event.button() == Qt.MouseButton.LeftButton:
            for pane in self.panes:
                if obj is pane.tabBar():  # a drag has ended: put a pinned tab back among the pinned ones
                    QTimer.singleShot(0, lambda pane=pane: pane in self.panes and self._keep_pins_first(pane))
        if event.type() == QEvent.Type.MouseButtonRelease and event.button() == Qt.MouseButton.MiddleButton:
            for pane in self.panes:
                if obj is pane.tabBar():
                    index = obj.tabAt(event.position().toPoint())
                    if index >= 0:
                        self.close_tab(pane.widget(index))
                        return True
        return super().eventFilter(obj, event)

    def _on_tab_state(self, tab: TerminalTab, state: str) -> None:
        pane = self._pane_of(tab)
        if pane:
            pane.setTabIcon(pane.indexOf(tab), self._tab_icon(tab))
            pane.setTabToolTip(pane.indexOf(tab), self._tab_tooltip(tab))
        self.tree.viewport().update()
        self._refresh_rail()
        self._update_status()
        self._update_command_placeholder()  # the broadcast count follows the connected tabs
        if state == STATE_CONNECTED and self.store.get(tab.session.id) is not None:
            self.settings.setValue(f"last_connected/{tab.session.id}", time.time())
            if self.sidebar_sort() == SORT_RECENT:
                self._reload_tree()
        if state == STATE_DISCONNECTED and pane and not tab.isVisible():
            self._background_tab_dropped(tab)

    def _background_tab_dropped(self, tab: TerminalTab) -> None:
        """A tab out of sight lost its connection: a red dot alone is easy to miss."""
        if not tab.unseen_output:
            tab.unseen_output = True
            self._refresh_tab_label(tab)
        reconnect = ("Reconnect", lambda: self._reconnect_if_open(tab))
        self.notify(f"{self._tab_name(tab)} is disconnected", "error", action=reconnect)
        if not self.isActiveWindow():
            QApplication.alert(self)

    @staticmethod
    def _tab_tooltip(tab: TerminalTab) -> str:
        protocol = dict(PROTOCOLS)[tab.session.protocol]
        lines = [tab.title, f"{protocol}  {tab.session.address}", tab.state.capitalize()]
        if tab.is_logging():
            lines.append(f"Logging to {tab.log_path()}")
        if tab.in_broadcast:
            lines.append("Marked to receive broadcasts")
        return "\n".join(lines)

    def _tab_name(self, tab: TerminalTab) -> str:
        """The tab's name, numbered when other tabs have it too: "web", "web (2)", "web (3)".

        Messages about a tab use it as well, so that they say which of them is meant.
        """
        # While a tab is being added its pane can briefly hold an empty slot, or a tab not yet numbered.
        order = getattr(tab, "opened_order", None)
        older = sum(
            1
            for other in self.open_tabs()
            if other is not None
            and other is not tab
            and other.title == tab.title
            and getattr(other, "opened_order", order) < order
        ) if order is not None else 0
        return tab.title + (f" ({older + 1})" if older else "")

    def _tab_title(self, tab: TerminalTab) -> str:
        """The tab's name with its marks, in full."""
        return (PIN_MARK if tab.pinned else "") + self._tab_name(tab) + (UNSEEN_MARK if tab.unseen_output else "")

    def _tab_label(self, tab: TerminalTab) -> str:
        """What the tab shows: its title, or only the dot for new output when pinned tabs show their icon only."""
        if tab.pinned and self.prefs.compact_pinned:
            return UNSEEN_MARK.strip() if tab.unseen_output else ""
        return self._tab_title(tab)

    def _refresh_tab_labels(self) -> None:
        for tab in self.open_tabs():
            self._refresh_tab_label(tab)

    def _refresh_tab_label(self, tab: TerminalTab) -> None:
        pane = self._pane_of(tab)
        if pane is None:
            return
        if pane.currentWidget() is tab:
            tab.unseen_output = False  # it is on screen, so its output has been seen
        pane.setTabText(pane.indexOf(tab), self._tab_label(tab))
        pane.setTabToolTip(pane.indexOf(tab), self._tab_tooltip(tab))
        close = pane.tabBar().tabButton(pane.indexOf(tab), QTabBar.ButtonPosition.RightSide)
        if close is not None:
            close.setAccessibleName(f"Close {tab.title}")

    def _tab_rang(self, tab: TerminalTab) -> None:
        """The bell: mark a background tab, and draw attention to the window if it is not in front."""
        if not tab.isVisible() and not tab.unseen_output:
            tab.unseen_output = True
            self._refresh_tab_label(tab)
        if not self.isActiveWindow():
            QApplication.alert(self)

    def _tab_went_quiet(self, tab: TerminalTab) -> None:
        """A tab that was being watched has stopped producing output: its command is probably done."""
        self.notify(f"{self._tab_name(tab)}: output has stopped", "success", 10000)
        if not tab.isVisible() and not tab.unseen_output:
            tab.unseen_output = True
            self._refresh_tab_label(tab)
        QApplication.alert(self)

    def watch_for_text(self, target=None, wanted: str | None = None) -> bool:
        """Be told when a tab's output contains some text: a build finishing, an error, a prompt."""
        tab = self._resolve(target)
        if tab is None:
            return False
        if wanted is None:
            wanted, ok = QInputDialog.getText(
                self,
                "Notify When Output Contains",
                "Text to wait for, in any case (start the command first, so its own echo does not count)",
                text=tab.watched_text(),
            )
            if not ok:
                return False
        if not tab.watch_for_text(wanted):
            return False
        self.notify(f"Watching {tab.title} for “{wanted.strip()}”")
        return True

    def _tab_saw_text(self, tab: TerminalTab, seen: str) -> None:
        self.notify(f"{self._tab_name(tab)}: “{seen}” appeared in the output", "success", 10000)
        if not tab.isVisible() and not tab.unseen_output:
            tab.unseen_output = True
            self._refresh_tab_label(tab)
        QApplication.alert(self)

    def send_break(self, target=None) -> bool:
        """Send a break signal to a tab: Telnet, Serial and SSH (when the server passes it on) have one."""
        tab = self._resolve(target)
        if tab is None:
            return False
        if tab.state != STATE_CONNECTED:
            self.notify(f"{tab.title} is not connected", "error")
            return False
        if not tab.can_send_break():
            protocol = dict(PROTOCOLS)[tab.session.protocol]
            self.notify(f"{protocol} connections have no break signal", "error")
            return False
        if not tab.send_break():
            self.notify(f"The break could not be sent to {tab.title}", "error")
            return False
        self.notify(f"Break sent to {tab.title}", "success")
        return True

    def send_text_file(self, target=None, path=None, confirm: bool = True) -> bool:
        """Type a text file into a tab, such as a configuration for a router.

        The pause between pasted lines (Settings, Terminal) applies, and Esc stops it.
        """
        tab = self._resolve(target)
        if tab is None:
            return False
        if tab.state != STATE_CONNECTED:
            self.notify(f"{tab.title} is not connected", "error")
            return False
        if path is None:
            path, _ = QFileDialog.getOpenFileName(
                self, "Send Text File", "", "Text files (*.txt *.cfg *.conf *.sh);;All files (*)"
            )
            if not path:
                return False
        try:
            data = Path(path).read_bytes()
        except OSError as exc:
            self.notify(f"Could not read {Path(path).name}: {exc.strerror or exc}", "error")
            return False
        if len(data) > MAX_SEND_FILE_BYTES:
            self.notify(f"{Path(path).name} is too large to type into a terminal (over 1 MB)", "error")
            return False
        if b"\0" in data[:8192]:
            self.notify(f"{Path(path).name} is not a text file", "error")
            return False
        text = data.decode("utf-8-sig", "replace")
        lines = text.rstrip("\r\n").splitlines()
        if not lines:
            self.notify(f"{Path(path).name} is empty", "error")
            return False
        if confirm:
            pace = (
                f"one line every {self.prefs.paste_line_delay_ms} ms; Esc stops it"
                if self.prefs.paste_line_delay_ms
                else "all at once; for slow devices set a pause between pasted lines in Settings, Terminal"
            )
            answer = QMessageBox.question(
                self,
                "Send Text File",
                f"Send {len(lines)} line{'s' if len(lines) != 1 else ''} of {Path(path).name} to {tab.title}?\n\n"
                f"Each line is run as it arrives, {pace}.",
            )
            if answer != QMessageBox.StandardButton.Yes:
                return False
        tab.terminal.paste_text(text)
        self.notify(f"Sending {Path(path).name} to {tab.title}", "success")
        return True

    def toggle_responses(self, target=None) -> bool:
        """Pause a tab's automatic responses, or resume them; returns whether they are now on."""
        tab = self._resolve(target)
        if tab is None:
            return False
        if not tab.has_responses():
            self.notify(f"{tab.title} has no automatic responses; add them in the session's settings", "error")
            return False
        tab.set_responses_paused(not tab.responses_paused)
        self.notify(f"Automatic responses {'paused' if tab.responses_paused else 'on'} for {tab.title}")
        return not tab.responses_paused

    def show_tunnels(self, target=None) -> TunnelsDialog | None:
        """See, stop and start the tunnels of a connected SSH tab without reconnecting it."""
        tab = self._resolve(target)
        if tab is None:
            return None
        if not tab.can_manage_tunnels():
            self.notify("Tunnels are for a connected SSH tab", "error")
            return None
        dialog = TunnelsDialog(tab, self.store.get(tab.session.id) is not None, self)
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        dialog.keepRequested.connect(lambda text: self._keep_tunnels(tab, text))
        dialog.show()
        return dialog

    def _keep_tunnels(self, tab: TerminalTab, text: str) -> None:
        """Add tunnels started on a live tab to its saved session too."""
        saved = self.store.get(tab.session.id)
        if saved is None:
            return
        updated = replace(saved, tunnels=", ".join(part for part in (saved.tunnels.strip(), text.strip()) if part))
        self.store.upsert(updated)
        tab.set_session(updated)
        self.notify(f"Tunnels kept in “{updated.title}”", "success")

    def show_connection_info(self, target=None) -> ConnectionInfoDialog | None:
        tab = self._resolve(target)
        if tab is None:
            return None
        dialog = ConnectionInfoDialog(tab.connection_details(), self)
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        dialog.show()
        return dialog

    def show_host_keys(self, path=KNOWN_HOSTS_FILE) -> HostKeysDialog:
        """The host keys that were approved, with a way to forget them so they are asked about again."""
        dialog = HostKeysDialog(approved_keys(path), lambda host: forget_host(host, path), self)
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        dialog.show()
        return dialog

    # ---- workspaces -----------------------------------------------------------------------

    def _saved_open_ids(self) -> list[str]:
        return [tab.session.id for tab in self.open_tabs() if self.store.get(tab.session.id) is not None]

    def save_workspace(self, name: str | None = None) -> bool:
        """Remember the saved sessions that are open now under a name, to open them together later."""
        ids = self._saved_open_ids()
        if not ids:
            self.notify("Open some saved sessions first", "error")
            return False
        if name is None or isinstance(name, bool):
            name, ok = QInputDialog.getText(self, "Save Workspace", "Name for this set of tabs")
            if not ok:
                return False
        name = name.strip()
        if not name:
            return False
        layout = self._tab_layout(local_shells=False)  # the saved sessions only, as in the workspace
        self.workspaces.set(name, ids, {key: value for key, value in layout.items() if key != "entries"})
        self._fill_recent()
        self.notify(f"Saved {len(ids)} sessions as the workspace \u201c{name}\u201d", "success")
        return True

    def delete_workspace(self, name: str, confirm: bool = True) -> bool:
        if confirm:
            answer = QMessageBox.question(
                self, "Delete Workspace", f"Delete the workspace “{name}”? Its sessions are kept."
            )
            if answer != QMessageBox.StandardButton.Yes:
                return False
        self.workspaces.remove(name)
        self._fill_recent()
        self.notify(f"Deleted the workspace “{name}”")
        return True

    def open_workspace(self, name: str) -> list[TerminalTab]:
        layout = self.workspaces.get_layout(name)
        layout["entries"] = self.workspaces.get(name)
        return self._open_layout(layout)

    # ---- which servers answer ---------------------------------------------------------------

    def check_connections(self, sessions: list[Session] | None = None) -> None:
        """Test, without logging in, which saved sessions can be reached; the sidebar shows the result."""
        if self._probe_many is not None and self._probe_many.isRunning():
            return
        sessions = list(self.store.sessions if sessions is None or isinstance(sessions, bool) else sessions)
        if not sessions:
            return
        for session in sessions:
            self._reachable.pop(session.id, None)
        self._probe_total = len(sessions)
        self._probe_many = connections.ProbeMany(sessions)
        self._probe_many.result.connect(self._connection_checked)
        self._probe_many.done.connect(self._connections_checked)
        self._probe_many.start()
        threads.park(self._probe_many)
        self.notify(f"Checking {len(sessions)} session{'' if len(sessions) == 1 else 's'}...", duration=0)

    def _connection_checked(self, session_id: str, reachable: bool, sentence: str) -> None:
        self._reachable[session_id] = (reachable, sentence)
        self.tree.viewport().update()

    def _connections_checked(self) -> None:
        self._reload_tree()  # so that pointing at a session says why it does not answer
        answering = sum(1 for reachable, _sentence in self._reachable.values() if reachable)
        self.notify(f"{answering} of {self._probe_total} checked sessions answer", "success" if answering == self._probe_total else "info", 8000)

    def reachability(self, session: Session) -> tuple[bool, str] | None:
        """The result of the last connection check for a session, if it was checked."""
        return self._reachable.get(session.id)

    def _keep_pins_first(self, pane: QTabWidget) -> None:
        """After a tab is moved: the pinned tabs sit before the others, in the order they had.

        While a tab is being dragged this waits for the mouse button to come up.
        """
        self._paint_close_buttons(pane)
        if not QApplication.mouseButtons() & Qt.MouseButton.LeftButton:
            self._settle_pins(pane)

    def _settle_pins(self, pane: QTabWidget) -> None:
        if self._reordering:
            return
        order = [pane.widget(i) for i in range(pane.count())]
        wanted = [t for t in order if t.pinned] + [t for t in order if not t.pinned]
        self._reordering = True  # moving a tab signals again
        try:
            for index, tab in enumerate(wanted):
                if pane.widget(index) is not tab:
                    pane.tabBar().moveTab(pane.indexOf(tab), index)
        finally:
            self._reordering = False
        self._paint_close_buttons(pane)

    def pin_tab(self, target=None, pinned: bool | None = None, announce: bool = True) -> None:
        """Pin a tab, or unpin it: it moves to the left of its pane, loses its close cross and
        is left alone by Close Other Tabs and the like."""
        tab = self._resolve(target)
        pane = self._pane_of(tab) if tab else None
        if pane is None:
            return
        tab.pinned = (not tab.pinned) if pinned is None else pinned
        others = sum(1 for i in range(pane.count()) if pane.widget(i) is not tab and pane.widget(i).pinned)
        self._reordering = True
        try:
            pane.tabBar().moveTab(pane.indexOf(tab), others)  # pinned tabs first; an unpinned one follows them
        finally:
            self._reordering = False
        self._refresh_tab_labels()
        self._paint_close_buttons(pane)
        if announce:
            self.notify(f"Pinned \u201c{tab.title}\u201d" if tab.pinned else f"Unpinned \u201c{tab.title}\u201d")

    def rename_tab(self, target=None, name: str | None = None) -> None:
        """Give one tab a name of its own; an empty name goes back to the session's name."""
        tab = self._resolve(target)
        if tab is None:
            return
        if name is None:
            name, ok = QInputDialog.getText(
                self, "Rename Tab", "Name for this tab (empty for the session's own name)", text=tab.title
            )
            if not ok:
                return
        tab.custom_title = name.strip() if name.strip() != tab.session.title else ""
        self._refresh_tab_labels()  # the old name's and the new name's numbering both change
        self._update_status()

    def _probe_server(self) -> None:
        """Measure the active tab's server: its round trip, and its load, memory and disk where it has them."""
        tab = self.current_tab()
        if tab is not None:
            self._probe_tab(tab)

    def _probe_background(self) -> None:
        """Measure the servers of the other connected tabs too, less often, so that their alerts can be raised."""
        if self.prefs.server_alerts:
            current = self.current_tab()
            for tab in self.open_tabs():
                if tab is not current:
                    self._probe_tab(tab)

    def _probe_tab(self, tab: TerminalTab) -> None:
        if not self.prefs.server_monitor or id(tab) in self._stats_probes:
            return
        round_trip = tab.latency_meter()
        run_command = tab.command_runner()
        if getattr(tab, "stats_failures", 0) >= monitor.GIVE_UP_AFTER:
            run_command = None  # this server does not have the figures; the round trip is still worth knowing
        if round_trip is None and run_command is None:
            return
        probe = self._stats_probes[id(tab)] = StatsProbe(tab, run_command, round_trip)
        probe.done.connect(self._server_probed)
        probe.start()
        threads.park(probe)  # so that quitting while it asks waits for it

    def _server_probed(self, tab: TerminalTab, stats, latency) -> None:
        probe = self._stats_probes.pop(id(tab), None)
        if probe is not None:
            threads.park(probe)
        if sip.isdeleted(tab):
            return  # the tab was closed while the server was being asked
        if probe is not None and probe.asks_for_figures:
            tab.stats_failures = 0 if stats is not None else getattr(tab, "stats_failures", 0) + 1
            tab.server_stats = stats
        tab.latency = latency
        if not hasattr(tab, "monitor_history"):
            tab.monitor_history = new_history()
        tab.monitor_history.append(Sample(time.time(), latency, getattr(tab, "server_stats", None)))
        self._raise_server_alerts(tab)
        self._update_status()

    def _raise_server_alerts(self, tab: TerminalTab) -> list[str]:
        """Tell of figures that have just turned critical; returns their words. Each is told once, and again only
        after it has been back to normal."""
        if not self.prefs.server_alerts:
            return []
        now = monitor.critical(getattr(tab, "server_stats", None), getattr(tab, "latency", None))
        fresh = [words for key, words in now.items() if key not in getattr(tab, "alerting", set())]
        tab.alerting = set(now)
        if not fresh:
            return []
        message = "; ".join(fresh)
        looking = self.isActiveWindow()
        if looking and tab is self.current_tab():
            return fresh  # it is in red in the status bar, in front of them
        def show():
            self.come_forward([])
            self._switch_to(KIND_TAB, tab)

        if looking:
            self.notify(f"{tab.title}: {message}", "error", action=("Show", show))
        else:
            self.notifier.show(f"{tab.title} needs attention", message, show)
            QApplication.alert(self)
        return fresh

    def _stats_text(self, tab: TerminalTab) -> str:
        """The server monitor's part of the status bar, or "" when there is nothing to show."""
        stats, latency = getattr(tab, "server_stats", None), getattr(tab, "latency", None)
        if not self.prefs.server_monitor or tab.state != STATE_CONNECTED:
            return ""

        def coloured(color: str, text: str) -> str:
            return f'<span style="color:{COLORS[color]}">{text}</span>' if color else text

        shown = []
        if latency is not None:
            shown.append(coloured(monitor.latency_level(latency), f"{latency * 1000:.0f} ms"))
        if stats is not None:
            shown += [
                coloured(monitor.level(stats, "load"), f"CPU {stats.load:.2f}"),
                coloured(monitor.level(stats, "memory"), f"Mem {stats.memory}%"),
                coloured(monitor.level(stats, "disk"), f"Disk {stats.disk}%"),
            ]
        return "&nbsp;&nbsp;".join(shown)

    def _on_current_changed(self, pane: QTabWidget) -> None:
        self._paint_close_buttons(pane)
        QTimer.singleShot(300, self._probe_server)  # figures for the tab now in front, without waiting
        tab = pane.currentWidget()
        if tab and tab.unseen_output:
            self._refresh_tab_label(tab)
        if tab and pane is self._active_pane:
            tab.terminal.setFocus()
        self._update_status()

    def _update_status(self) -> None:
        tab = self.current_tab()
        if tab is None:
            self.status_left.setText("Ready")
            self.status_left.setToolTip("")
            self.status_left.unsetCursor()
            count = len(self.store.sessions)
            self.status_right.setText(f"{count} saved session{'' if count == 1 else 's'}")
            self.status_right.setToolTip("")
            self.setWindowTitle(APP_NAME)
            return
        color = COLORS[_STATE_COLOR_KEYS[tab.state]]
        # A narrow window keeps what matters most: the state, the size and the time connected.
        narrow = self.width() < NARROW_WIDTH
        address = "" if narrow else f"&nbsp; • &nbsp;{tab.session.address}"
        self.status_left.setText(f'<span style="color:{color}">●</span>&nbsp; {tab.state.capitalize()}{address}')
        self.status_left.setToolTip("Connection info")
        self.status_left.setCursor(Qt.CursorShape.PointingHandCursor)
        term = tab.terminal
        protocol = dict(PROTOCOLS)[tab.session.protocol]
        parts = [f"{term.cols} × {term.rows}"] if narrow else [protocol, f"{term.cols} × {term.rows}", term.encoding().upper()]
        if tab.connected_at:
            # How long the connection has been up, and how much has gone each way.
            seconds = int(tab.duration())
            parts.append(f"{seconds // 3600:02d}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}")
            if not narrow:
                parts.append(f"↓ {human_size(tab.bytes_in)}  ↑ {human_size(tab.bytes_out)}")
        parts = [escape(part) for part in parts]
        stats = "" if narrow else self._stats_text(tab)
        if stats:
            parts.insert(0, stats)
        self.status_right.setText("&nbsp;&nbsp; • &nbsp;&nbsp;".join(parts))
        self.status_right.setToolTip(monitor.summary(getattr(tab, "server_stats", None), getattr(tab, "latency", None)) if stats else "")
        self.setWindowTitle(f"{self._tab_name(tab)} - {APP_NAME}")
        if self._monitor_window is not None and self._monitor_window.isVisible():
            self._monitor_window.show_tab(tab)

    def show_server_monitor(self) -> ServerMonitorWindow:
        """The last half hour of the active tab's server, in a window of its own that follows the tab in front."""
        if self._monitor_window is None:
            self._monitor_window = ServerMonitorWindow(self)
        self._monitor_window.show_tab(self.current_tab())
        self._monitor_window.show()
        self._monitor_window.raise_()
        self._monitor_window.activateWindow()
        return self._monitor_window

    # ---- settings -------------------------------------------------------------

    def open_settings(self, page: str | None = None) -> None:
        """Show the settings, on one of their pages (a click passes a bool, which is no page)."""
        dialog = SettingsDialog(self.prefs, self)
        dialog.applied.connect(self.apply_preferences)
        if isinstance(page, str):
            dialog.show_page(page)
        dialog.exec()

    def apply_preferences(self, prefs: Preferences) -> None:
        """Adopt, save and apply a new set of preferences to everything that is open."""
        self.prefs = prefs
        prefs.save()
        apply_appearance(prefs.theme, prefs.accent, prefs.ui_font, prefs.ui_size, prefs.terminal_scheme)
        self.retheme()
        for tab in self.open_tabs():
            tab.apply_preferences(prefs)
        self._apply_send_bar()
        self._apply_pane_arrangement()
        self._refresh_tab_labels()  # pinned tabs may show their icon only
        self._reload_tree()  # row heights follow the interface font

    def retheme(self) -> None:
        """Repaint everything that was drawn in theme colours rather than styled by the stylesheet."""
        for refresh in self._themed:
            refresh()
        self._tab_icons.clear()
        self._close_icon_cache = None
        for tab in self.open_tabs():
            pane = self._pane_of(tab)
            index = pane.indexOf(tab)
            pane.setTabIcon(index, self._tab_icon(tab))
            close = pane.tabBar().tabButton(index, QTabBar.ButtonPosition.RightSide)
            if close is not None:
                close.setIcon(self._close_icon())
            tab.retheme()
        for pane in self.panes:
            self._paint_close_buttons(pane)
        self.tree.itemDelegate().retheme()
        self.tree.viewport().update()
        self._refresh_rail()
        self._update_status()

    def _on_system_theme_changed(self) -> None:
        if self.prefs.theme == THEME_SYSTEM:
            apply_appearance(
                self.prefs.theme, self.prefs.accent, self.prefs.ui_font, self.prefs.ui_size, self.prefs.terminal_scheme
            )
            self.retheme()

    def _internal_error(self, _text: str) -> None:
        """Something unexpected went wrong inside the application; it was logged, say so."""
        self.notify(f"An internal error occurred; details are in {errors.ERROR_LOG}", "error", 10000)

    def _set_transfer_limit(self, kbps: int) -> None:
        """The speed limit was changed in a file browser; remember it for the next one."""
        self.prefs.transfer_limit_kbps = kbps
        self.prefs.save()

    def _set_font_size(self, size: int) -> None:
        """A terminal was zoomed with the keyboard or wheel; keep every tab in step."""
        if size == self.prefs.terminal_size:
            return
        self.prefs.terminal_size = size
        self.prefs.save()
        for tab in self.open_tabs():
            tab.terminal.set_font_size(size)

    # ---- actions --------------------------------------------------------------

    def _focus_quick_connect(self) -> None:
        self.quick.setFocus()
        self.quick.selectAll()

    @staticmethod
    def parse_quick_connect(text: str) -> Session | None:
        """A session for "user@host[:port]" or "protocol://..."; None if the text is not in that form."""
        local = _LOCAL_TARGET.match(text.strip())
        if local:
            return local_session(local["program"] or "")
        match = _QUICK_CONNECT.match(text.strip())
        if not match:
            return None
        protocol = (match["protocol"] or PROTO_SSH).lower()
        port = int(match["port"] or DEFAULT_PORTS[protocol])
        if port < 1 or (protocol != PROTO_SERIAL and port > 65535):
            return None
        username = match["user"] or (getpass.getuser() if protocol == PROTO_SSH else "")
        return Session(protocol=protocol, host=match["host"], port=port, username=username, auth=AUTH_AGENT)

    def _quick_connect(self) -> None:
        text = self.quick.text().strip()
        tab = self.open_target(text)
        if tab is None:
            self.notify("Quick connect expects user@host[:port] or the name of a saved session", "error")
            return
        if self.parse_quick_connect(text) is not None and not self._saved_named(text):
            history = [text] + [entry for entry in self.quick_history() if entry != text]
            self.settings.setValue("quick_history", history[:QUICK_HISTORY_LENGTH])
            # An address used once is often used again: keeping it is one click away.
            self.notify(f"Connecting to {text}", action=("Save", lambda: self.save_tab_as_session(tab)))
        self.quick.clear()
        self._refresh_quick_suggestions()

    def _saved_named(self, text: str) -> Session | None:
        wanted = text.strip().lower()
        return next((s for s in self.store.sessions if s.title.lower() == wanted), None)

    def clear_history(self, _checked: bool = False, *, ask: bool = True) -> bool:
        """Forget what was typed into the send bar and quick connect, the recent sessions on the start page,
        and the closed tabs that Reopen Closed Tab keeps. Saved sessions, and when each was last used, stay."""
        if ask:
            answer = QMessageBox.question(
                self,
                "Clear History",
                "Forget the commands sent from the send bar, the addresses typed into quick connect, the recent "
                "sessions on the start page and the closed tabs that can be reopened?\n\nSaved sessions are kept.",
            )
            if answer != QMessageBox.StandardButton.Yes:
                return False
        self.command.set_history([])
        self.settings.remove("send_history")
        self.settings.remove("quick_history")
        self.settings.remove("recent_sessions")
        self._closed_tabs.clear()
        self._refresh_quick_suggestions()
        self._fill_recent()
        self.notify("History cleared", "success")
        return True

    def clear_logs(self, _checked: bool = False, *, ask: bool = True) -> int | None:
        """Delete the .log files in the log folder, except those an open tab is still writing to.
        Returns how many were deleted, or None if the question was declined."""
        folder = self.prefs.log_directory()
        in_use = {p.resolve() for p in (t.log_path() for t in self.open_tabs()) if p}
        try:
            files = [f for f in folder.glob("*.log") if f.is_file() and f.resolve() not in in_use]
        except OSError:
            files = []
        if not files:
            self.notify("There are no log files to clear")
            return 0
        if ask:
            answer = QMessageBox.question(
                self,
                "Clear Logs",
                f"Delete {len(files)} log file{'s' if len(files) != 1 else ''} in\n{folder}?\n\n"
                "Logs that open tabs are still writing to are kept.",
            )
            if answer != QMessageBox.StandardButton.Yes:
                return None
        removed = 0
        for f in files:
            try:
                f.unlink()
                removed += 1
            except OSError:
                pass
        self.notify(f"Deleted {removed} log file{'s' if removed != 1 else ''}", "success")
        return removed

    def quick_history(self) -> list[str]:
        """Addresses typed into quick connect, most recent first."""
        stored = self.settings.value("quick_history") or []
        return [stored] if isinstance(stored, str) else [str(entry) for entry in stored]

    def _refresh_quick_suggestions(self) -> None:
        """Quick connect suggests earlier addresses first, then the names of saved sessions."""
        history = self.quick_history()
        names = sorted((s.title for s in self.store.sessions), key=str.lower)
        self._quick_model.set_suggestions(history, names)

    def open_target(self, text: str) -> TerminalTab | None:
        """Open a saved session by name, or else connect to "user@host" / "protocol://host"."""
        wanted = text.strip().lower()
        saved = next((s for s in self.store.sessions if s.title.lower() == wanted), None)
        session = saved or self.parse_quick_connect(text)
        return self.open_session(session) if session is not None else None

    def come_forward(self, targets: list[str]) -> None:
        """Another copy of the program was started: show this window in front, and open what it was given."""
        if self.isMinimized():
            self.showNormal()
        self.show()
        self.raise_()
        self.activateWindow()
        if targets:
            self.open_targets(targets)

    def open_targets(self, targets: list[str]) -> list[str]:
        """Open each of the targets; returns those that were not understood."""
        unknown = [target for target in targets if self.open_target(target) is None]
        if unknown:
            self.notify("Not a saved session or an address: " + ", ".join(unknown), "error")
        return unknown

    def send_to_targets(self, text: str, placeholders: bool = False) -> int:
        """Send text, line by line, to the active tab or to every tab if "All tabs" is ticked.

        With `placeholders`, {host}, {user}, {port} and {name} are filled in for each tab.
        Returns how many tabs it reached: a tab that is not connected drops what it is sent.
        """
        if self.all_tabs.isChecked():
            targets = self.broadcast_targets()
        else:
            targets = [self.current_tab()] if self.current_tab() else []
        targets = [tab for tab in targets if tab.state == STATE_CONNECTED]
        for tab in targets:
            filled = fill_placeholders(text, tab.session) if placeholders else text
            for line in filled.replace("\r\n", "\n").split("\n"):
                if line.strip():
                    tab.send_text(line + "\n")
        return len(targets)

    def broadcast_targets(self) -> list[TerminalTab]:
        """The tabs a broadcast goes to: those marked for it, or every tab if none is."""
        marked = [tab for tab in self.open_tabs() if tab.in_broadcast]
        return marked or self.open_tabs()

    def set_in_broadcast(self, target=None, included: bool | None = None) -> None:
        """Mark a tab to receive broadcasts, or unmark it; without `included` it switches."""
        tab = self._resolve(target)
        if tab is None:
            return
        tab.in_broadcast = not tab.in_broadcast if included is None else included
        self._broadcast_changed()

    def clear_broadcast_marks(self) -> None:
        for tab in self.open_tabs():
            tab.in_broadcast = False
        self._broadcast_changed()

    def _broadcast_changed(self) -> None:
        for tab in self.open_tabs():
            pane = self._pane_of(tab)
            pane.setTabIcon(pane.indexOf(tab), self._tab_icon(tab))
            pane.setTabToolTip(pane.indexOf(tab), self._tab_tooltip(tab))
        self._label_broadcast()
        self._update_command_placeholder()

    def _label_broadcast(self) -> None:
        """The broadcast box names its targets: every tab, or how many are marked."""
        marked = sum(1 for tab in self.open_tabs() if tab.in_broadcast)
        narrow = self.width() < NARROW_WIDTH
        if marked:
            self.all_tabs.setText(str(marked) if narrow else f"{marked} marked tab{'' if marked == 1 else 's'}")
            self.all_tabs.setToolTip("Broadcast the command to the tabs marked for it (tab menu, Include in Broadcast)")
        else:
            self.all_tabs.setText("All" if narrow else "All tabs")
            self.all_tabs.setToolTip("Broadcast the command to every connected tab")

    def ask_snippet_values(self, snippet: Snippet, prompts: list[tuple[str, str]]) -> dict[str, str] | None:
        """The answers to a saved command's {?label} prompts; None if the question was cancelled."""
        dialog = CommandValuesDialog(snippet.title, prompts, self)
        return dialog.values() if dialog.exec() else None

    def send_snippet(self, snippet: Snippet) -> int:
        """Send a saved command, with the values it asks for and its placeholders filled in for each tab it goes to."""
        command = snippet.command
        prompts = prompts_in(command)
        if prompts:
            if not self.current_tab():
                self._nothing_sent()
                return 0
            answers = self.ask_snippet_values(snippet, prompts)
            if answers is None:
                return 0
            command = fill_prompts(command, answers)
        sent = self.send_to_targets(command, placeholders=True)
        if not sent:
            self._nothing_sent()
        return sent

    def _nothing_sent(self) -> None:
        """Say why a command went nowhere, instead of letting it vanish."""
        if not self.open_tabs():
            self.notify("Open a tab first to send a command to it", "error")
        elif self.all_tabs.isChecked():
            self.notify("None of the tabs it goes to is connected", "error")
        else:
            tab = self.current_tab()
            self.notify(f"{self._tab_name(tab)} is not connected", "error", action=("Reconnect", lambda: self._reconnect_if_open(tab)))

    def _reconnect_if_open(self, tab: TerminalTab) -> None:
        """For a Reconnect offered in a notice: the tab may have been closed since."""
        if tab in self.open_tabs():
            tab.connect_to_host()

    def _send_command(self) -> None:
        text = self.command.text()
        if not text:
            return
        if not self.send_to_targets(text):
            self._nothing_sent()  # the command stays in the field, to send once connected
            return
        self.command.remember(text)
        self.settings.setValue("send_history", self.command.history)
        self.command.clear()

    def closeEvent(self, event):
        tabs = self.open_tabs()
        live = [self._tab_name(tab) for tab in tabs if tab.state != STATE_DISCONNECTED]
        if live and self.prefs.confirm_quit:
            # Naming them shows at a glance whether anything that matters would be cut off.
            named = live[:QUIT_NAMES_SHOWN]
            if len(live) > len(named):
                named.append(f"{len(live) - len(named)} more")
            listed = named[0] if len(named) == 1 else ", ".join(named[:-1]) + " and " + named[-1]
            verb = "is" if len(live) == 1 else "are"
            if not self.confirm("Quit", f"{listed} {verb} still open. Quit anyway?", "confirm_quit"):
                event.ignore()
                return
        self.remember_open_tabs()
        if self._internal_error in errors.listeners:
            errors.listeners.remove(self._internal_error)
        for tab in tabs:
            tab.shutdown()
        self.scheduler.shutdown()  # a task that is running stops between two commands
        self.notifier.close()
        connections.shutdown_all()
        self.settings.setValue("geometry", self.saveGeometry())
        self.settings.setValue("splitter", self.splitter.saveState())
        event.accept()
