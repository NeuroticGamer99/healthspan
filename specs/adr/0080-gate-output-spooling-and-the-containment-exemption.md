# ADR-0080: Gate Output Is Spooled to a File, and One Gate Is Exempt

## Status
Proposed

## Context and Problem Statement
`scripts/run_gates.py` streamed every gate's output to the console. That is right for a human at a terminal and wrong for the caller that actually runs it: an agent session, where a tool result is re-sent with every later request for the rest of the session. The output is read once, for a pass or a fail, and paid for hundreds of times after that.

Measured 2026-09-09 over this project's own session transcripts, so the scale is not asserted from intuition:

| | |
|---|---|
| Shell results whose command runs a gate or a test suite | **4,163 invocations**, ~**1.22 M** tokens of output entering session windows — **6.7%** of all tool-result output |
| Shell tool results as a share of modelled lifetime window cost | **44.4%**, the largest single source |
| Times the average shell result is re-sent before its session ends | **531** |

Method: token counts are a characters-÷-4 estimate over every `tool_result` block in the transcripts, bucketed by whether the originating command names a gate or test runner; the share and amplification figures come from replaying each assistant message's recorded `usage` block. Both are estimates — the amplification figure in particular is an upper bound, because it assumes a token entering the window survives to the end of the session and compaction breaks that.

**Described is not reproducible, and an external review was right to say so.** The paragraph above names the method but supplies no corpus boundary, no command classifier and no calculation artifact, so a reader cannot re-derive these numbers from it — and the transcripts it ran over are outside this repository and are not a fixed corpus, so the same method run later answers differently. Treat the figures as this decision's recorded evidence rather than as a check anyone can repeat. **The ranking is what the decision rests on, not the third digit**, and the ranking is the part that would have to invert to change the outcome: shell results would have to stop being the largest single source of window cost.

Writing that output to a file instead is a small change to already-tested code: the runner has teed a step's output to a file since ADR-0063's canary sink, and captured logs already land outside the repository. What it is not is a small change to the *surfaces* the repository's containment work has been tracking.

Two facts make this an ADR rather than routing rule 6.

1. **It creates a new persistence surface for gate output.** `Context.cleanup` removed the scratch directory unconditionally, and its docstring said why: it holds captured test output, the material the log-canary gate exists to police, outside the repository where the containment gate does not look. Retention reverses that. It has to: with the output no longer on the console, a path printed beside a failure must still resolve when someone goes to read it, and deleting the directory on the way out makes that path a lie.
2. **One gate's output is personal data.** `check_personal_containment.py` prints the offending repository path on a violation. [ADR-0079](0079-personal-data-outside-the-repository.md) §R2 is explicit that a recreated file's *name* is content-half personal data — "a filename under the old path is provenance" — and §4 sets the bar for any guard's surface: it must neither **replicate** a recreated file, **name** it (in stdout, a CI log, a manifest, a review comment), nor **read** it. Spooling that gate's output would write the one thing the gate exists to find into a file outliving the run.

The gate-behaviour axis is settled elsewhere and needs no ruling here: [ADR-0078](0078-gate-growth-beyond-an-enumerated-contract.md) routes a gate change by whether it alters the set of properties a green run certifies, and this alters none — every gate runs the same command, asks the same question, and exits the same way. [ADR-0075](0075-local-gate-divergence-from-ci.md) §1's three divergence classes are likewise not engaged, because no gate diverges. What is new is where the bytes go, which neither ADR speaks to.

## Decision Drivers
- The measurement above is the whole motivation; nothing here is worth doing for tidiness.
- A failure must stay legible without going looking. A quiet default that hides a red gate trades one silent-partial-green for another, which is the failure `run_gates.py` exists to prevent.
- ADR-0079 §4's audit bar applies to a surface being *added* as much as to a guard being *removed*. An audit whose conclusion lives only in a comment is the shape ADR-0079 §1 records failing six consecutive times.
- Prevention by construction over argument. A surface that is never created needs no reasoning about how safe its contents are.

## Considered Options
1. **Stream as before; tell sessions to pipe through `tail`** — rejected: a pipe returns the pipe's status, and this repository has already shipped a confident false green from exactly that.
2. **Spool everything, argue the contents are safe** — rejected: it makes the containment gate's violation output durable and rests the safety of the whole scheme on prose.
3. **Spool everything except the gate whose output can name a personal path** (chosen).
4. **Spool nothing; keep the console and accept the cost** — rejected: it declines the only measured lever with a single patch point.

## Decision Outcome

