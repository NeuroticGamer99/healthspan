# ADR-0076: Each test-reviewer Finding States the Mutation Its Remedy Must Survive

## Status
Proposed

## Context and Problem Statement
This decision came out of the review rounds on the gate-runner work (RUNG-1, merged as PR #89 → `042492a`) and the rounds on this ADR's own branch. **Its supporting figures live in session history and were never written into a repository artifact**, so they cannot be cited or re-derived. The case below is therefore deliberately structural: it rests on how the hand-off is shaped, not on a count, and no count is offered.

The structural point is that `test-reviewer` does not fix what it finds. Its mandate ends at a hard line stated in its own definition — *"Do not write the missing tests — identify, cite, and rank; fixing is the caller's job."* The fix therefore returns to the author whose misunderstanding produced the gap, holding a document that says **what is wrong** and nowhere says **what would prove them right**.

That is a hand-off with no acceptance test inside it. A remedy written under the misunderstanding that caused the defect can satisfy the finding's prose exactly and still miss its detector — and nothing in the exchange is positioned to notice, because the party who could check is the party who has already stopped.

The information needed to close that gap already exists at the moment the finding is written, and this is the part that makes the field cheap. Mutation is how the agent establishes detection power at all: its own definition directs it to land an edit, confirm the edit landed, and watch a named test go red, *"prefer[ring] it to reasoning about what a test would catch"* (`.claude/agents/test-reviewer.md` § Mutation testing). A finding resting on that evidence therefore has the evidence in hand before it is phrased. Today the evidence is spent on narration in the report body and then discarded.

## Decision Drivers
- A hand-off that depends on the fixer *remembering* to derive a criterion is the weaker mechanism. This repository's standing complaint against itself is that a rule held only in prose is the one that lapses — `specs/open-questions.md` carries the theme at length — and a criterion that arrives in the report does not depend on being remembered.
- The producing agent is the only party that has already run the experiment. Asking the consumer to reconstruct it is asking for the reconstruction to be wrong.
- Whatever is added must not erode the review/write boundary. An agent that starts specifying assertions has started writing the tests it is forbidden to write.
- A criterion presented as measured when it was merely imagined is worse than none, because it will be trusted.

## Considered Options
1. Leave it as prose narration in the report body.
2. Adopt mutation-first as a discipline for the *fixer* instead.
3. Require an `Acceptance:` field on every `test-reviewer` finding (chosen).
4. Require it on both reviewers.
5. Add the field to ADR-0072's round-ledger columns instead of the agent's report.

## Decision Outcome

### 1. The field
Every `test-reviewer` finding carries an **`Acceptance:`** line naming the mutation the **remedy** must survive: a concrete edit that, once the gap is closed, must redden at least one named test.

It states what would prove the remedy right. Everything else in the finding describes the gap.

### 2. It names a behavior, not a test case
The fixer still chooses the layer, the fixture, and the assertions. Specifying what to assert crosses into writing the test, which remains outside the agent's mandate. This ADR adds a field to an existing output; it does not touch the prohibition.

### 3. Measured and proposed criteria are distinguished in the line itself
A mutation run in the reviewer's snapshot worktree and confirmed landed is evidence the criterion is reachable. One that could not be run — the read-only fallback of ADR-0068, or a mutation the agent's own safety bounds put out of reach — is a **proposal** and must read as one. The two are never presented alike.

### 4. Not every finding is mutation-shaped
A synthetic-fixture violation, a deleted test, or a missing migration case states the observable that must change instead. Where a finding genuinely has no such observable, the line reads `Acceptance: none — <why>`. Inventing a mutation to fill the field is a worse failure than leaving it empty, because it sends the fixer to prove something that was never the point.

### 5. Scope: `test-reviewer` only, and this is a decision rather than an omission
`spec-reviewer` does not carry the field. Its mandate is read-only: it runs no mutation and routes enforcement questions to `test-reviewer` rather than answering them. It therefore has no mutation to state, and its findings already carry their own criterion — the cited spec sentence, which the remedy either matches or does not. Extending the field to it would mean inventing criteria for findings whose evidence is documentary, which §4 forbids for the same reason.

### 6. Relationship to ADR-0072's field vocabulary
Review raised the risk that this is a second, uncoordinated per-finding vocabulary competing with ADR-0072's round-ledger columns (`Assessment`, `Disposition`). It is not — and the reason is ADR-0072's own rule rather than a distinction drawn here. **Its §7 states that local rounds produce no fragment**: the `spec-reviewer`/`test-reviewer` smokes are classed as tests the owner would run by hand, and "a local smoke round consumes no number at all". `test-reviewer` findings therefore never reach the ledger, and there is no competing home for this datum to have gone to.

**That reasoning is only as settled as its premise.** ADR-0072 is **Proposed**, not Accepted, so §7 can still change before acceptance. If it ever admits local rounds to the ledger, this section's conclusion has to be re-argued rather than assumed — and the right move then is ADR-0072 extending itself to consume a field this ADR already obliges a producer to write, which is the direction its own problem statement prefers.

The axis distinction holds independently and is worth stating, but it describes the two fields rather than being what keeps them apart: the ledger's columns record a judgement of the *finding* and the caller's response to it, while `Acceptance:` records a criterion for the *remedy*, consumed by the fixer while fixing rather than by a ledger reader afterwards. That is why the field belongs in the agent's report. The "no fragment" rule is why there is no conflict to resolve.

### 7. Mechanization, and its stated bound
`scripts/check_reviewer_agents.py` gains a third assertion: `.claude/agents/test-reviewer.md` must still name the field. Every place describing that gate drops its ADR-0068-only framing in the same change, because a gate that can fail for a reason its own name excludes teaches the reader to misfile the failure. Most adopt the wording "required invariants"; the test module's docstring names both owning ADRs instead.

No count of those places is given here. The number is decoration on the decision rather than part of it, and nothing mechanically holds a count written in prose in step with the string literals it would be counting.

**The bound is part of the decision.** The assertion reads the instruction in the agent file; it cannot observe a report. An agent carrying the instruction and omitting the field anyway fails nothing. It is worth having regardless, because the party harmed by a silent deletion — a fixer handed a finding with no criterion — is exactly the party who cannot notice a field they never knew to expect.

### Positive Consequences
- The fixer gets a pass/fail criterion they did not author, for a defect they are predisposed to misunderstand.
- Evidence the agent already produces stops being discarded.
- A remedy can be checked before it is re-reviewed, which is what shortens the loop.

### Negative Consequences / Tradeoffs
- **This is not a general answer to remedy churn, and must not be cited as one.** A criterion attached to `test-reviewer`'s findings covers `test-reviewer`'s findings. Defects can still surface in later bot lenses after this agent's rounds have converged, and by construction nothing in this decision reaches them. The field targets the number of trips through review, not the need for it.
- Reports get longer, and a field that is sometimes `none` invites being filled reflexively. §4 is the mitigation and it is prose, not a gate.
- Self-verification misses a substantial fraction of defects regardless of model capability, so the second pair of eyes remains necessary.

## Pros and Cons of the Options

### Option 1 — leave it as narration
Free, and already happening. But narration is unstructured: the fixer has to notice it, recognise it as a criterion, and reconstruct it. Nothing obliges it to be present at all, and on findings where it was absent nobody could tell.

### Option 2 — mutation-first for the fixer
Puts the burden on the party who has not run the experiment and who holds the misunderstanding that caused the defect. It also depends on the fixer remembering, which is the mechanism the first Decision Driver above rejects.

### Option 3 — the `Acceptance:` field (chosen)
The criterion arrives *in the report*, from the party that already measured it. Small: one field on an existing output, no change to the mandate, no new artifact.

### Option 4 — both reviewers
Rejected per §5. `spec-reviewer` would have to invent criteria for documentary findings, which is precisely the failure §4 names.

### Option 5 — put it in ADR-0072's ledger
Rejected per §6. Wrong axis and wrong reader: the ledger is read after the round, the fixer needs it during.

## Links
- Extends: [ADR-0068](0068-reviewer-isolation-worktrees.md) — supplies the snapshot worktree in which a criterion can be *measured* rather than proposed, and the read-only fallback that makes the distinction in §3 necessary
- Related: [ADR-0072](0072-review-pipeline-and-ledgers.md) — owns the round-ledger's per-finding columns; §6 states why this field is not one of them
- Implemented by: `.claude/agents/test-reviewer.md` (§ Report format), gated by `scripts/check_reviewer_agents.py`
