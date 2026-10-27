"""Keeping background threads alive until they have really ended.

Qt ends the whole process if a QThread object is destroyed while its thread is still
running. A thread that is busy with the network cannot always be stopped at once, so
instead of waiting for it (and freezing the window) its object is parked here.
"""

from PyQt6.QtCore import QThread

_parked: set[QThread] = set()


def park(thread: QThread) -> None:
    """Hold on to a thread object until its thread has finished. Call from the UI thread."""
    # Earlier ones are dropped here, from the UI thread, never from their own thread.
    _parked.difference_update([parked for parked in _parked if parked.isFinished()])
    if thread.isRunning():
        _parked.add(thread)


def parked() -> list[QThread]:
    return list(_parked)


def wait_all(timeout_ms: int = 1500) -> None:
    """At exit: give every parked thread a moment to end, then stop the ones that have not."""
    for thread in list(_parked):
        if not thread.wait(timeout_ms):
            thread.terminate()
            thread.wait(500)
    _parked.clear()
