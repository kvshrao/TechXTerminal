"""Connection workers: one background thread per tab, one class per protocol."""

import getpass
import queue
import socket
import time
import struct
import threading

from PyQt6.QtCore import QThread, pyqtSignal

from . import threads
from .sessions import PROTO_LOCAL, PROTO_RAW, PROTO_RLOGIN, PROTO_SERIAL, PROTO_SSH, PROTO_TELNET, Session

PROMPT_PASSWORD = "password"
PROMPT_PASSPHRASE = "passphrase"
PROMPT_HOSTKEY = "hostkey"
PROMPT_INPUT = "input"  # a visible answer, e.g. a one-time code

CONNECT_TIMEOUT = 15
ADDRESS_STAGGER = 0.25  # seconds to wait for one address before the next is tried beside it
BREAK_SECONDS = 0.3  # how long a break holds the line; long enough for the slowest consoles
TERMINAL_TYPE = "xterm-256color"


def _resolve(host: str, port: int) -> list:
    return socket.getaddrinfo(host, port, socket.AF_UNSPEC, socket.SOCK_STREAM)


def _connect_one(family: int, proto: int, address, timeout: float, register) -> socket.socket:
    """Connect to one address. `register` is told of the socket first, so that it can be closed to stop the attempt."""
    sock = socket.socket(family, socket.SOCK_STREAM, proto)
    register(sock)
    try:
        sock.settimeout(timeout)
        sock.connect(address)
    except BaseException:
        sock.close()
        raise
    return sock


def _interleave(infos: list) -> list[tuple[int, int, tuple]]:
    """(family, proto, address) for every address, alternating between the families, the first one's first.

    A name with several IPv6 and several IPv4 addresses is tried v6, v4, v6, v4 ... instead of all of one family
    before any of the other.
    """
    by_family: dict[int, list] = {}
    for family, _kind, proto, _canonical, address in infos:
        by_family.setdefault(family, []).append((family, proto, address))
    lists = list(by_family.values())
    mixed = []
    for index in range(max((len(entries) for entries in lists), default=0)):
        mixed.extend(entries[index] for entries in lists if index < len(entries))
    return mixed


def open_tcp(host: str, port: int, timeout: float = CONNECT_TIMEOUT, should_stop=None) -> socket.socket:
    """Open a TCP connection to a host name, trying its addresses side by side.

    `socket.create_connection` tries them one after another, each for the whole timeout, so a name whose first
    address cannot be reached (typically IPv6 on a network that has none) makes every connection wait out the
    timeout before the working address gets its turn. Here the next address starts after a short pause, or at
    once when the one before it failed, and the first connection that succeeds is the one kept (RFC 8305).

    Raises what `create_connection` would: gaierror for a name that cannot be found, the last error when every
    address failed, a timeout when none answered in `timeout` seconds. `should_stop()` returning True abandons it.
    """
    addresses = _interleave(_resolve(host, port))
    deadline = time.monotonic() + timeout
    results: queue.Queue = queue.Queue()
    lock = threading.Lock()
    attempts: list[socket.socket] = []
    decided = False

    def register(sock: socket.socket) -> None:
        with lock:
            if decided:  # the race is over: this one is not needed
                sock.close()
                raise OSError("not needed")
            attempts.append(sock)

    def attempt(family: int, proto: int, address) -> None:
        try:
            results.put((_connect_one(family, proto, address, max(0.1, deadline - time.monotonic()), register), None))
        except OSError as exc:
            results.put((None, exc))

    winner = None
    started = running = 0
    next_at = 0.0
    error: OSError | None = None
    try:
        while True:
            now = time.monotonic()
            if started < len(addresses) and (running == 0 or now >= next_at):
                family, proto, address = addresses[started]
                started += 1
                running += 1
                next_at = now + ADDRESS_STAGGER
                threading.Thread(target=attempt, args=(family, proto, address), daemon=True).start()
            if running == 0 and started == len(addresses):
                raise error or OSError(f"No address to connect to for {host}")
            if should_stop is not None and should_stop():
                raise ConnectionAbortedError("Cancelled")
            if now >= deadline:
                raise socket.timeout("timed out")
            pause = min(0.05, deadline - now)
            if started < len(addresses):
                pause = min(pause, max(0.001, next_at - now))
            try:
                sock, failure = results.get(timeout=pause)
            except queue.Empty:
                continue
            running -= 1
            if sock is not None:
                winner = sock
                return sock
            error = failure
            next_at = 0.0  # that address failed: the next one starts at once
    finally:
        with lock:
            decided = True
            losers = [sock for sock in attempts if sock is not winner]
        for sock in losers:
            try:
                sock.shutdown(socket.SHUT_RDWR)  # ends an attempt that is still connecting
            except OSError:
                pass
            sock.close()


