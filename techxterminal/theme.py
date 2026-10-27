"""Colors, stylesheet, fonts and small painted icons."""

import math
import re
import tempfile
from pathlib import Path

from PyQt6.QtCore import QPointF, QRectF, Qt
from PyQt6.QtGui import (
    QColor,
    QFont,
    QFontDatabase,
    QIcon,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPalette,
    QPen,
    QPixmap,
    QPolygonF,
)
from PyQt6.QtWidgets import QApplication, QWidget

FONT_DIR = Path(__file__).parent / "fonts"

THEME_DARK = "dark"
THEME_LIGHT = "light"
THEME_SYSTEM = "system"

_PALETTES = {
    THEME_DARK: {
        "bg": "#0b0d12",
        "surface": "#10131a",
        "surface2": "#171b26",
        "hover": "#1c2130",
        "select": "#232c45",
        "border": "#1f2533",
        "border2": "#2e3649",
        "text": "#c9d1e4",
        "muted": "#6b7590",
        "accent": "#7aa2f7",
        "accent_hi": "#9bb8fb",
        "on_accent": "#0b0d12",
        "violet": "#bb9af7",
        "green": "#9ece6a",
        "amber": "#e0af68",
        "red": "#f7768e",
        "banner": "#24171f",
        "broadcast": "#241f14",
        "term_bg": "#0d1017",
        "term_fg": "#c0caf5",
        "term_sel": "#2a3a66",
    },
    THEME_LIGHT: {
        "bg": "#eceef4",
        "surface": "#f5f6fa",
        "surface2": "#ffffff",
        "hover": "#e3e7f0",
        "select": "#d3ddf6",
        "border": "#d8dce7",
        "border2": "#b9c0d2",
        "text": "#1f2433",
        "muted": "#687089",
        "accent": "#2f5fd0",
        "accent_hi": "#4674e0",
        "on_accent": "#ffffff",
        "violet": "#7b4fd6",
        "green": "#3a8a27",
        "amber": "#a8720c",
        "red": "#cf3350",
        "banner": "#fde7eb",
        "broadcast": "#fdf3df",
        "term_bg": "#fbfcfe",
        "term_fg": "#262d45",
        "term_sel": "#c8d5f7",
    },
}

# Names are the ones pyte uses for the 16 base colors.
_ANSI_PALETTES = {
    THEME_DARK: {
        "black": "#15161e",
        "red": "#f7768e",
        "green": "#9ece6a",
        "brown": "#e0af68",
        "blue": "#7aa2f7",
        "magenta": "#bb9af7",
        "cyan": "#7dcfff",
        "white": "#a9b1d6",
        "brightblack": "#4b5273",
        "brightred": "#ff899d",
        "brightgreen": "#b5e07f",
        "brightbrown": "#f5c57a",
        "brightblue": "#9bb8fb",
        "brightmagenta": "#cdb4fa",
        "brightcyan": "#a4daff",
        "brightwhite": "#e6ebff",
    },
    # On a light background "white" has to stay readable, so it is a mid grey here.
    THEME_LIGHT: {
        "black": "#1b1f2e",
        "red": "#c5283d",
        "green": "#2f7a22",
        "brown": "#94600c",
        "blue": "#2a55c4",
        "magenta": "#7443c0",
        "cyan": "#0c7786",
        "white": "#7d849b",
        "brightblack": "#5a6079",
        "brightred": "#e0405a",
        "brightgreen": "#3f9530",
        "brightbrown": "#b3791a",
        "brightblue": "#4674e0",
        "brightmagenta": "#9463e0",
        "brightcyan": "#1795a6",
        "brightwhite": "#454b62",
    },
}

# The active palette. Everything reads colours from these two dicts at paint time;
# set_theme() refills them in place so that existing imports stay valid.
COLORS = dict(_PALETTES[THEME_DARK])
ANSI = dict(_ANSI_PALETTES[THEME_DARK])
_active_theme = THEME_DARK


def resolve_theme(theme: str) -> str:
    """Turn "system" into the concrete theme the operating system is using."""
    if theme in _PALETTES:
        return theme
    app = QApplication.instance()
    scheme = app.styleHints().colorScheme() if app else Qt.ColorScheme.Unknown
    return THEME_LIGHT if scheme == Qt.ColorScheme.Light else THEME_DARK


def active_theme() -> str:
    return _active_theme


def palette_for(theme: str, accent: str = "") -> dict:
    """The colours of a concrete theme, with an optional accent override."""
    colors = dict(_PALETTES[theme])
    custom = QColor(accent)
    if accent and custom.isValid():
        colors["accent"] = custom.name()
        colors["accent_hi"] = custom.lighter(118).name()
        # Text on the accent must stay readable whichever colour was picked.
        colors["on_accent"] = "#0b0d12" if custom.lightnessF() > 0.55 else "#ffffff"
    return colors


_ANSI_NAMES = [
    "black", "red", "green", "brown", "blue", "magenta", "cyan", "white",
    "brightblack", "brightred", "brightgreen", "brightbrown", "brightblue", "brightmagenta", "brightcyan", "brightwhite",
]  # fmt: skip

