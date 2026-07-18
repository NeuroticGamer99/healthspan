# ADR-0059: CLI Manual-Entry Tooling Implementation Decisions (Phase 3 WI-4)

## Status
Proposed

## Context and Problem Statement

Phase 3 WI-4 is the phase's final work item and the platform's **first interactive
data-entry surface**: a CLI that lets the database owner type real lab results into the
encrypted database, draw by draw, and see them range-flagged. It is a **thin client** over
the REST surface already built — `POST /v1/import` ([ADR-0004](0004-data-ingestion-strategy.md),
[ADR-0052](0052-bulk-import-identity-and-conflict-resolution.md),
[ADR-0054](0054-biomarker-name-alias-fallback.md)) for writes and the `read`-scoped GET
routes ([ADR-0053](0053-read-endpoint-surface-and-pagination.md),
[ADR-0058](0058-range-comparison-implementation-decisions.md)) for lookups and flagged
readback — adding no new server endpoints, schema, or range semantics unless a decision
below says otherwise. It resolves the **manual-entry-efficiency** open question
([open-questions.md](../open-questions.md), Data Entry) with the development plan's mandated
shape: enter the lab and draw date once, then enter results against that draw.

The specs fix the client-facing contracts (the api-reference.md import/read ledger is
authoritative) and leave the CLI's own surface open: which credential it authenticates
with, its command shape and entry modality, how it handles an unresolved biomarker name,
how inline range flags pick a framework, and whether any of the three WI-3 deferrals this
surface is the named trigger for (`physical_min`, `Glucose (fasting)`, UCUM-at-import) are
folded in now versus left deferred with their triggers.

Owner decisions (2026-07-17, at WI-4 kickoff) fix these.

## Decision Drivers

- WI-4 is a **thin client**: every gap in the underlying data degrades to a safe, loud flag
  (`no_range`, `indeterminate`, `error`), never a crash or a silently wrong result, so the
  CLI must surface those honestly rather than paper over them.
- The manual-entry-efficiency question ([open-questions.md](../open-questions.md)) is about
  killing repetition — lab, draw date, and ranges repeat across every row of one draw.
- This is the first surface where a *fail-early* message beats a *late* read-time flag, which
  is why several WI-3 deferrals name it as their trigger.
- The credential the CLI carries is a security-relevant choice ([ADR-0026](0026-named-scoped-tokens.md)):
  the entry surface needs `import` + `read`, and no more.
- Personal health data typed at this surface must never leak into the repo (CLAUDE.md
  containment) — tests use synthetic values only.

## Decision Outcome

*Decisions are recorded here as they are made during implementation (CLAUDE.md decision
capture), not reconstructed at the end.*

### 1. Credential and authentication model — configurable, default `cli-admin`

The entry CLI reads its bearer token from the OS keyring entry `token:<name>`, where
`<name>` comes from a `--token-name` option (or a `[cli] token_name` config key), defaulting
to **`cli-admin`**. This works with zero configuration — `cli-admin` is always minted at first
`service start` ([ADR-0050](0050-token-store-and-auth-implementation-decisions.md) §1) and
carries both `import` and `read` — while letting the owner point the CLI at a narrower
hand-minted token (`healthspan token create cli-entry --scopes read,import`) for least
privilege. It reuses `keychain.load_token_plaintext` and the existing keyring convention
unchanged, so the Accepted [ADR-0026](0026-named-scoped-tokens.md) default-token set is
**not** extended (no new bootstrap token, no extension ADR). The CLI is a REST client only —
the Core Service must be running; there is no direct-database fallback, exactly as the
`token`/`auth`/`mcp` groups already work ([ADR-0051](0051-auth-lifecycle-and-rate-limiting-implementation-decisions.md)).

### 2. Command surface and entry modality — interactive draw-level entry + readback

The CLI ships the mandated **interactive draw-level template** (enter lab + draw date once,
then loop results against that draw — [open-questions.md](../open-questions.md) Manual entry
efficiency) **and** a small set of `read`-scoped readback commands (list recent results with
inline flags, biomarker history, browse draws/biomarkers), so the phase milestone —
"range-flagged, queryable" — is demonstrable through the CLI itself rather than only over raw
REST. Both halves are thin clients over the existing endpoints. A file/CSV batch-import format
is **out of scope** (it overlaps the Phase-7 ingestion adapters; the plan's mandate is the
interactive template). Entry previews against `POST /v1/import?dry_run=true` before the real
write, so validation errors and conflicts surface before anything is committed.

**Two entry-surface input checks are validated/warned client-side** (the "fail-early at the
interactive surface" rationale the project already applies to the parallel UCUM case,
[open-questions.md](../open-questions.md)), not at the import boundary — so no API contract
changes (rule 6, local):

