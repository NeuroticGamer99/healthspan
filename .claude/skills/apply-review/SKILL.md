---
name: apply-review
description: Read a code-review report produced by /review-handoff and implement its findings — re-verifying each against the current code before fixing, since the report's suggested fixes are unreviewed and the code may have drifted. Use in a fresh session to act on a handed-off report.
---

# /apply-review — implement the findings from a review report

The receiving half of the review-handoff flow: `/review-prep` pins the scope, the user runs
`/code-review` themselves, and `/review-handoff` writes the findings into a portable report.
This skill reads that report back and does the work. Assume the report was written by an
agent that could not see this session, and that **its suggested fixes were never reviewed** —
they are hints, not instructions. Your job is to re-confirm each finding against the live code,
then fix what genuinely needs fixing.

Argument: the report path (e.g. `/apply-review <scratchpad>/code-review-<branch>-<ts>.md`).

**The git recipes in this file are Bash — run them with the Bash tool, not PowerShell**, the same
declaration `/land` and `/ship` carry and for the same reasons. The construct that needs it is
**step 4's round-number query**: `[ -n "$mb" ]`, `|| { …; exit 1; }`, and a `grep`/`sort`/`tail`
pipeline, none of which survive PowerShell's parser. A guard that fails to parse is a guard that
does not run, and here that means a wrong round number written into the branch's history — the
outcome `/savepoint` step 3 calls worse than none. Step 1's classification is a different case and
is portable on its own: bare `git` invocations whose revisions are quoted whole, which both shells
pass through intact. Its PowerShell hazard is the *unquoted* `^{commit}` form, called out where it
arises rather than here.

## 1. Load the report

- If a path was passed, Read it — the normal case, since `/review-handoff` hands the report's
  absolute path back as a copyable block for exactly this hand-off (`.claude/operator-handoff.md`).
  If no path was passed, do **not** glob only your own
  session's scratchpad: the report was written by a *different* session and the scratchpad path is
  session-specific (a per-session UUID), so yours will not contain it. Instead search across the
  sibling session scratchpads — go up from your session's scratchpad to the per-project directory
  and match `*/scratchpad/code-review-*.md` — take the newest, name the file **under that same
  contract** and say which session it came from, and confirm with the user before using it, since
  it may be an unrelated review. The contract is not decoration on this path: you are asking the
  user to judge a candidate they did not choose, and a path abbreviated in prose is one they
  cannot open to check.