# Terminal colour schemes that can replace the theme's own terminal colours:
# name -> (label, background, text, selection, the sixteen colours in the order of _ANSI_NAMES).
TERMINAL_SCHEMES = {
    "solarized-dark": ("Solarized Dark", "#002b36", "#93a1a1", "#0a4a5a", [
        "#073642", "#dc322f", "#859900", "#b58900", "#268bd2", "#d33682", "#2aa198", "#eee8d5",
        "#586e75", "#cb4b16", "#93a1a1", "#839496", "#6c9bd2", "#6c71c4", "#5fb3ad", "#fdf6e3"]),
    "solarized-light": ("Solarized Light", "#fdf6e3", "#586e75", "#e4dcc3", [
        "#073642", "#dc322f", "#859900", "#b58900", "#268bd2", "#d33682", "#2aa198", "#93a1a1",
        "#657b83", "#cb4b16", "#586e75", "#657b83", "#268bd2", "#6c71c4", "#2aa198", "#073642"]),
    "dracula": ("Dracula", "#282a36", "#f8f8f2", "#44475a", [
        "#21222c", "#ff5555", "#50fa7b", "#f1fa8c", "#bd93f9", "#ff79c6", "#8be9fd", "#f8f8f2",
        "#6272a4", "#ff6e6e", "#69ff94", "#ffffa5", "#d6acff", "#ff92df", "#a4ffff", "#ffffff"]),
    "gruvbox-dark": ("Gruvbox Dark", "#282828", "#ebdbb2", "#504945", [
        "#282828", "#cc241d", "#98971a", "#d79921", "#458588", "#b16286", "#689d6a", "#a89984",
        "#928374", "#fb4934", "#b8bb26", "#fabd2f", "#83a598", "#d3869b", "#8ec07c", "#ebdbb2"]),
    "nord": ("Nord", "#2e3440", "#d8dee9", "#434c5e", [
        "#3b4252", "#bf616a", "#a3be8c", "#ebcb8b", "#81a1c1", "#b48ead", "#88c0d0", "#e5e9f0",
        "#4c566a", "#bf616a", "#a3be8c", "#ebcb8b", "#81a1c1", "#b48ead", "#8fbcbb", "#eceff4"]),
    "paper": ("Paper (black on white)", "#ffffff", "#1a1a1a", "#cfd8ea", [
        "#1a1a1a", "#b3261e", "#1b6e2d", "#8a5a00", "#1d4fb8", "#7a2e9e", "#0b6b78", "#6f6f6f",
        "#4d4d4d", "#d0392f", "#2f8f43", "#a87000", "#3667d6", "#9645bd", "#16899a", "#2b2b2b"]),
}  # fmt: skip


def terminal_colors(theme: str, scheme: str = "") -> tuple[str, str, str, dict]:
    """(background, text, selection, the sixteen named colours) for a concrete theme and scheme."""
    if scheme in TERMINAL_SCHEMES:
        _label, background, text, selection, sixteen = TERMINAL_SCHEMES[scheme]
        return background, text, selection, dict(zip(_ANSI_NAMES, sixteen))
    palette = _PALETTES[theme]
    return palette["term_bg"], palette["term_fg"], palette["term_sel"], dict(_ANSI_PALETTES[theme])


def set_theme(theme: str, accent: str = "", scheme: str = "") -> str:
    """Make a theme the active one; returns the concrete theme that was applied.

    A terminal colour scheme, if given, replaces the theme's own terminal colours.
    """
    global _active_theme
    _active_theme = resolve_theme(theme)
    COLORS.clear()
    COLORS.update(palette_for(_active_theme, accent))
    background, text, selection, sixteen = terminal_colors(_active_theme, scheme)
    COLORS.update(term_bg=background, term_fg=text, term_sel=selection)
    ANSI.clear()
    ANSI.update(sixteen)
    return _active_theme


# Label colours offered for sessions.
SESSION_COLORS = [
    ("Red", "#f7768e"),
    ("Orange", "#ff9e64"),
    ("Yellow", "#e0af68"),
    ("Green", "#9ece6a"),
    ("Teal", "#2ac3de"),
    ("Blue", "#7aa2f7"),
    ("Violet", "#bb9af7"),
    ("Pink", "#f5a9d0"),
]

UI_FONTS = ["Ubuntu", "Segoe UI", "Helvetica Neue", "DejaVu Sans"]
TERMINAL_FONTS = [
    "Ubuntu Mono",
    "Cascadia Mono",
    "Cascadia Code",
    "JetBrains Mono",
    "Consolas",
    "SF Mono",
    "Menlo",
    "DejaVu Sans Mono",
    "Courier New",
]
DEFAULT_UI_FONT = UI_FONTS[0]
DEFAULT_UI_SIZE = 10
DEFAULT_TERMINAL_FONT = TERMINAL_FONTS[0]
DEFAULT_TERMINAL_SIZE = 12  # Ubuntu Mono runs small for its point size

