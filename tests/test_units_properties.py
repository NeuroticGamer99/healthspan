"""Property-based acceptance suite for the units module (ADR-0031, testing-strategy.md).

These properties are written against the internal units-module API — never against
``ucumvert``/``pint`` directly — so they double as the acceptance harness for the
conversion-engine sub-decision: any engine behind :mod:`healthspan.units` must pass
this suite unchanged. Relative tolerance for float comparisons is fixed suite-wide
at ``1e-9`` (testing-strategy.md).
"""

import math

import pytest
from hypothesis import assume, given
from hypothesis import strategies as st

from healthspan.units import MissingMolarContextError, convert

REL_TOL = 1e-9
# Near-zero magnitudes have no meaningful relative tolerance; pair it with a tiny
# absolute floor so a legitimate 0.0 ≈ 1e-30 round-trip is not flagged.
ABS_TOL = 1e-12

# Units that share the mass-concentration dimension ([mass]/[volume]); all
# interconvertible by a pure scalar factor.
MASS_CONC_UNITS = ["g/L", "mg/L", "mg/dL", "g/dL", "ug/mL", "ng/mL", "mg/mL", "ug/dL"]

# Units that share the substance-concentration dimension ([substance]/[volume]).
SUBSTANCE_CONC_UNITS = ["mol/L", "mmol/L", "umol/L", "nmol/L", "pmol/L"]

# Every unit the suite exercises, for the dimension-agnostic identity property.
ALL_UNITS = MASS_CONC_UNITS + SUBSTANCE_CONC_UNITS + ["g", "mg", "ug", "L", "dL", "mL"]

# Magnitudes span the unit factors (ng/mL … g/dL is ~9 orders) without risking
# float overflow through the widest conversion.
magnitudes = st.floats(
    min_value=1e-6, max_value=1e6, allow_nan=False, allow_infinity=False
)
# Physiologically plausible molar masses (g/mol) — small molecules to large proteins.
molar_masses = st.floats(
    min_value=1.0, max_value=1e6, allow_nan=False, allow_infinity=False
)


def _close(a: float, b: float) -> bool:
    return math.isclose(a, b, rel_tol=REL_TOL, abs_tol=ABS_TOL)


@given(value=magnitudes, unit=st.sampled_from(ALL_UNITS))
def test_identity(value: float, unit: str) -> None:
    """Converting a value to its own unit returns it exactly."""
    assert convert(value, unit, unit) == value


@given(
    value=magnitudes,
    family=st.sampled_from([MASS_CONC_UNITS, SUBSTANCE_CONC_UNITS]),
    data=st.data(),
)
def test_round_trip(value: float, family: list[str], data: st.DataObject) -> None:
    """A→B→A recovers the original within tolerance."""
    a = data.draw(st.sampled_from(family))
    b = data.draw(st.sampled_from(family))
    there = convert(value, a, b)
    back = convert(there, b, a)
    assert _close(back, value)


@given(
    value=magnitudes,
    family=st.sampled_from([MASS_CONC_UNITS, SUBSTANCE_CONC_UNITS]),
    data=st.data(),
)
def test_composition(value: float, family: list[str], data: st.DataObject) -> None:
    """A→B→C agrees with A→C within tolerance."""
    a = data.draw(st.sampled_from(family))
    b = data.draw(st.sampled_from(family))
    c = data.draw(st.sampled_from(family))
    stepwise = convert(convert(value, a, b), b, c)
    direct = convert(value, a, c)
    assert _close(stepwise, direct)


@given(
    x=magnitudes,
    y=magnitudes,
    family=st.sampled_from([MASS_CONC_UNITS, SUBSTANCE_CONC_UNITS]),
    data=st.data(),
)
def test_order_preservation(
    x: float, y: float, family: list[str], data: st.DataObject
) -> None:
    """x < y before a linear conversion implies converted x < converted y."""
    assume(x != y)
    a = data.draw(st.sampled_from(family))
    b = data.draw(st.sampled_from(family))
    cx = convert(x, a, b)
    cy = convert(y, a, b)
    assert (x < y) == (cx < cy)


@given(
    value=magnitudes,
    molar_mass=molar_masses,
    mass_unit=st.sampled_from(MASS_CONC_UNITS),
    substance_unit=st.sampled_from(SUBSTANCE_CONC_UNITS),
)
def test_molar_round_trip(
    value: float, molar_mass: float, mass_unit: str, substance_unit: str
) -> None:
    """Mass-conc → substance-conc → mass-conc recovers the original, given the mass."""
    substance = convert(value, mass_unit, substance_unit, molar_mass=molar_mass)
    back = convert(substance, substance_unit, mass_unit, molar_mass=molar_mass)
    assert _close(back, value)


@given(
    value=magnitudes,
    mass_unit=st.sampled_from(MASS_CONC_UNITS),
    substance_unit=st.sampled_from(SUBSTANCE_CONC_UNITS),
)
def test_molar_conversion_without_context_fails_loud(
    value: float, mass_unit: str, substance_unit: str
) -> None:
    """A molar conversion attempted without biomarker context fails loudly — never a
    silent scalar fallback (ADR-0031)."""
    with pytest.raises(MissingMolarContextError):
        convert(value, mass_unit, substance_unit)
    with pytest.raises(MissingMolarContextError):
        convert(value, substance_unit, mass_unit)
