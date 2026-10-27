"""Compare a folder on this computer with one on the server, and say what differs, to copy just that.

Both sides are walked to the bottom. Files are matched by their path below the two folders and told apart by
size and modification time; times within TIME_SLACK count as the same, as some file systems keep them only
to the even second. A folder found on one side only is one difference, not one for each file inside it.
"""

import fnmatch
import os
import posixpath
import stat
from dataclasses import dataclass

TIME_SLACK = 2.0
MAX_ENTRIES = 20_000  # past this the walk stops: such a comparison is better left to a tool made for it
# Left out of a comparison unless changed: what tools and systems keep beside the files, and nobody copies.
DEFAULT_LEAVE_OUT = ".git, .svn, .hg, node_modules, __pycache__, .DS_Store, Thumbs.db, desktop.ini"

ONLY_HERE, ONLY_THERE = "only_here", "only_there"
NEWER_HERE, NEWER_THERE = "newer_here", "newer_there"
DIFFERENT = "different"  # the same date, another size: which way to copy is not for the program to guess
UPLOADS = (ONLY_HERE, NEWER_HERE, DIFFERENT)
DOWNLOADS = (ONLY_THERE, NEWER_THERE, DIFFERENT)
WORDS = {
    ONLY_HERE: "Only on this computer",
    ONLY_THERE: "Only on the server",
    NEWER_HERE: "Newer on this computer",
    NEWER_THERE: "Newer on the server",
    DIFFERENT: "Same date, different size",
}


class TooMany(Exception):
    pass


def patterns(text: str) -> list[str]:
    """The patterns in "a, b c": names or wildcards such as *.log, separated by commas or spaces."""
    return [part for part in text.replace(",", " ").split() if part]


def left_out(name: str, leave_out: list[str]) -> bool:
    """Whether a file or folder of this name is left out; a folder left out takes everything in it along."""
    return any(fnmatch.fnmatchcase(name.lower(), pattern.lower()) for pattern in leave_out)


@dataclass(frozen=True)
class Entry:
    path: str  # below the folder compared, with "/" between the parts
    is_dir: bool
    size: int
    modified: float


@dataclass(frozen=True)
class Difference:
    path: str
    kind: str
    is_dir: bool
    here: Entry | None
    there: Entry | None


def walk_local(root: str, hidden: bool = True, leave_out: list[str] = ()) -> dict[str, Entry]:
    found: dict[str, Entry] = {}
    for folder, dirs, files in os.walk(root):
        below = os.path.relpath(folder, root).replace(os.sep, "/")
        prefix = "" if below == "." else below + "/"
        if not hidden:
            dirs[:] = [name for name in dirs if not name.startswith(".")]
            files = [name for name in files if not name.startswith(".")]
        dirs[:] = [name for name in dirs if not left_out(name, leave_out)]  # not walked into either
        files = [name for name in files if not left_out(name, leave_out)]
        for name in dirs + files:
            full = os.path.join(folder, name)
            try:
                info = os.stat(full)
            except OSError:
                continue  # gone, or not to be read: it cannot be copied either
            is_dir = stat.S_ISDIR(info.st_mode)
            found[prefix + name] = Entry(prefix + name, is_dir, 0 if is_dir else info.st_size, info.st_mtime)
            if len(found) > MAX_ENTRIES:
                raise TooMany(f"More than {MAX_ENTRIES:,} files and folders on this computer to compare")
    return found


def walk_remote(sftp, root: str, hidden: bool = True, leave_out: list[str] = ()) -> dict[str, Entry]:
    found: dict[str, Entry] = {}
    waiting = [""]
    while waiting:
        below = waiting.pop()
        for attr in sftp.listdir_attr(posixpath.join(root, below) if below else root):
            name = attr.filename
            if name in (".", "..") or (not hidden and name.startswith(".")) or left_out(name, leave_out):
                continue
            path = posixpath.join(below, name) if below else name
            is_dir = stat.S_ISDIR(attr.st_mode or 0)
            found[path] = Entry(path, is_dir, 0 if is_dir else attr.st_size or 0, float(attr.st_mtime or 0))
            if is_dir:
                waiting.append(path)
            if len(found) > MAX_ENTRIES:
                raise TooMany(f"More than {MAX_ENTRIES:,} files and folders on the server to compare")
    return found


def compare(here: dict[str, Entry], there: dict[str, Entry]) -> list[Difference]:
    """What differs, folders first and then files, by path; nothing below a folder that is on one side only."""
    found = []
    one_sided: list[str] = []  # folders on one side only, whose contents are not listed

    def inside_one_sided(path: str) -> bool:
        return any(path.startswith(folder + "/") for folder in one_sided)

    for path in sorted(set(here) | set(there), key=lambda p: (p.count("/"), p.lower())):
        if inside_one_sided(path):
            continue
        mine, theirs = here.get(path), there.get(path)
        if theirs is None or mine is None:
            entry = mine or theirs
            found.append(Difference(path, ONLY_HERE if theirs is None else ONLY_THERE, entry.is_dir, mine, theirs))
            if entry.is_dir:
                one_sided.append(path)
            continue
        if mine.is_dir or theirs.is_dir:
            continue  # a folder on both sides: what matters is inside it (a file and a folder of one name is left)
        if mine.size == theirs.size and abs(mine.modified - theirs.modified) <= TIME_SLACK:
            continue
        if abs(mine.modified - theirs.modified) <= TIME_SLACK:
            kind = DIFFERENT
        else:
            kind = NEWER_HERE if mine.modified > theirs.modified else NEWER_THERE
        found.append(Difference(path, kind, False, mine, theirs))
    return sorted(found, key=lambda d: (not d.is_dir, d.path.lower()))
