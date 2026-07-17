"""Reference-range comparison core (ADR-0005, Phase 3 WI-3, ADR-0058).

Compares a stored lab result against a reference-range framework's
point-in-time target, normalizing both to the biomarker's ``canonical_unit``
(ADR-0030/ADR-0031) before any numeric comparison. This is the module ADR-0005
exists for: comparing an ApoB target in mg/dL against a result in g/L without
normalization silently produces a factor-of-100 wrong flag ("Safety
correction", ADR-0005 review item 3.D). Every ambiguous or unreconcilable path
is *named in the output* (a member of :data:`FLAGS`, with a ``reason`` string
where applicable) — never collapsed into a boolean or allowed to raise past
:func:`compare`.

The comparison model (result and target as intervals on the real line, then a
subset/disjoint question) is pinned by the WI-3 design brief §1; the five-step
evaluation order is pinned by §1.5; point-in-time range resolution is pinned
by §2; unit normalization is pinned by §3. This module implements exactly
that, not a re-derivation of it.

This module is pure-ish: :func:`resolve_ranges` and :func:`resolve_framework_id`
touch the database (read-only), :func:`compare` is a pure function over
already-fetched values. No FastAPI import belongs here — the read-enrichment
HTTP surface (``?framework=``) is a later pass's concern and calls into this
module, not the reverse.
"""

import json
import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import sqlcipher3

from healthspan import units