_QSS = """
* { outline: 0; }
QMainWindow, QDialog, QMessageBox, QInputDialog { background: @bg; }
QWidget { color: @text; font-family: "@font"; font-size: @fs_basept; }
QLabel { background: transparent; }

#TopBar { background: @surface; border-bottom: 1px solid @border; }
#Brand { font-size: @fs_largept; font-weight: 600; }
#Caption { color: @muted; font-size: @fs_smallpt; letter-spacing: 0.3px; }
/* The top bar's controls are pills of one height: 34px, or 28px in a short window. */
#TopTools { background: @bg; border: 1px solid @border; border-radius: 17px; }
#TopTools QToolButton#Icon { border-radius: 14px; padding: 0; }
#TopBar QPushButton#Primary { border-radius: 17px; padding: 0 16px 0 13px; }
#TopBar[dense="true"] #TopTools { border-radius: 14px; }
#TopBar[dense="true"] #TopTools QToolButton#Icon { border-radius: 11px; }
#TopBar[dense="true"] QPushButton#Primary { border-radius: 14px; }
/* Like #KeyCap in the shortcut list, a key seen from above, but quieter: it is only a hint. */
#KeyHint {
    color: @muted; background: @surface2; border: 1px solid @border2; border-bottom-width: 2px;
    border-radius: 5px; padding: 0 7px; font-size: @fs_smallpt;
}
#Muted { color: @muted; }
#SectionTitle { color: @muted; font-size: @fs_smallpt; font-weight: 700; letter-spacing: 1.5px; }

QLineEdit, QAbstractSpinBox, QComboBox {
    background: @surface2; border: 1px solid @border; border-radius: 8px;
    padding: 7px 10px; selection-background-color: @accent; selection-color: @on_accent;
}
QLineEdit:hover, QAbstractSpinBox:hover, QComboBox:hover { border-color: @border2; }
QLineEdit:focus, QAbstractSpinBox:focus, QComboBox:focus { border-color: @accent; }
QLineEdit:disabled, QComboBox:disabled { color: @muted; background: @surface; }
#QuickConnect { border-radius: 17px; padding: 0 14px 0 8px; background: @bg; }
#QuickConnect:hover { border-color: @border2; }
#QuickConnect:focus { border-color: @accent; background: @surface2; }
#SessionSearch { padding-left: 8px; }
QComboBox::drop-down { border: none; width: 28px; }
QComboBox::down-arrow { image: url("@arrow_down"); width: 12px; height: 12px; }
QComboBox QAbstractItemView {
    background: @surface2; border: 1px solid @border2; padding: 4px;
    selection-background-color: @select; selection-color: @text;
}
QAbstractSpinBox { padding-right: 26px; }
QAbstractSpinBox::up-button, QAbstractSpinBox::down-button {
    subcontrol-origin: border; width: 24px; border: none; background: transparent;
}
QAbstractSpinBox::up-button { subcontrol-position: top right; margin-top: 3px; }
QAbstractSpinBox::down-button { subcontrol-position: bottom right; margin-bottom: 3px; }
QAbstractSpinBox::up-arrow { image: url("@arrow_up"); width: 10px; height: 10px; }
QAbstractSpinBox::down-arrow { image: url("@arrow_down"); width: 10px; height: 10px; }

QPushButton {
    background: @surface2; border: 1px solid @border; border-radius: 8px; padding: 7px 14px;
}
QPushButton:hover { background: @hover; border-color: @border2; }
QPushButton:pressed { background: @select; }
/* The 2px border is there all the time, so the button keeps its size when it shows focus. */
QPushButton#Primary {
    background: @accent; color: @on_accent; border: 2px solid @accent; padding: 5px 12px; font-weight: 600;
}
QPushButton#Primary:hover { background: @accent_hi; border-color: @accent_hi; }
QPushButton#Primary:pressed { background: @accent; border-color: @accent; }
/* Floats over the terminal while its output is scrolled back. */
QPushButton#JumpToLatest {
    background: @accent; color: @on_accent; border: none; border-radius: 14px; padding: 6px 16px; font-weight: 600;
}
QPushButton#JumpToLatest:hover { background: @accent_hi; }
QPushButton#JumpToLatest:pressed { background: @accent; }
QToolButton#Icon { background: transparent; border: 1px solid transparent; border-radius: 8px; padding: 5px; }
QToolButton#Icon:hover { background: @hover; }
QToolButton#Icon:pressed { background: @select; }
/* Keyboard focus is always visible: the outline Qt draws is switched off above. */
QPushButton:focus, QToolButton#Icon:focus { border-color: @accent; }
QPushButton#Primary:focus { border-color: @text; }

QCheckBox { spacing: 8px; background: transparent; }
QCheckBox::indicator {
    width: 14px; height: 14px; border: 1px solid @border2; border-radius: 4px; background: @surface2;
}
QCheckBox::indicator:checked { background: @accent; border-color: @accent; image: url("@check"); }
QCheckBox::indicator:focus { border-color: @accent; }
QCheckBox::indicator:checked:focus { border-color: @text; }
/* Boxes ticked inside a list (the folder comparison) look like the others. */
QTreeView::indicator {
    width: 14px; height: 14px; border: 1px solid @border2; border-radius: 4px; background: @surface2;
}
QTreeView::indicator:checked { background: @accent; border-color: @accent; image: url("@check"); }
#CompareList::item { padding: 5px 0; }
QCheckBox::indicator:disabled { background: @surface; border-color: @border; }
QCheckBox::indicator:checked:disabled { image: url("@check_disabled"); }
QCheckBox::indicator:checked:disabled { background: @border2; border-color: @border2; }
QCheckBox:disabled, QLabel:disabled, QRadioButton:disabled { color: @muted; }
QRadioButton { spacing: 7px; background: transparent; }
QRadioButton::indicator {
    width: 14px; height: 14px; border: 1px solid @border2; border-radius: 8px; background: @surface2;
}
QRadioButton::indicator:hover, QRadioButton::indicator:focus { border-color: @accent; }
QRadioButton::indicator:checked {
    width: 6px; height: 6px; border: 5px solid @accent; border-radius: 8px; background: @bg;
}

#Sidebar { background: @surface; border-right: 1px solid @border; }
#SessionRail { background: @surface; border-right: 1px solid @border; }
#RailScroll, #RailTiles { background: transparent; }
QToolButton#RailSession { background: transparent; border: 1px solid transparent; border-radius: 12px; padding: 0; }
QToolButton#RailSession:hover { background: @hover; }
QToolButton#RailSession:pressed { background: @select; }
QToolButton#RailSession:focus { border-color: @accent; }
QTreeWidget { background: transparent; border: none; }
QTreeWidget::branch { background: transparent; }

QTabWidget::pane { border: none; }
QTabBar { background: @bg; qproperty-drawBase: 0; }
/* Every tab has the same borders, transparent but on the current one, so that none changes size as it is chosen. */
QTabBar::tab {
    background: transparent; color: @muted; padding: 8px 8px 8px 12px; margin: 6px 1px 0 1px;
    border: 1px solid transparent; border-bottom: none; border-top: 2px solid transparent;
    border-top-left-radius: 9px; border-top-right-radius: 9px;
    min-width: 96px; max-width: 220px;
}
QTabBar::tab:selected {
    background: @term_bg; color: @text; border-color: @border; border-top: 2px solid @accent;
}
QTabBar::tab:hover:!selected { background: @hover; color: @text; }
/* With several panes, the accent marks only the active one. */
QTabBar[inactive="true"]::tab:selected { border-top-color: @border2; }
QTabBar QToolButton { background: @surface2; border: none; }
#TabClose {
    border: none; background: transparent; border-radius: 9px; padding: 0;
    margin-top: 6px; /* the tabs' own top margin, so the button centres on the label */
}
#TabClose:hover { background: @red; }
#TabCorner { background: @bg; }
/* Short windows: thinner tabs and quick connect, so the terminal keeps its rows. */
QTabBar[dense="true"]::tab { padding: 5px 8px 5px 12px; margin-top: 3px; }
QTabBar[dense="true"] #TabClose { margin-top: 3px; }
#TopBar[dense="true"] #QuickConnect { border-radius: 14px; }

QScrollBar:vertical { background: transparent; width: 10px; margin: 0; }
QScrollBar::handle:vertical { background: @border2; border-radius: 5px; min-height: 30px; }
QScrollBar::handle:vertical:hover { background: @muted; }
QScrollBar:horizontal { background: transparent; height: 10px; margin: 0; }
QScrollBar::handle:horizontal { background: @border2; border-radius: 5px; min-width: 30px; }
QScrollBar::add-line, QScrollBar::sub-line { width: 0; height: 0; }
QScrollBar::add-page, QScrollBar::sub-page { background: transparent; }

QSplitter::handle { background: @border; }

QMenu { background: @surface2; border: 1px solid @border2; padding: 5px; }
QMenu::item { padding: 6px 24px 6px 12px; border-radius: 5px; }
QMenu::item:selected { background: @select; }
QMenu::item:disabled { color: @muted; }
QMenu::separator { height: 1px; background: @border2; margin: 5px 6px; }

QStatusBar { background: @bg; color: @muted; border-top: 1px solid @border; }
QStatusBar::item { border: none; }
QStatusBar QLabel { color: @muted; padding: 0 8px; font-size: @fs_smallpt; }
QStatusBar[level="success"] { color: @green; }
QStatusBar[level="error"] { color: @red; }
#NoticeAction {
    background: transparent; color: @accent; border: 1px solid @border2; border-radius: 6px;
    padding: 1px 10px; margin: 2px 6px; font-size: @fs_smallpt; font-weight: 600;
}
#NoticeAction:hover { background: @hover; border-color: @accent; }
QLineEdit[nomatch="true"], QLineEdit[nomatch="true"]:focus { border-color: @red; }
QToolTip { background: @surface2; color: @text; border: 1px solid @border2; padding: 4px 6px; }

#Banner { background: @banner; border-bottom: 1px solid @red; }
#CommandBar { background: @surface; border-top: 1px solid @border; }
#CommandBar[broadcast="true"] { background: @broadcast; border-top: 1px solid @amber; }
#CommandBar[broadcast="true"] QCheckBox { color: @amber; font-weight: 600; }
#Welcome { background: @term_bg; }
#WelcomeTitle { font-size: @fs_titlept; font-weight: 700; }
#WelcomeHint { color: @muted; }
#SettingsNav { background: @surface; border-right: 1px solid @border; }
#SettingsNav QPushButton {
    text-align: left; padding: 9px 14px; border: none; border-radius: 8px;
    background: transparent; color: @muted;
}
#SettingsNav QPushButton:hover { background: @hover; color: @text; }
#SettingsNav QPushButton:focus { background: @hover; color: @text; }
#SettingsNav QPushButton:checked { background: @select; color: @text; font-weight: 600; }
#SettingsTitle { font-size: @fs_largept; font-weight: 700; }
#SettingsFooter { border-top: 1px solid @border; }
#TransferStrip { background: @surface; border-top: 1px solid @border; }
#ThemeCard { background: transparent; border: 2px solid @border; border-radius: 10px; padding: 6px; }
#ThemeCard:hover { border-color: @border2; }
#ThemeCard:checked { border-color: @accent; }
#ThemeCard:focus { border-color: @text; }
#Segment { border-radius: 0; border-right: none; padding: 7px 16px; }
#Segment[first="true"] { border-top-left-radius: 8px; border-bottom-left-radius: 8px; }
#Segment[last="true"] {
    border-top-right-radius: 8px; border-bottom-right-radius: 8px; border-right: 1px solid @border;
}
#Segment:checked { background: @accent; color: @on_accent; border-color: @accent; font-weight: 600; }
#FindBar { background: @surface; border-bottom: 1px solid @border; }
#FileBrowser { background: @bg; }
/* The path of each side as buttons, over its field; the same background, so it reads as the field. */
#Crumbs { background: @surface2; }
#Crumbs:disabled { background: @surface; }
#Crumbs QToolButton {
    background: transparent; border: none; border-radius: 5px; padding: 2px 5px; color: @muted;
}
#Crumbs QToolButton:hover { background: @hover; color: @text; }
#Crumbs QToolButton[current="true"] { color: @text; font-weight: 600; }
#CrumbSep { color: @border2; padding: 0 1px; }
QAbstractScrollArea::corner { background: transparent; border: none; }
#Switcher { background: @surface; border: 1px solid @border2; border-radius: 12px; }
#SwitcherList { background: transparent; border: none; }
#SwitcherList::item { padding: 7px 10px; border-radius: 6px; }
#SwitcherList::item:selected { background: @select; color: @text; }
#SwitcherEmpty { color: @muted; padding: 16px 10px; }
/* No padding of its own: Qt sizes the list without it, and would add a scroll bar for the difference. */
#CompleterPopup {
    background: @surface2; border: 1px solid @border2; outline: 0;
}
#CompleterPopup::item { padding: 7px 10px; margin: 2px 4px; border-radius: 6px; }
#CompleterPopup::item:selected { background: @select; color: @text; }
#KeyCap {
    background: @surface2; color: @text; border: 1px solid @border2; border-bottom-width: 2px;
    border-radius: 5px; padding: 1px 7px; font-size: @fs_smallpt;
}
QPlainTextEdit, #SnippetList {
    background: @surface2; border: 1px solid @border; border-radius: 8px; padding: 6px;
    selection-background-color: @accent; selection-color: @on_accent;
}
QPlainTextEdit:focus { border-color: @accent; }
#SnippetList::item { padding: 6px 8px; border-radius: 6px; }
#SnippetList::item:selected { background: @select; color: @text; }
#RecentButton { background: @surface; border: 1px solid @border; border-radius: 8px; padding: 8px 14px; }
#RecentButton:hover { border-color: @accent; }
#TransferLog { background: @surface; border: none; border-top: 1px solid @border; padding: 4px 8px; }
#TransferLog::item { padding: 2px 4px; color: @muted; }
/* The file browser: the two sides and the queue are cards on the window's background. */
#FilePanel, #FileCard { background: @surface; border: 1px solid @border; border-radius: 14px; }
#FilePanelTitle { font-weight: 700; }
#FilePanel QLineEdit { padding: 4px 10px; border: none; border-radius: 8px; background: @surface2; }
#FilePanel QLineEdit:focus { border: 1px solid @accent; padding: 3px 9px; }
#FilePanel QToolButton#Icon { border-radius: 7px; padding: 3px; }
#FilePanel QToolButton#Icon:checked { background: @select; }
#FileToolbar { background: @bg; }
#FileGap::handle { background: transparent; }
#FileSummary { color: @muted; font-size: @fs_smallpt; padding: 4px 2px 6px 2px; }
#FileSplit::handle { background: transparent; }
QPushButton#TransferAction {
    font-weight: 600; padding: 4px 12px; border: none; border-radius: 8px; background: transparent;
}
QPushButton#TransferAction:hover { background: @select; }
QPushButton#TransferAction:pressed { background: @hover; }
QPushButton#TransferAction:disabled { color: @muted; }
#FileBrowser #SettingsFooter { border-top: none; background: transparent; }
#FileBrowser #SettingsFooter QPushButton, #FileBrowser #SettingsFooter QComboBox { border-radius: 8px; }
#TransferQueue QTreeWidget { background: transparent; border: none; }
#TransferQueue QTabBar::tab {
    min-width: 0; margin: 0 2px; padding: 7px 12px; border: none;
    border-bottom: 2px solid transparent; border-radius: 0; background: transparent; color: @muted;
}
#TransferQueue QTabBar::tab:hover { color: @text; }
#TransferQueue QTabBar { background: transparent; border-bottom: 1px solid @border; }
#TransferQueue QTabBar::tab:selected {
    color: @text; background: transparent; border-top: none; border-bottom: 2px solid @accent; font-weight: 600;
}
#FileCard QHeaderView::section, #FilePanel QHeaderView::section {
    background: transparent; border-bottom: none; padding: 4px 8px 2px 8px;
}
/* The sort arrow sits above the heading, as in Explorer, so it never covers a right-aligned one such as Size. */
QHeaderView::up-arrow, QHeaderView::down-arrow {
    width: 8px; height: 8px; subcontrol-origin: margin; subcontrol-position: top center; top: 1px;
}
QHeaderView::up-arrow { image: url("@arrow_up"); }
QHeaderView::down-arrow { image: url("@arrow_down"); }
#FilePanel QTreeView { background: transparent; border: none; }
#FilePanel QTreeView::item { padding: 3px 4px; border-radius: 6px; margin: 0 2px; }
#FilePanel QTreeView::item:hover { background: @hover; }
#FilePanel QTreeView::item:selected { background: @select; color: @text; }
/* Without this the indent left of a selected folder is filled with the accent colour. Styling the
   indent drops Qt's own arrows, so the folders get the chevrons the session list uses. */
#FilePanel QTreeView::branch:selected { background: @select; }
#FilePanel QTreeView::branch:has-children:closed { image: url("@branch_closed"); }
#FilePanel QTreeView::branch:has-children:open { image: url("@branch_open"); }
QHeaderView::section {
    background: @surface; color: @muted; border: none; border-bottom: 1px solid @border;
    padding: 7px 8px 5px 8px; font-size: @fs_smallpt;
}
QProgressBar { background: @border; border: none; border-radius: 2px; }
QProgressBar::chunk { background: @accent; border-radius: 2px; }
QToolButton#Icon:checked { background: @select; }
#AboutName { font-size: @fs_titlept; font-weight: 700; }
#Chip {
    color: @muted; background: @surface2; border: 1px solid @border; border-radius: 10px;
    padding: 2px 10px; font-size: @fs_smallpt;
}
#Card { background: @surface; border: 1px solid @border; border-radius: 12px; }
#MonitorHeading { font-size: @fs_largept; font-weight: 600; }
#MonitorValue { font-size: @fs_largept; font-weight: 600; }
#Preview {
    background: @term_bg; color: @term_fg; border: 1px solid @border;
    border-radius: 8px; padding: 12px;
}
"""


