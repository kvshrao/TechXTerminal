"""The server monitor: how busy the server of the active tab is, and how far away, in the status bar.

Every so often the window asks the server of the tab in front, on a channel of its own beside the terminal, for
its load, memory and disk use. Linux servers have all of it in /proc; a server that cannot answer (another
system, or a device with no shell) is left alone after a couple of tries. The round trip to the server is
measured with a keep-alive request, which every SSH server answers. Nothing is shown in the terminal.
"""

import re
from dataclasses import dataclass

from PyQt6.QtCore import QThread, pyqtSignal

INTERVAL_MS = 10_000  # the tab in front
BACKGROUND_INTERVAL_MS = 60_000  # the other connected tabs, for the alerts
# Read-only, and quick: the load, the number of processors, memory, and the disk the system is on.
STATS_COMMAND = (
    "cat /proc/loadavg; nproc 2>/dev/null || getconf _NPROCESSORS_ONLN; "
    "grep -E '^(MemTotal|MemAvailable):' /proc/meminfo; df -Pk / | tail -n 1"
)
GIVE_UP_AFTER = 2  # failed tries before a server is taken not to have the figures


@dataclass(frozen=True)
class ServerStats:
    load: float  # processes running or waiting, averaged over the last minute
    cpus: int
    memory: int  # per cent in use
    disk: int  # per cent of the system disk in use

    @property
    def load_share(self) -> float:
        """The load per processor: 1.0 is every processor busy."""
        return self.load / max(1, self.cpus)


def parse_stats(text: str) -> ServerStats | None:
    """The figures from what STATS_COMMAND printed, or None if it did not print them all."""
    load = re.search(r"^\s*(\d+(?:\.\d+)?)\s+\d+(?:\.\d+)?\s+\d+(?:\.\d+)?\s", text, re.MULTILINE)
    cpus = re.search(r"^\s*(\d+)\s*$", text, re.MULTILINE)
    total = re.search(r"^MemTotal:\s+(\d+)", text, re.MULTILINE)
    available = re.search(r"^MemAvailable:\s+(\d+)", text, re.MULTILINE)
    disk = re.search(r"\s(\d+)%\s+/\s*$", text, re.MULTILINE)
    if not (load and cpus and total and available and disk) or int(total.group(1)) == 0:
        return None
    memory = round(100 * (1 - int(available.group(1)) / int(total.group(1))))
    return ServerStats(float(load.group(1)), int(cpus.group(1)), max(0, min(100, memory)), int(disk.group(1)))


def level(stats: ServerStats, figure: str) -> str:
    """"", "amber" or "red": how worrying a figure is, for its colour."""
    value, warn, alert = {
        "load": (stats.load_share, 0.8, 1.5),
        "memory": (stats.memory, 80, 92),
        "disk": (stats.disk, 85, 95),
    }[figure]
    return "red" if value >= alert else "amber" if value >= warn else ""


def latency_level(seconds: float) -> str:
    """"", "amber" or "red" for a round trip: typing starts to feel slow past about 150 ms."""
    return "red" if seconds >= 0.4 else "amber" if seconds >= 0.15 else ""


def critical(stats: ServerStats | None, latency: float | None) -> dict[str, str]:
    """The figures that are critical now, each with words for an alert: {"disk": "System disk 96% full"}."""
    found = {}
    if latency is not None and latency_level(latency) == "red":
        found["latency"] = f"Round trip {latency * 1000:.0f} ms"
    if stats is not None:
        if level(stats, "load") == "red":
            found["load"] = f"Load {stats.load:.2f} on {stats.cpus} processor{'' if stats.cpus == 1 else 's'}"
        if level(stats, "memory") == "red":
            found["memory"] = f"Memory {stats.memory}% in use"
        if level(stats, "disk") == "red":
            found["disk"] = f"System disk {stats.disk}% full"
    return found


def summary(stats: ServerStats | None, latency: float | None) -> str:
    lines = []
    if latency is not None:
        lines.append(f"Round trip {latency * 1000:.0f} ms")
    if stats is not None:
        lines += [
            f"Load {stats.load:.2f} on {stats.cpus} processor{'' if stats.cpus == 1 else 's'}",
            f"Memory {stats.memory}% in use",
            f"System disk {stats.disk}% full",
        ]
    lines.append(f"Updated every {INTERVAL_MS // 1000} seconds; Settings, Behaviour turns this off")
    return "\n".join(lines)


class StatsProbe(QThread):
    """Measures one server, away from the window's thread. `done` gives the tab, the figures (None when the
    server did not give them, or was not asked) and the round trip in seconds (None if it was not measured)."""

    done = pyqtSignal(object, object, object)

    def __init__(self, tab, run_command, round_trip, parent=None):
        super().__init__(parent)
        self.tab = tab
        self._run_command = run_command  # None not to ask for the figures
        self._round_trip = round_trip  # None not to measure the round trip
        self.asks_for_figures = run_command is not None

    def run(self) -> None:
        latency = stats = None
        # A connection that dropped, a refused channel or a timeout gives no figure, not an error.
        if self._round_trip is not None:
            try:
                latency = self._round_trip()
            except Exception:  # noqa: BLE001
                latency = None
        if self._run_command is not None:
            try:
                stats = parse_stats(self._run_command(STATS_COMMAND))
            except Exception:  # noqa: BLE001
                stats = None
        self.done.emit(self.tab, stats, latency)
