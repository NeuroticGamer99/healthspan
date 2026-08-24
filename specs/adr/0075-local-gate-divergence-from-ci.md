# ADR-0075: When a Local Gate May Diverge from Its CI Step (extends ADR-0045)

## Status
Proposed

## Context and Problem Statement
[ADR-0045](0045-repository-workflow-and-ci-enforcement.md) decides the enforcement mechanics around this repository's CI gates: what protects `main`, which merge methods are allowed, what `ci-ok` aggregates, and that CI — not any local run — is the authority. It says nothing about the *local* side, because at the time there was no local side to say anything about: sessions reconstructed gate commands from memory, one command at a time.

`scripts/run_gates.py` gave the local side a shape. It reads every gate command and every pinned version out of `.github/workflows/ci.yml` at each invocation — derive, do not restate — so the local run is CI's run by construction. Except where it deliberately is not:

| Divergence | Local form | CI form |
|---|---|---|
| pytest | `-n auto` | serial on the ubuntu/macOS legs; `-n auto` on windows-latest |
| pytest's canary sink | `CANARY_CAPTURE_DIR` rooted in a scratch directory | the bare `canary-logs`, written into the workspace |
| containment | `--scope branch` | `--scope history` |
| lockfile | `uv lock --check` | `uv sync --locked` |
| pyright and pytest | `uv run --locked --with …`, built by `_uv_run` | bare `uv run --with …`, after that job's own `uv sync --locked` step |

**Five differences across four gates.** The runner's module docstring presents three of them, folding the canary sink into the pytest bullet rather than counting it separately; the fifth is stated at `_uv_run`, where the flag is applied, rather than in that list. The table above is what to diff against, because §4 makes "a further divergence" the trigger for citing this ADR and a count is the wrong thing to hang that on.