def load_fonts() -> None:
    """Register the bundled Ubuntu and Ubuntu Mono fonts with Qt."""
    for path in sorted(FONT_DIR.glob("*.ttf")):
        QFontDatabase.addApplicationFont(str(path))


def stylesheet(family: str = DEFAULT_UI_FONT, size: int = DEFAULT_UI_SIZE) -> str:
    values = dict(COLORS)
    values.update(
        font=family,
        fs_base=str(size),
        fs_small=str(max(6, size - 1)),
        fs_large=str(size + 2),
        fs_title=str(size * 2 + 2),
        arrow_down=_icon_file("chevron-down"),
        arrow_up=_icon_file("chevron-up"),
        branch_closed=_icon_file("chevron-right", share=0.5),
        branch_open=_icon_file("chevron-down", share=0.5),
        check=_icon_file("check", COLORS["on_accent"]),
        check_disabled=_icon_file("check"),
    )
    qss = _QSS
    # Longest names first so "@border2" is not clobbered by "@border".
    for name in sorted(values, key=len, reverse=True):
        qss = qss.replace("@" + name, values[name])
    return qss


def set_style_property(widget, name: str, value, children: bool = False) -> None:
    """Set a property the stylesheet looks at, and let the stylesheet see the change.

    With `children`, the widgets inside are restyled too, for rules such as `[dense="true"] QLabel`.
    """
    if widget.property(name) == value:
        return
    widget.setProperty(name, value)
    for target in [widget] + (widget.findChildren(QWidget) if children else []):
        target.style().unpolish(target)
        target.style().polish(target)


