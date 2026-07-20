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

Also recover, from `/review-prep`'s output earlier in the conversation (or by re-running the
commands if prep was skipped):

- the exact diff scope reviewed (the base ref and the command that produced the diff)
- branch and HEAD — `git rev-parse --abbrev-ref HEAD`, the **full** `git rev-parse HEAD` (the
  `Branch / HEAD` line records it — `apply-review` compares against it), and
  `git rev-parse --short HEAD` for the title
- areas examined that produced **no** findings (from the review's own output)

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
- Diff scope: `<exact command, e.g. git diff origin/main...HEAD>` — <N> files changed
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

<files/areas examined that produced no findings — so the reader knows silence is
coverage, not omission>

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

## 3. Hand off

Final message to the user:

1. The absolute report path on its own line (it is the deliverable).
2. A 2–3 sentence digest: finding count, severity spread, and — if the review ran a verify pass —
   the CONFIRMED vs PLAUSIBLE split.
3. The hand-off phrasing: in the receiving session, run `/apply-review <report-path>` (or paste
   the path and tell the agent to read that file before touching the diff).
