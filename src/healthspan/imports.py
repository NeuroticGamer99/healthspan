"""Bulk import engine: validation, conflict resolution, and audit (ADR-0004/0027/0052).

The single validated write path (ADR-0004). One import runs inside a single
``BEGIN IMMEDIATE`` transaction: it resolves and validates the whole batch
(collecting *every* error) while holding the write lock, then — only if clean —
applies the caller's explicit conflict policy against each row's natural key. A
validation failure rolls back, writing nothing; a dry-run rolls back the
identical apply, so its counts are truthful and it writes nothing.

Identity (ADR-0052): the payload is a per-table row map; a row's ``id`` is a
*batch-local handle* that wires intra-batch foreign keys (a ``lab_results``
row's ``lab_draw_id`` names a ``lab_draws`` row's payload ``id``), never
persistent identity. The server owns primary keys. Matching is by a defined
natural key per table, enforced as a partial-unique index over current rows
(migration 0003):

* ``lab_draws`` = ``(lab_id, draw_utc)`` — an identity/container row. A match
  is *reused* (its id resolves child FKs); a genuine metadata difference is an
  in-place ``update`` (ADR-0027 designated-metadata carve-out), never a
  supersession, so the draw id never moves and its results stay attached.
* ``lab_results`` = ``(lab_draw_id, biomarker_id)`` — a value row. A genuine
  difference *supersedes* (insert the corrected row, chain the old one, per-row
  ``correct`` audit with both images), so no clinical value is ever lost.

Catalog tables (``categories``, ``labs``, ``biomarkers``, ``biomarker_aliases``
— Phase 3 WI-2, ADR-0054/0055) have no ``superseded_by``/``import_batch_id``
columns: ``ImportableTable.has_supersession``/``has_provenance`` gate those
column families off, so a catalog table always reconciles a genuine
difference via in-place ``update`` (never supersession — there is no column
to supersede into). ``lab_results`` additionally accepts ``biomarker_name``
as an alternative to ``biomarker_id`` (ADR-0054 §4): resolved to an id via
:func:`resolve_biomarker_name` before natural-key/conflict handling runs, so
the rest of the pipeline only ever sees an id.
"""

import contextlib
import math
import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date

import sqlcipher3

from healthspan import audit
from healthspan.keyparams import utc_now_iso

# Conflict policies (ADR-0004): explicit and required, no silent default.
REJECT = "reject"
SKIP = "skip"
UPSERT = "upsert"
POLICIES = frozenset({REJECT, SKIP, UPSERT})

# Table classifications (ADR-0052): a value row supersedes on a genuine
# difference; an identity row reuses and repairs metadata in place.
CLASSIFICATION_VALUE = "value"
CLASSIFICATION_IDENTITY = "identity"

# Payload keys that are recognized but not data columns: the batch-local
# handle and the two server-owned provenance columns (ignored if supplied).
_RESERVED_KEYS = frozenset({"id", "import_batch_id", "superseded_by"})

_COMPARATORS = frozenset({"<", "<=", ">=", ">"})

# A date-only ISO-8601 calendar date. `framework_ranges.effective_date` must
# match this for ADR-0005's lexical point-in-time comparison to be sound
# (see _framework_range_errors).
_ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


@dataclass(frozen=True)
class ImportableTable:
    """A table the import endpoint may write, and how it is matched (ADR-0052)."""

    name: str
    natural_key: tuple[str, ...]
    columns: tuple[str, ...]  # client-supplyable data columns, in insert order
    defaults: Mapping[str, object]  # fill for an omitted non-key column
    classification: str
    parent_fk: tuple[str, str] | None  # (fk column, parent table) intra-batch handle
    external_fks: tuple[tuple[str, str], ...]  # (fk column, referenced table)
    requires_value_model: bool = False
    # Natural-key columns that may legitimately be NULL. Normally every key
    # column is required and matched with ``=``; a nullable key column is
    # matched with ``IS`` instead, because ``x = NULL`` is NULL in SQL and so
    # would never match a stored NULL — the row would be re-INSERTed on every
    # import and collide with its own unique index rather than reconcile.
    # ``framework_ranges.effective_date`` is the case this exists for: NULL
    # there means "always current" (ADR-0005), which is the *common* row, not
    # an edge case.
    nullable_key: tuple[str, ...] = ()
    # Catalog tables (ADR-0055/0054) carry neither column family: a
    # ``has_supersession=False`` table reconciles a genuine difference via
    # in-place ``update``, never ``_supersede`` (there is no column to park a
    # row out of currency into). ``has_provenance=False`` omits
    # ``import_batch_id`` from the INSERT.
    has_supersession: bool = True
    has_provenance: bool = True
    # Columns the server derives/sets at insert time (e.g. a ``created_utc``
    # timestamp): written on INSERT but excluded from ``compared``, so a
    # re-import of an otherwise-identical row is ``unchanged`` rather than a
    # spurious conflict, and an in-place reconcile leaves the stored value be.
    server_owned: tuple[str, ...] = ()

    @property
    def compared(self) -> tuple[str, ...]:
        """Non-key columns whose difference counts as a change (ADR-0052)."""
        return tuple(
            c
            for c in self.columns
            if c not in self.natural_key and c not in self.server_owned
        )


