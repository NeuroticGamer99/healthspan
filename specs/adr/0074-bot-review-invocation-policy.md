# ADR-0074: Bot Reviews Are Bounded by Repetition, Not by Authorization

## Status
Proposed

## Context and Problem Statement
Since 2026-07-26 the harness has required that every **re-review** by a cloud bot — CodeRabbit, Greptile, Copilot, Gemini — be explicitly requested by the owner before it is triggered. The rule was stated after a real failure: a Greptile re-trigger folded into "merge the PR per our standard process", inferring a review from a merge instruction.

The rule's stated justification was cost. Bot reviews were believed to be **metered per run**, with unknown per-bot limits, so each re-trigger was a discretionary spend that could not be undone. That premise is wrong, and was corrected by the owner on 2026-08-11: the reviews draw against a **monthly total, not an incremental charge**. Copilot could in principle bill, but its reviews are low-cost and — his words — "that doesn't justify making my tool flow harder."

The same correction named the hazard the rule was actually reaching for: *"I didn't want you running them non-stop in a loop, but an extra invocation every now and then is acceptable risk."* The concern is **repetition**, not authorization. Prompting was a poor instrument for it, because it taxes every single invocation — including the first, including the obviously-warranted one — to prevent a failure mode that only appears in bulk.

**The correction is a sweep rather than an edit, and that is the second problem.** The policy was never decided in one place. It is written into 22 lines across 10 files under `.claude/`, and into four lines of [ADR-0067](0067-unrepliable-finding-acknowledgement.md), with no document owning it. Correcting the prose without creating an owner reproduces the exact condition that made the correction expensive — the restatement-drift shape [open-questions.md](../open-questions.md) records as having defeated every hand-maintained inventory attempted against it.

**A third problem makes the sweep delicate.** The vocabulary of spending carries two unrelated claims in the same sentences. One is the dead budget claim. The other is a live soundness claim: that a chain which never ran and a chain that ran clean are **indistinguishable** to the merge gate, so silence is never proof a review happened. A find-and-replace over the word "spend" deletes both.

## Decision Drivers
- A control that fires on every instance to prevent a failure that only occurs in bulk is mispriced; the owner experienced it as friction, which is how controls get disabled rather than obeyed.
- The dead claim and the live claim share a vocabulary, so the correction must be read per site rather than swept — the risk is silently deleting the merge gate's justification.
- A policy restated in ten files and owned by none has already cost one sweep; correcting it without an owner buys the next one.
- The failure that produced the original rule was real and is not retracted. Inferring a review from an unrelated instruction was wrong; what changes is that the corrective is a bound on repetition rather than a prompt per invocation.
- Mechanizing the new bound is possible — `scripts/bot_review.py` funnels every trigger — but it is a new mechanism with its own configuration, and bundling it into a prose correction is how a remedy becomes the defect.

## Considered Options
1. **Leave the prose as it is** and rely on the corrected agent memory.
2. **Sweep the prose, record nothing** — the original recommendation in the session that surfaced this.
3. **This ADR states the policy once; the prose keeps its operational length but stops asserting the metered claim** — chosen.
4. **The full [ADR-0073](0073-operator-handoff-presentation.md) treatment** — one owning document, the ten files reduced to citations, a `check_doc_citations.py` registry row.
5. **Mechanize immediately** — a per-PR trigger counter in `scripts/bot_review.py`, landed with the correction.

## Decision Outcome

### 1. The policy
Cloud bot reviews draw against a monthly total. **Trigger them as the workflow calls for them — a stale review after fix commits, a blocked merge gate, a lens wanted on the final state — without prompting first.** Report what was triggered and why; do not stop and wait for permission.

**The bound is on the pattern, not the instance.** Never place a re-trigger inside a retry or polling loop. Never re-run the same bot on the same PR repeatedly within one working session. When a chain is about to run a third time on one PR, stop and say why rather than firing it. An occasional redundant invocation is accepted risk; a loop is not.

`/gemini-review` is unchanged and remains user-named, for a reason that was never cost: the free AI Studio tier is a genuinely exhaustible quota, the workflow usually fails on it, and its absence never blocks a merge.

### 2. What the correction must not delete
Three claims share the retired vocabulary and survive intact. They are listed here so the sweep has a reference and so a future reader does not mistake them for residue:

- **An absent artifact is not proof that a chain never ran, and never proof that it did** — a clean run leaves no artifact either. `/squash-merge` reports `NO FINDINGS POSTED` rather than `NOTHING OUTSTANDING` for precisely this reason, and that distinction is load-bearing at the point it blocks a merge.
- **Track what you triggered and read its `wait` result.** Never infer from silence whether a review ran.
- **Stop for the user's go before changing any code** in every review skill. That governs *applying findings* and has nothing to do with triggering reviews.

Also unchanged: `scripts/bot_review.py`'s fetch and triage mechanics — which review object, which timestamp floor — which are correctness rather than economy.

### 3. Vocabulary
The language of spending is retired **where it implies a per-run charge for CodeRabbit, Greptile or Copilot** and replaced with running or triggering. It is not retired where "spent" merely meant "run" and the surrounding claim is one of the survivors above; those sites are reworded for consistency, not for meaning.

**A third branch exists and is easy to miss, because it is the one case where the retired belief is true.** `/gemini-review` draws on an exhaustible free-tier quota (§1), so language calling *it* metered is accurate and stays exactly as written — `specs/open-questions.md`'s account of the Gemini lens is the worked example. Sorting by the question below without this branch sends those sentences to the first branch and sweeps a true statement.

**What is deliberately left alone, because a sweep that cannot say what it excluded is not reproducible:**

- **Every use where the thing being spent is not a bot review.** Stated as a rule rather than a list, because this repository has watched enumerations of "where the copies are" go wrong every time one was attempted, and an exclusion list is that same artifact. **The rule is one question about the referent: what is being spent?** If the answer is a bot review — the run, the chain, the lens — the sentence is in scope, and the **two paragraphs opening this section** decide which way it goes; the second of them is the Gemini branch, and skipping it is how a true statement gets swept. If the answer is anything else — time, attention, a build, a CI minute, a polling window, a subprocess — the sentence is outside this decision and stays as written. The question generalizes where a list of nouns does not, which is the whole point: the first draft of this bullet *was* a list of four nouns, and it missed two sentences in `.greptile/README.md` whose referent was the review run itself. Copilot's `effort parameter` is outside by a different route again — it matches the search only as a substring.
- **`.greptile/README.md`'s reasoning, though not its vocabulary.** Its sentences are swept by the rule above like anywhere else. What is left alone is the *argument*: the manual-trigger decision never rested on money, the cost it names is *triage attention*, and what changed was which commit the review lands on. No count of the edits is given here, because this bullet has already been wrong about that once.
- **The user-facing message strings in `scripts/bot_review.py`, and only those.** Its internal comments are reworded with everything else. "Unspent chain" survives in the printed `NO FINDINGS POSTED` text because that message *is* the first survivor claim in §2, stated at the one point a human reads it. Rewording it would gain nothing at the level of what is claimed. **No test pinned that clause when this was written** — `test_outstanding_says_no_findings_rather_than_nothing_outstanding` asserts on `NO FINDINGS POSTED`, on `NOTHING OUTSTANDING` being absent, and on "not evidence any bot reviewed it", none of which touches it. The test is named rather than cited by line, because a line number is the kind of claim this ADR declines to make elsewhere. It is still a point-in-time measurement and nothing holds it true, so read it as the reason this exclusion rests on the claim being load-bearing where it stands, rather than as a standing fact about coverage.
- **Prior ADRs' own prose**, including [ADR-0069](0069-local-checkpoint-commits.md)'s "purchased external loop". An ADR records what was decided when it was decided; correcting the vocabulary inside one would rewrite history rather than supersede it. This ADR is the correction, and [ADR-0067](0067-unrepliable-finding-acknowledgement.md) gains a navigation link because its *driver* is invalidated, which a link is the right weight for.

### 4. ADR-0067's Decision Driver weakens; its decision stands
[ADR-0067](0067-unrepliable-finding-acknowledgement.md) lists "re-triggering a bot is metered and requires explicit authorization" among its drivers, and its Considered Options weigh option 2 partly on that cost. **The driver is invalidated; the outcome is not.** The Greptile summary-only shape remains structurally unclearable — the gate counts comment objects and the only possible answer is a PR-level comment — and re-triggering still re-reviews unchanged code and may reproduce the same shape. Option 3 still wins, by a narrower margin. ADR-0067 is Proposed and its content is left as written; it gains only a navigation link, per this repository's extend-don't-modify rule.

