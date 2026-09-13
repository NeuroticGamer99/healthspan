---
name: repo-stats
description: Report the repository's size and shape at one point — files, lines, code/comment/blank split, ratios, and ADR-status breakdown, for the working tree or any past commit. Use when asked "how big is the repo", for a progress snapshot, to refresh the size tally, or to measure how large the repo was at some commit.
---

# /repo-stats — repository size and shape

The counting lives in `scripts/repo_stats.py` (stdlib-only, no dependencies). This skill runs it and narrates the result; it never re-implements the counting by hand.

This skill measures **one point** — the working tree, or any single commit. For more than one point, use `/repo-history`: it owns both the series over time and the `diff` between two commits ("what did this branch add"), driven by the same script and the same categories.

## 1. Produce the numbers

Run the script and show its markdown table verbatim:

```bash
python scripts/repo_stats.py
```

For a machine-readable dump (feeding another tool, or keeping a point for later):

```bash
python scripts/repo_stats.py --json                        # legacy spelling, still works
python scripts/repo_stats.py snapshot --format json        # the current one
```

To measure a past commit without checking it out — the same categories, a different tree:

```bash
python scripts/repo_stats.py snapshot <rev>
python scripts/repo_stats.py snapshot <rev> --format json --out <scratchpad>/point.json
```

`--out` writes to a file instead of stdout; write those to the scratchpad, never into the repository — a report is not a tracked artifact.

**Do not hand-subtract two JSON dumps to answer "what changed".** That is `/repo-history`'s `diff` subcommand, which resolves merge-bases correctly and renders ratios as transitions. Reach for it rather than rebuilding it here.

If the script reports a **Warnings** section (an unparseable Python file, a non-UTF-8 file), surface it — a parse failure means that file's comment/code split degraded to the `#`-only rule and is slightly off.

If the **Uncounted tracked files** footnote names anything, surface that too and say so plainly: those files are in no category and therefore in no total, so every number above them understates the repo by that much. The fix is a category, not a footnote — treat it as a defect in the script, not as a caveat to repeat.

## 2. Narrate the numbers

The table is the deliverable; add a few sentences of interpretation, not a restatement of the cells. Draw from what the numbers actually say this time — candidates:

- **Test balance** — the tests:implementation ratio, by *code* lines (the physical-line ratio is inflated by docstrings and blanks on both sides).
- **Doc weight** — docs:code, and whether ADRs still dominate the text surface.
- **Harness weight** — the `.claude/` skills-and-agents row against `scripts/`: the prose that drives the harness and the code that implements it are two halves of one thing, and either growing alone is worth a sentence.
- **ADR status** — the Accepted vs Proposed split; a rising Proposed count flags decisions awaiting lock-in.
- **Migrations** — the count is a proxy for how far the schema has moved past the initial cut.

Do not editorialize a metric that did not move.

## 3. Scope reminders (only if asked)

- `specs/personal/` is **excluded** by design — it must never exist (ADR-0079: personal data lives outside the repository), and a recreation must be neither counted nor read. The exclusion sits in the enumeration layer, so it holds for a historical revision exactly as for the live tree. The script prints this as a footnote; it counts no files and reads no content there.
- Enumeration is **git's, not the filesystem's** (`git ls-files`). An untracked file is therefore not counted, and git is required. That is what lets the live table and any historical point be compared at all.
- The code/comment/blank split treats a Python **docstring** as comment (found via the AST, so an assigned multi-line string stays code) and a trailing comment on a code line as code. Markdown has no comment column — every non-blank line is content.
- Category membership and every accepted limitation are documented in the `scripts/repo_stats.py` module docstring; point there rather than re-explaining.

This skill only reports. It makes no commits and edits no files.
