"""The server monitor's window: the last half hour of the active tab's server, one small chart per figure.

The charts share their time axis and nothing else: each figure has its own scale, never two on one axis. Each is
one line in the accent colour, with the current value written above it; a figure past its threshold says so in
words ("High", "Critical") beside a status dot, so colour is never the only sign. Pointing at a chart draws a
crosshair through all of them and tells the time and the value.
"""

import math
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime

from PyQt6.QtCore import QPointF, QRectF, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen
from PyQt6.QtWidgets import QDialog, QFrame, QHBoxLayout, QLabel, QToolTip, QVBoxLayout, QWidget

from .monitor import INTERVAL_MS, ServerStats, latency_level, level
from .theme import COLORS

HISTORY_LENGTH = 180  # readings kept per tab: half an hour at one every ten seconds
SPAN_SECONDS = HISTORY_LENGTH * INTERVAL_MS / 1000


@dataclass(frozen=True)
class Sample:
    when: float  # time.time()
    latency: float | None  # seconds
    stats: ServerStats | None


def new_history() -> deque:
    return deque(maxlen=HISTORY_LENGTH)


# key, title, how a value is written, the top of the scale (None: from the readings), the least top
METRICS = [
    ("latency", "Round trip", lambda value: f"{value:.0f} ms", None, 50.0),
    ("load", "CPU load", lambda value: f"{value:.2f}", None, 1.0),
    ("memory", "Memory in use", lambda value: f"{value:.0f}%", 100.0, 100.0),
    ("disk", "System disk", lambda value: f"{value:.0f}%", 100.0, 100.0),
]
LEVEL_WORDS = {"amber": "High", "red": "Critical"}


def nice_top(value: float) -> float:
    """The scale's top: the value rounded up to 1, 2, 2.5 or 5 times a power of ten, so its middle is round too."""
    if value <= 0:
        return 1.0
    power = 10 ** math.floor(math.log10(value))
    return next(step * power for step in (1, 2, 2.5, 5, 10) if step * power >= value)


def value_of(sample: Sample, key: str) -> float | None:
    if key == "latency":
        return None if sample.latency is None else sample.latency * 1000
    if sample.stats is None:
        return None
    return {"load": sample.stats.load, "memory": sample.stats.memory, "disk": sample.stats.disk}[key]


def level_of(sample: Sample, key: str) -> str:
    if key == "latency":
        return latency_level(sample.latency) if sample.latency is not None else ""
    return level(sample.stats, key) if sample.stats is not None else ""


class HistoryChart(QWidget):
    """One figure over the last half hour."""

    hovered = pyqtSignal(object)  # the time pointed at, or None

    LEFT, RIGHT, TOP, BOTTOM = 46, 12, 8, 8

    def __init__(self, key: str, parent=None):
        super().__init__(parent)
        self.key = key
        self._title, self._format, self._fixed_top, self._least_top = next(m[1:] for m in METRICS if m[0] == key)
        self.samples: list[Sample] = []
        self.now = time.time()
        self.pointed_at: float | None = None  # a time, shared by all the charts
        self.cpus = 1
        self.setMouseTracking(True)
        self.setMinimumHeight(70)

    def set_samples(self, samples: list[Sample], now: float) -> None:
        self.samples, self.now = samples, now
        self.cpus = next((s.stats.cpus for s in reversed(samples) if s.stats is not None), 1)
        self.update()

    def top(self) -> float:
        if self._fixed_top is not None:
            return self._fixed_top
        values = [v for s in self.samples if (v := value_of(s, self.key)) is not None]
        least = max(self._least_top, float(self.cpus)) if self.key == "load" else self._least_top
        return nice_top(max([least] + [v * 1.1 for v in values]))

    def _plot(self) -> QRectF:
        return QRectF(self.LEFT, self.TOP, max(1, self.width() - self.LEFT - self.RIGHT), max(1, self.height() - self.TOP - self.BOTTOM))

    def _x(self, when: float, plot: QRectF) -> float:
        return plot.right() - (self.now - when) / SPAN_SECONDS * plot.width()

    def _y(self, value: float, plot: QRectF, top: float) -> float:
        return plot.bottom() - min(value, top) / top * plot.height()

    def _nearest(self, when: float) -> Sample | None:
        known = [s for s in self.samples if value_of(s, self.key) is not None]
        return min(known, key=lambda s: abs(s.when - when)) if known else None

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        plot, top = self._plot(), self.top()
        small = QFont(self.font())
        small.setPointSizeF(self.font().pointSizeF() * 0.85)
        p.setFont(small)
        # Recessive hairline grid at the bottom, middle and top of the scale, labelled in the muted ink.
        for share in (0.0, 0.5, 1.0):
            y = round(plot.bottom() - share * plot.height()) + 0.5
            p.setPen(QPen(QColor(COLORS["border"]), 1))
            p.drawLine(QPointF(plot.left(), y), QPointF(plot.right(), y))
            p.setPen(QColor(COLORS["muted"]))
            p.drawText(QRectF(0, y - 9, self.LEFT - 8, 18), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, self._format(top * share))

        # The line, broken where a reading is missing, over a faint wash of its colour.
        accent = QColor(COLORS["accent"])
        runs, run = [], []
        for sample in self.samples:
            value = value_of(sample, self.key)
            if value is None:
                if run:
                    runs.append(run)
                run = []
                continue
            run.append(QPointF(self._x(sample.when, plot), self._y(value, plot, top)))
        if run:
            runs.append(run)
        wash = QColor(accent)
        wash.setAlphaF(0.10)
        for points in runs:
            if len(points) > 1:
                area = QPainterPath(QPointF(points[0].x(), plot.bottom()))
                for point in points:
                    area.lineTo(point)
                area.lineTo(QPointF(points[-1].x(), plot.bottom()))
                p.fillPath(area, wash)
                line = QPainterPath(points[0])
                for point in points[1:]:
                    line.lineTo(point)
                pen = QPen(accent, 2)
                pen.setCapStyle(Qt.PenCapStyle.RoundCap)
                pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
                p.setPen(pen)
                p.drawPath(line)
        if runs:
            self._dot(p, runs[-1][-1], accent)  # the latest reading

        if self.pointed_at is not None and (sample := self._nearest(self.pointed_at)) is not None:
            x = self._x(sample.when, plot)
            p.setPen(QPen(QColor(COLORS["border2"]), 1))
            p.drawLine(QPointF(x, plot.top()), QPointF(x, plot.bottom()))
            self._dot(p, QPointF(x, self._y(value_of(sample, self.key), plot, top)), accent)
        p.end()

    @staticmethod
    def _dot(p: QPainter, at: QPointF, color: QColor) -> None:
        p.setPen(QPen(QColor(COLORS["surface"]), 2))  # a ring of the card's colour, so it reads over the line
        p.setBrush(color)
        p.drawEllipse(at, 4.5, 4.5)

    def mouseMoveEvent(self, event):
        plot = self._plot()
        when = self.now - (plot.right() - event.position().x()) / plot.width() * SPAN_SECONDS
        sample = self._nearest(when)
        self.hovered.emit(sample.when if sample is not None else None)
        if sample is not None:
            stamp = datetime.fromtimestamp(sample.when).strftime("%H:%M:%S")
            QToolTip.showText(event.globalPosition().toPoint(), f"{stamp}\n{self._title} {self._format(value_of(sample, self.key))}", self)

    def leaveEvent(self, _event):
        self.hovered.emit(None)
        QToolTip.hideText()


