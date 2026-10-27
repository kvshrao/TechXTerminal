"""Saved sessions, persisted as JSON in the data folder (see paths.py)."""

import json
import os
import importlib.util
import shlex
import shutil
import uuid
from dataclasses import asdict, dataclass, field, fields, replace
from pathlib import Path

from .paths import CONFIG_DIR

_keyring_module = False  # False: not tried yet; None: not usable


def _keyring():
    """The keyring module, loaded the first time it is needed (it takes a tenth of a second); None without it."""
    global _keyring_module
    if _keyring_module is False:
        try:
            import keyring

            _keyring_module = keyring
        except Exception:  # keyring is optional; without it passwords are never stored
            _keyring_module = None
    return _keyring_module

SESSIONS_FILE = CONFIG_DIR / "sessions.json"
KNOWN_HOSTS_FILE = CONFIG_DIR / "known_hosts"
KEYRING_SERVICE = "TechXTerminal"

AUTH_PASSWORD = "password"
AUTH_KEY = "key"
AUTH_AGENT = "agent"

PROTO_RAW = "raw"
PROTO_TELNET = "telnet"
PROTO_RLOGIN = "rlogin"
PROTO_SSH = "ssh"
PROTO_SERIAL = "serial"
PROTO_LOCAL = "local"  # a program on this computer, such as PowerShell; the host is its command line
PROTOCOLS = [
    (PROTO_RAW, "Raw"),
    (PROTO_TELNET, "Telnet"),
    (PROTO_RLOGIN, "Rlogin"),
    (PROTO_SSH, "SSH"),
    (PROTO_SERIAL, "Serial"),
    (PROTO_LOCAL, "Local shell"),
]
# For serial lines the "port" is the speed in baud.
# Character sets offered in the session dialog; any name Python knows can be typed in.
ENCODINGS = ["utf-8", "iso-8859-1", "iso-8859-15", "cp1252", "cp437", "cp850", "koi8-r", "shift_jis", "gbk", "big5", "euc-kr"]
# A local shell has no port; SSH's keeps the field as it was when the protocol is switched there and back.
DEFAULT_PORTS = {PROTO_RAW: 23, PROTO_TELNET: 23, PROTO_RLOGIN: 513, PROTO_SSH: 22, PROTO_SERIAL: 9600, PROTO_LOCAL: 22}


@dataclass
class Session:
    name: str = ""
    host: str = ""
    port: int = 22
    username: str = ""
    auth: str = AUTH_PASSWORD
    key_file: str = ""
    group: str = ""
    save_password: bool = False
    startup_command: str = ""
    auto_responses: str = ""  # "text => answer" lines, see responses.py
    protocol: str = PROTO_SSH
    # SSH only. True: the password is handed to the SSH login itself. False: it is
    # typed into the terminal at the first password prompt, like the other protocols.
    password_in_command_line: bool = True
    wait_ms: int = 0  # pause before a user name or password is typed at a prompt
    command_line: str = ""  # extra options, see apply_command_line()
    color: str = ""  # "#rrggbb" label colour for the tab and sidebar, empty for none
    jump_host: str = ""  # SSH only: "user@host[:port]" hops to connect through, comma-separated
    tunnels: str = ""  # SSH only: port forwards, see parse_tunnels()
    forward_agent: bool = False  # SSH only: the server may use this computer's SSH agent to log in onward
    serial_format: str = "8N1"  # data bits, parity (N/E/O/M/S), stop bits (1, 1.5, 2)
    serial_flow: str = "none"  # none, xonxoff, rtscts or dsrdtr
    notes: str = ""  # free text for the user, shown in the sidebar tooltip
    encoding: str = "utf-8"  # character set the remote side uses
    backspace: str = "del"  # what the Backspace key sends: "del" (^?) or "bs" (^H)
    enter: str = "cr"  # what the Enter key sends: "cr" or "crlf"
    local_echo: bool = False  # show typed characters ourselves, for hosts that do not echo
    favourite: bool = False  # also listed under Favourites at the top of the sidebar
    pinned: bool = False  # its tab opens pinned, at the left of the tab bar
    start_folder: str = ""  # local shells only: the folder they start in, empty for the user's own
    id: str = field(default_factory=lambda: uuid.uuid4().hex)

    @property
    def title(self) -> str:
        return self.name or self.host

    @property
    def address(self) -> str:
        if self.protocol == PROTO_SERIAL:
            return f"{self.host} @ {self.port} baud"
        if self.protocol == PROTO_LOCAL:
            return f"local://{self.host}"
        addr = f"{self.username}@{self.host}" if self.username else self.host
        if self.port != DEFAULT_PORTS.get(self.protocol):
            addr = f"{addr}:{self.port}"
        return addr if self.protocol == PROTO_SSH else f"{self.protocol}://{addr}"


