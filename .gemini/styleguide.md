# Gemini Code Assist — Review Style Guide (healthspan)

This file teaches Gemini Code Assist the non-obvious conventions of the
healthspan project. It is the Gemini analog of the `path_instructions` in
`.coderabbit.yaml`; both derive from `CLAUDE.md`, which is the source of truth.
When this guide and `CLAUDE.md` disagree, `CLAUDE.md` wins — flag the drift.

Gemini is the third model-family reviewer on this repo (Google), alongside
CodeRabbit (OpenAI models) and the Claude-based `/code-review`. Focus on
substantive correctness, security, and spec-fidelity issues; CodeRabbit already
covers style nits under an assertive profile.

## ADR governance

ADRs live in `specs/adr/`. An Accepted ADR is an immutable historical record.

- The only permitted in-place edits to an Accepted ADR are correcting its
  `## Status` field to `Superseded by ADR-XXXX` and adding a navigation link in
  its `## Links` section.
- A decision change must be a **new** ADR that supersedes the old one; an
  addition must be a **new** ADR that extends it.
- Flag any content change to the body of an Accepted ADR.
- Any ADR add, status change, or title change must also update the `## Index`
  table in `specs/adr/README.md`. Flag a change that does not.
- Inline ADR status annotations in prose (for example "(ADR-0055, Proposed)")
  are point-in-time by design. Do not flag them as stale, and do flag edits that
  "chase" a status change in historical prose.

## Personal-data containment

`specs/personal/` is the only location where personal health data or personally
identifying information may live. It must never be committed — but do not treat
`.gitignore` as the guarantee; flag such content wherever it appears.

- Personal data means the database owner's actual health values, lab results,
  diagnoses, medications, or clinical history — and the provenance or sequence
  of their actual records (which lab, which panel, in what order), even with no
  values attached.
- It also means any personally identifying information — notes or details that
  would identify the database owner as an individual — even with no health
  values attached.
- Flag any such content appearing anywhere outside `specs/personal/`, including
  a tracked or force-added file that slipped past `.gitignore`.
- Never suggest writing personal health values or identifying information to any
  path outside `specs/personal/`.

## Decision-capture routing

A design decision surfaced during implementation must be recorded in the owning
doc layer in the same change. A decision that exists only in code is a spec bug.

- Architectural (new dependency, process, table, or anything touching a security
  invariant) routes to a new Proposed ADR.
- API surface (endpoints, request or response shapes, error formats, status
  codes, scopes) routes to `specs/api-reference.md`.
- Schema shapes (columns, constraints, indexes) route to `specs/data-model.md`.
- Config knobs and defaults route to the owning ADR.
- Deferred questions route to `specs/open-questions.md`.
- Flag new endpoints, response or error shapes, schema columns, or config
  defaults that have no matching spec or ADR update.

## Security invariants

`specs/security.md` holds an invariants table (key handling, encryption-at-rest,
auth and scopes, append-only audit, credential hashing, backup-guard,
orphan-sweep, non-loopback exposure).

- A change touching any invariant requires a Proposed (or extension) ADR, per
  the decision-capture routing above and `CLAUDE.md` — a standalone spec note is
  not sufficient. Flag any such change that lacks the required ADR.

## Python conventions

- Never write PEP 758 bare-comma `except A, B:` — always parenthesize the
  exception tuple as `except (A, B):`. The bare-comma form is valid 3.14 syntax
  but aliases the removed Python-2 footgun and trips reviewers and tools.
- The project is locale-invariant where it matters; do not introduce
  locale-dependent parsing or formatting without a note.

## PowerShell conventions

PowerShell files must read and write project files with explicit UTF-8. The repo
contains multibyte Unicode (em dashes, box-drawing, arrows) that the default
Windows-1252 encoding corrupts silently.

- For writes, use the BOM-free .NET pattern
  `[System.IO.File]::WriteAllText($path, $content, [System.Text.UTF8Encoding]::new($false))`.
- `-Encoding UTF8` is not BOM-free on Windows PowerShell 5.1 (it is on
  PowerShell 7+). For reads, `Get-Content -Encoding UTF8 -Raw` is fine.
- Flag `Get-Content -Raw` or `Set-Content` without `-Encoding UTF8` on project
  files.

## What not to flag

Per the `CLAUDE.md` decision-capture discriminator, dev-tooling and reviewer
configuration with no product, API, schema, or security contract and no blocking
CI gate route to "rule 6": code and config are their own record, and the
change's `Decisions:` section reads "none". Do not ask for an ADR or spec update
for such changes — this includes the `.gemini/` files themselves,
`.coderabbit.yaml`, `.editorconfig`, and similar tooling configuration.
