"""The ``.keyparams`` sidecar (ADR-0028).

A small plaintext TOML file next to the database recording what
re-derivation needs but must not guess: KDF parameters, key mode, and (in
passphrase-only mode) the salt. Nothing in it is secret, but it is
integrity-sensitive: both the writer and the reader enforce the OWASP
parameter floor, so tampering that would silently weaken Argon2id refuses
to derive instead.
"""

import base64
import binascii
import tomllib
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Any

from healthspan.fsperm import set_owner_only

SIDECAR_SUFFIX = ".keyparams"
SIDECAR_FORMAT = 1

ARGON2_VERSION = 19
HASH_LEN = 32
SALT_BYTES = 32

# Initial parameters: argon2-cffi's current defaults (ADR-0028).
DEFAULT_M = 65536  # KiB (64 MiB)
DEFAULT_T = 3
DEFAULT_P = 4

# OWASP floor (ADR-0028): below this, neither write nor derive proceeds.
FLOOR_M = 19_456  # KiB (19 MiB)
FLOOR_T = 2
FLOOR_P = 1

_KNOWN_KEYS = {
    "format",
    "kdf",
    "argon2_version",
    "m",
    "t",
    "p",
    "hash_len",
    "mode",
    "salt",
    "created_utc",
    "rotated_utc",
}

RECOVERY_GUIDANCE = (
    "Recover it by restoring the sidecar from any backup of the same key "
    "generation ('healthspan db backup' copies it alongside every backup); "
    "or, if the KDF parameters were never changed from the documented "
    "initial defaults, regenerate it with those defaults (ADR-0028)."
)


class KeyParamsError(Exception):
    """The sidecar is missing, malformed, or below the parameter floor."""


class KeyMode(Enum):
    TWO_FACTOR = "two-factor"
    PASSPHRASE_ONLY = "passphrase-only"  # noqa: S105 - mode name, not a credential


@dataclass(frozen=True)
class KeyParams:
    """The KDF parameters and key mode in force for one database."""

    mode: KeyMode
    m: int = DEFAULT_M
    t: int = DEFAULT_T
    p: int = DEFAULT_P
    hash_len: int = HASH_LEN
    argon2_version: int = ARGON2_VERSION
    salt: bytes | None = None  # passphrase-only mode only
    created_utc: str = ""
    rotated_utc: str = ""

    def __post_init__(self) -> None:
        if self.mode is KeyMode.PASSPHRASE_ONLY:
            if self.salt is None or len(self.salt) != SALT_BYTES:
                raise KeyParamsError(
                    f"passphrase-only mode requires a {SALT_BYTES}-byte salt"
                )
        elif self.salt is not None:
            raise KeyParamsError("two-factor mode must not carry a stored salt")

    def with_upgraded_parameters(self) -> KeyParams:
        """Raise any parameter below the current defaults (rotation ride-along)."""
        return replace(
            self,
            m=max(self.m, DEFAULT_M),
            t=max(self.t, DEFAULT_T),
            p=max(self.p, DEFAULT_P),
        )


def sidecar_path(database_path: Path) -> Path:
    """The sidecar's location for a given database file (ADR-0028)."""
    return database_path.with_name(database_path.name + SIDECAR_SUFFIX)


def utc_now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _check_floor(params: KeyParams, path: Path, action: str) -> None:
    if params.m < FLOOR_M or params.t < FLOOR_T or params.p < FLOOR_P:
        raise KeyParamsError(
            f"refusing to {action} {path}: Argon2id parameters "
            f"(m={params.m} KiB, t={params.t}, p={params.p}) fall below the "
            f"OWASP floor (m>={FLOOR_M}, t>={FLOOR_T}, p>={FLOOR_P}). "
            + RECOVERY_GUIDANCE
        )
    if params.hash_len != HASH_LEN:
        raise KeyParamsError(
            f"refusing to {action} {path}: hash_len must be {HASH_LEN}, "
            f"got {params.hash_len}. " + RECOVERY_GUIDANCE
        )


