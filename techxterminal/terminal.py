"""Terminal emulator widget: pyte does the VT parsing, this paints and handles input."""

import codecs
import html
import math
import re
import time
from collections import deque

import pyte
from pyte import charsets as _charsets
from pyte import modes as _modes
from PyQt6.QtCore import QEvent, QPointF, QRectF, Qt, QTimer, QUrl, pyqtSignal
from PyQt6.QtGui import (
    QColor,
    QDesktopServices,
    QFont,
    QFontDatabase,
    QFontMetricsF,
    QGuiApplication,
    QPainter,
    QPen,
)
from PyQt6.QtWidgets import QAbstractScrollArea, QFrame, QMenu, QMessageBox, QPushButton, QScrollBar

from .preferences import CURSOR_BAR, CURSOR_BLOCK, Preferences
from .theme import ANSI, COLORS, DEFAULT_TERMINAL_FONT, DEFAULT_TERMINAL_SIZE, TERMINAL_FONTS

PADDING = 8
MIN_FONT_SIZE, MAX_FONT_SIZE = 6, 32

_ALT_SCREEN_MODES = {47, 1047, 1049}
_MODE_APP_CURSOR = 1 << 5  # DECCKM, as stored by pyte for private modes
_MODE_BRACKETED_PASTE = 2004 << 5
# Mouse reporting requested by the remote program: clicks, clicks+drag, all motion.
_MODE_MOUSE_CLICK, _MODE_MOUSE_DRAG, _MODE_MOUSE_MOTION = 1000 << 5, 1002 << 5, 1003 << 5
_MODE_MOUSE_SGR = 1006 << 5  # the modern coordinate encoding, without the 223 column limit
_URL = re.compile(r"\b(?:https?|ftp)://[^\s<>\"'`]+")
# Incoming data is parsed in slices so a flood of output cannot freeze the window.
FEED_SLICE_BYTES = 16 * 1024
FEED_BUDGET_SECONDS = 0.012
# xterm "modifyOtherKeys" (CSI > ... m) is misread by pyte as an SGR underline.
_XTERM_MODKEYS = re.compile(rb"\x1b\[>[0-9;]*m")

_STUB, _WIDE, _NARROW = "stub", "wide", "narrow"  # how a non-ASCII cell is painted
# Printable ASCII, and everything else, in the order they come: see TermScreen.draw.
_TEXT_RUNS = re.compile(r"(?P<ascii>[ -~]+)|(?P<other>[^ -~]+)")

K = Qt.Key
_CURSOR_KEYS = {K.Key_Up: "A", K.Key_Down: "B", K.Key_Right: "C", K.Key_Left: "D", K.Key_Home: "H", K.Key_End: "F"}
_TILDE_KEYS = {
    K.Key_Insert: 2, K.Key_Delete: 3, K.Key_PageUp: 5, K.Key_PageDown: 6,
    K.Key_F5: 15, K.Key_F6: 17, K.Key_F7: 18, K.Key_F8: 19,
    K.Key_F9: 20, K.Key_F10: 21, K.Key_F11: 23, K.Key_F12: 24,
}
_SS3_KEYS = {K.Key_F1: "P", K.Key_F2: "Q", K.Key_F3: "R", K.Key_F4: "S"}


class TermScreen(pyte.HistoryScreen):
    """HistoryScreen plus the alternate screen buffer that vim, htop, less etc. rely on."""

    _alt_saved = None
    on_reply = None
    on_bell = None

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._cell_cache: dict = {}  # drawing attributes -> {character: the cell that shows it}

    def draw(self, data: str) -> None:
        """Show text at the cursor.

        pyte draws one character at a time, each time asking the screen for a dozen of its attributes through a
        hook, and that is what limits how fast a flood of output can be shown. Printable ASCII, which is nearly all
        of a flood, is written here a stretch of a line at a time with cells made once and reused. Everything else
        (wide and accented characters, graphics character sets, insert mode) is left to pyte as it always was.
        """
        if _modes.IRM in self.mode or (self.g1_charset if self.charset else self.g0_charset) is not _charsets.LAT1_MAP:
            super().draw(data)
            return
        for run in _TEXT_RUNS.finditer(data):
            if run.lastgroup == "ascii":
                self._draw_ascii(run.group())
            else:
                super().draw(run.group())

    def _draw_ascii(self, text: str) -> None:
        cursor, columns, buffer = self.cursor, self.columns, self.buffer
        wrap = _modes.DECAWM in self.mode
        attributes = cursor.attrs
        cells = self._cell_cache.get(attributes)
        if cells is None:
            if len(self._cell_cache) > 256:
                self._cell_cache.clear()  # an application that uses very many colours must not grow this without end
            cells = self._cell_cache[attributes] = {}
        position, length = 0, len(text)
        while position < length:
            if cursor.x > columns:
                # A resize to fewer columns can leave the cursor outside: pyte writes one character there and then
                # the cursor is at the edge, which a stretch written from here could not reach (it would never end).
                super().draw(text[position])
                position += 1
                continue
            if cursor.x == columns:
                if wrap:
                    self.dirty.add(cursor.y)
                    self.carriage_return()
                    self.linefeed()
                else:
                    cursor.x -= 1  # the last cell is written over
            line = buffer[cursor.y]
            x = cursor.x
            stop = min(length, position + columns - x)
            for character in text[position:stop]:
                cell = cells.get(character)
                if cell is None:
                    cell = cells[character] = attributes._replace(data=character)
                line[x] = cell
                x += 1
            cursor.x = x
            position = stop
        self.dirty.add(cursor.y)

    def bell(self, *args):
        if self.on_bell:
            self.on_bell()

    @property
    def in_alt_screen(self) -> bool:
        return self._alt_saved is not None

    def write_process_input(self, data: str) -> None:
        if self.on_reply:
            self.on_reply(data)

    def set_mode(self, *modes, **kwargs):
        if kwargs.get("private") and _ALT_SCREEN_MODES.intersection(modes) and self._alt_saved is None:
            saved = {y: dict(line) for y, line in self.buffer.items()}
            self._alt_saved = (saved, self.cursor.x, self.cursor.y)
            self.buffer.clear()
            self.cursor.x = self.cursor.y = 0
            self.dirty.update(range(self.lines))
        super().set_mode(*modes, **kwargs)

    def reset_mode(self, *modes, **kwargs):
        if kwargs.get("private") and _ALT_SCREEN_MODES.intersection(modes) and self._alt_saved is not None:
            saved, x, y = self._alt_saved
            self._alt_saved = None
            self.buffer.clear()
            for row, cells in saved.items():
                if row < self.lines:
                    self.buffer[row].update(cells)
            self.cursor.x = min(x, self.columns - 1)
            self.cursor.y = min(y, self.lines - 1)
            self.dirty.update(range(self.lines))
        super().reset_mode(*modes, **kwargs)

    def resize(self, lines=None, columns=None):
        lines = lines or self.lines
        columns = columns or self.columns
        if lines < self.lines:
            # pyte clips rows off the top but leaves the cursor where it was, which
            # detaches the prompt from its text. Scroll just enough to keep the
            # cursor visible instead, and move the scrolled-off rows into history.
            drop = max(0, self.cursor.y - (lines - 1))
            if drop:
                if self._alt_saved is None:
                    self.history.top.extend(self.buffer[y] for y in range(drop))
                kept = {y - drop: line for y, line in self.buffer.items() if y >= drop}
                self.buffer.clear()
                self.buffer.update(kept)
                self.cursor.y -= drop
            for y in [y for y in self.buffer if y >= lines]:
                del self.buffer[y]
            self.lines = lines
            self.set_margins()
            self.dirty.update(range(lines))
        super().resize(lines, columns)

    def index(self):
        if self._alt_saved is not None:
            pyte.Screen.index(self)  # full-screen apps must not pollute the scrollback
        else:
            super().index()

    def select_graphic_rendition(self, *attrs, **kwargs):
        if kwargs.get("private"):
            return
        super().select_graphic_rendition(*attrs)


