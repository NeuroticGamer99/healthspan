---
name: squash-merge
description: Squash-merge the current branch's PR with a clean composed commit message — gates verified green, no bot finding left unanswered, body written to a file and passed via --body-file <path>, result diffed against origin/main. Use when the user asks to merge after reviews are clean.
---

# /squash-merge — merge the PR with a clean squash message

The last step of the chain: `/land` → `/ship` → the review chains the user chose to run
(`/coderabbit-review`, `/greptile-review`, `/copilot-review` — reviews are opt-in per PR) →
`/squash-merge`. Invoking it is the user's approval to merge — but never past a red or pending
gate. Stop and report at any step that fails.

## 1. Preconditions

- An open PR exists for the current branch: `gh pr view --json number,title,state,headRefName`.
  If not, stop and say so.
- **Local state matches the remote PR**: `git fetch origin`, then confirm the working tree is
  clean and `git rev-parse HEAD` equals the PR's `headRefOid` (`gh pr view <N> --json
  headRefOid`). A mismatch means the message would be composed from commits the PR doesn't have
  (or miss ones it does); stop and reconcile. The fetch also refreshes `origin/main`, which
  step 3's `rev-list` depends on — a stale tracking ref mis-picks the first commit.
- **All checks green**: `gh pr checks <N>` must exit 0. Exit code 8 means checks are still
  *pending* — that is not green; watch in the background (`gh pr checks <N> --watch`,
  `run_in_background: true`) and merge only when everything has passed. A red check stops the
  merge outright.
- **No bot finding is unanswered** — run this, do not eyeball it. The PR's `createdAt` is the
  floor, so every round of every reviewer is in scope:

  ```bash
  uv run --locked python scripts/bot_review.py outstanding --pr <N> --since <PR createdAt>
  ```

  **Exit 2 clears this gate**: every finding any bot posted has a threaded reply from someone
  other than the bot itself. **Exit 0 stops the merge** — it prints each unanswered finding with
  its path, id and URL, which is exactly what this gate exists to catch. **Exit 1 also stops it** —
  read the output to know which kind. Under a `CANNOT CLEAR THE GATE` banner the sweep found a
  zero it could not *prove*: a bot posted a review but none of its comments matched the author
  filter, a summary states more findings than were matched, a summary does not read clean while
  nothing at all was posted to match, a bot renders findings in its review body where no reply can
  reach them, or a bot declared to review every PR left no artifact at all
  (no bot declares that today — Greptile did until `skipReview: "AUTOMATIC"`).
  Those are findings the sweep could not read, not findings that do not exist. Where the findings
  live in prose — a summary stating more than its comments, a not-clean summary with nothing posted
  to match, a review rendering findings in its
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
  `NO FINDINGS POSTED` rather than `NOTHING OUTSTANDING`, precisely because a chain that never ran and
  a clean run are indistinguishable from the comments alone.

- **Every review that was triggered has answered.** This is a separate precondition and it is
  yours to track, not the sweep's. A chain that was requested and is still pending stops the
  merge; a chain deliberately not run has nothing to wait for, and every chain here is now
  opt-in per PR — Greptile included, since `skipReview: "AUTOMATIC"`. Say plainly which reviewers
  ran and merge on the user's explicit say-so. **The sweep cannot do this for you**: it reads
  posted comments, so a chain that never ran and a clean run are indistinguishable to it, and nothing
  now reviews unasked to make silence anomalous.

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

## 2. Collapse the review ledger

Every external review round left a fragment at
`specs/reviews/angle-ledger/branches/<b6>/round-<N>.md`, committed and tracked. They exist so two
open branches never contend for a path; that need ends here, and `main` keeps **one digest per
merged PR and no fragments** (ADR-0072 §8). Uncollapsed, they reach several thousand files a year
and every one of them is walked by the link check, PyMarkdown, the containment history scan, the
containment gate's live-repository test **on all three OS legs**, and every later round's own
review diff.

```bash
python3 scripts/ledger.py collapse --pr <N>
```

It is idempotent and safe to re-run, and it refuses rather than guesses: an uncommitted fragment, a
fragment this branch did not add, or one whose content the existing digest does not hold each stop
it with the reason on stderr and a non-zero exit.

**Read what it printed and follow that row's "Then" column** — no count is given here on purpose,
because the one that was here went stale the moment `collapse` grew another return, and the row's
own action is the thing you need anyway:

| It printed | What it means | Then |
|---|---|---|
| `nothing to collapse` | this branch wrote no round fragments — the common case, since most PRs run no external round | skip the rest of step 2 **and** step 5's read-back |
| `already collapsed` | a previous run finished; the digest is there and the fragments are gone (if their directory lingered behind them, empty, this run removed it — nothing git tracks) | skip the rest of step 2; step 5's read-back still applies |
| `finished an interrupted collapse` | a previous run crashed between writing the digest and deleting the fragments; this run finished the deletion | commit and push below |
| `collapsed round(s) …` | the ordinary first run | commit and push below |

Naming only the first of those is what sends the operator into `git commit` with nothing staged,
which exits 1 in the middle of a checklist whose rule is to stop and report at any step that fails.

**Match on the prefix, and expect more messages than rows.** `collapse` returns more distinct
messages than this table has rows, deliberately: several can open with the same label when they
share its action — `already collapsed` covers both a digest whose fragments are already gone and
one that additionally removed the empty directory they left behind. So read the label, not the
whole line. No count of either appears here, because a count in this file is a claim about code in
another one; the mapping is pinned instead, in **both** directions, by
`tests/test_ledger.py::test_every_collapse_outcome_is_named_in_the_skill_and_every_row_is_real` —
it reads the labels out of the table above and the messages out of `collapse`'s own returns, so a
new message with no row here, and a row here matching no message, each redden. One arrived
unannounced before that existed: the empty-directory case was labelled `finished an interrupted
collapse`, which this table routes to `git add`/`git commit`, and it stages nothing.

**The collapse is the branch's last commit, and it must be pushed before the merge.** The digest
has to be *in* the merged content: `main`'s history is append-only and PR-mediated, so there is no
post-merge commit to put it in. Committing it here also means the digest is lint-gated and
link-gated by the PR that introduces it, which is what ADR-0072 §8's "clean by construction,
permanently" actually rests on.

```bash
git add -A -- specs/reviews/angle-ledger
git commit -m "Collapse the review ledger into the PR #<N> digest"
git push
```

`git add -A` is scoped to one pathspec deliberately: the collapse both adds a file and deletes a
directory, and a bare `git add -A` would sweep the whole tree — the shape `/savepoint` forbids for
the same reason.

**Then re-verify the two preconditions this push invalidated**: `gh pr checks <N>` must go green
again on the new head, and `git rev-parse HEAD` must equal the refreshed `headRefOid`. Do not
carry the earlier green forward — it belongs to a commit that is no longer the head. The bot sweep
does not need re-running: the collapse posts no findings and answers none.

## 3. Compose the squash message

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

  **A collapse in step 2 belongs in that line**, and it is content rather than bookkeeping: the
  digest is a file landing on `main` that no other commit accounts for, and a reader of `main` has
  only this message. Name it and the rounds it captured — "Includes the review-ledger collapse
  (rounds 1–4 → `specs/reviews/angle-ledger/digests/0/pr96.md`)". A step-2 run that printed
  `nothing to collapse` has nothing to say here.

  **The body must describe the merged state, not the first attempt.** The first commit's body is
  the *base*, not a quotation: where a later commit invalidated a claim it makes, correct that
  claim rather than reproducing it. The "Includes …" line records what rode along; it does not
  neutralize a sentence the branch went on to contradict, and a reader of `main` has only this
  message. Read the branch diff (`git diff origin/main...HEAD`), not just the first commit, and
  reconcile every claim against it before composing.

  **Read `<scratchpad>/squash-reconciliation/<branch>.md` first if it exists.** A session that
  triaged the bot rounds already had the evidence in hand and may have written the audit down:
  which claims later commits falsified, which survived and why, and what the "Includes …" line
  should say. Use it as the *input* to the reconciliation, never as a substitute for it — it was
  written at some commit, and anything pushed since is outside it, so check `git log` for commits
  later than the audit's stated anchor. Its absence means nothing has been audited, not that
  nothing moved: do the reconciliation from the branch diff, as below.

  The filename is the exact `git rev-parse --abbrev-ref HEAD`, unsanitized, matching `/land`'s
  `commit-msg/<branch>.txt` convention and inheriting its collision hazard — `/land` step 7
  records three measured mechanisms by which a file at the expected path can be another branch's.
  **The audit must therefore name its own branch and PR in its opening line**, and an audit that
  names neither is not trusted: a file whose stated branch or PR does not match the one being
  merged is stale, and so is one that states nothing. Without that requirement the guard is
  unenforceable — nothing else obliges a writer to include an identifier to check.

  **`/land` guards the same hazard with a checked value — a `.branch` sidecar — and this
  deliberately does not, for a reason worth stating rather than leaving as an unexplained
  divergence.** The sidecar protects the commit message, which *ships*: a wrong one reaches
  `main` and cannot be recalled. This file never ships. It is an input to a reconciliation that
  is performed from the branch diff regardless, so the worst a wrong or stale one costs is a
  re-derivation you were going to be capable of anyway. A weaker guard is proportionate to a
  smaller loss; it would not be proportionate on the message.

  If the audit was written in an earlier session it lives under *that* session's scratchpad; ask
  the user for the path rather than reconstructing it, exactly as `/ship` step 1 does for the
  commit message.

  This is not hypothetical: on PR #65 the review findings were about the entry's *own* accuracy,
  so the fixes falsified three of the first commit's sentences — an unqualified quota rate, a
  posture the branch had since softened, and a citation it had removed. Verbatim reuse would have
  put all three permanently on append-only `main`. Where a correction is judgment rather than
  fact — the claim is arguable, or rewriting it would change what the user approved at `/land` —
  surface the fork and let the user pick; everything else, fix and say so in the report.

## 4. Merge

**Write the composed body to a file first**, then pass that path — do not pipe it in from a
heredoc. `--body-file` takes a real path as happily as `-`, and keeping the composed text on disk
is what makes step 5's comparison an actual diff instead of a spot-check. A body that exists only
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
whatever bytes the file holds, and the damage is unrecoverable afterwards: step 5 forbids
force-pushing `main` to repair a bad message.

If you do pipe it, **`--body-file -` is the only stdin form and the heredoc delimiter must be
quoted** (`<<'EOF'` — unquoted, the shell runs command substitution on backticks and expands `$`
inside the message). `--body -` is accepted without error and sets the literal one-character
string `-` as the commit body — `gh` does not follow `git commit -F -` conventions. This exact
mistake shipped PR #43's squash commit (`97e43ce`, 2026-07-20) with a body of "`-`", and the same
flag pair exists on `gh pr create` (see `/ship`, which carries the same rule for PR bodies).

## 5. Verify — mandatory

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

**Read the ledger back off `origin/main`, not off your working tree.** A collapse that ran but
never reached the merge leaves `main` carrying the fragments it was supposed to remove, and the
local tree — where the collapse ran — looks correct either way:

```bash
git ls-tree -r --name-only origin/main -- specs/reviews/angle-ledger/
```

The branch's `branches/<b6>/` must be absent and the PR's digest present. CI's
`ledger-collapsed` gate asserts the fragment half of that on the next push to `main`, which catches
a merge that skipped step 2 entirely; this catches it one step earlier, while you still know which
PR it was, and it is the only check that also confirms the digest arrived. Skip it only when step 2
printed `nothing to collapse` — there is no digest to look for in that case, and `already
collapsed` is not the same answer.

## 6. Report

The merged SHA, confirmation the message verified, and the next queued step (worklist item or
phase work item) if one is on deck.