TABLES: dict[str, ImportableTable] = {
    "categories": ImportableTable(
        name="categories",
        natural_key=("name",),
        columns=("name", "description"),
        defaults={"description": None},
        classification=CLASSIFICATION_IDENTITY,
        parent_fk=None,
        external_fks=(),
        has_supersession=False,
        has_provenance=False,
    ),
    "labs": ImportableTable(
        name="labs",
        natural_key=("name",),
        columns=("name", "description"),
        defaults={"description": None},
        classification=CLASSIFICATION_IDENTITY,
        parent_fk=None,
        external_fks=(),
        has_supersession=False,
        has_provenance=False,
    ),
    "biomarkers": ImportableTable(
        name="biomarkers",
        natural_key=("canonical_name",),
        columns=(
            "canonical_name",
            "loinc_code",
            "canonical_unit",
            "category_id",
            "description",
        ),
        defaults={
            "loinc_code": None,
            "canonical_unit": None,
            "category_id": 0,
            "description": None,
        },
        classification=CLASSIFICATION_IDENTITY,
        parent_fk=None,
        external_fks=(("category_id", "categories"),),
        has_supersession=False,
        has_provenance=False,
    ),
    "biomarker_aliases": ImportableTable(
        name="biomarker_aliases",
        # The client supplies ``alias`` (+ ``biomarker_id``, ``source?``); the
        # server derives ``alias_normalized``/``created_utc`` (ADR-0054 §3),
        # see ``_derive_alias_row``. Natural-key matching is on the derived
        # ``alias_normalized``, never the client's raw spelling.
        natural_key=("alias_normalized",),
        columns=("biomarker_id", "alias", "alias_normalized", "source", "created_utc"),
        defaults={
            "biomarker_id": None,
            "alias": None,
            "source": None,
            "created_utc": None,
        },
        classification=CLASSIFICATION_IDENTITY,
        parent_fk=None,
        external_fks=(("biomarker_id", "biomarkers"),),
        has_supersession=False,
        has_provenance=False,
        # created_utc is stamped once at insert and preserved on reconcile —
        # never a compared column, or an identical re-import would churn.
        server_owned=("created_utc",),
    ),
    "range_frameworks": ImportableTable(
        name="range_frameworks",
        natural_key=("name",),
        columns=("name", "description", "source_url"),
        defaults={"description": None, "source_url": None},
        classification=CLASSIFICATION_IDENTITY,
        parent_fk=None,
        external_fks=(),
        has_supersession=False,
        has_provenance=False,
    ),
    "framework_ranges": ImportableTable(
        name="framework_ranges",
        # The ADR-0005 UNIQUE(framework_id, biomarker_id, effective_date), which
        # is also what makes point-in-time resolution provably single-valued.
        # ``effective_date`` is nullable: NULL is the "always current" dateless
        # default — the common row, not an edge case — so it is declared in
        # ``nullable_key`` rather than required. Without that, the default row
        # would be unimportable and only *dated* rows could be written, which is
        # backwards.
        natural_key=("framework_id", "biomarker_id", "effective_date"),
        nullable_key=("effective_date",),
        columns=(
            "framework_id",
            "biomarker_id",
            "range_low",
            "range_high",
            "unit",
            "range_text",
            "effective_date",
            "notes",
        ),
        defaults={
            "range_low": None,
            "range_high": None,
            "unit": None,
            "range_text": None,
            "effective_date": None,
            "notes": None,
        },
        classification=CLASSIFICATION_IDENTITY,
        parent_fk=None,
        # Both FKs resolve against already-stored rows, never same-batch
        # (ADR-0057 §9): a framework must land in a prior import before its
        # ranges can reference it.
        external_fks=(
            ("framework_id", "range_frameworks"),
            ("biomarker_id", "biomarkers"),
        ),
        has_supersession=False,
        has_provenance=False,
    ),
    "lab_draws": ImportableTable(
        name="lab_draws",
        natural_key=("lab_id", "draw_utc"),
        columns=(
            "lab_id",
            "draw_utc",
            "draw_local_recorded",
            "draw_local_tz",
            "draw_tz_inferred",
            "draw_context",
            "fasting",
            "notes",
        ),
        defaults={
            "draw_local_recorded": None,
            "draw_local_tz": None,
            "draw_tz_inferred": 0,
            "draw_context": None,
            "fasting": None,
            "notes": None,
        },
        classification=CLASSIFICATION_IDENTITY,
        parent_fk=None,
        external_fks=(("lab_id", "labs"),),
    ),
    "lab_results": ImportableTable(
        name="lab_results",
        natural_key=("lab_draw_id", "biomarker_id"),
        columns=(
            "lab_draw_id",
            "biomarker_id",
            "value_num",
            "comparator",
            "value_text",
            "unit",
            "reference_low",
            "reference_high",
            "reference_text",
            "notes",
        ),
        defaults={
            "value_num": None,
            "comparator": None,
            "value_text": None,
            "unit": None,
            "reference_low": None,
            "reference_high": None,
            "reference_text": None,
            "notes": None,
        },
        classification=CLASSIFICATION_VALUE,
        parent_fk=("lab_draw_id", "lab_draws"),
        external_fks=(("biomarker_id", "biomarkers"),),
        requires_value_model=True,
    ),
}

# The catalog tables, in dependency order: each is validated and applied by the
# generic identity path, and each precedes any table whose FK names it
# (`biomarkers` before `biomarker_aliases`/`framework_ranges`, `range_frameworks`
# before `framework_ranges`). `lab_draws`/`lab_results` are excluded here — they
# have bespoke orchestration for batch-local handles.
#
# This is the single source of truth for that set: the validation pass and
# IMPORT_ORDER both derive from it, so adding a catalog table is one edit rather
# than two lists to keep in agreement.
CATALOG_TABLES: tuple[str, ...] = (
    "categories",
    "labs",
    "biomarkers",
    "biomarker_aliases",
    "range_frameworks",
    "framework_ranges",
)

# Parent-before-child order (ADR-0052): a child's handles resolve to ids the
# parent pass has already assigned. Catalog tables precede the content
# tables that reference them by (real, already-stored) id.
IMPORT_ORDER: tuple[str, ...] = (*CATALOG_TABLES, "lab_draws", "lab_results")

Row = Mapping[str, object]


# --------------------------------------------------------------------------
# Name normalization + the biomarker_name resolver (ADR-0054 §2/§3) — the one
# place a display name ever turns into a biomarker id.
# --------------------------------------------------------------------------


def normalize_name(name: str) -> str:
    """The one normalization rule for biomarker display names (ADR-0054 §2).

    NFKC -> casefold -> strip -> collapse internal whitespace runs to one
    space. Used both to derive ``biomarker_aliases.alias_normalized`` at
    write time and to resolve a ``lab_results.biomarker_name`` at read-time
    of the import validation pass — the same function, so the two can never
    drift apart.
    """
    s = unicodedata.normalize("NFKC", name).casefold().strip()
    return " ".join(s.split())


