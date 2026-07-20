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

Confirm the scope with the user in one line before continuing. Know the limit of what prep can
do: `/code-review` chooses its own range and cannot be handed an arbitrary `git diff` range — its
argument (phase 0) is a PR number, branch name, or file path, nothing finer. So a **non-default**
pin (a custom base, or branch-diff-plus-working-tree) is a *recommendation* you pass to the user in
step 3, not something prep can force. The authoritative record of what was reviewed is whatever
`/code-review` states it looked at; prep's pin is the fallback the report uses only when the review
says nothing about its range.

## 2. Capture the metadata to a scratchpad carrier

Write the pinned scope and git metadata to a small file under the session scratchpad directory
listed in the system prompt. An on-disk carrier survives context compaction during a long review
and gives `/review-handoff` a source of truth better than fallible conversation memory:

```text
<scratchpad>/review-prep-<branch>.md
```

Sanitize `/` in the branch name to `-`; write it with the **Write tool** (never PowerShell
redirection — encoding corruption). Record:

- **Scope command and file count** — the exact diff command from step 1 and its `wc -l` count.
  This is the one datum that must not be reconstructed later (a re-derived range can misrepresent
  what was reviewed), so the on-disk copy matters most here.
- **Branch** — `git rev-parse --abbrev-ref HEAD`.
- **Full HEAD SHA** — `git rev-parse HEAD`. `/review-handoff` compares this against HEAD at
  transcription time; a mismatch means you committed between the review and the handoff, and the
  report must record *this* reviewed SHA, not the later one.
- **Short SHA** — `git rev-parse --short HEAD`, for the report title.

`/code-review` is read-only and does not move HEAD, so these values stay correct for the review as
long as *you* do not commit before running `/review-handoff`.

## 3. Hand the command back to the user

Final message:

1. The confirmed scope: the exact diff command and file count, and the path of the carrier file
   from step 2.
2. The command to run next, verbatim and copy-pasteable — `/code-review <effort>` (the effort from
   the argument, default `high`). Run it **bare**: do not add flags that make it act on findings
   (e.g. `--comment`, `--fix`) — the review must stay read-only; acting on findings is the receiving
   agent's job.
3. **If the pinned scope is non-default** (a custom base, or branch-diff-plus-working-tree), say so
   and tell the user to state that intended scope to `/code-review` — a bare command reviews its own
   default range, which would then differ from the pin. If the pin is just the default branch diff,
   the bare command already matches it and no extra instruction is needed.
4. The follow-up: **in this same session**, after `/code-review` finishes, run `/review-handoff` to
   capture its findings. The carrier file preserves the scope and SHAs across compaction, but the
   *findings themselves* live only in conversation context until the report is written — so a new
   session would still lose them.

Do **not** attempt to run `/code-review` yourself, and do not simulate its output. Stop after
this message and let the user run it.
