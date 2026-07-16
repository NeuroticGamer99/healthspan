"""Internal units module — UCUM parsing and canonical-unit normalization (ADR-0031).

Wraps ``ucumvert`` (UCUM 2.2 grammar → ``pint``) behind a small, engine-agnostic
API. This module is the *only* code that imports ``ucumvert``/``pint``: everything
downstream depends on canonical-unit normalization, not on which engine performs
it, so the engine stays swappable behind this surface (ADR-0031).

Every conversion either returns a value in the requested unit or raises. A units
mismatch never silently produces a number — that silent path is exactly the
mg/dL-vs-g/L safety bug ADR-0031 exists to close.

Mass-concentration ↔ substance-concentration conversions (mg/dL ↔ mmol/L) are not
pure scalar factors: they need the biomarker's molar mass as explicit context.
Attempted without it they fail loudly (:class:`MissingMolarContextError`), never
fall back to a scalar factor.

The engine is one-directional (UCUM → ``pint``), which is all normalization needs:
both endpoints of a conversion are UCUM strings the system was given and stored, so
we never ask "what UCUM string names this ``pint`` quantity?" (ADR-0031).
"""

from functools import lru_cache
from typing import Any


class UnitError(Exception):
    """Base for every units-module failure.

    Callers normalize before comparing (ADR-0005/ADR-0031); catching this base
    lets them treat any unreconcilable-units condition as the loud failure the
    unit-normalization requirement demands.
    """


class UnknownUnitError(UnitError):
    """A unit string the engine cannot parse as UCUM."""


class IncommensurableUnitsError(UnitError):
    """Two units whose dimensions cannot be reconciled, even with molar context."""


class MissingMolarContextError(UnitError):
    """A mass ↔ substance conversion attempted without the required molar mass."""


# Grams per mole: the molar-mass unit. Parsing it through the engine (rather than
# constructing a Quantity directly) guarantees it shares the registry of the
# conversion endpoints — pint refuses arithmetic across registries.
_MOLAR_MASS_UCUM = "g/mol"


@lru_cache(maxsize=1)
def _registry() -> Any:
    """The UCUM→pint registry, built once and cached.

    Constructed lazily so importing this module costs nothing until a conversion
    is actually needed (building the UCUM grammar is not free).
    """
    from ucumvert import PintUcumRegistry

    return PintUcumRegistry()


def parse_unit(unit: str) -> Any:
    """Parse a UCUM string into a ``pint`` quantity of magnitude ``1.0``.

    The returned quantity is the engine's own type, deliberately kept opaque
    (typed ``Any``) so the ``pint``/``ucumvert`` types never become part of this
    module's contract — downstream code goes through :func:`convert` and never
    depends on which engine runs (ADR-0031). Its registry is the shared one every
    other parse in this module uses, so quantities from separate calls are
    arithmetic-compatible.

    Raises :class:`UnknownUnitError` if ``unit`` is not valid UCUM.
    """
    from ucumvert import InvalidUcumError

    try:
        return _registry().from_ucum(unit)
    except InvalidUcumError as exc:
        raise UnknownUnitError(f"not a valid UCUM unit: {unit!r}") from exc


def is_valid_unit(unit: str) -> bool:
    """Whether ``unit`` parses as UCUM. A thin wrapper over :func:`parse_unit`."""
    try:
        parse_unit(unit)
    except UnknownUnitError:
        return False
    return True


def convert(
    value: float,
    from_unit: str,
    to_unit: str,
    *,
    molar_mass: float | None = None,
) -> float:
    """Convert ``value`` from ``from_unit`` to ``to_unit``, both UCUM strings.

    ``molar_mass`` is the biomarker's molar mass in grams per mole. It is required
    only when the two units differ by a mass ↔ substance factor (mg/dL ↔ mmol/L);
    for a same-dimension conversion it is ignored.

    - Same unit string → the value is returned unchanged (exact identity; no
      float round-trip is introduced).
    - Same dimensionality → a scalar conversion through ``pint``.
    - Mass ↔ substance concentration → bridged by ``molar_mass``; without it,
      :class:`MissingMolarContextError`.
    - Anything else → :class:`IncommensurableUnitsError`.

    Raises :class:`UnknownUnitError` for an unparseable unit and ``ValueError`` for
    a non-positive ``molar_mass``.
    """
    if from_unit == to_unit:
        return float(value)

    q_from = parse_unit(from_unit)
    q_to = parse_unit(to_unit)
    source = value * q_from

    if q_from.dimensionality == q_to.dimensionality:
        return float(source.to(q_to.units).magnitude)

    # Dimensions differ. The only difference this module reconciles is a molar
    # one: mass concentration vs substance concentration, bridged by molar mass.
    molar_mass_dim = parse_unit(_MOLAR_MASS_UCUM).dimensionality
    ratio = q_from.dimensionality / q_to.dimensionality
    if ratio == molar_mass_dim:
        # from_unit is heavier by [mass]/[substance]: divide out the molar mass.
        divide = True
    elif ratio == molar_mass_dim**-1:
        # from_unit is lighter by [substance]/[mass]: multiply in the molar mass.
        divide = False
    else:
        raise IncommensurableUnitsError(
            f"cannot reconcile {from_unit!r} and {to_unit!r}: "
            "their dimensions differ by more than a molar-mass factor"
        )

    if molar_mass is None:
        raise MissingMolarContextError(
            f"converting {from_unit!r} to {to_unit!r} is a molar conversion and "
            "requires the biomarker's molar mass (g/mol)"
        )
    if molar_mass <= 0:
        raise ValueError(f"molar mass must be positive, got {molar_mass!r}")

    molar_quantity = molar_mass * parse_unit(_MOLAR_MASS_UCUM)
    bridged = source / molar_quantity if divide else source * molar_quantity
    return float(bridged.to(q_to.units).magnitude)
