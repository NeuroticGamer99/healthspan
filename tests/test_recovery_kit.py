"""Recovery Kit rendering and the owner-only ``--output`` file (ADR-0033)."""

import os
from pathlib import Path

import pytest

from healthspan import recovery_kit

_SECRET = bytes(range(32))


def test_render_kit_shows_the_secret_and_custody_instructions() -> None:
    text = recovery_kit.render_kit(_SECRET)
    assert "HEALTHSPAN RECOVERY KIT" in text
    assert "Secret key (Base32):" in text


def test_write_kit_writes_the_rendered_kit(tmp_path: Path) -> None:
    out = recovery_kit.write_kit(_SECRET, tmp_path / "kit.txt")
    assert "HEALTHSPAN RECOVERY KIT" in out.read_text(encoding="utf-8")


@pytest.mark.skipif(os.name != "posix", reason="POSIX mode bits are the check")
def test_write_kit_is_owner_only_from_creation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # With set_owner_only stubbed out, a restrictive mode can only come from
    # the os.open(0o600) at creation — proving the plaintext secret key is
    # never briefly world/group-readable (the create-then-chmod window is
    # closed). Under the old write_text-then-chmod code this file would be
    # 0o644 (umask) at this point and the assertion would fail.
    def _noop(path: Path) -> None:
        return None

    monkeypatch.setattr(recovery_kit, "set_owner_only", _noop)
    out = recovery_kit.write_kit(_SECRET, tmp_path / "kit.txt")
    assert (out.stat().st_mode & 0o077) == 0