### 1. Every step's output is spooled; the console keeps the command and the failures
Each step's combined output is written to a file in a per-run directory outside the repository. The console gets the gate heading and the `$ command` line it already got, and nothing else on a pass. On a failure the runner replays that step's **last 40 lines** and names the file holding the rest, so a red gate is legible where it happens, and it says how many earlier lines stayed in the file rather than letting a tail read as the whole failure.

`--verbose` restores the previous streaming — a tee'd child sees a pipe rather than a console and changes its colouring and buffering, so capturing under `--verbose` would not be the old behaviour. That restoration is not total, and saying so was a drafting error caught in review: the pytest gate assigns its own sink for the canary scan to read back, so under `--verbose` that one gate both streams *and* captures. A step is replayed only when it was actually suppressed, so nothing is printed twice.

Two properties of the captured child follow from the pipe and are stated here because neither is visible from the console:

- **`stderr` is merged into `stdout`**, so one file holds the failure in the order it happened. It also means a shell redirection no longer splits them the way it did: `run_gates.py > out.log` previously left a tool's diagnostics on the terminal through the inherited `fd 2`, and now leaves the terminal with the failure banner while the cause goes to the file. The replayed tail is what keeps the failure legible in both spellings.
- **`stdin` is `DEVNULL`.** A captured child has no console to prompt on, and gate steps are deliberately unbounded, so a child that asks a question would write it into the log and then block on `fd 0` forever behind a `$ …` line indistinguishable from a slow test run. Failing fast is the only reportable outcome available.

Nothing about what a gate asserts, what it exits, or what the run summary prints changes. The exit code is carried inside the runner precisely so no pipe can swallow it.

### 2. The run directory is retained; a bounded number of runs survive
`Context.cleanup` no longer removes this run's directory. The **5 most recently completed runs** survive and older ones are pruned. Pruning is housekeeping and never fails a run, and it reports only what it verified gone rather than what it attempted — `rmtree` is called with `ignore_errors=True` precisely because a concurrent run may already have removed a directory, and that same tolerance leaves an undeletable one in place.

**Eligibility is an explicit completion mark, not recency.** The first draft ordered by modification time and claimed that this protected a concurrently-running invocation. It does not, and the claim was refuted by measurement: a directory's mtime does not advance while a log inside it is appended, so a long run's directory looks exactly as old as the moment it was created. Five short runs finishing during one test run would evict it mid-flight, taking the canary worker sink with it — leaving `scan_log_canary.py` to fail with no failing test or, where an open controller log survives its closed worker siblings, to scan the surviving subset and **pass**. A green built from a partial scan is the failure this whole runner exists to prevent, so recency had to go.

- A run writes a completion mark when it finishes. Only marked directories are prune candidates, ordered by when they were marked.
- An unmarked directory is a run in progress and is never counted against the retained number, so concurrent invocations do not compete for slots.
- An unmarked directory older than a **24-hour grace period** is treated as an orphan — a run killed before it could mark itself — and is collected. Without that, "unmarked means leave it alone" would be an unbounded promise.

**What bounding that promise costs, stated rather than left to be derived.** The grace period decides liveness from the directory's age, and age cannot prove the owning invocation exited. The measurement above is what makes this more than pedantry: because a directory's mtime advances when a step log is *created* and not while one is appended, a run hung inside a single step past the cutoff — or one spanning a suspend longer than it — is indistinguishable from a run killed before it could mark itself, and a concurrent invocation collects it. The consequence splits by platform, and the halves are not equally serious: on POSIX the tree goes whole, the canary scan matches nothing, and the literal glob it falls back on makes it exit 2 — loud, and fail-closed by design ([ADR-0063](0063-parallel-ci-test-execution.md)). On Windows the open logs refuse deletion while their closed siblings go, and the scan takes the surviving subset and **passes** — the same partial-scan green recency ordering was rejected for, reached on a 24-hour horizon instead of a five-slot race. This ADR keeps the bound: an unbounded promise holds disk forever, and reaching the residual needs a run live past the cutoff *and* a second invocation during that window. But the remedy — proving an exit rather than inferring it — is a lock-or-PID mechanism with cross-platform surface of its own, not a tightening of this one, and raising the grace period would not remove the class. Raised by Greptile on PR #106 and recorded in [open-questions.md](../open-questions.md) with the trigger that would settle it. `test_a_live_run_past_the_grace_period_is_collected_like_an_orphan` pins the misclassification as a characterization test, so a remedy proving an exit rather than inferring one fails it and is told to close the entry. What that test does **not** pin is the platform *difference* stated above: its `undeletable` helper models a filesystem refusing deletion on both legs rather than reproducing POSIX's unlink-an-open-file semantics, so the POSIX-goes-whole half rests on the reasoning here and not on a test. The POSIX stand-in is a non-writable containing directory, and whether that refuses anything turned out to be a property of the filesystem rather than of the platform. The helper therefore probes that property and skips where it does not hold, so a filesystem that would let the deletion through is a visible skip rather than a test passing for the wrong reason. `_mode_bits_refuse_deletion` carries the measurement, next to the code it justifies.

