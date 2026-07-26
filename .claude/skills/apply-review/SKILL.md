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

## 1. Load the report

- If a path was passed, Read it — the normal case, since `review-handoff` prints the report's
  absolute path for exactly this hand-off. If no path was passed, do **not** glob only your own
  session's scratchpad: the report was written by a *different* session and the scratchpad path is
  session-specific (a per-session UUID), so yours will not contain it. Instead search across the
  sibling session scratchpads — go up from your session's scratchpad to the per-project directory
  and match `*/scratchpad/code-review-*.md` — take the newest, name the file and which session it
  came from, and confirm with the user before using it, since it may be an unrelated review.
- Read the report's **Branch / HEAD** and **Diff scope** lines, then run `git rev-parse HEAD`
  and `git rev-parse --abbrev-ref HEAD`. Compare on the **full** SHA (the `Branch / HEAD` line
  records it); if a report carries only a short SHA, match it against the prefix rather than
  reporting false drift. If HEAD has moved or the branch differs, warn the user:
  findings may reference lines that have since shifted. This does not abort the run — step 3
  re-verifies every finding anyway — but a large drift is worth flagging up front.
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
2. **Decide the fix on the merits.** Implement the smallest correct change, which may differ
   from the report's "Suggested fix" — that sketch was explicitly unreviewed. Match surrounding
   code style, comment density, and idiom.
3. **Not every finding is a code edit.** Reports also raise git-workflow actions (e.g. "commit
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

## 5. Re-run the reviewers on what you changed

If any finding produced an edit — of any kind — run `spec-reviewer` and `test-reviewer` before
reporting. Invoke both in parallel in one message; this is standing authorization on this project
and does not need asking, even under a session default of not calling subagents. The pass that ran
before `/land` saw the pre-`/apply-review` code, and
`.claude/bot-review-triage.md` §4's rule is not bot-specific: **a review that ran before the
fixes is stale, not clean.** Unlike a bot review — which `/squash-merge` deliberately lets go
stale because it is rate-limited and expensive — these two are cheap, so there is nothing to
trade off.

- **"Any kind" is not a list of file categories.** Step 3's third item authorizes git-workflow
  actions, new-infrastructure proposals and process-gap fixes, so a round can legitimately edit
  CI workflows, tooling, or process docs while touching no code, test, spec or ADR at all. Any
  enumeration drifts out of sync with what that item allows, and a round falling through the list
  gets no re-run while `/land` step 6 has already disqualified the earlier pass — leaving it with
  no valid reviewer pass at all.
- **Do not scope them to your hunks.** Both agents default to `git diff origin/main...HEAD` plus
  uncommitted work, and that default is the point: on `bot-review-outstanding-gate` the defect a
  re-run caught was an *interaction* — a body-only fix sitting inside `unmatched_reviews`'s
  `if bot.findings: continue`, so it never ran when any comment matched — invisible to anything
  scoped to the changed lines. Name a narrower range only if the user did.
- **Judge their findings on the merits**, exactly as step 3 requires for the report's. A reviewer
  finding is a claim, not an instruction.
- **The loop recurses.** Fixing what they find invalidates the review that found it, so re-run
  after each round. **Stop when a round produces no substantive edit** — not after a fixed number
  of rounds. Three rounds happened on `bot-review-outstanding-gate`, and round 2 is where the
  reviewers *failed*; a "one extra round" rule would have shipped that defect.
- **Substantive** means it could change behavior or spec conformance. A typo, a list ordinal, a
  reflowed line — apply it and let the loop end; re-reviewing a diff whose only delta is a
  renumbered bullet buys nothing. When in doubt, treat it as substantive and run the round.
- **From round 2 on, only defects reopen the loop** — correctness, spec conformance, test
  validity. Cosmetic notes get reported in step 6 rather than fixed-and-re-reviewed, which is
  what makes the loop terminate.
- **A round that edits re-runs step 4's gates, not just the reviewers.** This section's own thesis
  applies to a gate run: step 4 passed on the pre-round code, so a reviewer-round fix that breaks
  ruff, pyright, pytest, `check_adr_index.py`, or `check_spec_links.py` would otherwise be reported
  green in step 6. Re-run them once the loop has settled, and report *that* result.
- If no finding produced an edit (all `already-resolved` or unconfirmed), skip this step and say
  so — there is nothing to invalidate.

## 6. Report outcomes

Do **not** commit — landing is `/land` + `/ship`'s job unless the user asks. End with:

1. A per-finding table: `fixed` / `already-resolved` / `skipped (reason)` / `needs-user-decision`,
   one row each, so nothing in the report is silently dropped.
2. What you changed, by file, and the result of the gates you ran.
3. The reviewer rounds from step 5 — how many ran, each round's verdict (**pass** / **pass with
   notes** / **fail**), and what each one changed. `/land` step 6 asks what changed since the last
   pass; this is the answer to that question, and it is the *only* record of it — nothing is
   written to disk, so if `/land` runs in a later session the record is gone and the honest move
   is to re-run rather than assert a pass you cannot evidence. The reviewers are cheap; that is
   the point of re-running them.
4. Anything you deliberately did not do and why — especially findings you judged wrong on
   re-verification (say so plainly; disagreeing with the report is allowed) and the
   scope-decisions from step 3 you are handing back.
5. If any fix created or changed a spec record (ADR, `api-reference.md`, `data-model.md`,
   `open-questions.md`), draft the `Decisions:` line for the eventual commit, per `CLAUDE.md`.
