"""Session editor and settings dialogs."""

import codecs
import platform
import time
from dataclasses import replace
from datetime import datetime
from pathlib import Path

import paramiko
from PyQt6.QtCore import PYQT_VERSION_STR, QT_VERSION_STR, QSize, QStringListModel, Qt, QTime, QTimer, QUrl, pyqtSignal
from PyQt6.QtGui import QColor, QDesktopServices, QGuiApplication, QIcon, QTextCursor
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QButtonGroup,
    QCheckBox,
    QColorDialog,
    QComboBox,
    QCompleter,
    QDialog,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QStackedWidget,
    QTimeEdit,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from . import APP_NAME, AUTHOR, LICENSE_NAME, LICENSE_URL, __version__, backup, connections, keys, licences, responses, threads
from .hostkeys import ApprovedKey
from .local import available_shells, find_program, start_folder
from .paths import portable_folder
from .screens import fit_to_screen
from .snippets import Snippet
from .tasks import (
    LOCAL_TARGETS,
    MAX_INTERVAL_MINUTES,
    OUTPUT_DIR,
    RUNNER_APP,
    RUNNER_WINDOWS,
    SCHEDULE_DAILY,
    SCHEDULE_INTERVAL,
    SCHEDULE_MANUAL,
    SCHEDULES,
    WINDOWS_FOLDER,
    Task,
    next_run,
    output_path,
    parse_time_of_day,
)
from .sessions import (
    AUTH_AGENT,
    AUTH_KEY,
    AUTH_PASSWORD,
    CONFIG_DIR,
    DEFAULT_PORTS,
    ENCODINGS,
    PROTO_LOCAL,
    PROTO_RLOGIN,
    PROTO_SERIAL,
    PROTO_SSH,
    PROTOCOLS,
    Session,
    SessionStore,
    parse_jump_hosts,
    parse_tunnels,
)
from .preferences import (
    CURSOR_BAR,
    CURSOR_BLOCK,
    CURSOR_UNDERLINE,
    MAX_PASTE_DELAY_MS,
    MAX_SCROLLBACK,
    MAX_TRANSFERS,
    MIN_SCROLLBACK,
    Preferences,
)
from .terminal import MAX_FONT_SIZE, MIN_FONT_SIZE
from .theme import (
    COLORS,
    DEFAULT_TERMINAL_FONT,
    DEFAULT_UI_FONT,
    SESSION_COLORS,
    THEME_DARK,
    THEME_LIGHT,
    THEME_SYSTEM,
    TERMINAL_SCHEMES,
    active_theme,
    app_icon,
    line_icon,
    terminal_colors,
    theme_preview,
    usable_font_families,
)

_AUTH_CHOICES = [
    ("Password", AUTH_PASSWORD),
    ("Private key file", AUTH_KEY),
    ("SSH agent / default keys", AUTH_AGENT),
]
_PREVIEW_TEXT = (
    "deploy@web-01:~$ ls -la /srv\n"
    "drwxr-xr-x 4 deploy 4096 Oct  7 releases\n"
    "0O 1lI |!  {} [] ()  => != <= ->"
)


def _section(text: str) -> QLabel:
    label = QLabel(text)
    label.setObjectName("SectionTitle")
    return label


class _ElidedLabel(QLabel):
    """A label for something with no spaces to wrap at, such as a path: it shortens the text in the middle to fit,
    and shows all of it as the tooltip."""

    def __init__(self, text: str, parent=None):
        super().__init__(parent)
        self._full = text
        self.setToolTip(text)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)  # it takes the width it is given
        self.setText(text)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.setText(self.fontMetrics().elidedText(self._full, Qt.TextElideMode.ElideMiddle, max(self.width(), 20)))


def _row(*widgets, stretch_first: bool = True) -> QWidget:
    holder = QWidget()
    layout = QHBoxLayout(holder)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(10)
    for position, widget in enumerate(widgets):
        layout.addWidget(widget, 1 if stretch_first and position == 0 else 0)
    return holder


def _form() -> QFormLayout:
    form = QFormLayout()
    form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
    form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
    form.setVerticalSpacing(10)
    form.setHorizontalSpacing(14)
    return form


def _form_labels(*forms: QFormLayout) -> list[QLabel]:
    """The labels of the forms' rows, made to sit level with the middle of their fields.

    A form gives a label that cannot grow only a little more than its own height, at the top
    of the row, so beside a tall field it rides high; one that may grow is centred.
    """
    labels = []
    for form in forms:
        for row in range(form.rowCount()):
            item = form.itemAt(row, QFormLayout.ItemRole.LabelRole)
            if item and isinstance(item.widget(), QLabel):
                label = item.widget()
                label.setSizePolicy(label.sizePolicy().horizontalPolicy(), QSizePolicy.Policy.Expanding)
                labels.append(label)
    return labels


def _system_details() -> list[tuple[str, str]]:
    """What the application runs on, as (label, value): the versions a bug report needs."""
    return [
        ("Python", platform.python_version()),
        ("Qt", f"{QT_VERSION_STR} (PyQt {PYQT_VERSION_STR})"),
        ("SSH library", f"paramiko {paramiko.__version__}"),
        ("System", f"{platform.system()} {platform.release()} ({platform.machine()})"),
    ]


def version_info() -> str:
    """The application's version and what it runs on, as text to paste into a bug report."""
    lines = [f"{APP_NAME} {__version__}"] + [f"{label}: {value}" for label, value in _system_details()]
    return "\n".join(lines) + "\n"


def ask_text(parent: QWidget, title: str, message: str, hidden: bool = False) -> tuple[str, bool]:
    """Ask for one line of text; returns (text, accepted).

    A hidden answer, such as a password, has a button at the end of the field that shows what has
    been typed, so a long one can be checked before it is sent.
    """
    dialog = QInputDialog(parent)
    dialog.setInputMode(QInputDialog.InputMode.TextInput)
    dialog.setWindowTitle(title)
    dialog.setLabelText(message)
    dialog.setTextEchoMode(QLineEdit.EchoMode.Password if hidden else QLineEdit.EchoMode.Normal)
    field = dialog.findChild(QLineEdit)
    field.setMinimumWidth(340)  # the dialog sizes itself to its contents, so room for the answer is asked here
    if hidden:
        add_reveal(field)
    accepted = dialog.exec() == QDialog.DialogCode.Accepted
    text = dialog.textValue()
    dialog.deleteLater()
    return text, accepted


def add_reveal(field: QLineEdit) -> None:
    """A button at the end of a hidden field that shows what has been typed, and hides it again."""
    reveal = field.addAction(line_icon("eye", COLORS["muted"]), QLineEdit.ActionPosition.TrailingPosition)
    reveal.setCheckable(True)
    reveal.setToolTip("Show what you type")

    def show(shown: bool) -> None:
        field.setEchoMode(QLineEdit.EchoMode.Normal if shown else QLineEdit.EchoMode.Password)
        reveal.setIcon(line_icon("eye-off" if shown else "eye", COLORS["muted"]))
        reveal.setToolTip("Hide what you type" if shown else "Show what you type")

    reveal.toggled.connect(show)