TUNNEL_LOCAL = "L"  # a local port leads to a host reachable from the server
TUNNEL_REMOTE = "R"  # a port on the server leads back to a host reachable from here
TUNNEL_DYNAMIC = "D"  # a local SOCKS proxy; each connection names its own destination


@dataclass
class Tunnel:
    listen_port: int
    host: str = ""
    port: int = 0
    listen_host: str = "127.0.0.1"
    kind: str = TUNNEL_LOCAL
    username: str = ""  # dynamic tunnels only: programs must log in to the proxy with these
    password: str = ""


def parse_tunnels(text: str) -> tuple[list[Tunnel], list[str]]:
    """Parse tunnel entries separated by commas or spaces.

    Local:   [L:][bind:]listen_port:host:port
    Remote:  R:[bind:]listen_port:host:port
    Dynamic: D:[bind:]listen_port, or D:user:password@[bind:]listen_port for a proxy with a login
    Returns the tunnels and the entries that could not be understood.
    """

    def valid_port(value: str) -> bool:
        return value.isdigit() and 0 < int(value) < 65536

    tunnels, bad = [], []
    for entry in text.replace(",", " ").split():
        username = password = ""
        address = entry
        if entry[:2].upper() == TUNNEL_DYNAMIC + ":" and "@" in entry:
            # The login may itself contain colons, so it is taken off before splitting.
            login, _, rest = entry[2:].rpartition("@")
            username, _, password = login.partition(":")
            if not username or not password:
                bad.append(entry)
                continue
            address = entry[:2] + rest
        parts = address.split(":")
        kind = TUNNEL_LOCAL
        if parts[0].upper() in (TUNNEL_LOCAL, TUNNEL_REMOTE, TUNNEL_DYNAMIC) and len(parts) > 1:
            kind, parts = parts[0].upper(), parts[1:]
        wanted = (1, 2) if kind == TUNNEL_DYNAMIC else (3, 4)
        if len(parts) not in wanted:
            bad.append(entry)
            continue
        listen_host = parts.pop(0) if len(parts) == wanted[1] else ""
        if kind == TUNNEL_DYNAMIC:
            listen_port, host, port = parts[0], "", "1"
        else:
            listen_port, host, port = parts
            if not host:
                bad.append(entry)
                continue
        if not (valid_port(listen_port) and valid_port(port)):
            bad.append(entry)
            continue
        tunnels.append(
            Tunnel(
                listen_port=int(listen_port),
                host=host,
                port=int(port) if kind != TUNNEL_DYNAMIC else 0,
                listen_host=listen_host or "127.0.0.1",
                kind=kind,
                username=username,
                password=password,
            )
        )
    return tunnels, bad


def parse_jump_hosts(text: str) -> list[tuple[str, str, int]] | None:
    """Split "user@gate1, user@gate2:2200" into hops in connection order; None if one is malformed."""
    hops = []
    for part in text.split(","):
        if not part.strip():
            continue
        spec = parse_host_spec(part)
        if spec is None:
            return None
        hops.append(spec)
    return hops


def parse_host_spec(spec: str, default_port: int = 22) -> tuple[str, str, int] | None:
    """Split "user@host[:port]" into (user, host, port); None if it is not in that form."""
    user, _, rest = spec.strip().rpartition("@")
    host, _, port = rest.partition(":")
    if not host or (port and not port.isdigit()):
        return None
    return user, host, int(port) if port else default_port


@dataclass
class CommandLine:
    session: Session  # the session with the options applied
    password: str | None = None
    compress: bool = False
    ignored: list[str] = field(default_factory=list)


