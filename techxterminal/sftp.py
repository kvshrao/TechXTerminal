"""File browser for SSH sessions: this computer on the left, the remote host on the right."""

import itertools
import os
import posixpath
import queue
import shutil
import stat
import tempfile
import threading
import time
from pathlib import Path
from collections import deque
from datetime import datetime
from functools import lru_cache

from PyQt6.QtCore import QDir, QFile, QItemSelectionModel, QProcess, QRectF, QSize, Qt, QThread, QTimer, QUrl, pyqtSignal
from PyQt6.QtGui import (
    QColor,
    QDesktopServices,
    QFileSystemModel,
    QIcon,
    QKeySequence,
    QPainter,
    QPainterPath,
    QPen,
    QShortcut,
)
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QSplitter,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QTabWidget,
    QToolButton,
    QTreeView,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from . import threads
from .preferences import MAX_TRANSFERS, Preferences, open_settings
from .screens import fit_to_screen, keep_on_screen
from . import foldersync
from .theme import COLORS, icon_pixmap, line_icon
from .untrusted import is_inside, literal, local_name, opens_by_running

ROLE_IS_DIR = Qt.ItemDataRole.UserRole
ROLE_SIZE = Qt.ItemDataRole.UserRole + 1
ROLE_MODIFIED = Qt.ItemDataRole.UserRole + 2
ROLE_MODE = Qt.ItemDataRole.UserRole + 3
ROLE_PATH = Qt.ItemDataRole.UserRole + 4  # remote folder tree: the folder's full path
ROLE_FILLED = Qt.ItemDataRole.UserRole + 5  # remote folder tree: its subfolders have been listed
ROLE_PROGRESS = Qt.ItemDataRole.UserRole + 6  # transfer queue: per cent done while the transfer runs

REMOTE_COLUMNS = ["Name", "Size", "Type", "Modified", "Permissions", "Owner"]
LOCAL_HEADINGS = {0: "Name", 1: "Size", 2: "Type", 3: "Modified"}
QUEUE_COLUMNS = ["Local file", "Direction", "Remote file", "Size", "Status"]
UPLOAD_ARROW, DOWNLOAD_ARROW = "-->>", "<<--"

LOG_LENGTH = 200  # lines kept in the transfer log

CHUNK_BYTES = 32 * 1024
# With no speed limit, this much is requested at a time; the requests travel together, which
# is far faster than asking for one chunk and waiting for it.
WINDOW_BYTES = 1024 * 1024
# A transfer is written under this name and renamed when complete, so a file that was only
# partly transferred is never mistaken for a whole one, and can be continued later.
PART_SUFFIX = ".part"
# Before continuing a partial file, this much of its end is compared with the source.
VERIFY_BYTES = 64 * 1024
# An upload is held back by the server's window: OpenSSH lets about 2 MB be on the way per channel, so one
# channel never moves more than 2 MB per round trip. A large file therefore goes in parts over several
# channels at once, each with a window of its own: about four times as fast to a server 50 ms away.
PARALLEL_FROM = 16 * 1024 * 1024
PARALLEL_STREAMS = 4
# OpenSSH takes write requests of up to 256 KB, including their header; fewer, larger requests are faster.
# Other servers are only sure to take the 32 KB the protocol promises.
LARGE_REQUEST_BYTES = 252 * 1024
# A download asks for this much at a time with no speed limit (one channel), or per part (several); the
# answers come while the next request is on its way. Channels are opened with a window that takes it all.
DOWNLOAD_BATCH_BYTES = 8 * 1024 * 1024
PART_BATCH_BYTES = 4 * 1024 * 1024
# Speed limits offered, in kilobytes per second; 0 means unlimited.
SPEED_PRESETS = [0, 64, 256, 1024, 5 * 1024, 20 * 1024]
STACK_BELOW_WIDTH = 760  # a narrower window shows the two sides one above the other
CARD_GAP = 8  # the space around and between the two sides and the queue
QUEUE_ROWS_SHOWN = 5  # queued transfers that the queue makes room for, without scrolling
MIN_PANES_HEIGHT = 180  # the file lists keep at least this much when the queue grows


def human_size(size: float) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{int(value)} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return ""


def speed_label(kbps: int) -> str:
    if kbps <= 0:
        return "Unlimited"
    return f"{kbps} KB/s" if kbps < 1024 else f"{kbps / 1024:g} MB/s"


def retry_if_locked(action, attempts: int = 15, pause: float = 0.2):
    """Run a local file operation, trying again if another program has the file open.

    On Windows a virus scanner or the search indexer often holds a file for a moment just
    after it was written; opening or renaming it then fails although nothing is wrong.
    """
    for attempt in range(attempts):
        try:
            return action()
        except PermissionError:
            if attempt == attempts - 1:
                raise
            time.sleep(pause)


def clock(seconds: float) -> str:
    """A length of time as the progress line writes it: 00:01:05."""
    seconds = max(0, int(seconds))
    return f"{seconds // 3600:02d}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"


def rate_text(rate: float) -> str:
    """A transfer speed such as 22.1 MiB/s."""
    if rate >= 1024 * 1024:
        return f"{rate / (1024 * 1024):.1f} MiB/s"
    if rate >= 1024:
        return f"{rate / 1024:.1f} KiB/s"
    return f"{int(rate)} B/s"


_SIZE_ALIGNMENT = Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
DATE_FORMAT = "%Y-%m-%d %H:%M"  # both sides of the file browser show times like this


@lru_cache(maxsize=8192)
def format_time(seconds: float) -> str:
    """A modification time as the file lists show it. Files saved together share one, so it is cached."""
    return datetime.fromtimestamp(seconds).strftime(DATE_FORMAT)


def file_type(name: str, is_dir: bool) -> str:
    """What a remote entry is, from its name: "Folder", "TXT file" or "File"."""
    if is_dir:
        return "Folder"
    stem, extension = posixpath.splitext(name)
    return f"{extension[1:].upper()} file" if extension and stem else "File"


# Kinds of file told apart by the colour of their icon, from the extension. Anything else is a plain file.
FILE_KINDS = {
    "archive": ("amber", {"zip", "tar", "gz", "tgz", "bz2", "xz", "7z", "rar", "zst", "deb", "rpm"}),
    "image": ("violet", {"png", "jpg", "jpeg", "gif", "bmp", "svg", "webp", "ico", "tif", "tiff"}),
    "code": ("green", {
        "py", "sh", "bash", "ps1", "bat", "cmd", "js", "ts", "go", "rs", "c", "h", "cpp", "java", "rb", "php",
        "pl", "sql", "json", "yaml", "yml", "toml", "xml", "html", "css", "conf", "cfg", "ini",
    }),
    "document": ("red", {"pdf", "doc", "docx", "xls", "xlsx", "ppt", "pptx", "odt", "ods", "md", "csv"}),
}


def file_kind(name: str) -> str:
    """Which of FILE_KINDS a file is by its extension, or "" for none of them."""
    stem, extension = posixpath.splitext(name.lower())
    extension = extension[1:] if stem else ""
    return next((kind for kind, (_color, extensions) in FILE_KINDS.items() if extension in extensions), "")


class RemoteRow:
    """One entry of the remote folder as listed. Its list item is only made once it is shown."""

    __slots__ = ("entry", "lower", "item")

    def __init__(self, entry: tuple) -> None:
        self.entry = entry  # name, is_dir, size, modified, mode, owner
        self.lower = entry[0].lower()
        self.item: QTreeWidgetItem | None = None


_SORT_KEYS = {
    1: lambda row: row.entry[2] or 0,
    2: lambda row: file_type(row.entry[0], row.entry[1]).lower(),
    3: lambda row: row.entry[3] or 0,
    4: lambda row: row.entry[4] or 0,
    5: lambda row: row.entry[5].lower(),
}


def sort_remote_rows(rows: list, column: int, ascending: bool) -> list:
    """The rows ordered by a column, folders always on top, equal values in name order.

    Qt would sort the list by calling back into Python for every comparison, which takes seconds
    in a folder of tens of thousands of files, so the browser orders its rows itself.
    """
    folders: list = []
    files: list = []
    for row in sorted(rows, key=lambda row: row.lower):
        (folders if row.entry[1] else files).append(row)
    key = _SORT_KEYS.get(column)
    for group in (folders, files):
        if key:
            group.sort(key=key, reverse=not ascending)  # a reversed sort still keeps ties in name order
        elif not ascending:
            group.reverse()
    return folders + files


class RemoteTree(QTreeWidget):
    """The remote list. The header only says how to sort; `FileBrowser` does the sorting itself."""

    def __init__(self) -> None:
        super().__init__()
        header = self.header()
        header.setSortIndicatorShown(True)
        header.setSectionsClickable(True)
        header.setSortIndicator(0, Qt.SortOrder.AscendingOrder)

    def sortByColumn(self, column: int, order: Qt.SortOrder = Qt.SortOrder.AscendingOrder) -> None:  # noqa: N802
        self.header().setSortIndicator(column, order)  # the browser listens and sorts


class QueueList(QTreeWidget):
    """One list of the transfer queue. While it is empty it says what would appear in it."""

    def __init__(self, empty_text: str) -> None:
        super().__init__()
        self.empty_text = empty_text

    def paintEvent(self, event):
        super().paintEvent(event)
        if self.topLevelItemCount():
            return
        painter = QPainter(self.viewport())
        painter.setPen(QColor(COLORS["muted"]))
        area = self.viewport().rect().adjusted(16, 0, -16, 0)
        painter.drawText(area, Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap, self.empty_text)


def summary_text(files: int, folders: int, size: int, selected: bool = False) -> str:
    """The line under each side: "3 files and 2 folders. Total size: 404,663 bytes"."""
    if not files and not folders:
        return "Nothing selected." if selected else "Empty folder."
    parts = []
    if files:
        parts.append(f"{files} file{'' if files == 1 else 's'}")
    if folders:
        parts.append(f"{folders} folder{'' if folders == 1 else 's'}")
    text = ("Selected " if selected else "") + " and ".join(parts) + "."
    return f"{text} Total size: {size:,} bytes" if files else text


