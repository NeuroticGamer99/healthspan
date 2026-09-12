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
redirection — encoding corruption).

**That filename is a hint, not proof of ownership**, and it matters more since this carrier gained
a tree hash. Flattening is not injective — `feat/x` and `feat-x` land on one name — and `/land`
step 7 records the wider case: on Windows even the unflattened path form collides, because
components lose trailing dots and spaces. So a stale carrier from a sibling branch sits at exactly
the expected path, and every value in it is individually plausible. Because `/apply-review`'s drift
classification keys on the tree hash below, a foreign carrier does not fail loudly — it answers
that check confidently and wrongly. The **Branch** field is what makes the case detectable:
`/review-handoff` compares it before trusting anything else here. Record it even though it looks
redundant against the filename; it is the one line in this file that is a check rather than a
convenience.

**The residual, stated rather than left implied** — `/land` step 7 states its own for the same
collision class, and an unstated one reads as a guarantee. This step still *writes* the carrier
unconditionally, so a colliding branch's prep run overwrites the earlier carrier and that file is
gone. What the `Branch` check buys is that the loss is never silent downstream: the overwritten
carrier fails `/review-handoff`'s comparison and the report falls back to its no-carrier rules,
rather than a foreign SHA and tree hash being recorded as this review's anchors. Detection, not
prevention — accepted here because a carrier is cheap to regenerate by re-running prep, whereas
`/land`'s message is not, which is why that one earned a sidecar and this one does not.

Record:

- **Scope command and file count** — the exact diff command from step 1 and its `wc -l` count.
  This is the one datum that must not be reconstructed later (a re-derived range can misrepresent
  what was reviewed), so the on-disk copy matters most here.
