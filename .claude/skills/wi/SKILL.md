---
name: wi
description: Start a phase work item — sync and branch off origin/main, load the memory/ADRs/specs that own it, stub its decisions ADR, then ask the clarifying questions. Use when beginning a numbered work item, e.g. "/wi 3 3" for Phase 3 WI-3.
---

# /wi — work-item kickoff

Args: the phase and work-item numbers (`/wi 3 3` = Phase 3, WI-3). If they are missing or
ambiguous, ask before doing anything else.

**This skill never starts implementing.** It ends by asking clarifying questions and waiting for an
explicit go. Never start a work item without it (`project_dev_plan_inputs`).

## 1. Sync and branch

```bash
git fetch origin --prune
git log origin/main --oneline -3
```

- Branch off **`origin/main`, never local `main`** — local main is routinely stale here. At Phase 3
  WI-2 kickoff it was 4 commits behind, and the preceding WI's work existed only on the remote.
- Name the branch `phase<N>-wi<M>-<slug>` (e.g. `phase3-wi2-reference-data`).
- Note whether the previous WI actually landed on `origin/main`. This project's merge flow rebases
  and then closes the PR and deletes the branch, so a closed PR is not evidence the work is missing
  — check `git log origin/main`, not the PR state.
- Report local branches whose upstream is `[gone]`:
  `git branch --format='%(refname:short) %(upstream:track)'`. Offer to delete the ones already
  merged into `origin/main`. Never delete an unmerged branch, and never delete without a go.

## 2. Load what owns this work item

Read these *before* asking anything — the questions have to be informed to be worth asking:

- The phase memory (`project_phase<N>_decisions`) and `project_dev_plan_inputs` — the WI's scope,
  its sequencing, and any gate that must already be resolved.
- `specs/development-plan.md` for the phase definition.
- The **owning ADRs** this WI implements, in full, plus any ADR they extend or concretize. These
  are the authority on what the WI must do; they routinely defer specifics to "the implementing
  WI PR" — those deferrals are the WI's real work.
- The specs the WI must update: `specs/api-reference.md` and `specs/data-model.md` (locate the
  `*TBD during implementation*` markers this WI replaces), and `specs/open-questions.md` for
  anything it resolves or defers.
- The modules and tests the WI touches.

Then state, in a few sentences, what the WI is and what it must land. That summary is the contract
the rest of the session works against.

## 3. Stub the decisions ADR

Create the next-numbered ADR as **Proposed**, following the WI-decisions pattern (ADR-0049, 0052,
0053, 0056, 0057): `specs/adr/00NN-<slug>-implementation-decisions.md`, with the MADR skeleton
(Status / Context and Problem Statement / Decision Drivers / Decision Outcome / Positive and
Negative Consequences / Consequences for Other Documents / Links). Add its row to the `## Index`
table in `specs/adr/README.md`.

Decisions get written into it **as they are made**, not reconstructed at the end — that
reconstruction is where decision records go wrong.

Confirm the docs gates still pass: `uv run python scripts/check_adr_index.py` and `uv run python scripts/check_spec_links.py`.

## 4. Ask the clarifying questions

Use `AskUserQuestion`. Ask only what would genuinely change the work and what the specs leave open
to the owner — scope and seed decisions, API surface choices, anything an owning ADR defers to the
implementing PR. For everything else, state the default you're taking and move on; a question with
an obvious answer is noise.

## 5. Plan, then delegate

Default execution — the owner's Phase-3+ cost lever (`project_dev_plan_inputs`): **Opus pins the
design and reviews; Sonnet subagents do the bulk implementation**, in passes with a review gate
between each.

- Write the design brief to the session scratchpad first. Pin anything load-bearing — schema DDL,
  security-relevant contracts, the exact shape of a write path — precisely enough that the subagent
  implements it rather than re-derives it. The brief is where the hard reasoning goes.
- Sequence passes that touch the same files; parallelize only genuinely independent ones. Review
  each pass's diff before starting the next — a wrong schema cascades.
- Run `spec-reviewer` and `test-reviewer` when the passes are done.
- **Flag, don't assume**, when a WI is security-critical (encryption, key derivation, tokens,
  process boundaries): recommend staying on Opus for that part and let the user decide.

Then stop and wait for the go.

## 6. How the work item ends

`/land` (gates + propose the commit) → `/ship` (commit + PR; `/ship coderabbit` also spends the
CodeRabbit chain) → further review chains as chosen (`/coderabbit-review`, `/copilot-review`).
Update the phase memory with the outcome — what landed, the non-obvious decisions, and what the
next WI inherits.