def resolve_biomarker_name(conn: sqlcipher3.Connection, name: str) -> int:
    """Exact-match resolve a display name to its biomarker id (ADR-0054 §3).

    Matches ``normalize_name(name)`` against the *union* namespace of
    normalized ``biomarkers.canonical_name`` and stored
    ``biomarker_aliases.alias_normalized``. Zero or more-than-one match is a
    :class:`ValueError` naming the unresolved string — fail loud, no fuzzy
    matching anywhere on this path. ``canonical_name`` is not stored
    normalized (it is the display spelling), so it is normalized here, in
    Python, at resolve time; ``alias_normalized`` is already normalized
    (derived at write time), so it is compared directly.
    """
    normalized = normalize_name(name)
    matches: set[int] = set()
    for row_id, canonical_name in conn.execute(
        "SELECT id, canonical_name FROM biomarkers"
    ).fetchall():
        if normalize_name(str(canonical_name)) == normalized:
            matches.add(int(row_id))
    for (row_id,) in conn.execute(
        "SELECT biomarker_id FROM biomarker_aliases WHERE alias_normalized = ?",
        (normalized,),
    ).fetchall():
        matches.add(int(row_id))
    if len(matches) != 1:
        raise ValueError(
            f"biomarker_name {name!r} did not resolve to exactly one "
            f"biomarker ({len(matches)} matches)"
        )
    return matches.pop()


@dataclass(frozen=True)
class BatchMeta:
    """Provenance for one import batch (the ``import_batches`` row, ADR-0004)."""

    source: str
    adapter_id: str | None = None
    adapter_version: str | None = None
    note: str | None = None


@dataclass(frozen=True)
class RowError:
    """One collected validation failure (ADR-0004: all errors, then no write)."""

    table: str
    row_index: int  # 0-based within its table's list; -1 for a table-level error
    message: str


@dataclass(frozen=True)
class TableSummary:
    """Per-table reconciliation counts; the four partition every input row."""

    rows_inserted: int = 0
    rows_corrected: int = 0
    rows_skipped: int = 0
    rows_unchanged: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "rows_inserted": self.rows_inserted,
            "rows_corrected": self.rows_corrected,
            "rows_skipped": self.rows_skipped,
            "rows_unchanged": self.rows_unchanged,
        }


@dataclass(frozen=True)
class ImportOutcome:
    """The result of an import: the batch id (``None`` on dry-run) and counts."""

    batch_id: int | None
    dry_run: bool
    conflict_policy: str
    summaries: dict[str, TableSummary]


class ImportValidationError(Exception):
    """Full-batch validation failed; ``errors`` names every offending row."""

    def __init__(self, errors: list[RowError]) -> None:
        self.errors = errors
        super().__init__(f"{len(errors)} import validation error(s)")


@dataclass
class _Counts:
    inserted: int = 0
    corrected: int = 0
    skipped: int = 0
    unchanged: int = 0

    def summary(self) -> TableSummary:
        return TableSummary(
            rows_inserted=self.inserted,
            rows_corrected=self.corrected,
            rows_skipped=self.skipped,
            rows_unchanged=self.unchanged,
        )


# --------------------------------------------------------------------------
# Public entry
# --------------------------------------------------------------------------


def run_import(
    conn: sqlcipher3.Connection,
    *,
    batch: BatchMeta,
    payload: Mapping[str, Sequence[Row]],
    conflict_policy: str,
    actor: str | None,
    dry_run: bool,
) -> ImportOutcome:
    """Validate, then atomically apply (or dry-run) an import (ADR-0004/0052).

    The whole operation runs inside one ``BEGIN IMMEDIATE`` transaction:
    resolution and full-batch validation read *under the write lock*, then — if
    the batch validates — the apply writes. Taking the lock before validation
    (not just before apply) is what makes the cross-table alias/canonical
    uniqueness check (ADR-0054 §3) race-free: it has no schema constraint
    backing it, so two concurrent imports validating against stale state could
    otherwise each pass and then serialize a collision. A validation failure
    rolls the transaction back and raises :class:`ImportValidationError` with
    the full error list, writing nothing; a dry-run rolls back a clean apply
    while returning the same truthful counts.
    """
    if conflict_policy not in POLICIES:
        raise ImportValidationError(
            [RowError("", -1, f"unknown conflict_policy {conflict_policy!r}")]
        )

    conn.execute("BEGIN IMMEDIATE")
    try:
        resolved_payload, resolve_errors = _resolve_payload(conn, payload)
        errors = resolve_errors + _validate(conn, resolved_payload, conflict_policy)
        if errors:
            raise ImportValidationError(errors)
        batch_id, summaries = _apply(
            conn, batch, resolved_payload, conflict_policy, actor
        )
    except BaseException:
        _rollback(conn)
        raise
    if dry_run:
        conn.execute("ROLLBACK")
        return ImportOutcome(None, True, conflict_policy, summaries)
    conn.execute("COMMIT")
    return ImportOutcome(batch_id, False, conflict_policy, summaries)


def _rollback(conn: sqlcipher3.Connection) -> None:
    with contextlib.suppress(sqlcipher3.Error):
        conn.execute("ROLLBACK")


# --------------------------------------------------------------------------
# Payload resolution (ADR-0054 §3/§4): server-side row rewrites applied once,
# before validation and before apply, so both passes see the identical
# materialized rows — no re-derivation, no drift between the two.
# --------------------------------------------------------------------------


def _resolve_payload(
    conn: sqlcipher3.Connection, payload: Mapping[str, Sequence[Row]]
) -> tuple[dict[str, list[Row]], list[RowError]]:
    """Resolve ``lab_results.biomarker_name`` and derive alias columns.

    ``lab_results.biomarker_name`` is resolved to ``biomarker_id`` here
    (ADR-0054 §4), *before* ADR-0052 natural-key/conflict handling runs, so
    the rest of the pipeline only ever sees an id. ``biomarker_aliases``
    rows get their server-owned ``alias_normalized``/``created_utc``
    filled in here (ADR-0054 §3), so the cross-table uniqueness check in
    ``_validate`` — and the eventual INSERT — see the same derived values.
    Tables this phase does not touch pass through unchanged; unknown table
    names are left for ``_validate`` to report.
    """
    out: dict[str, list[Row]] = {name: list(rows) for name, rows in payload.items()}
    errors: list[RowError] = []

    results = out.get("lab_results")
    if results:
        resolved: list[Row] = []
        for i, raw in enumerate(results):
            row, row_errs = _resolve_lab_result_row(conn, raw)
            errors.extend(RowError("lab_results", i, m) for m in row_errs)
            resolved.append(row)
        out["lab_results"] = resolved

    aliases = out.get("biomarker_aliases")
    if aliases:
        out["biomarker_aliases"] = [_derive_alias_row(raw) for raw in aliases]

    return out, errors


