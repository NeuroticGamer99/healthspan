"""Manual data-entry and readback CLI (ADR-0059, Phase 3 WI-4).

The platform's first interactive data-entry surface. ``healthspan enter`` walks
a **draw-level template** — enter the lab and draw date once, then each result
against that draw — submits the batch through the single validated write path
(``POST /v1/import``, ADR-0004/0052), and reads the entered results back
range-flagged (``?framework=``, ADR-0058). The ``results``/``draws``/
``biomarkers``/``labs``/``frameworks`` groups are thin read clients over the
ADR-0053/0058 GET routes, so "range-flagged, queryable" is demonstrable from
the CLI itself.

Each command opens **one** authenticated session (:class:`_Api`) — config read
once, keyring read once, one keep-alive client — and threads it through every
request it makes (``enter`` makes ~8-10). The Core Service must be running and
the CLI authenticates as the ``[cli] token_name`` token (default ``cli-admin``,
``--token-name`` overrides), which needs only ``import`` + ``read``. Nothing
here touches the database directly.

Biomarker resolution mirrors the server rule (:func:`imports.resolve_biomarker_name`)
against the canonical + alias namespace; a name that does not resolve triggers
an **interactive confirm-and-record** flow (ADR-0059 §3) — search, pick, and
optionally record the typed string as an alias so it resolves next time — never
a fuzzy auto-match. A confirmed alias is recorded only **after** the draw
commits (aborting the draw records nothing) and immediately resolves for the
rest of the session; the result always carries the picked biomarker's real id,
so the entry never depends on a same-batch alias (ADR-0057 §9).
"""

import json
import math
import re
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Annotated, Any, cast

import httpx
import typer

from healthspan import api_import, api_read, cli_client, imports, reads
from healthspan.cli_support import fail, load_config_or_exit
from healthspan.config import Config

results_app = typer.Typer(
    help="Read entered lab results (range-flagged with --framework).",
    no_args_is_help=True,
)
draws_app = typer.Typer(help="Read lab draws.", no_args_is_help=True)
biomarkers_app = typer.Typer(help="Browse the biomarker catalog.", no_args_is_help=True)
labs_app = typer.Typer(help="Browse lab sources.", no_args_is_help=True)
frameworks_app = typer.Typer(
    help="Browse reference-range frameworks.", no_args_is_help=True
)

_TokenOpt = Annotated[
    str | None,
    typer.Option(
        "--token-name",
        help="Keyring token to authenticate as (default: [cli] token_name).",
    ),
]
_FrameworkOpt = Annotated[
    str | None,
    typer.Option(
        "--framework", help="Flag results against this reference-range framework."
    ),
]
_LimitOpt = Annotated[
    int | None,
    typer.Option("--limit", help="Max rows to fetch (server page cap applies)."),
]
_JsonOpt = Annotated[
    bool, typer.Option("--json", help="Emit raw JSON instead of a rendered table.")
]


# --------------------------------------------------------------------------
# One authenticated session per command (config + keyring + client once)
# --------------------------------------------------------------------------


def _build_client(cfg: Config) -> httpx.Client:
    """The HTTP client for one command invocation (tests substitute this)."""
    return cli_client.default_client(cfg)