class ServerMonitorWindow(QDialog):
    """The figures of the active tab's server over the last half hour; it follows the tab in front."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Server Monitor")
        self.setMinimumSize(460, 520)
        self.resize(560, 600)
        self.heading = QLabel()
        self.heading.setObjectName("MonitorHeading")
        self.heading.setTextFormat(Qt.TextFormat.PlainText)  # a session's name can come from an imported file
        self.empty = QLabel(
            "No figures yet. They are read every ten seconds while an SSH tab is connected and in front, "
            "and the server monitor is on in Settings, Behaviour."
        )
        self.empty.setObjectName("Muted")
        self.empty.setWordWrap(True)
        self.charts: list[HistoryChart] = []
        self.values: dict[str, tuple[QLabel, QLabel]] = {}
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 14)
        layout.setSpacing(10)
        layout.addWidget(self.heading)
        layout.addWidget(self.empty)
        for key, title, *_rest in METRICS:
            card = QFrame()
            card.setObjectName("Card")
            column = QVBoxLayout(card)
            column.setContentsMargins(14, 10, 14, 8)
            column.setSpacing(4)
            header = QHBoxLayout()
            name = QLabel(title)
            name.setObjectName("SectionTitle")
            value = QLabel()
            value.setObjectName("MonitorValue")
            state = QLabel()
            state.setTextFormat(Qt.TextFormat.RichText)
            header.addWidget(name)
            header.addStretch(1)
            header.addWidget(state)
            header.addWidget(value)
            chart = HistoryChart(key)
            chart.hovered.connect(self._point_at)
            column.addLayout(header)
            column.addWidget(chart, 1)
            layout.addWidget(card, 1)
            self.charts.append(chart)
            self.values[key] = (value, state)
        note = QLabel(f"The last {SPAN_SECONDS / 60:.0f} minutes, a reading every {INTERVAL_MS // 1000} seconds.")
        note.setObjectName("Muted")
        layout.addWidget(note)

    def show_tab(self, tab) -> None:
        samples = list(getattr(tab, "monitor_history", [])) if tab is not None else []
        self.heading.setText(f"{tab.title}  ·  {tab.session.address}" if tab is not None else "No tab")
        self.empty.setVisible(not samples)
        now = time.time()
        for chart in self.charts:
            chart.set_samples(samples, now)
        latest = samples[-1] if samples else None
        for key, (value, state) in self.values.items():
            reading = value_of(latest, key) if latest is not None else None
            value.setText(next(m[2] for m in METRICS if m[0] == key)(reading) if reading is not None else "–")
            color = level_of(latest, key) if latest is not None else ""
            # The state in words beside a dot of its colour: never colour alone.
            state.setText(f'<span style="color:{COLORS[color]}">●</span> {LEVEL_WORDS[color]}&nbsp;&nbsp;' if color else "")

    def _point_at(self, when) -> None:
        for chart in self.charts:
            chart.pointed_at = when
            chart.update()