def _resolve_lab_result_row(
    conn: sqlcipher3.Connection, raw: Row
) -> tuple[Row, list[str]]:
    """Resolve one ``lab_results`` row's ``biomarker_name`` to ``biomarker_id``.

    Exactly one of ``biomarker_id``/``biomarker_name`` is required (ADR-0054
    §4); both or neither is a collected error naming the row. A name that
    does not resolve (:func:`resolve_biomarker_name`) is likewise a
    collected error — the row is returned unchanged (still carrying
    ``biomarker_name``, still missing ``biomarker_id``) so downstream
    required-column/shape checks report consistently alongside it.
    """
    has_id = raw.get("biomarker_id") is not None
    has_name = raw.get("biomarker_name") is not None
    if has_id and has_name:
        return raw, [
            "lab_results row must supply exactly one of biomarker_id / "
            "biomarker_name (both given)"
        ]
    if not has_id and not has_name:
        return raw, [
            "lab_results row must supply exactly one of biomarker_id / "
            "biomarker_name (neither given)"
        ]
    if has_id:
        return raw, []
    name = raw["biomarker_name"]
    try:
        resolved_id = resolve_biomarker_name(conn, str(name))
    except ValueError as exc:
        return raw, [str(exc)]
    out = {k: v for k, v in raw.items() if k != "biomarker_name"}
    out["biomarker_id"] = resolved_id
    return out, []


def _derive_alias_row(raw: Row) -> Row:
    """Server-derive ``alias_normalized``/``created_utc`` (ADR-0054 §3).

    The client supplies ``alias`` (display text) plus ``biomarker_id`` and
    an optional ``source``; the server derives the normalized match key and
    the timestamp. Any client-supplied value for either is silently
    overwritten — the same "server-owned, never trusted" treatment as
    ``import_batch_id``/``superseded_by`` elsewhere in this module. A missing,
    non-string, or blank (whitespace-only) ``alias`` — one whose normalized
    form is empty — is left for ``_domain_errors`` to reject.
    """
    out = dict(raw)
    # alias_normalized is server-owned: never let a client-supplied value
    # survive, even on a blank alias (which validation then rejects).
    out.pop("alias_normalized", None)
    alias = raw.get("alias")
    if isinstance(alias, str):
        normalized = normalize_name(alias)
        if normalized:  # a blank/whitespace-only alias is rejected in validation
            out["alias_normalized"] = normalized
    out["created_utc"] = utc_now_iso()
    return out


# --------------------------------------------------------------------------
# Validation (reads only; collects every error before any write)
# --------------------------------------------------------------------------


def _validate(
    conn: sqlcipher3.Connection,
    payload: Mapping[str, Sequence[Row]],
    policy: str,
) -> list[RowError]:
    errors: list[RowError] = []
    for name in payload:
        if name not in TABLES:
            errors.append(
                RowError(
                    name,
                    -1,
                    f"unknown import table {name!r}; importable: {sorted(TABLES)}",
                )
            )

    for table in CATALOG_TABLES:
        rows = payload.get(table) or ()
        if rows:
            errors.extend(_validate_identity_table(conn, TABLES[table], rows, policy))
    errors.extend(_validate_alias_canonical_uniqueness(conn, payload))

    draws = payload.get("lab_draws") or ()
    results = payload.get("lab_results") or ()
    draw_spec = TABLES["lab_draws"]
    result_spec = TABLES["lab_results"]
    assert result_spec.parent_fk is not None  # noqa: S101 - registry invariant
    parent_col = result_spec.parent_fk[0]

    # handle -> existing current draw id (or None if the draw is new to the DB)
    handle_existing: dict[object, int | None] = {}
    handle_seen: set[object] = set()
    draw_keys_seen: set[tuple[object, ...]] = set()

    for i, raw in enumerate(draws):
        row_errs = _row_errors(conn, draw_spec, raw)
        handle = raw.get("id")
        if handle is not None:
            if handle in handle_seen:
                row_errs.append(f"duplicate batch-local id {handle!r} in lab_draws")
            handle_seen.add(handle)
        # `not row_errs` guards the same unhashable-key TypeError the catalog
        # path guards (see _validate_identity_table): a draw_utc sent as a JSON
        # list is already a collected error, and hashing it here would raise
        # past that into a 500.
        if not row_errs and all(raw.get(c) is not None for c in draw_spec.natural_key):
            key = tuple(raw.get(c) for c in draw_spec.natural_key)
            if key in draw_keys_seen:
                row_errs.append(f"duplicate natural key {key} in lab_draws")
            draw_keys_seen.add(key)
            existing = _find_id(conn, draw_spec, key)
            if handle is not None:
                handle_existing[handle] = existing
            if existing is not None and policy == REJECT:
                normalized = _normalize(draw_spec, raw)
                image = audit.row_image(conn, draw_spec.name, existing)
                if _differs(draw_spec, image, normalized):
                    row_errs.append(
                        f"conflict: a lab_draws row {key} already exists "
                        "(policy 'reject')"
                    )
        errors.extend(RowError("lab_draws", i, m) for m in row_errs)

    result_keys_seen: set[tuple[object, object]] = set()
    for i, raw in enumerate(results):
        row_errs = _row_errors(conn, result_spec, raw)
        handle = raw.get(parent_col)
        if handle is None:
            row_errs.append(
                f"missing {parent_col} (the batch-local id of a lab_draws row "
                "in this import)"
            )
        elif handle not in handle_seen:
            row_errs.append(
                f"{parent_col}={handle!r} matches no lab_draws row in this import"
            )
        biomarker = raw.get("biomarker_id")
        if handle is not None and biomarker is not None:
            within = (handle, biomarker)
            if within in result_keys_seen:
                row_errs.append(
                    f"duplicate ({parent_col}, biomarker_id)={within} in lab_results"
                )
            result_keys_seen.add(within)
            existing_parent = handle_existing.get(handle)
            if existing_parent is not None and policy == REJECT:
                key = (existing_parent, biomarker)
                existing = _find_id(conn, result_spec, key)
                if existing is not None:
                    normalized = _normalize(
                        result_spec, raw, resolved_parent=existing_parent
                    )
                    image = audit.row_image(conn, result_spec.name, existing)
                    if _differs(result_spec, image, normalized):
                        row_errs.append(
                            f"conflict: a lab_results row for biomarker "
                            f"{biomarker!r} in this draw already exists "
                            "(policy 'reject')"
                        )
        errors.extend(RowError("lab_results", i, m) for m in row_errs)

    return errors


