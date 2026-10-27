"""Painting for the session list: two-line rows with a live status dot, and folder headers."""

from PyQt6.QtCore import QEvent, QRectF, QSize, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QColor, QCursor, QFont, QFontMetricsF, QPainter
from PyQt6.QtWidgets import QAbstractItemView, QStyle, QStyledItemDelegate, QToolTip, QTreeWidget

from .theme import COLORS, icon_pixmap, session_avatar

ROLE_SESSION_ID = Qt.ItemDataRole.UserRole
ROLE_SUBTITLE = Qt.ItemDataRole.UserRole + 1
ROLE_IS_GROUP = Qt.ItemDataRole.UserRole + 2
ROLE_COLOR = Qt.ItemDataRole.UserRole + 3
ROLE_FAVOURITE = Qt.ItemDataRole.UserRole + 4
ROLE_PINNED = Qt.ItemDataRole.UserRole + 5  # the session opens its tab pinned

PIN_WIDTH = 16  # room for the pinned diamond beside the star

# The heading favourites are listed under. The star keeps it apart from a folder of that name.
FAVOURITES = "\u2605 Favourites"

# Palette keys, looked up when painting so a theme switch takes effect at once.
_STATE_COLOR_KEYS = {"connected": "green", "connecting": "amber"}


AVATAR_SIZE = 30
STAR_WIDTH = 22