class LocalFileModel(QFileSystemModel):
    """This computer's files, shown the way the remote side shows its own: same sizes, dates and icons."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.icon_for = None  # (name, is_dir) -> icon; set by the window, in the theme's colours

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        column = index.column()
        if role == Qt.ItemDataRole.DisplayRole and column == 1:
            return "" if self.isDir(index) else human_size(self.size(index))
        if role == Qt.ItemDataRole.DisplayRole and column == 2:
            return file_type(self.fileName(index), self.isDir(index))  # worded as on the remote side
        if role == Qt.ItemDataRole.DisplayRole and column == 3:
            modified = self.lastModified(index)
            return modified.toPyDateTime().strftime(DATE_FORMAT) if modified.isValid() else ""
        if role == Qt.ItemDataRole.DecorationRole and column == 0 and self.icon_for is not None:
            return self.icon_for(self.fileName(index), self.isDir(index))
        if role == Qt.ItemDataRole.TextAlignmentRole and column == 1:
            return Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        return super().data(index, role)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if role == Qt.ItemDataRole.DisplayRole and orientation == Qt.Orientation.Horizontal and section in LOCAL_HEADINGS:
            return LOCAL_HEADINGS[section]
        if role == Qt.ItemDataRole.TextAlignmentRole and orientation == Qt.Orientation.Horizontal and section == 1:
            return _SIZE_ALIGNMENT  # over the sizes, which are right-aligned
        return super().headerData(section, orientation, role)


class TransferCancelled(Exception):
    """The transfer was stopped. `keep_partial` is set when it may be continued later."""

    def __init__(self, keep_partial: bool = False):
        super().__init__()
        self.keep_partial = keep_partial


class _Job:
    """A queued transfer: what it does, and whether it has been cancelled."""

    def __init__(self, number: int, action, directory: str | None = None, targets: tuple[str, ...] = ()):
        self.number = number
        self.action = action  # called with the _Transfer that runs it
        self.targets = targets  # what it writes ("remote:/a/b" or "local:c:/a/b"); one that overlaps a running transfer waits
        self.directory = directory  # the remote folder it sends into, if any: listed again when the last one is done
        self.settled = False  # it no longer counts towards the transfers still to come into that folder
        self.cancelled = False


class _Transfer:
    """One transfer while it runs. It has a channel of its own, so others can run beside it."""

    def __init__(self, worker: "SftpWorker", job: _Job, sftp):
        self.worker = worker
        self.job = job
        self.sftp = sftp
        self.changed = self.skipped = 0  # names from the server made usable here, and ones that could not be

    def copy(self, fetch, store, label: str, total: int, start: int = 0, step: int = WINDOW_BYTES) -> None:
        """Move a file's bytes from `fetch(offset, wanted)` to `store(data)`, beginning at `start`.

        Reports progress and obeys pause, the speed limit, cancel and stop. With no speed limit it moves `step`
        bytes at a time.
        """
        worker, job = self.worker, self.job
        done = start
        began = last_report = time.monotonic()
        # The limiter compares bytes sent with time passed since this mark; the mark moves
        # whenever the limit changes or a pause ends, so both take effect at once.
        mark_time, mark_bytes, mark_limit = began, done, worker.share()
        while done < total:
            if worker.is_paused():
                pause_began = time.monotonic()
                while worker.is_paused() and not job.cancelled and not worker.stopping:
                    time.sleep(0.1)
                held = time.monotonic() - pause_began
                began += held  # time spent paused does not count towards the speed shown
                mark_time, mark_bytes = time.monotonic(), done
            if worker.stopping:
                raise TransferCancelled(keep_partial=True)
            if job.cancelled:
                raise TransferCancelled()
            limit = worker.share()
            data = fetch(done, min(total - done, CHUNK_BYTES if limit else step))
            if not data:
                break  # the source turned out shorter than announced
            store(data)
            done += len(data)
            now = time.monotonic()
            if limit != mark_limit:
                mark_time, mark_bytes, mark_limit = now, done, limit
            if limit:
                ahead = (done - mark_bytes) / limit - (now - mark_time)
                while ahead > 0 and not job.cancelled and not worker.stopping and not worker.is_paused() and worker.share() == limit:
                    time.sleep(min(ahead, 0.1))  # short naps, so cancel, pause and limit changes are noticed
                    ahead = (done - mark_bytes) / limit - (time.monotonic() - mark_time)
                now = time.monotonic()
            if now - last_report > 0.1:  # do not flood the UI with signals
                last_report = now
                worker.progress.emit(job.number, label, done, total, (done - start) / max(now - began, 1e-6))
        worker.progress.emit(job.number, label, done, max(total, done), (done - start) / max(time.monotonic() - began, 1e-6))

    def announce_resume(self, label: str, start: int, total: int) -> None:
        if start:
            self.worker.resumed.emit(label, int(start * 100 / max(total, 1)))

    def replace_remote(self, part: str, final: str) -> None:
        """Give a finished upload its real name, replacing a file that is already there."""
        try:
            self.sftp.posix_rename(part, final)  # one step, where the server offers it
            return
        except OSError:
            pass
        try:
            self.sftp.remove(final)
        except OSError:
            pass  # normally: there was nothing to replace
        self.sftp.rename(part, final)

    @staticmethod
    def tune(target, sftp) -> None:
        """Make a remote file being written as fast as its server allows."""
        target.set_pipelined(True)  # do not wait for each chunk to be acknowledged
        transport = sftp.get_channel().get_transport()
        if "OpenSSH" in (transport.remote_version or ""):
            target.MAX_REQUEST_SIZE = LARGE_REQUEST_BYTES

    def put(self, local: str, remote: str) -> None:
        label, size = os.path.basename(local), os.path.getsize(local)
        part = remote + PART_SUFFIX
        with open(local, "rb") as source:
            start = self.upload_resume_point(source, part, size) if self.worker.resume else 0
            self.announce_resume(label, start, size)
            try:
                # In parts at once when the file is large and starts afresh; a speed limit keeps it to one
                # channel, so that the limit is kept to.
                if start == 0 and size >= PARALLEL_FROM and not self.worker.share():
                    self.put_parallel(local, part, label, size)
                else:
                    with self.sftp.open(part, "r+b" if start else "wb") as target:
                        self.tune(target, self.sftp)
                        source.seek(start)
                        target.seek(start)
                        self.copy(lambda _offset, wanted: source.read(wanted), target.write, label, size, start)
            except TransferCancelled as stopped:
                if not stopped.keep_partial:
                    try:
                        self.sftp.remove(part)
                    except OSError:
                        pass
                raise
        self.replace_remote(part, remote)
        if self.worker.keep_dates:
            try:
                # Keep the file's own date, so sorting by date still means something on the server.
                modified = os.path.getmtime(local)
                self.sftp.utime(remote, (modified, modified))
            except OSError:
                pass  # not every server allows it; the transfer itself succeeded

    def in_parts(self, label: str, size: int, move, cut_back) -> None:
        """Move a large file in parts at once, each over a channel of its own: as many as the server gives.

        `move(sftp, begin, end, advance)` moves the bytes from `begin` to `end` over one channel, calling
        `advance(position)` before each piece, and stopping when it returns False. Stopped part way, the
        partial file is cut back by `cut_back(complete)` to what is complete from its start, so that it can be
        continued like any other: the parts after a gap would otherwise pass for the file's end.
        """
        worker, job = self.worker, self.job
        channels = [self.sftp]
        for _ in range(PARALLEL_STREAMS - 1):
            try:
                channels.append(worker.open_channel())
            except Exception:  # noqa: BLE001 - the server allows no more at once: the file goes with what there is
                break
        share = -(-size // len(channels))
        share += -share % CHUNK_BYTES  # every part starts on a chunk boundary
        ranges = [(begin, min(begin + share, size)) for begin in range(0, size, share)]
        moved = [0] * len(ranges)  # bytes of each part that have arrived
        failures: list[BaseException] = []
        lock = threading.Lock()

        def run(index: int) -> None:
            begin, end = ranges[index]

            def advance(position: int) -> bool:
                with lock:
                    moved[index] = position - begin
                while worker.is_paused() and not job.cancelled and not worker.stopping:
                    time.sleep(0.1)
                return not (failures or job.cancelled or worker.stopping)

            try:
                move(channels[index], begin, end, advance)
            except BaseException as exc:  # noqa: BLE001 - handed to the transfer's own thread, which raises it
                with lock:
                    failures.append(exc)

        movers = [threading.Thread(target=run, args=(index,), daemon=True) for index in range(len(ranges))]
        began = time.monotonic()
        paused_for = 0.0
        for mover in movers:
            mover.start()
        try:
            while any(mover.is_alive() for mover in movers):
                time.sleep(0.1)
                if worker.is_paused():
                    paused_for += 0.1  # time spent paused does not count towards the speed shown
                with lock:
                    done = sum(moved)
                worker.progress.emit(job.number, label, done, size, done / max(time.monotonic() - began - paused_for, 1e-6))
        finally:
            for mover in movers:
                mover.join()
            for extra in channels[1:]:
                try:
                    extra.close()
                except Exception:  # noqa: BLE001
                    pass
        if failures:
            raise failures[0]
        if job.cancelled:
            raise TransferCancelled()
        if worker.stopping and sum(moved) < size:  # stopped too late to matter, it is simply finished
            complete = 0  # how much is there without a gap, from the start
            for (begin, end), done in zip(ranges, moved):
                complete = begin + done
                if complete < end:
                    break
            try:
                cut_back(complete)
            except OSError:
                raise TransferCancelled() from None  # it cannot be continued, so it is not kept
            raise TransferCancelled(keep_partial=True)
        worker.progress.emit(job.number, label, size, size, size / max(time.monotonic() - began - paused_for, 1e-6))

    def put_parallel(self, local: str, part: str, label: str, size: int) -> None:
        with self.sftp.open(part, "wb"):  # made, and emptied, before the parts are written into it
            pass

        def move(sftp, begin: int, end: int, advance) -> None:
            with open(local, "rb") as source, sftp.open(part, "r+b") as target:
                self.tune(target, sftp)
                source.seek(begin)
                target.seek(begin)
                position = begin
                while position < end and advance(position):
                    data = source.read(min(WINDOW_BYTES, end - position))
                    if not data:
                        raise OSError(f"{label} became shorter while it was being sent")
                    target.write(data)
                    position += len(data)
            advance(position)  # once the file is closed, and so every piece acknowledged

        self.in_parts(label, size, move, lambda complete: self.sftp.truncate(part, complete))

    def get_parallel(self, remote: str, part: str, label: str, size: int) -> None:
        with retry_if_locked(lambda: open(part, "wb")):  # made, and emptied, before the parts are written into it
            pass

        def move(sftp, begin: int, end: int, advance) -> None:
            with sftp.open(remote, "rb") as source, open(part, "r+b") as target:
                target.seek(begin)
                position = begin
                while position < end and advance(position):
                    wanted = min(PART_BATCH_BYTES, end - position)
                    pieces = [(at, min(CHUNK_BYTES, position + wanted - at)) for at in range(position, position + wanted, CHUNK_BYTES)]
                    for data in source.readv(pieces):
                        target.write(data)
                    position += wanted
            advance(position)

        self.in_parts(label, size, move, lambda complete: os.truncate(part, complete))

    def upload_resume_point(self, source, part: str, size: int) -> int:
        """How much of a local file is already on the server as a partial upload (0 to start over)."""
        try:
            have = self.sftp.stat(part).st_size or 0
        except OSError:
            return 0
        if not 0 < have <= size:
            return 0
        check = min(VERIFY_BYTES, have)
        try:
            with self.sftp.open(part, "rb") as existing:
                existing.seek(have - check)
                remote_end = existing.read(check)
        except OSError:
            return 0
        source.seek(have - check)
        # Only continue if the partial file really is the beginning of this file.
        return have if source.read(check) == remote_end else 0

    def get(self, remote: str, local: str, size: int | None = None, modified: float | None = None) -> None:
        if size is None or modified is None:
            attributes = self.sftp.stat(remote)
            size, modified = attributes.st_size or 0, attributes.st_mtime
        label = posixpath.basename(remote)
        part = local + PART_SUFFIX
        with self.sftp.open(remote, "rb") as source:
            start = self.download_resume_point(source, part, size) if self.worker.resume else 0
            self.announce_resume(label, start, size)

            def fetch(offset: int, wanted: int) -> bytes:
                if wanted <= CHUNK_BYTES:
                    source.seek(offset)
                    return source.read(wanted)
                # Ask for a whole window at once; the pieces come back in order.
                pieces = [(position, min(CHUNK_BYTES, offset + wanted - position)) for position in range(offset, offset + wanted, CHUNK_BYTES)]
                return b"".join(source.readv(pieces))

            try:
                # As for uploads: in parts at once when large and starting afresh, unless there is a speed limit.
                if start == 0 and size >= PARALLEL_FROM and not self.worker.share():
                    self.get_parallel(remote, part, label, size)
                else:
                    with retry_if_locked(lambda: open(part, "r+b" if start else "wb")) as target:
                        target.seek(start)
                        target.truncate()
                        self.copy(fetch, target.write, label, size, start, step=DOWNLOAD_BATCH_BYTES)
            except TransferCancelled as stopped:
                if not stopped.keep_partial:
                    try:
                        os.remove(part)
                    except OSError:
                        pass
                raise
        retry_if_locked(lambda: os.replace(part, local))
        if modified and self.worker.keep_dates:
            try:
                os.utime(local, (modified, modified))  # the downloaded copy keeps the remote file's date
            except OSError:
                pass

    def download_resume_point(self, source, part: str, size: int) -> int:
        """How much of a remote file is already here as a partial download (0 to start over)."""
        try:
            have = os.path.getsize(part)
        except OSError:
            return 0
        if not 0 < have <= size:
            return 0
        check = min(VERIFY_BYTES, have)
        try:
            with open(part, "rb") as existing:
                existing.seek(have - check)
                local_end = existing.read(check)
        except OSError:
            return 0
        source.seek(have - check)
        return have if source.read(check) == local_end else 0

    def upload(self, local_paths: list[str], remote_dir: str) -> None:
        count = 0
        try:
            for local in local_paths:
                name = os.path.basename(local.rstrip("/\\"))
                if os.path.isdir(local):
                    count += self.upload_tree(local, posixpath.join(remote_dir, name))
                else:
                    self.put(local, posixpath.join(remote_dir, name))
                    count += 1
        finally:
            self.worker.list_after(self.job, self.sftp)
        # After the listing, which would otherwise replace this message with an item count.
        self.worker.finished_job.emit(f"Uploaded {count} file{'' if count == 1 else 's'}")

    def upload_tree(self, local_dir: str, remote_dir: str) -> int:
        count = 0
        for root, _dirs, files in os.walk(local_dir):
            relative = os.path.relpath(root, local_dir).replace(os.sep, "/")
            target = remote_dir if relative == "." else posixpath.join(remote_dir, relative)
            try:
                self.sftp.mkdir(target)
            except OSError:
                pass  # already there
            for name in files:
                self.put(os.path.join(root, name), posixpath.join(target, name))
                count += 1
        return count

    def download(self, remote_path: str, local_path: str) -> None:
        name = posixpath.basename(remote_path.rstrip("/"))
        attributes = self.sftp.stat(remote_path)
        if stat.S_ISDIR(attributes.st_mode or 0):
            self.changed = self.skipped = 0
            count = self.download_tree(remote_path, local_path)
            note = ""
            if self.changed or self.skipped:
                note = f" ({self.changed} renamed and {self.skipped} left out: the server's names cannot be file names here)"
            self.worker.finished_job.emit(f"Downloaded {name}: {count} file{'' if count == 1 else 's'}{note}")
        else:
            self.get(remote_path, local_path, attributes.st_size or 0, attributes.st_mtime)
            self.worker.finished_job.emit(f"Downloaded {name}")
            self.worker.downloaded.emit(remote_path, local_path)

    def save_back(self, local_path: str, remote_path: str) -> None:
        self.put(local_path, remote_path)
        self.worker.finished_job.emit(f"Saved {posixpath.basename(remote_path)} to the server")

    def download_tree(self, remote_dir: str, local_dir: str) -> int:
        os.makedirs(local_dir, exist_ok=True)
        count = 0
        for attr in self.sftp.listdir_attr(remote_dir):
            # The server chooses these names. One like "..\\..\\x" must not put a file outside the folder.
            name = local_name(attr.filename)
            local = os.path.join(local_dir, name) if name else ""
            if not name or not is_inside(local_dir, local):
                self.skipped += 1
                continue
            if name != attr.filename:
                self.changed += 1
            remote = posixpath.join(remote_dir, attr.filename)
            mode = attr.st_mode or 0
            if stat.S_ISDIR(mode):
                count += self.download_tree(remote, local)
            elif stat.S_ISLNK(mode):
                continue  # links are skipped: following them could loop or leave the folder
            else:
                self.get(remote, local, attr.st_size or 0, attr.st_mtime)
                count += 1
        return count


class SftpWorker(QThread):
    """Runs the remote file operations off the UI thread.

    Listing, making folders, deleting and the like run one after another on a channel of their own.
    Transfers run beside them, up to `set_parallel` at a time, each on a further channel, so a long
    transfer does not keep the file list from answering and several can go together.
    """

    listed = pyqtSignal(str, list)  # directory, [(name, is_dir, size, modified, mode, owner)]
    folders_listed = pyqtSignal(str, list)  # directory, names of its folders: for the folder tree
    job_started = pyqtSignal(int)  # a queued transfer has begun
    job_finished = pyqtSignal(int, bool, str)  # transfer, whether it succeeded, why not
    progress = pyqtSignal(int, str, "qlonglong", "qlonglong", float)  # transfer, what, bytes done, bytes in total, bytes per second
    finished_job = pyqtSignal(str)  # message for the status line
    failed = pyqtSignal(str)
    compared = pyqtSignal(str, str, list)  # local folder, remote folder, [foldersync.Difference]
    busy = pyqtSignal(bool)  # at least one transfer is running
    waiting = pyqtSignal(int)  # transfers queued, not yet started
    downloaded = pyqtSignal(str, str)  # remote file, local copy: one file has arrived
    resumed = pyqtSignal(str, int)  # file name, per cent already there: a partial file is continued
    paused = pyqtSignal(bool)
    measured = pyqtSignal(str, "qlonglong", int, int, bool)  # folder, bytes, files, folders, whether all of it was counted

    def __init__(self, opener, parent=None):
        super().__init__(parent)
        self._opener = opener  # returns a connected paramiko.SFTPClient; each call is a new channel
        self._jobs: queue.Queue = queue.Queue()  # what is not a transfer: list, make a folder, delete ...
        self._sftp = None
        self._job_ids = itertools.count(1)
        self._limit = 0  # bytes per second for all transfers together, 0 for unlimited
        self.keep_dates = True
        self.resume = True
        # Transfers. The condition guards what follows it, and is also how idle transfer threads
        # are woken; it is re-entrant because a signal sent from the UI thread can reach a slot
        # that asks for the waiting count right away.
        self._cond = threading.Condition(threading.RLock())
        self._pending: deque[_Job] = deque()
        self._running: dict[int, _Job] = {}
        self._into: dict[str, int] = {}  # remote folder -> transfers into it that are queued or running
        self._list_lock = threading.Lock()  # listings are made one at a time, so the newest is always the last sent
        self._shown: str | None = None  # the folder of the listing sent last
        self._lanes: list[threading.Thread] = []
        self._channels = 0  # transfer channels that are open
        self._hopeful = 0  # transfer threads that have not given up opening a channel
        self._channel_cap = MAX_TRANSFERS  # lowered if the server turns out to allow fewer
        self._parallel = 1
        self._paused = False
        self._stopping = False  # the window is closing: stop, but keep partial files
        self._closing = False

    @property
    def stopping(self) -> bool:
        return self._stopping

    def waiting_count(self) -> int:
        """Transfers queued and not yet started, as of now."""
        with self._cond:
            return len(self._pending)

    def running_count(self) -> int:
        with self._cond:
            return len(self._running)

    def open_channel(self):
        """A further file-transfer channel on the connection, for one file sent in parts; raises if refused."""
        return self._opener()

    def share(self) -> int:
        """Bytes per second one running transfer may use: the limit, shared by those running."""
        limit = self._limit
        return max(1024, limit // max(1, len(self._running))) if limit else 0

    # ---- called from the UI thread ------------------------------------------

    def list(self, path: str) -> None:
        self._jobs.put(lambda: self._list(path))

    MEASURE_LIMIT = 500_000  # entries counted before a folder's size is given as "at least"

    def measure(self, path: str) -> None:
        """Add up the size of a remote folder. It runs on a channel of its own, so the lists stay responsive."""
        threading.Thread(target=self._measure, args=(path,), daemon=True).start()

    def _measure(self, path: str) -> None:
        total = files = folders = 0
        complete = True
        try:
            sftp = self._opener()
        except Exception:
            self.measured.emit(path, 0, 0, 0, False)
            return
        try:
            pending = [path]
            while pending and not self._stopping and files + folders < self.MEASURE_LIMIT:
                current = pending.pop()
                try:
                    entries = sftp.listdir_attr(current)
                except OSError:
                    complete = False  # a folder that could not be read
                    continue
                for entry in entries:
                    name = entry.filename
                    if not name or "/" in name or name in (".", ".."):
                        continue
                    mode = entry.st_mode or 0
                    if stat.S_ISDIR(mode):
                        folders += 1
                        pending.append(posixpath.join(current, name))
                    else:
                        files += 1
                        total += entry.st_size or 0  # a link counts as itself, and is never followed
            complete = complete and not pending  # stopped early: the figure is "at least"
        finally:
            try:
                sftp.close()
            except Exception:
                pass
        self.measured.emit(path, total, files, folders, complete)

    def list_folders(self, path: str) -> None:
        """List only the folders in a remote folder, for the folder tree; the file list stays."""
        self._jobs.put(lambda: self._list_folders(path))

    def compare(self, local_root: str, remote_root: str, hidden: bool, leave_out: list[str] = ()) -> None:
        """Compare a local folder with a remote one, all the way down; `compared` tells what differs."""
        self._jobs.put(lambda: self._compare(local_root, remote_root, hidden, list(leave_out)))

    def _compare(self, local_root: str, remote_root: str, hidden: bool, leave_out: list[str]) -> None:
        try:
            here = foldersync.walk_local(local_root, hidden, leave_out)
            there = foldersync.walk_remote(self._sftp, remote_root, hidden, leave_out)
        except foldersync.TooMany as exc:
            self.failed.emit(str(exc))
            return
        self.compared.emit(local_root, remote_root, foldersync.compare(here, there))

    def upload(self, local_paths: list[str], remote_dir: str) -> int:
        """Queue an upload; returns its number, which job_started and job_finished report."""
        targets = tuple("remote:" + posixpath.join(remote_dir, os.path.basename(path.rstrip("/\\"))).rstrip("/") for path in local_paths)
        return self._queue_transfer(lambda transfer: transfer.upload(local_paths, remote_dir), remote_dir, targets)

    def download(self, remote_path: str, local_path: str) -> int:
        return self._queue_transfer(lambda transfer: transfer.download(remote_path, local_path), None, (self._local_key(local_path),))

    def upload_file(self, local_path: str, remote_path: str) -> int:
        """Copy one local file to an exact remote path (used to save an edited file back)."""
        return self._queue_transfer(lambda transfer: transfer.save_back(local_path, remote_path), None, ("remote:" + remote_path.rstrip("/"),))

    @staticmethod
    def _local_key(path: str) -> str:
        return "local:" + os.path.normcase(os.path.abspath(path)).replace("\\", "/").rstrip("/")

    def remove_queued(self, job: int) -> bool:
        """Take a transfer that has not started yet out of the queue; False if it has started."""
        with self._cond:
            for queued in self._pending:
                if queued.number == job:
                    self._pending.remove(queued)
                    self._settle(queued)
                    break
            else:
                return False
            count = len(self._pending)
        self.waiting.emit(count)
        return True

    def _queue_transfer(self, action, directory: str | None = None, targets: tuple[str, ...] = ()) -> int:
        job = _Job(next(self._job_ids), action, directory, targets)
        with self._cond:
            if not self._closing:
                self._pending.append(job)
                if directory is not None:
                    self._into[directory] = self._into.get(directory, 0) + 1
                self._start_lanes()
                self._cond.notify_all()
            count = len(self._pending)
        self.waiting.emit(count)
        return job.number

    def make_directory(self, path: str) -> None:
        self._jobs.put(lambda: self._simple(lambda: self._sftp.mkdir(path), path, "Created"))

    def delete(self, path: str, is_dir: bool) -> None:
        """Delete a file, or a folder with everything in it."""
        remove = (lambda: self._remove_tree(path)) if is_dir else (lambda: self._sftp.remove(path))
        self._jobs.put(lambda: self._simple(remove, path, "Deleted"))

    MAX_DELETE_DEPTH = 64  # folders inside folders, far more than any real tree

    def _remove_tree(self, path: str, depth: int = 0) -> None:
        """Remove a folder and all it holds. A link is removed, never followed, so nothing outside is touched."""
        if posixpath.normpath(path) in ("", ".", "/") or depth > self.MAX_DELETE_DEPTH:
            raise OSError("Refusing to delete this folder")
        for entry in self._sftp.listdir_attr(path):
            name = entry.filename
            if not name or "/" in name or name in (".", ".."):
                continue  # a name from the server that would point somewhere else is not followed
            child = posixpath.join(path, name)
            if stat.S_ISDIR(entry.st_mode or 0):
                self._remove_tree(child, depth + 1)
            else:
                self._sftp.remove(child)
        self._sftp.rmdir(path)

    def rename(self, path: str, new_path: str) -> None:
        self._jobs.put(lambda: self._simple(lambda: self._sftp.rename(path, new_path), new_path, "Renamed to"))

    def change_mode(self, path: str, mode: int) -> None:
        """Set a remote file's permission bits, e.g. 0o644."""
        self._jobs.put(lambda: self._simple(lambda: self._sftp.chmod(path, mode), path, f"Permissions {mode:03o} set on"))

    def set_speed_limit(self, kbps: int) -> None:
        """Limit all transfers together to this many kilobytes per second (0 for unlimited); applies at once."""
        self._limit = max(0, kbps) * 1024

    def set_parallel(self, count: int) -> None:
        """How many transfers may run at the same time. More start at once; fewer as the running ones end."""
        with self._cond:
            self._parallel = max(1, min(MAX_TRANSFERS, int(count)))
            self._start_lanes()
            self._cond.notify_all()

    def max_transfers(self) -> int:
        """How many transfers may run at the same time now: what was asked, or less if the server allows less."""
        with self._cond:
            return self._allowed()

    def set_options(self, keep_dates: bool, resume: bool) -> None:
        """Whether a copy keeps its original's date, and whether a partly transferred file is continued."""
        self.keep_dates, self.resume = bool(keep_dates), bool(resume)

    def cancel(self, job: int | None = None) -> None:
        """Stop the running transfers, or one of them, and discard what they had. Queued ones stay."""
        with self._cond:
            for number, running in self._running.items():
                if job is None or number == job:
                    running.cancelled = True

    def set_paused(self, paused: bool) -> None:
        """Hold the running transfers (and the ones waiting) until resumed."""
        with self._cond:
            if paused == self._paused or (paused and not self._running):
                return  # nothing is running, so nothing could be resumed from the window
            self._paused = paused
            self._cond.notify_all()
        self.paused.emit(paused)

    def is_paused(self) -> bool:
        return self._paused

    def stop(self, finish_queue: bool = False) -> None:
        """End the thread. Normally a running transfer is abandoned (a partial file is kept to
        continue from) and waiting ones are dropped; with finish_queue, whatever is running or
        waiting is completed first (used for edited files)."""
        with self._cond:
            if not finish_queue:
                self._stopping = True  # unlike cancel, this leaves the partial file to continue from
                self._pending.clear()
            self._closing = True
            self._paused = False
            self._cond.notify_all()
        self._jobs.put(None)

    # ---- worker thread: the list, folders and the like --------------------------------

    def run(self) -> None:
        try:
            self._sftp = self._opener()
        except Exception as exc:
            self.failed.emit(f"File transfer is not available: {exc}")
            return
        try:
            while True:
                job = self._jobs.get()
                if job is None:
                    break
                try:
                    job()
                except Exception as exc:
                    self.failed.emit(self._describe(exc))
        finally:
            self._join_lanes()
            try:
                self._sftp.close()
            except Exception:
                pass

    def _join_lanes(self) -> None:
        """Give the transfer threads a moment to end; one stuck on the network is left behind."""
        with self._cond:
            lanes = list(self._lanes)
            patient = not self._stopping  # edited files being saved back are worth waiting for
        deadline = time.monotonic() + (10 if patient else 3)
        for lane in lanes:
            lane.join(max(0.0, deadline - time.monotonic()))

    # ---- transfer threads ----------------------------------------------------------------

    def _allowed(self) -> int:
        return min(self._parallel, self._channel_cap)

    def _start_lanes(self) -> None:
        """Start as many transfer threads as the queue can use now. The condition must be held."""
        wanted = min(self._allowed(), len(self._pending) + len(self._running))
        while len(self._lanes) < wanted:
            lane = threading.Thread(target=self._lane, name="file-transfer", daemon=True)
            self._lanes.append(lane)
            self._hopeful += 1
            lane.start()

    def _blocked(self, job: _Job) -> bool:
        """Whether a running transfer writes what this one would: the same file, or a folder holding it."""
        for running in self._running.values():
            for mine in job.targets:
                for theirs in running.targets:
                    if mine == theirs or mine.startswith(theirs + "/") or theirs.startswith(mine + "/"):
                        return True
        return False

    def _take_job(self) -> "_Job | None":
        """Wait for the next transfer that may start; None when the thread should end."""
        with self._cond:
            while True:
                if self._stopping or (self._closing and not self._pending):
                    return None
                job = None
                if not self._paused and len(self._running) < self._allowed():
                    # The first in the queue that does not write where a running one writes: the same file
                    # sent twice goes one after the other, as it always did, not into one .part file at once.
                    job = next((queued for queued in self._pending if not self._blocked(queued)), None)
                if job is not None:
                    self._pending.remove(job)
                    self._running[job.number] = job
                    if len(self._running) == 1:
                        self.busy.emit(True)  # sent here, under the lock, so it cannot overtake the False of a job that just ended
                    count = len(self._pending)
                    break
                self._cond.wait(0.25)
        self.waiting.emit(count)
        return job

    def _settle(self, job: _Job) -> int:
        """The transfer stops counting towards its folder; returns how many others still do. Lock held."""
        if job.directory is None or job.settled:
            return 0
        job.settled = True
        left = self._into.get(job.directory, 0) - 1
        if left > 0:
            self._into[job.directory] = left
            return left
        self._into.pop(job.directory, None)
        return 0

    def list_after(self, job: _Job, sftp) -> None:
        """A transfer into a folder has ended. The last one to end lists the folder again, if it is the
        one on show, so that the list is up to date before the transfer says it is done."""
        with self._cond:
            if self._settle(job):
                return  # another is still on its way; that one will
        directory = job.directory
        if directory is not None and self._shown is not None and directory.rstrip("/") == self._shown.rstrip("/"):
            self._list(directory, sftp)

    def _release(self, job: _Job, settle: bool = True) -> None:
        """A transfer is over (or was refused). The condition must be held."""
        if settle:
            self._settle(job)
        self._running.pop(job.number, None)
        if not self._running:
            if self._paused:
                self._paused = False  # nothing is left to hold, and the window has no way to resume
                self.paused.emit(False)
            self.busy.emit(False)
        self._cond.notify_all()

    def _lane(self) -> None:
        """One transfer thread: a channel of its own, and transfers taken from the queue one by one."""
        sftp = None
        hopeful = True
        try:
            while True:
                job = self._take_job()
                if job is None:
                    break
                if sftp is None:
                    try:
                        sftp = self._opener()
                    except Exception as exc:
                        if self._channel_refused(job, exc):
                            hopeful = False
                            break  # another thread takes the transfer over
                        continue
                    with self._cond:
                        self._channels += 1
                self._run(job, sftp)
        finally:
            if sftp is not None:
                try:
                    sftp.close()
                except Exception:
                    pass
            with self._cond:
                if sftp is not None:
                    self._channels -= 1
                if hopeful:
                    self._hopeful -= 1
                self._lanes.remove(threading.current_thread())
                self._cond.notify_all()

    def _channel_refused(self, job: _Job, exc: Exception) -> bool:
        """No channel could be opened for a transfer. True if another thread takes it over and this one gives up.

        A refusal can be the server's limit, or a passing failure; either way the other threads may do better.
        The last thread that has not given up reports the failure instead, so that nothing waits for ever.
        """
        with self._cond:
            if self._hopeful > 1:
                self._hopeful -= 1  # this thread gives up
                open_now = self._channels
                if open_now:
                    self._channel_cap = open_now  # the server allows this many at once
                self._pending.appendleft(job)
                self._release(job, settle=False)  # queued again, it still counts towards its folder
                count = len(self._pending)
                taken_over = True
            else:
                self._release(job)
                taken_over = False
        if taken_over:
            self.waiting.emit(count)
            if open_now:
                self.finished_job.emit(f"The server allows {open_now} transfer{'' if open_now == 1 else 's'} at a time; carrying on with that many")
            return True
        reason = f"File transfer is not available: {exc}"
        self.job_finished.emit(job.number, False, reason)
        self.failed.emit(reason)
        return False

    def _run(self, job: _Job, sftp) -> None:
        self.job_started.emit(job.number)
        try:
            job.action(_Transfer(self, job, sftp))
        except TransferCancelled as stopped:
            reason = "Stopped; it continues from there next time" if stopped.keep_partial else "Cancelled"
            self.job_finished.emit(job.number, False, reason)
            if not stopped.keep_partial:
                self.finished_job.emit("Transfer cancelled")
        except Exception as exc:
            reason = self._describe(exc)
            self.job_finished.emit(job.number, False, reason)
            self.failed.emit(reason)
        else:
            self.job_finished.emit(job.number, True, "")
        finally:
            with self._cond:
                self._release(job)

    @staticmethod
    def _describe(exc: Exception) -> str:
        if isinstance(exc, PermissionError):
            return "Permission denied"
        if isinstance(exc, FileNotFoundError):
            return "No such file or directory"
        if isinstance(exc, OSError) and str(exc) == "Failure":
            return "The server refused the operation (a directory must be empty to be deleted)"
        return str(exc) or exc.__class__.__name__

    def _list(self, path: str, sftp=None) -> None:
        sftp = sftp or self._sftp  # a transfer lists on its own channel
        with self._list_lock:
            directory = sftp.normalize(path or ".")
            entries = []
            for attr in sftp.listdir_attr(directory):
                mode = attr.st_mode or 0
                is_dir = stat.S_ISDIR(mode)
                if stat.S_ISLNK(mode):
                    try:  # show a link to a directory as a directory
                        is_dir = stat.S_ISDIR(sftp.stat(posixpath.join(directory, attr.filename)).st_mode or 0)
                    except OSError:
                        pass
                entries.append((attr.filename, is_dir, attr.st_size or 0, attr.st_mtime or 0, mode, self._owner(attr)))
            entries.sort(key=lambda entry: (not entry[1], entry[0].lower()))
            self._shown = directory
            self.listed.emit(directory, entries)

    @staticmethod
    def _owner(attr) -> str:
        """"user group" from the server's own listing line, or their numbers if it gave none."""
        fields = (getattr(attr, "longname", "") or "").split()
        if len(fields) > 3:
            return f"{fields[2]} {fields[3]}"
        if attr.st_uid is not None:
            return f"{attr.st_uid} {attr.st_gid}"
        return ""

    def _list_folders(self, path: str) -> None:
        directory = self._sftp.normalize(path or ".")
        names = sorted(
            (attr.filename for attr in self._sftp.listdir_attr(directory) if stat.S_ISDIR(attr.st_mode or 0)),
            key=str.lower,
        )
        self.folders_listed.emit(directory, names)

    def _simple(self, action, path: str, verb: str) -> None:
        action()
        self._list(posixpath.dirname(path))
        self.finished_job.emit(f"{verb} {posixpath.basename(path)}")


