"""Back up and restore everything the application stores, in one file.

The file is a zip holding the saved sessions, saved commands, scheduled tasks, workspaces,
approved host keys and preferences. Passwords are not in it: they live in the operating system's credential store.

With a passphrase the zip is encrypted: the file is MAGIC, a random salt and nonce, then the zip sealed with
AES-256-GCM under a key that scrypt derives from the passphrase. GCM also tells a wrong passphrase and a damaged
or altered file apart from a good one, so nothing half-read is ever restored.
"""

import io
import json
import os
import zipfile
from dataclasses import asdict, fields
from pathlib import Path

from . import APP_NAME
from .preferences import Preferences

SESSIONS_NAME = "sessions.json"
SNIPPETS_NAME = "snippets.json"
KNOWN_HOSTS_NAME = "known_hosts"
WORKSPACES_NAME = "workspaces.json"
TASKS_NAME = "tasks.json"
PREFERENCES_NAME = "preferences.json"


MAGIC = b"TECHXBK1"  # the start of an encrypted backup
SALT_SIZE, NONCE_SIZE = 16, 12
SCRYPT_COST = 2**15  # a fraction of a second here, and as long again for every guess at the passphrase
ENCRYPTED_SUFFIX = ".txbackup"
MIN_PASSPHRASE = 8


class BackupError(Exception):
    pass


class PassphraseNeeded(BackupError):
    """The backup is encrypted, and no passphrase was given."""


def _key(passphrase: str, salt: bytes) -> bytes:
    from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

    return Scrypt(salt=salt, length=32, n=SCRYPT_COST, r=8, p=1).derive(passphrase.encode("utf-8"))


def encrypt(data: bytes, passphrase: str) -> bytes:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    salt, nonce = os.urandom(SALT_SIZE), os.urandom(NONCE_SIZE)
    return MAGIC + salt + nonce + AESGCM(_key(passphrase, salt)).encrypt(nonce, data, MAGIC)


def decrypt(data: bytes, passphrase: str) -> bytes:
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    salt = data[len(MAGIC) : len(MAGIC) + SALT_SIZE]
    nonce = data[len(MAGIC) + SALT_SIZE : len(MAGIC) + SALT_SIZE + NONCE_SIZE]
    try:
        return AESGCM(_key(passphrase, salt)).decrypt(nonce, data[len(MAGIC) + SALT_SIZE + NONCE_SIZE :], MAGIC)
    except (InvalidTag, ValueError):
        raise BackupError("Wrong passphrase, or the backup is damaged") from None


def is_encrypted(source: Path) -> bool:
    try:
        with open(source, "rb") as handle:
            return handle.read(len(MAGIC)) == MAGIC
    except OSError:
        return False


def create_backup(
    target: Path,
    sessions_file: Path,
    snippets_file: Path,
    known_hosts_file: Path,
    prefs: Preferences,
    workspaces_file: Path | None = None,
    tasks_file: Path | None = None,
    passphrase: str = "",
) -> list[str]:
    """Write a backup, encrypted when there is a passphrase; returns the names of the parts that went into it."""
    parts = []
    packed = io.BytesIO()
    try:
        with zipfile.ZipFile(packed, "w", zipfile.ZIP_DEFLATED) as archive:
            files = [(SESSIONS_NAME, sessions_file), (SNIPPETS_NAME, snippets_file), (KNOWN_HOSTS_NAME, known_hosts_file)]
            if workspaces_file is not None:
                files.append((WORKSPACES_NAME, workspaces_file))
            if tasks_file is not None:
                files.append((TASKS_NAME, tasks_file))
            for name, source in files:
                if Path(source).is_file():
                    archive.write(source, name)
                    parts.append(name)
            archive.writestr(PREFERENCES_NAME, json.dumps(asdict(prefs), indent=2))
            parts.append(PREFERENCES_NAME)
        data = packed.getvalue()
        Path(target).write_bytes(encrypt(data, passphrase) if passphrase else data)
    except OSError as exc:
        raise BackupError(f"Could not write the backup: {exc.strerror or exc}") from None
    return parts


def read_backup(source: Path, passphrase: str | None = None) -> dict[str, bytes]:
    """The parts of a backup file, by name. Raises PassphraseNeeded for an encrypted one without a passphrase,
    and BackupError if it is not a backup, or the passphrase is wrong."""
    wanted = {SESSIONS_NAME, SNIPPETS_NAME, KNOWN_HOSTS_NAME, PREFERENCES_NAME, WORKSPACES_NAME, TASKS_NAME}
    try:
        data = Path(source).read_bytes()
    except OSError:
        raise BackupError(f"That file is not a {APP_NAME} backup") from None
    if data.startswith(MAGIC):
        if not passphrase:
            raise PassphraseNeeded("This backup is protected by a passphrase")
        data = decrypt(data, passphrase)
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            parts = {name: archive.read(name) for name in archive.namelist() if name in wanted}
    except (OSError, zipfile.BadZipFile):
        raise BackupError(f"That file is not a {APP_NAME} backup") from None
    if SESSIONS_NAME not in parts and PREFERENCES_NAME not in parts:
        raise BackupError(f"That file is not a {APP_NAME} backup")
    for name in (SESSIONS_NAME, SNIPPETS_NAME, PREFERENCES_NAME, TASKS_NAME):
        if name in parts:
            try:
                json.loads(parts[name].decode("utf-8"))
            except ValueError:
                raise BackupError(f"The backup is damaged ({name} cannot be read)") from None
    return parts


def preferences_from(data: bytes) -> Preferences:
    """Preferences from a backup; anything missing or of the wrong type keeps its default."""
    raw = json.loads(data.decode("utf-8"))
    prefs = Preferences()
    for field in fields(Preferences):
        value = raw.get(field.name) if isinstance(raw, dict) else None
        if value is not None and type(value) is type(getattr(prefs, field.name)):
            setattr(prefs, field.name, value)
    return prefs


def restore_files(
    parts: dict[str, bytes],
    sessions_file: Path,
    snippets_file: Path,
    known_hosts_file: Path,
    workspaces_file: Path | None = None,
    tasks_file: Path | None = None,
) -> None:
    """Put the session, command and host key files of a backup in place, keeping the old ones as .bak."""
    targets = [(SESSIONS_NAME, sessions_file), (SNIPPETS_NAME, snippets_file), (KNOWN_HOSTS_NAME, known_hosts_file)]
    if workspaces_file is not None:
        targets.append((WORKSPACES_NAME, workspaces_file))
    if tasks_file is not None:
        targets.append((TASKS_NAME, tasks_file))
    for name, target in targets:
        if name not in parts:
            continue
        target = Path(target)
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.is_file():
                target.replace(target.with_name(target.name + ".bak"))
            target.write_bytes(parts[name])
        except OSError as exc:
            raise BackupError(f"Could not restore {name}: {exc.strerror or exc}") from None