class ConnectionWorker(QThread):
    connected = pyqtSignal()
    data_received = pyqtSignal(bytes)
    status = pyqtSignal(str)
    closed = pyqtSignal(str)  # error message, empty when the session ended normally
    # kind, message. The GUI must reply with answer(); None cancels.
    prompt = pyqtSignal(str, str)

    def __init__(self, session: Session, password: str | None, cols: int, rows: int, parent=None):
        super().__init__(parent)
        self.session = session
        self.password = password
        self._cols, self._rows = cols, rows
        self.keepalive = 30  # seconds between keep-alives on an idle connection, 0 for none
        self._stop = False
        self._answer: str | None = None
        self._answered = threading.Event()
        self.waited = 0.0  # seconds spent waiting for the user to answer prompts

    # ---- called from the GUI thread -------------------------------------

    def answer(self, value: str | None) -> None:
        self._answer = value
        self._answered.set()

    def send(self, data: bytes) -> None:
        raise NotImplementedError

    def resize(self, cols: int, rows: int) -> None:
        self._cols, self._rows = cols, rows

    def details(self) -> list[tuple[str, str]]:
        """(label, value) rows about the live connection that only this kind of worker knows."""
        return []

    def can_send_break(self) -> bool:
        """Whether this kind of connection has a break signal, as consoles and routers use."""
        return False

    def send_break(self) -> bool:
        """Send a break signal; False if this kind of connection has none or sending failed."""
        return False

    def stop(self) -> None:
        self._stop = True
        self._answered.set()
        self._interrupt()

    # ---- worker thread ---------------------------------------------------

    def ask(self, kind: str, message: str) -> str | None:
        self._answered.clear()
        self._answer = None
        asked = time.monotonic()
        self.prompt.emit(kind, message)
        self._answered.wait()
        self.waited += time.monotonic() - asked  # so that a slow login can be told from a slow typist
        return None if self._stop else self._answer

    def run(self) -> None:
        error = ""
        try:
            self._open()
            self.connected.emit()
            self._read_loop()
        except Exception as exc:
            if not self._stop:
                error = self._describe(exc)
        finally:
            self._close()
        if not self._stop:
            self.closed.emit(error)

    def _open(self) -> None:
        raise NotImplementedError

    def _read_loop(self) -> None:
        raise NotImplementedError

    def _close(self) -> None:
        raise NotImplementedError

    def _interrupt(self) -> None:
        """Unblock the worker thread; by default closing the transport does that."""
        self._close()

    def _describe(self, exc: Exception) -> str:
        s = self.session
        if isinstance(exc, socket.gaierror):
            return f"Could not resolve host '{s.host}'"
        if isinstance(exc, (socket.timeout, TimeoutError)):
            return f"Connection to {s.host}:{s.port} timed out"
        if isinstance(exc, ConnectionRefusedError):
            return f"Could not connect to {s.host}:{s.port} (connection refused)"
        if isinstance(exc, ConnectionError):
            return f"Connection to {s.host} was lost"
        return str(exc) or exc.__class__.__name__