class PathEdit(QLineEdit):
    """Where a side is, as a row of buttons, one for each folder on the way there; a click on one goes to it.

    A click beside the buttons turns the row back into the field, to type a path into; Enter goes there, and
    Esc or leaving the field puts back the folder on show. When the buttons do not fit, the first folders give
    way to an ellipsis, which also opens the field.
    """

    navigate = pyqtSignal(str)
    LEFT = 34  # clear of the folder icon at the start of the field

    def __init__(self, remote: bool, parent=None):
        super().__init__(parent)
        self._remote = remote
        self._shown = ""  # the folder on show; what is typed replaces it only once Enter takes it there
        self.crumbs = QWidget(self)
        self.crumbs.setObjectName("Crumbs")
        self.crumbs.setCursor(Qt.CursorShape.IBeamCursor)
        self.crumbs.mousePressEvent = lambda _event: self._edit()
        self._row = QHBoxLayout(self.crumbs)
        self._row.setContentsMargins(0, 0, 0, 0)
        self._row.setSpacing(0)
        self._buttons: list[tuple[QLabel | None, QToolButton]] = []
        self._more = self._crumb("…", "Type a path", self._edit)

    def setText(self, text: str) -> None:
        super().setText(text)
        self._shown = text
        self._rebuild()

    def segments(self) -> list[tuple[str, str]]:
        """(label, path) of each folder from the top down to the one on show."""
        path = self._shown
        if self._remote:
            if not path.startswith("/"):
                return [(path, path)] if path else []
            found, so_far = [("/", "/")], "/"
            for part in (part for part in path.split("/") if part):
                so_far = posixpath.join(so_far, part)
                found.append((part, so_far))
            return found
        if not path:
            return [("This computer", "")]
        parts = Path(path).parts
        found, so_far = [], ""
        for part in parts:
            so_far = os.path.join(so_far, part) if so_far else part
            found.append((part.rstrip("\\/") or part, so_far))  # "C:\" is shown as "C:"
        return found

    def _crumb(self, text: str, tooltip: str, handler) -> QToolButton:
        button = QToolButton()
        button.setText(text)
        button.setToolTip(tooltip)
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        button.clicked.connect(handler)
        return button

    def _rebuild(self) -> None:
        while self._row.count():
            widget = self._row.takeAt(0).widget()
            if widget is not None and widget is not self._more:
                widget.deleteLater()
        self._row.addWidget(self._more)
        self._buttons = []
        segments = self.segments()
        for number, (label, path) in enumerate(segments):
            separator = None
            if number:
                separator = QLabel("›")
                separator.setObjectName("CrumbSep")
                self._row.addWidget(separator)
            button = self._crumb(label, path or label, lambda _checked=False, p=path: self.navigate.emit(p))
            button.setProperty("current", number == len(segments) - 1)
            self._row.addWidget(button)
            self._buttons.append((separator, button))
        self._row.addStretch(1)
        self._fit()

    def _fit(self) -> None:
        """Give way from the top folder down until the rest fits, and show the ellipsis for those left out."""
        room = self.crumbs.width()
        for separator, button in self._buttons:
            button.show()
            if separator is not None:
                separator.show()
        self._more.hide()
        for widget in self.crumbs.findChildren(QWidget):
            widget.ensurePolished()  # measured as styled: new buttons are not yet
        widths = [button.sizeHint().width() + (separator.sizeHint().width() if separator else 0) for separator, button in self._buttons]
        hidden = 0
        while hidden < len(self._buttons) - 1 and sum(widths[hidden:]) + (self._more.sizeHint().width() if hidden else 0) > room:
            separator, button = self._buttons[hidden]
            button.hide()
            if separator is not None:
                separator.hide()
            hidden += 1
        if hidden:
            self._more.show()
            next_separator = self._buttons[hidden][0]
            if next_separator is not None:
                next_separator.show()  # between the ellipsis and the first folder still shown

    def _edit(self) -> None:
        self.setFocus(Qt.FocusReason.MouseFocusReason)
        self.selectAll()

    def _place(self) -> None:
        self.crumbs.setGeometry(self.LEFT, 3, max(0, self.width() - self.LEFT - 6), max(0, self.height() - 6))
        self._fit()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._place()

    def focusInEvent(self, event):
        super().focusInEvent(event)
        self.crumbs.hide()

    def focusOutEvent(self, event):
        super().focusOutEvent(event)
        super().setText(self._shown)  # a path typed but not taken there is let go
        self.crumbs.show()
        self._place()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape and self.hasFocus():
            self.clearFocus()  # which puts back the folder on show
            return
        super().keyPressEvent(event)


class ElidedLabel(QLabel):
    """A label for a path: it shortens the text in the middle to fit, and shows all of it as the tooltip."""

    def __init__(self) -> None:
        super().__init__()
        self._full = ""
        self.setTextFormat(Qt.TextFormat.PlainText)  # paths come from the server
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)  # it takes the width it is given

    def set_full_text(self, text: str) -> None:
        if text != self._full:
            self._full = text
            self.setToolTip(literal(text) if text else "")
            self._elide()

    def full_text(self) -> str:
        return self._full

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._elide()

    def _elide(self) -> None:
        self.setText(self.fontMetrics().elidedText(self._full, Qt.TextElideMode.ElideMiddle, max(self.width(), 20)))


class TransferBar(QProgressBar):
    """The bar of the running transfers, with the per cent written on it in a colour that reads over both halves."""

    def __init__(self) -> None:
        super().__init__()
        self._label = ""
        self.setRange(0, 1000)  # a bar counts in an int: byte counts would overflow it for big files
        self.setTextVisible(False)
        self.setFixedHeight(20)
        self.setMinimumWidth(120)
        self.setAccessibleName("Transfer progress")

    def set_progress(self, permille: int, label: str) -> None:
        self._label = label
        self.setValue(permille)
        self.update()

    def label(self) -> str:
        return self._label

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        box = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        shape = QPainterPath()
        shape.addRoundedRect(box, 3, 3)
        painter.fillPath(shape, QColor(COLORS["border"]))
        span = max(self.maximum() - self.minimum(), 1)
        filled = QRectF(box.left(), box.top(), box.width() * (self.value() - self.minimum()) / span, box.height())
        painter.save()
        painter.setClipPath(shape)
        painter.fillRect(filled, QColor(COLORS["green"]))
        painter.restore()
        painter.setPen(QPen(QColor(COLORS["border2"]), 1))
        painter.drawPath(shape)
        # The per cent is drawn twice: in the usual text colour, and over the filled part in one that shows on green.
        on_green = QColor("#101418") if QColor(COLORS["green"]).lightnessF() > 0.5 else QColor("#ffffff")
        painter.setPen(QColor(COLORS["text"]))
        painter.drawText(box, Qt.AlignmentFlag.AlignCenter, self._label)
        painter.setClipRect(filled)
        painter.setPen(on_green)
        painter.drawText(box, Qt.AlignmentFlag.AlignCenter, self._label)


class StatusDelegate(QStyledItemDelegate):
    """The Status column of the transfer queue: the text, and while it runs a thin bar under it."""

    def paint(self, painter, option, index):
        percent = index.data(ROLE_PROGRESS)
        if percent is None or percent < 0:
            super().paint(painter, option, index)
            return
        area = option.rect
        lifted = QStyleOptionViewItem(option)
        lifted.rect = area.adjusted(0, 0, 0, -5)  # the text sits in what is left above the bar
        super().paint(painter, lifted, index)
        track = QRectF(area.left() + 6, area.bottom() - 3, max(0, area.width() - 12), 2)
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(COLORS["border2"]))
        painter.drawRoundedRect(track, 1.5, 1.5)
        painter.setBrush(QColor(COLORS["green"]))
        painter.drawRoundedRect(QRectF(track.left(), track.top(), track.width() * min(percent, 100) / 100, track.height()), 1.5, 1.5)
        painter.restore()


