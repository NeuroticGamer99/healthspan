"""Sidecar round-trip, floor enforcement on both paths, tamper refusal."""

from pathlib import Path

import pytest

from healthspan.keyparams import (
    KeyMode,
    KeyParams,
    KeyParamsError,
    read_keyparams,
    sidecar_path,
    utc_now_iso,
    write_keyparams,
)


def test_sidecar_round_trip_two_factor(tmp_path: Path) -> None:
    path = tmp_path / "healthspan.db.keyparams"
    params = KeyParams(mode=KeyMode.TWO_FACTOR, created_utc=utc_now_iso())
    write_keyparams(path, params)
    assert read_keyparams(path) == params


def test_sidecar_round_trip_passphrase_only(tmp_path: Path) -> None:
    path = tmp_path / "healthspan.db.keyparams"
    params = KeyParams(
        mode=KeyMode.PASSPHRASE_ONLY,
        salt=bytes(range(32)),
        created_utc=utc_now_iso(),
        rotated_utc=utc_now_iso(),
    )
    write_keyparams(path, params)
    assert read_keyparams(path) == params


def test_sidecar_path_convention(tmp_path: Path) -> None:
    assert sidecar_path(tmp_path / "healthspan.db") == (
        tmp_path / "healthspan.db.keyparams"
    )


def test_missing_sidecar_error_carries_recovery_guidance(tmp_path: Path) -> None:
    with pytest.raises(KeyParamsError, match="restoring the sidecar from any backup"):
        read_keyparams(tmp_path / "nope.keyparams")


def test_write_refuses_below_owasp_floor(tmp_path: Path) -> None:
    params = KeyParams(mode=KeyMode.TWO_FACTOR, m=8192)
    with pytest.raises(KeyParamsError, match="OWASP floor"):
        write_keyparams(tmp_path / "x.keyparams", params)


def test_read_refuses_tampered_weakened_parameters(tmp_path: Path) -> None:
    path = tmp_path / "x.keyparams"
    params = KeyParams(mode=KeyMode.TWO_FACTOR, created_utc=utc_now_iso())
    write_keyparams(path, params)
    weakened = path.read_text(encoding="utf-8").replace("m = 65536", "m = 1024")
    path.write_text(weakened, encoding="utf-8")
    with pytest.raises(KeyParamsError, match="OWASP floor"):
        read_keyparams(path)


def test_read_rejects_unknown_keys(tmp_path: Path) -> None:
    path = tmp_path / "x.keyparams"
    write_keyparams(path, KeyParams(mode=KeyMode.TWO_FACTOR))
    path.write_text(
        path.read_text(encoding="utf-8") + 'surprise = "key"\n', encoding="utf-8"
    )
    with pytest.raises(KeyParamsError, match="unknown key"):
        read_keyparams(path)


def test_read_rejects_unsupported_format_and_kdf(tmp_path: Path) -> None:
    path = tmp_path / "x.keyparams"
    write_keyparams(path, KeyParams(mode=KeyMode.TWO_FACTOR))
    original = path.read_text(encoding="utf-8")
    path.write_text(original.replace("format = 1", "format = 99"), encoding="utf-8")
    with pytest.raises(KeyParamsError, match="unsupported sidecar format"):
        read_keyparams(path)
    path.write_text(original.replace("argon2id", "pbkdf2"), encoding="utf-8")
    with pytest.raises(KeyParamsError, match="unsupported kdf"):
        read_keyparams(path)


def test_read_rejects_remaining_tamper_branches(tmp_path: Path) -> None:
    path = tmp_path / "x.keyparams"
    write_keyparams(
        path, KeyParams(mode=KeyMode.PASSPHRASE_ONLY, salt=bytes(range(32)))
    )
    original = path.read_text(encoding="utf-8")

    cases = [
        ('mode = "passphrase-only"', 'mode = "sideways"', "unknown mode"),
        ("argon2_version = 19", "argon2_version = 16", "unsupported argon2_version"),
        ("hash_len = 32", "hash_len = 64", "hash_len must be 32"),
        ("t = 3", 't = "three"', "'t' must be an integer"),
        ("t = 3", "t = true", "'t' must be an integer"),
    ]
    for old, new, message in cases:
        assert old in original
        path.write_text(original.replace(old, new), encoding="utf-8")
        with pytest.raises(KeyParamsError, match=message):
            read_keyparams(path)

    salt_line = next(
        line for line in original.splitlines() if line.startswith("salt = ")
    )
    path.write_text(
        original.replace(salt_line, 'salt = "not-base64!!"'), encoding="utf-8"
    )
    with pytest.raises(KeyParamsError, match="not valid Base64"):
        read_keyparams(path)
    # A truncated (wrong-length) salt is caught by the dataclass invariant.
    path.write_text(original.replace(salt_line, 'salt = "QUJD"'), encoding="utf-8")
    with pytest.raises(KeyParamsError, match="32-byte salt"):
        read_keyparams(path)


def test_read_rejects_non_toml(tmp_path: Path) -> None:
    path = tmp_path / "x.keyparams"
    path.write_text("not = = toml", encoding="utf-8")
    with pytest.raises(KeyParamsError, match="not a valid sidecar"):
        read_keyparams(path)


def test_mode_salt_consistency_is_enforced() -> None:
    with pytest.raises(KeyParamsError, match="requires a 32-byte salt"):
        KeyParams(mode=KeyMode.PASSPHRASE_ONLY)
    with pytest.raises(KeyParamsError, match="must not carry a stored salt"):
        KeyParams(mode=KeyMode.TWO_FACTOR, salt=bytes(32))


def test_parameter_upgrade_ride_along_never_lowers() -> None:
    low = KeyParams(mode=KeyMode.TWO_FACTOR, m=19456, t=2, p=1)
    upgraded = low.with_upgraded_parameters()
    assert (upgraded.m, upgraded.t, upgraded.p) == (65536, 3, 4)
    high = KeyParams(mode=KeyMode.TWO_FACTOR, m=131072, t=5, p=8)
    kept = high.with_upgraded_parameters()
    assert (kept.m, kept.t, kept.p) == (131072, 5, 8)