def _validate_identity_table(
    conn: sqlcipher3.Connection,
    spec: ImportableTable,
    rows: Sequence[Row],
    policy: str,
) -> list[RowError]:
    """Generic identity-table validation for a catalog table.

    Shape/FK/domain errors (:func:`_row_errors`), an in-batch duplicate
    natural key, and — under ``reject`` — a genuine conflict against an
    already-stored row. Mirrors the ``lab_draws`` validation shape, minus
    the parent-handle wiring lab_draws/lab_results need: a batch cannot both
    create a catalog row and reference it by (real) id in the same call —
    catalog tables have no batch-local child dependents this phase, they are
    plain external foreign keys like any other already-stored row (mirrors
    how ``lab_results.biomarker_id`` already only ever names a stored row,
    never a batch handle).
    """
    errors: list[RowError] = []
    keys_seen: set[tuple[object, ...]] = set()
    for i, raw in enumerate(rows):
        row_errs = _row_errors(conn, spec, raw)
        # `not row_errs` first: a key component that already failed validation
        # must not reach `keys_seen`. An unhashable value (a JSON list or object
        # where a scalar belongs) raises TypeError from the set membership test
        # and escapes as a 500, losing the structured 422 this row's error was
        # already collected for. Pre-existing for every table — any column can
        # be sent as a list — and reachable through `framework_ranges` because
        # `effective_date` is exempt from the None check below.
        #
        # The rest of the guard asks "is the key complete enough to match on",
        # so a nullable key column is exempt: NULL there *is* the key value (the
        # ADR-0005 dateless default), not a missing one. Its absence from the
        # payload and an explicit null are the same key, which is correct —
        # both mean "always current".
        if not row_errs and all(
            raw.get(c) is not None
            for c in spec.natural_key
            if c not in spec.nullable_key
        ):
            key = tuple(raw.get(c) for c in spec.natural_key)
            if key in keys_seen:
                row_errs.append(f"duplicate natural key {key} in {spec.name}")
            keys_seen.add(key)
            existing = _find_id(conn, spec, key)
            if existing is not None and policy == REJECT:
                normalized = _normalize(spec, raw)
                image = audit.row_image(conn, spec.name, existing)
                if _differs(spec, image, normalized):
                    row_errs.append(
                        f"conflict: a {spec.name} row {key} already exists "
                        "(policy 'reject')"
                    )
        errors.extend(RowError(spec.name, i, m) for m in row_errs)
    return errors


def _validate_alias_canonical_uniqueness(
    conn: sqlcipher3.Connection, payload: Mapping[str, Sequence[Row]]
) -> list[RowError]:
    """Cross-table normalized-name uniqueness (ADR-0054 §3).

    A display name may not live as both a canonical biomarker name and an
    alias — in any combination of already-stored and in-this-batch rows.
    SQLite cannot express a uniqueness constraint spanning two tables, so it
    is enforced here, in validation, fail-loud, before any write:

    * an alias whose ``alias_normalized`` equals any biomarker's normalized
      ``canonical_name`` (stored or in-batch) is rejected as redundant or
      ambiguous — this also covers an alias equal to its own biomarker's
      exact canonical spelling, a strict subset of this rule;
    * a biomarker whose normalized ``canonical_name`` equals an existing
      alias's ``alias_normalized``, or a *different* biomarker's normalized
      ``canonical_name`` (stored or in-batch), is rejected. Re-submitting a
      biomarker's own unchanged exact spelling is the ordinary reconcile
      path (:func:`_validate_identity_table`), not a collision.

    Alias-vs-alias duplicates need no code here: ``alias_normalized`` is the
    ``biomarker_aliases`` natural key, so an exact duplicate is either an
    ordinary reconcile against a stored row or an in-batch duplicate caught
    by :func:`_validate_identity_table`.
    """
    errors: list[RowError] = []

    # normalized -> the exact stored spelling (assumed collision-free among
    # already-stored rows: this same check enforced that on every prior
    # write).
    canonical_existing: dict[str, str] = {
        normalize_name(str(name)): str(name)
        for _id, name in conn.execute(
            "SELECT id, canonical_name FROM biomarkers"
        ).fetchall()
    }
    alias_existing: set[str] = {
        str(row[0])
        for row in conn.execute(
            "SELECT alias_normalized FROM biomarker_aliases"
        ).fetchall()
    }

    biomarker_rows = payload.get("biomarkers") or ()
    alias_rows = payload.get("biomarker_aliases") or ()

    batch_canonical: dict[str, str] = {}
    for i, raw in enumerate(biomarker_rows):
        name = raw.get("canonical_name")
        if not isinstance(name, str):
            continue
        normalized = normalize_name(name)
        if normalized in alias_existing:
            errors.append(
                RowError(
                    "biomarkers",
                    i,
                    f"canonical_name {name!r} collides with an existing "
                    "biomarker_aliases entry (normalized)",
                )
            )
        existing_spelling = canonical_existing.get(normalized)
        if existing_spelling is not None and existing_spelling != name:
            errors.append(
                RowError(
                    "biomarkers",
                    i,
                    f"canonical_name {name!r} normalizes the same as the "
                    f"existing biomarker {existing_spelling!r}",
                )
            )
        prior_spelling = batch_canonical.get(normalized)
        if prior_spelling is not None and prior_spelling != name:
            errors.append(
                RowError(
                    "biomarkers",
                    i,
                    f"canonical_name {name!r} normalizes the same as "
                    f"another biomarker in this batch ({prior_spelling!r})",
                )
            )
        batch_canonical.setdefault(normalized, name)

    for i, raw in enumerate(alias_rows):
        alias_normalized = raw.get("alias_normalized")
        alias_text = raw.get("alias")
        if not isinstance(alias_normalized, str):
            continue
        if alias_normalized in canonical_existing:
            errors.append(
                RowError(
                    "biomarker_aliases",
                    i,
                    f"alias {alias_text!r} collides with an existing "
                    "biomarker canonical_name (normalized) — redundant or "
                    "ambiguous",
                )
            )
        if alias_normalized in batch_canonical:
            errors.append(
                RowError(
                    "biomarker_aliases",
                    i,
                    f"alias {alias_text!r} collides with a biomarker's "
                    "canonical_name in this batch (normalized)",
                )
            )

    return errors


