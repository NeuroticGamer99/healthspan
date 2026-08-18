# Round 2 — chore/brief-5b-ledger-collapse

## Angle record

- **Date:** 2026-08-18
- **Loop:** external
- **Effort:** xhigh
- **Surface:** the review self-reported executing "all ten finder angles plus
  the sweep, with each candidate probed against the running code". It emitted
  **no** CONFIRMED/PLAUSIBLE verdicts — no verify pass ran — and it named no
  not-reached list, which the brief asked for explicitly
- **Base (resolved):** `21e944242dcee9b13fffa5845ad697820fc2972d`
- **HEAD (pinned by the brief):** `d37ef08f81c7cb3547040263d8f0702407e693c3`
- **HEAD (actually reviewed):** `562da021755a136a77485948d78a4d5c11772494`
- **HEAD tree (actually reviewed):** `1940bb6951fd23d14349408f5e45e06aa5c30ed6`
- **Diff size:** 8 files, +887/-123 as reviewed (7 files, +855/-123 as pinned)
- **Brief revision stamp:** `7ec6107c`
- **Angles briefed:** the collapse's delete decision in its third
  implementation; the economy angle over the prose this branch added; the remedy
  angle over round 1's fixes and the four smokes that followed; the
  whole-artifact angle, dispatched without an exclusion list
- **Angles executed:** not separately recorded. The report attributes all ten
  finder angles plus the sweep to the round but maps no finding to an angle, so
  the roster is the reviewer's claim rather than an observation
- **Briefed but not executed:** none named as skipped, but two of the brief's
  six uncertainties came back unanswered — #3 (whether `specs/reviews/` is the
  right anchor for `check_collapsed`, and whether that choice has a false-pass
  left in it) drew no finding at all, and #4 (whether the hand-backfilled
  round-1 fragment is legitimate) was touched only obliquely, cited as evidence
  that a hand-amended shape is reachable without judging the backfill
- **Examined:** findings landed on three files — `scripts/ledger.py` (7),
  `tests/test_ledger.py` (3), `.claude/skills/squash-merge/SKILL.md` (1). The
  report states per-file clean coverage is **not** stated and must not be
  inferred from the changed-file list, so the other five files in scope are
  unexamined rather than clean

### Do-not-re-run, carried into this round

| Excluded | Paths | Cleared at | Cleared by | Evidence |
|---|---|---|---|---|
| _nothing_ | — | — | — | Round 1 enumerated no clean areas, so it cleared nothing and no entry exists to carry or to lapse. The precedence conflict between an exclusion list and the whole-artifact angle therefore does not arise this round. |

**Scope note.** The base is round 1's HEAD rather than `origin/main`, deliberately:
this round is pointed at the lines round 1 never saw. The eight files round 1
listed as scope but never reported clean remain unexamined, and this round does
not reach them either — that remainder is stated in the brief rather than left to
read as covered.

**The round read its own fragment.** The brief pinned `…d37ef08`; the review ran
against `…562da02`, one commit later, and that commit is this file's own
allocation. The extra 32 lines are ledger bookkeeping and no finding touched
them — but it is the feedback loop ADR-0072 §8 names arriving in the smallest
possible form, and it is why both anchors are recorded above instead of the
pinned one alone.

## Round record

- **Findings:** 11 raised, 11 confirmed on re-verification, 0 declined
- **Severity (ADR-0072 §6, applied at apply time):** `silent-wrong` 5,
  `loud-wrong` 4, `hygiene` 2
- **Assessment:** `real` 11, `wrong` 0
- **Disposition:** `fixed` 11
- **Fraction inside the previous round's fixes:** **7 of 11 wholly, plus 1
  interaction — against round 1's 0 of 15.** Measured with `git log -S` over the
  pinned range rather than judged: findings 1, 3, 6, 7, 9, 10 and 11 land on code
  or tests introduced by `[x1]` or by the four `[x1s*]` smokes that followed it;
  findings 2, 5 and 8 land on the original `[b]` implementation. Finding 4 spans
  both — the non-atomic write is original, the header-only re-entry check is
  round 1's remedy, and neither is a defect without the other
- **Scope mismatch:** the review read one commit past the pinned head (above).
  Not a mismatch that cost anything, and recorded rather than smoothed over
- **Convergence call, as made at apply time:** **do not stop.** Two findings were
  fresh `silent-wrong` data-loss paths in the collapse, and the remedy fraction
  inverted from 0/15 to 8/11 in one round — the opposite of convergence, and the
  second consecutive round to find a data-loss path every local smoke had cleared
- **What the smoke loop then showed, recorded because it revises that call
  rather than confirming it:** the *production module* converged immediately —
  `scripts/ledger.py` took no functional change after `[x2]`. What did not
  converge was one test, for six rounds, and its non-convergence was caused by
  the shape of each fix rather than by the code under it. A convergence call
  needs to say *what* is converging; this one did not, and read as pessimism
  about the module when the module was already still. The analysis block below
  carries the loop that showed it

### Round record, second half — the analysis

