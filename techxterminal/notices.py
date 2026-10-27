"""Desktop notifications: a message from the notification area, for news that should reach someone who is not
looking at the window, such as a scheduled task that failed.

The icon in the notification area is there only while a message is fresh, so the application does not keep a
permanent place there.
"""

from PyQt6.QtCore import QObject, QTimer
from PyQt6.QtWidgets import QSystemTrayIcon

from . import APP_NAME
from .theme import app_icon

SHOW_MS = 10_000  # how long the message is shown
KEEP_ICON_MS = 60_000  # the icon stays this long after, so the message can still be clicked in the Action Center


class DesktopNotifier(QObject):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._tray: QSystemTrayIcon | None = None
        self._on_click = None
        self._hide_timer = QTimer(self)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.timeout.connect(self._hide)
        self.shown: list[tuple[str, str]] = []  # (title, message) of every message, newest last

    @staticmethod
    def available() -> bool:
        return QSystemTrayIcon.isSystemTrayAvailable()

    def show(self, title: str, message: str, on_click=None) -> bool:
        """Show a message; `on_click` runs if it is clicked. False when the desktop has nowhere to show it."""
        self.shown.append((title, message))
        if not self.available():
            return False
        if self._tray is None:
            self._tray = QSystemTrayIcon(app_icon(), self)
            self._tray.setToolTip(APP_NAME)
            self._tray.messageClicked.connect(self._clicked)
        self._on_click = on_click
        self._tray.show()
        self._tray.showMessage(title, message, app_icon(), SHOW_MS)
        self._hide_timer.start(KEEP_ICON_MS)
        return True

    def _clicked(self) -> None:
        callback, self._on_click = self._on_click, None
        if callback is not None:
            callback()

    def _hide(self) -> None:
        if self._tray is not None:
            self._tray.hide()

    def close(self) -> None:
        self._hide_timer.stop()
        self._hide()
