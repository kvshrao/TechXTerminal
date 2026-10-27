"""Import sessions from an OpenSSH config file and from Saved Sessions in the Windows registry."""

import getpass
import json
import re
import sys
from dataclasses import asdict
from pathlib import Path
from urllib.parse import unquote

import paramiko

from . import APP_NAME
from .sessions import (
    AUTH_AGENT,
    AUTH_KEY,
    DEFAULT_PORTS,
    PROTO_SERIAL,
    PROTO_SSH,
    PROTOCOLS,
    Session,
    SessionStore,
    parse_jump_hosts,
    session_from_dict,
)

OPENSSH_CONFIG = Path.home() / ".ssh" / "config"
OPENSSH_GROUP = "Imported (OpenSSH)"
# Where the Saved Sessions are kept; the key name is fixed by the program that writes them.
SAVED_SESSIONS_KEY = r"Software\SimonTatham\PuTTY\Sessions"
SAVED_SESSIONS_GROUP = "Imported (Saved Sessions)"


_MATCH_LINE = re.compile(r"\s*match(?:\s|=|$)", re.IGNORECASE)
_HOST_LINE = re.compile(r"\s*host(?:\s|=)", re.IGNORECASE)


def without_match_blocks(text: str) -> str:
    """An ssh_config without its `Match` sections.

    They only adjust a host's settings under conditions, one of which can be a command to run (`Match exec`);
    importing a file must never run anything, and paramiko cannot list the hosts of a file that has one.
    """
    kept, skipping = [], False
    for line in text.splitlines():
        if _MATCH_LINE.match(line):
            skipping = True
        elif _HOST_LINE.match(line):
            skipping = False
        if not skipping:
            kept.append(line)
    return "\n".join(kept) + "\n"


def from_openssh_config(path: Path = OPENSSH_CONFIG) -> list[Session]:
    """Every concrete `Host` entry of an OpenSSH config file; patterns such as `*` are skipped."""
    try:
        config = paramiko.SSHConfig.from_text(without_match_blocks(Path(path).read_text(encoding="utf-8", errors="replace")))
    except (OSError, paramiko.ConfigParseError):
        return []
    sessions = []
    for alias in sorted(config.get_hostnames()):
        if any(character in alias for character in "*?!"):
            continue
        try:
            entry = config.lookup(alias)
        except Exception:  # noqa: BLE001 - paramiko cannot always resolve an entry, such as CanonicalizeHostname without domains
            entry = {}
        try:
            port = int(entry.get("port", 22))
        except ValueError:
            port = 22
        identities = entry.get("identityfile") or []
        jump = entry.get("proxyjump", "")
        if jump.lower() == "none" or not parse_jump_hosts(jump):
            jump = ""  # hops must be given as user@host[:port]
        sessions.append(
            Session(
                name=alias,
                group=OPENSSH_GROUP,
                host=entry.get("hostname", alias),
                port=port,
                username=entry.get("user", getpass.getuser()),
                auth=AUTH_KEY if identities else AUTH_AGENT,
                key_file=identities[0] if identities else "",
                jump_host=jump,
                forward_agent=entry.get("forwardagent", "no").lower() == "yes",
            )
        )
    return sessions


def _number(value, default: int) -> int:
    """A whole number from a registry value, which may be text; the default when it is not one."""
    try:
        return int(value) or default
    except (TypeError, ValueError):
        return default


def session_from_saved(name: str, values: dict) -> Session | None:
    """Build a session from one Saved Session's registry values; None if it has no target."""
    protocol = str(values.get("Protocol", PROTO_SSH)).lower()
    if protocol not in dict(PROTOCOLS):
        return None
    if protocol == PROTO_SERIAL:
        host = str(values.get("SerialLine", ""))
        port = _number(values.get("SerialSpeed"), DEFAULT_PORTS[PROTO_SERIAL])
        username = ""
    else:
        username, _, host = str(values.get("HostName", "")).rpartition("@")
        port = _number(values.get("PortNumber"), DEFAULT_PORTS[protocol])
        username = username or str(values.get("UserName", ""))
        if protocol == PROTO_SSH and not username:
            username = getpass.getuser()
    if not host:
        return None
    key_file = str(values.get("PublicKeyFile", "")) if protocol == PROTO_SSH else ""
    return Session(
        name=name,
        group=SAVED_SESSIONS_GROUP,
        protocol=protocol,
        host=host,
        port=port,
        username=username,
        auth=AUTH_KEY if key_file else AUTH_AGENT,
        key_file=key_file,
        forward_agent=protocol == PROTO_SSH and str(values.get("AgentFwd", 0)) == "1",
    )


def from_saved_sessions() -> list[Session]:
    """Saved Sessions of the current Windows user; empty elsewhere or if there are none."""
    if sys.platform != "win32":
        return []
    import winreg

    sessions = []
    try:
        root = winreg.OpenKey(winreg.HKEY_CURRENT_USER, SAVED_SESSIONS_KEY)
    except OSError:
        return []
    with root:
        for index in range(winreg.QueryInfoKey(root)[0]):
            key_name = winreg.EnumKey(root, index)
            name = unquote(key_name)
            if name == "Default Settings":
                continue
            values = {}
            with winreg.OpenKey(root, key_name) as key:
                for wanted in ("Protocol", "HostName", "PortNumber", "UserName", "PublicKeyFile", "SerialLine", "SerialSpeed"):
                    try:
                        values[wanted] = winreg.QueryValueEx(key, wanted)[0]
                    except OSError:
                        pass
            session = session_from_saved(name, values)
            if session:
                sessions.append(session)
    return sessions


def export_sessions(sessions: list[Session], path: Path) -> int:
    """Write sessions to a file that from_sessions_file() can read back. Passwords are never in it."""
    data = {"application": APP_NAME, "sessions": [asdict(session) for session in sessions]}
    Path(path).write_text(json.dumps(data, indent=2), encoding="utf-8")
    return len(sessions)


def from_sessions_file(path: Path) -> list[Session]:
    """Sessions from a file written by export_sessions(); empty if it cannot be read."""
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        items = raw["sessions"]
    except (OSError, ValueError, KeyError, TypeError):
        return []
    if not isinstance(items, list):
        return []
    sessions = []
    for item in items:
        # Imported sessions get identities of their own, so the saved one is left out.
        session = session_from_dict({key: value for key, value in item.items() if key != "id"} if isinstance(item, dict) else None)
        if session:
            session.save_password = False  # the password stayed in the other computer's credential store
            sessions.append(session)
    return sessions


def merge(store: SessionStore, sessions: list[Session]) -> int:
    """Add the sessions that are not saved yet; returns how many were added."""

    def identity(session: Session) -> tuple:
        return session.protocol, session.host.lower(), session.port, session.username

    known = {identity(session) for session in store.sessions}
    added = 0
    for session in sessions:
        if identity(session) in known:
            continue
        known.add(identity(session))
        store.sessions.append(session)
        added += 1
    if added:
        store.save()
    return added
