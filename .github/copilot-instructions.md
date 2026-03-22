# GitHub Copilot Instructions

See `CLAUDE.md` at the project root for full project instructions. The most critical rule is summarized here:

## Personal data containment

`specs/personal/` is the only location where personal health data or personally identifying information may be written. This folder is gitignored and must never be committed.

Do not suggest writing personal health values, lab results, diagnoses, medications, or clinical history to any file outside `specs/personal/`.