class SessionDelegate(QStyledItemDelegate):
    def __init__(self, tree, state_for):
        super().__init__(tree)
        self.tree = tree
        self.state_for = state_for  # session id -> "connected" / "connecting" / None
        self.reachable_for = lambda _session_id: None  # session id -> True / False / None (not tested)
        self.toggle_favourite = None  # session id -> None, for a click on a row's star
        self._after_double_click = False
        self.retheme()

    def retheme(self) -> None:
        self._avatars: dict[tuple[str, str, str], object] = {}  # drawn in the theme's colours, so drawn again
        self._chevrons = {
            True: icon_pixmap("chevron-down", COLORS["muted"], 48),
            False: icon_pixmap("chevron-right", COLORS["muted"], 48),
        }

    @staticmethod
    def _small(font: QFont) -> QFont:
        small = QFont(font)
        small.setPointSizeF(font.pointSizeF() * 0.88)
        return small

    def sizeHint(self, option, index):
        line = option.fontMetrics.height()
        if index.data(ROLE_IS_GROUP):
            return QSize(80, line + 18)
        return QSize(80, int(line * 1.88) + 20)

    def paint(self, p: QPainter, option, index):
        p.save()
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(option.rect).adjusted(2, 2, -2, -2)
        if index.data(ROLE_IS_GROUP):
            self._paint_group(p, rect, option, index)
        else:
            self._paint_session(p, rect, option, index)
        p.restore()

    def _paint_group(self, p, rect, option, index):
        chevron = self._chevrons[self.tree.isExpanded(index)]
        size = 12
        p.drawPixmap(
            QRectF(rect.left() + 6, rect.center().y() - size / 2 + 2, size, size), chevron, QRectF(chevron.rect())
        )
        font = self._small(option.font)
        font.setBold(True)
        font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 1.2)
        p.setFont(font)
        p.setPen(QColor(COLORS["muted"]))
        text_rect = rect.adjusted(26, 4, -8, 0)
        count = str(self.tree.model().rowCount(index))
        p.drawText(text_rect, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, str(index.data()).upper())
        p.drawText(text_rect, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, count)

    @staticmethod
    def _star_rect(row) -> QRectF:
        """Where a session row's favourite star is: at its right end."""
        row = QRectF(row).adjusted(2, 2, -2, -2)
        return QRectF(row.right() - STAR_WIDTH - 4, row.top(), STAR_WIDTH, row.height())

    def editorEvent(self, event, model, option, index):
        # A click on the star changes the favourite, and does not select or open the session as well.
        kinds = (QEvent.Type.MouseButtonPress, QEvent.Type.MouseButtonRelease, QEvent.Type.MouseButtonDblClick)
        if (
            event.type() in kinds
            and event.button() == Qt.MouseButton.LeftButton
            and not index.data(ROLE_IS_GROUP)
            and self._star_rect(option.rect).contains(event.position())
        ):
            # A double click is press, release, double click, release: only its first release counts.
            if event.type() == QEvent.Type.MouseButtonPress:
                self._after_double_click = False  # whatever came before, this is a new click
            elif event.type() == QEvent.Type.MouseButtonDblClick:
                self._after_double_click = True
            elif event.type() == QEvent.Type.MouseButtonRelease:
                if self._after_double_click:
                    self._after_double_click = False
                elif self.toggle_favourite is not None:
                    # Once the click is over: the list is rebuilt for it, which must not happen under the
                    # view's feet while it is still handling the mouse.
                    session_id = index.data(ROLE_SESSION_ID)
                    QTimer.singleShot(0, lambda: self.toggle_favourite(session_id))
            return True
        return super().editorEvent(event, model, option, index)

    def helpEvent(self, event, view, option, index):
        if index.isValid() and not index.data(ROLE_IS_GROUP) and self._star_rect(option.rect).contains(event.pos().toPointF()):
            favourite = index.data(ROLE_FAVOURITE)
            QToolTip.showText(event.globalPos(), "Remove from Favourites" if favourite else "Add to Favourites", view)
            return True
        return super().helpEvent(event, view, option, index)

    def _paint_session(self, p, rect, option, index):
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        hovered = bool(option.state & QStyle.StateFlag.State_MouseOver)
        if selected or hovered:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(COLORS["select" if selected else "hover"]))
            p.drawRoundedRect(rect, 8, 8)

        # The same tile as on the rail the sidebar collapses to: initials in the label colour, and the dot of
        # the connection state on its corner while a tab of it is open.
        state_key = _STATE_COLOR_KEYS.get(self.state_for(index.data(ROLE_SESSION_ID)))
        key = (str(index.data()), index.data(ROLE_COLOR) or "", COLORS[state_key] if state_key else "")
        if key not in self._avatars:
            self._avatars[key] = session_avatar(*key).pixmap(96, 96)
        size = AVATAR_SIZE
        tile = QRectF(rect.left() + 6, rect.center().y() - size / 2, size, size)
        p.drawPixmap(tile, self._avatars[key], QRectF(self._avatars[key].rect()))

        # The favourite star: filled for a favourite, an outline to click while the row is pointed at.
        favourite = bool(index.data(ROLE_FAVOURITE))
        if favourite or hovered:
            star = self._star_rect(option.rect)
            on_star = star.contains(self.tree.viewport().mapFromGlobal(QCursor.pos()).toPointF())
            p.setFont(option.font)
            p.setPen(QColor(COLORS["amber"] if favourite or on_star else COLORS["muted"]))
            p.drawText(star, Qt.AlignmentFlag.AlignCenter, "\u2605" if favourite else "\u2606")
        marks_right = STAR_WIDTH + 8
        if index.data(ROLE_PINNED):
            # A small diamond, as on a pinned tab, left of the star: this session opens its tab pinned.
            p.setFont(self._small(option.font))
            p.setPen(QColor(COLORS["accent"]))
            p.drawText(
                rect.adjusted(0, 0, -marks_right, 0),
                Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                "◆",
            )
            marks_right += PIN_WIDTH
        reachable = self.reachable_for(index.data(ROLE_SESSION_ID))
        if reachable is not None:
            # The result of the last connection check: a tick if the server answered.
            p.setFont(option.font)
            p.setPen(QColor(COLORS["green" if reachable else "red"]))
            p.drawText(
                rect.adjusted(0, 0, -marks_right, 0),
                Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                "\u2713" if reachable else "\u2717",
            )
        title_font = QFont(option.font)
        sub_font = self._small(option.font)
        title_fm, sub_fm = QFontMetricsF(title_font), QFontMetricsF(sub_font)
        text = rect.adjusted(AVATAR_SIZE + 16, 0, -marks_right, 0)
        top = rect.center().y() - (title_fm.height() + sub_fm.height() + 1) / 2
        align = Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter

        p.setFont(title_font)
        p.setPen(QColor(COLORS["text"]))
        title = title_fm.elidedText(str(index.data()), Qt.TextElideMode.ElideRight, text.width())
        p.drawText(QRectF(text.left(), top, text.width(), title_fm.height()), align, title)

        p.setFont(sub_font)
        p.setPen(QColor(COLORS["muted"]))
        subtitle = sub_fm.elidedText(str(index.data(ROLE_SUBTITLE) or ""), Qt.TextElideMode.ElideMiddle, text.width())
        p.drawText(QRectF(text.left(), top + title_fm.height() + 1, text.width(), sub_fm.height()), align, subtitle)


class SessionTree(QTreeWidget):
    """The session list. Dragging a session onto a folder, or onto a session in it, moves it there."""

    sessionDropped = pyqtSignal(str, str)  # session id, folder name ("" for no folder)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.setDropIndicatorShown(True)
        self.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)

    def folder_of(self, item) -> str:
        """The folder a drop on this row means: the folder itself, or the one the session is in."""
        if item is None:
            return ""
        if item.data(0, ROLE_IS_GROUP):
            return item.text(0)
        parent = item.parent()
        return parent.text(0) if parent is not None else ""

    def dropEvent(self, event):
        dragged = self.currentItem()
        session_id = dragged.data(0, ROLE_SESSION_ID) if dragged is not None else None
        target = self.itemAt(event.position().toPoint())
        # The rows themselves are rebuilt from the saved sessions, so Qt must not move anything.
        event.ignore()
        if session_id:
            self.sessionDropped.emit(session_id, self.folder_of(target))
