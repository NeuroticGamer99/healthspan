"""Synthetic fixture loader and canary-manifest derivation (Phase 1 WI-3b).

testing-strategy.md ("Synthetic Test Data") gives the fixture loader two jobs:

1. Create an ephemeral SQLCipher database, apply all migrations, and load the
   committed synthetic fixtures into it (``create_loaded_database`` /
   ``load_fixtures``).
2. Derive the *canary manifest* -- the complete list of synthetic health
   values present in the fixtures -- programmatically from the parsed typed
   records, so there is no hand-maintained list to drift (``build_manifest``).

The log canary gate (``scripts/scan_log_canary.py``) consumes the manifest this
module derives; the interim raw-text regex derivation the gate shipped with in
Phase 0 is retired. Deriving from parsed records rather than raw file text is
the point: a value reachable only through JSON parsing or numeric
normalization is caught here but was invisible to the regex
(open-questions.md, Testing -- resolved by this loader).

Fixtures are JSON only (testing-strategy.md, narrowed from the earlier "JSON or
SQL": raw SQL cannot be parsed back into the typed records the manifest
derivation depends on). Each file is an object mapping table name -> list of
row objects; files merge, and rows within a table load in file order. The
health-bearing columns are declared in ``CANARY_FIELDS``; every synthetic
health value must be grep-distinctive per the canary rule -- text fields embed
a ``CANARY-`` marker token, numeric fields use a high-entropy decimal (>= 6
significant digits) -- and ``build_manifest`` enforces the numeric half.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    import sqlcipher3

    from healthspan.kdf import DbKey

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"
FIXTURE_SUFFIX = ".json"
# Incidental OS/VCS metadata that tools drop into any directory; tolerated
# (skipped) rather than failing the canary gate, while a genuinely unexpected
# file still fails loudly (no silent skip of a real fixture).
_IGNORED_FILENAMES = frozenset(
    {".ds_store", "thumbs.db", "desktop.ini", ".gitkeep", ".gitignore"}
)

# Embedded canary tokens in text health fields. Same shape the scanner anchors
# on: interior dots allowed, never leading/trailing (so a sentence-final
# period is not captured but a dotted token survives).
CANARY_TOKEN = re.compile(r"CANARY-[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)*")
_DECIMAL_SHAPE = re.compile(r"\d+\.\d+")
# Column identifiers are only ever taken from trusted committed fixtures; this
# gate makes that explicit so the parameterized-insert builder below is airtight.
_IDENTIFIER = re.compile(r"[a-z_][a-z0-9_]*")

TEXT = "text"
DECIMAL = "decimal"

# Grep-distinctive owner-health columns, by table. ``TEXT`` -> contribute
# embedded CANARY- tokens; ``DECIMAL`` -> a numeric health value, required to be
# high-entropy. This registry is the complete map of owner-health columns that
# CAN be made grep-distinctive; a column omitted here is one of:
#   * catalog/vocabulary/reference/provenance/timestamp data -- not the owner's
#     own health values (biomarker names, framework ranges, import notes, ...);
#   * an INTEGER-typed metric that the canary rule cannot make distinctive
#     (wearable_daily.steps / resting_heart_rate / sleep_minutes / ... collide
#     with ports, offsets, and status codes -- see testing-strategy.md, "the
#     gate is only as strong as the canary rule"). These are excluded by
#     construction, not by oversight: the rule admits only CANARY- tokens and
#     high-entropy decimals, and an integer count is neither.
# Every table's free-text ``notes``/``description``/``body`` and every REAL
# health metric IS registered, so a new fixture value in a covered column
# cannot silently escape the manifest.
CANARY_FIELDS: dict[str, dict[str, str]] = {
    "lab_draws": {"notes": TEXT},
    "lab_results": {"value_num": DECIMAL, "value_text": TEXT, "notes": TEXT},
    "cgm_readings": {"glucose_mg_dl": DECIMAL},
    "body_composition": {
        "weight_kg": DECIMAL,
        "body_fat_pct": DECIMAL,
        "skeletal_muscle_mass_kg": DECIMAL,
        "total_body_water_kg": DECIMAL,
        "phase_angle_deg": DECIMAL,
        "ecw_tbw_ratio": DECIMAL,
        "intracellular_water_kg": DECIMAL,
        "extracellular_water_kg": DECIMAL,
        "visceral_fat_area_cm2": DECIMAL,
        "notes": TEXT,
    },
    # wearable_daily's steps/heart-rate/sleep metrics are INTEGER and cannot be
    # grep-distinctive (see the note above); only its free-text notes qualify.
    "wearable_daily": {"notes": TEXT},
    "events": {"title": TEXT, "description": TEXT, "notes": TEXT},
    "interventions": {"name": TEXT, "notes": TEXT},
    "intervention_dose_history": {"dose": DECIMAL, "notes": TEXT},
    "clinical_documents": {"body": TEXT, "notes": TEXT},
    "subjective_observations": {"body": TEXT, "notes": TEXT},
    "analyses": {"title": TEXT, "body": TEXT, "result_data": TEXT, "notes": TEXT},
}

# FK-safe insertion order: parents before children, content tables before the
# junction tables that reference them. Every table a fixture may target must
# appear here (and match migration 0001).
TABLE_ORDER: tuple[str, ...] = (
    "import_batches",
    "jobs",
    "biomarkers",
    "labs",
    "range_frameworks",
    "framework_ranges",
    "lab_draws",
    "lab_results",
    "body_composition",
    "cgm_readings",
    "wearable_daily",
    "events",
    "interventions",
    "intervention_dose_history",
    "clinical_documents",
    "subjective_observations",
    "analyses",
    "document_lab_draws",
    "document_events",
    "document_interventions",
    "observation_interventions",
    "observation_events",
    "analysis_lab_draws",
    "analysis_documents",
    "analysis_interventions",
    "analysis_observations",
)
_TABLE_SET = frozenset(TABLE_ORDER)

Row = dict[str, Any]
Fixtures = dict[str, list[Row]]


class FixtureError(Exception):
    """A fixture file or record violates the loader's contract."""


