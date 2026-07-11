"""Property-based Argon2id determinism suite (testing-strategy.md).

Generalizes the known-answer and NFC unit tests in test_kdf.py. Runs with
minimal test-only Argon2id parameters (KDFs are deliberately slow) and the
Hypothesis deadline disabled; the known-answer vectors at production
parameters stay in the plain unit tests.
"""

import unicodedata

from hypothesis import given, settings
from hypothesis import strategies as st

from healthspan.kdf import (
    decode_secret_key,
    derive_db_key,
    encode_secret_key,
)
from healthspan.keyparams import KeyMode, KeyParams

# Minimal parameters: fast, deliberately below the OWASP floor — legal here
# because the floor is enforced at the sidecar boundary (read/write), not in
# the derivation function, and no sidecar is involved.
TEST_PARAMS = KeyParams(mode=KeyMode.TWO_FACTOR, m=8, t=1, p=1)

passphrases = st.text(min_size=1, max_size=64)
salts = st.binary(min_size=32, max_size=32)


@settings(deadline=None)
@given(passphrase=passphrases, salt=salts)
def test_derivation_is_deterministic(passphrase: str, salt: bytes) -> None:
    first = derive_db_key(passphrase, salt, TEST_PARAMS)
    second = derive_db_key(passphrase, salt, TEST_PARAMS)
    assert first.hex() == second.hex()


@settings(deadline=None)
@given(passphrase=passphrases, salt=salts)
def test_derivation_is_normalization_invariant(passphrase: str, salt: bytes) -> None:
    nfc = unicodedata.normalize("NFC", passphrase)
    nfd = unicodedata.normalize("NFD", passphrase)
    assert (
        derive_db_key(nfc, salt, TEST_PARAMS).hex()
        == derive_db_key(nfd, salt, TEST_PARAMS).hex()
    )


@settings(deadline=None)
@given(secret=salts)
def test_base32_round_trip_for_arbitrary_secret_keys(secret: bytes) -> None:
    assert decode_secret_key(encode_secret_key(secret)) == secret