@dataclass(frozen=True)
class _Api:
    """One command's live REST session: resolved config, token, open client."""

    cfg: Config
    token_name: str
    token: str
    client: httpx.Client

    def send(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        return cli_client.send_on(
            self.client, self.cfg, self.token_name, self.token, method, path, **kwargs
        )

    def request(self, method: str, path: str, **kwargs: Any) -> Any:
        return cli_client.request_on(
            self.client, self.cfg, self.token_name, self.token, method, path, **kwargs
        )

    def get(self, path: str, params: dict[str, Any]) -> Any:
        return self.request("GET", path, params=params)


@contextmanager
def _api(ctx: typer.Context, token_name: str | None) -> Generator[_Api]:
    """Resolve config + token once and open one client for the whole command."""
    cfg = load_config_or_exit(ctx)
    name = token_name or cfg.cli.token_name
    token = cli_client.token_plaintext(name)
    with _build_client(cfg) as client:
        yield _Api(cfg=cfg, token_name=name, token=token, client=client)


def _fetch_all(api: _Api, path: str, params: dict[str, Any]) -> list[dict[str, Any]]:
    """Follow keyset cursors to collect every row of a list resource."""
    items: list[dict[str, Any]] = []
    query = dict(params)
    cursor: str | None = None
    for _ in range(10_000):  # safety bound; real catalogs are far smaller
        if cursor is not None:
            query["cursor"] = cursor
        page = api.get(path, query)
        items.extend(page["items"])
        cursor = page.get("next_cursor")
        if cursor is None:
            return items
    raise fail("pagination did not terminate; narrow the query and retry")


def _submit_import(
    api: _Api, payload: dict[str, Any], *, dry_run: bool
) -> tuple[int, Any]:
    """POST an import batch, returning ``(status_code, parsed_body_or_None)``.

    Uses :meth:`_Api.send` (not ``request``) so a structured ``422``/``409``
    reaches the caller (the preview offering an ``upsert``) instead of exiting;
    transport errors and a stale ``401`` still fail loud.
    """
    response = api.send(
        "POST",
        api_import.IMPORT_PATH,
        params={"dry_run": True} if dry_run else {},
        json=payload,
    )
    try:
        body: Any = response.json()
    except ValueError:
        body = None
    return response.status_code, body


# --------------------------------------------------------------------------
# Value parsing (ADR-0030 fidelity — a comparator is never lost)
# --------------------------------------------------------------------------

# Longest first so "<=" wins over "<" and ">=" over ">"; the vocabulary itself
# is the server's (imports.COMPARATORS, ADR-0030), so the two cannot drift.
_COMPARATORS = tuple(sorted(imports.COMPARATORS, key=len, reverse=True))

# A well-formed thousands-grouped number ("150,000", "1,234.56"). Only this
# exact grouping is accepted as numeric; an ambiguous "1,5" stays qualitative.
_THOUSANDS = re.compile(r"-?\d{1,3}(,\d{3})+(\.\d+)?")

# A value that fell through to qualitative text but looks like a decimal typed
# with a comma ("5,2", "1,23") — a plausible mis-typed separator we warn about
# rather than silently store as text (and never guess it into a number).
_LOOKS_LIKE_DECIMAL_COMMA = re.compile(r"-?\d+,\d{1,2}")


def parse_value(raw: str) -> tuple[float | None, str | None, str | None]:
    """Parse a typed value into the ADR-0030 ``(value_num, comparator, value_text)``.

    A leading comparator (``<``, ``<=``, ``>=``, ``>``) is preserved and its
    magnitude parsed as a number — a censored ``<0.1`` becomes
    ``(0.1, "<", None)``, **never** the bare number ``0.1`` (the fidelity the
    schema exists to protect). A bare finite number is numeric (thousands
    separators tolerated); anything else is a qualitative ``value_text``. A
    comparator with no parseable finite number is a :class:`ValueError` (the
    ADR-0030 CHECK forbids it).
    """
    s = raw.strip()
    if not s:
        raise ValueError("a value is required")
    for comparator in _COMPARATORS:
        if s.startswith(comparator):
            magnitude = _finite_float(s[len(comparator) :].strip())
            if magnitude is None:
                raise ValueError(
                    f"comparator {comparator!r} needs a finite number, got "
                    f"{s[len(comparator) :].strip()!r}"
                )
            return magnitude, comparator, None
    numeric = _finite_float(s)
    if numeric is not None:
        return numeric, None, None
    return None, None, s  # qualitative


def _finite_float(text: str) -> float | None:
    try:
        value = float(text)
    except ValueError:
        # A US-style thousands-grouped number is a number, not qualitative text;
        # anything else (a stray comma, "1,5") stays text rather than guess.
        if _THOUSANDS.fullmatch(text):
            try:
                value = float(text.replace(",", ""))
            except ValueError:
                return None
        else:
            return None
    return value if math.isfinite(value) else None


# --------------------------------------------------------------------------
# Biomarker resolution + interactive confirm-and-record (ADR-0059 §3)
# --------------------------------------------------------------------------


def _biomarker_catalog(api: _Api) -> list[dict[str, Any]]:
    return _fetch_all(api, api_read.BIOMARKERS_PATH, {})


def _resolution_index(api: _Api, catalog: list[dict[str, Any]]) -> dict[str, int]:
    """Normalized name -> biomarker id over the canonical + alias namespace.

    Mirrors the server resolver (:func:`imports.resolve_biomarker_name`,
    ADR-0054 §3): a typed name resolves against both canonical names and
    stored aliases, so a name the owner recorded as an alias last session
    resolves silently this session, exactly as ADR-0059 §3 promises. Canonical
    names are normalized here (they are stored as display spelling);
    ``alias_normalized`` is already normalized (server-derived at write time).
    """
    index: dict[str, int] = {}
    for row in catalog:
        index.setdefault(
            imports.normalize_name(str(row["canonical_name"])), int(row["id"])
        )
    for row in _fetch_all(api, api_read.BIOMARKER_ALIASES_PATH, {}):
        index.setdefault(str(row["alias_normalized"]), int(row["biomarker_id"]))
    return index


def _name_map(rows: list[dict[str, Any]]) -> dict[int, str]:
    return {int(row["id"]): str(row["canonical_name"]) for row in rows}


def _catalog_matches(rows: list[dict[str, Any]], query: str) -> list[dict[str, Any]]:
    """Biomarker rows whose normalized canonical name contains ``query``."""
    needle = imports.normalize_name(query)
    return [
        row
        for row in rows
        if needle in imports.normalize_name(str(row["canonical_name"]))
    ]


def _resolve_biomarker(
    typed: str,
    index: dict[str, int],
    rows: list[dict[str, Any]],
    pending_aliases: list[dict[str, Any]],
) -> int | None:
    """A real biomarker id for ``typed``, or ``None`` if the owner skips.

    An exact (normalized) match over the canonical + alias namespace resolves
    silently. Anything else drops into the interactive pick; if the owner then
    confirms recording the typed string as an alias, it is queued in
    ``pending_aliases`` (written only if the draw commits) and added to
    ``index`` so it resolves silently for the rest of this session.
    """
    norm = imports.normalize_name(typed)
    exact = index.get(norm)
    if exact is not None:
        return exact
    chosen = _pick_biomarker(typed, rows)
    if chosen is None:
        return None
    biomarker_id = int(chosen["id"])
    canonical = str(chosen["canonical_name"])
    if norm != imports.normalize_name(canonical) and typer.confirm(
        f"  record '{typed}' as an alias of {canonical}?", default=True
    ):
        pending_aliases.append(
            {"biomarker_id": biomarker_id, "alias": typed, "source": "manual"}
        )
        index[norm] = biomarker_id  # resolve silently for the rest of this session
    return biomarker_id


def _pick_biomarker(typed: str, rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Search-and-pick a biomarker for an unresolved name (ADR-0059 §3)."""
    typer.echo(f"  '{typed}' matches no biomarker name.")
    query = typed
    while True:
        shown = _catalog_matches(rows, query)[:20]
        if shown:
            typer.echo(f"  candidates for {query!r}:")
            for number, row in enumerate(shown, start=1):
                typer.echo(
                    f"    {number}. {row['canonical_name']}  "
                    f"(id {row['id']}, {row.get('category', '?')})"
                )
        else:
            typer.echo(f"  no candidates for {query!r}.")
        choice = typer.prompt(
            "  pick a number, type a new search, or blank to skip this result",
            default="",
            show_default=False,
        ).strip()
        if not choice:
            return None
        picked = _choice_index(choice)
        if picked is not None:
            if 1 <= picked <= len(shown):
                return shown[picked - 1]
            typer.echo("  no candidate has that number.")
            continue
        query = choice


def _choice_index(choice: str) -> int | None:
    """A typed pick as an int, or ``None`` if it is not a plain decimal number.

    ``isdecimal()``, not ``isdigit()``: ``int()`` accepts only decimal digits,
    while ``isdigit()`` is ``True`` for superscripts/circled digits that
    ``int()`` rejects — gating on ``isdigit()`` would crash on ``int('²')``.
    A non-numeric choice returns ``None`` so the caller treats it as a search.
    """
    return int(choice) if choice.isdecimal() else None


# --------------------------------------------------------------------------
# `enter` — the interactive draw-level entry flow
# --------------------------------------------------------------------------


def enter(
    ctx: typer.Context,
    token_name: _TokenOpt = None,
    framework: _FrameworkOpt = None,
    lab_id: Annotated[
        int | None,
        typer.Option("--lab-id", help="Skip the lab prompt with a known id."),
    ] = None,
    on_conflict: Annotated[
        str,
        typer.Option(
            "--on-conflict",
            help="Conflict policy for the import: reject | skip | upsert.",
        ),
    ] = imports.REJECT,
) -> None:
    """Enter one lab draw and its results, previewed then committed (ADR-0059)."""
    if on_conflict not in imports.POLICIES:
        raise fail(
            f"--on-conflict must be one of {', '.join(sorted(imports.POLICIES))}, "
            f"got {on_conflict!r}"
        )
    with _api(ctx, token_name) as api:
        # Verify the framework up front — against the server's own resolver —
        # so a typo fails before any data is entered, never at the post-commit
        # readback.
        _verify_framework(api, framework)

        resolved_lab_id = _resolve_lab(api, lab_id)
        draw_row = _prompt_draw(resolved_lab_id)
        draw_utc = str(draw_row["draw_utc"])

        catalog = _biomarker_catalog(api)
        index = _resolution_index(api, catalog)
        result_rows, pending_aliases = _prompt_results(index, catalog)
        if not result_rows:
            typer.echo("No results entered; nothing to import.")
            raise typer.Exit()

        payload = {
            "source": "manual",
            "conflict_policy": on_conflict,
            "lab_draws": [draw_row],
            "lab_results": result_rows,
        }
        body = _preview_and_commit(api, payload, on_conflict)
        typer.echo(f"Imported batch {body['batch_id']}: {_summary_line(body)}")
        # Aliases are recorded only now, after the draw is committed — so
        # aborting the draw records nothing (ADR-0059 §3).
        _record_aliases(api, pending_aliases)
        _readback(api, resolved_lab_id, draw_utc, framework, catalog)


def _preview_and_commit(
    api: _Api, payload: dict[str, Any], on_conflict: str
) -> dict[str, Any]:
    """Dry-run preview, confirm, then commit; returns the committed 200 body."""
    status, body = _submit_import(api, payload, dry_run=True)
    preview = _require_ok(status, body, on_conflict)
    typer.echo(f"\nPreview ({on_conflict}): {_summary_line(preview)}")
    if not typer.confirm("Commit this draw?", default=True):
        typer.echo("Aborted; nothing written.")
        raise typer.Exit()
    status, body = _submit_import(api, payload, dry_run=False)
    return _require_ok(status, body, on_conflict)


def _require_ok(status: int, body: Any, on_conflict: str) -> dict[str, Any]:
    """A committed/previewed import's 200 body, or a clean CLI failure."""
    if status != 200:
        _render_import_error(status, body, on_conflict)
        raise typer.Exit(code=1)
    if not isinstance(body, dict):
        # A 200 with a non-JSON body: guard rather than deref None into a
        # traceback (the request() path names this same "wrong port" case).
        raise fail(
            "the Core Service returned 200 with a non-JSON body; is something "
            "else listening on its port?"
        )
    return cast(dict[str, Any], body)


def _record_aliases(api: _Api, aliases: list[dict[str, Any]]) -> None:
    """Persist the confirmed aliases (idempotent skip), reporting honestly."""
    if not aliases:
        return
    payload = {
        "source": "manual",
        "conflict_policy": imports.SKIP,  # idempotent: an existing alias is a no-op
        "biomarker_aliases": aliases,
    }
    status, body = _submit_import(api, payload, dry_run=False)
    if status != 200 or not isinstance(body, dict):
        typer.echo(f"  (aliases not recorded: {status} {_error_detail(body)})")
        return
    summary = cast(dict[str, Any], body).get("summary")
    summary_map = cast(dict[str, Any], summary) if isinstance(summary, dict) else {}
    counts_obj = summary_map.get("biomarker_aliases", {})
    counts = cast(dict[str, Any], counts_obj) if isinstance(counts_obj, dict) else {}
    inserted = int(counts.get("rows_inserted", 0) or 0)
    unchanged = int(counts.get("rows_unchanged", 0) or 0)
    skipped = int(counts.get("rows_skipped", 0) or 0)
    if inserted:
        typer.echo(f"  recorded {inserted} new alias(es).")
    if unchanged:
        typer.echo(f"  {unchanged} alias(es) already on record.")
    if skipped:
        # 'skip' keeps a pre-existing row whose stored details differ — the CLI
        # must not claim it recorded a mapping the server left untouched.
        typer.echo(
            f"  {skipped} alias(es) left unchanged — already stored differently."
        )


def _resolve_lab(api: _Api, lab_id: int | None) -> int:
    labs = _fetch_all(api, api_read.LABS_PATH, {})
    known = ", ".join(sorted(str(lab["name"]) for lab in labs)) or "(none)"
    if lab_id is not None:
        if any(int(lab["id"]) == lab_id for lab in labs):
            return lab_id
        raise fail(f"no lab with id {lab_id}; known labs: {known}")
    name = typer.prompt("Lab name").strip()
    matches = [lab for lab in labs if str(lab["name"]).casefold() == name.casefold()]
    if len(matches) == 1:
        return int(matches[0]["id"])
    if len(matches) > 1:
        raise fail(
            f"{name!r} matches more than one lab by case ({known}); pass --lab-id."
        )
    raise fail(
        f"no lab named {name!r}. Known labs: {known}. Add a new lab via "
        "'POST /v1/import' (labs table), or pass --lab-id."
    )


def _prompt_draw(lab_id: int) -> dict[str, Any]:
    draw_utc = typer.prompt(
        "Draw date (YYYY-MM-DD, or YYYY-MM-DDThh:mm:ssZ for a UTC time)"
    ).strip()
    if not draw_utc:
        raise fail("a draw date is required")
    _validate_draw_utc(draw_utc)
    row: dict[str, Any] = {"id": 1, "lab_id": lab_id, "draw_utc": draw_utc}
    fasting = _prompt_fasting()
    if fasting is not None:
        row["fasting"] = fasting
    context = typer.prompt(
        "Draw context (optional)", default="", show_default=False
    ).strip()
    if context:
        row["draw_context"] = context
    notes = typer.prompt(
        "Draw notes (optional)", default="", show_default=False
    ).strip()
    if notes:
        row["notes"] = notes
    return row


def _validate_draw_utc(value: str) -> None:
    """Require a hyphenated ``YYYY-MM-DD`` date, optionally with a ``T`` + UTC time.

    This is deliberately **not** "any ISO-8601". Point-in-time resolution keys on
    ``substr(draw_utc, 1, 10)`` (ADR-0005), so the stored value must *begin* with
    an extended (hyphenated) ``YYYY-MM-DD`` — its first ten characters are the
    comparison date. A basic-format ISO-8601 value (``20260115``, or the timestamp
    ``20260115T083000Z``) is valid ISO-8601 yet its first ten characters are not
    the date, so it is rejected — and `date`/`datetime.fromisoformat` *accept* it,
    which is exactly why this check cannot lean on them alone. Accepted:

    * a bare date ``YYYY-MM-DD``; or
    * ``YYYY-MM-DDT<time>`` with a UTC designator (``Z`` or ``+00:00``) — the ``T``
      separator is required, and the column being ``draw_utc`` a naive or offset
      time is rejected (it is not UTC ground truth and its local date could differ
      from the UTC date the comparison uses).

    The import boundary does not check ``draw_utc`` (unlike the sibling
    ``framework_ranges.effective_date``), so this is the fail-early guard; it
    shares the import path's dashed-date grammar (``imports.ISO_DATE``, public).
    """
    if imports.ISO_DATE.fullmatch(value):  # a bare YYYY-MM-DD
        try:
            date.fromisoformat(value)
        except ValueError:
            raise fail(f"draw date {value!r} is not a real calendar date") from None
        return
    bad = (
        f"draw date {value!r} must be YYYY-MM-DD, or a UTC timestamp that begins "
        "with it (e.g. 2026-01-15T08:30:00Z)"
    )
    # The date prefix must be the hyphenated form and the separator a literal T,
    # so substr(draw_utc, 1, 10) is the comparison date (rejects 20260115T...).
    if imports.ISO_DATE.fullmatch(value[:10]) is None or value[10:11] != "T":
        raise fail(bad)
    try:
        stamp = datetime.fromisoformat(value)
    except ValueError:
        raise fail(bad) from None
    if stamp.tzinfo is None or stamp.utcoffset() != timedelta(0):
        raise fail(
            f"draw timestamp {value!r} must be UTC — end it with 'Z' or '+00:00'"
        )


def _prompt_fasting() -> int | None:
    answer = (
        typer.prompt("Fasting? [y/n/blank=unknown]", default="", show_default=False)
        .strip()
        .casefold()
    )
    if answer in ("y", "yes"):
        return 1
    if answer in ("n", "no"):
        return 0
    return None  # blank or "unknown"


def _prompt_results(
    index: dict[str, int], catalog: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Prompt result rows against the shared draw; returns (rows, pending_aliases).

    Re-entering a biomarker (the natural way to fix a mistyped value) replaces
    the earlier row last-wins, rather than emitting a duplicate
    ``(lab_draw_id, biomarker_id)`` the import engine would reject — which would
    otherwise 422 the whole batch and discard the session.
    """
    typer.echo("Enter results (blank biomarker name to finish):")
    rows: list[dict[str, Any]] = []
    aliases: list[dict[str, Any]] = []
    seen: dict[int, int] = {}  # biomarker_id -> its index in `rows`
    while True:
        name = typer.prompt("  Biomarker", default="", show_default=False).strip()
        if not name:
            return rows, aliases
        biomarker_id = _resolve_biomarker(name, index, catalog, aliases)
        if biomarker_id is None:
            typer.echo("  (result skipped)")
            continue
        raw_value = typer.prompt("  Value (e.g. 5.2, <0.1, positive)").strip()
        try:
            value_num, comparator, value_text = parse_value(raw_value)
        except ValueError as exc:
            typer.echo(f"  {exc}; result skipped.")
            continue
        if (
            value_num is None
            and value_text is not None
            and _LOOKS_LIKE_DECIMAL_COMMA.fullmatch(value_text)
        ):
            # Kept as text (no locale guessing) but never silently — a mis-typed
            # decimal separator is a plausible mistake at the entry surface.
            typer.echo(
                f"  note: recorded {value_text!r} as text; if you meant a "
                "number, use '.' not ','."
            )
        default_unit = _canonical_unit(catalog, biomarker_id)
        unit = typer.prompt(
            "  Unit (UCUM)", default=default_unit or "", show_default=bool(default_unit)
        ).strip()
        row: dict[str, Any] = {"lab_draw_id": 1, "biomarker_id": biomarker_id}
        if value_num is not None:
            row["value_num"] = value_num
        if comparator is not None:
            row["comparator"] = comparator
        if value_text is not None:
            row["value_text"] = value_text
        if unit:
            row["unit"] = unit
        if biomarker_id in seen:
            typer.echo("  (replacing your earlier entry for this biomarker)")
            rows[seen[biomarker_id]] = row
        else:
            seen[biomarker_id] = len(rows)
            rows.append(row)


def _canonical_unit(catalog: list[dict[str, Any]], biomarker_id: int) -> str | None:
    for row in catalog:
        if int(row["id"]) == biomarker_id:
            unit = row.get("canonical_unit")
            return str(unit) if unit is not None else None
    return None


def _verify_framework(api: _Api, framework: str | None) -> None:
    """Reject an unknown framework up front, using the server's own resolver.

    Probes ``?framework=`` with ``limit=1`` so the check uses the exact
    ``COLLATE NOCASE`` resolution the readback will — a name that passes here
    cannot 422 at the post-commit readback, which a parallel client-side
    casefold could not guarantee for non-ASCII names.
    """
    if framework is None:
        return
    response = api.send(
        "GET", api_read.LAB_RESULTS_PATH, params={"framework": framework, "limit": 1}
    )
    if response.status_code == 422:  # the only 422 this probe can raise
        known = sorted(
            str(row["name"])
            for row in _fetch_all(api, api_read.RANGE_FRAMEWORKS_PATH, {})
        )
        listing = ", ".join(known) or "(none seeded)"
        raise fail(f"unknown framework {framework!r}. Known frameworks: {listing}")
    if response.status_code >= 400:
        raise fail(
            f"the Core Service answered {response.status_code}: "
            f"{cli_client.detail(response)}"
        )


# --------------------------------------------------------------------------
# Readback + rendering
# --------------------------------------------------------------------------


def _readback(
    api: _Api,
    lab_id: int,
    draw_utc: str,
    framework: str | None,
    catalog: list[dict[str, Any]],
) -> None:
    """Show the just-entered results, range-flagged if a framework was given."""
    draws = _fetch_all(
        api,
        api_read.LAB_DRAWS_PATH,
        {"lab_id": lab_id, "draw_from": draw_utc, "draw_to": draw_utc},
    )
    match = next((d for d in draws if str(d["draw_utc"]) == draw_utc), None)
    if match is None:
        typer.echo("(entered draw not found on readback)")
        return
    params: dict[str, Any] = {"lab_draw_id": int(match["id"]), "order": "asc"}
    if framework is not None:
        params["framework"] = framework
    results = _fetch_all(api, api_read.LAB_RESULTS_PATH, params)
    typer.echo(f"\nDraw {match['id']} ({draw_utc}):")
    names = _name_map(catalog)
    for row in results:
        typer.echo(_render_result(row, names))


def _render_result(row: dict[str, Any], names: dict[int, str]) -> str:
    biomarker_id = int(row["biomarker_id"])  # NOT NULL on every lab_results row
    label = names.get(biomarker_id, f"biomarker {biomarker_id}")
    unit = str(row.get("unit") or "")
    line = f"  {label}: {row.get('display', '')} {unit}".rstrip()
    comparison = row.get("range_comparison")
    if isinstance(comparison, dict):
        return f"{line}  [{_render_comparison(cast(dict[str, Any], comparison))}]"
    lab_range = _format_range(
        row.get("reference_low"), row.get("reference_high"), row.get("reference_text")
    )
    return f"{line}  (lab ref: {lab_range})" if lab_range else line


def _render_comparison(comparison: dict[str, Any]) -> str:
    parts = [str(comparison.get("flag"))]
    target = _format_range(
        comparison.get("range_low"),
        comparison.get("range_high"),
        comparison.get("range_text"),
    )
    unit = str(comparison.get("unit") or "")
    if target:
        parts.append(f"target {target} {unit}".rstrip())
    reason = comparison.get("reason")
    if reason:
        parts.append(str(reason))
    framework = comparison.get("framework")
    if framework:
        parts.append(str(framework))
    return " | ".join(parts)


def _format_range(low: Any, high: Any, text: Any) -> str:
    """Render an interval bound for display; a one-sided bound reads as ≤/≥.

    A one-sided target (``range_low`` NULL, ``range_high`` 200 — the shape of
    every seeded lipid/A1c target) must not render as ``"-200"``, which reads
    as a *negative* bound.
    """
    if text is not None:
        return str(text)
    if low is not None and high is not None:
        return f"{_num(low)}-{_num(high)}"
    if high is not None:
        return f"≤{_num(high)}"  # <= high
    if low is not None:
        return f"≥{_num(low)}"  # >= low
    return ""


def _num(value: Any) -> str:
    if value is None:
        return ""
    return reads.format_num(float(value))  # one integral-strip rule, shared


def _summary_line(body: dict[str, Any]) -> str:
    summary = body.get("summary", {})
    parts: list[str] = []
    for table, counts in summary.items():
        segments = [
            f"{count} {label.removeprefix('rows_')}"
            for label, count in counts.items()
            if count
        ]
        parts.append(f"{table}: {', '.join(segments) or 'no changes'}")
    return "; ".join(parts) or "nothing to do"


def _render_import_error(status: int, body: Any, on_conflict: str) -> None:
    typer.echo(f"error: import rejected ({status})", err=True)
    detail = (
        cast(dict[str, Any], body).get("detail") if isinstance(body, dict) else None
    )
    if isinstance(detail, list):
        saw_conflict = False
        for item in cast(list[Any], detail):
            if isinstance(item, dict):
                entry = cast(dict[str, Any], item)
                message = str(entry.get("message", ""))
                typer.echo(
                    f"  - {entry.get('table')}[{entry.get('row_index')}]: {message}",
                    err=True,
                )
                saw_conflict = saw_conflict or "conflict" in message
            else:
                typer.echo(f"  - {item}", err=True)
        if saw_conflict and on_conflict == imports.REJECT:
            typer.echo(
                "  These rows conflict with existing data. Re-run with "
                "'--on-conflict upsert' to correct them, or '--on-conflict "
                "skip' to keep the existing values.",
                err=True,
            )
    else:
        typer.echo(f"  {_error_detail(body)}", err=True)


def _error_detail(body: Any) -> str:
    extracted = cli_client.detail_from_body(body)
    if extracted is not None:
        return extracted
    return "(no response body)" if body is None else str(cast(object, body))


# --------------------------------------------------------------------------
# Read commands
# --------------------------------------------------------------------------


def _emit_json(obj: Any) -> None:
    typer.echo(json.dumps(obj, indent=2, default=str))


def _more_hint(page: dict[str, Any]) -> None:
    if page.get("next_cursor") is not None:
        typer.echo("  (more rows exist; narrow the filters or raise --limit)")


def _list_named(api: _Api, path: str, json_out: bool) -> None:
    """List an ``(id, name, description)`` catalog resource (labs, frameworks)."""
    rows = _fetch_all(api, path, {})
    if json_out:
        _emit_json(rows)
        return
    for row in rows:
        typer.echo(
            f"  {row['id']}  {row['name']}  {row.get('description') or ''}".rstrip()
        )


def _echo_biomarker(row: dict[str, Any]) -> None:
    unit = row.get("canonical_unit") or ""
    typer.echo(
        f"  {row['id']}  {row['canonical_name']}  "
        f"[{row.get('category', '?')}]  {unit}".rstrip()
    )


@results_app.command("list")
def results_list(
    ctx: typer.Context,
    token_name: _TokenOpt = None,
    biomarker_id: Annotated[int | None, typer.Option("--biomarker-id")] = None,
    lab_id: Annotated[int | None, typer.Option("--lab-id")] = None,
    lab_draw_id: Annotated[int | None, typer.Option("--lab-draw-id")] = None,
    draw_from: Annotated[str | None, typer.Option("--draw-from")] = None,
    draw_to: Annotated[str | None, typer.Option("--draw-to")] = None,
    framework: _FrameworkOpt = None,
    limit: _LimitOpt = None,
    json_out: _JsonOpt = False,
) -> None:
    """List lab results (newest first), optionally range-flagged."""
    with _api(ctx, token_name) as api:
        params = _drop_none(
            {
                "biomarker_id": biomarker_id,
                "lab_id": lab_id,
                "lab_draw_id": lab_draw_id,
                "draw_from": draw_from,
                "draw_to": draw_to,
                "framework": framework,
                "limit": limit,
            }
        )
        page = api.get(api_read.LAB_RESULTS_PATH, params)
        if json_out:
            _emit_json(page)
            return
        names = _name_map(_biomarker_catalog(api))
        for row in page["items"]:
            typer.echo(_render_result(row, names))
        _more_hint(page)


@results_app.command("show")
def results_show(
    ctx: typer.Context,
    result_id: int,
    token_name: _TokenOpt = None,
    framework: _FrameworkOpt = None,
    json_out: _JsonOpt = False,
) -> None:
    """Show one lab result by id, optionally range-flagged."""
    with _api(ctx, token_name) as api:
        row = api.get(
            f"{api_read.LAB_RESULTS_PATH}/{result_id}",
            _drop_none({"framework": framework}),
        )
        if json_out:
            _emit_json(row)
            return
        # One biomarker lookup for the one row, not the whole catalog.
        biomarker = api.get(
            f"{api_read.BIOMARKERS_PATH}/{int(row['biomarker_id'])}", {}
        )
        names = {int(biomarker["id"]): str(biomarker["canonical_name"])}
        typer.echo(_render_result(row, names))


@draws_app.command("list")
def draws_list(
    ctx: typer.Context,
    token_name: _TokenOpt = None,
    lab_id: Annotated[int | None, typer.Option("--lab-id")] = None,
    draw_from: Annotated[str | None, typer.Option("--draw-from")] = None,
    draw_to: Annotated[str | None, typer.Option("--draw-to")] = None,
    limit: _LimitOpt = None,
    json_out: _JsonOpt = False,
) -> None:
    """List lab draws (newest first)."""
    with _api(ctx, token_name) as api:
        params = _drop_none(
            {
                "lab_id": lab_id,
                "draw_from": draw_from,
                "draw_to": draw_to,
                "limit": limit,
            }
        )
        page = api.get(api_read.LAB_DRAWS_PATH, params)
        if json_out:
            _emit_json(page)
            return
        for row in page["items"]:
            fasting = row.get("fasting")
            fasting_txt = "?" if fasting is None else ("yes" if fasting else "no")
            typer.echo(
                f"  {row['id']}  {row['draw_utc']}  lab {row.get('lab_id')}  "
                f"fasting={fasting_txt}  {row.get('draw_context') or ''}".rstrip()
            )
        _more_hint(page)


@biomarkers_app.command("list")
def biomarkers_list(
    ctx: typer.Context,
    token_name: _TokenOpt = None,
    category: Annotated[str | None, typer.Option("--category")] = None,
    limit: _LimitOpt = None,
    json_out: _JsonOpt = False,
) -> None:
    """List biomarkers (by name), optionally filtered by category name."""
    with _api(ctx, token_name) as api:
        page = api.get(
            api_read.BIOMARKERS_PATH, _drop_none({"category": category, "limit": limit})
        )
        if json_out:
            _emit_json(page)
            return
        for row in page["items"]:
            _echo_biomarker(row)
        _more_hint(page)


@biomarkers_app.command("search")
def biomarkers_search(
    ctx: typer.Context,
    query: str,
    token_name: _TokenOpt = None,
    json_out: _JsonOpt = False,
) -> None:
    """Substring-search the biomarker catalog by canonical name."""
    with _api(ctx, token_name) as api:
        matches = _catalog_matches(_biomarker_catalog(api), query)
    if json_out:
        _emit_json(matches)
        return
    if not matches:
        typer.echo(f"no biomarker matches {query!r}.")
        return
    for row in matches:
        _echo_biomarker(row)


@labs_app.command("list")
def labs_list(
    ctx: typer.Context, token_name: _TokenOpt = None, json_out: _JsonOpt = False
) -> None:
    """List lab sources."""
    with _api(ctx, token_name) as api:
        _list_named(api, api_read.LABS_PATH, json_out)


@frameworks_app.command("list")
def frameworks_list(
    ctx: typer.Context, token_name: _TokenOpt = None, json_out: _JsonOpt = False
) -> None:
    """List reference-range frameworks."""
    with _api(ctx, token_name) as api:
        _list_named(api, api_read.RANGE_FRAMEWORKS_PATH, json_out)


def _drop_none(params: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in params.items() if value is not None}