- **Locus:** `scripts/ledger.py` took 7 findings, `tests/test_ledger.py` 3, and
  `.claude/skills/squash-merge/SKILL.md` 1. The ADR, the CI wiring, the gate
  registry and the `specs/` documents drew none — and were not reported clean
- **Type mix (reviewer's own categories, verbatim):** `correctness` 8,
  `test-coverage` 1, `documentation accuracy` 1, `cleanup` 1
- **Precision:** 11 of 11 survived re-verification, and every measurable one was
  reproduced against the running module before any edit. Two of the review's own
  scenarios needed repair before they reproduced — the probe's bugs, not the
  findings' — which is worth recording because a scenario that fails to reproduce
  reads exactly like a finding that is wrong
- **Under-reporting:** the peer search found sites the review did not name in
  **two** of eleven findings. Finding 8 named three `read_text` call sites and
  there were four; finding 11 named one unused fixture and there were two. Both
  are the `.claude/bot-review-triage.md` §1 case, and both were cheap to find
- **Remedy churn, this round:** **3 of 11**, and one of those consumed six
  rounds on its own. *(Written at `[x2]` as "one of eleven", before any smoke had
  run; corrected here once the loop settled, which is what ADR-0072 §7 requires
  and what a smoke-6 reviewer caught this fragment not having done.)* The three:
  finding 9's sync oracle — the source side got an AST reader while the identical
  whole-file-scan defect stayed on the skill side, where my own newly added prose
  then satisfied the substring check, so deleting the table row outright kept the
  test green; finding 4's atomicity test, which asserted only that no staging
  file survived and stayed green against a direct write plus a leftover-free
  decoy; and finding 8's `_read_text` mechanization, below
- **Cross-lens delta:** the external round found what four local smokes had not,
  for the second round running. Findings 1 and 4 are both data-loss paths through
  code the smokes had mutation-tested in both directions. Within this apply the
  two lenses then split just as cleanly and did not overlap: the test lens found
  every mechanism defect, the spec lens every claim that outran its mechanism — a
  stale count written inside the round that introduced it, a docstring broader
  than its AST scan, a test named "every file access" that ignored deletion, and
  an ADR sentence giving *prevention* to a step that runs after the merge. One
  smoke round of the six was clean on either side
- **The smoke loop behind those figures** — recorded here rather than under a
  heading of its own, because §7's block list is authoritative and names three
  blocks; a fourth was drafted and a reviewer caught it. Six rounds, twelve
  reviewer passes, all isolated with no sequential fallback; two returned
  **fail**. Every round produced a substantive edit, which is why it ran to six
  rather than stopping on a count. **The production module converged
  immediately** — `scripts/ledger.py` took no functional change after `[x2]`, its
  one later commit deleting a stale docstring count — so what churned for six
  rounds was a single meta-test. A whole-artifact round at `x2s4` returned the
  only two findings no narrow round had surfaced — a mis-cited section pointer in
  round 1's own fragment, and ADR-0072's "Consequences for Other Documents"
  manifest omitting the `specs/open-questions.md` changes this branch itself made
  — which is the disjoint-classes effect measured again
- **Four defects in this apply were the orchestrator's, not the reviewers':**
  tearing down a round's worktrees while its second agent was still working;
  writing file content through a heredoc and corrupting a docstring with it;
  asserting an enumeration to a reviewer without measuring it — that two of §10's
  "enforced by nothing" rules were now mechanized, when neither appears in the ADR
  at all; and describing a diff to a reviewer as untouched when it had been
  edited. Each cost at least part of a round, and they are recorded because they
  are the reusable part
- **What this record cannot evidence.** The verdict tally and mutation counts
  above are the orchestrator's transcription: local rounds produce no fragment by
  §7's own rule, so nothing on the branch preserves them and a reviewer told me
  plainly it could neither confirm nor refute them. The mechanism claims *are*
  checkable and were checked — that `scripts/ledger.py` went untouched, and that
  every token the prose names is in the code

**The causal read.** Round 1's lesson was that the smokes were pointed at the
refusals as a set while the external lens asked what happens where no refusal
fires. This round's is narrower and sharper: **the smokes were pointed at the
remedies, and the remedies were where the defects were** — 8 of 11 findings land
on `[x1]`/`[x1s*]` code — yet the smokes still missed them. What the external
lens had that they did not was the *composed* artifact. Findings 1, 2 and 4 are
each invisible to any test of a single fragment or a single function: one needs
two fragments concatenated, one needs the digest's title above the inlined body,
one needs the digest's header separated from its body. Four rounds of
mutation-testing individual guards cannot reach a defect that only exists once
the pieces are assembled.

**Why that meta-test cost six rounds, which is the reusable lesson.** Each fix
extended it by exactly the token the last mutation had used — `open`, then a
read/write mode split, then `unlink`/`rmtree`, then `os.remove` — and each time
the next round found the next sibling. **Reactive extension guarantees a next
sibling.** The terminating fix was structural, and a reviewer named its *shape*
before it was applied: derive the watched set from the module's **import
surface**, once, and record a new import as the only trigger to revisit it.