**The runner refuses a temp root inside the checkout.** Retention only makes sense somewhere the repository's own tooling does not reach, and `tempfile.gettempdir()` honours `TMPDIR`, `TEMP` and `TMP` — so "outside the repository", which this ADR and the runner's docstring both state, was a property of the machine rather than of the code. `_temp_root` now raises rather than spooling into the tree, checking the raw *and* resolved paths so a junction, a symlink or a relative root cannot reach it under an unequal name. Refusing rather than qualifying the sentence is the decision: a guard makes the claim true, where narrowing the prose would leave the behaviour and describe it more carefully. Raised by Copilot on PR #106.

What is removed at the end of a run depends on how the run ended:

- **On a green run, working material a gate registered as such** — today, exactly the canary worker sink. Nothing outside the run reads it, and it is the bulk of the volume: measured at ~104 MB of DEBUG-level per-worker capture for one full run, against ~530 KB of step logs.
- **On a red run it is kept.** A canary hit's report names the worker log and the line its match came from, and that report is the last thing printed before cleanup runs. Sweeping the sink there deletes the evidence between naming it and the operator opening it — the same rule that stopped `cleanup` deleting the run directory, one level in. Only failed runs pay the volume, and the same retention bounds them.
- Anything a future gate registers the same way. The distinction is *output someone may read afterwards* versus *material consumed inside the run*, and it is declared on the context rather than inferred from a path.

The retention count and the replay length are defaults of this script, owned here and stated above rather than left to the source. `tests/test_run_gates.py` asserts both values as literals and asserts this document still states them, because every test that derived its fixture size from the constant let it be changed to anything at all with the suite still green.

### 3. A gate whose output can name a personal path is never spooled
The `Gate` registry carries a per-gate declaration that its output must not be written to disk. Exactly one gate sets it: **containment**. Its violation output names the path it found, which ADR-0079 §R2 classes as provenance. It keeps the pre-change behaviour — inherited console, nothing on disk. The cost is the **two** lines it prints on a pass: the holds-clean line and the enumeration-only note under it. "One line" was written here and at two other sites before anyone ran it.

**The rule for future gates:** a gate that prints a path under the containment directory **by design** — as its function, on the condition it exists to detect — or that prints a health value, declares itself unspooled. This is not a judgement call to be made per run; it is a property of what the gate prints when it fails.

**"By design" is doing the work in that sentence, and an earlier draft read "can print", which is a different and much larger rule.** Almost every gate *can* name such a path: `ruff check .`, `ruff format`, `pyright`, `markdown-lint`, `spec-links` and `pytest` all walk the tree and all report `path:line:…` on a violation, so a recreation under the containment directory could surface through any of them. Read that way the rule exempts nearly the whole registry and there is no spooling left to speak of — and exempting some arbitrary subset of those gates would be worse than exempting none, because it spends the benefit while leaving the identical surface open through the gates not named. What distinguishes `containment` is not that the string can appear in its output but that emitting it *is* the output: it is the one gate whose sole purpose is to find that path and say which one it found, on a run that has already gone wrong. The others name it only incidentally, in a state this gate exists to stop first.

The exemption is scoped to the sink the *runner* assigns. A gate that chooses its own — only the pytest gate does, for the canary scan that reads it — keeps it.

### 4. The audit ADR-0079 §4 asks for, recorded rather than assumed
Every retained file, and why its retention is acceptable:

| Retained | Why it is safe to retain |
|---|---|
| the pytest gate's captured output | It is the file `scan_log_canary.py` reads. A fixture health value in it fails the run that produced it. This is the only retained file that claim is true of, and stating it of the others was a drafting error caught in review. |
| the canary scan's own report | Its manifest is derived from the synthetic fixture tree, so a hit names a synthetic value, never the owner's. The report echoes the *whole matched line*, not just the value, so the reasoning has to cover the line — and it does, for the same reason: the line is test output produced from that same synthetic tree. |
| lint, type, lockfile and docs output | Tool output over tracked files, with one residual case below. None of it reads the database, and the repository it reads holds no personal data by construction. |
| the containment gate's output | Not retained. §3. |