def _application_palette() -> QPalette:
    """Qt's own dialogs and item views draw from the palette, not the stylesheet."""
    c = {name: QColor(value) for name, value in COLORS.items()}
    palette = QPalette()
    roles = QPalette.ColorRole
    for role, color in {
        roles.Window: c["bg"],
        roles.WindowText: c["text"],
        roles.Base: c["surface2"],
        roles.AlternateBase: c["surface"],
        roles.Text: c["text"],
        roles.Button: c["surface2"],
        roles.ButtonText: c["text"],
        roles.BrightText: c["red"],
        roles.Highlight: c["accent"],
        roles.HighlightedText: c["on_accent"],
        roles.ToolTipBase: c["surface2"],
        roles.ToolTipText: c["text"],
        roles.PlaceholderText: c["muted"],
        roles.Link: c["accent"],
        roles.Light: c["border2"],
        roles.Mid: c["border"],
        roles.Dark: c["border"],
        roles.Shadow: c["bg"],
    }.items():
        palette.setColor(role, color)
    for role in (roles.WindowText, roles.Text, roles.ButtonText):
        palette.setColor(QPalette.ColorGroup.Disabled, role, c["muted"])
    return palette


def apply_appearance(theme: str, accent: str, family: str, size: int, scheme: str = "") -> str:
    """Apply theme, accent and interface font to the whole application.

    Safe to call while running. Returns the concrete theme ("dark" or "light").
    Widgets that painted icons or text in theme colours must refresh themselves afterwards.
    """
    app = QApplication.instance()
    hints = app.styleHints()
    try:
        # Native title bars follow this; "system" hands the decision back to the OS.
        if theme == THEME_SYSTEM:
            hints.unsetColorScheme()
        else:
            hints.setColorScheme(Qt.ColorScheme.Light if theme == THEME_LIGHT else Qt.ColorScheme.Dark)
    except AttributeError:  # Qt older than 6.8
        pass
    applied = set_theme(theme, accent, scheme)
    font = QFont(family, size)
    font.setFamilies([family] + [f for f in UI_FONTS if f != family])
    app.setFont(font)
    app.setPalette(_application_palette())
    app.setStyleSheet(stylesheet(family, size))
    return applied


