"""User preferences, persisted with QSettings."""

import os
from dataclasses import dataclass, fields
from pathlib import Path

from PyQt6.QtCore import QSettings

from . import ORG_NAME, SETTINGS_NAME
from .paths import CONFIG_DIR, portable_folder
from .theme import (
    DEFAULT_TERMINAL_FONT,
    DEFAULT_TERMINAL_SIZE,
    DEFAULT_UI_FONT,
    DEFAULT_UI_SIZE,
    TERMINAL_SCHEMES,
    THEME_DARK,
    THEME_LIGHT,
    THEME_SYSTEM,
)

CURSOR_BLOCK = "block"
CURSOR_BAR = "bar"
CURSOR_UNDERLINE = "underline"

MIN_SCROLLBACK, MAX_SCROLLBACK = 100, 200_000
MAX_PASTE_DELAY_MS = 5000
# Each transfer running at the same time has a channel of its own on the connection, and servers
# limit those (OpenSSH allows ten sessions per connection, the shell and file list among them).
MAX_TRANSFERS = 8

# Set this to a file path to keep settings in that file instead of the user's own
# settings. The tests use it so that they never touch real preferences.
SETTINGS_FILE_VARIABLE = "TECHX_SETTINGS_FILE"


def open_settings() -> QSettings:
    """The store for preferences and window layout."""
    override = os.environ.get(SETTINGS_FILE_VARIABLE)
    if override:
        return QSettings(override, QSettings.Format.IniFormat)
    portable = portable_folder()
    if portable:
        return QSettings(str(portable / "settings.ini"), QSettings.Format.IniFormat)  # a portable copy leaves the registry alone
    # Note: this constructor always uses the native store (the registry on Windows),
    # whatever QSettings.setDefaultFormat() says.
    return QSettings(ORG_NAME, SETTINGS_NAME)


# Settings keys that differ from the field name (kept from earlier versions).
_KEYS = {"terminal_size": "font_size", "ui_size": "ui_font_size"}


@dataclass
class Preferences:
    # Appearance
    theme: str = THEME_DARK  # "dark", "light" or "system"
    accent: str = ""  # "#rrggbb", empty for the theme's own accent
    ui_font: str = DEFAULT_UI_FONT
    ui_size: int = DEFAULT_UI_SIZE
    # Terminal
    terminal_font: str = DEFAULT_TERMINAL_FONT
    terminal_size: int = DEFAULT_TERMINAL_SIZE
    terminal_scheme: str = ""  # a key of theme.TERMINAL_SCHEMES, empty to follow the theme
    cursor_style: str = CURSOR_BLOCK
    cursor_blink: bool = True
    scrollback: int = 10_000
    copy_on_select: bool = True
    confirm_paste: bool = True  # ask before pasting more than one line
    paste_line_delay_ms: int = 0  # pause after each pasted line, for slow devices; 0 for none
    right_click_paste: bool = False  # right-click pastes; Ctrl+right-click opens the menu
    visual_bell: bool = True  # flash the terminal when the remote side rings the bell
    highlight_keywords: bool = False  # colour words such as error, warning, success
    highlight_words: str = ""  # extra words to colour, separated by commas
    # Behaviour
    confirm_quit: bool = True
    confirm_close_tab: bool = False
    show_send_bar: bool = True
    tab_list_filter_at: int = 8  # more open tabs than this: the list of them has a filter box
    compact_pinned: bool = False  # a pinned tab shows its icon only; its name is in the tooltip
    stack_panes: bool = False  # split panes one above the other instead of side by side
    pane_grid: bool = False  # three or four panes in two columns: side by side and stacked at once
    auto_reconnect: bool = False  # retry when a connection drops unexpectedly
    server_monitor: bool = True  # show the load, memory and disk of the active SSH tab's server
    server_alerts: bool = True  # tell when a connected server's figures turn critical, in any tab
    keepalive_seconds: int = 30  # send a keep-alive this often on idle connections, 0 for never
    restore_tabs: bool = False  # reopen the saved sessions that were open when the app closed
    auto_log: bool = False  # write every session's output to a file
    log_timestamps: bool = False  # start every logged line with the time
    log_dir: str = ""  # empty for the default, see log_directory()
    # File transfer
    transfer_limit_kbps: int = 0  # speed limit in KB/s for all transfers together, 0 for unlimited
    transfer_parallel: int = 3  # transfers that run at the same time
    transfer_confirm_replace: bool = True  # ask before a transfer replaces a file that is already there
    transfer_keep_dates: bool = True  # the copy keeps the original's modification date
    transfer_resume: bool = True  # carry on from a partly transferred file instead of starting over

    def sanitize(self) -> None:
        """Bring values that make no sense (a hand-edited or damaged settings file) back into range."""
        defaults = Preferences()
        self.scrollback = max(MIN_SCROLLBACK, min(MAX_SCROLLBACK, self.scrollback))
        self.terminal_size = max(6, min(32, self.terminal_size))
        self.ui_size = max(8, min(16, self.ui_size))
        self.keepalive_seconds = max(0, min(3600, self.keepalive_seconds))
        self.tab_list_filter_at = max(1, min(99, self.tab_list_filter_at))
        self.transfer_limit_kbps = max(0, self.transfer_limit_kbps)
        self.transfer_parallel = max(1, min(MAX_TRANSFERS, self.transfer_parallel))
        self.paste_line_delay_ms = max(0, min(MAX_PASTE_DELAY_MS, self.paste_line_delay_ms))
        if self.theme not in (THEME_DARK, THEME_LIGHT, THEME_SYSTEM):
            self.theme = defaults.theme
        if self.cursor_style not in (CURSOR_BLOCK, CURSOR_BAR, CURSOR_UNDERLINE):
            self.cursor_style = defaults.cursor_style
        if self.terminal_scheme and self.terminal_scheme not in TERMINAL_SCHEMES:
            self.terminal_scheme = ""

    def log_directory(self) -> Path:
        return Path(self.log_dir) if self.log_dir else CONFIG_DIR / "logs"

    @staticmethod
    def _settings() -> QSettings:
        return open_settings()

    @classmethod
    def load(cls) -> "Preferences":
        settings = cls._settings()
        prefs = cls()
        for field in fields(cls):
            raw = settings.value(_KEYS.get(field.name, field.name))
            if raw is None:
                continue
            default = getattr(prefs, field.name)
            try:
                if isinstance(default, bool):
                    # QSettings hands booleans back as "true"/"false" text on some backends.
                    value = raw if isinstance(raw, bool) else str(raw).lower() in ("true", "1")
                elif isinstance(default, int):
                    value = int(raw)
                else:
                    value = str(raw)
            except (TypeError, ValueError):
                continue
            setattr(prefs, field.name, value)
        prefs.sanitize()
        return prefs

    def save(self) -> None:
        settings = self._settings()
        for field in fields(self):
            settings.setValue(_KEYS.get(field.name, field.name), getattr(self, field.name))
