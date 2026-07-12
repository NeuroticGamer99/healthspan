"""OS keychain storage: the secret key and bearer-token entries
(ADR-0013/0026 via ``keyring``).

The keychain holds the two-factor secret key in its human-facing form:
grouped, unpadded RFC 4648 Base32 (ADR-0028) — the same string the
Recovery Kit shows, so what a user reads in their keychain matches what
they would type from paper.

It also holds the ADR-0026 credential entries, all under service
``healthspan``: each client's own token *plaintext* at ``token:<name>``
(written at bootstrap and on rotation), and the MCP client-facing secret
as a SHA-256 *hash* at ``mcp-client-secret`` — there the keychain is the
verifier's store, so the discipline inverts (ADR-0026).
"""

import keyring
import keyring.errors

from healthspan.kdf import decode_secret_key, encode_secret_key

SERVICE = "healthspan"
SECRET_KEY_ENTRY = "secret-key"  # noqa: S105 - entry name, not a credential
TOKEN_ENTRY_PREFIX = "token:"  # noqa: S105 - entry-name prefix, not a credential
MCP_CLIENT_SECRET_ENTRY = "mcp-client-secret"  # noqa: S105 - entry name only


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


def token_entry(name: str) -> str:
    """The keyring username for a token holder's plaintext (ADR-0026)."""
    return f"{TOKEN_ENTRY_PREFIX}{name}"


def store_token_plaintext(name: str, token: str) -> None:
    """Store a client's own token plaintext at ``token:<name>`` (ADR-0026)."""
    try:
        keyring.set_password(SERVICE, token_entry(name), token)
    except keyring.errors.KeyringError as exc:
        raise KeychainError(f"could not store the token '{name}': {exc}") from exc


def load_token_plaintext(name: str) -> str | None:
    """A client's stored token plaintext, or ``None`` if no entry exists."""
    try:
        return keyring.get_password(SERVICE, token_entry(name))
    except keyring.errors.KeyringError as exc:
        raise KeychainError(f"could not read the token '{name}': {exc}") from exc


def store_mcp_client_hash(hexdigest: str) -> None:
    """Store SHA-256 of the MCP client-facing secret — never its plaintext."""
    try:
        keyring.set_password(SERVICE, MCP_CLIENT_SECRET_ENTRY, hexdigest)
    except keyring.errors.KeyringError as exc:
        raise KeychainError(
            f"could not store the MCP client-secret hash: {exc}"
        ) from exc


def load_mcp_client_hash() -> str | None:
    """The stored MCP client-secret hash, or ``None`` if no entry exists."""
    try:
        return keyring.get_password(SERVICE, MCP_CLIENT_SECRET_ENTRY)
    except keyring.errors.KeyringError as exc:
        raise KeychainError(
            f"could not read the MCP client-secret hash: {exc}"
        ) from exc