def theme_preview(theme: str, accent: str = "", width: int = 132, height: int = 84) -> QPixmap:
    """Miniature of the main window in a theme, for the theme chooser."""
    scale = 3
    pm = QPixmap(width * scale, height * scale)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.scale(scale, scale)
    frame = QRectF(0, 0, width, height)
    rounded = QPainterPath()
    rounded.addRoundedRect(frame, 7, 7)
    p.setClipPath(rounded)

    def draw(colors: dict) -> None:
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(colors["bg"]))
        p.drawRoundedRect(frame, 7, 7)
        p.setBrush(QColor(colors["surface"]))
        p.drawRect(QRectF(0, 14, 36, height - 14))
        p.setBrush(QColor(colors["term_bg"]))
        p.drawRect(QRectF(37, 24, width - 37, height - 24))
        p.setBrush(QColor(colors["accent"]))
        p.drawRoundedRect(QRectF(width - 30, 4, 24, 6), 3, 3)
        p.drawRect(QRectF(40, 22, 26, 2))
        p.setBrush(QColor(colors["muted"]))
        for row, length in enumerate((20, 26, 16)):
            p.drawRoundedRect(QRectF(6, 22 + row * 9, length, 3), 1.5, 1.5)
        for row, (length, key) in enumerate(((54, "term_fg"), (38, "green"), (62, "term_fg"), (30, "accent"))):
            p.setBrush(QColor(colors[key]))
            p.drawRoundedRect(QRectF(44, 32 + row * 9, length, 3), 1.5, 1.5)

    if theme == THEME_SYSTEM:
        # Half dark, half light: the app follows the operating system.
        draw(palette_for(THEME_DARK, accent))
        p.setClipRect(QRectF(width / 2, 0, width / 2, height), Qt.ClipOperation.IntersectClip)
        draw(palette_for(THEME_LIGHT, accent))
    else:
        draw(palette_for(theme, accent))
    p.end()
    pm.setDevicePixelRatio(scale)
    return pm


# ---- icons -------------------------------------------------------------------