_BAD_WORDS = "error|errors|failed|failure|fatal|denied|refused|critical|panic|exception"
_WARN_WORDS = "warning|warnings|warn|deprecated|timeout|timed out"
_GOOD_WORDS = "success|successful|succeeded|passed|completed"


def _highlight_pattern(extra_words: str = ""):
    """One pattern for all highlighted words; the group name says which colour a match gets."""
    parts = [f"(?P<bad>\\b(?:{_BAD_WORDS})\\b)", f"(?P<warn>\\b(?:{_WARN_WORDS})\\b)", f"(?P<good>\\b(?:{_GOOD_WORDS})\\b)"]
    words = [re.escape(word.strip()) for word in extra_words.split(",") if word.strip()]
    if words:
        parts.insert(0, f"(?P<own>{'|'.join(words)})")  # the user's own words win over the built-in ones
    return re.compile("|".join(parts), re.IGNORECASE)


def pick_terminal_font(size: int, family: str = DEFAULT_TERMINAL_FONT) -> QFont:
    # Bitmap fonts such as Fixedsys are listed by Windows but cannot be rendered.
    available = {f for f in QFontDatabase.families() if QFontDatabase.isSmoothlyScalable(f)}
    family = next((f for f in [family] + TERMINAL_FONTS if f in available), None)
    font = QFont(family, size) if family else QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)
    font.setPointSize(size)
    font.setFixedPitch(True)
    font.setStyleHint(QFont.StyleHint.Monospace)
    font.setHintingPreference(QFont.HintingPreference.PreferFullHinting)
    return font


class MarkedScrollBar(QScrollBar):
    """The terminal's scrollbar, with a mark for every find match, so they can be seen at a glance."""

    def __init__(self, terminal: "TerminalWidget"):
        super().__init__(Qt.Orientation.Vertical)
        self._terminal = terminal

    def paintEvent(self, event):
        super().paintEvent(event)
        lines, current = self._terminal.find_marks()
        if not lines:
            return
        total = max(1, self._terminal.total_lines())
        height = self.height()
        p = QPainter(self)
        for line in lines:
            # One mark per line, however many matches the line holds.
            p.fillRect(QRectF(2, int(line / total * height), self.width() - 4, 2), self._terminal.find_mark_colour(False))
        if current is not None:
            p.fillRect(QRectF(0, int(current / total * height) - 1, self.width(), 4), self._terminal.find_mark_colour(True))
        p.end()