class BackupPassphraseDialog(QDialog):
    """Asks for the passphrase that will protect a backup, twice; or for none, which leaves it a plain zip."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Back Up Everything")
        self.setMinimumWidth(440)
        self.encrypt = True  # False when "Without a passphrase" is chosen
        intro = QLabel(
            "A passphrase encrypts the backup, so it is safe to keep on a USB stick or in cloud storage. "
            "It holds your server names, user names and settings; passwords are never in it.\n\n"
            "There is no way back without the passphrase, so keep it somewhere safe."
        )
        intro.setWordWrap(True)
        self.passphrase = QLineEdit()
        self.passphrase.setEchoMode(QLineEdit.EchoMode.Password)
        self.passphrase.setPlaceholderText(f"At least {backup.MIN_PASSPHRASE} characters")
        self.again = QLineEdit()
        self.again.setEchoMode(QLineEdit.EchoMode.Password)
        self.again.setPlaceholderText("The same again")
        for field in (self.passphrase, self.again):
            add_reveal(field)
            field.textChanged.connect(lambda _text: self.error.setText(""))
        self.error = QLabel()
        self.error.setStyleSheet(f"color: {COLORS['red']};")
        form = _form()
        form.addRow("Passphrase", self.passphrase)
        form.addRow("Repeat", self.again)
        plain = QPushButton("Without a Passphrase")
        plain.setToolTip("A plain zip, as before: anyone with the file can read it")
        plain.clicked.connect(self._plain)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        protect = QPushButton("Encrypt and Save...")
        protect.setObjectName("Primary")
        protect.setDefault(True)
        protect.clicked.connect(self._protect)
        buttons = QHBoxLayout()
        buttons.addWidget(plain)
        buttons.addStretch(1)
        buttons.addWidget(cancel)
        buttons.addWidget(protect)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 16)
        layout.setSpacing(12)
        layout.addWidget(intro)
        layout.addLayout(form)
        layout.addWidget(self.error)
        layout.addLayout(buttons)

    def problem(self) -> str:
        """What is wrong with the passphrase as typed, or "" when it will do."""
        if len(self.passphrase.text()) < backup.MIN_PASSPHRASE:
            return f"The passphrase needs at least {backup.MIN_PASSPHRASE} characters"
        if self.again.text() != self.passphrase.text():
            return "The two passphrases are not the same"
        return ""

    def _protect(self) -> None:
        problem = self.problem()
        if problem:
            self.error.setText(problem)
            return
        self.encrypt = True
        self.accept()

    def _plain(self) -> None:
        self.encrypt = False
        self.accept()

    def chosen_passphrase(self) -> str:
        return self.passphrase.text() if self.encrypt else ""


class ColorPicker(QWidget):
    """A row of colour swatches: none, the presets, and one custom colour."""

    colorChanged = pyqtSignal(str)

    def __init__(self, color: str = "", none_tooltip: str = "No colour", parent=None):
        super().__init__(parent)
        self._color = ""
        self._swatches: dict[str, QToolButton] = {}
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(5)
        layout.addWidget(self._swatch("", none_tooltip))
        for name, value in SESSION_COLORS:
            layout.addWidget(self._swatch(value, name))
        self.custom = QToolButton()
        self.custom.setText("Custom...")
        self.custom.setCheckable(True)
        self.custom.setFixedHeight(24)
        self.custom.clicked.connect(self._pick_custom)
        layout.addWidget(self.custom)
        layout.addStretch(1)
        self.set_color(color)

    def _swatch(self, value: str, tooltip: str) -> QToolButton:
        button = QToolButton()
        button.setCheckable(True)
        button.setFixedSize(22, 22)
        button.setToolTip(tooltip)
        button.setAccessibleName(tooltip.split(" (")[0])
        button.clicked.connect(lambda: self.set_color(value))
        self._swatches[value] = button
        return button

    def color(self) -> str:
        return self._color

    def set_color(self, color: str, force: bool = False) -> None:
        color = color.lower() if QColor(color).isValid() else ""
        changed = color != self._color or force
        self._color = color
        is_custom = bool(color) and color not in self._swatches
        for value, button in self._swatches.items():
            selected = value == color
            button.setChecked(selected)
            fill = value or "transparent"
            ring = COLORS["text"] if selected else (COLORS["border2"] if not value else "transparent")
            button.setStyleSheet(
                f"QToolButton {{ background: {fill}; border: 2px solid {ring}; border-radius: 11px; }}"
                f"QToolButton:focus {{ border-color: {COLORS['accent']}; }}"
            )
        self.custom.setChecked(is_custom)
        ring = COLORS["text"] if is_custom else COLORS["border"]
        fill = color if is_custom else COLORS["surface2"]
        text = COLORS["bg"] if is_custom else COLORS["text"]
        self.custom.setStyleSheet(
            f"QToolButton {{ background: {fill}; color: {text}; border: 2px solid {ring};"
            " border-radius: 12px; padding: 0 10px; }"
        )
        if changed:
            self.colorChanged.emit(color)

    def _pick_custom(self) -> None:
        start = QColor(self._color or COLORS["accent"])
        chosen = QColorDialog.getColor(start, self, "Choose Colour")
        self.set_color(chosen.name() if chosen.isValid() else self._color)


def _font_combo(monospaced: bool) -> QComboBox:
    combo = QComboBox()
    combo.setMaxVisibleItems(14)
    combo.addItems(usable_font_families(monospaced))
    return combo


def _select_font(combo: QComboBox, family: str, fallback: str) -> None:
    index = combo.findText(family)
    combo.setCurrentIndex(index if index >= 0 else max(0, combo.findText(fallback)))


class SessionDialog(QDialog):
    # Below this width the two columns are stacked into one.
    TWO_COLUMN_WIDTH = 820

    def __init__(self, store: SessionStore, session: Session | None = None, parent=None):
        super().__init__(parent)
        self.store = store
        self._original = session or Session()
        self.password: str | None = None  # set only when the user typed a new one
        self.connect_after = False
        self._pw_confirmed = False  # a password in the command line has been accepted as plain text
        self.setWindowTitle("Edit Session" if session else "New Session")
        self.setMinimumSize(420, 320)
        s = self._original

        # ---- connection ----
        self.protocol_group = QButtonGroup(self)
        protocol_row = QWidget()
        protocol_layout = QHBoxLayout(protocol_row)
        protocol_layout.setContentsMargins(0, 0, 0, 0)
        protocol_layout.setSpacing(14)
        self._protocol_buttons: dict[str, QRadioButton] = {}
        for value, label in PROTOCOLS:
            button = QRadioButton(label)
            self.protocol_group.addButton(button)
            protocol_layout.addWidget(button)
            self._protocol_buttons[value] = button
        protocol_layout.addStretch(1)
        self._protocol_buttons.get(s.protocol, self._protocol_buttons[PROTO_SSH]).setChecked(True)
        self._protocol = self.protocol()

        self.host = QLineEdit(s.host)
        self.host_label = QLabel()
        # A local shell's "host" is the program to run; the shells found here are suggested.
        self._shell_completer = QCompleter(QStringListModel([command for _name, command in available_shells()], self), self)
        self._shell_completer.setCompletionMode(QCompleter.CompletionMode.UnfilteredPopupCompletion)
        self.start_folder = QLineEdit(s.start_folder)
        self.start_folder.setPlaceholderText(f"{Path.home()}  (your own folder)")
        self.start_folder.setToolTip("The folder the shell starts in. %VARIABLES% such as %USERPROFILE% and ~ are filled in.")
        browse_folder = QPushButton("Browse")
        browse_folder.clicked.connect(self._browse_start_folder)
        self.start_folder_row = _row(self.start_folder, browse_folder)
        self.port = QSpinBox()
        self.port.setRange(1, 4_000_000)  # also holds serial speeds
        self.port.setValue(s.port)
        self.port.setFixedWidth(100)
        self.port_label = QLabel()
        self.name = QLineEdit(s.name)
        self.name.setPlaceholderText("Defaults to the server name")
        self.group = QComboBox()
        self.group.setEditable(True)
        self.group.addItems([""] + store.groups())
        self.group.setCurrentText(s.group)
        self.group.lineEdit().setPlaceholderText("Optional")

        # ---- logon ----
        self.username = QLineEdit(s.username)
        self.auth = QComboBox()
        for label, value in _AUTH_CHOICES:
            self.auth.addItem(label, value)
        self.auth.setCurrentIndex(max(0, self.auth.findData(s.auth)))
        self.key_file = QLineEdit(s.key_file)
        self.key_file.setPlaceholderText("Path to private key")
        browse = QPushButton("Browse")
        browse.clicked.connect(self._browse_key)
        generate = QPushButton("Generate...")
        generate.setToolTip("Create a new key pair and use it for this session")
        generate.clicked.connect(self._generate_key)
        self.key_row = _row(self.key_file, browse, generate)

        self.password_edit = QLineEdit()
        self.password_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.password_edit.setPlaceholderText(
            "Leave empty to keep the saved password" if s.save_password else "Leave empty to be asked or type it yourself"
        )
        self.save_password = QCheckBox("Save in system credential store")
        self.save_password.setChecked(s.save_password and store.can_store_passwords())
        if not store.can_store_passwords():
            self.save_password.setEnabled(False)
            self.save_password.setToolTip("Install the 'keyring' package to enable this")
        self.password_in_command_line = QCheckBox("Password in command line (SSH only)")
        self.password_in_command_line.setChecked(s.password_in_command_line)
        self.password_in_command_line.setToolTip(
            "Checked: the password is handed straight to the SSH login.\n"
            "Unchecked: it is typed at the first password prompt, after the wait period."
        )
        self.wait_ms = QSpinBox()
        self.wait_ms.setRange(0, 60_000)
        self.wait_ms.setSingleStep(100)
        self.wait_ms.setSuffix(" ms")
        self.wait_ms.setValue(s.wait_ms)
        self.wait_ms.setFixedWidth(120)
        self.wait_ms.setToolTip("Pause between a login or password prompt appearing and the answer being typed")

        # ---- extras ----
        self.command_line = QLineEdit(s.command_line)
        self.command_line.setPlaceholderText("Extra options, e.g.  -i C:\\keys\\id_ed25519  -C  -P 2222")
        self.command_line.setToolTip(
            "Supported: -ssh -telnet -rlogin -raw -serial, -P port, -l user, -pw password,\n"
            "-i keyfile, -C (compression), -A (forward the SSH agent), -L / -R port:host:port,\n"
            "-D port, -J user@host.\n"
            "They override the fields in this window."
        )
        self.jump_host = QLineEdit(s.jump_host)
        self.jump_host.setPlaceholderText("user@host[:port]  -  connect through this server (optional)")
        self.jump_host.setToolTip("Several hops can be given in order, separated by commas:  me@gate1, me@gate2")
        self.tunnels = QLineEdit(s.tunnels)
        self.tunnels.setPlaceholderText("8080:localhost:80, R:9090:localhost:3000, D:1080  (optional)")
        self.tunnels.setToolTip(
            "Port forwards, separated by commas, active while the session is open:\n"
            "  8080:host:80        local port 8080 reaches host:80 through the server\n"
            "  R:9090:host:3000    port 9090 on the server reaches host:3000 on this side\n"
            "  D:1080              a SOCKS proxy on local port 1080\n"
            "  D:name:secret@1080  the same, but programs must log in to use it"
        )
        self.forward_agent = QCheckBox("Forward agent")
        self.forward_agent.setChecked(s.forward_agent)
        self.forward_agent.setToolTip(
            "Lets the server use the keys in this computer's SSH agent (Pageant or the OpenSSH agent)\n"
            "to log in to further servers, for example with git or ssh from there.\n"
            "Only tick this for servers you trust: their administrators could use your keys while\n"
            "you are connected."
        )
        self.serial_format = QComboBox()
        self.serial_format.setEditable(True)
        self.serial_format.addItems(["8N1", "7E1", "7O1", "8E1", "8O1", "8N2", "7N2"])
        self.serial_format.setCurrentText(s.serial_format)
        self.serial_format.setToolTip("Data bits, parity (N, E, O, M or S) and stop bits (1, 1.5 or 2)")
        self.serial_flow = QComboBox()
        for label, value in (("None", "none"), ("XON/XOFF", "xonxoff"), ("RTS/CTS", "rtscts"), ("DSR/DTR", "dsrdtr")):
            self.serial_flow.addItem(label, value)
        self.serial_flow.setCurrentIndex(max(0, self.serial_flow.findData(s.serial_flow)))
        self.startup = QLineEdit(s.startup_command)
        self.startup.setPlaceholderText("Command to run after login (optional)")
        self.auto_responses = QPlainTextEdit(s.auto_responses)
        self.auto_responses.setTabChangesFocus(True)
        self.auto_responses.setPlaceholderText("--More-- => \\s   one per line: text => answer (optional)")
        self.auto_responses.setToolTip(
            "When the output shows the text on the left, the answer on the right is typed.\n"
            "One per line, as  text => answer. The text is found in any case.\n"
            "The answer is typed exactly as written: add \\r for Enter.\n"
            "Also \\s space, \\t Tab, \\e Esc, \\n line feed, \\\\ backslash, \\xHH any byte.\n"
            "Lines starting with # are skipped."
        )
        self.auto_responses.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        # As tall as a line field while it holds one rule, growing with more, so the dialog
        # still fits a small screen without scrolling.
        self.auto_responses.document().setDocumentMargin(2)
        self.auto_responses.textChanged.connect(self._fit_responses)
        self._fit_responses()

        def new_form() -> QFormLayout:
            return _form()

        def section(title: str, *content) -> QWidget:
            box = QWidget()
            box_layout = QVBoxLayout(box)
            box_layout.setContentsMargins(0, 0, 0, 0)
            box_layout.setSpacing(12)
            box_layout.addWidget(_section(title))
            for item in content:
                if isinstance(item, QWidget):
                    box_layout.addWidget(item)
                else:
                    box_layout.addLayout(item)
            return box

        # The protocol choice runs across the top; it decides which fields appear below.
        protocol_form = new_form()
        protocol_form.addRow("Protocol", protocol_row)

        connection = new_form()
        connection.addRow(self.host_label, _row(self.host, self.port_label, self.port))
        connection.addRow("Format", _row(self.serial_format, QLabel("Flow control"), self.serial_flow))
        connection.addRow("Command line", self.command_line)
        connection.addRow("Start in", self.start_folder_row)

        listing = new_form()
        listing.addRow("Display name", self.name)
        listing.addRow("Folder", self.group)
        self.color = ColorPicker(s.color)
        listing.addRow("Colour", self.color)
        self.notes = QLineEdit(s.notes)
        self.notes.setPlaceholderText("Anything worth remembering about this session (optional)")
        listing.addRow("Notes", self.notes)
        self.pinned = QCheckBox("Open its tab pinned")
        self.pinned.setChecked(s.pinned)
        self.pinned.setToolTip("A pinned tab stays at the left of the tab bar and is not closed by Close Other Tabs")
        listing.addRow("Tab", self.pinned)

        self.logon = new_form()
        self.logon.addRow("User name", self.username)
        self.auth_row = _row(self.auth, self.forward_agent)
        self.logon.addRow("Authentication", self.auth_row)
        self.logon.addRow("Key file", self.key_row)
        self.logon.addRow("Password", self.password_edit)
        self.logon.addRow("", self.save_password)
        self.logon.addRow("", self.password_in_command_line)
        self.wait_label = QLabel("Wait before typing the password")
        self.wait_label.setWordWrap(True)
        wait_row = _row(self.wait_label, self.wait_ms)

        extras = new_form()
        extras.addRow("Startup command", self.startup)
        extras.addRow("Responses", self.auto_responses)
        routing = new_form()
        routing.addRow("Jump host", self.jump_host)
        routing.addRow("Tunnels", self.tunnels)

        self.encoding = QComboBox()
        self.encoding.setEditable(True)
        self.encoding.addItems(ENCODINGS)
        self.encoding.setCurrentText(s.encoding)
        self.encoding.setToolTip("The character set the remote side uses. UTF-8 suits almost every modern system.")
        self.backspace = QComboBox()
        self.backspace.addItem("Delete (^?)", "del")
        self.backspace.addItem("Backspace (^H)", "bs")
        self.backspace.setCurrentIndex(max(0, self.backspace.findData(s.backspace)))
        self.backspace.setToolTip("Change this if Backspace prints ^? or ^H instead of erasing.")
        self.enter_key = QComboBox()
        self.enter_key.addItem("CR", "cr")
        self.enter_key.addItem("CR + LF", "crlf")
        self.enter_key.setCurrentIndex(max(0, self.enter_key.findData(s.enter)))
        self.local_echo = QCheckBox("Local echo")
        self.local_echo.setToolTip("Show what you type yourself, for devices that do not echo it back.")
        self.local_echo.setChecked(s.local_echo)
        terminal_form = new_form()
        terminal_form.addRow("Encoding", _row(self.encoding, self.local_echo))
        terminal_form.addRow("Backspace sends", _row(self.backspace, QLabel("Enter sends"), self.enter_key))
        # Left: everything needed to connect. Right: how the session appears in the list and what
        # happens once it is open. A narrow dialog stacks them in this order.
        self.connection_section = section("CONNECTION", connection)
        self.logon_section = section("LOGON SETTINGS", self.logon, wait_row)
        self.routing_section = section("ROUTING", routing)
        self.listing_section = section("SESSION LIST", listing)
        self.extras_section = section("AFTER LOGIN", extras)
        self.terminal_section = section("TERMINAL", terminal_form)
        self._column_sections = (
            (self.connection_section, self.logon_section, self.routing_section),
            (self.listing_section, self.extras_section, self.terminal_section),
        )
        self._columns = 0

        # The sections sit in a grid that is re-arranged as the dialog is resized, inside a
        # scroll area so that nothing is cut off on a small screen.
        content = QWidget()
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(22, 16, 22, 6)
        content_layout.setSpacing(12)
        content_layout.addLayout(protocol_form)
        self._grid = QGridLayout()
        self._grid.setHorizontalSpacing(34)
        # Two stacks side by side, so a short section leaves no gap in the other column.
        self._left, self._right = QVBoxLayout(), QVBoxLayout()
        for column in (self._left, self._right):
            column.setContentsMargins(0, 0, 0, 0)
            column.setSpacing(16)
        self._grid.addLayout(self._left, 0, 0)
        self._grid.addLayout(self._right, 0, 1)
        self._grid.setColumnStretch(0, 1)
        content_layout.addLayout(self._grid)
        content_layout.addStretch(1)
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._scroll.setWidget(content)

        self.error = QLabel()
        self.error.setStyleSheet(f"color: {COLORS['red']};")
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        save = QPushButton("Save")
        save.clicked.connect(lambda: self._finish(False))
        connect = QPushButton("Save && Connect")
        connect.setObjectName("Primary")
        connect.setDefault(True)
        connect.clicked.connect(lambda: self._finish(True))
        footer = QFrame()
        footer.setObjectName("SettingsFooter")
        buttons = QHBoxLayout(footer)
        buttons.setContentsMargins(22, 12, 22, 14)
        self.test_button = QPushButton("Test")
        self.test_button.setToolTip("Check that the server answers, without logging in")
        self.test_button.clicked.connect(self.test_connection)
        self._probe = None
        buttons.addWidget(self.test_button)
        buttons.addWidget(self.error, 1)
        buttons.addWidget(cancel)
        buttons.addWidget(save)
        buttons.addWidget(connect)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self._scroll, 1)
        layout.addWidget(footer)

        self._connection = connection
        # Give the label columns of the forms in one column a single width, so their fields
        # line up; the protocol row follows the left column.
        self.host_label.setText("Server name")
        for group in ((protocol_form, connection, self.logon, routing), (listing, extras, terminal_form)):
            labels = _form_labels(*group)
            width = max(label.sizeHint().width() for label in labels)
            for label in labels:
                label.setMinimumWidth(width)

        self.auth.currentIndexChanged.connect(self._update_rows)
        self.password_in_command_line.toggled.connect(self._update_rows)
        self.protocol_group.buttonToggled.connect(self._on_protocol_toggled)
        self._update_rows()
        # Open wide enough for two columns and as tall as the content, within the screen.
        screen = self.screen().availableGeometry() if self.screen() else None
        width = min(980, screen.width() - 80) if screen else 980
        self._arrange(width)
        height = content.sizeHint().height() + footer.sizeHint().height() + 12
        self.resize(width, min(height, screen.height() - 80) if screen else height)
        (self.host if not s.host else self.name).setFocus()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._arrange(event.size().width())

    def _arrange(self, width: int) -> None:
        """Lay the sections out in two columns, or in one when the dialog is narrow."""
        columns = 2 if width >= self.TWO_COLUMN_WIDTH else 1
        if columns == self._columns:
            return
        self._columns = columns
        for column in (self._left, self._right):
            while column.count():
                column.takeAt(0)
        left, right = self._column_sections
        if columns == 2:
            stacks = ((self._left, left), (self._right, right))
        else:
            stacks = ((self._left, left + right),)
        for column, sections in stacks:
            for widget in sections:
                column.addWidget(widget)
        for column in (self._left, self._right):
            column.addStretch(1)
        self._grid.setColumnStretch(1, 1 if columns == 2 else 0)

    def protocol(self) -> str:
        return next(value for value, button in self._protocol_buttons.items() if button.isChecked())

    def _on_protocol_toggled(self, _button, checked: bool) -> None:
        if not checked:
            return
        previous, self._protocol = self._protocol, self.protocol()
        # Follow the new protocol's usual port unless the user chose a custom one.
        if self.port.value() == DEFAULT_PORTS[previous]:
            self.port.setValue(DEFAULT_PORTS[self._protocol])
        self._update_rows()

    def _fit_responses(self) -> None:
        editor = self.auto_responses
        editor.ensurePolished()  # the frame width includes the theme's border and padding
        lines = min(4, max(1, editor.document().blockCount()))
        margin = editor.document().documentMargin()
        editor.setFixedHeight(int(2 * editor.frameWidth() + 2 * margin + lines * editor.fontMetrics().lineSpacing()))
        # A single line must not show a scroll bar for the line it already shows.
        self.auto_responses.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded if lines == 4 else Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )

    def _update_rows(self) -> None:
        protocol = self.protocol()
        ssh, serial, local = protocol == PROTO_SSH, protocol == PROTO_SERIAL, protocol == PROTO_LOCAL
        auth = self.auth.currentData()
        self.host_label.setText("Serial line" if serial else "Program" if local else "Server name")
        self.host.setPlaceholderText(
            "COM3 or /dev/ttyUSB0" if serial
            else "pwsh.exe, powershell.exe, cmd.exe, wsl.exe -d Ubuntu ..." if local
            else "Host name or IP address"
        )
        self.host.setCompleter(self._shell_completer if local else None)
        self.port_label.setText("Speed" if serial else "Port")
        self.port_label.setVisible(not local)
        self.port.setVisible(not local)
        self._connection.setRowVisible(self.command_line, not local)
        self._connection.setRowVisible(self.start_folder_row, local)
        self.logon_section.setVisible(not local)  # the program runs as you, with nothing to log in to
        self.logon.setRowVisible(self.auth_row, ssh)
        self._connection.setRowVisible(self.serial_format.parentWidget(), serial)
        self.routing_section.setVisible(ssh)  # jump hosts and tunnels are SSH features
        self.logon.setRowVisible(self.key_row, ssh and auth == AUTH_KEY)
        self.password_in_command_line.setEnabled(ssh)
        # The wait only matters when the password is typed rather than sent with the SSH login.
        typed = not ssh or not self.password_in_command_line.isChecked()
        self.wait_ms.setEnabled(typed)
        self.wait_label.setEnabled(typed)

    def _browse_start_folder(self) -> None:
        chosen = QFileDialog.getExistingDirectory(self, "Start In", str(start_folder(self.session())))
        if chosen:
            self.start_folder.setText(str(Path(chosen)))

    def _browse_key(self) -> None:
        start = self.key_file.text() or str(Path.home() / ".ssh")
        path, _ = QFileDialog.getOpenFileName(self, "Select Private Key", start)
        if path:
            self.key_file.setText(path)

    def _say(self, text: str, good: bool = False) -> None:
        self.error.setStyleSheet(f"color: {COLORS['green' if good else 'red']};")
        self.error.setText(text)

    def test_connection(self) -> None:
        """Try to reach the server as entered so far; the result appears next to the buttons."""
        if not self.host.text().strip():
            return self._say("Enter a server name first")
        self.test_button.setEnabled(False)
        self.error.setStyleSheet(f"color: {COLORS['muted']};")
        self.error.setText("Testing...")
        self._probe = connections.Probe(self.session())  # no parent: it may outlive this dialog
        self._probe.finished_with.connect(self._tested)
        self._probe.start()

    def _tested(self, reachable: bool, message: str) -> None:
        self.test_button.setEnabled(True)
        self._say(message, good=reachable)

    def done(self, result: int) -> None:
        if self._probe is not None and self._probe.isRunning():
            self._probe.finished_with.disconnect()
            threads.park(self._probe)  # it ends by itself within its timeout
        super().done(result)

    def _generate_key(self) -> None:
        dialog = KeyDialog(self)
        if dialog.exec() and dialog.created_path:
            self.key_file.setText(dialog.created_path)

    def _finish(self, connect_after: bool) -> None:
        protocol = self.protocol()
        try:
            codecs.lookup(self.encoding.currentText().strip() or "utf-8")
        except LookupError:
            self._say(f"Unknown encoding: {self.encoding.currentText().strip()}")
            self.encoding.setFocus()
            return
        _rules, bad_responses = responses.parse_rules(self.auto_responses.toPlainText())
        if bad_responses:
            self._say(f"Response not understood: {bad_responses[0][:40]}  (expected  text => answer)")
            self.auto_responses.setFocus()
            return
        if not self.host.text().strip():
            self._say(
                "Serial line is required" if protocol == PROTO_SERIAL
                else "Program is required" if protocol == PROTO_LOCAL
                else "Server name is required"
            )
            self.host.setFocus()
            return
        if protocol == PROTO_LOCAL and find_program(self.host.text()) is None:
            self._say("There is no such program on this computer")
            self.host.setFocus()
            return
        if protocol == PROTO_LOCAL and not start_folder(self.session()).is_dir():
            self._say("The start folder is not there")
            self.start_folder.setFocus()
            return
        if protocol in (PROTO_SSH, PROTO_RLOGIN) and not self.username.text().strip():
            self._say("User name is required")
            self.username.setFocus()
            return
        if protocol == PROTO_SSH:
            # The same rules the connection uses, so a mistake shows here instead of at connect time.
            jump = self.jump_host.text().strip()
            if jump and parse_jump_hosts(jump) is None:
                self._say("Jump host: use user@host or user@host:port, several separated by commas")
                self.jump_host.setFocus()
                return
            _tunnels, bad = parse_tunnels(self.tunnels.text())
            if bad:
                self._say(f"Tunnel “{bad[0]}” is not understood; hover over the field for the forms")
                self.tunnels.setFocus()
                return
        if "-pw" in self.command_line.text().split() and not self._pw_confirmed:
            answer = QMessageBox.question(
                self,
                "Password in Command Line",
                "A password written after -pw is saved as plain text in the sessions file, where anyone who can "
                "read your files can see it.\n\nThe Password field with “Save in system credential store” "
                "keeps it in the credential store of the system instead.\n\nSave it as plain text anyway?",
            )
            if answer != QMessageBox.StandardButton.Yes:
                self.command_line.setFocus()
                return
            self._pw_confirmed = True
        self.connect_after = connect_after
        self.password = self.password_edit.text() or None
        self.accept()

    def session(self) -> Session:
        protocol = self.protocol()
        auth = self.auth.currentData() if protocol == PROTO_SSH else AUTH_PASSWORD
        return replace(
            self._original,
            protocol=protocol,
            name=self.name.text().strip(),
            group=self.group.currentText().strip(),
            color=self.color.color(),
            host=self.host.text().strip(),
            port=self.port.value(),
            username=self.username.text().strip(),
            auth=auth,
            key_file=self.key_file.text().strip() if auth == AUTH_KEY else "",
            save_password=self.save_password.isChecked(),
            password_in_command_line=self.password_in_command_line.isChecked(),
            wait_ms=self.wait_ms.value(),
            command_line=self.command_line.text().strip(),
            notes=self.notes.text().strip(),
            pinned=self.pinned.isChecked(),
            # A pseudo console always speaks UTF-8.
            encoding="utf-8" if protocol == PROTO_LOCAL else self.encoding.currentText().strip().lower() or "utf-8",
            backspace=self.backspace.currentData(),
            enter=self.enter_key.currentData(),
            local_echo=self.local_echo.isChecked(),
            jump_host=self.jump_host.text().strip() if protocol == PROTO_SSH else "",
            tunnels=self.tunnels.text().strip() if protocol == PROTO_SSH else "",
            forward_agent=self.forward_agent.isChecked() and protocol == PROTO_SSH,
            serial_format=self.serial_format.currentText().strip().upper() or "8N1",
            serial_flow=self.serial_flow.currentData(),
            startup_command=self.startup.text().strip(),
            auto_responses=self.auto_responses.toPlainText().strip(),
            start_folder=self.start_folder.text().strip() if protocol == PROTO_LOCAL else "",
        )


class Segmented(QWidget):
    """A row of joined buttons of which exactly one is selected."""

    changed = pyqtSignal(str)

    def __init__(self, options: list[tuple[str, str]], parent=None):
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self._group = QButtonGroup(self)
        self._buttons: dict[str, QPushButton] = {}
        for position, (value, label) in enumerate(options):
            button = QPushButton(label)
            button.setObjectName("Segment")
            button.setCheckable(True)
            button.setProperty("first", position == 0)
            button.setProperty("last", position == len(options) - 1)
            button.clicked.connect(lambda _checked, v=value: self.changed.emit(v))
            self._group.addButton(button)
            layout.addWidget(button)
            self._buttons[value] = button

    def value(self) -> str:
        return next(value for value, button in self._buttons.items() if button.isChecked())

    def set_value(self, value: str) -> None:
        self._buttons.get(value, next(iter(self._buttons.values()))).setChecked(True)


class TextViewer(QDialog):
    """A long text to read, such as a licence or the release notes. It can be searched and copied."""

    def __init__(self, title: str, text: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setMinimumSize(480, 320)
        self.view = QPlainTextEdit()
        self.view.setReadOnly(True)
        self.view.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        # A widget stylesheet is the only thing that outranks the application-wide font rule.
        self.view.setStyleSheet(f'font-family: "{DEFAULT_TERMINAL_FONT}", monospace;')
        self.view.setPlainText(text)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Find in this text")
        self.search.setClearButtonEnabled(True)
        self.search.returnPressed.connect(self._find)
        self.search.textChanged.connect(self._find_from_start)
        self.found = QLabel()
        self.found.setObjectName("Muted")
        copy = QPushButton("Copy All")
        copy.clicked.connect(lambda: QGuiApplication.clipboard().setText(text))
        close = QPushButton("Close")
        close.setObjectName("Primary")
        close.clicked.connect(self.accept)
        for button in (copy, close):
            button.setAutoDefault(False)  # Enter in the search field must not press a button
        top = QHBoxLayout()
        top.addWidget(self.search, 1)
        top.addWidget(self.found)
        bottom = QHBoxLayout()
        bottom.addWidget(copy)
        bottom.addStretch(1)
        bottom.addWidget(close)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(10)
        layout.addLayout(top)
        layout.addWidget(self.view, 1)
        layout.addLayout(bottom)
        fit_to_screen(self, 780, 580)

    def _find(self) -> None:
        """The next match after the cursor, starting over from the top once the end is reached."""
        wanted = self.search.text()
        self.found.setText("")
        if not wanted:
            return
        if not self.view.find(wanted):
            self.view.moveCursor(QTextCursor.MoveOperation.Start)
            if not self.view.find(wanted):
                self.found.setText("Not found")

    def _find_from_start(self) -> None:
        self.view.moveCursor(QTextCursor.MoveOperation.Start)
        self._find()


class SettingsDialog(QDialog):
    """Preferences in three pages: appearance, terminal and behaviour.

    "Apply" and "Save" emit `applied` with the new Preferences; nothing is changed before that.
    """

    applied = pyqtSignal(object)

    def __init__(self, prefs: Preferences, parent=None):
        super().__init__(parent)
        self._original = prefs
        self.setWindowTitle("Settings")
        self.setMinimumSize(560, 430)

        # ---- navigation ----
        nav = QFrame()
        nav.setObjectName("SettingsNav")
        nav.setFixedWidth(168)
        nav_layout = QVBoxLayout(nav)
        nav_layout.setContentsMargins(10, 16, 10, 12)
        nav_layout.setSpacing(4)
        heading = QLabel("Settings")
        heading.setObjectName("SettingsTitle")
        heading.setContentsMargins(8, 0, 0, 10)
        nav_layout.addWidget(heading)
        self.pages = QStackedWidget()
        self._nav_group = QButtonGroup(self)
        self._page_index: dict[str, int] = {}
        for index, (key, name, build) in enumerate(
            (
                ("appearance", "Appearance", self._appearance_page),
                ("terminal", "Terminal", self._terminal_page),
                ("behaviour", "Behaviour", self._behaviour_page),
                ("transfers", "File Transfer", self._transfer_page),
                ("about", "About", self._about_page),
            )
        ):
            self._page_index[key] = index
            button = QPushButton(name)
            button.setCheckable(True)
            button.setChecked(index == 0)
            button.clicked.connect(lambda _checked, i=index: self.pages.setCurrentIndex(i))
            self._nav_group.addButton(button, index)
            nav_layout.addWidget(button)
            self.pages.addWidget(build())
        nav_layout.addStretch(1)
        self.pages.currentChanged.connect(lambda index: self._nav_group.button(index).setChecked(True))

        # ---- footer ----
        footer = QFrame()
        footer.setObjectName("SettingsFooter")
        buttons = QHBoxLayout(footer)
        buttons.setContentsMargins(16, 12, 16, 12)
        reset = QPushButton("Reset to defaults")
        reset.clicked.connect(lambda: self.set_preferences(Preferences()))
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        apply_button = QPushButton("Apply")
        apply_button.clicked.connect(self._apply)
        save = QPushButton("Save")
        save.setObjectName("Primary")
        save.setDefault(True)
        save.clicked.connect(self._save)
        buttons.addWidget(reset)
        buttons.addStretch(1)
        buttons.addWidget(cancel)
        buttons.addWidget(apply_button)
        buttons.addWidget(save)

        body = QHBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)
        body.addWidget(nav)
        body.addWidget(self.pages, 1)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addLayout(body, 1)
        layout.addWidget(footer)

        self.set_preferences(prefs)
        self.terminal_font.currentTextChanged.connect(self._update_preview)
        self.terminal_size.valueChanged.connect(self._update_preview)
        self.accent.colorChanged.connect(self._paint_theme_cards)
        self._theme_group.buttonToggled.connect(lambda *_: self._update_preview())
        fit_to_screen(self, 720, 540)

    # ---- pages ----------------------------------------------------------------

    @staticmethod
    def _page(title: str) -> tuple[QWidget, QVBoxLayout]:
        """A scrollable page, so the dialog stays usable on small screens."""
        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(24, 18, 24, 18)
        layout.setSpacing(12)
        heading = QLabel(title)
        heading.setObjectName("SettingsTitle")
        layout.addWidget(heading)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setWidget(content)
        return scroll, layout

    @staticmethod
    def _setting(title: str, description: str, control: QWidget) -> QWidget:
        """One settings row: what it is on the left, the control on the right."""
        row = QWidget()
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 4, 0, 4)
        layout.setSpacing(16)
        text = QVBoxLayout()
        text.setSpacing(2)
        text.addWidget(QLabel(title))
        if description:
            hint = QLabel(description)
            hint.setObjectName("Muted")
            hint.setWordWrap(True)
            text.addWidget(hint)
        layout.addLayout(text, 1)
        layout.addWidget(control, 0, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        return row

    def _appearance_page(self) -> QWidget:
        page, layout = self._page("Appearance")

        layout.addWidget(_section("THEME"))
        self._theme_group = QButtonGroup(self)
        self._theme_cards: dict[str, QToolButton] = {}
        cards = QHBoxLayout()
        cards.setSpacing(12)
        for value, label in ((THEME_SYSTEM, "System"), (THEME_LIGHT, "Light"), (THEME_DARK, "Dark")):
            card = QToolButton()
            card.setObjectName("ThemeCard")
            card.setCheckable(True)
            card.setText(label)
            card.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextUnderIcon)
            card.setIconSize(QSize(132, 84))
            card.setCursor(Qt.CursorShape.PointingHandCursor)
            self._theme_group.addButton(card)
            self._theme_cards[value] = card
            cards.addWidget(card)
        cards.addStretch(1)
        layout.addLayout(cards)
        self._theme_cards[THEME_SYSTEM].setToolTip("Follow the operating system's light or dark mode")

        layout.addSpacing(6)
        layout.addWidget(_section("ACCENT COLOUR"))
        self.accent = ColorPicker(none_tooltip="Theme default")
        layout.addWidget(self.accent)

        layout.addSpacing(6)
        layout.addWidget(_section("INTERFACE FONT"))
        self.ui_font = _font_combo(monospaced=False)
        self.ui_size = QSpinBox()
        self.ui_size.setRange(8, 16)
        self.ui_size.setSuffix(" pt")
        self.ui_size.setFixedWidth(96)
        layout.addWidget(_row(self.ui_font, self.ui_size))
        layout.addStretch(1)
        return page

    def _terminal_page(self) -> QWidget:
        page, layout = self._page("Terminal")

        layout.addWidget(_section("FONT"))
        self.terminal_font = _font_combo(monospaced=True)
        self.terminal_size = QSpinBox()
        self.terminal_size.setRange(MIN_FONT_SIZE, MAX_FONT_SIZE)
        self.terminal_size.setSuffix(" pt")
        self.terminal_size.setFixedWidth(96)
        layout.addWidget(_row(self.terminal_font, self.terminal_size))
        self.preview = QLabel(_PREVIEW_TEXT)
        self.preview.setObjectName("Preview")
        self.preview.setMinimumHeight(92)
        layout.addWidget(self.preview)

        layout.addSpacing(6)
        layout.addWidget(_section("COLOURS"))
        self.terminal_scheme = QComboBox()
        self.terminal_scheme.addItem("Follow the theme", "")
        for key, scheme in TERMINAL_SCHEMES.items():
            self.terminal_scheme.addItem(scheme[0], key)
        self.terminal_scheme.currentIndexChanged.connect(lambda _index: self._update_preview())
        layout.addWidget(self._setting("Colour scheme", "The terminal's background, text and sixteen colours.", self.terminal_scheme))
        layout.addSpacing(6)
        self.highlight_keywords = QCheckBox()
        layout.addWidget(
            self._setting(
                "Highlight keywords",
                "Colour words such as error and failed in red, warning in amber, success and passed in green.",
                self.highlight_keywords,
            )
        )
        self.highlight_words = QLineEdit()
        self.highlight_words.setPlaceholderText("Your own words to highlight, separated by commas")
        layout.addWidget(self.highlight_words)
        layout.addSpacing(6)
        layout.addWidget(_section("CURSOR"))
        self.cursor_style = Segmented([(CURSOR_BLOCK, "Block"), (CURSOR_BAR, "Bar"), (CURSOR_UNDERLINE, "Underline")])
        layout.addWidget(self._setting("Shape", "", self.cursor_style))
        self.cursor_blink = QCheckBox()
        layout.addWidget(self._setting("Blink", "The cursor flashes while the terminal has focus.", self.cursor_blink))

        self.visual_bell = QCheckBox()
        layout.addWidget(
            self._setting("Visual bell", "Flash the terminal when the remote side rings the bell.", self.visual_bell)
        )
        layout.addSpacing(6)
        layout.addWidget(_section("BUFFER AND CLIPBOARD"))
        self.scrollback = QSpinBox()
        self.scrollback.setRange(MIN_SCROLLBACK, MAX_SCROLLBACK)
        self.scrollback.setSingleStep(1000)
        self.scrollback.setSuffix(" lines")
        self.scrollback.setFixedWidth(140)
        layout.addWidget(self._setting("Scrollback", "How much output each tab keeps for scrolling back.", self.scrollback))
        self.copy_on_select = QCheckBox()
        layout.addWidget(
            self._setting("Copy on select", "Selecting text with the mouse copies it to the clipboard.", self.copy_on_select)
        )
        self.right_click_paste = QCheckBox()
        layout.addWidget(
            self._setting(
                "Right-click pastes", "Paste with the right mouse button; Ctrl+right-click opens the menu.", self.right_click_paste
            )
        )
        self.confirm_paste = QCheckBox()
        layout.addWidget(
            self._setting("Confirm multi-line paste", "Ask before pasting text that contains several lines.", self.confirm_paste)
        )
        self.paste_delay = QSpinBox()
        self.paste_delay.setRange(0, MAX_PASTE_DELAY_MS)
        self.paste_delay.setSingleStep(50)
        self.paste_delay.setSuffix(" ms")
        self.paste_delay.setSpecialValueText("Off")
        self.paste_delay.setFixedWidth(120)
        layout.addWidget(
            self._setting(
                "Pause between pasted lines",
                "For switches, routers and serial consoles that lose characters when many lines arrive at once. "
                "Esc stops a paste that is still going.",
                self.paste_delay,
            )
        )
        layout.addStretch(1)
        return page

    def _behaviour_page(self) -> QWidget:
        page, layout = self._page("Behaviour")
        layout.addWidget(_section("CONFIRMATIONS"))
        self.confirm_quit = QCheckBox()
        layout.addWidget(
            self._setting("Confirm on quit", "Ask before closing the window while sessions are open.", self.confirm_quit)
        )
        self.confirm_close_tab = QCheckBox()
        layout.addWidget(
            self._setting("Confirm closing a tab", "Ask before closing a tab that is still connected.", self.confirm_close_tab)
        )
        layout.addSpacing(6)
        layout.addWidget(_section("TABS"))
        self.compact_pinned = QCheckBox()
        layout.addWidget(
            self._setting(
                "Pinned tabs show only their icon",
                "Saves room in the tab bar; the name is in the tooltip.",
                self.compact_pinned,
            )
        )
        self.tab_list_filter_at = QSpinBox()
        self.tab_list_filter_at.setRange(1, 99)
        self.tab_list_filter_at.setFixedWidth(110)
        layout.addWidget(
            self._setting(
                "Filter the list of open tabs from",
                "The list beside the + button gets a box to filter it when more tabs than this are open.",
                self.tab_list_filter_at,
            )
        )
        layout.addSpacing(6)
        layout.addWidget(_section("CONNECTIONS"))
        self.auto_reconnect = QCheckBox()
        layout.addWidget(
            self._setting(
                "Reconnect automatically",
                "When a connection drops unexpectedly, try again up to five times.",
                self.auto_reconnect,
            )
        )
        self.server_monitor = QCheckBox()
        layout.addWidget(
            self._setting(
                "Server monitor",
                "Show the load, memory and disk use of the active SSH tab's server in the status bar. "
                "It reads them every ten seconds on a channel of its own, out of sight of the terminal.",
                self.server_monitor,
            )
        )
        self.server_alerts = QCheckBox()
        layout.addWidget(
            self._setting(
                "Server alerts",
                "Tell when a connected server's disk, memory, load or round trip turns critical, also in "
                "tabs not in front, which are checked once a minute. Each is told once, until it recovers.",
                self.server_alerts,
            )
        )
        self.keepalive = QSpinBox()
        self.keepalive.setRange(0, 3600)
        self.keepalive.setSingleStep(10)
        self.keepalive.setSuffix(" s")
        self.keepalive.setSpecialValueText("Off")
        self.keepalive.setFixedWidth(110)
        layout.addWidget(
            self._setting(
                "Keep-alive",
                "How often an idle connection sends a signal so that firewalls do not drop it. Applies to new connections.",
                self.keepalive,
            )
        )
        self.restore_tabs = QCheckBox()
        layout.addWidget(
            self._setting(
                "Reopen tabs at start",
                "Open the saved sessions that were open when the application was last closed.",
                self.restore_tabs,
            )
        )
        layout.addSpacing(6)
        layout.addWidget(_section("LOGGING"))
        self.auto_log = QCheckBox()
        layout.addWidget(
            self._setting("Log every session", "Save each session's output to a text file when it connects.", self.auto_log)
        )
        self.log_timestamps = QCheckBox()
        layout.addWidget(
            self._setting("Timestamps in logs", "Start every logged line with the time it arrived.", self.log_timestamps)
        )
        self.log_dir = QLineEdit()
        self.log_dir.setPlaceholderText(str(Preferences().log_directory()))
        browse = QPushButton("Browse")
        browse.clicked.connect(self._browse_log_dir)
        layout.addWidget(_row(self.log_dir, browse))
        layout.addSpacing(6)
        layout.addWidget(_section("LAYOUT"))
        self.stack_panes = QCheckBox()
        layout.addWidget(
            self._setting(
                "Stack split panes",
                "Put split panes one above the other instead of side by side (Ctrl+Shift+O).",
                self.stack_panes,
            )
        )
        self.pane_grid = QCheckBox()
        layout.addWidget(
            self._setting(
                "Grid of panes",
                "With three or four panes, place them in two columns: the first pane on the left and "
                "the others stacked on the right, or two above two.",
                self.pane_grid,
            )
        )
        self.show_send_bar = QCheckBox()
        layout.addWidget(
            self._setting(
                "Send bar", "Show the bar under the terminal that sends a command to one or all tabs.", self.show_send_bar
            )
        )
        layout.addStretch(1)
        return page

    def show_page(self, key: str) -> None:
        """Switch to a page by its key: appearance, terminal, behaviour, transfers or about."""
        if key in self._page_index:
            self.pages.setCurrentIndex(self._page_index[key])

    def _transfer_page(self) -> QWidget:
        page, layout = self._page("File Transfer")
        layout.addWidget(_section("TRANSFERS"))
        self.transfer_parallel = QSpinBox()
        self.transfer_parallel.setRange(1, MAX_TRANSFERS)
        self.transfer_parallel.setFixedWidth(110)
        layout.addWidget(
            self._setting(
                "Simultaneous transfers",
                "How many files are copied at the same time. Each uses a channel of its own on the connection, "
                "and servers allow about ten. A change applies at once, in file windows that are open too.",
                self.transfer_parallel,
            )
        )
        self.transfer_limit = QSpinBox()
        self.transfer_limit.setRange(0, 1_000_000)
        self.transfer_limit.setSingleStep(64)
        self.transfer_limit.setSuffix(" KB/s")
        self.transfer_limit.setSpecialValueText("Unlimited")
        self.transfer_limit.setFixedWidth(130)
        layout.addWidget(
            self._setting(
                "Speed limit",
                "The most that the running transfers use together; they share it.",
                self.transfer_limit,
            )
        )
        layout.addSpacing(6)
        layout.addWidget(_section("FILES"))
        self.transfer_confirm_replace = QCheckBox()
        layout.addWidget(
            self._setting(
                "Ask before replacing files",
                "Ask when an upload or a download would replace a file that is already there.",
                self.transfer_confirm_replace,
            )
        )
        self.transfer_keep_dates = QCheckBox()
        layout.addWidget(
            self._setting(
                "Keep file dates",
                "Give the copy the modification date of the original, so that sorting by date still means something.",
                self.transfer_keep_dates,
            )
        )
        self.transfer_resume = QCheckBox()
        layout.addWidget(
            self._setting(
                "Continue partial transfers",
                "Carry on from a file that was only partly transferred (named with .part) instead of starting over.",
                self.transfer_resume,
            )
        )
        layout.addStretch(1)
        return page

    def _browse_log_dir(self) -> None:
        start = self.log_dir.text() or str(Preferences().log_directory())
        chosen = QFileDialog.getExistingDirectory(self, "Folder for Session Logs", start)
        if chosen:
            self.log_dir.setText(chosen)

    def _about_page(self) -> QWidget:
        page, layout = self._page("About")
        # The version and licence are chips under the name, and the author has a line of their own.
        icon = QLabel()
        icon.setPixmap(app_icon().pixmap(72, 72))
        name = QLabel(APP_NAME)
        name.setObjectName("AboutName")
        tagline = QLabel("A modern tabbed SSH terminal.")
        tagline.setObjectName("Muted")
        chips = QHBoxLayout()
        chips.setSpacing(6)
        for text in (f"Version {__version__}", LICENSE_NAME):
            chip = QLabel(text)
            chip.setObjectName("Chip")
            chips.addWidget(chip)
        chips.addStretch(1)
        self.author = QLabel(f"Author: {AUTHOR}")
        self.author.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        titles = QVBoxLayout()
        titles.setSpacing(4)
        titles.addWidget(name)
        titles.addWidget(tagline)
        titles.addSpacing(4)
        titles.addLayout(chips)
        titles.addSpacing(2)
        titles.addWidget(self.author)
        header = QHBoxLayout()
        header.setSpacing(18)
        header.addWidget(icon, 0, Qt.AlignmentFlag.AlignTop)
        header.addLayout(titles, 1)
        layout.addSpacing(6)
        layout.addLayout(header)

        # What a bug report needs, with the buttons that act on it inside the same card.
        form = _form()
        for label, value in _system_details():
            shown = QLabel(value)
            shown.setWordWrap(True)
            shown.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            form.addRow(label, shown)
        self.data_folder_label = _ElidedLabel(str(CONFIG_DIR))
        form.addRow("Data folder (portable)" if portable_folder() else "Data folder", self.data_folder_label)
        for label in _form_labels(form):
            label.setObjectName("Muted")
        self.copy_version = QPushButton("Copy version info")
        self.copy_version.setToolTip("Copy the versions above as text, to paste into a bug report")
        self.copy_version.clicked.connect(self._copy_version_info)
        self._copied = QTimer(self)
        self._copied.setSingleShot(True)
        self._copied.setInterval(1500)
        self._copied.timeout.connect(lambda: self.copy_version.setText("Copy version info"))
        data_folder = QPushButton("Open data folder")
        data_folder.setToolTip("Show where the saved sessions, logs and the error log are kept")
        data_folder.clicked.connect(self._open_data_folder)
        support = QHBoxLayout()
        support.setSpacing(8)
        support.addWidget(self.copy_version)
        support.addWidget(data_folder)
        support.addStretch(1)
        card = QFrame()
        card.setObjectName("Card")
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(18, 14, 18, 14)
        card_layout.setSpacing(14)
        card_layout.addLayout(form)
        card_layout.addLayout(support)
        layout.addSpacing(10)
        layout.addWidget(_section("SYSTEM"))
        layout.addWidget(card)

        terms = QLabel(
            "Free software under the GNU General Public License, version 3: you may use, study, change "
            "and share it. It comes with no warranty. It is built on Python, Qt (through PyQt6), paramiko, "
            "pyte, keyring and pyserial, with the Ubuntu fonts, each under its own licence."
        )
        terms.setObjectName("Muted")
        terms.setWordWrap(True)
        read_licence = QPushButton("Read the licence")
        read_licence.setToolTip("The GNU General Public License, version 3, as shipped with this program")
        read_licence.clicked.connect(lambda: self._read("Licence", licences.licence, LICENSE_URL))
        read_third_party = QPushButton("Third-party licences")
        read_third_party.setToolTip("The licences of the libraries and fonts this program is built on")
        read_third_party.clicked.connect(lambda: self._read("Third-Party Licences", licences.third_party_notices))
        legal = QHBoxLayout()
        legal.setSpacing(8)
        legal.addWidget(read_licence)
        legal.addWidget(read_third_party)
        if licences.find("CHANGELOG.md") is not None:
            release_notes = QPushButton("Release notes")
            release_notes.setToolTip("What each version added or fixed")
            release_notes.clicked.connect(lambda: self._read("Release Notes", licences.release_notes))
            legal.addWidget(release_notes)
        legal.addStretch(1)
        layout.addSpacing(10)
        layout.addWidget(_section("LICENCE AND CREDITS"))
        layout.addWidget(terms)
        layout.addSpacing(2)
        layout.addLayout(legal)
        layout.addStretch(1)
        return page

    def _copy_version_info(self) -> None:
        QGuiApplication.clipboard().setText(version_info())
        self.copy_version.setText("Copied")
        self._copied.start()

    @staticmethod
    def _open_data_folder() -> None:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(CONFIG_DIR)))

    def _read(self, title: str, load, fallback_url: str = "") -> None:
        """Show a text in a window of its own; if it is not there, open its web page instead, if there is one."""
        text = load()
        if text is None:
            if fallback_url:
                QDesktopServices.openUrl(QUrl(fallback_url))
            return
        TextViewer(title, text, self).exec()

    # ---- values ---------------------------------------------------------------

    def set_preferences(self, prefs: Preferences) -> None:
        self._theme_cards.get(prefs.theme, self._theme_cards[THEME_DARK]).setChecked(True)
        self.accent.set_color(prefs.accent)
        _select_font(self.ui_font, prefs.ui_font, DEFAULT_UI_FONT)
        self.ui_size.setValue(prefs.ui_size)
        _select_font(self.terminal_font, prefs.terminal_font, DEFAULT_TERMINAL_FONT)
        self.terminal_size.setValue(prefs.terminal_size)
        self.cursor_style.set_value(prefs.cursor_style)
        self.cursor_blink.setChecked(prefs.cursor_blink)
        self.scrollback.setValue(prefs.scrollback)
        self.copy_on_select.setChecked(prefs.copy_on_select)
        self.confirm_paste.setChecked(prefs.confirm_paste)
        self.paste_delay.setValue(prefs.paste_line_delay_ms)
        self.right_click_paste.setChecked(prefs.right_click_paste)
        self.visual_bell.setChecked(prefs.visual_bell)
        self.highlight_keywords.setChecked(prefs.highlight_keywords)
        self.highlight_words.setText(prefs.highlight_words)
        self.auto_reconnect.setChecked(prefs.auto_reconnect)
        self.server_monitor.setChecked(prefs.server_monitor)
        self.server_alerts.setChecked(prefs.server_alerts)
        self.auto_log.setChecked(prefs.auto_log)
        self.log_timestamps.setChecked(prefs.log_timestamps)
        self.keepalive.setValue(prefs.keepalive_seconds)
        self.compact_pinned.setChecked(prefs.compact_pinned)
        self.tab_list_filter_at.setValue(prefs.tab_list_filter_at)
        self.restore_tabs.setChecked(prefs.restore_tabs)
        self.terminal_scheme.setCurrentIndex(max(0, self.terminal_scheme.findData(prefs.terminal_scheme)))
        self.log_dir.setText(prefs.log_dir)
        self.confirm_quit.setChecked(prefs.confirm_quit)
        self.confirm_close_tab.setChecked(prefs.confirm_close_tab)
        self.show_send_bar.setChecked(prefs.show_send_bar)
        self.stack_panes.setChecked(prefs.stack_panes)
        self.pane_grid.setChecked(prefs.pane_grid)
        self.transfer_parallel.setValue(prefs.transfer_parallel)
        self.transfer_limit.setValue(prefs.transfer_limit_kbps)
        self.transfer_confirm_replace.setChecked(prefs.transfer_confirm_replace)
        self.transfer_keep_dates.setChecked(prefs.transfer_keep_dates)
        self.transfer_resume.setChecked(prefs.transfer_resume)
        self._paint_theme_cards()
        self._update_preview()

    def preferences(self) -> Preferences:
        return replace(
            self._original,
            theme=next(value for value, card in self._theme_cards.items() if card.isChecked()),
            accent=self.accent.color(),
            ui_font=self.ui_font.currentText(),
            ui_size=self.ui_size.value(),
            terminal_font=self.terminal_font.currentText(),
            terminal_size=self.terminal_size.value(),
            cursor_style=self.cursor_style.value(),
            cursor_blink=self.cursor_blink.isChecked(),
            scrollback=self.scrollback.value(),
            copy_on_select=self.copy_on_select.isChecked(),
            confirm_paste=self.confirm_paste.isChecked(),
            paste_line_delay_ms=self.paste_delay.value(),
            right_click_paste=self.right_click_paste.isChecked(),
            visual_bell=self.visual_bell.isChecked(),
            highlight_keywords=self.highlight_keywords.isChecked(),
            highlight_words=self.highlight_words.text().strip(),
            auto_reconnect=self.auto_reconnect.isChecked(),
            server_monitor=self.server_monitor.isChecked(),
            server_alerts=self.server_alerts.isChecked(),
            auto_log=self.auto_log.isChecked(),
            log_timestamps=self.log_timestamps.isChecked(),
            keepalive_seconds=self.keepalive.value(),
            compact_pinned=self.compact_pinned.isChecked(),
            tab_list_filter_at=self.tab_list_filter_at.value(),
            restore_tabs=self.restore_tabs.isChecked(),
            terminal_scheme=self.terminal_scheme.currentData() or "",
            log_dir=self.log_dir.text().strip(),
            confirm_quit=self.confirm_quit.isChecked(),
            confirm_close_tab=self.confirm_close_tab.isChecked(),
            show_send_bar=self.show_send_bar.isChecked(),
            stack_panes=self.stack_panes.isChecked(),
            pane_grid=self.pane_grid.isChecked(),
            transfer_parallel=self.transfer_parallel.value(),
            transfer_limit_kbps=self.transfer_limit.value(),
            transfer_confirm_replace=self.transfer_confirm_replace.isChecked(),
            transfer_keep_dates=self.transfer_keep_dates.isChecked(),
            transfer_resume=self.transfer_resume.isChecked(),
        )

    def _paint_theme_cards(self) -> None:
        for value, card in self._theme_cards.items():
            card.setIcon(QIcon(theme_preview(value, self.accent.color())))

    def _update_preview(self) -> None:
        family = self.terminal_font.currentText()
        # A widget stylesheet is the only thing that outranks the application-wide font rule.
        theme = next((value for value, card in self._theme_cards.items() if card.isChecked()), THEME_DARK)
        concrete = theme if theme in (THEME_DARK, THEME_LIGHT) else active_theme()
        background, text, _selection, _sixteen = terminal_colors(concrete, self.terminal_scheme.currentData() or "")
        self.preview.setStyleSheet(
            f'font-family: "{family}"; font-size: {self.terminal_size.value()}pt;'
            f" background: {background}; color: {text};"
        )

    def _apply(self) -> None:
        self._original = self.preferences()
        self.applied.emit(self._original)
        # The theme may just have changed under this dialog; redraw what it painted itself.
        self.accent.set_color(self.accent.color(), force=True)

    def _save(self) -> None:
        self._apply()
        self.accept()


class SnippetsDialog(QDialog):
    """Edit the list of saved commands. `snippets()` gives the result after it is accepted."""

    def __init__(self, snippets: list[Snippet], parent=None):
        super().__init__(parent)
        self.setWindowTitle("Saved Commands")
        self.setMinimumSize(520, 340)
        fit_to_screen(self, 680, 420)
        self._snippets = [Snippet(s.name, s.command) for s in snippets]
        self._row = -1

        self.list = QListWidget()
        self.list.setObjectName("SnippetList")
        self.list.setFixedWidth(210)
        self.list.currentRowChanged.connect(self._show)
        add = QPushButton("Add")
        add.clicked.connect(self.add)
        self.remove_button = QPushButton("Delete")
        self.remove_button.clicked.connect(self.remove)
        # The order here is the order of the menu, so it can be changed.
        self.up_button = self._order_button("chevron-up", "Move up", -1)
        self.down_button = self._order_button("chevron-down", "Move down", 1)
        list_buttons = QHBoxLayout()
        list_buttons.addWidget(add)
        list_buttons.addWidget(self.remove_button)
        list_buttons.addWidget(self.up_button)
        list_buttons.addWidget(self.down_button)
        left = QVBoxLayout()
        left.addWidget(self.list, 1)
        left.addLayout(list_buttons)

        self.name = QLineEdit()
        self.name.setPlaceholderText("Name shown in the menu")
        self.name.textEdited.connect(self._store)
        self.command = QPlainTextEdit()
        self.command.setPlaceholderText("Command to send. Several lines are sent one after another.")
        self.command.textChanged.connect(self._store)
        # Shown all the time, not only while the box is empty, so it is there when editing.
        placeholders = QLabel(
            "{host}, {user}, {port} and {name} are filled in with the values of each tab it goes to. "
            "{?label} or {?label=default} asks for a value each time the command is sent."
        )
        placeholders.setObjectName("Muted")
        placeholders.setWordWrap(True)
        right = QVBoxLayout()
        right.addWidget(_section("NAME"))
        right.addWidget(self.name)
        right.addSpacing(6)
        right.addWidget(_section("COMMAND"))
        right.addWidget(self.command, 1)
        right.addWidget(placeholders)

        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        save = QPushButton("Save")
        save.setObjectName("Primary")
        save.setDefault(True)
        save.clicked.connect(self.accept)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        buttons.addWidget(cancel)
        buttons.addWidget(save)

        body = QHBoxLayout()
        body.setSpacing(16)
        body.addLayout(left)
        body.addLayout(right, 1)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 16)
        layout.setSpacing(14)
        layout.addLayout(body, 1)
        layout.addLayout(buttons)

        for snippet in self._snippets:
            self.list.addItem(snippet.title)
        self.list.setCurrentRow(0 if self._snippets else -1)
        self._show(self.list.currentRow())

    def _show(self, row: int) -> None:
        self._row = -1  # while the fields are filled in, do not write them back
        has = 0 <= row < len(self._snippets)
        self.name.setEnabled(has)
        self.command.setEnabled(has)
        self.remove_button.setEnabled(has)
        self.up_button.setEnabled(has and row > 0)
        self.down_button.setEnabled(has and row < len(self._snippets) - 1)
        self.name.setText(self._snippets[row].name if has else "")
        self.command.setPlainText(self._snippets[row].command if has else "")
        self._row = row if has else -1

    def _store(self) -> None:
        if self._row < 0:
            return
        snippet = self._snippets[self._row]
        snippet.name, snippet.command = self.name.text().strip(), self.command.toPlainText()
        self.list.item(self._row).setText(snippet.title)

    def add(self) -> None:
        self._snippets.append(Snippet())
        self.list.addItem(self._snippets[-1].title)
        self.list.setCurrentRow(len(self._snippets) - 1)
        self.name.setFocus()

    def remove(self) -> None:
        row = self.list.currentRow()
        if 0 <= row < len(self._snippets):
            self._row = -1
            del self._snippets[row]
            self.list.takeItem(row)
            self._show(self.list.currentRow())

    def _order_button(self, icon: str, tooltip: str, step: int) -> QToolButton:
        button = QToolButton()
        button.setObjectName("Icon")
        button.setIcon(line_icon(icon))
        button.setIconSize(QSize(16, 16))
        button.setToolTip(tooltip)
        button.setAccessibleName(tooltip.split(" (")[0])
        button.clicked.connect(lambda: self.move_command(step))
        return button

    def move_command(self, step: int) -> None:
        """Move the selected command up (-1) or down (1) the list."""
        row = self.list.currentRow()
        target = row + step
        if not (0 <= row < len(self._snippets) and 0 <= target < len(self._snippets)):
            return
        self._row = -1
        self._snippets[row], self._snippets[target] = self._snippets[target], self._snippets[row]
        self.list.insertItem(target, self.list.takeItem(row))
        self.list.setCurrentRow(target)
        self._show(target)

    def snippets(self) -> list[Snippet]:
        return [snippet for snippet in self._snippets if snippet.command.strip()]


class HostKeysDialog(QDialog):
    """The host keys that were approved. A server whose key was forgotten is asked about again at its next login,
    which is what to do after its key changed on purpose."""

    def __init__(self, keys: list[ApprovedKey], forget, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Approved Host Keys")
        self.setMinimumSize(480, 320)
        fit_to_screen(self, 760, 420)
        self._keys = keys
        self._forget = forget  # callable(host) -> number removed

        self.filter = QLineEdit()
        self.filter.setPlaceholderText("Filter by name or fingerprint")
        self.filter.setClearButtonEnabled(True)
        self.filter.setAccessibleName("Filter host keys")
        self.filter.textChanged.connect(self._fill)
        self.list = QListWidget()
        self.list.setObjectName("HostKeyList")
        self.list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.list.itemSelectionChanged.connect(self._selection_changed)
        self.count = QLabel()
        self.count.setObjectName("Muted")

        self.forget_button = QPushButton("Forget")
        self.forget_button.setToolTip("Ask again about the selected hosts at their next login")
        self.forget_button.clicked.connect(self.forget_selected)
        self.copy_button = QPushButton("Copy Fingerprint")
        self.copy_button.clicked.connect(self.copy_fingerprints)
        close = QPushButton("Close")
        close.setDefault(True)
        close.clicked.connect(self.accept)
        buttons = QHBoxLayout()
        buttons.addWidget(self.forget_button)
        buttons.addWidget(self.copy_button)
        buttons.addStretch(1)
        buttons.addWidget(close)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 16)
        layout.setSpacing(12)
        layout.addWidget(self.filter)
        layout.addWidget(self.list, 1)
        layout.addWidget(self.count)
        layout.addLayout(buttons)
        self._fill()

    def _fill(self) -> None:
        words = self.filter.text().lower().split()
        self.list.clear()
        shown = 0
        for key in self._keys:
            text = f"{key.host}    {key.key_type}    {key.fingerprint}"
            if all(word in text.lower() for word in words):
                item = QListWidgetItem(text)
                item.setData(Qt.ItemDataRole.UserRole, key)
                if key.hashed:
                    item.setToolTip("The name is kept as a hash, so this key cannot be forgotten from here")
                self.list.addItem(item)
                shown += 1
        self.count.setText(f"{shown} of {len(self._keys)} approved" if words else f"{len(self._keys)} approved")
        self._selection_changed()

    def _selected(self) -> list[ApprovedKey]:
        return [item.data(Qt.ItemDataRole.UserRole) for item in self.list.selectedItems()]

    def _selection_changed(self) -> None:
        selected = self._selected()
        self.forget_button.setEnabled(any(not key.hashed for key in selected))
        self.copy_button.setEnabled(bool(selected))

    def forget_selected(self) -> int:
        """Forget the keys of the selected hosts, after asking; returns how many keys went."""
        hosts = sorted({key.host for key in self._selected() if not key.hashed})
        if not hosts:
            return 0
        listed = hosts[0] if len(hosts) == 1 else f"{len(hosts)} hosts"
        answer = QMessageBox.question(
            self, "Forget Host Keys", f"Forget the keys of {listed}? They will be asked about again at the next login."
        )
        if answer != QMessageBox.StandardButton.Yes:
            return 0
        removed = sum(self._forget(host) for host in hosts)
        self._keys = [key for key in self._keys if key.hashed or key.host not in hosts]
        self._fill()
        return removed

    def copy_fingerprints(self) -> None:
        QGuiApplication.clipboard().setText("\n".join(key.fingerprint for key in self._selected()))


class CommandValuesDialog(QDialog):
    """Asks for the values a saved command wants, one field per {?label}. `values()` gives the answers."""

    def __init__(self, title: str, prompts: list[tuple[str, str]], parent=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setMinimumWidth(360)
        fit_to_screen(self, 460, 120 + 36 * len(prompts))
        self.fields: dict[str, QLineEdit] = {}
        form = QFormLayout()
        form.setSpacing(10)
        for label, default in prompts:
            field = QLineEdit(default)
            field.setAccessibleName(label)
            field.returnPressed.connect(self.accept)
            self.fields[label] = field
            form.addRow(label, field)

        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        send = QPushButton("Send")
        send.setObjectName("Primary")
        send.setDefault(True)
        send.clicked.connect(self.accept)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        buttons.addWidget(cancel)
        buttons.addWidget(send)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 16)
        layout.setSpacing(14)
        layout.addLayout(form)
        layout.addLayout(buttons)
        first = next(iter(self.fields.values()), None)
        if first is not None:
            first.setFocus()
            first.selectAll()

    def values(self) -> dict[str, str]:
        return {label: field.text() for label, field in self.fields.items()}


class TasksDialog(QDialog):
    """Edit the scheduled tasks. `tasks()` gives the result after it is accepted.

    Run Now runs a task as it stands in the window, saved or not, so it can be tried before it is saved.
    """

    def __init__(self, task_list: list[Task], sessions: list[Session], scheduler, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Scheduled Tasks")
        self.setMinimumSize(620, 460)
        fit_to_screen(self, 900, 640)
        self._tasks = [replace(task) for task in task_list]
        self._scheduler = scheduler
        self._row = -1

        self.list = QListWidget()
        self.list.setObjectName("SnippetList")
        self.list.setFixedWidth(210)
        self.list.currentRowChanged.connect(self._show)
        add = QPushButton("Add")
        add.clicked.connect(self.add)
        self.remove_button = QPushButton("Delete")
        self.remove_button.clicked.connect(self.remove)
        list_buttons = QHBoxLayout()
        list_buttons.addWidget(add)
        list_buttons.addWidget(self.remove_button)
        left = QVBoxLayout()
        left.addWidget(self.list, 1)
        left.addLayout(list_buttons)

        self.name = QLineEdit()
        self.name.setPlaceholderText("Name shown in the list and in the output file")
        self.target = QComboBox()
        for value, label in LOCAL_TARGETS:
            self.target.addItem(label, value)
        ssh_sessions = sorted((s for s in sessions if s.protocol == PROTO_SSH), key=lambda s: s.title.lower())
        if ssh_sessions:
            self.target.insertSeparator(self.target.count())
        for session in ssh_sessions:
            self.target.addItem(f"{session.title}   ({session.address})", session.id)
        self.target.setToolTip(
            "Where the commands run. A server must have been connected to once from a tab, so that its key is\n"
            "known, and must log in without asking: with a key, the SSH agent or a saved password."
        )
        self.commands = QPlainTextEdit()
        self.commands.setPlaceholderText("One command per line, for example:\nuptime\ndf -h\ncd /var/log && tail -n 50 syslog")
        self.commands.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.commands_hint = commands_hint = QLabel()
        commands_hint.setObjectName("Muted")
        commands_hint.setWordWrap(True)
        self.as_script = QCheckBox("Run the lines as one script")
        self.as_script.setToolTip(
            "Ticked: all lines run in one shell, so a cd or a variable holds for the lines after it,\n"
            "and the run has one exit status. Unticked: each line runs on its own, with its own exit status."
        )
        self.output_file = QLineEdit()
        self.output_file.setPlaceholderText(str(OUTPUT_DIR / "{name}.txt"))
        self.output_file.setToolTip(
            "{name}, {date} and {time} are filled in for each run: C:\\Reports\\{name}-{date}.txt makes a file a day.\n"
            f"A name without a folder is kept in {OUTPUT_DIR}."
        )
        browse = QPushButton("Browse")
        browse.clicked.connect(self._browse_output)
        self.append = QCheckBox("Add each run to the end of the file (otherwise each run replaces it)")
        self.schedule = QComboBox()
        for value, label in SCHEDULES:
            self.schedule.addItem(label, value)
        self.every = QSpinBox()
        self.every.setRange(1, MAX_INTERVAL_MINUTES)
        self.every.setSuffix(" minutes")
        self.every.setFixedWidth(130)
        self.daily_at = QTimeEdit()
        self.daily_at.setDisplayFormat("HH:mm")
        self.daily_at.setFixedWidth(100)
        self.runner = QComboBox()
        self.runner.addItem(f"{APP_NAME}, while it is open", RUNNER_APP)
        self.runner.addItem("Windows Task Scheduler, also while it is closed", RUNNER_WINDOWS)
        self.runner.setToolTip(
            "Windows Task Scheduler starts this program in the background at the times above, while you are\n"
            f"signed in to Windows. The task appears there in the {WINDOWS_FOLDER} folder."
        )
        self.stop_on_error = QCheckBox("Stop at the first command that fails")
        self.timeout = QSpinBox()
        self.timeout.setRange(5, 86_400)
        self.timeout.setSuffix(" s")
        self.timeout.setFixedWidth(110)
        self.timeout.setToolTip("A command still running after this long is stopped, and the task carries on")
        self.enabled = QCheckBox("Enabled")
        self.enabled.setToolTip("Untick to pause the schedule; Run Now still works")
        self.then_run = QComboBox()
        self.then_run.setToolTip(
            "Another task to start when every command of this one succeeded, so that a backup can be followed\n"
            "by its check. It is started in the same way (by this program, or by Windows) and not if it is running."
        )
        self.notify_on_failure = QCheckBox("Notify me when a run fails")
        self.notify_on_failure.setToolTip(
            "A desktop notification, when the window is not in front or the task is run by Windows.\n"
            "Clicking it opens the output file."
        )
        self.status = QLabel()
        self.status.setObjectName("Muted")
        self.status.setWordWrap(True)
        self.status.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)

        form = _form()
        form.addRow("Name", self.name)
        form.addRow("Run on", self.target)
        form.addRow("Commands", self.commands)
        form.addRow("", commands_hint)
        form.addRow("", self.as_script)
        form.addRow("Output file", _row(self.output_file, browse))
        form.addRow("", self.append)
        self.schedule_row = _row(self.schedule, self.every, self.daily_at, stretch_first=False)
        self.schedule_row.layout().addStretch(1)
        form.addRow("Schedule", self.schedule_row)
        form.addRow("Run by", self.runner)
        form.addRow("Then run", self.then_run)
        limit_row = _row(self.timeout, self.stop_on_error, stretch_first=False)
        limit_row.layout().addStretch(1)
        form.addRow("Time limit", limit_row)
        enabled_row = _row(self.enabled, self.notify_on_failure, stretch_first=False)
        enabled_row.layout().addStretch(1)
        form.addRow("", enabled_row)
        self._form = form
        right = QVBoxLayout()
        right.addLayout(form, 1)
        right.addWidget(self.status)

        self.run_button = QPushButton("Run Now")
        self.run_button.setToolTip("Run the task as it is now, saved or not, and write its output file")
        self.run_button.clicked.connect(self.run_now)
        self.open_button = QPushButton("Open Output")
        self.open_button.clicked.connect(self.open_output)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        save = QPushButton("Save")
        save.setObjectName("Primary")
        save.setDefault(True)
        save.clicked.connect(self._finish)
        for button in (self.run_button, self.open_button, cancel):
            button.setAutoDefault(False)
        self.error = QLabel()
        self.error.setStyleSheet(f"color: {COLORS['red']};")
        self.error.setWordWrap(True)
        buttons = QHBoxLayout()
        buttons.addWidget(self.run_button)
        buttons.addWidget(self.open_button)
        buttons.addWidget(self.error, 1)
        buttons.addWidget(cancel)
        buttons.addWidget(save)

        body = QHBoxLayout()
        body.setSpacing(16)
        body.addLayout(left)
        body.addLayout(right, 1)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 16)
        layout.setSpacing(14)
        layout.addLayout(body, 1)
        layout.addLayout(buttons)

        for changed in (
            self.name.textEdited,
            self.commands.textChanged,
            self.output_file.textEdited,
            self.append.toggled,
            self.schedule.currentIndexChanged,
            self.every.valueChanged,
            self.daily_at.timeChanged,
            self.runner.currentIndexChanged,
            self.stop_on_error.toggled,
            self.timeout.valueChanged,
            self.enabled.toggled,
            self.target.currentIndexChanged,
            self.as_script.toggled,
            self.notify_on_failure.toggled,
            self.then_run.currentIndexChanged,
        ):
            changed.connect(self._store)
        if scheduler is not None:
            scheduler.started.connect(self._run_changed)
            scheduler.finished.connect(self._run_changed)

        for task in self._tasks:
            self.list.addItem(task.title)
        self._mark_results()
        self.list.setCurrentRow(0 if self._tasks else -1)
        self._show(self.list.currentRow())

    def _mark_results(self) -> None:
        """A tick or a cross beside each task for how it last went, a clock while it runs."""
        for row, task in enumerate(self._tasks):
            item = self.list.item(row)
            if self._scheduler is not None and self._scheduler.is_running(task.id):
                item.setIcon(line_icon("clock", COLORS["amber"]))
                item.setToolTip("Running")
            elif task.last_run and task.last_result:
                item.setIcon(line_icon("check", COLORS["green"]) if task.last_ok else line_icon("close", COLORS["red"]))
                item.setToolTip(f"Last run {datetime.fromtimestamp(task.last_run):%Y-%m-%d %H:%M}: {task.last_result}")
            else:
                item.setIcon(QIcon())
                item.setToolTip("Not run yet")

    def _current(self) -> Task | None:
        return self._tasks[self._row] if 0 <= self._row < len(self._tasks) else None

    def _show(self, row: int) -> None:
        self._row = -1  # while the fields are filled in, do not write them back
        has = 0 <= row < len(self._tasks)
        task = self._tasks[row] if has else Task()
        for widget in (
            self.name, self.target, self.commands, self.output_file, self.append, self.schedule, self.every,
            self.daily_at, self.runner, self.stop_on_error, self.timeout, self.enabled, self.remove_button,
            self.open_button, self.as_script, self.notify_on_failure, self.then_run,
        ):  # fmt: skip
            widget.setEnabled(has)
        self.name.setText(task.name)
        index = self.target.findData(task.target)
        if index < 0 and has:
            self.target.addItem("(a deleted session)", task.target)
            index = self.target.count() - 1
        self.target.setCurrentIndex(max(0, index))
        self.commands.setPlainText(task.commands)
        self.output_file.setText(task.output_file)
        self.append.setChecked(task.append)
        self.schedule.setCurrentIndex(max(0, self.schedule.findData(task.schedule)))
        self.every.setValue(task.every_minutes)
        self.daily_at.setTime(QTime(*(parse_time_of_day(task.daily_at) or (9, 0))))
        self.runner.setCurrentIndex(max(0, self.runner.findData(task.runner)))
        self.stop_on_error.setChecked(task.stop_on_error)
        self.timeout.setValue(task.timeout)
        self.enabled.setChecked(task.enabled)
        self.as_script.setChecked(task.as_script)
        self.notify_on_failure.setChecked(task.notify_on_failure)
        self.then_run.clear()
        self.then_run.addItem("Nothing", "")
        for other in self._tasks:
            if other is not task:
                self.then_run.addItem(other.title, other.id)
        self.then_run.setCurrentIndex(max(0, self.then_run.findData(task.then_run)))
        self._row = row if has else -1
        self._update_rows()

    def _store(self) -> None:
        task = self._current()
        self._update_rows()
        if task is None:
            return
        task.name = self.name.text().strip()
        task.target = self.target.currentData()
        task.commands = self.commands.toPlainText()
        task.output_file = self.output_file.text().strip()
        task.append = self.append.isChecked()
        task.schedule = self.schedule.currentData()
        task.every_minutes = self.every.value()
        task.daily_at = self.daily_at.time().toString("HH:mm")
        task.runner = self.runner.currentData()
        task.stop_on_error = self.stop_on_error.isChecked()
        task.timeout = self.timeout.value()
        task.enabled = self.enabled.isChecked()
        task.then_run = self.then_run.currentData() or ""
        task.as_script = self.as_script.isChecked()
        task.notify_on_failure = self.notify_on_failure.isChecked()
        self.list.item(self._row).setText(task.title)
        self._update_status()

    def _update_rows(self) -> None:
        schedule = self.schedule.currentData()
        script = self.as_script.isChecked()
        self.commands_hint.setText(
            "The lines run one after another in one shell, so a cd holds for the lines after it. "
            "Lines starting with # are skipped."
            if script
            else "Each line runs on its own, so a cd does not carry over to the next line: join them with &&, "
            "or tick “Run the lines as one script”. Lines starting with # are skipped."
        )
        self.stop_on_error.setText(
            "Stop the script at the first command that fails" if script else "Stop at the first command that fails"
        )
        self.every.setVisible(schedule == SCHEDULE_INTERVAL)
        self.daily_at.setVisible(schedule == SCHEDULE_DAILY)
        self._form.setRowVisible(self.runner, schedule != SCHEDULE_MANUAL)
        self._update_status()

    def _update_status(self) -> None:
        task = self._current()
        running = task is not None and self._scheduler is not None and self._scheduler.is_running(task.id)
        self.run_button.setEnabled(task is not None and not running)
        self.run_button.setText("Running..." if running else "Run Now")
        if task is None:
            self.status.setText("Add a task to run a series of commands now and then, and keep what they print.")
            return
        lines = []
        if task.last_run:
            lines.append(f"Last run {datetime.fromtimestamp(task.last_run):%Y-%m-%d %H:%M}: {task.last_result or 'running'}")
        else:
            lines.append("Not run yet.")
        if task.schedule == SCHEDULE_MANUAL:
            lines.append("Runs only when you press Run Now.")
        elif not task.enabled:
            lines.append("Paused.")
        elif task.runner == RUNNER_WINDOWS:
            lines.append("Windows Task Scheduler runs it once the tasks are saved.")
        else:
            due = next_run(task)
            if due is not None:
                lines.append(f"Next run {datetime.fromtimestamp(max(due, time.time())):%Y-%m-%d %H:%M}, if {APP_NAME} is open.")
        lines.append(f"Output: {output_path(task)}")
        self.status.setText("\n".join(lines))

    def _run_changed(self, *_args) -> None:
        self._mark_results()
        self._update_status()

    def _browse_output(self) -> None:
        task = self._current()
        start = str(output_path(task)) if task else str(OUTPUT_DIR)
        options = QFileDialog.Option.DontConfirmOverwrite  # the file is added to, not replaced, by default
        path, _ = QFileDialog.getSaveFileName(self, "Output File", start, "Text files (*.txt *.log);;All files (*)", options=options)
        if path:
            self.output_file.setText(str(Path(path)))
            self._store()

    def add(self) -> None:
        self._tasks.append(Task(name=f"Task {len(self._tasks) + 1}"))
        self.list.addItem(self._tasks[-1].title)
        self._mark_results()
        self.list.setCurrentRow(len(self._tasks) - 1)
        self.name.setFocus()
        self.name.selectAll()

    def remove(self) -> None:
        row = self.list.currentRow()
        if 0 <= row < len(self._tasks):
            self._row = -1
            del self._tasks[row]
            self.list.takeItem(row)
            self._show(self.list.currentRow())

    def run_now(self) -> None:
        task = self._current()
        if task is None or self._scheduler is None:
            return
        if not task.command_lines():
            self.error.setText("There are no commands to run")
            return
        self.error.setText("")
        self._scheduler.run(task)
        self._update_status()

    def open_output(self) -> None:
        task = self._current()
        if task is None:
            return
        path = output_path(task)
        if not path.exists():
            self.error.setText("There is no output yet: run the task first")
            return
        self.error.setText("")
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

    def _finish(self) -> None:
        for row, task in enumerate(self._tasks):
            problem = ""
            if not task.command_lines():
                problem = "has no commands"
            elif task.runner == RUNNER_WINDOWS and task.schedule == SCHEDULE_MANUAL:
                problem = "is to be run by Windows, which needs a schedule"
            if problem:
                self.list.setCurrentRow(row)
                self.error.setText(f"{task.title} {problem}")
                return
        self.accept()

    def done(self, result: int) -> None:
        if self._scheduler is not None:
            for signal in (self._scheduler.started, self._scheduler.finished):
                try:
                    signal.disconnect(self._run_changed)
                except TypeError:
                    pass
        super().done(result)

    def tasks(self) -> list[Task]:
        return list(self._tasks)


class KeyDialog(QDialog):
    """Create an Ed25519 key pair. `created_path` is the private key file once that is done."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Generate SSH Key Pair")
        self.setMinimumWidth(480)
        self.created_path = ""
        self.public_line = ""

        self.path = QLineEdit(str(keys.default_key_path()))
        browse = QPushButton("Browse")
        browse.clicked.connect(self._browse)
        self.comment = QLineEdit(keys.default_comment())
        self.passphrase = QLineEdit()
        self.passphrase.setEchoMode(QLineEdit.EchoMode.Password)
        self.passphrase.setPlaceholderText("Optional, but recommended")
        self.confirm = QLineEdit()
        self.confirm.setEchoMode(QLineEdit.EchoMode.Password)
        form = _form()
        form.addRow("Save as", _row(self.path, browse))
        form.addRow("Comment", self.comment)
        form.addRow("Passphrase", self.passphrase)
        form.addRow("Repeat", self.confirm)
        _form_labels(form)

        self.message = QLabel(
            "Creates an Ed25519 key. The private key stays on this computer; the .pub file next to it "
            "is what goes on servers."
        )
        self.message.setObjectName("Muted")
        self.message.setWordWrap(True)
        self.public = QLineEdit()
        self.public.setReadOnly(True)
        self.public.setPlaceholderText("The public key appears here")
        self.copy_button = QPushButton("Copy")
        self.copy_button.setEnabled(False)
        self.copy_button.clicked.connect(lambda: QGuiApplication.clipboard().setText(self.public_line))

        self.close_button = QPushButton("Cancel")
        self.close_button.clicked.connect(self.reject)
        self.generate_button = QPushButton("Generate")
        self.generate_button.setObjectName("Primary")
        self.generate_button.setDefault(True)
        self.generate_button.clicked.connect(self.generate)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        buttons.addWidget(self.close_button)
        buttons.addWidget(self.generate_button)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 20, 22, 18)
        layout.setSpacing(12)
        layout.addLayout(form)
        layout.addWidget(self.message)
        layout.addWidget(_row(self.public, self.copy_button))
        layout.addLayout(buttons)

    def _browse(self) -> None:
        chosen, _ = QFileDialog.getSaveFileName(self, "Save Private Key As", self.path.text())
        if chosen:
            self.path.setText(chosen)

    def _fail(self, text: str) -> None:
        self.message.setStyleSheet(f"color: {COLORS['red']};")
        self.message.setText(text)

    def generate(self) -> None:
        if self.created_path:
            return self.accept()  # the button reads "Done" by now
        if self.passphrase.text() != self.confirm.text():
            return self._fail("The two passphrases are not the same.")
        if not self.path.text().strip():
            return self._fail("Choose where to save the key.")
        try:
            self.public_line = keys.generate_keypair(
                Path(self.path.text().strip()), self.passphrase.text(), self.comment.text()
            )
        except keys.KeyError_ as exc:
            return self._fail(str(exc))
        self.created_path = self.path.text().strip()
        self.public.setText(self.public_line)
        self.copy_button.setEnabled(True)
        self.message.setStyleSheet("")
        self.message.setText(
            f"Created {self.created_path} and its .pub file. To log in with it, install the public key on a "
            "server: open a tab to that server and choose Install Public Key on Server from the tab menu."
        )
        self.generate_button.setText("Done")
        self.close_button.hide()