def _icon_pixmap(name: str, color: str, px: int = 96) -> QPixmap:
    """Line icons drawn on a 24x24 grid so they stay crisp at any scale."""
    pm = QPixmap(px, px)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.scale(px / 24, px / 24)
    pen = QPen(QColor(color), 1.9)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    p.setPen(pen)
    p.setBrush(Qt.BrushStyle.NoBrush)

    if name == "plus":
        p.drawLine(QPointF(12, 5), QPointF(12, 19))
        p.drawLine(QPointF(5, 12), QPointF(19, 12))
    elif name == "check":
        pen.setWidthF(2.6)
        p.setPen(pen)
        p.drawPolyline(QPolygonF([QPointF(5.5, 12.5), QPointF(10, 17), QPointF(18.5, 7.5)]))
    elif name == "close":
        p.drawLine(QPointF(6, 6), QPointF(18, 18))
        p.drawLine(QPointF(18, 6), QPointF(6, 18))
    elif name == "sidebar":
        p.drawRoundedRect(QRectF(3, 4.5, 18, 15), 3, 3)
        p.drawLine(QPointF(9.5, 4.5), QPointF(9.5, 19.5))
    elif name in ("chevron-down", "chevron-up", "chevron-right"):
        points = {
            "chevron-down": [(6, 9), (12, 15), (18, 9)],
            "chevron-up": [(6, 15), (12, 9), (18, 15)],
            "chevron-right": [(9, 6), (15, 12), (9, 18)],
        }[name]
        p.drawPolyline(QPolygonF([QPointF(x, y) for x, y in points]))
    elif name == "settings":
        # A gear: a thick ring with eight square teeth growing out of it.
        pen.setWidthF(2.6)
        p.setPen(pen)
        p.drawEllipse(QPointF(12, 12), 5.4, 5.4)
        pen.setWidthF(3.4)
        pen.setCapStyle(Qt.PenCapStyle.FlatCap)
        p.setPen(pen)
        for step in range(8):
            angle = step * math.pi / 4
            dx, dy = math.cos(angle), math.sin(angle)
            p.drawLine(QPointF(12 + dx * 6.2, 12 + dy * 6.2), QPointF(12 + dx * 9.6, 12 + dy * 9.6))
    elif name == "computer":
        p.drawRoundedRect(QRectF(3, 4.5, 18, 12), 2, 2)
        p.drawLine(QPointF(12, 16.5), QPointF(12, 19.5))
        p.drawLine(QPointF(8, 19.5), QPointF(16, 19.5))
    elif name == "server":
        for top in (4, 13):
            p.drawRoundedRect(QRectF(4, top, 16, 7), 2, 2)
            p.drawPoint(QPointF(7.5, top + 3.5))
    elif name == "search":
        p.drawEllipse(QPointF(10.5, 10.5), 6, 6)
        p.drawLine(QPointF(15, 15), QPointF(20, 20))
    elif name == "folder":
        p.drawPolygon(QPolygonF([QPointF(x, y) for x, y in ((3, 6), (9, 6), (11, 8.5), (21, 8.5), (21, 19), (3, 19))]))
    elif name == "file":
        p.drawPolygon(QPolygonF([QPointF(x, y) for x, y in ((6, 3), (14, 3), (18, 7), (18, 21), (6, 21))]))
        p.drawPolyline(QPolygonF([QPointF(14, 3), QPointF(14, 7), QPointF(18, 7)]))
    elif name in ("up", "upload"):
        base = 16 if name == "upload" else 19
        p.drawLine(QPointF(12, base), QPointF(12, 5))
        p.drawPolyline(QPolygonF([QPointF(6.5, 10.5), QPointF(12, 5), QPointF(17.5, 10.5)]))
        if name == "upload":
            p.drawLine(QPointF(5, 20), QPointF(19, 20))
    elif name in ("arrow-right", "arrow-left"):
        tip, tail = (19, 5) if name == "arrow-right" else (5, 19)
        back = tip - 6 if name == "arrow-right" else tip + 6
        p.drawLine(QPointF(tail, 12), QPointF(tip, 12))
        p.drawPolyline(QPolygonF([QPointF(back, 6.5), QPointF(tip, 12), QPointF(back, 17.5)]))
    elif name == "import":
        p.drawLine(QPointF(12, 4), QPointF(12, 15))
        p.drawPolyline(QPolygonF([QPointF(6.5, 9.5), QPointF(12, 15), QPointF(17.5, 9.5)]))
        p.drawLine(QPointF(5, 20), QPointF(19, 20))
    elif name == "refresh":
        p.drawArc(QRectF(5, 5, 14, 14), 40 * 16, 290 * 16)
        p.drawPolyline(QPolygonF([QPointF(19.5, 4), QPointF(19.5, 9), QPointF(14.5, 9)]))
    elif name == "split":
        p.drawRoundedRect(QRectF(3, 4.5, 18, 15), 3, 3)
        p.drawLine(QPointF(12, 4.5), QPointF(12, 19.5))
    elif name == "star":
        points = []
        for step in range(10):
            angle = -math.pi / 2 + step * math.pi / 5
            radius = 8.5 if step % 2 == 0 else 3.6
            points.append(QPointF(12 + radius * math.cos(angle), 12.5 + radius * math.sin(angle)))
        p.drawPolygon(QPolygonF(points))
    elif name == "list":
        for y in (7, 12, 17):
            p.drawLine(QPointF(9, y), QPointF(20, y))
            p.drawPoint(QPointF(4.5, y))
    elif name == "sort":
        # Lines growing shorter: an ordered list.
        for y, length in ((6.5, 16), (12, 11), (17.5, 6)):
            p.drawLine(QPointF(4, y), QPointF(4 + length, y))
    elif name == "clock":
        p.drawEllipse(QPointF(12, 12), 8.5, 8.5)
        p.drawPolyline(QPolygonF([QPointF(12, 7), QPointF(12, 12), QPointF(15.5, 14)]))
    elif name == "terminal":
        p.drawRoundedRect(QRectF(3, 4.5, 18, 15), 2.5, 2.5)
        p.drawPolyline(QPolygonF([QPointF(7, 9.5), QPointF(10, 12), QPointF(7, 14.5)]))
        p.drawLine(QPointF(12, 15), QPointF(17, 15))
    elif name == "send":
        p.drawPolyline(QPolygonF([QPointF(5, 7), QPointF(10, 12), QPointF(5, 17)]))
        p.drawLine(QPointF(12, 17), QPointF(19, 17))
    elif name == "sun":
        p.drawEllipse(QPointF(12, 12), 4, 4)
        for step in range(8):
            angle = step * math.pi / 4
            dx, dy = math.cos(angle), math.sin(angle)
            p.drawLine(QPointF(12 + dx * 7, 12 + dy * 7), QPointF(12 + dx * 9.5, 12 + dy * 9.5))
    elif name == "moon":
        # A crescent: a disc with a smaller one taken out of its upper right.
        crescent = QPainterPath()
        crescent.addEllipse(QPointF(12, 12), 8, 8)
        bite = QPainterPath()
        bite.addEllipse(QPointF(16.5, 8), 6.5, 6.5)
        p.drawPath(crescent.subtracted(bite))
    elif name in ("eye", "eye-off"):
        outline = QPainterPath()
        outline.moveTo(2.5, 12)
        outline.quadTo(12, 3.5, 21.5, 12)
        outline.quadTo(12, 20.5, 2.5, 12)
        p.drawPath(outline)
        p.drawEllipse(QPointF(12, 12), 2.8, 2.8)
        if name == "eye-off":
            p.drawLine(QPointF(4.5, 19.5), QPointF(19.5, 4.5))
    p.end()
    return pm


def line_icon(name: str, color: str | None = None) -> QIcon:
    return QIcon(_icon_pixmap(name, color or COLORS["text"]))


def badged_icon(name: str, badge: str) -> QIcon:
    """A line icon with a dot of the colour `badge` at its top right, cut out of the line beneath it."""
    pm = _icon_pixmap(name, COLORS["text"])
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setCompositionMode(QPainter.CompositionMode.CompositionMode_Clear)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(Qt.GlobalColor.black)
    p.drawEllipse(QPointF(76, 20), 22, 22)
    p.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)
    p.setBrush(QColor(badge))
    p.drawEllipse(QPointF(76, 20), 15, 15)
    p.end()
    return QIcon(pm)