def _significant_digits(literal: str) -> int:
    # Leading and trailing zeros carry no entropy: 100.000 has one significant
    # digit and must not pass as grep-distinctive.
    return len(literal.replace(".", "").strip("0"))


def parse_fixtures(fixtures_dir: Path | None = None) -> Fixtures:
    """Parse every fixture file into ``table -> rows``, merged across files.

    Raises ``FixtureError`` for a non-JSON file, malformed JSON, an unknown
    table name, or a structurally wrong shape -- silently skipping any of these
    would under-load the database or under-derive the canary manifest. A
    missing fixtures directory yields an empty mapping (a trivially green gate).
    """
    fixtures_dir = FIXTURES_DIR if fixtures_dir is None else fixtures_dir
    merged: Fixtures = {}
    if not fixtures_dir.is_dir():
        return merged
    for path in sorted(fixtures_dir.rglob("*")):
        if not path.is_file():
            continue
        if path.name.lower() in _IGNORED_FILENAMES:
            continue  # incidental OS/VCS metadata, never a fixture
        if path.suffix != FIXTURE_SUFFIX:
            msg = (
                f"unexpected non-fixture file under {fixtures_dir}: {path} "
                f"(fixtures are JSON only)"
            )
            raise FixtureError(msg)
        _merge_file(path, merged)
    return merged


def _merge_file(path: Path, merged: Fixtures) -> None:
    try:
        raw: Any = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        msg = f"invalid JSON in fixture {path}: {exc}"
        raise FixtureError(msg) from exc
    if not isinstance(raw, dict):
        msg = f"fixture {path} must be an object mapping table -> list of rows"
        raise FixtureError(msg)
    data = cast("dict[str, Any]", raw)
    for table, rows in data.items():
        if table not in _TABLE_SET:
            msg = f"fixture {path} references unknown table {table!r}"
            raise FixtureError(msg)
        if not isinstance(rows, list):
            msg = f"fixture {path}: table {table!r} must map to a list of rows"
            raise FixtureError(msg)
        bucket = merged.setdefault(table, [])
        for row in rows:  # pyright: ignore[reportUnknownVariableType] - json Any
            if not isinstance(row, dict):
                msg = f"fixture {path}: each {table!r} row must be an object"
                raise FixtureError(msg)
            bucket.append(row)  # pyright: ignore[reportUnknownArgumentType] - guarded


