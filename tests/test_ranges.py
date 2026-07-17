"""Tests for the reference-range comparison core (ADR-0005, Phase 3 WI-3).

Three groups, matching the design brief's §7 requirements:

1. Example-based tests reproducing every row of the brief's §1.4 worked-example
   table, plus each §3 unit-normalization error condition and each §1.5
   short-circuit — these pin :func:`healthspan.ranges.compare`'s behavior
   exactly, row for row.
2. A Hypothesis property suite over the interval logic, including the
   unit-normalization invariance property that is the single most valuable
   test here: it is the one that would have caught the ADR-0005
   mg/dL-vs-g/L factor-of-100 bug.
3. Integration tests over :func:`healthspan.ranges.resolve_ranges` and
   :func:`healthspan.ranges.resolve_framework_id` against a real (synthetic)
   database, proving the point-in-time SQL resolution rule (brief §2).

Fixtures use generic public biomarker/framework names and synthetic numeric
values — no personal health data (CLAUDE.md).
"""

from collections.abc import Iterator
from pathlib import Path

import pytest
import sqlcipher3
from hypothesis import given
from hypothesis import strategies as st

from healthspan import db, migrate
from healthspan.kdf import DbKey

# Whitebox imports of the interval-math internals, for the mutual-exclusivity
# property only (below): brief §7 pins "in_range/below/above are mutually
# exclusive" as a property of the *predicates*, not just of compare()'s
# single-valued return (which is trivially "exclusive" by Python control
# flow regardless of whether the underlying booleans are ever inconsistent).
# There is no equivalent black-box formulation that does not either
# duplicate the interval algebra under test or weaken the property, so this
# reaches past the public surface deliberately and narrowly.
from healthspan.ranges import (
    FLAGS,
    Comparison,
    ResolvedRange,
    ResultValue,
    _is_subset,  # pyright: ignore[reportPrivateUsage]
    _lies_entirely_above,  # pyright: ignore[reportPrivateUsage]
    _lies_entirely_below,  # pyright: ignore[reportPrivateUsage]
    _result_interval,  # pyright: ignore[reportPrivateUsage]
    _target_interval,  # pyright: ignore[reportPrivateUsage]
    compare,
    resolve_framework_id,
    resolve_ranges,
)
from healthspan.units import convert

KEY = DbKey(bytearray(range(1, 33)))

# An arbitrary valid UCUM unit used whenever a test wants normalization to be
# a no-op (units.convert's same-unit identity fast path — still validated,
# never a silent bypass) so the interval logic is exercised in isolation.
_UNIT = "mg/dL"


# ---------------------------------------------------------------------------
# compare() helpers
# ---------------------------------------------------------------------------


def _cmp(
    value_num: float | None,
    comparator: str | None,
    low: float | None,
    high: float | None,
    *,
    unit: str = _UNIT,
    range_text: str | None = None,
    value_text: str | None = None,
) -> Comparison:
    """Call compare() with matched units so normalization is a no-op."""
    return compare(
        framework="Generic Framework",
        biomarker="Generic Biomarker",
        canonical_unit=unit,
        molar_mass=None,
        range_row=ResolvedRange(
            range_low=low,
            range_high=high,
            unit=unit,
            range_text=range_text,
            effective_date="2026-01-01",
        ),
        result=ResultValue(
            value_num=value_num, comparator=comparator, value_text=value_text, unit=unit
        ),
    )


# ---------------------------------------------------------------------------
# §1.4 — the worked-example table, every reachable row
# ---------------------------------------------------------------------------

# (value, comparator, low, high, expected_flag) — brief §1.4, in table order.
# The final table row (5, None, None, None -> "in_range") is marked
# unreachable by the brief itself (the ADR-0005 CHECK forbids a range row
# with neither bound nor range_text) and is exercised separately below via
# the not_comparable short-circuit it actually hits.
WORKED_EXAMPLES = [
    pytest.param(92.0, None, None, 60.0, "above", id="92_vs_(,60]"),
    pytest.param(55.0, None, None, 60.0, "in_range", id="55_vs_(,60]"),
    pytest.param(60.0, None, None, 60.0, "in_range", id="60_vs_(,60]_inclusive"),
    pytest.param(0.1, "<", 0.5, None, "below", id="<0.1_vs_[0.5,)"),
    pytest.param(0.1, "<", None, 1.0, "in_range", id="<0.1_vs_(,1.0]"),
    pytest.param(
        0.1, "<", 0.0, 10.0, "indeterminate", id="<0.1_vs_[0,10]_sign_assumption"
    ),
    pytest.param(5.0, "<", 0.0, 10.0, "indeterminate", id="<5_vs_[0,10]"),
    pytest.param(150.0, ">", None, 100.0, "above", id=">150_vs_(,100]"),
    pytest.param(150.0, ">", 100.0, None, "in_range", id=">150_vs_[100,)"),
    pytest.param(0.5, "<", 0.5, None, "below", id="<0.5_vs_[0.5,)_open_boundary"),
    pytest.param(
        0.5, "<=", 0.5, None, "indeterminate", id="<=0.5_vs_[0.5,)_closed_boundary"
    ),
    pytest.param(40.0, ">=", 40.0, None, "in_range", id=">=40_vs_[40,)"),
    pytest.param(40.0, ">", 40.0, None, "in_range", id=">40_vs_[40,)"),
]