- **Branch** — `git rev-parse --abbrev-ref HEAD`.
- **Base SHA** — `git rev-parse "<the base from step 1>^{commit}"`. Step 1 pins the scope as a
  command, in which the base appears as a **ref** — `origin/main` by default, a branch name or tag
  when the user names one — and a ref is not a comparable value. `/review-brief` hands the
  adjudication lens a resolved SHA, so step 3 item 5's mismatch check has a SHA on one side and
  nothing on the other unless the base is resolved here, once.
  **Quote the whole revision**, as written. `/land` carries the measurement: unquoted, PowerShell's
  parser splits `^{commit}` off into an argument of its own and git answers about the wrong one —
  `git rev-parse` exits 128, and `git cat-file -e` exits 129 on ``unknown switch `n'``. Both shells
  pass the quoted form through intact.
  **What this records is what the ref meant at prep time**, which is the property the comparison
  wants rather than an incidental one: a later `git fetch` moving `origin/main` does not
  retroactively change the round's base, so the resolved value and not the ref name is what the
  carrier keeps — the same reasoning the tree hash below is kept for.
- **Full HEAD SHA** — `git rev-parse HEAD`. `/review-handoff` compares this against HEAD at
  transcription time; a mismatch means you committed between the review and the handoff, and the
  report must record *this* reviewed SHA, not the later one.
- **Short SHA** — `git rev-parse --short HEAD`, for the report title.
- **Tree hash** — `git rev-parse 'HEAD^{tree}'`. The SHA above anchors the review only while the
  branch's commits stand; `/ship`'s collapse (ADR-0069) rewrites a savepoint branch into one
  commit, leaving every recorded pre-collapse SHA dangling, and the tree is what survives that
  unchanged. It is the anchor `/apply-review` step 1 item 3 falls back on to tell an explained
  collapse from real drift, so a carrier without it reduces that check to a warning it cannot
  resolve.

  **Both anchors describe committed state only — say so when the tree is not clean.** Step 1
  explicitly permits reviewing uncommitted work, and neither `HEAD` nor `HEAD^{tree}` moves when
  the working tree does, so on a dirty tree these values name something the review did not look
  at. That is worse than a missing anchor: `/apply-review` step 1 item 3 would compare against
  them and answer *explained* on the strength of a value that never described the reviewed state,
  which is the confident-and-wrong outcome `/review-handoff` calls "worse than none". Prefer
  pinning a clean tree — a `/savepoint` before prep costs one commit and makes both anchors exact,
  which is the practice ADR-0069 institutes. If the scope must stay dirty, record that in the
  carrier beside the values rather than letting a committed-tree hash stand in for the reviewed
  one, and hash the uncommitted material explicitly: `git stash create` covers tracked
  modifications and untracked files need `git hash-object` per path.
  `.claude/reviewer-isolation.md` § The two invariants, invariant 1 owns that split and the
  reasons; do not restate them here.

`/code-review` is read-only and does not move HEAD, so these values stay correct for the review as
long as *you* do not commit before running `/review-handoff`.

## 3. Hand the command back to the user

Final message:

1. The confirmed scope: the exact diff command and the file count.
2. The step-2 carrier file, presented per `.claude/operator-handoff.md`. It is the only thing
   carrying the pinned SHAs forward if this session compacts, which is why it is handed over
   rather than merely written.
3. The command to run next, under the same contract — `/code-review <effort>`, with **the effort
   resolved to a literal** (from the argument, default `high`): `<effort>` is a placeholder, and
   a block reading `/code-review <effort>` is an unrunnable template however well it satisfies
   everything else. Run it **bare**: do not
   add flags that make it act on findings (e.g. `--comment`, `--fix`) — the review must stay
   read-only; acting on findings is the receiving agent's job.
4. **If the pinned scope is non-default** (a custom base, or branch-diff-plus-working-tree), say so
   and tell the user to state that intended scope to `/code-review` — a bare command reviews its own
   default range, which would then differ from the pin. If the pin is just the default branch diff,
   the bare command already matches it and no extra instruction is needed.
5. **The adjudication lens's command, relayed verbatim**, when this round was briefed (ADR-0072
   §1 — an external round runs two lenses and is not complete until both have reported).
   `/review-brief` (`.claude/skills/review-brief/SKILL.md`, step 6) composes the whole
   `/codex:adversarial-review` line with every value resolved and hands it over as a fenced block;
   reprint **that block**, unchanged, in a block of its own.
   Do not compose a line here: the values in it — the brief's absolute path, the pinned base SHA —
   belong to the briefing session, and a second spelling of the command in this file is a second
   thing to drift. If no brief reached this session, say the round is running **one** lens, rather
   than running one silently.

   **First compare that command's `--base` against the carrier's `Base SHA`, and stop on a
   mismatch.** Compare it against that recorded value and nothing else — the scope command names
   the base as a ref, and a ref against a SHA is not a comparison that can succeed or a mismatch
   that means anything. Relaying verbatim is right and is not sufficient: step 1 deliberately
   permits a custom base and a dirty-tree scope, so this session can pin a range the briefing
   session never saw — and the relayed command still carries the brief's base. Nothing downstream
   would notice.
   The adjudication lens would answer about one diff while `/code-review` looked at another, and
   the round's two reports would be merged as though they covered the same surface, which is worse
   than a round that ran one lens: the gap is invisible in both reports. The brief's base is the
   round's base (`/review-brief` step 4 pins it and its fragment records it), so on a mismatch this
   session is the deviation — **say so and stop, naming both SHAs**, rather than relaying a command
   the pin contradicts or silently re-pointing it. Re-briefing at the intended scope, or accepting
   the brief's, is the operator's call and needs their answer, not a guess. The repair is cheap and
   is the reason this fails closed: `/review-brief` is re-runnable, and a round compared across two
   diffs is not repairable after the fact.
6. The follow-up: **in this same session**, after `/code-review` finishes, run `/review-handoff` to
   capture its findings. The carrier file preserves the scope and SHAs across compaction, but the
   *findings themselves* live only in conversation context until the report is written — so a new
   session would still lose them.

Do **not** attempt to run `/code-review` yourself, and do not simulate its output. Stop after
this message and let the user run it.