class ShortcutsDialog(QDialog):
    """Every keyboard shortcut, grouped, with a search field. `sections` is [(title, [(keys, what)])]."""

    def __init__(self, sections: list[tuple[str, list[tuple[str, str]]]], parent=None):
        super().__init__(parent)
        self.setWindowTitle("Keyboard Shortcuts")
        self.setMinimumSize(420, 360)
        fit_to_screen(self, 560, 620)
        self._rows: list[tuple[QWidget, str, QLabel]] = []  # row, what it is searched by, its section title

        self.search = QLineEdit()
        self.search.setPlaceholderText("Search shortcuts")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self._filter)

        content = QWidget()
        column = QVBoxLayout(content)
        column.setContentsMargins(22, 6, 22, 16)
        column.setSpacing(4)
        for title, entries in sections:
            heading = _section(title.upper())
            column.addSpacing(10)
            column.addWidget(heading)
            for combo, what in entries:
                row = QWidget()
                line = QHBoxLayout(row)
                line.setContentsMargins(0, 3, 0, 3)
                line.setSpacing(6)
                line.addWidget(QLabel(what), 1)
                for alternative in combo.split(" / "):
                    cap = QLabel(alternative)
                    cap.setObjectName("KeyCap")
                    line.addWidget(cap)
                column.addWidget(row)
                self._rows.append((row, f"{what} {combo}".lower(), heading))
        column.addStretch(1)
        self.empty = QLabel("No shortcut matches.")
        self.empty.setObjectName("Muted")
        self.empty.hide()
        column.insertWidget(0, self.empty)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setWidget(content)

        close = QPushButton("Close")
        close.setObjectName("Primary")
        close.setDefault(True)
        close.clicked.connect(self.accept)
        footer = QFrame()
        footer.setObjectName("SettingsFooter")
        buttons = QHBoxLayout(footer)
        buttons.setContentsMargins(22, 12, 22, 14)
        hint = QLabel("Ctrl+Shift+P finds any command by name.")
        hint.setObjectName("Muted")
        buttons.addWidget(hint, 1)
        buttons.addWidget(close)

        top = QVBoxLayout()
        top.setContentsMargins(22, 18, 22, 0)
        top.addWidget(self.search)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addLayout(top)
        layout.addWidget(scroll, 1)
        layout.addWidget(footer)
        self.search.setFocus()

    def _filter(self, text: str) -> None:
        words = text.lower().split()
        visible_headings = set()
        for row, haystack, heading in self._rows:
            show = all(word in haystack for word in words)
            row.setVisible(show)
            if show:
                visible_headings.add(heading)
        for _row, _haystack, heading in self._rows:
            heading.setVisible(heading in visible_headings)
        self.empty.setVisible(not visible_headings)

    def visible_entries(self) -> list[str]:
        """The descriptions of the rows the search leaves showing."""
        return [row.findChild(QLabel).text() for row, _haystack, _heading in self._rows if not row.isHidden()]