class FolderCompareDialog(QDialog):
    """What differs between the local and the remote folder, each with a box to tick; Upload and Download
    copy the ticked ones in the direction their difference points."""

    upload_wanted = pyqtSignal(list)  # [foldersync.Difference]
    download_wanted = pyqtSignal(list)
    compare_again = pyqtSignal(str)  # the patterns to leave out, as typed

    def __init__(self, local_root: str, remote_root: str, differences: list, leave_out: str = "", parent=None):
        super().__init__(parent)
        self.setWindowTitle("Compare Folders")
        self.differences = differences
        heading = QLabel(literal(f"{local_root}   \u2194   {remote_root}"))
        heading.setObjectName("FilePanelTitle")
        heading.setWordWrap(True)
        counts: dict[str, int] = {}
        for difference in differences:
            counts[difference.kind] = counts.get(difference.kind, 0) + 1
        summary = QLabel(
            "; ".join(f"{foldersync.WORDS[kind]}: {count}" for kind, count in counts.items())
            if differences else "The two folders are the same: nothing to copy."
        )
        summary.setObjectName("Muted")
        summary.setWordWrap(True)
        self.leave_out = QLineEdit(leave_out)
        self.leave_out.setPlaceholderText("Names or patterns such as *.log, separated by commas")
        self.leave_out.setToolTip("Files and folders of these names are not compared or copied; a folder takes all it holds along")
        self.leave_out.returnPressed.connect(lambda: self.compare_again.emit(self.leave_out.text()))
        again = QPushButton("Compare Again")
        again.clicked.connect(lambda: self.compare_again.emit(self.leave_out.text()))
        leave_row = QHBoxLayout()
        leave_row.setSpacing(8)
        leave_label = QLabel("Leave out")
        leave_label.setObjectName("Muted")
        leave_row.addWidget(leave_label)
        leave_row.addWidget(self.leave_out, 1)
        leave_row.addWidget(again)
        self.list = QTreeWidget()
        self.list.setObjectName("CompareList")
        self.list.setHeaderLabels(["Name", "This computer", "Server", "Difference"])
        self.list.setRootIsDecorated(False)
        self.list.setUniformRowHeights(True)
        self.list.setTextElideMode(Qt.TextElideMode.ElideMiddle)
        self.resize(900, 520)
        for difference in differences:
            item = QTreeWidgetItem([difference.path + ("/" if difference.is_dir else ""), self._side(difference.here), self._side(difference.there), foldersync.WORDS[difference.kind]])
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            # Which way to copy a file of the same date and another size is not for the program to guess.
            item.setCheckState(0, Qt.CheckState.Unchecked if difference.kind == foldersync.DIFFERENT else Qt.CheckState.Checked)
            item.setData(0, Qt.ItemDataRole.UserRole, difference)
            self.list.addTopLevelItem(item)
        header = self.list.header()
        header.setStretchLastSection(False)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for column, width in ((1, 200), (2, 200), (3, 180)):
            header.resizeSection(column, width)
        self.list.itemChanged.connect(lambda *_: self._count())
        self.upload_button = QPushButton()
        self.upload_button.setObjectName("Primary")
        self.upload_button.clicked.connect(lambda: self._send(self.upload_wanted, foldersync.UPLOADS))
        self.download_button = QPushButton()
        self.download_button.setObjectName("Primary")
        self.download_button.clicked.connect(lambda: self._send(self.download_wanted, foldersync.DOWNLOADS))
        close = QPushButton("Close")
        close.clicked.connect(self.reject)
        tick_all = QPushButton("Tick All")
        tick_all.clicked.connect(lambda: self.tick_all(True))
        tick_none = QPushButton("Tick None")
        tick_none.clicked.connect(lambda: self.tick_all(False))
        buttons = QHBoxLayout()
        buttons.addWidget(tick_all)
        buttons.addWidget(tick_none)
        buttons.addStretch(1)
        buttons.addWidget(close)
        buttons.addWidget(self.download_button)
        buttons.addWidget(self.upload_button)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 14)
        layout.setSpacing(10)
        layout.addWidget(heading)
        layout.addLayout(leave_row)
        layout.addWidget(summary)
        layout.addWidget(self.list, 1)
        layout.addLayout(buttons)
        self.list.setVisible(bool(differences))
        self._count()

    @staticmethod
    def _side(entry) -> str:
        if entry is None:
            return "\u2013"
        if entry.is_dir:
            return "Folder"
        return f"{human_size(entry.size)}, {format_time(entry.modified)}"

    def ticked(self, kinds) -> list:
        found = []
        for row in range(self.list.topLevelItemCount()):
            item = self.list.topLevelItem(row)
            difference = item.data(0, Qt.ItemDataRole.UserRole)
            if item.checkState(0) == Qt.CheckState.Checked and difference.kind in kinds:
                found.append(difference)
        return found

    def tick_all(self, ticked: bool) -> None:
        state = Qt.CheckState.Checked if ticked else Qt.CheckState.Unchecked
        for row in range(self.list.topLevelItemCount()):
            self.list.topLevelItem(row).setCheckState(0, state)

    def _count(self) -> None:
        up, down = len(self.ticked(foldersync.UPLOADS)), len(self.ticked(foldersync.DOWNLOADS))
        self.upload_button.setText(f"Upload ({up})")
        self.download_button.setText(f"Download ({down})")
        self.upload_button.setEnabled(bool(up))
        self.download_button.setEnabled(bool(down))

    def _send(self, signal, kinds) -> None:
        chosen = self.ticked(kinds)
        if chosen:
            signal.emit(chosen)
            self.accept()


def folder_size_text(total: int, files: int, folders: int, complete: bool) -> str:
    """"12.3 MB in 40 files and 5 folders", with "at least" when not all of it could be counted."""
    counts = f"{files:,} file{'' if files == 1 else 's'} and {folders:,} folder{'' if folders == 1 else 's'}"
    return f"{'At least ' if not complete else ''}{human_size(total)} ({total:,} bytes) in {counts}"


class PropertiesDialog(QDialog):
    """What is known about one file or folder, on either side. A folder's size is added up while it is open."""

    measured = pyqtSignal(object)  # (bytes, files, folders, complete) for a local folder

    def __init__(self, title: str, rows: list[tuple[str, str]], measuring: bool = False, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"Properties - {title}")
        self.setMinimumWidth(420)
        self._closed = False
        grid = QGridLayout()
        grid.setHorizontalSpacing(14)
        grid.setVerticalSpacing(6)
        for row, (name, value) in enumerate(rows):
            label = QLabel(name)
            label.setObjectName("Muted")
            label.setAlignment(Qt.AlignmentFlag.AlignTop)
            grid.addWidget(label, row, 0)
            grid.addWidget(self._value(value), row, 1)
        self._size = None
        if measuring:
            self._size = self._value("Adding up...")
            label = QLabel("Total size")
            label.setObjectName("Muted")
            label.setAlignment(Qt.AlignmentFlag.AlignTop)
            grid.addWidget(label, len(rows), 0)
            grid.addWidget(self._size, len(rows), 1)
        grid.setColumnStretch(1, 1)
        close = QPushButton("Close")
        close.clicked.connect(self.close)
        close.setDefault(True)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        buttons.addWidget(close)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 14)
        layout.setSpacing(14)
        layout.addLayout(grid)
        layout.addLayout(buttons)
        self.measured.connect(lambda result: self.set_size(*result))

    @staticmethod
    def _value(text: str) -> QLabel:
        label = QLabel(text)
        label.setTextFormat(Qt.TextFormat.PlainText)  # names come from the server
        label.setWordWrap(True)
        label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        return label

    def set_size(self, total: int, files: int, folders: int, complete: bool) -> None:
        if self._size is not None:
            self._size.setText(folder_size_text(total, files, folders, complete))

    def measure_local(self, path: str) -> None:
        """Add up a local folder on a thread of its own."""
        threading.Thread(target=self._measure_local, args=(path,), daemon=True).start()

    def _measure_local(self, path: str) -> None:
        total = files = folders = 0
        pending = [path]
        complete = True
        while pending and not self._closed:
            if files + folders >= SftpWorker.MEASURE_LIMIT:
                complete = False
                break
            try:
                with os.scandir(pending.pop()) as scan:
                    for entry in scan:
                        try:
                            if entry.is_dir(follow_symlinks=False):
                                folders += 1
                                pending.append(entry.path)
                            else:
                                files += 1
                                total += entry.stat(follow_symlinks=False).st_size
                        except OSError:
                            complete = False
            except OSError:
                complete = False
        if not self._closed:
            try:
                self.measured.emit((total, files, folders, complete and not pending))
            except RuntimeError:
                pass  # the dialog was deleted meanwhile

    def done(self, result: int) -> None:
        self._closed = True  # stops a count still running
        super().done(result)