def icon_pixmap(name: str, color: str | None = None, px: int = 96) -> QPixmap:
    return _icon_pixmap(name, color or COLORS["text"], px)


def _icon_file(name: str, color: str | None = None, share: float = 1.0) -> str:
    """Stylesheets can only reference images by path, so arrows and ticks are written to a temp file.

    `share` draws the icon that part of the image's width, centred: Qt stretches a tree's branch
    image to the row, and the empty margin keeps the arrow small however tall the rows are.
    """
    color = color or COLORS["muted"]
    suffix = f"-{round(share * 100)}" if share != 1.0 else ""
    path = Path(tempfile.gettempdir()) / f"techxterminal-{name}-{color.lstrip('#')}{suffix}.png"
    if not path.exists():
        image = QPixmap(48, 48)
        image.fill(Qt.GlobalColor.transparent)
        size = round(48 * share)
        painter = QPainter(image)
        painter.drawPixmap((48 - size) // 2, (48 - size) // 2, _icon_pixmap(name, color, size))
        painter.end()
        image.save(str(path))
    return path.as_posix()


def usable_font_families(monospaced: bool = False) -> list[str]:
    """Font families that are safe to offer: scalable ones only.

    Old bitmap fonts (Fixedsys, Terminal, System ...) cannot be loaded by Qt's
    DirectWrite backend and only produce warnings, and "@" names are vertical variants.
    """
    families = []
    for family in QFontDatabase.families():
        if family.startswith("@") or not QFontDatabase.isSmoothlyScalable(family):
            continue
        if monospaced and not QFontDatabase.isFixedPitch(family):
            continue
        families.append(family)
    return families


def tab_icon(state_color: str, session_color: str = "", ring_color: str = "") -> QIcon:
    """Tab icon: the session's label colour as a bar, then the connection status dot.

    A ring around the dot marks a tab that takes part in a broadcast.
    """
    pm = QPixmap(88, 64)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setPen(Qt.PenStyle.NoPen)
    if session_color:
        p.setBrush(QColor(session_color))
        p.drawRoundedRect(QRectF(4, 6, 16, 52), 8, 8)
    p.setBrush(QColor(state_color))
    p.drawEllipse(QRectF(46, 18, 28, 28))
    if ring_color:
        p.setPen(QPen(QColor(ring_color), 6))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawEllipse(QRectF(36, 8, 48, 48))
    p.end()
    return QIcon(pm)


def dot_icon(color: str) -> QIcon:
    """Small status dot."""
    pm = QPixmap(32, 32)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor(color))
    p.drawEllipse(QRectF(9, 9, 14, 14))
    p.end()
    return QIcon(pm)


def initials(name: str) -> str:
    """Two letters for a session's avatar: the first of two words ("db primary" gives "DP"), else the first two.

    A number keeps its digits, so numbered servers stay apart: "web-01" and "web-02" give "W1" and "W2".
    """
    words = [word for word in re.split(r"[\s._\-@]+", name) if word]
    if len(words) >= 2 and words[1].isdigit():
        return (words[0][0] + str(int(words[1]))[-2:]).upper()
    if len(words) >= 2:
        return (words[0][0] + words[1][0]).upper()
    return (words[0][:2] if words else "?").upper()


def session_avatar(name: str, color: str = "", state_color: str = "", pinned: bool = False) -> QIcon:
    """A saved session as a rounded tile with its initials, in its label colour if it has one, and the dot
    of its connection state at the corner, cut out of the tile so that it reads on any colour."""
    pm = QPixmap(96, 96)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    fill = QColor(color or COLORS["surface2"])
    p.setPen(QPen(QColor(COLORS["border2"]), 2) if not color else Qt.PenStyle.NoPen)
    p.setBrush(fill)
    p.drawRoundedRect(QRectF(4, 4, 88, 88), 24, 24)
    # Dark letters on a light label colour, white on a dark one, and the text colour on the plain tile.
    if not color:
        ink = COLORS["text"]
    else:
        ink = "#0b0d12" if fill.lightnessF() > 0.55 else "#ffffff"
    p.setPen(QColor(ink))
    font = QFont(UI_FONTS[0])
    font.setFamilies(UI_FONTS)
    font.setPixelSize(34)
    font.setBold(True)
    p.setFont(font)
    p.drawText(QRectF(4, 4, 88, 88), Qt.AlignmentFlag.AlignCenter, initials(name))
    if state_color:
        p.setCompositionMode(QPainter.CompositionMode.CompositionMode_Clear)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(Qt.GlobalColor.black)
        p.drawEllipse(QPointF(80, 80), 18, 18)
        p.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)
        p.setBrush(QColor(state_color))
        p.drawEllipse(QPointF(80, 80), 12, 12)
    if pinned:
        # A diamond at the opposite corner, cut out of the tile like the dot: this session opens its tab pinned.
        for mode, radius, brush in (
            (QPainter.CompositionMode.CompositionMode_Clear, 20, Qt.GlobalColor.black),
            (QPainter.CompositionMode.CompositionMode_SourceOver, 13, QColor(COLORS["accent"])),
        ):
            p.setCompositionMode(mode)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(brush)
            p.drawPolygon(QPolygonF([QPointF(80 + dx * radius, 16 + dy * radius) for dx, dy in ((0, -1), (1, 0), (0, 1), (-1, 0))]))
    p.end()
    return QIcon(pm)


def app_icon() -> QIcon:
    pm = QPixmap(256, 256)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    grad = QLinearGradient(0, 0, 256, 256)
    grad.setColorAt(0, QColor("#2f6fe0"))
    grad.setColorAt(1, QColor("#0f2a6b"))
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(grad)
    p.drawRoundedRect(QRectF(12, 12, 232, 232), 54, 54)
    font = QFont(TERMINAL_FONTS[0])
    font.setFamilies(TERMINAL_FONTS)
    font.setPixelSize(120)
    font.setBold(True)
    p.setFont(font)
    p.setPen(QColor("#eef3ff"))  # light on the dark blue
    p.drawText(QRectF(12, 4, 232, 232), Qt.AlignmentFlag.AlignCenter, ">_")
    p.end()
    return QIcon(pm)
