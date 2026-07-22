# CLAUDE.md — Project Instructions

## Personal data containment

`specs/personal/` is the only location where personal health data or personally identifying information may be written. This folder is gitignored and must never be committed.

**What belongs in `specs/personal/`:**
- Any document containing the database owner's actual health values, lab results, diagnoses, medications, or clinical history
- The provenance or sequence of the owner's actual records — which lab, which panel, in what order — is personal even with no values attached
- Session-orientation files (e.g. `project-context-personal.md`)
- Any notes that would identify the database owner as an individual

**What belongs outside `specs/personal/` (e.g. directly in `specs/`):**
- Architectural decisions and design rationale
- Schema documentation
- Generic how-to and onboarding guides
- Anything safe to publish in a public repository

When creating or editing any file, verify it contains no personal health data before placing it outside `specs/personal/`.

**Working with real data:** analyze the owner's actual reports and exports in conversation or under `specs/personal/`; publish only the generic structure they reveal — value shapes, units, range dimensions, source-format quirks — never the values or their provenance (attributing a quirk or format to one of the owner's actual sources is provenance, even with no values attached).

## PowerShell file encoding

When running PowerShell commands that read or write project files, always use explicit UTF-8 encoding. The project contains multibyte Unicode characters (em dashes, box-drawing characters, arrows) that Windows-1252 — PowerShell's default encoding — will corrupt silently.

**Reading files:**

```powershell
[System.IO.File]::ReadAllText($path, [System.Text.UTF8Encoding]::new($false))
# or
Get-Content $path -Encoding UTF8 -Raw
```

**Writing files:**

```powershell
[System.IO.File]::WriteAllText($path, $content, [System.Text.UTF8Encoding]::new($false))
```

Never use `Get-Content -Raw` without `-Encoding UTF8` on project files, and never use `Set-Content` without `-Encoding UTF8`. The `$false` parameter to `UTF8Encoding` suppresses the BOM.

## ADR governance

Before creating or modifying any file in `specs/adr/`:

1. **Check the ADR's current status** — read the `## Status` field. Also check the remote origin for the latest version (`git fetch` if needed) since the branch may be behind.
2. **Never modify an accepted ADR's content** — accepted ADRs are immutable historical records. The only permitted in-place edits are: correcting the `## Status` field to `Superseded by ADR-XXXX`, and adding a navigation link to the `## Links` section pointing to the superseding ADR.
3. **Supersede, don't edit** — if a decision changes, create a new ADR that supersedes the old one. Mark the old ADR's status as `Superseded by ADR-XXXX`.
4. **Extend, don't modify** — if an accepted ADR needs additions (new fields, new policies that don't reverse the original decision), create a new ADR that extends it. Add an `Extended by ADR-XXXX` navigation link to the original's `## Links` section. Keep the original's status as `Accepted`.
5. **Minor edits only for typos/links** — fixing a broken link or correcting a typo in an accepted ADR is acceptable without a new ADR. Anything that changes decision content is not.
6. **Keep the index current** — after any ADR change (new file, status update, title change), update the `## Index` table in `specs/adr/README.md` to reflect it. The index must always match the actual files and their `## Status` fields. (Mechanized by CI's docs-consistency gate, `scripts/check_adr_index.py`.)

Inline status annotations in prose — "(ADR-0055, Proposed)"-style — anywhere in `specs/` are point-in-time: they record the status when the sentence was written and are not kept in sync. The authoritative current status is always the ADR's `## Status` field and the `specs/adr/README.md` index. Do not edit historical annotations to chase status changes, and do not read them as current.

## Implementation decision capture

Implementing to the specs will surface decisions the specs deliberately leave open. Every such decision is recorded in the document layer that owns it, **in the same PR (or commit) that implements it**. A design decision that exists only in code is a spec bug.

**Routing rules — where each kind of decision is recorded:**

1. **Architectural** — a new dependency; a new process, component, scope, or table; anything touching a security invariant (the invariants table in `specs/security.md`); anything extending or contradicting an Accepted ADR; anything that constrains future decisions → a **new Proposed ADR**, landed with or before the implementing change. ADR governance above applies unchanged.
2. **API surface** — endpoint paths, request/response shapes, error formats, status codes, per-route scope declarations, MCP tool signatures → **`specs/api-reference.md`**, updated in the same PR. Its "*Endpoints TBD during implementation*" markers are replaced as part of implementation, never retroactively.
3. **Schema shapes** — columns, constraints, indexes not already fixed by an ADR → **`specs/data-model.md`** (or the owning ADR if it is still Proposed).
4. **Config knobs and defaults** → the **owning ADR** (the ADR-0035/0037/0038 pattern). If the owning ADR is Accepted, that costs an extension ADR — an accepted consequence of acceptance; batching several accumulated defaults into one extension ADR is fine.
5. **Questions discovered but deliberately deferred** → a **`specs/open-questions.md`** entry stating what triggers resolution.
6. **Local implementation detail** — private module layout, internal naming, algorithm choices with no external contract → **no spec record**; code and tests are the record.

**The discriminator:** Would someone building a client, plugin, or replacement component need this fact without reading the source? Record it (rules 2–4). Could it constrain or contradict a future ADR? Propose an ADR (rule 1). Neither? Rule 6.

**Mechanization:** every implementing PR's description carries a **`Decisions:`** section — links to the records it created or updated, or the explicit word "none". Never omit the section.