class SocketWorker(ConnectionWorker):
    """Plain TCP: bytes go straight through. This is the Raw protocol."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._sock: socket.socket | None = None
        self._send_lock = threading.Lock()  # the GUI and the worker thread both write

    def _open(self) -> None:
        self.status.emit(f"Connecting to {self.session.address} ...")
        self._sock = self._connect_socket()
        self._sock.settimeout(None)
        if self.keepalive:
            # These protocols have no keep-alive of their own; let TCP probe an idle link.
            self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        self._handshake()

    def _connect_socket(self) -> socket.socket:
        return open_tcp(self.session.host, self.session.port, CONNECT_TIMEOUT, lambda: self._stop)

    def _handshake(self) -> None:
        pass

    def _read_loop(self) -> None:
        while not self._stop:
            data = self._sock.recv(65536)
            if not data:
                break
            data = self._incoming(data)
            if data:
                self.data_received.emit(data)

    def _incoming(self, data: bytes) -> bytes:
        return data

    def _outgoing(self, data: bytes) -> bytes:
        return data

    def send(self, data: bytes) -> None:
        self._send_raw(self._outgoing(data))

    def _send_raw(self, data: bytes) -> None:
        sock = self._sock
        if sock is None:
            return
        try:
            with self._send_lock:
                sock.sendall(data)
        except OSError:
            pass

    def _close(self) -> None:
        sock = self._sock
        if sock is None:
            return
        for action in (lambda: sock.shutdown(socket.SHUT_RDWR), sock.close):
            try:
                action()
            except OSError:
                pass


IAC, DONT, DO, WONT, WILL, SB, SE = 255, 254, 253, 252, 251, 250, 240
BRK = 243
OPT_ECHO, OPT_SGA, OPT_TTYPE, OPT_NAWS = 1, 3, 24, 31
MAX_TELNET_COMMAND = 64 * 1024  # the longest option subnegotiation that is waited for


class TelnetWorker(SocketWorker):
    """Telnet with the options a terminal needs: echo, terminal type and window size."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._pending = b""
        self._naws = False
        self._replied: set[tuple[int, int]] = set()

    def _incoming(self, data: bytes) -> bytes:
        buf = self._pending + data
        out = bytearray()
        i, n = 0, len(buf)
        while i < n:
            if buf[i] != IAC:
                end = buf.find(b"\xff", i)
                end = n if end < 0 else end
                out += buf[i:end]
                i = end
                continue
            if i + 1 >= n:
                break  # command split across packets; wait for the rest
            command = buf[i + 1]
            if command == IAC:
                out.append(IAC)
                i += 2
            elif command in (DO, DONT, WILL, WONT):
                if i + 2 >= n:
                    break
                self._negotiate(command, buf[i + 2])
                i += 3
            elif command == SB:
                end = buf.find(bytes([IAC, SE]), i + 2)
                if end < 0:
                    break
                self._subnegotiate(buf[i + 2 : end])
                i = end + 2
            else:
                i += 2
        self._pending = buf[i:]
        if len(self._pending) > MAX_TELNET_COMMAND:
            self._pending = b""  # a command that never ends: a broken or hostile server must not make this grow, and be searched, for ever
        return bytes(out).replace(b"\r\x00", b"\r")

    def _reply(self, command: int, option: int) -> None:
        if (command, option) not in self._replied:  # never answer twice: that is how option loops start
            self._replied.add((command, option))
            self._send_raw(bytes([IAC, command, option]))

    def _negotiate(self, command: int, option: int) -> None:
        if command == DO:
            if option in (OPT_TTYPE, OPT_NAWS, OPT_SGA):
                self._reply(WILL, option)
                if option == OPT_NAWS:
                    self._naws = True
                    self._send_window_size()
            else:
                self._reply(WONT, option)
        elif command == WILL:
            self._reply(DO if option in (OPT_ECHO, OPT_SGA) else DONT, option)
        elif command == DONT and option == OPT_NAWS:
            self._naws = False

    def _subnegotiate(self, payload: bytes) -> None:
        if payload[:2] == bytes([OPT_TTYPE, 1]):  # SEND
            name = TERMINAL_TYPE.upper().encode()
            self._send_raw(bytes([IAC, SB, OPT_TTYPE, 0]) + name + bytes([IAC, SE]))

    def _send_window_size(self) -> None:
        size = struct.pack("!HH", self._cols, self._rows).replace(b"\xff", b"\xff\xff")
        self._send_raw(bytes([IAC, SB, OPT_NAWS]) + size + bytes([IAC, SE]))

    def can_send_break(self) -> bool:
        return True

    def send_break(self) -> bool:
        if self._sock is None:
            return False
        self._send_raw(bytes([IAC, BRK]))
        return True

    def _outgoing(self, data: bytes) -> bytes:
        # A bare CR must be followed by NUL on a telnet line, and 0xFF must be doubled.
        return data.replace(b"\xff", b"\xff\xff").replace(b"\r", b"\r\x00")

    def resize(self, cols: int, rows: int) -> None:
        super().resize(cols, rows)
        if self._naws:
            self._send_window_size()


