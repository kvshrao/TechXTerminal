"""The host keys that were approved, as kept in the known_hosts file: listed, and forgotten one by one."""

from dataclasses import dataclass
from pathlib import Path

import paramiko
from paramiko.hostkeys import HostKeyEntry, InvalidHostKey

from .sessions import KNOWN_HOSTS_FILE, write_atomically
from .ssh import fingerprint


@dataclass
class ApprovedKey:
    host: str  # as kept: "name", or "[name]:port" for a port other than 22; "(hidden)" for a hashed name
    key_type: str
    fingerprint: str
    hashed: bool = False  # the name was stored as a hash, so it cannot be shown or picked out by name


def _entries(path: Path) -> list[tuple[str, HostKeyEntry | None]]:
    """Each line of the file with what could be read from it; comments and damaged lines have None."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return []
    entries = []
    for line in lines:
        try:
            entries.append((line, None if line.lstrip().startswith("#") else HostKeyEntry.from_line(line)))
        except (InvalidHostKey, ValueError, paramiko.SSHException):
            entries.append((line, None))
    return entries


def approved_keys(path: Path = KNOWN_HOSTS_FILE) -> list[ApprovedKey]:
    """Every key in the file, one per host name it is kept for, sorted by name."""
    found = []
    for _, entry in _entries(path):
        if entry is None:
            continue
        for name in entry.hostnames:
            hashed = name.startswith("|1|")
            found.append(
                ApprovedKey("(hidden)" if hashed else name, entry.key.get_name(), fingerprint(entry.key), hashed)
            )
    return sorted(found, key=lambda key: (key.host.lower(), key.key_type))


def forget_host(host: str, path: Path = KNOWN_HOSTS_FILE) -> int:
    """Remove every key kept for the host name; returns how many were removed.

    A line that holds several names keeps the others. Lines that are not keys (comments, damaged ones) stay as
    they are, and nothing is written when nothing matched.
    """
    kept, removed = [], 0
    for line, entry in _entries(path):
        if entry is None or host not in entry.hostnames or host.startswith("|1|"):
            kept.append(line)
            continue
        removed += 1
        others = [name for name in entry.hostnames if name != host]
        if others:
            entry.hostnames = others
            kept.append(entry.to_line().rstrip("\n"))
    if removed:
        write_atomically(path, "\n".join(kept) + ("\n" if kept else ""))
    return removed
