# GitHub Copilot Instructions

See `CLAUDE.md` at the project root for full project instructions. The most critical rule is summarized here:

## Personal data containment

Personal health data and personally identifying information live outside this repository. No path in the repository may hold them, and `specs/personal/` must never exist.

Do not suggest writing personal health values, lab results, diagnoses, medications, or clinical history to any file in this repository. If you encounter such content, report only the path and data category; never quote the values. Copilot has no path filter, so a file under `specs/personal/` can appear in a diff you review: report only that a file exists under that directory — never its filename, never its contents — because the filename itself is provenance.