class RloginWorker(SocketWorker):
    def _connect_socket(self) -> socket.socket:
        s = self.session
        family, kind, proto, _, address = socket.getaddrinfo(s.host, s.port, type=socket.SOCK_STREAM)[0]
        # rlogin servers traditionally insist on a privileged source port; fall back to
        # an ordinary one (0) where the OS will not hand those out.
        for source_port in [*range(1023, 1007, -1), 0]:
            sock = socket.socket(family, kind, proto)
            try:
                if source_port:
                    sock.bind(("", source_port))
            except OSError:
                sock.close()
                continue
            try:
                sock.settimeout(CONNECT_TIMEOUT)
                sock.connect(address)
                return sock
            except OSError:
                sock.close()
                raise
        raise OSError("No local port available")

    def _handshake(self) -> None:
        s = self.session
        local_user = getpass.getuser()
        fields = ["", local_user, s.username or local_user, f"{TERMINAL_TYPE}/38400", ""]
        self._send_raw("\0".join(fields).encode())
        self._sock.settimeout(CONNECT_TIMEOUT)
        if self._sock.recv(1) != b"\0":
            raise ConnectionError("The server rejected the rlogin request")
        self._sock.settimeout(None)
        self._send_window_size()

    def _send_window_size(self) -> None:
        self._send_raw(b"\xff\xffss" + struct.pack("!HHHH", self._rows, self._cols, 0, 0))

    def resize(self, cols: int, rows: int) -> None:
        super().resize(cols, rows)
        self._send_window_size()

    def _describe(self, exc: Exception) -> str:
        if isinstance(exc, ConnectionError) and str(exc):
            return str(exc)
        return super()._describe(exc)


def parse_serial_format(text: str) -> tuple[int, str, float]:
    """Turn "8N1" / "7E1" / "8N1.5" into (data bits, parity letter, stop bits); 8N1 if malformed."""
    text = text.strip().upper()
    try:
        data_bits, parity, stop_bits = int(text[0]), text[1], float(text[2:])
    except (IndexError, ValueError):
        return 8, "N", 1
    if data_bits not in (5, 6, 7, 8) or parity not in "NEOMS" or stop_bits not in (1, 1.5, 2):
        return 8, "N", 1
    return data_bits, parity, int(stop_bits) if stop_bits != 1.5 else 1.5


