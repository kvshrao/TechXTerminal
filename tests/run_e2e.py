"""End-to-end test: drives the real application against throwaway servers on localhost.

Run it from the repository root with the project's Python:

    python tests/run_e2e.py

It opens the main window briefly, uses a temporary home directory (your own sessions
and settings are not touched) and exits with status 0 when every check passes.
Screenshots of the main views are written to the temporary directory it prints.
"""
import os
import posixpath
import select
import socket
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WORK = Path(tempfile.mkdtemp(prefix="techx-e2e-"))
HOME = WORK / "home"
SCRATCH = WORK / "shots"
SFTP_ROOT = WORK / "remote"
for directory in (HOME, SCRATCH, SFTP_ROOT):
    directory.mkdir(parents=True)
# Everything the app stores goes under the temporary home, not the real one.
os.environ["USERPROFILE"] = str(HOME)
os.environ["HOME"] = str(HOME)
# Preferences too: without this the app would use (and the test would overwrite) the real ones.
os.environ["TECHX_SETTINGS_FILE"] = str(HOME / "settings.ini")
sys.path.insert(0, str(ROOT))

import paramiko
from PyQt6.QtCore import QSettings, QTimer
from PyQt6.QtWidgets import QApplication, QInputDialog, QLineEdit, QMessageBox, QPushButton

REAL_SETTINGS = QSettings("TechX", "TechXTerminal")
REAL_SETTINGS_BEFORE = {key: REAL_SETTINGS.value(key) for key in REAL_SETTINGS.allKeys()}

from techxterminal.app import create_app
from techxterminal.mainwindow import MainWindow
from techxterminal.dialogs import SessionDialog, SettingsDialog
from techxterminal.sessions import Session

assert Path.home() == HOME, Path.home()
print("working directory:", WORK)

# ---- a small SSH server ------------------------------------------------------------

HOST_KEY = paramiko.RSAKey.generate(2048)
USER_KEY = paramiko.RSAKey.generate(2048)  # accepted for the user "keyuser"
BANNER = (
    "\x1b[1;35mWelcome to demo-box\x1b[0m  (Ubuntu 24.04 LTS)\r\n\r\n"
    "\x1b[1;34mbin\x1b[0m  \x1b[1;34metc\x1b[0m  \x1b[1;32mdeploy.sh\x1b[0m  notes.txt  \x1b[1;36mlink\x1b[0m -> /srv  \x1b[41;97m ERR \x1b[0m \x1b[4munder\x1b[0m \x1b[3mitalic\x1b[0m \x1b[7mreverse\x1b[0m\r\n"
    + "".join(f"\x1b[48;5;{n}m " for n in range(16, 88)) + "\x1b[0m\r\n"
    + "".join(f"\x1b[38;2;{255 - i * 4};{i * 4};180m\u2588" for i in range(60)) + "\x1b[0m\r\n"
    "unicode: \u2713 \u2192 \u03bb caf\u00e9 \u4e2d\u6587 ok\r\n"
)
PROMPT = "\x1b[1;32mdeploy@demo-box\x1b[0m:\x1b[1;34m~\x1b[0m$ "
BIG_OUTPUT_LINES = 30000


def shell(chan, srv):
    """The interactive 'shell' of the test server: echoes input and knows a few commands."""
    chan.send(BANNER + PROMPT)
    line = b""
    while True:
        try:
            data = chan.recv(1024)
        except OSError:
            return
        if not data:
            return
        srv.received += data
        for b in data:
            ch = bytes([b])
            if ch != b"\r":
                line += ch
                chan.send(ch)
                continue
            cmd = line.decode(errors="replace")
            line = b""
            if cmd == "exit":
                chan.send("\r\nlogout\r\n")
                chan.send_exit_status(0)
                chan.close()
                return
            if cmd == "drop":  # the connection dies without a logout
                srv.transport.close()
                return
            if cmd == "alt":
                chan.send("\x1b[?1049h\x1b[H\x1b[2JFULLSCREEN APP")
            elif cmd == "unalt":
                chan.send("\x1b[?1049l")
            elif cmd == "mouse":
                chan.send("\x1b[?1000h\x1b[?1006h\r\n" + PROMPT)
            elif cmd == "nomouse":
                chan.send("\x1b[?1000l\x1b[?1006l\r\n" + PROMPT)
            elif cmd == "flood":
                chan.send("\r\n" + "".join(f"line {i}\r\n" for i in range(500)) + PROMPT)
            elif cmd == "big":
                chan.sendall(("\r\n" + "".join(f"big output row {i:06d} {'x' * 40}\r\n" for i in range(BIG_OUTPUT_LINES)) + "BIG DONE\r\n" + PROMPT).encode())
            elif cmd == "url":
                chan.send("\r\nsee https://example.com/docs/page?id=7. and (http://intranet.local/x) done\r\n" + PROMPT)
            else:
                chan.send(f"\r\nran: {cmd} size={srv.size}\r\n" + PROMPT)


class StubSFTPHandle(paramiko.SFTPHandle):
    def stat(self):
        return paramiko.SFTPAttributes.from_stat(os.fstat(self.readfile.fileno()))


RENAMES = {"one_step": True, "posix": 0, "plain": 0}  # how the stand-in SFTP server was asked to rename


class StubSFTP(paramiko.SFTPServerInterface):
    """Serves SFTP_ROOT as the remote file system."""

    def canonicalize(self, path):
        # paramiko's default relies on os.path, which misreads "/x" on Windows.
        return posixpath.normpath("/" + path.replace("\\", "/").lstrip("/"))

    def _real(self, path):
        return str(SFTP_ROOT) + self.canonicalize(path)

    def list_folder(self, path):
        try:
            out = []
            for name in os.listdir(self._real(path)):
                attr = paramiko.SFTPAttributes.from_stat(os.stat(os.path.join(self._real(path), name)))
                attr.filename = name
                out.append(attr)
            return out
        except OSError as e:
            return paramiko.SFTPServer.convert_errno(e.errno)

    def stat(self, path):
        try:
            return paramiko.SFTPAttributes.from_stat(os.stat(self._real(path)))
        except OSError as e:
            return paramiko.SFTPServer.convert_errno(e.errno)

    lstat = stat

    def open(self, path, flags, attr):
        try:
            binary = getattr(os, "O_BINARY", 0)
            fd = os.open(self._real(path), flags | binary, 0o666)
            mode = "wb" if flags & (os.O_WRONLY | os.O_RDWR) else "rb"
            f = os.fdopen(fd, "r+b" if flags & os.O_RDWR else mode)
        except OSError as e:
            return paramiko.SFTPServer.convert_errno(e.errno)
        handle = StubSFTPHandle(flags)
        handle.filename = self._real(path)
        handle.readfile = f
        handle.writefile = f
        return handle

    def remove(self, path):
        try:
            os.remove(self._real(path))
        except OSError as e:
            return paramiko.SFTPServer.convert_errno(e.errno)
        return paramiko.SFTP_OK

    def rename(self, oldpath, newpath):
        RENAMES["plain"] += 1
        try:
            os.rename(self._real(oldpath), self._real(newpath))
        except OSError as e:
            return paramiko.SFTPServer.convert_errno(e.errno)
        return paramiko.SFTP_OK

    def posix_rename(self, oldpath, newpath):
        # What OpenSSH offers: the new file takes the old one's place in a single step.
        if not RENAMES["one_step"]:
            return paramiko.SFTP_OP_UNSUPPORTED
        RENAMES["posix"] += 1
        try:
            os.replace(self._real(oldpath), self._real(newpath))
        except OSError as e:
            return paramiko.SFTPServer.convert_errno(e.errno)
        return paramiko.SFTP_OK

    def chattr(self, path, attr):
        try:
            real = self._real(path)
            if attr.st_mode is not None:
                os.chmod(real, attr.st_mode)
            if attr.st_mtime is not None:
                os.utime(real, (attr.st_atime or attr.st_mtime, attr.st_mtime))
            if attr.st_size is not None:  # truncate, as OpenSSH allows
                os.truncate(real, attr.st_size)
        except OSError as e:
            return paramiko.SFTPServer.convert_errno(e.errno)
        return paramiko.SFTP_OK

    def mkdir(self, path, attr):
        try:
            os.mkdir(self._real(path))
        except OSError as e:
            return paramiko.SFTPServer.convert_errno(e.errno)
        return paramiko.SFTP_OK

    def rmdir(self, path):
        try:
            os.rmdir(self._real(path))
        except OSError as e:
            return paramiko.SFTPServer.convert_errno(e.errno)
        return paramiko.SFTP_OK


class Server(paramiko.ServerInterface):
    def __init__(self, transport):
        self.transport = transport
        self.size = None
        self.received = b""
        self.username = None
        self.forwards = {}
        self.remote_listeners = []
        self.password_ok = False

    def check_channel_request(self, kind, chanid):
        return paramiko.OPEN_SUCCEEDED

    def check_channel_direct_tcpip_request(self, chanid, origin, destination):
        self.forwards[chanid] = destination
        return paramiko.OPEN_SUCCEEDED

    def check_port_forward_request(self, address, port):
        """A remote tunnel: listen here and hand each connection to the client over a new channel."""
        sock = socket.socket()
        try:
            sock.bind(("127.0.0.1", port))
        except OSError:
            return False
        sock.listen(4)
        self.remote_listeners.append(sock)

        def accept():
            while True:
                try:
                    conn, peer = sock.accept()
                except OSError:
                    return
                try:
                    chan = self.transport.open_forwarded_tcpip_channel(peer, ("127.0.0.1", port))
                except (paramiko.SSHException, EOFError):
                    conn.close()
                    continue
                threading.Thread(target=relay_pair, args=(chan, conn), daemon=True).start()

        threading.Thread(target=accept, daemon=True).start()
        return sock.getsockname()[1]

    def get_allowed_auths(self, username):
        if username == "mfa":
            return "keyboard-interactive" if self.password_ok else "password"
        return "publickey" if username == "keyuser" else "password"

    def check_auth_password(self, username, password):
        self.username = username
        if username == "keyuser" or password != "secret":
            return paramiko.AUTH_FAILED
        if username == "mfa":  # a second factor is still required
            self.password_ok = True
            return paramiko.AUTH_PARTIALLY_SUCCESSFUL
        return paramiko.AUTH_SUCCESSFUL

    def check_auth_publickey(self, username, key):
        self.username = username
        ok = username == "keyuser" and key.asbytes() == USER_KEY.asbytes()
        return paramiko.AUTH_SUCCESSFUL if ok else paramiko.AUTH_FAILED

    def check_auth_interactive(self, username, submethods):
        if username != "mfa" or not self.password_ok:
            return paramiko.AUTH_FAILED
        query = paramiko.InteractiveQuery("Two-factor login", "Enter the code from your authenticator")
        query.add_prompt("Verification code: ", True)
        return query

    def check_auth_interactive_response(self, responses):
        return paramiko.AUTH_SUCCESSFUL if list(responses) == ["123456"] else paramiko.AUTH_FAILED

    def check_channel_pty_request(self, channel, term, width, height, pw, ph, modes):
        self.size = (width, height)
        return True

    def check_channel_window_change_request(self, channel, width, height, pw, ph):
        self.size = (width, height)
        return True

    def check_channel_shell_request(self, channel):
        threading.Thread(target=shell, args=(channel, self), daemon=True).start()
        return True

    def check_channel_exec_request(self, channel, command):
        """A command run without a terminal, as scheduled tasks run them: "fail" fails, "sleep" never ends."""
        command = command.decode(errors="replace")
        self.executed = getattr(self, "executed", []) + [command]

        def answer():
            time.sleep(0.1)  # paramiko answers the request after this method returns; output must come after that
            if command == "sleep":
                time.sleep(30)
                return
            if "/proc/loadavg" in command:  # the server monitor
                channel.sendall(b"0.42 0.30 0.25 1/234 5678\n4\nMemTotal:        8000000 kB\nMemAvailable:    2000000 kB\n/dev/sda1  102400000  62000000  40000000  96% /\n")
                channel.send_exit_status(0)
                channel.shutdown_write()
                channel.close()
                return
            channel.sendall(f"ran: {command}\nsecond line\n".encode())
            if command == "fail":
                channel.sendall_stderr(b"something went wrong\n")
            channel.send_exit_status(3 if command == "fail" else 0)
            channel.shutdown_write()
            channel.close()

        threading.Thread(target=answer, daemon=True).start()
        return True


servers = []


def relay_pair(chan, sock):
    """Copy bytes both ways between an SSH channel and a socket until either side closes."""
    try:
        while True:
            readable, _, _ = select.select([sock, chan], [], [], 30)
            if sock in readable:
                data = sock.recv(65536)
                if not data:
                    break
                chan.sendall(data)
            if chan in readable:
                data = chan.recv(65536)
                if not data:
                    break
                sock.sendall(data)
    except (OSError, EOFError):
        pass
    finally:
        sock.close()
        try:
            chan.close()
        except (OSError, EOFError):
            pass  # the SSH connection is already gone


def relay(chan, destination):
    try:
        sock = socket.create_connection(destination, timeout=5)
    except OSError:
        chan.close()
        return
    relay_pair(chan, sock)


def serve(conn):
    t = paramiko.Transport(conn)
    t.add_server_key(HOST_KEY)
    t.set_subsystem_handler("sftp", paramiko.SFTPServer, StubSFTP)
    srv = Server(t)
    servers.append(srv)
    try:
        t.start_server(server=srv)
    except (paramiko.SSHException, EOFError):
        return
    accepted = []  # paramiko closes a channel nothing refers to: one waiting for its subsystem request must stay
    while t.is_active():
        chan = t.accept(0.5)
        # Session channels are served from the shell/subsystem requests; only forwards need work here.
        if chan is not None:
            accepted.append(chan)
        if chan is not None and chan.get_id() in srv.forwards:
            threading.Thread(target=relay, args=(chan, srv.forwards[chan.get_id()]), daemon=True).start()
    for remote_listener in srv.remote_listeners:
        remote_listener.close()


listener = socket.socket()
listener.bind(("127.0.0.1", 0))
listener.listen(8)
PORT = listener.getsockname()[1]


def accept_loop():
    while True:
        try:
            conn, _ = listener.accept()
        except OSError:
            return
        threading.Thread(target=serve, args=(conn,), daemon=True).start()


threading.Thread(target=accept_loop, daemon=True).start()

from PyQt6.QtCore import QtMsgType, qInstallMessageHandler
qt_messages = []


def _qt_message(mode, _context, message):
    qt_messages.append(message)
    if mode in (QtMsgType.QtFatalMsg, QtMsgType.QtCriticalMsg):
        # Qt ends the process after a fatal message; make sure the reason is seen first.
        print("QT", mode.name, message, flush=True)


qInstallMessageHandler(_qt_message)
app = create_app(sys.argv)
win = MainWindow()
for name, group in [("web-01", "Production"), ("web-02", "Production"), ("db-primary", "Production"),
                    ("staging-api", "Staging"), ("build-runner", "")]:
    win.store.sessions.append(Session(name=name, group=group, host="127.0.0.1", port=PORT, username="deploy"))
win._reload_tree()
win._update_status()
win.show()

# The system clipboard can be unavailable (locked screen, remote session); use a stand-in then.
CLIP = QApplication.clipboard()
CLIP.setText("clipboard-probe")
if CLIP.text() != "clipboard-probe":
    import techxterminal.terminal as _terminal_module

    class _FakeClipboard:
        _text = ""

        def setText(self, value):
            self._text = value

        def text(self):
            return self._text

    class _GuiShim:
        _clip = _FakeClipboard()
        clipboard = staticmethod(lambda: _GuiShim._clip)
        keyboardModifiers = staticmethod(_terminal_module.QGuiApplication.keyboardModifiers)

    _terminal_module.QGuiApplication = _GuiShim
    CLIP = _GuiShim._clip
    print("note: system clipboard unavailable, using a stand-in")

seen = {"hostkey": 0, "password": 0, "code": 0, "code_echo": None, "reveal": None, "code_actions": None}


def auto_dialogs():
    w = app.activeModalWidget()
    if isinstance(w, QMessageBox):
        seen["hostkey"] += 1
        for b in w.findChildren(QPushButton):
            if "Trust" in b.text():
                b.click()
    elif isinstance(w, QInputDialog):
        if w.windowTitle() == "Verification":  # a one-time code, typed in a visible field
            seen["code"] += 1
            seen["code_echo"] = w.textEchoMode() == QLineEdit.EchoMode.Normal
            seen["code_actions"] = len(w.findChild(QLineEdit).actions())
            w.setTextValue("123456")
        else:
            seen["password"] += 1
            field = w.findChild(QLineEdit)
            if field.actions() and seen["reveal"] is None:  # the button at the end of the field shows what is typed
                field.actions()[0].trigger()
                shown = field.echoMode() == QLineEdit.EchoMode.Normal
                field.actions()[0].trigger()
                seen["reveal"] = (shown, field.echoMode() == QLineEdit.EchoMode.Password)
            w.setTextValue("secret")
        w.accept()


poller = QTimer()
poller.timeout.connect(auto_dialogs)
poller.start(100)


def pump(seconds, until=None):
    end = time.time() + seconds
    while time.time() < end:
        app.processEvents()
        try:
            if until and until():
                return True
        except PermissionError:
            pass  # a file still being written (or scanned) is locked on Windows: not ready yet
        time.sleep(0.01)
    return until is None


def text(tab):
    t = tab.terminal
    base = 0 if t.screen.in_alt_screen else len(t.screen.history.top)
    return "\n".join(t._line_text(base + r).rstrip() for r in range(t.rows))


pump(0.5)
win.grab().save(str(SCRATCH / "shot_welcome.png"))

results = []


def check(name, ok, extra=""):
    results.append(ok)
    print(("PASS" if ok else "FAIL"), name, ascii(extra) if extra else "")


# 1. password supplied up front, unknown host key -> prompt
tab1 = win.open_session(win.store.sessions[0], "secret")
check("connect", pump(15, lambda: tab1.state == "connected"), tab1.state)
check("hostkey prompt shown once", seen["hostkey"] == 1, str(seen))
pump(0.5)
check("banner rendered", "Welcome to demo-box" in text(tab1))
check("known_hosts written", (HOME / ".techxterminal" / "known_hosts").read_text().strip() != "")
check("connect: the terminal says which address answered", "Connected to 127.0.0.1, logging in" in text(tab1), text(tab1)[:200])

# ---- connecting: the addresses of a name are tried side by side, not one after another ----------
from techxterminal import connections as _conn
from techxterminal import ssh as _ssh_module

real_resolve, real_connect_one, real_stagger = _conn._resolve, _conn._connect_one, _conn.ADDRESS_STAGGER


def fake_names(*addresses):
    return lambda host, port: [(family, socket.SOCK_STREAM, 6, "", (name, port)) for family, name in addresses]


def fake_connect(behaviour):
    """What each made-up address does when connected to: it answers (really: the test server), is refused, or stays silent."""

    def connect_one(family, proto, address, timeout, register):
        what = behaviour[address[0]]
        if what == "answers":
            return real_connect_one(socket.AF_INET, 6, ("127.0.0.1", PORT), timeout, register)
        if what == "refused":
            raise ConnectionRefusedError("refused")
        time.sleep(min(timeout, 3))
        raise socket.timeout("timed out")

    return connect_one


def timed_open(names, behaviour, timeout=10, stagger=None, stop=None):
    _conn._resolve = fake_names(*names)
    _conn._connect_one = fake_connect(behaviour)
    if stagger is not None:
        _conn.ADDRESS_STAGGER = stagger
    began = time.monotonic()
    try:
        sock = _conn.open_tcp("example.test", 22, timeout, stop)
        outcome = ("socket", sock.getpeername()[1] == PORT)
        sock.close()
    except Exception as exc:
        outcome = (type(exc).__name__, str(exc))
    finally:
        _conn._resolve, _conn._connect_one, _conn.ADDRESS_STAGGER = real_resolve, real_connect_one, real_stagger
    return outcome, time.monotonic() - began


v6_then_v4 = [(socket.AF_INET6, "v6"), (socket.AF_INET, "v4")]
outcome, took = timed_open(v6_then_v4, {"v6": "silent", "v4": "answers"})
check("connect: an address that never answers does not hold up the one that does", outcome == ("socket", True) and took < 2.0, f"{outcome} {took:.2f}s")
outcome, took = timed_open(v6_then_v4, {"v6": "refused", "v4": "answers"}, stagger=3.0)
check("connect: when one address fails the next is tried at once", outcome == ("socket", True) and took < 1.5, f"{outcome} {took:.2f}s")
outcome, took = timed_open(v6_then_v4, {"v6": "refused", "v4": "refused"})
check("connect: every address refused is reported as a refusal", outcome[0] == "ConnectionRefusedError", repr(outcome))
outcome, took = timed_open(v6_then_v4, {"v6": "silent", "v4": "silent"}, timeout=0.6)
check("connect: no answer from any address times out at the limit", outcome[0] == "TimeoutError" and 0.5 < took < 2.5, f"{outcome} {took:.2f}s")
stopped = [False]
threading.Timer(0.3, lambda: stopped.__setitem__(0, True)).start()
outcome, took = timed_open(v6_then_v4, {"v6": "silent", "v4": "silent"}, timeout=10, stop=lambda: stopped[0])
check("connect: cancelling stops the attempt at once", outcome[0] == "ConnectionAbortedError" and took < 2.0, f"{outcome} {took:.2f}s")
_conn._resolve = lambda host, port: (_ for _ in ()).throw(socket.gaierror(11001, "no such host"))
try:
    _conn.open_tcp("nowhere.invalid", 22, 5)
    not_found = "connected"
except socket.gaierror:
    not_found = "gaierror"
finally:
    _conn._resolve = real_resolve
check("connect: a name that cannot be found is reported as such", not_found == "gaierror", not_found)
order = _conn._interleave([(10, 1, 6, "", ("a6", 22)), (10, 1, 6, "", ("b6", 22)), (2, 1, 6, "", ("a4", 22))])
check("connect: the families alternate, the first one first", [address[0] for _family, _proto, address in order] == ["a6", "a4", "b6"], repr(order))
direct = _conn.open_tcp("127.0.0.1", PORT, 5)
check("connect: the real thing reaches a server", direct.getpeername()[1] == PORT)
direct.close()

real_slow = _ssh_module.SLOW_LOGIN_SECONDS
_ssh_module.SLOW_LOGIN_SECONDS = -1  # so that every login counts as a slow one
slow_tab = win.open_session(win.store.sessions[1], "secret")
pump(15, lambda: slow_tab.state == "connected")
pump(0.5)
_ssh_module.SLOW_LOGIN_SECONDS = real_slow
check("connect: a slow login is explained in the terminal", "Logging in took" in text(slow_tab), text(slow_tab)[:300])
win.close_tab(slow_tab)

from PyQt6.QtCore import Qt
from PyQt6.QtTest import QTest

tab1.terminal.setFocus()
QTest.keyClicks(tab1.terminal, "uname -a")
QTest.keyClick(tab1.terminal, Qt.Key.Key_Return)
pump(0.6)
out = text(tab1)
check("typed keys echoed + ran", "ran: uname -a" in out, out.splitlines()[-3:].__repr__())
size = servers[0].size
check("pty size matches widget", size == (tab1.terminal.cols, tab1.terminal.rows), f"{size} vs {(tab1.terminal.cols, tab1.terminal.rows)}")

# special keys
sent = []
tab1.terminal.send.disconnect(tab1._on_input)
tab1.terminal.send.connect(sent.append)
for key, mod in [(Qt.Key.Key_Up, Qt.KeyboardModifier.NoModifier), (Qt.Key.Key_C, Qt.KeyboardModifier.ControlModifier),
                 (Qt.Key.Key_F5, Qt.KeyboardModifier.NoModifier), (Qt.Key.Key_Tab, Qt.KeyboardModifier.NoModifier),
                 (Qt.Key.Key_Left, Qt.KeyboardModifier.ControlModifier), (Qt.Key.Key_Backspace, Qt.KeyboardModifier.NoModifier)]:
    QTest.keyClick(tab1.terminal, key, mod)
check("key encoding", sent == [b"\x1b[A", b"\x03", b"\x1b[15~", b"\t", b"\x1b[1;5D", b"\x7f"], repr(sent))
tab1.terminal.send.disconnect(sent.append)
tab1.terminal.send.connect(tab1._on_input)
pump(0.3)
win.grab().save(str(SCRATCH / "shot_connected.png"))

# alt screen
tab1._worker.send(b"alt\r")
pump(0.5)
check("alt screen entered", tab1.terminal.screen.in_alt_screen and "FULLSCREEN APP" in text(tab1))
tab1._worker.send(b"unalt\r")
pump(0.5)
check("alt screen restored", not tab1.terminal.screen.in_alt_screen and "Welcome to demo-box" in text(tab1))

# scrollback
tab1._worker.send(b"flood\r")
pump(1.5)
hist = len(tab1.terminal.screen.history.top)
bar = tab1.terminal.verticalScrollBar()
check("scrollback filled, pinned to bottom", hist > 400 and bar.value() == bar.maximum() == hist, f"hist={hist} bar={bar.value()}/{bar.maximum()}")
bar.setValue(hist - 100)
pump(0.2)
tab1.terminal._sel_anchor, tab1.terminal._sel_end = (hist - 100, 0), (hist - 99, 3)
sel = tab1.terminal.selected_text()
check("selection across scrollback", sel.startswith("line ") and "\n" in sel, repr(sel))
tab1.terminal._clear_selection()
bar.setValue(bar.maximum())

# 2. no password -> password prompt; host key already trusted
tab2 = win.open_session(win.store.sessions[3])
check("connect via password prompt", pump(15, lambda: tab2.state == "connected"), tab2.state)
check("password prompted, no second hostkey prompt", seen["password"] >= 1 and seen["hostkey"] == 1, str(seen))
check("password prompt: a button shows what is typed and hides it again", seen["reveal"] == (True, True), repr(seen["reveal"]))

# broadcast
win.all_tabs.setChecked(True)
win.command.setText("echo broadcast")
win._send_command()
pump(0.8)
check("broadcast reached both tabs", "ran: echo broadcast" in text(tab1) and "ran: echo broadcast" in text(tab2))

# resize propagates
win.resize(1000, 600)
pump(0.8)
check("resize sent to server", servers[-1].size == (tab2.terminal.cols, tab2.terminal.rows), f"{servers[-1].size}")

# remote exit -> banner + disconnected, Enter reconnects
tab2._worker.send(b"exit\r")
check("remote close detected", pump(5, lambda: tab2.state == "disconnected"), tab2.state)
check("banner visible", tab2.banner.isVisible())
pump(0.3)
win.grab().save(str(SCRATCH / "shot_disconnected.png"))
QTest.keyClick(tab2.terminal, Qt.Key.Key_Return)
check("enter reconnects", pump(15, lambda: tab2.state == "connected"), tab2.state)

# 3. connection refused
bad = Session(name="dead", host="127.0.0.1", port=1, username="x")
tab3 = win.open_session(bad, "x")
check("refused -> disconnected", pump(20, lambda: tab3.state == "disconnected" and tab3.banner.isVisible()), tab3.banner_label.text())
print("   banner:", tab3.banner_label.text())

# quick connect parsing
win.quick.setText(f"deploy@127.0.0.1:{PORT}")
win._quick_connect()
tab4 = win.current_tab()
check("quick connect", tab4.session.username == "deploy" and tab4.session.port == PORT and pump(15, lambda: tab4.state == "connected"), tab4.state)

win.close_tab(win.tabs.indexOf(tab3))
win.tabs.setCurrentIndex(0)
win.resize(1240, 780)
pump(0.6)
win.grab().save(str(SCRATCH / "shot_tabs.png"))

# ---- other protocols ---------------------------------------------------------
import re as _re
import struct as _struct


def tcp_server(handler):
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(5)

    def loop():
        while True:
            try:
                conn, _ = srv.accept()
            except OSError:
                return
            threading.Thread(target=handler, args=(conn,), daemon=True).start()

    threading.Thread(target=loop, daemon=True).start()
    return srv.getsockname()[1]


# raw: banner + echo
def raw_handler(conn):
    conn.sendall(b"RAW READY\r\n")
    while True:
        data = conn.recv(1024)
        if not data:
            return
        conn.sendall(data.upper())


telnet = {"raw": b"", "lines": [], "times": {}}
_IAC_SEQ = _re.compile(rb"\xff[\xfb-\xfe].|\xff\xfa.*?\xff\xf0", _re.S)


def telnet_handler(conn):
    conn.sendall(bytes([255, 253, 24, 255, 253, 31, 255, 251, 1, 255, 251, 3]))
    time.sleep(0.2)
    conn.sendall(bytes([255, 250, 24, 1, 255, 240]))
    conn.sendall(b"Last login: never\r\nUbuntu 24.04\r\nlogin: ")
    telnet["times"]["login_prompt"] = time.time()
    buf = b""
    stage = 0
    while True:
        data = conn.recv(1024)
        if not data:
            return
        telnet["raw"] += data
        buf += _IAC_SEQ.sub(b"", data)
        while b"\r" in buf:
            line, _, buf = buf.partition(b"\r")
            buf = buf.lstrip(b"\x00")
            telnet["lines"].append(line.decode())
            if stage == 0:
                telnet["times"]["user"] = time.time()
                conn.sendall(b"\r\nPassword: ")
                telnet["times"]["password_prompt"] = time.time()
            elif stage == 1:
                telnet["times"]["password"] = time.time()
                conn.sendall(b"\r\nWelcome to telnet-box\r\n$ ")
            else:
                conn.sendall(b"\r\nran: " + line + b"\r\n$ ")
            stage += 1


rlogin = {"hello": b"", "rest": b""}


def rlogin_handler(conn):
    data = b""
    while data.count(b"\0") < 4:
        data += conn.recv(1024)
    rlogin["hello"] = data
    conn.sendall(b"\0")
    conn.sendall(b"Password: ")
    while True:
        chunk = conn.recv(1024)
        if not chunk:
            return
        rlogin["rest"] += chunk
        if b"\r" in rlogin["rest"] and not rlogin.get("done"):
            rlogin["done"] = True
            conn.sendall(b"\r\nrlogin session ready\r\n$ ")


RAW_PORT, TELNET_PORT, RLOGIN_PORT = tcp_server(raw_handler), tcp_server(telnet_handler), tcp_server(rlogin_handler)

from techxterminal.sessions import apply_command_line  # noqa: E402

# raw
traw = win.open_session(Session(name="raw", protocol="raw", host="127.0.0.1", port=RAW_PORT))
check("raw: connect + banner", pump(10, lambda: traw.state == "connected" and "RAW READY" in text(traw)), traw.state)
traw.terminal.setFocus()
QTest.keyClicks(traw.terminal, "abc")
check("raw: bytes round trip", pump(3, lambda: "ABC" in text(traw)), ascii(text(traw).strip()))

# telnet with automatic logon and a wait period
ttel = win.open_session(
    Session(name="tel", protocol="telnet", host="127.0.0.1", port=TELNET_PORT, username="operator", wait_ms=500,
            startup_command="uptime"),
    "t3lnetpw",
)
check("telnet: logged in automatically", pump(12, lambda: "Welcome to telnet-box" in text(ttel)), ascii(text(ttel).strip()[-120:]))
check("telnet: user then password typed", telnet["lines"][:2] == ["operator", "t3lnetpw"], repr(telnet["lines"]))
tt = telnet["times"]
waits = (tt["user"] - tt["login_prompt"], tt["password"] - tt["password_prompt"])
check("telnet: wait period honoured", all(0.45 <= w < 2.5 for w in waits), f"{waits[0]:.2f}s {waits[1]:.2f}s")
check("telnet: startup command sent after logon", pump(4, lambda: "ran: uptime" in text(ttel)), repr(telnet["lines"]))
raw = telnet["raw"]
check("telnet: options negotiated", bytes([255, 251, 24]) in raw and bytes([255, 251, 31]) in raw and bytes([255, 253, 1]) in raw)
check("telnet: terminal type sent", bytes([255, 250, 24, 0]) + b"XTERM-256COLOR" + bytes([255, 240]) in raw)
naws = bytes([255, 250, 31]) + _struct.pack("!HH", ttel.terminal.cols, ttel.terminal.rows) + bytes([255, 240])
check("telnet: window size sent", naws in raw, f"{ttel.terminal.cols}x{ttel.terminal.rows}")
check("telnet: password not echoed into terminal", "t3lnetpw" not in text(ttel))
win.grab().save(str(SCRATCH / "shot_telnet.png"))

# rlogin
trl = win.open_session(
    Session(name="rl", protocol="rlogin", host="127.0.0.1", port=RLOGIN_PORT, username="remoteuser"), "rl0ginpw"
)
check("rlogin: session ready", pump(10, lambda: "rlogin session ready" in text(trl)), trl.state + ascii(text(trl).strip()[-80:]))
parts = rlogin["hello"].split(b"\0")
check("rlogin: handshake fields", parts[0] == b"" and parts[2] == b"remoteuser" and parts[3] == b"xterm-256color/38400", repr(rlogin["hello"]))
check("rlogin: password typed at prompt, window size sent", b"rl0ginpw\r" in rlogin["rest"] and b"\xff\xffss" in rlogin["rest"], repr(rlogin["rest"][:60]))

# serial (pyserial loopback device)
tser = win.open_session(Session(name="ser", protocol="serial", host="loop://", port=115200))
check("serial: opened", pump(8, lambda: tser.state == "connected"), tser.state + " " + tser.banner_label.text())
tser.terminal.setFocus()
QTest.keyClicks(tser.terminal, "AT+OK")
check("serial: bytes round trip", pump(3, lambda: "AT+OK" in text(tser)), ascii(text(tser).strip()))
tbad = win.open_session(Session(name="serbad", protocol="serial", host="COM_DOES_NOT_EXIST", port=9600))
check("serial: bad port reported", pump(8, lambda: tbad.state == "disconnected" and tbad.banner.isVisible()), tbad.banner_label.text()[:80])

# ssh with the password typed at a prompt instead of sent in the login: no key and no
# password in the login means the app has to ask for one, which the poller answers.
before = seen["password"]
tsh = win.open_session(
    Session(name="typed", host="127.0.0.1", port=PORT, username="deploy", password_in_command_line=False), "secret"
)
check("ssh: password kept out of login when unchecked", pump(15, lambda: tsh.state == "connected") and seen["password"] == before + 1, f"{tsh.state} {seen}")

# command line options
cl = apply_command_line(Session(host="h", username="u", command_line='-P 2222 -C -l root -i "C:/keys/my key" -X'))
check("command line parsed", (cl.session.port, cl.session.username, cl.session.key_file, cl.compress, cl.ignored) == (2222, "root", "C:/keys/my key", True, ["-X"]), repr(cl))
tcl = win.open_session(
    Session(name="cl", host="127.0.0.1", port=1, username="nobody", command_line=f"-P {PORT} -l deploy -pw secret -C -bogus")
)
check("command line: port, user, password applied", pump(15, lambda: tcl.state == "connected"), tcl.state + " " + tcl.banner_label.text())
check("command line: unknown option reported", "Ignored command line options: -bogus" in "\n".join(tcl.terminal._line_text(i) for i in range(tcl.terminal.rows)))

# quick connect with a protocol prefix
win.quick.setText(f"telnet://127.0.0.1:{RAW_PORT}")
win._quick_connect()
tq = win.current_tab()
check("quick connect: protocol prefix", tq.session.protocol == "telnet" and pump(8, lambda: "RAW READY" in text(tq)), tq.session.address)

# session dialog: protocol switching
dlg = SessionDialog(win.store, None, win)
dlg.show()
pump(0.3)
check("dialog: defaults to ssh/22", dlg.protocol() == "ssh" and dlg.port.value() == 22 and not dlg.wait_ms.isEnabled() and dlg.auth.isVisible())
dlg._protocol_buttons["telnet"].setChecked(True)
pump(0.2)
check("dialog: telnet -> port 23, ssh-only rows off", dlg.port.value() == 23 and not dlg.auth.isVisible() and not dlg.password_in_command_line.isEnabled() and dlg.wait_ms.isEnabled())
dlg._protocol_buttons["rlogin"].setChecked(True)
check("dialog: rlogin -> 513", dlg.port.value() == 513)
dlg._protocol_buttons["serial"].setChecked(True)
pump(0.2)
check("dialog: serial -> speed 9600", dlg.port.value() == 9600 and dlg.host_label.text() == "Serial line" and dlg.port_label.text() == "Speed")
dlg.grab().save(str(SCRATCH / "shot_dialog_serial.png"))
dlg._protocol_buttons["ssh"].setChecked(True)
dlg.host.setText("example.org")
dlg.username.setText("me")
dlg.password_in_command_line.setChecked(False)
dlg.wait_ms.setValue(750)
dlg.command_line.setText("-C")
pump(0.3)
check("dialog: wait enabled when password is typed", dlg.wait_ms.isEnabled())
made = dlg.session()
check("dialog: values saved", (made.protocol, made.port, made.wait_ms, made.password_in_command_line, made.command_line) == ("ssh", 22, 750, False, "-C"), repr(made))
dlg.grab().save(str(SCRATCH / "shot_dialog.png"))
dlg.close()

# old sessions.json without the new fields still loads
import json as _json
legacy = HOME / "legacy.json"
legacy.write_text(_json.dumps({"sessions": [{"name": "old", "host": "h", "port": 22, "username": "u", "id": "abc"}]}))
from techxterminal.sessions import SessionStore as _Store
old = _Store(legacy).sessions[0]
check("legacy session file loads with defaults", old.protocol == "ssh" and old.password_in_command_line and old.wait_ms == 0)

for t in (traw, ttel, trl, tser, tbad, tsh, tcl, tq):
    win.close_tab(win.tabs.indexOf(t))
win.tabs.setCurrentIndex(0)
pump(0.3)

# ---- session colours + font warnings -------------------------------------------
from techxterminal.sidebar import ROLE_COLOR, ROLE_PINNED, ROLE_SESSION_ID, ROLE_SUBTITLE
from techxterminal.dialogs import ColorPicker

win.tabs.setCurrentIndex(0)
prod = win.store.sessions[0]  # web-01, open in tab1
check("colour: none by default, strip hidden", prod.color == "" and tab1.color_strip.isHidden())

orig_dialog_exec = SessionDialog.exec


def choose_red(self):
    self.color.set_color("#f7768e")
    return True


SessionDialog.exec = choose_red
win.edit_session(prod)
SessionDialog.exec = orig_dialog_exec
pump(0.4)
saved = win.store.get(prod.id)
check("colour: saved on the session", saved.color == "#f7768e", saved.color)
check("colour: open tab updated live", tab1.session.color == "#f7768e" and not tab1.color_strip.isHidden())
strip = tab1.color_strip.grab().toImage().pixelColor(5, 1).name()
check("colour: strip painted in that colour", strip == "#f7768e", strip)
items = [win.tree.topLevelItem(i) for i in range(win.tree.topLevelItemCount())]
rows = [it.child(j) for it in items for j in range(it.childCount())] + [it for it in items if not it.childCount()]
colours = {r.text(0): r.data(0, ROLE_COLOR) for r in rows}
check("colour: sidebar row carries it", colours.get("web-01") == "#f7768e" and colours.get("web-02") == "", repr(colours))
import json as _json2
on_disk = _json2.loads((HOME / ".techxterminal" / "sessions.json").read_text())["sessions"]
check("colour: written to sessions.json", any(s.get("color") == "#f7768e" for s in on_disk))

# a second coloured session, to see two colours side by side
green = Session(name="db-green", group="Staging", host="127.0.0.1", port=PORT, username="deploy", color="#9ece6a")
win.store.upsert(green)
win._reload_tree()
tgreen = win.open_session(green, "secret")
pump(15, lambda: tgreen.state == "connected")
pump(0.4)
win.tabs.setCurrentIndex(0)
pump(0.3)
win.grab().save(str(SCRATCH / "shot_colors.png"))

picker = ColorPicker("#123456")
check("picker: custom colour kept", picker.color() == "#123456" and picker.custom.isChecked())
picker.set_color("#7AA2F7")
check("picker: preset selected", picker.color() == "#7aa2f7" and picker._swatches["#7aa2f7"].isChecked() and not picker.custom.isChecked())
picker.set_color("")
check("picker: none", picker.color() == "" and picker._swatches[""].isChecked())
picker.set_color("not-a-colour")
check("picker: invalid input ignored", picker.color() == "")

dlg = SessionDialog(win.store, saved, win)
dlg.show()
pump(0.4)
check("dialog: shows the saved colour", dlg.color.color() == "#f7768e" and dlg.session().color == "#f7768e")
dlg.grab().save(str(SCRATCH / "shot_dialog_color.png"))
dlg.close()

win.close_tab(win.tabs.indexOf(tgreen))
win.tabs.setCurrentIndex(0)
pump(0.3)

# settings dialog, themes and preferences
from dataclasses import replace as _replace
from PyQt6.QtGui import QFontInfo, QColor as _QColor
from PyQt6.QtWidgets import QApplication as _QApp
from techxterminal import theme as _theme
from techxterminal.preferences import Preferences

P = win.prefs
check("prefs: defaults", (P.theme, P.terminal_font, P.terminal_size, P.ui_font, P.ui_size, P.cursor_style, P.scrollback) == ("dark", "Ubuntu Mono", 12, "Ubuntu", 10, "block", 10000), repr(P))


def lum(image, x, y):
    return image.pixelColor(x, y).lightnessF()


sd = SettingsDialog(win.prefs, win)
sd.applied.connect(win.apply_preferences)
check("settings: shows current values", sd.preferences() == win.prefs, repr(sd.preferences()))
sd.show()
pump(0.4)
sd.grab().save(str(SCRATCH / "shot_settings_appearance.png"))
sd.pages.setCurrentIndex(1)
pump(0.3)
sd.grab().save(str(SCRATCH / "shot_settings_terminal.png"))
sd.pages.setCurrentIndex(2)
pump(0.3)
sd.grab().save(str(SCRATCH / "shot_settings_behaviour.png"))
sd.pages.setCurrentIndex(0)

# fonts: no bitmap fonts offered and no Qt warnings while browsing the lists
qt_messages.clear()
for combo in (sd.terminal_font, sd.ui_font):
    combo.showPopup()
    pump(0.3)
    for i in range(combo.count()):
        combo.setCurrentIndex(i)
    pump(0.2)
    combo.hidePopup()
names = [sd.terminal_font.itemText(i) for i in range(sd.terminal_font.count())]
check("fonts: no bitmap fonts offered", not {"Fixedsys", "Terminal", "System", "Courier"} & set(names) and "Ubuntu Mono" in names, repr(names[:6]))
check("fonts: no Qt font warnings", not [m for m in qt_messages if "font" in m.lower()])
sd.set_preferences(win.prefs)

# switch to the light theme with Apply, while the dialog and tabs are open
win.tabs.setCurrentIndex(0)
dark_term = tab1.terminal.grab().toImage()
dark_bar = win.grab().toImage()
sd._theme_cards["light"].setChecked(True)
sd._apply()
pump(0.6)
check("theme: light applied", _theme.active_theme() == "light" and win.prefs.theme == "light" and _theme.COLORS["bg"] == "#eceef4")
light_term = tab1.terminal.grab().toImage()
light_bar = win.grab().toImage()
check("theme: terminal background switched", lum(dark_term, 300, 300) < 0.2 and lum(light_term, 300, 300) > 0.9, f"{lum(dark_term, 300, 300):.2f} -> {lum(light_term, 300, 300):.2f}")
check("theme: window chrome switched", lum(dark_bar, 300, 12) < 0.2 and lum(light_bar, 300, 12) > 0.8, f"{lum(dark_bar, 300, 12):.2f} -> {lum(light_bar, 300, 12):.2f}")
check("theme: palette follows", _QApp.palette().window().color().name() == "#eceef4")
check("theme: terminal text colour is dark on light", tab1.terminal._fg.lightnessF() < 0.3 and tab1.terminal._color("green", tab1.terminal._fg).name() == "#2f7a22")
check("theme: status dot uses the light palette", _theme.COLORS["green"] in win.status_left.text(), win.status_left.text()[:60])
win.grab().save(str(SCRATCH / "shot_light.png"))
sd.grab().save(str(SCRATCH / "shot_settings_light.png"))
win.tabs.setCurrentIndex(win.tabs.indexOf(tgreen) if "tgreen" in dir() and win.tabs.indexOf(tgreen) >= 0 else 0)

# accent colour
sd.accent.set_color("#d1344f")
sd._apply()
pump(0.4)
check("accent: custom colour applied", _theme.COLORS["accent"] == "#d1344f" and tab1.terminal._cursor_color.name() == "#d1344f" and _theme.COLORS["on_accent"] == "#ffffff", _theme.COLORS["accent"])
sd.accent.set_color("")
sd._apply()
pump(0.3)
check("accent: back to theme default", _theme.COLORS["accent"] == "#2f5fd0")

# terminal options
cols_before = tab1.terminal.cols
sd.set_preferences(_replace(win.prefs, terminal_font="Consolas", terminal_size=16, ui_font="Segoe UI", ui_size=12, cursor_style="bar", cursor_blink=False, scrollback=200, copy_on_select=False, show_send_bar=False))
sd._apply()
pump(0.8)
t = tab1.terminal
check("terminal font applied", QFontInfo(t._fonts[(False, False)]).family() == "Consolas" and t.cols < cols_before, f"cols {cols_before}->{t.cols}")
check("ui font applied", QFontInfo(win.quick.font()).family() == "Segoe UI" and win.quick.font().pointSize() == 12, f"{QFontInfo(win.quick.font()).family()} {win.quick.font().pointSize()}")
check("pty resized after font change", servers[0].size == (t.cols, t.rows), str(servers[0].size))
check("cursor style + blink applied", t._cursor_style == "bar" and not t._cursor_blink)
check("scrollback trimmed to the newest lines", len(t.screen.history.top) == 200 and t.screen.history.top.maxlen == 200 and t.verticalScrollBar().maximum() == 200, f"{len(t.screen.history.top)}")
check("scrollback still works after trimming", "line 4" in t._line_text(150), t._line_text(150).strip())
check("send bar hidden", win.command_bar.isHidden())
CLIP.setText("untouched")
t._sel_anchor, t._sel_end = (10, 0), (10, 5)
t._selecting = True
from PyQt6.QtCore import QPointF as _QPointF, QEvent as _QEvent
from PyQt6.QtGui import QMouseEvent as _QMouseEvent
t.mouseReleaseEvent(_QMouseEvent(_QEvent.Type.MouseButtonRelease, _QPointF(5, 5), Qt.MouseButton.LeftButton, Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier))
check("copy on select off: clipboard untouched", CLIP.text() == "untouched", CLIP.text())
t._clear_selection()
win.grab().save(str(SCRATCH / "shot_light_bar_cursor.png"))

reloaded = Preferences.load()
check("prefs: persisted and reloaded with the right types", reloaded == win.prefs and reloaded.cursor_blink is False and reloaded.scrollback == 200 and reloaded.theme == "light", repr(reloaded))

# new tabs pick up the preferences
tnew = win.open_session(Session(name="np", protocol="serial", host="loop://", port=9600))
pump(1.0)
check("new tab uses preferences", tnew.terminal._cursor_style == "bar" and tnew.terminal.screen.history.top.maxlen == 200 and tnew.terminal._bg.lightnessF() > 0.9)
tnew.terminal.set_terminal_font("Fixedsys", 16)
pump(0.4)
check("fonts: a saved bitmap font falls back cleanly", QFontInfo(tnew.terminal._fonts[(False, False)]).family() == "Ubuntu Mono")

# confirmations
asked = []
win.confirm = lambda title, text, setting: (asked.append(title), False)[1]
win.apply_preferences(_replace(win.prefs, confirm_close_tab=True))
count = win.tabs.count()
win.close_tab(win.tabs.indexOf(tnew))
check("confirm closing a tab: asked, and No keeps it", asked == ["Close Tab"] and win.tabs.count() == count, repr(asked))
del win.confirm
hostkeys_seen = seen["hostkey"]  # the dialog poller counts every message box


def answer_confirmation(tick):
    box = app.activeModalWidget()
    if isinstance(box, QMessageBox) and box.checkBox() is not None:
        box.checkBox().setChecked(tick)
        box.button(QMessageBox.StandardButton.Yes).click()


QTimer.singleShot(300, lambda: answer_confirmation(True))
stopped_asking = win.confirm("Close Tab", "Close it anyway?", "confirm_close_tab")
seen["hostkey"] = hostkeys_seen
check("confirmations: Don't ask again with Yes turns the setting off and says where it is", stopped_asking and not win.prefs.confirm_close_tab and not Preferences.load().confirm_close_tab and "Settings, Behaviour" in win.statusBar().currentMessage(), win.statusBar().currentMessage())
win.close_tab(win.tabs.indexOf(tnew))
check("confirm closing a tab off: closes silently", asked == ["Close Tab"] and win.tabs.count() == count - 1)

# system theme + reset
sd.close()
win.apply_preferences(_replace(win.prefs, theme="system"))
pump(0.4)
check("theme: system resolves to a concrete theme", _theme.active_theme() in ("dark", "light") and _theme.COLORS["bg"] == _theme._PALETTES[_theme.active_theme()]["bg"], _theme.active_theme())
sd3 = SettingsDialog(win.prefs, win)
sd3.applied.connect(win.apply_preferences)
sd3.set_preferences(Preferences())
sd3._save()
pump(0.6)
check("reset to defaults", win.prefs == Preferences() and _theme.active_theme() == "dark" and not win.command_bar.isHidden() and QFontInfo(tab1.terminal._fonts[(False, False)]).family() == "Ubuntu Mono")
check("theme: back to dark", lum(tab1.terminal.grab().toImage(), 300, 300) < 0.2)

# responsive
win.resize(620, 520)
pump(0.6)
check("compact: sidebar hidden, labels dropped", win.sidebar.isHidden() and win.brand_text.isHidden() and win.caption.isHidden() and win.new_button.text() == "")
check("status bar: a message is never drawn over the connection state", win.status_left.isVisible() == (not win.statusBar().currentMessage()), repr(win.statusBar().currentMessage()))
win.grab().save(str(SCRATCH / "shot_narrow.png"))
win.statusBar().hide()
win.notify("While the bar was hidden")
win.statusBar().show()
pump(0.1)
hidden_under_message = not win.status_left.isVisible()
win.statusBar().clearMessage()
check("status bar: a message that came while it was hidden still covers the connection state, which comes back after it", hidden_under_message and win.status_left.isVisible())
win.toggle_sidebar()
pump(0.4)
check("compact: sidebar opens on demand", not win.sidebar.isHidden() and win.sidebar.width() <= 320, str(win.sidebar.width()))
from techxterminal.theme import initials as _initials
win.toggle_sidebar()
pump(0.3)
rail_tiles = [tile for tile in win.rail.findChildren(type(win.add_tab_button)) if tile.objectName() == "RailSession"]
check("sidebar: collapsed, it leaves a rail with a tile for each saved session", win.sidebar.isHidden() and not win.rail.isHidden() and len(rail_tiles) == len(win.store.sessions) and all(tile.toolTip() for tile in rail_tiles), f"{len(rail_tiles)} of {len(win.store.sessions)}")
tabs_before_rail = len(win.open_tabs())
first_session = win._rail_sessions()[0]
rail_tab = win._rail_open(first_session)
again = win._rail_open(first_session)
check("sidebar: a tile opens its session, or goes to its tab when it is open already", rail_tab is again and rail_tab.session.id == first_session.id and len(win.open_tabs()) == tabs_before_rail + 1 and win.current_tab() is rail_tab)
win.close_tab(rail_tab, force=True)
pump(0.2)
check("sidebar: tiles show initials, keeping the numbers of numbered servers apart", [_initials(name) for name in ("db-primary", "web-01", "web-12", "raspberry", "my server")] == ["DP", "W1", "W12", "RA", "MS"])
win.toggle_sidebar()
pump(0.3)
check("sidebar: opened again, the rail gives way to it", not win.sidebar.isHidden() and win.rail.isHidden())
win.grab().save(str(SCRATCH / "shot_narrow_sidebar.png"))
win.resize(1240, 780)
pump(0.6)
check("wide again: sidebar + labels back", not win.sidebar.isHidden() and not win.brand_text.isHidden() and not win.caption.isHidden() and win.new_button.text() == "New Session")
win.resize(460, 320)
pump(0.5)
check("minimum size usable", tab1.terminal.cols >= 20 and tab1.terminal.rows >= 4, f"{tab1.terminal.cols}x{tab1.terminal.rows}")
win.grab().save(str(SCRATCH / "shot_min.png"))
win.resize(1240, 780)
pump(0.5)

# ==== features added in 0.2 ===================================================================
import base64 as _b64
import hashlib as _hashlib
import hmac as _hmac
import struct as _st

from PyQt6.QtCore import QPoint as _QPoint, QPointF as _QPF
from PyQt6.QtGui import QWheelEvent as _QWheelEvent
from techxterminal import importers, ppk
from techxterminal.connections import parse_serial_format
from techxterminal.preferences import Preferences as _Prefs
from techxterminal.sessions import parse_host_spec, parse_jump_hosts, parse_tunnels
from techxterminal.terminal import TerminalWidget

win.tabs.setCurrentIndex(0)
win.resize(1240, 780)
pump(0.4)
t1 = tab1.terminal


def run(tab, command, wait=0.6):
    if tab._worker is None:
        # Say why, rather than failing on the next line with nothing to go on.
        tail = [tab.terminal._line_text(i).rstrip() for i in range(max(0, tab.terminal._total_lines() - 12), tab.terminal._total_lines())]
        raise RuntimeError(f"{tab.title} is {tab.state}; banner: {tab.banner_label.text()!r}; last output: {[line for line in tail if line][-5:]}")
    tab._worker.send(command.encode() + b"\r")
    pump(wait)


def file_bytes(path):
    """A file's contents, or None while it is missing or being replaced."""
    try:
        return Path(path).read_bytes()
    except OSError:
        return None


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def cell_point(term, col, row):
    from techxterminal.terminal import PADDING
    return _QPoint(int(PADDING + (col + 0.5) * term._cw), int(PADDING + (row + 0.5) * term._ch))


# ---- two-factor login ---------------------------------------------------------------
before_codes = seen["code"]
tmfa = win.open_session(Session(name="mfa-box", host="127.0.0.1", port=PORT, username="mfa"), "secret")
check("2FA: password then verification code logs in", pump(20, lambda: tmfa.state == "connected"), tmfa.state + " " + tmfa.banner_label.text())
check("2FA: the code was asked for once, in a visible field", seen["code"] == before_codes + 1 and seen["code_echo"] is True and seen["code_actions"] == 0, repr(seen))
win.close_tab(tmfa)

# ---- .ppk key files -----------------------------------------------------------------
from cryptography.hazmat.primitives.asymmetric import ed25519 as _ed
from cryptography.hazmat.primitives.ciphers import Cipher as _Cipher, algorithms as _alg, modes as _modes


def _s(data):
    return _st.pack(">I", len(data)) + data


def _mp(number):
    raw = number.to_bytes((number.bit_length() + 8) // 8, "big")
    return _s(raw)


def write_ppk(path, algorithm, public, private, version, passphrase=None):
    """An independent writer for the documented .ppk layout, used to exercise the reader."""
    comment = "test-key"
    encryption = "aes256-cbc" if passphrase else "none"
    extra = []
    padded = private
    if passphrase:
        padded = private + bytes((-len(private)) % 16)
        if version == 2:
            pw = passphrase.encode()
            key = (_hashlib.sha1(b"\0\0\0\0" + pw).digest() + _hashlib.sha1(b"\0\0\0\1" + pw).digest())[:32]
            iv = bytes(16)
            mac_key = _hashlib.sha1(b"putty-private-key-file-mac-key" + pw).digest()
        else:
            from cryptography.hazmat.primitives.kdf.argon2 import Argon2id
            salt = os.urandom(16)
            out = Argon2id(salt=salt, length=80, iterations=3, lanes=1, memory_cost=8192).derive(passphrase.encode())
            key, iv, mac_key = out[:32], out[32:48], out[48:]
            extra = ["Key-Derivation: Argon2id", "Argon2-Memory: 8192", "Argon2-Passes: 3", "Argon2-Parallelism: 1", f"Argon2-Salt: {salt.hex()}"]
        enc = _Cipher(_alg.AES(key), _modes.CBC(iv)).encryptor()
        stored = enc.update(padded) + enc.finalize()
    else:
        stored = private
        mac_key = _hashlib.sha1(b"putty-private-key-file-mac-key").digest() if version == 2 else b""
    payload = _s(algorithm.encode()) + _s(encryption.encode()) + _s(comment.encode()) + _s(public) + _s(padded)
    mac = _hmac.new(mac_key, payload, _hashlib.sha1 if version == 2 else _hashlib.sha256).hexdigest()

    def lines(blob):
        text = _b64.b64encode(blob).decode()
        return [text[i : i + 64] for i in range(0, len(text), 64)]

    pub_lines, priv_lines = lines(public), lines(stored)
    body = [f"PuTTY-User-Key-File-{version}: {algorithm}", f"Encryption: {encryption}", f"Comment: {comment}",
            f"Public-Lines: {len(pub_lines)}", *pub_lines, *extra, f"Private-Lines: {len(priv_lines)}", *priv_lines, f"Private-MAC: {mac}"]
    Path(path).write_text("\n".join(body) + "\n")


numbers = USER_KEY.key.private_numbers()
rsa_public = _s(b"ssh-rsa") + _mp(numbers.public_numbers.e) + _mp(numbers.public_numbers.n)
rsa_private = _mp(numbers.d) + _mp(numbers.p) + _mp(numbers.q) + _mp(numbers.iqmp)
ed_key = _ed.Ed25519PrivateKey.generate()
from cryptography.hazmat.primitives import serialization as _ser
ed_pub_raw = ed_key.public_key().public_bytes(_ser.Encoding.Raw, _ser.PublicFormat.Raw)
ed_priv_raw = ed_key.private_bytes(_ser.Encoding.Raw, _ser.PrivateFormat.Raw, _ser.NoEncryption())
ed_public = _s(b"ssh-ed25519") + _s(ed_pub_raw)

ppk_ok = True
for version in (2, 3):
    for passphrase in (None, "secret"):
        p_rsa = WORK / f"rsa-v{version}-{'enc' if passphrase else 'plain'}.ppk"
        write_ppk(p_rsa, "ssh-rsa", rsa_public, rsa_private, version, passphrase)
        ppk_ok &= ppk.load(str(p_rsa), passphrase).asbytes() == USER_KEY.asbytes()
        p_ed = WORK / f"ed-v{version}-{'enc' if passphrase else 'plain'}.ppk"
        write_ppk(p_ed, "ssh-ed25519", ed_public, _s(ed_priv_raw), version, passphrase)
        ppk_ok &= ppk.load(str(p_ed), passphrase).asbytes() == ed_public
check(".ppk: RSA and Ed25519, versions 2 and 3, plain and encrypted", ppk_ok)
encrypted_ppk = str(WORK / "rsa-v3-enc.ppk")
try:
    ppk.load(encrypted_ppk, None)
    needs = False
except paramiko.PasswordRequiredException:
    needs = True
try:
    ppk.load(encrypted_ppk, "wrong")
    wrong = False
except paramiko.PasswordRequiredException:
    wrong = True
check(".ppk: missing or wrong passphrase is reported as such", needs and wrong and ppk.is_ppk(encrypted_ppk))
damaged = WORK / "damaged.ppk"
damaged.write_text(Path(WORK / "rsa-v2-plain.ppk").read_text().replace("Comment: test-key", "Comment: tampered"))
try:
    ppk.load(str(damaged))
    tamper = False
except ppk.PpkError:
    tamper = True
check(".ppk: a tampered file is rejected", tamper)


def ppk_outcome(content):
    """What reading a key file with this content does, with a time limit: a hang would otherwise stop the whole run."""
    target = WORK / "hostile.ppk"
    target.write_text(content)
    result = []

    def attempt():
        try:
            ppk.load(str(target), "secret")
            result.append("loaded")
        except ppk.PpkError:
            result.append("PpkError")
        except paramiko.PasswordRequiredException:
            result.append("passphrase")
        except BaseException as error:  # noqa: BLE001
            result.append(type(error).__name__)

    worker = threading.Thread(target=attempt, daemon=True)
    began = time.perf_counter()
    worker.start()
    worker.join(20)
    return (result[0] if result else "still running"), time.perf_counter() - began


v3_text = Path(WORK / "rsa-v3-enc.ppk").read_text()
hostile = {
    "a negative number of lines": v3_text.replace("Public-Lines: ", "Public-Lines: -").replace("Private-Lines: ", "Private-Lines: -"),
    "more lines than the file has": v3_text.replace("Private-Lines: ", "Private-Lines: 9"),
    "Argon2 asked for terabytes": v3_text.replace("Argon2-Memory: 8192", "Argon2-Memory: 4000000000"),
    "Argon2 asked for a billion passes": v3_text.replace("Argon2-Passes: 3", "Argon2-Passes: 1000000000"),
    "Argon2 salt that is not hex": v3_text.replace("Argon2-Salt: ", "Argon2-Salt: zz"),
    "an Argon2 setting missing": "\n".join(line for line in v3_text.split("\n") if not line.startswith("Argon2-Passes")),
    "a number that is not a number": v3_text.replace("Argon2-Parallelism: 1", "Argon2-Parallelism: one"),
    "cut off in the middle": v3_text[: len(v3_text) // 2],
    "empty": "",
}
outcomes = {name: ppk_outcome(content) for name, content in hostile.items()}
check(".ppk: damaged or hostile files are refused with an error of their own, quickly, and never hang", all(outcome == "PpkError" and took < 5 for outcome, took in outcomes.values()), repr({name: (outcome, round(took, 1)) for name, (outcome, took) in outcomes.items() if outcome != "PpkError" or took >= 5}))
before_pp = seen["password"]
tkey = win.open_session(Session(name="key-box", host="127.0.0.1", port=PORT, username="keyuser", auth="key", key_file=encrypted_ppk))
check(".ppk: logs in with an encrypted key after asking for the passphrase", pump(20, lambda: tkey.state == "connected") and seen["password"] == before_pp + 1, tkey.state + " " + tkey.banner_label.text())
win.close_tab(tkey)

# ---- DSA keys are refused with an explanation ---------------------------------------------------
from techxterminal.ssh import is_dsa_key

dsa_pem = WORK / "id_dsa"
dsa_pem.write_text("-----BEGIN DSA PRIVATE KEY-----\nMIIBuwIBAAKBgQD\n-----END DSA PRIVATE KEY-----\n")
dsa_ppk = WORK / "old.ppk"
dsa_ppk.write_text("PuTTY-User-Key-File-2: ssh-dss\nEncryption: none\n")
dsa_openssh = WORK / "id_dsa_new"
dsa_openssh.write_text("-----BEGIN OPENSSH PRIVATE KEY-----\n" + _b64.b64encode(b"openssh-key-v1\0" + _s(b"none") + _s(b"none") + _s(b"") + b"\0\0\0\1" + _s(_s(b"ssh-dss") + b"x" * 40)).decode() + "\n-----END OPENSSH PRIVATE KEY-----\n")
rsa_file = WORK / "id_rsa_ok"
USER_KEY.write_private_key_file(str(rsa_file))
check("DSA: recognised in all three file formats, other keys are not", is_dsa_key(str(dsa_pem)) and is_dsa_key(str(dsa_ppk)) and is_dsa_key(str(dsa_openssh)) and not is_dsa_key(str(rsa_file)) and not is_dsa_key(encrypted_ppk) and not is_dsa_key(str(WORK / "missing")))
prompts_before = seen["password"]
tdsa = win.open_session(Session(name="old-key", host="127.0.0.1", port=PORT, username="keyuser", auth="key", key_file=str(dsa_pem)))
check("DSA: the session stops with an explanation instead of asking for passwords", pump(15, lambda: tdsa.state == "disconnected" and "DSA keys are no longer supported" in tdsa.banner_label.text()) and seen["password"] == prompts_before, tdsa.banner_label.text())
win.close_tab(tdsa)
trsa = win.open_session(Session(name="rsa-key", host="127.0.0.1", port=PORT, username="keyuser", auth="key", key_file=str(rsa_file)))
check("keys: an ordinary RSA key file still logs in", pump(20, lambda: trsa.state == "connected"), trsa.banner_label.text())
win.close_tab(trsa)

# ---- jump host and tunnels ----------------------------------------------------------
check("parsers: tunnels and host specs", parse_tunnels("8080:localhost:80, 0.0.0.0:53:dns:53 junk")[0][1].listen_host == "0.0.0.0" and parse_tunnels("a:b")[1] == ["a:b"] and parse_host_spec("me@gate:2200") == ("me", "gate", 2200) and parse_host_spec("gate") == ("", "gate", 22))
logins, bad_logins = parse_tunnels("D:al:s:e:c@1081 D:al:pw@0.0.0.0:1082 D:al@1083 D::x@1")
check("parsers: SOCKS proxy logins", [(t.username, t.password, t.listen_host, t.listen_port) for t in logins] == [("al", "s:e:c", "127.0.0.1", 1081), ("al", "pw", "0.0.0.0", 1082)] and bad_logins == ["D:al@1083", "D::x@1"], repr((logins, bad_logins)))
kinds, bad = parse_tunnels("L:81:a:80 R:9090:localhost:3000, r:0.0.0.0:9091:web:80 D:1080 d:10.0.0.1:1081 D:x R:9:h 70000:h:1")
check("parsers: local, remote and SOCKS tunnels", [(t.kind, t.listen_host, t.listen_port, t.host, t.port) for t in kinds] == [("L", "127.0.0.1", 81, "a", 80), ("R", "127.0.0.1", 9090, "localhost", 3000), ("R", "0.0.0.0", 9091, "web", 80), ("D", "127.0.0.1", 1080, "", 0), ("D", "10.0.0.1", 1081, "", 0)] and bad == ["D:x", "R:9:h", "70000:h:1"], repr((kinds, bad)))
check("parsers: several jump hosts", parse_jump_hosts("a@g1, g2:2200 ,") == [("a", "g1", 22), ("", "g2", 2200)] and parse_jump_hosts("a@g1, :bad") is None and parse_jump_hosts("") == [])
cl2 = apply_command_line(Session(host="h", username="u", tunnels="1:a:2", command_line="-L 3:b:4 -R 5:c:6 -D 1080 -J x@g1,y@g2"))
check("command line: -L -R -D -J", cl2.session.tunnels == "1:a:2 3:b:4 R:5:c:6 D:1080" and cl2.session.jump_host == "x@g1,y@g2" and not cl2.ignored, repr(cl2.session.tunnels))
connections_before = len(servers)
tjump = win.open_session(Session(name="via-jump", host="127.0.0.1", port=PORT, username="deploy", jump_host=f"deploy@127.0.0.1:{PORT}"), "secret")
check("jump host: session opens through the intermediate server", pump(25, lambda: tjump.state == "connected") and len(servers) >= connections_before + 2 and any(s.forwards for s in servers[connections_before:]), f"{tjump.state} {tjump.banner_label.text()} {len(servers) - connections_before}")
run(tjump, "hostname")
check("jump host: the shell works end to end", "ran: hostname" in text(tjump))
win.close_tab(tjump)
connections_before = len(servers)
tjump2 = win.open_session(Session(name="via-two-jumps", host="127.0.0.1", port=PORT, username="deploy", jump_host=f"deploy@127.0.0.1:{PORT}, deploy@127.0.0.1:{PORT}"), "secret")
check("jump hosts: a chain of two hops reaches the target", pump(40, lambda: tjump2.state == "connected") and sum(1 for srv in servers[connections_before:] if srv.forwards) >= 2 and len(tjump2._worker._jumps) == 2, f"{tjump2.state} {tjump2.banner_label.text()}")
run(tjump2, "whoami")
check("jump hosts: the shell works through the chain", "ran: whoami" in text(tjump2))
win.close_tab(tjump2)

tunnel_port = free_port()
ttun = win.open_session(Session(name="tunnel", host="127.0.0.1", port=PORT, username="deploy", tunnels=f"{tunnel_port}:127.0.0.1:{RAW_PORT}, bogus"), "secret")
pump(20, lambda: ttun.state == "connected")
pump(0.5)
try:
    with socket.create_connection(("127.0.0.1", tunnel_port), timeout=5) as c:
        c.settimeout(5)
        greeting = c.recv(100)
        c.sendall(b"ping")
        reply = c.recv(100)
except OSError as exc:
    greeting, reply = repr(exc).encode(), b""
check("tunnel: local port reaches the remote service through SSH", greeting.startswith(b"RAW READY") and reply == b"PING", repr((greeting, reply)))
notices = "\n".join(ttun.terminal._line_text(i) for i in range(ttun.terminal._total_lines()))
check("tunnel: started and malformed entries are reported", f"Forwarding 127.0.0.1:{tunnel_port}" in notices and "Tunnel 'bogus' ignored" in notices)
win.close_tab(ttun)
pump(0.5)
try:
    socket.create_connection(("127.0.0.1", tunnel_port), timeout=1).close()
    still_open = True
except OSError:
    still_open = False
check("tunnel: the local port closes with the session", not still_open)

remote_port, socks_port, locked_port = free_port(), free_port(), free_port()
tfw = win.open_session(Session(name="forwards", host="127.0.0.1", port=PORT, username="deploy", tunnels=f"R:{remote_port}:127.0.0.1:{RAW_PORT}, D:{socks_port}, D:proxyuser:pr0xy:pw@{locked_port}"), "secret")
pump(20, lambda: tfw.state == "connected")
pump(0.5)


def through(port, prelude=None):
    """Connect to a local port, optionally speak a proxy handshake, then use the raw echo service."""
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=5) as c:
            c.settimeout(5)
            handshake = prelude(c) if prelude else b""
            greeting = c.recv(100)
            c.sendall(b"ping")
            return handshake, greeting, c.recv(100)
    except OSError as exc:
        return b"", repr(exc).encode(), b""


def socks5(c):
    c.sendall(b"\x05\x01\x00")
    methods = c.recv(2)
    c.sendall(b"\x05\x01\x00\x03" + bytes([9]) + b"localhost" + _st.pack(">H", RAW_PORT))
    return methods + c.recv(10)


def socks4(c):
    c.sendall(b"\x04\x01" + _st.pack(">H", RAW_PORT) + socket.inet_aton("127.0.0.1") + b"me\x00")
    return c.recv(8)


def socks5_bind(c):
    c.sendall(b"\x05\x01\x00")
    c.recv(2)
    c.sendall(b"\x05\x02\x00\x01" + socket.inet_aton("127.0.0.1") + _st.pack(">H", RAW_PORT))
    return c.recv(10)


_, greeting, reply = through(remote_port)
check("remote tunnel: a port on the server leads back to a service on this side", greeting.startswith(b"RAW READY") and reply == b"PING", repr((greeting, reply)))
handshake, greeting, reply = through(socks_port, socks5)
check("SOCKS proxy: version 5 with a host name", handshake[:2] == b"\x05\x00" and handshake[2:4] == b"\x05\x00" and greeting.startswith(b"RAW READY") and reply == b"PING", repr((handshake, greeting, reply)))
handshake, greeting, reply = through(socks_port, socks4)
check("SOCKS proxy: version 4", handshake[:2] == b"\x00\x5a" and greeting.startswith(b"RAW READY") and reply == b"PING", repr((handshake, greeting, reply)))
with socket.create_connection(("127.0.0.1", socks_port), timeout=5) as refused_connection:
    refused_connection.settimeout(5)
    refusal = socks5_bind(refused_connection)  # the proxy answers, then hangs up
check("SOCKS proxy: anything but CONNECT is refused", refusal[:2] == b"\x05\x07", repr(refusal))


def socks5_login(name, secret):
    def handshake(c):
        c.sendall(b"\x05\x02\x00\x02")
        chosen = c.recv(2)
        c.sendall(b"\x01" + bytes([len(name)]) + name + bytes([len(secret)]) + secret)
        verdict = c.recv(2)
        if verdict != b"\x01\x00":
            return chosen + verdict
        c.sendall(b"\x05\x01\x00\x01" + socket.inet_aton("127.0.0.1") + _st.pack(">H", RAW_PORT))
        return chosen + verdict + c.recv(10)

    return handshake


handshake, greeting, reply = through(locked_port, socks5_login(b"proxyuser", b"pr0xy:pw"))
check("SOCKS login: the right name and password are let through", handshake[:4] == b"\x05\x02\x01\x00" and greeting.startswith(b"RAW READY") and reply == b"PING", repr((handshake, greeting, reply)))
with socket.create_connection(("127.0.0.1", locked_port), timeout=5) as c:
    c.settimeout(5)
    wrong = socks5_login(b"proxyuser", b"guess")(c)
with socket.create_connection(("127.0.0.1", locked_port), timeout=5) as c:
    c.settimeout(5)
    c.sendall(b"\x05\x01\x00")  # offers "no login" only
    no_login = c.recv(2)
with socket.create_connection(("127.0.0.1", locked_port), timeout=5) as c:
    c.settimeout(5)
    old_version = socks4(c)
check("SOCKS login: a wrong password, no login and SOCKS4 are all refused", wrong == b"\x05\x02\x01\x01" and no_login == b"\x05\xff" and old_version[:2] == b"\x00\x5b", repr((wrong, no_login, old_version)))
notices = "\n".join(tfw.terminal._line_text(i) for i in range(tfw.terminal._total_lines()))
check("SOCKS login: announced as needing a login", f"SOCKS proxy on 127.0.0.1:{locked_port} (login required)" in notices)
check("tunnels: remote forward and proxy are announced", f"Forwarding remote 127.0.0.1:{remote_port}" in notices and f"SOCKS proxy on 127.0.0.1:{socks_port}" in notices)
win.close_tab(tfw)
pump(0.8)
check("tunnels: both ports close with the session", through(socks_port)[2] == b"" and through(remote_port)[2] == b"")

# ---- tunnels on a live connection ------------------------------------------------------
live_session = Session(name="live-tunnels", host="127.0.0.1", port=PORT, username="deploy")
win.store.upsert(live_session)
tlive = win.open_session(live_session, "secret")
pump(15, lambda: tlive.state == "connected")
live_local, live_remote_raw, live_remote_telnet = free_port(), free_port(), free_port()
tunnels_dialog = win.show_tunnels(tlive)
check("live tunnels: the window opens for a connected SSH tab, with nothing running yet", tunnels_dialog is not None and tunnels_dialog.list.count() == 0 and tunnels_dialog.empty.isVisible())
tunnels_dialog.entry.setText(f"{live_local}:127.0.0.1:{RAW_PORT}, R:{live_remote_raw}:127.0.0.1:{RAW_PORT}, R:{live_remote_telnet}:127.0.0.1:{TELNET_PORT}, bogus")
started_live = tunnels_dialog.start_entered()
pump(0.3)
check("live tunnels: started without reconnecting, and a mistake is reported", len(started_live) == 3 and tunnels_dialog.list.count() == 3 and "'bogus' is not a tunnel" in tunnels_dialog.message.text() and tlive.state == "connected", repr((started_live, tunnels_dialog.message.text())))
check("live tunnels: a local one works at once", through(live_local)[1:] == (b"RAW READY\r\n", b"PING"), repr(through(live_local)))
remote_raw = through(live_remote_raw)
with socket.create_connection(("127.0.0.1", live_remote_telnet), timeout=5) as c:
    c.settimeout(5)
    telnet_greeting = c.recv(3)
check("live tunnels: two remote tunnels each reach their own destination", remote_raw[1:] == (b"RAW READY\r\n", b"PING") and telnet_greeting == b"\xff\xfd\x18", repr((remote_raw, telnet_greeting)))
for index in range(tunnels_dialog.list.count()):
    tunnels_dialog.list.item(index).setSelected(tunnels_dialog.list.item(index).text().startswith(f"Forwarding 127.0.0.1:{live_local}"))
check("live tunnels: Stop ends the selected one only", tunnels_dialog.stop_selected() == 1 and tunnels_dialog.list.count() == 2 and through(live_local)[2] == b"" and through(live_remote_raw)[1:] == (b"RAW READY\r\n", b"PING"), repr(tlive.running_tunnels()))
remote_one = next(d for d in tlive.running_tunnels() if f":{live_remote_raw} " in d)
stopped_remote = tlive.stop_tunnel(remote_one)
with socket.create_connection(("127.0.0.1", live_remote_telnet), timeout=5) as c:
    c.settimeout(5)
    telnet_again = c.recv(3)
check("live tunnels: a remote one can be stopped, and the other keeps working", stopped_remote and through(live_remote_raw)[2] == b"" and telnet_again == b"\xff\xfd\x18", repr((tlive.running_tunnels(), telnet_again)))
kept_port = free_port()
tunnels_dialog.keep.setChecked(True)
tunnels_dialog.entry.setText(f"D:{kept_port}")
tunnels_dialog.start_entered()
check("live tunnels: kept in the saved session when asked", win.store.get(live_session.id).tunnels == f"D:{kept_port}" and tlive.session.tunnels == f"D:{kept_port}" and any("SOCKS proxy" in d for d in tlive.running_tunnels()), repr(win.store.get(live_session.id).tunnels))
check("live tunnels: Connection Info lists what is running now", f"SOCKS proxy on 127.0.0.1:{kept_port}" in dict(tlive.connection_details()).get("Tunnels", ""))
tunnels_dialog.close()
win.close_tab(tlive, force=True)
pump(0.8)
check("live tunnels: they all end with the connection", through(kept_port)[2] == b"" and through(live_remote_telnet)[2] == b"")
win.store.remove(win.store.get(live_session.id))
win._closed_tabs.clear()
raw_only = win.open_session(Session(name="raw-only", protocol="raw", host="127.0.0.1", port=RAW_PORT))
pump(10, lambda: raw_only.state == "connected")
check("live tunnels: offered only for a connected SSH tab", win.show_tunnels(raw_only) is None and "connected SSH tab" in win.statusBar().currentMessage())
win.close_tab(raw_only, force=True)
win._closed_tabs.clear()

# ---- serial settings ----------------------------------------------------------------
check("serial: format parsing", parse_serial_format("7e1") == (7, "E", 1) and parse_serial_format("8N1.5") == (8, "N", 1.5) and parse_serial_format("9X9") == (8, "N", 1) and parse_serial_format("") == (8, "N", 1))
tser2 = win.open_session(Session(name="ser7", protocol="serial", host="loop://", port=19200, serial_format="7E1", serial_flow="xonxoff"))
check("serial: opens with 7E1 and XON/XOFF", pump(8, lambda: tser2.state == "connected") and tser2._worker._port.bytesize == 7 and tser2._worker._port.parity == "E" and tser2._worker._port.xonxoff, tser2.state)
win.close_tab(tser2)

# ---- find in scrollback -------------------------------------------------------------
win.tabs.setCurrentIndex(0)
t1.reset()
pump(0.2)
run(tab1, "flood", 1.5)
count = t1.find("line 49")
check("find: counts every match in scrollback", count == 11, str(count))  # line 49, 490-499
first = t1.find_status()
t1.find_step(False)
check("find: stepping moves the active match and scrolls to it", first == (11, 11) and t1.find_status() == (10, 11) and t1.verticalScrollBar().value() <= t1._find_matches[9][0] < t1.verticalScrollBar().value() + t1.rows, f"{first} {t1.find_status()}")
t1.find_step(True)
t1.find_step(True)
check("find: wraps around", t1.find_status() == (1, 11))
check("find: case sensitivity", t1.find("LINE 49", True) == 0 and t1.find("LINE 49", False) == 11)
row, c0, c1 = t1._find_matches[t1._find_index]
pump(0.2)
image = t1.grab().toImage()
from techxterminal.terminal import PADDING as _PAD
dpr = image.devicePixelRatio()  # screenshots are in device pixels
px = image.pixelColor(int((_PAD + (c0 + 0.1) * t1._cw) * dpr), int((_PAD + (row - t1.verticalScrollBar().value() + 0.1) * t1._ch) * dpr))
check("find: the active match is highlighted", abs(px.red() - t1._find_current.red()) < 12 and abs(px.blue() - t1._find_current.blue()) < 12, px.name())
win.find_in_tab()
pump(0.2)
tab1.find_bar.query.setText("line 12")
tab1.find_bar.search()
check("find bar: opens with Ctrl+Shift+F action and shows the count", not tab1.find_bar.isHidden() and tab1.find_bar.count.text().endswith("of 11"), tab1.find_bar.count.text())
win.grab().save(str(SCRATCH / "shot_find.png"))
tab1.find_bar.query.setText("no-such-text-anywhere")
tab1.find_bar.search()
check("find bar: no matches", tab1.find_bar.count.text() == "No matches")
tab1.find_bar.close_bar()
check("find bar: closing clears the highlights", tab1.find_bar.isHidden() and not t1._find_rows)

# ---- links --------------------------------------------------------------------------
run(tab1, "url")
t1._scroll_to_bottom()
pump(0.1)
link_row = next(i for i in range(t1._total_lines()) if "https://example.com" in t1._line_text(i))
line_text = t1._line_text(link_row)
col = line_text.index("https://")
check("links: found under the pointer, trailing punctuation dropped", t1.link_at(link_row, col + 3) == "https://example.com/docs/page?id=7" and t1.link_at(link_row, line_text.index("http://intranet") + 2) == "http://intranet.local/x" and t1.link_at(link_row, 0) is None, repr(t1.link_at(link_row, col + 3)))
opened = []
orig_open = TerminalWidget.open_link
TerminalWidget.open_link = staticmethod(opened.append)
point = cell_point(t1, col + 3, link_row - t1.verticalScrollBar().value())
QTest.mouseClick(t1.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.ControlModifier, point)
QTest.mouseClick(t1.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, point)
TerminalWidget.open_link = orig_open
check("links: Ctrl+click opens, a plain click does not", opened == ["https://example.com/docs/page?id=7"], repr(opened))

# ---- multi-line paste confirmation ----------------------------------------------------
sent = []
t1.send.disconnect(tab1._on_input)
t1.send.connect(sent.append)
answers = []
orig_question = QMessageBox.question
QMessageBox.question = staticmethod(lambda *a, **k: (answers.append(a[1]), reply_with[0])[1])
reply_with = [QMessageBox.StandardButton.No]
CLIP.setText("first\nsecond\n")
t1.paste()
declined = list(sent)
reply_with[0] = QMessageBox.StandardButton.Yes
t1.paste()
CLIP.setText("single line")
t1.paste()
check("paste: several lines ask first; No sends nothing", answers == ["Paste Several Lines", "Paste Several Lines"] and declined == [], repr((answers, declined)))
check("paste: Yes sends it; one line never asks", sent == [b"first\rsecond\r", b"single line"], repr(sent))
QMessageBox.question = orig_question

# ---- mouse reporting ------------------------------------------------------------------
t1.send.disconnect(sent.append)
t1.send.connect(tab1._on_input)
run(tab1, "mouse")
check("mouse: the remote program's request is recognised", bool(t1._mouse_mode()))
t1.send.disconnect(tab1._on_input)
sent.clear()
t1.send.connect(sent.append)
QTest.mouseClick(t1.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, cell_point(t1, 4, 2))
check("mouse: click is reported as press and release at the cell", sent == [b"\x1b[<0;5;3M", b"\x1b[<0;5;3m"], repr(sent))
sent.clear()
QTest.mouseClick(t1.viewport(), Qt.MouseButton.RightButton, Qt.KeyboardModifier.NoModifier, cell_point(t1, 9, 0))
wheel = _QWheelEvent(_QPF(cell_point(t1, 1, 1)), _QPF(t1.viewport().mapToGlobal(cell_point(t1, 1, 1))), _QPoint(0, 0), _QPoint(0, 120), Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier, Qt.ScrollPhase.NoScrollPhase, False)
t1.wheelEvent(wheel)
check("mouse: right button and wheel are reported", sent == [b"\x1b[<2;10;1M", b"\x1b[<2;10;1m", b"\x1b[<64;2;2M"], repr(sent))
sent.clear()
t1._clear_selection()
QTest.mousePress(t1.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.ShiftModifier, cell_point(t1, 0, 1))
QTest.mouseMove(t1.viewport(), cell_point(t1, 6, 1))
QTest.mouseRelease(t1.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.ShiftModifier, cell_point(t1, 6, 1))
check("mouse: Shift keeps local selection", sent == [] and t1.has_selection(), repr(sent))
t1._clear_selection()
t1.send.disconnect(sent.append)
t1.send.connect(tab1._on_input)
run(tab1, "nomouse")
check("mouse: reporting stops when the program turns it off", not t1._mouse_mode())

# ---- double-width characters ----------------------------------------------------------
wide = TerminalWidget(_Prefs())
wide.resize(700, 200)
wide.show()
pump(0.2)
wide.feed("\u4e2d\u6587 ok \u2713 end".encode())
pump(0.2)
cells = wide._line_cells(0)
check("wide characters: occupy two cells", cells[:8] == ["\u4e2d", "", "\u6587", "", " ", "o", "k", " "], repr(cells[:8]))
image = wide.grab().toImage()


def ink(col):
    scale = image.devicePixelRatio()  # screenshots are in device pixels
    x0, x1 = int((_PAD + col * wide._cw) * scale) + 2, int((_PAD + (col + 1) * wide._cw) * scale) - 2
    background = wide._bg.rgb()
    rows = range(int(_PAD * scale), int((_PAD + wide._ch) * scale))
    return sum(image.pixel(x, y) != background for x in range(x0, x1) for y in rows)


check("wide characters: text after them stays on its own cells", ink(4) == 0 and ink(5) > 0 and ink(6) > 0 and ink(7) == 0 and ink(0) > 0 and ink(2) > 0, repr([ink(c) for c in range(9)]))
wide._sel_anchor, wide._sel_end = (0, 0), (0, 6)
check("wide characters: copied text is intact", wide.selected_text() == "\u4e2d\u6587 ok", repr(wide.selected_text()))
check("wide characters: found by search at the right columns", wide.find("ok") == 1 and wide._find_matches[0][1:] == (5, 6), repr(wide._find_matches))
wide.close()

# ---- heavy output keeps the window responsive -------------------------------------------
gaps, last_tick = [], [time.perf_counter()]


def tick():
    now = time.perf_counter()
    gaps.append(now - last_tick[0])
    last_tick[0] = now


heartbeat = QTimer()
heartbeat.timeout.connect(tick)
heartbeat.start(10)
started = time.perf_counter()
tab1._worker.send(b"big\r")
pump(20, lambda: t1.pending_bytes() > 0 or "big output row" in text(tab1))  # the server has started sending
gaps.clear()
last_tick[0] = time.perf_counter()
finished = pump(120, lambda: t1.pending_bytes() == 0 and "BIG DONE" in text(tab1))
heartbeat.stop()
worst = max(gaps) if gaps else 0
check("heavy output: all of it arrives", finished and f"big output row {BIG_OUTPUT_LINES - 1:06d}" in "\n".join(t1._line_text(i) for i in range(t1._total_lines() - 40, t1._total_lines())), f"{time.perf_counter() - started:.1f}s")
check("heavy output: the window keeps processing events throughout", finished and len(gaps) > 10 and worst < 0.35, f"worst gap {worst * 1000:.0f} ms over {len(gaps)} ticks")

# The terminal now keeps up with a local server, so a backlog is made on purpose: 1.7 MB handed over at once.
flooded = TerminalWidget()
flooded.resize(900, 500)
flooded.show()
flood_gaps, flood_last = [], [time.perf_counter()]


def flood_tick():
    now = time.perf_counter()
    flood_gaps.append(now - flood_last[0])
    flood_last[0] = now


flood_beat = QTimer()
flood_beat.timeout.connect(flood_tick)
flood_beat.start(10)
flooded.feed("".join(f"flood row {i:06d} {'x' * 40}\r\n" for i in range(BIG_OUTPUT_LINES)).encode())
flood_backlog = flooded.pending_bytes()
flood_drained = pump(60, lambda: flooded.pending_bytes() == 0)
flood_beat.stop()
check("heavy output: a backlog of 1.7 MB drains in slices, so the window stays responsive", flood_backlog > 1_000_000 and flood_drained and len(flood_gaps) > 5 and max(flood_gaps) < 0.35 and f"flood row {BIG_OUTPUT_LINES - 1:06d}" in flooded.all_text(), f"backlog {flood_backlog} worst gap {max(flood_gaps) * 1000:.0f} ms over {len(flood_gaps)} ticks")
flooded.close()

# ---- session logging --------------------------------------------------------------------
log_file = WORK / "logs" / "manual.log"
check("logging: starts", tab1.start_logging(log_file) and tab1.is_logging())
run(tab1, "echo logged-marker-42")
tab1.stop_logging()
logged = log_file.read_text(encoding="utf-8")
check("logging: output is written as plain text", "ran: echo logged-marker-42" in logged and "deploy@demo-box:~$" in logged and "\x1b" not in logged and "\r" not in logged and "log started" in logged and "log ended" in logged, ascii(logged[-160:]))
win.apply_preferences(_replace(win.prefs, auto_log=True, log_dir=str(WORK / "autologs")))
tlog = win.open_session(win.store.sessions[1], "secret")
pump(15, lambda: tlog.state == "connected")
pump(0.5)
auto_path = tlog.log_path()
check("logging: every session is logged when the setting is on", auto_path is not None and auto_path.parent == WORK / "autologs" and auto_path.name.startswith("web-02-") and "Welcome to demo-box" in auto_path.read_text(encoding="utf-8"), str(auto_path))
win.apply_preferences(_replace(win.prefs, auto_log=False, log_dir=""))

# ---- automatic reconnect ------------------------------------------------------------------
win.apply_preferences(_replace(win.prefs, auto_reconnect=True))
connections_before = len(servers)
tlog._worker.send(b"drop\r")
check("reconnect: a dropped connection is noticed and a retry is scheduled", pump(8, lambda: tlog.state == "disconnected" and "Reconnecting in" in tlog.banner_label.text()), tlog.banner_label.text())
check("reconnect: it reconnects by itself", pump(15, lambda: tlog.state == "connected") and len(servers) == connections_before + 1 and tlog.banner.isHidden(), tlog.state)
tlog._worker.send(b"exit\r")
pump(5, lambda: tlog.state == "disconnected")
pump(0.5)
check("reconnect: a normal logout is left alone", tlog.state == "disconnected" and "Reconnecting" not in tlog.banner_label.text() and not tlog._retry_timer.isActive(), tlog.banner_label.text())
tlog.connect_to_host()
pump(15, lambda: tlog.state == "connected")
tlog._worker.send(b"drop\r")
pump(8, lambda: "Reconnecting in" in tlog.banner_label.text())
tlog.cancel_retry.click()
pump(4.0)
check("reconnect: Cancel stops the retries", tlog.state == "disconnected" and "cancelled" in tlog.banner_label.text() and not tlog._retry_timer.isActive(), tlog.banner_label.text())
win.apply_preferences(_replace(win.prefs, auto_reconnect=False))
tlog.connect_to_host()
pump(15, lambda: tlog.state == "connected")
tlog._worker.send(b"drop\r")
pump(6, lambda: tlog.state == "disconnected")
pump(0.5)
check("reconnect: off by default, a drop just shows the banner", "was lost" in tlog.banner_label.text() and "Reconnecting" not in tlog.banner_label.text(), tlog.banner_label.text())
tlog.connect_to_host()
pump(15, lambda: tlog.state == "connected")

# ---- file transfer ----------------------------------------------------------------------
real_question = QMessageBox.question
QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes)
(SFTP_ROOT / "readme.txt").write_bytes(b"remote file body\n")
(SFTP_ROOT / "projects").mkdir()
(SFTP_ROOT / "projects" / "notes.md").write_text("# notes\n")
check("files: only offered for a connected SSH session", tlog.can_browse_files())
win.tabs.setCurrentIndex(win.tabs.indexOf(tlog))
win.prefs.transfer_parallel = 1  # the checks up to the parallel ones below expect one transfer at a time
win.toggle_files()
panel = tlog._files
check("files: the browser opens as its own window, not inside the tab", panel is not None and panel.isWindow() and panel.isVisible() and tlog.splitter.count() == 1 and "web-02" in panel.windowTitle() and pump(10, lambda: panel.tree.topLevelItemCount() == 2), panel.status.text() if panel else "no browser")

# Large uploads go in parts over several channels at once.
import types as _types
from techxterminal import sftp as _sftp

parallel_source = WORK / "parallel.bin"
parallel_source.write_bytes(os.urandom(20 * 1024 * 1024))
opener = tlog.sftp_opener()
opened_extra = []


def fake_worker(stop_after=None, limit=0, stop_after_checks=None):
    worker = _types.SimpleNamespace(stopping=False, share=lambda: limit, resume=True, keep_dates=False)
    checks = [0]

    def is_paused():
        # Asked before every piece: a stop after so many is the same however fast the computer is.
        checks[0] += 1
        if stop_after_checks is not None and checks[0] >= stop_after_checks:
            worker.stopping = True
        return False

    worker.is_paused = is_paused

    def open_channel():
        opened_extra.append(1)
        return opener()

    def progress(_number, _label, done, _total, _speed):
        if stop_after is not None and done >= stop_after:
            worker.stopping = True

    worker.open_channel = open_channel
    worker.progress = _types.SimpleNamespace(emit=progress)
    worker.resumed = _types.SimpleNamespace(emit=lambda *a: None)
    return worker


_sftp._Transfer(fake_worker(), _types.SimpleNamespace(cancelled=False, number=1), opener()).put(str(parallel_source), "/parallel.bin")
arrived = (SFTP_ROOT / "parallel.bin").read_bytes() == parallel_source.read_bytes()
check("fast uploads: a large file goes in parts over four channels at once, and arrives whole", arrived and len(opened_extra) == _sftp.PARALLEL_STREAMS - 1 and not (SFTP_ROOT / "parallel.bin.part").exists(), repr(len(opened_extra)))
(SFTP_ROOT / "parallel.bin").unlink()
try:
    _sftp._Transfer(fake_worker(stop_after=6 * 1024 * 1024), _types.SimpleNamespace(cancelled=False, number=2), opener()).put(str(parallel_source), "/parallel.bin")
    kept = None
except _sftp.TransferCancelled as stopped:
    kept = stopped.keep_partial
partial = (SFTP_ROOT / "parallel.bin.part").read_bytes() if (SFTP_ROOT / "parallel.bin.part").exists() else b""
check("fast uploads: stopped part way, what is kept is the file's start with no gaps, so it can be continued", kept is True and 0 < len(partial) < 20 * 1024 * 1024 and partial == parallel_source.read_bytes()[: len(partial)], repr((kept, len(partial))))
_sftp._Transfer(fake_worker(), _types.SimpleNamespace(cancelled=False, number=3), opener()).put(str(parallel_source), "/parallel.bin")
check("fast uploads: and it is continued from there to a whole file", (SFTP_ROOT / "parallel.bin").read_bytes() == parallel_source.read_bytes())
(SFTP_ROOT / "parallel.bin").unlink()
opened_extra.clear()
_sftp._Transfer(fake_worker(limit=50 * 1024 * 1024), _types.SimpleNamespace(cancelled=False, number=4), opener()).put(str(parallel_source), "/parallel.bin")
check("fast uploads: with a speed limit a large file keeps to one channel, so the limit holds", not opened_extra and (SFTP_ROOT / "parallel.bin").read_bytes() == parallel_source.read_bytes())
(SFTP_ROOT / "parallel.bin").unlink()
parallel_source.unlink()
remote_big = SFTP_ROOT / "download-big.bin"
remote_big.write_bytes(os.urandom(20 * 1024 * 1024))
landed = WORK / "download-big.bin"
opened_extra.clear()
_sftp._Transfer(fake_worker(), _types.SimpleNamespace(cancelled=False, number=5), opener()).get("/download-big.bin", str(landed))
check("fast downloads: a large file comes in parts over four channels at once, and arrives whole", landed.read_bytes() == remote_big.read_bytes() and len(opened_extra) == _sftp.PARALLEL_STREAMS - 1 and not Path(str(landed) + ".part").exists(), repr(len(opened_extra)))
landed.unlink()
try:
    _sftp._Transfer(fake_worker(stop_after_checks=6), _types.SimpleNamespace(cancelled=False, number=6), opener()).get("/download-big.bin", str(landed))
    kept_down = None
except _sftp.TransferCancelled as stopped:
    kept_down = stopped.keep_partial
partial_down = Path(str(landed) + ".part").read_bytes() if Path(str(landed) + ".part").exists() else b""
check("fast downloads: stopped part way, what is kept is the file's start with no gaps", kept_down is True and 0 < len(partial_down) < 20 * 1024 * 1024 and partial_down == remote_big.read_bytes()[: len(partial_down)], repr((kept_down, len(partial_down))))
_sftp._Transfer(fake_worker(), _types.SimpleNamespace(cancelled=False, number=7), opener()).get("/download-big.bin", str(landed))
check("fast downloads: and it is continued from there to a whole file", landed.read_bytes() == remote_big.read_bytes())
landed.unlink()
opened_extra.clear()
_sftp._Transfer(fake_worker(limit=50 * 1024 * 1024), _types.SimpleNamespace(cancelled=False, number=8), opener()).get("/download-big.bin", str(landed))
check("fast downloads: with a speed limit a large file keeps to one channel", not opened_extra and landed.read_bytes() == remote_big.read_bytes())
landed.unlink()
remote_big.unlink()

check("files: the local side starts in the home folder", os.path.normpath(panel.local_directory) == os.path.normpath(str(HOME)) and panel.local_path.text() == panel.local_directory, panel.local_directory)
names = [panel.tree.topLevelItem(i).text(0) for i in range(panel.tree.topLevelItemCount())]
check("files: folders first, then files, with sizes", names == ["projects", "readme.txt"] and panel.tree.topLevelItem(1).text(1) == "17 B" and panel.directory == "/", repr(names) + panel.directory)
pump(0.3)
local_up = WORK / "upload me.bin"
local_up.write_bytes(os.urandom(300_000))
panel.upload([str(local_up)])
check("files: upload", pump(20, lambda: (SFTP_ROOT / "upload me.bin").exists() and (SFTP_ROOT / "upload me.bin").read_bytes() == local_up.read_bytes() and panel.tree.topLevelItemCount() == 3), panel.status.text())

# ---- the file browser's layout: folder trees, columns, summaries, the transfer queue ----
from techxterminal import sftp as _sftp

pump(2, lambda: panel.done_list.topLevelItemCount() >= 1)
done_row = panel.done_list.topLevelItem(panel.done_list.topLevelItemCount() - 1)
check("queue: a finished upload is listed under Successful transfers, with its direction", done_row.text(0) == str(local_up) and done_row.text(1) == _sftp.UPLOAD_ARROW and done_row.text(2) == "/" and done_row.text(3) == "293.0 KB" and done_row.text(4).startswith("Done at") and panel.queued_list.topLevelItemCount() == 0 and panel.queue_tabs.tabText(2).startswith("Successful transfers ("), repr([done_row.text(c) for c in range(5)]))
headings = [panel.tree.headerItem().text(c) for c in range(panel.tree.columnCount())]
local_headings = [panel.local_model.headerData(c, Qt.Orientation.Horizontal) for c in range(4)]
check("layout: both sides have Name, Size, Type and Modified; the remote side also Permissions and Owner", headings == ["Name", "Size", "Type", "Modified", "Permissions", "Owner"] and local_headings == ["Name", "Size", "Type", "Modified"] and not panel.local_view.isColumnHidden(2), repr((headings, local_headings)))
readme_row = next(panel.tree.topLevelItem(i) for i in range(panel.tree.topLevelItemCount()) if panel.tree.topLevelItem(i).text(0) == "readme.txt")
projects_row = next(panel.tree.topLevelItem(i) for i in range(panel.tree.topLevelItemCount()) if panel.tree.topLevelItem(i).text(0) == "projects")
check("layout: remote rows say what each entry is and who owns it", readme_row.text(2) == "TXT file" and projects_row.text(2) == "Folder" and readme_row.text(5) != "", repr([readme_row.text(c) for c in range(6)]))
check("layout: the remote side has a folder tree that follows the folder shown", "/" in panel._remote_nodes and "/projects" in panel._remote_nodes and panel.remote_folders.currentItem() is panel._remote_nodes["/"], repr(sorted(panel._remote_nodes)))
local_tree_path = os.path.normpath(panel.local_folders_model.filePath(panel.local_folders.currentIndex()))
check("layout: the local side has a folder tree that follows the folder shown", local_tree_path == os.path.normpath(panel.local_directory), local_tree_path)
check("layout: each side sums up what it shows", panel.remote_summary.text() == "2 files and 1 folder. Total size: 300,017 bytes" and "Total size" in panel.local_summary.text(), repr((panel.remote_summary.text(), panel.local_summary.text())))
panel.tree.clearSelection()
readme_row.setSelected(True)
check("layout: and what is selected, when something is", panel.remote_summary.text() == "Selected 1 file. Total size: 17 bytes", panel.remote_summary.text())
panel.tree.clearSelection()
check("layout: summary lines read naturally", _sftp.summary_text(0, 0, 0) == "Empty folder." and _sftp.summary_text(1, 0, 5) == "1 file. Total size: 5 bytes" and _sftp.summary_text(0, 2, 0) == "2 folders." and _sftp.file_type("archive.tar.gz", False) == "GZ file" and _sftp.file_type(".bashrc", False) == "File")

panel._remote_node("/projects").setExpanded(True)
pump(5, lambda: panel._remote_node("/projects").data(0, _sftp.ROLE_FILLED))
check("layout: opening a folder in the tree lists its folders without leaving the folder shown", panel._remote_node("/projects").data(0, _sftp.ROLE_FILLED) and panel.directory == "/")
panel.remote_folders.itemClicked.emit(panel._remote_node("/projects"), 0)
check("layout: clicking a folder in the tree shows it", pump(10, lambda: panel.directory == "/projects"), panel.directory)
panel.go("/")
pump(10, lambda: panel.directory == "/")

panel._new_folder(enter=True, name="fresh")
check("menu: Create Folder and Enter It makes the folder and opens it", pump(10, lambda: panel.directory == "/fresh") and (SFTP_ROOT / "fresh").is_dir(), panel.directory)
panel.worker.delete("/fresh", True)
pump(10, lambda: panel.directory == "/" and not (SFTP_ROOT / "fresh").exists())
local_start = panel.local_directory
panel.new_local_folder("made-here", enter=True)
entered = os.path.normpath(panel.local_directory) == os.path.normpath(os.path.join(local_start, "made-here"))
panel.go_local(local_start)
os.rmdir(os.path.join(local_start, "made-here"))
check("menu: the same on this computer", entered)

slow = WORK / "slow.bin"
slow.write_bytes(os.urandom(1_500_000))
second = WORK / "second.bin"
second.write_bytes(b"never sent")
panel.set_speed_limit(128)
panel.upload([str(slow)])
panel.upload([str(second)])
pump(10, lambda: panel.cancel_button.isVisible())
waiting = panel.queued_list.topLevelItemCount()
second_job = panel.queued_jobs()[-1]
removed = panel.remove_from_queue([second_job])
check("queue: waiting transfers are listed, and one can be taken out before it starts", waiting == 2 and removed == 1 and panel.queued_list.topLevelItemCount() == 1 and panel.queue_tabs.tabText(0) == "Queued files (1)", repr((waiting, removed)))
check("queue: the running one shows its progress and cannot be taken out", pump(5, lambda: "%" in panel.queued_list.topLevelItem(0).text(4)) and panel.remove_from_queue(panel.queued_jobs()) == 0, panel.queued_list.topLevelItem(0).text(4))
panel.cancel_button.click()
pump(10, lambda: panel.failed_list.topLevelItemCount() == 1)
failed_row = panel.failed_list.topLevelItem(0) if panel.failed_list.topLevelItemCount() else None
check("queue: a cancelled transfer is listed under Failed transfers, with the reason", failed_row is not None and failed_row.text(4) == "Cancelled" and panel.queued_list.topLevelItemCount() == 0 and panel.queue_tabs.tabText(1) == "Failed transfers (1)", repr(failed_row.text(4) if failed_row else None))
pump(1.5)
check("queue: the transfer taken out never ran", not (SFTP_ROOT / "second.bin").exists())
panel.set_speed_limit(0)
check("queue: Try Again queues a failed transfer again, and it completes", panel.retry_failed() == 1 and panel.failed_list.topLevelItemCount() == 0 and pump(20, lambda: (SFTP_ROOT / "slow.bin").exists() and (SFTP_ROOT / "slow.bin").stat().st_size == 1_500_000), panel.status.text())
panel._clear_list(panel.done_list)
check("queue: a list can be cleared", panel.done_list.topLevelItemCount() == 0 and panel.queue_tabs.tabText(2) == "Successful transfers")
_empty_shot = panel.failed_list.viewport().grab().toImage()
_empty_colours = {_empty_shot.pixel(x, y) for x in range(0, _empty_shot.width(), 2) for y in range(0, _empty_shot.height(), 2)}
check("queue: an empty list says what would be in it", len(_empty_colours) > 2 and all(lst.empty_text for lst in (panel.queued_list, panel.failed_list, panel.done_list)), str(len(_empty_colours)))
panel.worker.delete("/slow.bin", False)
pump(10, lambda: not (SFTP_ROOT / "slow.bin").exists() and panel.tree.topLevelItemCount() == 3)
local_dir = WORK / "tree"
(local_dir / "sub").mkdir(parents=True)
(local_dir / "a.txt").write_text("a")
(local_dir / "sub" / "b.txt").write_text("b")
panel.upload([str(local_dir)])
check("files: a dropped folder is uploaded with its contents", pump(20, lambda: (SFTP_ROOT / "tree" / "sub" / "b.txt").exists() and (SFTP_ROOT / "tree" / "a.txt").read_text() == "a"), panel.status.text())
local_down = WORK / "downloaded.txt"
panel.worker.download("/readme.txt", str(local_down))
check("files: download", pump(15, lambda: local_down.exists() and local_down.read_bytes() == b"remote file body\n" and "Downloaded" in panel.status.text()), panel.status.text())
folder_target = WORK / "pulled" / "tree"
panel.worker.download("/tree", str(folder_target))
check("files: a whole folder downloads with its subfolders", pump(20, lambda: (folder_target / "sub" / "b.txt").exists() and (folder_target / "a.txt").read_text() == "a" and "2 files" in panel.status.text()), panel.status.text())
panel.go("/projects")
check("files: open a folder and go back up", pump(10, lambda: panel.directory == "/projects" and panel.tree.topLevelItemCount() == 1) and (panel.go_up() or pump(10, lambda: panel.directory == "/")), panel.directory)
panel.worker.make_directory("/newdir")
check("files: new folder", pump(10, lambda: (SFTP_ROOT / "newdir").is_dir()))
panel.worker.rename("/newdir", "/renamed dir")
check("files: rename", pump(10, lambda: (SFTP_ROOT / "renamed dir").is_dir() and not (SFTP_ROOT / "newdir").exists() and "Renamed to renamed dir" in panel.status.text() and any(panel.tree.topLevelItem(i).text(0) == "renamed dir" for i in range(panel.tree.topLevelItemCount()))), panel.status.text())
panel.worker.rename("/renamed dir", "/newdir")
pump(10, lambda: (SFTP_ROOT / "newdir").is_dir())
panel.worker.delete("/newdir", True)
panel.worker.delete("/upload me.bin", False)
check("files: delete", pump(10, lambda: not (SFTP_ROOT / "newdir").exists() and not (SFTP_ROOT / "upload me.bin").exists()))
(SFTP_ROOT / "deep" / "a" / "b").mkdir(parents=True)
(SFTP_ROOT / "deep" / "top.txt").write_text("1")
(SFTP_ROOT / "deep" / "a" / "mid.txt").write_text("2")
(SFTP_ROOT / "deep" / "a" / "b" / "low.txt").write_text("3")
panel.worker.delete("/deep", True)
check("files: a folder with files and folders in it is deleted with all of them", pump(10, lambda: not (SFTP_ROOT / "deep").exists()), panel.status.text())
# -- folder size, properties and the terminal
import shutil as _shutil

(SFTP_ROOT / "sz" / "inner").mkdir(parents=True)
(SFTP_ROOT / "sz" / "a.txt").write_bytes(b"x" * 10)
(SFTP_ROOT / "sz" / "inner" / "b.txt").write_bytes(b"y" * 5)
measured_got = []
panel.worker.measured.connect(lambda *args: measured_got.append(args))
panel.worker.measure("/sz")
check("size: a remote folder's total counts what is in its subfolders", pump(10, lambda: bool(measured_got)) and measured_got[0] == ("/sz", 15, 2, 1, True), repr(measured_got))
panel.go("/")
pump(10, lambda: any(panel.tree.topLevelItem(i).text(0) == "sz" for i in range(panel.tree.topLevelItemCount())))
sz_item = next((panel.tree.topLevelItem(i) for i in range(panel.tree.topLevelItemCount()) if panel.tree.topLevelItem(i).text(0) == "sz"), None)
props = panel.show_remote_properties(sz_item) if sz_item else None
check("properties: a remote folder shows its details and then its total size", props is not None and pump(10, lambda: props._size is not None and "15 B" in props._size.text() or "15" in props._size.text()), props._size.text() if props else "")
if props:
    props.close()
asked = []
panel.terminalRequested.connect(asked.append)
panel.open_terminal_here(sz_item)
panel.open_terminal_here(None)
check("terminal: Open Terminal Here asks for the selected folder, or the one showing", asked == ["/sz", "/"], repr(asked))
from techxterminal.sftp import PropertiesDialog as _PD

(WORK / "sizeside" / "d").mkdir(parents=True)
(WORK / "sizeside" / "one.bin").write_bytes(b"z" * 7)
(WORK / "sizeside" / "d" / "two.bin").write_bytes(b"z" * 3)
local_dialog = _PD("sizeside", [("Name", "sizeside")], measuring=True)
local_dialog.measure_local(str(WORK / "sizeside"))
check("properties: a local folder's total size is added up", pump(10, lambda: "10 B" in local_dialog._size.text() and "2 files and 1 folder" in local_dialog._size.text()), local_dialog._size.text())
local_dialog.close()
_shutil.rmtree(SFTP_ROOT / "sz", ignore_errors=True)
panel.set_folders_shown(True)
shown_trees = (panel.local_folders.isVisibleTo(panel), panel.remote_folders.isVisibleTo(panel))
panel.set_folders_shown(False)
check("browser: the folder trees start hidden, and the toggle shows and hides both", shown_trees == (True, True) and not panel.local_folders.isVisibleTo(panel) and not panel.remote_folders.isVisibleTo(panel), repr(shown_trees))
queue_before = panel._queue_card.isVisibleTo(panel)
panel.queue_button.setChecked(True)
queue_asked = panel._queue_card.isVisibleTo(panel)
panel.queue_button.setChecked(False)
check("browser: the queue shows only when asked for or while it holds transfers", queue_asked and (queue_before == any(l.topLevelItemCount() for l in (panel.queued_list, panel.failed_list, panel.done_list))), repr((queue_before, queue_asked)))
panel.go("/")
# -- the two sides: select on one, transfer to the folder showing on the other
from PyQt6.QtCore import QItemSelectionModel as _QISM

local_side = WORK / "localside"
(local_side / "inner").mkdir(parents=True)
(local_side / "report.txt").write_bytes(b"local report\n")
(local_side / "inner" / "deep.txt").write_bytes(b"deep\n")
panel.go_local(str(local_side))
check("browser: the local side shows the chosen folder", os.path.normpath(panel.local_directory) == os.path.normpath(str(local_side)) and panel.local_path.text() == panel.local_directory)
panel.go_local(str(local_side / "missing"))
check("browser: a folder that does not exist is refused", os.path.normpath(panel.local_directory) == os.path.normpath(str(local_side)) and "No such folder" in panel.status.text(), panel.status.text())
panel.go_local_up()
check("browser: parent folder on the local side", os.path.normpath(panel.local_directory) == os.path.normpath(str(WORK)), panel.local_directory)
panel.go_local(str(local_side))
pump(0.5)
_shown_folder = panel.local_folders.visualRect(panel.local_folders.currentIndex())
check("browser: the local folder tree shows the current folder, after its neighbours have arrived", panel.local_folders.viewport().rect().contains(_shown_folder), repr((_shown_folder, panel.local_folders.viewport().rect())))
_folder_bar = panel.local_folders.verticalScrollBar()
_folder_bar.triggerAction(_folder_bar.SliderAction.SliderToMinimum)
(WORK / "arrived-later").mkdir()
pump(1.5)
check("browser: once the user scrolls the folder tree, it stays where they put it", _folder_bar.value() == 0, repr(_folder_bar.value()))
panel.go_local(str(local_side))
pump(0.5)
_sizes_heading = (panel.local_model.headerData(1, Qt.Orientation.Horizontal, Qt.ItemDataRole.TextAlignmentRole), panel.tree.headerItem().textAlignment(1))
check("browser: the Size headings are right-aligned, over the sizes", all(int(getattr(value, "value", value)) & Qt.AlignmentFlag.AlignRight.value for value in _sizes_heading), repr(_sizes_heading))


def select_local(*names):
    selection = panel.local_view.selectionModel()
    selection.clearSelection()
    for name in names:
        selection.select(panel.local_model.index(str(local_side / name)), _QISM.SelectionFlag.Select | _QISM.SelectionFlag.Rows)


def select_remote(*names):
    panel.tree.clearSelection()
    for i in range(panel.tree.topLevelItemCount()):
        item = panel.tree.topLevelItem(i)
        item.setSelected(item.text(0) in names)


select_local("report.txt", "inner")
check("browser: local selection is read back", sorted(os.path.basename(p) for p in panel.selected_local_paths()) == ["inner", "report.txt"], repr(panel.selected_local_paths()))
panel.upload_button.click()
check("browser: Upload sends the selected files and folders to the remote folder", pump(20, lambda: (SFTP_ROOT / "report.txt").exists() and (SFTP_ROOT / "report.txt").read_bytes() == b"local report\n" and (SFTP_ROOT / "inner" / "deep.txt").exists() and "Uploaded 1 file" in panel.status.text()), panel.status.text())  # one transfer for each selected item, so each says "1 file"
select_local()
panel.upload_button.click()
check("browser: Upload with nothing selected explains itself", "Select files on this computer" in panel.status.text(), panel.status.text())

pump(10, lambda: any(panel.tree.topLevelItem(i).text(0) == "readme.txt" for i in range(panel.tree.topLevelItemCount())))
select_remote("readme.txt", "projects")
panel.download_button.click()
check("browser: Download brings the selected files and folders into the local folder", pump(20, lambda: (local_side / "readme.txt").exists() and (local_side / "readme.txt").read_bytes() == b"remote file body\n" and (local_side / "projects" / "notes.md").exists()), panel.status.text())
pump(0.5)
(local_side / "readme.txt").write_bytes(b"edited locally\n")
asked = []
orig_question = QMessageBox.question
QMessageBox.question = staticmethod(lambda *a, **k: (asked.append(a[1]), QMessageBox.StandardButton.No)[1])
select_remote("readme.txt")
panel.download_button.click()
pump(1.0)
check("browser: replacing an existing local file asks first, and No keeps it", asked == ["Replace Files"] and (local_side / "readme.txt").read_bytes() == b"edited locally\n", repr(asked))
QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes)
panel.download_button.click()
check("browser: Yes replaces it", pump(15, lambda: (local_side / "readme.txt").read_bytes() == b"remote file body\n"))
QMessageBox.question = orig_question
pump(0.5)
panel.grab().save(str(SCRATCH / "shot_files.png"))

# -- speed limit
big = local_side / "big.bin"
big.write_bytes(os.urandom(300_000))
select_local("big.bin")
started = time.time()
panel.upload_button.click()
pump(20, lambda: (SFTP_ROOT / "big.bin").exists() and (SFTP_ROOT / "big.bin").stat().st_size == 300_000 and "Uploaded" in panel.status.text())
unlimited_seconds = time.time() - started
panel.set_speed_limit(100)
check("speed limit: the choice is shown, passed on and remembered", panel.speed.currentText() == "100 KB/s" and win.prefs.transfer_limit_kbps == 100 and Preferences.load().transfer_limit_kbps == 100, panel.speed.currentText())
(SFTP_ROOT / "big.bin").unlink()
seen_rates = []
panel.worker.progress.connect(lambda job, label, done, total, rate: seen_rates.append(rate))
started = time.time()
panel.upload_button.click()
saw_cancel_button = pump(5, lambda: panel.cancel_button.isVisible() and panel.progress.isVisible() and "/s" in panel.transfer_summary())
finished = pump(30, lambda: "Uploaded" in panel.status.text())
limited_seconds = time.time() - started
pump(5, lambda: not panel.cancel_button.isVisible())  # hidden by the signal that follows the message
check("speed limit: a 300 KB upload at 100 KB/s takes about three seconds", finished and 2.2 < limited_seconds < 9 and limited_seconds > unlimited_seconds + 1.5 and (SFTP_ROOT / "big.bin").read_bytes() == big.read_bytes(), f"limited {limited_seconds:.1f}s, unlimited {unlimited_seconds:.1f}s")
check("speed limit: progress shows the speed and offers Cancel while it runs", saw_cancel_button and not panel.cancel_button.isVisible() and seen_rates and max(seen_rates[3:] or seen_rates) < 190_000, f"{[int(r / 1024) for r in seen_rates[-4:]]} KB/s")
big.unlink()
select_remote("big.bin")
started = time.time()
panel.download_button.click()
finished = pump(30, lambda: big.exists() and big.stat().st_size == 300_000 and "Downloaded" in panel.status.text())
check("speed limit: downloads are limited too", finished and time.time() - started > 2.2 and big.read_bytes() == (SFTP_ROOT / "big.bin").read_bytes(), f"{time.time() - started:.1f}s")
panel.set_speed_limit(333)
check("speed limit: a custom value joins the list", panel.speed.currentText() == "333 KB/s" and panel.speed.findData(333) >= 0 and panel.speed.itemText(panel.speed.count() - 1) == "Custom...")

# -- cancel
panel.set_speed_limit(40)
big.unlink()
select_remote("big.bin")
panel.download_button.click()
pump(5, lambda: panel.cancel_button.isVisible() and Path(str(big) + ".part").exists())
panel.cancel_button.click()
check("cancel: stops a download and leaves no partial file", pump(5, lambda: "cancelled" in panel.status.text().lower()) and not big.exists() and not Path(str(big) + ".part").exists() and not panel.cancel_button.isVisible(), panel.status.text())
big.write_bytes(os.urandom(300_000))
(SFTP_ROOT / "big.bin").unlink()
select_local("big.bin")
panel.upload_button.click()
pump(5, lambda: panel.cancel_button.isVisible())
pump(0.6)
panel.cancel_button.click()
check("cancel: stops an upload", pump(5, lambda: "cancelled" in panel.status.text().lower()) and (not (SFTP_ROOT / "big.bin").exists() or (SFTP_ROOT / "big.bin").stat().st_size < 300_000), panel.status.text())
panel.set_speed_limit(0)
check("speed limit: back to unlimited", panel.speed.currentText() == "Unlimited" and win.prefs.transfer_limit_kbps == 0)
select_local("report.txt")
panel.upload_button.click()
check("browser: transfers work again after a cancel", pump(15, lambda: "Uploaded 1 file" in panel.status.text()), panel.status.text())
for leftover in ("big.bin", "report.txt"):
    if (SFTP_ROOT / leftover).exists():
        (SFTP_ROOT / leftover).unlink()

# -- pause and resume
pause_file = local_side / "pause.bin"
pause_file.write_bytes(os.urandom(400_000))
remote_pause = SFTP_ROOT / "pause.bin"
remote_part = SFTP_ROOT / "pause.bin.part"
panel.set_speed_limit(100)
select_local("pause.bin")
clicked_at = time.perf_counter()
panel.upload_button.click()
pump(10, lambda: panel.pause_button.isVisible())
pause_after = time.perf_counter() - clicked_at  # how long the window takes to show that something is running
pump(10, lambda: remote_part.exists() and remote_part.stat().st_size > 30_000)
check("partial files: Pause and Cancel appear within a second or two of starting an upload", pause_after < 2.0, f"{pause_after:.2f} s")
check("partial files: an upload is written as name.part until it is complete", remote_part.exists() and not remote_pause.exists() and panel.pause_button.isVisible() and panel.pause_button.text() == "Pause", repr((remote_part.exists(), remote_pause.exists(), panel.pause_button.isVisible(), panel.pause_button.text(), panel.worker.running_count(), panel._started, panel.status.text())))
panel.pause_button.click()
pump(0.6)
held_at = remote_part.stat().st_size
pump(1.5)
check("pause: the transfer stands still, and says so", remote_part.stat().st_size == held_at and panel.pause_button.text() == "Resume" and panel.status.text().startswith("Paused") and "pause.bin" in panel.status.text() and panel.worker.is_paused() and panel.cancel_button.isVisible(), f"{held_at} -> {remote_part.stat().st_size} {panel.status.text()}")
panel.pause_button.click()
check("resume: it carries on from the same place and completes", pump(20, lambda: "Uploaded" in panel.status.text() and remote_pause.exists() and not panel.pause_button.isVisible()) and remote_pause.read_bytes() == pause_file.read_bytes() and not remote_part.exists() and panel.pause_button.text() == "Pause" and not panel.pause_button.isVisible(), panel.status.text())
recent_log = [panel.log.item(i).text()[11:] for i in range(panel.log.count())][-4:]
check("pause: both steps are in the log", "Paused" in recent_log and "Resumed" in recent_log, repr(recent_log))

pause_file.unlink()
local_part = local_side / "pause.bin.part"
pump(10, lambda: any(panel.tree.topLevelItem(i).text(0) == "pause.bin" for i in range(panel.tree.topLevelItemCount())))
select_remote("pause.bin")
panel.download_button.click()
pump(10, lambda: local_part.exists() and local_part.stat().st_size > 30_000)
panel.pause_button.click()
pump(0.6)
held_at = local_part.stat().st_size
pump(1.2)
stood_still = local_part.stat().st_size == held_at and not pause_file.exists()
panel.pause_button.click()
check("pause and resume work for downloads too", stood_still and pump(20, lambda: pause_file.exists() and "Downloaded" in panel.status.text()) and pause_file.read_bytes() == remote_pause.read_bytes() and not local_part.exists(), f"{stood_still} {panel.status.text()}")

remote_pause.unlink()
select_local("pause.bin")
QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes)
panel.upload_button.click()
pump(10, lambda: remote_part.exists() and remote_part.stat().st_size > 30_000)
panel.pause_button.click()
pump(0.4)
panel.cancel_button.click()
check("pause then cancel: the transfer is dropped, its partial file removed, and the queue is not left held", pump(8, lambda: "cancelled" in panel.status.text().lower()) and not remote_part.exists() and not remote_pause.exists() and not panel.worker.is_paused() and panel.pause_button.text() == "Pause", panel.status.text())

# -- an interrupted transfer continues where it stopped
panel.set_speed_limit(60)
panel.refresh()
pump(1.0)
select_local("pause.bin")
panel.upload_button.click()
pump(10, lambda: remote_part.exists() and remote_part.stat().st_size > 60_000)
win.toggle_files()  # the window is closed in the middle of the upload
pump(1.0)
left_behind = remote_part.stat().st_size if remote_part.exists() else 0
check("interrupted: closing the window part-way leaves the partial file for later", 60_000 < left_behind < 400_000 and not remote_pause.exists() and tlog._files is None, str(left_behind))
win.toggle_files()
panel = tlog._files  # a new window; the checks below carry on with it
pump(10, lambda: panel.directory == "/")
panel.set_speed_limit(0)
continued = []
first_progress = []
panel.worker.resumed.connect(lambda name, percent: continued.append((name, percent)))
panel.worker.progress.connect(lambda job, name, done, total, rate: first_progress.append(done))
pump(0.5)
select_local("pause.bin")
panel.upload_button.click()
check("interrupted: transferring the file again continues from the partial file", pump(20, lambda: "Uploaded" in panel.status.text() and remote_pause.exists()) and remote_pause.read_bytes() == pause_file.read_bytes() and not remote_part.exists() and len(continued) == 1 and continued[0][0] == "pause.bin" and 10 < continued[0][1] < 100 and min(first_progress) >= left_behind, f"{continued} first={min(first_progress or [0])} left={left_behind}")
check("interrupted: the log says where it picked up", any(panel.log.item(i).text().endswith(f"Continuing pause.bin from {continued[0][1]}%") for i in range(panel.log.count())) if continued else False)

continued.clear()
remote_pause.unlink()
remote_part.write_bytes(os.urandom(120_000))  # a partial file that is not the beginning of this file
panel.refresh()
pump(1.0)
select_local("pause.bin")
panel.upload_button.click()
check("interrupted: a partial file that does not match is not trusted; the transfer starts over", pump(20, lambda: "Uploaded" in panel.status.text() and remote_pause.exists()) and remote_pause.read_bytes() == pause_file.read_bytes() and continued == [] and not remote_part.exists(), repr(continued))

original = pause_file.read_bytes()
pause_file.unlink()
local_part.write_bytes(original[:150_000])  # as left by a download that was cut off
pump(10, lambda: any(panel.tree.topLevelItem(i).text(0) == "pause.bin" for i in range(panel.tree.topLevelItemCount())))
select_remote("pause.bin")
panel.download_button.click()
check("interrupted: a download continues from its partial file", pump(20, lambda: pause_file.exists() and "Downloaded" in panel.status.text()) and pause_file.read_bytes() == original and continued == [("pause.bin", 37)] and not local_part.exists(), repr(continued) + " " + panel.status.text())
continued.clear()
pause_file.unlink()
local_part.write_bytes(b"x" * 150_000)
panel.download_button.click()
check("interrupted: and starts over when its partial file does not match", pump(20, lambda: pause_file.exists() and "Downloaded" in panel.status.text()) and pause_file.read_bytes() == original and continued == [], repr(continued) + " " + panel.status.text())
(SFTP_ROOT / "empty.txt").write_bytes(b"")
panel.refresh()
pump(10, lambda: any(panel.tree.topLevelItem(i).text(0) == "empty.txt" for i in range(panel.tree.topLevelItemCount())))
select_remote("empty.txt")
panel.download_button.click()
check("partial files: an empty file transfers too", pump(10, lambda: (local_side / "empty.txt").exists()) and (local_side / "empty.txt").stat().st_size == 0 and not (local_side / "empty.txt.part").exists())
for leftover in (remote_pause, SFTP_ROOT / "empty.txt", pause_file, local_side / "empty.txt"):
    leftover.unlink()
panel.set_speed_limit(0)

# -- permissions, filters, bookmarks, file times


def names_now():
    return [panel.tree.topLevelItem(i).text(0) for i in range(panel.tree.topLevelItemCount())]


def remote_item(name):
    return next(panel.tree.topLevelItem(i) for i in range(panel.tree.topLevelItemCount()) if panel.tree.topLevelItem(i).text(0) == name)


panel.refresh()
pump(10, lambda: "readme.txt" in names_now())
check("permissions: shown for files and folders", remote_item("readme.txt").text(4).startswith("-r") and len(remote_item("readme.txt").text(4)) == 10 and remote_item("projects").text(4).startswith("d") and panel.tree.headerItem().text(4) == "Permissions", remote_item("readme.txt").text(4))
changed = panel.change_permissions(remote_item("readme.txt"), "444")
check("permissions: changing them reaches the server and the list", changed and pump(10, lambda: not os.access(SFTP_ROOT / "readme.txt", os.W_OK) and "Permissions 444 set on readme.txt" in panel.status.text() and remote_item("readme.txt").text(4) == "-r--r--r--"), panel.status.text())
panel.change_permissions(remote_item("readme.txt"), "644")
pump(10, lambda: os.access(SFTP_ROOT / "readme.txt", os.W_OK))
check("permissions: and back; nonsense is refused", os.access(SFTP_ROOT / "readme.txt", os.W_OK) and not panel.change_permissions(remote_item("readme.txt"), "9z") and "three or four digits" in panel.status.text(), panel.status.text())

everything = names_now()
panel.remote_filter.setText("READ")
narrowed = names_now()
filtered_status = panel.status.text()
panel.remote_filter.clear()
check("filter: the remote list narrows as you type, in any case, and comes back", narrowed == ["readme.txt"] and f"({len(everything) - 1} filtered out)" in filtered_status and names_now() == everything, repr((narrowed, filtered_status)))
panel.local_filter.setText("report")
local_filters = list(panel.local_model.nameFilters())
panel.local_filter.clear()
check("filter: the local list too", local_filters == ["*report*"] and list(panel.local_model.nameFilters()) == [])

panel.go("/projects")
pump(10, lambda: panel.directory == "/projects")
check("bookmarks: none to begin with", panel.bookmarks(True) == [] and panel.bookmarks(False) == [])
added_remote, added_local = panel.toggle_bookmark(True), panel.toggle_bookmark(False)
check("bookmarks: the folders showing can be bookmarked on each side", added_remote and added_local and panel.bookmarks(True) == ["/projects"] and [os.path.normpath(b) for b in panel.bookmarks(False)] == [os.path.normpath(str(local_side))])
check("bookmarks: toggling again removes one", panel.toggle_bookmark(False) is False and panel.bookmarks(False) == [] and panel.bookmarks(True) == ["/projects"])
panel.go("/")
pump(10, lambda: panel.directory == "/")

long_ago = 1_500_000_000  # July 2017
dated = local_side / "dated.txt"
dated.write_bytes(b"kept its date\n")
os.utime(dated, (long_ago, long_ago))
select_local("dated.txt")
panel.upload_button.click()
pump(15, lambda: (SFTP_ROOT / "dated.txt").exists() and "Uploaded" in panel.status.text())
pump(0.5)
uploaded_time = (SFTP_ROOT / "dated.txt").stat().st_mtime
dated.unlink()
pump(10, lambda: "dated.txt" in names_now())
select_remote("dated.txt")
panel.download_button.click()
pump(15, lambda: dated.exists() and "Downloaded" in panel.status.text())
check("file times: an uploaded and a downloaded file keep their modification time", abs(uploaded_time - long_ago) < 3 and abs(dated.stat().st_mtime - long_ago) < 3 and remote_item("dated.txt").text(3).startswith("2017-07"), f"{uploaded_time} {dated.stat().st_mtime}")
(SFTP_ROOT / "dated.txt").unlink()
dated.unlink()

# -- sorting, hidden files
from techxterminal.sftp import clock as _clock, rate_text as _rate_text
from PyQt6.QtCore import QMimeData as _QMimeData, QUrl as _QUrl
from PyQt6.QtGui import QDropEvent as _QDropEvent


def remote_names():
    return [panel.tree.topLevelItem(i).text(0) for i in range(panel.tree.topLevelItemCount())]


(SFTP_ROOT / "s1.bin").write_bytes(b"1")
(SFTP_ROOT / ".profile").write_bytes(b"hidden")
panel.refresh()
pump(10, lambda: "s1.bin" in remote_names())
check("browser: folders first, then files by name", remote_names() == ["inner", "projects", "tree", "readme.txt", "s1.bin"], repr(remote_names()))
panel.tree.sortByColumn(0, Qt.SortOrder.DescendingOrder)
check("browser: reversing the sort keeps folders on top", remote_names() == ["tree", "projects", "inner", "s1.bin", "readme.txt"], repr(remote_names()))
panel.tree.sortByColumn(1, Qt.SortOrder.AscendingOrder)
check("browser: sorting by size", remote_names()[-2:] == ["s1.bin", "readme.txt"], repr(remote_names()))
panel.tree.sortByColumn(0, Qt.SortOrder.AscendingOrder)
check("browser: dot-files are hidden by default and counted", ".profile" not in remote_names() and "(1 hidden)" in panel.status.text(), panel.status.text())
panel.hidden_box.setChecked(True)
check("browser: Hidden files shows them, and the choice is saved", remote_names()[-1] == "s1.bin" and ".profile" in remote_names() and str(panel._settings.value("file_browser_hidden")) == "true", repr(remote_names()))
panel.hidden_box.setChecked(False)

# -- replacing remote files asks first
(local_side / "report.txt").write_bytes(b"first version\n")
select_local("report.txt")
panel.upload_button.click()
pump(15, lambda: (SFTP_ROOT / "report.txt").exists() and "report.txt" in remote_names())
(local_side / "report.txt").write_bytes(b"second version\n")
asked = []
QMessageBox.question = staticmethod(lambda *a, **k: (asked.append(a[1]), QMessageBox.StandardButton.No)[1])
panel.upload_button.click()
pump(1.0)
check("browser: uploading over an existing remote file asks first, and No keeps it", asked == ["Replace Files"] and (SFTP_ROOT / "report.txt").read_bytes() == b"first version\n", repr(asked))
QMessageBox.question = staticmethod(lambda *a, **k: (asked.append(a[1]), QMessageBox.StandardButton.Yes)[1])
RENAMES.update(posix=0, plain=0)
panel.upload_button.click()
check("browser: Yes replaces it", pump(15, lambda: file_bytes(SFTP_ROOT / "report.txt") == b"second version\n"))
pump(0.3)
check("browser: the replacement is one step where the server offers it", RENAMES["posix"] == 1 and RENAMES["plain"] == 0, repr(RENAMES))
RENAMES.update(one_step=False, posix=0, plain=0)
(local_side / "report.txt").write_bytes(b"third version\n")
panel.upload_button.click()
replaced_anyway = pump(15, lambda: file_bytes(SFTP_ROOT / "report.txt") == b"third version\n")
pump(0.3)
check("browser: and still works on a server that does not", replaced_anyway and RENAMES["plain"] == 1 and not (SFTP_ROOT / "report.txt.part").exists(), repr(RENAMES))
RENAMES.update(one_step=True)

# -- deleting several remote items with one confirmation
asked.clear()
pump(10, lambda: "report.txt" in remote_names())
select_remote("s1.bin", "report.txt")
panel.delete_selected()
check("browser: several remote items are deleted after one question", pump(10, lambda: not (SFTP_ROOT / "s1.bin").exists() and not (SFTP_ROOT / "report.txt").exists()) and asked == ["Delete"], repr(asked))
(SFTP_ROOT / ".profile").unlink()

# -- local file operations
panel.new_local_folder("made here")
check("browser: new local folder", (local_side / "made here").is_dir())
panel.rename_local(str(local_side / "made here"), "renamed here")
check("browser: rename a local item", (local_side / "renamed here").is_dir() and not (local_side / "made here").exists())
panel.rename_local(str(local_side / "renamed here"), "inner")
check("browser: renaming onto an existing name is refused", (local_side / "renamed here").is_dir() and "already exists" in panel.status.text(), panel.status.text())
pump(0.6)
trashed = []
panel._move_to_trash = lambda p: (trashed.append(os.path.basename(p)), os.rmdir(p))[0] is None  # stand-in for the Recycle Bin
select_local("renamed here")
asked.clear()
panel.trash_selected_local()
check("browser: local items go to the Recycle Bin after a question", trashed == ["renamed here"] and not (local_side / "renamed here").exists() and asked == ["Move to Recycle Bin"] and "Moved 1 item" in panel.status.text(), panel.status.text())
QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes)

# -- drag and drop
tree_point = panel.tree.viewport().mapTo(panel, panel.tree.viewport().rect().center())
local_point = panel.local_view.viewport().mapTo(panel, panel.local_view.viewport().rect().center())
check("drag: the window knows which side a point is over", panel._side_under(tree_point) == "remote" and panel._side_under(local_point) == "local" and panel._side_under(panel.upload_button.mapTo(panel, panel.upload_button.rect().center())) == "")
decisions = [panel._drop_action(panel.tree, "local", False), panel._drop_action(panel.tree, "remote", False), panel._drop_action(panel.local_view, "remote", True), panel._drop_action(panel.local_view, "local", True), panel._drop_action(None, "local", True), panel._drop_action(None, "remote", False)]
check("drag: across the sides transfers, within one side does nothing, from outside uploads", decisions == ["download", "", "upload", "", "upload", ""], repr(decisions))
(local_side / "dropped.txt").write_bytes(b"dropped in\n")
mime = _QMimeData()
mime.setUrls([_QUrl.fromLocalFile(str(local_side / "dropped.txt"))])
panel.dropEvent(_QDropEvent(_QPointF(tree_point), Qt.DropAction.CopyAction, mime, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier))
check("drag: files dropped from outside are uploaded", pump(15, lambda: (SFTP_ROOT / "dropped.txt").exists() and (SFTP_ROOT / "dropped.txt").read_bytes() == b"dropped in\n"), panel.status.text())
(SFTP_ROOT / "dropped.txt").unlink()

# -- time left, and the log
check("progress: times are written as hours:minutes:seconds", [_clock(0.2), _clock(5), _clock(125), _clock(3700), _clock(-3)] == ["00:00:00", "00:00:05", "00:02:05", "01:01:40", "00:00:00"])
check("progress: speeds are written in KiB/s and MiB/s", [_rate_text(900), _rate_text(2048), _rate_text(22.1 * 1024 * 1024)] == ["900 B/s", "2.0 KiB/s", "22.1 MiB/s"])
panel.set_speed_limit(100)
statuses = []
panel.worker.progress.connect(lambda *_: statuses.append(panel.transfer_summary()))
pump(0.5)  # let the previous upload's "finished" message arrive, so it is not mistaken for this one's
select_local("big.bin")
panel.upload_button.click()
pump(30, lambda: bool(statuses) and "Uploaded" in panel.status.text() and not panel.cancel_button.isVisible())
panel.set_speed_limit(0)
line_shape = _re.compile(r"-->>.*\d\d:\d\d:\d\d elapsed +\d\d:\d\d:\d\d left +\d+\.\d%  +[\d,]+ bytes \([\d.]+ (?:KiB|MiB)/s\)")
check("progress: the line shows names, direction, time elapsed and left, per cent, bytes and speed", any(line_shape.search(line) for line in statuses) and any("big.bin" in line for line in statuses), repr(statuses[1:3]))
(SFTP_ROOT / "big.bin").unlink()
log_lines = [panel.log.item(i).text() for i in range(panel.log.count())]
check("log: transfers are recorded with the time", len(log_lines) > 5 and "Uploaded 1 file" in log_lines[-1] and log_lines[-1][2] == ":" and panel.log.isHidden(), repr((len(log_lines), panel.log.isHidden(), log_lines[-2:])))
panel.log_button.setChecked(True)
check("log: the Log button shows it, and that is remembered", panel.log.isVisible() and str(panel._settings.value("file_browser_log")) == "true")
panel.log_button.setChecked(False)

# -- edit a remote file in place
opened_files = []
panel._open_file = opened_files.append  # instead of launching an editor
(SFTP_ROOT / "config.ini").write_bytes(b"port=1\n")
panel.refresh()
pump(10, lambda: "config.ini" in remote_names())
edit_target = next(panel.tree.topLevelItem(i) for i in range(panel.tree.topLevelItemCount()) if panel.tree.topLevelItem(i).text(0) == "config.ini")
panel.edit_item(edit_target)
check("edit: the remote file is fetched and handed to the editor", pump(15, lambda: len(opened_files) == 1) and open(opened_files[0], "rb").read() == b"port=1\n" and os.path.basename(opened_files[0]) == "config.ini" and panel.editing() == ["/config.ini"] and "Editing config.ini" in panel.status.text(), panel.status.text())
edit_local = opened_files[0]


def save_in_editor(content):
    with open(edit_local, "wb") as handle:
        handle.write(content)
    stamp = os.path.getmtime(edit_local) + 5  # make sure the change is visible whatever the clock resolution
    os.utime(edit_local, (stamp, stamp))


save_in_editor(b"port=2\n")
check("edit: saving in the editor copies the file back to the server", pump(15, lambda: file_bytes(SFTP_ROOT / "config.ini") == b"port=2\n" and "Saved config.ini to the server" in panel.status.text()), panel.status.text())
save_in_editor(b"port=3\nmode=fast\n")
check("edit: and again on every later save", pump(15, lambda: file_bytes(SFTP_ROOT / "config.ini") == b"port=3\nmode=fast\n"))
pump(2.5)
check("edit: nothing is uploaded when the file has not changed", panel.log.item(panel.log.count() - 1).text().endswith("Saved config.ini to the server") and sum("Saved config.ini" in panel.log.item(i).text() for i in range(panel.log.count())) == 2)
panel.edit_item(next(panel.tree.topLevelItem(i) for i in range(panel.tree.topLevelItemCount()) if panel.tree.topLevelItem(i).data(0, Qt.ItemDataRole.UserRole)))
pump(0.5)
check("edit: folders are not opened for editing", len(opened_files) == 1)
save_in_editor(b"port=4\n")  # saved just before the window closes; checked after it has closed

panel.worker.delete("/no-such-folder-anywhere", True)
def reported_failure() -> bool:
    # The save above runs beside the delete on a channel of its own, so which message is last is not fixed: look at all of them.
    messages = [panel.status.text()] + [panel.log.item(i).text() for i in range(panel.log.count())]
    return any(word in message.lower() for message in messages for word in ("refused", "failure", "not empty", "no such file"))


check("files: a failed operation is reported, not raised", pump(10, reported_failure), panel.status.text())
red = _QColor(_theme.COLORS["red"]).name()
check("log: failures are shown in red", any(panel.log.item(i).foreground().color().name() == red for i in range(panel.log.count())))
panel.go("/projects")
pump(10, lambda: panel.directory == "/projects")
# Comparing the two folders, and copying only what differs
from techxterminal import foldersync as _foldersync

sync_here = WORK / "sync-here"
sync_there = SFTP_ROOT / "sync-there"
for folder in (sync_here, sync_there):
    folder.mkdir()
(sync_here / "same.txt").write_text("same")
(sync_there / "same.txt").write_text("same")
(sync_here / "mine.txt").write_text("only on this computer")
(sync_there / "theirs.txt").write_text("only on the server")
(sync_here / "edited-here.txt").write_text("newer here")
(sync_there / "edited-here.txt").write_text("older")
(sync_here / "edited-there.txt").write_text("older")
(sync_there / "edited-there.txt").write_text("newer there")
(sync_here / "odd.txt").write_text("one size")
(sync_there / "odd.txt").write_text("another size")
(sync_here / "new-folder").mkdir()
(sync_here / "new-folder" / "inside.txt").write_text("inside")
(sync_here / "node_modules").mkdir()
(sync_here / "node_modules" / "package.js").write_text("left out by default")
panel._settings.remove("compare_leave_out")  # the usual patterns
for path, when in ((sync_here / "same.txt", 1_000_000), (sync_there / "same.txt", 1_000_001), (sync_here / "edited-here.txt", 2_000_000), (sync_there / "edited-here.txt", 1_000_000), (sync_here / "edited-there.txt", 1_000_000), (sync_there / "edited-there.txt", 2_000_000), (sync_here / "odd.txt", 1_500_000), (sync_there / "odd.txt", 1_500_000)):
    os.utime(path, (when, when))
folders_before = (panel.local_directory, panel.directory)  # put back afterwards, for the checks that follow
panel.go_local(str(sync_here))
panel.go("/sync-there")
pump(10, lambda: panel.directory == "/sync-there")
panel.comparison = None
panel.compare_folders()
pump(10, lambda: panel.comparison is not None)
comparison = panel.comparison
kinds = {difference.path: difference.kind for difference in comparison.differences} if comparison else {}
check("compare: what differs between the two folders is found, all the way down, and nothing else", kinds == {"new-folder": _foldersync.ONLY_HERE, "mine.txt": _foldersync.ONLY_HERE, "theirs.txt": _foldersync.ONLY_THERE, "edited-here.txt": _foldersync.NEWER_HERE, "edited-there.txt": _foldersync.NEWER_THERE, "odd.txt": _foldersync.DIFFERENT}, repr(kinds))
check("compare: a file of the same date and another size is not ticked, as which way to copy it is not clear", comparison.upload_button.text() == "Upload (3)" and comparison.download_button.text() == "Download (2)", repr((comparison.upload_button.text(), comparison.download_button.text())))
comparison.upload_button.click()
pump(20, lambda: (sync_there / "new-folder" / "inside.txt").exists() and (sync_there / "mine.txt").exists() and (sync_there / "edited-here.txt").read_text() == "newer here")
uploaded_check = pump(20, lambda: "remain" in panel.status.text() or "now match" in panel.status.text())
check("compare: once the copies are done it compares again, and says what is left", uploaded_check and panel.status.text() == "Copied; 3 differences remain", panel.status.text())
check("compare: Upload copies what is newer or only on this computer, folders included, into the right folders", (sync_there / "new-folder" / "inside.txt").read_text() == "inside" and (sync_there / "mine.txt").read_text() == "only on this computer" and (sync_there / "edited-here.txt").read_text() == "newer here" and (sync_there / "odd.txt").read_text() == "another size")
panel.comparison = None
panel.compare_folders()
pump(10, lambda: panel.comparison is not None)
panel.comparison.download_button.click()
pump(20, lambda: (sync_here / "theirs.txt").exists() and (sync_here / "edited-there.txt").read_text() == "newer there")
check("compare: Download copies what is newer or only on the server, and the window closes", (sync_here / "theirs.txt").read_text() == "only on the server" and (sync_here / "edited-there.txt").read_text() == "newer there" and (sync_here / "odd.txt").read_text() == "one size" and pump(2, lambda: panel.comparison is None))
pump(20, lambda: "remain" in panel.status.text() or "now match" in panel.status.text())
after_download = panel.status.text()
(sync_here / "build.log").write_text("a log")
panel.comparison = None
panel.compare_folders()
pump(10, lambda: panel.comparison is not None)
with_log = {difference.path for difference in panel.comparison.differences}
first_window = panel.comparison
first_window.leave_out.setText(_foldersync.DEFAULT_LEAVE_OUT + ", *.log")
QTest.keyClick(first_window.leave_out, Qt.Key.Key_Return)
pump(10, lambda: panel.comparison is not None and panel.comparison is not first_window)
without_log = {difference.path for difference in panel.comparison.differences}
check("compare: what the usual patterns leave out is not listed, and patterns typed in the window are compared again and kept", after_download == "Copied; 1 difference remains" and "node_modules" not in with_log and "build.log" in with_log and without_log == {"odd.txt"} and "*.log" in panel.leave_out_text(), repr((after_download, with_log, without_log)))
panel.comparison.tick_all(True)
all_ticked = panel.comparison.upload_button.text()
panel.comparison.tick_all(False)
check("compare: Tick All and Tick None", all_ticked == "Upload (1)" and panel.comparison.upload_button.text() == "Upload (0)" and not panel.comparison.upload_button.isEnabled(), all_ticked)
panel.comparison.close()
panel._settings.remove("compare_leave_out")
panel.go_local(folders_before[0])
panel.go(folders_before[1])
pump(10, lambda: panel.directory == folders_before[1])
run(tlog, "still-alive")
check("files: the terminal keeps working alongside", "ran: still-alive" in text(tlog))
win.toggle_files()
check("files: the browser closes", tlog._files is None and not panel.isVisible())
check("edit: a save made just before closing still reaches the server, and the local copies are removed", (SFTP_ROOT / "config.ini").read_bytes() == b"port=4\n" and not os.path.exists(edit_local), repr(((SFTP_ROOT / "config.ini").read_bytes(), os.path.exists(edit_local), [panel.log.item(i).text()[11:] for i in range(max(0, panel.log.count() - 4), panel.log.count())])))
(SFTP_ROOT / "config.ini").unlink()
win.toggle_files()
reopened = tlog._files
pump(5, lambda: reopened.tree.topLevelItemCount() > 0)
check("browser: reopens in the folders last used on both sides", reopened.directory == "/projects" and reopened.bookmarks(True) == ["/projects"] and os.path.normpath(reopened.local_directory) == os.path.normpath(str(WORK / "localside")) and not reopened.hidden_box.isChecked(), f"{reopened.directory} {reopened.local_directory}")
reopened.go("/")
pump(10, lambda: reopened.directory == "/")
reopened.close()  # the window's own close button
pump(0.3)
check("files: closing the window with its own button works too", tlog._files is None and tlog.state == "connected")
win.toggle_files()
pump(0.5)
tlog.disconnect_from_host()
pump(0.5)
check("files: the browser closes when the session disconnects", tlog._files is None)
tlog.connect_to_host()
pump(15, lambda: tlog.state == "connected")

# ---- tab conveniences ---------------------------------------------------------------------
import json as _json3


def label(tab):
    pane = win._pane_of(tab)
    return pane.tabText(pane.indexOf(tab))


win.tabs.setCurrentIndex(win.tabs.indexOf(tab1))
pump(0.3)
check("activity: no mark to begin with", label(tlog) == "web-02" and label(tab1) == "web-01", repr((label(tlog), label(tab1))))
run(tlog, "background-output")
run(tab1, "foreground-output")
check("activity: a background tab with new output gets a dot, the visible one does not", label(tlog) == "web-02 \u2022" and tlog.unseen_output and label(tab1) == "web-01" and not tab1.unseen_output, ascii((label(tlog), label(tab1))))
win.tabs.setCurrentIndex(win.tabs.indexOf(tlog))
pump(0.3)
check("activity: looking at the tab clears the dot", label(tlog) == "web-02" and not tlog.unseen_output)

win.rename_tab(tlog, "deploy box")
check("rename tab: the tab and window title change, the saved session does not", label(tlog) == "deploy box" and "deploy box" in win.windowTitle() and tlog.session.name == "web-02" and win.store.get(tlog.session.id).name == "web-02", win.windowTitle())
win.tabs.setCurrentIndex(win.tabs.indexOf(tab1))
run(tlog, "more-output")
check("rename tab: the dot follows the new name", label(tlog) == "deploy box \u2022", ascii(label(tlog)))
win.rename_tab(tlog, "")
win.tabs.setCurrentIndex(win.tabs.indexOf(tlog))
pump(0.2)
check("rename tab: an empty name restores the session's name", label(tlog) == "web-02" and tlog.custom_title == "" and "web-02" in win.windowTitle())
win.tabs.tabBarDoubleClicked.emit(win.tabs.indexOf(tlog))  # the dialog is answered by the poller
pump(1.0)
check("rename tab: double-clicking a tab asks for a name", label(tlog) == "secret", label(tlog))
win.rename_tab(tlog, "web-02")
check("rename tab: typing the session's own name is the same as clearing it", tlog.custom_title == "" and label(tlog) == "web-02")
was_current = win.current_tab() is tlog
win.pin_tab(tlog, True)
now_current = win.current_tab() is tlog
check("pin tab: a pinned tab moves to the left and is marked", win.tabs.indexOf(tlog) == 0 and label(tlog).startswith("◆ ") and tlog.pinned, ascii(label(tlog)))
closed_pinned = win._close_several([tlog])
check("pin tab: the current tab stays the current tab", was_current == now_current, str((was_current, now_current)))
check("pin tab: the bulk closes leave a pinned tab open", closed_pinned == 0 and tlog in win.open_tabs(), str(closed_pinned))
win.pin_tab(tlog, False)
check("pin tab: unpinning removes the mark", not tlog.pinned and label(tlog) == "web-02", ascii(label(tlog)))
win.prefs = _replace(win.prefs, compact_pinned=True)
win.pin_tab(tlog, True, announce=False)
compact_label, compact_tip = label(tlog), win.tabs.tabToolTip(win.tabs.indexOf(tlog))
others_full = all(label(t) for t in win.open_tabs() if t is not tlog and win._pane_of(t) is win.tabs)
in_list = [action.text() for action in win.tab_list().actions()]
win.pin_tab(tlog, False, announce=False)
win.prefs = _replace(win.prefs, compact_pinned=False)
win._refresh_tab_labels()
check("pin tab: with the setting on, a pinned tab shows its icon only and its name is in the tooltip", compact_label == "" and "web-02" in compact_tip and others_full and any(text.startswith("◆ ") for text in in_list) and label(tlog) == "web-02", repr((compact_label, compact_tip, in_list)))
from techxterminal.preferences import Preferences as _Prefs_clamp
_clamped = _Prefs_clamp(tab_list_filter_at=5000)
_clamped.sanitize()
check("settings: the tab list filter threshold stays in range", _clamped.tab_list_filter_at == 99)
twin = win.open_session(win.store.get(tlog.session.id), "secret")
pump(0.3)
twin_label = label(twin)
win.close_tab(twin, force=True)
pump(0.3)
check("tab names: a second tab of a session is numbered, and the number goes with it", twin_label == "web-02 (2)" and label(tlog) == "web-02", repr(twin_label))

# ---- send bar history
win.all_tabs.setChecked(False)
win.tabs.setCurrentIndex(win.tabs.indexOf(tlog))  # closing the twin may have selected its neighbour
for command in ("echo one", "echo two", "echo two"):
    win.command.setText(command)
    win._send_command()
pump(0.6)
check("send history: commands are remembered once each and saved", win.command.history[-2:] == ["echo one", "echo two"] and list(win.settings.value("send_history"))[-2:] == ["echo one", "echo two"] and "ran: echo two" in text(tlog), repr(win.command.history[-3:]))
win.command.setText("dra")
recalled = []
for key in (Qt.Key.Key_Up, Qt.Key.Key_Up, Qt.Key.Key_Down, Qt.Key.Key_Down, Qt.Key.Key_Down):
    QTest.keyClick(win.command, key)
    recalled.append(win.command.text())
check("send history: Up and Down step through it and return to what was being typed", recalled == ["echo two", "echo one", "echo two", "dra", "dra"], repr(recalled))
win.command.clear()
from techxterminal.mainwindow import HistoryLineEdit as _HLE
fresh = _HLE()
fresh.set_history([f"c{i}" for i in range(80)])
fresh.remember("last")
check("send history: only the most recent 50 are kept", len(fresh.history) == 50 and fresh.history[-1] == "last" and fresh.history[0] == "c31", f"{len(fresh.history)} {fresh.history[0]}")

# ---- export and import of sessions
export_file = WORK / "exported sessions.json"
exported = win.export_sessions(export_file)
on_disk = _json3.loads(export_file.read_text(encoding="utf-8"))
check("export: every saved session is written, without passwords", exported == len(win.store.sessions) == len(on_disk["sessions"]) and "secret" not in export_file.read_text(encoding="utf-8") and any(item["name"] == "web-01" and item["color"] == "#f7768e" for item in on_disk["sessions"]), str(exported))
read_back = importers.from_sessions_file(export_file)
originals = {session.id for session in win.store.sessions}
check("import file: sessions come back complete, as new entries", len(read_back) == exported and not originals & {session.id for session in read_back} and sorted(session.name for session in read_back) == sorted(session.name for session in win.store.sessions) and all(not session.save_password for session in read_back))
from techxterminal.sessions import SessionStore as _Store2
other = _Store2(WORK / "another-computer.json")
unique = len({(session.protocol, session.host.lower(), session.port, session.username) for session in read_back})
check("import file: merging into an empty list adds them once", importers.merge(other, read_back) == unique and importers.merge(other, read_back) == 0 and len(_Store2(WORK / "another-computer.json").sessions) == unique, f"{unique}")
check("import file: importing your own export changes nothing", win.import_sessions(read_back) == 0)
(WORK / "junk.json").write_text("{ not json")
(WORK / "wrong.json").write_text(_json3.dumps({"sessions": [{"name": "no host"}, "text", {"host": "ok.example", "unknown_field": 1}]}))
check("import file: unreadable or partial files are handled", importers.from_sessions_file(WORK / "junk.json") == [] and importers.from_sessions_file(WORK / "nope.json") == [] and [session.host for session in importers.from_sessions_file(WORK / "wrong.json")] == ["ok.example"])

# ---- transfers waiting in the file browser
win.tabs.setCurrentIndex(win.tabs.indexOf(tlog))
win.toggle_files()
queue_browser = tlog._files
pump(10, lambda: queue_browser.directory == "/")
queue_browser.set_speed_limit(80)
queue_file = WORK / "localside" / "big.bin"
for _ in range(3):
    queue_browser.upload([str(queue_file)])
saw_waiting = pump(10, lambda: "+2 waiting" in queue_browser.transfer_summary())
queue_browser.set_speed_limit(0)
done_all = pump(30, lambda: queue_browser._waiting == 0 and not queue_browser.cancel_button.isVisible() and "Uploaded" in queue_browser.status.text())
check("file browser: shows how many transfers are waiting", saw_waiting and done_all and (SFTP_ROOT / "big.bin").stat().st_size == queue_file.stat().st_size, queue_browser.status.text())
(SFTP_ROOT / "big.bin").unlink()
win.toggle_files()
check("file browser: the remembered limit is back to unlimited", win.prefs.transfer_limit_kbps == 0 and tlog._files is None)

# ---- several transfers at the same time, and the settings for them -----------------------------
win.apply_preferences(_replace(win.prefs, transfer_parallel=3))
win.tabs.setCurrentIndex(win.tabs.indexOf(tlog))
win.toggle_files()
multi = tlog._files
pump(10, lambda: multi.directory == "/")
multi_files = []
for index in range(3):
    path = WORK / f"multi{index}.bin"
    path.write_bytes(os.urandom(360_000))
    multi_files.append(path)
multi.set_speed_limit(180)  # 60 KB/s each while three run, so that they overlap for a few seconds
multi.upload([str(path) for path in multi_files])
together = pump(15, lambda: multi.worker.running_count() == 3 and len(multi._started) == 3)
check("parallel: three selected files are transferred at the same time", together and multi.worker.waiting_count() == 0 and pump(5, lambda: "3 transfers" in multi.status.text()), multi.status.text())
pump(1.5)
bars = [multi.queued_list.topLevelItem(i).data(4, _sftp.ROLE_PROGRESS) for i in range(multi.queued_list.topLevelItemCount())]
check("parallel: every running transfer shows its own progress in the queue", len(bars) == 3 and all(isinstance(bar, int) and 0 <= bar <= 100 for bar in bars) and all("%" in multi.queued_list.topLevelItem(i).text(4) for i in range(3)), repr(bars))
multi.go("/projects")
browsed = pump(5, lambda: multi.directory == "/projects")
still_three = multi.worker.running_count() == 3
multi.go("/")
pump(5, lambda: multi.directory == "/")
check("parallel: the file list still answers while three transfers run", browsed and still_three, repr((browsed, still_three)))
rates = [entry[3] for entry in multi._progress.values()]
check("parallel: the speed limit is shared by the transfers that run", len(rates) == 3 and all(rate > 0 for rate in rates) and sum(rates) < 180 * 1024 * 1.5, repr([int(rate) for rate in rates]))
parts = [SFTP_ROOT / f"multi{index}.bin.part" for index in range(3)]
multi.pause_button.click()
pump(0.4)
held_at = [part.stat().st_size for part in parts if part.exists()]
pump(0.8)
held_still = [part.stat().st_size for part in parts if part.exists()]
check("parallel: Pause holds every running transfer", len(held_at) == 3 and held_at == held_still and multi.pause_button.text() == "Resume", repr((held_at, held_still)))
multi.pause_button.click()
moved_on = pump(5, lambda: [part.stat().st_size for part in parts if part.exists()] != held_at)
check("parallel: Resume lets them all carry on", moved_on and multi.pause_button.text() == "Pause", multi.pause_button.text())
stopped_job = sorted(multi._started)[0]
multi.worker.cancel(stopped_job)
pump(10, lambda: multi.failed_list.topLevelItemCount() == 1)
cancelled_name = os.path.basename(multi.failed_list.topLevelItem(0).text(0)) if multi.failed_list.topLevelItemCount() else ""
check("parallel: one transfer can be cancelled while the others go on", multi.failed_list.topLevelItem(0).text(4) == "Cancelled" and len(multi._started) == 2 and multi.worker.running_count() == 2 and not (SFTP_ROOT / (cancelled_name + ".part")).exists(), repr((cancelled_name, multi._started)))
multi.set_speed_limit(0)
others = [path for path in multi_files if path.name != cancelled_name]
finished = pump(30, lambda: all((SFTP_ROOT / path.name).exists() and (SFTP_ROOT / path.name).read_bytes() == path.read_bytes() for path in others))
pump(10, lambda: multi.worker.running_count() == 0 and not multi.cancel_button.isVisible())
check("parallel: the rest complete, byte for byte, and the cancelled one is not there", finished and not (SFTP_ROOT / cancelled_name).exists() and multi.worker.running_count() == 0 and not multi.cancel_button.isVisible(), repr((cancelled_name, finished, (SFTP_ROOT / cancelled_name).exists(), multi.worker.running_count(), multi.cancel_button.isVisible())))
for path in multi_files:
    (SFTP_ROOT / path.name).unlink(missing_ok=True)
multi._clear_list(multi.failed_list)
multi._clear_list(multi.done_list)

# Two at a time with four queued; raising the number starts the waiting ones at once.
win.apply_preferences(_replace(win.prefs, transfer_parallel=2))
multi.set_speed_limit(60)  # slow enough that the first two are still going when the number is raised
more_files = []
for index in range(4):
    path = WORK / f"more{index}.bin"
    path.write_bytes(os.urandom(150_000))
    more_files.append(path)
multi.upload([str(path) for path in more_files])
two_and_two = pump(10, lambda: multi.worker.running_count() == 2 and multi.worker.waiting_count() == 2)
pump(5, lambda: "+2 waiting" in multi.transfer_summary())  # the progress line follows the first progress report
row_height = multi.queued_list.sizeHintForRow(0)
check("file browser: the queue makes room for the transfers that are queued", multi.queued_list.viewport().height() >= 4 * row_height, repr((multi.queued_list.viewport().height(), row_height)))
check("parallel: only as many run as the setting allows, the rest wait", two_and_two and multi.queue_tabs.tabText(0) == "Queued files (4)" and "+2 waiting" in multi.transfer_summary(), repr((two_and_two, multi.queue_tabs.tabText(0), multi.status.text())))
win.apply_preferences(_replace(win.prefs, transfer_parallel=4, transfer_limit_kbps=60))
check("parallel: a higher number starts the waiting ones at once", pump(10, lambda: multi.worker.running_count() == 4 and multi.worker.waiting_count() == 0) and multi.worker.max_transfers() == 4, repr((multi.worker.running_count(), multi.worker.waiting_count())))
multi.set_speed_limit(0)
check("parallel: all four arrive", pump(30, lambda: all((SFTP_ROOT / path.name).exists() and (SFTP_ROOT / path.name).stat().st_size == 150_000 for path in more_files)) and pump(10, lambda: multi.worker.running_count() == 0), "")
for path in more_files:
    (SFTP_ROOT / path.name).unlink(missing_ok=True)

# The same file queued twice goes one after the other: two threads must not write one .part file at once.
twice = WORK / "twice.bin"
twice.write_bytes(os.urandom(120_000))
multi.set_speed_limit(120)
done_before = multi.done_list.topLevelItemCount()
multi.upload([str(twice)])
multi.upload([str(twice)])
at_once = []
both_finished = pump(30, lambda: (at_once.append(multi.worker.running_count()), multi.done_list.topLevelItemCount() >= done_before + 2)[1])
pump(5, lambda: not multi.cancel_button.isVisible())
check("parallel: the same file queued twice is sent one after the other, and arrives whole", both_finished and max(at_once) == 1 and (SFTP_ROOT / "twice.bin").read_bytes() == twice.read_bytes() and not (SFTP_ROOT / "twice.bin.part").exists() and multi.failed_list.topLevelItemCount() == 0, repr((both_finished, max(at_once or [0]), multi.failed_list.topLevelItemCount())))
multi.set_speed_limit(0)
(SFTP_ROOT / "twice.bin").unlink(missing_ok=True)

# A channel that cannot be opened: another thread takes the transfer over. If none can, it fails, and nothing waits for ever.
from techxterminal.sftp import SftpWorker as _SftpWorker

real_open = tlog._worker.open_sftp
refusals = {"left": 1}


def sometimes_refused():
    if refusals["left"] > 0 and threading.current_thread().name == "file-transfer":
        refusals["left"] -= 1
        raise paramiko.SSHException("Channel closed.")
    return real_open()


def never_opens():
    raise paramiko.SSHException("no channels")


outcomes = {}
flaky = _SftpWorker(sometimes_refused)
flaky.set_parallel(2)
flaky.job_finished.connect(lambda job, ok, why: outcomes.__setitem__(job, (ok, why)))
flaky.start()
small_files = []
for index in range(2):
    path = WORK / f"flaky{index}.bin"
    path.write_bytes(os.urandom(50_000))
    small_files.append(path)
flaky_jobs = [flaky.upload([str(path)], "/") for path in small_files]
both_done = pump(15, lambda: len(outcomes) == 2)
check("channels: a transfer whose channel could not be opened is taken over by another thread", both_done and refusals["left"] == 0 and all(outcomes[job][0] for job in flaky_jobs) and all((SFTP_ROOT / path.name).read_bytes() == path.read_bytes() for path in small_files), repr(outcomes))
flaky.stop()
flaky.wait(5000)
for path in small_files:
    (SFTP_ROOT / path.name).unlink(missing_ok=True)
refused = {}
hopeless = _SftpWorker(never_opens)
hopeless.set_parallel(2)
hopeless.job_finished.connect(lambda job, ok, why: refused.__setitem__(job, (ok, why)))
hopeless.start()
hopeless_jobs = [hopeless.upload([str(path)], "/") for path in small_files]
all_failed = pump(15, lambda: len(refused) == 2)
check("channels: when no channel can be opened the transfers fail, with the reason, and the queue is empty", all_failed and all(not refused[job][0] and "no channels" in refused[job][1] for job in hopeless_jobs) and hopeless.waiting_count() == 0 and hopeless.running_count() == 0, repr(refused))
hopeless.stop()
hopeless.wait(5000)

# The settings page, and what the settings do to a file window that is open.
sd5 = SettingsDialog(win.prefs, win)
sd5.show_page("transfers")
on_page = sd5.pages.currentIndex() == sd5._page_index["transfers"] and sd5._nav_group.button(sd5.pages.currentIndex()).isChecked() and sd5._nav_group.button(sd5.pages.currentIndex()).text() == "File Transfer"
shown = (sd5.transfer_parallel.value(), sd5.transfer_limit.value(), sd5.transfer_confirm_replace.isChecked(), sd5.transfer_keep_dates.isChecked(), sd5.transfer_resume.isChecked())
sd5.transfer_parallel.setValue(2)
sd5.transfer_limit.setValue(500)
sd5.transfer_confirm_replace.setChecked(False)
sd5.transfer_keep_dates.setChecked(False)
sd5.transfer_resume.setChecked(False)
chosen = sd5.preferences()
check("settings: the File Transfer page shows the current values and returns the changed ones", on_page and shown == (4, 0, True, True, True) and (chosen.transfer_parallel, chosen.transfer_limit_kbps, chosen.transfer_confirm_replace, chosen.transfer_keep_dates, chosen.transfer_resume) == (2, 500, False, False, False), repr((on_page, shown)))
sd5.applied.connect(win.apply_preferences)
sd5._apply()
sd5.close()
saved = Preferences.load()
check("settings: applying them reaches a file window that is open, and is saved", multi.worker.max_transfers() == 2 and multi.speed_limit() == 500 and multi.worker.keep_dates is False and multi.worker.resume is False and (saved.transfer_parallel, saved.transfer_limit_kbps, saved.transfer_confirm_replace, saved.transfer_keep_dates, saved.transfer_resume) == (2, 500, False, False, False), repr((multi.worker.max_transfers(), multi.speed_limit())))
too_many, too_few = _replace(Preferences(), transfer_parallel=99), _replace(Preferences(), transfer_parallel=0)
too_many.sanitize()
too_few.sanitize()
check("settings: a number of transfers out of range is brought back", (too_many.transfer_parallel, too_few.transfer_parallel) == (8, 1), repr((too_many.transfer_parallel, too_few.transfer_parallel)))
multi.set_speed_limit(0)

# Not asking before replacing, and not keeping the date, as chosen above.
(SFTP_ROOT / "replace-me.txt").write_bytes(b"old\n")
multi.refresh()
pump(5, lambda: "replace-me.txt" in multi.remote_names())
replacement = WORK / "replace-me.txt"
replacement.write_bytes(b"new\n")
os.utime(replacement, (long_ago, long_ago))
asked = []
QMessageBox.question = staticmethod(lambda *a, **k: (asked.append(a[1]), QMessageBox.StandardButton.No)[1])
multi.upload([str(replacement)])
replaced = pump(10, lambda: (SFTP_ROOT / "replace-me.txt").read_bytes() == b"new\n")
undated = abs((SFTP_ROOT / "replace-me.txt").stat().st_mtime - time.time()) < 300
check("settings: with the question off, a file is replaced without asking, and takes today's date", replaced and not asked and undated, repr((asked, (SFTP_ROOT / "replace-me.txt").stat().st_mtime)))
win.apply_preferences(_replace(win.prefs, transfer_confirm_replace=True, transfer_keep_dates=True))
replacement.write_bytes(b"newer\n")
multi.upload([str(replacement)])
pump(1.0)
check("settings: with the question on, it asks, and No leaves the file as it was", asked == ["Replace Files"] and (SFTP_ROOT / "replace-me.txt").read_bytes() == b"new\n", repr(asked))
QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes)
(SFTP_ROOT / "replace-me.txt").unlink(missing_ok=True)

# The button in the file window opens the settings on this page.
settings_seen = []


def look_at_settings():
    dialog = app.activeModalWidget()
    if isinstance(dialog, SettingsDialog):
        settings_seen.append(dialog.pages.currentIndex() == dialog._page_index["transfers"])
        dialog.reject()


QTimer.singleShot(400, look_at_settings)
multi.settings_button.click()
check("file browser: its settings button opens the settings on the File Transfer page", settings_seen == [True], repr(settings_seen))
win.toggle_files()
win.apply_preferences(_replace(win.prefs, transfer_parallel=3, transfer_limit_kbps=0, transfer_confirm_replace=True, transfer_keep_dates=True, transfer_resume=True))
QMessageBox.question = real_question
win.tabs.setCurrentIndex(0)
pump(0.3)

# ---- saved commands ------------------------------------------------------------------------
from techxterminal.dialogs import SnippetsDialog
from techxterminal.snippets import Snippet, SnippetStore

win.snippets.replace_all([Snippet("Disk", "df -h"), Snippet("", "uptime\nwhoami"), Snippet("blank", "   ")])
reloaded_snippets = SnippetStore()
check("saved commands: stored in their own file, empty ones dropped", [(sn.name, sn.command) for sn in reloaded_snippets.snippets] == [("Disk", "df -h"), ("", "uptime\nwhoami")] and (HOME / ".techxterminal" / "snippets.json").exists() and reloaded_snippets.snippets[1].title == "uptime")
win.tabs.setCurrentIndex(win.tabs.indexOf(tlog))
win.all_tabs.setChecked(False)
sent_to = win.send_to_targets(win.snippets.snippets[1].command)
pump(0.9)
check("saved commands: each line is sent to the active tab", sent_to == 1 and "ran: uptime" in text(tlog) and "ran: whoami" in text(tlog) and "ran: uptime" not in text(tab1))
win.all_tabs.setChecked(True)
sent_to = win.send_to_targets("df -h")
pump(0.9)
check("saved commands: or to every tab", sent_to == len(win.open_tabs()) and all("ran: df -h" in text(tab) for tab in win.open_tabs()))
win.all_tabs.setChecked(False)
editor = SnippetsDialog(win.snippets.snippets, win)
editor.show()
pump(0.3)
editor.add()
editor.name.setText("Restart web")
editor.command.setPlainText("sudo systemctl restart nginx")
editor.list.setCurrentRow(0)
editor.remove()
edited = editor.snippets()
check("saved commands: the editor adds, renames and deletes", [(sn.name, sn.command) for sn in edited] == [("", "uptime\nwhoami"), ("Restart web", "sudo systemctl restart nginx")] and [editor.list.item(i).text() for i in range(editor.list.count())] == ["uptime", "Restart web"] and [sn.name for sn in win.snippets.snippets] == ["Disk", ""], repr(edited))
editor.grab().save(str(SCRATCH / "shot_snippets.png"))
editor.close()

from techxterminal.snippets import fill_prompts, prompts_in

check("saved commands: {?label} and {?label=default} are the values asked for, each once, in order", prompts_in("scp {?file} {user}@{?host=db-1}:{?dir=/tmp}\nls {?file}") == [("file", ""), ("host", "db-1"), ("dir", "/tmp")] and prompts_in("awk '{print $1}' {host}") == [], repr(prompts_in("scp {?file} {?host=db-1}")))
check("saved commands: answers fill the prompts, an unanswered one keeps its default, and a line break cannot make a second command", fill_prompts("rm {?file}; ls {?dir=/tmp}", {"file": "a b\nrm -rf /"}) == "rm a b rm -rf /; ls /tmp" and fill_prompts("x {?a=1}", {"a": ""}) == "x ")
asked_for = []
win.ask_snippet_values = lambda snippet, prompts: asked_for.append((snippet.title, prompts)) or {"word": "typed-value"}
win.all_tabs.setChecked(False)
win.tabs.setCurrentIndex(win.tabs.indexOf(tlog))
sent_to = win.send_snippet(Snippet("Say", "echo {?word=none} on {host}"))
pump(0.9)
check("saved commands: a command with prompts asks first, then sends with the answers and the tab's placeholders", sent_to == 1 and asked_for == [("Say", [("word", "none")])] and "ran: echo typed-value on 127.0.0.1" in text(tlog), repr(asked_for))
win.ask_snippet_values = lambda snippet, prompts: None
check("saved commands: cancelling the question sends nothing", win.send_snippet(Snippet("Say", "echo {?word} cancelled-marker")) == 0)
pump(0.5)
check("saved commands: (and nothing reached the tab)", "ran: echo  cancelled-marker" not in text(tlog) and "cancelled-marker" not in text(tlog).replace("echo {?word} cancelled-marker", ""))
del win.ask_snippet_values
from techxterminal.dialogs import CommandValuesDialog

values_dialog = CommandValuesDialog("Say", [("host", "db-1"), ("dir", "")], win)
values_dialog.show()
pump(0.2)
values_dialog.fields["dir"].setText("/srv")
check("saved commands: the question has a field for each label, with the default filled in", list(values_dialog.fields) == ["host", "dir"] and values_dialog.values() == {"host": "db-1", "dir": "/srv"})
values_dialog.close()

before_count = len(win.snippets.snippets)
kept = win.save_as_snippet("  sudo journalctl -u nginx   \n  -n 50  \n", "Nginx log")
check("saved commands: text from the terminal is kept as a saved command, trimmed", kept is not None and (kept.name, kept.command) == ("Nginx log", "sudo journalctl -u nginx\n  -n 50") and len(win.snippets.snippets) == before_count + 1 and SnippetStore().snippets[-1].command == kept.command, repr(kept))
check("saved commands: nothing selected keeps nothing", win.save_as_snippet("  \n ", "x") is None and len(win.snippets.snippets) == before_count + 1)
win.snippets.replace_all(win.snippets.snippets[:before_count])

# ---- approved host keys ----------------------------------------------------------------------------------
from techxterminal import hostkeys as _hostkeys

keys_file = WORK / "approved_known_hosts"
_book = paramiko.HostKeys()
_book.add("web-01", "ssh-rsa", paramiko.RSAKey.generate(2048))
_book.add("[10.0.0.9]:2222", "ssh-rsa", paramiko.RSAKey.generate(2048))
_book.save(str(keys_file))
keys_file.write_text(keys_file.read_text(encoding="utf-8") + "# kept as it is\nnot a key line\n", encoding="utf-8")
approved = _hostkeys.approved_keys(keys_file)
check("host keys: every approved key is listed with its fingerprint, comments and damaged lines left out", [(k.host, k.key_type) for k in approved] == [("[10.0.0.9]:2222", "ssh-rsa"), ("web-01", "ssh-rsa")] and all(k.fingerprint.startswith("SHA256:") for k in approved) and _hostkeys.approved_keys(WORK / "no-such-file") == [], repr(approved))
check("host keys: forgetting an unknown name changes nothing", _hostkeys.forget_host("nobody", keys_file) == 0)
host_keys_window = win.show_host_keys(keys_file)
pump(0.2)
shown_all = host_keys_window.list.count()
host_keys_window.filter.setText("web")
shown_filtered = host_keys_window.list.count()
host_keys_window.filter.setText("")
check("host keys: the window lists them and filters by name", shown_all == 2 and shown_filtered == 1 and host_keys_window.list.count() == 2)
host_keys_window.list.selectAll()
real_question = QMessageBox.question
QMessageBox.question = staticmethod(lambda *args, **kwargs: QMessageBox.StandardButton.No)
declined = host_keys_window.forget_selected()
QMessageBox.question = staticmethod(lambda *args, **kwargs: QMessageBox.StandardButton.Yes)
host_keys_window.filter.setText("web")
host_keys_window.list.selectAll()
removed = host_keys_window.forget_selected()
QMessageBox.question = real_question
check("host keys: Forget asks first, then removes only the selected host, and the file keeps its other lines", declined == 0 and removed == 1 and [k.host for k in _hostkeys.approved_keys(keys_file)] == ["[10.0.0.9]:2222"] and "# kept as it is" in keys_file.read_text(encoding="utf-8") and "not a key line" in keys_file.read_text(encoding="utf-8"))
host_keys_window.close()

# ---- folders -----------------------------------------------------------------------------------------
production = [session.name for session in win.sessions_in("Production")]
folder_tabs = win.connect_folder("Production")
check("folders: Connect All opens every session in the folder", production == ["db-primary", "web-01", "web-02"] and [tab.session.name for tab in folder_tabs] == production and pump(40, lambda: all(tab.state == "connected" for tab in folder_tabs)), repr([tab.state for tab in folder_tabs]))
recent_names = [session.name for session in win.recent_sessions()]
check("recent: the sessions opened last come first, unsaved quick connections are left out", recent_names[:3] == ["web-02", "web-01", "db-primary"] and "127.0.0.1" not in recent_names and len(recent_names) <= 6, repr(recent_names))
recent_buttons = [win.recent_row.itemAt(i).widget() for i in range(win.recent_row.count())]
check("recent: shown as buttons on the start page", [button.text() for button in recent_buttons] == recent_names and not win.recent_bar.isHidden())
for tab in folder_tabs:
    win.close_tab(tab)
before_tabs = len(win.open_tabs())
recent_buttons[0].click()
pump(0.3)
from_recent = win.current_tab()
check("recent: clicking one opens that session", len(win.open_tabs()) == before_tabs + 1 and from_recent.session.name == "web-02" and pump(20, lambda: from_recent.state == "connected"))
win.close_tab(from_recent)

staging_tab = next(tab for tab in win.open_tabs() if tab.session.name == "staging-api")
win.rename_folder("Staging", "QA")
tree_groups = [win.tree.topLevelItem(i).text(0) for i in range(win.tree.topLevelItemCount()) if win.tree.topLevelItem(i).childCount()]
check("folders: renaming a folder updates the sessions, the sidebar and open tabs", "QA" in win.store.groups() and "Staging" not in win.store.groups() and "QA" in tree_groups and staging_tab.session.group == "QA" and "QA" in (HOME / ".techxterminal" / "sessions.json").read_text(), repr(tree_groups))
runner = next(session for session in win.store.sessions if session.name == "build-runner")
win.move_session(runner, "QA")
check("folders: a session can be moved into a folder", win.store.get(runner.id).group == "QA" and "build-runner" in [session.name for session in win.sessions_in("QA")])
win.move_session(win.store.get(runner.id), "")
check("folders: and back out of it", win.store.get(runner.id).group == "" and "build-runner" not in [session.name for session in win.sessions_in("QA")])
win.rename_folder("QA", "Staging")
win.rename_folder("Staging", "  ")
check("folders: an empty name changes nothing", "Staging" in win.store.groups() and "QA" not in win.store.groups())

# ---- save output --------------------------------------------------------------------------------------
run(tlog, "echo save-me-123")
saved_output = WORK / "saved output.txt"
saved_ok = win.save_output(tlog, saved_output)
content = saved_output.read_text(encoding="utf-8")
check("save output: scrollback and screen are written as plain text", saved_ok and "ran: echo save-me-123" in content and "Welcome to demo-box" in content and "\x1b" not in content and content.endswith("$\n") and len(content.splitlines()) > 20, ascii(content[-80:]))
check("save output: a path that cannot be written is reported", not win.save_output(tlog, WORK / "no such folder" / "x.txt"))
win.tabs.setCurrentIndex(0)
pump(0.3)

# pinned tabs head the palette's tab list
win.pin_tab(tlog, True, announce=False)
pinned_palette = win.show_switcher()
pump(0.3)
palette_kinds = pinned_palette.shown()
pinned_palette.reject()
win.pin_tab(tlog, False, announce=False)
win.tabs.setCurrentIndex(0)
check("palette: pinned tabs are listed first, as Pinned", palette_kinds[0] == ("pinned", win._tab_name(tlog)) and all(kind == "tab" for kind, _ in palette_kinds[1 : len(win.open_tabs())]), repr(palette_kinds[:3]))

# a saved session can be set to open pinned from the sidebar, without editing it
win.set_session_pinned(win.store.get(runner.id), True)
runner_pinned = win.store.get(runner.id).pinned
def _tree_items():
    stack = [win.tree.topLevelItem(i) for i in range(win.tree.topLevelItemCount())]
    while stack:
        item = stack.pop()
        stack.extend(item.child(i) for i in range(item.childCount()))
        yield item


sidebar_pinned = [item.data(0, ROLE_PINNED) for item in _tree_items() if item.data(0, ROLE_SESSION_ID) == runner.id]
win._fill_rail()
rail_tips = [win._rail_column.itemAt(i).widget().toolTip() for i in range(win._rail_column.count() - 1)]
rail_marked = [tip for tip in rail_tips if runner.address in tip and "Opens its tab pinned" in tip]
from techxterminal.theme import session_avatar as _avatar
tile_differs = _avatar("build-runner", "", "", True).pixmap(96, 96).toImage() != _avatar("build-runner", "", "", False).pixmap(96, 96).toImage()
runner_tab = win.open_session(win.store.get(runner.id), "secret")
pump(0.3)
runner_opens_pinned = runner_tab.pinned
win.close_tab(runner_tab, force=True)
win._closed_tabs.clear()
win.set_session_pinned(win.store.get(runner.id), False)
sidebar_unpinned = [item.data(0, ROLE_PINNED) for item in _tree_items() if item.data(0, ROLE_SESSION_ID) == runner.id]
check("pin tab: the sidebar can set a session to open pinned, and back", runner_pinned and runner_opens_pinned and not win.store.get(runner.id).pinned)
check("pin tab: the rail marks it too, with a diamond on its tile and a line in its tooltip", len(rail_marked) == 1 and tile_differs, repr((rail_tips, tile_differs)))
check("pin tab: the sidebar marks a session that opens pinned", sidebar_pinned and all(sidebar_pinned) and sidebar_unpinned and not any(sidebar_unpinned), repr((sidebar_pinned, sidebar_unpinned)))

# a server answer that arrives after its tab was closed is dropped, not applied to a deleted tab
gone = win.open_session(win.store.get(tlog.session.id), "secret")
win.close_tab(gone, force=True)
win._closed_tabs.clear()
pump(0.3)
try:
    win._server_probed(gone, None, 12.0)
    late_answer_ok = True
except RuntimeError as exc:
    late_answer_ok = False
check("monitor: an answer that comes after its tab was closed is ignored", late_answer_ok)

# the workspace file: an older one without layouts still loads, and a layout survives saving
from techxterminal.workspaces import WorkspaceStore as _WS
_ws_path = WORK / "ws-layout-test.json"
_ws_path.write_text('{"workspaces": {"old": ["a", "b"]}}', encoding="utf-8")
_older = _WS(_ws_path)
_older.set("new", ["c"], {"pinned": ["0"], "panes": ["1"]})
_again = _WS(_ws_path)
check("workspaces: an older file without layouts loads, and a saved layout comes back", _older.get("old") == ["a", "b"] and _again.get_layout("old") == {} and _again.get_layout("new") == {"pinned": ["0"], "panes": ["1"]} and _again.get("old") == ["a", "b"])
_again.remove("new")
check("workspaces: removing one removes its layout", _WS(_ws_path).get_layout("new") == {})

# ---- quick switcher ------------------------------------------------------------------------
switcher = win.show_switcher()
pump(0.3)
listed = switcher.shown()
open_count = len(win.open_tabs())
rest = [kind for kind, _ in listed[open_count + len(win.store.sessions) :]]
check("switcher: open tabs come first, then every saved session, then commands, saved commands and workspaces", [kind for kind, _ in listed[:open_count]] == ["tab"] * open_count and [kind for kind, _ in listed[open_count : open_count + len(win.store.sessions)]] == ["session"] * len(win.store.sessions) and rest == sorted(rest, key=["action", "snippet", "workspace"].index) and rest.count("snippet") == len(win.snippets.snippets) and switcher.isVisible(), repr(listed[:5]))
switcher.query.setText("stag api")
check("switcher: typing narrows the list, words in any order", switcher.shown() == [("tab", "staging-api"), ("session", "staging-api")], repr(switcher.shown()))
QTest.keyClick(switcher.query, Qt.Key.Key_Down)
moved_down = switcher.list.currentRow()
QTest.keyClick(switcher.query, Qt.Key.Key_Up)
check("switcher: the arrow keys move through the list", moved_down == 1 and switcher.list.currentRow() == 0)
switcher.query.setText("")
QTest.keyClick(switcher.query, Qt.Key.Key_PageDown)
paged_down = switcher.list.currentRow()
for _ in range(200):
    QTest.keyClick(switcher.query, Qt.Key.Key_PageDown)
at_end = switcher.list.currentRow()
QTest.keyClick(switcher.query, Qt.Key.Key_PageUp)
paged_up = switcher.list.currentRow()
switcher.list.setCurrentRow(1)
QTest.keyClick(switcher.query, Qt.Key.Key_PageUp)
check("switcher: Page Down and Page Up move a screenful, and stop at the ends", 1 < paged_down < at_end == switcher.list.count() - 1 and paged_up == at_end - paged_down and switcher.list.currentRow() == 0, repr((paged_down, at_end, paged_up)))
switcher.query.setText("stag api")
QTest.keyClick(switcher.query, Qt.Key.Key_Return)
pump(0.3)
check("switcher: Enter on a tab switches to it", win.current_tab().session.name == "staging-api" and not switcher.isVisible(), win.current_tab().title)
switcher = win.show_switcher()
switcher.query.setText("zzzz-nothing")
QTest.keyClick(switcher.query, Qt.Key.Key_Return)
check("switcher: no match, nothing happens", switcher.shown() == [] and switcher.isVisible() and len(win.open_tabs()) == open_count)
switcher.query.setText("db-primary")
QTest.keyClick(switcher.query, Qt.Key.Key_Return)
pump(0.3)
via_switcher = win.current_tab()
check("switcher: Enter on a session opens it", len(win.open_tabs()) == open_count + 1 and via_switcher.session.name == "db-primary" and pump(20, lambda: via_switcher.state == "connected"))
win.close_tab(via_switcher)

# ---- reopening tabs --------------------------------------------------------------------------
win.remember_open_tabs()
remembered = list(win.settings.value("open_sessions"))
saved_open = [tab.session.id for tab in win.open_tabs() if win.store.get(tab.session.id)]
check("restore: the saved sessions that are open are remembered, quick connections are not", remembered == saved_open and len(remembered) == len(win.open_tabs()) - 1, f"{len(remembered)} of {len(win.open_tabs())}")
restored = win.restore_tabs()
check("restore: they are opened again in the same order", [tab.session.id for tab in restored] == remembered and pump(40, lambda: all(tab.state == "connected" for tab in restored)), repr([tab.state for tab in restored]))
for tab in restored:
    win.close_tab(tab)
tab_list = win.tab_list()
listed = [action.text() for action in tab_list.actions()]
check("tab list: every open tab is listed, and the current one is ticked", len(listed) == len(win.open_tabs()) and sum(action.isChecked() for action in tab_list.actions()) == 1, repr(listed))
other = next(t for t in win.open_tabs() if t is not win.current_tab())
next(action for action, t in zip(tab_list.actions(), win.open_tabs()) if t is other).trigger()
check("tab list: choosing a tab goes to it", win.current_tab() is other)
tab_list.deleteLater()

# a filter box for a long list of tabs
_kept_prefs = win.prefs
win.prefs = _replace(win.prefs, tab_list_filter_at=2)
long_list = win.tab_list()
box = getattr(long_list, "filter_box", None)
rows = [action for action in long_list.actions() if action.isCheckable()]
box.setText("zzz-nothing")
none_shown = not any(action.isVisible() for action in rows)
box.setText(win._tab_name(win.open_tabs()[0]).split()[0])
some_shown = any(action.isVisible() for action in rows)
check("tab list: a long list has a filter box that narrows it", box is not None and none_shown and some_shown and len(rows) == len(win.open_tabs()), repr((none_shown, some_shown)))
long_list.deleteLater()
win.prefs = _kept_prefs
check("tab list: a short list has none", getattr(win.tab_list(), "filter_box", None) is None or len(win.open_tabs()) > win.prefs.tab_list_filter_at)

# pinned tabs cannot be dragged out of the pinned group
drag_pane = win.tabs
order_before = [drag_pane.widget(i) for i in range(drag_pane.count())]
anchor = win.open_tabs()[0]
if win._pane_of(anchor) is not drag_pane:
    anchor = drag_pane.widget(0)
win.pin_tab(anchor, True, announce=False)
others_here = [drag_pane.widget(i) for i in range(drag_pane.count()) if drag_pane.widget(i) is not anchor]
drag_pane.tabBar().moveTab(drag_pane.indexOf(others_here[-1]), 0)  # an unpinned tab dropped before the pinned one
unpinned_stays = drag_pane.indexOf(anchor) == 0
drag_pane.tabBar().moveTab(0, drag_pane.count() - 1)  # the pinned tab dropped at the far end
pinned_stays = drag_pane.indexOf(anchor) == 0
check("pin tab: a tab dragged before a pinned one, or a pinned one dragged after the rest, goes back", unpinned_stays and pinned_stays, repr((unpinned_stays, pinned_stays)))
win.pin_tab(anchor, False, announce=False)
for place, tab in enumerate(order_before):  # leave the tabs as they were for what follows
    drag_pane.tabBar().moveTab(drag_pane.indexOf(tab), place)

# a session can open its tab pinned
pin_dialog = SessionDialog(win.store, win.store.get(runner.id), win)
pin_dialog.pinned.setChecked(True)
pin_saved = pin_dialog.session().pinned
pin_dialog.close()
auto_pinned = win.open_session(_replace(win.store.get(tlog.session.id), pinned=True), "secret")
pump(0.3)
check("pin tab: a session set to open pinned does, and the dialog saves the setting", pin_saved and auto_pinned.pinned and label(auto_pinned).startswith("◆ ") and win._pane_of(auto_pinned).indexOf(auto_pinned) == sum(1 for t in win.open_tabs() if t.pinned and win._pane_of(t) is win._pane_of(auto_pinned)) - 1, repr(label(auto_pinned)))
win.close_tab(auto_pinned, force=True)
win._closed_tabs.clear()
check("restore: off unless asked for", Preferences().restore_tabs is False)

# pins come back with the tabs, after a restart and after a close
first = win.restore_tabs()
win.pin_tab(first[-1], True, announce=False)
pinned_id = first[-1].session.id
win.remember_open_tabs()
saved_entries, saved_pins = list(win.settings.value("open_sessions")), [int(i) for i in win.settings.value("pinned_tabs")]
check("restore: the pinned tab is remembered by its place in the list", len(saved_pins) == 1 and saved_entries[saved_pins[0]] == pinned_id, repr((saved_entries, saved_pins)))
for tab in first:
    win.close_tab(tab, force=True)
win._closed_tabs.clear()
win.settings.setValue("open_sessions", [saved_entries[saved_pins[0]]] + [t for t in remembered])  # the pinned one, then the others
win.settings.setValue("pinned_tabs", ["0"])
again = win.restore_tabs()
check("restore: a pinned tab is pinned again, the others are not", [t.session.id for t in again if t.pinned] == [pinned_id] and len(again) == len(remembered) + 1, repr([(t.session.name, t.pinned) for t in again]))
win.close_tab(next(t for t in again if t.pinned), force=True)
reopened_pin = win.reopen_closed_tab()
check("reopen closed tab: a pinned tab comes back pinned", len(reopened_pin) == 1 and reopened_pin[0].pinned)
for tab in list(again) + list(reopened_pin):
    if tab in win.open_tabs():
        win.close_tab(tab, force=True)
win._closed_tabs.clear()
win.settings.setValue("open_sessions", remembered)  # as before this block
win.settings.setValue("pinned_tabs", [])

# ---- SSH keys ----------------------------------------------------------------------------------
from techxterminal import keys as _keys
from techxterminal.dialogs import KeyDialog

new_key = WORK / "keys" / "id_new"
public_line = _keys.generate_keypair(new_key, "pass phrase", "me@test")
loaded = paramiko.Ed25519Key.from_private_key_file(str(new_key), "pass phrase")
check("keys: an Ed25519 pair is created, private key encrypted, public key beside it", public_line.startswith("ssh-ed25519 ") and public_line.endswith(" me@test") and Path(str(new_key) + ".pub").read_text() == public_line + "\n" and loaded.get_base64() == public_line.split()[1] and b"ENCRYPTED" not in new_key.read_bytes()[:40] and new_key.read_text().startswith("-----BEGIN OPENSSH PRIVATE KEY-----"))
try:
    paramiko.Ed25519Key.from_private_key_file(str(new_key), "wrong")
    wrong_accepted = True
except paramiko.SSHException:
    wrong_accepted = False
try:
    _keys.generate_keypair(new_key, "", "")
    overwrote = True
except _keys.KeyError_ as exc:
    overwrote = "already exists" not in str(exc)
check("keys: the passphrase is really needed, and existing keys are never overwritten", not wrong_accepted and not overwrote)
plain_key = WORK / "keys" / "id_plain"
plain_line = _keys.generate_keypair(plain_key, "", "")
Path(str(plain_key) + ".pub").unlink()
Path(str(new_key) + ".keep").write_text("x")
derived = _keys.public_key_line(plain_key)
check("keys: the public key is read from the .pub file, or worked out from the private key", _keys.public_key_line(new_key) == public_line and _keys.public_key_line(Path(str(new_key) + ".pub")) == public_line and derived.split()[:2] == plain_line.split()[:2])
Path(str(new_key) + ".pub").rename(WORK / "keys" / "moved.pub")
try:
    _keys.public_key_line(new_key)
    explained = False
except _keys.KeyError_ as exc:
    explained = "passphrase" in str(exc)
check("keys: an encrypted private key without its .pub file is explained", explained)

win.tabs.setCurrentIndex(win.tabs.indexOf(tlog))
authorized = SFTP_ROOT / ".ssh" / "authorized_keys"
win.install_public_key(tlog, str(WORK / "keys" / "moved.pub"))
check("keys: the public key is installed on the server", pump(15, lambda: authorized.exists() and authorized.read_text() == public_line + "\n") and pump(5, lambda: "Installed the public key me@test on 127.0.0.1" in "\n".join(tlog.terminal._line_text(i) for i in range(tlog.terminal._total_lines()))))
win.install_public_key(tlog, str(WORK / "keys" / "moved.pub"))
check("keys: installing it twice does not duplicate it", pump(10, lambda: "was already installed" in "\n".join(tlog.terminal._line_text(i) for i in range(tlog.terminal._total_lines()))) and authorized.read_text() == public_line + "\n")
authorized.write_bytes(b"ssh-rsa AAAAold older@key")  # no line ending, as some files have
win.install_public_key(tlog, str(plain_key))
check("keys: existing entries are kept, each on its own line", pump(15, lambda: authorized.read_bytes().count(b"\n") == 2) and authorized.read_text().splitlines()[0] == "ssh-rsa AAAAold older@key" and authorized.read_text().splitlines()[1].split()[1] == plain_line.split()[1], ascii(authorized.read_bytes()[:60]))
win.install_public_key(tlog, str(WORK / "keys" / "id_new.keep"))
pump(1.0)
check("keys: a file that is not a key is reported", "is not a key file" in win.statusBar().currentMessage() and "http" not in win.statusBar().currentMessage(), win.statusBar().currentMessage())
shutil_target = SFTP_ROOT / ".ssh"
authorized.unlink()
shutil_target.rmdir()

key_dialog = KeyDialog(win)
key_dialog.show()
key_dialog.path.setText(str(WORK / "keys" / "from dialog"))
key_dialog.passphrase.setText("one")
key_dialog.confirm.setText("two")
key_dialog.generate()
mismatch = key_dialog.message.text()
key_dialog.confirm.setText("one")
key_dialog.generate()
pump(0.3)
check("key dialog: mismatched passphrases are refused, then the key is made and shown", "not the same" in mismatch and key_dialog.created_path == str(WORK / "keys" / "from dialog") and key_dialog.public.text().startswith("ssh-ed25519 ") and (WORK / "keys" / "from dialog.pub").exists() and key_dialog.generate_button.text() == "Done" and key_dialog.copy_button.isEnabled(), mismatch)
key_dialog.grab().save(str(SCRATCH / "shot_key_dialog.png"))
key_dialog.close()

# ---- session notes ------------------------------------------------------------------------------
noted = SessionDialog(win.store, win.store.get(runner.id), win)
noted.notes.setText("Jenkins agent, restart after 02:00 only")
win.store.upsert(noted.session())
noted.close()
win._reload_tree()
runner_item = next(win.tree.topLevelItem(i) for i in range(win.tree.topLevelItemCount()) if win.tree.topLevelItem(i).text(0) == "build-runner")
check("notes: saved with the session and shown when pointing at it", win.store.get(runner.id).notes.startswith("Jenkins agent") and "Jenkins agent" in runner_item.toolTip(0) and "deploy@127.0.0.1" in runner_item.toolTip(0))
win.search.setText("jenkins")
found_by_note = [win.tree.topLevelItem(i).text(0) for i in range(win.tree.topLevelItemCount())]
why_shown = win.tree.topLevelItem(0).data(0, ROLE_SUBTITLE)
win.search.setText("runner")
why_name = win.tree.topLevelItem(0).data(0, ROLE_SUBTITLE)
win.search.clear()
win.search.clear()
check("notes: the sidebar search looks in them", found_by_note == ["build-runner"], repr(found_by_note))
check("notes: a row found only by its note says so, one found by its name shows its address", str(why_shown).startswith("Note: Jenkins") and why_name == runner.address, repr((why_shown, why_name)))

# ---- terminal colour schemes -----------------------------------------------------------------------
from techxterminal.theme import TERMINAL_SCHEMES as _SCHEMES, terminal_colors as _terminal_colors

win.apply_preferences(_replace(win.prefs, terminal_scheme="dracula"))
pump(0.5)
shot = tlog.terminal.grab().toImage()
corner = shot.pixelColor(shot.width() - 40, shot.height() - 20).name()
check("colour scheme: the terminal takes the scheme's colours", _theme.COLORS["term_bg"] == "#282a36" and tlog.terminal._bg.name() == "#282a36" and tlog.terminal._fg.name() == "#f8f8f2" and _theme.ANSI["green"] == "#50fa7b" and corner == "#282a36", corner)
check("colour scheme: the rest of the window keeps the theme", _theme.COLORS["bg"] == _theme._PALETTES["dark"]["bg"] and _theme.active_theme() == "dark")
win.apply_preferences(_replace(win.prefs, theme="light"))
check("colour scheme: it stays when the theme changes", _theme.COLORS["bg"] == _theme._PALETTES["light"]["bg"] and _theme.COLORS["term_bg"] == "#282a36")
win.apply_preferences(_replace(win.prefs, terminal_scheme=""))
check("colour scheme: without one the terminal follows the theme", _theme.COLORS["term_bg"] == _theme._PALETTES["light"]["term_bg"] and _terminal_colors("dark", "nope")[0] == _theme._PALETTES["dark"]["term_bg"] and all(len(scheme[4]) == 16 for scheme in _SCHEMES.values()))
win.apply_preferences(_replace(win.prefs, theme="dark"))
scheme_dialog = SettingsDialog(win.prefs, win)
scheme_dialog.terminal_scheme.setCurrentIndex(scheme_dialog.terminal_scheme.findData("solarized-light"))
check("colour scheme: Settings lists them and previews the choice", scheme_dialog.terminal_scheme.count() == len(_SCHEMES) + 1 and "#fdf6e3" in scheme_dialog.preview.styleSheet() and scheme_dialog.preferences().terminal_scheme == "solarized-light", scheme_dialog.preview.styleSheet())
scheme_dialog.keepalive.setValue(0)
scheme_dialog.restore_tabs.setChecked(True)
got = scheme_dialog.preferences()
check("settings: keep-alive and reopen tabs round-trip", got.keepalive_seconds == 0 and got.restore_tabs is True and scheme_dialog.keepalive.text() == "Off")
scheme_dialog.pages.setCurrentIndex(1)
scheme_dialog.show()
pump(0.3)
scheme_dialog.grab().save(str(SCRATCH / "shot_settings_terminal.png"))
scheme_dialog.close()

# ---- keep-alive -----------------------------------------------------------------------------------
win.apply_preferences(_replace(win.prefs, keepalive_seconds=45))
ka_ssh = win.open_session(win.store.sessions[1], "secret")
ka_raw = win.open_session(Session(name="ka-raw", protocol="raw", host="127.0.0.1", port=RAW_PORT))
pump(20, lambda: ka_ssh.state == "connected" and ka_raw.state == "connected")
check("keep-alive: the interval reaches new connections", ka_ssh._worker.keepalive == 45 and ka_raw._worker._sock.getsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE) != 0, f"{ka_ssh.state} {ka_raw.state}")
win.close_tab(ka_ssh)
win.close_tab(ka_raw)
win.apply_preferences(_replace(win.prefs, keepalive_seconds=0))
ka_off = win.open_session(Session(name="ka-off", protocol="raw", host="127.0.0.1", port=RAW_PORT))
pump(10, lambda: ka_off.state == "connected")
check("keep-alive: Off turns it off", ka_off._worker.keepalive == 0 and ka_off._worker._sock.getsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE) == 0)
win.close_tab(ka_off)
win.apply_preferences(_replace(win.prefs, keepalive_seconds=30))

# ---- full screen ------------------------------------------------------------------------------------


def set_full_screen(wanted):
    """Ask for full screen on or off, and try again if the window manager did not do it: it can take its time or lose the request."""
    for _attempt in range(3):
        if win.isFullScreen() == wanted:
            return True
        win.toggle_full_screen()
        if pump(5, lambda: win.isFullScreen() == wanted):
            return True
    return False


was_full = set_full_screen(True)
back_again = set_full_screen(False)
pump(0.3)
check("full screen: F11 switches it on and off", was_full and back_again, repr((was_full, back_again, int(win.windowState().value))))
win.resize(1240, 780)
win.tabs.setCurrentIndex(0)
pump(0.5)

# ---- per-session terminal options -----------------------------------------------------------
import re as _re2
from PyQt6.QtGui import QContextMenuEvent as _QContextMenuEvent

from PyQt6.QtGui import QKeyEvent as _QKeyEvent

legacy = TerminalWidget(_Prefs())
legacy.resize(700, 220)
legacy.show()
pump(0.2)
legacy.configure_session("iso-8859-1", "bs", "crlf")
legacy.feed(b"caf\xe9 \xfcber\r\n")
legacy.configure_session("cp437", "bs", "crlf")
legacy.feed(b"\xc9\xcd\xbb")
pump(0.2)
check("encoding: bytes are read in the session's character set", legacy._line_text(0).startswith("caf\u00e9 \u00fcber") and legacy._line_text(1).startswith("\u2554\u2550\u2557") and legacy.encoding() == "cp437", ascii(legacy._line_text(0)[:12] + legacy._line_text(1)[:4]))
typed = []
legacy.send.connect(typed.append)
legacy.configure_session("iso-8859-1", "bs", "crlf")
legacy.setFocus()
QTest.keyClick(legacy, Qt.Key.Key_Backspace)
QTest.keyClick(legacy, Qt.Key.Key_Backspace, Qt.KeyboardModifier.ControlModifier)
QTest.keyClick(legacy, Qt.Key.Key_Return)
legacy.keyPressEvent(_QKeyEvent(_QEvent.Type.KeyPress, 0, Qt.KeyboardModifier.NoModifier, "\u00e9"))  # QTest cannot type non-ASCII
legacy.send_text("a\nb")
check("keys: Backspace, Enter and typed characters follow the session's settings", typed == [b"\x08", b"\x7f", b"\r\n", b"\xe9", b"a\r\nb"], repr(typed))
typed.clear()
legacy.configure_session("no-such-charset")
QTest.keyClick(legacy, Qt.Key.Key_Backspace)
QTest.keyClick(legacy, Qt.Key.Key_Return)
legacy.keyPressEvent(_QKeyEvent(_QEvent.Type.KeyPress, 0, Qt.KeyboardModifier.NoModifier, "\u00e9"))  # QTest cannot type non-ASCII
check("keys: the defaults are UTF-8, Delete and CR; an unknown character set falls back", legacy.encoding() == "utf-8" and typed == [b"\x7f", b"\r", b"\xc3\xa9"], repr(typed))
legacy.close()

echo_tab = win.open_session(Session(name="echo", protocol="raw", host="127.0.0.1", port=RAW_PORT, local_echo=True, encoding="iso-8859-1", enter="crlf"))
pump(10, lambda: echo_tab.state == "connected" and "RAW READY" in text(echo_tab))
echo_tab.terminal.setFocus()
QTest.keyClicks(echo_tab.terminal, "hi")
pump(1.0)
shown = text(echo_tab)
check("local echo: typed characters appear even though the host only sends its own", "h" in shown.replace("RAW READY", "") and "i" in shown.replace("RAW READY", "") and "H" in shown.replace("RAW READY", "") and echo_tab.bytes_out == 2, ascii(shown.strip()[-12:]))
pump(1.2)
check("status bar: shows the character set, time connected and traffic", "ISO-8859-1" in win.status_right.text() and bool(_re2.search(r"\d\d:\d\d:\d\d", win.status_right.text())) and "\u2193" in win.status_right.text() and "\u2191 2 B" in win.status_right.text(), ascii(win.status_right.text()))
first_time = _re2.search(r"\d\d:\d\d:\d\d", win.status_right.text()).group()
pump(2.2)
check("status bar: the time keeps counting", _re2.search(r"\d\d:\d\d:\d\d", win.status_right.text()).group() != first_time)
echo_tab.disconnect_from_host()
pump(0.3)
check("status bar: no time or traffic while disconnected", not _re2.search(r"\d\d:\d\d:\d\d", win.status_right.text()) and echo_tab.duration() == 0)
win.close_tab(echo_tab)

options = SessionDialog(win.store, None, win)
options.show()
pump(0.2)
options.host.setText("legacy.example")
options.username.setText("op")
options.encoding.setCurrentText("CP850")
options.backspace.setCurrentIndex(options.backspace.findData("bs"))
options.enter_key.setCurrentIndex(options.enter_key.findData("crlf"))
options.local_echo.setChecked(True)
made = options.session()
check("session dialog: terminal options are saved", (made.encoding, made.backspace, made.enter, made.local_echo) == ("cp850", "bs", "crlf", True) and options.terminal_section.isVisible(), repr(made))
options.encoding.setCurrentText("klingon-8")
options._finish(False)
check("session dialog: an unknown character set is refused", "Unknown encoding" in options.error.text() and options.isVisible())
options.grab().save(str(SCRATCH / "shot_dialog.png"))
options.close()

# ---- keyword highlighting ---------------------------------------------------------------------


def colour_pixels(widget, colour, row, first, last):
    """How many pixels of some cells are (nearly) a given colour."""
    image = widget.grab().toImage()
    scale = image.devicePixelRatio()
    wanted = _QColor(colour)
    count = 0
    for y in range(int((_PAD + row * widget._ch) * scale), int((_PAD + (row + 1) * widget._ch) * scale)):
        for x in range(int((_PAD + first * widget._cw) * scale), int((_PAD + (last + 1) * widget._cw) * scale)):
            pixel = image.pixelColor(x, y)
            if abs(pixel.red() - wanted.red()) + abs(pixel.green() - wanted.green()) + abs(pixel.blue() - wanted.blue()) < 40:
                count += 1
    return count


sample = b"ERROR: nginx failed, warning; all passed\r\n\x1b[32mERROR printed in green\x1b[0m"
plain = TerminalWidget(_Prefs())
marked = TerminalWidget(_replace(_Prefs(), highlight_keywords=True, highlight_words="nginx"))
for widget in (plain, marked):
    widget.resize(700, 200)
    widget.show()
    widget.feed(sample)
pump(0.4)
red, amber, green, accent = (_theme.COLORS[key] for key in ("red", "amber", "green", "accent"))
check("highlight: off by default", colour_pixels(plain, red, 0, 0, 4) == 0 and colour_pixels(plain, amber, 0, 21, 27) == 0)
check("highlight: errors red, warnings amber, successes green, your own words in the accent colour", colour_pixels(marked, red, 0, 0, 4) > 20 and colour_pixels(marked, red, 0, 13, 18) > 20 and colour_pixels(marked, amber, 0, 21, 27) > 20 and colour_pixels(marked, green, 0, 34, 39) > 20 and colour_pixels(marked, accent, 0, 7, 11) > 20, repr([colour_pixels(marked, red, 0, 0, 4), colour_pixels(marked, amber, 0, 21, 27), colour_pixels(marked, green, 0, 34, 39), colour_pixels(marked, accent, 0, 7, 11)]))
check("highlight: text a program coloured itself is left alone", colour_pixels(marked, red, 1, 0, 4) == 0 and colour_pixels(marked, _theme.ANSI["green"], 1, 0, 4) > 20)
marked.apply_preferences(_replace(_Prefs(), highlight_keywords=False))
pump(0.3)
check("highlight: switching it off restores plain text", colour_pixels(marked, red, 0, 0, 4) == 0)
plain.close()
marked.close()

# ---- bell ---------------------------------------------------------------------------------------
ringer = TerminalWidget(_Prefs())
rings = []
ringer.bell.connect(lambda: rings.append(1))
ringer.feed(b"ding\x07")
flashed = ringer._flash_until > time.monotonic()
quiet = TerminalWidget(_replace(_Prefs(), visual_bell=False))
quiet.bell.connect(lambda: rings.append(2))
quiet.feed(b"\x07")
check("bell: it is noticed and flashes the terminal, unless the visual bell is off", rings == [1, 2] and flashed and quiet._flash_until == 0.0, repr(rings))
win.tabs.setCurrentIndex(win.tabs.indexOf(tab1))
pump(0.2)
tlog.terminal.feed(b"\x07")
pump(0.2)
check("bell: a background tab that rings is marked", label(tlog).endswith("\u2022") and not label(tab1).endswith("\u2022"), ascii(label(tlog)))
win.tabs.setCurrentIndex(win.tabs.indexOf(tlog))
pump(0.2)

# ---- right-click paste ----------------------------------------------------------------------------
clicker = TerminalWidget(_replace(_Prefs(), right_click_paste=True))
clicker.resize(400, 200)
clicker.show()
pump(0.2)
pasted = []
clicker.send.connect(pasted.append)
CLIP.setText("from the clipboard")
clicker.contextMenuEvent(_QContextMenuEvent(_QContextMenuEvent.Reason.Mouse, _QPoint(20, 20), clicker.mapToGlobal(_QPoint(20, 20))))
check("right-click paste: the right button pastes when the option is on", pasted == [b"from the clipboard"], repr(pasted))
clicker.close()

# ---- dragging a session to a folder ------------------------------------------------------------------
from PyQt6.QtCore import QMimeData as _QMimeData2
from PyQt6.QtGui import QDropEvent as _QDropEvent2

win._reload_tree()
tops = [win.tree.topLevelItem(i) for i in range(win.tree.topLevelItemCount())]
staging_item = next(item for item in tops if item.text(0) == "Staging")
production_item = next(item for item in tops if item.text(0) == "Production")
loose_item = next(item for item in tops if item.text(0) == "build-runner")
check("drag: a drop lands in the folder under it, or in no folder", win.tree.folder_of(staging_item) == "Staging" and win.tree.folder_of(production_item.child(0)) == "Production" and win.tree.folder_of(loose_item) == "" and win.tree.folder_of(None) == "")
win.tree.setCurrentItem(loose_item)
target_point = win.tree.visualItemRect(staging_item).center()
win.tree.dropEvent(_QDropEvent2(_QPointF(target_point), Qt.DropAction.MoveAction, _QMimeData2(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier))
check("drag: dropping a session on a folder moves it there", win.store.get(runner.id).group == "Staging" and "build-runner" in [session.name for session in win.sessions_in("Staging")] and '"group": "Staging"' in (HOME / ".techxterminal" / "sessions.json").read_text())
win.tree.sessionDropped.emit(runner.id, "")
check("drag: dropping it outside any folder takes it out", win.store.get(runner.id).group == "")

# ---- settings for these ------------------------------------------------------------------------------
behaviour = SettingsDialog(win.prefs, win)
behaviour.set_preferences(_replace(win.prefs, right_click_paste=True, visual_bell=False, highlight_keywords=True, highlight_words="nginx, core"))
got = behaviour.preferences()
check("settings: the new terminal options round-trip", (got.right_click_paste, got.visual_bell, got.highlight_keywords, got.highlight_words) == (True, False, True, "nginx, core"))
behaviour.applied.connect(win.apply_preferences)
behaviour._apply()
pump(0.3)
check("settings: they reach open terminals", tlog.terminal._right_click_paste and not tlog.terminal._visual_bell and tlog.terminal._highlight is not None and Preferences.load().highlight_words == "nginx, core")
behaviour.close()
win.apply_preferences(_replace(win.prefs, right_click_paste=False, visual_bell=True, highlight_keywords=False, highlight_words=""))
win.tabs.setCurrentIndex(0)
pump(0.3)

import techxterminal.app as _app_module_early

# ---- favourites ------------------------------------------------------------------------------
from techxterminal.sidebar import FAVOURITES as _FAVOURITES


def headings():
    return [win.tree.topLevelItem(i).text(0) for i in range(win.tree.topLevelItemCount())]


def under(name):
    item = next(win.tree.topLevelItem(i) for i in range(win.tree.topLevelItemCount()) if win.tree.topLevelItem(i).text(0) == name)
    return [item.child(j).text(0) for j in range(item.childCount())]


check("favourites: no heading until something is starred", _FAVOURITES not in headings())
win.set_favourite(win.store.get(prod.id), True)
check("favourites: a starred session is listed first, and stays in its folder", headings()[0] == _FAVOURITES and under(_FAVOURITES) == ["web-01"] and "web-01" in under("Production") and [session.name for session in win.sessions_in(_FAVOURITES)] == ["web-01"] and win.store.get(prod.id).favourite, repr(headings()[:3]))
win._session_dropped(runner.id, _FAVOURITES)
check("favourites: dropping a session on the heading stars it without moving it", win.store.get(runner.id).favourite and win.store.get(runner.id).group == "" and under(_FAVOURITES) == ["build-runner", "web-01"])
win.search.setText("runner")
check("favourites: the search covers them too", headings() == [_FAVOURITES, "build-runner"] and under(_FAVOURITES) == ["build-runner"], repr(headings()))
win.search.clear()
win.set_favourite(win.store.get(prod.id), False)
win.set_favourite(win.store.get(runner.id), False)
check("favourites: the heading goes away with the last star", _FAVOURITES not in headings() and not win.store.get(runner.id).favourite)
win.search.setText("prod web-01")
check("sidebar search: each word may match a different part, in any order", headings() == ["Production"] and under("Production") == ["web-01"], repr(headings()))
win.search.clear()

# ---- more tab management -----------------------------------------------------------------------
extra = [win.open_session(Session(name=f"extra-{letter}", protocol="raw", host="127.0.0.1", port=RAW_PORT)) for letter in "abc"]
pump(15, lambda: all(tab.state == "connected" for tab in extra))
kept = len(win.open_tabs()) - 3
extra[1].disconnect_from_host()
closed = win.close_disconnected()
check("tabs: Close Disconnected Tabs closes exactly those", closed == 1 and len(win.open_tabs()) == kept + 2 and win._pane_of(extra[1]) is None)
extra[2].disconnect_from_host()
reconnected = win.reconnect_disconnected()
check("tabs: Reconnect Disconnected Tabs brings them back", reconnected == 1 and pump(10, lambda: extra[2].state == "connected"))
win.close_tabs_right(extra[0])
check("tabs: Close Tabs to the Right keeps the tab and those before it", len(win.open_tabs()) == kept + 1 and win._pane_of(extra[0]) is not None and win._pane_of(extra[2]) is None)
win.close_tab(extra[0])

# ---- test connection -----------------------------------------------------------------------------
from techxterminal.connections import probe as _probe

answers = _probe(Session(host="127.0.0.1", port=PORT))
refused = _probe(Session(host="127.0.0.1", port=1))
unknown = _probe(Session(host="no-such-host.invalid"), timeout=3)
loop_ok = _probe(Session(protocol="serial", host="loop://", port=9600))
no_port = _probe(Session(protocol="serial", host="COM_DOES_NOT_EXIST", port=9600))
check("test connection: reachable, refused, unknown name and serial lines are told apart", answers[0] and "answers on port" in answers[1] and " ms)" in answers[1] and not refused[0] and "refused" in refused[1] and not unknown[0] and "cannot be found" in unknown[1] and loop_ok[0] and not no_port[0], repr((answers[1], refused[1], unknown[1], no_port[1][:40])))
tester = SessionDialog(win.store, None, win)
tester.show()
tester.test_connection()
nothing_entered = tester.error.text()
tester.host.setText("127.0.0.1")
tester.port.setValue(PORT)
tester.username.setText("deploy")
tester.test_connection()
busy = not tester.test_button.isEnabled()
check("test connection: the Test button reports in the dialog", nothing_entered == "Enter a server name first" and busy and pump(10, lambda: "answers on port" in tester.error.text()) and tester.test_button.isEnabled(), tester.error.text())
tester.close()

# ---- command palette -------------------------------------------------------------------------------
names = [title for title, _keys2, _handler in win.commands()]
check("palette: the application's commands are offered", {"New Session", "Settings", "File Browser", "Back Up Everything", "Close Disconnected Tabs"} <= set(names) and len(names) == len(set(names)) and "Switch to Light Theme" in names, repr(names[:4]))
palette = win.show_switcher()
palette.query.setText("light theme")
only = palette.shown()
QTest.keyClick(palette.query, Qt.Key.Key_Return)
pump(0.5)
check("palette: typing finds a command and Enter runs it", only == [("action", "Switch to Light Theme")] and _theme.active_theme() == "light" and win.prefs.theme == "light" and "Switch to Dark Theme" in [title for title, _k, _h in win.commands()], repr(only))
win.toggle_theme()
pump(0.4)
check("palette: and back", _theme.active_theme() == "dark")

# ---- top bar -------------------------------------------------------------------------------------
dark_tip = win.theme_button.toolTip()
win.theme_button.click()
pump(0.4)
check("top bar: the theme button switches theme and says which it switches to", "light" in dark_tip and _theme.active_theme() == "light" and "dark" in win.theme_button.toolTip(), dark_tip)
check("title bar: the main window's takes the top bar's colour, in the theme just applied", win.property("titleBarColor") == _theme.COLORS["surface"], repr(win.property("titleBarColor")))
win.theme_button.click()
pump(0.4)
win.tree.setFocus()
win.quick.clear()
pump(0.1)
idle_hint = win.quick.key_hint.isVisible() and win.quick.key_hint.text() == "Ctrl+Shift+K"
win.quick.setText("deploy@")  # typing, not focus: Windows gives no focus to a window behind another
pump(0.1)
check("top bar: quick connect shows its shortcut while idle, not while in use", idle_hint and not win.quick.key_hint.isVisible())
win.quick.clear()
check("sidebar: the session search shows its shortcut as a key too, and the start page lists keys as keys", win.search.key_hint.text() == "Ctrl+Shift+S" and {"Ctrl+Shift+N", "Ctrl+Shift+K"} <= {label.text() for label in win.shortcut_card.findChildren(type(win.brand_text)) if label.objectName() == "KeyCap"})
quick_middle = win.quick.mapTo(win, win.quick.rect().center()).x()
check("top bar: quick connect is in the middle of the window", abs(quick_middle - win.width() / 2) <= 4, f"{quick_middle} of {win.width()}")

# ---- sessions named on the command line ---------------------------------------------------------------
import subprocess as _subprocess

by_name = win.open_target("WEB-02")
by_address = win.open_target(f"raw://127.0.0.1:{RAW_PORT}")
check("command line: a saved session by name, any case, or an address", by_name is not None and by_name.session.name == "web-02" and by_address is not None and by_address.session.protocol == "raw" and by_address.session.port == RAW_PORT)
not_understood = win.open_targets(["no such session here", f"127.0.0.1:{RAW_PORT}"])
from_list = win.current_tab()
check("command line: what cannot be opened is reported, the rest is opened", not_understood == ["no such session here"] and "no such session here" in win.statusBar().currentMessage() and from_list.session.port == RAW_PORT)
for tab in (by_name, by_address, from_list):
    win.close_tab(tab)
quick = _subprocess.run(
    [sys.executable, "-c", "import sys; import techxterminal.mainwindow; from techxterminal.sessions import SessionStore; "
     "ok = SessionStore.can_store_passwords(); before = 'keyring' in sys.modules; "
     "from techxterminal.sessions import Session; SessionStore.get_password(Session(name='x', save_password=True)); "
     "print(sys.modules.get('invoke', 'absent') is None, before, 'keyring' in sys.modules, ok)"],
    capture_output=True, text=True, timeout=120, env=dict(os.environ, PYTHONPATH=str(ROOT)),
)
check("start-up: invoke and keyring are not loaded until they are needed", quick.stdout.split() == ["True", "False", "True", "True"], quick.stdout + quick.stderr[-300:])
from techxterminal import paths as _paths

portable_program = WORK / "portable-copy"
portable_program.mkdir()
fake_exe = portable_program / "TechXTerminal.exe"
fake_exe.write_bytes(b"")
check("portable: without a data folder next to it the program keeps its files where it always did", _paths.portable_folder(portable_program) is None and _paths.data_directory(portable_program) == Path.home() / ".techxterminal")
(portable_program / "TechXTerminal-data").write_text("a file, not a folder")
check("portable: a file with that name does not count", _paths.portable_folder(portable_program) is None)
(portable_program / "TechXTerminal-data").unlink()
(portable_program / "TechXTerminal-data").mkdir()
check("portable: a data folder next to the program is where its files go", _paths.portable_folder(portable_program) == portable_program / "TechXTerminal-data" and _paths.data_directory(portable_program) == portable_program / "TechXTerminal-data")
portable_code = (
    "import sys; sys.frozen = True; sys.executable = sys.argv[1]; "
    "from techxterminal import paths; from techxterminal.preferences import Preferences, open_settings; "
    "prefs = Preferences(); prefs.scrollback = 1234; prefs.save(); "
    "print(paths.CONFIG_DIR, open_settings().fileName(), prefs.log_directory(), sep='|')"
)
portable_env = {key: value for key, value in os.environ.items() if key != "TECHX_SETTINGS_FILE"}
portable_run = _subprocess.run([sys.executable, "-c", portable_code, str(fake_exe)], capture_output=True, text=True, timeout=120, env=dict(portable_env, PYTHONPATH=str(ROOT)))
shown_config, shown_settings, shown_logs = (portable_run.stdout.strip().split("|") + ["", "", ""])[:3]
data_folder = portable_program / "TechXTerminal-data"
check("portable: sessions, preferences and logs are kept in that folder", Path(shown_config) == data_folder and Path(shown_settings) == data_folder / "settings.ini" and Path(shown_logs) == data_folder / "logs" and "scrollback=1234" in (data_folder / "settings.ini").read_text(), portable_run.stdout + portable_run.stderr[-300:])
plain_code = "import sys; sys.frozen = True; sys.executable = sys.argv[1]; from techxterminal import paths; print(paths.CONFIG_DIR)"
plain_run = _subprocess.run([sys.executable, "-c", plain_code, str(WORK / "somewhere-else" / "TechXTerminal.exe")], capture_output=True, text=True, timeout=120, env=dict(portable_env, PYTHONPATH=str(ROOT)))
check("portable: a copy with no data folder next to it uses the user's folder", Path(plain_run.stdout.strip()) == Path.home() / ".techxterminal", plain_run.stdout + plain_run.stderr[-300:])
listing = _subprocess.run([sys.executable, str(ROOT / "main.py"), "--list"], capture_output=True, text=True, timeout=60, env=dict(os.environ))
usage = _subprocess.run([sys.executable, str(ROOT / "main.py"), "--help"], capture_output=True, text=True, timeout=60, env=dict(os.environ))
from techxterminal import __version__ as _version

version_run = _subprocess.run([sys.executable, str(ROOT / "main.py"), "--version"], capture_output=True, text=True, timeout=60, env=dict(os.environ))
check("command line: --version prints the name and version, and --help lists it", version_run.stdout.strip() == f"Tech X Terminal {_version}" and version_run.returncode == 0 and "--version" in usage.stdout, version_run.stdout[:60])

# One copy at a time: this process listens as the running copy would, and a second start hands over to it.
from techxterminal import instance as _instance

running_copy = _instance.InstanceServer()
handed = []
running_copy.activated.connect(handed.append)
second = _subprocess.Popen([sys.executable, str(ROOT / "main.py"), "web-02", "raw://127.0.0.1:1"], env=dict(os.environ))
pump(30, lambda: second.poll() is not None and handed)
second_code = second.poll()
if second_code is None:
    second.kill()
check("one copy: a second start hands its sessions to the copy running and ends", running_copy.is_listening() and second_code == 0 and handed == [["web-02", "raw://127.0.0.1:1"]], repr((second_code, handed)))
tasks_while_open = _subprocess.run([sys.executable, str(ROOT / "main.py"), "--tasks"], capture_output=True, text=True, timeout=60, env=dict(os.environ))
check("one copy: the options that open no window still work while it runs", tasks_while_open.returncode == 0 and handed == [["web-02", "raw://127.0.0.1:1"]], tasks_while_open.stderr[-300:])
forward_tabs = len(win.open_tabs())
win.showMinimized()
pump(0.3)
win.come_forward(["web-02"])
pump(0.5)
check("one copy: the window it hands over to comes back from the taskbar and opens the session", not win.isMinimized() and len(win.open_tabs()) == forward_tabs + 1)
win.close_tab(win.open_tabs()[-1], force=True)
running_copy.server.close()
check("command line: --list prints the saved sessions and --help the usage, without opening a window","web-01\t" in listing.stdout and "db-primary" in listing.stdout and listing.returncode == 0 and usage.stdout.startswith("Usage:"), listing.stdout[:60] + listing.stderr[:120])

# ---- a shell on this computer, in a tab ------------------------------------------------------------------
from techxterminal import local as _local
from techxterminal.sessions import PROTO_LOCAL

parsed_local = win.parse_quick_connect("local://cmd.exe")
default_local = win.parse_quick_connect("Local")
check("local shell: quick connect understands local and local://program", parsed_local.protocol == PROTO_LOCAL and parsed_local.host == "cmd.exe" and parsed_local.title == "Command Prompt" and parsed_local.address == "local://cmd.exe" and default_local.protocol == PROTO_LOCAL and default_local.host == _local.default_shell(), repr((parsed_local, default_local.host)))
shell_names = [name for name, _command in _local.available_shells()]
check("local shell: the shells of this computer are found, PowerShell first", "Command Prompt" in shell_names and shell_names[0] in ("PowerShell", "Windows PowerShell"), repr(shell_names))
check("local shell: a program that is not there is not found", _local.find_program("no-such-shell.exe") is None and _local.find_program('"C:\\Windows\\System32\\cmd.exe" /k') is not None)
local_tab = win.open_target("local://cmd.exe")
check("local shell: the Command Prompt opens in a tab and is connected", pump(15, lambda: local_tab.state == "connected"), local_tab.state)
pump(15, lambda: ">" in text(local_tab))
run(local_tab, "echo local-shell-%COMPUTERNAME%-ok", wait=0.2)
check("local shell: what is typed reaches the shell and its output is shown", pump(10, lambda: f"local-shell-{os.environ['COMPUTERNAME']}-ok" in text(local_tab)), text(local_tab)[-300:])
local_tab.terminal.resize(600, 400)
pump(0.5)
run(local_tab, "mode con", wait=0.2)
check("local shell: the shell is told the size of the tab", pump(10, lambda: f"Columns:        {local_tab.terminal.cols}" in text(local_tab)), text(local_tab)[-400:])
check("local shell: Connection Info names the program", ("Program", "cmd.exe") in local_tab._worker.details())
run(local_tab, "exit 4", wait=0.2)
check("local shell: when the shell ends the tab says so and can be started again", pump(10, lambda: local_tab.state == "disconnected") and "exit code 4" in text(local_tab), text(local_tab)[-200:])
local_tab.connect_to_host()
check("local shell: Reconnect starts it again", pump(15, lambda: local_tab.state == "connected"))
win.remember_open_tabs()
remembered_local = [str(entry) for entry in win.settings.value("open_sessions")]
win.close_tab(local_tab)
reopened_all = win.restore_tabs()
reopened_local = [tab for tab in reopened_all if tab.session.protocol == PROTO_LOCAL]
check("local shell: an open shell is remembered for Reopen Tabs at Start, and opened again", "local://cmd.exe" in remembered_local and len(reopened_local) == 1 and reopened_local[0].session.host == "cmd.exe" and pump(15, lambda: reopened_local[0].state == "connected"), repr(remembered_local))
for tab in reopened_all:
    win.close_tab(tab)
missing_tab = win.open_session(_local.local_session("no-such-shell.exe"))
check("local shell: a program that is not there is reported in the tab", pump(10, lambda: missing_tab.state == "disconnected") and "Program not found: no-such-shell.exe" in text(missing_tab), text(missing_tab)[-200:])
win.close_tab(missing_tab)
start_here = WORK / "start here"
start_here.mkdir()
folder_tab = win.open_session(_local.local_session("cmd.exe", folder=str(start_here)))
check("local shell: it starts in the folder the session names", pump(15, lambda: folder_tab.state == "connected") and pump(10, lambda: f"{start_here}>" in text(folder_tab)) and ("Started in", str(start_here)) in folder_tab._worker.details(), text(folder_tab)[-200:])
win.close_tab(folder_tab)
os.environ["TECHX_TEST_FOLDER"] = str(start_here)
check("local shell: %VARIABLES% in the start folder are filled in, and an empty one is the user's folder", _local.start_folder(_local.local_session("cmd.exe", folder="%TECHX_TEST_FOLDER%")) == start_here and _local.start_folder(_local.local_session("cmd.exe")) == Path.home())
gone_folder_tab = win.open_session(_local.local_session("cmd.exe", folder=str(WORK / "not a folder")))
check("local shell: a start folder that is not there is reported in the tab", pump(10, lambda: gone_folder_tab.state == "disconnected") and "The start folder is not there" in text(gone_folder_tab), text(gone_folder_tab)[-200:])
win.close_tab(gone_folder_tab)
local_dialog = SessionDialog(win.store, None, win)
check("local shell: the session window offers a start folder only for local shells", not local_dialog.start_folder_row.isVisibleTo(local_dialog))
local_dialog._protocol_buttons[PROTO_LOCAL].setChecked(True)
local_dialog.host.setText("no-such-shell.exe")
local_dialog._finish(False)
refused = local_dialog.error.text()
local_dialog.host.setText("cmd.exe")
local_dialog.start_folder.setText(str(WORK / "not a folder"))
local_dialog._finish(False)
refused_folder = local_dialog.error.text()
local_dialog.start_folder.setText(str(start_here))
local_dialog.encoding.setCurrentText("cp437")
local_dialog._finish(False)
saved_local = local_dialog.session()
check("local shell: the session window asks for a program instead of a server, and checks it is there", local_dialog.host_label.text() == "Program" and not local_dialog.logon_section.isVisibleTo(local_dialog) and not local_dialog.port.isVisibleTo(local_dialog) and refused == "There is no such program on this computer" and local_dialog.result() == 1 and saved_local.protocol == PROTO_LOCAL and saved_local.encoding == "utf-8" and local_dialog.start_folder_row.isVisibleTo(local_dialog) and refused_folder == "The start folder is not there" and saved_local.start_folder == str(start_here), refused + " / " + refused_folder)
local_dialog.deleteLater()

# ---- scheduled tasks -------------------------------------------------------------------------------------
from datetime import datetime as _datetime, timedelta as _timedelta

from PyQt6.QtCore import QTime
from techxterminal import tasks as _tasks
from techxterminal.dialogs import TasksDialog

task_server = Session(name="task-box", host="127.0.0.1", port=PORT, username="deploy", command_line="-pw secret")
win.store.sessions.append(task_server)
win.store.save()  # the command line, further down, reads it from the file
ssh_task = _tasks.Task(name="Nightly checks", target=task_server.id, commands="uptime\n# not run\n\nfail\ndf -h", append=False)
ssh_result = _tasks.run_task(ssh_task, win.store)
ssh_output = ssh_result.path.read_text(encoding="utf-8") if ssh_result.path else ""
check("tasks: the commands run on the server one after another, and the output file has each with its exit status", "$ uptime\nran: uptime\nsecond line\n[exit status 0]" in ssh_output and "$ fail\nran: fail\nsecond line\nsomething went wrong\n[exit status 3]" in ssh_output and "$ df -h\nran: df -h" in ssh_output and "not run" not in ssh_output, ssh_output[-500:])
check("tasks: the result says how many failed, and the file is named after the task", not ssh_result.ok and ssh_result.summary == "1 of 3 commands failed" and ssh_result.path == HOME / ".techxterminal" / "task-output" / "Nightly checks.txt" and ssh_output.startswith("===== Nightly checks on task-box, "), ssh_result.summary)
ssh_task.stop_on_error = True
ssh_task.append = True
ssh_task.output_file = str(WORK / "reports" / "{name}-{date}.txt")
stopped_result = _tasks.run_task(ssh_task, win.store)
stopped_output = stopped_result.path.read_text(encoding="utf-8")
check("tasks: {name} and {date} are filled in, a folder that is not there is made, and stopping at the first failure skips the rest", stopped_result.path == WORK / "reports" / f"Nightly checks-{_datetime.now():%Y-%m-%d}.txt" and "Stopped at the first failure: fail" in stopped_result.summary and "$ df -h" not in stopped_output, stopped_result.summary)
_tasks.run_task(ssh_task, win.store)
check("tasks: each run is added to the end of the file", stopped_result.path.read_text(encoding="utf-8").count("===== Nightly checks on task-box") == 2)
slow_task = _tasks.Task(name="slow", target=task_server.id, commands="sleep\nuptime", timeout=2)
slow_began = time.monotonic()
slow_result = _tasks.run_task(slow_task, win.store)
check("tasks: a command that runs past the time limit is stopped and the task carries on", time.monotonic() - slow_began < 15 and "(stopped after 2 s)" in slow_result.path.read_text(encoding="utf-8") and "ran: uptime" in slow_result.path.read_text(encoding="utf-8"), slow_result.summary)
unknown_host = Session(name="unknown", host="localhost", port=PORT, username="deploy", command_line="-pw secret")
win.store.sessions.append(unknown_host)
unknown_result = _tasks.run_task(_tasks.Task(name="unknown", target=unknown_host.id, commands="uptime"), win.store)
check("tasks: an unknown host key is not trusted with nobody there to approve it", not unknown_result.ok and "Host key was not accepted" in unknown_result.summary, unknown_result.summary)
no_password = Session(name="no-password", host="127.0.0.1", port=PORT, username="deploy")
win.store.sessions.append(no_password)
asked_before = seen["password"]
no_password_result = _tasks.run_task(_tasks.Task(name="nopw", target=no_password.id, commands="uptime"), win.store)
check("tasks: a server that wants a password that is not saved fails without asking anyone", not no_password_result.ok and seen["password"] == asked_before and "cancelled" in no_password_result.summary.lower(), no_password_result.summary)
gone_result = _tasks.run_task(_tasks.Task(name="gone", target="0" * 32, commands="uptime"), win.store)
check("tasks: a task whose session was deleted says so", not gone_result.ok and "gone" in gone_result.summary and "(a deleted session)" in gone_result.path.read_text(encoding="utf-8"))

cmd_task = _tasks.Task(name="local cmd", target=_tasks.TARGET_CMD, commands='echo "quoted & joined" && ver\nexit 5', append=False)
cmd_result = _tasks.run_task(cmd_task, win.store)
cmd_output = cmd_result.path.read_text(encoding="utf-8")
check("tasks: commands run in the Command Prompt here, with && and quotes as typed", '"quoted & joined"' in cmd_output and "Microsoft Windows" in cmd_output and "[exit status 5]" in cmd_output and cmd_result.summary == "1 of 2 commands failed", cmd_output[-300:])
ps_task = _tasks.Task(name="local ps", target=_tasks.TARGET_POWERSHELL, commands="Write-Output \"it's $(1 + 1)\"\nGet-Item C:\\no-such-folder-here", append=False)
ps_result = _tasks.run_task(ps_task, win.store)
ps_output = ps_result.path.read_text(encoding="utf-8")
check("tasks: PowerShell commands run here, and their errors are text, not XML", "it's 2\n[exit status 0]" in ps_output and "Cannot find path" in ps_output and "CLIXML" not in ps_output and "[exit status 1]" in ps_output, ps_output[-400:])

cmd_script = _tasks.Task(name="cmd script", target=_tasks.TARGET_CMD, commands=f'cd /d "{start_here}"\ncd\nset WORD=kept\necho %WORD%\nexit /b 4\necho after', as_script=True, append=False)
cmd_script_result = _tasks.run_task(cmd_script, win.store)
cmd_script_output = cmd_script_result.path.read_text(encoding="utf-8")
check("one script: in the Command Prompt a cd and a variable hold for the lines after them, and the script's exit status is the run's", f"{start_here}\nkept\n[exit status 4]" in cmd_script_output and "$ echo after\n" in cmd_script_output and cmd_script_output.count("[exit status") == 1 and cmd_script_result.summary == "The script failed (exit status 4)", cmd_script_output[-300:])
ps_script = _tasks.Task(name="ps script", target=_tasks.TARGET_POWERSHELL, commands=f"Set-Location '{start_here}'\n(Get-Location).Path\nGet-Item C:\\no-such-folder-here\n'not reached'", as_script=True, stop_on_error=True, append=False)
ps_script_result = _tasks.run_task(ps_script, win.store)
ps_script_output = ps_script_result.path.read_text(encoding="utf-8")
check("one script: in PowerShell a Set-Location holds, and stopping at the first failure ends the script there", f"{start_here}\n" in ps_script_output and "Cannot find path" in ps_script_output and "not reached\n[" not in ps_script_output and not ps_script_result.ok, ps_script_output[-300:])
executed_before = sum(len(getattr(server, "executed", [])) for server in servers)
ssh_script = _tasks.Task(name="ssh script", target=task_server.id, commands="cd /var/log\ntail -n 5 syslog", as_script=True, stop_on_error=True, append=False)
ssh_script_result = _tasks.run_task(ssh_script, win.store)
sent_scripts = [command for server in servers for command in getattr(server, "executed", [])][executed_before:]
check("one script: on a server the lines go as one command, which set -e stops at the first failure", sent_scripts == ["set -e\ncd /var/log\ntail -n 5 syslog"] and ssh_script_result.ok and ssh_script_result.summary == "The script succeeded", repr(sent_scripts))
check("one script: lines starting with # are left out of it", _tasks.task_script(_tasks.Task(commands="a\n# b\nc"), "ssh") == "a\nc")

toasts = []
win.notifier.show = lambda title, message, on_click=None: toasts.append((title, message)) or True
real_active = win.isActiveWindow
failing = _tasks.Task(name="failing", target=_tasks.TARGET_CMD, commands="exit 3")
win.isActiveWindow = lambda: True
win.scheduler.run(failing)
pump(15, lambda: not win.scheduler.is_running(failing.id))
check("notify: no desktop notification while the window is in front: the status bar says it", toasts == [] and "Task “failing”: 1 of 1 command failed" in win.statusBar().currentMessage(), repr(toasts))
win.isActiveWindow = lambda: False
win.scheduler.run(failing)
pump(15, lambda: not win.scheduler.is_running(failing.id))
check("notify: a run that fails while the window is not in front is told by the desktop", toasts == [("Task failed: failing", "1 of 1 command failed")], repr(toasts))
win.scheduler.run(_replace(failing, notify_on_failure=False))
pump(15, lambda: not win.scheduler.is_running(failing.id))
win.scheduler.run(_replace(failing, commands="exit 0"))
pump(15, lambda: not win.scheduler.is_running(failing.id))
check("notify: not when the task says not to, nor for a run that succeeds", len(toasts) == 1, repr(toasts))
win.isActiveWindow = real_active
del win.notifier.show
from techxterminal import notices as _notices
import techxterminal.app as _app_for_notices

cli_toasts = []
real_show = _notices.DesktopNotifier.show
_notices.DesktopNotifier.show = lambda self, title, message, on_click=None: cli_toasts.append((title, message)) and False
_app_for_notices.notify_failure("by windows", _tasks.TaskResult(False, "2 of 3 commands failed"))
_notices.DesktopNotifier.show = real_show
check("notify: a task run by Windows that fails is told by the desktop too", cli_toasts == [("Task failed: by windows", "2 of 3 commands failed")], repr(cli_toasts))

big_output = WORK / "big-output.txt"
big_output.write_text("".join(f"===== run {number} on x, today =====\n" + "line\n" * 50 + "----- ok -----\n\n" for number in range(200)), encoding="utf-8")
trimmed = _tasks.trim_output(big_output, limit=20_000)
trimmed_text = big_output.read_text(encoding="utf-8")
first_run_kept = trimmed_text.split("\n\n", 1)[1] if "\n\n" in trimmed_text else ""
check("tasks: an output file that is added to is cut back, oldest runs first and whole, once it is too big", trimmed and big_output.stat().st_size <= 20_000 and "===== run 199 on" in trimmed_text and "===== run 0 on" not in trimmed_text and first_run_kept.startswith("===== run ") and trimmed_text.startswith("(Older runs were removed") and not _tasks.trim_output(big_output, limit=20_000), trimmed_text[:120])

now = time.time()
interval_task = _tasks.Task(name="every", schedule=_tasks.SCHEDULE_INTERVAL, every_minutes=10, commands="x", created=now - 60)
check("tasks: an interval task is due that many minutes after it last ran, or was made", not _tasks.is_due(interval_task, now) and _tasks.is_due(interval_task, now + 9 * 60 + 1) and _tasks.next_run(_replace(interval_task, last_run=now)) == now + 600)
morning = _datetime.now().replace(hour=9, minute=30, second=0, microsecond=0)
daily_task = _tasks.Task(name="daily", schedule=_tasks.SCHEDULE_DAILY, daily_at="09:30", commands="x", created=morning.timestamp() - 3600)
check("tasks: a daily task is due at its time, and next on the day after once it has run", _tasks.next_run(daily_task) == morning.timestamp() and _tasks.next_run(_replace(daily_task, last_run=morning.timestamp() + 5)) == (morning + _timedelta(days=1)).timestamp())
check("tasks: a task run by hand, paused, or run by Windows is not the application's to start", all(_tasks.next_run(_replace(interval_task, **change)) is None for change in ({"schedule": _tasks.SCHEDULE_MANUAL}, {"enabled": False}, {"runner": _tasks.RUNNER_WINDOWS}, {"commands": "# only a comment"})))
windows_interval = _tasks.schtasks_create_arguments(_replace(interval_task, runner=_tasks.RUNNER_WINDOWS))
windows_daily = _tasks.schtasks_create_arguments(_replace(daily_task, runner=_tasks.RUNNER_WINDOWS))
try:
    _tasks.schtasks_create_arguments(_replace(interval_task, schedule=_tasks.SCHEDULE_MANUAL))
    manual_refused = False
except ValueError:
    manual_refused = True
check("tasks: Windows Task Scheduler is asked to start this program with --run-task on the same schedule", windows_interval[:4] == ["/Create", "/F", "/TN", "\\TechXTerminal\\every"] and windows_interval[-4:] == ["/SC", "MINUTE", "/MO", "10"] and windows_interval[5].endswith(f"--run-task {interval_task.id}") and "main.py" in windows_interval[5] and windows_daily[-4:] == ["/SC", "DAILY", "/ST", "09:30"] and manual_refused, repr(windows_interval))

scheduled = _tasks.Task(name="from the app", target=_tasks.TARGET_CMD, commands="echo scheduled-run", schedule=_tasks.SCHEDULE_INTERVAL, every_minutes=5, created=time.time() - 400)
check("tasks: saving them keeps them in the data folder", win.save_tasks([scheduled]) == [] and "from the app" in (HOME / ".techxterminal" / "tasks.json").read_text(encoding="utf-8"))
started_now = win.scheduler.check()
check("tasks: the application starts a task that is due, once", [task.name for task in started_now] == ["from the app"] and win.scheduler.check() == [])
running_tip = win.tasks_button.toolTip()
check("tasks: when it finishes the status bar says how it went and offers its output", pump(30, lambda: "Task “from the app”: 1 command succeeded" in win.statusBar().currentMessage()) and win.notice_action.text() == "Open Output", win.statusBar().currentMessage())
check("tasks: when it last ran is kept, so it is not run again until its time", win.tasks.get(scheduled.id).last_run > 0 and win.tasks.get(scheduled.id).last_result == "1 command succeeded" and not _tasks.is_due(win.tasks.get(scheduled.id)) and win.scheduler.check() == [])
check("top bar: the clock says a task is running, then that one is scheduled and when it runs next", "“from the app” is running" in running_tip and "1 scheduled; next, “from the app” at " in win.tasks_button.toolTip(), running_tip + " / " + win.tasks_button.toolTip())

task_dialog = TasksDialog(win.tasks.tasks, win.store.sessions, win.scheduler, win)
check("tasks window: a tick beside a task whose last run succeeded", task_dialog.list.item(0).toolTip().endswith(": 1 command succeeded") and not task_dialog.list.item(0).icon().isNull(), task_dialog.list.item(0).toolTip())
check("tasks window: lists the tasks and offers this computer and the SSH sessions to run on", task_dialog.list.count() == 1 and task_dialog.target.findData(task_server.id) >= 0 and task_dialog.target.findData(_tasks.TARGET_CMD) >= 0 and "Last run" in task_dialog.status.text())
task_dialog.add()
task_dialog.name.setText("Disk report")
task_dialog.name.textEdited.emit("Disk report")
task_dialog.target.setCurrentIndex(task_dialog.target.findData(task_server.id))
task_dialog.commands.setPlainText("df -h")
task_dialog.as_script.setChecked(True)
check("tasks window: ticking one script changes what the window says about cd", "a cd holds" in task_dialog.commands_hint.text() and task_dialog.stop_on_error.text().startswith("Stop the script"))
task_dialog.as_script.setChecked(False)
check("tasks window: a new task notifies when a run fails, unless told not to", task_dialog.notify_on_failure.isChecked() and "tick “Run the lines as one script”" in task_dialog.commands_hint.text())
task_dialog.schedule.setCurrentIndex(task_dialog.schedule.findData(_tasks.SCHEDULE_DAILY))
task_dialog.daily_at.setTime(QTime(6, 15))
check("tasks window: only the fields of the chosen schedule are shown", task_dialog.daily_at.isVisibleTo(task_dialog) and not task_dialog.every.isVisibleTo(task_dialog))
task_dialog.run_now()
check("tasks window: Run Now runs the task as it stands, before it is saved", pump(20, lambda: "1 command succeeded" in task_dialog.status.text()), task_dialog.status.text())
task_dialog.runner.setCurrentIndex(task_dialog.runner.findData(_tasks.RUNNER_APP))
task_dialog.schedule.setCurrentIndex(task_dialog.schedule.findData(_tasks.SCHEDULE_MANUAL))
task_dialog.runner.setCurrentIndex(task_dialog.runner.findData(_tasks.RUNNER_WINDOWS))
task_dialog._finish()
check("tasks window: a task for Windows with no schedule is not saved", task_dialog.result() == 0 and "needs a schedule" in task_dialog.error.text(), task_dialog.error.text())
task_dialog.runner.setCurrentIndex(task_dialog.runner.findData(_tasks.RUNNER_APP))
task_dialog.schedule.setCurrentIndex(task_dialog.schedule.findData(_tasks.SCHEDULE_DAILY))
task_dialog._finish()
edited = task_dialog.tasks()
check("tasks window: Save gives the edited tasks", task_dialog.result() == 1 and [task.name for task in edited] == ["from the app", "Disk report"] and edited[1].target == task_server.id and edited[1].daily_at == "06:15" and edited[1].schedule == _tasks.SCHEDULE_DAILY, repr([(t.name, t.daily_at) for t in edited]))
win.save_tasks(edited)
task_dialog.deleteLater()

palette_task = [entry for entry in win.switcher_entries() if entry[2] == "Run Task: Disk report"]
check("tasks: each task can be run from the command palette", len(palette_task) == 1)
palette_task[0][1]()
check("tasks: run from the palette, the status bar says when it starts and how it went", "Task “Disk report” started" in win.statusBar().currentMessage() and pump(20, lambda: "Task “Disk report”: 1 command succeeded" in win.statusBar().currentMessage()), win.statusBar().currentMessage())
check("tasks: how a run went is kept with the task", win.tasks.find("Disk report").last_ok and _tasks.TaskStore().find("Disk report").last_ok)
listed = _subprocess.run([sys.executable, str(ROOT / "main.py"), "--tasks"], capture_output=True, text=True, timeout=60, env=dict(os.environ))
check("command line: --tasks prints the scheduled tasks", "from the app\tThis computer: Command Prompt\tevery 5 min" in listed.stdout and "Disk report\ttask-box\tdaily at 06:15" in listed.stdout, listed.stdout + listed.stderr[-300:])
cli_run = _subprocess.run([sys.executable, str(ROOT / "main.py"), "--run-task", "disk REPORT"], capture_output=True, text=True, timeout=120, env=dict(os.environ))
check("command line: --run-task runs a task by name without a window, and says where its output is", cli_run.returncode == 0 and "Disk report: 1 command succeeded" in cli_run.stdout and "Disk report.txt" in cli_run.stdout, cli_run.stdout + cli_run.stderr[-300:])
win.tasks.load()
check("command line: the run is recorded for the window to show", win.tasks.find("Disk report").last_result == "1 command succeeded")
cli_missing = _subprocess.run([sys.executable, str(ROOT / "main.py"), "--run-task", "nothing like this"], capture_output=True, text=True, timeout=60, env=dict(os.environ))
check("command line: --run-task with an unknown task exits with status 2", cli_missing.returncode == 2 and "No scheduled task named" in cli_missing.stdout, cli_missing.stdout)
first_link = _tasks.Task(name="link one", target=_tasks.TARGET_CMD, commands="echo chain-one", append=False)
second_link = _tasks.Task(name="link two", target=_tasks.TARGET_CMD, commands="echo chain-two", append=False)
first_link.then_run, second_link.then_run = second_link.id, first_link.id  # each names the other: they must still run once each
win.save_tasks([first_link, second_link])
win.scheduler.run(win.tasks.get(first_link.id))
check("tasks: a task that succeeded starts the one it names, and tasks that name each other run once each", pump(40, lambda: win.tasks.get(second_link.id).last_run > 0 and not win.scheduler.is_running(first_link.id) and not win.scheduler.is_running(second_link.id)) and win.tasks.get(second_link.id).last_ok, repr(win.tasks.get(second_link.id).last_result))
pump(1.0)
check("tasks: (the chain ended, and each output file has its own run)", not win.scheduler.is_running(first_link.id) and not win.scheduler.is_running(second_link.id) and "chain-one" in _tasks.output_path(first_link).read_text(encoding="utf-8") and "chain-two" in _tasks.output_path(second_link).read_text(encoding="utf-8"))
second_link.then_run = ""  # the second no longer names the first; the first still names the second
first_link.commands = "exit 1"
win.save_tasks([first_link, second_link])
win.tasks.get(second_link.id).last_run = 0
win.tasks.save()
win.scheduler.run(win.tasks.get(first_link.id))
pump(15, lambda: not win.scheduler.is_running(first_link.id))
pump(1.0)
check("tasks: a task that failed does not start the next", win.tasks.get(second_link.id).last_run == 0 and not win.tasks.get(first_link.id).last_ok)
first_link.commands = "echo chain-one"
win.save_tasks([first_link, second_link])
chain_run = _subprocess.run([sys.executable, str(ROOT / "main.py"), "--run-task", "link one"], capture_output=True, text=True, timeout=120, env=dict(os.environ))
check("command line: --run-task goes on to the task named to follow", chain_run.returncode == 0 and "link one: 1 command succeeded" in chain_run.stdout and "link two: 1 command succeeded" in chain_run.stdout, chain_run.stdout + chain_run.stderr[-300:])
chain_dialog = TasksDialog(win.tasks.tasks, win.store.sessions, win.scheduler, win)
chain_dialog.list.setCurrentRow(0)
pick = chain_dialog.then_run
offered = [pick.itemText(i) for i in range(pick.count())]
pick.setCurrentIndex(0)
pick.setCurrentIndex(1)
chain_dialog.list.setCurrentRow(1)
chain_dialog.list.setCurrentRow(0)
check("tasks window: Then Run offers the other tasks but not the task itself, and keeps the choice", offered == ["Nothing", "link two"] and pick.currentData() == second_link.id and chain_dialog.tasks()[0].then_run == second_link.id, repr(offered))
chain_dialog.deleteLater()

win.save_tasks([])
for extra in (task_server, unknown_host, no_password):
    win.store.sessions.remove(extra)
win.store.save()

# ---- closing a tab while it is still asking for a password
prompting = win.open_session(win.store.sessions[2])  # no password known: it will ask
pump(0.05)
win.close_tab(prompting)  # closed before (or while) the question is on screen
pump(3.0)
check("prompts: closing a tab that is asking for a password is harmless", not _app_module_early.unhandled_errors and win._pane_of(prompting) is None, (_app_module_early.unhandled_errors or [""])[0][-300:])

# ---- regular expressions in find, jumping to the ends ------------------------------------------------
finder = TerminalWidget(_Prefs())
finder.resize(600, 200)
finder.feed(b"alpha 12\r\nbeta 345\r\ngamma x9")
numbers = finder.find(r"\d+", regex=True)
spans = [(row, first, last) for row, first, last in finder._find_matches]
check("find: a regular expression matches by pattern", numbers == 3 and spans == [(0, 6, 7), (1, 5, 7), (2, 7, 7)], repr(spans))
check("find: the same text without the option is taken literally", finder.find(r"\d+") == 0 and finder.find("a.", regex=True) == 5 and finder.find("ALPHA", True, True) == 0 and finder.find("x*", regex=True) == 1)
check("find: an invalid pattern is reported, not raised", finder.find("(", regex=True) == 0 and finder.find_error.startswith("Invalid pattern") and finder.find("alpha") == 1 and finder.find_error == "")
win.tabs.setCurrentIndex(win.tabs.indexOf(tlog))
tlog.show_find()
tlog.find_bar.regex.setChecked(True)
tlog.find_bar.query.setText("(")
tlog.find_bar.search()
invalid_label = tlog.find_bar.count.text()
tlog.find_bar.query.setText(r"web-\d\d")
tlog.find_bar.search()
check("find bar: the .* button switches to patterns", invalid_label == "Invalid pattern" and " of " in tlog.find_bar.count.text(), tlog.find_bar.count.text())
tlog.find_bar.regex.setChecked(False)
tlog.find_bar.close_bar()
scroll = tlog.terminal.verticalScrollBar()
tlog.terminal.setFocus()
QTest.keyClick(tlog.terminal, Qt.Key.Key_Home, Qt.KeyboardModifier.ShiftModifier)
at_top = scroll.value()
QTest.keyClick(tlog.terminal, Qt.Key.Key_End, Qt.KeyboardModifier.ShiftModifier)
check("scrolling: Shift+Home and Shift+End jump to the ends", scroll.maximum() > 0 and at_top == 0 and scroll.value() == scroll.maximum(), f"{at_top} {scroll.value()}/{scroll.maximum()}")

# ---- timestamps in logs --------------------------------------------------------------------------------
from techxterminal.tab import SessionLog as _SessionLog

stamped = _SessionLog(WORK / "stamped.log", timestamps=True)
stamped.write(b"one\r\ntw")
stamped.write(b"o\r\n\x1b[31mthree\x1b[0m\r\n")
stamped.close()
stamped_lines = (WORK / "stamped.log").read_text(encoding="utf-8").splitlines()
check("log timestamps: each line starts with the time once, even when it arrives in pieces", [bool(_re2.fullmatch(r"\[\d\d:\d\d:\d\d\] (one|two|three)", line)) for line in stamped_lines[1:4]] == [True, True, True], repr(stamped_lines[1:4]))
win.apply_preferences(_replace(win.prefs, log_timestamps=True))
tlog.start_logging(WORK / "tab-stamped.log")
run(tlog, "echo stamped")
tlog.stop_logging()
win.apply_preferences(_replace(win.prefs, log_timestamps=False))
check("log timestamps: the setting reaches a tab's log", bool(_re2.search(r"^\[\d\d:\d\d:\d\d\] ran: echo stamped", (WORK / "tab-stamped.log").read_text(encoding="utf-8"), _re2.M)))

# ---- backup and restore ------------------------------------------------------------------------------------
import zipfile as _zipfile

backup_file = WORK / "everything.zip"
from techxterminal.tasks import Task as _BackedTask

win.save_tasks([_BackedTask(name="backed-up task", commands="uptime")])
backed_up = win.backup_to(backup_file)
with _zipfile.ZipFile(backup_file) as archive:
    in_backup = set(archive.namelist())
    everything = b"".join(archive.read(name) for name in archive.namelist())
check("backup: sessions, saved commands, host keys and settings in one file, no passwords", backed_up and in_backup == {"sessions.json", "snippets.json", "known_hosts", "preferences.json", "tasks.json"} and b"secret" not in everything and b"web-01" in everything)
sessions_before = sorted((session.name, session.group) for session in win.store.sessions)
commands_before = [(sn.name, sn.command) for sn in win.snippets.snippets]
win.rename_folder("Staging", "Changed")
win.snippets.replace_all([])
win.save_tasks([])
win.apply_preferences(_replace(win.prefs, theme="light", scrollback=555))
check("backup: (things are changed after it was made)", "Changed" in win.store.groups() and not win.snippets.snippets and _theme.active_theme() == "light")
restored_ok = win.restore_from(backup_file)
pump(0.5)
check("restore: sessions, saved commands and settings come back", restored_ok and sorted((session.name, session.group) for session in win.store.sessions) == sessions_before and [(sn.name, sn.command) for sn in win.snippets.snippets] == commands_before and win.prefs.scrollback == 10000 and _theme.active_theme() == "dark" and "Staging" in headings() and "Changed" not in headings())
check("restore: scheduled tasks come back too", [task.name for task in win.tasks.tasks] == ["backed-up task"] and win.tasks.tasks[0].registered_as == "", repr([task.name for task in win.tasks.tasks]))
win.save_tasks([])
check("restore: the files that were replaced are kept as .bak", (HOME / ".techxterminal" / "sessions.json.bak").exists() and "Changed" in (HOME / ".techxterminal" / "sessions.json.bak").read_text() and tlog.state == "connected" and tlog.terminal.screen.history.top.maxlen == 10000)
check("restore: a file that is not a backup is refused and nothing changes", not win.restore_from(WORK / "junk.json") and "not a Tech X Terminal backup" in win.statusBar().currentMessage() and sorted((session.name, session.group) for session in win.store.sessions) == sessions_before)
from techxterminal import backup as _backup
from techxterminal.dialogs import BackupPassphraseDialog as _PassphraseDialog

secret_file = WORK / "everything.txbackup"
encrypted_ok = win.backup_to(secret_file, "correct horse battery")
sealed = secret_file.read_bytes()
names_now = sorted((session.name, session.group) for session in win.store.sessions)
check("encrypted backup: nothing in the file can be read without the passphrase", encrypted_ok and sealed.startswith(_backup.MAGIC) and b"sessions.json" not in sealed and not any(name.encode() in sealed for name, _group in names_now) and "encrypted" in win.statusBar().currentMessage())
win.rename_folder("Staging", "Changed again")
wrong = win.restore_from(secret_file, "not the passphrase")
check("encrypted backup: a wrong passphrase is refused, and nothing changes", not wrong and "Wrong passphrase" in win.statusBar().currentMessage() and "Changed again" in win.store.groups())
try:
    _backup.read_backup(secret_file)
    asked_for = False
except _backup.PassphraseNeeded:
    asked_for = True
right = win.restore_from(secret_file, "correct horse battery")
pump(0.3)
check("encrypted backup: with the passphrase it restores, and without one it is asked for", asked_for and right and sorted((session.name, session.group) for session in win.store.sessions) == names_now)
asking = _PassphraseDialog(win)
asking.passphrase.setText("short")
too_short = asking.problem()
asking.passphrase.setText("long enough here")
asking.again.setText("long enough there")
not_same = asking.problem()
asking.again.setText("long enough here")
check("encrypted backup: the passphrase must be long enough and typed the same twice", "at least 8" in too_short and "not the same" in not_same and asking.problem() == "" and asking.chosen_passphrase() == "long enough here", repr((too_short, not_same)))
asking.deleteLater()
win.tabs.setCurrentIndex(0)
pump(0.3)

# ---- robustness -------------------------------------------------------------------------------
from PyQt6.QtCore import QThread as _QThread
from techxterminal import errors as _errors, threads as _threads
from techxterminal.sessions import SessionStore as _Store3

safe_file = WORK / "safety" / "sessions.json"
safe_store = _Store3(safe_file)
safe_store.upsert(Session(name="kept", host="h1", username="u"))
check("saving: the file is replaced in one step, with nothing left behind", [p.name for p in safe_file.parent.iterdir()] == ["sessions.json"] and _Store3(safe_file).sessions[0].name == "kept")
safe_file.write_text('{"sessions": [{"name": "half written", "host": "h"')  # as after a crash mid-write
damaged = _Store3(safe_file)
check("loading: a damaged file is set aside instead of being silently replaced", damaged.sessions == [] and "could not be read" in damaged.load_error and "sessions.json.corrupt" in damaged.load_error and (safe_file.parent / "sessions.json.corrupt").read_text().startswith('{"sessions": [{"name": "half written"'), damaged.load_error)
damaged.upsert(Session(name="new", host="h2", username="u"))
check("loading: work can go on, and the damaged copy stays", _Store3(safe_file).sessions[0].name == "new" and (safe_file.parent / "sessions.json.corrupt").exists() and _Store3(safe_file).load_error == "")
safe_file.write_text("[1, 2, 3]")
check("loading: a file of the wrong shape is treated the same way", _Store3(safe_file).sessions == [] and _Store3(safe_file).load_error != "")
safe_file.write_text(_json3.dumps({"sessions": ["text", {"name": "no host"}, {"name": "odd", "host": "h3", "port": "twenty-two", "favourite": "yes", "wait_ms": 250, "made_up": 1}]}))
tolerant = _Store3(safe_file)
check("loading: wrong values in one session fall back to defaults, the rest loads", [(x.name, x.host, x.port, x.favourite, x.wait_ms) for x in tolerant.sessions] == [("odd", "h3", 22, False, 250)] and tolerant.load_error == "", repr(tolerant.sessions))

odd_settings = QSettings(os.environ["TECHX_SETTINGS_FILE"], QSettings.Format.IniFormat)
saved_values = {key: odd_settings.value(key) for key in ("font_size", "theme", "cursor_style", "scrollback", "terminal_scheme", "ui_font_size")}
for key, value in (("font_size", 500), ("theme", "purple"), ("cursor_style", "wavy"), ("scrollback", -5), ("terminal_scheme", "no-such"), ("ui_font_size", "big")):
    odd_settings.setValue(key, value)
odd_settings.sync()
odd = Preferences.load()
check("settings: values that make no sense fall back instead of breaking the window", (odd.terminal_size, odd.theme, odd.cursor_style, odd.scrollback, odd.terminal_scheme, odd.ui_size) == (32, "dark", "block", 100, "", 10), repr((odd.terminal_size, odd.theme, odd.cursor_style, odd.scrollback, odd.terminal_scheme, odd.ui_size)))
win.prefs.save()  # put the real values back
check("settings: (restored)", Preferences.load() == win.prefs)


class _Sleeper(_QThread):
    def run(self):
        time.sleep(0.5)


sleeper = _Sleeper()
sleeper.start()
_threads.park(sleeper)
held = sleeper in _threads.parked()
del sleeper  # the only other reference: without parking, Qt would end the process here
pump(1.0)
finished_one = _Sleeper()
_threads.park(finished_one)  # not running: nothing to hold, and it clears out the finished one
check("threads: a running thread is held until it ends, then let go", held and not any(isinstance(t, _Sleeper) for t in _threads.parked()))

closing = SessionDialog(win.store, None, win)
closing.show()
closing.host.setText("10.255.255.1")  # an address that does not answer
closing.username.setText("u")
closing.test_connection()
pump(0.3)
closing.reject()
closing.deleteLater()
pump(0.5)
check("threads: a connection test still running when its dialog closes is held until it ends", any(type(t).__name__ == "Probe" for t in _threads.parked()) and len(_errors.unhandled) == 0, repr([type(t).__name__ for t in _threads.parked()]))

win.tabs.setCurrentIndex(win.tabs.indexOf(tlog))
win.toggle_files()
mid = tlog._files
pump(10, lambda: mid.directory != "")
mid.set_speed_limit(20)
mid.upload([str(WORK / "localside" / "big.bin")])
pump(5, lambda: mid.cancel_button.isVisible())
win.toggle_files()  # closed in the middle of the transfer
pump(1.5)
mid.worker.wait(5000)
check("file browser: closing it during a transfer stops the transfer cleanly", tlog._files is None and tlog.state == "connected" and mid.worker.isFinished() and (not (SFTP_ROOT / "big.bin").exists() or (SFTP_ROOT / "big.bin").stat().st_size < 300_000))
check("file browser: the transfer cut off by closing left its partial file", (SFTP_ROOT / "big.bin.part").exists())
for leftover in list(SFTP_ROOT.glob("big.bin*")):
    leftover.unlink()
win.apply_preferences(_replace(win.prefs, transfer_limit_kbps=0))

errors_before = len(_errors.unhandled)
QTimer.singleShot(0, lambda: 1 / 0)  # an error nobody expected, inside the event loop
pump(0.5)
logged = _errors.ERROR_LOG.read_text(encoding="utf-8") if _errors.ERROR_LOG.exists() else ""
check("errors: an unexpected error is logged and reported, and the application keeps running", len(_errors.unhandled) == errors_before + 1 and "ZeroDivisionError" in _errors.unhandled[-1] and "ZeroDivisionError" in logged and "error.log" in win.statusBar().currentMessage() and tlog.state == "connected", win.statusBar().currentMessage())
del _errors.unhandled[errors_before:]  # that one was on purpose
run(tlog, "still-here")
check("errors: sessions carry on afterwards", "ran: still-here" in text(tlog))
from techxterminal import __version__ as _version
_change_log = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
# Every build raises the version (build.py), so no one number is expected: a well-formed one, and a change log
# that has what is new since the last release, or this version's heading once it is released.
check("version: three numbers, with a change log", _re.fullmatch(r"\d+\.\d+\.\d+", _version) is not None and ("## Unreleased" in _change_log or f"## {_version}" in _change_log), _version)
win.tabs.setCurrentIndex(0)
pump(0.3)

# ---- workspaces ------------------------------------------------------------------------------
from techxterminal.workspaces import WorkspaceStore as _WorkspaceStore

open_ids = win._saved_open_ids()
check("workspaces: none yet", win.workspaces.names() == [] and len(open_ids) >= 2)
check("workspaces: the open saved sessions are remembered under a name", win.save_workspace("Morning") and win.workspaces.names() == ["Morning"] and win.workspaces.get("Morning") == open_ids and _WorkspaceStore().get("Morning") == open_ids and "Morning" in win.statusBar().currentMessage())
before_count = len(win.open_tabs())
morning = win.open_workspace("Morning")
check("workspaces: opening one opens all its sessions", [tab.session.id for tab in morning] == open_ids and len(win.open_tabs()) == before_count + len(open_ids) and pump(40, lambda: all(tab.state == "connected" for tab in morning)))
for tab in morning:
    win.close_tab(tab)
check("workspaces: an empty name is refused and an unknown one opens nothing", not win.save_workspace("   ") and win.open_workspace("No such") == [])
win.save_workspace("Keep")
with_spaces = WORK / "with-workspaces.zip"
win.backup_to(with_spaces)
with _zipfile.ZipFile(with_spaces) as archive:
    has_workspaces = "workspaces.json" in archive.namelist()
win.workspaces.remove("Keep")
win.workspaces.remove("Morning")
emptied = win.workspaces.names() == []
win.restore_from(with_spaces)
check("workspaces: they are part of a backup and come back with a restore", has_workspaces and emptied and win.workspaces.names() == ["Keep", "Morning"])
win.workspaces.remove("Keep")
win.workspaces.remove("Morning")

# ---- notify when output stops ----------------------------------------------------------------------
win.tabs.setCurrentIndex(win.tabs.indexOf(tlog))
quiet = []
tlog.silenceDetected.connect(lambda: quiet.append(time.time()))
tlog.watch_for_silence(0.8)
for step in range(5):
    run(tlog, f"echo step-{step}", 0.3)  # output keeps coming: not quiet yet
still_busy = list(quiet)
pump(1.6)
check("silence: reported once, only after the output has stopped", still_busy == [] and len(quiet) == 1 and not tlog.is_watching() and "output has stopped" in win.statusBar().currentMessage(), f"{len(still_busy)} then {len(quiet)}")
pump(1.2)
check("silence: it does not repeat", len(quiet) == 1)
win.tabs.setCurrentIndex(win.tabs.indexOf(tab1))
tlog.watch_for_silence(0.4)
pump(1.0)
check("silence: a background tab is marked", len(quiet) == 2 and label(tlog).endswith("\u2022"), ascii(label(tlog)))
win.tabs.setCurrentIndex(win.tabs.indexOf(tlog))
tlog.watch_for_silence(0.4)
tlog.stop_watching()
pump(0.9)
check("silence: watching can be switched off again", len(quiet) == 2 and not tlog.is_watching())

# ---- which sessions answer ---------------------------------------------------------------------------
dead_box = Session(name="dead-box", group="Production", host="127.0.0.1", port=1, username="x")
win.store.upsert(dead_box)
win._reload_tree()
win.check_connections()
checking = "Checking" in win.statusBar().currentMessage()
pump(30, lambda: len(win._reachable) == len(win.store.sessions) and not win._probe_many.isRunning())
pump(0.3)
alive = win.reachability(win.store.get(prod.id))
gone = win.reachability(dead_box)
check("reachability: every saved session is tested and the answers are told apart", checking and alive is not None and alive[0] and gone is not None and not gone[0] and "refused" in gone[1] and f"{len(win.store.sessions) - 1} of {len(win.store.sessions)} checked sessions answer" in win.statusBar().currentMessage(), win.statusBar().currentMessage())
production_node = next(win.tree.topLevelItem(i) for i in range(win.tree.topLevelItemCount()) if win.tree.topLevelItem(i).text(0) == "Production")
dead_item = next(production_node.child(j) for j in range(production_node.childCount()) if production_node.child(j).text(0) == "dead-box")
delegate = win.tree.itemDelegate()
check("reachability: the sidebar knows each result and explains a failure", delegate.reachable_for(dead_box.id) is False and delegate.reachable_for(prod.id) is True and delegate.reachable_for("unknown") is None and "refused" in dead_item.toolTip(0))
win._reachable.clear()
win.check_connections(win.sessions_in("Staging"))
pump(20, lambda: not win._probe_many.isRunning())
pump(0.3)
check("reachability: a single folder can be checked on its own", set(win._reachable) == {session.id for session in win.sessions_in("Staging")}, f"{len(win._reachable)}")
win.store.remove(dead_box)
win._reachable.clear()
win._reload_tree()
win.grab().save(str(SCRATCH / "shot_tabs.png"))
win.tabs.setCurrentIndex(0)
pump(0.3)

# ---- split panes --------------------------------------------------------------------------
import techxterminal.mainwindow as _mw

win.tabs.setCurrentIndex(0)
total = len(win.open_tabs())
o1, o2 = [tab for tab in win.open_tabs() if tab not in (tab1, tlog)][:2]


def layout():
    """Which tabs sit in which pane, left to right."""
    names = {tab1: "tab1", tlog: "tlog", o1: "o1", o2: "o2"}
    return [[names[pane.widget(i)] for i in range(pane.count())] for pane in win.panes]


win.move_to_other_pane(tlog)
pump(0.5)
check("split: a tab moves to a second pane beside the first", len(win.panes) == 2 and win.panes[1].count() == 1 and win.tabs.count() == total - 1 and tlog.terminal.isVisible() and tab1.terminal.isVisible() and tlog.state == "connected", repr(layout()))

# a pinned tab moved to another pane joins the pinned ones there, an unpinned one goes after them
pane_one, pane_two = win.panes[0], win.panes[1]
order_one = [pane_one.widget(i) for i in range(pane_one.count())]
win.pin_tab(o2, True, announce=False)
win.move_tab(o2, 1)  # arrives after the unpinned tlog, and must sit before it
joined_pinned = pane_two.indexOf(o2) == 0
win.pin_tab(tlog, True, announce=False)
win.move_tab(o1, 1)
after_pinned = pane_two.indexOf(o1) == pane_two.count() - 1 and pane_two.indexOf(tlog) < pane_two.indexOf(o1)
win.move_tab(o1, -1)
win.pin_tab(tlog, False, announce=False)
win.pin_tab(o2, False, announce=False)
win.move_tab(o2, -1)
for place, tab in enumerate(order_one):
    pane_one.tabBar().moveTab(pane_one.indexOf(tab), place)
pump(0.3)
check("pin tab: moved to another pane, a pinned tab joins the pinned ones and others go after them", joined_pinned and after_pinned and len(win.panes) == 2 and pane_two.count() == 1, repr((joined_pinned, after_pinned, layout())))

# Close All Unpinned Tabs closes exactly the tabs that are not pinned (checked without closing the shared ones)
win.pin_tab(tab1, True, announce=False)
asked = []
_real_close_several = win._close_several
win._close_several = lambda tabs: asked.append(list(tabs)) or len(tabs)
win.close_unpinned()
win._close_several = _real_close_several
win.pin_tab(tab1, False, announce=False)
for place, tab in enumerate(order_one):
    pane_one.tabBar().moveTab(pane_one.indexOf(tab), place)
check("pin tab: Close All Unpinned Tabs asks to close every tab but the pinned ones, in all panes", len(asked) == 1 and tab1 not in asked[0] and tlog in asked[0] and len(asked[0]) == len(win.open_tabs()) - 1, repr(len(asked)))

# Pin All and Unpin All keep the order of the tabs
order_all = win.open_tabs()
unpinned_count = sum(not t.pinned for t in order_all)
changed_on = win.pin_all(True)
all_pinned = all(t.pinned for t in win.open_tabs()) and win.open_tabs() == order_all
changed_off = win.pin_all(False)
none_pinned = not any(t.pinned for t in win.open_tabs()) and win.open_tabs() == order_all
check("pin tab: Pin All and Unpin All change every tab and keep the order", changed_on == unpinned_count and all_pinned and changed_off == len(order_all) and none_pinned and win.pin_all(False) == 0, repr((changed_on, changed_off)))

# the pane of each tab is remembered, and the tabs return to their panes
kept_settings = (win.settings.value("open_sessions"), win.settings.value("pinned_tabs"), win.settings.value("tab_panes"))
win.pane_splitter.setSizes([350, 650])
front_before = win.panes[0].currentWidget()
saved_in_front_pane = [win.panes[0].widget(i) for i in range(win.panes[0].count()) if win.store.get(win.panes[0].widget(i).session.id) is not None]
front_target = saved_in_front_pane[0]  # not the last one opened again, so that being in front is not by chance
win.panes[0].setCurrentWidget(front_target)
pump(0.1)
kept_sizes = win.settings.value("pane_sizes")
win.remember_open_tabs()
saved_entries = [str(entry) for entry in win.settings.value("open_sessions")]
saved_panes = [str(pane) for pane in win.settings.value("tab_panes")]
tlog_spot = saved_entries.index(tlog.session.id)
panes_saved = len(saved_panes) == len(saved_entries) and saved_panes[tlog_spot] == "1" and "0" in saved_panes
before_restore = set(win.open_tabs())
back = win.restore_tabs()
pump(0.3)
twin_of_tlog = next(tab for tab in back if tab.session.id == tlog.session.id)
front_back = win.panes[0].currentWidget()
front_ok = len(saved_in_front_pane) > 1 and front_back in back and front_back.session.id == front_target.session.id
back_in_place = win._pane_of(twin_of_tlog) is win.panes[1] and all(win._pane_of(t) is win.panes[0] for t in back if t is not twin_of_tlog and t.session.id != tlog.session.id)
for tab in back:
    win.close_tab(tab, force=True)
win._closed_tabs.clear()
win.settings.setValue("open_sessions", kept_settings[0])
win.settings.setValue("pinned_tabs", kept_settings[1])
win.settings.setValue("tab_panes", kept_settings[2])
saved_shares = [int(share) for share in win.settings.value("pane_sizes")]
win.pane_splitter.setSizes([500, 500])
win.settings.setValue("pane_sizes", [str(share) for share in saved_shares])
win._restore_pane_sizes([str(share) for share in saved_shares])  # what restore_tabs ends with, without opening the tabs again
pump(0.2)
now_sizes = win.pane_splitter.sizes()
share_now = now_sizes[0] * 1000 / sum(now_sizes)
win.settings.setValue("pane_sizes", kept_sizes)
win._balance_panes()
pump(0.1)
check("restore: the share of the window each pane had comes back", len(saved_shares) == 2 and abs(saved_shares[0] - 350) < 60 and abs(share_now - saved_shares[0]) < 60, repr((saved_shares, now_sizes)))
win.panes[0].setCurrentWidget(front_before)
check("restore: the tab in front in each pane is the one that was", front_ok, repr(front_back.session.name))
check("restore: each tab's pane is remembered and the tabs go back to their panes", panes_saved and back_in_place and set(win.open_tabs()) == before_restore and len(win.panes) == 2, repr((saved_entries, saved_panes, back_in_place, layout())))

# a workspace keeps how its tabs were arranged: pins, panes, and the pane in use
win.pin_tab(tlog, True, announce=False)
win._set_active_pane(win.panes[1])
win.save_workspace("layout ws")
kept_layout = win.workspaces.get_layout("layout ws")
win._set_active_pane(win.panes[0])
ws_tabs = win.open_workspace("layout ws")
pump(0.3)
ws_twin = next(t for t in ws_tabs if t.session.id == tlog.session.id)
ws_parts = (ws_twin.pinned, win.panes.index(win._pane_of(ws_twin)), sum(t.pinned for t in ws_tabs), [win.panes.index(win._pane_of(t)) for t in ws_tabs], win.panes.index(win._active_pane))
ws_ok = ws_twin.pinned and win._pane_of(ws_twin) is win.panes[1] and sum(t.pinned for t in ws_tabs) == 1 and all(win._pane_of(t) is win.panes[0] for t in ws_tabs if t is not ws_twin)
for tab in ws_tabs:
    win.close_tab(tab, force=True)
win._closed_tabs.clear()
win.delete_workspace("layout ws", confirm=False)
win.pin_tab(tlog, False, announce=False)
win._set_active_pane(win.panes[0])
win._focus_pane(win.panes[1])  # what opening it ends with; a tab that is still connecting may take the focus back after
active_applied = win._active_pane is win.panes[1]
win._set_active_pane(win.panes[0])
check("workspace: the pane in use is saved with it and can be given the focus", kept_layout.get("active") == ["1"] and active_applied, repr(kept_layout.get("active")))
check("workspace: opened again, its tabs are pinned and in their panes as they were saved", ws_ok and "pinned" in kept_layout and "panes" in kept_layout, repr((ws_parts, ws_ok)))
check("split: both panes share the width", abs(win.panes[0].width() - win.panes[1].width()) < 80, f"{win.panes[0].width()} / {win.panes[1].width()}")
run(tlog, "in-right-pane")
check("split: the moved session is still live", "ran: in-right-pane" in text(tlog))

check("panes: Split Right adds a third and a fourth pane next to the source", win.split_tab(o1) and win.split_tab(o2) and layout() == [["tab1"], ["o2"], ["o1"], ["tlog"]], repr(layout()))
pump(0.5)
widths = [pane.width() for pane in win.panes]
check("panes: four terminals are visible side by side", all(tab.terminal.isVisible() for tab in (tab1, tlog, o1, o2)) and max(widths) - min(widths) < 80, repr(widths))


def marked_panes():
    """The panes whose tab bar keeps the accent, as the stylesheet sees it."""
    return [pane for pane in win.panes if not pane.tabBar().property("inactive")]


o1.terminal.setFocus()
pump(0.3)
first_marked = marked_panes()
tlog.terminal.setFocus()
pump(0.3)
check("panes: only the active pane's tab keeps the accent, and it follows the focus", first_marked == [win._pane_of(o1)] and marked_panes() == [win._pane_of(tlog)], repr((len(first_marked), len(marked_panes()))))
tab1.terminal.setFocus()
pump(0.3)
win.grab().save(str(SCRATCH / "shot_split.png"))
win.toggle_pane_stacking()
pump(0.5)
tops = [pane.y() for pane in win.panes]
heights = [pane.height() for pane in win.panes]
check("panes: stacked, they sit one above the other and share the height", win.panes_stacked() and tops == sorted(tops) and len(set(tops)) == 4 and len({pane.x() for pane in win.panes}) == 1 and max(heights) - min(heights) < 60 and all(tab.terminal.isVisible() for tab in (tab1, tlog, o1, o2)), repr((tops, heights)))
check("panes: stacking is remembered", win.prefs.stack_panes and Preferences.load().stack_panes)
win.grab().save(str(SCRATCH / "shot_stacked.png"))
win.set_panes_stacked(False)
pump(0.4)
check("panes: and back to side by side", not win.panes_stacked() and len({pane.y() for pane in win.panes}) == 1 and not Preferences.load().stack_panes)
check("panes: a pane's only tab cannot be split off", not win.split_tab(tab1) and len(win.panes) == 4)


def corner(pane):
    """A pane's top-left corner in window coordinates; in the grid panes have different parents."""
    point = pane.mapTo(win, _QPoint(0, 0))
    return point.x(), point.y()


win.set_pane_grid(True)
pump(0.5)
corners = [corner(pane) for pane in win.panes]
grid_sizes = [(pane.width(), pane.height()) for pane in win.panes]
check("grid: four panes sit two above two, in reading order", win.panes_in_grid() and corners[0][1] == corners[1][1] < corners[2][1] == corners[3][1] and corners[0][0] == corners[2][0] < corners[1][0] == corners[3][0] and layout() == [["tab1"], ["o2"], ["o1"], ["tlog"]], repr(corners))
check("grid: they share the window evenly and every terminal shows", max(w for w, _ in grid_sizes) - min(w for w, _ in grid_sizes) < 80 and max(h for _, h in grid_sizes) - min(h for _, h in grid_sizes) < 60 and all(tab.terminal.isVisible() for tab in (tab1, tlog, o1, o2)), repr(grid_sizes))
run(o1, "in-the-grid")
check("grid: the sessions stay live while the panes move", "ran: in-the-grid" in text(o1) and tlog.state == "connected")
check("grid: the choice is remembered, and is neither side by side nor stacked", Preferences.load().pane_grid and not win.panes_stacked())
win.grab().save(str(SCRATCH / "shot_grid.png"))
win.set_panes_stacked(False)
pump(0.4)
check("grid: choosing side by side leaves the grid, in the same order", not win.panes_in_grid() and not win.prefs.pane_grid and len({corner(pane)[1] for pane in win.panes}) == 1 and layout() == [["tab1"], ["o2"], ["o1"], ["tlog"]] and all(tab.terminal.isVisible() for tab in (tab1, tlog, o1, o2)), repr([corner(pane) for pane in win.panes]))
win.all_tabs.setChecked(True)
win.command.setText("echo every-pane")
win._send_command()
pump(0.8)
check("panes: broadcast reaches every pane", all("ran: echo every-pane" in text(tab) for tab in (tab1, tlog, o1, o2)))
win.all_tabs.setChecked(False)
win._set_active_pane(win._pane_of(tlog))
check("panes: the active pane decides the current tab", win.current_tab() is tlog and "web-02" in win.windowTitle(), win.windowTitle())

check("panes: moving a tab right joins that pane and drops the emptied one", win.move_tab(o2, 1) and layout() == [["tab1"], ["o1", "o2"], ["tlog"]], repr(layout()))
win.set_pane_grid(True)
pump(0.4)
corners = [corner(pane) for pane in win.panes]
heights = [pane.height() for pane in win.panes]
check("grid: with three panes the first fills the left and the others are stacked on the right", win.panes_in_grid() and corners[0][0] < corners[1][0] == corners[2][0] and corners[1][1] < corners[2][1] and heights[0] > heights[1] * 1.6, repr((corners, heights)))
win.move_tab(tlog, -1)
pump(0.3)
two_left = win.panes_in_grid()
win.split_tab(tlog)
pump(0.3)
check("grid: down to two panes they sit side by side, and the grid returns with a third", not two_left and win.panes_in_grid() and layout() == [["tab1"], ["o1", "o2"], ["tlog"]], repr(layout()))
win.set_pane_grid(False)
pump(0.3)
check("grid: turned off, the three panes go back to a row", not win.panes_in_grid() and len({corner(pane)[1] for pane in win.panes}) == 1 and not Preferences.load().pane_grid)
_mw.MAX_PANES = 3
at_limit = win.split_tab(o2)
_mw.MAX_PANES = 4
check("panes: no more panes than the limit", not at_limit and len(win.panes) == 3)
check("panes: moving left, pane by pane", win.move_tab(tlog, -1) and layout() == [["tab1"], ["o1", "o2", "tlog"]] and win.move_tab(tlog, -1) and layout() == [["tab1", "tlog"], ["o1", "o2"]] and not win.move_tab(tlog, -1), repr(layout()))
win.move_to_other_pane(tlog)
check("panes: Ctrl+Shift+D moves to the pane on the right when there is one", layout() == [["tab1"], ["o1", "o2", "tlog"]], repr(layout()))
win.close_tab(o1)
win.close_tab(o2)
pump(0.3)
win.move_to_other_pane(tlog)
pump(0.4)
check("panes: from the last pane it wraps back to the first, closing the empty pane", layout() == [["tab1", "tlog"]] and win._pane_of(tlog) is win.tabs, repr(layout()))
win.move_to_other_pane(tlog)
pump(0.3)
check("panes: and splits again when there is no pane to the right", layout() == [["tab1"], ["tlog"]], repr(layout()))
win.close_tab(tab1)
pump(0.4)
check("panes: when the first pane empties, its neighbour's tabs move into it", len(win.panes) == 1 and win.tabs.count() == 1 and win.tabs.widget(0) is tlog and win.stack.currentIndex() == 1 and tlog.state == "connected")
check("panes: a single pane keeps the accent", marked_panes() == [win.tabs])
run(tlog, "after-merge")
check("panes: the session survives all the moves", "ran: after-merge" in text(tlog))
tab1 = tlog  # the remaining tab; later sections refer to it

# ---- import ---------------------------------------------------------------------------------
config = WORK / "ssh_config"
config.write_text(
    "Host *\n  ServerAliveInterval 30\n\n"
    "Host web\n  HostName web.example.com\n  User alice\n  Port 2222\n  IdentityFile ~/.ssh/id_web\n\n"
    "Host db\n  HostName 10.0.0.5\n  ProxyJump alice@gate.example.com\n\n"
    "Host multi\n  HostName far.example.com\n  ProxyJump a@g1,b@g2:2200\n\n"
    "Host *.internal\n  User ops\n"
)
found = {s.name: s for s in importers.from_openssh_config(config)}
check("import: OpenSSH hosts, without wildcard patterns", set(found) == {"web", "db", "multi"}, repr(set(found)))
web, db = found.get("web"), found.get("db")
check("import: host, user, port, key and jump host are carried over", web and (web.host, web.username, web.port, web.auth) == ("web.example.com", "alice", 2222, "key") and web.key_file.replace("\\", "/").endswith(".ssh/id_web") and db.jump_host == "alice@gate.example.com" and found["multi"].jump_host == "a@g1,b@g2:2200" and db.auth == "agent", repr((web, db)))
check("import: a missing config file gives nothing", importers.from_openssh_config(WORK / "nope") == [])
match_marker = WORK / "match_ran.txt"
match_config = WORK / "ssh_config_match"
match_config.write_text(
    "Host first\n  HostName one.example.com\n  User amy\n\n"
    f'Match exec "echo ran > {match_marker.as_posix()}"\n  User mallory\n\n'
    "Match host two.example.com\n  Port 2200\n\n"
    "host second\n  HostName two.example.com\n\n"
    "Match all\n  User nobody\n"
)
match_found = {s.name: s for s in importers.from_openssh_config(match_config)}
check("import: a config with Match blocks still gives its hosts, and nothing is run", set(match_found) == {"first", "second"} and match_found["first"].username == "amy" and not match_marker.exists(), repr((set(match_found), match_marker.exists())))
check("import: Match sections are the only part left out", importers.without_match_blocks("Host a\n  User x\nMatch all\n  User y\nHOST b\n  Port 1\n") == "Host a\n  User x\nHOST b\n  Port 1\n")
canon_config = WORK / "ssh_config_canon"
canon_config.write_text("CanonicalizeHostname yes\nHost box\n  HostName box.example.com\n  User cy\n")
canon_found = {s.name: s for s in importers.from_openssh_config(canon_config)}
check("import: a config paramiko cannot resolve fully still gives its hosts", "box" in canon_found and canon_found["box"].host in ("box", "box.example.com"), repr(canon_found))
odd_file = WORK / "odd_sessions.json"
odd_file.write_text(_json.dumps({"sessions": [
    {"name": "bool host", "host": True},
    {"name": "typed wrongly", "host": "ok.example.com", "port": {"a": 1}, "username": 5, "group": ["x"]},
    "not a session",
    {"name": "fine", "host": "good.example.com", "port": 2200, "username": "gus", "save_password": True, "id": "reused-id"},
]}))
odd = {s.name: s for s in importers.from_sessions_file(odd_file)}
odd_store = _Store(WORK / "odd_store.json")
check("import: a sessions file with values of the wrong type gives the sessions that make sense", set(odd) == {"typed wrongly", "fine"} and odd["typed wrongly"].port == 22 and odd["typed wrongly"].username == "" and odd["fine"].port == 2200 and not odd["fine"].save_password and odd["fine"].id != "reused-id", repr({k: (v.host, v.port, v.username) for k, v in odd.items()}))
check("import: and they can be merged, listed and described", importers.merge(odd_store, list(odd.values())) == 2 and all(session.address and session.title for session in odd_store.sessions))
odd_file.write_text(_json.dumps({"sessions": 5}))
check("import: a sessions file whose list is not a list gives nothing", importers.from_sessions_file(odd_file) == [])
saved_odd = importers.session_from_saved("odd", {"Protocol": "ssh", "HostName": "u@h.example.com", "PortNumber": "not a number"})
saved_serial = importers.session_from_saved("odd serial", {"Protocol": "serial", "SerialLine": "COM3", "SerialSpeed": "fast"})
check("import: a Saved Session with a port that is not a number gets the usual port", saved_odd is not None and saved_odd.port == 22 and saved_serial is not None and saved_serial.port == 9600, repr((saved_odd and saved_odd.port, saved_serial and saved_serial.port)))
p1 = importers.session_from_saved("prod box", {"Protocol": "ssh", "HostName": "root@10.1.1.1", "PortNumber": 2200, "PublicKeyFile": "C:\\k\\id.ppk"})
p2 = importers.session_from_saved("switch", {"Protocol": "serial", "SerialLine": "COM4", "SerialSpeed": 115200})
p3 = importers.session_from_saved("tel", {"Protocol": "telnet", "HostName": "10.2.2.2", "PortNumber": 23, "UserName": "admin"})
check("import: Saved Sessions map to protocols and fields", (p1.username, p1.host, p1.port, p1.auth, p1.key_file) == ("root", "10.1.1.1", 2200, "key", "C:\\k\\id.ppk") and (p2.protocol, p2.host, p2.port) == ("serial", "COM4", 115200) and (p3.protocol, p3.username) == ("telnet", "admin") and importers.session_from_saved("empty", {"Protocol": "ssh", "HostName": ""}) is None)
menu_source = (ROOT / "techxterminal" / "mainwindow.py").read_text(encoding="utf-8")
check("import: the menu entry reads Saved Sessions", '"From Saved Sessions"' in menu_source and "PuTTY" not in menu_source)
check("import: reading the registry does not fail", isinstance(importers.from_saved_sessions(), list))
saved_before = len(win.store.sessions)
added = win.import_sessions(list(found.values()) + [p1, p2, p3])
again = win.import_sessions(list(found.values()))
check("import: new sessions are added once, into their own folders", added == 6 and again == 0 and len(win.store.sessions) == saved_before + 6 and "Imported (OpenSSH)" in win.store.groups() and "Imported (Saved Sessions)" in win.store.groups(), f"{added} {again}")
check("import: the sidebar and saved file are updated", any(win.tree.topLevelItem(i).text(0) == "Imported (OpenSSH)" for i in range(win.tree.topLevelItemCount())) and "web.example.com" in (HOME / ".techxterminal" / "sessions.json").read_text())

# ---- dialogs ----------------------------------------------------------------------------------
dlg = SessionDialog(win.store, None, win)
dlg.show()
pump(0.3)
check("session dialog: jump host and tunnels for SSH, format for serial", dlg.jump_host.isVisible() and dlg.tunnels.isVisible() and not dlg.serial_format.isVisible())
left, right = dlg.connection_section, dlg.listing_section
check("session dialog: two columns side by side at its normal width", dlg.width() >= dlg.TWO_COLUMN_WIDTH and right.x() > left.x() + left.width() - 5 and abs(right.y() - left.y()) < 4 and dlg.logon_section.x() == left.x() and dlg.routing_section.x() == left.x() and dlg.extras_section.x() == right.x() and dlg.terminal_section.x() == right.x() and left.y() < dlg.logon_section.y() < dlg.routing_section.y() and right.y() < dlg.extras_section.y() < dlg.terminal_section.y() and abs(left.width() - right.width()) < 12, f"{dlg.width()} {left.geometry().getRect()} {right.geometry().getRect()}")
middle = dlg.width() // 2
check("session dialog: what is needed to connect is in the left column, the rest on the right", all(field.mapTo(dlg, _QPoint(0, 0)).x() < middle for field in (dlg.host, dlg.port, dlg.username, dlg.auth, dlg.password_edit, dlg.jump_host, dlg.tunnels)) and all(field.mapTo(dlg, _QPoint(0, 0)).x() > middle for field in (dlg.name, dlg.group, dlg.notes, dlg.startup, dlg.auto_responses, dlg.encoding)), str(middle))
focus_chain = []
focused = dlg.host
while focused not in focus_chain:
    focus_chain.append(focused)
    focused = focused.nextInFocusChain()
tab_order = [dlg.host, dlg.port, dlg.command_line, dlg.username, dlg.auth, dlg.password_edit, dlg.save_password, dlg.wait_ms, dlg.jump_host, dlg.tunnels, dlg.name, dlg.group, dlg.notes, dlg.startup, dlg.auto_responses, dlg.encoding, dlg.backspace, dlg.enter_key]
tab_positions = [focus_chain.index(field) for field in tab_order]
check("session dialog: Tab goes down the left column, then down the right", tab_positions == sorted(tab_positions), repr(tab_positions))
check("session dialog: nothing needs scrolling when it opens", not dlg._scroll.verticalScrollBar().isVisible(), f"{dlg.height()}")
dlg.grab().save(str(SCRATCH / "shot_dialog_two_columns.png"))
wide_size = dlg.size()
dlg.resize(520, 520)
pump(0.3)
stacked = [dlg.connection_section, dlg.logon_section, dlg.routing_section, dlg.listing_section, dlg.extras_section, dlg.terminal_section]
check("session dialog: narrows to one column, with scrolling", all(below.x() == above.x() and below.y() > above.y() + above.height() - 5 for above, below in zip(stacked, stacked[1:])) and dlg._scroll.verticalScrollBar().isVisible() and not dlg._scroll.horizontalScrollBar().isVisible(), f"{left.geometry().getRect()} {right.geometry().getRect()}")
dlg.grab().save(str(SCRATCH / "shot_dialog_one_column.png"))
dlg.resize(wide_size)
pump(0.3)
check("session dialog: widening restores two columns", right.x() > left.x() and abs(right.y() - left.y()) < 4)
dlg._protocol_buttons["serial"].setChecked(True)
pump(0.2)
check("session dialog: serial shows format and flow control instead", not dlg.jump_host.isVisible() and dlg.serial_format.isVisible() and dlg.serial_flow.isVisible())
dlg.host.setText("COM7")
dlg.serial_format.setCurrentText("7e1")
dlg.serial_flow.setCurrentIndex(dlg.serial_flow.findData("rtscts"))
made = dlg.session()
check("session dialog: serial values saved", (made.serial_format, made.serial_flow, made.jump_host) == ("7E1", "rtscts", ""), repr(made))
dlg.grab().save(str(SCRATCH / "shot_dialog_serial.png"))
dlg._protocol_buttons["ssh"].setChecked(True)
dlg.host.setText("h")
dlg.username.setText("u")
dlg.jump_host.setText("me@gate")
dlg.tunnels.setText("8080:localhost:80")
made = dlg.session()
check("session dialog: SSH extras saved", (made.jump_host, made.tunnels) == ("me@gate", "8080:localhost:80"))
pump(0.2)  # the rows hidden for serial give their room back on the next layout pass
dlg.grab().save(str(SCRATCH / "shot_dialog.png"))
dlg.close()

sd4 = SettingsDialog(win.prefs, win)
sd4.applied.connect(win.apply_preferences)
sd4.show()
sd4.show_page("about")
pump(0.3)
from techxterminal import AUTHOR as _AUTHOR, __author__ as _author
about_text = " ".join(label.text() for label in sd4.pages.widget(sd4._page_index["about"]).findChildren(type(sd4.preview)))
check("about: author and licence are shown", _AUTHOR == _author == "Venkata S Kuntamukkala" and "Venkata S Kuntamukkala" in about_text and "GNU GPL v3" in about_text, about_text[:120])
check("about: the author is named once, on a line of their own, and there is no credit line", about_text.count("Kuntamukkala") == 1 and sd4.author.text() == "Author: Venkata S Kuntamukkala" and "Made with" not in about_text, about_text[:200])
from techxterminal import CAPTION as _CAPTION

brand_pos, credit_pos = win.brand_text.mapTo(win, _QPoint(0, 0)), win.caption.mapTo(win, _QPoint(0, 0))
welcome_text = " ".join(label.text() for label in win.stack.widget(0).findChildren(type(win.caption)))
check("caption: says what the program is for, just below its name in the top bar, with no credit line anywhere", _CAPTION == "Modern Shell Terminal" and win.caption.text() == _CAPTION and not win.caption.isHidden() and win.caption.toolTip() == f"Tech X Terminal {_version}" and credit_pos.y() >= brand_pos.y() + win.brand_text.height() and credit_pos.x() <= brand_pos.x() and "Made with" not in about_text and "Made with" not in welcome_text and "©" not in about_text, f"{win.caption.text()} {brand_pos} {credit_pos}")
from techxterminal import LICENSE_URL as _LICENSE_URL, __version__ as _version
from techxterminal.dialogs import version_info as _version_info

about_buttons = [button.text() for button in sd4.pages.widget(sd4._page_index["about"]).findChildren(QPushButton)]
sd4.copy_version.click()
copied = QApplication.clipboard().text()
told_copied = sd4.copy_version.text()
check("about: version, buttons and a version report to copy", f"Version {_version}" in about_text and "modern tabbed SSH terminal" in about_text and sorted(about_buttons) == sorted(["Copy version info", "Open data folder", "Read the licence", "Third-party licences", "Release notes"]) and copied == _version_info() and copied.startswith(f"Tech X Terminal {_version}\n") and all(f"{label}: " in copied for label in ("Python", "Qt", "SSH library", "System")) and _LICENSE_URL.startswith("https://www.gnu.org/licenses/gpl-3.0"), copied)
pump(1.8)
check("about: Copy version info says it has copied, then goes back to its name", told_copied == "Copied" and sd4.copy_version.text() == "Copy version info", repr((told_copied, sd4.copy_version.text())))

# The licences and release notes are read in the program, from what ships with it.
import techxterminal.dialogs as _dialogs
from PyQt6.QtWidgets import QSizePolicy
from techxterminal import licences as _licences
from techxterminal.dialogs import TextViewer as _TextViewer
from techxterminal.sessions import CONFIG_DIR as _CONFIG_DIR


def read_in_viewer(button_text):
    """Press an About button and report what the window it opens shows."""
    seen = {}

    def look():
        viewer = app.activeModalWidget()
        if isinstance(viewer, _TextViewer):
            seen["title"], seen["text"] = viewer.windowTitle(), viewer.view.toPlainText()
            viewer.accept()

    QTimer.singleShot(400, look)
    next(button for button in sd4.pages.widget(sd4._page_index["about"]).findChildren(QPushButton) if button.text() == button_text).click()
    return seen


licence_view = read_in_viewer("Read the licence")
third_party_view = read_in_viewer("Third-party licences")
notes_view = read_in_viewer("Release notes")
check("about: the licence is read in the program, not on a web page", licence_view.get("title") == "Licence" and "GNU GENERAL PUBLIC LICENSE" in licence_view.get("text", "") and "Version 3, 29 June 2007" in licence_view.get("text", ""), repr(licence_view.get("title")))
check("about: the licences of what it is built on can be read, LGPL text included", third_party_view.get("title") == "Third-Party Licences" and "paramiko" in third_party_view.get("text", "") and "GNU LESSER GENERAL PUBLIC LICENSE" in third_party_view.get("text", "") and "Ubuntu Font Licence" in third_party_view.get("text", ""), repr(third_party_view.get("title")))
check("about: the release notes can be read", notes_view.get("title") == "Release Notes" and notes_view.get("text", "").startswith("# Changes") and "Tech X Terminal" in notes_view.get("text", ""), repr(notes_view.get("title")))

opened_addresses = []


class _FakeDesktop:
    @staticmethod
    def openUrl(url):
        opened_addresses.append(url.toString())
        return True


real_desktop = _dialogs.QDesktopServices
_dialogs.QDesktopServices = _FakeDesktop
next(button for button in sd4.pages.widget(sd4._page_index["about"]).findChildren(QPushButton) if button.text() == "Open data folder").click()
sd4._read("Missing", lambda: None, "https://example.test/licence")  # a text that is not there: its web page instead
sd4._read("Missing", lambda: None)  # and nothing to open
_dialogs.QDesktopServices = real_desktop
check("about: Open data folder shows the folder the program keeps its files in, and a missing text falls back to its web page", opened_addresses == [_QUrl.fromLocalFile(str(_CONFIG_DIR)).toString(), "https://example.test/licence"] and _CONFIG_DIR.is_dir(), repr(opened_addresses))
check("about: the data folder is shown shortened to fit, with all of it in the tooltip", sd4.data_folder_label.toolTip() == str(_CONFIG_DIR) and sd4.data_folder_label.sizePolicy().horizontalPolicy() == QSizePolicy.Policy.Ignored)

viewer = _TextViewer("Sample", "alpha\nbeta\nGamma delta")
viewer.show()
viewer.search.setText("gam")
found_first = viewer.view.textCursor().selectedText()
viewer.search.setText("zzz")
not_found = viewer.found.text()
viewer.search.setText("a")
QTest.keyClick(viewer.search, Qt.Key.Key_Return)  # next match; must neither close the window nor press a button
still_open = viewer.isVisible()
check("about: the text window finds text from the top, says when it is not there, and Enter in its search does not close it", found_first == "Gam" and not_found == "Not found" and still_open, repr((found_first, not_found, still_open)))
viewer.close()
check("about: the licences and notes are found where a build or the source keeps them", _licences.find("LICENSE") is not None and _licences.find("no such file.txt") is None and "paramiko" in _licences.third_party_notices() and _licences.release_notes().startswith("# Changes"))
sd4.grab().save(str(SCRATCH / "shot_settings_about.png"))
sd4.pages.setCurrentIndex(2)
pump(0.2)
sd4.grab().save(str(SCRATCH / "shot_settings_behaviour.png"))
sd4.set_preferences(_replace(win.prefs, confirm_paste=False, auto_reconnect=True, auto_log=True, log_dir=str(WORK / "x"), stack_panes=True))
got = sd4.preferences()
check("settings: new options round-trip", (got.confirm_paste, got.auto_reconnect, got.auto_log, got.log_dir, got.stack_panes) == (False, True, True, str(WORK / "x"), True))
sd4._apply()
pump(0.3)
check("settings: Stack split panes applies", win.panes_stacked())
win.apply_preferences(_replace(win.prefs, confirm_paste=True, auto_reconnect=False, auto_log=False, log_dir="", stack_panes=False))
sd4.close()
requirements = (ROOT / "requirements.txt").read_text().lower()
check("requirements: build and lint tools are listed", "pyinstaller" in requirements and "pyflakes" in requirements)
licence = (ROOT / "LICENSE").read_text()
notice = (ROOT / "NOTICE").read_text()
from techxterminal import LICENSE_NAME as _LICENSE_NAME
check("licence: GNU GPL v3 in LICENSE and NOTICE, and no address of a project page in NOTICE", licence.lstrip().startswith("GNU GENERAL PUBLIC LICENSE") and "Version 3, 29 June 2007" in licence and "Venkata S Kuntamukkala" in notice and "GNU General Public License, version 3" in notice and "MIT" not in notice and "github.com" not in notice and _LICENSE_NAME == "GNU GPL v3")

import techxterminal as _techxterminal_package
shipped_text = [ROOT / "NOTICE", ROOT / "README.md", ROOT / "packaging" / "installer.iss", *sorted((ROOT / "techxterminal").glob("*.py"))]
check("no address of a public project page is shipped", not any(("kvs" + "hrao") in path.read_text(encoding="utf-8", errors="ignore") for path in shipped_text) and not hasattr(_techxterminal_package, "PROJECT_URL"))

from techxterminal import APP_NAME as _APP_NAME

spelled_out = [path.name for path in (ROOT / "techxterminal").glob("*.py") if "TechX Terminal" in path.read_text(encoding="utf-8")]
installer_script = (ROOT / "packaging" / "installer.iss").read_text()
check("app name: Tech X Terminal, defined once and shown everywhere", _APP_NAME == "Tech X Terminal" and not spelled_out and win.brand_text.text() == _APP_NAME and _APP_NAME in about_text and "TechX Terminal" not in notice and 'AppName "Tech X Terminal"' in installer_script and (ROOT / "README.md").read_text().startswith("# Tech X Terminal"), repr(spelled_out))

# ---- shortcuts, notices, palette, saved command placeholders, broadcast ------------------------------
from PyQt6.QtGui import QShortcut as _QShortcut
from PyQt6.QtWidgets import QTabBar
from techxterminal.mainwindow import SHORTCUTS as _SHORTCUTS
from techxterminal.snippets import fill_placeholders as _fill
from techxterminal.theme import COLORS as _COLORS

bound = sorted(shortcut.key().toString() for shortcut in win.findChildren(_QShortcut) if shortcut.parent() is win)
table_keys = [keys for keys, _what in _SHORTCUTS.values()]
check("shortcuts: every shortcut in the table is bound once, and no two share keys", len(set(table_keys)) == len(table_keys) and all(bound.count(keys) == 1 for keys in table_keys), repr(bound))
offered = {keys for _title, keys, _handler in win.commands() if keys}
check("shortcuts: the command palette shows the same keys as the table", offered <= set(table_keys) and {"Ctrl+Shift+N", "Ctrl+Shift+H", "Ctrl+Shift+L"} <= offered, repr(offered - set(table_keys)))
mainwindow_source = (ROOT / "techxterminal" / "mainwindow.py").read_text(encoding="utf-8")
table_end = mainwindow_source.index("def keys_for(")  # after the table and the terminal key list
import re as _re

# String literals and menu hints that spell out keys; docstrings and the generated Alt+n bindings are fine.
stray_keys = _re.findall(r'(?<!")"(?:Ctrl|Alt|Shift)\+[^"{]*"|\\t(?:Ctrl|Alt|Shift)\+', mainwindow_source[table_end:])
check("shortcuts: keys are not written out anywhere else in the main window", stray_keys == [], repr(stray_keys))

reference = win.show_shortcuts()
pump(0.3)
everything = reference.visible_entries()
check("shortcut reference: lists every window and terminal shortcut", all(what in everything for _keys, what in _SHORTCUTS.values()) and "Paste" in everything and "Jump to a tab" in everything and reference.isVisible(), repr(everything[:4]))
reference.search.setText("pane")
pump(0.1)
check("shortcut reference: the search narrows the list", reference.visible_entries() == ["Split / move to other pane", "Stack panes / side by side"] and not reference.empty.isVisible(), repr(reference.visible_entries()))
reference.search.setText("no such key at all")
pump(0.1)
check("shortcut reference: says so when nothing matches", reference.visible_entries() == [] and reference.empty.isVisible())
reference.grab().save(str(SCRATCH / "shot_shortcuts.png"))
reference.close()

win.notify("all went well", "success")
success_level = win.statusBar().property("level")
win.notify("that went wrong", "error")
error_level = win.statusBar().property("level")
win.statusBar().clearMessage()
pump(0.1)
check("notices: successes and errors are told apart, and the colour goes with the message", (success_level, error_level, win.statusBar().property("level")) == ("success", "error", "info"))

close_button = win.tabs.tabBar().tabButton(win.tabs.indexOf(tlog), QTabBar.ButtonPosition.RightSide)
check("tabs: the close button is a drawn icon like the others, not a text character", close_button is not None and close_button.text() == "" and not close_button.icon().isNull() and "Ctrl+Shift+W" in close_button.toolTip())
from techxterminal import monitor as _monitor

win.tabs.setCurrentIndex(win.tabs.indexOf(tlog))
win._set_active_pane(win.tabs)
tlog.server_stats = None
win._probe_server()
pump(10, lambda: getattr(tlog, "server_stats", None) is not None)
monitor_text = win.status_right.text()
check("server monitor: the active SSH tab's load, memory and disk are in the status bar, a full disk in red", tlog.server_stats == _monitor.ServerStats(0.42, 4, 75, 96) and "CPU 0.42" in monitor_text and "Mem 75%" in monitor_text and f'color:{_theme.COLORS["red"]}">Disk 96%' in monitor_text and "System disk 96% full" in win.status_right.toolTip(), monitor_text)
check("server monitor: what a server without the figures prints is not taken for them", _monitor.parse_stats("ran: cat /proc/loadavg\nsecond line\n") is None and _monitor.parse_stats("") is None)
latency = getattr(tlog, "latency", None)
check("server monitor: the round trip to the server is measured and shown first", latency is not None and 0 <= latency < 5 and f"{latency * 1000:.0f} ms" in monitor_text and "Round trip" in win.status_right.toolTip(), repr(latency))
tlog.stats_failures = _monitor.GIVE_UP_AFTER  # as for a server without the figures
tlog.server_stats = None
tlog.latency = None
win._probe_server()
pump(10, lambda: tlog.latency is not None)
check("server monitor: a server without the figures still has its round trip shown", tlog.latency is not None and tlog.server_stats is None and " ms" in win.status_right.text() and "CPU" not in win.status_right.text(), win.status_right.text())
tlog.stats_failures = 0
win.apply_preferences(_replace(win.prefs, server_monitor=False))
pump(0.2)
check("server monitor: Settings turns it off", "CPU" not in win.status_right.text() and " ms" not in win.status_right.text() and win.status_right.toolTip() == "")
win.apply_preferences(_replace(win.prefs, server_monitor=True))
from techxterminal.monitorview import nice_top as _nice_top
win._probe_server()  # a fresh reading with the figures, after the checks above that went without
pump(10, lambda: tlog.monitor_history and tlog.monitor_history[-1].stats is not None)
QTest.mouseClick(win.status_right, Qt.MouseButton.LeftButton)
pump(0.3)
monitor_window = win._monitor_window
memory_value = monitor_window.values["memory"][0].text() if monitor_window else ""
check("server monitor: a click on the figures opens their last half hour, for the tab in front", monitor_window is not None and monitor_window.isVisible() and len(tlog.monitor_history) >= 2 and tlog.session.address in monitor_window.heading.text() and memory_value == "75%" and "Critical" in monitor_window.values["disk"][1].text() and not monitor_window.empty.isVisible(), repr((memory_value, len(getattr(tlog, "monitor_history", [])))))
check("server monitor: the command palette opens it too, and its scales end on round numbers", "Server Monitor" in [title for title, _keys, _handler in win.commands()] and [_nice_top(v) for v in (213, 4.0, 0.7, 130)] == [250, 5, 1.0, 200])
monitor_window.close()

# Alerts: the test server's disk is 96% full, which is critical, and its tab is put behind another.
in_front = win.open_local_shell("cmd.exe")
pump(0.5)
win.notifier.shown.clear()
win.statusBar().clearMessage()
tlog.alerting = set()


def alerts_told():
    told = [message for _title, message in win.notifier.shown] + [win.statusBar().currentMessage()]
    return sum("System disk 96% full" in text for text in told)


win._probe_background()
pump(10, lambda: alerts_told() > 0)
first_alerts = alerts_told()
win.notifier.shown.clear()
win.statusBar().clearMessage()
win._probe_background()
pump(3, lambda: id(tlog) not in win._stats_probes)
check("server alerts: a critical figure on a server in a tab behind is told once, not at every reading", first_alerts == 1 and alerts_told() == 0 and "disk" in tlog.alerting, repr((first_alerts, tlog.alerting)))
check("server alerts: the words say what is critical", _monitor.critical(_monitor.ServerStats(0.4, 4, 50, 96), 0.5) == {"latency": "Round trip 500 ms", "disk": "System disk 96% full"} and _monitor.critical(_monitor.ServerStats(0.4, 4, 50, 60), 0.02) == {})
win.apply_preferences(_replace(win.prefs, server_alerts=False))
tlog.alerting = set()
win._probe_background()
pump(2)
check("server alerts: Settings turns them off, and tabs behind are then not measured", alerts_told() == 0 and id(tlog) not in win._stats_probes)
win.apply_preferences(_replace(win.prefs, server_alerts=True))
win.close_tab(in_front, force=True)
pump(0.3)

# Saving what quick connect opened
saved_before = len(win.store.sessions)
win.quick.setText(f"quickuser@127.0.0.1:{PORT}")
win._quick_connect()
quick_tab = win.current_tab()
offered = win.notice_action.text()
menu_before = [action.text() for action in win.tab_menu(quick_tab).actions()]
saved = win.save_tab_as_session(quick_tab)
menu_after = [action.text() for action in win.tab_menu(quick_tab).actions()]
check("quick connect: an address is offered to be saved, and saving keeps the tab as that session", offered == "Save" and "Save as Session" in menu_before and "Save as Session" not in menu_after and saved is not None and len(win.store.sessions) == saved_before + 1 and win.store.get(quick_tab.session.id) is not None and not saved.save_password and win.save_tab_as_session(quick_tab) is None, repr((offered, saved)))
win.close_tab(quick_tab, force=True)
win.store.remove(win.store.get(saved.id))
win._reload_tree()
close_faces = lambda: [win.tabs.tabBar().tabButton(i, QTabBar.ButtonPosition.RightSide).icon().cacheKey() == win._close_icon().cacheKey() for i in range(win.tabs.count())]
second_tab = win.open_local_shell("cmd.exe")  # a tab that is not the current one, to point at
win.tabs.setCurrentIndex(win.tabs.indexOf(tlog))
pump(0.2)
win._paint_close_buttons(win.tabs, -1)
at_rest = close_faces()
other = next(i for i in range(win.tabs.count()) if i != win.tabs.currentIndex())
win._paint_close_buttons(win.tabs, other)
pointed_at = close_faces()
win._paint_close_buttons(win.tabs, -1)
check("tabs: only the current tab and the one pointed at show their close cross", win.tabs.count() >= 2 and at_rest == [i == win.tabs.currentIndex() for i in range(win.tabs.count())] and pointed_at[other] and pointed_at[win.tabs.currentIndex()], repr((at_rest, pointed_at)))
win.close_tab(second_tab, force=True)
pump(0.2)

check("placeholders: host, user, port and name are filled in; other braces are left alone", _fill("ssh {user}@{host} -p {port} # {name} awk '{print $1}' {nope}", tlog.session) == f"ssh deploy@127.0.0.1 -p {PORT} # {tlog.session.title} awk '{{print $1}}' {{nope}}", _fill("{user}@{host}:{port}", tlog.session))
win.tabs.setCurrentIndex(win.tabs.indexOf(tlog))
win._set_active_pane(win.tabs)
pump(0.2)
win.snippets.replace_all([Snippet("Where am I", "echo at-{user}@{host}:{port}")])
palette = win.show_switcher()
palette.query.setText("where am")
found_snippet = palette.shown()
QTest.keyClick(palette.query, Qt.Key.Key_Return)
pump(0.9)
check("palette: a saved command can be found and sent, with its placeholders filled in", found_snippet == [("snippet", "Where am I")] and f"ran: echo at-deploy@127.0.0.1:{PORT}" in text(tlog), ascii(text(tlog)[-160:]))
palette = win.show_switcher()
palette.query.setText("settings")
ranked = palette.shown()
palette.close()
check("palette: the best match comes first", ranked[:1] == [("action", "Settings")], repr(ranked[:3]))
win.save_workspace("Evening")
palette = win.show_switcher()
palette.query.setText("evening")
found_workspace = palette.shown()
before_count = len(win.open_tabs())
QTest.keyClick(palette.query, Qt.Key.Key_Return)
pump(0.5)
evening_tabs = [tab for tab in win.open_tabs()][before_count:]
check("palette: a workspace can be opened from it", found_workspace == [("workspace", "Evening")] and len(evening_tabs) == len(win.workspaces.get("Evening")) > 0, repr(found_workspace))
for tab in evening_tabs:
    win.close_tab(tab, force=True)
pump(0.3)
palette = win.show_switcher()
palette.query.setText("qqqqzzzz")
check("palette: says so when nothing matches", palette.shown() == [] and palette.empty.isVisible() and not palette.empty.isHidden())
palette.close()

QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.StandardButton.No)
kept = not win.delete_workspace("Evening") and win.workspaces.names() == ["Evening"]
QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes)
deleted = win.delete_workspace("Evening") and win.workspaces.names() == []
QMessageBox.question = orig_question
check("workspaces: deleting one asks first, like every other delete", kept and deleted, repr(win.workspaces.names()))

win.all_tabs.setChecked(True)
pump(0.1)
broadcasting = (win.command_bar.property("broadcast"), win.command.placeholderText())
win.all_tabs.setChecked(False)
pump(0.1)
check("broadcast: the send bar is marked while it sends to every tab, and says to how many", broadcasting[0] is True and "connected tab" in broadcasting[1] and win.command_bar.property("broadcast") is False and "active tab" in win.command.placeholderText(), repr(broadcasting))
win.snippets.replace_all([])

count_before = len(win.store.sessions)
status_updates = []
real_update_status = win._update_status
win._update_status = lambda: (status_updates.append(len(win.store.sessions)), real_update_status())
win._duplicate_session(win.store.get(tlog.session.id))
copy = next(s for s in win.store.sessions if s.name.endswith("(copy)"))
copy_offer = (win.statusBar().currentMessage(), win.notice_action.isVisible(), win.notice_action.text())
QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes)
win._delete_session(copy)
QMessageBox.question = orig_question
del win._update_status
check("sidebar: duplicating or deleting a session refreshes the saved count", count_before + 1 in status_updates and status_updates[-1] == count_before and len(win.store.sessions) == count_before, repr(status_updates))
check("sidebar: a duplicate says what it is called and offers to edit it", copy_offer == (f"Saved a copy as “{copy.title}”", True, "Edit"), repr(copy_offer))
delete_offer = (win.statusBar().currentMessage(), win.notice_action.text())
win.notice_action.click()
check("sidebar: a deleted session can be brought back from the status bar", delete_offer == (f"Deleted “{copy.title}”", "Undo") and win.store.get(copy.id) == copy and _Store().get(copy.id) is not None and "is back" in win.statusBar().currentMessage(), repr(delete_offer))
win.store.remove(copy)
win._reload_tree()

win.activateWindow()
win.raise_()
pump(3, lambda: win.isActiveWindow())
win.search.setText("zz-nothing-zz")
pump(0.1)
check("sidebar search: says when nothing matches", win.empty_hint.isVisible() and "zz-nothing-zz" in win.empty_hint.text(), win.empty_hint.text())
QTest.keyClick(win.search, Qt.Key.Key_Escape)
check("sidebar search: Esc clears it", win.search.text() == "" and not win.empty_hint.isVisible())
win.search.setFocus()
win.search.setText("staging")
first_match = win._item_session(win._session_rows()[0])
QTest.keyClick(win.search, Qt.Key.Key_Down)
check("sidebar search: Down moves into the list, onto the first match", win.focusWidget() is win.tree and win._item_session(win.tree.currentItem()) == first_match, repr((win.focusWidget(), first_match.name)))
asked = []
QMessageBox.question = staticmethod(lambda *a, **k: (asked.append(a[1]), QMessageBox.StandardButton.No)[1])
QTest.keyClick(win.tree, Qt.Key.Key_Delete)
QMessageBox.question = orig_question
check("sidebar: Delete on a session asks before deleting it", asked == ["Delete Session"] and win.store.get(first_match.id) is not None, repr(asked))
star_item = win._session_rows()[0]
star_id = win._item_session(star_item).id
was_favourite = win.store.get(star_id).favourite
tabs_before_star = len(win.open_tabs())


def click_star(double=False):
    item = next(item for item in win._session_rows() if win._item_session(item).id == star_id)
    row_rect = win.tree.visualItemRect(item)
    point = _QPoint(row_rect.right() - 2 - 4 - 11, row_rect.center().y())  # the middle of the star
    (QTest.mouseDClick if double else QTest.mouseClick)(win.tree.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, point)
    pump(0.3)
    return win.store.get(star_id).favourite


starred = click_star()
after_double = click_star(double=True)
unstarred = click_star()
check("sidebar: the star on a row makes the session a favourite and back, and a double click on it opens nothing", starred == (not was_favourite) and after_double == starred and unstarred == was_favourite and len(win.open_tabs()) == tabs_before_star, repr((was_favourite, starred, after_double, unstarred, len(win.open_tabs()) - tabs_before_star)))
tabs_before = len(win.open_tabs())
win.search.setFocus()
win.search.setText("staging")
QTest.keyClick(win.search, Qt.Key.Key_Return)
pump(0.3)
opened = win.open_tabs()[tabs_before:]
check("sidebar search: Enter opens the first match and clears the search", len(opened) == 1 and opened[0].session.id == first_match.id and win.search.text() == "", repr([tab.title for tab in opened]))
win.focus_session_search()
check("sidebar search: has its own shortcut", win.search.hasFocus() and any(keys == "Ctrl+Shift+S" for keys, _what in _SHORTCUTS.values()))
pump(15, lambda: opened[0].state == "connected")
tip = win._pane_of(opened[0]).tabToolTip(win._pane_of(opened[0]).indexOf(opened[0]))
check("tabs: the tooltip tells the protocol, address and state", "SSH" in tip and opened[0].session.address in tip and "Connected" in tip, repr(tip))
check("tabs: the disconnected banner says that Enter reconnects", opened[0].enter_hint.text() == "or press Enter")
win.close_tab(opened[0], force=True)

tabs_before = len(win.open_tabs())
win.quick.setText("staging-api")
win._quick_connect()
pump(0.3)
by_name = win.open_tabs()[tabs_before:]
check("quick connect: the name of a saved session opens that session", len(by_name) == 1 and by_name[0].session.id == next(s.id for s in win.store.sessions if s.name == "staging-api") and "staging-api" not in win.quick_history())
for tab in by_name:
    win.close_tab(tab, force=True)
win.quick.setText("someone@127.0.0.1:1")  # nothing listens there; only the history matters
win._quick_connect()
pump(0.3)
suggested = win.quick.completer().model().stringList()
check("quick connect: addresses are remembered and suggested first, then session names", win.quick_history()[0] == "someone@127.0.0.1:1" and suggested[0] == "someone@127.0.0.1:1" and "staging-api" in suggested, repr(suggested[:4]))
suggestion_model = win.quick.completer().model()
suggestion_icon = lambda text: suggestion_model.data(suggestion_model.index(suggested.index(text)), Qt.ItemDataRole.DecorationRole)
check("quick connect: a typed address and a saved session are marked differently", suggestion_icon("someone@127.0.0.1:1").cacheKey() != suggestion_icon("staging-api").cacheKey() and suggestion_icon("staging-api").cacheKey() == suggestion_icon("staging-api").cacheKey())
win.close_tab(win.open_tabs()[-1], force=True)
win.quick.setText("not an address at all")
win._quick_connect()
check("quick connect: something it cannot use is explained", "name of a saved session" in win.statusBar().currentMessage() and win.statusBar().property("level") == "error")

from techxterminal.sftp import LocalFileModel as _LocalFileModel

listing = WORK / "listing"
listing.mkdir(exist_ok=True)
(listing / "sized.bin").write_bytes(b"x" * 2048)
local_model = _LocalFileModel()
root_index = local_model.setRootPath(str(listing))
pump(5, lambda: local_model.rowCount(root_index) > 0)
row = local_model.index(str(listing / "sized.bin"))
size_text = local_model.data(row.siblingAtColumn(1))
date_text = local_model.data(row.siblingAtColumn(3))
check("file browser: this computer's side shows sizes, dates and headings like the remote side", size_text == "2.0 KB" and len(date_text) == 16 and date_text[4] == "-" and local_model.headerData(3, Qt.Orientation.Horizontal) == "Modified", repr((size_text, date_text)))

tlog.show_find()
tlog.find_bar.query.setText("no-such-text-anywhere-42")
tlog.find_bar.search()
red_when_missing = tlog.find_bar.query.property("nomatch")
tlog.find_bar.query.setText("deploy")
tlog.find_bar.search()
check("find: the field turns red when nothing matches, and back when something does", red_when_missing is True and tlog.find_bar.query.property("nomatch") is False)
tlog.find_bar.close_bar()

extra_tab = win.open_session(win.store.get(tlog.session.id))
pump(15, lambda: extra_tab.state == "connected")
extra_tab.custom_title = "reopen me"
win.close_tab(extra_tab, force=True)
pump(0.2)
offered = (win.statusBar().currentMessage(), win.notice_action.isVisible(), win.notice_action.text())
tabs_before = len(win.open_tabs())
win.notice_action.click()
pump(0.3)
back = win.open_tabs()[tabs_before:]
check("closed tabs: closing one offers to reopen it, with its own name", offered == ("Closed “reopen me”", True, "Reopen") and len(back) == 1 and back[0].title == "reopen me" and back[0].session.id == tlog.session.id and not win.notice_action.isVisible(), repr(offered))
pump(15, lambda: back[0].state == "connected")
spare = win.open_session(win.store.get(tlog.session.id))
pump(15, lambda: spare.state == "connected")
for tab in (back[0], spare):
    tab.disconnect_from_host()
pump(0.3)
closed_count = win.close_disconnected()
group_message = win.statusBar().currentMessage()
reopened_group = win.reopen_closed_tab()
check("closed tabs: tabs closed together come back together", closed_count >= 2 and group_message == f"Closed {closed_count} tabs" and len(reopened_group) == closed_count, repr((closed_count, group_message)))
win._close_several(reopened_group)
win._closed_tabs.clear()
check("closed tabs: with nothing to reopen it says so", win.reopen_closed_tab() == [] and "no closed tab" in win.statusBar().currentMessage())

background = win.open_session(win.store.get(tlog.session.id))
pump(15, lambda: background.state == "connected")
win.tabs.setCurrentIndex(win.tabs.indexOf(tlog))
pump(0.2)
background.disconnect_from_host()
pump(0.2)
dropped = (win.statusBar().currentMessage(), win.statusBar().property("level"), win.notice_action.text(), label(background))
win.notice_action.click()
check("background tabs: losing the connection out of sight is reported, with a way to reconnect", dropped[0] == f"{win._tab_name(background)} is disconnected" and win._tab_name(background) != background.title and dropped[1] == "error" and dropped[2] == "Reconnect" and dropped[3].endswith("•") and pump(15, lambda: background.state == "connected"), repr(dropped))
win.close_tab(background, force=True)
win.statusBar().clearMessage()

ordering = SnippetsDialog([Snippet("first", "a"), Snippet("second", "b"), Snippet("third", "c")], win)
ordering.list.setCurrentRow(2)
up_enabled_at_bottom = ordering.up_button.isEnabled() and not ordering.down_button.isEnabled()
ordering.move_command(-1)
ordering.move_command(-1)
check("saved commands: can be put in another order, which is the menu's order", up_enabled_at_bottom and [s.name for s in ordering.snippets()] == ["third", "first", "second"] and [ordering.list.item(i).text() for i in range(3)] == ["third", "first", "second"] and ordering.list.currentRow() == 0 and not ordering.up_button.isEnabled())
ordering.reject()

# ---- broadcast to chosen tabs, recent sessions, slow paste, coloured output ------------------------
from techxterminal.mainwindow import time_ago as _time_ago
from techxterminal.sidebar import ROLE_IS_GROUP as _ROLE_IS_GROUP

chosen = [win.open_session(win.store.get(tlog.session.id)) for _ in range(2)]
pump(20, lambda: all(tab.state == "connected" for tab in chosen))
left_out = chosen[1]
win.set_in_broadcast(tlog, True)
win.set_in_broadcast(chosen[0], True)
win.all_tabs.setChecked(True)
pump(0.1)
marked_label = win.all_tabs.text()
placeholder = win.command.placeholderText()
win.command.setText("echo to-the-marked-ones")
win._send_command()
pump(0.9)
check("broadcast: with tabs marked, the send bar goes to those only, and says so", "ran: echo to-the-marked-ones" in text(tlog) and "ran: echo to-the-marked-ones" in text(chosen[0]) and "to-the-marked-ones" not in text(left_out) and marked_label == "2 marked tabs" and "2 marked tabs" in placeholder, repr((marked_label, placeholder)))
icon_key = (tlog.state, tlog.session.color, True)
tip = win._pane_of(tlog).tabToolTip(win._pane_of(tlog).indexOf(tlog))
check("broadcast: marked tabs carry a ring on their tab and say so when pointed at", icon_key in win._tab_icons and "receive broadcasts" in tip, repr(tip))
win.clear_broadcast_marks()
check("broadcast: clearing the marks goes back to every tab", win.broadcast_targets() == win.open_tabs() and win.all_tabs.text() in ("All tabs", "All"))
win.all_tabs.setChecked(False)
left_out.disconnect_from_host()
pump(0.3)
win._place(left_out, win._pane_of(left_out))
win._set_active_pane(win._pane_of(left_out))
pump(0.2)
win.command.setText("echo nowhere")
win._send_command()
check("send bar: a command for a tab that is not connected stays in the field, and says why with a way to reconnect", win.command.text() == "echo nowhere" and "is not connected" in win.statusBar().currentMessage() and win.notice_action.text() == "Reconnect" and "echo nowhere" not in win.command.history, repr(win.statusBar().currentMessage()))
win.notice_action.click()
pump(15, lambda: left_out.state == "connected")
win._send_command()
pump(0.9)
check("send bar: once connected the same Enter sends it", win.command.text() == "" and "ran: echo nowhere" in text(left_out))

check("recent: how long ago is said in words", (_time_ago(5), _time_ago(60), _time_ago(7200), _time_ago(86400 * 3)) == ("just now", "1 minute ago", "2 hours ago", "3 days ago"))
used = win.last_connected(tlog.session)
check("recent: the time a session connected is remembered", 0 < time.time() - used < 3600 and win.last_connected(Session(name="never")) == 0)
win.set_sidebar_sort("recent")
pump(0.2)
top_row = win.tree.topLevelItem(0)
check("recent: the sidebar can list the session used last first, in one list without folders", win._item_session(top_row) is not None and win.last_connected(win._item_session(top_row)) == max(win.last_connected(s) for s in win.store.sessions) and not any(win.tree.topLevelItem(i).data(0, _ROLE_IS_GROUP) for i in range(win.tree.topLevelItemCount())) and not win.tree.dragEnabled() and "Last connected" in top_row.toolTip(0), top_row.toolTip(0))
win.set_sidebar_sort("name")
pump(0.2)
check("recent: and back to folders by name, remembered", "Production" in headings() and win.tree.dragEnabled() and win.sidebar_sort() == "name")

win.apply_preferences(_replace(win.prefs, paste_line_delay_ms=200))
win.tabs.setCurrentIndex(win.tabs.indexOf(tlog))
tlog.terminal.paste_text("echo slow-1\necho slow-2\necho slow-3\n")
waiting_after_first = tlog.terminal.pasting()
pump(1.2)
slow_text = text(tlog)
check("slow paste: with a pause set, pasted lines go one at a time, in order", waiting_after_first == 2 and tlog.terminal.pasting() == 0 and slow_text.index("ran: echo slow-1") < slow_text.index("ran: echo slow-2") < slow_text.index("ran: echo slow-3"), repr(waiting_after_first))
tlog.terminal.paste_text("\n".join(f"echo never-{i}" for i in range(6)) + "\n")
QTest.keyClick(tlog.terminal, Qt.Key.Key_Escape)
pump(1.0)
check("slow paste: Esc stops a paste that is still going", tlog.terminal.pasting() == 0 and "ran: echo never-5" not in text(tlog) and "ran: echo never-0" in text(tlog))
sd_paste = SettingsDialog(win.prefs, win)
check("slow paste: the pause is a setting", sd_paste.preferences().paste_line_delay_ms == 200 and sd_paste.paste_delay.specialValueText() == "Off")
sd_paste.close()
win.apply_preferences(_replace(win.prefs, paste_line_delay_ms=0))

page_path = WORK / "output.html"
saved_page = win.save_output(tlog, page_path)
page = page_path.read_text(encoding="utf-8")
check("save output: a .html name gives a page that keeps the colours", saved_page and page.startswith("<!doctype html>") and "Welcome to demo-box" in page and "font-weight:bold" in page and "color:#" in page and "\x1b" not in page, page[:120])
for tab in chosen:
    win.close_tab(tab, force=True)
win._closed_tabs.clear()

# ---- connection info, notify when output contains --------------------------------------------------
details = dict(tlog.connection_details())
check("connection info: what the tab knows, and what the SSH server told it", details.get("Protocol") == "SSH" and details.get("State") == "Connected" and "Connected for" in details and details.get("Server software", "").startswith("SSH-2.0") and "SHA256:" in details.get("Host key", "") and details.get("Encryption") and details.get("Tunnels") == "None", repr(details))
info = win.show_connection_info(tlog)
pump(0.2)
info_text = info.as_text()
check("connection info: shown in a window, and copied as aligned text", info.isVisible() and "Host key" in info_text and "SHA256:" in info_text and info_text.splitlines()[0].startswith("Session"), info_text[:120])
info.close()
nowhere = win.open_session(Session(name="nowhere", host="127.0.0.1", port=1, username="u"))
pump(10, lambda: nowhere.state == "disconnected")
nowhere_details = dict(nowhere.connection_details())
check("connection info: a tab that is not connected says so, without server details", nowhere_details.get("State") == "Disconnected" and "Server software" not in nowhere_details and "Connected for" not in nowhere_details, repr(nowhere_details))

seen_words = []
tlog.textSeen.connect(seen_words.append)
check("watch: nothing to watch for, or a broken pattern, is refused", not tlog.watch_for_text("   ") and not tlog.watch_for_text("([", regex=True) and tlog.watched_text() == "")
check("watch: the main window asks and starts the watch", win.watch_for_text(tlog, "Finished-OK") and tlog.watched_text() == "Finished-OK" and "Finished-OK" in win.statusBar().currentMessage())
win.tabs.setCurrentIndex(win.tabs.indexOf(nowhere))  # so that tlog is in the background
pump(0.2)
background_watch = not tlog.isVisible()
tlog._on_data(b"build step 3 ... \x1b[32mfinish")
first_half = list(seen_words)
tlog._on_data(b"ed-ok\x1b[0m\r\n")
pump(0.2)
check("watch: found even when split between packets and coloured, in any case, and only once", first_half == [] and seen_words == ["finished-ok"] and tlog.watched_text() == "" and "appeared in the output" in win.statusBar().currentMessage(), repr(seen_words))
check("watch: a tab in the background is marked", background_watch and label(tlog).endswith("•"), ascii(label(tlog)))
tlog._on_data(b"finished-ok again\r\n")
check("watch: it does not fire again", seen_words == ["finished-ok"])
tlog.textSeen.disconnect(seen_words.append)
win.close_tab(nowhere, force=True)
win._closed_tabs.clear()
win.tabs.setCurrentIndex(win.tabs.indexOf(tlog))
pump(0.2)

# ---- send break, SSH agent forwarding ------------------------------------------------------------
from techxterminal import importers as _importers2

SSH_BREAKS, AGENT_REQUESTS = [], []
_real_handle_request = paramiko.Channel._handle_request


def _spy_request(channel, message):
    """Notes break requests reaching the stand-in server; paramiko itself ignores them."""
    if channel.transport.server_mode:
        start = message.packet.tell()
        if message.get_text() == "break":
            message.get_boolean()
            SSH_BREAKS.append(message.get_int())
            return
        message.packet.seek(start)
    return _real_handle_request(channel, message)


paramiko.Transport._channel_handler_table[paramiko.common.MSG_CHANNEL_REQUEST] = _spy_request  # the table, not the method, is what paramiko calls
Server.check_channel_forward_agent_request = lambda self, channel: AGENT_REQUESTS.append(channel.get_id()) or True

tbrk = win.open_session(Session(name="brk-telnet", protocol="telnet", host="127.0.0.1", port=TELNET_PORT, username="operator"))
pump(10, lambda: tbrk.state == "connected")
raw_before = len(telnet["raw"])
sent_telnet_break = win.send_break(tbrk)
pump(0.5, lambda: b"\xff\xf3" in telnet["raw"][raw_before:])
check("break: a Telnet tab sends IAC BREAK and says so", sent_telnet_break and b"\xff\xf3" in telnet["raw"][raw_before:] and "Break sent" in win.statusBar().currentMessage(), repr(telnet["raw"][raw_before:]))
win.close_tab(tbrk, force=True)

tbrk_raw = win.open_session(Session(name="brk-raw", protocol="raw", host="127.0.0.1", port=RAW_PORT))
pump(10, lambda: tbrk_raw.state == "connected")
check("break: a Raw tab has none, and the status bar says why", not tbrk_raw.can_send_break() and not win.send_break(tbrk_raw) and "no break signal" in win.statusBar().currentMessage(), win.statusBar().currentMessage())
win.close_tab(tbrk_raw, force=True)

tbrk_ser = win.open_session(Session(name="brk-serial", protocol="serial", host="loop://", port=9600))
pump(10, lambda: tbrk_ser.state == "connected")
check("break: a serial line sends one", tbrk_ser.can_send_break() and win.send_break(tbrk_ser))
pump(0.5)
win.close_tab(tbrk_ser, force=True)

tagent = win.open_session(Session(name="agent-box", host="127.0.0.1", port=PORT, username="deploy", forward_agent=True), "secret")
pump(15, lambda: tagent.state == "connected")
pump(1.0, lambda: AGENT_REQUESTS)
agent_details = dict(tagent.connection_details())
check("agent: a session that forwards the agent asks the server for it, before the shell", len(AGENT_REQUESTS) == 1 and agent_details.get("Agent forwarding") == "On", repr((AGENT_REQUESTS, agent_details.get("Agent forwarding"))))
check("agent: the shell still works with the agent forwarded", "deploy@demo-box" in text(tagent), text(tagent)[-200:])
sent_ssh_break = win.send_break(tagent)
pump(1.0, lambda: SSH_BREAKS)
check("break: an SSH tab asks the server to send a break, with its length", sent_ssh_break and SSH_BREAKS == [300], repr(SSH_BREAKS))
win.close_tab(tagent, force=True)
check("agent: off unless the session asks for it", dict(tlog.connection_details()).get("Agent forwarding") == "Off" and len(AGENT_REQUESTS) == 1)
win._closed_tabs.clear()

check("agent: -A in the command line turns it on", apply_command_line(Session(host="h", username="u", command_line="-A -C")).session.forward_agent)
agent_config = WORK / "agent_config"
agent_config.write_text("Host gate\n  HostName 10.9.9.9\n  ForwardAgent yes\nHost plain\n  HostName 10.9.9.8\n", encoding="utf-8")
agent_found = {session.name: session for session in _importers2.from_openssh_config(agent_config)}
saved_agent = _importers2.session_from_saved("fwd", {"Protocol": "ssh", "HostName": "u@10.1.1.2", "AgentFwd": 1})
check("agent: imported from ForwardAgent in an OpenSSH config and from Saved Sessions", agent_found["gate"].forward_agent and not agent_found["plain"].forward_agent and saved_agent.forward_agent)
agent_dialog = SessionDialog(win.store, Session(host="h", username="u", forward_agent=True), win)
kept_on = agent_dialog.session().forward_agent
agent_dialog._protocol_buttons["telnet"].setChecked(True)
check("agent: the session dialog keeps the choice, and only for SSH", kept_on and not agent_dialog.session().forward_agent)
agent_dialog.reject()
paramiko.Transport._channel_handler_table[paramiko.common.MSG_CHANNEL_REQUEST] = _real_handle_request

# ---- automatic responses, send text file ----------------------------------------------------------
from techxterminal import responses as _responses

rules, bad_rules = _responses.parse_rules("--More-- => \\s\n# a comment\n\n[yes/no] => yes\\r\nno arrow here\nempty =>\nbad => \\q\nhex => \\x03")
check("responses: rules are read, comments skipped, mistakes reported", [(rule.pattern, rule.reply) for rule in rules] == [("--More--", " "), ("[yes/no]", "yes\r"), ("hex", "\x03")] and bad_rules == ["no arrow here", "empty =>", "bad => \\q"], repr((rules, bad_rules)))
responder = _responses.AutoResponder(rules)
split_replies = responder.feed("line one\r\n--Mo") + responder.feed("re-- line two [YES/no] ")
check("responses: found across packets and in any case, answered in order", split_replies == [" ", "yes\r"], repr(split_replies))
check("responses: each appearance is answered once", responder.feed(" more output") == [])
fake_now = [0.0]
looping = _responses.AutoResponder(_responses.parse_rules("again => x")[0], clock=lambda: fake_now[0])
loop_answers = sum(len(looping.feed("again ")) for _ in range(40))
check("responses: a rule answering over and over is stopped", looping.stopped and loop_answers == _responses.MAX_RESPONSES, repr(loop_answers))

tresp = win.open_session(
    Session(name="responder", protocol="telnet", host="127.0.0.1", port=TELNET_PORT, username="operator",
            auto_responses="ran: ping => pong-reply\\r"),
    "pw",
)
pump(12, lambda: "Welcome to telnet-box" in text(tresp))
check("responses: a session with rules has them, on", tresp.has_responses() and not tresp.responses_paused)
run(tresp, "ping")
check("responses: the answer is typed when its text appears", pump(4, lambda: "ran: pong-reply" in text(tresp)), ascii(text(tresp).strip()[-160:]))
check("responses: pausing from the window stops the answers", win.toggle_responses(tresp) is False and tresp.responses_paused)
run(tresp, "ping", 1.0)
check("responses: none while paused", text(tresp).count("ran: pong-reply") == 1, ascii(text(tresp).strip()[-160:]))
win.toggle_responses(tresp)
win.close_tab(tresp, force=True)

tloop = win.open_session(
    Session(name="looper", protocol="telnet", host="127.0.0.1", port=TELNET_PORT, username="operator",
            auto_responses="ran: => again\\r"),
    "pw",
)
pump(12, lambda: "Welcome to telnet-box" in text(tloop))
stopped_messages = []
tloop.responsesStopped.connect(lambda: stopped_messages.append(True))
run(tloop, "go", 0.2)
pump(6, lambda: stopped_messages)
pump(0.5)
check("responses: a rule that keeps matching its own answer is turned off, and the tab says so", stopped_messages == [True] and text(tloop).count("ran: again") <= _responses.MAX_RESPONSES + 1 and "Automatic responses turned off" in text(tloop) and "automatic responses turned off" in win.statusBar().currentMessage(), repr(text(tloop).count("ran: again")))
check("responses: a tab without rules has nothing to pause", not tlog.has_responses() and not win.toggle_responses(tlog))

config_file = WORK / "router.cfg"
config_file.write_text("hostname edge-1\ninterface ge-0/0/1\n description uplink\n", encoding="utf-8")
win.apply_preferences(_replace(win.prefs, paste_line_delay_ms=150))
sent_file = win.send_text_file(tloop, config_file, confirm=False)
queued = tloop.terminal.pasting()
pump(2.0, lambda: "ran:  description uplink" in text(tloop))
loop_text = text(tloop)
check("send file: the lines are typed in order, with the pause between lines", sent_file and queued == 2 and loop_text.index("ran: hostname edge-1") < loop_text.index("ran: interface ge-0/0/1") < loop_text.index("ran:  description uplink"), repr(queued))
win.apply_preferences(_replace(win.prefs, paste_line_delay_ms=0))
binary_file = WORK / "blob.bin"
binary_file.write_bytes(b"\x00\x01\x02binary")
check("send file: a binary file is refused", not win.send_text_file(tloop, binary_file, confirm=False) and "not a text file" in win.statusBar().currentMessage())
check("send file: a missing file is reported", not win.send_text_file(tloop, WORK / "no-such.cfg", confirm=False) and "Could not read" in win.statusBar().currentMessage())
win.close_tab(tloop, force=True)
win._closed_tabs.clear()

responses_dialog = SessionDialog(win.store, Session(host="h", username="u", auto_responses="[y/n] => y\\r"), win)
kept_rules = responses_dialog.session().auto_responses
responses_dialog.auto_responses.setPlainText("just text")
responses_dialog._finish(False)
check("responses: the session dialog keeps the rules and refuses a line it cannot read", kept_rules == "[y/n] => y\\r" and "Response not understood" in responses_dialog.error.text(), responses_dialog.error.text())
responses_dialog.reject()

# ---- keyboard focus, names for screen readers, route checks, status click, find marks, start page --
from PyQt6.QtCore import Qt as _Qt
from techxterminal.dialogs import ConnectionInfoDialog as _InfoDialog
from techxterminal.terminal import MarkedScrollBar as _MarkedScrollBar

sheet = QApplication.instance().styleSheet()
check("focus: buttons, check boxes and radio buttons show keyboard focus", "QPushButton:focus" in sheet and "QCheckBox::indicator:focus" in sheet and "QRadioButton::indicator:focus" in sheet and "#ThemeCard:focus" in sheet)
check("focus: clicking the window's own buttons leaves the focus in the terminal", all(widget.focusPolicy() == _Qt.FocusPolicy.TabFocus for widget in (win.new_button, win.all_tabs, win.notice_action)))
tab_close = win.tabs.tabBar().tabButton(win.tabs.indexOf(tlog), QTabBar.ButtonPosition.RightSide)
check("screen readers: icon buttons have names", win.import_button.accessibleName() == "Import, export and back up" and win.sort_button.accessibleName() == "Order of the sessions" and tlog.find_bar.case.accessibleName() == "Match case" and tab_close.accessibleName() == f"Close {tlog.title}", repr((win.import_button.accessibleName(), tab_close.accessibleName())))
win.rename_tab(tlog, "renamed for readers")
check("screen readers: a tab's close button follows its name", tab_close.accessibleName() == "Close renamed for readers")
win.rename_tab(tlog, "")

route = SessionDialog(win.store, None, win)
route.show()
route.host.setText("h")
route.username.setText("u")
route.tunnels.setText("8080:localhost:80, banana")
route._finish(False)
bad_tunnel = (route.result(), route.error.text(), route.focusWidget() is route.tunnels)
route.tunnels.setText("8080:localhost:80")
route.jump_host.setText("me@gate:notaport")
route._finish(False)
bad_jump = (route.result(), route.error.text(), route.focusWidget() is route.jump_host)
route.jump_host.setText("me@gate, me@gate2:2200")
route._finish(False)
check("session dialog: a tunnel or jump host it cannot use is caught when saving, not at connect time", bad_tunnel[0] == 0 and "banana" in bad_tunnel[1] and bad_tunnel[2] and bad_jump[0] == 0 and "Jump host" in bad_jump[1] and bad_jump[2] and route.result() == 1, repr((bad_tunnel, bad_jump)))

win.tabs.setCurrentIndex(win.tabs.indexOf(tlog))
pump(0.2)
QTest.mouseClick(win.status_left, _Qt.MouseButton.LeftButton)
pump(0.3)
opened_info = [widget for widget in QApplication.topLevelWidgets() if isinstance(widget, _InfoDialog) and widget.isVisible()]
check("status bar: clicking the connection opens Connection Info", len(opened_info) == 1 and win.status_left.toolTip() == "Connection info", repr(len(opened_info)))
for widget in opened_info:
    widget.close()

terminal = tlog.terminal
terminal.find("ran:")
lines_marked, current_mark = terminal.find_marks()
check("find: every line with a match is marked on the scrollbar, the active one too", isinstance(terminal.verticalScrollBar(), _MarkedScrollBar) and len(lines_marked) > 1 and lines_marked == sorted(set(lines_marked)) and current_mark in lines_marked, repr((lines_marked[:4], current_mark)))
terminal.clear_find()
check("find: and the marks go when the search does", terminal.find_marks() == ([], None))

win.save_workspace("Start page")
pump(0.2)
start_buttons = [win.workspace_row.itemAt(i).widget().text() for i in range(win.workspace_row.count())]
shown_workspaces = not win.workspace_bar.isHidden()
win.delete_workspace("Start page", confirm=False)
pump(0.2)
check("start page: workspaces are one click away there, and go when deleted", shown_workspaces and "Start page" in start_buttons and win.workspace_bar.isHidden(), repr(start_buttons))
welcome = win.stack.widget(0)
win._fit_welcome()
check("start page: the shortcut list is shown only when it fits", win.shortcut_card.isHidden() or welcome.layout().sizeHint().height() <= welcome.height())

# ---- menus stay one short column ----------------------------------------------------------------


def menu_labels(menu, path=""):
    """Every entry of a menu and its submenus, as "Submenu/Entry" without the shortcut."""
    found = []
    for action in menu.actions():
        if action.isSeparator() or not action.text():
            continue
        label = path + action.text().split("\t")[0]
        found.append(label)
        if action.menu():
            found += menu_labels(action.menu(), label + "/")
    return found


tab_menu = win.tab_menu(tlog)
top_level = [a.text().split("\t")[0] for a in tab_menu.actions() if not a.isSeparator()]
everything_in_menu = menu_labels(tab_menu)
available_height = win.screen().availableGeometry().height()
check("tab menu: a short single column, with the rest in submenus", len(top_level) <= 14 and [t for t in top_level if any(a.menu() and a.text() == t for a in tab_menu.actions())] == ["Send", "Output", "SSH", "Panes", "Other Tabs"] and tab_menu.sizeHint().height() < available_height * 0.7, repr((top_level, tab_menu.sizeHint().height(), available_height)))
wanted_entries = ["Reconnect", "Disconnect", "Duplicate Tab", "Rename Tab...", "Find...", "Connection Info...", "Send/Include in Broadcast", "Send/Send Text File...", "Send/Send Break", "Output/Save Output...", "Output/Notify When Output Stops", "SSH/Tunnels...", "SSH/Generate SSH Key Pair...", "Panes/Grid of Panes", "Close", "Other Tabs/Close Other Tabs", "Other Tabs/Reopen Closed Tab"]
check("tab menu: every action is still there", all(entry in everything_in_menu for entry in wanted_entries), repr([entry for entry in wanted_entries if entry not in everything_in_menu]))
tab_menu.deleteLater()

few_menu = win.new_tab_menu()
check("+ menu: with a few saved sessions, each is listed", all(any(a.text() == session.title for a in few_menu.actions()) for session in win.store.sessions) and len(win.store.sessions) <= 15)
few_menu.deleteLater()
many = [Session(name=f"bulk-{n:02d}", host=f"10.0.0.{n}", group=f"Rack {n % 3}") for n in range(30)]
win.store.sessions.extend(many)  # in memory only: not saved
many_menu = win.new_tab_menu()
many_labels = menu_labels(many_menu)
all_entry = next((a for a in many_menu.actions() if a.text().startswith("All Sessions (")), None)
check("+ menu: with many, a short list and every session under All Sessions, by folder", all_entry is not None and many_menu.sizeHint().height() < available_height * 0.7 and f"{all_entry.text()}/Rack 1/bulk-01" in many_labels and sum(1 for label in many_labels if label.startswith(all_entry.text() + "/") and label.count("/") == 2) >= 30, repr((len(many_menu.actions()), many_menu.sizeHint().height())))
many_menu.deleteLater()
for session in many:
    win.store.sessions.remove(session)

# ---- every screen size -------------------------------------------------------------------------
from PyQt6.QtCore import QEvent as _QEvent
from PyQt6.QtWidgets import QDialog as _QDialog
from techxterminal import screens as _screens
from techxterminal.sftp import FileBrowser as _FileBrowser

area = _screens.available()
huge = _QDialog()
huge.setMinimumSize(200, 150)
_screens.fit_to_screen(huge, 9000, 9000)
check("screens: a window asking for more than the screen gets what fits, on the screen", huge.width() <= area.width() and huge.height() <= area.height() and area.contains(huge.geometry().center()), repr((huge.geometry().getRect(), area.getRect())))
huge.setGeometry(area.right() + 4000, area.bottom() + 4000, 500, 400)  # as if its monitor had been unplugged
moved = _screens.keep_on_screen(huge)
check("screens: a window left on a monitor that is gone comes back", moved and area.intersects(huge.frameGeometry()) and not _screens.keep_on_screen(huge), repr(huge.geometry().getRect()))
huge.deleteLater()
check("screens: the main window and the dialogs open within the screen", win.width() <= area.width() and SettingsDialog(win.prefs, win).height() <= area.height())

geometry_before = win.saveGeometry()
win.showNormal()
win.resize(900, 420)
pump(0.5)
cramped = (win.command_bar.isVisible(), win.prefs.show_send_bar, win.tabs.tabBar().property("dense"), win.welcome_icon.isVisibleTo(win.welcome_icon.parentWidget()))
win.all_tabs.setChecked(True)
pump(0.1)
broadcast_kept = win.command_bar.isVisible()
win.all_tabs.setChecked(False)
win.resize(900, 700 if area.height() >= 700 else area.height() - 40)
pump(0.5)
check("short windows: the send bar is tucked away and the bars get thinner; the setting is kept", cramped == (False, True, True, False) and broadcast_kept and win.command_bar.isVisible() and win.tabs.tabBar().property("dense") is False, repr(cramped))

win.resize(560, 500)
pump(0.5)
win._update_status()
narrow_status = (win.status_left.text(), win.status_right.text())
check("narrow windows: the status bar keeps the state, the size and the time, and drops the rest", tlog.session.address not in narrow_status[0] and "UTF-8" not in narrow_status[1] and f"{tlog.terminal.cols} × {tlog.terminal.rows}" in narrow_status[1], repr(narrow_status))
small_palette = win.show_switcher()
pump(0.2)
check("narrow windows: the quick switcher fits inside the window", small_palette.width() <= win.width() - 24 and small_palette.height() <= win.height(), repr((small_palette.width(), win.width())))
small_palette.close()

portrait_tabs = [win.open_session(win.store.get(tlog.session.id)) for _ in range(2)]
pump(0.3)
win.split_tab(portrait_tabs[-1])
win.resize(560, min(area.height() - 40, 1000))
pump(0.8)
portrait = win.panes_stacked()
win.resize(min(area.width() - 40, 1400), 420 if area.height() > 460 else area.height() - 40)
pump(0.8)
landscape = win.panes_stacked()
check("split panes: stacked on a tall narrow window, side by side on a wide short one, the setting unchanged", portrait and not landscape and not win.prefs.stack_panes, repr((portrait, landscape, win.size())))
for tab in portrait_tabs:
    win.close_tab(tab, force=True)
win._closed_tabs.clear()
win.restoreGeometry(geometry_before)
pump(0.5)

def _no_server():
    raise OSError("no server for this check")

narrow_files = _FileBrowser(_no_server, "narrow", 0, win)
narrow_files.show()
narrow_files.resize(600, 520)
pump(0.4)
from techxterminal.sftp import file_kind as _file_kind
narrow_files.path.setText("/home/deploy/site")
crumbs = narrow_files.path.segments()
went = []
narrow_files.path.navigate.connect(went.append)
narrow_files.path._buttons[1][1].click()
narrow_files.local_path.setText(r"C:\Users\someone")
local_crumbs = narrow_files.local_path.segments()
check("files: the path is a row of folders, and a click on one goes there", crumbs == [("/", "/"), ("home", "/home"), ("deploy", "/home/deploy"), ("site", "/home/deploy/site")] and went == ["/home"] and local_crumbs[0][0] == "C:" and local_crumbs[-1] == ("someone", r"C:\Users\someone"), repr((crumbs, went, local_crumbs)))
narrow_files.path.setFocus()
narrow_files.path.setText("/home/deploy/site")
narrow_files.path.insert("/typed-but-not-taken")
QTest.keyClick(narrow_files.path, Qt.Key.Key_Escape)
check("files: Esc in the path puts back the folder on show", narrow_files.path.text() == "/home/deploy/site", narrow_files.path.text())
check("files: archives, images, code and documents are told apart by their icon's colour", [_file_kind(name) for name in ("a.tar.gz", "logo.PNG", "deploy.sh", "report.pdf", "data.bin", ".bashrc")] == ["archive", "image", "code", "document", "", ""])
stacked_files = narrow_files._panes.orientation() == Qt.Orientation.Vertical
narrow_files.resize(1000, 520)
pump(0.4)
check("file browser: the two sides stack on a narrow window and sit side by side on a wide one", stacked_files and narrow_files._panes.orientation() == Qt.Orientation.Horizontal)
narrow_files.shutdown()
narrow_files.close()

# -- a folder of many files: sorting and filtering must not stall the window
import functools as _functools
import random as _rnd
from techxterminal.sftp import RemoteRow as _RemoteRow, file_type as _file_type, sort_remote_rows as _sort_rows, summary_text as _summary_text


def _reference_order(entries, column, ascending):
    """The order the list is meant to have, written as the comparison Qt used to ask Python for."""
    def primary(entry):
        name, is_dir, size, modified, mode, owner = entry
        return {1: size or 0, 2: _file_type(name, is_dir).lower(), 3: modified or 0, 4: mode or 0, 5: owner.lower()}.get(column, name.lower())

    def compare(a, b):
        if a[1] != b[1]:
            return -1 if a[1] else 1  # folders stay on top whichever way the column goes
        left, right = primary(a), primary(b)
        if left != right:
            return (-1 if left < right else 1) * (1 if ascending else -1)
        return (a[0].lower() > b[0].lower()) - (a[0].lower() < b[0].lower())  # equal values: by name

    return sorted(entries, key=_functools.cmp_to_key(compare))


def _shown_names(entries, column=0, ascending=True, wanted="", hidden=False):
    keep = [entry for entry in entries if (hidden or not entry[0].startswith(".")) and wanted in entry[0].lower()]
    return [entry[0] for entry in _reference_order(keep, column, ascending)]


generator = _rnd.Random(7)
mismatches = []
for round_number in range(12):
    names = {}
    while len(names) < 60:
        name = "".join(generator.choice("abcABC.-_ 019é") for _ in range(generator.randint(1, 7)))
        names.setdefault(name.lower(), name)  # no two names that differ only in case
    sample = [
        (name, generator.random() < 0.3, generator.choice([None, 0, 5, 5, 900, 70000]), generator.choice([None, 0, 1.6e9, 1.6e9, 1.7e9]),
         generator.choice([None, 0o100644, 0o100644, 0o40755, 0o100600]), generator.choice(["", "root root", "deploy staff", "Deploy Staff"]))
        for name in names.values()
    ]
    for column in range(6):
        for ascending in (True, False):
            rows = [_RemoteRow(entry) for entry in sample]
            if [row.entry for row in _sort_rows(rows, column, ascending)] != _reference_order(sample, column, ascending):
                mismatches.append((round_number, column, ascending))
check("big folder: sorting by any column and way matches the order the list always had", not mismatches and len(sample) > 20, repr(mismatches[:5]))

big_entries = [
    (f"file-{i:05d}.{'log' if i % 3 else 'txt'}", i % 40 == 0, 1000 + (i * 7919) % 10007, 1.7e9 + (i * 104729) % 100003, 0o100644, "deploy staff")
    for i in range(10000)
] + [(".hidden", False, 5, 1.7e9, 0o100600, "deploy staff")]
big = _FileBrowser(_no_server, "big", 0, win)
big.show()
big.resize(1000, 620)
pump(0.3)
began = time.perf_counter()
big._show_listing("/big", big_entries)
listing_took = time.perf_counter() - began
big_total = sum(entry[2] for entry in big_entries if not entry[1] and not entry[0].startswith("."))
check("big folder: 10,000 files are listed in moments, folders first, dot-files left out", big.tree.topLevelItemCount() == 10000 and big.tree.topLevelItem(0).text(0) == "file-00000.txt" and big.tree.topLevelItem(249).data(0, _sftp.ROLE_IS_DIR) and not big.tree.topLevelItem(250).data(0, _sftp.ROLE_IS_DIR) and "10000 items (1 hidden)" in big.status.text() and listing_took < 2.0, f"{listing_took:.2f} s {big.status.text()}")
check("big folder: the summary counts what is listed", big.remote_summary.text() == _summary_text(9750, 250, big_total, False), big.remote_summary.text())

built = {row.entry[0]: row.item for row in big._rows if row.item is not None}
typing_took = []
for typed in ("1", "12", "123", "1234"):
    began = time.perf_counter()
    big.remote_filter.setText(typed)
    typing_took.append(time.perf_counter() - began)
shown_now = [big.tree.topLevelItem(i).text(0) for i in range(big.tree.topLevelItemCount())]
check("big folder: typing in the filter box answers at once and lists the right files", shown_now == _shown_names(big_entries, wanted="1234") and max(typing_took) < 0.3 and "filtered out" in big.status.text(), f"{[round(t, 3) for t in typing_took]} {shown_now[:3]}")
matching = [entry for entry in big_entries if "1234" in entry[0]]
check("big folder: the summary follows the filter", big.remote_summary.text() == _summary_text(sum(1 for e in matching if not e[1]), sum(1 for e in matching if e[1]), sum(e[2] for e in matching if not e[1]), False), big.remote_summary.text())
big.remote_filter.setText("file-0004")
big.tree.topLevelItem(0).setSelected(True)
big.remote_filter.setText("file-0009")
check("big folder: rows the filter hides are no longer selected", big.tree.selectedItems() == [] and big.tree.topLevelItemCount() == 10, f"{len(big.tree.selectedItems())} {big.tree.topLevelItemCount()}")
big.remote_filter.clear()
reused = all(row.item is built.get(row.entry[0], row.item) for row in big._rows)
check("big folder: clearing the filter brings the same rows back without making them again", reused and big.tree.topLevelItemCount() == 10000)

pick = [big.tree.topLevelItem(index) for index in (300, 4000, 9999)]
for item in pick:
    item.setSelected(True)
chosen = sorted(item.text(0) for item in pick)
began = time.perf_counter()
big.tree.sortByColumn(1, Qt.SortOrder.DescendingOrder)
resort_took = time.perf_counter() - began
by_size = [big.tree.topLevelItem(i).text(0) for i in range(big.tree.topLevelItemCount())]
check("big folder: clicking a column sorts all rows at once, in the right order", by_size == _shown_names(big_entries, 1, False) and resort_took < 0.3 and big.tree.header().sortIndicatorSection() == 1, f"{resort_took:.3f} s")
check("big folder: sorting keeps the rows that were selected", sorted(item.text(0) for item in big.tree.selectedItems()) == chosen and big.remote_summary.text().startswith("Selected"), repr([item.text(0) for item in big.tree.selectedItems()]))
big.tree.selectAll()
big.tree.sortByColumn(3, Qt.SortOrder.AscendingOrder)
check("big folder: sorting with everything selected keeps everything selected", len(big.tree.selectedItems()) == 10000 and [big.tree.topLevelItem(i).text(0) for i in range(60)] == _shown_names(big_entries, 3, True)[:60])
big.tree.clearSelection()
big.tree.sortByColumn(0, Qt.SortOrder.AscendingOrder)
big.remote_filter.setText("log")
by_name = [big.tree.topLevelItem(i).text(0) for i in range(big.tree.topLevelItemCount())]
check("big folder: a filter and a sort work together", by_name == _shown_names(big_entries, wanted="log"), repr(by_name[:3]))
big.remote_filter.clear()

kept = {row.entry[0]: row.item for row in big._rows}
refreshed = list(big_entries)
changed_name = refreshed[5][0]
refreshed[5] = (changed_name, refreshed[5][1], 123456, *refreshed[5][3:])
refreshed.pop()  # the hidden file is gone
refreshed.pop(0)
began = time.perf_counter()
big._show_listing("/big", refreshed)
refresh_took = time.perf_counter() - began
same_rows = sum(1 for row in big._rows if row.item is not None and kept.get(row.entry[0]) is row.item)
names_now = {big.tree.topLevelItem(i).text(0) for i in range(big.tree.topLevelItemCount())}
changed_row = next(row for row in big._rows if row.entry[0] == changed_name)
check("big folder: listing the folder again only remakes what changed", same_rows >= 9990 and changed_row.item.text(1) == "120.6 KB" and "file-00000.txt" not in names_now and big.tree.topLevelItemCount() == 9999 and refresh_took < 0.6, f"{same_rows} {changed_row.item.text(1)} {refresh_took:.3f} s")
big.retheme()
check("big folder: a new theme reaches the listed rows", not changed_row.item.icon(0).isNull())
big._show_listing("/big", [])
check("big folder: an emptied folder shows nothing and counts nothing", big.tree.topLevelItemCount() == 0 and big.remote_summary.text() == _summary_text(0, 0, 0, False) and big.status.text().startswith("0 items"), big.remote_summary.text() + " | " + big.status.text())
dir_entries = [(f"dir-{(i * 7919) % 3000:05d}", True, 4096, 1.7e9, 0o40755, "root root") for i in range(3000)]
began = time.perf_counter()
big._show_listing("/dirs", dir_entries)
dirs_took = time.perf_counter() - began
dirs_node = big._remote_node("/dirs")
folder_labels = [dirs_node.child(i).text(0) for i in range(dirs_node.childCount())]
check("big folder: thousands of subfolders reach the folder tree at once, in order", dirs_took < 1.5 and folder_labels == sorted(entry[0] for entry in dir_entries) and dirs_node.data(0, _sftp.ROLE_FILLED), f"{dirs_took:.2f} s {len(folder_labels)}")
trimmed = dir_entries[100:] + [(f"new-{i}", True, 4096, 1.7e9, 0o40755, "root root") for i in range(50)]
big._show_listing("/dirs", trimmed)
folder_labels = [dirs_node.child(i).text(0) for i in range(dirs_node.childCount())]
check("big folder: listing it again drops the folders that went and adds the new ones", folder_labels == sorted(entry[0] for entry in trimmed) and len(big._remote_nodes) == len(trimmed) + 3, f"{len(folder_labels)} {len(big._remote_nodes)}")
big.shutdown()
big.close()

# -- the progress line of running transfers, laid out like the one a command-line copy prints
from techxterminal.sftp import TransferBar as _TransferBar

line_browser = _FileBrowser(_no_server, "line", 0, win)
line_browser.show()
line_browser.resize(1000, 600)
pump(0.3)
check("progress line: hidden while nothing is running", line_browser.transfer_strip.isHidden() and line_browser.transfer_summary() == "")
line_local = r"C:\Users\kvshr\Downloads\mariadb-2026-06-08_22-59_41206919.zip"
line_remote = "/home14/thelonewolf/.apps/backup/mariadb-2026-06-08_22-59_41206919.zip"
line_browser._add_queue_row(1, line_local, _sftp.DOWNLOAD_ARROW, line_remote, 100_000_000, None)
line_browser._job_started(1)
line_browser._started_at[1] -= 2
line_browser._show_progress(1, "mariadb-2026-06-08_22-59_41206919.zip", 40_000_000, 100_000_000, 20_000_000)
pump(0.2)
single_line = line_browser.transfer_summary()
check("progress line: a download shows both names with the direction, time elapsed and left, per cent, bytes and speed",
      single_line == f"{line_local}   <<--   {line_remote}   00:00:02 elapsed   00:00:03 left   40.0%   40,000,000 bytes (19.1 MiB/s)" and line_browser.transfer_strip.isVisible(), single_line)
check("progress line: the bar is filled to the per cent and says it", line_browser.progress.value() == 400 and line_browser.progress.label() == "40.0%")
check("progress line: the queue row says the same per cent and speed, and the status bar names the file", line_browser._queue_rows[1].text(4) == "40.0%   19.1 MiB/s" and line_browser.status.text() == "mariadb-2026-06-08_22-59_41206919.zip", line_browser._queue_rows[1].text(4))
line_browser._add_queue_row(2, r"C:\up.bin", _sftp.UPLOAD_ARROW, "/srv/up.bin", 5000, None)
line_browser._job_started(2)
line_browser._show_progress(2, "up.bin", 1_000, 5_000, 1_000)
line_browser._waiting = 2
line_browser._show_overall()
check("progress line: several transfers are added up, and the line says how many are waiting", line_browser.transfer_summary().startswith("2 transfers running   00:00:02 elapsed") and "40,001,000 bytes" in line_browser.transfer_summary() and line_browser.transfer_summary().endswith("+2 waiting") and line_browser.transfer_remote.isHidden() and line_browser.status.text() == "2 transfers", line_browser.transfer_summary())
real_is_paused = line_browser.worker.is_paused
line_browser.worker.is_paused = lambda: True
line_browser._show_overall()
check("progress line: while paused it says so in place of the time left, and shows no speed", " Paused " in line_browser.transfer_summary() and " left" not in line_browser.transfer_summary() and "/s" not in line_browser.transfer_summary() and line_browser.status.text() == "Paused   2 transfers", line_browser.transfer_summary())
line_browser.worker.is_paused = real_is_paused
line_browser._job_finished(2, True, "")
line_browser._queue_rows[1].setText(0, "<b>mine</b>")
line_browser._show_overall()
check("progress line: names are shown as plain text", line_browser.transfer_local.textFormat() == Qt.TextFormat.PlainText and line_browser.transfer_local.text() == "<b>mine</b>" and "&lt;b&gt;mine" in line_browser.transfer_local.toolTip(), line_browser.transfer_local.toolTip())
line_browser.resize(500, 600)
pump(0.3)
check("progress line: a long path is shortened in the middle to fit", line_browser.transfer_remote.text().startswith("/home14") and "\u2026" in line_browser.transfer_remote.text() and line_browser.transfer_remote.text().endswith(".zip") and line_browser.transfer_remote.full_text() == line_remote, line_browser.transfer_remote.text())
line_browser._job_finished(1, True, "")
line_browser._set_busy(False)
check("progress line: it goes away when the transfers end", line_browser.transfer_strip.isHidden() and line_browser.transfer_summary() == "" and not line_browser._tick.isActive())

# finishing: the title shows how far it is, the taskbar flashes if the window is in the background, and a finished row has a menu
line_browser._add_queue_row(5, str(WORK / "readme-local.txt"), _sftp.DOWNLOAD_ARROW, "/srv/readme.txt", 100, None)
(WORK / "readme-local.txt").write_text("x")
line_browser._job_started(5)
line_browser._show_progress(5, "readme.txt", 25, 100, 10)
title_while_running = line_browser.windowTitle()
alerts = []
real_alert = QApplication.alert
QApplication.alert = staticmethod(lambda widget, *args: alerts.append(widget))
line_browser.isActiveWindow = lambda: False
line_browser._set_busy(True)
line_browser._job_finished(5, True, "")
line_browser._set_busy(False)
alerted_in_background = len(alerts)
line_browser.isActiveWindow = lambda: True
line_browser._set_busy(True)
line_browser._set_busy(False)
QApplication.alert = real_alert
check("progress line: the window title shows how far the transfer is, and goes back when it ends", title_while_running == "25% - Files - line" and line_browser.windowTitle() == "Files - line", f"{title_while_running!r} {line_browser.windowTitle()!r}")
check("progress line: the taskbar flashes when transfers finish in a window that is in the background, and only then", alerted_in_background == 1 and len(alerts) == 1, repr((alerted_in_background, len(alerts))))
finished_row = line_browser.done_list.topLevelItem(line_browser.done_list.topLevelItemCount() - 1)
launched = []
real_start = _sftp.QProcess.startDetached
_sftp.QProcess.startDetached = staticmethod(lambda program, arguments: launched.append((program, arguments)) or True)
shown_ok = line_browser.show_in_folder(finished_row)
missing_row = _sftp.QTreeWidgetItem([str(WORK / "no-such-file.bin"), "<<--", "/x", "", "Done"])
many_row = _sftp.QTreeWidgetItem(["3 items from C:\\somewhere", "-->>", "/x", "", "Done"])
shown_missing, shown_many = line_browser.show_in_folder(missing_row), line_browser.show_in_folder(many_row)
_sftp.QProcess.startDetached = real_start
check("done list: Show in Folder picks the file out in Explorer; a file that is gone, or a group of items, has nothing to show", shown_ok and not shown_missing and not shown_many and len(launched) == 1 and launched[0][0] == "explorer.exe" and launched[0][1] == ["/select,", os.path.normpath(str(WORK / "readme-local.txt"))], repr(launched))
line_browser.copy_text("C:\\copied\\path.txt")
check("done list: the local path can be copied", QApplication.clipboard().text() == "C:\\copied\\path.txt" and line_browser.status.text() == "Copied")
(WORK / "readme-local.txt").unlink()
line_browser.shutdown()
line_browser.close()

bar = _TransferBar()
bar.resize(200, 20)
bar.show()
pump(0.2)
bar.set_progress(500, "50.0%")
pump(0.1)
picture = bar.grab().toImage()
fill_pixel, empty_pixel = picture.pixelColor(picture.width() // 4, picture.height() // 5), picture.pixelColor(picture.width() * 3 // 4, picture.height() // 5)
check("progress bar: the filled part is green and the rest is the empty track", fill_pixel.name() == _COLORS["green"] and empty_pixel.name() == _COLORS["border"], f"{fill_pixel.name()} {empty_pixel.name()}")
bar.close()

measured = []
real_apply_font = tlog.terminal._apply_font
tlog.terminal._apply_font = lambda: (measured.append(True), real_apply_font())
QApplication.sendEvent(tlog.terminal, _QEvent(_QEvent.Type.DevicePixelRatioChange))
del tlog.terminal._apply_font
check("terminal: moving to a monitor with other scaling measures the font again", measured == [True] and tlog.terminal.cell_size()[0] > 0)

aligned = SessionDialog(win.store, None, win)
aligned.show()
pump(0.3)
label_mid = aligned.host_label.mapTo(aligned, aligned.host_label.rect().center()).y()
field_mid = aligned.host.mapTo(aligned, aligned.host.rect().center()).y()
check("session dialog: labels sit level with their fields", abs(label_mid - field_mid) <= 2, f"{label_mid} vs {field_mid}")
aligned.reject()

validator = SessionDialog(win.store, None, win)
validator.host.setText("somewhere")
validator._say("Server answers on port 22", good=True)
validator._finish(False)  # no user name
check("session dialog: a mistake is shown in red even after a successful test", validator.error.text() == "User name is required" and _COLORS["red"] in validator.error.styleSheet() and _COLORS["green"] not in validator.error.styleSheet(), validator.error.styleSheet())
validator.reject()

# ---- what a server says is not trusted: file names, text shown, what is opened -------------------------
import stat as _stat
from techxterminal import untrusted as _untrusted
from techxterminal.sftp import SftpWorker as _SftpWorker2, _Job as _Job2, _Transfer as _Transfer2


class _Listed:
    def __init__(self, filename, mode=_stat.S_IFREG | 0o644, size=5):
        self.filename, self.st_mode, self.st_size, self.st_mtime = filename, mode, size, 0


class _HostileServer:
    """A server whose folder holds names meant to put files where they do not belong."""

    names = ["good.txt", "..\\..\\Startup\\evil.bat", "..", ".", "C:\\Windows\\drop.txt", "CON", "tail.", ".hidden", "sub"]

    def listdir_attr(self, path):
        if path.endswith("/sub"):
            return [_Listed("inner.txt"), _Listed("..\\..\\up.txt")]
        return [_Listed(name, _stat.S_IFDIR | 0o755 if name == "sub" else _stat.S_IFREG | 0o644) for name in self.names]


landing = WORK / "landing"
landing.mkdir()
hostile = _HostileServer()
transfer = _Transfer2(_SftpWorker2(lambda: None), _Job2(1, None), hostile)
arrived = []
transfer.get = lambda remote, local, size=None, modified=None: arrived.append(local)
transfer.changed = transfer.skipped = 0
fetched = transfer.download_tree("/hostile", str(landing))
check("hostile names: every file from a server's folder lands inside the folder it was sent to", fetched == len(arrived) and all(_untrusted.is_inside(str(landing), path) for path in arrived) and not any(".." in os.path.relpath(path, landing).split(os.sep) for path in arrived), repr([os.path.relpath(path, landing) for path in arrived]))
check("hostile names: they are made into plain names, and the unusable ones are left out and counted", sorted(os.path.relpath(path, landing) for path in arrived) == sorted(["good.txt", ".._.._Startup_evil.bat", "C__Windows_drop.txt", "_CON", "tail", ".hidden", os.path.join("sub", "inner.txt"), os.path.join("sub", ".._.._up.txt")]) and transfer.skipped == 2 and transfer.changed == 5, repr((transfer.skipped, transfer.changed)))
check("hostile names: nothing was written outside, and the folders made are only the ones asked for", sorted(os.listdir(landing)) == ["sub"] and not (WORK / "Startup").exists(), repr(os.listdir(landing)))

from PyQt6.QtWidgets import QTreeWidgetItem

win.tabs.setCurrentIndex(win.tabs.indexOf(tlog))
win.toggle_files()
hostile_panel = tlog._files
pump(10, lambda: hostile_panel.directory == "/")
queued_downloads = []
hostile_panel._queue_download = lambda remote, local, size: queued_downloads.append((remote, local))
bad_item = QTreeWidgetItem(["..\\..\\Startup\\x.bat", "", "", "", "", ""])
ugly_item = QTreeWidgetItem(["..", "", "", "", "", ""])
fine_item = QTreeWidgetItem(["fine.txt", "", "", "", "", ""])
for listed_item in (bad_item, ugly_item, fine_item):
    listed_item.setData(0, _sftp.ROLE_IS_DIR, False)
    listed_item.setData(0, _sftp.ROLE_SIZE, 5)
hostile_panel.download_items([bad_item, ugly_item, fine_item])
check("hostile names: downloading from the list never builds a path outside the local folder", len(queued_downloads) == 2 and all(_untrusted.is_inside(hostile_panel.local_directory, local) for _remote, local in queued_downloads) and [os.path.basename(local) for _remote, local in queued_downloads] == [".._.._Startup_x.bat", "fine.txt"] and "cannot have the name" in hostile_panel.log.item(hostile_panel.log.count() - 1).text(), repr(queued_downloads))

hostile_panel._add_queue_row(900, "<b>local</b>", "-->", "/<img src=//evil/x.png>", 1, None)
tip_row = hostile_panel._queue_rows[900]
check("hostile text: a name that looks like HTML is shown as written, not drawn as HTML", tip_row.toolTip(0).startswith("<qt>") and "&lt;b&gt;" in tip_row.toolTip(0) and "&lt;img" in tip_row.toolTip(2) and "<img" not in tip_row.toolTip(2) and hostile_panel.status.textFormat() == _Qt.TextFormat.PlainText and tlog.banner_label.textFormat() == _Qt.TextFormat.PlainText, tip_row.toolTip(2))
hostile_panel._queue_rows.pop(900)

# Editing a file that Windows would run (a script, a program) shows its text in Notepad; it does not run it.
started_programs = []
opened_normally = []


class _FakeQProcess:
    @staticmethod
    def startDetached(program, arguments):
        started_programs.append((program, arguments))
        return True


real_qprocess = _sftp.QProcess
_sftp.QProcess = _FakeQProcess
hostile_panel._open_file = lambda path: opened_normally.append(path)
hostile_panel._open_for_editing(str(WORK / "run-me.bat"))
hostile_panel._open_for_editing(str(WORK / "Setup.EXE"))
hostile_panel._open_for_editing(str(WORK / "notes.txt"))
_sftp.QProcess = real_qprocess
check("edit: a script or program is shown in Notepad instead of being run, and a text file opens as usual", started_programs == [("notepad.exe", [str(WORK / "run-me.bat")]), ("notepad.exe", [str(WORK / "Setup.EXE")])] and opened_normally == [str(WORK / "notes.txt")], repr((started_programs, opened_normally)))
win.toggle_files()

# A password written in a session's command line is saved as plain text: asked about once.
pw_dialog = SessionDialog(win.store, None, win)
pw_dialog.host.setText("h")
pw_dialog.username.setText("u")
pw_dialog.command_line.setText("-P 22 -pw hunter2")
pw_asked = []
QMessageBox.question = staticmethod(lambda *a, **k: (pw_asked.append(a[1]), QMessageBox.StandardButton.No)[1])
pw_dialog._finish(False)
declined = (pw_asked == ["Password in Command Line"], pw_dialog.result() == 0)
QMessageBox.question = staticmethod(lambda *a, **k: (pw_asked.append(a[1]), QMessageBox.StandardButton.Yes)[1])
pw_dialog._finish(False)
accepted_once = (pw_dialog.result() == 1, pw_dialog.session().command_line == "-P 22 -pw hunter2")
QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes)
plain_dialog = SessionDialog(win.store, None, win)
plain_dialog.host.setText("h")
plain_dialog.username.setText("u")
plain_dialog.command_line.setText("-P 22 -C")
asked_before = len(pw_asked)
plain_dialog._finish(False)
check("command line: a password after -pw is confirmed as plain text before it is saved, and No keeps the dialog open", declined == (True, True) and accepted_once == (True, True) and pw_asked == ["Password in Command Line"] * 2, repr((declined, accepted_once, pw_asked)))
check("command line: nothing is asked when there is no password in it", plain_dialog.result() == 1 and len(pw_asked) == asked_before, repr(pw_asked))
pw_dialog.reject()

# ---- start page without sessions, output scrolled back ---------------------------------------------
saved_sessions = list(win.store.sessions)
win.store.sessions.clear()
win._update_start_hint()
first_run = ("import the ones you already have" in win.start_hint.text(), not win.import_start.isHidden())
win.store.sessions.extend(saved_sessions)
win._update_start_hint()
check("start page: with no saved sessions it says how to begin and offers to import", first_run == (True, True) and "Double-click a saved session" in win.start_hint.text() and win.import_start.isHidden(), repr(first_run))

latest_term = TerminalWidget()
latest_term.resize(640, 360)
latest_term.show()
pump(0.2)
latest_term.feed("".join(f"row {i}\r\n" for i in range(200)).encode())
pump(0.3)
latest_bar = latest_term.verticalScrollBar()
live_hidden = latest_term._latest.isHidden()
latest_bar.setValue(latest_bar.maximum() - 30)
pump(0.1)
back_shown = not latest_term._latest.isHidden() and latest_term.viewport().rect().contains(latest_term._latest.geometry())
latest_term._latest.click()
pump(0.1)
returned = latest_bar.value() == latest_bar.maximum() and latest_term._latest.isHidden()
latest_bar.setValue(10)
latest_term.feed(b"\x1b[?1049h")  # a full-screen program has no scrollback to return from
pump(0.1)
alt_hidden = latest_term._latest.isHidden()
check("terminal: a button over the output returns to the latest while it is scrolled back", live_hidden and back_shown and returned and alt_hidden and latest_term._latest.focusPolicy() == _Qt.FocusPolicy.NoFocus, repr((live_hidden, back_shown, returned, alt_hidden)))
latest_term.close()

# ---- the terminal's speed: the fast way of drawing text must leave exactly what pyte's own drawing does -------------
import random as _random
import pyte as _pyte
from techxterminal.terminal import TermScreen as _TermScreen


class _PyteDraws(_TermScreen):
    def draw(self, data):
        _pyte.Screen.draw(self, data)


_pieces_ascii = [chr(code) for code in range(0x20, 0x7F)]
_pieces_other = ["é", "中", "文", "█", "─", "\U0001F600", "é", "́", " "]
_pieces_escape = ["\x1b[0m", "\x1b[1m", "\x1b[31m", "\x1b[42m", "\x1b[38;5;196m", "\x1b[H", "\x1b[2J", "\x1b[K", "\x1b[3;7H", "\x1b[5A", "\x1b[4C",
                  "\x1b[L", "\x1b[M", "\x1b[2@", "\x1b[2P", "\x1b[?7l", "\x1b[?7h", "\x1b[4h", "\x1b[4l", "\x1b[2;6r", "\x1b[r", "\x1b(0", "\x1b(B",
                  "\x1b[?1049h", "\x1b[?1049l", "\r", "\n", "\r\n", "\t", "\b"]


def _random_stream(seed):
    chance = _random.Random(seed)
    parts = []
    for _ in range(chance.randint(5, 60)):
        kind = chance.random()
        if kind < 0.5:
            parts.append("".join(chance.choice(_pieces_ascii) for _ in range(chance.choice([1, 3, 8, 15, 30, 80]))))
        elif kind < 0.6:
            parts.append("".join(chance.choice(_pieces_other) for _ in range(chance.randint(1, 6))))
        else:
            parts.append(chance.choice(_pieces_escape))
    return "".join(parts)


def _screen_state(screen):
    return ({y: dict(line) for y, line in screen.buffer.items()}, (screen.cursor.x, screen.cursor.y), screen.cursor.attrs,
            [dict(line) for line in screen.history.top], set(screen.mode), set(screen.dirty))


unequal = 0
for seed in range(250):
    for columns, lines in ((20, 6), (7, 4), (80, 24)):
        quick, slow = _TermScreen(columns, lines, history=50), _PyteDraws(columns, lines, history=50)
        stream_data, step = _random_stream(seed), _random.Random(seed)
        quick_stream, slow_stream = _pyte.Stream(quick), _pyte.Stream(slow)
        position = 0
        while position < len(stream_data):  # in pieces of random size, as it arrives over a network
            size = step.choice([1, 5, 40, 500])
            quick_stream.feed(stream_data[position:position + size])
            slow_stream.feed(stream_data[position:position + size])
            position += size
        unequal += _screen_state(quick) != _screen_state(slow)
check("terminal speed: drawing text a stretch at a time leaves the same screen as pyte's own drawing, on 750 random streams", unequal == 0, f"{unequal} differ")

unequal_resized = 0
for seed in range(250):
    quick, slow = _TermScreen(40, 8, history=50), _PyteDraws(40, 8, history=50)
    stream_data, step = _random_stream(seed), _random.Random(seed)
    quick_stream, slow_stream = _pyte.Stream(quick), _pyte.Stream(slow)
    position = 0
    while position < len(stream_data):
        size = step.choice([1, 5, 40, 500])
        quick_stream.feed(stream_data[position:position + size])
        slow_stream.feed(stream_data[position:position + size])
        position += size
        if step.random() < 0.3:  # the window is resized while output arrives, which can leave the cursor outside the new width
            new_size = (step.choice([2, 4, 8, 12]), step.choice([5, 12, 40, 60]))
            quick.resize(*new_size)
            slow.resize(*new_size)
    unequal_resized += _screen_state(quick) != _screen_state(slow)
check("terminal speed: the same holds when the window is resized between pieces of output", unequal_resized == 0, f"{unequal_resized} differ")


def _output_after_narrowing():
    narrowed = _TermScreen(80, 4)
    stream = _pyte.Stream(narrowed)
    stream.feed("x" * 70)
    narrowed.resize(4, 30)  # the cursor is now at column 70 of 30
    stream.feed("hello world\r\nafter")


hang_probe = threading.Thread(target=_output_after_narrowing, daemon=True)
hang_probe.start()
hang_probe.join(10)
check("terminal: text written after the window was narrowed past the cursor does not hang", not hang_probe.is_alive())

from techxterminal.connections import TelnetWorker as _TelnetWorker

telnet_probe = _TelnetWorker(Session(name="t", host="h", protocol="telnet"), None, 80, 24)
began = time.perf_counter()
for _ in range(2000):
    telnet_probe._incoming(b"\xff\xfa\x18" + b"A" * 4096)  # IAC SB ..., and the end never comes
telnet_took = time.perf_counter() - began
check("telnet: a subnegotiation that never ends is dropped rather than kept and searched for ever", len(telnet_probe._pending) <= 64 * 1024 and telnet_took < 3 and telnet_probe._incoming(b"hi") == b"hi", f"{len(telnet_probe._pending)} bytes, {telnet_took:.2f} s")

zero_width = _TermScreen(20, 3)
_pyte.Stream(zero_width).feed("foo​bar\r\n﻿start")
shown_rows = ["".join(zero_width.buffer[row][x].data for x in range(10)).rstrip() for row in range(2)]
check("terminal: text after a zero-width character is shown (pyte drops the rest of the run)", shown_rows == ["foobar", "start"], repr(shown_rows))

speed_term = TerminalWidget()
speed_term.resize(900, 500)
speed_term.show()
pump(0.2)
speed_text = ("".join(f"line {number:06d} {'x' * 90} \x1b[32mok\x1b[0m\r\n" for number in range(5000))).encode()  # about 0.6 MB
speed_began = time.perf_counter()
speed_term.stream.feed(speed_term._decoder.decode(speed_text))
speed_seconds = time.perf_counter() - speed_began
check("terminal speed: 0.6 MB of coloured output is parsed in well under a second (it took 1.5 s before)", speed_seconds < 0.9, f"{speed_seconds:.2f} s")

# Searching the scrollback: line text is kept with lines that cannot change, and follows the width.
speed_term.find("line 004999")
first_found = list(speed_term._find_matches)
kept_text = [line for line in speed_term.screen.history.top if getattr(line, "_text", None)]
speed_term.find("line 004999")
check("terminal search: the text of lines in the scrollback is kept, and a second search finds the same", len(first_found) == 1 and len(kept_text) > 4000 and speed_term._find_matches == first_found, repr((len(first_found), len(kept_text))))
speed_term.resize(500, 500)
pump(0.3)
speed_term.find("line 004999")
check("terminal search: after a resize the kept text is made again for the new width, and the match is still found", len(speed_term._find_matches) == 1 and "line 004999" in speed_term.all_text(), repr(speed_term._find_matches))
speed_term.close()

win.tabs.setCurrentIndex(0)
pump(0.3)
win.grab().save(str(SCRATCH / "shot_tabs.png"))

# clearing the history
win.command.remember("echo remembered")
win.settings.setValue("send_history", win.command.history)
win.settings.setValue("quick_history", ["admin@10.0.0.1"])
win.settings.setValue("recent_sessions", [win.store.sessions[0].id])
win._closed_tabs.append([(win.store.sessions[0], "", None, False)])
cleared = win.clear_history(ask=False)
check("history: clearing forgets sent commands, typed addresses, recent sessions and closed tabs, and keeps the sessions", cleared and win.command.history == [] and not win.settings.value("send_history") and not win.settings.value("quick_history") and not win.settings.value("recent_sessions") and not win._closed_tabs and win.quick_history() == [] and len(win.store.sessions) > 0)

poller.stop()
for tab in win.open_tabs():
    win.close_tab(tab)
from techxterminal import connections
connections.shutdown_all()
from techxterminal import threads as _threads_end

check("all background threads have ended", not _threads_end.parked())
import techxterminal.app as _app_module

check("no unexpected internal errors during the whole run", not _app_module.unhandled_errors, (_app_module.unhandled_errors or [""])[0][-400:])
REAL_SETTINGS.sync()
_real_now = {key: REAL_SETTINGS.value(key) for key in REAL_SETTINGS.allKeys()}
_real_changed = sorted(key for key in set(_real_now) | set(REAL_SETTINGS_BEFORE) if _real_now.get(key) != REAL_SETTINGS_BEFORE.get(key))
check("isolation: the user's real settings were not touched", _real_now == REAL_SETTINGS_BEFORE and (HOME / "settings.ini").exists(), repr(_real_changed))
print("screenshots:", SCRATCH)
print("RESULT", "ALL PASS" if all(results) else "FAILURES", f"{sum(results)}/{len(results)}")
# os._exit skips Python's own shutdown, so output sent to a file or pipe must be flushed first.
sys.stdout.flush()
os._exit(0 if all(results) else 1)
