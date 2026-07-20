---
name: repo-stats
description: Report the repository's size and shape — files, lines, code/comment/blank split, ratios, and ADR-status breakdown. Use when asked "how big is the repo", for a progress snapshot, or to refresh the size tally.
---

# /repo-stats — repository size and shape

The counting lives in `scripts/repo_stats.py` (stdlib-only, no dependencies). This skill runs it and narrates the result; it never re-implements the counting by hand.

## 1. Produce the numbers

Run the script and show its markdown table verbatim:

```
python scripts/repo_stats.py
```

For a machine-readable dump (feeding another tool, diffing two points in time):

```
python scripts/repo_stats.py --json
```

If the script reports a **Warnings** section (an unparseable Python file, a non-UTF-8 file), surface it — a parse failure means that file's comment/code split degraded to the `#`-only rule and is slightly off.

## 2. Narrate what changed

The table is the deliverable; add a few sentences of interpretation, not a restatement of the cells. Draw from what the numbers actually say this time — candidates:

- **Test balance** — the tests:implementation ratio, by *code* lines (the physical-line ratio is inflated by docstrings and blanks on both sides).
- **Doc weight** — docs:code, and whether ADRs still dominate the text surface.
- **ADR status** — the Accepted vs Proposed split; a rising Proposed count flags decisions awaiting lock-in.
- **Migrations** — the count is a proxy for how far the schema has moved past the initial cut.

Do not editorialize a metric that did not move.

## 3. Scope reminders (only if asked)

- `specs/personal/` is **excluded** by design — gitignored personal data, absent in CI, not part of the shippable surface. The script prints this as a footnote; it counts no files and reads no content there.
- The code/comment/blank split treats a Python **docstring** as comment (found via the AST, so an assigned multi-line string stays code) and a trailing comment on a code line as code. Markdown has no comment column — every non-blank line is content.
- Category membership and every accepted limitation are documented in the `scripts/repo_stats.py` module docstring; point there rather than re-explaining.

This skill only reports. It makes no commits and edits no files.