@pytest.mark.parametrize(
    ("value", "comparator", "low", "high", "expected"), WORKED_EXAMPLES
)
def test_worked_examples(
    value: float,
    comparator: str | None,
    low: float | None,
    high: float | None,
    expected: str,
) -> None:
    result = _cmp(value, comparator, low, high)
    assert result.flag == expected


def test_open_vs_closed_boundary_is_the_only_difference() -> None:
    # The brief's explicitly load-bearing pair, side by side: identical
    # value and range, differing only in comparator strictness.
    below = _cmp(0.5, "<", 0.5, None)
    indeterminate = _cmp(0.5, "<=", 0.5, None)
    assert below.flag == "below"
    assert indeterminate.flag == "indeterminate"


def test_worked_examples_carry_normalized_range_and_unit() -> None:
    # range_low/range_high/unit on a successful comparison are the
    # normalized values actually compared, with the canonical unit.
    result = _cmp(55.0, None, None, 60.0)
    assert result.range_high == 60.0
    assert result.unit == _UNIT
    assert result.reason is None


# ---------------------------------------------------------------------------
# §1.6 — the closed flag vocabulary
# ---------------------------------------------------------------------------


def test_flags_is_the_pinned_closed_set() -> None:
    assert (
        frozenset(
            {
                "in_range",
                "below",
                "above",
                "indeterminate",
                "not_comparable",
                "no_range",
                "error",
            }
        )
        == FLAGS
    )


# ---------------------------------------------------------------------------
# §1.5 — evaluation order / short-circuits
# ---------------------------------------------------------------------------


def test_step1_no_range_row_short_circuits_before_any_unit_work() -> None:
    result = compare(
        framework="Generic Framework",
        biomarker="Generic Biomarker",
        canonical_unit=None,  # would be an `error` condition if ever reached
        molar_mass=None,
        range_row=None,
        result=ResultValue(
            value_num=90.0, comparator=None, value_text=None, unit=_UNIT
        ),
    )
    assert result.flag == "no_range"
    assert result.reason is None
    assert result.range_low is None
    assert result.range_high is None
    assert result.unit is None
    assert result.effective_date is None
    assert result.range_text is None


def test_step2_range_text_only_target_is_not_comparable() -> None:
    result = _cmp(90.0, None, None, None, range_text="Trending down over time")
    assert result.flag == "not_comparable"
    assert result.reason == "range has no numeric bounds (range_text only)"
    assert result.range_text == "Trending down over time"
    assert result.range_low is None
    assert result.range_high is None


def test_step3_qualitative_result_against_numeric_range_is_not_comparable() -> None:
    result = _cmp(None, None, 0.0, 10.0, value_text="Not Detected")
    assert result.flag == "not_comparable"
    assert result.reason == "result is qualitative and has no numeric value to compare"


def test_step2_precedes_step3_when_both_apply() -> None:
    # A qualitative result against a range_text-only target: both steps'
    # conditions hold. The brief pins that the answer is not_comparable
    # either way, but the *reason* carried is the range's (step 2's).
    result = _cmp(
        None,
        None,
        None,
        None,
        range_text="See practitioner notes",
        value_text="Reactive",
    )
    assert result.flag == "not_comparable"
    assert result.reason == "range has no numeric bounds (range_text only)"
    assert result.range_text == "See practitioner notes"


def test_unreachable_neither_bound_nor_text_still_resolves_gracefully() -> None:
    # Brief §1.4's final table row: a range with neither bound nor text is
    # forbidden by the ADR-0005 CHECK, so this ResolvedRange shape cannot
    # come from the database — but compare() must not crash if it is ever
    # constructed, and step 2's condition (no numeric bounds) still fires.
    result = _cmp(5.0, None, None, None)
    assert result.flag == "not_comparable"


# ---------------------------------------------------------------------------
# §3 — unit-normalization error conditions ("error" is loud, never fatal)
# ---------------------------------------------------------------------------


def test_error_no_canonical_unit_is_a_catalog_gap() -> None:
    result = compare(
        framework="Generic Framework",
        biomarker="Generic Biomarker",
        canonical_unit=None,
        molar_mass=None,
        range_row=ResolvedRange(
            range_low=0.0,
            range_high=10.0,
            unit=_UNIT,
            range_text=None,
            effective_date=None,
        ),
        result=ResultValue(value_num=5.0, comparator=None, value_text=None, unit=_UNIT),
    )
    assert result.flag == "error"
    assert result.reason == (
        "no canonical unit for this biomarker — cannot normalize (catalog gap)"
    )


