---
name: review-prep
description: Pin and confirm the diff scope for a portable code review, capture the branch/HEAD metadata, and print the exact /code-review command to run next. Use before running /code-review when the findings will be handed off with /review-handoff.
---

# /review-prep — pin the scope, then hand the review back to the human

The opening half of the review-handoff flow. `/code-review` is a built-in Claude Code
command that **an agent cannot invoke** — its front-matter forbids agent calls, so a skill
that "runs" it only ends up simulating a review from its own reading of the diff, which is
worse than no review. The real command has to be run by you.

This skill does the part an agent *can* do: it fixes the exact diff scope that the review
should cover, records the git metadata the report will need, and tells you precisely what to
type. You then run `/code-review` yourself; afterwards `/review-handoff` (in this same
session) transcribes its findings into a portable report.

Argument: an optional effort level (e.g. `/review-prep high`). Default `high`. Whatever you
pass is echoed back in the command to run in step 3.

## 1. Establish the diff scope

Determine what the review should cover and state it as an exact command:

- Default to the branch diff against the trunk: `git diff origin/main...HEAD` (run
  `git fetch origin --quiet` first so the base is current).
- If the working tree has uncommitted changes the user means to review, or the user named a
  different base, use that instead — and say which you chose and why.
- Report the file count (`git diff --name-only <range> | wc -l`) so the scope is concrete.

Confirm the scope with the user in one line before continuing. `/code-review` will pick its
own default if run bare; pinning it here is the whole reason this step exists, so the report's
`Diff scope` line reflects what was actually reviewed rather than a guessed range.

## 2. Capture the metadata the report will need

`/code-review` is read-only and does not move HEAD, so this metadata stays valid through the
review. Capture it now so `/review-handoff` can transcribe it verbatim:

- `git rev-parse --abbrev-ref HEAD` — branch name
- `git rev-parse HEAD` — the **full** SHA (the `Branch / HEAD` line records this; `apply-review`
  compares against it)
- `git rev-parse --short HEAD` — short SHA for the report title

Keep these in the conversation — `/review-handoff` reads them back from context in this same
session. There is no on-disk carrier for them until the report is written.

## 3. Hand the command back to the user

Final message, three lines:

1. The confirmed scope: the exact diff command and file count.
2. The command to run next, verbatim and copy-pasteable — `/code-review <effort>` (the effort
   from the argument, default `high`).
3. The follow-up: **in this same session**, after `/code-review` finishes, run `/review-handoff`
   to capture its findings into the portable report. Note the same-session requirement plainly —
   the handoff reads the review out of conversation context, so a new session would lose it.

Do **not** attempt to run `/code-review` yourself, and do not simulate its output. Stop after
this message and let the user run it.