def apply_command_line(session: Session) -> CommandLine:
    """Apply a session's "Command line" options.

    Understood: -ssh -telnet -rlogin -raw -serial, -P port, -l user, -pw password,
    -i keyfile, -C (compression), -A (forward the SSH agent), -L / -R [bind:]port:host:port
    (local / remote tunnel), -D [bind:]port (SOCKS proxy) and -J user@host[:port][,user@host...] (jump hosts).
    Anything else is reported back as ignored.
    """
    result = CommandLine(session)
    try:
        args = [arg.strip('"') for arg in shlex.split(session.command_line, posix=False)]
    except ValueError:
        result.ignored.append(session.command_line)
        return result
    changes: dict = {}
    protocols = {f"-{name}": name for name, _label in PROTOCOLS}
    position = 0
    while position < len(args):
        arg = args[position]
        position += 1
        if arg in protocols:
            changes["protocol"] = protocols[arg]
        elif arg == "-C":
            result.compress = True
        elif arg == "-A":
            changes["forward_agent"] = True
        elif arg in ("-P", "-l", "-pw", "-i", "-L", "-R", "-D", "-J") and position < len(args):
            value = args[position]
            position += 1
            if arg == "-P":
                if value.isdigit():
                    changes["port"] = int(value)
                else:
                    result.ignored.append(f"-P {value}")
            elif arg == "-l":
                changes["username"] = value
            elif arg == "-pw":
                result.password = value
            elif arg in ("-L", "-R", "-D"):
                entry = value if arg == "-L" else f"{arg[1]}:{value}"
                changes["tunnels"] = f"{changes.get('tunnels', session.tunnels)} {entry}".strip()
            elif arg == "-J":
                changes["jump_host"] = value
            else:
                changes.update(auth=AUTH_KEY, key_file=value)
        else:
            result.ignored.append(arg)
    if changes:
        result.session = replace(session, **changes)
    return result


def write_atomically(path: Path, text: str) -> None:
    """Replace a file's contents in one step, so a crash or power cut cannot leave half a file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)


def set_aside(path: Path) -> Path | None:
    """Keep a copy of a file that could not be read, before anything overwrites it."""
    copy = path.with_name(path.name + ".corrupt")
    try:
        shutil.copyfile(path, copy)
    except OSError:
        return None
    return copy


def session_from_dict(item) -> Session | None:
    """A session from saved data, or None when it has no host.

    A value of the wrong type (a hand-edited file, say) falls back to the default.
    """
    if not isinstance(item, dict):
        return None
    defaults = Session()
    clean = {
        f.name: item[f.name]
        for f in fields(Session)
        if f.name in item and type(item[f.name]) is type(getattr(defaults, f.name))
    }
    return Session(**clean) if clean.get("host") else None


class SessionStore:
    def __init__(self, path: Path = SESSIONS_FILE):
        self.path = path
        self.sessions: list[Session] = []
        self.load_error = ""
        self.load()

    def load(self) -> None:
        self.sessions = []
        self.load_error = ""  # set when the file exists but could not be understood
        try:
            text = self.path.read_text(encoding="utf-8")
        except OSError:
            return  # no file yet
        try:
            items = json.loads(text)["sessions"]
            if not isinstance(items, list):
                raise ValueError("not a list")
        except (ValueError, KeyError, TypeError):
            # Do not just start empty: the next save would wipe whatever is in the file.
            copy = set_aside(self.path)
            kept = f" It was kept as {copy.name}." if copy else ""
            self.load_error = f"The saved sessions in {self.path.name} could not be read.{kept}"
            return
        for item in items:
            session = session_from_dict(item)
            if session:
                self.sessions.append(session)

    def save(self) -> None:
        data = {"sessions": [asdict(s) for s in self.sessions]}
        write_atomically(self.path, json.dumps(data, indent=2))

    def get(self, session_id: str) -> Session | None:
        return next((s for s in self.sessions if s.id == session_id), None)

    def upsert(self, session: Session) -> None:
        for i, existing in enumerate(self.sessions):
            if existing.id == session.id:
                self.sessions[i] = session
                break
        else:
            self.sessions.append(session)
        self.save()

    def remove(self, session: Session) -> None:
        self.sessions = [s for s in self.sessions if s.id != session.id]
        self.set_password(session, None)
        self.save()

    def groups(self) -> list[str]:
        return sorted({s.group for s in self.sessions if s.group}, key=str.lower)

    # Passwords live in the OS credential store, never in sessions.json.

    @staticmethod
    def can_store_passwords() -> bool:
        return importlib.util.find_spec("keyring") is not None  # found, not loaded: asking must stay quick

    @staticmethod
    def get_password(session: Session) -> str | None:
        keyring = _keyring()
        if keyring is None or not session.save_password:
            return None
        try:
            return keyring.get_password(KEYRING_SERVICE, session.id)
        except Exception:
            return None

    @staticmethod
    def set_password(session: Session, password: str | None) -> None:
        keyring = _keyring()
        if keyring is None:
            return
        try:
            if password:
                keyring.set_password(KEYRING_SERVICE, session.id, password)
            else:
                keyring.delete_password(KEYRING_SERVICE, session.id)
        except Exception:
            pass