def _row_errors(
    conn: sqlcipher3.Connection, spec: ImportableTable, raw: Row
) -> list[str]:
    """Shape, required-column, foreign-key, domain, and value-model errors."""
    errs: list[str] = []
    allowed = set(spec.columns) | _RESERVED_KEYS
    if spec.name == "lab_results":
        # Consumed by the biomarker_name resolver (ADR-0054 §4) before this
        # check ever sees it on the success path; recognized here too so a
        # row whose name failed to resolve doesn't also get an unrelated
        # "unknown column" error piled on.
        allowed = allowed | {"biomarker_name"}
    for key in raw:
        if key not in allowed:
            errs.append(f"unknown column {key!r} for {spec.name}")

    # Every supplied value must be a JSON scalar. Nothing else can be bound to
    # a SQLite parameter, and a natural-key column additionally gets hashed
    # into the duplicate-key set — where an unhashable list or dict raises
    # TypeError and escapes as a 500 instead of this collected 422. Checking
    # the type here rather than guarding the hash fixes the whole class at the
    # boundary: it covers every table and every column, including the ones no
    # per-table domain rule happens to inspect.
    for key, value in raw.items():
        if value is not None and not isinstance(value, str | int | float | bool):
            errs.append(
                f"{key} must be a string, number, boolean, or null for "
                f"{spec.name}, got {type(value).__name__}"
            )

    for col in spec.natural_key:
        if spec.parent_fk is not None and col == spec.parent_fk[0]:
            continue  # the handle's presence is checked in the child orchestration
        if col in spec.nullable_key:
            continue  # NULL is a legitimate key value here (see nullable_key)
        if raw.get(col) is None:
            errs.append(f"missing required column {col!r} for {spec.name}")

    for col, ref in spec.external_fks:
        value = raw.get(col)
        if value is not None and not _fk_exists(conn, ref, value):
            errs.append(f"{col}={value!r} does not exist in {ref}")

    errs.extend(_domain_errors(spec, raw))
    if spec.requires_value_model:
        errs.extend(_value_model_errors(raw))
    return errs


def _framework_range_errors(raw: Row) -> list[str]:
    """Domain rules for a ``framework_ranges`` row (ADR-0005, ADR-0058 §5).

    The two integrity CHECKs are mirrored here rather than left to the database
    so a bad row is one named validation error among the batch's others, not an
    opaque IntegrityError that aborts the whole import at apply time — the same
    reason the ADR-0030 value model is validated at this boundary despite also
    being CHECK-enforced.
    """
    errs: list[str] = []

    # `unit` is NOT NULL (ADR-0005's mandatory-unit safety correction) but is
    # not part of the natural key, so the generic required-column pass over
    # natural_key never sees it — require it explicitly, as biomarker_aliases
    # does for biomarker_id.
    if raw.get("unit") is None:
        errs.append(
            "missing required column 'unit' for framework_ranges: a numeric "
            "range with no unit is the safety bug ADR-0005 exists to close"
        )

    low, high = raw.get("range_low"), raw.get("range_high")
    if low is None and high is None and raw.get("range_text") is None:
        errs.append(
            "framework_ranges needs at least one of range_low, range_high, "
            "or range_text (ADR-0005)"
        )
    # Reject a non-finite or boolean bound before it can be stored. STRICT's
    # REAL affinity accepts both (`True` silently becomes 1.0), and the
    # `range_low <= range_high` CHECK cannot catch a NaN: `NaN <= x` is NULL,
    # and a CHECK fails only on FALSE. A stored NaN bound then compares
    # false against everything and flags `indeterminate` forever — a range
    # that silently never decides, which is the quiet-wrong-answer family
    # ADR-0005 exists to close. JSON has no NaN literal but Python's decoder
    # accepts one, so this is reachable from a real client, not just in-process.
    for name, bound in (("range_low", low), ("range_high", high)):
        if bound is None:
            continue
        if isinstance(bound, bool) or not isinstance(bound, int | float):
            errs.append(f"{name} must be a number, got {bound!r}")
        elif not math.isfinite(bound):
            errs.append(f"{name} must be a finite number, got {bound!r}")

    if (
        isinstance(low, int | float)
        and isinstance(high, int | float)
        and not isinstance(low, bool)
        and not isinstance(high, bool)
        and math.isfinite(low)
        and math.isfinite(high)
        and low > high
    ):
        errs.append(f"range_low ({low}) must be <= range_high ({high}) (ADR-0005)")

    # Point-in-time resolution compares `effective_date` lexically against the
    # date portion of a result's `draw_utc` (ADR-0005's rule, ranges.py). That
    # is only sound for a date-only value: '2024-06-01T00:00:00Z' would sort
    # *after* '2024-06-01' and so silently lose its own effective day, resolving
    # to the previous row or to none. Enforce the shape the rule assumes — and
    # the calendar behind it: '2025-02-30' matches the shape but is not a day
    # that exists, so it would sort into the gap before '2025-03-01' and act as
    # a date it does not name. `date.fromisoformat` is the calendar check;
    # the regex still runs first because fromisoformat also accepts forms this
    # rule must reject (it parses '20250101' and, on 3.11+, full timestamps).
    effective_date = raw.get("effective_date")
    if effective_date is not None:
        if (
            not isinstance(effective_date, str)
            or _ISO_DATE.fullmatch(effective_date) is None
        ):
            errs.append(
                f"effective_date must be an ISO-8601 date (YYYY-MM-DD), got "
                f"{effective_date!r}: point-in-time resolution compares it "
                "lexically against a date-only draw date (ADR-0005)"
            )
        else:
            try:
                date.fromisoformat(effective_date)
            except ValueError:
                errs.append(
                    f"effective_date {effective_date!r} is not a real calendar "
                    "date (ADR-0005)"
                )
    return errs