The fifth row is **class 1**, and it arrived by the route §4 predicts — implemented first, cited afterwards (CodeRabbit, PR #100). Without `--locked`, a local `uv run` re-locks whenever `pyproject.toml` has moved ahead of `uv.lock` and **rewrites that tracked file** from a step that reads as read-only. CI can afford that: it discards its workspace, and its pyright and pytest jobs already hold the lock steady through a preceding `uv sync --locked`, so the bare `uv run` there is not the unguarded form it resembles. A developer's tree has neither property, which is what the flag buys and why the same question is being asked on both sides.

Two were decided before the runner existed and are only *applied* by it. `-n auto` locally is the pre-existing norm [ADR-0063](0063-parallel-ci-test-execution.md) records in its Context; `--scope branch` is the scope [ADR-0070](0070-personal-data-containment-gate.md) §2 assigns to `/land` by name. **The other two have no owner.** The lockfile check and the scratch-rooted canary sink were both invented by the runner and are stated only in that module's docstring. Their test cover differs, and the difference is worth stating precisely: `test_the_lockfile_gate_checks_rather_than_syncs` pins the lockfile form *deliberately*, while the canary sink is pinned only *incidentally* — `test_the_real_canary_step_defers_its_argv_until_the_worker_logs_exist` asserts the sink and the scan agree on a directory, so restoring CI's bare value in one of them reddens it (measured), but nothing asserts that the directory is scratch-rooted as such.

The problem is not the invocation — an invocation detail is routing rule 6 and needs no record. The problem is that these share a **rule**, that rule is now precedent, and the next divergence will cite it whether or not anyone wrote it down. That is CLAUDE.md routing rule 1's operative clause: *anything that constrains future decisions*.

There is a second, quieter thing to record. The runner's anti-drift test — `test_every_ci_run_step_is_claimed` — compares gate **identity** (which CI step a gate reproduces) rather than argv. That choice is what makes deliberate divergence expressible at all: an argv comparison would have failed on every row of the table. Without a policy beside it, a future reader has to read it as an oversight, and "tighten the drift test to compare commands" is the natural, wrong repair.

## Decision Drivers
- The rule is already precedent in code and prose; an unwritten precedent gets cited anyway, and the citation is where it stops being examined. This repository has watched exactly that shape — *"this needs no ADR because that one needed none"* — wave changes through.
- The two owned divergences are owned in ADRs about *other* subjects. Neither ADR-0063 nor ADR-0070 states a general rule, so neither can be cited for the next case without stretching it.
- The enabling mechanism (identity, not argv) reads as looseness rather than as design. A gate whose deliberate slack looks accidental invites a tightening that breaks every working divergence at once.
- **A rule stated as one admission class rejects most of what it must sanction.** Only the lockfile check and the canary sink are pure side-effect purchases; pytest's justification is wall-clock and containment's is a deliberately narrower question. A rule that rejects its own instances does not get applied strictly — it gets routed around, which is the unexamined-precedent failure the first driver names, arrived at from the opposite direction.
- Divergence is genuinely dangerous in the general case: a local gate that answers an easier question than CI's is the silent-partial-green failure the runner exists to prevent. The policy has to say what is *not* permitted as clearly as what is.
- CI stays authoritative regardless (ADR-0045 §2, §4). Nothing here can weaken the merge gate, which bounds the blast radius of getting this rule wrong.

## Considered Options
1. **Record nothing** — the divergence is an invocation detail, routing rule 6; the docstring and the test are the record.
2. **A new standalone ADR** owning local-gate policy in general.
3. **A short ADR extending ADR-0045 with the divergence rule only** — chosen.
4. **Amend the runner's docstring** to state the rule where the divergences are, and stop there.

## Decision Outcome

### 1. The rule
**A local gate may differ from its CI step in one of three ways, and only these. The difference is stated where the gate is defined, and CI's form remains the authority in every case (ADR-0045 §2, §4).**

1. **Side-effect purchase** — the same question, asked by a command that avoids a side effect CI can afford because it discards its runner. The **lockfile** gate: `uv sync --locked` would install into the developer's environment to answer a question `uv lock --check` answers without touching it. The **canary sink** is the same class: CI writes `canary-logs/` into a workspace it throws away, where a local run would drop that directory into the repository, so the runner roots it in scratch — reading the *name* from `ci.yml` so the sink and the scan cannot disagree about it.
2. **Cost purchase** — the same question, asked in an execution mode that avoids a cost CI absorbs and a developer's edit-run loop cannot. The **pytest** gate: ADR-0063 measured 372.8 s serial against 110.9 s at CI's worker count, and the serial form has run past this environment's command timeout and been killed.
3. **Declared narrowing** — a genuinely *narrower* question, admissible only when two things hold: the local caller's question really is the narrower one, and the narrowing is named at the gate. (That CI's broader form stays the merge authority is *not* a third condition. ADR-0045 makes it true of every gate in every class, so it discriminates nothing — stated here because listing it as a condition would make this class look better guarded than it is.) The **containment** gate: `--scope branch` asks "is what I am about to commit clean", where CI's `--scope history` walks `--all` and fails a PR over a personal file on any pushed branch. `ci.yml` states the relationship in that step's own comment — the history scan "is the backstop, not the control … it detects rather than prevents; prevention is the same script run pre-push".

Classes 1 and 2 must fail on exactly the same property as CI's form. Class 3 changes what is asked, by definition — which is why it carries conditions the others do not, and why §2's prohibition is on narrowing that is **silent**, not on narrowing as such. The containment gate meets the naming condition visibly and by construction: the gate's registry `summary` carries the words "(branch scope)" and `--list` prints that summary verbatim, so the reduction is legible at the point of use rather than inferable from the argv.

**Of class 3's two conditions only the second is checkable, and the first is this ADR's soft spot.** "Genuinely narrower" is the clause a future author would reach for to bless a local gate that simply checks less; nothing but review stands in front of it. Read class 3 sceptically, and prefer a new ADR to a stretched precedent when the case is arguable.

**Not every difference between the two files is one of these, and the boundary needs saying** — otherwise the next reader diffs the runner against `ci.yml`, finds something the table does not list, and concludes the enumeration has rotted. Two differences exist today that the table deliberately omits, for two *different* reasons:

- `markdown-lint` assembles its own batches where CI pipes `git ls-files -z` into `xargs -0 -r`. The `git ls-files` arguments are identical on both sides, and neither form *guarantees* a single invocation — both split a long enough list, against different limits: `xargs` on the limit of the platform it runs on, which for CI's docs job is `ubuntu-latest`, and the runner on assembled length, because Windows caps a command line near 32k. At this repository's current size neither actually splits. Nothing is bought here; this one is spelling.
- **Every gate the runner builds through `_docs_gate`** runs `sys.executable` against an absolute script path where CI's step text spells `python3 scripts/…`, and the runner's `--list` renders CI's spelling so the displayed command stays pasteable. That set is wider than the docs-consistency job — the **containment** gate is built the same way, so it differs from its CI step in two places at once: the declared narrowing above, and this. The script is the same, and the local form avoids a PATH lookup that resolves differently on Windows — but calling this spelling would overstate it. There is **no `actions/setup-python` anywhere in `.github/`**, so CI's `python3` is the runner image's system interpreter while the local one is whatever `uv` resolved against `requires-python`. The two can be different versions, and a construct newer than CI's interpreter would then pass locally and fail there. That is not a divergence the rule admits — nothing is being bought — it is an **unclosed risk**, recorded rather than blessed. Closing it means pinning CI's Python for these steps, which is a CI change and out of scope here.

  **The discriminator is CI's side, not the runner's**, and that is easy to get backwards: the canary scan reaches `sys.executable` through the same `_python()` helper as everything above, yet carries none of this risk, because *its* CI step spells `uv run python scripts/scan_log_canary.py` and so resolves the same uv-managed interpreter the runner itself runs under. A reader who greps for the helper and takes the result as the exposed set will include it wrongly — an earlier draft of this bullet did exactly that.

So the discriminator for the table is: *is one of the three purchases being made?* A difference that buys nothing is either spelling or a gap nobody has closed, and those are not the same thing.

**Neither of those two is fully regression-protected, and both rest largely on inspection** — they are instances of the gap §3 names, not exceptions to it. Measured: dropping `--others` and the `:(exclude)specs/personal/**` pathspec from the local `git ls-files` call leaves `tests/test_run_gates.py` green. That pathspec is a guard for a hypothetical future *tracked* file rather than a live containment control — `specs/personal/` is gitignored, so `git ls-files` does not list it either way — but the missing detector is real, which is why this paragraph describes the current state rather than promising it will hold. The interpreter case has partial cover: `test_every_local_gate_names_a_runnable_program` reddens if `_python()` names a program that cannot start, though nothing asserts it is the interpreter CI's `python3` would resolve to — which is precisely the version risk above.

### 2. What is not permitted
Two different failures live here, and separating them is the point of the split below: the first and third bullets bound a gate that **runs differently**, the second bounds a gate that **does not run at all**. They are grouped because at the point of use they are confusable — both end with a local run reporting green over less than CI checks — and not because gate absence is a form of divergence. It is not, and §1 does not govern it.

**Divergence:**

- **A local gate must never answer a weaker question than its CI step *without saying so*.** Undeclared narrowing is not divergence — it is a gate that does not run, and `run_gates.py`'s docstring names silent partial green as the worst of the three failures it was built against. §1's class 3 is the only admissible narrowing, and its conditions are the whole difference between the two: the containment gate is legible as narrowed at the point of use, where a gate that quietly checked less would not be.
- **A divergence must be stated where the gate is defined.** The registry entry and the module docstring are the point of use; a divergence discoverable only by diffing against `ci.yml` is one nobody will discover.

**Absence:**

- **A gate this machine cannot run is reported as skipped and named in the summary, never omitted.** Two are: the macOS leg and gitleaks' hash-verified binary. Each carries a `ci_only_reason` — an *optional* field on the `Gate` dataclass, whose non-emptiness is enforced by `test_ci_only_gates_are_declared_with_a_reason` rather than by the registry's shape. The distinction matters because it is the test, not the type, that would have to be defeated to land a silent omission.