- **Draw date** must **begin with a hyphenated `YYYY-MM-DD`** — either a bare date, or
  `YYYY-MM-DDT<time>` with a UTC designator (`Z`/`+00:00`) and a literal `T` separator. This is
  deliberately narrower than "any ISO-8601": point-in-time resolution keys on
  `substr(draw_utc, 1, 10)` ([ADR-0005](0005-reference-range-frameworks.md)), so the stored value's
  first ten characters must *be* the comparison date. A **basic-format** ISO-8601 value (`20260115`,
  or the timestamp `20260115T083000Z`) is valid ISO-8601 but its first ten characters are not the
  date, so it is rejected — and `date`/`datetime.fromisoformat` *accept* it, which is why the check
  gates on `imports.ISO_DATE` (promoted public) rather than the parser alone. Also rejected:
  unpadded/invalid dates (`2026-1-1`, `2026-13-01`, `01/15/2026`) and naive/non-UTC timestamps. The
  import boundary does **not** validate `draw_utc` (unlike the sibling
  `framework_ranges.effective_date`), so this is the fail-early guard; a bad value would otherwise
  misdate the draw permanently and silently mis-flag it. Closing the same gap at `POST /v1/import`
  and in the schema (so a non-CLI writer cannot store a malformed `draw_utc` either) is deferred —
  [open-questions.md](../open-questions.md), the write-boundary sibling of the UCUM-at-import item.
- **Comma-decimal values** stay qualitative text (the parser never *guesses* between a thousands
  separator and a decimal comma — `150,000` is the number 150000, `1,5` stays text), but the
  downgrade is no longer **silent**: a value that looks like a mis-typed decimal (`5,2`) prints a
  one-line notice ("recorded as text; if you meant a number, use '.' not ','"). Warn, don't
  guess — the locale is not established in-repo.

### 3. Unresolved biomarker name — interactive confirm-and-record

Server-side resolution is exact-match and fail-loud ([ADR-0054](0054-biomarker-name-alias-fallback.md) §4).
When a typed name does not resolve, the CLI searches the catalog (client-side substring over
`GET /v1/biomarkers` pages, since the endpoint has no name filter), presents the candidates,
and lets the owner **pick the intended biomarker or abort**. On a pick it uses the resolved
`biomarker_id` directly for the current result, and **offers to record the typed string as an
alias** so the name resolves next time. No fuzzy auto-matching is ever performed; the human
confirms every mapping.

**A confirmed alias is recorded only after the draw commits** — not eagerly at pick time. It is
queued during entry and written (a *separate* `biomarker_aliases` import under the idempotent
`skip` policy) once the draw's own preview/commit succeeds; declining "Commit this draw?" writes
nothing, so an aborted draw leaves no orphan alias. A confirmed alias is also added to the
in-session resolution index immediately, so re-typing the same name later in the *same* session
resolves silently rather than re-prompting. The result row always carries the picked
`biomarker_id`, so it never depends on a same-batch alias — sidestepping the same-batch
visibility rule ([ADR-0057](0057-reference-data-and-catalog-import-implementation-decisions.md) §9)
cleanly. The post-commit record honestly distinguishes *inserted* (newly recorded) from *skipped*
(a pre-existing row with different stored details, left untouched by `skip`), never claiming to
have recorded a mapping the server left unchanged.

### 4. Inline range-flag framework selection — opt-in `--framework`, else the lab's own range

An optional `--framework <name>` on the entry-confirmation readback and the read commands
attaches the [ADR-0058](0058-range-comparison-implementation-decisions.md) `range_comparison`
flag to each result (passed straight through as `?framework=`). **Absent the option**, the CLI
shows the lab's own `reference_low`/`reference_high`/`reference_text` that rode in on the row —
no framework comparison, nothing guessed. Flagging against a single framework (not all seeded
frameworks at once) matches the one-framework `?framework=` contract and keeps output legible.
An unknown framework name is rejected loudly, two ways: the read commands surface the endpoint's
own `422`, while `enter` runs a **pre-flight** check and fails before any data is entered — so a
typo never wastes an entry session or, worse, surfaces only at the post-commit readback. The
pre-flight probes the server's *own* resolver (`GET /v1/lab-results?framework=<name>&limit=1`,
whose `422` is the same `COLLATE NOCASE` resolution the readback uses) rather than a parallel
client-side casefold — so a name that passes the check cannot then `422` at the readback, which a
divergent client-side fold could not guarantee for non-ASCII names. No default-framework config
knob is introduced.

### 5. Disposition of the three WI-3 deferrals this surface triggers — all three stay deferred

