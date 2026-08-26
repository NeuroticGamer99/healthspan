# ADR-0078: When a Gate Grows a Check Its Owning ADR Did Not Enumerate (extends ADR-0045)

## Status
Proposed

## Context and Problem Statement
[ADR-0045](0045-repository-workflow-and-ci-enforcement.md) §6 enumerates what each CI gate verifies. For the ADR-index gate it lists four checks: every row's file exists, numbering matches, the Status cell equals the file's `## Status` field, and every ADR file has exactly one row. ADR-0045 is `Accepted`, and CLAUDE.md's ADR-governance rule 5 permits typo and link fixes to an Accepted ADR and nothing structural — so that enumeration cannot be extended in place, ever.

Gates grow anyway. Two changes to this one arrived within days of each other and were routed opposite ways, neither wrongly:

- **PR #100** made `check_adr_index.py`'s `strip_links` run on both sides of its status comparison. Recorded under routing rule 6, no ADR.
- **This change** added a per-row check that an `[ADR-NNNN](dest)` in a status value names that ADR's own file. Left as an open question until a bot review raised it as a blocking finding.

A third instance is older and went the other way: [ADR-0061](0061-markdown-link-check-gate.md) added a blocking link-check step to the same docs-consistency job and judged an extension ADR *required*, saying so in terms that name this problem exactly — the gate "changes what §6 documents the docs gate as running", and "a new blocking enforcement surface is a stronger step than report-only coverage, so CLAUDE.md rule 1 fires".

**So the repository has three instances, two routings, and no stated rule.** The distinction reached for in conversation — *a new script versus a new function inside an existing one* — does not survive contact: it is a fact about where code lives, and CLAUDE.md rule 1's discriminator is about what a reader needs to know without reading the source. An unwritten precedent gets cited anyway, and the citation is where it stops being examined; ADR-0075's first driver records this repository watching that exact shape wave changes through.

The cost of leaving it unstated is asymmetric and falls on the silent side. A change routed to rule 6 leaves ADR-0045 §6 under-reporting what a green run certifies, and nothing anywhere says so; a change routed to rule 1 costs one short ADR. Whoever adds the next check will reach for whichever precedent is nearer to hand.

## Decision Drivers
- **The enumeration is immutable but the behaviour is not.** Rule 5 guarantees the list can never be corrected in place, so every growth either gets a record elsewhere or silently makes an Accepted ADR wrong. There is no third outcome, and only one of the two is visible.
- **Two live precedents already disagree**, so the next author has cover for either answer. That is the unexamined-precedent failure arriving with the examination already skipped.
- **"New script versus new function" is not the question CLAUDE.md asks.** Rule 1 turns on whether a fact constrains future decisions or is needed without reading the source; file layout is routing rule 6's own example of something that is *not*.
- **A rule that fires on every change gets routed around.** PR #100 removed a false failure and genuinely needed no ADR; a rule demanding one there would be ignored within a month, taking the real cases with it.
- **CI stays authoritative regardless** (ADR-0045 §2, §4). Nothing here can weaken the merge gate, which bounds the blast radius of getting this rule wrong.

## Considered Options
1. **Record nothing** — let each change route itself; the docstring and tests are the record.
2. **Require an extension ADR for every change to gate behaviour** — simple, and rejects its own instances.
3. **Route on whether the change alters what a green run certifies** — chosen.
4. **Allow the enumeration to be edited in place** — rejected; it reverses governance rule 5 for one class of ADR.

## Decision Outcome

### 1. The rule
**A change to a gate requires a Proposed extension ADR when it alters the set of properties a green run certifies. Otherwise it is routing rule 6, and code and tests are the record.**

Ask one question: *after this change, does a green run guarantee something it did not guarantee before — or stop guaranteeing something it did?* Both directions qualify. Growth makes the owning ADR's enumeration silently incomplete; shrinkage weakens a gate, which is worse and must never be the quiet path.

What does **not** qualify, however much code moves:
- Removing a false positive. The set of certified properties is unchanged; the gate simply stops being wrong about them.
- Fixing a false negative **within an already-enumerated check**. The property was always claimed; the gate now delivers it.
- Refactoring, renaming, changing an error message, or moving a check between files.

