"""What happens to an error nobody expected: it is logged, and the application carries on.

Without a hook of our own, PyQt ends the whole application when an error escapes from a
slot, taking every open session with it.
"""

import sys
import traceback
from datetime import datetime

from .sessions import CONFIG_DIR

ERROR_LOG = CONFIG_DIR / "error.log"
MAX_LOG_BYTES = 1_000_000

# Unexpected errors since start, newest last; also appended to ERROR_LOG.
unhandled: list[str] = []
# Called with the error text, e.g. to tell the user in the status bar.
listeners: list = []


def report(kind, value, trace) -> None:
    text = "".join(traceback.format_exception(kind, value, trace))
    unhandled.append(text)
    del unhandled[:-50]  # a repeating error must not use up memory
    print(text, file=sys.stderr)
    try:
        ERROR_LOG.parent.mkdir(parents=True, exist_ok=True)
        if ERROR_LOG.exists() and ERROR_LOG.stat().st_size > MAX_LOG_BYTES:
            ERROR_LOG.replace(ERROR_LOG.with_name(ERROR_LOG.name + ".old"))
        with open(ERROR_LOG, "a", encoding="utf-8") as handle:
            handle.write(f"--- {datetime.now():%Y-%m-%d %H:%M:%S} ---\n{text}\n")
    except OSError:
        pass
    for listener in list(listeners):
        try:
            listener(text)
        except Exception:
            pass  # reporting an error must never raise another


def install() -> None:
    sys.excepthook = report
