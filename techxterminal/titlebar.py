"""Title bars in the theme's colours, so that a window's title bar and the top of its content read as one.

Windows 11 lets a window choose the colours of its caption, caption text and border. The main window's title
bar takes the colour of the top bar beneath it, and other windows take their background. Elsewhere (Windows 10,
other systems) nothing is changed, and the system draws its own title bar, dark or light as the theme asks.
"""

import sys

from PyQt6.QtCore import QEvent, QObject, Qt
from PyQt6.QtWidgets import QApplication, QMainWindow, QWidget

from .theme import COLORS

DWMWA_BORDER_COLOR = 34
DWMWA_CAPTION_COLOR = 35
DWMWA_TEXT_COLOR = 36


def _colorref(color: str) -> int:
    """'#rrggbb' as the 0x00bbggrr that Windows takes."""
    red, green, blue = (int(color[i : i + 2], 16) for i in (1, 3, 5))
    return red | green << 8 | blue << 16


def title_bar_colors(window: QWidget) -> tuple[str, str, str]:
    """(caption, text, border) for a window in the current theme."""
    caption = COLORS["surface"] if isinstance(window, QMainWindow) else COLORS["bg"]
    return caption, COLORS["text"], COLORS["border"]


def paint_title_bar(window: QWidget) -> None:
    caption, text, border = title_bar_colors(window)
    window.setProperty("titleBarColor", caption)  # what was asked for, for anyone checking
    if sys.platform != "win32":
        return
    import ctypes

    hwnd = ctypes.c_void_p(int(window.winId()))
    for attribute, color in ((DWMWA_CAPTION_COLOR, caption), (DWMWA_TEXT_COLOR, text), (DWMWA_BORDER_COLOR, border)):
        value = ctypes.c_uint(_colorref(color))
        # Windows 10 does not know these attributes and returns an error, which leaves its own colours.
        ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, attribute, ctypes.byref(value), ctypes.sizeof(value))


def _has_title_bar(widget) -> bool:
    return (
        isinstance(widget, QWidget)
        and widget.isWindow()
        and widget.windowType() in (Qt.WindowType.Window, Qt.WindowType.Dialog)
        and not widget.windowFlags() & Qt.WindowType.FramelessWindowHint
    )


class _TitleBarPainter(QObject):
    """Paints each window's title bar as it is shown, and again when the theme changes."""

    def eventFilter(self, obj, event):
        kind = event.type()
        # A theme change reaches windows not shown yet too; those are left until they are, because asking for
        # the native window of one would create it before its layout has settled.
        if kind in (QEvent.Type.Show, QEvent.Type.PaletteChange) and _has_title_bar(obj):
            if kind == QEvent.Type.Show or obj.isVisible():
                paint_title_bar(obj)
        return False


def install(app: QApplication) -> None:
    painter = app._title_bar_painter = _TitleBarPainter(app)  # kept on the app, which owns it
    app.installEventFilter(painter)
    # Off again before the program winds down: Python may free the painter while Qt is still closing windows,
    # and an event handed to it then crashes the program on its way out.
    app.aboutToQuit.connect(lambda: app.removeEventFilter(painter))
