"""init provisioning: keychain-before-files ordering and failure cleanup."""

from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

import pytest

from healthspan import provisioning
from healthspan.config import Config, ConfigSource
from healthspan.keyparams import KeyMode, sidecar_path


def test_failed_init_removes_a_config_file_it_created(
    make_config: Callable[[], Config], monkeypatch: pytest.MonkeyPatch
) -> None:
    # A DEFAULT-source config with no file on disk: initialize creates the
    # skeleton config (now AFTER the keychain store, per ADR-0047), then fails
    # writing the sidecar. The config this run created must be cleaned up so a
    # failed init leaves nothing on disk and stays re-runnable.
    cfg = replace(make_config(), source=ConfigSource.DEFAULT, loaded_from_file=False)
    assert not cfg.path.exists()

    def _boom(sidecar: Path, params: object) -> None:
        raise OSError("simulated sidecar write failure")

    monkeypatch.setattr(provisioning, "write_keyparams", _boom)
    with pytest.raises(OSError, match="simulated sidecar write failure"):
        provisioning.initialize(
            cfg, "a perfectly good passphrase", KeyMode.PASSPHRASE_ONLY
        )

    assert not cfg.path.exists()  # the config this run created was removed
    assert not cfg.database.path.exists()  # and the empty database
    assert not sidecar_path(cfg.database.path).exists()
