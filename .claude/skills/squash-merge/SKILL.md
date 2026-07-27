---
name: squash-merge
description: Squash-merge the current branch's PR with a clean composed commit message — gates verified green, no bot finding left unanswered, body written to a file and passed via --body-file <path>, result diffed against origin/main. Use when the user asks to merge after reviews are clean.
---

# /squash-merge — merge the PR with a clean squash message

The last step of the chain: `/land` → `/ship` → the review chains the user chose to spend
(`/coderabbit-review`, `/copilot-review` — reviews are opt-in per PR, except Greptile, which
reviews every PR unasked and is collected with `/greptile-review`) → `/squash-merge`. Invoking
it is the user's approval to merge — but never past a red or pending gate. Stop and report at any
step that fails.

## 1. Preconditions

- An open PR exists for the current branch: `gh pr view --json number,title,state,headRefName`.
  If not, stop and say so.
- **Local state matches the remote PR**: `git fetch origin`, then confirm the working tree is
  clean and `git rev-parse HEAD` equals the PR's `headRefOid` (`gh pr view <N> --json
  headRefOid`). A mismatch means the message would be composed from commits the PR doesn't have
  (or miss ones it does); stop and reconcile. The fetch also refreshes `origin/main`, which
  step 2's `rev-list` depends on — a stale tracking ref mis-picks the first commit.
- **All checks green**: `gh pr checks <N>` must exit 0. Exit code 8 means checks are still
  *pending* — that is not green; watch in the background (`gh pr checks <N> --watch`,
  `run_in_background: true`) and merge only when everything has passed. A red check stops the
  merge outright.
- **No bot finding is unanswered** — run this, do not eyeball it. The PR's `createdAt` is the
  floor, so every round of every reviewer is in scope:

  ```bash
  uv run python scripts/bot_review.py outstanding --pr <N> --since <PR createdAt>
  ```

  **Exit 2 clears this gate**: every finding any bot posted has a threaded reply from someone
  other than the bot itself. **Exit 0 stops the merge** — it prints each unanswered finding with
  its path, id and URL, which is exactly what this gate exists to catch. **Exit 1 also stops it** —
  read the output to know which kind. Under a `CANNOT CLEAR THE GATE` banner the sweep found a
  zero it could not *prove*: a bot posted a review but none of its comments matched the author
  filter, a summary states more findings than were matched, a bot renders findings in its review
  body where no reply can reach them, or a bot that reviews every PR left no artifact at all.
  Those are findings the sweep could not read, not findings that do not exist. Where the findings
  live in prose — a summary stating more than its comments, a review rendering findings in its
  body, and (for a summary-comment bot only) a review whose inline comments never landed — the
  banner prints the exit: an `Acknowledges <bot> (summary|review) <id>` PR-level comment answering
  them (`.claude/bot-review-triage.md` §2, ADR-0067) — an artifact so acknowledged no longer
  blocks, so re-running the sweep after posting it is how the gate clears. **Without** the
  banner, exit 1 is an ordinary failure — `gh` auth, a network timeout, a usage error — and the
  gate simply did not run. Both stop the merge; only one is about the PR. It reports every bot
  including the ones that found nothing, so a zero is visibly a zero rather than a silence.

  This sweeps *posted comments*, which bounds what it can prove. It answers "is any finding
  unread"; it cannot answer "did the review I asked for ever arrive", because a review still in
  flight has posted nothing to find. When every bot reports zero findings the command says
  `NO FINDINGS POSTED` rather than `NOTHING OUTSTANDING`, precisely because an unspent chain and
  a clean run are indistinguishable from the comments alone.

- **Every review that was triggered has answered.** This is a separate precondition and it is
  yours to track, not the sweep's. A chain that was requested and is still pending stops the
  merge; a chain deliberately not spent has nothing to wait for (reviews are opt-in per PR, and
  Greptile is never legitimately unspent since its App reviews every PR unasked) — say plainly
  which reviewers ran and merge on the user's explicit say-so.

  **Do not delegate this to `gh pr checks`.** Its coverage is partial and bot-specific: CodeRabbit
  posts a commit *status*, Copilot and Greptile post *check-runs*, and Gemini posts nothing at all
  — its workflow is dispatched against `main` (ADR-0064), so its run is associated with the
  default branch rather than the PR head and can never appear in the PR's checks. A green check
  list is not evidence that a requested review landed.

  Judgment still belongs to `.claude/bot-review-triage.md`; the sweep only proves nothing was
  *silently* skipped.

  **Staleness is not a blocker.** Fix commits land after a review by definition, so a triaged PR
  reaches this step with its reviews a commit or two behind. That means the merged code may not
  have been reviewed, which is worth saying out loud; it is not an unmet precondition. Re-trigger
  a lens if you want a verdict on the final state.
- The user has asked for the merge in this session. `/ship` and `/copilot-review` never merge;
  neither does this skill uninvited.

## 2. Compose the squash message

GitHub's default squash message concatenates every branch commit — the `/land`-approved message
jumbled together with "address review" fixups. Always replace it:

- **Subject**: the PR title plus ` (#<N>)`.
- **Body**: the body of the branch's *first* commit — the message `/land` proposed and the user
  approved:

  ```bash
  first=$(git rev-list origin/main..HEAD | tail -1)
  git log -1 --format=%b "$first"
  ```

  Keep its `Decisions:` section and co-author trailer intact. If fix commits followed
  (`git rev-list --count origin/main..HEAD` greater than 1), insert one line above the
  `Decisions:` section noting what rode along — "Includes bot-review fixes (…)" — never the fixup
  messages themselves. If the first commit's body is somehow empty, compose from the PR
  description's "What landed" and `Decisions:` sections instead.

  **The body must describe the merged state, not the first attempt.** The first commit's body is
  the *base*, not a quotation: where a later commit invalidated a claim it makes, correct that
  claim rather than reproducing it. The "Includes …" line records what rode along; it does not
  neutralize a sentence the branch went on to contradict, and a reader of `main` has only this
  message. Read the branch diff (`git diff origin/main...HEAD`), not just the first commit, and
  reconcile every claim against it before composing.

  This is not hypothetical: on PR #65 the review findings were about the entry's *own* accuracy,
  so the fixes falsified three of the first commit's sentences — an unqualified quota rate, a
  posture the branch had since softened, and a citation it had removed. Verbatim reuse would have
  put all three permanently on append-only `main`. Where a correction is judgment rather than
  fact — the claim is arguable, or rewriting it would change what the user approved at `/land` —
  surface the fork and let the user pick; everything else, fix and say so in the report.

## 3. Merge

**Write the composed body to a file first**, then pass that path — do not pipe it in from a
heredoc. `--body-file` takes a real path as happily as `-`, and keeping the composed text on disk
is what makes step 4's comparison an actual diff instead of a spot-check. A body that exists only
inside a heredoc cannot be compared against what landed without retyping it, which compares your
memory rather than your message.

```bash
msg=<scratchpad>/squash-body.txt   # write the composed body here first
gh pr merge <N> --squash --delete-branch --subject "<subject>" --body-file "$msg"
```

**Write that file as UTF-8 without a BOM** — use the Write tool, or PowerShell's
`[System.IO.File]::WriteAllText($path, $content, [System.Text.UTF8Encoding]::new($false))`.
Every commit message in this repo is full of em dashes, and `Set-Content` without `-Encoding UTF8`
writes cp1252 and corrupts them silently (`CLAUDE.md`, PowerShell file encoding). `gh` ships
whatever bytes the file holds, and the damage is unrecoverable afterwards: step 4 forbids
force-pushing `main` to repair a bad message.

If you do pipe it, **`--body-file -` is the only stdin form and the heredoc delimiter must be
quoted** (`<<'EOF'` — unquoted, the shell runs command substitution on backticks and expands `$`
inside the message). `--body -` is accepted without error and sets the literal one-character
string `-` as the commit body — `gh` does not follow `git commit -F -` conventions. This exact
mistake shipped PR #43's squash commit (`97e43ce`, 2026-07-20) with a body of "`-`", and the same
flag pair exists on `gh pr create` (see `/ship`, which carries the same rule for PR bodies).

## 4. Verify — mandatory

A zero exit is not a clean merge. Refresh the tracking ref (do not trust `gh pr merge` to have
fast-forwarded it — that behavior is incidental, not guaranteed), then compare the **whole
message, subject included**, against the expected form built from the same file you merged with:

```bash
git fetch origin main
{ printf '%s\n\n' "<subject>"; cat <scratchpad>/squash-body.txt; } > <scratchpad>/expected.raw
git log --format=%B -1 origin/main                                > <scratchpad>/landed.raw

# Normalize BOTH sides identically before comparing: `$(cat …)` drops every
# trailing newline and `printf '%s\n'` puts exactly one back. Without this the
# diff fails on a merge that landed perfectly — `git log %B` appends a newline
# and a composed file may end with none (or several), and the step's own stated
# tolerance ("trailing whitespace is the only permitted difference") would then
# be contradicted by the command enforcing it.
for f in expected landed; do
  printf '%s\n' "$(cat <scratchpad>/$f.raw)" > <scratchpad>/$f.txt
done
diff <scratchpad>/expected.txt <scratchpad>/landed.txt
```

Verified against a real commit in both failure shapes — a body file with no
final newline and one with extra trailing blanks — and confirmed still to catch
a wrong subject and an altered body line.

A body-only comparison leaves the subject unverified, and the subject is the line carrying the
`(#<N>)` reference and the one most likely to be retyped by hand.

**Diff it; do not spot-check it.** Comparing a few phrases proves the phrases you thought of
survived, which is the weaker half — the failure mode is a line you did *not* think to check.
Trailing whitespace is the only permitted difference (`%B` appends a newline and GitHub may
normalize trailing blanks); every content line must match. This read-back is what caught
`97e43ce`. `origin/main` is the authoritative check.

When a claim was corrected during the branch, also assert the **superseded wording is absent** —
`grep -c` on the old string must be `0`. A diff proves the new text landed; only this proves the
text it replaced did not survive alongside it. Then
sync local `main` explicitly rather than trusting gh's incidental fast-forward:

```bash
git checkout main && git merge --ff-only origin/main
```

(a no-op when gh already fast-forwarded it), and confirm the feature branch is gone. If the
message is wrong, stop and report what the body actually says — never force-push `main` to
repair it; `main`'s history is append-only and the fix is the user's call.

## 5. Report

The merged SHA, confirmation the message verified, and the next queued step (worklist item or
phase work item) if one is on deck.