class TerminalWidget(QAbstractScrollArea):
    send = pyqtSignal(bytes)  # bytes typed/pasted by the user
    sizeChanged = pyqtSignal(int, int)  # cols, rows
    titleChanged = pyqtSignal(str)
    fontSizeChanged = pyqtSignal(int)
    findRequested = pyqtSignal()
    saveRequested = pyqtSignal()
    saveCommandRequested = pyqtSignal(str)  # "Save as Saved Command..." was chosen, with the selected text
    bell = pyqtSignal()  # the remote side rang the bell

    def __init__(self, prefs: Preferences | None = None, parent=None):
        super().__init__(parent)
        prefs = prefs or Preferences()
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setVerticalScrollBar(MarkedScrollBar(self))
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOn)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.viewport().setCursor(Qt.CursorShape.IBeamCursor)
        self.viewport().setMouseTracking(True)
        self.viewport().setAutoFillBackground(False)

        self._load_colors()
        self._cursor_style = prefs.cursor_style
        self._cursor_blink = prefs.cursor_blink
        self._copy_on_select = prefs.copy_on_select
        self._confirm_paste = prefs.confirm_paste
        self._apply_behaviour(prefs)

        self._pending = bytearray()
        # A slow paste: the lines still to send, one per tick of the timer.
        self._paste_queue: list[str] = []
        self._paste_timer = QTimer(self)
        self._paste_timer.setSingleShot(True)
        self._paste_timer.timeout.connect(self._paste_next)
        self._feed_timer = QTimer(self)
        self._feed_timer.setSingleShot(True)
        self._feed_timer.timeout.connect(self._drain)
        self._mouse_cell = None  # last cell reported to the remote program
        self._mouse_button = None
        # Find results: (absolute line, first column, last column), and the active one.
        self._find_matches: list[tuple[int, int, int]] = []
        self._find_rows: dict[int, list[tuple[int, int, int]]] = {}
        self._find_index = -1
        self._find_query = ("", False, False)
        self._find_stale = False
        self.find_error = ""

        self.cols, self.rows = 80, 24
        self.screen = TermScreen(self.cols, self.rows, history=prefs.scrollback)
        self.screen.on_reply = lambda text: self.send.emit(text.encode())
        self.screen.on_bell = self._ring
        # Bytes are decoded here, in the session's character set, then parsed as text.
        self.stream = pyte.Stream(self.screen)
        self._encoding = "utf-8"
        self._decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self._backspace = b"\x7f"
        self._enter = b"\r"
        self._flash_until = 0.0
        self._title = ""

        # Selection endpoints are (absolute line, column); absolute lines count
        # from the top of the scrollback so they stay valid as output scrolls.
        self._sel_anchor: tuple[int, int] | None = None
        self._sel_end: tuple[int, int] | None = None
        self._selecting = False

        self._blink_on = True
        self._blink = QTimer(self)
        self._blink.setInterval(600)
        self._blink.timeout.connect(self._toggle_blink)
        self._blink.start()

        self.verticalScrollBar().valueChanged.connect(self.viewport().update)
        # A button over the output, shown while it is scrolled back, to return to the live end.
        self._latest = QPushButton("↓  Latest output", self.viewport())
        self._latest.setObjectName("JumpToLatest")
        self._latest.setCursor(Qt.CursorShape.PointingHandCursor)
        self._latest.setFocusPolicy(Qt.FocusPolicy.NoFocus)  # the keyboard stays with the terminal
        self._latest.setToolTip("Jump to the latest output (Shift+End)")
        self._latest.setAccessibleName("Jump to the latest output")
        self._latest.clicked.connect(self._scroll_to_bottom)
        self._latest.hide()
        self.verticalScrollBar().valueChanged.connect(self._update_latest)
        self.verticalScrollBar().rangeChanged.connect(self._update_latest)
        self._font_family = prefs.terminal_font
        self._font_size = prefs.terminal_size
        self._apply_font()

    # ---- public API -------------------------------------------------------

    def _load_colors(self) -> None:
        self._bg = QColor(COLORS["term_bg"])
        self._fg = QColor(COLORS["term_fg"])
        self._sel_bg = QColor(COLORS["term_sel"])
        self._cursor_color = QColor(COLORS["accent"])
        self._find_current = QColor(COLORS["amber"])
        # Other matches get a quieter version of the same colour.
        blend = lambda a, b: int(a * 0.4 + b * 0.6)  # noqa: E731
        self._find_other = QColor(
            blend(self._find_current.red(), self._bg.red()),
            blend(self._find_current.green(), self._bg.green()),
            blend(self._find_current.blue(), self._bg.blue()),
        )
        self._colors: dict[str, QColor] = {}

    def _apply_behaviour(self, prefs: Preferences) -> None:
        self._right_click_paste = prefs.right_click_paste
        self._visual_bell = prefs.visual_bell
        self._highlight = _highlight_pattern(prefs.highlight_words) if prefs.highlight_keywords else None
        self._paste_delay = prefs.paste_line_delay_ms

    def configure_session(self, encoding: str = "utf-8", backspace: str = "del", enter: str = "cr") -> None:
        """Per-session behaviour: the remote character set and what Backspace and Enter send."""
        try:
            codecs.lookup(encoding)
        except LookupError:
            encoding = "utf-8"
        if encoding != self._encoding:
            self._encoding = encoding
            self._decoder = codecs.getincrementaldecoder(encoding)("replace")
        self._backspace = b"\x08" if backspace == "bs" else b"\x7f"
        self._enter = b"\r\n" if enter == "crlf" else b"\r"

    def encoding(self) -> str:
        return self._encoding

    def _ring(self) -> None:
        if self._visual_bell:
            self._flash_until = time.monotonic() + 0.18
            self.viewport().update()
            QTimer.singleShot(200, self.viewport().update)
        self.bell.emit()

    def retheme(self) -> None:
        """Pick up the active theme's colours; text already on screen is recoloured too."""
        self._load_colors()
        self.viewport().update()

    def apply_preferences(self, prefs: Preferences) -> None:
        self._cursor_style = prefs.cursor_style
        self._cursor_blink = prefs.cursor_blink
        self._copy_on_select = prefs.copy_on_select
        self._confirm_paste = prefs.confirm_paste
        self._apply_behaviour(prefs)
        self._blink_on = True
        history = self.screen.history
        if history.top.maxlen != prefs.scrollback:
            bar = self.verticalScrollBar()
            at_bottom = bar.value() == bar.maximum()
            # deque(..., maxlen=n) keeps the newest n lines when shrinking.
            self.screen.history = history._replace(
                top=deque(history.top, maxlen=prefs.scrollback),
                bottom=deque(history.bottom, maxlen=prefs.scrollback),
                size=prefs.scrollback,
                position=prefs.scrollback,
            )
            self._clear_selection()
            self._sync_scrollbar(at_bottom)
        self.set_terminal_font(prefs.terminal_font, prefs.terminal_size)
        self.viewport().update()

    def feed(self, data: bytes) -> None:
        """Queue output for display. Small amounts appear at once; floods are worked off
        in slices between UI events, so the window stays responsive throughout."""
        self._pending += data
        if not self._feed_timer.isActive():
            self._drain()

    def _drain(self) -> None:
        bar = self.verticalScrollBar()
        at_bottom = bar.value() == bar.maximum()
        deadline = time.perf_counter() + FEED_BUDGET_SECONDS
        while self._pending:
            chunk = bytes(self._pending[:FEED_SLICE_BYTES])
            del self._pending[:FEED_SLICE_BYTES]
            try:
                self.stream.feed(self._decoder.decode(_XTERM_MODKEYS.sub(b"", chunk)))
            except Exception:
                pass  # a malformed escape sequence must never take the tab down
            if time.perf_counter() > deadline:
                break
        self._find_stale = True
        self._sync_scrollbar(at_bottom)
        if self.screen.title != self._title:
            self._title = self.screen.title
            self.titleChanged.emit(self._title)
        self.viewport().update()
        if self._pending:
            self._feed_timer.start(0)

    def pending_bytes(self) -> int:
        return len(self._pending)

    def write_notice(self, text: str, color: int = 90) -> None:
        """Print a local (not from the server) message into the terminal."""
        self.feed(f"\r\x1b[{color}m{text}\x1b[0m\r\n".encode())

    def clear_scrollback(self) -> None:
        self.screen.history.top.clear()
        self._clear_selection()
        self._sync_scrollbar(True)
        self.viewport().update()

    def reset(self) -> None:
        self.screen.reset()
        self.clear_scrollback()

    def font_size(self) -> int:
        return self._font_size

    def set_font_size(self, size: int) -> None:
        self.set_terminal_font(self._font_family, size)

    def set_terminal_font(self, family: str, size: int) -> None:
        size = max(MIN_FONT_SIZE, min(MAX_FONT_SIZE, size))
        if (family, size) == (self._font_family, self._font_size):
            return
        size_changed = size != self._font_size
        self._font_family, self._font_size = family, size
        self._apply_font()
        if size_changed:
            self.fontSizeChanged.emit(size)

    def has_selection(self) -> bool:
        return self._sel_anchor is not None and self._sel_anchor != self._sel_end

    def selected_text(self) -> str:
        if not self.has_selection():
            return ""
        (l0, c0), (l1, c1) = sorted((self._sel_anchor, self._sel_end))
        out = []
        for absline in range(l0, l1 + 1):
            cells = self._line_cells(absline)
            start = c0 if absline == l0 else 0
            end = c1 + 1 if absline == l1 else len(cells)
            out.append("".join(cells[start:end]).rstrip())
        return "\n".join(out)

    def copy(self) -> None:
        text = self.selected_text()
        if text:
            QGuiApplication.clipboard().setText(text)

    def paste(self) -> None:
        text = QGuiApplication.clipboard().text()
        lines = text.strip("\r\n").splitlines()
        if len(lines) > 1 and self._confirm_paste:
            preview = "\n".join(line[:70] for line in lines[:4]) + ("\n..." if len(lines) > 4 else "")
            answer = QMessageBox.question(
                self.window(),
                "Paste Several Lines",
                f"Paste {len(lines)} lines into the terminal? Each line is run as it arrives.\n\n{preview}",
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        self.paste_text(text)

    def paste_text(self, text: str) -> None:
        """Paste text: at once, or a line at a time when a delay between lines is set.

        Switches, routers and serial consoles often lose characters when many lines arrive
        together; a short pause after each line gives them time. Esc stops a slow paste.
        """
        lines = text.replace("\r\n", "\n").split("\n")
        if not self._paste_delay or len(lines) < 2:
            self.send_text(text, as_paste=True)
            return
        # Every line but the last ends in Enter, exactly as in the text.
        self._paste_queue.extend(line + "\n" for line in lines[:-1])
        if lines[-1]:
            self._paste_queue.append(lines[-1])
        if not self._paste_timer.isActive():
            self._paste_next()

    def _paste_next(self) -> None:
        if self._paste_queue:
            self.send_text(self._paste_queue.pop(0))
        if self._paste_queue:
            self._paste_timer.start(self._paste_delay)

    def pasting(self) -> int:
        """Lines of a slow paste still waiting to be sent."""
        return len(self._paste_queue)

    def cancel_paste(self) -> None:
        self._paste_queue.clear()
        self._paste_timer.stop()

    def send_text(self, text: str, as_paste: bool = False) -> None:
        if not text:
            return
        data = text.replace("\r\n", "\n").replace("\n", "\r").encode(self._encoding, "replace")
        if self._enter != b"\r":
            data = data.replace(b"\r", self._enter)
        if as_paste and _MODE_BRACKETED_PASTE in self.screen.mode:
            data = b"\x1b[200~" + data + b"\x1b[201~"
        self._scroll_to_bottom()
        self.send.emit(data)

    def all_text(self) -> str:
        """Scrollback and screen as plain text, without the empty lines at the end."""
        lines = [self._text_of(line, in_history).rstrip() for _number, line, in_history in self._lines()]
        while lines and not lines[-1]:
            lines.pop()
        return "\n".join(lines) + ("\n" if lines else "")

    def all_html(self, title: str = "") -> str:
        """Scrollback and screen as a web page that keeps the colours, bold, italic and underline."""
        lines = []
        for absline in range(self._total_lines()):
            line = self._line(absline)
            cells = []
            for x in range(self.cols):
                cell = line[x]
                if not cell.data:
                    continue  # second half of a double-width character
                fg, bg = self._color(cell.fg, self._fg), self._color(cell.bg, self._bg)
                if cell.reverse:
                    fg, bg = bg, fg
                cells.append((cell.data, (fg.name(), bg.name(), cell.bold, cell.italics, cell.underscore)))
            # Blank cells at the end of a line, on the page's own background, carry nothing.
            while cells and cells[-1][0] == " " and cells[-1][1][1] == self._bg.name():
                cells.pop()
            lines.append(cells)
        while lines and not lines[-1]:
            lines.pop()

        def span(text: str, style) -> str:
            fg, bg, bold, italic, underline = style
            css = []
            if fg != self._fg.name():
                css.append(f"color:{fg}")
            if bg != self._bg.name():
                css.append(f"background:{bg}")
            if bold:
                css.append("font-weight:bold")
            if italic:
                css.append("font-style:italic")
            if underline:
                css.append("text-decoration:underline")
            text = html.escape(text, quote=False)
            return f'<span style="{";".join(css)}">{text}</span>' if css else text

        body = []
        for cells in lines:
            runs, text, style = [], [], None
            for data, cell_style in cells:
                if cell_style != style and text:
                    runs.append(span("".join(text), style))
                    text = []
                style = cell_style
                text.append(data)
            if text:
                runs.append(span("".join(text), style))
            body.append("".join(runs))
        family = html.escape(self._fonts[(False, False)].family())
        return (
            "<!doctype html>\n<html><head><meta charset=\"utf-8\">"
            f"<title>{html.escape(title)}</title>"
            f"<style>body{{margin:0;background:{self._bg.name()};color:{self._fg.name()}}}"
            f"pre{{margin:0;padding:16px;font-family:'{family}',monospace;font-size:13px;line-height:1.3}}</style>"
            "</head><body><pre>" + "\n".join(body) + "\n</pre></body></html>\n"
        )

    def select_all(self) -> None:
        total = len(self.screen.history.top) + self.rows
        self._sel_anchor, self._sel_end = (0, 0), (total - 1, self.cols - 1)
        self.viewport().update()

    # ---- geometry ---------------------------------------------------------

    def _apply_font(self) -> None:
        base = pick_terminal_font(self._font_size, self._font_family)
        self._fonts = {}
        for bold in (False, True):
            for italic in (False, True):
                f = QFont(base)
                f.setBold(bold)
                f.setItalic(italic)
                self._fonts[(bold, italic)] = f
        fm = QFontMetricsF(base, self.viewport())  # measured for the screen it is shown on
        self._cw = fm.horizontalAdvance("M")
        self._ch = math.ceil(fm.height())
        self._ascent = fm.ascent()
        self._recalc_size()
        self.viewport().update()

    def cell_size(self) -> tuple[float, int]:
        """Width and height of one character cell, in pixels."""
        return self._cw, self._ch

    def _recalc_size(self) -> None:
        vp = self.viewport().size()
        cols = max(20, int((vp.width() - 2 * PADDING) // self._cw))
        rows = max(4, int((vp.height() - 2 * PADDING) // self._ch))
        if (cols, rows) == (self.cols, self.rows):
            return
        bar = self.verticalScrollBar()
        at_bottom = bar.value() == bar.maximum()
        self.cols, self.rows = cols, rows
        self.screen.resize(rows, cols)
        self._clear_selection()
        self._sync_scrollbar(at_bottom)
        self.sizeChanged.emit(cols, rows)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._recalc_size()
        self._place_latest()

    def _update_latest(self) -> None:
        """Show the button that returns to the live end only while the output is scrolled back."""
        bar = self.verticalScrollBar()
        scrolled_back = bar.value() < bar.maximum()
        if scrolled_back == self._latest.isHidden():
            self._latest.setVisible(scrolled_back)
            self._place_latest()

    def _place_latest(self) -> None:
        button, view = self._latest, self.viewport()
        button.ensurePolished()
        button.adjustSize()
        button.move(view.width() - button.width() - 16, view.height() - button.height() - 12)
        button.raise_()

    def event(self, event):
        # Moved to a monitor with other scaling: measure the font again, as it is drawn there.
        if event.type() == QEvent.Type.DevicePixelRatioChange:
            self._apply_font()
        return super().event(event)

    def _sync_scrollbar(self, stick_to_bottom: bool) -> None:
        bar = self.verticalScrollBar()
        history = 0 if self.screen.in_alt_screen else len(self.screen.history.top)
        bar.setRange(0, history)
        bar.setPageStep(self.rows)
        if stick_to_bottom:
            bar.setValue(history)

    def _scroll_to_bottom(self) -> None:
        bar = self.verticalScrollBar()
        bar.setValue(bar.maximum())

    def _line(self, absline: int):
        history = self.screen.history.top
        if self.screen.in_alt_screen:
            return self.screen.buffer[absline]
        if absline < len(history):
            return history[absline]
        return self.screen.buffer[absline - len(history)]

    def _line_cells(self, absline: int) -> list[str]:
        """One string per column; the cell after a double-width character is empty."""
        return self._cells_of(self._line(absline))

    def _cells_of(self, line) -> list[str]:
        # A cell never written is the line's default. Asking for it with line[x] goes through a Python hook for each
        # of the blanks beside the text, which made a search of a long scrollback slow; get() does not.
        get = line.get
        default = getattr(line, "default", None)
        blank = default.data if default is not None else " "
        return [blank if (cell := get(x)) is None else (cell.data or "") for x in range(self.cols)]

    def _line_text(self, absline: int) -> str:
        history = 0 if self.screen.in_alt_screen else len(self.screen.history.top)
        return self._text_of(self._line(absline), absline < history)

    def _text_of(self, line, in_history: bool) -> str:
        """The text of a line. A line in the scrollback never changes, so its text is made once and kept with it,
        which is what keeps searching a long scrollback quick on every key typed into the find box."""
        if in_history:
            kept = getattr(line, "_text", None)
            if kept is not None and kept[0] == self.cols:  # made for another width: made again
                return kept[1]
        text = "".join(self._cells_of(line))
        if in_history:
            try:
                line._text = (self.cols, text)
            except AttributeError:
                pass  # a line that cannot carry it is simply made each time
        return text

    def _lines(self):
        """(absolute line number, the line, whether it is in the scrollback) for every line, in order.

        The scrollback is walked, not indexed: a deque is slow to index in the middle, and the screen is asked for
        its attributes once, not once for each of a hundred thousand lines.
        """
        screen = self.screen
        history = () if screen.in_alt_screen else screen.history.top
        number = -1
        for number, line in enumerate(history):
            yield number, line, True
        buffer = screen.buffer
        for row in range(self.rows):
            yield number + 1 + row, buffer[row], False

    def _total_lines(self) -> int:
        return (0 if self.screen.in_alt_screen else len(self.screen.history.top)) + self.rows

    # ---- find ---------------------------------------------------------------

    def find(self, text: str, case_sensitive: bool = False, regex: bool = False) -> int:
        """Highlight every occurrence in scrollback and screen; returns how many there are.

        With `regex` the text is a regular expression; an invalid one finds nothing and
        sets `find_error`. The active match starts as the last one at or above the bottom
        of the view, because recent output is what people usually search for.
        """
        self._find_query = (text, case_sensitive, regex)
        self._find_stale = False
        self._find_matches, self._find_rows, self._find_index = [], {}, -1
        self.find_error = ""
        pattern = None
        if text:
            try:
                pattern = re.compile(text if regex else re.escape(text), 0 if case_sensitive else re.IGNORECASE)
            except re.error as exc:
                self.find_error = f"Invalid pattern: {exc.msg}"
        if pattern is not None:
            for absline, line, in_history in self._lines():
                if not line:
                    continue  # an untouched row holds no text
                haystack = self._text_of(line, in_history)
                # Map string offsets back to columns: wide characters leave an empty cell. In plain ASCII every
                # character is one column, which is nearly every line, so nothing needs mapping.
                columns = range(len(haystack)) if haystack.isascii() else None
                for match in pattern.finditer(haystack):
                    if match.end() == match.start():
                        continue  # a pattern such as "x*" also matches nothing at all
                    if columns is None:
                        cells = self._cells_of(line)
                        columns = [col for col, cell in enumerate(cells) for _ in cell]
                    if match.start() < len(columns):
                        end = min(match.end(), len(columns)) - 1
                        self._find_matches.append((absline, columns[match.start()], columns[end]))
            for index, (absline, first, last) in enumerate(self._find_matches):
                self._find_rows.setdefault(absline, []).append((first, last, index))
            if self._find_matches:
                bottom = self.verticalScrollBar().value() + self.rows - 1
                at_or_above = [i for i, match in enumerate(self._find_matches) if match[0] <= bottom]
                self._find_index = at_or_above[-1] if at_or_above else 0
                self._show_find_match()
        self.viewport().update()
        self.verticalScrollBar().update()
        return len(self._find_matches)

    def find_step(self, forward: bool) -> None:
        if self._find_stale and self._find_query[0]:
            # Output arrived since the search; redo it but stay on the same match if possible.
            previous = self._find_matches[self._find_index] if self._find_index >= 0 else None
            self.find(*self._find_query)
            if previous in self._find_matches:
                self._find_index = self._find_matches.index(previous)
        if not self._find_matches:
            return
        self._find_index = (self._find_index + (1 if forward else -1)) % len(self._find_matches)
        self._show_find_match()
        self.viewport().update()
        self.verticalScrollBar().update()

    def find_status(self) -> tuple[int, int]:
        """(position of the active match counting from 1, number of matches)."""
        return self._find_index + 1, len(self._find_matches)

    def clear_find(self) -> None:
        self._find_query = ("", False, False)
        self._find_matches, self._find_rows, self._find_index = [], {}, -1
        self.viewport().update()
        self.verticalScrollBar().update()

    def find_marks(self) -> tuple[list[int], int | None]:
        """The lines that hold find matches, once each, and the line of the active match."""
        current = self._find_matches[self._find_index][0] if self._find_index >= 0 else None
        return sorted(self._find_rows), current

    def find_mark_colour(self, current: bool) -> QColor:
        return self._find_current if current else self._find_other

    def total_lines(self) -> int:
        """Lines of scrollback and screen together."""
        return self._total_lines()

    def _show_find_match(self) -> None:
        absline = self._find_matches[self._find_index][0]
        bar = self.verticalScrollBar()
        if not bar.value() <= absline < bar.value() + self.rows:
            bar.setValue(max(0, min(bar.maximum(), absline - self.rows // 2)))

    # ---- links --------------------------------------------------------------

    def link_at(self, absline: int, col: int) -> str | None:
        if not 0 <= absline < self._total_lines():
            return None
        cells = self._line_cells(absline)
        columns = [c for c, cell in enumerate(cells) for _ in cell]
        for match in _URL.finditer("".join(cells)):
            url = match.group().rstrip(".,;:!?)]}'\"")
            first, last = match.start(), match.start() + len(url) - 1
            if last < len(columns) and columns[first] <= col <= columns[last]:
                return url
        return None

    @staticmethod
    def open_link(url: str) -> None:
        QDesktopServices.openUrl(QUrl(url))

    def _cell_at(self, pos) -> tuple[int, int]:
        col = int((pos.x() - PADDING) // self._cw)
        row = int((pos.y() - PADDING) // self._ch)
        col = max(0, min(self.cols - 1, col))
        row = max(0, min(self.rows - 1, row))
        return self.verticalScrollBar().value() + row, col

    # ---- painting ---------------------------------------------------------

    def _highlight_colour(self, group: str) -> QColor:
        key = {"bad": "red", "warn": "amber", "good": "green"}.get(group, "accent")
        return QColor(COLORS[key])

    def _color(self, name: str, default: QColor) -> QColor:
        if name == "default":
            return default
        color = self._colors.get(name)
        if color is None:
            color = QColor(ANSI.get(name) or "#" + name)
            if not color.isValid():
                color = default
            self._colors[name] = color
        return color

    def paintEvent(self, event):
        p = QPainter(self.viewport())
        p.fillRect(event.rect(), self._bg)
        ch = self._ch
        top = self.verticalScrollBar().value()

        sel = None
        if self.has_selection():
            sel = sorted((self._sel_anchor, self._sel_end))

        for row in range(self.rows):
            absline = top + row
            line = self._line(absline)
            y = PADDING + row * ch

            sel_from = sel_to = -1
            if sel and sel[0][0] <= absline <= sel[1][0]:
                sel_from = sel[0][1] if absline == sel[0][0] else 0
                sel_to = sel[1][1] if absline == sel[1][0] else self.cols - 1
            found = self._find_rows.get(absline)
            marked = None
            if self._highlight is not None and line:
                # One character per column, so match positions are column numbers.
                row_text = "".join((line[x].data or " ")[0] for x in range(self.cols))
                for match in self._highlight.finditer(row_text):
                    marked = marked or {}
                    colour = self._highlight_colour(match.lastgroup)
                    for column in range(match.start(), match.end()):
                        marked[column] = colour

            # Cells are grouped into runs sharing one style so each run is a single draw call.
            run_start, run_text, run_style = 0, [], None
            for x in range(self.cols + 1):
                style = None
                data = ""
                if x < self.cols:
                    cell = line[x]
                    data = cell.data
                    fg = self._color(cell.fg, self._fg)
                    bg = self._color(cell.bg, self._bg)
                    if marked and x in marked and cell.fg == "default":
                        fg = marked[x]  # only plain text: a program's own colours are left alone
                    if cell.reverse:
                        fg, bg = bg, fg
                    if sel_from <= x <= sel_to:
                        bg = self._sel_bg
                    if found:
                        for first, last, index in found:
                            if first <= x <= last:
                                current = index == self._find_index
                                bg = self._find_current if current else self._find_other
                                fg = self._bg if current else self._fg
                                break
                    style = (fg.rgb(), bg.rgb(), cell.bold, cell.italics, cell.underscore, cell.strikethrough)
                    if not data:
                        # Second half of a double-width character: painted with that
                        # character, and it must not shift the text that follows.
                        style += (x, _STUB)
                    elif ord(data[0]) > 0x7E:
                        # Non-ASCII glyphs may come from a fallback font with a different
                        # advance, so each one gets its own run to stay on the cell grid.
                        wide = x + 1 < self.cols and not line[x + 1].data
                        style += (x, _WIDE if wide else _NARROW)
                if style != run_style:
                    if run_style is not None:
                        self._draw_run(p, run_start, x - run_start, y, "".join(run_text), run_style)
                    run_start, run_text, run_style = x, [], style
                run_text.append(data)

        self._draw_cursor(p, top)
        if time.monotonic() < self._flash_until:
            p.setPen(QPen(self._cursor_color, 3))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRect(self.viewport().rect().adjusted(1, 1, -2, -2))
        p.end()

    def _draw_run(self, p: QPainter, col: int, width: int, y: float, text: str, style) -> None:
        fg, bg, bold, italic, underline, strike = style[:6]
        kind = style[7] if len(style) > 7 else None
        if kind == _STUB:
            return
        if kind == _WIDE:
            width = 2
        x = PADDING + col * self._cw
        if bg != self._bg.rgb():
            p.fillRect(QRectF(x, y, width * self._cw, self._ch), QColor(bg))
        if not text.strip() and not underline and not strike:
            return
        p.setPen(QColor(fg))
        p.setFont(self._fonts[(bool(bold), bool(italic))])
        if kind == _WIDE:
            # Centre the glyph in its two cells whatever the fallback font's width is.
            p.drawText(QRectF(x, y, 2 * self._cw, self._ch), Qt.AlignmentFlag.AlignCenter, text)
        else:
            p.drawText(QPointF(x, y + self._ascent), text)
        if underline:
            ly = y + self._ascent + 2
            p.drawLine(QPointF(x, ly), QPointF(x + width * self._cw, ly))
        if strike:
            ly = y + self._ch / 2
            p.drawLine(QPointF(x, ly), QPointF(x + width * self._cw, ly))

    def _draw_cursor(self, p: QPainter, top: int) -> None:
        cursor = self.screen.cursor
        if cursor.hidden:
            return
        history = 0 if self.screen.in_alt_screen else len(self.screen.history.top)
        row = history + cursor.y - top
        if not 0 <= row < self.rows:
            return
        col = min(cursor.x, self.cols - 1)
        rect = QRectF(PADDING + col * self._cw, PADDING + row * self._ch, self._cw, self._ch)
        visible = self._blink_on or not self._cursor_blink
        if self._cursor_style != CURSOR_BLOCK:
            if self._cursor_style == CURSOR_BAR:
                shape = QRectF(rect.x(), rect.y(), 2, rect.height())
            else:
                shape = QRectF(rect.x(), rect.bottom() - 2, rect.width(), 2)
            if not self.hasFocus():
                dim = QColor(self._cursor_color)
                dim.setAlphaF(0.45)
                p.fillRect(shape, dim)
            elif visible:
                p.fillRect(shape, self._cursor_color)
        elif not self.hasFocus():
            p.setPen(self._cursor_color)
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRect(rect.adjusted(0.5, 0.5, -0.5, -0.5))
        elif visible:
            p.fillRect(rect, self._cursor_color)
            cell = self.screen.buffer[cursor.y][col]
            if cell.data.strip():
                p.setPen(self._bg)
                p.setFont(self._fonts[(bool(cell.bold), bool(cell.italics))])
                p.drawText(QPointF(rect.x(), rect.y() + self._ascent), cell.data)

    def _toggle_blink(self) -> None:
        self._blink_on = not self._blink_on
        if self.hasFocus():
            self.viewport().update()

    def focusInEvent(self, event):
        super().focusInEvent(event)
        self._blink_on = True
        self.viewport().update()

    def focusOutEvent(self, event):
        super().focusOutEvent(event)
        self.viewport().update()

    # ---- keyboard ---------------------------------------------------------

    def focusNextPrevChild(self, next_child: bool) -> bool:
        return False  # Tab belongs to the remote shell

    def keyPressEvent(self, event):
        key = event.key()
        mods = event.modifiers()
        ctrl = bool(mods & Qt.KeyboardModifier.ControlModifier)
        shift = bool(mods & Qt.KeyboardModifier.ShiftModifier)

        if key == K.Key_Escape and self._paste_queue:
            return self.cancel_paste()  # stop a slow paste; Esc reaches the remote side otherwise
        if ctrl and shift and key == K.Key_C:
            return self.copy()
        if (ctrl and shift and key == K.Key_V) or (shift and not ctrl and key == K.Key_Insert):
            return self.paste()
        if shift and not ctrl and key in (K.Key_Home, K.Key_End):
            bar = self.verticalScrollBar()
            return bar.setValue(bar.minimum() if key == K.Key_Home else bar.maximum())
        if shift and not ctrl and key in (K.Key_PageUp, K.Key_PageDown):
            bar = self.verticalScrollBar()
            step = self.rows - 1
            return bar.setValue(bar.value() + (step if key == K.Key_PageDown else -step))
        if ctrl and not shift:
            if key in (K.Key_Plus, K.Key_Equal):
                return self.set_font_size(self._font_size + 1)
            if key == K.Key_Minus:
                return self.set_font_size(self._font_size - 1)
            if key == K.Key_0:
                return self.set_font_size(DEFAULT_TERMINAL_SIZE)

        data = self._translate_key(event)
        if data is None:
            event.ignore()
            return
        self._blink_on = True
        self._scroll_to_bottom()
        self.send.emit(data)

    def _translate_key(self, event) -> bytes | None:
        key = event.key()
        mods = event.modifiers()
        ctrl = bool(mods & Qt.KeyboardModifier.ControlModifier)
        shift = bool(mods & Qt.KeyboardModifier.ShiftModifier)
        alt = bool(mods & Qt.KeyboardModifier.AltModifier)
        modifier = 1 + shift + 2 * alt + 4 * ctrl  # xterm modifier parameter

        if key in _CURSOR_KEYS:
            letter = _CURSOR_KEYS[key]
            if modifier > 1:
                return f"\x1b[1;{modifier}{letter}".encode()
            prefix = "\x1bO" if _MODE_APP_CURSOR in self.screen.mode else "\x1b["
            return (prefix + letter).encode()
        if key in _TILDE_KEYS:
            code = _TILDE_KEYS[key]
            return (f"\x1b[{code};{modifier}~" if modifier > 1 else f"\x1b[{code}~").encode()
        if key in _SS3_KEYS:
            letter = _SS3_KEYS[key]
            return (f"\x1b[1;{modifier}{letter}" if modifier > 1 else f"\x1bO{letter}").encode()

        esc = b"\x1b" if alt and not ctrl else b""
        if key == K.Key_Backspace:
            other = b"\x08" if self._backspace == b"\x7f" else b"\x7f"
            return esc + (other if ctrl else self._backspace)  # Ctrl gives the other of the two
        if key in (K.Key_Return, K.Key_Enter):
            return esc + self._enter
        if key == K.Key_Tab:
            return b"\t"
        if key == K.Key_Backtab:
            return b"\x1b[Z"
        if key == K.Key_Escape:
            return b"\x1b"
        if ctrl and not alt:
            if key == K.Key_Space or key == K.Key_At:
                return b"\x00"
            if K.Key_A <= key <= K.Key_Z:
                return bytes([key - K.Key_A + 1])
            if key in (K.Key_BracketLeft, K.Key_Backslash, K.Key_BracketRight):
                return bytes([key - K.Key_BracketLeft + 27])
            if key == K.Key_Underscore:
                return b"\x1f"

        text = event.text()
        if text:
            # Ctrl+Alt is AltGr on Windows keyboards; that text is a plain character.
            return esc + text.encode(self._encoding, "replace")
        return None

    # ---- mouse ------------------------------------------------------------

    def _clear_selection(self) -> None:
        self._sel_anchor = self._sel_end = None

    def _mouse_mode(self) -> int:
        """Which mouse reporting the remote program asked for (0 for none)."""
        mode = self.screen.mode
        for candidate in (_MODE_MOUSE_MOTION, _MODE_MOUSE_DRAG, _MODE_MOUSE_CLICK):
            if candidate in mode:
                return candidate
        return 0

    def _reports_mouse(self, event) -> bool:
        # Shift always gives the mouse back to local selection, as in other terminals.
        return bool(self._mouse_mode()) and not event.modifiers() & Qt.KeyboardModifier.ShiftModifier

    def _report_mouse(self, event, button: int, release: bool = False, motion: bool = False) -> None:
        absline, col = self._cell_at(event.position())
        row = absline - self.verticalScrollBar().value()
        mods = event.modifiers()
        code = button + (32 if motion else 0)
        code += 8 if mods & Qt.KeyboardModifier.AltModifier else 0
        code += 16 if mods & Qt.KeyboardModifier.ControlModifier else 0
        if _MODE_MOUSE_SGR in self.screen.mode:
            data = f"\x1b[<{code};{col + 1};{row + 1}{'m' if release else 'M'}".encode()
        else:
            if release:
                code = (code & ~3) | 3  # the classic encoding cannot say which button
            data = b"\x1b[M" + bytes([32 + code, 32 + min(col + 1, 223), 32 + min(row + 1, 223)])
        self._mouse_cell = (absline, col)
        self.send.emit(data)

    _MOUSE_BUTTONS = {Qt.MouseButton.LeftButton: 0, Qt.MouseButton.MiddleButton: 1, Qt.MouseButton.RightButton: 2}

    def mousePressEvent(self, event):
        if self._reports_mouse(event) and event.button() in self._MOUSE_BUTTONS:
            self._mouse_button = self._MOUSE_BUTTONS[event.button()]
            self._report_mouse(event, self._mouse_button)
            return
        if event.button() == Qt.MouseButton.LeftButton:
            if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
                url = self.link_at(*self._cell_at(event.position()))
                if url:
                    self.open_link(url)
                    return
            self._sel_anchor = self._sel_end = self._cell_at(event.position())
            self._selecting = True
            self.viewport().update()
        elif event.button() == Qt.MouseButton.MiddleButton:
            self.paste()

    def mouseMoveEvent(self, event):
        if self._selecting:
            self._sel_end = self._cell_at(event.position())
            self.viewport().update()
            return
        if self._reports_mouse(event):
            mode = self._mouse_mode()
            held = self._mouse_button is not None
            wanted = mode == _MODE_MOUSE_MOTION or (mode == _MODE_MOUSE_DRAG and held)
            if wanted and self._cell_at(event.position()) != self._mouse_cell:
                self._report_mouse(event, self._mouse_button if held else 3, motion=True)
            return
        # Ctrl turns links into something clickable; show that with the cursor.
        over_link = bool(
            event.modifiers() & Qt.KeyboardModifier.ControlModifier and self.link_at(*self._cell_at(event.position()))
        )
        self.viewport().setCursor(Qt.CursorShape.PointingHandCursor if over_link else Qt.CursorShape.IBeamCursor)

    def mouseReleaseEvent(self, event):
        if self._mouse_button is not None and not self._selecting:
            button, self._mouse_button = self._mouse_button, None
            if self._mouse_mode():
                self._report_mouse(event, button, release=True)
            return
        if event.button() == Qt.MouseButton.LeftButton and self._selecting:
            self._selecting = False
            if self.has_selection():
                if self._copy_on_select:
                    self.copy()  # selecting copies, so a paste is one click away
            else:
                self._clear_selection()
            self.viewport().update()

    def mouseDoubleClickEvent(self, event):
        if self._reports_mouse(event):
            return self.mousePressEvent(event)  # remote programs count the clicks themselves
        if event.button() != Qt.MouseButton.LeftButton:
            return
        absline, col = self._cell_at(event.position())
        cells = self._line_cells(absline)

        def blank(index: int) -> bool:
            return cells[index].isspace()  # an empty wide-character half is not a gap

        if blank(col):
            return
        start = end = col
        while start > 0 and not blank(start - 1):
            start -= 1
        while end < self.cols - 1 and not blank(end + 1):
            end += 1
        self._sel_anchor, self._sel_end = (absline, start), (absline, end)
        self._selecting = False
        if self._copy_on_select:
            self.copy()
        self.viewport().update()

    def wheelEvent(self, event):
        delta = event.angleDelta().y()
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            if delta:
                self.set_font_size(self._font_size + (1 if delta > 0 else -1))
            return
        if delta and self._reports_mouse(event):
            return self._report_mouse(event, 64 if delta > 0 else 65)
        if self.screen.in_alt_screen and delta:
            # No scrollback in full-screen apps; scroll them with arrow keys instead.
            prefix = b"\x1bO" if _MODE_APP_CURSOR in self.screen.mode else b"\x1b["
            self.send.emit((prefix + (b"A" if delta > 0 else b"B")) * 3)
            return
        super().wheelEvent(event)

    def contextMenuEvent(self, event):
        if self._mouse_mode() and not QGuiApplication.keyboardModifiers() & Qt.KeyboardModifier.ShiftModifier:
            return  # the right button belongs to the remote program; Shift+right-click opens this menu
        if self._right_click_paste and not QGuiApplication.keyboardModifiers() & Qt.KeyboardModifier.ControlModifier:
            return self.paste()  # Ctrl+right-click still opens the menu
        menu = QMenu(self)
        url = self.link_at(*self._cell_at(self.viewport().mapFromGlobal(event.globalPos())))
        if url:
            menu.addAction("Open Link", lambda: self.open_link(url))
            menu.addAction("Copy Link", lambda: QGuiApplication.clipboard().setText(url))
            menu.addSeparator()
        copy = menu.addAction("Copy\tCtrl+Shift+C", self.copy)
        copy.setEnabled(self.has_selection())
        menu.addAction("Paste\tCtrl+Shift+V", self.paste)
        menu.addAction("Select All", self.select_all)
        menu.addAction("Find...\tCtrl+Shift+F", self.findRequested.emit)
        menu.addSeparator()
        keep = menu.addAction("Save Selection as Saved Command...", lambda: self.saveCommandRequested.emit(self.selected_text()))
        keep.setEnabled(self.has_selection())
        menu.addAction("Save Output...", self.saveRequested.emit)
        menu.addAction("Clear Scrollback", self.clear_scrollback)
        menu.addAction("Reset Terminal", self.reset)
        menu.exec(event.globalPos())
