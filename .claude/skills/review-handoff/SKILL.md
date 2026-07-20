---
name: review-handoff
description: Capture the findings from a /code-review the user just ran and save them as a self-contained scratchpad report whose path can be handed to another agent (received with /apply-review). Run in the same session, right after /code-review. Use when review detail must outlive this session.
---

# /review-handoff — capture a just-run review into a portable report

The closing half of the review-handoff flow. `/review-prep` pins the scope and you run
`/code-review <effort>` yourself — `/code-review` is a built-in command an agent cannot invoke,
so the human runs the real review. This skill transcribes what that review just produced,
in this conversation, into a standalone markdown file under the session scratchpad directory —
written for a reader with none of this conversation's context, typically another agent session.
It ends by printing the absolute path.

Because it reads the review out of conversation context, **it must run in the same session
where you ran `/code-review`.** There is no on-disk artifact to recover the findings from
otherwise.

The report this produces is the input to `/apply-review`, which reads the file back and
implements the findings in a fresh session.

## 1. Locate the review just run

Find the `/code-review` output earlier in this conversation — the findings it produced, whether
as a `ReportFindings` call (at efforts that run a verify pass) or as an inline JSON array / text
list (the `high` default is inline-only: finder angles + dedup, **no verify pass**, so it emits
findings and calls no `ReportFindings` — that is expected, not a gap).

- If no `/code-review` was run in this session, stop and tell the user: run `/review-prep`, then
  `/code-review <effort>`, then this skill — all in one session. Do **not** review the diff
  yourself to fill the gap; a simulated review is exactly what this flow exists to avoid.
- Transcribe what the review actually found. Do not add, drop, or re-judge findings — your job
  is faithful capture, not a second review.

Also recover the scope and metadata the report records. First read `/review-prep`'s carrier file if
it exists — `<scratchpad>/review-prep-<branch>.md` in this session's scratchpad directory (branch
`/` sanitized to `-`); it is the on-disk source of truth that survives compaction. Then:

- **Diff scope** — prefer the range `/code-review` itself stated it reviewed; that is what was
  actually inspected. Fall back to the carrier file's pinned scope only when the review said nothing
  about its range. If both exist and **disagree**, record the review's stated range and note the
  discrepancy in the report (the pin was a recommendation prep could not enforce — see `/review-prep`
  step 1). Do **not** reconstruct the scope by re-running `git diff` — a re-derived range can
  misrepresent what was reviewed. If neither source pins it (prep was skipped and the review stated
  no range), record the scope as `unknown — not pinned`, say so in the digest, and recommend
  rerunning via `/review-prep`.
- **Branch and HEAD** — prefer the carrier file's captured SHA, then compare it against a fresh
  `git rev-parse HEAD`. If they **match**, record that SHA. If they **differ**, you committed between
  the review and this handoff: record the carrier's (reviewed) SHA — not the current one — and flag
  the drift in the report, so `/apply-review`'s HEAD comparison is not silently satisfied by a SHA
  the review never saw. Only with **no** carrier file, re-derive `git rev-parse --abbrev-ref HEAD` /
  `HEAD` / `--short HEAD` (correct only if you did not commit since the review).
- **Areas reviewed clean** — take these from the review's own output *only if it enumerated them*.
  A bare findings list (inline JSON or `ReportFindings`) names findings, not clean areas; when the
  review did not state its clean coverage, write "not stated by the review" rather than inferring it
  from the changed-file list. Do not manufacture a coverage guarantee the review did not make.

## 2. Write the report

Write the file with the **Write tool** (never PowerShell redirection — encoding corruption) to
the scratchpad directory listed in the system prompt:

```text
<scratchpad>/code-review-<branch>-<timestamp>.md
```

Sanitize `/` in the branch name to `-`; take the timestamp from Bash `date -u +%Y%m%d-%H%M%S`.

The report must be self-contained: assume the reader has the repo checked out at the same HEAD
but has seen neither this conversation nor the review's own output. Structure:

```markdown
# Code review report — <branch> @ <short sha>

- Generated: <UTC timestamp>
- Branch / HEAD: <branch> / <full sha>
- Diff scope: `<exact command, e.g. git diff origin/main...HEAD>` — <N> files changed (or `unknown — not pinned` when neither the review nor a prep carrier fixed the range)
- Effort: <the effort actually run — `high` unless the user chose otherwise>
- Verification: <what actually ran — e.g. "high effort: finder angles + dedup, no verify pass; the Verdict lines below are the review's confidence, not machine-verified">

## Findings (most severe first)

### 1. <one-line summary>
- Where: `path/to/file.py:123`
- Category: correctness | simplification | efficiency | test-coverage | …
- Verdict: <the review's own verdict if it ran a verify pass — CONFIRMED / PLAUSIBLE — or omit if none ran; never assert a verified verdict the review did not produce>
- Failure scenario: <concrete inputs/state → wrong output or crash>
- Evidence: <the relevant lines, quoted in a code block>
- Suggested fix: <sketch — note that the suggestion itself has NOT been separately reviewed>

## Reviewed clean

<files/areas the review stated it examined with no findings — so the reader knows silence is
coverage, not omission. If the review did not enumerate clean areas, write "not stated by the
review" here; do not infer coverage from the changed-file list.>

## Not covered

<anything the review skipped or could not reach, or "none">
```

**Transcribe the review's verdicts; don't invent them.** Efforts that run a verify pass emit
real CONFIRMED/PLAUSIBLE verdicts — carry those through and say so in the `Verification` line.
The `high` default runs no verify pass, so it produces no verdicts; in that case omit the
`Verdict` field (or, if you add your own reading, label it `reviewer confidence` and state in
`Verification` that no machine verify ran). Never claim an adversarial-verify pass that did not
happen — the receiving `/apply-review` re-verifies every finding regardless, so honesty here
costs nothing.

Zero findings still gets a report — the metadata plus "Reviewed clean" sections are exactly
what the next agent needs in order to not redo the work.

**Personal-data containment:** quote repository code only. If a finding involves anything under
`specs/personal/`, reference the path and describe the issue without quoting values — this file
is designed to travel between sessions.

**Short alias copy.** The scratchpad path embeds the project slug and session GUID (~130 chars) —
unusable to type or copy by hand. So after writing the canonical file, write the *same content* to a
short fixed path in the temp dir: resolve `%TEMP%` (`$env:TEMP` in PowerShell) and write
`<TEMP>\review-report.md`, overwriting any previous alias (Write tool, not redirection). This alias
is **clobbered by the next review** — the timestamped scratchpad file stays canonical. Hand the
alias out for convenience; give the timestamped path when a durable reference is needed.

## 3. Hand off

Final message to the user:

1. Both report paths: the canonical timestamped scratchpad file (the deliverable a later review
   won't overwrite) and the short alias `<TEMP>\review-report.md` (convenient, but clobbered by the
   next review).
2. A 2–3 sentence digest: finding count, severity spread, and — if the review ran a verify pass —
   the CONFIRMED vs PLAUSIBLE split.
3. The hand-off command in a **fenced code block** — the VS Code chat webview renders assistant
   prose as unselectable text, but a code fence gets a hover *copy* button. Print the full command
   with the **resolved absolute** alias path (resolve `%TEMP%`; do not print the literal `%TEMP%`
   token — the receiving agent would have to expand it):

   ```text
   /apply-review <TEMP>\review-report.md
   ```

   In the receiving session, running that reads the report back (or paste the path and tell the
   agent to read the file before touching the diff). Give the canonical timestamped path alongside
   for a reference the next review won't overwrite.