def test_error_result_has_no_unit() -> None:
    result = compare(
        framework="Generic Framework",
        biomarker="Generic Biomarker",
        canonical_unit=_UNIT,
        molar_mass=None,
        range_row=ResolvedRange(
            range_low=0.0,
            range_high=10.0,
            unit=_UNIT,
            range_text=None,
            effective_date=None,
        ),
        result=ResultValue(value_num=5.0, comparator=None, value_text=None, unit=None),
    )
    assert result.flag == "error"
    assert result.reason == "result has no unit — cannot normalize"


def test_error_unknown_unit_names_the_offending_string() -> None:
    result = compare(
        framework="Generic Framework",
        biomarker="Generic Biomarker",
        canonical_unit=_UNIT,
        molar_mass=None,
        range_row=ResolvedRange(
            range_low=0.0,
            range_high=10.0,
            unit=_UNIT,
            range_text=None,
            effective_date=None,
        ),
        result=ResultValue(
            value_num=5.0, comparator=None, value_text=None, unit="not-a-real-unit"
        ),
    )
    assert result.flag == "error"
    assert result.reason is not None
    assert "not-a-real-unit" in result.reason


def test_error_incommensurable_units_names_both() -> None:
    result = compare(
        framework="Generic Framework",
        biomarker="Generic Biomarker",
        canonical_unit="L",
        molar_mass=None,
        range_row=ResolvedRange(
            range_low=0.0,
            range_high=10.0,
            unit="L",
            range_text=None,
            effective_date=None,
        ),
        result=ResultValue(value_num=5.0, comparator=None, value_text=None, unit="g"),
    )
    assert result.flag == "error"
    assert result.reason is not None
    assert "g" in result.reason
    assert "L" in result.reason


def test_error_missing_molar_context_names_the_biomarker() -> None:
    result = compare(
        framework="Generic Framework",
        biomarker="Apolipoprotein B",
        canonical_unit="mmol/L",
        molar_mass=None,  # not curated -> molar bridge unavailable
        range_row=ResolvedRange(
            range_low=0.0,
            range_high=10.0,
            unit="mmol/L",
            range_text=None,
            effective_date=None,
        ),
        result=ResultValue(
            value_num=90.0, comparator=None, value_text=None, unit="mg/dL"
        ),
    )
    assert result.flag == "error"
    assert result.reason is not None
    assert "Apolipoprotein B" in result.reason
    assert "molar_mass" in result.reason


def test_range_side_unit_error_also_caught() -> None:
    # The range row's own unit can be the culprit, not just the result's.
    result = compare(
        framework="Generic Framework",
        biomarker="Generic Biomarker",
        canonical_unit=_UNIT,
        molar_mass=None,
        range_row=ResolvedRange(
            range_low=0.0,
            range_high=10.0,
            unit="not-a-real-unit",
            range_text=None,
            effective_date=None,
        ),
        result=ResultValue(value_num=5.0, comparator=None, value_text=None, unit=_UNIT),
    )
    assert result.flag == "error"
    assert result.reason is not None
    assert "not-a-real-unit" in result.reason


def test_error_never_raises() -> None:
    # ADR-0005's safety correction: unreconcilable units fail loud, not
    # fatally. None of the error-path tests above should ever have let an
    # exception escape; this is a direct restatement of that guarantee for
    # the worst-case combination (everything wrong at once).
    result = compare(
        framework="Generic Framework",
        biomarker="Generic Biomarker",
        canonical_unit="not-a-real-unit-either",
        molar_mass=None,
        range_row=ResolvedRange(
            range_low=0.0,
            range_high=10.0,
            unit="also-not-real",
            range_text=None,
            effective_date=None,
        ),
        result=ResultValue(
            value_num=5.0, comparator=None, value_text=None, unit="still-fake"
        ),
    )
    assert result.flag == "error"


def test_non_positive_molar_mass_is_a_bug_not_a_data_condition() -> None:
    # Brief §3: this ValueError is deliberately NOT caught by compare() — a
    # migration-0005 CHECK failure, not a normal data condition.
    with pytest.raises(ValueError, match="molar mass must be positive"):
        compare(
            framework="Generic Framework",
            biomarker="Generic Biomarker",
            canonical_unit="mmol/L",
            molar_mass=-1.0,
            range_row=ResolvedRange(
                range_low=0.0,
                range_high=10.0,
                unit="mmol/L",
                range_text=None,
                effective_date=None,
            ),
            result=ResultValue(
                value_num=90.0, comparator=None, value_text=None, unit="mg/dL"
            ),
        )


# ---------------------------------------------------------------------------
# Property suite — the interval logic (brief §7)
# ---------------------------------------------------------------------------

_finite = st.floats(
    min_value=-1e9, max_value=1e9, allow_nan=False, allow_infinity=False
)
_comparators = st.sampled_from([None, "<", "<=", ">=", ">"])


