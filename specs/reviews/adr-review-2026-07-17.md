# ADR and Spec Consistency Review — 2026-07-17

Pre-lock-in sweep of all 61 ADRs, the cross-cutting specs, and the shipped code, run at the
close of Phase 3 (WI-4 merged as `3d6975f`) and before the Proposed→Accepted lock-in review
of the implementation-decision ADRs 0049–0059. Five parallel audit passes: Phase-2 impl
ADRs vs code/specs, Phase-3 impl ADRs vs code/specs, Accepted ADRs 0001–0048 chain
hygiene, cross-cutting specs vs code, and open-questions/development-plan hygiene.

Point-in-time artifact, same genre as the `architecture-review-*.md` files: findings were
accurate at `3d6975f` and are not maintained afterward. Severity is scoped to the lock-in:
**what does it cost if ADRs 0049–0059 freeze as-is?**

---

## Clean bill (verified, not assumed)

- **ADRs 0049–0059 describe the shipped code accurately.** Claim-by-claim verification —
  units API and error taxonomy, the 7-flag range vocabulary and 1e-9 bound tolerance,
  migrations 0002–0005 DDL and seed contents (64 biomarkers, 19 categories, 19 molar
  masses, 3 frameworks), limiter parameters, cursor semantics, natural keys and partial
  unique indexes, CLI manual-entry behaviors, alias routes — found no falsehoods beyond
  findings A1/A2 below.
- [api-reference.md](../api-reference.md) ↔ all 26 registered routes: paths, methods, scopes,
  and error behavior match; every remaining *Endpoints TBD* marker corresponds to
  legitimately-future surface (Phase 4+), none hides shipped surface.
- [data-model.md](../data-model.md) ↔ migrations 0001–0005: exact match (categories + reserved
  row + delete trigger, `category_id` FK, `biomarker_aliases`, `molar_mass` CHECK, framework
  tables, natural-key indexes).
- [security.md](../security.md) INV-1…INV-6: upheld or vacuously N/A (plugin and annotate
  surfaces not yet built); the nine-scope vocabulary matches `tokens.SCOPES` exactly.
- [observability.md](../observability.md) and [testing-strategy.md](../testing-strategy.md) ↔
  code and `ci.yml`: structured-log fields, metrics shape, serial CI with canary, 3-OS
  matrix, pinned tool versions, Hypothesis profiles — all reconcile.
- Personal-data containment scan of `specs/` (excluding `specs/personal/`): clean. The
  open-questions accumulation-evidence paragraph names data *types* and a published range
  only, no owner values.
- ADR chain reciprocity: ~25 extend/extended-by pairs verified bidirectional; the only
  broken Accepted-side pair is finding B4 (0013↔0034).
- Post-acceptance immutability: every edit to an Accepted ADR since the 2026-07-08 flip
  (`fd08c31`) was a navigation link or status normalization (verified via git diffs).
- `Decisions:` convention: present on every Phase-3 merge commit; tooling commits correctly
  state "none".
- [open-questions.md](../open-questions.md) §-references all resolve to real sections;
  deferral symmetry with 0049–0059 holds in both directions (exceptions catalogued in C4).

---

## A. Fix inside the soon-immutable ADRs while they are still Proposed

**A1 — SHOULD-FIX (borderline blocker). ADR-0049 §7 dangling deferral.**
[ADR-0049](../adr/0049-core-service-skeleton-implementation-decisions.md) line 63: "How to
expose API docs *securely* (behind auth, or `monitor`-gated) is a WI-2 question decided
with the auth layer." WI-2 shipped (ADR-0050/0051) and decided nothing about it; no
open-questions entry exists anywhere in `specs/`; the code hard-disables all three doc
routes (`service.py:276-278`). Accepted as-is, 0049 immutably points at a work item that
never resolved the question. Fix while Proposed: reword to a proper deferral with an
open-questions entry, or record the decision ("docs stay disabled until X").

**A2 — SHOULD-FIX. ADR-0050 §5 makes a security claim the shipped limiter does not
deliver.** [ADR-0050](../adr/0050-token-store-and-auth-implementation-decisions.md) line 41
(echoed line 58): "The unauthenticated-failure audit write is bounded by the WI-2b rate
limiter once it lands." It is not: every throttled attempt still writes an `auth_audit`
row (`api_security.py:325-338`; contractual at [api-reference.md](../api-reference.md) line 23
"A rate-limited request writes an `auth_audit` row"), and 429s are answered immediately, so
per-request audit growth under a local flood is unchanged. The limiter bounds its own
in-memory state, not audit-table growth; ADR-0051 does not correct the claim. The same
passage carries stale "accepted for the one intervening PR" hedges describing a window that
has closed — fix in the same edit. Alternative: consciously accept the unbounded-append
property (mitigated by the loopback-only default bind) and say so.

