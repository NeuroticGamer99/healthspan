---
name: apply-review
description: Read a code-review report produced by /review-handoff and implement its findings — re-verifying each against the current code before fixing, since the report's suggested fixes are unreviewed and the code may have drifted. Use in a fresh session to act on a handed-off report.
---

# /apply-review — implement the findings from a review report

The receiving half of `/review-handoff`. That command runs a review and writes a portable
report; this one reads the report back and does the work. Assume the report was written by an
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

## 2. Build the worklist

Enumerate the findings into a checklist (use TodoWrite). Carry each finding's **category** forward,
and its **Verdict** if the report has one — but treat a verdict as the reviewer's *confidence*, not
a machine guarantee (the `high` review runs no verify pass, so any verdicts are hand-added). It
informs how hard you look in step 3; it never lets you skip step 3.
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
skill). For ADR or index edits, run `python scripts/check_adr_index.py`; for any `specs/` `.md`
edit, run `python scripts/check_spec_links.py`. Report a gate that comes back red — never paper
over it.

## 5. Report outcomes

Do **not** commit — landing is `/land` + `/ship`'s job unless the user asks. End with:

1. A per-finding table: `fixed` / `already-resolved` / `skipped (reason)` / `needs-user-decision`,
   one row each, so nothing in the report is silently dropped.
2. What you changed, by file, and the result of the gates you ran.
3. Anything you deliberately did not do and why — especially findings you judged wrong on
   re-verification (say so plainly; disagreeing with the report is allowed) and the
   scope-decisions from step 3 you are handing back.
4. If any fix created or changed a spec record (ADR, `api-reference.md`, `data-model.md`,
   `open-questions.md`), draft the `Decisions:` line for the eventual commit, per `CLAUDE.md`.