def _domain_errors(spec: ImportableTable, raw: Row) -> list[str]:
    errs: list[str] = []
    if spec.name == "lab_draws":
        inferred = raw.get("draw_tz_inferred")
        if inferred is not None and inferred not in (0, 1):
            errs.append("draw_tz_inferred must be 0 or 1")
        fasting = raw.get("fasting")
        if fasting is not None and fasting not in (0, 1):
            errs.append("fasting must be 0 or 1")
    elif spec.name == "lab_results":
        comparator = raw.get("comparator")
        if comparator is not None and comparator not in _COMPARATORS:
            errs.append("comparator must be one of <, <=, >=, > (ADR-0030)")
    elif spec.name == "framework_ranges":
        errs.extend(_framework_range_errors(raw))
    elif spec.name == "biomarker_aliases":
        # Not part of the natural key (that's the derived alias_normalized),
        # so the generic "missing required column" pass over natural_key
        # never sees these; require them explicitly.
        if raw.get("biomarker_id") is None:
            errs.append("missing required column 'biomarker_id' for biomarker_aliases")
        alias = raw.get("alias")
        # A whitespace-only alias is truthy but normalizes to "" — reject it
        # rather than persist an empty alias_normalized (ADR-0054 §2).
        if not isinstance(alias, str) or not normalize_name(alias):
            errs.append(
                "missing or blank required column 'alias' for biomarker_aliases"
            )
    return errs


def _value_model_errors(raw: Row) -> list[str]:
    errs: list[str] = []
    if raw.get("value_num") is None and raw.get("value_text") is None:
        errs.append("a result needs value_num or value_text (ADR-0030)")
    if raw.get("comparator") is not None and raw.get("value_num") is None:
        errs.append("comparator requires a value_num (ADR-0030)")
    return errs


# --------------------------------------------------------------------------
# Apply (writes; inside the caller's BEGIN IMMEDIATE)
# --------------------------------------------------------------------------


def _apply(
    conn: sqlcipher3.Connection,
    batch: BatchMeta,
    payload: Mapping[str, Sequence[Row]],
    policy: str,
    actor: str | None,
) -> tuple[int, dict[str, TableSummary]]:
    batch_id = _insert_batch(conn, batch)
    summaries: dict[str, TableSummary] = {}
    handle_real: dict[object, int] = {}

    for table in IMPORT_ORDER:
        rows = payload.get(table) or ()
        if not rows:
            continue
        spec = TABLES[table]
        counts = _apply_table(conn, spec, rows, policy, actor, batch_id, handle_real)
        summaries[table] = counts.summary()
        audit.record_import(
            conn,
            table_name=table,
            summary=_batch_summary(counts, policy, batch),
            actor=actor,
            import_batch_id=batch_id,
        )
    return batch_id, summaries


def _apply_table(
    conn: sqlcipher3.Connection,
    spec: ImportableTable,
    rows: Sequence[Row],
    policy: str,
    actor: str | None,
    batch_id: int,
    handle_real: dict[object, int],
) -> _Counts:
    counts = _Counts()
    for raw in rows:
        resolved_parent = _resolve_parent(spec, raw, handle_real)
        normalized = _normalize(spec, raw, resolved_parent=resolved_parent)
        key = tuple(normalized[c] for c in spec.natural_key)
        match = _find_id(conn, spec, key)
        if match is None:
            row_id = _insert_row(conn, spec, normalized, batch_id)
            counts.inserted += 1
        else:
            row_id = _reconcile(
                conn, spec, match, normalized, policy, actor, batch_id, counts
            )
        handle = raw.get("id")
        if handle is not None:
            handle_real[handle] = row_id
    return counts


def _reconcile(
    conn: sqlcipher3.Connection,
    spec: ImportableTable,
    existing_id: int,
    normalized: dict[str, object],
    policy: str,
    actor: str | None,
    batch_id: int,
    counts: _Counts,
) -> int:
    """A natural-key match: no-op, skip, supersede, or in-place repair.

    Returns the row id that now holds this key (the reused/updated existing id
    for identity rows, the new superseding id for a corrected value row) so a
    child's foreign key resolves to a current row.
    """
    image = audit.row_image(conn, spec.name, existing_id)
    if not _differs(spec, image, normalized):
        counts.unchanged += 1
        return existing_id
    if policy == SKIP:
        counts.skipped += 1
        return existing_id
    # UPSERT (REJECT was rejected during validation).
    counts.corrected += 1
    if spec.classification == CLASSIFICATION_VALUE:
        if not spec.has_supersession:
            # Registry invariant, not a user-reachable state: a value-row
            # table must carry the superseded_by column to supersede into.
            raise RuntimeError(
                f"{spec.name}: classification=value requires "
                "has_supersession=True (registry misconfiguration)"
            )
        return _supersede(conn, spec, existing_id, image, normalized, actor, batch_id)
    _update_in_place(conn, spec, existing_id, image, normalized, actor, batch_id)
    return existing_id