@st.composite
def _bounds(draw: st.DrawFn) -> tuple[float | None, float | None]:
    """A (low, high) pair honoring the ADR-0005 CHECK: low <= high when both
    are present; either side (never both) may be absent."""
    low = draw(st.one_of(st.none(), _finite))
    high = draw(st.one_of(st.none(), _finite))
    if low is not None and high is not None and low > high:
        low, high = high, low
    if low is None and high is None:
        # At least one numeric bound must exist for a numeric-target scenario.
        high = draw(_finite)
    return low, high


@given(value=_finite, comparator=_comparators, bounds=_bounds())
def test_verdict_is_always_in_the_closed_flag_set(
    value: float, comparator: str | None, bounds: tuple[float | None, float | None]
) -> None:
    low, high = bounds
    result = _cmp(value, comparator, low, high)
    assert result.flag in FLAGS


@given(
    range_row_present=st.booleans(),
    text_only=st.booleans(),
    qualitative=st.booleans(),
    bad_unit=st.booleans(),
    value=_finite,
    comparator=_comparators,
    bounds=_bounds(),
)
def test_verdict_is_always_in_flags_across_every_short_circuit(
    range_row_present: bool,
    text_only: bool,
    qualitative: bool,
    bad_unit: bool,
    value: float,
    comparator: str | None,
    bounds: tuple[float | None, float | None],
) -> None:
    """The closed-set membership property exercised across every evaluation
    path (no_range / not_comparable / error / the four interval verdicts),
    not just the matched-unit happy path."""
    low, high = (None, None) if text_only else bounds
    range_row = (
        None
        if not range_row_present
        else ResolvedRange(
            range_low=low,
            range_high=high,
            unit="not-a-unit" if bad_unit else _UNIT,
            range_text="some note" if text_only else None,
            effective_date=None,
        )
    )
    result = compare(
        framework="Generic Framework",
        biomarker="Generic Biomarker",
        canonical_unit=_UNIT,
        molar_mass=None,
        range_row=range_row,
        result=ResultValue(
            value_num=None if qualitative else value,
            comparator=None if qualitative else comparator,
            value_text="Reactive" if qualitative else None,
            unit=_UNIT,
        ),
    )
    assert result.flag in FLAGS
    assert (result.reason is not None) == (result.flag in ("error", "not_comparable"))


@given(value=_finite, bounds=_bounds())
def test_exact_value_in_range_iff_naive_comparison_agrees(
    value: float, bounds: tuple[float | None, float | None]
) -> None:
    """An exact value v flags in_range iff L <= v <= H (NULL as infinity) —
    the interval machinery must agree with the naive comparison (brief §7)."""
    low, high = bounds
    naive_in_range = (low is None or low <= value) and (high is None or value <= high)
    result = _cmp(value, None, low, high)
    assert (result.flag == "in_range") == naive_in_range


@given(value=_finite, comparator=_comparators, bounds=_bounds())
def test_in_range_below_above_are_mutually_exclusive(
    value: float,
    comparator: str | None,
    bounds: tuple[float | None, float | None],
) -> None:
    """At most one of the three disjoint-classification predicates ever
    holds — the internal consistency the four-way verdict depends on.

    ``comparator`` is drawn, not fixed: the *target* interval is closed at
    every finite bound by construction, so the result interval's comparator
    branch is the only place open/closed variability exists at all. Pinning
    it to None here would confine the property to closed point intervals and
    never reach the boundary logic the module calls load-bearing — the very
    logic this property reaches past the public surface to protect.
    """
    low, high = bounds
    r = _result_interval(value, comparator)
    t = _target_interval(low, high)
    predicates = (
        _is_subset(r, t),
        _lies_entirely_below(r, t),
        _lies_entirely_above(r, t),
    )
    assert sum(predicates) <= 1


# Exact-arithmetic strategies for the ground-truth property below. Integers
# (widened to float) keep every probe point in `_oracle_verdict` exactly
# representable, so a disagreement is a real logic error and never float
# fuzz at a shared boundary — which is exactly where this property looks.
_exact = st.integers(min_value=-100, max_value=100).map(float)


@st.composite
def _exact_bounds(draw: st.DrawFn) -> tuple[float | None, float | None]:
    """A (low, high) pair over `_exact`, honoring the ADR-0005 CHECK."""
    low = draw(st.one_of(st.none(), _exact))
    high = draw(st.one_of(st.none(), _exact))
    if low is not None and high is not None and low > high:
        low, high = high, low
    if low is None and high is None:
        high = draw(_exact)
    return low, high


