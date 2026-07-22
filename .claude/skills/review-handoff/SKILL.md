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
  the review never saw. With **no** carrier file, do **not** substitute the current checkout: a fresh
  `git rev-parse HEAD` identifies HEAD *now*, which cannot prove it is the commit `/code-review`
  inspected — a mid-session commit would make it wrong and silently satisfy `/apply-review`'s check,
  the exact failure the carrier exists to prevent. Use a reviewed SHA only if the review's own output
  named one; otherwise record the SHA as `unknown — prep skipped`, say so in the digest, and
  recommend rerunning via `/review-prep` for a drift-checkable report. The branch name is still safe
  to re-derive (`git rev-parse --abbrev-ref HEAD`); it is the SHA that must not be fabricated.
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
- Branch / HEAD: <branch> / <full sha, or `unknown — prep skipped` when no carrier and the review named no commit>
- Diff scope: `<exact command, e.g. git diff origin/main...HEAD>` — <N> files changed (or `unknown — not pinned` when neither the review nor a prep carrier fixed the range). Give `<N>` only when the review stated it, or a carrier whose pinned range matches the reviewed range supplies it; otherwise write `file count not stated by the review` — never recompute it (that reconstructs scope), and never reuse a carrier count whose range disagrees with the reviewed range.
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

The timestamped scratchpad file is the single canonical report — do not copy it anywhere else. Its
path is long (project slug + session GUID), but that is fine: the hand-off command below goes out in a
fenced code block whose *copy* button transfers it in one click, so it never has to be typed by hand.

## 3. Hand off

Final message to the user:

1. The canonical timestamped scratchpad report path — resolved and absolute, with no
   `<scratchpad>` / `<branch>` / `<timestamp>` placeholders left in it.
2. A 2–3 sentence digest: finding count, severity spread, and — if the review ran a verify pass —
   the CONFIRMED vs PLAUSIBLE split.
3. The hand-off command, emitted **inside a fenced `text` code block** — the VS Code chat webview
   renders assistant prose as unselectable text, but a code fence gets a hover *copy* button. The
   block must contain exactly `/apply-review` followed by the **resolved absolute** path to the
   canonical report (item 1 above), in **double quotes** so a path containing spaces reaches
   `/apply-review` as a single argument — nothing else. Resolve every part of the path first: the
   fenced block is the user's copy target, so it must be a runnable command, never a template — if
   any `<scratchpad>`, `<branch>`, or `<timestamp>` placeholder survives into it, the receiving
   session gets an unreadable path. In the receiving session, running that command reads the report
   back (or the user pastes the path and tells the agent to read the file before touching the diff).
