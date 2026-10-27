"""Reader for .ppk private key files (format versions 2 and 3).

paramiko only understands OpenSSH and PEM keys, so a .ppk file is decoded here and
handed over as an OpenSSH key held in memory; nothing is written to disk.
"""

import base64
import hashlib
import hmac
import io
import struct

import paramiko
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519, rsa
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

_HEADER = "PuTTY-User-Key-File-"  # the literal first line of the file format
_CURVES = {"nistp256": ec.SECP256R1, "nistp384": ec.SECP384R1, "nistp521": ec.SECP521R1}
# What a key file may ask of Argon2: PuTTYgen writes 8 MiB and a dozen passes, so these allow far more
# than any real key while a damaged or hostile file cannot make the program grind for minutes or run out of memory.
_ARGON2_MAX_MEMORY_KIB = 512 * 1024
_ARGON2_MAX_PASSES = 1000
_ARGON2_MAX_LANES = 64
_ARGON2_MAX_WORK_KIB = 4 * 1024 * 1024  # passes times memory: about two seconds of work


class PpkError(Exception):
    pass


def is_ppk(path: str) -> bool:
    try:
        with open(path, "rb") as handle:
            return handle.read(len(_HEADER)) == _HEADER.encode()
    except OSError:
        return False


class _Reader:
    """Walks the length-prefixed fields of an SSH key blob."""

    def __init__(self, data: bytes):
        self.data, self.offset = data, 0

    def string(self) -> bytes:
        if self.offset + 4 > len(self.data):
            raise PpkError("Key data is truncated")
        (length,) = struct.unpack_from(">I", self.data, self.offset)
        start = self.offset + 4
        if start + length > len(self.data):
            raise PpkError("Key data is truncated")
        self.offset = start + length
        return self.data[start : start + length]

    def mpint(self) -> int:
        return int.from_bytes(self.string(), "big", signed=False)