def _oracle_verdict(
    value: float, comparator: str | None, low: float | None, high: float | None
) -> str:
    """Ground truth for the four interval verdicts, derived independently.

    Deliberately *not* the interval algebra under test: this decides by
    testing membership of concrete probe points, from the plain definitions
    of "the result says the true value is <v" and "the target is [L, H]".
    Two formulations that must agree is the whole point — a transcription
    error in `ranges.py`'s bound handling cannot hide here, because nothing
    is shared but the answer.

    The probe set is every bound, each nudged either side, plus far-field
    points. That is sufficient rather than approximate: both intervals are
    unions of at most two bound-delimited pieces, so any disagreement
    between them must show up adjacent to a bound or in the far field, and
    integral inputs make every probe exact.
    """

    def in_result(x: float) -> bool:
        if comparator is None:
            return x == value
        if comparator == "<":
            return x < value
        if comparator == "<=":
            return x <= value
        if comparator == ">":
            return x > value
        return x >= value

    def in_target(x: float) -> bool:
        return (low is None or x >= low) and (high is None or x <= high)

    probes: set[float] = {-1e9, 1e9}
    for bound in (value, low, high):
        if bound is not None:
            probes.update({bound - 1.0, bound - 0.5, bound, bound + 0.5, bound + 1.0})

    in_r = [x for x in sorted(probes) if in_result(x)]
    assert in_r, "the probe set must always intersect a non-empty result interval"
    hits = [in_target(x) for x in in_r]

    if all(hits):
        return "in_range"
    if not any(hits):
        # Disjoint. Which side: every probe in R sits below T, or above it.
        if low is not None and all(x < low for x in in_r):
            return "below"
        if high is not None and all(x > high for x in in_r):
            return "above"
        # Disjoint from an unbounded-that-side target is impossible.
        raise AssertionError("disjoint from T but on neither side of it")
    return "indeterminate"


@given(value=_exact, comparator=_comparators, bounds=_exact_bounds())
def test_verdict_agrees_with_an_independent_membership_oracle(
    value: float,
    comparator: str | None,
    bounds: tuple[float | None, float | None],
) -> None:
    """compare()'s verdict matches a ground truth derived from point
    membership rather than from interval algebra (brief §7).

    This is the property that makes the censored/open-closed logic safe for
    randomized input rather than only for the ~13 hand-written rows of the
    §1.4 table: it ranges over every (comparator, bound-relationship)
    combination, including the shared-boundary cases where `<0.5` must be
    `below` but `<=0.5` must be `indeterminate` against `[0.5, +inf)`.
    """
    low, high = bounds
    assert _cmp(value, comparator, low, high).flag == _oracle_verdict(
        value, comparator, low, high
    )


@given(
    low=st.floats(min_value=-1e6, max_value=1e6, allow_nan=False),
    delta=st.floats(min_value=0.001, max_value=1e6, allow_nan=False),
)
def test_censored_subset_of_in_range_never_flags_below_or_above(
    low: float, delta: float
) -> None:
    """A censored result whose interval is a subset of the exact value's
    in-range set never flags below/above (monotonicity sanity, brief §7)."""
    high = low + delta
    midpoint = low + delta / 2
    # "<midpoint" against (None, high]: (-inf, midpoint) is a strict subset
    # of (-inf, high] whenever midpoint < high.
    below_censored = _cmp(midpoint, "<", None, high)
    assert below_censored.flag not in ("below", "above")
    assert below_censored.flag == "in_range"
    # ">midpoint" against [low, None): (midpoint, +inf) is a strict subset
    # of [low, +inf) whenever midpoint > low.
    above_censored = _cmp(midpoint, ">", low, None)
    assert above_censored.flag not in ("below", "above")
    assert above_censored.flag == "in_range"


# The unit-normalization invariance property — the single most valuable test
# here (brief §7): it is what would have caught the ADR-0005 mg/dL-vs-g/L
# factor-of-100 bug. Comparing the same physical quantity and the same
# physical range expressed in two different unit systems must produce the
# same verdict.

_positive = st.floats(
    min_value=1e-3, max_value=1e6, allow_nan=False, allow_infinity=False
)
_molar_masses = st.floats(
    min_value=1.0, max_value=1e4, allow_nan=False, allow_infinity=False
)


@st.composite
def _positive_bounds(draw: st.DrawFn) -> tuple[float | None, float | None]:
    low = draw(st.one_of(st.none(), _positive))
    high = draw(st.one_of(st.none(), _positive))
    if low is not None and high is not None and low > high:
        low, high = high, low
    if low is None and high is None:
        high = draw(_positive)
    return low, high


