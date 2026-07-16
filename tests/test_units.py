"""Example-based unit tests and the local verification layer for the units module.

ADR-0031 adopts a pre-1.0 engine (``ucumvert``) behind this module *with our own
verification of the specific biomarkers/units in real use* rather than blind trust.
This file is that verification layer: known-answer conversions for common biomarkers,
asserted against independently published clinical conversion values, plus the
fail-loud error paths. The biomarker molar masses below are generic public reference
data (no personal health values); the property suite in test_units_properties.py is
the standing regression net over the wider input space.
"""

import math

import pytest

from healthspan.units import (
    IncommensurableUnitsError,
    MissingMolarContextError,
    UnknownUnitError,
    convert,
    is_valid_unit,
    parse_unit,
)

# Generic biomarkers in common lab use, each with a published molar mass (g/mol)
# and a known-answer conversion. Values are textbook clinical figures, not any
# individual's results. Tolerance is loose enough to absorb rounding in the
# published targets while still catching a wrong conversion factor.
#
# (biomarker, molar_mass, value, from_unit, to_unit, expected)
KNOWN_ANSWERS = [
    ("glucose", 180.156, 100.0, "mg/dL", "mmol/L", 5.551),
    ("glucose", 180.156, 5.551, "mmol/L", "mg/dL", 100.0),
    ("total cholesterol", 386.654, 200.0, "mg/dL", "mmol/L", 5.172),
    ("creatinine", 113.12, 1.0, "mg/dL", "umol/L", 88.4),
    ("uric acid", 168.11, 6.0, "mg/dL", "umol/L", 356.9),
    ("calcium", 40.078, 9.5, "mg/dL", "mmol/L", 2.371),
]


@pytest.mark.parametrize(
    ("biomarker", "molar_mass", "value", "from_unit", "to_unit", "expected"),
    KNOWN_ANSWERS,
)
def test_known_answer_molar_conversions(
    biomarker: str,
    molar_mass: float,
    value: float,
    from_unit: str,
    to_unit: str,
    expected: float,
) -> None:
    result = convert(value, from_unit, to_unit, molar_mass=molar_mass)
    assert math.isclose(result, expected, rel_tol=1e-3), biomarker


def test_same_dimension_scalar_conversion_is_exact() -> None:
    # 100 mg/dL == 1000 mg/L == 1 g/L
    assert math.isclose(convert(100.0, "mg/dL", "g/L"), 1.0, rel_tol=1e-12)
    # 1 g/dL == 1000 mg/dL
    assert math.isclose(convert(1.0, "g/dL", "mg/dL"), 1000.0, rel_tol=1e-12)


def test_identity_returns_value_unchanged() -> None:
    assert convert(4.2, "mg/dL", "mg/dL") == 4.2


def test_apob_mg_dl_vs_g_l_is_not_silently_wrong() -> None:
    # The ADR-0031 motivating bug: an ApoB target in mg/dL vs a result in g/L is
    # a factor-of-100, and must convert correctly rather than compare raw.
    assert math.isclose(convert(1.0, "g/L", "mg/dL"), 100.0, rel_tol=1e-12)


def test_molar_conversion_without_context_fails_loud() -> None:
    with pytest.raises(MissingMolarContextError):
        convert(100.0, "mg/dL", "mmol/L")


def test_incommensurable_units_fail_loud() -> None:
    with pytest.raises(IncommensurableUnitsError):
        convert(1.0, "g", "L")


def test_incommensurable_even_with_molar_mass_fails_loud() -> None:
    # A molar mass cannot bridge mass to volume; the difference is not molar.
    with pytest.raises(IncommensurableUnitsError):
        convert(1.0, "g", "L", molar_mass=180.0)


def test_unknown_unit_fails_loud() -> None:
    with pytest.raises(UnknownUnitError):
        convert(1.0, "not-a-unit", "mg/dL")
    with pytest.raises(UnknownUnitError):
        parse_unit("")


def test_identity_of_invalid_unit_still_fails_loud() -> None:
    # The from == to fast path must not become a validation bypass: an identical
    # garbage pair is still an invalid unit and must fail loud, not pass through.
    with pytest.raises(UnknownUnitError):
        convert(5.0, "not-a-unit", "not-a-unit")


def test_non_positive_molar_mass_rejected() -> None:
    with pytest.raises(ValueError, match="molar mass must be positive"):
        convert(100.0, "mg/dL", "mmol/L", molar_mass=0.0)
    with pytest.raises(ValueError, match="molar mass must be positive"):
        convert(100.0, "mg/dL", "mmol/L", molar_mass=-5.0)


def test_molar_mass_ignored_when_not_needed() -> None:
    # Supplying molar mass for a same-dimension conversion is harmless — it is
    # simply unused, not an error.
    assert math.isclose(
        convert(100.0, "mg/dL", "g/L", molar_mass=180.0), 1.0, rel_tol=1e-12
    )


def test_is_valid_unit() -> None:
    assert is_valid_unit("mg/dL")
    assert is_valid_unit("mmol/L")
    assert not is_valid_unit("not-a-unit")
    assert not is_valid_unit("")