class FileBrowser(QWidget):
    """A window with this computer's files on the left and the remote host's on the right.

    Select on one side and press Upload or Download; the other side's folder is the destination.
    Several transfers run at the same time (see Preferences.transfer_parallel).
    """

    closed = pyqtSignal()
    speedLimitChanged = pyqtSignal(int)  # kilobytes per second, 0 for unlimited
    settingsRequested = pyqtSignal()  # the button for the transfer settings was pressed
    terminalRequested = pyqtSignal(str)  # a remote folder to change to in the session's terminal

    def __init__(
        self,
        opener,
        title: str = "",
        speed_limit: int = 0,
        parent=None,
        start_remote: str = ".",
        bookmark_key: str = "",
        prefs: Preferences | None = None,
    ):
        super().__init__(parent, Qt.WindowType.Window)
        self.setObjectName("FileBrowser")
        self.setWindowTitle(f"Files - {title}" if title else "Files")
        self._base_title = self.windowTitle()
        self._busy = False
        # Remote bookmarks are kept per session; without a session they are only for this window.
        self._bookmark_key = f"file_bookmarks/{bookmark_key}" if bookmark_key else ""
        self._window_bookmarks: list[str] = []
        self.setAcceptDrops(True)
        self.setMinimumSize(560, 360)
        fit_to_screen(self, 1040, 620)
        self.directory = ""  # remote folder shown
        self.local_directory = ""
        self._themed: list = []
        self._folder_toggles: list = []  # the buttons that show or hide each side's folder tree
        self._folder_views: list = []
        self._speed_limit = max(0, speed_limit)
        self._waiting = 0
        self._entries: list = []  # the remote folder as last listed, before hiding anything
        self._rows: list = []  # the same entries as RemoteRow, in the order the list is sorted
        self._shown: list = []  # the rows in the list now
        self._shown_totals = (0, 0, 0)  # files, folders and bytes in the list now
        self._settings = open_settings()  # remembers this window's own choices
        self._show_hidden = str(self._settings.value("file_browser_hidden", "false")).lower() == "true"

        self._prefs = prefs or Preferences()
        self.worker = SftpWorker(opener)
        self.worker.set_speed_limit(self._speed_limit)
        self.worker.set_parallel(self._prefs.transfer_parallel)
        self.worker.set_options(self._prefs.transfer_keep_dates, self._prefs.transfer_resume)
        self._measuring: dict[str, list[PropertiesDialog]] = {}  # remote folder -> dialogs waiting for its size
        self.worker.measured.connect(self._remote_measured)
        self.worker.listed.connect(self._show_listing)
        self.worker.compared.connect(self._show_comparison)
        self.comparison: FolderCompareDialog | None = None  # the last one shown
        self._sync_jobs: set[int] = set()  # copies started from a comparison, not yet done
        self._checking_sync = False  # the next comparison is the check made once they are
        self.worker.job_finished.connect(self._sync_job_done)
        self.worker.progress.connect(self._show_progress)
        self.worker.finished_job.connect(lambda message: self._show_done(message))
        self.worker.failed.connect(lambda message: self._show_done(message, error=True))
        self.worker.busy.connect(self._set_busy)
        self.worker.waiting.connect(self._set_waiting)
        self.worker.downloaded.connect(self._file_arrived)
        self.worker.resumed.connect(self._show_resumed)
        self.worker.paused.connect(self._show_paused)
        self._last_progress = ""
        # Remote files opened for editing: local copy -> (remote path, time last saved back).
        self._editing: dict[str, tuple[str, float]] = {}
        self._edit_requests: set[str] = set()
        self._edit_dir = ""
        self._edit_timer = QTimer(self)
        self._edit_timer.setInterval(1000)
        self._edit_timer.timeout.connect(self._check_edited_files)

        # ---- this computer: folder tree above the file list ----
        self.local_model = LocalFileModel(self)
        self.local_folders_model = LocalFileModel(self)
        self._apply_local_filter()
        self.local_model.setRootPath("")
        self.local_folders_model.setRootPath("")
        self.local_folders = QTreeView()
        self.local_folders.setModel(self.local_folders_model)
        self.local_folders.setHeaderHidden(True)
        for column in (1, 2, 3):
            self.local_folders.hideColumn(column)
        self.local_folders.setUniformRowHeights(True)
        self.local_folders.clicked.connect(lambda index: self.go_local(self.local_folders_model.filePath(index)))
        # The folders around the current one are listed and sorted after it is shown, and push it out
        # of sight: the tree follows it until the user scrolls or opens a folder there themselves.
        self._follow_local_folder = False
        self._reveal_timer = QTimer(self)
        self._reveal_timer.setSingleShot(True)
        self._reveal_timer.setInterval(30)
        self._reveal_timer.timeout.connect(self._reveal_local_folder)
        self.local_folders_model.rowsInserted.connect(self._reveal_timer.start)
        self.local_folders_model.layoutChanged.connect(self._reveal_timer.start)
        for bar in (self.local_folders.verticalScrollBar(), self.local_folders.horizontalScrollBar()):
            bar.actionTriggered.connect(self._stop_following_local_folder)
        self.local_folders.expanded.connect(self._stop_following_local_folder)
        self.local_folders.collapsed.connect(self._stop_following_local_folder)
        self.local_view = QTreeView()
        self.local_view.setModel(self.local_model)
        self.local_view.setRootIsDecorated(False)
        self.local_view.setItemsExpandable(False)
        self.local_view.setUniformRowHeights(True)
        self.local_view.setTextElideMode(Qt.TextElideMode.ElideMiddle)  # keep the file's ending in view
        self.local_view.setSortingEnabled(True)
        self.local_view.sortByColumn(0, Qt.SortOrder.AscendingOrder)
        self.local_view.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.local_view.doubleClicked.connect(self._activate_local)
        self.local_view.setDragEnabled(True)  # drag onto the remote side to upload
        self.local_view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.local_view.customContextMenuRequested.connect(self._local_menu)
        self._set_columns(self.local_view.header(), [72, 84, 124])
        self.local_view.selectionModel().selectionChanged.connect(lambda *_: self._update_local_summary())
        self.local_model.directoryLoaded.connect(lambda _path: self._update_local_summary())
        self.local_filter = QLineEdit()
        self.local_filter.textChanged.connect(self._filter_local)
        self.remote_filter = QLineEdit()
        self.remote_filter.textChanged.connect(lambda _text: self._fill_remote())
        self.local_path = PathEdit(remote=False)
        self.local_path.setPlaceholderText("Folder on this computer")
        self.local_path.navigate.connect(self.go_local)
        self.local_path.returnPressed.connect(lambda: self.go_local(self.local_path.text()))
        self.local_summary = QLabel()
        local_pane = self._side(
            "This computer",
            "computer",
            self.local_path,
            [("up", "Parent folder", self.go_local_up), ("star", "Bookmarks", lambda: self._bookmark_menu(False))],
            self.local_filter,
            self.local_folders,
            self.local_view,
            self.local_summary,
        )

        # ---- the remote host: folder tree above the file list ----
        self.remote_folders = QTreeWidget()
        self.remote_folders.setHeaderHidden(True)
        self.remote_folders.setUniformRowHeights(True)
        self.remote_folders.itemClicked.connect(lambda item, _column: self.go(item.data(0, ROLE_PATH)))
        self.remote_folders.itemExpanded.connect(self._remote_folder_expanded)
        self._remote_nodes: dict[str, QTreeWidgetItem] = {}
        self.tree = RemoteTree()
        self.tree.setHeaderLabels(REMOTE_COLUMNS)
        self.tree.headerItem().setTextAlignment(1, _SIZE_ALIGNMENT)
        self.tree.setRootIsDecorated(False)
        self.tree.setUniformRowHeights(True)
        self.tree.setTextElideMode(Qt.TextElideMode.ElideMiddle)
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._remote_menu)
        self.tree.itemDoubleClicked.connect(self._activate_remote)
        self.tree.itemSelectionChanged.connect(self._update_remote_summary)
        self.tree.header().sortIndicatorChanged.connect(lambda _column, _order: self._resort_remote())
        self.tree.setDragEnabled(True)  # drag onto the local side to download
        self._set_columns(self.tree.header(), [72, 84, 124, 92, 90], name_width=200)
        self.path = PathEdit(remote=True)
        self.path.setPlaceholderText("Remote folder")
        self.path.navigate.connect(self.go)
        self.path.returnPressed.connect(lambda: self.go(self.path.text()))
        self.remote_summary = QLabel("Not connected.")
        remote_pane = self._side(
            title or "Server",
            "server",
            self.path,
            [
                ("up", "Parent folder", self.go_up),
                ("refresh", "Refresh", self.refresh),
                ("star", "Bookmarks", lambda: self._bookmark_menu(True)),
            ],
            self.remote_filter,
            self.remote_folders,
            self.tree,
            self.remote_summary,
        )

        panes = self._panes = QSplitter(Qt.Orientation.Horizontal)
        panes.setObjectName("FileGap")  # the cards stand apart, with the window showing between them
        panes.setChildrenCollapsible(False)
        panes.setHandleWidth(CARD_GAP)
        panes.addWidget(local_pane)
        panes.addWidget(remote_pane)

        # ---- the transfer queue: waiting, failed and done ----
        self._queue_rows: dict[int, QTreeWidgetItem] = {}  # transfer number -> its row while queued
        self._retry: dict[int, tuple] = {}  # failed row id -> what to queue again
        self._started: set[int] = set()  # transfers that are running
        self._started_at: dict[int, float] = {}  # when each of them began, on the monotonic clock
        self._tick = QTimer(self)  # keeps the time elapsed and the time left moving between progress reports
        self._tick.setInterval(1000)
        self._tick.timeout.connect(self._show_overall)
        self._progress: dict[int, tuple] = {}  # running transfer -> (what, bytes done, bytes in total, bytes per second)
        self.queued_list = self._queue_list(
            self._queued_menu, "Nothing waiting. Drag files from one side to the other, or use Upload and Download."
        )
        self.failed_list = self._queue_list(self._failed_menu, "No failed transfers.")
        self.done_list = self._queue_list(self._done_menu, "Nothing transferred yet.")
        self.queue_tabs = QTabWidget()
        self.queue_tabs.setObjectName("TransferQueue")
        self.queue_tabs.setDocumentMode(True)
        for queue_list in (self.queued_list, self.failed_list, self.done_list):
            self.queue_tabs.addTab(queue_list, "")
        self._update_queue_labels()
        self.worker.job_started.connect(self._job_started)
        self.worker.job_finished.connect(self._job_finished)
        self.worker.folders_listed.connect(self._fill_remote_folders)

        queue_card = self._queue_card = QFrame()
        queue_card.setObjectName("FileCard")
        queue_layout = QVBoxLayout(queue_card)
        queue_layout.setContentsMargins(6, 4, 6, 6)
        queue_layout.addWidget(self.queue_tabs)
        main = self._main_split = QSplitter(Qt.Orientation.Vertical)
        main.setObjectName("FileGap")
        main.setChildrenCollapsible(False)
        main.setHandleWidth(CARD_GAP)
        main.addWidget(panes)
        main.addWidget(queue_card)
        main.setStretchFactor(0, 4)
        main.setStretchFactor(1, 1)
        self._queue_height_chosen = False  # the user dragged the divider: leave the height alone
        main.splitterMoved.connect(lambda *_: setattr(self, "_queue_height_chosen", True))

        # ---- toolbar: the two transfer directions ----
        self.upload_button = QPushButton("Upload")
        self.upload_button.setObjectName("TransferAction")
        self.upload_button.setToolTip("Copy the selected local files to the remote folder")
        self.upload_button.clicked.connect(self.upload_selected)
        self.download_button = QPushButton("Download")
        self.download_button.setObjectName("TransferAction")
        self.download_button.setToolTip("Copy the selected remote files to the local folder")
        self.download_button.clicked.connect(self.download_selected)
        self.compare_button = QPushButton("Compare")
        self.compare_button.setObjectName("TransferAction")
        self.compare_button.setToolTip("Compare the two folders, all the way down, and copy only what differs")
        self.compare_button.clicked.connect(self.compare_folders)
        self._themed.append(lambda: self.upload_button.setIcon(line_icon("upload", COLORS["accent"])))
        self._themed.append(lambda: self.download_button.setIcon(line_icon("import", COLORS["accent"])))
        self._themed.append(lambda: self.compare_button.setIcon(line_icon("split", COLORS["accent"])))
        for button in (self.upload_button, self.download_button, self.compare_button):
            button.setIconSize(QSize(15, 15))
            button.setCursor(Qt.CursorShape.PointingHandCursor)
        toolbar = QFrame()
        toolbar.setObjectName("FileToolbar")
        toolbar_row = QHBoxLayout(toolbar)
        toolbar_row.setContentsMargins(CARD_GAP, 6, CARD_GAP, 0)
        toolbar_row.setSpacing(4)
        toolbar_row.addWidget(self.upload_button)
        toolbar_row.addWidget(self.download_button)
        toolbar_row.addWidget(self.compare_button)
        toolbar_row.addStretch(1)
        self.settings_button = self._button(
            "settings", "Transfer settings: simultaneous transfers, speed limit and more", lambda: self.settingsRequested.emit()
        )
        toolbar_row.addWidget(self.settings_button)

        # ---- footer: speed limit, progress, status ----
        self.speed = QComboBox()
        self.speed.setToolTip("Limit how fast files are uploaded and downloaded")
        self._fill_speed_choices()
        self.speed.activated.connect(self._speed_chosen)
        self.progress = TransferBar()
        self.transfer_strip = self._build_transfer_strip()
        self.status = QLabel("Connecting...")
        self.status.setTextFormat(Qt.TextFormat.PlainText)  # it shows names that the server chose
        self.status.setObjectName("Muted")
        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.setToolTip("Stop every transfer that is running; the ones waiting stay in the queue")
        self.cancel_button.clicked.connect(lambda: self.worker.cancel())
        self.cancel_button.hide()
        self.pause_button = QPushButton("Pause")
        self.pause_button.setToolTip("Hold the running transfers; Resume carries on from the same place")
        self.pause_button.clicked.connect(self.toggle_pause)
        self.pause_button.hide()
        self.hidden_box = QCheckBox("Hidden files")
        self.hidden_box.setToolTip("Show files and folders whose names start with a dot, and hidden local files")
        self.hidden_box.setChecked(self._show_hidden)
        self.hidden_box.toggled.connect(self.set_show_hidden)
        self.queue_button = QPushButton("Queue")
        self.queue_button.setCheckable(True)
        self.queue_button.setToolTip("Show the waiting, failed and finished transfers (shown by itself while there are any)")
        self.queue_button.toggled.connect(lambda _on: self._sync_queue_card())
        self._sync_queue_card()
        self.log_button = QPushButton("Log")
        self.log_button.setCheckable(True)
        self.log_button.setToolTip("Show what has been transferred in this window")
        self.log_button.toggled.connect(self._toggle_log)
        self.log = QListWidget()
        self.log.setObjectName("TransferLog")
        self.log.setFixedHeight(120)
        self.log.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.log.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.log.customContextMenuRequested.connect(self._log_menu)
        self.log.hide()
        self.clear_log_button = QPushButton("Clear")
        self.clear_log_button.setToolTip("Forget the transfers listed in the log")
        self.clear_log_button.clicked.connect(self.clear_log)
        self.clear_log_button.hide()
        footer = QFrame()
        footer.setObjectName("SettingsFooter")
        footer_row = QHBoxLayout(footer)
        footer_row.setContentsMargins(12, 4, 12, 6)
        footer_row.setSpacing(8)
        limit_label = QLabel("Limit")
        limit_label.setObjectName("Muted")
        footer_row.addWidget(limit_label)
        footer_row.addWidget(self.speed)
        footer_row.addSpacing(8)
        footer_row.addWidget(self.status, 1)
        footer_row.addWidget(self.pause_button)
        footer_row.addWidget(self.cancel_button)
        footer_row.addWidget(self.hidden_box)
        footer_row.addWidget(self.clear_log_button)
        footer_row.addWidget(self.queue_button)
        footer_row.addWidget(self.log_button)

        body = QVBoxLayout()
        body.setContentsMargins(CARD_GAP, CARD_GAP, CARD_GAP, CARD_GAP)
        body.addWidget(main)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(toolbar)
        layout.addLayout(body, 1)
        layout.addWidget(self.transfer_strip)
        layout.addWidget(self.log)
        layout.addWidget(footer)

        self._add_shortcuts()
        self.retheme()
        geometry = self._settings.value("file_browser_geometry")
        if geometry:
            self.restoreGeometry(geometry)
            keep_on_screen(self)
        self.set_folders_shown(str(self._settings.value("file_browser_folders", "false")).lower() == "true")
        self._restore_sort()
        self._restore_log()
        last_local = str(self._settings.value("file_browser_local_dir", "") or "")
        self.go_local(last_local if last_local and os.path.isdir(last_local) else os.path.expanduser("~"))
        self.log_button.setChecked(str(self._settings.value("file_browser_log", "false")).lower() == "true")
        self.worker.start()
        self.go(start_remote or ".")
        self.local_view.setFocus()  # typing and the arrow keys start in the files, not on a button

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._arrange(event.size().width())

    def _arrange(self, width: int) -> None:
        """Side by side on a wide window; on a narrow one this computer above the remote host."""
        stacked = width < STACK_BELOW_WIDTH
        if stacked == (self._panes.orientation() == Qt.Orientation.Vertical):
            return
        self._panes.setOrientation(Qt.Orientation.Vertical if stacked else Qt.Orientation.Horizontal)

    # ---- construction helpers -----------------------------------------------

    @staticmethod
    def _set_columns(header: QHeaderView, widths: list[int], name_width: int = 0) -> None:
        """Widths for the columns after the name, in order.

        Without `name_width` the name takes the room that is left. With it, the name starts that
        wide, the last column takes what is left, and the list scrolls sideways when the columns
        do not fit, so names are never squeezed to nothing.
        """
        if name_width:
            header.setStretchLastSection(True)
            header.setSectionResizeMode(0, QHeaderView.ResizeMode.Interactive)
            header.resizeSection(0, name_width)
        else:
            header.setStretchLastSection(False)
            header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for column, width in enumerate(widths, start=1):
            header.resizeSection(column, width)

    def _side(
        self, title: str, icon: str, path_edit: QLineEdit, buttons, filter_edit: QLineEdit, folders, view, summary
    ) -> QWidget:
        """One side of the window, as a card: what it is, where it is, its folders above its files, and a summary."""
        pane = QFrame()
        pane.setObjectName("FilePanel")
        badge = QLabel()
        badge.setFixedSize(18, 18)
        badge.setScaledContents(True)
        self._themed.append(lambda: badge.setPixmap(icon_pixmap(icon, COLORS["accent"], 54)))
        heading = QLabel(title)
        heading.setObjectName("FilePanelTitle")
        heading.setTextFormat(Qt.TextFormat.PlainText)  # a session's name can come from an imported file
        header = QHBoxLayout()
        header.setSpacing(8)
        header.addWidget(badge)
        header.addWidget(heading, 1)
        for name, tooltip, handler in buttons:
            header.addWidget(self._button(name, tooltip, handler))
        # The folder tree is for jumping far; the path above and the bookmarks do most of that, so it
        # starts hidden and the list gets the whole card. One choice holds for both sides.
        toggle = self._button("folder", "Show the folder tree", lambda: None)
        toggle.setCheckable(True)
        toggle.clicked.disconnect()
        toggle.toggled.connect(self.set_folders_shown)
        header.addWidget(toggle)
        self._folder_toggles.append(toggle)
        self._folder_views.append(folders)
        # Icons inside the fields say what each one is for, so they need no labels.
        folder_mark = path_edit.addAction(QIcon(), QLineEdit.ActionPosition.LeadingPosition)
        search_mark = filter_edit.addAction(QIcon(), QLineEdit.ActionPosition.LeadingPosition)
        self._themed.append(lambda: folder_mark.setIcon(line_icon("folder", COLORS["muted"])))
        self._themed.append(lambda: search_mark.setIcon(line_icon("search", COLORS["muted"])))
        filter_edit.setPlaceholderText("Filter")
        filter_edit.setClearButtonEnabled(True)
        filter_edit.setMinimumWidth(90)
        filter_edit.setMaximumWidth(180)
        bar = QHBoxLayout()
        bar.setSpacing(6)
        bar.addWidget(path_edit, 1)
        bar.addWidget(filter_edit)
        split = QSplitter(Qt.Orientation.Vertical)
        split.setObjectName("FileSplit")
        split.setChildrenCollapsible(False)
        split.setHandleWidth(1)
        folders.setMinimumHeight(60)
        folders.setIndentation(14)  # deep paths stay in view instead of drifting to the right
        view.setMinimumHeight(80)
        split.addWidget(folders)
        split.addWidget(view)
        split.setStretchFactor(0, 1)
        split.setStretchFactor(1, 2)
        summary.setObjectName("FileSummary")
        layout = QVBoxLayout(pane)
        layout.setContentsMargins(10, 8, 10, 0)
        layout.setSpacing(6)
        layout.addLayout(header)
        layout.addLayout(bar)
        layout.addWidget(split, 1)
        layout.addWidget(summary)
        return pane

    def _queue_list(self, menu_handler, empty_text: str) -> QueueList:
        queue_list = QueueList(empty_text)
        queue_list.setHeaderLabels(QUEUE_COLUMNS)
        queue_list.headerItem().setTextAlignment(3, _SIZE_ALIGNMENT)
        queue_list.setRootIsDecorated(False)
        queue_list.setUniformRowHeights(True)
        queue_list.setTextElideMode(Qt.TextElideMode.ElideMiddle)
        queue_list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        queue_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        queue_list.customContextMenuRequested.connect(lambda pos: menu_handler(queue_list, pos))
        queue_list.setItemDelegateForColumn(4, StatusDelegate(queue_list))
        header = queue_list.header()
        header.setStretchLastSection(False)
        for column in (0, 2):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.Stretch)
        header.resizeSection(1, 70)
        header.resizeSection(3, 80)
        header.resizeSection(4, 220)
        return queue_list

    def _add_shortcuts(self) -> None:
        """The usual file-manager keys, each acting on the side that has the focus."""
        bindings = (
            (self.tree, "F5", self.refresh),
            (self.tree, "Backspace", self.go_up),
            (self.tree, "Delete", self.delete_selected),
            (self.tree, "F2", lambda: self._rename(self.tree.currentItem()) if self.tree.currentItem() else None),
            (self.tree, "Return", lambda: self._activate_remote(self.tree.currentItem()) if self.tree.currentItem() else None),
            (self.tree, "Ctrl+F", lambda: self.remote_filter.setFocus()),
            (self.tree, "Ctrl+L", self.path._edit),
            (self.tree, "Ctrl+Shift+C", self.copy_remote_paths),
            (self.remote_filter, "Escape", self._leave_filter(self.remote_filter, self.tree)),
            (self.local_filter, "Escape", self._leave_filter(self.local_filter, self.local_view)),
            (self.local_view, "Ctrl+F", lambda: self.local_filter.setFocus()),
            (self.local_view, "Ctrl+L", self.local_path._edit),
            (self.local_view, "Ctrl+Shift+C", self.copy_local_paths),
            (self.local_view, "Backspace", self.go_local_up),
            (self.local_view, "Delete", self.trash_selected_local),
            (self.local_view, "F2", self._rename_local_selected),
            (self.local_view, "Return", lambda: self._activate_local(self.local_view.currentIndex())),
        )
        for widget, keys, handler in bindings:
            shortcut = QShortcut(QKeySequence(keys), widget, activated=handler)
            shortcut.setContext(Qt.ShortcutContext.WidgetShortcut)

    @staticmethod
    def _leave_filter(box: QLineEdit, view: QWidget):
        """Escape in a filter box empties it, and a second Escape returns to the list."""

        def leave() -> None:
            if box.text():
                box.clear()
            else:
                view.setFocus()

        return leave

    def show_remote_properties(self, item: QTreeWidgetItem | None = None) -> PropertiesDialog | None:
        item = item or self.tree.currentItem()
        if item is None:
            return None
        name = item.text(0)
        is_dir = bool(item.data(0, ROLE_IS_DIR))
        size, modified, mode = item.data(0, ROLE_SIZE), item.data(0, ROLE_MODIFIED), item.data(0, ROLE_MODE)
        path = self._remote(item)
        rows = [("Name", name), ("Location", self.directory), ("Type", file_type(name, is_dir) or ("Folder" if is_dir else "File"))]
        if not is_dir:
            rows.append(("Size", f"{human_size(size or 0)} ({(size or 0):,} bytes)"))
        rows.append(("Modified", format_time(modified) if modified else "Unknown"))
        if mode:
            rows.append(("Permissions", f"{stat.filemode(mode)}  ({stat.S_IMODE(mode):03o})"))
        rows.append(("Owner", item.text(5) or "Unknown"))
        dialog = PropertiesDialog(name, rows, measuring=is_dir, parent=self)
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        if is_dir:
            self._measuring.setdefault(path, []).append(dialog)
            dialog.destroyed.connect(lambda _=None, path=path, d=dialog: self._forget_measuring(path, d))
            self.worker.measure(path)
        dialog.show()
        return dialog

    def _forget_measuring(self, path: str, dialog: PropertiesDialog) -> None:
        waiting = self._measuring.get(path, [])
        if dialog in waiting:
            waiting.remove(dialog)
        if not waiting:
            self._measuring.pop(path, None)

    def _remote_measured(self, path: str, total: int, files: int, folders: int, complete: bool) -> None:
        for dialog in list(self._measuring.get(path, [])):
            dialog.set_size(total, files, folders, complete)

    def show_local_properties(self) -> PropertiesDialog | None:
        paths = self.selected_local_paths()
        if len(paths) != 1:
            return None
        path = paths[0]
        try:
            info = os.stat(path)
        except OSError as exc:
            self._add_log(f"Could not read {os.path.basename(path)}: {exc.strerror or exc}", True)
            return None
        is_dir = os.path.isdir(path)
        name = os.path.basename(path) or path
        rows = [("Name", name), ("Location", os.path.dirname(path)), ("Type", file_type(name, is_dir) or ("Folder" if is_dir else "File"))]
        if not is_dir:
            rows.append(("Size", f"{human_size(info.st_size)} ({info.st_size:,} bytes)"))
        rows.append(("Modified", format_time(info.st_mtime)))
        dialog = PropertiesDialog(name, rows, measuring=is_dir, parent=self)
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        if is_dir:
            dialog.measure_local(path)
        dialog.show()
        return dialog

    def open_terminal_here(self, item: QTreeWidgetItem | None = None) -> None:
        """Ask the session's terminal to change to a remote folder: the one selected, or else the one showing."""
        folder = self._remote(item) if item is not None and item.data(0, ROLE_IS_DIR) else self.directory
        if folder:
            self.terminalRequested.emit(folder)

    def copy_remote_paths(self) -> None:
        paths = [self._remote(item) for item in self.tree.selectedItems()] or ([self.directory] if self.directory else [])
        if paths:
            self.copy_text("\n".join(paths))

    def copy_local_paths(self) -> None:
        paths = self.selected_local_paths() or ([self.local_directory] if self.local_directory else [])
        if paths:
            self.copy_text("\n".join(paths))

    def _apply_local_filter(self) -> None:
        hidden = QDir.Filter.Hidden if self._show_hidden else QDir.Filter(0)
        self.local_model.setFilter(QDir.Filter.AllEntries | QDir.Filter.NoDotAndDotDot | hidden)
        self.local_folders_model.setFilter(QDir.Filter.AllDirs | QDir.Filter.Drives | QDir.Filter.NoDotAndDotDot | hidden)

    def set_show_hidden(self, show: bool) -> None:
        self._show_hidden = bool(show)
        self._settings.setValue("file_browser_hidden", "true" if show else "false")
        if self.hidden_box.isChecked() != show:
            self.hidden_box.setChecked(show)
        self._apply_local_filter()
        self._fill_remote()
        if self.directory:
            self._fill_remote_folders(self.directory, [entry[0] for entry in self._entries if entry[1]])

    def set_folders_shown(self, show: bool) -> None:
        """Show or hide the folder trees above the file lists."""
        show = bool(show)
        for view in self._folder_views:
            view.setVisible(show)
        for button in self._folder_toggles:
            if button.isChecked() != show:
                button.blockSignals(True)
                button.setChecked(show)
                button.blockSignals(False)
            button.setToolTip("Hide the folder tree" if show else "Show the folder tree")
        self._settings.setValue("file_browser_folders", "true" if show else "false")

    def _toggle_log(self, show: bool) -> None:
        self.log.setVisible(show)
        self.clear_log_button.setVisible(show)
        self._settings.setValue("file_browser_log", "true" if show else "false")

    def clear_log(self) -> None:
        """Forget the transfers listed in the log, here and for the next time the window opens."""
        self.log.clear()
        self._settings.remove("file_browser_log_lines")

    def _add_log(self, message: str, error: bool = False, ok: bool = False) -> None:
        stamp = f"{datetime.now():%Y-%m-%d %H:%M:%S}"
        self._log_item(stamp, message, error, ok)
        while self.log.count() > LOG_LENGTH:
            self.log.takeItem(0)
        self.log.scrollToBottom()
        self._store_log()

    def _log_item(self, stamp: str, message: str, error: bool, ok: bool) -> None:
        item = QListWidgetItem(f"{stamp[11:]}   {message}")
        item.setData(Qt.ItemDataRole.UserRole, (stamp, message, error, ok))
        item.setToolTip(stamp)
        if error:
            item.setForeground(QColor(COLORS["red"]))
        elif ok:
            item.setForeground(QColor(COLORS["green"]))
        self.log.addItem(item)

    def _store_log(self) -> None:
        lines = []
        for row in range(self.log.count()):
            stamp, message, error, ok = self.log.item(row).data(Qt.ItemDataRole.UserRole)
            flag = "e" if error else "o" if ok else "-"
            lines.append(f"{flag}|{stamp}|{message}")
        self._settings.setValue("file_browser_log_lines", lines)

    def _restore_log(self) -> None:
        stored = self._settings.value("file_browser_log_lines") or []
        for line in [stored] if isinstance(stored, str) else stored:
            flag, _, rest = str(line).partition("|")
            stamp, _, message = rest.partition("|")
            if len(stamp) == 19 and message:
                self._log_item(stamp, message, flag == "e", flag == "o")
        while self.log.count() > LOG_LENGTH:
            self.log.takeItem(0)
        self.log.scrollToBottom()

    def _log_text(self, items: list[QListWidgetItem]) -> str:
        rows = (item.data(Qt.ItemDataRole.UserRole) for item in items)
        return "\n".join(f"{stamp}   {message}" for stamp, message, _error, _ok in rows)

    def _log_menu(self, pos) -> None:
        items = [self.log.item(row) for row in range(self.log.count())]
        chosen = self.log.selectedItems()
        menu = QMenu(self)
        copy = menu.addAction("Copy", lambda: self.copy_text(self._log_text(chosen)))
        copy.setEnabled(bool(chosen))
        copy_all = menu.addAction("Copy All", lambda: self.copy_text(self._log_text(items)))
        copy_all.setEnabled(bool(items))
        menu.addSeparator()
        save = menu.addAction("Save Log to a File...", self.save_log)
        save.setEnabled(bool(items))
        menu.addAction("Clear", self.clear_log).setEnabled(bool(items))
        menu.exec(self.log.mapToGlobal(pos))

    def save_log(self, path: str | None = None) -> bool:
        """Write the log to a text file; asks where unless a path is given."""
        if path is None:
            path, _ = QFileDialog.getSaveFileName(
                self, "Save Transfer Log", str(Path.home() / "transfers.log"), "Log files (*.log *.txt)"
            )
        if not path:
            return False
        text = self._log_text([self.log.item(row) for row in range(self.log.count())])
        try:
            Path(path).write_text(text + "\n", encoding="utf-8")
        except OSError as exc:
            self._add_log(f"Could not save the log: {exc.strerror or exc}", True)
            return False
        self.status.setText(f"Log saved to {path}")
        return True

    def _restore_sort(self) -> None:
        """Bring back the order each list was last sorted in, and remember changes from now on."""
        for key, view_header, apply in (
            ("remote", self.tree.header(), self.tree.sortByColumn),
            ("local", self.local_view.header(), self.local_view.sortByColumn),
        ):
            try:
                column = int(self._settings.value(f"file_browser_sort/{key}_column", 0))
                descending = str(self._settings.value(f"file_browser_sort/{key}_descending", "false")).lower() == "true"
            except (TypeError, ValueError):
                continue
            if 0 <= column < view_header.count():
                apply(column, Qt.SortOrder.DescendingOrder if descending else Qt.SortOrder.AscendingOrder)
            view_header.sortIndicatorChanged.connect(lambda column, order, key=key: self._store_sort(key, column, order))

    def _store_sort(self, key: str, column: int, order: Qt.SortOrder) -> None:
        self._settings.setValue(f"file_browser_sort/{key}_column", column)
        self._settings.setValue(
            f"file_browser_sort/{key}_descending", "true" if order == Qt.SortOrder.DescendingOrder else "false"
        )

    def _button(self, icon: str, tooltip: str, handler) -> QToolButton:
        button = QToolButton()
        button.setObjectName("Icon")
        button.setIconSize(QSize(16, 16))
        button.setToolTip(tooltip)
        button.setAccessibleName(tooltip.split(" (")[0])
        button.clicked.connect(handler)
        self._themed.append(lambda: button.setIcon(line_icon(icon)))
        return button

    def _icon_for(self, name: str, is_dir: bool) -> QIcon:
        return self._folder_icon if is_dir else self._file_icons[file_kind(name)]

    def retheme(self) -> None:
        for refresh in self._themed:
            refresh()
        self._folder_icon = line_icon("folder", COLORS["accent"])
        self._file_icons = {"": line_icon("file", COLORS["muted"])}
        self._file_icons.update({kind: line_icon("file", COLORS[color]) for kind, (color, _ext) in FILE_KINDS.items()})
        for model in (self.local_model, self.local_folders_model):
            model.icon_for = self._icon_for
        self.local_view.viewport().update()
        self.local_folders.viewport().update()
        self.progress.update()
        for node in self._remote_nodes.values():
            node.setIcon(0, self._folder_icon)
        for row in self._rows:
            if row.item is not None:
                row.item.setIcon(0, self._icon_for(row.entry[0], row.entry[1]))

    # ---- editing remote files -------------------------------------------------------

    def edit_item(self, item: QTreeWidgetItem) -> None:
        """Open a remote file in the computer's own editor; each save is copied back to the server."""
        if item.data(0, ROLE_IS_DIR):
            return
        if not self._edit_dir:
            self._edit_dir = tempfile.mkdtemp(prefix="techx-edit-")
        remote = self._remote(item)
        name = local_name(item.text(0))
        if name is None:
            self._show_done(f"Cannot edit {item.text(0)}: a file here cannot have that name", error=True)
            return
        # One folder per file, so files with the same name from different folders cannot collide.
        local = os.path.join(tempfile.mkdtemp(dir=self._edit_dir), name)
        self._edit_requests.add(remote)
        self.status.setText(f"Opening {item.text(0)}...")
        self._queue_download(remote, local, item.data(0, ROLE_SIZE))

    def _file_arrived(self, remote: str, local: str) -> None:
        if remote not in self._edit_requests:
            return
        self._edit_requests.discard(remote)
        self._editing[local] = (remote, os.path.getmtime(local))
        self._edit_timer.start()
        self._show_done(f"Editing {posixpath.basename(remote)}: saving it in the editor copies it back to the server")
        self._open_for_editing(local)

    def _open_for_editing(self, path: str) -> None:
        """Show a copy of a remote file in an editor. Windows would run a program or script that is "opened",
        so for those the text is shown in Notepad instead: an edit must not run what a server sent."""
        if not opens_by_running(path):
            self._open_file(path)
        elif QProcess.startDetached("notepad.exe", [path]):
            self._add_log(f"{os.path.basename(path)} is a kind of file that Windows would run: opened in Notepad instead")
        else:
            self._show_done(f"{os.path.basename(path)} was not opened: Windows would run it, and Notepad could not be started", error=True)

    @staticmethod
    def _open_file(path: str) -> None:
        QDesktopServices.openUrl(QUrl.fromLocalFile(path))

    def _check_edited_files(self) -> None:
        for local, (remote, saved_at) in list(self._editing.items()):
            try:
                modified = os.path.getmtime(local)
            except OSError:
                continue  # an editor may briefly remove the file while it saves
            if modified != saved_at:
                self._editing[local] = (remote, modified)
                job = self.worker.upload_file(local, remote)
                self._add_queue_row(job, local, UPLOAD_ARROW, remote, os.path.getsize(local), None)

    def editing(self) -> list[str]:
        """Remote paths that are open for editing."""
        return [remote for remote, _saved in self._editing.values()]

    def shutdown(self) -> None:
        self._edit_timer.stop()
        self._check_edited_files()  # a last look, so a save just before closing is not lost
        self._settings.setValue("file_browser_geometry", self.saveGeometry())
        # With files open for editing, let pending saves finish instead of abandoning them.
        self.worker.stop(finish_queue=bool(self._editing))
        self.worker.wait(8000 if self._editing else 2000)
        threads.park(self.worker)  # a transfer stuck on the network may need longer to notice
        if self._edit_dir:
            # A virus scanner may hold a just-written file for a moment; try a few times.
            for _attempt in range(5):
                shutil.rmtree(self._edit_dir, ignore_errors=True)
                if not os.path.exists(self._edit_dir):
                    break
                time.sleep(0.2)

    def closeEvent(self, event):
        self._settings.setValue("file_browser_geometry", self.saveGeometry())
        self.closed.emit()
        event.accept()

    # ---- speed limit ----------------------------------------------------------

    def _fill_speed_choices(self) -> None:
        self.speed.clear()
        choices = sorted(set(SPEED_PRESETS) | {self._speed_limit})
        for kbps in choices:
            self.speed.addItem(speed_label(kbps), kbps)
        self.speed.addItem("Custom...", -1)
        self.speed.setCurrentIndex(self.speed.findData(self._speed_limit))

    def _speed_chosen(self, index: int) -> None:
        kbps = self.speed.itemData(index)
        if kbps == -1:
            kbps, ok = QInputDialog.getInt(
                self, "Speed Limit", "Kilobytes per second (0 for unlimited)", max(self._speed_limit, 0), 0, 1_000_000
            )
            if not ok:
                kbps = self._speed_limit
        self.set_speed_limit(kbps)

    def apply_preferences(self, prefs: Preferences) -> None:
        """Adopt changed preferences; they apply to the transfers running and queued now as well."""
        self._prefs = prefs
        self.worker.set_parallel(prefs.transfer_parallel)
        self.worker.set_options(prefs.transfer_keep_dates, prefs.transfer_resume)
        if prefs.transfer_limit_kbps != self._speed_limit:
            self.set_speed_limit(prefs.transfer_limit_kbps)

    def speed_limit(self) -> int:
        return self._speed_limit

    def set_speed_limit(self, kbps: int) -> None:
        kbps = max(0, int(kbps))
        changed = kbps != self._speed_limit
        self._speed_limit = kbps
        self.worker.set_speed_limit(kbps)
        self._fill_speed_choices()
        if changed:
            self.speedLimitChanged.emit(kbps)

    # ---- this computer ------------------------------------------------------------

    def go_local(self, path: str) -> None:
        """Show a local folder; an empty path shows the drives."""
        path = os.path.abspath(os.path.expanduser(path)) if path else ""
        if path and not os.path.isdir(path):
            self.status.setText(f"No such folder: {path}")
            self.local_path.setText(self.local_directory)
            return
        self.local_directory = path
        self.local_path.setText(path)
        if path:
            self._settings.setValue("file_browser_local_dir", path)
        self.local_view.setRootIndex(self.local_model.index(path) if path else self.local_model.index(""))
        self.local_view.clearSelection()
        if path:
            folder = self.local_folders_model.index(path)
            self.local_folders.setCurrentIndex(folder)
            self.local_folders.expand(folder)
            self._follow_local_folder = True
            self._reveal_local_folder()
        else:
            self.local_folders.clearSelection()
        self._update_local_summary()

    def _reveal_local_folder(self) -> None:
        if self._follow_local_folder:
            self._follow_local_folder = None  # scrolling there may open its parents: that is not the user
            self.local_folders.scrollTo(self.local_folders.currentIndex(), QAbstractItemView.ScrollHint.PositionAtCenter)
            self._follow_local_folder = True

    def _stop_following_local_folder(self, *_args) -> None:
        if self._follow_local_folder is not None:
            self._follow_local_folder = False

    def _update_local_summary(self) -> None:
        model, root = self.local_model, self.local_view.rootIndex()
        selected = self.local_view.selectionModel().selectedRows(0)
        rows = selected or [model.index(row, 0, root) for row in range(model.rowCount(root))]
        folders = sum(1 for index in rows if model.isDir(index))
        size = sum(model.size(index) for index in rows if not model.isDir(index))
        self.local_summary.setText(summary_text(len(rows) - folders, folders, size, bool(selected)))

    def go_local_up(self) -> None:
        parent = os.path.dirname(self.local_directory.rstrip("\\/")) if self.local_directory else ""
        # At the top of a drive the parent is the drive itself; one more step shows all drives.
        self.go_local("" if parent == self.local_directory.rstrip("\\/") or not self.local_directory else parent)

    def _activate_local(self, index) -> None:
        if self.local_model.isDir(index):
            self.go_local(self.local_model.filePath(index))

    def open_local_selected(self) -> None:
        """Open: a folder is entered, a file opened in this computer's program for it."""
        index = self.local_view.currentIndex()
        if not index.isValid():
            return
        if self.local_model.isDir(index):
            self.go_local(self.local_model.filePath(index))
        else:
            self._open_file(self.local_model.filePath(index))

    def selected_local_paths(self) -> list[str]:
        rows = self.local_view.selectionModel().selectedRows(0)
        return [os.path.normpath(self.local_model.filePath(index)) for index in rows]

    def _local_menu(self, pos) -> None:
        paths = self.selected_local_paths()
        here = bool(self.local_directory)
        menu = QMenu(self)
        upload = menu.addAction(line_icon("arrow-right"), "Upload", self.upload_selected)
        upload.setEnabled(bool(paths) and bool(self.directory))
        menu.addSeparator()
        menu.addAction("Open\tEnter", self.open_local_selected).setEnabled(len(paths) == 1)
        menu.addAction("Show in File Manager", self.open_local_folder).setEnabled(here)
        menu.addSeparator()
        menu.addAction("Create Folder...", self.new_local_folder).setEnabled(here)
        menu.addAction("Create Folder and Enter It...", lambda: self.new_local_folder(enter=True)).setEnabled(here)
        menu.addAction("Refresh", lambda: self.go_local(self.local_directory))
        menu.addAction("Copy Path\tCtrl+Shift+C", self.copy_local_paths).setEnabled(bool(paths or here))
        menu.addSeparator()
        menu.addAction("Delete\tDel", self.trash_selected_local).setEnabled(bool(paths))
        menu.addAction("Rename...\tF2", self._rename_local_selected).setEnabled(len(paths) == 1)
        menu.addAction("Properties...", self.show_local_properties).setEnabled(len(paths) == 1)
        menu.exec(self.local_view.viewport().mapToGlobal(pos))

    def open_local_folder(self) -> None:
        if self.local_directory:
            QDesktopServices.openUrl(QUrl.fromLocalFile(self.local_directory))

    def new_local_folder(self, name: str | None = None, enter: bool = False) -> None:
        """Create a folder in the local folder showing; with `enter`, open it as well."""
        if not self.local_directory:
            return
        if name is None or isinstance(name, bool):
            name, ok = QInputDialog.getText(self, "Create Folder", "Folder name")
            if not ok:
                return
        name = name.strip()
        if not name or os.path.basename(name) != name:
            return
        path = os.path.join(self.local_directory, name)
        try:
            os.mkdir(path)
        except OSError as exc:
            self._show_done(f"Could not create the folder: {exc.strerror or exc}", error=True)
            return
        if enter:
            self.go_local(path)

    def rename_local(self, path: str, name: str) -> None:
        name = name.strip()
        target = os.path.join(os.path.dirname(path), name)
        if not name or os.path.basename(name) != name or target == path:
            return
        if os.path.exists(target):
            self._show_done(f"\u201c{name}\u201d already exists", error=True)
            return
        try:
            os.rename(path, target)
        except OSError as exc:
            self._show_done(f"Could not rename: {exc.strerror or exc}", error=True)

    def _rename_local_selected(self) -> None:
        paths = self.selected_local_paths()
        if len(paths) != 1:
            return
        name, ok = QInputDialog.getText(self, "Rename", "New name", text=os.path.basename(paths[0]))
        if ok:
            self.rename_local(paths[0], name)

    @staticmethod
    def _move_to_trash(path: str) -> bool:
        return QFile.moveToTrash(path)

    def trash_selected_local(self) -> None:
        """Send the selected local files to the recycle bin, where they can be restored."""
        paths = self.selected_local_paths()
        if not paths:
            return
        names = ", ".join(os.path.basename(path) for path in paths[:4]) + ("..." if len(paths) > 4 else "")
        answer = QMessageBox.question(self, "Move to Recycle Bin", f"Move to the Recycle Bin?\n\n{names}")
        if answer != QMessageBox.StandardButton.Yes:
            return
        failed = [os.path.basename(path) for path in paths if not self._move_to_trash(path)]
        if failed:
            self._show_done("Could not move to the Recycle Bin: " + ", ".join(failed), error=True)
        else:
            self._show_done(f"Moved {len(paths)} item{'' if len(paths) == 1 else 's'} to the Recycle Bin")

    # ---- the remote host ------------------------------------------------------------

    def go(self, path: str) -> None:
        self.worker.list(path)

    def go_up(self) -> None:
        if self.directory:
            self.go(posixpath.dirname(self.directory.rstrip("/")) or "/")

    def refresh(self) -> None:
        self.go(self.directory or ".")

    def _show_listing(self, directory: str, entries: list) -> None:
        self.directory = directory
        self.path.setText(directory)
        self._set_entries(entries)
        node = self._remote_node(directory)
        self._fill_remote_folders(directory, [entry[0] for entry in entries if entry[1]])
        self.remote_folders.setCurrentItem(node)
        node.setExpanded(True)
        self.remote_folders.scrollToItem(node, QAbstractItemView.ScrollHint.PositionAtCenter)

    def _new_remote_node(self, label: str, path: str) -> QTreeWidgetItem:
        node = QTreeWidgetItem([label])
        node.setData(0, ROLE_PATH, path)
        node.setIcon(0, self._folder_icon)
        node.setChildIndicatorPolicy(QTreeWidgetItem.ChildIndicatorPolicy.ShowIndicator)
        self._remote_nodes[path] = node
        return node

    def _remote_node(self, path: str) -> QTreeWidgetItem:
        """The folder tree's item for a remote folder, made along with its parents if need be."""
        path = path.rstrip("/") or "/"
        node = self._remote_nodes.get(path)
        if node is not None:
            return node
        if path == "/" or "/" not in path:
            node = self._new_remote_node(path, path)
            self.remote_folders.addTopLevelItem(node)
        else:
            parent = self._remote_node(posixpath.dirname(path) or "/")
            node = self._new_remote_node(posixpath.basename(path), path)
            parent.addChild(node)
            parent.sortChildren(0, Qt.SortOrder.AscendingOrder)
        return node

    def _fill_remote_folders(self, directory: str, names: list) -> None:
        """Put a remote folder's subfolders into the folder tree, dropping ones that have gone."""
        node = self._remote_node(directory)
        base = node.data(0, ROLE_PATH)
        shown = [name for name in names if self._show_hidden or not name.startswith(".")]
        wanted = {posixpath.join(base, name) for name in shown}
        for index in reversed(range(node.childCount())):
            child = node.child(index)
            if child.data(0, ROLE_PATH) not in wanted:
                self._forget_remote_node(child)
                node.removeChild(child)
        fresh = []
        for name in shown:
            path = posixpath.join(base, name)
            if "/" in name or not name:
                self._remote_node(path)
            elif path not in self._remote_nodes:
                fresh.append(self._new_remote_node(name, path))
        if fresh:  # added together and sorted once: sorting after each of thousands of folders took seconds
            node.addChildren(fresh)
            node.sortChildren(0, Qt.SortOrder.AscendingOrder)
        node.setData(0, ROLE_FILLED, True)
        node.setChildIndicatorPolicy(QTreeWidgetItem.ChildIndicatorPolicy.DontShowIndicatorWhenChildless)

    def _forget_remote_node(self, node: QTreeWidgetItem) -> None:
        self._remote_nodes.pop(node.data(0, ROLE_PATH), None)
        for index in range(node.childCount()):
            self._forget_remote_node(node.child(index))

    def _remote_folder_expanded(self, node: QTreeWidgetItem) -> None:
        if not node.data(0, ROLE_FILLED):
            self.worker.list_folders(node.data(0, ROLE_PATH))

    def _update_remote_summary(self) -> None:
        if not self.directory:
            return
        selected = self.tree.selectedItems()
        if selected:
            rows = [(bool(item.data(0, ROLE_IS_DIR)), item.data(0, ROLE_SIZE) or 0) for item in selected]
            folders = sum(1 for is_dir, _size in rows if is_dir)
            files, size = len(rows) - folders, sum(size for is_dir, size in rows if not is_dir)
        else:
            files, folders, size = self._shown_totals
        self.remote_summary.setText(summary_text(files, folders, size, bool(selected)))

    def _set_entries(self, entries: list) -> None:
        """Take a fresh listing of the remote folder."""
        self.tree.invisibleRootItem().takeChildren()
        before = {row.entry: row for row in self._rows}  # an entry that has not changed keeps its item
        self._entries = entries
        self._rows = [before.pop(entry, None) or RemoteRow(entry) for entry in entries]
        self._shown = []
        self._shown_totals = (0, 0, 0)
        self._sort_remote_rows()
        self._fill_remote()

    def _sort_remote_rows(self) -> None:
        header = self.tree.header()
        ascending = header.sortIndicatorOrder() == Qt.SortOrder.AscendingOrder
        self._rows = sort_remote_rows(self._rows, header.sortIndicatorSection(), ascending)

    def _resort_remote(self) -> None:
        """The header was clicked: put the rows in the new order and keep what was selected."""
        self._sort_remote_rows()
        self._fill_remote(keep_selection=True)

    def _remote_item(self, row: RemoteRow) -> QTreeWidgetItem:
        name, is_dir, size, modified, mode, owner = row.entry
        item = QTreeWidgetItem(
            [
                name,
                "" if is_dir else human_size(size),
                file_type(name, is_dir),
                format_time(modified) if modified else "",
                stat.filemode(mode) if mode else "",
                owner,
            ]
        )
        item.setData(0, ROLE_MODE, mode)
        item.setIcon(0, self._icon_for(name, is_dir))
        item.setData(0, ROLE_IS_DIR, is_dir)
        item.setData(0, ROLE_SIZE, size)
        item.setData(0, ROLE_MODIFIED, modified)
        item.setTextAlignment(1, _SIZE_ALIGNMENT)
        row.item = item
        return item

    def _fill_remote(self, keep_selection: bool = False) -> None:
        """Show the listing minus hidden files and whatever the filter leaves out.

        Each row's item is made once and then only moved in and out of the list, so typing in the
        filter box stays quick in a folder of tens of thousands of files.
        """
        wanted = self.remote_filter.text().strip().lower()
        show_hidden = self._show_hidden
        shown = [row for row in self._rows if (show_hidden or not row.lower.startswith(".")) and wanted in row.lower]
        if shown != self._shown:
            tree = self.tree
            chosen = tree.selectedItems() if keep_selection else []
            current = tree.currentItem() if keep_selection else None
            root = tree.invisibleRootItem()
            tree.setUpdatesEnabled(False)
            try:
                root.takeChildren()  # the items stay alive in their rows
                root.addChildren([self._remote_item(row) if row.item is None else row.item for row in shown])
                if chosen and len(chosen) == len(shown):
                    tree.selectAll()
                elif chosen:
                    selection = tree.selectionModel()
                    selection.blockSignals(True)  # one summary at the end, not one per row
                    try:
                        for item in chosen:
                            item.setSelected(True)
                    finally:
                        selection.blockSignals(False)
                if current is not None:
                    tree.setCurrentItem(current, 0, QItemSelectionModel.SelectionFlag.NoUpdate)
            finally:
                tree.setUpdatesEnabled(True)
            if current is not None:
                tree.scrollToItem(current)
            self._shown = shown
            folders = sum(1 for row in shown if row.entry[1])
            self._shown_totals = (len(shown) - folders, folders, sum(row.entry[2] or 0 for row in shown if not row.entry[1]))
        self._update_remote_summary()
        if self.transfer_strip.isHidden():
            hidden = len(self._rows) - len(shown)
            extra = f" ({hidden} {'filtered out' if wanted else 'hidden'})" if hidden else ""
            self.status.setText(f"{len(shown)} item{'' if len(shown) == 1 else 's'}{extra}")

    def remote_names(self) -> set[str]:
        return {entry[0] for entry in self._entries}

    def _remote(self, item: QTreeWidgetItem) -> str:
        return posixpath.join(self.directory, item.text(0))

    def _activate_remote(self, item: QTreeWidgetItem, _column: int = 0) -> None:
        if item.data(0, ROLE_IS_DIR):
            self.go(self._remote(item))
        else:
            self.download_items([item])

    # ---- transfers ----------------------------------------------------------------------

    def upload(self, local_paths: list[str]) -> None:
        """Copy local files and folders into the remote folder that is showing."""
        if not (self.directory and local_paths):
            return
        existing = [name for name in (os.path.basename(p.rstrip("/\\")) for p in local_paths) if name in self.remote_names()]
        if existing and self._prefs.transfer_confirm_replace and not self._confirm_replace(existing, self.directory):
            return
        self.status.setText("Uploading...")
        for path in local_paths:  # one transfer for each, so that several can run at once
            self._queue_upload([path], self.directory)

    def _queue_upload(self, local_paths: list[str], remote_dir: str) -> int:
        job = self.worker.upload(local_paths, remote_dir)
        files =[path for path in local_paths if os.path.isfile(path)]
        size = sum(os.path.getsize(path) for path in files) if len(files) == len(local_paths) else None
        label = local_paths[0] if len(local_paths) == 1 else f"{len(local_paths)} items from {os.path.dirname(local_paths[0])}"
        self._add_queue_row(job, label, UPLOAD_ARROW, remote_dir, size, ("upload", list(local_paths), remote_dir))
        return job

    def _queue_download(self, remote: str, local: str, size: int | None) -> int:
        job = self.worker.download(remote, local)
        self._add_queue_row(job, local, DOWNLOAD_ARROW, remote, size, ("download", remote, local, size))
        return job

    def _confirm_replace(self, names: list[str], where: str) -> bool:
        listed = ", ".join(names[:4]) + ("..." if len(names) > 4 else "")
        answer = QMessageBox.question(
            self, "Replace Files", literal(f"These already exist in {where} and will be replaced:\n\n{listed}")
        )
        return answer == QMessageBox.StandardButton.Yes

    def upload_selected(self) -> None:
        paths = self.selected_local_paths()
        if not paths:
            self.status.setText("Select files on this computer to upload.")
        self.upload(paths)

    def download_items(self, items: list[QTreeWidgetItem]) -> None:
        """Copy remote files and folders into the local folder that is showing."""
        if not items:
            self.status.setText("Select remote files to download.")
            return
        if not self.local_directory:
            self.status.setText("Open a local folder to download into.")
            return
        # The names are the server's. Each becomes a file name here, or is refused if nothing usable is left of it.
        named = [(item, local_name(item.text(0))) for item in items]
        refused = [item.text(0) for item, name in named if name is None]
        named = [(item, name) for item, name in named if name is not None]
        if refused:
            listed = ", ".join(refused[:3]) + ("..." if len(refused) > 3 else "")
            self._show_done(f"Not downloaded: a file here cannot have the name {listed}", error=True)
        if not named:
            return
        existing = [name for _item, name in named if os.path.exists(os.path.join(self.local_directory, name))]
        if existing and self._prefs.transfer_confirm_replace and not self._confirm_replace(existing, self.local_directory):
            return
        self.status.setText("Downloading...")
        for item, name in named:
            size = None if item.data(0, ROLE_IS_DIR) else item.data(0, ROLE_SIZE)
            self._queue_download(self._remote(item), os.path.join(self.local_directory, name), size)

    def compare_folders(self) -> None:
        """Compare the local folder showing with the remote one; the differences open in a window of their own."""
        if not (self.directory and self.local_directory):
            self.status.setText("Open a folder on both sides to compare them.")
            return
        self.status.setText("Comparing the two folders...")
        self.worker.compare(self.local_directory, self.directory, self._show_hidden, foldersync.patterns(self.leave_out_text()))

    def leave_out_text(self) -> str:
        """The patterns a comparison leaves out, as last typed; a set of usual ones the first time."""
        stored = self._settings.value("compare_leave_out")
        return foldersync.DEFAULT_LEAVE_OUT if stored is None else str(stored)

    def _compare_again(self, text: str) -> None:
        self._settings.setValue("compare_leave_out", text.strip())
        if self.comparison is not None:
            self.comparison.close()
        self.compare_folders()

    def _show_comparison(self, local_root: str, remote_root: str, differences: list) -> FolderCompareDialog | None:
        if self._checking_sync:
            # The comparison made once the copies were done: a word in the status line is enough.
            self._checking_sync = False
            count = len(differences)
            remain = "1 difference remains" if count == 1 else f"{count} differences remain"
            self.status.setText("Copied: the two folders now match" if not count else f"Copied; {remain}")
            return None
        self.status.setText(f"{len(differences)} difference{'' if len(differences) == 1 else 's'} found" if differences else "The two folders are the same")
        dialog = FolderCompareDialog(local_root, remote_root, differences, self.leave_out_text(), self)
        dialog.compare_again.connect(self._compare_again)
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        dialog.upload_wanted.connect(lambda chosen: self.sync_up(local_root, remote_root, chosen))
        dialog.download_wanted.connect(lambda chosen: self.sync_down(local_root, remote_root, chosen))
        self.comparison = dialog
        dialog.destroyed.connect(lambda: setattr(self, "comparison", None))
        dialog.show()
        return dialog

    def sync_up(self, local_root: str, remote_root: str, differences: list) -> list[int]:
        """Upload what a comparison found, each into its own folder on the server; returns the transfers."""
        jobs = []
        for difference in differences:
            local = os.path.join(local_root, *difference.path.split("/"))
            folder = posixpath.dirname(difference.path)
            jobs.append(self._queue_upload([local], posixpath.join(remote_root, folder) if folder else remote_root))
        self.status.setText(f"Uploading {len(jobs)} item{'' if len(jobs) == 1 else 's'} that differ...")
        self._watch_sync(jobs)
        return jobs

    def sync_down(self, local_root: str, remote_root: str, differences: list) -> list[int]:
        """Download what a comparison found, each into its own local folder; returns the transfers."""
        jobs = []
        for difference in differences:
            local = os.path.join(local_root, *difference.path.split("/"))
            size = None if difference.is_dir else difference.there.size
            jobs.append(self._queue_download(posixpath.join(remote_root, difference.path), local, size))
        self.status.setText(f"Downloading {len(jobs)} item{'' if len(jobs) == 1 else 's'} that differ...")
        self._watch_sync(jobs)
        return jobs

    def _watch_sync(self, jobs: list[int]) -> None:
        """Once these copies are all done, compare again, to say whether the folders now match."""
        self._sync_jobs.update(jobs)

    def _sync_job_done(self, number: int, _ok: bool, _why: str) -> None:
        if number not in self._sync_jobs:
            return
        self._sync_jobs.discard(number)
        if not self._sync_jobs and self.directory and self.local_directory:
            self._checking_sync = True
            self.worker.compare(self.local_directory, self.directory, self._show_hidden, foldersync.patterns(self.leave_out_text()))

    def download_selected(self) -> None:
        self.download_items(self.tree.selectedItems())

    def toggle_pause(self) -> None:
        self.worker.set_paused(not self.worker.is_paused())

    def _show_paused(self, paused: bool) -> None:
        self.pause_button.setText("Resume" if paused else "Pause")
        if paused:
            self.status.setText(f"Paused   {self._last_progress}".rstrip())
            self._add_log("Paused")
        elif self._started:  # the pause also ends when nothing is left running; that is not a resume
            self._add_log("Resumed")

    def _show_resumed(self, label: str, percent: int) -> None:
        self._add_log(f"Continuing {label} from {percent}%")

    def _build_transfer_strip(self) -> QFrame:
        """The progress of what is running, laid out like the line a command-line copy prints:
        the two names with the direction between them, then time elapsed, time left, a bar and the bytes with the speed."""
        self.transfer_local, self.transfer_remote = ElidedLabel(), ElidedLabel()
        self.transfer_arrow = QLabel()
        self.transfer_elapsed, self.transfer_left = QLabel(), QLabel()
        self.transfer_bytes, self.transfer_waiting = QLabel(), QLabel()
        for label in (self.transfer_arrow, self.transfer_elapsed, self.transfer_left, self.transfer_bytes, self.transfer_waiting):
            label.setTextFormat(Qt.TextFormat.PlainText)
        self.transfer_waiting.setObjectName("Muted")
        metrics = self.fontMetrics()
        self.transfer_elapsed.setMinimumWidth(metrics.horizontalAdvance("00:00:00 elapsed"))
        self.transfer_left.setMinimumWidth(metrics.horizontalAdvance("00:00:00 left"))
        self.transfer_bytes.setMinimumWidth(metrics.horizontalAdvance("000,000,000,000 bytes (000.0 MiB/s)"))
        self.transfer_bytes.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        strip = QFrame()
        strip.setObjectName("TransferStrip")
        names = QHBoxLayout()
        names.setSpacing(10)
        names.addWidget(self.transfer_local, 1)
        names.addWidget(self.transfer_arrow)
        names.addWidget(self.transfer_remote, 1)
        numbers = QHBoxLayout()
        numbers.setSpacing(12)
        numbers.addWidget(self.transfer_elapsed)
        numbers.addWidget(self.transfer_left)
        numbers.addWidget(self.progress, 1)
        numbers.addWidget(self.transfer_bytes)
        numbers.addWidget(self.transfer_waiting)
        layout = QVBoxLayout(strip)
        layout.setContentsMargins(12, 8, 12, 8)
        layout.setSpacing(6)
        layout.addLayout(names)
        layout.addLayout(numbers)
        strip.hide()
        return strip

    def transfer_summary(self) -> str:
        """The progress line of the running transfers as text; empty when nothing is running."""
        if self.transfer_strip.isHidden():
            return ""
        parts = [
            self.transfer_local.full_text(),
            self.transfer_arrow.text(),
            self.transfer_remote.full_text(),
            self.transfer_elapsed.text(),
            self.transfer_left.text(),
            self.progress.label(),
            self.transfer_bytes.text(),
            self.transfer_waiting.text(),
        ]
        return "   ".join(part for part in parts if part)

    def _hide_transfer_strip(self) -> None:
        self.transfer_strip.hide()
        self.setWindowTitle(self._base_title)
        if not self._started:
            self._tick.stop()

    def _set_busy(self, busy: bool) -> None:
        finished = self._busy and not busy
        self._busy = busy
        self.cancel_button.setVisible(busy)
        self.pause_button.setVisible(busy)
        if not busy:
            self._hide_transfer_strip()
        if finished and not self.isActiveWindow():
            QApplication.alert(self)  # the taskbar button flashes until the window is looked at

    def _set_waiting(self, _count: int) -> None:
        # The count is read afresh: a report sent from the transfer thread can arrive after
        # newer ones sent from this thread, and would otherwise put back an old number.
        self._waiting = self.worker.waiting_count()

    # ---- the transfer queue ----------------------------------------------------------------

    def _add_queue_row(self, job: int, local: str, direction: str, remote: str, size, again) -> None:
        row = QTreeWidgetItem([local, direction, remote, "" if size is None else human_size(size), "Waiting"])
        row.setTextAlignment(1, Qt.AlignmentFlag.AlignCenter)
        row.setTextAlignment(3, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        row.setData(0, Qt.ItemDataRole.UserRole, job)
        row.setToolTip(0, literal(local))  # names from the server must not be read as HTML
        row.setToolTip(2, literal(remote))
        self._queue_rows[job] = row
        if again is not None:
            self._retry[job] = again
        self.queued_list.addTopLevelItem(row)
        self._update_queue_labels()
        QTimer.singleShot(0, self._make_room_for_queue)

    def _make_room_for_queue(self) -> None:
        """With several transfers queued, show a few of them without scrolling, unless the height was set by hand."""
        count = min(self.queued_list.topLevelItemCount(), QUEUE_ROWS_SHOWN)
        sizes = self._main_split.sizes()
        if self._queue_height_chosen or not count or len(sizes) != 2 or sum(sizes) == 0:
            return
        queue_list = self.queued_list
        # What the queue takes besides its rows (the card, the tabs, the heading) is measured, not added up.
        around = sizes[1] - queue_list.viewport().height()
        needed = around + count * queue_list.sizeHintForRow(0) + 4
        take = min(needed - sizes[1], sizes[0] - MIN_PANES_HEIGHT)
        if take > 0:
            self._main_split.setSizes([sizes[0] - take, sizes[1] + take])

    def _update_queue_labels(self) -> None:
        for index, (name, queue_list) in enumerate(
            (("Queued files", self.queued_list), ("Failed transfers", self.failed_list), ("Successful transfers", self.done_list))
        ):
            count = queue_list.topLevelItemCount()
            self.queue_tabs.setTabText(index, f"{name} ({count})" if count else name)
        self._sync_queue_card()

    def _sync_queue_card(self) -> None:
        """The queue takes room only while it is asked for or has something in it."""
        if not hasattr(self, "queue_button"):
            return  # still being built
        busy = any(l.topLevelItemCount() for l in (self.queued_list, self.failed_list, self.done_list))
        self._queue_card.setVisible(self.queue_button.isChecked() or busy)

    def _job_started(self, job: int) -> None:
        self._started.add(job)
        self._started_at[job] = time.monotonic()
        self._tick.start()
        row = self._queue_rows.get(job)
        if row is not None:
            row.setText(4, "Transferring")
            row.setData(4, ROLE_PROGRESS, 0)

    def _job_finished(self, job: int, succeeded: bool, reason: str) -> None:
        self._started.discard(job)
        self._started_at.pop(job, None)
        self._progress.pop(job, None)
        if self._started:
            self._show_overall()
        else:
            self._tick.stop()
        row = self._queue_rows.pop(job, None)
        if row is None:
            return  # taken out of the queue meanwhile
        row.setData(4, ROLE_PROGRESS, None)
        self.queued_list.takeTopLevelItem(self.queued_list.indexOfTopLevelItem(row))
        if succeeded:
            self._retry.pop(job, None)
            row.setText(4, f"Done at {datetime.now():%H:%M:%S}")
            self.done_list.addTopLevelItem(row)
        else:
            row.setText(4, reason)
            row.setToolTip(4, literal(reason))
            row.setForeground(4, QColor(COLORS["red"]))
            self.failed_list.addTopLevelItem(row)
        self._update_queue_labels()

    def queued_jobs(self) -> list[int]:
        return [self.queued_list.topLevelItem(index).data(0, Qt.ItemDataRole.UserRole) for index in range(self.queued_list.topLevelItemCount())]

    def remove_from_queue(self, jobs: list[int]) -> int:
        """Take transfers that are still waiting out of the queue; returns how many were."""
        removed = 0
        for job in jobs:
            row = self._queue_rows.get(job)
            if row is None or job in self._started or not self.worker.remove_queued(job):
                continue  # one that has begun is cancelled, not removed
            self._queue_rows.pop(job)
            self._retry.pop(job, None)
            self.queued_list.takeTopLevelItem(self.queued_list.indexOfTopLevelItem(row))
            removed += 1
        self._update_queue_labels()
        return removed

    def retry_failed(self, rows: list[QTreeWidgetItem] | None = None) -> int:
        """Queue failed transfers again; returns how many were."""
        rows = rows if rows is not None else [self.failed_list.topLevelItem(i) for i in range(self.failed_list.topLevelItemCount())]
        queued = 0
        for row in rows:
            again = self._retry.pop(row.data(0, Qt.ItemDataRole.UserRole), None)
            if again is None:
                continue
            self.failed_list.takeTopLevelItem(self.failed_list.indexOfTopLevelItem(row))
            if again[0] == "upload":
                self._queue_upload(again[1], again[2])
            else:
                self._queue_download(again[1], again[2], again[3])
            queued += 1
        self._update_queue_labels()
        return queued

    def _clear_list(self, queue_list: QTreeWidget) -> None:
        for index in range(queue_list.topLevelItemCount()):
            self._retry.pop(queue_list.topLevelItem(index).data(0, Qt.ItemDataRole.UserRole), None)
        queue_list.clear()
        self._update_queue_labels()

    def _queued_menu(self, queue_list: QTreeWidget, pos) -> None:
        chosen = [row.data(0, Qt.ItemDataRole.UserRole) for row in queue_list.selectedItems()]
        menu = QMenu(self)
        stop = menu.addAction("Cancel Transfer", lambda: [self.worker.cancel(job) for job in chosen])
        stop.setEnabled(any(job in self._started for job in chosen))
        menu.addAction("Remove from Queue", lambda: self.remove_from_queue(chosen)).setEnabled(
            any(job not in self._started for job in chosen)
        )
        menu.addSeparator()
        menu.addAction("Remove All Waiting", lambda: self.remove_from_queue(self.queued_jobs())).setEnabled(
            any(job not in self._started for job in self.queued_jobs())
        )
        menu.exec(queue_list.viewport().mapToGlobal(pos))

    def _failed_menu(self, queue_list: QTreeWidget, pos) -> None:
        chosen = queue_list.selectedItems()
        menu = QMenu(self)
        menu.addAction("Try Again", lambda: self.retry_failed(chosen)).setEnabled(bool(chosen))
        menu.addAction("Try All Again", self.retry_failed).setEnabled(queue_list.topLevelItemCount() > 0)
        menu.addSeparator()
        menu.addAction("Copy Error", lambda: self.copy_text(chosen[0].text(4))).setEnabled(len(chosen) == 1)
        menu.addAction("Clear List", lambda: self._clear_list(queue_list)).setEnabled(queue_list.topLevelItemCount() > 0)
        menu.exec(queue_list.viewport().mapToGlobal(pos))

    def _done_menu(self, queue_list: QTreeWidget, pos) -> None:
        chosen = queue_list.selectedItems()
        row = chosen[0] if len(chosen) == 1 else None
        menu = QMenu(self)
        menu.addAction("Show in Folder", lambda: self.show_in_folder(row)).setEnabled(row is not None and self._local_target(row) is not None)
        menu.addAction("Copy Local Path", lambda: self.copy_text(row.text(0))).setEnabled(row is not None)
        menu.addSeparator()
        menu.addAction("Clear List", lambda: self._clear_list(queue_list)).setEnabled(queue_list.topLevelItemCount() > 0)
        menu.exec(queue_list.viewport().mapToGlobal(pos))

    @staticmethod
    def _local_target(row: QTreeWidgetItem) -> str | None:
        """The file or folder on this computer that a finished transfer's row stands for, if it is there."""
        path = row.text(0)
        return path if path and os.path.exists(path) else None

    def show_in_folder(self, row: QTreeWidgetItem | None) -> bool:
        """Open the folder of a transferred item on this computer, with the item picked out; False if it is not there."""
        path = self._local_target(row) if row is not None else None
        if path is None:
            return False
        path = os.path.normpath(path)
        if os.name == "nt":
            QProcess.startDetached("explorer.exe", ["/select,", path])  # only picks the item out: nothing is run
        else:
            QDesktopServices.openUrl(QUrl.fromLocalFile(os.path.dirname(path) or "."))
        return True

    def copy_text(self, text: str) -> None:
        QApplication.clipboard().setText(text)
        self.status.setText("Copied")

    def _show_progress(self, job: int, label: str, done: int, total: int, rate: float) -> None:
        self._progress[job] = (label, done, total, rate)
        row = self._queue_rows.get(job)
        if row is not None:
            percent = done * 100 // max(total, 1)
            row.setText(4, f"{done * 100 / max(total, 1):.1f}%   {rate_text(rate)}")
            row.setData(4, ROLE_PROGRESS, percent)
        self._show_overall()

    def _show_overall(self) -> None:
        """One progress line for everything that is running, and a short note in the status bar."""
        running = [job for job in self._started if job in self._progress]
        if not running:
            return
        done = sum(self._progress[job][1] for job in running)
        total = sum(self._progress[job][2] for job in running)
        rate = sum(self._progress[job][3] for job in running)
        paused = self.worker.is_paused()
        label = self._progress[running[0]][0] if len(running) == 1 else f"{len(running)} transfers"
        row = self._queue_rows.get(running[0]) if len(running) == 1 else None
        if row is not None:
            self.transfer_local.set_full_text(row.text(0))
            self.transfer_arrow.setText(row.text(1))
            self.transfer_remote.set_full_text(row.text(2))
        else:  # several at once, or one that is not in the queue
            self.transfer_local.set_full_text(label if len(running) == 1 else f"{len(running)} transfers running")
            self.transfer_arrow.setText("")
            self.transfer_remote.set_full_text("")
        self.transfer_arrow.setVisible(row is not None)
        self.transfer_remote.setVisible(row is not None)
        began = min(self._started_at.get(job, time.monotonic()) for job in running)
        self.transfer_elapsed.setText(f"{clock(time.monotonic() - began)} elapsed")
        if paused:
            self.transfer_left.setText("Paused")
        else:
            self.transfer_left.setText(f"{clock((total - done) / rate) if rate > 0 and total > done else '--:--:--'} left")
        self.progress.set_progress(done * 1000 // max(total, 1), f"{done * 100 / max(total, 1):.1f}%")
        title = f"{done * 100 // max(total, 1)}% - {self._base_title}"  # so the taskbar and Alt+Tab show how far it is
        if self.windowTitle() != title:
            self.setWindowTitle(title)
        self.transfer_bytes.setText(f"{done:,} bytes" if paused else f"{done:,} bytes ({rate_text(rate)})")
        self.transfer_waiting.setText(f"+{self._waiting} waiting" if self._waiting else "")
        self.transfer_waiting.setVisible(bool(self._waiting))
        if self.transfer_strip.isHidden():
            self.transfer_strip.show()
            self.layout().activate()  # the strip took its height from the lists and the queue: give the queue its rows back
            self._make_room_for_queue()
        self._last_progress = label
        if paused:
            # A progress report that was already on its way must not hide that it is paused.
            self.status.setText(f"Paused   {label}")
        else:
            self.status.setText(label)

    def _show_done(self, message: str, error: bool = False) -> None:
        self._hide_transfer_strip()
        self.status.setText(message)
        self._add_log(message, error, ok=not error)
        if error and not self.directory:
            self.remote_summary.setText("Not connected.")

    # ---- remote menu and drag-and-drop ------------------------------------------------------

    def _remote_menu(self, pos) -> None:
        items = self.tree.selectedItems()
        item = items[0] if len(items) == 1 else None
        is_file = item is not None and not item.data(0, ROLE_IS_DIR)
        menu = QMenu(self)
        download = menu.addAction(line_icon("arrow-left"), "Download", self.download_selected)
        download.setEnabled(bool(items) and bool(self.local_directory))
        menu.addSeparator()
        menu.addAction("Open\tEnter", lambda: self._activate_remote(item)).setEnabled(item is not None)
        menu.addAction("Edit", lambda: self.edit_item(item)).setEnabled(is_file)
        menu.addSeparator()
        menu.addAction("Create Folder...", self._new_folder)
        menu.addAction("Create Folder and Enter It...", lambda: self._new_folder(enter=True))
        menu.addAction("Refresh\tF5", self.refresh)
        menu.addAction("Copy Path\tCtrl+Shift+C", self.copy_remote_paths).setEnabled(bool(items or self.directory))
        menu.addSeparator()
        menu.addAction("Delete\tDel", self.delete_selected).setEnabled(bool(items))
        menu.addAction("Rename...\tF2", lambda: self._rename(item)).setEnabled(item is not None)
        menu.addAction("Permissions...", lambda: self.change_permissions(item)).setEnabled(item is not None)
        menu.addAction("Properties...", lambda: self.show_remote_properties(item)).setEnabled(item is not None)
        menu.addSeparator()
        menu.addAction("Open Terminal Here", lambda: self.open_terminal_here(item)).setEnabled(bool(self.directory))
        menu.exec(self.tree.viewport().mapToGlobal(pos))

    def _new_folder(self, enter: bool = False, name: str | None = None) -> None:
        """Create a folder in the remote folder showing; with `enter`, open it as well."""
        if name is None:
            name, ok = QInputDialog.getText(self, "Create Folder", "Folder name")
            if not ok:
                return
        name = name.strip()
        if name and "/" not in name:
            path = posixpath.join(self.directory, name)
            self.worker.make_directory(path)
            if enter:
                self.go(path)  # queued after the folder is made

    def _rename(self, item: QTreeWidgetItem) -> None:
        old = item.text(0)
        name, ok = QInputDialog.getText(self, "Rename", "New name", text=old)
        name = name.strip()
        if ok and name and name != old and "/" not in name:
            self.worker.rename(self._remote(item), posixpath.join(self.directory, name))

    def change_permissions(self, item: QTreeWidgetItem, text: str | None = None) -> bool:
        """Set a remote item's permissions from octal digits such as 644 or 755."""
        current = (item.data(0, ROLE_MODE) or 0) & 0o7777
        if text is None:
            text, ok = QInputDialog.getText(
                self,
                "Permissions",
                f"Permissions for {item.text(0)} as octal digits (for example 644 for a file, 755 for a folder)",
                text=f"{current:03o}",
            )
            if not ok:
                return False
        text = text.strip()
        if not (3 <= len(text) <= 4 and all(digit in "01234567" for digit in text)):
            self._show_done("Permissions are three or four digits from 0 to 7, such as 644", error=True)
            return False
        self.worker.change_mode(self._remote(item), int(text, 8))
        return True

    # ---- filters and bookmarks ---------------------------------------------------------

    def _filter_local(self, text: str) -> None:
        text = text.strip()
        self.local_model.setNameFilterDisables(False)  # hide what does not match instead of greying it
        self.local_model.setNameFilters([f"*{text}*"] if text else [])

    def bookmarks(self, remote: bool) -> list[str]:
        if remote and not self._bookmark_key:
            return list(self._window_bookmarks)
        stored = self._settings.value(self._bookmark_key if remote else "file_bookmarks/local") or []
        return [stored] if isinstance(stored, str) else [str(entry) for entry in stored]

    def _store_bookmarks(self, remote: bool, entries: list[str]) -> None:
        if remote and not self._bookmark_key:
            self._window_bookmarks = entries
        else:
            self._settings.setValue(self._bookmark_key if remote else "file_bookmarks/local", entries)

    def toggle_bookmark(self, remote: bool) -> bool:
        """Bookmark the folder showing on one side, or remove its bookmark; returns whether it is now bookmarked."""
        folder = self.directory if remote else self.local_directory
        if not folder:
            return False
        entries = self.bookmarks(remote)
        if folder in entries:
            entries.remove(folder)
        else:
            entries.append(folder)
        self._store_bookmarks(remote, sorted(entries, key=str.lower))
        return folder in entries

    def _bookmark_menu(self, remote: bool) -> None:
        folder = self.directory if remote else self.local_directory
        entries = self.bookmarks(remote)
        menu = QMenu(self)
        for entry in entries:
            menu.addAction(entry, lambda target=entry: self.go(target) if remote else self.go_local(target))
        if entries:
            menu.addSeparator()
        toggle = menu.addAction(
            "Remove Bookmark for This Folder" if folder in entries else "Bookmark This Folder",
            lambda: self.toggle_bookmark(remote),
        )
        toggle.setEnabled(bool(folder))
        menu.exec(self.cursor().pos())

    def delete_selected(self) -> None:
        """Delete the selected remote files and folders (with what is in them), after one confirmation."""
        items = self.tree.selectedItems()
        if not items:
            return
        if len(items) == 1:
            kind = "folder" if items[0].data(0, ROLE_IS_DIR) else "file"
            more = " and everything in it" if kind == "folder" else ""
            question = f"Delete the remote {kind} \u201c{items[0].text(0)}\u201d{more}? This cannot be undone."
        else:
            names = ", ".join(item.text(0) for item in items[:4]) + ("..." if len(items) > 4 else "")
            more = " Folders are deleted with everything in them." if any(item.data(0, ROLE_IS_DIR) for item in items) else ""
            question = f"Delete these {len(items)} remote items?{more} This cannot be undone.\n\n{names}"
        if QMessageBox.question(self, "Delete", literal(question)) != QMessageBox.StandardButton.Yes:
            return
        for item in items:
            self.worker.delete(self._remote(item), bool(item.data(0, ROLE_IS_DIR)))

    def _side_under(self, pos) -> str:
        """Which list a point of this window is over: "remote", "local" or ""."""
        widget = self.childAt(pos)
        while widget is not None and widget is not self:
            if widget is self.tree:
                return "remote"
            if widget is self.local_view:
                return "local"
            widget = widget.parentWidget()
        return ""

    def _drop_action(self, source, side: str, has_urls: bool) -> str:
        """What a drop would do: "upload", "download" or "" for nothing."""
        if source is self.tree or (source is not None and source is self.tree.viewport()):
            return "download" if side == "local" else ""
        if source is self.local_view or (source is not None and source is self.local_view.viewport()):
            return "upload" if side == "remote" else ""
        # From outside this window (the desktop, a file manager): always an upload.
        return "upload" if has_urls else ""

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls() or event.source() is not None:
            event.acceptProposedAction()

    def dragMoveEvent(self, event):
        side = self._side_under(event.position().toPoint())
        if self._drop_action(event.source(), side, event.mimeData().hasUrls()):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event):
        side = self._side_under(event.position().toPoint())
        action = self._drop_action(event.source(), side, event.mimeData().hasUrls())
        if action == "download":
            event.acceptProposedAction()
            self.download_selected()
        elif action == "upload":
            # Dragged from the local side or from outside (the desktop, a file manager).
            paths = [url.toLocalFile() for url in event.mimeData().urls() if url.isLocalFile()]
            if paths:
                event.acceptProposedAction()
                self.upload(paths)
