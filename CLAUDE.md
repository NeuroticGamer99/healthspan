# CLAUDE.md — Project Instructions

## Personal data containment

`specs/personal/` is the only location where personal health data or personally identifying information may be written. This folder is gitignored and must never be committed.

**What belongs in `specs/personal/`:**
- Any document containing the database owner's actual health values, lab results, diagnoses, medications, or clinical history
- Session-orientation files (e.g. `project-context-personal.md`)
- Any notes that would identify the database owner as an individual

**What belongs outside `specs/personal/` (e.g. directly in `specs/`):**
- Architectural decisions and design rationale
- Schema documentation
- Generic how-to and onboarding guides
- Anything safe to publish in a public repository

When creating or editing any file, verify it contains no personal health data before placing it outside `specs/personal/`.

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
6. **Keep the index current** — after any ADR change (new file, status update, title change), update the `## Index` table in `specs/adr/README.md` to reflect it. The index must always match the actual files and their `## Status` fields.
