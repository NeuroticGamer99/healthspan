---
name: repo-history
description: Report how the repository's size and shape changed across commits — either a series over time (CSV/JSON/chart) or the delta between two points. Use when asked how the repo has grown, what a branch or PR added, what changed between two commits or tags, when a trend matters more than a total, or to produce data to graph.
---

# /repo-history — repository shape across commits

The counting lives in `scripts/repo_stats.py` (stdlib-only, no dependencies) — the same script and the same categories `/repo-stats` uses for a single point. This skill owns everything involving **more than one** point: the `history` series and the `diff` delta. It never re-implements the counting, and it never compares numbers that came from different category sets.

**Which of the two:** use `diff` when only the endpoints matter ("what did this branch add?") — it costs two reports. Use `history` when the shape of the curve matters — it costs one report per commit.

## 1. The delta between two points

```bash
python scripts/repo_stats.py diff main...HEAD    # work done on this branch
python scripts/repo_stats.py diff v0.3 v0.4      # between two tags
python scripts/repo_stats.py diff main           # vs the working tree, uncommitted edits included
```

**The range spellings are git's own, and the distinction matters.** `main HEAD` and `main..HEAD` compare those two commits literally. `main...HEAD` resolves the **merge base** first — so once `main` has moved ahead of the branch point, it shows only this branch's work, where the two-dot form would charge main's independent commits against you as deletions. An omitted head means the working tree.

Naming three endpoints (`diff main..HEAD v0.3`) is refused rather than resolved by silently dropping one.

Options: `--all` keeps categories that did not change (default is only those that did); `--format json` carries both full reports plus the delta; `--format csv` is tidy-long over `(category, metric, base, head, delta, categories_version)` and keeps every category, since a missing row and a zero are indistinguishable once pivoted.

Ratios render as transitions (`3.97:1 → 4.01:1`), and the ADR line names only the buckets that moved. Read a `+0` row as "files changed, net size did not" — not as "nothing happened there".

**Cross-check against git when it is cheap.** `git diff --stat <base>` reports insertions and deletions; this reports *net physical growth*. They agree when little was rewritten in place and diverge on a refactor — if they disagree wildly, that is information, not an error.

## 2. Produce the series

Default is one point per commit, written as tidy-long CSV:

```bash
python scripts/repo_stats.py history --out <scratchpad>/shape.csv
```

Downsample when a chart is the goal and per-commit resolution is noise:

```bash
python scripts/repo_stats.py history --every week --out <scratchpad>/shape.csv
python scripts/repo_stats.py history --every month --format md
```

`--format md` prints a compact one-row-per-point table straight to the terminal — the right choice when the user asked a question rather than asked for a file. Its **`Code (py+sql)`** column is deliberately narrower than the snapshot table's `Code`, which sums all nine categories: the two tables agree on Files and Lines to the digit, so the narrower column says so in its header rather than leaving a reader to discover it. Markdown content is reported beside it, under `Docs`. `--format json` carries what CSV's fixed columns cannot (per-point ADR status, warnings, full shas). `--format html --out <path>` writes a self-contained stacked-area chart — inline SVG, no JavaScript, no network — that opens in any browser offline. It is a complete HTML document, so hand it over as a file; it is not in the shape the Artifact tool expects.

Narrow the walk with `--since`, `--until`, and `--ref`.

Write output files to the scratchpad, not into the repository. A series is a report, not a tracked artifact.

**If a run reports a degraded measurement, say so before quoting any number — and say which kind, because they distort in opposite directions.** Read the per-file lines, not the header count:

- **A blob that is not valid UTF-8, or a file that could not be read, is skipped** and is therefore absent from that point's counts. In a `diff` its absence at the *head* renders as *negative growth* — a shrinking repo that did not shrink — and its absence at the *base* renders as growth that did not happen.
- **A Python file that will not parse is not skipped.** It is counted in full; only its docstrings move from `comment` to `code`. Files and physical lines are unaffected and `code` is *overstated*. Reporting this one as a missing file is wrong twice over, and the tool said exactly that until PR #109.

The markdown and HTML outputs carry these inline; CSV and JSON have no slot for prose, so the same list goes to **stderr**. Do not narrate a degraded run as if it were clean.

**An `Uncounted` report is a third thing again, and it is not a degraded measurement.** Nothing failed: those files are tracked, readable, and claimed by no category, so they are absent from every number in the run. It travels beside the warnings on all four `history` formats and on `diff` — inline in markdown and HTML, on stderr for CSV and JSON. Treat it as a defect in the script's category list rather than a caveat to repeat, the same way `/repo-stats` does for a single point.

**A blob that could not be *read* at a revision is a different thing and you will not see it here — the run stops.** It exits 2 naming the object, the commit and the path, rather than warning and carrying on, because at a revision that silence is unrecoverable: the point would report a smaller repository than existed and nothing downstream could tell it from real shrinkage. The working tree keeps the gentler behaviour (one local, visible file; every other number stands). The `scripts/repo_stats.py` module docstring holds the full reasoning — point there rather than re-deciding it.

## 3. Two things to state, not assume

A reader will get both of these wrong unless told:

- **The walk is first-parent.** One point per merged PR; commits inside a merge do not appear. A PR that took twenty commits is one step in the series, and that is deliberate — an all-commits walk would interleave concurrent branches into what reads as a single timeline.
- **Every point is measured by *today's* categories.** That is what makes the series comparable at all, but it means a category can be non-zero before anyone thought of it, and re-running after a category changes will produce a different history. The `categories_version` stamp carried by every CSV row and JSON payload — the `diff` CSV included — is how a stale series is recognised; never merge two series whose stamps differ. Nothing *reads* the stamp back: it is provenance for a human comparing two files, and the refusal to combine versions is this instruction, not a check the script performs.

An empty week or month yields **no point**, not a repeat of the previous value. A gap in the series is a real gap in the commit record — and the chart's x axis is proportional to elapsed time, so a gap is drawn as one.

## 4. Narrate the trend (or the delta)

The data is the deliverable; add interpretation, not a restatement of rows. Draw from what actually moved:

- **Inflection points** — a category that jumps in one step usually names a specific PR. Say which, if it is obvious from the date.
- **Ratios over time** — tests:implementation and docs:code are more informative as trends than as today's snapshot. A ratio holding steady while both sides grow is a different story from one drifting.
- **Harness vs product** — `.claude/` prose plus `scripts/` code against `src/`, over time, is the clearest available measure of how much of this project is machinery.
- **ADR accumulation** — the Accepted/Proposed split per point (JSON format only) shows whether decisions are being locked in or piling up.

For a **delta** specifically, the useful sentences are different: which categories moved and which did not, whether tests kept pace with implementation, and whether the docs:code ratio held. A branch that adds implementation with a flat tests row is worth saying out loud.

Do not editorialize a series that is simply monotone growth, or a delta whose only story is "code was added".

## 5. Cost (only if asked)

`diff` is two reports: measured ~2 s on this repository.

A full per-commit walk measured **~36 s over this repository's 212 first-parent commits**; `--every week` cuts it to a few seconds. It spawns one `git ls-tree` per commit and shares a single `git cat-file --batch` plus a blob cache across the whole run, so each distinct file content is classified once per language rather than once per commit it survives into — measured, that is 1,410 distinct keys against 30,078 path-instances.

Re-measure rather than quoting these figures if the answer matters; they move as the repo grows. Three documents once carried three different answers for the same walk, which is what that sentence is for.

Category membership and every accepted limitation are documented in the `scripts/repo_stats.py` module docstring; point there rather than re-explaining.

This skill only reports. It makes no commits and edits no files in the repository.
