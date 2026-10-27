"""SSH connection worker: login (including two-factor), jump hosts, tunnels and SFTP access."""

import base64
import hashlib
import hmac
import select
import socket
import struct
import threading
import time
from dataclasses import dataclass

import paramiko
from paramiko.agent import AgentRequestHandler
from paramiko.common import cMSG_CHANNEL_REQUEST

from . import ppk
from .connections import (
    BREAK_SECONDS,
    CONNECT_TIMEOUT,
    PROMPT_HOSTKEY,
    PROMPT_INPUT,
    PROMPT_PASSPHRASE,
    PROMPT_PASSWORD,
    TERMINAL_TYPE,
    ConnectionWorker,
    open_tcp,
)
from .sessions import (
    AUTH_KEY,
    AUTH_PASSWORD,
    KNOWN_HOSTS_FILE,
    TUNNEL_DYNAMIC,
    TUNNEL_REMOTE,
    Tunnel,
    parse_jump_hosts,
    parse_tunnels,
)

MAX_AUTH_ATTEMPTS = 4
SLOW_STEP_SECONDS = 1.0  # a connection step that took longer than this is mentioned, with its time
SLOW_LOGIN_SECONDS = 5.0  # a login this slow is explained: the server is the usual cause


def fingerprint(key: paramiko.PKey) -> str:
    digest = hashlib.sha256(key.asbytes()).digest()
    return "SHA256:" + base64.b64encode(digest).decode().rstrip("=")


def _read_exactly(conn: socket.socket, count: int) -> bytes:
    data = b""
    while len(data) < count:
        chunk = conn.recv(count - len(data))
        if not chunk:
            raise ValueError("connection closed during the SOCKS handshake")
        data += chunk
    return data


def _read_until_nul(conn: socket.socket, limit: int = 512) -> bytes:
    data = b""
    while len(data) < limit:
        byte = _read_exactly(conn, 1)
        if byte == b"\0":
            return data
        data += byte
    raise ValueError("SOCKS field too long")


def _read_socks_request(conn: socket.socket, login: tuple[str, str] | None = None) -> tuple[str, int, int] | None:
    """Read a SOCKS4, SOCKS4a or SOCKS5 CONNECT request; returns (host, port, version).

    Anything other than a plain CONNECT is answered with a refusal and gives None. With a
    login, only SOCKS5 clients that give that user name and password are served.
    """
    version = _read_exactly(conn, 1)[0]
    if version == 5:
        methods = _read_exactly(conn, _read_exactly(conn, 1)[0])  # ways the client can log in
        if login:
            if 2 not in methods:  # the client cannot do user name and password
                conn.sendall(b"\x05\xff")
                return None
            conn.sendall(b"\x05\x02")
            _read_exactly(conn, 1)  # version of the login exchange
            name = _read_exactly(conn, _read_exactly(conn, 1)[0])
            secret = _read_exactly(conn, _read_exactly(conn, 1)[0])
            # compare_digest takes the same time whether or not the guess is close
            accepted = hmac.compare_digest(name, login[0].encode()) and hmac.compare_digest(secret, login[1].encode())
            conn.sendall(b"\x01\x00" if accepted else b"\x01\x01")
            if not accepted:
                return None
        else:
            conn.sendall(b"\x05\x00")
        _version, command, _reserved, kind = _read_exactly(conn, 4)
        if kind == 1:
            host = socket.inet_ntoa(_read_exactly(conn, 4))
        elif kind == 3:
            host = _read_exactly(conn, _read_exactly(conn, 1)[0]).decode("ascii", "replace")
        elif kind == 4:
            host = socket.inet_ntop(socket.AF_INET6, _read_exactly(conn, 16))
        else:
            return None
        (port,) = struct.unpack(">H", _read_exactly(conn, 2))
        if command != 1:
            conn.sendall(b"\x05\x07\x00\x01" + bytes(6))  # command not supported
            return None
        return host, port, 5
    if version == 4:
        command = _read_exactly(conn, 1)[0]
        (port,) = struct.unpack(">H", _read_exactly(conn, 2))
        address = _read_exactly(conn, 4)
        _read_until_nul(conn)  # user id, unused
        if address[:3] == b"\0\0\0" and address[3]:  # SOCKS4a: a host name follows
            host = _read_until_nul(conn).decode("ascii", "replace")
        else:
            host = socket.inet_ntoa(address)
        # SOCKS4 has no way to log in, so a proxy with a login refuses it. The request is
        # read in full first: hanging up on unread data would reset the connection instead.
        if command != 1 or login:
            conn.sendall(b"\x00\x5b" + bytes(6))
            return None
        return host, port, 4
    return None