def _ssh_string(data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + data


def _parse(text: str) -> dict:
    lines = text.replace("\r\n", "\n").split("\n")
    fields: dict = {}
    position = 0
    while position < len(lines):
        line = lines[position]
        position += 1
        if ": " not in line:
            continue
        name, value = line.split(": ", 1)
        if name.startswith(_HEADER):
            fields["version"], fields["algorithm"] = int(name[len(_HEADER) :]), value
        elif name in ("Public-Lines", "Private-Lines"):
            count = int(value)
            if not 0 <= count <= len(lines) - position:
                raise ValueError("more lines than the file has")  # a negative count would read the same line for ever
            fields[name] = base64.b64decode("".join(lines[position : position + count]))
            position += count
        else:
            fields[name] = value
    for required in ("version", "Encryption", "Public-Lines", "Private-Lines", "Private-MAC"):
        if required not in fields:
            raise PpkError("Not a valid .ppk key file")
    if fields["version"] not in (2, 3):
        raise PpkError(f"Unsupported .ppk format version {fields['version']}")
    return fields


def _decrypt(fields: dict, passphrase: str | None) -> tuple[bytes, bytes]:
    """Returns the decrypted private blob and the key for the integrity check."""
    encryption, version, private = fields["Encryption"], fields["version"], fields["Private-Lines"]
    secret = (passphrase or "").encode()
    if encryption == "none":
        if version == 2:
            return private, hashlib.sha1(b"putty-private-key-file-mac-key").digest()
        return private, b""
    if encryption != "aes256-cbc":
        raise PpkError(f"Unsupported .ppk encryption '{encryption}'")
    if not passphrase:
        raise paramiko.PasswordRequiredException("The key file is protected by a passphrase")

    if version == 2:
        key = (hashlib.sha1(b"\0\0\0\0" + secret).digest() + hashlib.sha1(b"\0\0\0\1" + secret).digest())[:32]
        iv = bytes(16)
        mac_key = hashlib.sha1(b"putty-private-key-file-mac-key" + secret).digest()
    else:
        if fields.get("Key-Derivation") != "Argon2id":
            raise PpkError(f"Unsupported key derivation '{fields.get('Key-Derivation')}' (only Argon2id)")
        try:
            from cryptography.hazmat.primitives.kdf.argon2 import Argon2id
        except ImportError:
            raise PpkError("This key needs Argon2 support (cryptography 44 or newer)") from None
        try:
            salt = bytes.fromhex(fields["Argon2-Salt"])
            passes, lanes, memory = int(fields["Argon2-Passes"]), int(fields["Argon2-Parallelism"]), int(fields["Argon2-Memory"])
        except (KeyError, ValueError):
            raise PpkError("Not a valid .ppk key file") from None
        if not (
            1 <= passes <= _ARGON2_MAX_PASSES
            and 1 <= lanes <= _ARGON2_MAX_LANES
            and 8 * lanes <= memory <= _ARGON2_MAX_MEMORY_KIB
            and passes * memory <= _ARGON2_MAX_WORK_KIB
            and 8 <= len(salt) <= 1024
        ):
            raise PpkError("The key file has Argon2 settings that cannot be used")
        derived = Argon2id(salt=salt, length=80, iterations=passes, lanes=lanes, memory_cost=memory).derive(secret)
        key, iv, mac_key = derived[:32], derived[32:48], derived[48:]
    if len(private) % 16:
        raise PpkError("Encrypted key data has the wrong length")
    decryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
    return decryptor.update(private) + decryptor.finalize(), mac_key


def _verify(fields: dict, private: bytes, mac_key: bytes) -> None:
    payload = b"".join(
        _ssh_string(part)
        for part in (
            fields["algorithm"].encode(),
            fields["Encryption"].encode(),
            fields.get("Comment", "").encode(),
            fields["Public-Lines"],
            private,
        )
    )
    digest = hashlib.sha1 if fields["version"] == 2 else hashlib.sha256
    expected = hmac.new(mac_key, payload, digest).hexdigest()
    if not hmac.compare_digest(expected, fields["Private-MAC"].strip().lower()):
        if fields["Encryption"] != "none":
            # A wrong passphrase decrypts to garbage, which shows up here.
            raise paramiko.PasswordRequiredException("Wrong passphrase for the key file")
        raise PpkError("The key file is damaged (integrity check failed)")


def _to_private_key(algorithm: str, public: bytes, private: bytes):
    pub, priv = _Reader(public), _Reader(private)
    if pub.string().decode() != algorithm:
        raise PpkError("The key file is inconsistent")
    if algorithm == "ssh-rsa":
        e, n = pub.mpint(), pub.mpint()
        d, p, q, iqmp = priv.mpint(), priv.mpint(), priv.mpint(), priv.mpint()
        numbers = rsa.RSAPrivateNumbers(
            p, q, d, rsa.rsa_crt_dmp1(d, p), rsa.rsa_crt_dmq1(d, q), iqmp, rsa.RSAPublicNumbers(e, n)
        )
        return numbers.private_key(), paramiko.RSAKey
    if algorithm == "ssh-ed25519":
        pub.string()
        return ed25519.Ed25519PrivateKey.from_private_bytes(priv.string()), paramiko.Ed25519Key
    if algorithm.startswith("ecdsa-sha2-"):
        curve = _CURVES.get(pub.string().decode())
        if curve is None:
            raise PpkError("Unsupported elliptic curve in the key file")
        return ec.derive_private_key(priv.mpint(), curve()), paramiko.ECDSAKey
    raise PpkError(f"Unsupported key type '{algorithm}'")


def load(path: str, passphrase: str | None = None) -> paramiko.PKey:
    """Load a .ppk file as a paramiko key.

    Raises paramiko.PasswordRequiredException when a passphrase is needed or wrong,
    and PpkError for anything else that is wrong with the file.
    """
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            fields = _parse(handle.read())
    except OSError as exc:
        raise PpkError(f"Cannot read key file: {exc.strerror or exc}") from None
    except ValueError:
        raise PpkError("Not a valid .ppk key file") from None
    try:
        private, mac_key = _decrypt(fields, passphrase)
        _verify(fields, private, mac_key)
        key, key_class = _to_private_key(fields["algorithm"], fields["Public-Lines"], private)
        openssh = key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.OpenSSH, serialization.NoEncryption()
        )
        return key_class.from_private_key(io.StringIO(openssh.decode()))
    except paramiko.PasswordRequiredException:
        raise
    except (KeyError, ValueError, IndexError, TypeError, OverflowError, UnicodeError, struct.error, paramiko.SSHException):
        raise PpkError("Not a valid .ppk key file") from None  # a field that is missing, cut short or out of range