### 3. The drift test compares identity, deliberately
`test_every_ci_run_step_is_claimed` asserts that every named `run:` step in `ci.yml` is either reproduced by a registry gate or registered as a non-gate step — `NON_GATE_STEPS` (today, the `ci-ok` aggregate) or `NON_GATE_STEP_PREFIXES` (today, gitleaks' install step, matched by prefix so a version bump does not fail the test). It compares *which step a gate reproduces*, not the argv it runs. **That is what makes §1 expressible**, and it is recorded here so it is not "fixed". An argv comparison would fail on every row of the divergence table and force either their removal or a per-gate argv exemption list.

**That exemption list is not the same thing as the two allowlists the test already carries, and the line is worth drawing** — otherwise the objection above reads as an objection to allowlists in general, which the design would then be violating in its own test. `NON_GATE_STEPS` and `NON_GATE_STEP_PREFIXES` name CI steps that are **not gates**: a status aggregate and a tool install. Nothing is excused from a comparison there, because there is no gate to compare. An argv exemption list would be the other thing entirely — a gate excused from the one check it is subject to, with the excuse living in the same file as the drift it hides.

The cost is stated rather than hidden: identity comparison cannot catch a gate that keeps its CI step's *name* while its command drifts into answering something else. Nothing mechanized closes that. What stands in its place is per-gate: the lockfile divergence is pinned by `test_the_lockfile_gate_checks_rather_than_syncs`, which asserts `sync` is absent from the argv and says why in its docstring.

### 4. Ownership
This ADR owns the rule. The instances keep their existing owners — ADR-0063 for `-n auto`, ADR-0070 §2 for `--scope branch` — and this ADR takes the three the runner invented and nothing owned: the lockfile check, the scratch-rooted canary sink, and the structural `--locked` on every local `uv run`. A further divergence cites this ADR; if it satisfies none of §1's three classes, it needs its own ADR rather than this one's precedent. A case that only *arguably* satisfies class 3 counts as not satisfying it — §1 says why that class in particular earns the stricter reading.

### Positive Consequences
- The next divergence has a rule to be tested against instead of a precedent chain to be waved through.
- The identity-comparison design is documented as design, so tightening it becomes a decision rather than a cleanup.
- The lockfile divergence acquires an owner without moving where it is implemented or tested.

### Negative Consequences / Tradeoffs
- **The strongest argument for this ADR does not hold, and saying so is part of the record.** "Someone 'fixes' the lockfile gate to match CI and nothing notices" is false — `test_the_lockfile_gate_checks_rather_than_syncs` reddens. The loss this ADR prevents is the *unexamined next case*, which is weaker and slower-acting than a broken gate.
- **ADR-0045's subject is CI enforcement, and a local runner's invocation choice sits at its edge.** Extending it here stretches its remit, and the next extension will stretch from this one. Recorded as a real cost, not dismissed: the alternative was a standalone ADR (option 2), rejected only because a rule this small reads better attached to the decision it qualifies than standing alone.
- Another ADR at Proposed, on a backlog already flagged as overdue.
- §1 is judgement, not a predicate. "A side effect free in a disposable runner and harmful in a developer's tree" needs a person to apply it, and nothing gates it.

## Pros and Cons of the Options

### Option 1 — record nothing
- Pro: the divergence is an invocation detail, and this repository's routing rules put invocation details in code and tests. The lockfile case *is* pinned by a test.
- Con: rule 6 is the residual, not the discriminator. It answers "no external contract"; it cannot answer rule 1's "constrains future decisions", which is the clause that applies here.
- Con: the argument that carried this position was that `run_gates.py` mirrors already-decided gates and is not a new decision surface. The lockfile gate is the one place it deliberately does not mirror CI — an argument from mirroring cannot cover the exception to mirroring.
- Con: two independent review lenses raised decision-capture on the runner's branch. Each was individually answerable; the convergence was not nothing.

### Option 2 — a standalone ADR
- Pro: no stretch of ADR-0045's remit, and room to say more about local gates than divergence.
- Con: there is nothing else to say yet. An ADR sized for future content is one that gets amended into a different subject.

### Option 3 — extend ADR-0045 (chosen)
- Pro: the rule lands beside the decision it qualifies, and ADR-0045's `## Links` already carries four `Extended by` entries, so the pattern is established.
- Con: the remit stretch above.

### Option 4 — docstring only
- Pro: the rule would sit exactly where a divergence is written, which is where it is needed.
- Con: a docstring is not where governance looks, and a rule that constrains future ADRs has to be findable from the ADR index. The docstring keeps its statement either way; this is not a replacement for it.

## Links
- Extends: [ADR-0045](0045-repository-workflow-and-ci-enforcement.md) — whose CI-enforcement mechanics this qualifies on the local side; CI remains authoritative
- Related: [ADR-0063](0063-parallel-ci-test-execution.md) — owns the `-n auto` divergence, and records the wall-clock measurement §1 cites
- Related: [ADR-0070](0070-personal-data-containment-gate.md) — §2 owns the `--scope branch` divergence by assigning that scope to `/land`
- Related: [testing-strategy.md](../testing-strategy.md) — CI Gates; the gate content this rule governs the local reproduction of
