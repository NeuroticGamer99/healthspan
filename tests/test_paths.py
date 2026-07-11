"""Unit tests for the platform path layout (ADR-0046 directory table)."""

from collections.abc import Callable
from pathlib import Path

import platformdirs
import pytest

from healthspan import paths


def test_file_and_directory_names_match_adr_0046() -> None:
    assert paths.default_config_path() == paths.config_dir() / "config.toml"
    assert paths.default_database_path() == paths.data_dir() / "healthspan.db"
    assert paths.default_backup_dir() == paths.data_dir() / "backups"


@pytest.mark.parametrize(
    ("platformdirs_attr", "subject"),
    [
        ("user_config_dir", paths.config_dir),
        ("user_data_dir", paths.data_dir),
    ],
)
def test_platformdirs_wiring(
    monkeypatch: pytest.MonkeyPatch,
    platformdirs_attr: str,
    subject: Callable[[], Path],
) -> None:
    calls: list[tuple[str, bool, bool]] = []

    def fake(appname: str, *, appauthor: bool, roaming: bool) -> str:
        calls.append((appname, appauthor, roaming))
        return "/fake/dir"

    monkeypatch.setattr(platformdirs, platformdirs_attr, fake)
    assert subject() == Path("/fake/dir")
    # ADR-0046: appname "healthspan", no author segment, no roaming profile.
    assert calls == [("healthspan", False, False)]