@given(value=_positive, comparator=_comparators, bounds=_positive_bounds())
def test_unit_normalization_invariance_scalar(
    value: float, comparator: str | None, bounds: tuple[float | None, float | None]
) -> None:
    """The same result and range, expressed in mg/dL vs g/L, must verdict
    identically once both are normalized to a fixed canonical_unit."""
    low, high = bounds

    def in_mg_dl() -> Comparison:
        return compare(
            framework="Generic Framework",
            biomarker="Generic Biomarker",
            canonical_unit="mg/dL",
            molar_mass=None,
            range_row=ResolvedRange(
                range_low=low,
                range_high=high,
                unit="mg/dL",
                range_text=None,
                effective_date=None,
            ),
            result=ResultValue(
                value_num=value, comparator=comparator, value_text=None, unit="mg/dL"
            ),
        )

    def in_g_l() -> Comparison:
        # Re-express the identical physical quantities in g/L via the same
        # trusted conversion the module itself uses — the test's own
        # correctness does not depend on hand-computing the factor.
        return compare(
            framework="Generic Framework",
            biomarker="Generic Biomarker",
            canonical_unit="mg/dL",
            molar_mass=None,
            range_row=ResolvedRange(
                range_low=None if low is None else convert(low, "mg/dL", "g/L"),
                range_high=None if high is None else convert(high, "mg/dL", "g/L"),
                unit="g/L",
                range_text=None,
                effective_date=None,
            ),
            result=ResultValue(
                value_num=convert(value, "mg/dL", "g/L"),
                comparator=comparator,
                value_text=None,
                unit="g/L",
            ),
        )

    assert in_mg_dl().flag == in_g_l().flag


@given(
    value=_positive,
    comparator=_comparators,
    bounds=_positive_bounds(),
    molar_mass=_molar_masses,
)
def test_unit_normalization_invariance_molar(
    value: float,
    comparator: str | None,
    bounds: tuple[float | None, float | None],
    molar_mass: float,
) -> None:
    """Same invariance property across a molar mass <-> substance bridge
    (mg/dL vs mmol/L) — the exact class of conversion ADR-0056 added
    ``molar_mass`` to support."""
    low, high = bounds

    def in_mg_dl() -> Comparison:
        return compare(
            framework="Generic Framework",
            biomarker="Generic Biomarker",
            canonical_unit="mg/dL",
            molar_mass=molar_mass,
            range_row=ResolvedRange(
                range_low=low,
                range_high=high,
                unit="mg/dL",
                range_text=None,
                effective_date=None,
            ),
            result=ResultValue(
                value_num=value, comparator=comparator, value_text=None, unit="mg/dL"
            ),
        )

    def in_mmol_l() -> Comparison:
        return compare(
            framework="Generic Framework",
            biomarker="Generic Biomarker",
            canonical_unit="mg/dL",
            molar_mass=molar_mass,
            range_row=ResolvedRange(
                range_low=(
                    None
                    if low is None
                    else convert(low, "mg/dL", "mmol/L", molar_mass=molar_mass)
                ),
                range_high=(
                    None
                    if high is None
                    else convert(high, "mg/dL", "mmol/L", molar_mass=molar_mass)
                ),
                unit="mmol/L",
                range_text=None,
                effective_date=None,
            ),
            result=ResultValue(
                value_num=convert(value, "mg/dL", "mmol/L", molar_mass=molar_mass),
                comparator=comparator,
                value_text=None,
                unit="mmol/L",
            ),
        )

    assert in_mg_dl().flag == in_mmol_l().flag


# ---------------------------------------------------------------------------
# Bound coincidence across units (ADR-0058 §3) — normalization is float
# arithmetic, so a value that is *exactly* a bound in one unit need not land
# exactly on it in another. The verdict must still be the same.
# ---------------------------------------------------------------------------


def test_value_exactly_on_a_bound_via_the_molar_path_is_in_range() -> None:
    """The regression that motivated the bound tolerance (ADR-0058 §3).

    5.171967933798811 mmol/L is exactly 200 mg/dL for Total Cholesterol
    (molar mass 386.7) — bridging it back yields 200.00000000000003, which an
    exact `==` flagged `above` while the very same quantity in mg/dL and g/L
    flagged `in_range`. One physical value, three unit representations, two
    verdicts: the silently-wrong-flag class ADR-0005 exists to close.
    """
    molar_mass = 386.7
    exactly_the_bound_in_mmol = convert(200.0, "mg/dL", "mmol/L", molar_mass=molar_mass)
    # Precondition: the round trip really does overshoot, or this test is
    # asserting nothing and would keep passing if the tolerance were removed.
    assert (
        convert(exactly_the_bound_in_mmol, "mmol/L", "mg/dL", molar_mass=molar_mass)
        > 200.0
    )

    def flag_for(value: float, unit: str) -> str:
        return compare(
            framework="Generic Framework",
            biomarker="Total Cholesterol",
            canonical_unit="mg/dL",
            molar_mass=molar_mass,
            range_row=ResolvedRange(
                range_low=None,
                range_high=200.0,
                unit="mg/dL",
                range_text=None,
                effective_date=None,
            ),
            result=ResultValue(
                value_num=value, comparator=None, value_text=None, unit=unit
            ),
        ).flag

    assert flag_for(200.0, "mg/dL") == "in_range"  # identity path
    assert flag_for(2.0, "g/L") == "in_range"  # scalar path
    assert flag_for(exactly_the_bound_in_mmol, "mmol/L") == "in_range"  # molar path


