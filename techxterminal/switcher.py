"""Quick switcher: type a few letters to jump to a tab, open a session, run a command or send a saved one."""

import re

from PyQt6.QtCore import QRectF, QSize, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QFontMetricsF, QPainter
from PyQt6.QtWidgets import (
    QDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QStyle,
    QStyledItemDelegate,
    QVBoxLayout,
)

from .theme import COLORS

KIND_TAB = "tab"
KIND_PINNED = "pinned"  # a tab too; the pinned ones are listed first, under this name
KIND_SESSION = "session"
KIND_ACTION = "action"
KIND_SNIPPET = "snippet"
KIND_WORKSPACE = "workspace"

_PREFIXES = {KIND_TAB: "Tab", KIND_PINNED: "Pinned", KIND_SESSION: "Open", KIND_ACTION: "Run", KIND_SNIPPET: "Send", KIND_WORKSPACE: "Workspace"}

ROLE_KIND = Qt.ItemDataRole.UserRole
ROLE_DETAIL = Qt.ItemDataRole.UserRole + 1

_WORD = re.compile(r"[a-z0-9]+")


def _rank(title: str, words: list[str]) -> int:
    """0 when the title starts with what was typed, 1 when every word starts a word of it, else 2."""
    title = title.lower()
    if title.startswith(" ".join(words)):
        return 0
    starts = _WORD.findall(title)
    if all(any(part.startswith(word) for part in starts) for word in words):
        return 1
    return 2


class _RowDelegate(QStyledItemDelegate):
    """One row: what choosing it does, the name, and either details or a shortcut on the right."""

    BADGE_WIDTH = 78

    def sizeHint(self, option, index):
        return QSize(120, int(option.fontMetrics.height()) + 16)

    def paint(self, p: QPainter, option, index):
        p.save()
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(option.rect).adjusted(1, 1, -1, -1)
        if option.state & QStyle.StateFlag.State_Selected:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(COLORS["select"]))
            p.drawRoundedRect(rect, 6, 6)
        elif option.state & QStyle.StateFlag.State_MouseOver:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(COLORS["hover"]))
            p.drawRoundedRect(rect, 6, 6)

        kind = index.data(ROLE_KIND)
        detail = index.data(ROLE_DETAIL) or ""
        align = Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
        small = QFont(option.font)
        small.setPointSizeF(option.font.pointSizeF() * 0.88)
        inner = rect.adjusted(10, 0, -10, 0)

        p.setFont(small)
        p.setPen(QColor(COLORS["accent" if kind in (KIND_TAB, KIND_PINNED, KIND_SESSION) else "muted"]))
        p.drawText(QRectF(inner.left(), inner.top(), self.BADGE_WIDTH, inner.height()), align, _PREFIXES[kind])

        # A command's shortcut sits on the right as a key cap; other details follow the name.
        right = inner.right()
        if kind == KIND_ACTION and detail:
            metrics = QFontMetricsF(small)
            cap = QRectF(0, 0, metrics.horizontalAdvance(detail) + 14, metrics.height() + 4)
            cap.moveCenter(inner.center())
            cap.moveRight(inner.right())
            p.setPen(QColor(COLORS["border2"]))
            p.setBrush(QColor(COLORS["surface2"]))
            p.drawRoundedRect(cap, 4, 4)
            p.setPen(QColor(COLORS["muted"]))
            p.drawText(cap, Qt.AlignmentFlag.AlignCenter, detail)
            right = cap.left() - 12
            detail = ""

        title_left = inner.left() + self.BADGE_WIDTH
        title_metrics = QFontMetricsF(option.font)
        space = right - title_left
        title = title_metrics.elidedText(str(index.data()), Qt.TextElideMode.ElideRight, space)
        p.setFont(option.font)
        p.setPen(QColor(COLORS["text"]))
        p.drawText(QRectF(title_left, inner.top(), space, inner.height()), align, title)
        if detail:
            left = title_left + title_metrics.horizontalAdvance(title) + 14
            p.setFont(small)
            p.setPen(QColor(COLORS["muted"]))
            rest = QFontMetricsF(small).elidedText(detail, Qt.TextElideMode.ElideMiddle, max(0.0, right - left))
            p.drawText(QRectF(left, inner.top(), max(0.0, right - left), inner.height()), align, rest)
        p.restore()


class QuickSwitcher(QDialog):
    """`entries` are (kind, target, title, detail); `chosen` reports the kind and target picked."""

    chosen = pyqtSignal(str, object)

    def __init__(self, entries: list[tuple[str, object, str, str]], parent=None):
        super().__init__(parent, Qt.WindowType.Popup | Qt.WindowType.FramelessWindowHint)
        self.setObjectName("Switcher")
        self._entries = entries
        self._shown: list[tuple[str, object, str, str]] = []

        self.query = QLineEdit()
        self.query.setPlaceholderText("Go to a tab, open a session, run a command or send a saved one...")
        self.query.textChanged.connect(self._filter)
        self.query.returnPressed.connect(lambda: self.activate(self.list.currentRow()))
        self.list = QListWidget()
        self.list.setObjectName("SwitcherList")
        self.list.setItemDelegate(_RowDelegate(self.list))
        self.list.setMouseTracking(True)
        self.list.setUniformItemSizes(True)
        self.list.itemClicked.connect(lambda item: self.activate(self.list.row(item)))
        self.empty = QLabel("Nothing matches. Try fewer letters.")
        self.empty.setObjectName("SwitcherEmpty")
        self.empty.setAlignment(Qt.AlignmentFlag.AlignCenter)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(8)
        layout.addWidget(self.query)
        layout.addWidget(self.empty)
        layout.addWidget(self.list, 1)
        self.resize(600, 400)
        self._filter("")

    def _filter(self, text: str) -> None:
        words = text.lower().split()
        matching = [
            entry for entry in self._entries if all(word in f"{entry[2]} {entry[3]}".lower() for word in words)
        ]
        # Better matches first; among equals the order stays: tabs, sessions, commands, the rest.
        self._shown = sorted(matching, key=lambda entry: _rank(entry[2], words)) if words else matching
        self.list.clear()
        for kind, _target, title, detail in self._shown:
            item = QListWidgetItem(title)
            item.setData(ROLE_KIND, kind)
            item.setData(ROLE_DETAIL, detail)
            item.setToolTip(detail)
            self.list.addItem(item)
        self.list.setCurrentRow(0 if self._shown else -1)
        self.empty.setVisible(not self._shown)

    def shown(self) -> list[tuple[str, str]]:
        """(kind, title) of the rows currently listed, top to bottom."""
        return [(entry[0], entry[2]) for entry in self._shown]

    def activate(self, row: int) -> None:
        if 0 <= row < len(self._shown):
            kind, target = self._shown[row][0], self._shown[row][1]
            self.accept()
            self.chosen.emit(kind, target)

    def keyPressEvent(self, event):
        # The arrow keys move through the list while the typing stays in the search box; they wrap
        # around at the ends. Page Up and Page Down move a screenful, and stop at the ends.
        count = self.list.count()
        step = {Qt.Key.Key_Down: 1, Qt.Key.Key_Up: -1}.get(event.key())
        if step is not None and count:
            self.list.setCurrentRow((self.list.currentRow() + step) % count)
            return
        page = max(1, self.list.viewport().height() // max(1, self.list.sizeHintForRow(0)) - 1)
        jump = {Qt.Key.Key_PageDown: page, Qt.Key.Key_PageUp: -page}.get(event.key())
        if jump is not None and count:
            self.list.setCurrentRow(max(0, min(count - 1, self.list.currentRow() + jump)))
            return
        super().keyPressEvent(event)