class TunnelsDialog(QDialog):
    """The tunnels of one connected SSH tab: see them, stop them and start more, without reconnecting."""

    keepRequested = pyqtSignal(str)  # tunnels, as typed, to add to the saved session as well

    def __init__(self, tab, can_keep: bool = True, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"Tunnels - {tab.title}")
        self._tab = tab
        heading = _section("RUNNING")
        self.list = QListWidget()
        self.list.setObjectName("SnippetList")
        self.list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.list.itemSelectionChanged.connect(self._update_buttons)
        self.empty = QLabel("No tunnels are running on this connection.")
        self.empty.setObjectName("Muted")
        self.stop_button = QPushButton("Stop")
        self.stop_button.setToolTip("Stop the selected tunnels; connections already made through them carry on")
        self.stop_button.clicked.connect(self.stop_selected)

        self.entry = QLineEdit()
        self.entry.setPlaceholderText("8080:localhost:80, R:9090:localhost:3000, D:1080")
        self.entry.setToolTip(
            "Written as in the session's Tunnels field, separated by commas:\n"
            "  8080:host:80        local port 8080 reaches host:80 through the server\n"
            "  R:9090:host:3000    port 9090 on the server reaches host:3000 on this side\n"
            "  D:1080              a SOCKS proxy on local port 1080"
        )
        self.entry.returnPressed.connect(self.start_entered)
        start = QPushButton("Start")
        start.setObjectName("Primary")
        start.clicked.connect(self.start_entered)
        self.keep = QCheckBox("Also keep them in the saved session, for the next connection")
        self.keep.setVisible(can_keep)  # a tab opened from quick connect has no saved session
        self.message = QLabel()
        self.message.setWordWrap(True)
        close = QPushButton("Close")
        close.clicked.connect(self.accept)

        running_row = QHBoxLayout()
        running_row.addWidget(heading, 1)
        running_row.addWidget(self.stop_button)
        add_row = QHBoxLayout()
        add_row.addWidget(self.entry, 1)
        add_row.addWidget(start)
        footer = QHBoxLayout()
        footer.addWidget(self.message, 1)
        footer.addWidget(close)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 18, 22, 16)
        layout.setSpacing(10)
        layout.addLayout(running_row)
        layout.addWidget(self.list, 1)
        layout.addWidget(self.empty)
        layout.addSpacing(6)
        layout.addWidget(_section("START MORE"))
        layout.addLayout(add_row)
        layout.addWidget(self.keep)
        layout.addLayout(footer)
        fit_to_screen(self, 620, 420)
        self.refresh()

    def refresh(self) -> None:
        self.list.clear()
        for description in self._tab.running_tunnels():
            self.list.addItem(description)
        self.empty.setVisible(self.list.count() == 0)
        self._update_buttons()

    def _update_buttons(self) -> None:
        self.stop_button.setEnabled(bool(self.list.selectedItems()))

    def _say(self, text: str, good: bool) -> None:
        self.message.setStyleSheet(f"color: {COLORS['green' if good else 'red']};")
        self.message.setText(text)

    def start_entered(self) -> list[str]:
        """Start what is typed; returns the descriptions of the tunnels that started."""
        text = self.entry.text().strip()
        if not text:
            return []
        started, problems = self._tab.add_tunnels(text)
        if problems:
            self._say("\n".join(problems), good=False)
        else:
            self._say(f"Started {len(started)} tunnel{'' if len(started) == 1 else 's'}", good=True)
            if self.keep.isChecked() and self.keep.isVisible():
                self.keepRequested.emit(text)
            self.entry.clear()
        self.refresh()
        return started

    def stop_selected(self) -> int:
        chosen = [item.text() for item in self.list.selectedItems()]
        stopped = sum(1 for description in chosen if self._tab.stop_tunnel(description))
        if stopped:
            self._say(f"Stopped {stopped} tunnel{'' if stopped == 1 else 's'}", good=True)
        self.refresh()
        return stopped


