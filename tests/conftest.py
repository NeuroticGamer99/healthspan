"""Test-suite bootstrap: scripts/ importability, keychain isolation, helpers."""

import sys
from collections.abc import Callable, Iterator
from pathlib import Path

import keyring
import keyring.backend
import keyring.errors
import pytest

from healthspan.config import (
    BackupConfig,
    Config,
    ConfigSource,
    DatabaseConfig,
    LoggingConfig,
    ServiceConfig,
)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))


class InMemoryKeyring(keyring.backend.KeyringBackend):
    """Isolated keyring backend so tests never touch the OS keychain."""

    priority = 1  # pyright: ignore[reportAssignmentType] - classproperty upstream

    def __init__(self) -> None:
        super().__init__()
        self.store: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, username: str) -> str | None:
        return self.store.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        self.store[(service, username)] = password

    def delete_password(self, service: str, username: str) -> None:
        if (service, username) not in self.store:
            raise keyring.errors.PasswordDeleteError(username)
        del self.store[(service, username)]


@pytest.fixture(autouse=True)
def fake_keychain() -> Iterator[InMemoryKeyring]:
    """Every test runs against an in-memory keychain (never the real one)."""
    previous = keyring.get_keyring()
    backend = InMemoryKeyring()
    keyring.set_keyring(backend)
    try:
        yield backend
    finally:
        keyring.set_keyring(previous)


@pytest.fixture
def make_config(tmp_path: Path) -> Callable[[], Config]:
    """An effective Config rooted in tmp_path (no file on disk needed)."""

    def _make() -> Config:
        return Config(
            config_version=1,
            database=DatabaseConfig(path=tmp_path / "healthspan.db"),
            backup=BackupConfig(
                directory=tmp_path / "backups", schedule="daily", retention_count=14
            ),
            logging=LoggingConfig(level="INFO"),
            service=ServiceConfig(host="127.0.0.1", port=8464, passphrase_file=None),
            path=tmp_path / "config.toml",
            source=ConfigSource.FLAG,
            loaded_from_file=True,
        )

    return _make


@pytest.fixture
def write_file() -> Callable[[Path, str], Path]:
    """Write a UTF-8 text file and return its path (shared authoring helper)."""

    def _write(path: Path, content: str) -> Path:
        path.write_text(content, encoding="utf-8")
        return path

    return _write
