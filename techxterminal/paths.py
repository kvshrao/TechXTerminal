"""Where the program keeps its files.

Normally in `.techxterminal` in the user's folder, with the preferences in the registry. A copy that has a
folder named `TechXTerminal-data` next to it (a portable copy, on a USB stick say) keeps all of it there
instead, and leaves nothing behind on the computer it runs on, except saved passwords, which live in the
computer's own credential store.
"""

import sys
from pathlib import Path

PORTABLE_FOLDER = "TechXTerminal-data"


def program_folder() -> Path | None:
    """The folder a built copy runs from; None when running from the source."""
    return Path(sys.executable).parent if getattr(sys, "frozen", False) else None


def portable_folder(program: Path | None = None) -> Path | None:
    """The data folder of a portable copy, if the program has one next to it."""
    program = program if program is not None else program_folder()
    candidate = program / PORTABLE_FOLDER if program else None
    return candidate if candidate is not None and candidate.is_dir() else None


def data_directory(program: Path | None = None) -> Path:
    return portable_folder(program) or Path.home() / ".techxterminal"


CONFIG_DIR = data_directory()