WI-4 is the named *trigger surface* for `physical_min`, the `Glucose (fasting)` split, and
UCUM-validation-at-import ([project's open-questions.md](../open-questions.md)), but per the
development plan's binding rule — *deferred-with-trigger questions stay deferred; never resolve
ahead of the trigger* — none is folded into WI-4. Each degrades to a safe, loud flag, so the
CLI ships without them and each fires organically into its own follow-up PR when real data
makes its verdict visibly wrong. The one revisit condition: `physical_min` is folded in only if
the *very first* real below-detection entry produces a visibly-wrong `indeterminate` — otherwise
it stays separate (cleaner diff, matches the WI-1→WI-3 scoping).

### 6. A `biomarker_aliases` read endpoint (`GET /v1/biomarker-aliases`)

§3's promise — an alias the owner records "resolves next time" — is only real for
the CLI if the CLI can *see* aliases. Its resolver runs client-side (there is no
"resolve this name" endpoint), and the only alias-bearing surface was import-write,
so a recorded alias would have shortcut nothing on the next `enter`. So this WI adds
the `read`-scoped `GET /v1/biomarker-aliases` (list + get-by-id, `biomarker_id`
filter, `alias_normalized`-ascending), the read counterpart api-reference.md already
earmarked for WI-4. The CLI's resolver now consults the **canonical + alias**
namespace, mirroring the server resolver ([ADR-0054](0054-biomarker-name-alias-fallback.md) §3),
so a name aliased last session resolves silently this session. It is an ordinary
catalog read on the existing keyset/page-cap machinery ([ADR-0053](0053-read-endpoint-surface-and-pagination.md)),
no schema change — the one server-side addition in an otherwise pure-client WI, made
because the approved §3 behavior is not deliverable without it.

### Positive Consequences

- The manual-entry-efficiency question is resolved with the plan's mandated shape; real lab
  data can finally enter the encrypted database by hand, range-flagged and queryable — the
  Phase 3 milestone.
- Zero-config out of the box (default `cli-admin`) yet least-privilege-capable (`--token-name`).
- The alias confirm-and-record loop turns each unrecognized name into durable future
  resolution, without ever guessing.
- No new **write** endpoint, schema, or range semantics: the write path stays the single
  validated `POST /v1/import` (§6's `GET /v1/biomarker-aliases` is the one added read route), and
  every data gap stays a loud flag.

### Negative Consequences / Tradeoffs

- Defaulting to `cli-admin` means the entry surface runs with admin scope unless the owner
  mints a narrower token; least privilege is opt-in, not the default. This is in tension with
  [security.md](../security.md)'s "secure by default, never traded for convenience" principle,
  and is accepted deliberately: this is a single-user local platform, the default reuses the
  always-minted [ADR-0050](0050-token-store-and-auth-implementation-decisions.md) §1 token rather
  than minting a new broad one, `--token-name` / `[cli] token_name` make narrowing a one-line
  change, and no security invariant (INV-1…6) is touched. A dedicated `read`+`import` default
  token would be strictly better but costs an [ADR-0026](0026-named-scoped-tokens.md) extension
  (its default set is Accepted); that is the natural follow-up if least-privilege-by-default is
  wanted.
- Client-side substring biomarker search is O(catalog) fetches; fine at ~64 rows, but not a
  server-side search — revisit if the catalog grows large.
- The three deferrals remaining open means a first-real-data session may surface an
  `indeterminate`/`no_range`/`error` that reads as a wart until its follow-up PR lands.

## Consequences for Other Documents

- **[api-reference.md](../api-reference.md)** — the new `read`-scoped `GET /v1/biomarker-aliases`
  list/get routes (§6) are documented under Reference data, replacing the "no read endpoint yet
  (deferred to WI-4)" notes. The `[cli] token_name` config key is client config, not API surface;
  its decision is recorded in §1 above (there is no separate config-reference doc — the
  ADR-0051 `[auth]` knobs follow the same ADR-only pattern).
- **[open-questions.md](../open-questions.md)** — the **Manual entry efficiency** entry is
  resolved by this WI (§2); the three WI-3 deferrals this surface triggers stay deferred (§5),
  each annotated that WI-4 shipped without folding it in and its trigger is now live.

## Links
- Implements: the Phase 3 WI-4 "CLI manual-entry tooling" item in [development-plan.md](../development-plan.md)
- Builds on: [ADR-0052](0052-bulk-import-identity-and-conflict-resolution.md) — the import identity and conflict model the CLI submits against
- Builds on: [ADR-0054](0054-biomarker-name-alias-fallback.md) — the `biomarker_name` resolver and alias namespace the entry flow uses
- Builds on: [ADR-0053](0053-read-endpoint-surface-and-pagination.md) — the read surface the CLI queries
- Builds on: [ADR-0058](0058-range-comparison-implementation-decisions.md) — the `?framework=` range-comparison enrichment the CLI renders inline
- Related: [ADR-0026](0026-named-scoped-tokens.md) — the token/scope model the CLI's credential draws from
- Related: [open-questions.md](../open-questions.md) — Manual entry efficiency (resolved here); the WI-3 deferrals this surface triggers
