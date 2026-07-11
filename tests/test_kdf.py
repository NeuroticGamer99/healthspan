"""Argon2id derivation: known-answer vectors, NFC, Base32, DbKey hygiene."""

import pytest

from healthspan.kdf import (
    DbKey,
    decode_secret_key,
    derive_db_key,
    encode_secret_key,
    generate_secret_key,
    normalize_passphrase,
)
from healthspan.keyparams import KeyMode, KeyParams

FIXED_SALT = bytes(range(32))
PASSPHRASE = "correct horse battery staple"

# Deterministic across machines, library versions, and years (ADR-0028);
# a change here means derivation drifted and existing databases would not open.
KAT_PRODUCTION = "0b220637b2f1ea0425e3c3e4ea5c5c14fd91b0f26b5726839a04f2e387f75f73"
KAT_FLOOR = "092d6e91987840e63e2fac5e187ac5d29b489f05597971fd6554555a1a20ce2a"


def test_known_answer_at_production_parameters() -> None:
    params = KeyParams(mode=KeyMode.TWO_FACTOR)
    key = derive_db_key(PASSPHRASE, FIXED_SALT, params)
    assert key.hex() == KAT_PRODUCTION


def test_known_answer_at_floor_parameters() -> None:
    params = KeyParams(mode=KeyMode.TWO_FACTOR, m=19456, t=2, p=1)
    key = derive_db_key(PASSPHRASE, FIXED_SALT, params)
    assert key.hex() == KAT_FLOOR


def test_nfc_composed_and_decomposed_derive_identically() -> None:
    composed = "café au lait"  # e-acute as one code point
    decomposed = "café au lait"  # e + combining acute
    assert composed != decomposed
    assert normalize_passphrase(composed) == normalize_passphrase(decomposed)
    params = KeyParams(mode=KeyMode.TWO_FACTOR, m=19456, t=2, p=1)
    assert (
        derive_db_key(composed, FIXED_SALT, params).hex()
        == derive_db_key(decomposed, FIXED_SALT, params).hex()
    )


def test_secret_key_base32_round_trip() -> None:
    secret = generate_secret_key()
    encoded = encode_secret_key(secret)
    assert decode_secret_key(encoded) == secret


def test_base32_encoding_is_grouped_and_unpadded() -> None:
    encoded = encode_secret_key(FIXED_SALT)
    assert "=" not in encoded
    groups = encoded.split("-")
    assert all(len(g) == 4 for g in groups)
    assert sum(len(g) for g in groups) == 52  # 32 bytes -> 52 Base32 chars


def test_decode_is_forgiving_about_case_spacing_and_dashes() -> None:
    encoded = encode_secret_key(FIXED_SALT)
    mangled = encoded.replace("-", " ").lower()
    assert decode_secret_key(mangled) == FIXED_SALT


def test_decode_rejects_wrong_length_and_garbage() -> None:
    with pytest.raises(ValueError, match="32 bytes"):
        decode_secret_key("AAAA-BBBB")
    with pytest.raises(ValueError, match=r"Base32|32 bytes"):
        decode_secret_key("not base32 at all!!!")


def test_dbkey_repr_never_exposes_material() -> None:
    key = DbKey(bytearray(FIXED_SALT))
    assert key.hex() not in repr(key)
    assert key.hex() not in str(key)
    assert "redacted" in repr(key)


def test_dbkey_zeroize_overwrites_and_blocks_reuse() -> None:
    buf = bytearray(FIXED_SALT)
    key = DbKey(buf)
    key.zeroize()
    assert bytes(buf) == b"\x00" * 32
    with pytest.raises(RuntimeError, match="zeroized"):
        key.hex()


def test_dbkey_requires_exactly_32_bytes() -> None:
    with pytest.raises(ValueError, match="32 bytes"):
        DbKey(bytearray(16))