- Read the report's **Branch / HEAD** and **Diff scope** lines, then run `git rev-parse HEAD`
  and `git rev-parse --abbrev-ref HEAD`. Compare on the **full** SHA (the `Branch / HEAD` line
  records it); if a report carries only a short SHA, match it against the prefix rather than
  reporting false drift.

  **Check the branch first, and on its own.** Whether you are on the right branch is a different
  question from where HEAD sits, and the three-step classification below answers only the second.
  If `git rev-parse --abbrev-ref HEAD` does not equal the report's branch, stop and confirm with
  the user before touching anything: a report pinned on `chore/a`, applied in a session sitting on
  `chore/b` branched off that same commit, passes item 1 and exits 0 at item 2 — so the
  classification reports "savepoints landed since the review: the expected steady state", which is
  reassuring output for findings being applied to the wrong branch. Nothing downstream consumes
  the branch name, so this is the only place the mismatch can surface. A deliberate cross-branch
  apply is legitimate — a report re-used on a follow-up branch — but it has to be the user's
  choice rather than an unremarked default.

  If HEAD has moved, **classify the mismatch with
  commands, not impressions** (ADR-0069) — `git log <sha>..HEAD` alone cannot do it: it runs
  happily from a dangling SHA and prints the whole branch, which reads as "the intervening
  commits" in exactly the case it isn't. In order:
  1. `git cat-file -e "<report sha>^{commit}"` — if this fails the SHA is unknown or gc'd:
     report the anchor as unresolvable, skip to the tree comparison below, and lean on step 3.
     Quote the whole revision, as written: unquoted, PowerShell splits `^{commit}` off and git
     rejects the remains (exit 129, measured), so a perfectly live anchor reads as gc'd and the
     early warning this classification exists to give is lost. Run these with the Bash tool.
  2. `git merge-base --is-ancestor "<report sha>" HEAD` — exit 0 means savepoints landed since
     the review: the expected steady state, and the enumeration is the useful output. Run
     `git log <report sha>..HEAD --oneline`, name what the intervening commits touched, and
     give findings citing those files extra care in step 3.
  3. Not an ancestor (or unresolvable): compare the report's recorded **tree hash** against
     `git rev-parse 'HEAD^{tree}'`. A match means `/ship`'s collapse rewrote the savepoints
     into one commit over the identical tree — explained, not drift; the report's per-commit
     SHAs are gone by design. A recorded hash that does **not** match is **real drift** — warn
     the user: findings may reference lines that have since shifted. A report carrying **no**
     tree hash is a third outcome, not the second: say the anchor is unavailable and that the
     mismatch is therefore unclassifiable, and lean on step 3's per-finding re-verification.
     `/review-handoff` records the field, but only reports written after it began to are
     obliged to carry one — reading absence as drift would fire a false alarm on every older
     report, which is the alarm ADR-0069 exists to remove rather than relocate.
     **A field reading `unknown — prep skipped` is that same third outcome, not a mismatch.**
     `/review-handoff` writes that sentinel whenever it cannot honestly anchor the hash — prep
     was skipped, or HEAD had moved past the reviewed SHA — and the same wording carries in the
     `Branch / HEAD` line, which already has its own sentinel clause below. Treat the two fields
     alike: the sentinel is not a hash, it can never match, and reading it as a *recorded* hash
     fires the drift alarm on exactly the reports that already declared they could not anchor
     themselves.

  Neither case aborts the run — step 3 re-verifies every finding anyway — but unexplained
  drift is worth flagging up front.
  If the SHA reads `unknown` (the review's reviewed commit was never pinned — prep was skipped),
  there is nothing to compare against: skip the drift check, note that the early-warning is
  unavailable, and lean entirely on step 3's per-finding re-verification.

## 2. Build the worklist

Enumerate the findings into a checklist (use TodoWrite). Carry each finding's **category** forward,
and its **Verdict** if the report has one. Whether a verdict is a machine result or the reviewer's
hand-added confidence depends on the effort that ran, and the report's **Verification** line states
which: a verify-pass effort emits real CONFIRMED/PLAUSIBLE verdicts, while the `high` default runs
no verify pass so any verdicts there are hand-added. Read that line and treat the verdicts
accordingly. Either way a verdict only informs how hard you look in step 3; it never lets you skip
step 3.
Work findings in the report's order (most severe first). If one finding's fix would change the
lines another finding cites, do the earlier one and re-read before the later.

## 3. Per finding: re-verify, then act

For each finding, in order:

1. **Re-verify against current code — every finding, whatever its verdict.** Read the cited
   `file:line` and the enclosing context. Confirm the defect is still present and still reachable.
   A **PLAUSIBLE** verdict (or none) carries more doubt — look harder, and if you cannot convince
   yourself it is real, do **not** guess-fix; report it as unconfirmed and move on. A **CONFIRMED**
   verdict is the reviewer's confidence, not a licence to skip this step.
   - If the finding is already resolved (code changed, or it never applied), mark it
     `already-resolved` with the one-line reason. This is a normal outcome, not a failure.
2. **Enumerate the peer sites before editing, and show the search.** A finding naming one site is
   a work item for the rule, not for the site. **Read `.claude/bot-review-triage.md` §1
   (*Under-reporting*) now, before your first fix** — it states the requirement and governs, and
   the pointer alone is not enough here: the rule is a *procedure to run*, not an adjective to
   apply, so a session that never opens the file cannot know what to search for, that the command
   and its hits must be reported, or that the search re-runs after the edit. Its title says
   "Bot-review triage" and this is not a bot round, which is exactly why the instruction to open
   it has to be explicit. It binds report findings and local-reviewer findings identically. Having
   read it, run it; do not restate it here.
3. **Decide the fix on the merits.** Implement the smallest correct change, which may differ
   from the report's "Suggested fix" — that sketch was explicitly unreviewed. Match surrounding
   code style, comment density, and idiom. **"Smallest" constrains the remedy, not its scope**:
   the smallest correct change to a rule broken at four sites still touches four sites, and
   reading this line as licence to fix only the cited one is the failure item 2 exists to stop.
4. **Not every finding is a code edit.** Reports also raise git-workflow actions (e.g. "commit
   these renames separately to preserve history"), new-infrastructure proposals (e.g. "add a CI
   link-check gate"), and process gaps. Do the ones that are safe, mechanical, and clearly in
   scope. **Stop and surface** — do not silently perform — anything destructive, anything that
   rewrites history, or any genuine scope decision (standing up new CI, changing a documented
   convention). Those are the user's call.

**Honor the repo's rules while editing.** The project `CLAUDE.md` governs: never edit an Accepted
ADR's decision content (link/typo fixes only); keep `specs/adr/README.md`'s index current after
any ADR change; write files as UTF-8; and never move anything out of `specs/personal/`. If a
finding's fix would cross one of these lines, treat it as a scope decision and surface it.

## 4. Verify what you changed

Don't declare a finding fixed on faith. For code changes, run the same gates CI runs — read the
pinned versions from the `env:` block of `.github/workflows/ci.yml` and run ruff / pyright /
pytest over the affected area; for behavior with a runtime surface, drive it (the `verify`
skill). For ADR or index edits, run `python scripts/check_adr_index.py`; always run
`python scripts/check_spec_links.py` (it validates link targets anywhere in the repo, so a
change *outside* `specs/` can break a spec link). Report a gate that comes back red — never paper
over it.

**Then checkpoint the batch: run `/savepoint`** (ADR-0069) — containment scan, the explicit
path list of what this batch changed, commit. This is the highest-leverage savepoint site: it
puts a revert point behind the verified work and a commit boundary exactly where step 5's next
smoke pass starts its diff. A batch that produced no edit has nothing to checkpoint — skip.

**Tag it `[x<N>]`**, where `N` is which external review this is — the report you are applying is
round `N`'s. This skill is the only place that knows that number: `/savepoint` cannot derive it,
and an untagged checkpoint here is what leaves a branch reading as an undifferentiated pile. If
the branch's own log does not tell you `N` (a report applied in a fresh session), read the highest
`N` already used and add one:

```bash
mb=$(git merge-base origin/main HEAD)
[ -n "$mb" ] && git cat-file -e "$mb^{commit}" || { echo "no merge base — stop"; exit 1; }
git log --oneline "$mb"..HEAD | sed -nE 's/^[0-9a-f]+ \[x([0-9]+)\].*/\1/p' | sort -n | tail -1
```

**No output does not by itself mean this is `[x1]`.** It means no *tagged* round, which is equally
what a branch that adopted the convention mid-flight looks like — this one included, its first two
external rounds predating the tag. When the query comes back empty, count the reports instead.
Step 1 does not already do this for you: it walks the sibling scratchpads only in its
no-path-passed branch, and it takes the *newest* rather than enumerating them, so this is a search
of its own — go up from your scratchpad to the per-project directory and list, oldest first, with
`<branch-flat>` standing for the branch name with every `/` replaced by `-` (the report filename
convention `/review-handoff` documents):

```bash
ls -1 <per-project dir>/*/scratchpad/code-review-<branch-flat>-*.md | sort
```

The report you are applying should be the last line, and its 1-based position is `N`. **That is
evidence, not an answer.** It holds only while every round produced exactly one report and every
report was applied, and neither is guaranteed: a re-run of `/review-handoff` writes a second file
for a single round, and a review abandoned before `/apply-review` leaves a report no `[x…]` tag
will ever match. Both inflate `N`.

**So confirm the number with the user before committing it — always, on this path, not only when
something looks wrong.** You reach this fallback precisely because the tag query came back empty,
which makes report counting the *only* source for `N` by construction; there is nothing left to
cross-check it against, and a rule that says "ask when the count disagrees with the tags" is
vacuous exactly here, where no tags exist. Show the count, the report list it came from, and the
report in your hand, and let the user confirm or correct it. A wrong number is worse than none —
`/savepoint` step 3's rule, and the reason for all of this.

Four details of the query earn their keep for the same reason. It is a **single `sed` capture,
deliberately, not a grep pipeline** — this recipe shipped once as
`grep -oE '^[0-9a-f]+ \[x[0-9]+\]' | grep -oE '[0-9]+$'` and was dead on arrival: stage 1's output
ends in `]`, so stage 2's `$`-anchored match never fired and the query returned empty on every
branch, silently taking the fallback below each time (found by review, not by failure, because an
empty result is also a *legal* answer here). The tempting repair — dropping the `$` — is worse: an
unanchored `[0-9]+` over the whole line harvests digits from the commit *sha*, answering `196`
from `7a1f196`. The capture group takes exactly the digits between `[x` and `]` and nothing else
can match. It requires the closing bracket immediately after the digits, so it matches the batch
tag `[x2]` and **not** the smoke tags `[x2s1]`…`[x2s3]` this same skill adds a few lines below —
a bare `[x…]` match would count those too, turning one applied review into four and tagging the
next batch `[x5]`. It is **anchored to the start of the subject** (`^[0-9a-f]+ \[x…`, since
`--oneline` puts the sha first), because an unanchored match reads a tag out of any commit that
merely *mentions* one — a subject like "Correct the `[x99]` example" would select round 100. Take
the **highest** `N` rather than a count, since a batch that produced no edit leaves no tag and a
count then re-issues a number already on the branch. And the merge base carries the same guard its
three siblings in `/land` and `/ship` do: unguarded, a failed substitution leaves
`git log --oneline ..HEAD`, which git accepts, exits 0 on, and prints nothing for (measured) —
indistinguishable from a branch with no prior rounds, so the fourth external review would be
tagged `[x1]`.

## 5. Re-run the reviewers on what you changed

If any finding produced an edit — of any kind — run `spec-reviewer` and `test-reviewer` before
reporting. This is standing authorization on this project and does not need asking, even under a
session default of not calling subagents. Launch them per `.claude/reviewer-isolation.md` —
parallel when its setup succeeds; its fallback is sequential, and that file owns the
procedure, the fallback, and the reasons, so do not restate them here. The pass that ran before `/land` saw the pre-`/apply-review` code, and
`.claude/bot-review-triage.md` §4's rule is not bot-specific: **a review that ran before the
fixes is stale, not clean.** Unlike a bot review — which `/squash-merge` deliberately lets go
stale because it is rate-limited and expensive — these two are cheap, so there is nothing to
trade off.

- **Tear down every round, on every exit path.** `.claude/reviewer-isolation.md` § Teardown
  owns the command and the reasons; what belongs here is that this skill is a *caller* and
  the obligation is the caller's. A round that reports and moves on to `/ship` leaves two
  full copies of the repo on disk and registered, carrying the uncommitted work under review.
  **The party this wedges is THIS session's next round, not a later one** — the earlier
  wording had that backwards, and the inversion was load-bearing. The scratch path is
  `…/claude/<project>/<session-uuid>/scratchpad`, so a later session resolves a *different*
  `--scratch`, finds no state file, and its setup succeeds; setup's guard only inspects
  `<scratch>/review-worktrees.json`. It is the next round in this same session — same UUID,
  same scratch dir — whose setup exits 1. What a later session actually inherits is the
  leaked worktree directories and their `.git/worktrees/` registrations, which block nothing
  and which no teardown will ever remove (`scripts/review_worktree.py` reports them as
  unrecorded strays and deliberately does not delete them).
  Read the old way round, the consequence is worse than an untidy disk: you skip teardown on
  the strength of "not for this one", the loop below re-runs setup in the same scratch dir,
  it exits 1, and the exit-1 reads as a new fault — putting the round at risk of being
  dropped to the sequential fallback, which `.claude/reviewer-isolation.md` and
  `specs/testing-strategy.md` **explicitly forbid for this cause** ("Recoverable — fix and
  rerun, do not fall back").
- **Each round re-runs `setup`, not just the agents.** Stated here because the recursion
  bullet below says only "re-run after each round", which reads as re-running the two
  reviewers — and the teardown bullet above depends on a new setup happening. Relaunch both
  agents against round 1's worktrees and they review a snapshot pinned *before* this round's
  fixes, so both return **pass** on code that no longer exists: the stale-not-clean failure
  `.claude/reviewer-isolation.md` invariant 1 names, reached with no bot involved. New round
  = teardown, setup, relaunch.
- **"Any kind" is not a list of file categories.** Step 3's fourth item authorizes git-workflow
  actions, new-infrastructure proposals and process-gap fixes, so a round can legitimately edit
  CI workflows, tooling, or process docs while touching no code, test, spec or ADR at all. Any
  enumeration drifts out of sync with what that item allows, and a round falling through the list
  gets no re-run while `/land` step 6 has already disqualified the earlier pass — leaving it with
  no valid reviewer pass at all.
- **Do not scope them to your hunks.** Both agents default to the whole change under review —
  isolated, that is the diff against the manifest's base ref inside their worktree plus the
  untracked files the invoking prompt lists; in the fallback there is no worktree and no
  manifest, so it is `<base>...HEAD` **plus `git diff` and `git diff --cached`** — the
  uncommitted tracked work, which in the isolated form arrives inside the snapshot commit and
  in the fallback has nowhere else to come from — plus the untracked files they enumerate
  themselves. Only with that middle clause do the two spellings mean the same scope; without
  it the fallback reads as `<base>...HEAD` plus untracked, which on a branch carrying zero
  commits is *nothing plus the new files* — every tracked modification silently out of scope,
  and the sentence asserting the spellings match is what hides it. The agent files carry the
  full form; this is the copy that has to agree with them. That default is the point: on `bot-review-outstanding-gate` the defect a
  re-run caught was an *interaction* — a body-only fix sitting inside `unmatched_reviews`'s
  `if bot.findings: continue`, so it never ran when any comment matched — invisible to anything
  scoped to the changed lines. Name a narrower range only if the user did.
- **Judge their findings on the merits**, exactly as step 3 requires for the report's. A reviewer
  finding is a claim, not an instruction.
- **The loop recurses.** Fixing what they find invalidates the review that found it, so
  re-run after each round — teardown, a fresh `setup`, then both agents, per the bullet
  above; relaunching the agents alone reviews the previous round's snapshot. **Stop when a
  round produces no substantive edit** — not after a fixed number of rounds. Three rounds happened on `bot-review-outstanding-gate`, and round 2 is where the
  reviewers *failed*; a "one extra round" rule would have shipped that defect.
- **Substantive** means it could change behavior or spec conformance. A typo, a list ordinal, a
  reflowed line — apply it and let the loop end; re-reviewing a diff whose only delta is a
  renumbered bullet buys nothing. When in doubt, treat it as substantive and run the round.
- **From round 2 on, only defects reopen the loop** — correctness, spec conformance, test
  validity. A cosmetic note can still be applied; it just does not earn another round. Report it
  in step 6 either way. That is what makes the loop terminate.
- **A round that edits ends with a `/savepoint`** — scan, path list, commit, tagged
  **`[x<N>s<M>]`** for smoke pass `M` inside external review `N`'s apply — before the next
  round's teardown + setup (ADR-0069). **Derive `M` the same way as `N`, and never reuse one**:
  take the highest already on the branch for *this* `N` and add one, anchored the same way, which
  matters because an apply resumed in a fresh session has no other memory of how many smokes ran —

  ```bash
  mb=$(git merge-base origin/main HEAD)
  [ -n "$mb" ] && git cat-file -e "$mb^{commit}" || { echo "no merge base — stop"; exit 1; }
  git log --oneline "$mb"..HEAD | sed -nE "s/^[0-9a-f]+ \[x${N}s([0-9]+)\].*/\1/p" | sort -n | tail -1
  ```

  The resolution and guard are repeated here rather than inherited from step 4's block, because
  this snippet runs in a *different* Bash call and shell variables do not survive between calls —
  the rule this file already states, and the rule this block shipped once violating: copied alone,
  an empty `$mb` leaves `git log --oneline ..HEAD`, which exits 0 printing nothing, so `M`
  silently derives as `1` and a smoke number gets reused.

  No output means this is `s1`. A gap in the sequence is information rather than an error — a smoke
  that found nothing has no edit to checkpoint, so a missing number means a clean pass — which is
  why the rule is *highest plus one* rather than *count plus one*: counting re-issues a number
  already on the branch. If the query disagrees with what you believe ran, ask rather than pick;
  a wrong `M` is the same defect as a wrong `N`, and `/savepoint` step 3 says why. These local passes are **smokes**, not reviews: that
  skill's step 3 owns the vocabulary and the reason, and the short version is that "review"
  belongs to the external loop and a smoke never counts as one. The tag is what makes
  the loop legible afterwards: a branch showing `[x2s1]`…`[x2s4]` says one review's findings took
  four passes to settle, which is the signal that a fix shape is wrong rather than merely
  incomplete. The next setup then pins a clean tree through the
  launcher's branch-diff path, and the round boundary becomes diffable
  (`git diff <previous round's sha> HEAD`) instead of an assertion the next reviewer takes on
  trust. A round whose only outcome was `already-resolved`/unconfirmed has no edit to checkpoint.
- **A round that edits re-runs step 4's gates, not just the reviewers.** This section's own thesis
  applies to a gate run: step 4 passed on the pre-round code, so a reviewer-round fix that breaks
  ruff, pyright, pytest, `check_adr_index.py`, or `check_spec_links.py` would otherwise be reported
  green in step 6. Re-run them once the loop has settled, and report *that* result.
- If no finding produced an edit (all `already-resolved` or unconfirmed), skip this step and say
  so — there is nothing to invalidate.

## 6. Report outcomes

Do **not** make the landing commit — that is `/land` + `/ship`'s job unless the user asks. The
`/savepoint` checkpoints in steps 4 and 5 are the deliberate exception (ADR-0069): local
scaffolding `/ship` collapses, not a landing. End with:

1. A per-finding table: `fixed` / `already-resolved` / `skipped (reason)` / `needs-user-decision`,
   one row each, so nothing in the report is silently dropped. Every finding of the form "X is
   wrong here" also carries step 3 item 2's peer search — the condition is
   `.claude/bot-review-triage.md` §1's, not a narrower one of this skill's, since a session
   deciding a finding did not "name a specific site" could otherwise skip the column and still be
   compliant. The row records **both** counts §1 requires: the command with the hits it returned
   before the edit, and the re-run afterwards, which is the half that shows the work finished —
   a pre-edit count of four behind a `fixed` verdict is equally consistent with three peers
   repaired and one left. Where the count exceeded one, say which peers were fixed and which were
   judged legitimately different. A row asserting `fixed` with no search behind it is the shape
   of a rule patched at one site.
2. What you changed, by file, and the result of the gates you ran.
3. The smoke passes from step 5 — how many ran, **which mode each ran in** (isolated, or the
   sequential live-tree fallback), **each round's anchor** — the `git rev-parse HEAD` and
   `HEAD^{tree}` values recorded at its launch (`.claude/reviewer-isolation.md` § Launch; the
   commit SHA dangles after `/ship`'s collapse, the tree hash survives it — without them here
   the recorded anchor dies with the session and the drift check it exists for reads nothing) —
   each round's verdict (**pass** / **pass with notes** / **fail**), and what each one changed. `/land` step 6 asks what changed since the last pass
   *and in which mode it ran*; this is the answer to both, and it is the *only* record of them —
   nothing is written to disk, so a record omitting the mode leaves `/land`'s fallback check
   nothing to read and an unmutated pass gets counted as equivalent. If `/land` runs in a later
   session the record is gone entirely and the honest move is to re-run rather than assert a
   pass you cannot evidence. The reviewers are cheap; that is the point of re-running them.
4. Anything you deliberately did not do and why — especially findings you judged wrong on
   re-verification (say so plainly; disagreeing with the report is allowed) and the
   scope-decisions from step 3 you are handing back.
5. If any fix created or changed a spec record (ADR, `api-reference.md`, `data-model.md`,
   `open-questions.md`), draft the `Decisions:` line for the eventual commit, per `CLAUDE.md`.