class ConnectionInfoDialog(QDialog):
    """What is known about one tab's connection, to read or copy. `rows` is [(label, value)]."""

    def __init__(self, rows: list[tuple[str, str]], parent=None):
        super().__init__(parent)
        self.setWindowTitle("Connection Info")
        self.setMinimumWidth(420)
        self._rows = rows

        form = _form()
        for label, value in rows:
            shown = QLabel(value)
            shown.setTextFormat(Qt.TextFormat.PlainText)  # some of it is what the server says about itself
            shown.setWordWrap(True)
            shown.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            form.addRow(label, shown)
        for label in _form_labels(form):
            label.setObjectName("Muted")

        copy = QPushButton("Copy")
        copy.setToolTip("Copy all of this as text, for a ticket or a message")
        copy.clicked.connect(lambda: QGuiApplication.clipboard().setText(self.as_text()))
        close = QPushButton("Close")
        close.setObjectName("Primary")
        close.setDefault(True)
        close.clicked.connect(self.accept)
        buttons = QHBoxLayout()
        buttons.addWidget(copy)
        buttons.addStretch(1)
        buttons.addWidget(close)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 20, 22, 18)
        layout.setSpacing(16)
        layout.addLayout(form)
        layout.addLayout(buttons)
        fit_to_screen(self, 560, layout.sizeHint().height())

    def as_text(self) -> str:
        width = max(len(label) for label, _value in self._rows)
        lines = []
        for label, value in self._rows:
            continuation = "\n" + " " * (width + 2)
            lines.append(f"{label.ljust(width)}  {value.replace(chr(10), continuation)}")
        return "\n".join(lines) + "\n"