**A3 — MINOR. ADR-0053 §2 misleading term.**
[ADR-0053](../adr/0053-read-endpoint-surface-and-pagination.md) line 39 says the pagination
cursor encodes "a schema version"; the code encodes a cursor-*format* version
(`reads.py:44`, `CURSOR_VERSION = 1`), unrelated to the database `schema_version`. Reword
to "format version" while editable.

**A4 — MINOR. ADR-0055 §2 implies an app-level delete-guard mechanism.**
[ADR-0055](../adr/0055-biomarker-category-taxonomy.md) line 73 describes the reserved-row
guard as "mirroring ADR-0051's reserved-name guard" (an app-level check); the implementing
[ADR-0057](../adr/0057-reference-data-and-catalog-import-implementation-decisions.md) §1 chose
a SQL `BEFORE DELETE` trigger and declares it the single enforcement point (migration
`0004`, trigger `categories_reserved_no_delete`). Not a contradiction — 0055 delegates
mechanics to the implementing WI — but frozen as-is it permanently implies a mechanism
that was superseded. Touch up or consciously acknowledge.

## B. Acceptance ordering and navigation links to land before the freeze

**B1 — SHOULD-FIX. Accept foundation-first (or as one batch).** The entire 0049–0059 chain
is Proposed and interdependent: 0054→0052, 0057→0052/0053, 0058/0059→0053, 0059→0050/0051.
Flipping the later ADRs to Accepted while the ADRs they extend stay mutable creates
immutable records built on still-editable contracts. Order the flips 0049–0053 before (or
together with) 0054–0059.

**B2 — SHOULD-FIX. ADR-0052's Links understate its extenders.**
[ADR-0052](../adr/0052-bulk-import-identity-and-conflict-resolution.md) line 98 declares only
"Extended by: ADR-0054", but ADR-0057 generalized the import engine (catalog-table flags,
four new tables) and ADR-0058 §6 added two more plus `nullable_key`; the importable
registry 0052 fixed at two tables now has eight (`imports.py`, `IMPORT_ORDER`). 0052
anticipated exactly this (line 86: "extended, not contradicted, when they land"). Add
`Extended by: ADR-0057, ADR-0058` before it freezes.

**B3 — SHOULD-FIX. ADR-0053's Links carry no Extended-by entries at all.** Since 0053
shipped: (a) ADR-0055 changed the `category` filter from free-text equality to
case-insensitive category-name lookup — [api-reference.md](../api-reference.md) line 107
itself calls this a "breaking change from the Phase 2 free-text `category` filter"; (b)
ADR-0058 added the `?framework=` parameter to 0053's own lab-results routes; (c)
ADR-0057/0059 grew the surface from eight `read`-scoped routes over four resources to
sixteen over eight. Add the Extended-by links (and arguably re-badge 0055's relationship
from "Related" to "Extends") — or accept 0053 explicitly as a Phase-2 historical snapshot
whose surface enumeration is dated.

**B4 — SHOULD-FIX. Accepted-side navigation-link gaps (all permitted in-place edits).**
- [ADR-0013](../adr/0013-encryption-at-rest.md) Links is missing `Extended by: ADR-0034`
  ([ADR-0034](../adr/0034-clinical-document-storage.md) line 75 declares "Extends: ADR-0013").
  The only broken extend/extended-by pair in ADRs 0001–0048.
- [ADR-0040](../adr/0040-health-endpoint-authentication.md) mandates a per-source-address rate
  cap with no number; ADR-0049 §4 fixes it (30 req / rolling 1 s) but lists 0040 only as
  "Related". Add `Extended by: ADR-0049` to 0040.
- [ADR-0042](../adr/0042-process-supervision-and-single-instance-locking.md) has no
  counterpart link for ADR-0049 decision 6, which repurposes 0042's advisory lock as the
  `db migrate`/`backup`/`restore` service-up guard.