### 2. The three instances, classified
Stated because a rule that cannot classify its own history is not yet a rule.

| Change | Does green certify a different set? | Routing |
|---|---|---|
| ADR-0061's link-check step | Yes — green newly certifies no dead relative link | Extension ADR (as taken) |
| PR #100's symmetric `strip_links` | No — same properties, one fewer false failure | Rule 6 (as taken) |
| This change's destination check | Yes — green newly certifies every status link names its own ADR's file | Extension ADR (this one) |

The rule therefore ratifies both existing precedents rather than overturning either, which is the test it had to pass: a rule invented to justify one decision would have had to call the other one wrong.

### 3. Where the record goes
The extension ADR carries the addition. **The owning ADR's enumeration is still never edited** — rule 5 is untouched by this decision. What the owning ADR gets is the navigation link rule 4 already permits: an `Extended by: [ADR-XXXX](…)` line in its `## Links` section. A reader arriving at ADR-0045 §6 finds the enumeration as written and the extensions listed below it.

This is deliberately not a mechanism for keeping §6 current. It cannot be one: §6 stays as it was written, and the reader has to follow the links. What it buys is that the links exist.

### 4. Ownership and what is not mechanized
This rule is owned here and cited by whoever adds the next check. **Nothing enforces it** — no gate can decide whether a change alters what green certifies, because that is a judgement about meaning, which is ADR-0070's rejected option and does not become viable by moving into a hook (ADR-0077 §7 draws the same boundary). The control is the `Decisions:` section every implementing PR already carries: a change to gate behaviour states which side of §1 it falls on, and "none" is an answer that has to be defended like any other.

### Positive Consequences
- The next author has one question to ask instead of two precedents to choose between.
- Both existing precedents stay valid, so nothing has to be revisited.
- The dangerous direction — a gate quietly certifying *less* — is explicitly the loud path.

### Negative Consequences / Tradeoffs
- **"What a green run certifies" is a judgement, not a measurement.** It is sharper than "new script versus new function" but it is not mechanical, and a determined author can argue a real growth into the false-positive column. The `Decisions:` section is the only check.
- **ADR-0045 §6 gets further from current with each extension.** This rule makes the drift *recorded* rather than *stopped*; a reader still has to follow six `Extended by:` links to learn what the docs-consistency job does today. Consolidating that is a separate question and is not answered here.
- **One more ADR per real growth**, which is a real cost on a gate that grows often. The false-positive carve-out in §1 is what keeps it from being one per change.

## Pros and Cons of the Options

### Option 1 — record nothing
Cheapest, and it is the status quo that produced two routings and no rule. Leaves the Accepted ADR silently wrong with nothing saying so.

### Option 2 — an extension ADR for every behaviour change
Simple to apply and rejects its own instances: PR #100 would have needed an ADR for removing a false failure. A rule that rejects what it must sanction gets routed around (ADR-0075's fourth driver, arrived at from the same direction).

### Option 3 — route on what a green run certifies (chosen)
Classifies all three known instances the way they were actually decided, and names a direction (shrinkage) that neither precedent covers. Costs a judgement call the `Decisions:` section has to carry.

### Option 4 — edit the enumeration in place
Would keep §6 current and cannot be taken: governance rule 5 makes an Accepted ADR's content immutable, and carving out an exception for enumerations reopens the question of what else is "just description".

## Links
- Extends: [ADR-0045](0045-repository-workflow-and-ci-enforcement.md) — §6, whose gate enumeration this rule governs the growth of
- Related: [ADR-0061](0061-markdown-link-check-gate.md) — the instance that judged an extension ADR required, and said why in terms this rule generalises
- Related: [ADR-0075](0075-local-gate-divergence-from-ci.md) — the sibling rule for when a *local* gate may diverge from its CI step; same owner, same shape of unwritten precedent
- Related: [ADR-0077](0077-local-invocation-hooks.md) — §7's boundary between a rule decidable from command text and one needing a judgement about content, which is why §4 leaves this unmechanized
- Related: [ADR-0070](0070-personal-data-containment-gate.md) — the enumeration/content split this rule's unmechanized half follows
- Related: [CLAUDE.md](../../CLAUDE.md) — decision-capture routing rules 1 and 6, which this ADR states the boundary between for one class of change