def build_manifest(fixtures_dir: Path | None = None) -> set[str]:
    """Derive the canary manifest from the parsed fixture records.

    Text health fields contribute their embedded ``CANARY-`` tokens; numeric
    health fields contribute their value, required to be a high-entropy decimal
    (>= 6 significant digits) so the log canary gate can see it. A numeric
    health value that is not grep-distinctive fails loudly here -- mechanizing
    the fixture-review half of the canary rule.
    """
    fixtures = parse_fixtures(fixtures_dir)
    manifest: set[str] = set()
    for table, fields in CANARY_FIELDS.items():
        for row in fixtures.get(table, []):
            for column, kind in fields.items():
                value = row.get(column)
                if value is None:
                    continue
                if kind == TEXT:
                    manifest.update(CANARY_TOKEN.findall(str(value)))
                else:
                    manifest.add(_canary_decimal(table, column, value))
    return manifest


def _canary_decimal(table: str, column: str, value: Any) -> str:
    # bool is an int subclass; a boolean in a numeric column is a fixture bug.
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        msg = (
            f"{table}.{column} is a numeric health field but fixture value "
            f"{value!r} is not a number"
        )
        raise FixtureError(msg)
    literal = str(value)
    if _DECIMAL_SHAPE.fullmatch(literal) is None or _significant_digits(literal) < 6:
        msg = (
            f"{table}.{column} health value {literal} is not grep-distinctive: "
            f"numeric health values need a high-entropy decimal with at least "
            f"six significant digits (canary rule, testing-strategy.md)"
        )
        raise FixtureError(msg)
    return literal


def load_fixtures(conn: sqlcipher3.Connection, fixtures: Fixtures) -> None:
    """Insert parsed fixture rows into an already-migrated database.

    Tables load in FK-safe order (``TABLE_ORDER``) so the runtime connection's
    enforced foreign keys are satisfied; rows within a table load in file
    order. Table and column identifiers come only from ``TABLE_ORDER`` and the
    keys of trusted committed fixtures (validated below) -- never external
    input -- and every value is bound as a parameter.
    """
    for table in TABLE_ORDER:
        for row in fixtures.get(table, []):
            columns = list(row.keys())
            for column in columns:
                if _IDENTIFIER.fullmatch(column) is None:
                    msg = f"illegal column identifier {column!r} in {table!r}"
                    raise FixtureError(msg)
            collist = ", ".join(columns)
            placeholders = ", ".join("?" for _ in columns)
            # Identifiers validated above; values are bound, not interpolated.
            sql = f"INSERT INTO {table} ({collist}) VALUES ({placeholders})"  # noqa: S608
            conn.execute(sql, [row[column] for column in columns])
    conn.commit()


def create_loaded_database(
    path: Path, key: DbKey, fixtures_dir: Path | None = None
) -> sqlcipher3.Connection:
    """Provision, migrate, and load fixtures into a fresh SQLCipher database.

    Returns an open runtime connection (foreign keys enforced); the caller owns
    closing it. Encryption is always on -- the same SQLCipher path as
    production (testing-strategy.md).
    """
    # Imported lazily so ``build_manifest`` (which the canary scanner calls with
    # no database) stays free of the sqlcipher/db import chain.
    from healthspan import db, migrate

    db.provision(path, key)
    migrate.migrate_database(path, key)
    fixtures = parse_fixtures(fixtures_dir)
    conn = db.connect(path, key)
    load_fixtures(conn, fixtures)
    return conn