class SerialWorker(ConnectionWorker):
    """Serial line: the session's host is the device (COM3, /dev/ttyUSB0) and its port the speed."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._port = None

    def _open(self) -> None:
        try:
            import serial
        except ImportError:
            raise RuntimeError("Serial connections need the 'pyserial' package (pip install pyserial)") from None
        s = self.session
        self.status.emit(f"Opening {s.host} at {s.port} baud ...")
        data_bits, parity, stop_bits = parse_serial_format(s.serial_format)
        self._port = serial.serial_for_url(
            s.host,
            baudrate=s.port,
            bytesize=data_bits,
            parity=parity,
            stopbits=stop_bits,
            xonxoff=s.serial_flow == "xonxoff",
            rtscts=s.serial_flow == "rtscts",
            dsrdtr=s.serial_flow == "dsrdtr",
            timeout=0.2,
        )

    def _read_loop(self) -> None:
        port = self._port
        while not self._stop:
            data = port.read(max(1, port.in_waiting))
            if data:
                self.data_received.emit(data)

    def send(self, data: bytes) -> None:
        port = self._port
        if port is None:
            return
        try:
            port.write(data)
        except Exception:
            pass

    def can_send_break(self) -> bool:
        return True

    def send_break(self) -> bool:
        port = self._port
        if port is None:
            return False

        def hold_break():
            try:
                port.send_break(duration=BREAK_SECONDS)
            except Exception:
                pass

        # send_break() holds the line for its whole duration; keep the window responsive meanwhile.
        threading.Thread(target=hold_break, daemon=True).start()
        return True

    def _interrupt(self) -> None:
        pass  # reads time out every 200 ms, so the loop notices the stop flag by itself

    def _close(self) -> None:
        port, self._port = self._port, None
        if port is not None:
            try:
                port.close()
            except Exception:
                pass

    def _describe(self, exc: Exception) -> str:
        return f"Serial line {self.session.host}: {exc}"


def probe(session: Session, timeout: float = 5.0) -> tuple[bool, str]:
    """Can the session's target be reached at all? Returns (reachable, a sentence for the user).

    This only opens and closes a connection (or the serial line); it does not log in.
    """
    if session.protocol == PROTO_LOCAL:
        from .local import find_program

        program = find_program(session.host)
        return (True, f"{program} is there") if program else (False, f"There is no program {session.host}")
    if session.protocol == PROTO_SERIAL:
        try:
            import serial

            serial.serial_for_url(session.host, baudrate=session.port, timeout=0.2).close()
        except ImportError:
            return False, "Serial connections need the 'pyserial' package"
        except Exception as exc:
            return False, f"Cannot open {session.host}: {exc}"
        return True, f"{session.host} can be opened"
    started = time.perf_counter()
    try:
        open_tcp(session.host, session.port, timeout).close()
    except socket.gaierror:
        return False, f"The name '{session.host}' cannot be found"
    except (socket.timeout, TimeoutError):
        return False, f"No answer from {session.host} port {session.port} within {timeout:g} s"
    except ConnectionRefusedError:
        return False, f"{session.host} refused the connection on port {session.port}"
    except OSError as exc:
        return False, f"Cannot reach {session.host}: {exc.strerror or exc}"
    milliseconds = (time.perf_counter() - started) * 1000
    return True, f"{session.host} answers on port {session.port} ({milliseconds:.0f} ms)"


class Probe(QThread):
    """Runs probe() off the UI thread."""

    finished_with = pyqtSignal(bool, str)

    def __init__(self, session: Session, parent=None):
        super().__init__(parent)
        self._session = session

    def run(self) -> None:
        self.finished_with.emit(*probe(self._session))


class ProbeMany(QThread):
    """Tests several sessions side by side and reports each as it is known."""

    result = pyqtSignal(str, bool, str)  # session id, reachable, sentence
    done = pyqtSignal()

    def __init__(self, sessions: list[Session], timeout: float = 4.0, parent=None):
        super().__init__(parent)
        self._sessions = list(sessions)
        self._timeout = timeout

    def run(self) -> None:
        from concurrent.futures import ThreadPoolExecutor, as_completed

        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = {pool.submit(probe, session, self._timeout): session for session in self._sessions}
            for future in as_completed(futures):
                reachable, sentence = future.result()
                self.result.emit(futures[future].id, reachable, sentence)
        self.done.emit()


def create_worker(
    session: Session, password: str | None, cols: int, rows: int, compress: bool = False, keepalive: int = 30
) -> ConnectionWorker:
    if session.protocol == PROTO_SSH:
        from .ssh import SSHWorker  # deferred: ssh.py builds on this module

        return SSHWorker(session, password, cols, rows, compress=compress, keepalive=keepalive)
    if session.protocol == PROTO_LOCAL:
        from .local import LocalWorker  # deferred, like SSH

        return LocalWorker(session, password, cols, rows)
    worker_class = {
        PROTO_RAW: SocketWorker,
        PROTO_TELNET: TelnetWorker,
        PROTO_RLOGIN: RloginWorker,
        PROTO_SERIAL: SerialWorker,
    }[session.protocol]
    worker = worker_class(session, password, cols, rows)
    worker.keepalive = keepalive
    return worker


def retire(worker: ConnectionWorker) -> None:
    """Stop a worker and keep it referenced until its thread has really exited."""
    worker.stop()
    threads.park(worker)


def shutdown_all(timeout_ms: int = 1500) -> None:
    threads.wait_all(timeout_ms)
