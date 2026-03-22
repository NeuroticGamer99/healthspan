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