# The closed flag vocabulary (brief §1.6). A verdict is always exactly one of
# these — the property suite pins that as an invariant, and adding a flag
# without updating both this set and the tests that assert against it is a
# deliberate, visible break.
FLAGS = frozenset(
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

_NEG_INF = float("-inf")
_POS_INF = float("inf")


# ---------------------------------------------------------------------------
# Public data shapes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ResultValue:
    """The ADR-0030 value triple plus the reported unit.

    What one ``lab_results`` row means before normalization: ``comparator``
    non-NULL marks a censored bound (below/above assay detection);
    ``value_num IS NULL`` marks a purely qualitative result. Both are
    first-class here, never coerced to a bare number (ADR-0030).
    """

    value_num: float | None
    comparator: str | None  # '<' | '<=' | '>=' | '>' | None (exact)
    value_text: str | None
    unit: str | None  # UCUM string as reported; NULL if the result has none


@dataclass(frozen=True)
class ResolvedRange:
    """One point-in-time-resolved ``framework_ranges`` row (brief §2).

    Exactly the stored row's own values — normalization to the biomarker's
    canonical unit happens later, in :func:`compare`, never here.
    """

    range_low: float | None
    range_high: float | None
    unit: str  # UCUM string; NOT NULL in the schema (ADR-0005)
    range_text: str | None
    effective_date: str | None


@dataclass(frozen=True)
class Comparison:
    """The result of comparing one lab result to one resolved framework range.

    ``range_low``/``range_high``/``unit`` are the *normalized* values the
    comparison actually used — the canonical-unit magnitudes, not the raw
    stored ones — so a client sees what was actually compared (the same
    honesty principle as ADR-0053's ``display``). They are ``None`` whenever
    no normalized comparison happened (``no_range``/``not_comparable``/
    ``error``). ``reason`` is set iff ``flag`` is ``"error"`` or
    ``"not_comparable"``; ``None`` otherwise.
    """

    framework: str
    flag: str  # one of FLAGS
    range_low: float | None
    range_high: float | None
    unit: str | None
    effective_date: str | None
    range_text: str | None
    reason: str | None


# ---------------------------------------------------------------------------
# Interval model (brief §1.3) — genuine interval arithmetic with explicit
# open/closed bound handling. Deliberately NOT a chain of comparator
# special-cases: openness is load-bearing exactly at a shared boundary
# (`<0.5` vs `[0.5, +inf)` is `below`; `<=0.5` vs the same range is
# `indeterminate` — brief §1.3's worked pair).
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Interval:
    """An interval on the extended real line, closed/open per bound.

    Closedness at an infinite bound is meaningless (no real number equals
    +/-inf), so every constructor below sets it False there — a value can
    never "attain" an unbounded side. This is what lets the general subset /
    ordering formulas below handle unbounded intervals without a special case.
    """

    low: float
    low_closed: bool
    high: float
    high_closed: bool


def _result_interval(value: float, comparator: str | None) -> _Interval:
    """Build R from the ADR-0030 magnitude + comparator (brief §1.1).

    Assumes nothing about sign: an unbounded side is a genuine ``(-inf, v)``
    or ``(v, +inf)``, never clipped to a non-negative domain the engine has
    no basis to assume holds (brief §1.1 — `<0.1` is `(-inf, 0.1)`, not
    `[0, 0.1)`).
    """
    if comparator is None:
        return _Interval(value, True, value, True)
    if comparator == "<":
        return _Interval(_NEG_INF, False, value, False)
    if comparator == "<=":
        return _Interval(_NEG_INF, False, value, True)
    if comparator == ">":
        return _Interval(value, False, _POS_INF, False)
    if comparator == ">=":
        return _Interval(value, True, _POS_INF, False)
    # Unreachable given the ADR-0030 CHECK on lab_results.comparator's domain.
    raise ValueError(f"unknown comparator: {comparator!r}")


def _target_interval(low: float | None, high: float | None) -> _Interval:
    """Build T = [L, H] from framework_ranges bounds (brief §1.2).

    Inclusive at both ends; NULL means unbounded (+/-inf), which the
    dataclass's own invariant then treats as open (see :class:`_Interval`).
    """
    lo = _NEG_INF if low is None else low
    hi = _POS_INF if high is None else high
    return _Interval(lo, math.isfinite(lo), hi, math.isfinite(hi))


# Bound-coincidence tolerance (ADR-0058 §3). Normalization is float
# arithmetic, so a result that is *exactly* a bound in one unit need not land
# exactly on it in another: 5.171967933798811 mmol/L is exactly 200 mg/dL, but
# bridging it through molar mass yields 200.00000000000003, and an exact `==`
# then flags `above` — the same physical quantity verdicted differently by the
# unit it happens to be reported in, which is precisely the class of silently
# wrong flag ADR-0005 exists to close, reintroduced by the arithmetic that
# closes it.
#
# So two magnitudes within a relative 1e-9 are treated as the same point. That
# is ~7 orders of magnitude above the ~1e-16 relative error a conversion
# actually introduces, and far below any clinically meaningful difference
# (2e-7 mg/dL at a cholesterol bound of 200). It matches the tolerance WI-1's
# property suite already uses for conversion round-trips (ADR-0056).
#
# `math.isclose` handles the infinities these intervals use: isclose(inf, inf)
# is True, isclose(inf, x) is False for finite x. No abs_tol is set (default
# 0.0) and none is needed: every conversion is a multiplication, so an exact
# zero converts to an exact zero and never has to be recognized across error.
_BOUND_REL_TOL = 1e-9


def _same_point(a: float, b: float) -> bool:
    """Whether two magnitudes name the same point, within conversion error."""
    return math.isclose(a, b, rel_tol=_BOUND_REL_TOL)


def _strictly_less(a: float, b: float) -> bool:
    """``a < b`` and not merely a float-error hair below it."""
    return a < b and not _same_point(a, b)


def _strictly_greater(a: float, b: float) -> bool:
    """``a > b`` and not merely a float-error hair above it."""
    return a > b and not _same_point(a, b)


def _low_within(inner: _Interval, outer: _Interval) -> bool:
    """Whether ``inner``'s low bound sits inside (or matches) ``outer``'s."""
    if _strictly_greater(inner.low, outer.low):
        return True
    return _same_point(inner.low, outer.low) and (
        outer.low_closed or not inner.low_closed
    )


def _high_within(inner: _Interval, outer: _Interval) -> bool:
    """Whether ``inner``'s high bound sits inside (or matches) ``outer``'s."""
    if _strictly_less(inner.high, outer.high):
        return True
    return _same_point(inner.high, outer.high) and (
        outer.high_closed or not inner.high_closed
    )


def _is_subset(inner: _Interval, outer: _Interval) -> bool:
    """R ⊆ T (brief §1.3 step 1)."""
    return _low_within(inner, outer) and _high_within(inner, outer)


def _lies_entirely_below(r: _Interval, t: _Interval) -> bool:
    """R ∩ T = ∅ with R's supremum at or before T's infimum (brief §1.3 step 2).

    The shared-boundary case (``r.high`` and ``t.low`` the same point) is
    disjoint only when the two sides do not both hold that point closed — this
    is exactly the `<0.5` vs `[0.5, +inf)` (`below`) vs `<=0.5` vs the same
    range (`indeterminate`) distinction the brief pins as load-bearing. The
    tolerance decides *whether the bounds coincide*; it never decides the
    open/closed question, so that distinction survives it untouched.
    """
    if _strictly_less(r.high, t.low):
        return True
    return _same_point(r.high, t.low) and not (r.high_closed and t.low_closed)


def _lies_entirely_above(r: _Interval, t: _Interval) -> bool:
    """R ∩ T = ∅ with T's supremum at or before R's infimum (brief §1.3 step 3)."""
    if _strictly_less(t.high, r.low):
        return True
    return _same_point(t.high, r.low) and not (t.high_closed and r.low_closed)


def _verdict(r: _Interval, t: _Interval) -> str:
    """The four-way, mutually exclusive, exhaustive classification (brief §1.3)."""
    if _is_subset(r, t):
        return "in_range"
    if _lies_entirely_below(r, t):
        return "below"
    if _lies_entirely_above(r, t):
        return "above"
    return "indeterminate"


# ---------------------------------------------------------------------------
# compare() — brief §1.5's five-step evaluation order
# ---------------------------------------------------------------------------


def _incomplete(
    framework: str,
    flag: str,
    reason: str | None,
    range_row: ResolvedRange | None,
) -> Comparison:
    """A Comparison for any flag other than the four interval verdicts.

    No normalized comparison happened, so range_low/range_high/unit carry
    nothing (there is nothing normalized to show); range_text/effective_date
    still travel through when a range row was resolved, so a client can
    render the target's own facts even when it could not be compared.
    """
    return Comparison(
        framework=framework,
        flag=flag,
        range_low=None,
        range_high=None,
        unit=None,
        effective_date=None if range_row is None else range_row.effective_date,
        range_text=None if range_row is None else range_row.range_text,
        reason=reason,
    )


def compare(
    *,
    framework: str,
    biomarker: str,
    canonical_unit: str | None,
    molar_mass: float | None,
    range_row: ResolvedRange | None,
    result: ResultValue,
) -> Comparison:
    """Evaluate one result against one point-in-time-resolved range.

    Runs the five pinned steps in order (brief §1.5), each short-circuiting:

    1. No range resolved at all -> ``no_range``.
    2. A ``range_text``-only target (no numeric bounds) -> ``not_comparable``,
       carrying ``range_text`` through.
    3. A qualitative result (``value_num IS NULL``, ADR-0030) -> ``not_comparable``.
       Step 2 precedes step 3 so a qualitative result against a text-only
       target reports the *target's* uncomparability, not the result's
       (brief §1.5 note — the answer is `not_comparable` either way; only the
       carried reason differs).
    4. Normalize both endpoints to ``canonical_unit`` (brief §3). Any failure
       to reconcile units is a loud ``error`` with a reason string — never a
       silent pass, never an exception escaping this function. The one
       exception (literally): a non-positive ``molar_mass`` raises bare
       ``ValueError`` from :func:`healthspan.units.convert` and is
       deliberately left uncaught here — the migration 0005 ``CHECK``
       guards against it existing in the database at all, so seeing one
       means that guard failed, a genuine bug rather than a data condition.
    5. Compare the normalized intervals (brief §1.3) -> ``in_range`` /
       ``below`` / ``above`` / ``indeterminate``.

    ``biomarker`` is a human-readable label (e.g. ``canonical_name``) used
    only to build the ``MissingMolarContextError`` reason string, per brief
    §3's "name the biomarker" requirement.
    """
    # Step 1.
    if range_row is None:
        return _incomplete(framework, "no_range", None, None)

    # Step 2.
    if range_row.range_low is None and range_row.range_high is None:
        return _incomplete(
            framework,
            "not_comparable",
            "range has no numeric bounds (range_text only)",
            range_row,
        )

    # Step 3.
    if result.value_num is None:
        return _incomplete(
            framework,
            "not_comparable",
            "result is qualitative and has no numeric value to compare",
            range_row,
        )

    # Step 4 — normalize. Each condition below is one of brief §3's named
    # error reasons; UnitError's subclasses are caught explicitly first
    # (MissingMolarContextError needs biomarker-naming ranges.py alone can
    # supply) and the base class second (its own message already names the
    # offending unit(s) — brief §3's "name the offending unit string" /
    # "name both units" requirements are exactly what UnknownUnitError and
    # IncommensurableUnitsError's messages already do).
    if canonical_unit is None:
        return _incomplete(
            framework,
            "error",
            "no canonical unit for this biomarker — cannot normalize (catalog gap)",
            range_row,
        )
    if result.unit is None:
        return _incomplete(
            framework, "error", "result has no unit — cannot normalize", range_row
        )
    try:
        value_norm = units.convert(
            result.value_num, result.unit, canonical_unit, molar_mass=molar_mass
        )
        low_norm = (
            None
            if range_row.range_low is None
            else units.convert(
                range_row.range_low,
                range_row.unit,
                canonical_unit,
                molar_mass=molar_mass,
            )
        )
        high_norm = (
            None
            if range_row.range_high is None
            else units.convert(
                range_row.range_high,
                range_row.unit,
                canonical_unit,
                molar_mass=molar_mass,
            )
        )
    except units.MissingMolarContextError:
        return _incomplete(
            framework,
            "error",
            f"{biomarker!r} has no molar_mass set, but reconciling units here "
            "is a mass ↔ substance conversion that requires one",
            range_row,
        )
    except units.UnitError as exc:
        return _incomplete(framework, "error", str(exc), range_row)

    # Step 5. Comparator survives normalization unchanged: every conversion
    # units.convert performs is a positive scalar factor (dimension-matched
    # conversion) or a positive-molar-mass bridge (mass <-> substance) —
    # never order-reversing — so `<` stays `<` and no "flip" is ever needed
    # (brief §3).
    r = _result_interval(value_norm, result.comparator)
    t = _target_interval(low_norm, high_norm)
    return Comparison(
        framework=framework,
        flag=_verdict(r, t),
        range_low=low_norm,
        range_high=high_norm,
        unit=canonical_unit,
        effective_date=range_row.effective_date,
        range_text=range_row.range_text,
        reason=None,
    )


# ---------------------------------------------------------------------------
# Point-in-time range resolution (brief §2) — ONE SQL query per page.
# ---------------------------------------------------------------------------

# Fully static SQL text: the variable-length input travels as a single bound
# JSON-array parameter consumed by json_each, not as interpolated SQL text —
# so a page of any size resolves in exactly one round trip without ever
# constructing the query string dynamically. ROW_NUMBER ranks each pair's
# candidate framework_ranges rows by the ADR-0005 §2 point-in-time rule: a
# dated row (effective_date <= draw_date) beats the dateless default, and
# among dated rows the greatest effective_date wins.
_RESOLVE_RANGES_SQL = """
    WITH pairs AS (
        SELECT
            CAST(json_extract(p.value, '$[0]') AS INTEGER) AS idx,
            CAST(json_extract(p.value, '$[1]') AS INTEGER) AS biomarker_id,
            json_extract(p.value, '$[2]') AS draw_date
        FROM json_each(?) AS p
    ),
    candidates AS (
        SELECT
            pairs.idx AS idx,
            fr.range_low AS range_low,
            fr.range_high AS range_high,
            fr.unit AS unit,
            fr.range_text AS range_text,
            fr.effective_date AS effective_date,
            (fr.effective_date IS NULL) AS is_default,
            ROW_NUMBER() OVER (
                PARTITION BY pairs.idx
                ORDER BY (fr.effective_date IS NULL) ASC, fr.effective_date DESC
            ) AS rn
        FROM pairs
        JOIN framework_ranges fr
            ON fr.framework_id = ?
           AND fr.biomarker_id = pairs.biomarker_id
           AND (fr.effective_date IS NULL OR fr.effective_date <= pairs.draw_date)
    )
    SELECT idx, range_low, range_high, unit, range_text, effective_date, is_default
    FROM candidates
    WHERE rn <= 2
    ORDER BY idx, rn
"""


def resolve_ranges(
    conn: sqlcipher3.Connection,
    framework_id: int,
    pairs: Sequence[tuple[int, str]],
) -> list[ResolvedRange | None]:
    """Point-in-time resolve the applicable range for each (biomarker_id, draw_date).

    ``pairs[i] = (biomarker_id, draw_date)`` where ``draw_date`` is the ISO
    date portion of the result's ``draw_utc`` (``substr(draw_utc, 1, 10)`` —
    the caller's job, per the ADR-0053 lexical-prefix convention this module
    reuses). Returns a list positionally aligned with ``pairs``; ``None`` at
    position *i* means no range exists for that pair at all (evaluation-order
    step 1 in :func:`compare` — ``no_range``).

    One SQL query regardless of ``len(pairs)`` — see :data:`_RESOLVE_RANGES_SQL`
    — so resolving a whole page of results never becomes an N+1 Python loop
    (brief §2).

    The schema's ``UNIQUE(framework_id, biomarker_id, effective_date)`` plus
    the partial ``ux_framework_ranges_default`` index guarantee at most one
    dated row per date and one dateless default per pair (ADR-0005 §2), so at
    most one row can ever legitimately win the point-in-time rank. This is
    checked here, not merely assumed: fetching the top two ranked candidates
    per pair and comparing their rank keys catches a genuine tie (only
    reachable if that schema guarantee were ever bypassed) and raises rather
    than silently picking one via ``ROW_NUMBER``'s arbitrary tie-break, which
    would hide the corruption instead of surfacing it.
    """
    if not pairs:
        return []

    payload = json.dumps(
        [
            [idx, biomarker_id, draw_date]
            for idx, (biomarker_id, draw_date) in enumerate(pairs)
        ]
    )
    cur = conn.execute(_RESOLVE_RANGES_SQL, (payload, framework_id))

    grouped: dict[int, list[tuple[Any, ...]]] = {}
    for row in cur.fetchall():
        grouped.setdefault(int(row[0]), []).append(tuple(row))

    results: list[ResolvedRange | None] = [None] * len(pairs)
    for idx, rows in grouped.items():
        winner = rows[0]  # rn=1, guaranteed first by "ORDER BY idx, rn"
        if len(rows) > 1:
            runner_up = rows[1]
            # Compare (is_default, effective_date) rank keys — index 6 and 5.
            if (winner[6], winner[5]) == (runner_up[6], runner_up[5]):
                raise RuntimeError(
                    "ambiguous point-in-time range resolution for "
                    f"framework_id={framework_id}, "
                    f"(biomarker_id, draw_date)={pairs[idx]!r}: two framework_ranges "
                    "rows tied for the point-in-time winner, which the schema's "
                    "UNIQUE(framework_id, biomarker_id, effective_date) constraint "
                    "and ux_framework_ranges_default partial index are supposed to "
                    "make impossible (ADR-0005 §2)"
                )
        results[idx] = ResolvedRange(
            range_low=winner[1],
            range_high=winner[2],
            unit=winner[3],
            range_text=winner[4],
            effective_date=winner[5],
        )
    return results


def resolve_framework_id(conn: sqlcipher3.Connection, name: str) -> int | None:
    """Case-insensitive ``range_frameworks.name`` -> ``id`` lookup.

    Mirrors the ``?category=`` case-insensitivity rule (ADR-0055 §1). Unlike
    that rule, an unresolved framework name is deliberately the read layer's
    problem to turn into a 422, not an empty-page fallback here (brief §6) —
    this function only reports "found" vs "not found"; the read enrichment
    pass owns the HTTP-status decision.
    """
    row = conn.execute(
        "SELECT id FROM range_frameworks WHERE name = ? COLLATE NOCASE", (name,)
    ).fetchone()
    return int(row[0]) if row is not None else None
