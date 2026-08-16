# ADR-0077: A PreToolUse Hook May Refuse a Local Invocation That Cannot Work

## Status
Proposed

## Context and Problem Statement

[ADR-0075](0075-local-gate-divergence-from-ci.md) settled what a local gate may *be*. It did not
address how a session *reaches* one, and that turned out to be the part that fails.

`scripts/run_gates.py` ([RUNG-1](https://github.com/NeuroticGamer99/healthspan/pull/89)) deleted the
second copy of CI's gate commands and pointed `/land`, `/ship`, `/apply-review` and `/wi` at it.
The commands are now correct, derived, and one token away. They still were not used. Measured
twice in a single session, before the runner existed and with the knowledge already written down:

- `/land` documented `pytest -n auto` **with its reason** — the suite is isolated for worker
  parallelism — and the suite was run serially anyway, until the 600 s command timeout killed it.
  The parallel form finishes in ~112 s.
- `uv run ruff` was typed unprompted. It fails in 0.42 s with exit 2, "Failed to spawn: ruff",
  because ruff is not a project dependency and never has been.

The generalisable finding is that **documentation at the point of need did not prevent either**,
and the first case is the sharper one: the instruction existed, carried its justification, sat in
the skill being followed, and was not read. RUNG-1's own scoping recorded this as the reason the
runner alone would be dead code — "the wiring is not optional."

A fourth shape is worse than all of these and is invisible rather than noisy. `uvx ruff` with no
`@version` **succeeds**, against whatever is latest. Measured 2026-08-13, it resolved 0.16.3 while
`ci.yml` pins 0.15.21 — two minor versions apart. Nothing in the output says so. A session running
it gets a confident green from a tool CI does not run, which is precisely the silent-partial-green
failure `run_gates.py`'s docstring names as the worst of the three it was built against.

## Decision Drivers

- The failure is a *reach*, not a *misunderstanding*. Nothing a document says can intercept a
  command the author was confident enough not to look up.
- Three of the four shapes fail anyway. Refusing them costs a session nothing it was going to get.
- The fourth does not fail, which is why it needs a mechanism rather than more prose: there is no
  feedback signal to learn from.
- A hook with false positives is a hook an operator disables. [ADR-0070](0070-personal-data-containment-gate.md)
  rejected mechanising the containment rule's **content** half for exactly this reason — "a gate
  with false positives on every test fixture is one an operator disables" — while mechanising its
  enumeration half, which is the whole subject of that ADR. It is the content half's argument that
  transfers here, and §7 adopts the same boundary.
- Any tool list inside the hook is a third copy of what `ci.yml` already states and `run_gates.py`
  already derives — the drift this series exists to delete, reintroduced at the enforcement layer.
- An unwritten precedent gets cited anyway, and the citation is where it stops being examined.
  ADR-0075's first decision driver names this shape — *"this needs no ADR because that one needed
  none"* — and declining to record a mechanism that **refuses actions** would be the clearest
  instance of it yet.

## Considered Options

1. **Record nothing** — routing rule 6, as RUNG-1 took. The hook is an invocation detail; the
   script and its tests are the record.
2. **Extend ADR-0075**, which already owns the local side.
3. **A short standalone ADR** owning what a local-invocation hook may refuse — chosen.
4. **Document it harder** — a fifth restatement of the correct commands, in CLAUDE.md.

## Decision Outcome

### 1. What the hook refuses, and what it must not

A `PreToolUse` hook may refuse a local command **only** when the command cannot produce the result
its author intends. Four shapes qualify today, all derived rather than listed:

| Shape | Why it is refused |
|---|---|
| `uv run <tool>` for a non-dependency | exit 2, "Failed to spawn" |
| A bare `<tool>` for the same set | exit 127, not on PATH |
| `uvx <tool>` with no `@version`, for a tool `ci.yml` pins | **succeeds against the wrong version** |
| Full-suite `pytest` with no `-n` | killed at the 600 s timeout; `-n auto` takes ~112 s |

**The shape is the tool being run, not the word that was typed.** `uv tool run ruff` is `uvx ruff`,
`python -m pytest` is `pytest`, and `uvx ruff@latest` is `uvx ruff` with an `@` that pins nothing.
Each is normalised to the executed tool before the rules run, so the four rows above stay four rows.

**A command that merely looks unusual is not refused.** The boundary is that the refused command is
*wrong*, not *unidiomatic* — a distinction with a concrete test: a targeted `pytest <path> -k ...`
is deliberately allowed, because a scoped serial run is legitimate and denying it would make the
hook a false-positive machine. RUNG-2's scoping named that case by name as the one to protect, and
`test_a_scoped_pytest_is_never_denied` pins each way of scoping a run separately.

### 2. Deny, not warn — because "warn" does not reach the reader it needs to

RUNG-2 was scoped as **warn, not block**. That decision was taken against a wrong premise and is
reversed here, with the measurement that reversed it:

- A `PreToolUse` hook exiting **0 shows the model nothing** — debug log only. The model is the party
  reaching for the wrong command, so a warning in that form is addressed to nobody.
- The only shape that both warns and proceeds is `permissionDecision: "allow"` with
  `additionalContext`. But `allow` **auto-approves**: warning would silently spend the permission
  prompt the command would otherwise get. A hook that weakens the permission model in order to
  deliver a message it could deliver by denying is a bad trade.

So a match emits `permissionDecision: "deny"` with a reason naming the derived replacement command.
Denial loses nothing, because every refused shape already fails, wastes ten minutes, or lies.

**Exit 0 remains the process contract.** The decision travels in the JSON, not the exit status:
exiting 2 would *also* block, producing a second denial with a different message, and any non-zero
exit renders as a hook error on a hook that is working correctly.

### 3. The tool set is derived, and that is the load-bearing property

The hook names no tool. It builds `run_gates.py`'s registry and reads the tools out of the commands
the gates produce: a `uvx tool@version` argument names a pinned uvx tool, the executed word of a
`uv run` names an ephemeral one, and subtracting `pyproject.toml`'s dev dependency group leaves
exactly those that a bare or `uv run` invocation cannot find. When `ci.yml` pins a new tool, the
hook covers it with no edit.

This is not theoretical: the derivation's first run returned **`pip-audit`**, which the work item
scoping this hook had not listed, alongside the `ruff` / `pyright` / `pymarkdown` it did.
`test_a_newly_pinned_uvx_tool_is_policed_with_no_edit` is the test that holds it — it introduces a
tool the hook has never heard of and requires a denial. A hook carrying a literal list passes every
other test in the module and fails that one.

**Only the *executed* command word counts.** A package named in `--with pytest==9.1.1` is a
dependency of the run, not the thing being run. Collecting those was measured to mis-attribute
pytest to the pyright gate, so a serial-pytest denial told the reader to run pyright — a denial
whose remedy is wrong is worse than no denial.

**The dev-group subtraction is what keeps `pytest` legitimate.** pytest is reached through `--with`
in CI *and* is a dev dependency, so `uv run pytest <path>` works and must never be refused.

### 4. One restatement, named rather than hidden

`pymarkdownlnt` is the package; its console script is `pymarkdown`. That mapping is in neither
`ci.yml` nor `pyproject.toml` — it lives in the installed package's metadata — so
`_CONSOLE_SCRIPTS` states it. It is one entry, it is tested, and a wrong entry **fails open** (a
missed denial) rather than closed (a wrongly refused command). Adding an entry is adding a copy,
with a copy's obligations.

### 5. Fail open, and make the failure visible — but only *running* code can fail open

A hook that cannot derive its facts **allows the command and says so** in a `systemMessage`, rather
than blocking a session on its own bug. The notice deliberately carries **no** `permissionDecision`:
emitting `allow` would auto-approve, a downgrade the hook has no business making because it broke.

This is the same trade `run_gates.py` makes for a gate it cannot run — the residual failure becomes
*visibly absent* rather than *misleadingly green*. A derivation that finds an empty tool set raises
instead of reporting a clean pass, because policing nothing and finding nothing are
indistinguishable from the outside. The guard is deliberately broader than `DerivationError`: any
exception escaping `evaluate` produces the same notice, because a parser bug raising `IndexError`
and a missing `ci.yml` are the same event to the person whose command stopped working.

**Fail-open is a property of the script, and the script has to start for it to hold.** This
distinction was missing from the first version of this ADR, and the omission was not academic —
it cost a session, described in full below.

| Failure | What happens | Design response |
|---|---|---|
| **Derivation** — `ci.yml` unreadable, a gate will not build, an unexpected exception | The script is running; it emits a `systemMessage` and no decision | Fail open, as above |
| **Launch** — the interpreter cannot open the registered script at all | No code of ours runs, so nothing can be emitted. The session that hit this reported a hook error on every matched tool call | **Must be prevented, not tolerated** |

**What is measured here, and what is not.** That a launch failure leaves this script unable to emit
anything is structural — no code of ours runs. What the *harness* then does with the failed hook
was contested, and external round 2 settled the documentary half by quotation: a hook that cannot
start exits ~127, which the documentation places in the **non-blocking** bucket — *"For most hook
events, the action proceeds."* `PreToolUse` blocks on **exit 2** or an explicit deny decision, and
on nothing else.

So the single observation to the contrary — one session reporting its shell calls failing after
the hook error — is unreproduced, and has a known alternative explanation that the open-questions
entry already records: a non-blocking hook error hands the call to the ordinary permission flow,
where a *separate* denial reads to the session as the hook having blocked it.

**The two claims above come from different parts of that source, and the difference matters.** The
"most hook events" hedge is *prose*, and it is the reason the non-blocking half is stated as a
strong reading rather than a certainty. The exit-2 half is not prose: it is a table row naming
`PreToolUse` explicitly, so the hedge does not reach it. Saying that here because this passage has
now been revised in three consecutive rounds, each time for claiming more certainty than it had,
and an unlabelled categorical sentence sitting two lines from an acknowledged hedge is how the
fourth would start.

Two residuals stand: that prose hedge, and **the contained experiment has still not been run** — so
the blocking question is documentary rather than measured. `specs/open-questions.md` carries both.

**One thing here did get measured, unplanned.** While applying external round 2, this hook denied
one of the applying session's own Bash calls — a false positive, since fixed. That shows the
harness honours `permissionDecision: "deny"` end-to-end, which no test can show, because every
test drives the script directly rather than through a registered hook.

It says nothing about *launch* failure: the hook launched correctly, and a working hook refusing a
command is the designed path, not the failure this section is about. `specs/open-questions.md`
carries what the same event does and does not establish about the caching claim, and why the
evidence there is weaker than a first reading suggests.

The decision does not depend on which reading is right. A hook that cannot start cannot fail open,
cannot report, and cannot be observed to have stopped policing — that is enough to prevent it,
without also claiming to know how the harness renders it.

The first registered command was `python3 "${CLAUDE_PROJECT_DIR}/scripts/check_gate_invocation.py"`,
and it fails in a **git worktree** — this project's normal working mode, and the mode ADR-0068
established for reviewer isolation. `${CLAUDE_PROJECT_DIR}` resolves to the *primary* checkout while
the script exists only in the worktree, so the interpreter could not open the file. Two defects, and
only one of them was about the path:

1. **The path was wrong**, and wrong in a way that outlives the merge. Even once the script exists
   in both checkouts, `${CLAUDE_PROJECT_DIR}` still names the primary copy — so a worktree session
   would be policed against the *primary* checkout's `ci.yml`, silently reading the wrong repository
   state to decide what to refuse.
2. **The test written for exactly this class did not check it.**
   `test_the_registered_interpreter_is_a_bare_portable_name` verified the interpreter *name* and
   never that the script path resolved to anything at all. A registered path is the one part of the
   wiring that is trivially checkable and was not checked.

So the vehicle changed. The registered command is now an **inline bootstrap** — an interpreter that
always starts and performs its own resolution: the **cwd's repository first**, then
`CLAUDE_PROJECT_DIR`, and **silence** when neither holds the script. A path in `settings.json` is
the wrong shape for this, because a path that does not resolve has no way to fail quietly; a
process that always starts does.

Silence specifically means **exit 0, empty stdout, empty stderr**. Any output on a hook that cannot
work renders as an error on every matched call, which is the thing being prevented.
`test_an_unresolvable_hook_script_allows_rather_than_blocks` asserts all three.

**The interpreter is a second door into the same class, and it is only partly closed.** The
bootstrap runs under a bare `python3` — whatever that resolves to on `PATH`, which this repository
does not control, in a repository where every other tool is pinned through `uv`. Anything imported
at *module* scope runs before `main` exists and is therefore outside every guard in the file.
Measured: an unavailable module-level import exits 1 with a traceback on **every** matched call.

`tomllib` was that import — 3.11+ — and it is now deferred into the function that parses TOML,
inside the derivation guard, so it degrades to a visible notice.
`test_no_module_level_import_can_fail_on_a_supported_interpreter` holds the module-level import
list rather than a single name, so the next such import is caught too.

**That lowers the floor; it does not remove it, and an earlier draft of this paragraph said
otherwise.** The module's real floor is **3.9**, set by language features rather than by imports:
`dict[str, str]` as a `default_factory` (PEP 585) and `str.removesuffix` both run at import. An
import-list test cannot see either, so the mechanism covers one half of the constraint it was
described as covering — found by external review, not by the test. Mechanising the other half
means compiling against a target version, which is larger work and moot if the hook stops running
under an uncontrolled interpreter at all.

**What remains open is stated rather than implied.** Thirty-three module-level statements still
execute at import — regex compiles, frozen dataclasses, the wrapper table — and a *bug* in any of
them is still a traceback rather than a notice. Closing that needs a guard in the registered
command itself, and the quoting required to express `try`/`except` inside a one-line `-c` string
that must survive both `cmd.exe` and `bash` is precisely the fragility that caused the original
incident, so it was not attempted here. The durable fix is to stop running the hook under an
uncontrolled interpreter at all — `uv run` is already permitted by the settings test's allowlist and
costs ~145 ms more per call, measured — and that is a decision this ADR does not take.

### 6. Every shell tool is covered, not just Bash

`matcher` filters by tool name only, and this environment exposes a **PowerShell** tool beside
Bash. A hook registered for one is a rule the other routes around — the per-tool enumeration
failure `specs/open-questions.md` already records this project measuring once, where a deny list
naming only the obvious file-read tool was silently defeated via a search tool. Both matchers are
registered as literal names rather than one regex alternation, so a matcher that is not
regex-aware fails visibly instead of silently never firing.

**Registering a matcher is not the same as understanding its shell, and the first version of this
section confused the two.** It described only the wiring. But the command *string* the two tools
carry is written in different dialects, and they disagree on precisely the two characters this
parser depends on most:

| | Bash | PowerShell |
|---|---|---|
| `\` | escape — `.\ruff.exe` lexes to `.ruff.exe` | ordinary path separator |
| `` ` `` | command substitution — the command really runs | escape character — nothing runs |
| `$(…)` | substitution | substitution |

Reading PowerShell with bash rules therefore did both harms at once: it let `.\ruff.exe check .`
past every rule (the backslash was eaten before the tool name could be recognised) *and* denied
``Write-Host "use `pytest` here"``, which executes nothing at all. A denial is not
user-overridable, so the second is the more expensive of the two.

The parser therefore takes the dialect from the payload's `tool_name` rather than assuming one.
**An unmapped tool falls back to bash, and that fallback is a gap rather than a safety margin** —
a distinction worth stating because the code first claimed the opposite. Bash rules extract *more*
commands on the substitution axis, so an unknown tool is over-inspected there; they extract
*fewer* on the path axis, because `\` is consumed as an escape. No single default is conservative
on both. A third shell tool whose dialect treats `\` as a separator would be under-inspected until
it is added to the mapping, which fails open in the direction §5 accepts.

### 7. Scope: this hook only, and the two candidates it does not ship

The same `PreToolUse` machinery has two other known candidates, both recorded here so the mechanism
is **sized once** rather than rediscovered per row. Neither ships in this change:

1. **Read-only `tests/**` during a fix.** ImpossibleBench measured Claude-family models cheating
   predominantly by editing the test file, and prose is not a control there (93% → 1%).
2. **The CLAUDE.md PowerShell UTF-8 encoding rule.** The 2026-08-06 harness audit called it
   "genuinely hook-gateable" and "the real target if hooks get built" — a check on command text
   rather than a judgement about meaning, which is the same class as this hook's four shapes.

They are separate PRs because a new mechanism plus a new policy inside one change is the shape this
repository has repeatedly paid for. Each is a row in an established mechanism once this lands.
**A candidate that requires judging *content* rather than matching *command text* is out of scope
for this mechanism entirely** — that is ADR-0070's rejected option, and it does not become viable
by moving to a hook.

### 8. The settings file is now behaviour, and is tested as such

`.claude/settings.json` was previously inert configuration covered by no gate — measured: neither
CI nor the citation registry reads it. It now carries a hook that changes what commands run, and a
hook that silently stops firing is indistinguishable from one that was never installed. The wiring
is therefore pinned by tests: that both shell tools are registered, that each matcher names the hook
script, that the interpreter is a bare portable name, and — added after the launch failure in §5 —
that the **registered command string itself is executed**, through a real shell, in each of the
three resolution cases: the cwd's repository, the `CLAUDE_PROJECT_DIR` fallback, and neither.

That last group is the one worth naming, because it is the only test here that would have caught
the defect that cost a session. It runs the string from `settings.json` verbatim rather than a
reconstruction of it. The interpreter word is swapped for the running `sys.executable`, for the
reason `test_the_registered_interpreter_is_a_bare_portable_name` already gives: asserting that
`python3` resolves on the host turns a runner-image fact into a red on this change.

**Each case runs under more than one shell, and the reason is a correction to this section.** It
first claimed the tests made shell portability "an answered question on each CI leg". They did not:
`subprocess.run(..., shell=True)` is `cmd.exe` on Windows and `/bin/sh` on POSIX — never both — so
the Windows leg exercised only cmd.exe, and a bootstrap edit valid in one shell and not the other
would have passed on whichever leg agreed with it, under a claim that all three were covered.
Measured: `echo hello; echo $0` through `shell=True` on Windows returns the literal string,
unsplit and unexpanded. The cases now run under the platform default *and* under a POSIX shell
wherever one is installed — which on Windows is the Git Bash this project's tooling already
depends on. Where no second shell exists the parametrisation quietly has one entry, so the honest
claim is "the platform default, plus a POSIX shell when the machine has one" rather than "all
three legs".

[ADR-0071](0071-commit-shared-claude-settings.md) anticipated this arrival — it landed
`settings.json` as an empty stub precisely so "hooks and shared grants arrive in their own reviewed
changes" — and it attaches a precondition: content landing in the tracked file must carry no
machine-specific path and no personal data, with anything local-only going to
`settings.local.json`. This change satisfies it: the bootstrap contains no absolute path, resolving
everything at run time from the cwd and the environment, and the interpreter is the bare name
`python3`. `test_the_bootstrap_names_no_machine_specific_path` pins that, because the inline form is
longer than the path it replaced and is correspondingly better cover for an absolute path.

**`python3` is registered despite being the slower spelling here, and that is the ADR-0071
precondition doing its job.** On this machine `python3` resolves to a Store shim costing 396 ms
against 159 ms for `python` — but that gap is an artefact of one machine, and choosing the
registered name from it would put a machine-specific optimisation into the shared, reviewed file.
`python3` is also the spelling `ci.yml` uses throughout. The ~240 ms is accepted as the price of a
portable shared file; a contributor who wants the faster spelling has `settings.local.json`, which
is where ADR-0071 puts exactly this kind of local-only preference.

### Positive Consequences

- The expensive failure class (wrong command, silently partial or ten minutes wasted) is replaced
  by a refusal that names the right command.
- The unpinned-`uvx` green — which had no feedback signal at all — now has one.
- A tool newly pinned in `ci.yml` is policed with no edit to the hook.
- `.claude/settings.json` gains test cover it never had.

### Negative Consequences / Tradeoffs

- **A process per Bash call.** ~160 ms, of which the derivation is ~2 ms; the rest is interpreter
  startup. Reduced from 149 ms of derivation by suppressing a `git ls-files` the hook does not
  need, which couples it to a private name in `run_gates.py` — a coupling that degrades to *slow*
  rather than *wrong*, and is pinned by a test so it is caught rather than absorbed.
- **A false positive blocks real work.** Mitigated by the allow corpus, which pins the shapes most
  likely to be caught by accident — a tool name quoted inside a `grep`, a scoped `pytest`, a
  correctly pinned `uvx` — but the residual risk is real and is the reason §1 draws its boundary at
  *cannot work* rather than *looks wrong*.
- **The hook enforces locally what only CI can decide.** Nothing here weakens ADR-0045 §2: CI
  remains the authority, and a denied command is a command that would not have helped either way.
- **One reading feeds every parser rewrite, and it is where the defects land.** `pytest`'s scoping
  test treats *any* non-flag token as a test path. Every transformation that removes or inserts a
  token therefore reaches it, and external round 2 found four separate routes into that one
  reading: substitution erasure (`$(…)` collapsed to a space, so `pytest $(git diff --name-only)`
  read as unscoped and was **denied**), process substitution, `xargs` unwrapping, and a line
  continuation left in argv as a token. A fifth — the `2>&1` file-descriptor digit — had already
  been fixed one round earlier, which is what identifies this as a class rather than four
  coincidences. Substitutions now emit a placeholder *word* rather than a space, continuations are
  removed as a shell removes them, and `xargs` left the wrapper set. **Fix the reading, not the
  routes** is the standing instruction for the next one.
- **Command parsing is an approximation, with one limit worth naming exactly.** The hook lexes with
  quoting honoured and reaches chained and substituted commands, including a substitution nested
  inside an argument (`cd $(dirname $(which uv)) && ruff check .` is denied). What it cannot reach
  is a command whose **command word is itself the output of a substitution** —
  `result=$(uvx $(echo ruff) check .)` slips through, because knowing that `$(echo ruff)` means
  `ruff` requires running it. It fails open, which is the right direction for a convenience
  mechanism, and `test_nested_substitution_is_reached_where_the_command_word_is_literal` pins both
  halves so the limit stays recorded rather than rediscovered.
- **The hook now runs from wherever the session is, which is a wider contract than a fixed path.**
  Resolving the script from the cwd's repository is what makes a worktree session policed by its own
  checkout, and it also means a session whose cwd is *some other* clone runs *that* clone's copy.
  That is the correct behaviour — the tool set is derived from the repository being worked in, so
  reading a different repository's `ci.yml` is precisely the silent-wrong-state failure §5.1
  describes — but it does mean the hook is no longer a single fixed program, and *which* file it
  runs is now derived at call time. The search is therefore bounded to exactly two candidates:
  `scripts/check_gate_invocation.py` under the **root of the cwd's repository** (the nearest
  ancestor holding a `.git` entry, which is a file in a worktree and a directory in a primary
  checkout), and the same path under `CLAUDE_PROJECT_DIR`. Finding nothing is silent, so the
  failure direction stays open.

  **Stated as a correctness tradeoff above, it understates the residual, and external review was
  right to press on it.** The file is *executed* — `runpy.run_path` — not inspected. So any clone
  containing that relative path runs its own code on every Bash and PowerShell call for as long as
  a session's cwd sits inside it, with this session's authority and no integrity check. Cloning a
  repository and running a shell command in it is an ordinary thing to do, which is what makes this
  a trust boundary rather than a curiosity.

  It is **accepted, not overlooked**, and the alternative is worse in a way this ADR has already
  paid for. Restricting resolution to `CLAUDE_PROJECT_DIR` alone reinstates §5's original defect
  exactly: that variable names the *primary* checkout, so a worktree session would be policed by
  the wrong tree — the failure that cost a session and prompted this whole design. The bound that
  is available is the one in place: two candidates, a `.git` boundary, and never an unbounded walk.
  Anyone extending this mechanism should treat "the hook executes code found relative to the cwd"
  as the property to preserve deliberately or to remove deliberately, not as an implementation
  detail.

  **The first version of this bootstrap was not bounded, and the gap was found in review.** It
  walked `Path.cwd().parents` to the drive root and would have executed the first matching file
  anywhere above the session — while this ADR described the narrow behaviour. That is a wider trust
  surface than the decision intends (any directory above a session becomes a place to put code that
  runs on every shell call), and it is recorded here because the shipped mechanism and its
  description having drifted apart is the failure this section exists to prevent.
  `test_the_search_stops_at_the_repository_root` plants a script above the repository root and
  requires that it never run.
- **A command word is not always the tool.** Four spellings reach the same four shapes and each was
  measured slipping through the rules as first written: `uvx tool@latest` (an `@` that pins
  nothing), `uv tool run tool` (uvx's long spelling), `python -m pytest`, and
  `uv run python -m pytest`. They are normalised to the executed tool before the rules run, rather
  than added as new rules, so each rule stays written once. The normalisation is itself an
  enumeration with the same rot risk as the walkers below — `_PYTHON_VALUE_FLAGS` exists because
  `python -X dev -m pytest` otherwise stops the walk at `dev`.
- **Wrapper commands are unwrapped only in their bare form, and the retreat to that rule is the
  most instructive thing in this ADR.** A command whose trailing words are themselves a command —
  `sudo pytest`, `env pytest`, `exec`, `nohup`, `command` — reaches the rule. Anything carrying
  flags does not, and fails open.

  The first version modelled each wrapper's own argument grammar: which flags take a separate
  value, which merely describe, how many positionals the wrapper owns. It was wrong in a **new way
  in each of four consecutive review rounds**, and every failure was the same underlying mistake —
  the walker mis-identifying which token was the command word:

  | Round | Defect | Effect |
  |---|---|---|
  | 1 | `doas -a`, `sudo -U` absent from the value set | flag's value read as the command; missed denial |
  | 2 | `xargs -e, --eof[=END]`, `-l[MAX-LINES]` are *optional*-argument, so a bare one consumes nothing | command word eaten; missed denial |
  | 3 | `sudo -h` is `--help`, not `-h host` | `sudo -h pytest` denied — a **false positive**, pinned in the deny corpus |
  | 3 | `--host` removed alongside `-h`, though only `-h` is ambiguous | `sudo --host remote pytest` re-opened |
  | 4 | the test proving round 3's fix could not fail — `timeout`'s positional swallowed the probe | a green that checked nothing |

  Two further properties made this unfixable by more enumeration. The grammar **varies by
  implementation** — `sudo-rs 0.2.13` has no `--host` at all while classic sudo does — so there is
  no single correct table. And the oracle was **hand-written from the same reading that produced
  the table**, so whenever that reading was wrong, both halves agreed and the suite stayed green.
  A derived oracle was measured surviving three deletions, because deleting a flag deletes the case
  that would catch it.

  So the grammar was deleted rather than repaired. The replacement needs no knowledge of any other
  program: the token after the wrapper either is an option or is not. That gives up
  `xargs -a files pytest` and the flagged forms of the rest — one of the six shapes originally
  reported — and it fixes a class *by construction* rather than by enumeration, since `sudo -h
  pytest` is left alone for the same reason every other flagged form is. `timeout` leaves the set
  entirely: it owns a mandatory positional, so `timeout 30 pytest` would read `30` as the command.

  **The general lesson, which outlives this table:** a mechanism whose correctness depends on
  restating another program's interface has no stable resting point, and the review rounds were
  measuring that rather than converging on it. ADR-0077 §3 already refuses to restate `ci.yml`'s
  tool list for the same reason; this is the same rule applied to a place it had quietly been
  broken.

  `su` and `watch` were excluded from the outset, and for a **different** reason worth keeping
  distinct: their trailing words are not a command list at all. Both take the command as a single
  string after `-c`, so reaching it means re-lexing that string rather than skipping tokens. That
  is the shape being wrong, not the grammar going stale — folding it into the lesson above would
  lose the fact that actually motivated it.

  **`xargs` left the set in external round 2, for a third reason.** Its arguments arrive on
  **stdin**, which is not in the command text at all, so `git ls-files "*_test.py" | xargs pytest`
  — a scoped run — was unwrapped to a bare `pytest` and refused for "exceeding the 600 s timeout",
  which is false of a run finishing in seconds. Only the empty-stdin case is genuinely a full
  suite, and nothing in the command distinguishes them. That is strictly further outside §7's
  boundary than `timeout`'s positional: this hook stops carrying knowledge of another program's
  *interface*, and `xargs` would require knowing another program's *input stream*.
- **The argument walkers are an enumeration, and enumerations rot.** Which options consume a
  following value is stated as three explicit sets, because inferring it was measured wrong in both
  directions: `uv run -p 3.12 ruff check .` read the version as the tool and defeated two rules at
  once, and `pytest -p no:cacheprovider` read a plugin name as a test path and allowed the full
  serial run. A flag added to uv or pytest that takes a value and is missing from these sets fails
  open the same way. The sets are shared by every function that walks those arguments — two
  hand-rolled copies had already drifted, one knowing `-p` and the other not.

## Pros and Cons of the Options

### Option 1 — record nothing (rule 6)
RUNG-1 could argue rule 6 honestly: it re-expressed commands CI already owned and refused nothing.
A hook **refuses actions**, which is new authority over what a session may do, and §2 reverses a
prior owner decision on measured grounds. Both are decision content, not implementation detail.

### Option 2 — extend ADR-0075
ADR-0075 owns whether a local gate may *differ* from its CI step. This owns whether a local
invocation may be *refused*. Folding them would make the divergence rule harder to cite for the
thing it actually governs, and ADR-0075 §4 already says a case that only arguably fits should get
its own ADR.

### Option 3 — a short standalone ADR (chosen)
Owns one rule, states the boundary that keeps it from growing, and gives the two unshipped
candidates a home so the mechanism is argued once.

### Option 4 — document it harder
The option the evidence rejects. `/land` already documented the `-n auto` instruction with its
reason and it was not read; a fifth restatement is the intervention that has already been measured
failing, and it would add a copy this series exists to remove.

## Links

- Extends [ADR-0045](0045-repository-workflow-and-ci-enforcement.md) — CI remains the authority;
  this governs only what a local session is stopped from typing.
- Builds on [ADR-0075](0075-local-gate-divergence-from-ci.md) — the local gate policy whose
  invocation path this protects.
- Related: [ADR-0063](0063-parallel-ci-test-execution.md) — owns `-n auto` as the local norm, which
  is the rule the pytest shape enforces.
- Related: [ADR-0071](0071-commit-shared-claude-settings.md) — landed `.claude/settings.json` as an
  empty stub so hooks would "arrive in their own reviewed changes"; this is that change, and §8
  answers the precondition it attached.
- Related: [ADR-0070](0070-personal-data-containment-gate.md) — its rejection of mechanising a
  content rule is the boundary §7 adopts for which candidates this mechanism may take.
