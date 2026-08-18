# Round 1 — chore/brief-5b-ledger-collapse

## Angle record

- **Date:** 2026-08-18
- **Loop:** external
- **Effort:** xhigh
- **Surface:** not recorded by the report; no verify pass ran, so no finding
  carried a machine verdict
- **Base (resolved):** `388f8d6f600f7f9ef4d352a5b70a547e176f08a4`
- **HEAD:** `21e944242dcee9b13fffa5845ad697820fc2972d`
- **HEAD tree:** `153467e4e8712405d893500f08b879e30cf6f45c`
- **Diff size:** 1581 lines (+1581/-48), of which ~1351 code/CI and ~230 prose
- **Brief revision stamp:** none — this round predates the ledger's use on this
  branch and was not briefed through `/review-brief`
- **Angles briefed:** none; no brief was issued
- **Angles executed:** not recorded. The report states the review "emitted a
  findings list only and enumerated no clean areas"
- **Briefed but not executed:** not applicable
- **Examined:** unknown, and it resolves to *not examined*. Findings landed on
  four files — `scripts/ledger.py`, `tests/test_ledger.py`,
  `.claude/skills/review-brief/SKILL.md`, `.claude/skills/squash-merge/SKILL.md`
  — and the report is explicit that the other eight files in scope are "listed
  as scope, **not** as reviewed-clean"

### Do-not-re-run, carried into this round

| Excluded | Paths | Cleared at | Cleared by | Evidence |
|---|---|---|---|---|
| _nothing_ | — | — | — | The round enumerated no clean areas, so nothing was cleared. Reviewer silence is not a verdict (ADR-0072 §7). |

**This fragment was written after the round, not at allocation.** The branch ran
its first external review before the ledger was in use, so `max(existing) + 1`
would have re-issued `1` to the round that follows — the "adopted the tag
convention mid-flight" case ADR-0072's Context and Problem Statement names as a
defect of the heuristic this mechanism replaced. (§5 was cited here originally,
and it is the wrong pointer: §5 carries allocation durability and the four
re-entry cases, but never this one. Corrected as a broken pointer, not as a
change to what the round recorded.) Every field
above is transcribed from the round's own report and from the apply that
consumed it; nothing is inferred. Recorded here rather than left out because the
next brief is generated from this record, and an absent round reads as a round
that never happened.

## Round record

- **Findings:** 15 raised, 14 confirmed on re-verification, 1 declined as wrong
- **Severity (ADR-0072 §6, applied at apply time):** `silent-wrong` 5,
  `loud-wrong` 5, `latent` 2, `hygiene` 2, plus the one declined
- **Assessment:** `real` 14, `wrong` 1
- **Disposition:** `fixed` 14, `no-action` 1
- **Fraction inside the previous round's fixes:** 0 of 15 — every finding landed
  on original material rather than on a prior remedy
- **Scope mismatch:** none; the review honoured the pinned range
- **Convergence call:** do not stop. The round found a data-loss defect that
  three local smokes had cleared, which is the opposite of convergence

### Round record, second half — the analysis

- **Locus:** `scripts/ledger.py` took 11 findings, `tests/test_ledger.py` 2, and
  the two skill files 1 each. The ADR, the CI wiring, the gate registry and the
  three `specs/` documents drew none — and were not reported clean either
- **Type mix:** `gap` 11, `cosmetic` 2, `false-claim` 2 (one of them the
  declined finding, whose premise about the gate's naming was checkable and
  false)
- **Precision:** 14 of 15 survived re-verification. Every measurable finding was
  reproduced in a scratch repository before any edit
- **Remedy churn:** 4 of 14 remedies were wrong on the first attempt, and one —
  the collapse's delete decision — was wrong twice, through three
  implementations (round numbers, substring membership, per-round fingerprints).
  The four smokes that followed exist because of that churn
- **Cross-lens delta:** the external round found what three local smokes had
  not, including a data-loss path that a mutation matrix over the same function
  had reported clean in both directions. The reason is recorded with the fix: the
  crash-recovery test restored the surviving fragment with byte-identical
  content, which is exactly the case a round-number comparison gets right

**The causal read.** The local smokes were briefed at angles — fidelity,
detection power, whole-artifact — and the external round was briefed at none,
reading the whole diff cold. What it found was not subtler than what the smokes
looked for; it was in the place none of them was pointed. The smokes attacked the
refusals as a set and confirmed each fired; the external lens asked what happens
to a fragment on the path where no refusal fires at all.