@given(
    bound=st.floats(min_value=0.1, max_value=1e4, allow_nan=False),
    molar_mass=st.floats(min_value=1.0, max_value=1000.0, allow_nan=False),
    inclusive_side=st.sampled_from(["low", "high"]),
)
def test_a_value_reported_exactly_on_a_bound_in_another_unit_is_in_range(
    bound: float, molar_mass: float, inclusive_side: str
) -> None:
    """A result sitting exactly on an inclusive bound flags `in_range`
    regardless of which unit it is reported in (ADR-0058 §3).

    The generalization of the regression above: bounds are inclusive, so the
    bound itself is in range by definition, and re-expressing that same
    physical quantity in mmol/L must not move it out. This is the property
    that fails the moment the bound comparison goes back to exact `==`.
    """
    as_mmol = convert(bound, "mg/dL", "mmol/L", molar_mass=molar_mass)
    low, high = (bound, None) if inclusive_side == "low" else (None, bound)
    result = compare(
        framework="Generic Framework",
        biomarker="Generic Biomarker",
        canonical_unit="mg/dL",
        molar_mass=molar_mass,
        range_row=ResolvedRange(
            range_low=low,
            range_high=high,
            unit="mg/dL",
            range_text=None,
            effective_date=None,
        ),
        result=ResultValue(
            value_num=as_mmol, comparator=None, value_text=None, unit="mmol/L"
        ),
    )
    assert result.flag == "in_range"


# ---------------------------------------------------------------------------
# resolve_ranges() / resolve_framework_id() — the point-in-time SQL (brief §2)
# ---------------------------------------------------------------------------


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlcipher3.Connection]:
    path = tmp_path / "healthspan.db"
    db.provision(path, KEY)
    migrate.migrate_database(path, KEY)
    connection = db.connect(path, KEY)
    try:
        yield connection
    finally:
        db.close(connection)


def _insert_biomarker(conn: sqlcipher3.Connection, name: str) -> int:
    conn.execute(
        "INSERT INTO biomarkers (canonical_name, canonical_unit) VALUES (?, ?)",
        (name, _UNIT),
    )
    row = conn.execute(
        "SELECT id FROM biomarkers WHERE canonical_name = ?", (name,)
    ).fetchone()
    assert row is not None
    return int(row[0])


def _insert_framework(conn: sqlcipher3.Connection, name: str) -> int:
    conn.execute("INSERT INTO range_frameworks (name) VALUES (?)", (name,))
    row = conn.execute(
        "SELECT id FROM range_frameworks WHERE name = ?", (name,)
    ).fetchone()
    assert row is not None
    return int(row[0])


def _insert_range(
    conn: sqlcipher3.Connection,
    framework_id: int,
    biomarker_id: int,
    *,
    low: float | None,
    high: float | None,
    effective_date: str | None,
    unit: str = _UNIT,
) -> None:
    conn.execute(
        "INSERT INTO framework_ranges "
        "(framework_id, biomarker_id, range_low, range_high, unit, effective_date) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (framework_id, biomarker_id, low, high, unit, effective_date),
    )


def test_resolve_ranges_empty_pairs_is_a_no_op(conn: sqlcipher3.Connection) -> None:
    assert resolve_ranges(conn, 1, []) == []


def test_resolve_ranges_no_range_for_pair_resolves_none(
    conn: sqlcipher3.Connection,
) -> None:
    framework_id = _insert_framework(conn, "Empty Framework")
    biomarker_id = _insert_biomarker(conn, "Uncovered Marker")
    resolved = resolve_ranges(conn, framework_id, [(biomarker_id, "2026-01-01")])
    assert resolved == [None]


def test_resolve_ranges_dateless_default_used_when_no_dated_row_qualifies(
    conn: sqlcipher3.Connection,
) -> None:
    framework_id = _insert_framework(conn, "Default Only Framework")
    biomarker_id = _insert_biomarker(conn, "Default Only Marker")
    _insert_range(
        conn, framework_id, biomarker_id, low=0.0, high=10.0, effective_date=None
    )

    resolved = resolve_ranges(
        conn, framework_id, [(biomarker_id, "2020-01-01"), (biomarker_id, "2030-01-01")]
    )
    assert resolved[0] is not None
    assert resolved[0].range_high == 10.0
    assert resolved[0].effective_date is None
    assert resolved[1] is not None
    assert resolved[1].effective_date is None


