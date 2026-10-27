"""Window sizes that suit the screen they open on: a small laptop, a large monitor, a portrait one."""

from PyQt6.QtCore import QRect
from PyQt6.QtGui import QGuiApplication

# The share of the screen a window may take when it first opens; the rest stays visible around it.
SCREEN_SHARE = 0.92


def available(widget=None) -> QRect:
    """The usable area (without the taskbar) of the screen a widget is on, or of the primary screen."""
    screen = None
    if widget is not None:
        anchor = widget.parentWidget() or widget
        screen = anchor.screen() if anchor.isVisible() else None
    screen = screen or QGuiApplication.primaryScreen()
    return screen.availableGeometry() if screen else QRect(0, 0, 1280, 720)


def fit_to_screen(widget, width: int, height: int, area: QRect | None = None) -> None:
    """Resize a window to the size it would like, but never past the screen, and centre it.

    A dialog is centred over its parent window; anything else over the screen.
    """
    area = area or available(widget)
    width = max(widget.minimumWidth(), min(width, int(area.width() * SCREEN_SHARE)))
    height = max(widget.minimumHeight(), min(height, int(area.height() * SCREEN_SHARE)))
    widget.resize(width, height)
    parent = widget.parentWidget()
    on_parent = parent is not None and parent.isVisible() and area.contains(parent.window().frameGeometry().center())
    centre = parent.window().frameGeometry().center() if on_parent else area.center()
    frame = widget.frameGeometry()
    frame.moveCenter(centre)
    # Keep the title bar on the screen, so the window can always be moved.
    frame.moveLeft(max(area.left(), min(frame.left(), area.right() - frame.width())))
    frame.moveTop(max(area.top(), min(frame.top(), area.bottom() - frame.height())))
    widget.move(frame.topLeft())


def _overlap(area: QRect, frame: QRect) -> int:
    shared = area.intersected(frame)
    return shared.width() * shared.height() if shared.isValid() else 0


def keep_on_screen(widget) -> bool:
    """After restoring a saved position: bring back a window that is off every screen or too big.

    That happens when it was last used on a monitor that is gone, or on a larger one.
    Returns whether the window had to be moved or resized.
    """
    frame = widget.frameGeometry()
    areas = [screen.availableGeometry() for screen in QGuiApplication.screens()]
    if not areas:
        return False
    area = max(areas, key=lambda candidate: _overlap(candidate, frame))
    visible = area.intersected(frame)
    fits = frame.width() <= area.width() and frame.height() <= area.height()
    # Enough of the window, its title bar included, must be on the screen to take hold of it.
    reachable = visible.width() >= min(200, frame.width()) and visible.height() >= 40 and frame.top() >= area.top() - 4
    if fits and reachable:
        return False
    fit_to_screen(widget, min(widget.width(), area.width()), min(widget.height(), area.height()), area)
    return True
