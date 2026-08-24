# Round 1 — mark-1

## Angle record

- **Date:** 2026-08-23
- **Loop:** external
- **Effort:** max
- **Surface:** _not yet filled — /review-handoff_
- **Base (resolved):** 5be44593646d580abae56b3fa1ab596717ec68a7
- **HEAD:** 04fbbd80925a7f815e7c2cae51107046bee8efd7
- **HEAD tree:** aadc493403b9ef2129c1f286f5b8044392d2aef8
- **Diff size:** 1909 lines (1909 excluding the ledger — this is the branch's first fragment, so the ledger contributes nothing to its own scope)
- **Brief revision stamp:** 34b14e8c
- **Angles briefed:** the remedy (the whole scope is remedy for a pass whose findings are already applied); the economy angle (1066 of 1664 added `.py` lines are comment/docstring prose); the whole-artifact angle, dispatched with no exclusion list; guard-interaction sweep (the orchestrator's uncertainty 5)
- **Angles executed:** _not yet filled — /review-handoff_
- **Briefed but not executed:** _not yet filled — /review-handoff_
- **Examined:** _not yet filled — /review-handoff_

### Numbering note — read before comparing this to the commit tags

`scripts/ledger.py next-round` answered **1**, and that answer is used verbatim
rather than corrected, because the allocator is the authority for fragment
naming and a fragment whose `N` is not its answer is a detectable error.

It is nonetheless **not** this branch's first external pass. An earlier
`/code-review max` ran on 2026-08-23 against the state at the base commit below,
returned 15 findings plus a cut list, and predates the ledger — it left no
fragment, so nothing on disk or in any digest records it. Its remedies carry the
commit tag `[x1]`.

Consequence to expect: this fragment is `round-1` while the *previous* external
round's commits read `[x1]`. Tag this round's remedies **`[x2]`** so the tags
stay a true count of external passes; the fragment numbers will then trail the
tags by one for the life of this branch.

### Do-not-re-run, carried into this round

| Excluded | Paths | Cleared at | Cleared by | Evidence |
|---|---|---|---|---|

_Empty, and not by oversight: this is the branch's first fragment, so no round
has ever recorded a clearance here and there is nothing to lapse. The
whole-artifact angle therefore carries no exclusion list to conflict with, which
is the one round where that precedence rule does not bite._
