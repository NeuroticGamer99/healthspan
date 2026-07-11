"""Argon2id key derivation and key-material handling (ADR-0028).

Implements the byte-exact construction ADR-0028 specifies: NFC-normalized
UTF-8 passphrase, the 32-byte secret key (or sidecar salt) as the Argon2id
salt, parameters read from the ``.keyparams`` sidecar. Derivation must be
exactly reproducible — same inputs, same key, on any machine, forever.
"""

import base64
import re
import secrets
import unicodedata

from argon2.low_level import Type, hash_secret_raw

from healthspan.keyparams import KeyParams

SECRET_KEY_BYTES = 32
DB_KEY_BYTES = 32

# Base32 groups of 4 separated by dashes: 32 bytes -> 52 chars -> 13 groups.
_GROUP_SIZE = 4
_B32_CHARS = re.compile(r"[A-Z2-7]+")


class DbKey:
    """The derived 32-byte database key, held in a mutable buffer.

    A ``bytearray`` (never ``bytes``) so :meth:`zeroize` can genuinely
    overwrite it in place (ADR-0028 key-lifetime rules). The repr never
    exposes key material; the transient hex form is produced only for the
    ``PRAGMA key``/``PRAGMA rekey`` statement and must not be retained.
    """

    __slots__ = ("_buf",)

    def __init__(self, buf: bytearray) -> None:
        if len(buf) != DB_KEY_BYTES:
            raise ValueError(f"database key must be {DB_KEY_BYTES} bytes")
        self._buf = buf

    def hex(self) -> str:
        """Transient lowercase hex for the raw-key PRAGMA; do not retain."""
        if not any(self._buf):
            raise RuntimeError("database key has been zeroized")
        return bytes(self._buf).hex()

    def zeroize(self) -> None:
        """Best-effort in-place overwrite (hygiene, not a boundary)."""
        for i in range(len(self._buf)):
            self._buf[i] = 0

    def __repr__(self) -> str:
        return "DbKey(<redacted>)"

    __str__ = __repr__


def normalize_passphrase(passphrase: str) -> bytes:
    """NFC-normalize and UTF-8 encode (ADR-0028 passphrase encoding)."""
    return unicodedata.normalize("NFC", passphrase).encode("utf-8")


def derive_db_key(passphrase: str, salt: bytes, params: KeyParams) -> DbKey:
    """Derive the database key: ``Argon2id(passphrase, salt, m/t/p)``.

    ``salt`` is the 32-byte secret key (two-factor mode) or the sidecar
    salt (passphrase-only mode) — raw bytes in both cases.
    """
    raw = hash_secret_raw(
        secret=normalize_passphrase(passphrase),
        salt=salt,
        time_cost=params.t,
        memory_cost=params.m,
        parallelism=params.p,
        hash_len=params.hash_len,
        type=Type.ID,
        version=params.argon2_version,
    )
    return DbKey(bytearray(raw))


def generate_secret_key() -> bytes:
    """Fresh 32-byte secret key / sidecar salt (``secrets.token_bytes``)."""
    return secrets.token_bytes(SECRET_KEY_BYTES)


def encode_secret_key(secret_key: bytes) -> str:
    """RFC 4648 Base32, no padding, dash-grouped for humans (ADR-0028)."""
    if len(secret_key) != SECRET_KEY_BYTES:
        raise ValueError(f"secret key must be {SECRET_KEY_BYTES} bytes")
    b32 = base64.b32encode(secret_key).decode("ascii").rstrip("=")
    groups = [b32[i : i + _GROUP_SIZE] for i in range(0, len(b32), _GROUP_SIZE)]
    return "-".join(groups)


def decode_secret_key(text: str) -> bytes:
    """Decode a human-entered Base32 secret key back to its 32 raw bytes.

    Forgiving on the human side (case, dashes, spaces); strict on the
    result (must decode to exactly 32 bytes).
    """
    compact = "".join(_B32_CHARS.findall(text.upper()))
    padded = compact + "=" * (-len(compact) % 8)
    try:
        raw = base64.b32decode(padded)
    except ValueError as exc:
        raise ValueError(f"not a valid Base32 secret key: {exc}") from exc
    if len(raw) != SECRET_KEY_BYTES:
        raise ValueError(
            f"secret key must decode to {SECRET_KEY_BYTES} bytes, got {len(raw)}"
        )
    return raw
