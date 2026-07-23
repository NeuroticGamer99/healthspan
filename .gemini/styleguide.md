# Gemini review styleguide

Prompt input for the opt-in Antigravity SDK reviewer
(`.github/workflows/gemini-review.yml`, driven by `/gemini-review`). Sibling
config: `.coderabbit.yaml` teaches the same conventions to CodeRabbit — a
convention added or changed here must be mirrored there, and vice versa.
Authoritative sources are `CLAUDE.md` and `specs/`; this file is the distilled
review lens, kept lean on purpose.

## What to review for

- **Correctness and design** of the changed code, at the scale and threat model
  this system actually has (local-first, single owner, loopback service).
- **Personal-data containment.** `specs/personal/` is the ONLY location for
  personal health values, lab results, diagnoses, medications, clinical
  history, or the provenance/sequence of the owner's actual records. Flag any
  such content anywhere else — including in test fixtures, comments, and docs —
  but report only the path and the data category; **never quote, copy, or echo
  the actual values, provenance, or identifying details into a review
  comment** (reviews are public). Never suggest writing personal health values
  or identifying information to any path outside `specs/personal/`.
- **ADR governance.** Accepted ADRs are immutable: the only in-place edits are
  a Status flip to `Superseded by ADR-XXXX`, a Links navigation entry, and
  typo/broken-link fixes. A decision change is a NEW superseding ADR; an
  addition is a NEW extending ADR. Any ADR add/status/title change must update
  the Index in `specs/adr/README.md`. Inline "(ADR-XXXX, Status)" annotations
  in prose are point-in-time by design — flag edits that "chase" them.
- **Decision capture.** A design decision surfaced by the diff must be recorded
  in its owning doc layer in the same change: architectural → new Proposed ADR;
  API surface → `specs/api-reference.md`; schema shapes →
  `specs/data-model.md`; config knobs/defaults → the owning ADR; deferred
  questions → `specs/open-questions.md`. A decision that exists only in code is
  a spec bug.
- **Security invariants** (`specs/security.md`): INV-1 key confinement, INV-2
  no plugin code in the Core Service, INV-3 plugin-tier credentials, INV-4
  data-only plugin influence, INV-5 named/scoped/revocable credentials, INV-6
  source-data purity, INV-7 append-only audit surfaces, INV-8 hash-only
  credential storage. Flag changes that could touch one without a matching
  ADR/spec note.
- **Python:** never PEP 758 bare-comma `except A, B:` — always parenthesize
  exception tuples. **PowerShell:** project files must be read/written with
  explicit BOM-free UTF-8 (`UTF8Encoding($false)`); flag `Get-Content -Raw` or
  `Set-Content` without `-Encoding UTF8`.
- **Locale invariance.** The project is locale-invariant where it matters; flag
  locale-dependent parsing or formatting introduced without a note.

## What not to flag

- Style already gated by CI: ruff lint/format, pyright strict, PyMarkdown
  markdown style, the ADR-index and spec-link checks.
- Dev-tooling or reviewer configuration with no product/API/schema/security
  contract and no blocking CI gate — per `CLAUDE.md` that routes to rule 6
  ("code is the record", Decisions: none).
- Restatements of the diff, or findings on lines the diff does not touch.
