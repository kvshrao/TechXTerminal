"""Names and text that come from the other end of a connection are not to be trusted.

A server decides what its files are called, what its software calls itself and what it says when it
hangs up. A file named `..\\..\\Startup\\x.bat` is an ordinary name on a Linux server; written here
unchecked it lands outside the folder it was meant for. A name or message that looks like HTML is
drawn as HTML by a Qt label, tooltip or message box, which can pull in images, even from a network
share. These helpers make both harmless.
"""

import html
import os

_WINDOWS_DEVICES = {"con", "prn", "aux", "nul", *(f"com{n}" for n in range(1, 10)), *(f"lpt{n}" for n in range(1, 10))}
_NOT_IN_WINDOWS_NAMES = '<>:"/\\|?*'

# What Windows runs when it is asked to open one of these, instead of showing it in an editor.
RUNS_WHEN_OPENED = {
    ".exe", ".com", ".scr", ".pif", ".bat", ".cmd", ".msi", ".msp", ".lnk", ".url", ".hta", ".cpl", ".reg",
    ".vbs", ".vbe", ".js", ".jse", ".wsf", ".wsh", ".ps1", ".psm1", ".jar", ".appx", ".msix", ".gadget", ".application",
}  # fmt: skip


def local_name(name: str) -> str | None:
    """A file name taken from a server, made fit to use as a file name here; None if nothing usable is left.

    Separators and the other characters that cannot be in a file name here become "_", so that a name can
    never reach beyond the folder it is created in. Names that mean something to the file system ("", ".",
    "..") and names made of dots alone are refused. On Windows a trailing dot or space (which Windows drops)
    is removed and a device name such as "con" is made an ordinary one.
    """
    cleaned = "".join("_" if ord(character) < 32 or character == "\x7f" else character for character in name)
    for character in _NOT_IN_WINDOWS_NAMES if os.name == "nt" else "/":
        cleaned = cleaned.replace(character, "_")
    if os.name == "nt":
        cleaned = cleaned.rstrip(" .")
        if cleaned.split(".")[0].rstrip(" ").lower() in _WINDOWS_DEVICES:
            cleaned = "_" + cleaned
    if not cleaned or set(cleaned) <= {"."}:
        return None
    return cleaned


def is_inside(folder: str, path: str) -> bool:
    """Whether `path` is in `folder` or below it, as the file system will see it ("..", links to drives and all)."""
    folder, path = os.path.normcase(os.path.abspath(folder)), os.path.normcase(os.path.abspath(path))
    try:
        return os.path.commonpath([folder, path]) == folder
    except ValueError:  # another drive
        return False


def opens_by_running(name: str) -> bool:
    """Whether Windows would run a file of this name when asked to open it, rather than show it in an editor."""
    return os.name == "nt" and os.path.splitext(name.rstrip(" ."))[1].lower() in RUNS_WHEN_OPENED


def literal(text: str) -> str:
    """Text for a widget that reads HTML (a tooltip, a message box), made to show exactly as written."""
    return "<qt>" + html.escape(text).replace("\n", "<br>") + "</qt>"
