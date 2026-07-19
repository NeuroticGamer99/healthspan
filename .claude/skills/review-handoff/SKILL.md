---
name: review-handoff
description: Run /code-review at high effort and save the full findings as a self-contained scratchpad report whose path can be handed to another agent (received with /apply-review). Use when review detail must outlive this session.
---

# /review-handoff — run a high-effort review and save a portable report

Runs the built-in code-review skill, then captures everything it found in a standalone markdown
file under the session scratchpad directory — written for a reader with none of this
conversation's context, typically another agent session. Ends by printing the absolute path.

The report this produces is the input to `/apply-review`, which reads the file back and
implements the findings in a fresh session.

## 1. Run the review

Run the repo's code-review at high effort — the `/code-review high` command, which an agent
invokes via the Skill tool: `Skill(skill: "code-review", args: "high")`. If the user passed a
different effort level as an argument to `/review-handoff`, pass that through instead of `high`.

- Do **not** pass `--comment` or `--fix`. This command is read-only: findings go to the report
  file, and acting on them is the receiving agent's job.
- Follow the loaded code-review instructions fully, including any `ReportFindings` call it makes
  at the chosen effort. Not every effort makes one: the `high` default is inline-only (finder
  angles + dedup, **no verify pass**), so it emits findings as a JSON array and calls no
  `ReportFindings` — that is expected. The report file is written from whatever the review
  produced, not a replacement for it.

While reviewing, keep hold of what step 2 needs:

- the exact diff scope reviewed (base ref and the command that produced the diff)
- branch and HEAD — both representations: `git rev-parse --abbrev-ref HEAD`, `git rev-parse HEAD`
  (the **full** SHA the `Branch / HEAD` line records — `apply-review` compares against it), and
  `git rev-parse --short HEAD` for the report title
- areas examined that produced **no** findings

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
- Effort: <the effort actually run — `high` unless the user asked otherwise>
- Verification: <what actually ran — e.g. "high effort: 8 finder angles + dedup, no verify pass; the Verdict lines below are reviewer confidence, not machine-verified">

## Findings (most severe first)

### 1. <one-line summary>
- Where: `path/to/file.py:123`
- Category: correctness | simplification | efficiency | test-coverage | …
- Verdict: <reviewer's confidence — CONFIRMED / PLAUSIBLE — or omit if no verify pass ran; never assert a verified verdict the review did not produce>
- Failure scenario: <concrete inputs/state → wrong output or crash>
- Evidence: <the relevant lines, quoted in a code block>
- Suggested fix: <sketch — note that the suggestion itself has NOT been reviewed>

## Reviewed clean

<files/areas examined that produced no findings — so the reader knows silence is
coverage, not omission>

## Not covered

<anything the review skipped or could not reach, or "none">
```

**Verdicts are the reviewer's confidence, not machine output.** The `high` review runs no verify
pass, so it produces no CONFIRMED/PLAUSIBLE verdicts of its own. If you add a `Verdict` per finding
it is *your* confidence as the reviewing agent — label it (`CONFIRMED (reviewer confidence)`) and
make the top-level `Verification` line state exactly what ran. Never claim an adversarial-verify
pass that did not happen; the receiving `/apply-review` re-verifies every finding regardless, so
honesty here costs nothing. (A higher effort that does verify emits real verdicts — then say so.)

Zero findings still gets a report — the metadata plus "Reviewed clean" sections are exactly
what the next agent needs in order to not redo the work.

**Personal-data containment:** quote repository code only. If a finding involves anything under
`specs/personal/`, reference the path and describe the issue without quoting values — this file
is designed to travel between sessions.

## 3. Hand off

Final message to the user:

1. The absolute report path on its own line (it is the deliverable).
2. A 2–3 sentence digest: finding count, severity spread, and — if you assigned confidence verdicts — the CONFIRMED vs PLAUSIBLE split.
3. The hand-off phrasing: in the receiving session, run `/apply-review <report-path>` (or paste
   the path and tell the agent to read that file before touching the diff).