def test_resolve_ranges_dated_row_wins_over_default_and_greatest_qualifying_date_wins(
    conn: sqlcipher3.Connection,
) -> None:
    framework_id = _insert_framework(conn, "Versioned Framework")
    biomarker_id = _insert_biomarker(conn, "Versioned Marker")
    _insert_range(
        conn, framework_id, biomarker_id, low=0.0, high=100.0, effective_date=None
    )
    _insert_range(
        conn,
        framework_id,
        biomarker_id,
        low=0.0,
        high=50.0,
        effective_date="2024-01-01",
    )
    _insert_range(
        conn,
        framework_id,
        biomarker_id,
        low=0.0,
        high=40.0,
        effective_date="2025-06-01",
    )

    pairs = [
        (biomarker_id, "2023-12-31"),  # before any dated row -> dateless default
        (biomarker_id, "2024-01-01"),  # exactly the first dated row
        (biomarker_id, "2024-06-01"),  # between the two dated rows -> 2024 wins
        (biomarker_id, "2025-06-01"),  # exactly the second dated row
        (biomarker_id, "2030-01-01"),  # after both -> greatest qualifying (2025) wins
    ]
    resolved = resolve_ranges(conn, framework_id, pairs)
    expected_highs = [100.0, 50.0, 50.0, 40.0, 40.0]
    expected_dates: list[str | None] = [
        None,
        "2024-01-01",
        "2024-01-01",
        "2025-06-01",
        "2025-06-01",
    ]
    for row, expected_high, expected_date in zip(
        resolved, expected_highs, expected_dates, strict=True
    ):
        assert row is not None
        assert row.range_high == expected_high
        assert row.effective_date == expected_date


def test_resolve_ranges_is_positionally_aligned_across_multiple_biomarkers(
    conn: sqlcipher3.Connection,
) -> None:
    framework_id = _insert_framework(conn, "Multi Marker Framework")
    marker_a = _insert_biomarker(conn, "Marker A")
    marker_b = _insert_biomarker(conn, "Marker B")
    _insert_range(conn, framework_id, marker_a, low=1.0, high=2.0, effective_date=None)
    # marker_b deliberately has no range at all.

    pairs = [
        (marker_a, "2026-01-01"),
        (marker_b, "2026-01-01"),
        (marker_a, "2026-06-01"),
    ]
    resolved = resolve_ranges(conn, framework_id, pairs)
    assert resolved[0] is not None
    assert resolved[0].range_low == 1.0
    assert resolved[1] is None
    assert resolved[2] is not None
    assert resolved[2].range_low == 1.0


def test_resolve_ranges_does_not_leak_across_frameworks(
    conn: sqlcipher3.Connection,
) -> None:
    framework_a = _insert_framework(conn, "Framework A")
    framework_b = _insert_framework(conn, "Framework B")
    biomarker_id = _insert_biomarker(conn, "Shared Marker")
    _insert_range(
        conn, framework_a, biomarker_id, low=1.0, high=2.0, effective_date=None
    )
    # No range seeded under framework_b for this biomarker.

    resolved_b = resolve_ranges(conn, framework_b, [(biomarker_id, "2026-01-01")])
    assert resolved_b == [None]


def test_resolve_ranges_carries_range_text_and_unit_through(
    conn: sqlcipher3.Connection,
) -> None:
    framework_id = _insert_framework(conn, "Text Framework")
    biomarker_id = _insert_biomarker(conn, "Text Marker")
    conn.execute(
        "INSERT INTO framework_ranges "
        "(framework_id, biomarker_id, range_text, unit, effective_date) "
        "VALUES (?, ?, ?, ?, NULL)",
        (framework_id, biomarker_id, "Consult practitioner", _UNIT),
    )
    resolved = resolve_ranges(conn, framework_id, [(biomarker_id, "2026-01-01")])
    assert resolved[0] is not None
    assert resolved[0].range_text == "Consult practitioner"
    assert resolved[0].range_low is None
    assert resolved[0].range_high is None


def test_resolve_framework_id_is_case_insensitive(conn: sqlcipher3.Connection) -> None:
    framework_id = _insert_framework(conn, "My Framework")
    assert resolve_framework_id(conn, "my framework") == framework_id
    assert resolve_framework_id(conn, "MY FRAMEWORK") == framework_id
    assert resolve_framework_id(conn, "My Framework") == framework_id


def test_resolve_framework_id_unknown_name_is_none(conn: sqlcipher3.Connection) -> None:
    assert resolve_framework_id(conn, "Does Not Exist") is None


def test_resolve_then_compare_end_to_end(conn: sqlcipher3.Connection) -> None:
    # A small end-to-end proof that resolve_ranges' output feeds compare()
    # directly, matching the shape the (later) read-enrichment pass will use.
    framework_id = _insert_framework(conn, "End To End Framework")
    biomarker_id = _insert_biomarker(conn, "End To End Marker")
    _insert_range(
        conn, framework_id, biomarker_id, low=0.0, high=10.0, effective_date=None
    )

    (resolved,) = resolve_ranges(conn, framework_id, [(biomarker_id, "2026-01-01")])
    result = compare(
        framework="End To End Framework",
        biomarker="End To End Marker",
        canonical_unit=_UNIT,
        molar_mass=None,
        range_row=resolved,
        result=ResultValue(value_num=5.0, comparator=None, value_text=None, unit=_UNIT),
    )
    assert result.flag == "in_range"