def is_dsa_key(path: str) -> bool:
    """Whether a private key file holds a DSA key, in any of the usual file formats."""
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            text = handle.read(8192)
    except OSError:
        return False
    first = text.splitlines()[0] if text else ""
    if "BEGIN DSA PRIVATE KEY" in first or (first.startswith("PuTTY-User-Key-File-") and first.endswith("ssh-dss")):
        return True
    if "BEGIN OPENSSH PRIVATE KEY" in first:
        body = "".join(line for line in text.splitlines()[1:] if not line.startswith("-----"))
        try:
            return b"ssh-dss" in base64.b64decode(body[: len(body) // 4 * 4])[:160]
        except ValueError:
            return False
    return False


@dataclass
class RunningTunnel:
    tunnel: Tunnel
    description: str
    listener: socket.socket | None = None  # local and SOCKS tunnels; a remote one listens on the server


def describe_tunnel(tunnel: Tunnel) -> str:
    if tunnel.kind == TUNNEL_DYNAMIC:
        needs_login = " (login required)" if tunnel.username else ""
        return f"SOCKS proxy on {tunnel.listen_host}:{tunnel.listen_port}{needs_login}"
    remote = "remote " if tunnel.kind == TUNNEL_REMOTE else ""
    return f"Forwarding {remote}{tunnel.listen_host}:{tunnel.listen_port} -> {tunnel.host}:{tunnel.port}"


class _Cancelled(paramiko.SSHException):
    """The user dismissed a prompt; do not ask again."""


class _AskUserPolicy(paramiko.MissingHostKeyPolicy):
    """Unknown host keys are shown to the user and only trusted on approval."""

    def __init__(self, worker: "SSHWorker"):
        self.worker = worker

    def missing_host_key(self, client, hostname, key):
        message = (
            f"The authenticity of host '{hostname}' can't be established.\n\n"
            f"{key.get_name()} key fingerprint:\n{fingerprint(key)}\n\n"
            "Trust this host and continue connecting?"
        )
        if self.worker.ask(PROMPT_HOSTKEY, message) != "yes":
            raise _Cancelled("Host key was not accepted")
        client.get_host_keys().add(hostname, key.get_name(), key)
        try:
            client.save_host_keys(str(KNOWN_HOSTS_FILE))
        except OSError:
            pass


class _Client(paramiko.SSHClient):
    """SSHClient that also completes keyboard-interactive logins (one-time codes and the like)."""

    interactive = None  # callable(title, instructions, [(prompt, echo)]) -> [answers]

    def _auth(self, username, *args, **kwargs):
        error = None
        try:
            super()._auth(username, *args, **kwargs)
        except paramiko.PasswordRequiredException:
            raise  # the key file needs its passphrase first
        except paramiko.SSHException as exc:
            error = exc
        transport = self._transport
        if transport.is_authenticated():
            return
        # Either nothing worked, or the server accepted a first factor and wants more.
        if self.interactive and transport.is_active():
            try:
                transport.auth_interactive(username, self.interactive)
            except _Cancelled:
                raise
            except paramiko.SSHException as exc:
                error = error or exc
            if transport.is_authenticated():
                return
        raise error or paramiko.AuthenticationException("Authentication failed")


SFTP_WINDOW_BYTES = 16 * 1024 * 1024


class SSHWorker(ConnectionWorker):
    def __init__(self, session, password, cols, rows, compress: bool = False, keepalive: int = 30, parent=None):
        super().__init__(session, password, cols, rows, parent)
        self.keepalive = keepalive
        self._compress = compress
        self._client: paramiko.SSHClient | None = None
        self._jumps: list[paramiko.SSHClient] = []  # intermediate hops, in connection order
        self._chan: paramiko.Channel | None = None
        self._tunnels: list[RunningTunnel] = []
        self._tunnels_lock = threading.Lock()  # tunnels are started and stopped from the window too
        self._remote_routes: dict[int, Tunnel] = {}  # server port -> remote tunnel, see _route_remote
        self._agent: AgentRequestHandler | None = None  # set while the agent is forwarded

    # ---- called from other threads ---------------------------------------

    def send(self, data: bytes) -> None:
        chan = self._chan
        if chan is None:
            return
        try:
            chan.sendall(data)
        except (OSError, EOFError, paramiko.SSHException):
            pass

    def resize(self, cols: int, rows: int) -> None:
        super().resize(cols, rows)
        chan = self._chan
        if chan is None:
            return
        try:
            chan.resize_pty(width=cols, height=rows)
        except (OSError, EOFError, paramiko.SSHException):
            pass

    @property
    def tunnels_running(self) -> list[str]:
        """Descriptions of the tunnels that are running now."""
        with self._tunnels_lock:
            return [running.description for running in self._tunnels]

    def add_tunnels(self, text: str) -> tuple[list[str], list[str]]:
        """Start tunnels on the live connection, written as in the session's Tunnels field.

        Returns the descriptions of those that started, and a sentence for each that did not.
        """
        transport = self._client.get_transport() if self._client is not None else None
        if transport is None or not transport.is_active() or self._chan is None:
            return [], ["Not connected"]
        tunnels, bad = parse_tunnels(text)
        problems = [f"'{entry}' is not a tunnel: expected [bind:]port:host:port, R:port:host:port or D:port" for entry in bad]
        started = []
        for tunnel in tunnels:
            description, problem = self._start_tunnel(transport, tunnel)
            if problem:
                problems.append(problem)
            else:
                started.append(description)
        return started, problems

    def stop_tunnel(self, description: str) -> bool:
        """Stop a running tunnel; connections already made through it carry on until they end."""
        with self._tunnels_lock:
            running = next((item for item in self._tunnels if item.description == description), None)
            if running is None:
                return False
            self._tunnels.remove(running)
        tunnel = running.tunnel
        if running.listener is not None:
            try:
                running.listener.close()  # its accept loop ends with it
            except OSError:
                pass
            return True
        self._remote_routes.pop(tunnel.listen_port, None)
        transport = self._client.get_transport() if self._client is not None else None
        if transport is not None and transport.is_active():
            try:
                # Not cancel_port_forward(): that also drops the handler the other remote tunnels share.
                transport.global_request("cancel-tcpip-forward", (tunnel.listen_host, tunnel.listen_port), wait=True)
            except (OSError, EOFError, paramiko.SSHException):
                pass
        return True

    def can_send_break(self) -> bool:
        return True

    def send_break(self) -> bool:
        """Ask the server to send a break on our behalf (RFC 4335), as console servers act on."""
        chan = self._chan
        if chan is None or chan.closed:
            return False
        message = paramiko.Message()
        message.add_byte(cMSG_CHANNEL_REQUEST)
        message.add_int(chan.remote_chanid)
        message.add_string("break")
        message.add_boolean(False)  # no reply: servers without breaks would only refuse
        message.add_int(int(BREAK_SECONDS * 1000))
        try:
            chan.transport._send_user_message(message)  # paramiko has no public call for this request
        except (OSError, EOFError, paramiko.SSHException, AttributeError):
            return False
        return True

    def details(self) -> list[tuple[str, str]]:
        """What can be said about the connection: server, host key, encryption, route, tunnels."""
        transport = self._client.get_transport() if self._client is not None else None
        if transport is None or not transport.is_active():
            return []
        key = transport.get_remote_server_key()
        rows = [
            ("Server software", transport.remote_version or ""),
            ("Host key", f"{key.get_name()}  {fingerprint(key)}"),
            ("Encryption", f"{transport.remote_cipher}, {transport.remote_mac}"),
            ("Compression", "On" if transport.remote_compression not in (None, "none") else "Off"),
        ]
        if self.session.jump_host:
            rows.append(("Through", self.session.jump_host))
        rows.append(("Tunnels", "\n".join(self.tunnels_running) or "None"))
        rows.append(("Agent forwarding", "On" if self._agent is not None else "Off"))
        return rows

    def round_trip(self) -> float:
        """Seconds for a message to the server and its answer: a keep-alive request, which every server
        answers, if only to say it does not know it. Raises if the session is not connected."""
        transport = self._client.get_transport() if self._client is not None else None
        if transport is None or not transport.is_active() or self._chan is None:
            raise paramiko.SSHException("Not connected")
        started = time.monotonic()
        transport.global_request("keepalive@openssh.com", wait=True)
        return time.monotonic() - started

    def run_command(self, command: str, timeout: float = 8.0) -> str:
        """Run a command on its own channel of this connection, out of sight of the terminal; returns what it
        printed. Raises if the session is not connected, or the command takes longer than `timeout` seconds."""
        transport = self._client.get_transport() if self._client is not None else None
        if transport is None or not transport.is_active() or self._chan is None:
            raise paramiko.SSHException("Not connected")
        channel = transport.open_session(timeout=timeout)
        try:
            channel.settimeout(timeout)
            channel.exec_command(command)
            chunks = []
            while data := channel.recv(65536):
                chunks.append(data)
            return b"".join(chunks).decode("utf-8", "replace")
        finally:
            channel.close()

    def open_sftp(self) -> paramiko.SFTPClient:
        """A file-transfer channel on this connection; raises if the session is not connected."""
        if self._client is None or self._chan is None:
            raise paramiko.SSHException("Not connected")
        # A window larger than paramiko's 2 MB, so that a download can have several megabytes on the way;
        # with 2 MB, a server 50 ms away can send no more than 40 MB a second down one channel.
        return paramiko.SFTPClient.from_transport(self._client.get_transport(), window_size=SFTP_WINDOW_BYTES)

    def _close(self) -> None:
        agent, self._agent = self._agent, None
        if agent is not None:
            try:
                agent.close()
            except Exception:
                pass
        with self._tunnels_lock:
            running, self._tunnels = self._tunnels, []
        for item in running:
            if item.listener is not None:
                try:
                    item.listener.close()
                except OSError:
                    pass
        for obj in (self._chan, self._client, *reversed(self._jumps)):
            try:
                if obj is not None:
                    obj.close()
            except Exception:
                pass

    # ---- connecting --------------------------------------------------------

    def _open(self) -> None:
        s = self.session
        KNOWN_HOSTS_FILE.parent.mkdir(parents=True, exist_ok=True)
        KNOWN_HOSTS_FILE.touch(exist_ok=True)
        key_file = s.key_file if s.auth == AUTH_KEY and s.key_file else None

        via_jump = None
        if s.jump_host:
            hops = parse_jump_hosts(s.jump_host)
            if hops is None:
                raise paramiko.SSHException(f"Jump host '{s.jump_host}' is not in the form user@host[:port]")
            # Each hop is reached through the one before it; the target through the last.
            for jump_user, jump_host, jump_port in hops:
                self.status.emit(f"Connecting to jump host {jump_host} ...")
                jump = self._login(
                    jump_host, jump_port, jump_user or s.username, None, key_file, use_keys=True, is_target=False,
                    sock_factory=via_jump,
                )
                via_jump = self._channel_factory(jump.get_transport(), jump_host, jump_port)
            via_jump = self._channel_factory(self._jumps[-1].get_transport(), s.host, s.port)

        self.status.emit(f"Connecting to {s.address} ...")
        self._client = self._login(
            s.host,
            s.port,
            s.username,
            self.password,
            key_file,
            use_keys=s.auth != AUTH_PASSWORD,
            is_target=True,
            sock_factory=via_jump,
        )
        if self._stop:
            raise _Cancelled("Cancelled")
        transport = self._client.get_transport()
        transport.set_keepalive(max(0, self.keepalive))
        self._chan = self._open_shell(transport)
        self._start_tunnels(transport)

    def _open_shell(self, transport: paramiko.Transport) -> paramiko.Channel:
        """The interactive shell, with the SSH agent forwarded first if the session asks for it."""
        chan = transport.open_session(timeout=CONNECT_TIMEOUT)
        if self.session.forward_agent:
            # The request must come before the shell starts, or the shell will not see the agent.
            try:
                self._agent = AgentRequestHandler(chan)
            except Exception as exc:
                self.status.emit(f"SSH agent not forwarded: {exc}")
        chan.get_pty(term=TERMINAL_TYPE, width=self._cols, height=self._rows)
        chan.invoke_shell()
        return chan

    @staticmethod
    def _reached(sock: socket.socket, seconds: float) -> str:
        """The line that says which address answered, and how long that took when it took long."""
        try:
            address = sock.getpeername()[0]
        except OSError:
            address = "the server"
        how_long = f" ({seconds:.1f} s)" if seconds >= SLOW_STEP_SECONDS else ""
        return f"Connected to {address}{how_long}, logging in ..."

    @staticmethod
    def _channel_factory(transport: paramiko.Transport, host: str, port: int):
        """A callable opening a connection to host:port from the far side of a transport.

        A fresh channel per login attempt: a failed attempt leaves its channel unusable.
        """
        return lambda: transport.open_channel("direct-tcpip", (host, port), ("127.0.0.1", 0))

    def _login(self, host, port, username, password, key_file, use_keys, is_target, sock_factory=None):
        """Connect and authenticate one hop, asking the user for whatever is missing."""
        client = _Client()
        client.load_system_host_keys()
        client.load_host_keys(str(KNOWN_HOSTS_FILE))
        client.set_missing_host_key_policy(_AskUserPolicy(self))
        if is_target:
            self._client = client  # so that stop() can interrupt a connect in progress
        else:
            self._jumps.append(client)
        passphrase = None
        state = {"password": password}

        def interactive(title, instructions, prompts):
            # A known password has already been tried by the time this runs, so every
            # prompt here is something only the user can answer.
            answers = []
            for text, echo in prompts:
                heading = "\n".join(part for part in (title, instructions) if part)
                message = f"{heading}\n{text.strip()}" if heading else text.strip()
                reply = self.ask(PROMPT_INPUT if echo else PROMPT_PASSWORD, f"{username}@{host}\n{message}")
                if reply is None:
                    raise _Cancelled("Authentication cancelled")
                if not echo and "password" in text.lower():
                    state["password"] = reply  # remember it for reconnects
                answers.append(reply)
            return answers

        client.interactive = interactive
        for attempt in range(MAX_AUTH_ATTEMPTS):
            try:
                pkey = None
                if key_file and is_dsa_key(key_file):
                    raise _Cancelled(
                        "DSA keys are no longer supported: current SSH servers refuse them. "
                        "Use an Ed25519 or RSA key instead."
                    )
                if key_file and ppk.is_ppk(key_file):
                    pkey = ppk.load(key_file, passphrase)
                if sock_factory:
                    sock = sock_factory()
                else:
                    # Opened here, not by paramiko, which would try a name's addresses one at a time.
                    began = time.monotonic()
                    sock = open_tcp(host, port, CONNECT_TIMEOUT, lambda: self._stop)
                    if attempt == 0:
                        self.status.emit(self._reached(sock, time.monotonic() - began))
                began, asked = time.monotonic(), self.waited
                client.connect(
                    host,
                    port=port,
                    username=username,
                    password=state["password"],
                    pkey=pkey,
                    key_filename=None if pkey else key_file,
                    passphrase=passphrase,
                    allow_agent=use_keys,
                    look_for_keys=use_keys,
                    compress=self._compress and is_target,
                    sock=sock,
                    timeout=CONNECT_TIMEOUT,
                    banner_timeout=20,
                    auth_timeout=60,
                )
                took = time.monotonic() - began - (self.waited - asked)  # not counting the time spent typing
                if took > SLOW_LOGIN_SECONDS:
                    self.status.emit(
                        f"Logging in took {took:.0f} s. If that is always so, the cause is usually on the server: "
                        "a reverse DNS lookup of this computer (UseDNS) or GSS-API login attempts in its sshd settings."
                    )
                break
            except _Cancelled:
                raise
            except paramiko.SSHException as exc:
                # With no key and no password paramiko has nothing to offer the
                # server; treat that like a failed login and ask for a password.
                auth_failed = isinstance(exc, paramiko.AuthenticationException) or (
                    "No authentication methods" in str(exc)
                )
                if not auth_failed or self._stop or attempt == MAX_AUTH_ATTEMPTS - 1:
                    raise
                if isinstance(exc, paramiko.PasswordRequiredException) and key_file:
                    passphrase = self.ask(PROMPT_PASSPHRASE, f"Passphrase for key {key_file}")
                    reply = passphrase
                else:
                    state["password"] = self.ask(PROMPT_PASSWORD, f"Password for {username}@{host}")
                    reply = state["password"]
                if reply is None:
                    raise _Cancelled("Authentication cancelled") from None
        if is_target:
            self.password = state["password"]
        return client

    # ---- tunnels ---------------------------------------------------------

    def _start_tunnels(self, transport: paramiko.Transport) -> None:
        tunnels, bad = parse_tunnels(self.session.tunnels)
        for entry in bad:
            self.status.emit(f"Tunnel '{entry}' ignored: expected [bind:]port:host:port, R:port:host:port or D:port")
        for tunnel in tunnels:
            description, problem = self._start_tunnel(transport, tunnel)
            self.status.emit(problem or description)

    def _start_tunnel(self, transport: paramiko.Transport, tunnel: Tunnel) -> tuple[str, str]:
        """Start one tunnel; returns its description, or a sentence saying why it did not start."""
        if tunnel.kind == TUNNEL_REMOTE:
            return self._start_remote_tunnel(transport, tunnel)
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            listener.bind((tunnel.listen_host, tunnel.listen_port))
            listener.listen(16)
        except OSError as exc:
            listener.close()
            return "", f"Tunnel on port {tunnel.listen_port} not started: {exc.strerror or exc}"
        description = describe_tunnel(tunnel)
        with self._tunnels_lock:
            self._tunnels.append(RunningTunnel(tunnel, description, listener))
        threading.Thread(target=self._accept_loop, args=(listener, tunnel, transport), daemon=True).start()
        return description, ""

    def _start_remote_tunnel(self, transport: paramiko.Transport, tunnel: Tunnel) -> tuple[str, str]:
        """Ask the server to listen on a port and carry its connections back to a host on this side."""
        if tunnel.listen_port in self._remote_routes:
            return "", f"Port {tunnel.listen_port} on the server is already forwarded"
        self._remote_routes[tunnel.listen_port] = tunnel
        try:
            # One handler serves every remote tunnel: paramiko keeps only the last one given.
            transport.request_port_forward(tunnel.listen_host, tunnel.listen_port, self._route_remote)
        except paramiko.SSHException:
            self._remote_routes.pop(tunnel.listen_port, None)
            return "", f"The server refused to listen on port {tunnel.listen_port} for a remote tunnel"
        description = describe_tunnel(tunnel)
        with self._tunnels_lock:
            self._tunnels.append(RunningTunnel(tunnel, description))
        return description, ""

    def _route_remote(self, channel, _origin, server) -> None:
        """A connection arrived at a port the server listens on for us: carry it to that tunnel's host."""
        tunnel = self._remote_routes.get(server[1])
        if tunnel is None:  # stopped meanwhile
            channel.close()
            return

        def connect_and_pump():
            try:
                sock = socket.create_connection((tunnel.host, tunnel.port), timeout=CONNECT_TIMEOUT)
            except OSError:
                channel.close()
                return
            sock.settimeout(None)
            self._pump(sock, channel)

        threading.Thread(target=connect_and_pump, daemon=True).start()

    def _accept_loop(self, listener: socket.socket, tunnel: Tunnel, transport: paramiko.Transport) -> None:
        while not self._stop:
            try:
                conn, peer = listener.accept()
            except OSError:
                return  # listener closed
            if tunnel.kind == TUNNEL_DYNAMIC:
                login = (tunnel.username, tunnel.password) if tunnel.username else None
                threading.Thread(target=self._serve_socks, args=(conn, peer, transport, login), daemon=True).start()
                continue
            try:
                channel = transport.open_channel("direct-tcpip", (tunnel.host, tunnel.port), peer, timeout=CONNECT_TIMEOUT)
            except Exception:
                conn.close()
                continue
            threading.Thread(target=self._pump, args=(conn, channel), daemon=True).start()

    def _serve_socks(self, conn: socket.socket, peer, transport: paramiko.Transport, login=None) -> None:
        """One SOCKS client: read where it wants to go, open that through the server, then relay."""
        try:
            conn.settimeout(15)
            request = _read_socks_request(conn, login)
            if request is None:
                conn.close()
                return
            host, port, version = request
            granted = b"\x05\x00\x00\x01" + bytes(6) if version == 5 else b"\x00\x5a" + bytes(6)
            refused = b"\x05\x05\x00\x01" + bytes(6) if version == 5 else b"\x00\x5b" + bytes(6)
            try:
                channel = transport.open_channel("direct-tcpip", (host, port), peer, timeout=CONNECT_TIMEOUT)
            except Exception:
                conn.sendall(refused)
                conn.close()
                return
            conn.sendall(granted)
            conn.settimeout(None)
        except (OSError, ValueError):
            conn.close()
            return
        self._pump(conn, channel)

    @staticmethod
    def _pump(conn: socket.socket, channel: paramiko.Channel) -> None:
        try:
            while True:
                readable, _, _ = select.select([conn, channel], [], [], 30)
                if conn in readable:
                    data = conn.recv(65536)
                    if not data:
                        break
                    channel.sendall(data)
                if channel in readable:
                    data = channel.recv(65536)
                    if not data:
                        break
                    conn.sendall(data)
                if channel.closed:
                    break
        except (OSError, EOFError, paramiko.SSHException):
            pass
        finally:
            channel.close()
            conn.close()

    # ---- reading ---------------------------------------------------------

    def _read_loop(self) -> None:
        chan = self._chan
        while not self._stop:
            data = chan.recv(65536)
            if not data:
                break
            # Coalesce bursts so the GUI thread is not flooded with tiny signals.
            while chan.recv_ready() and len(data) < 1 << 20:
                data += chan.recv(65536)
            self.data_received.emit(data)
        # A shell that ends normally reports an exit status first. Without one the
        # connection itself went away, which is what automatic reconnect is for.
        if not self._stop and chan.exit_status == -1:
            raise ConnectionError(f"Connection to {self.session.host} was lost")

    def _describe(self, exc: Exception) -> str:
        s = self.session
        if isinstance(exc, paramiko.BadHostKeyException):
            return (
                f"HOST KEY MISMATCH for {exc.hostname}. The server's key differs from the one saved "
                "earlier - this may be a man-in-the-middle attack. Connection refused."
            )
        if isinstance(exc, _Cancelled):
            return str(exc)
        if isinstance(exc, ppk.PpkError):
            return f"Key file: {exc}"
        if isinstance(exc, paramiko.AuthenticationException):
            return f"Authentication failed: {exc}"
        if isinstance(exc, paramiko.ssh_exception.NoValidConnectionsError):
            return f"Could not connect to {s.host}:{s.port} (connection refused)"
        if isinstance(exc, paramiko.ChannelException):
            return f"The jump host could not reach {s.host}:{s.port}"
        return super()._describe(exc)