def write_keyparams(path: Path, params: KeyParams) -> None:
    """Write the sidecar with owner-only permissions.

    Callers are ``healthspan init`` and the rotation / mode-conversion
    commands — nothing else rewrites this file.
    """
    _check_floor(params, path, "write")
    lines = [
        f"format = {SIDECAR_FORMAT}",
        'kdf = "argon2id"',
        f"argon2_version = {params.argon2_version}",
        f"m = {params.m}",
        f"t = {params.t}",
        f"p = {params.p}",
        f"hash_len = {params.hash_len}",
        f'mode = "{params.mode.value}"',
        f'created_utc = "{params.created_utc}"',
    ]
    if params.rotated_utc:
        lines.append(f'rotated_utc = "{params.rotated_utc}"')
    if params.mode is KeyMode.PASSPHRASE_ONLY:
        if params.salt is None:  # pragma: no cover - __post_init__ invariant
            raise KeyParamsError("passphrase-only params lost their salt")
        salt_b64 = base64.b64encode(params.salt).decode("ascii")
        lines.append(f'salt = "{salt_b64}"')
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    set_owner_only(path)


def read_keyparams(path: Path) -> KeyParams:
    """Read and validate the sidecar; refuse to derive below the floor."""
    if not path.is_file():
        raise KeyParamsError(
            f"key-parameter sidecar {path} is missing — the database cannot "
            f"be unlocked without it. " + RECOVERY_GUIDANCE
        )
    try:
        data: dict[str, Any] = tomllib.loads(path.read_text(encoding="utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        raise KeyParamsError(f"{path}: not a valid sidecar: {exc}") from exc

    unknown = sorted(set(data) - _KNOWN_KEYS)
    if unknown:
        raise KeyParamsError(f"{path}: unknown key(s): {', '.join(unknown)}")
    fmt = data.get("format")
    if fmt != SIDECAR_FORMAT:
        raise KeyParamsError(
            f"{path}: unsupported sidecar format {fmt!r} "
            f"(this build supports {SIDECAR_FORMAT})"
        )
    if data.get("kdf") != "argon2id":
        raise KeyParamsError(f"{path}: unsupported kdf {data.get('kdf')!r}")

    try:
        mode = KeyMode(data.get("mode"))
    except ValueError:
        raise KeyParamsError(f"{path}: unknown mode {data.get('mode')!r}") from None

    salt: bytes | None = None
    if "salt" in data:
        raw = data["salt"]
        if not isinstance(raw, str):
            raise KeyParamsError(f"{path}: 'salt' must be a Base64 string")
        try:
            salt = base64.b64decode(raw, validate=True)
        except binascii.Error as exc:
            raise KeyParamsError(f"{path}: 'salt' is not valid Base64") from exc

    def _int(key: str) -> int:
        value = data.get(key)
        if isinstance(value, bool) or not isinstance(value, int):
            raise KeyParamsError(f"{path}: '{key}' must be an integer")
        return value

    def _str(key: str) -> str:
        value = data.get(key, "")
        if not isinstance(value, str):
            raise KeyParamsError(f"{path}: '{key}' must be a string")
        return value

    try:
        params = KeyParams(
            mode=mode,
            m=_int("m"),
            t=_int("t"),
            p=_int("p"),
            hash_len=_int("hash_len"),
            argon2_version=_int("argon2_version"),
            salt=salt,
            created_utc=_str("created_utc"),
            rotated_utc=_str("rotated_utc"),
        )
    except KeyParamsError as exc:
        raise KeyParamsError(f"{path}: {exc}") from None
    if params.argon2_version != ARGON2_VERSION:
        raise KeyParamsError(
            f"{path}: unsupported argon2_version {params.argon2_version} "
            f"(this build supports {ARGON2_VERSION})"
        )
    _check_floor(params, path, "derive with")
    return params