**Two gates reach untracked paths, not one, and the sweep is not clean.** Both `markdown-lint` and `spec-links` build their file list from the same `git ls-files --cached --others --exclude-standard -- '*.md'` query, so both can name a file that is present in the working tree but not tracked. Each filters the containment directory its own way, and neither filter is case-insensitive on every leg:

- `markdown-lint` excludes it with the pathspec `:(exclude)specs/personal/**`, which is **case-sensitive on every platform**. Measured in a scratch repository: it does not exclude `SPECS/PERSONAL/z.md` — and on a case-insensitive filesystem it is worse than the single mixed-case file that measurement suggests, because git records the directory under whichever spelling it saw first, so one mixed-case path makes the exclusion miss *every* file there. `:(exclude,icase)specs/personal/**` excludes them all.
- `spec-links` filters with `Path.is_relative_to`, which casefolds on Windows and does not on POSIX — so it excludes a mixed-case recreation on the local Windows leg and lints it on the Linux CI leg.

`specs/open-questions.md` ("Case-sensitivity of the lint carve-out for the personal path") recorded the `markdown-lint` asymmetry as acceptable, and named the residual surface precisely: "a lint log *naming* a mixed-case recreation". **This change moves where that log lives**, which is a change in the surface rather than in the reasoning, and it is recorded here rather than closed. It is deliberately *not* recorded as making the log durable rather than transient. An unspooled gate streams to the console, and how long that survives is a property of whoever is running the gate — a terminal scrollback, a CI job log, a session transcript — none of which this repository controls or can characterize, so calling the old behaviour transient would assume the most favourable of them. What this ADR can state without leaving the repository is narrower, and it is **two bounds over two states rather than one over both**. An *unmarked* directory is bounded by **age**: past the grace period it is collected. A *completed* one is bounded by **count**: it survives until five newer runs complete, and nothing ages it out — so if the gates are not run again it persists indefinitely, and the prune only ever runs as a side effect of another invocation. Said plainly because a security audit is the wrong place to imply a time limit the code does not enforce: **a retained log has no maximum lifetime.** What bounds it is how many more runs happen, and where the temp root is — which §2 makes a property of the code rather than of the machine. Closing it means widening both carve-outs, which is a change to the lint gate ([ADR-0062](0062-markdown-style-lint-gate.md)), to `check_spec_links.py`, and to `ci.yml` — larger than this ADR's scope and owed its own decision.

What bounds it meanwhile is a chain of independent preconditions, **not the single one an earlier draft claimed**. That draft said "the precondition is independently gated: a recreation under the containment directory, in any casing, fails `check_personal_containment.py`, whose own pathspec **is** `:(icase)`." Raised by Copilot on PR #106, whose comment named the selector case exactly. **It is not one bound, because the two gates are not ordered alike.** `markdown-lint` is ordered *behind* `containment`, so a full run stops before reaching it and only a run selecting it by name — which executes no other gate — gets that far. `spec-links` is ordered *ahead* of `containment` and therefore has no such protection at all: it runs first on every full run, and by the time the containment gate would fail, it has already produced whatever output it was going to. That is the sharper of the two, and `test_containment_precedes_the_gates_that_lint_the_tree` pins the relationship so the next reader gets an answer from the registry rather than from prose.

The guards differ too. `markdown-lint` excludes the directory with a pathspec; `spec-links` excludes it in Python with `Path.is_relative_to`, and `md_sources`' own docstring is explicit that this filter — not `.gitignore` — is its exclusion mechanism, because `--cached` lists a *force-added* tracked file whatever `.gitignore` says. So the first bullet below bounds `spec-links` only for the untracked case.

What bounds it is a conjunction, each part measured:

- **`.gitignore` catches it first, on any clone that folds case.** Both lint gates build their list with `git ls-files --cached --others --exclude-standard`, and `--exclude-standard` applies `.gitignore` *before* the pathspec is consulted at all. Measured in a scratch repository: with `core.ignorecase=true`, the root `specs/personal/` line excludes `SPECS/PERSONAL/z.md` and it never reaches the linter; with `core.ignorecase=false`, it does. Git chooses that value by probing the filesystem, so the case-sensitive spelling of the pathspec only ever matters on a clone that does not fold case.
- **The output is not produced on this repository's own legs.** Both local legs read `core.ignorecase=true` from the shared config, so both take the excluded branch above.
- **CI does not reach this code path.** `ci.yml` names each gate command directly and never names `scripts/run_gates.py`, so nothing spools there and nothing is retained. `test_the_gate_runner_is_not_reached_from_ci` pins that, and pins exactly that much: it reads `ci.yml`'s own text, so a step invoking a *wrapper* that runs the runner would satisfy it while falsifying this bullet. Resolving every `run:` step's script to see what it executes is the check that would close the gap, and it is not worth building for a bullet that is one conjunct of a defence-in-depth bound — but the tripwire covers the spelling anyone would actually write, and this sentence is the record of what it does not cover.
- **`markdown-lint` names a file only when it has a finding.** Measured: `pymarkdownlnt` scanning a clean file prints nothing and exits 0, so the recreation must also violate a markdown rule before its path appears in any log.

So this is defence in depth that has thinned in one narrow configuration — a selector run, on a clone that does not fold case, over a mixed-case recreation that also fails a lint rule — rather than a control that has failed. **The remedy is not to exempt these two gates**; §3 says why that reasoning generalizes to nearly the whole registry and closes nothing. It is to widen the carve-outs at their source, which is the parked work `specs/open-questions.md` routes to the containment-gate shrink audit and which fixes every gate at once. ADR-0075 §1 records that nothing detects either pathspec's removal, which applies unchanged.

### 5. Ownership
This ADR owns the spooling behaviour, the retention policy and its defaults, and the unspooled declaration. It does not touch ADR-0063's canary contract: the sink's name, location and fail-closed empty glob are unchanged, and the pytest gate's own capture is neither moved, renamed, nor written twice. It does not touch ADR-0075's divergence table, because no gate diverges. It does not touch ADR-0079's decision, which it applies rather than extends.

### Positive Consequences
- The dominant single source of gate-driven context cost is removed at its one patch point, with no call site edited and no instruction for anyone to remember.
- A failing gate is more legible than before, not less: the failure and its cause arrive together instead of at the end of a transcript.
- The full output of a run survives it, which the previous unconditional removal did not allow. A rare intermittent failure now leaves evidence by default.

### Negative Consequences / Tradeoffs
- Captured test output persists in the system temp directory across a bounded number of runs where it previously did not persist at all. Bounded is not zero.
- A gate's child sees a pipe rather than a console, so colour and buffering differ from an interactive run. `--verbose` is the way back.
- The unspooled declaration is a property an author must set correctly on a new gate. Nothing can infer what a script prints, so nothing can check it is *needed*. What is checked is the converse, as an equality over the registry — the set of unspooled gates is exactly `{containment}` — so a second gate quietly declaring itself exempt fails a test by name rather than passing unnoticed. Adding a gate that genuinely needs the exemption therefore costs an edit here as well, which is intended.

## Pros and Cons of the Options

### Option 1 — pipe through `tail` in prose
- Pro: no code change.
- Con: the pipe's exit status replaces the command's. This repository has measured that failure and the confident false green it produced. It is also an instruction to remember, and the gate runner's own docstring records an instruction with its reason going unread.

### Option 2 — spool everything
- Pro: one rule, no exceptions to get wrong.
- Con: makes the containment gate's violation output durable, and makes the safety of the scheme an argument about contents rather than a property of the design. The first draft of this change did exactly this, and review found it.

### Option 3 — spool everything except the gate that can name a personal path (chosen)
- Pro: the surface is closed by construction; the exception is declared at the point of use; the cost is two lines of console.
- Con: one more field on the gate registry, and a rule a future author has to apply.

### Option 4 — keep the console
- Pro: nothing changes.
- Con: declines the only lever in the measurement with a single patch point.

## Links
- Applies [ADR-0079](0079-personal-data-outside-the-repository.md) — §R2 is why §3 exempts the gate it does, and §4 is the audit §4 above answers. Applying is not extending, so ADR-0079 owes no reciprocal link and is not edited.
- Relates to [ADR-0075](0075-local-gate-divergence-from-ci.md) — owns the local runner's shape. No divergence class is engaged here, so its table is untouched and this ADR owns the new behaviour itself.
- Relates to [ADR-0063](0063-parallel-ci-test-execution.md) — the canary sink and scan, unchanged by this.
- Relates to [ADR-0070](0070-personal-data-containment-gate.md) — the gate §3 exempts.
- Relates to [ADR-0078](0078-gate-growth-beyond-an-enumerated-contract.md) — the gate-behaviour axis, which this change does not move.
