"""OS keychain storage for the secret key (ADR-0013 via ``keyring``).

The keychain holds the two-factor secret key in its human-facing form:
grouped, unpadded RFC 4648 Base32 (ADR-0028) — the same string the
Recovery Kit shows, so what a user reads in their keychain matches what
they would type from paper.
"""

import keyring
import keyring.errors

from healthspan.kdf import decode_secret_key, encode_secret_key

SERVICE = "healthspan"
SECRET_KEY_ENTRY = "secret-key"  # noqa: S105 - entry name, not a credential


class KeychainError(Exception):
    """The OS keychain could not serve or store the secret key."""


def store_secret_key(secret_key: bytes) -> None:
    try:
        keyring.set_password(SERVICE, SECRET_KEY_ENTRY, encode_secret_key(secret_key))
    except keyring.errors.KeyringError as exc:
        raise KeychainError(f"could not store the secret key: {exc}") from exc


def load_secret_key() -> bytes:
    try:
        stored = keyring.get_password(SERVICE, SECRET_KEY_ENTRY)
    except keyring.errors.KeyringError as exc:
        raise KeychainError(f"could not read the secret key: {exc}") from exc
    if stored is None:
        raise KeychainError(
            "no secret key in the OS keychain. If this machine never ran "
            "'healthspan init', restore the key from your Recovery Kit; "
            "the entry is service 'healthspan', name 'secret-key'."
        )
    try:
        return decode_secret_key(stored)
    except ValueError as exc:
        raise KeychainError(f"keychain entry is not a valid secret key: {exc}") from exc


def delete_secret_key() -> None:
    try:
        keyring.delete_password(SERVICE, SECRET_KEY_ENTRY)
    except keyring.errors.PasswordDeleteError:
        pass  # already absent - deletion is idempotent
    except keyring.errors.KeyringError as exc:
        raise KeychainError(f"could not delete the secret key: {exc}") from exc