**B5 — MINOR. Index and verb nits (uncaught by CI, which never compares titles).**
`adr/README.md` title drift for 0040 ("…Liveness Exemption and Monitor Scope" vs the file's
"…the Liveness Exemption and the Monitor Scope") and 0043 ("the Annotate Scope" vs "the
`annotate` Scope"). [ADR-0032](../adr/0032-biomarker-loinc-cardinality.md) says "Extends:
ADR-0030" while [ADR-0030](../adr/0030-biomarker-identity.md) lists 0032 only as "Related" —
asymmetric verbs, harmless while 0032 is a stub.

## C. Decisions to make deliberately at lock-in (not text defects)

**C1 — Non-loopback posture (the finding that touches decision content).** Accepting
ADR-0049 hardens its §Binding-posture LAN-bind opt-in while the open question
([open-questions.md](../open-questions.md) lines 182-185) still offers resolution (b) —
rejecting non-loopback hosts in the config parser — which would then require a superseding
ADR instead of an in-place amendment. Related wording issue:
[security.md](../security.md) lines 92-95 describe Host-header validation and CORS as *active*
controls ("the server rejects any request whose Host header does not match…"), but neither
is implemented (`service.py` adds only Metrics/RequestID middleware). security.md is a
mutable standing-requirements doc — those controls could be re-worded as "required before
any non-loopback bind", which is what open-questions already says. Decide (a) vs (b) before
flipping 0049, or flip with eyes open.

**C2 — ADR-0020 stub carries load-bearing normative content.** The "Proposed — stub" with
Decision Outcome "TBD" ([ADR-0020](../adr/0020-plugin-registry.md) lines 326-341) contains a
binding requirement — the publication age gate, default `min_release_age_days = 14` — that
Accepted [ADR-0036](../adr/0036-plugin-package-installation-integrity.md) treats as part of
its stated control set (lines 45, 76). An Accepted decision depends on content that lives
only in a TBD stub. Promote the age-gate content to a decided ADR (Phase 7 timing works) or
record the dependency deliberately.

**C3 — Pre-decide ADR-0014's supersession mechanics.** The plan expects Phase-4
"supersession/subsumption by ADR-0011", but 0011 is Accepted and immutable. The legal
moves: mark 0014 `Superseded by ADR-0011` and add a navigation link on 0011 (both permitted
edits), or write a small new ADR. Nothing currently contradicts 0014's text; this is a
one-line decision whose *mechanics* are worth agreeing on before Phase 4.

**C4 — Deferral-tracking gaps to bless or fix.**
- ADR-0049 §6 defers the psutil PID-identity refinement to Phase 6 with no open-questions
  entry (unlike the rekey-fsync deferral beside it). Mitigated: Accepted ADR-0042 owns the
  mechanism — but the trigger lives only in 0049's soon-immutable prose.
- ADR-0052 line 67 defers `data.imported`/`data.corrected` event emission to Phase 4 via
  phase sequencing only (line 94: "no new deferral") — the sole 0049–0059 deferral without
  an open-questions entry. Defensible (the event bus is ADR-0011/Phase-4-owned); make it a
  conscious call.
- The open-questions "Database integrity & sanity check" consolidation entry (lines
  101-108) double-tracks the draw_utc, UCUM-at-import, and view-recreation invariants,
  which also stand as separate entries. Decide fold-in vs. standalone before the ADRs
  that cite them freeze.

**C5 — NOTE. ADR-0044 line 520** ("The project has 44 ADRs and no code") is dated context
that will fossilize if 0044 is ever accepted verbatim. Not in this lock-in (0044 is
Phase-5-triggered); flag for whenever it comes up.

## D. Mutable-doc hygiene (cheap, fix any time)

**D1 — SHOULD-FIX. development-plan.md carries no phase-completion state.** Phases 0–3 are
all shipped, but the text is forward-tense throughout (Phase 3 "fully ungated" as pending;
the Phase-3 milestone unmarked; Phase 2 present-tense) and the preamble still frames the
repo at "no code yet" (line 5, historically dated but misleading in a living doc). Add
per-phase status markers.

**D2 — MINOR. specs/README.md** design-documents index omits
[molar-mass-provenance.md](../molar-mass-provenance.md) (created in PR #28); the
architecture-review files are deliberately unlisted, but this is a durable design doc.

**D3 — MINOR. open-questions.md line 52** says the cohort-dimension instances arose "in a
seed of only seven rows"; migration 0005 seeds six `framework_ranges` rows (NIH 3, ADA 2,
AHA 1) — the seventh is presumably the deliberately-dropped HDL range.

## E. Code nits surfaced during verification (out of ADR scope)

**E1 — `api_read.py` line 10** docstring: "Fourteen `read`-scoped GET routes" over seven
resources; the module defines sixteen over eight (`biomarker-aliases`, added in WI-4, is
missing from the enumeration). No spec claims a route count; purely internal staleness.

**E2 — `pool.py` line 66 bare-comma `except` — a toolchain conflict, not a violation.**
Verified during this review: the CI-pinned ruff **0.15.21** formatter actively rewrites
`except (A, B, C):` to the bare-comma PEP 758 form (ruff 0.14.1 left the parentheses
alone). The in-code comment is accurate — the parenthesized form cannot survive
`ruff format` at the pinned version. `reads.py` escaped via a common base class
(`except ValueError:`); pool.py's trio (`sqlcipher3.Error`, `db.DatabaseError`,
`PoolClosedError`) has no common base short of `Exception`. Options: a `# fmt: off` guard,
a nested try/except refactor, or updating the standing always-parenthesize preference to
acknowledge that the pinned formatter forces the bare form.

---

## Suggested sequencing for the lock-in

1. **One docs-only PR while everything is still Proposed:** A1–A4 (in-ADR fixes), B2–B5
   (navigation links, including the Accepted-side permitted edits), D1–D3 (hygiene). All
   cheap; none changes a decision.
2. **Decide C1** (non-loopback (a) vs (b)) — the only finding that changes an ADR's
   decision content, and the one that motivated the lock-in review in the first place.
3. **Then flip, foundation-first:** 0049–0053, then 0054–0059 (or one batch), recording
   the C4 calls in the flip PR's `Decisions:` section. C2/C3/C5 are later-phase items —
   record them as open-questions entries or notes now.
