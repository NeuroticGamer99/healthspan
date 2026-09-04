# ADR-0078: A Local Check Whose Subject Lives Outside the Repository (extends ADR-0077)

*(The filename keeps its original `session-start-reporting-hooks` slug for reference stability, as
`scripts/check_spec_links.py` keeps its own — the number is the identity. The `SessionStart` hook it
was named for was withdrawn before shipping; see option 1.)*

## Status
Proposed

## Context and Problem Statement

Two memories were orphaned on this project in a single day — a file written, its
`MEMORY.md` row never added. `MEMORY.md` is what loads at session start, so a file no row points at
is never recalled and the write accomplished nothing. **That failure is silent by construction**,
which is what separates it from every other defect class this repository gates: there is no red, no
exit code, no output at all. The corpus is also unprotected upstream — auto memory has no locking,
no merge and no conflict detection, and the upstream report of cross-session corruption
(anthropics/claude-code#23769) was auto-closed as a duplicate of a duplicate and locked with none of
its mitigations implemented. Detection is the only half available at this layer.

**CI cannot be the mechanism, and this is the load-bearing fact about the whole change.** The
subject lives outside the repository and is machine-local; the docs are explicit that auto memory is
"not shared across machines". So there is no CI leg to add and no row in the repo-invariants
register, and nothing here should be modelled on `check_adr_index.py`'s CI-gated shape. What CI
*can* run is the checker's **tests**, which build the directories they examine under `tmp_path`.
The distinction to hold onto is that the tests are gated and the check is not.

**This ADR originally proposed reaching the check from a `SessionStart` hook, and that hook was
withdrawn before it shipped.** Four external review rounds returned **15, 24, 26 and 27** findings
with no decline, and the severity of every early round traced to *hook context* rather than to the
checking logic: output injected into every session, an exit-code inversion that turned a finding
into a blank error notice, four quoting layers in a shell bootstrap, and a
`contextlib.suppress(BaseException)` wrap that silently falsified this ADR's own mutation table.

Every one of those totals is **15 reported plus the remainder each review demoted below its own
15-finding cap** — 0, 9, 11 and 12 respectively. The cap is the cap, not the defect count, and the
distinction matters twice over: round 4's demoted set included the personal-data containment item
that review called its own "would not let slide". Counted by heading and by numbered demoted item
against the reports themselves, so the figures are re-derivable rather than remembered; this
sentence read "15, 19 and 26" until that count was actually run, and the 19 had no derivation
anyone could reproduce. Classifying the fifteen round 3 reported: one
died with the hook, four were hook-*amplified* (the defect was in the checker; the hook made it
silent and permanent), and ten were checker logic that survived untouched. **Dropping the hook
removed the blast radius, not the defects** — which is why this ADR shrank rather than disappeared.
What is left needs deciding on its own terms: a check whose subject is outside the repository, run
from a skill, gated by nothing.

## Decision Drivers

- Detection is the only half available, so a detector that is easy to switch off is worth little.
- A check that no gate runs still needs an owner and an invocation, or it becomes a script nobody
  calls — the outcome this ADR exists to avoid.
- A check that reads outside the repository must **say so**; an undisclosed read of the operator's
  home is how a "hermetic" test suite turns machine-state-dependent.
- ADR-0077 §7 scoped itself to "this hook only" and warned that "a new mechanism plus a new policy
  inside one change is the shape this repository has repeatedly paid for". Withdrawing the hook is
  that warning being taken.

## Considered Options

1. **A `SessionStart` hook** — *withdrawn*. It fires automatically and needs no caller, which is a
   real property the chosen option gives up. Against it: the machinery is the defect class above,
   and it made every finding silent and permanent rather than loud and once.
2. **A `Stop` or `SessionEnd` hook** — *rejected*. It is automatic and fires *after* the session
   wrote its memories, which is closer to the failure than `SessionStart` ever was. Rejected because
   it is the identical machinery — the same bootstrap quoting, the same exit-code semantics, the
   same suppress wrap — so it would buy the frequency back at exactly the price just paid.
3. **A checker invoked from a skill** — *chosen*.
4. **Record nothing and run the checker by hand** — rejected. A check with no named caller is not a
   control; it is a script whose absence from a session is indistinguishable from a clean corpus.

## Decision Outcome

### 1. An ordinary check that gates nothing

`scripts/check_memory_index.py` reconciles the corpus against its `MEMORY.md`: files with no row
(orphans), rows with no file (dangling), duplicate rows, identity drift between a memory's
frontmatter `name:` and its filename, and an index past the load limit. It **exits 1 on a finding
and 0 when clean**, like every other `check_*.py` in this repository, and a failure to resolve
anything raises loudly rather than being swallowed. No CI job runs it and no gate depends on it;
that is a property of its subject, not a weakening.

### 2. One owner for the invocation

The command is spelled in exactly one place — a `/memory-check` skill — and other skills call that
skill rather than restating the script path and its flags. This repository has measured the
alternative: `/land` and `/ship` carried two copies of the gate commands and disagreed, with
`/land`'s copy being the one that could not run.

### 3. Silence when clean, and what absence forgives

`--quiet` suppresses the clean-run summary **and nothing else**: warnings still print, because a
caller that runs many times a session must be silent when there is nothing to say and must not be
silent when there is. A forward-reference wiki-link is legitimate and permanent by design, so an
earlier version that withheld the summary only when no warning existed cost two lines at every
invocation, forever.

`--skip-if-absent` forgives a *genuinely absent* corpus — no project directory, or no `memory/`
inside one — and nothing else. It does not forgive a directory that exists and has no index, and it
must never be allowed to paper over a resolution defect: it did exactly that once, when a wrong
project slug made every worktree session resolve nothing and the flag reported clean.

### 4. The reads it performs outside the repository, disclosed

The checker and its tests read four things outside `tmp_path`, and all four are named here because
an undisclosed one is what makes a suite's result depend on the machine it runs on:

1. the repository's own `.claude/settings.json`,
2. the repository's own `.claude/settings.local.json` — gitignored, present in the maintainer's
   checkout and absent from a fresh clone, which is exactly why omitting it from this list was a
   defect rather than a detail: it is machine-dependent by construction, `_settings_files` reads it
   for the same `autoMemoryDirectory` redirect as the file above, and a BOM'd or UTF-16 copy now
   *raises*,
3. the checker's source,
4. **the operator's home** — `~/.claude/settings.json` for an `autoMemoryDirectory` redirect, and
   `~/.claude/projects/` to match the project slug.

**The operator's home** was found by external review round 3 rather than disclosed: a test that
pinned no `HOME` read the real one, and a remedy in the same change turned an unreadable settings
file from a skip into a raise, making that test's "prints nothing" assertion depend on machine
state. (Named rather than numbered, because this sentence read "the third" until round 4 inserted
an item above it and silently repointed it at the checker's own source — a positional reference
into a list that is still being extended.) **Any test
reaching resolution must pin `HOME` and `USERPROFILE`.** A memory corpus is **treated here as**
personal data under [CLAUDE.md](../../CLAUDE.md)'s containment rule, so nothing read from it is ever
written into an error message beyond a filename, and the resolver names the slug it looked for
rather than enumerating the directories it found. `CLAUDE.md` does not name a memory corpus, so
that is an inference — though a short one, since its rule already covers "any notes that would
identify the database owner as an individual" and a memory recording the owner's background is
inside that clause directly. [`open-questions.md`](../open-questions.md) carries the reasoning, the
part that genuinely remains open (whether *counts* over the corpus are themselves provenance), and
the trigger that would settle it. The conservative half costs nothing and is what the code does
either way.

### 5. `MEMORY.md` is repaired with an anchored edit, never rewritten

Auto memory has no locking. A wholesale rewrite of the index drops any row a concurrent session
added between read and write — which is the same concurrent-write failure the duplicate-row check
exists to detect, caused by the tool meant to fix it. The checker's own remedy line says this, and
it is a rule for every caller, not advice.

### 6. Test obligations

ADR-0077 §8's obligations apply as written, minus the hook-wiring ones that died with the hook. What
this ADR adds: every reconciliation rule is pinned by a fixture that breaks exactly that rule, and a
remedy for an externally-reported defect is pinned by a test that **fails against the code as it was**
— demonstrated by mutation, not asserted. This branch measured why: four generations of this
checker's parser shipped silent behaviour changes and every one landed with a passing test written
for it. `scripts/diff_check_memory_index.py` exists for that reason and is part of the obligation —
a behaviour change must be *chosen* with an old-vs-new diff in hand, not discovered later.

### 7. Scope

This ADR governs this check and its invocation. It authorizes no other reporting mechanism, no hook,
and no new event. A second check of this shape is a new decision.

## Consequences

### Positive

- The orphan class is detectable at all, and a finding is loud and once rather than silent and
  permanent.
- The checker is an ordinary script with ordinary exit codes, so it is testable, runnable by hand,
  and carries none of the bootstrap machinery three review rounds found defects in.
- Withdrawing the hook removed an entire surface: the exit-code inversion, the shell quoting layers,
  the suppress wrap, and the per-session cost of output injected into every session.

### Negative / Tradeoffs

- **A skill runs only when a caller invokes it, so nothing catches a session that writes a memory
  and then does nothing else.** The hook did. This is a genuine weakening and the honest reason to
  revisit; it is recorded in [open-questions.md](../open-questions.md) with the deferred hook, and
  the trigger is an orphan reaching the live corpus uncaught.
- The invocation is a convention rather than a mechanism. Nothing fails if a caller stops calling.
- The check reads the operator's home, which no repository gate does. §4 is the mitigation and the
  disclosure is the whole of it.

## Links

- Extends: [ADR-0077](0077-local-invocation-hooks.md) — supplies the §8 test obligations this
  applies; the bootstrap it also supplied is no longer used here, the hook having been withdrawn
- Related: [ADR-0045](0045-repository-workflow-and-ci-enforcement.md) §6 — the CI gate shape this
  check deliberately does *not* take, because its subject is outside the repository
- Related: [open-questions.md](../open-questions.md) — the deferred hook and the detection-frequency
  cost of withdrawing it
- Related: [CLAUDE.md](../../CLAUDE.md) — personal-data containment, which is why §4 discloses every
  read outside the repository
