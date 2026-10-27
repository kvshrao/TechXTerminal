"""SSH key helpers: create a key pair, and install a public key on a server."""

import getpass
import os
import socket
from pathlib import Path

import paramiko
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519
from PyQt6.QtCore import QThread, pyqtSignal

from . import ppk

AUTHORIZED_KEYS = ".ssh/authorized_keys"


class KeyError_(Exception):
    """Something the user can fix: the file exists, a passphrase is needed, and so on."""


def default_key_path() -> Path:
    return Path.home() / ".ssh" / "id_ed25519_techx"


def default_comment() -> str:
    return f"{getpass.getuser()}@{socket.gethostname()}"


def generate_keypair(path: Path, passphrase: str = "", comment: str = "") -> str:
    """Create an Ed25519 key pair: the private key at `path`, the public key at `path`.pub.

    Returns the public key line. Existing files are never overwritten.
    """
    path = Path(path)
    public_path = Path(str(path) + ".pub")
    for existing in (path, public_path):
        if existing.exists():
            raise KeyError_(f"{existing} already exists; choose another file name")
    key = ed25519.Ed25519PrivateKey.generate()
    encryption = (
        serialization.BestAvailableEncryption(passphrase.encode()) if passphrase else serialization.NoEncryption()
    )
    private = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.OpenSSH, encryption)
    public = key.public_key().public_bytes(serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH)
    line = public.decode() + (f" {comment.strip()}" if comment.strip() else "")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # Created for the owner only, so the private key is never readable by others even briefly.
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(private)
        public_path.write_text(line + "\n", encoding="utf-8")
    except OSError as exc:
        raise KeyError_(f"Could not write the key: {exc.strerror or exc}") from None
    return line


def public_key_line(path: Path, passphrase: str | None = None) -> str:
    """The one-line public key for a key file: a .pub file, or derived from a private key."""
    path = Path(path)
    candidates = [path] if path.suffix == ".pub" else [Path(str(path) + ".pub")]
    for candidate in candidates:
        try:
            for line in candidate.read_text(encoding="utf-8", errors="replace").splitlines():
                fields = line.split()
                if len(fields) >= 2 and fields[0].startswith(("ssh-", "ecdsa-", "sk-")):
                    return line.strip()
        except OSError:
            pass
    if path.suffix == ".pub":
        raise KeyError_(f"{path} does not contain a public key")
    try:
        key = ppk.load(str(path), passphrase) if ppk.is_ppk(str(path)) else paramiko.PKey.from_path(str(path), passphrase)
    except paramiko.PasswordRequiredException:
        raise KeyError_("This private key is protected by a passphrase; choose its .pub file instead") from None
    except (paramiko.SSHException, ppk.PpkError, OSError, ValueError, TypeError) as exc:
        # The underlying library reports a missing passphrase in its own words.
        if any(word in str(exc).lower() for word in ("password", "passphrase", "encrypted")):
            raise KeyError_("This private key is protected by a passphrase; choose its .pub file instead") from None
        raise KeyError_(f"{path.name} is not a key file this application can read") from None
    return f"{key.get_name()} {key.get_base64()} {default_comment()}"


def install_public_key(sftp: paramiko.SFTPClient, line: str) -> bool:
    """Add a public key to the login's authorized_keys; returns False if it was already there."""
    fields = line.split()
    if len(fields) < 2:
        raise KeyError_("That is not a public key")
    try:
        sftp.mkdir(".ssh", 0o700)
    except OSError:
        pass  # normally: it exists already
    try:
        with sftp.open(AUTHORIZED_KEYS, "r") as handle:
            existing = handle.read().decode("utf-8", "replace")
    except OSError:
        existing = ""
    if any(fields[1] in known.split() for known in existing.splitlines()):
        return False
    separator = "" if not existing or existing.endswith("\n") else "\n"
    with sftp.open(AUTHORIZED_KEYS, "w") as handle:
        handle.write((existing + separator + line.strip() + "\n").encode())
    for target, mode in ((".ssh", 0o700), (AUTHORIZED_KEYS, 0o600)):
        try:
            sftp.chmod(target, mode)  # sshd ignores the file if others can write to it
        except OSError:
            pass
    return True


class KeyInstaller(QThread):
    """Installs a public key over an open SSH connection without blocking the window."""

    done = pyqtSignal(bool)  # True if added, False if it was already installed
    failed = pyqtSignal(str)

    def __init__(self, opener, line: str, parent=None):
        super().__init__(parent)
        self._opener = opener  # returns a connected paramiko.SFTPClient
        self._line = line

    def run(self) -> None:
        try:
            sftp = self._opener()
            try:
                self.done.emit(install_public_key(sftp, self._line))
            finally:
                sftp.close()
        except Exception as exc:
            self.failed.emit(str(exc) or exc.__class__.__name__)