One consequence of the correction is worth stating because ADR-0067 reasoned about it: the dilemma that shape created — override the gate, or pay for a re-trigger — was expensive only on the second horn. **It no longer is.** Re-triggering is now an ordinary move, which makes the choice a matter of whether a fresh review would say anything new rather than whether it is affordable.

### Positive Consequences
- The instrument now matches the hazard: an occasional redundant review costs the owner nothing measurable, and a loop is what the rule names.
- The policy has an owner. A future change edits one document and the citations to it, rather than rediscovering ten sites.
- The merge gate's justification is written down independently of the budget claim it was tangled with, so the next sweep cannot delete it by accident.

### Negative Consequences / Tradeoffs
- **Nothing enforces the new bound.** It is author discipline at the moment of use, exactly like the rule it replaces, and a session that loops will not be stopped by this document. The mechanization that would stop it is deliberately deferred (option 5 below).
- Another ADR at Proposed, on a backlog already flagged as overdue. No count is stated here on purpose: acceptance freezes this text while the backlog keeps moving, so a number written now is wrong by the time it is read. Accepted deliberately — the owner is holding the harness ADRs to close as a group.
- The prose keeps ten independent statements. Option 4 would have removed them, and this ADR declines it — so the restatement-drift exposure is *reduced by having an owner* and not *closed*. `open-questions.md` already records why collapsing them is not automatic: several deliberately say the same thing at different lengths for different audiences.
- Retiring a vocabulary across ten files is a judgement per site, and a site read wrongly deletes a live claim silently. §2 is the mitigation, not a guarantee.

## Pros and Cons of the Options

### Option 1 — leave the prose
- Pro: no diff, no review surface.
- Con: the repository would assert a rule the owner has retracted, and any session reading it reintroduces the friction. Agent memory is machine-local and unversioned; the repo is what a fresh session and every reviewer reads.

### Option 2 — sweep, record nothing
- Pro: the tightest possible change; the correction is factual, and one does not write an ADR to retract an overcorrection.
- Con: a policy reversal is invisible in a prose diff — a reviewer sees deletions, not a decision.
- Con: leaves the drift mechanism fully intact, which is the condition that made this sweep necessary.
- Con: the deferred mechanization would then need to establish the policy from scratch in its own ADR.

### Option 3 — ADR plus a bounded prose sweep (chosen)
- Pro: records the reversal and its rationale where governance looks for it, at one document's cost.
- Pro: gives the eventual trigger counter a home — its threshold becomes a config knob owned by this ADR rather than a new decision.
- Con: does not reduce the number of prose copies; see the tradeoffs above.

### Option 4 — full owning-document treatment
- Pro: the only option that closes restatement drift, and the repository has the mechanism built and tested.
- Con: a restructuring of ten files, several of which need their own length for their own reader — a much larger change than the correction that prompted it, landing in the same PR as delicate judgement work.
- Con: the per-copy question it requires ("is this length doing work a citation could not?") is open in `open-questions.md` and is answered per copy by whoever edits one, not in bulk here.

### Option 5 — mechanize now
- Pro: the bound would be enforced rather than asserted, and `scripts/bot_review.py` already funnels every trigger, so the hook point exists.
- Con: a new mechanism with a threshold to choose, arriving inside a prose correction — the shape where the remedy carries more risk than the finding it answers.
- Con: the correct threshold is unknown. "A third run on one PR" is a guess, and guessing it into a gate is worse than stating it as discipline until the pattern is observed.

## Links
- Extends: [ADR-0067](0067-unrepliable-finding-acknowledgement.md) — whose "re-triggering is metered and requires explicit authorization" driver this invalidates while leaving its decision standing
- Related: [ADR-0064](0064-reviewer-workflow-trust-boundary.md) — the Gemini reviewer's trust boundary; its opt-in posture is unchanged here and rests on quota, not cost
- Related: [ADR-0073](0073-operator-handoff-presentation.md) — the owning-document-plus-citations pattern this ADR considers and declines for now
- Related: [open-questions.md](../open-questions.md) — restatement drift, and the parked skill-frontmatter question whose rejected option depended on this policy