def _supersede(
    conn: sqlcipher3.Connection,
    spec: ImportableTable,
    old_id: int,
    old_image: audit.RowImage,
    normalized: dict[str, object],
    actor: str | None,
    batch_id: int,
) -> int:
    """Value correction: insert the new row, chain the old (ADR-0027).

    The partial-unique index (migration 0003) forbids two *current* rows on
    one natural key even transiently, so the new row is inserted already
    parked out of the index (``superseded_by`` = the old id), the old row is
    then chained to it, and only then is the new row released to current — no
    instant has two current rows for the key.
    """
    new_id = _insert_row(conn, spec, normalized, batch_id, superseded_by=old_id)
    conn.execute(
        f"UPDATE {spec.name} SET superseded_by = ? WHERE id = ?",  # noqa: S608 - table from registry
        (new_id, old_id),
    )
    conn.execute(
        f"UPDATE {spec.name} SET superseded_by = NULL WHERE id = ?",  # noqa: S608
        (new_id,),
    )
    new_image = audit.row_image(conn, spec.name, new_id)
    audit.record_correct(
        conn,
        table_name=spec.name,
        row_id=new_id,
        old_image=old_image,
        new_image=new_image,
        actor=actor,
        import_batch_id=batch_id,
        reason=f"upsert re-import, batch {batch_id}",
    )
    return new_id


def _update_in_place(
    conn: sqlcipher3.Connection,
    spec: ImportableTable,
    row_id: int,
    old_image: audit.RowImage,
    normalized: dict[str, object],
    actor: str | None,
    batch_id: int,
) -> None:
    """Designated-metadata repair on an identity row: in-place ``update`` (ADR-0027)."""
    assignments = ", ".join(f"{c} = ?" for c in spec.compared)
    conn.execute(
        f"UPDATE {spec.name} SET {assignments} WHERE id = ?",  # noqa: S608 - registry
        (*(normalized[c] for c in spec.compared), row_id),
    )
    new_image = audit.row_image(conn, spec.name, row_id)
    audit.record_update(
        conn,
        table_name=spec.name,
        row_id=row_id,
        old_image=old_image,
        new_image=new_image,
        actor=actor,
        import_batch_id=batch_id,
    )


# --------------------------------------------------------------------------
# Row primitives
# --------------------------------------------------------------------------


def _resolve_parent(
    spec: ImportableTable, raw: Row, handle_real: dict[object, int]
) -> int | None:
    if spec.parent_fk is None:
        return None
    handle = raw.get(spec.parent_fk[0])
    # Validation guaranteed the handle names an in-batch parent already applied.
    return handle_real[handle]


def _normalize(
    spec: ImportableTable, raw: Row, resolved_parent: int | None = None
) -> dict[str, object]:
    """A full data-column image: supplied values, defaults for omitted ones.

    The parent foreign key (a handle in the payload) is replaced by the
    resolved real parent id, so the natural key and the insert use identity,
    never the batch-local handle (ADR-0052).
    """
    out: dict[str, object] = {}
    for col in spec.columns:
        if (
            spec.parent_fk is not None
            and col == spec.parent_fk[0]
            and resolved_parent is not None
        ):
            out[col] = resolved_parent
        elif col in raw:
            out[col] = raw[col]
        else:
            out[col] = spec.defaults[col]
    return out


def _insert_row(
    conn: sqlcipher3.Connection,
    spec: ImportableTable,
    normalized: Mapping[str, object],
    batch_id: int,
    superseded_by: int | None = None,
) -> int:
    columns: list[str] = list(spec.columns)
    values: list[object] = [normalized[c] for c in spec.columns]
    if spec.has_provenance:
        columns.append("import_batch_id")
        values.append(batch_id)
    if spec.has_supersession:
        columns.append("superseded_by")
        values.append(superseded_by)
    placeholders = ", ".join("?" for _ in columns)
    cursor = conn.execute(
        f"INSERT INTO {spec.name} ({', '.join(columns)}) "  # noqa: S608 - registry
        f"VALUES ({placeholders})",
        values,
    )
    return _last_id(cursor)


def _insert_batch(conn: sqlcipher3.Connection, batch: BatchMeta) -> int:
    cursor = conn.execute(
        "INSERT INTO import_batches (source, adapter_id, adapter_version, "
        "created_utc, note) VALUES (?, ?, ?, ?, ?)",
        (
            batch.source,
            batch.adapter_id,
            batch.adapter_version,
            utc_now_iso(),
            batch.note,
        ),
    )
    return _last_id(cursor)


def _last_id(cursor: sqlcipher3.Cursor) -> int:
    row_id = cursor.lastrowid
    if row_id is None:  # pragma: no cover - INSERT always assigns a rowid
        raise RuntimeError("INSERT did not assign a rowid")
    return int(row_id)


def _find_id(
    conn: sqlcipher3.Connection, spec: ImportableTable, key: tuple[object, ...]
) -> int | None:
    # A nullable key column is matched with `IS`, not `=`: SQL's `x = NULL` is
    # NULL (never true), so an `=` match would miss a stored NULL every time and
    # the row would be re-INSERTed into its own unique index. `IS` is NULL-safe
    # equality in SQLite and behaves identically to `=` for non-NULL operands
    # (and is likewise indexable), so this is exact for both cases.
    where = " AND ".join(
        f"{c} IS ?" if c in spec.nullable_key else f"{c} = ?" for c in spec.natural_key
    )
    sql = f"SELECT id FROM {spec.name} WHERE {where}"  # noqa: S608 - registry
    if spec.has_supersession:
        sql += " AND superseded_by IS NULL"
    row = conn.execute(sql, key).fetchone()
    return None if row is None else int(row[0])


def _fk_exists(conn: sqlcipher3.Connection, table: str, value: object) -> bool:
    row = conn.execute(
        f"SELECT 1 FROM {table} WHERE id = ?",  # noqa: S608 - table from registry
        (value,),
    ).fetchone()
    return row is not None


def _differs(
    spec: ImportableTable, image: audit.RowImage, normalized: Mapping[str, object]
) -> bool:
    """Whether any compared column differs between stored and incoming rows."""
    return any(image[c] != normalized[c] for c in spec.compared)


def _batch_summary(counts: _Counts, policy: str, batch: BatchMeta) -> audit.RowImage:
    """The ``import`` audit row's summary JSON (ADR-0027 batch-level audit)."""
    return {
        "rows_inserted": counts.inserted,
        "rows_corrected": counts.corrected,
        "rows_skipped": counts.skipped,
        "rows_unchanged": counts.unchanged,
        "conflict_policy": policy,
        "source": batch.source,
        "adapter_id": batch.adapter_id,
        "adapter_version": batch.adapter_version,
    }
