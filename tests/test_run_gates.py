"""The local gate runner (scripts/run_gates.py).

The runner exists to delete a second copy of CI's gate commands and pinned tool
versions. That goal is only met while two properties hold, and both are
invisible by inspection — which is why they are the load-bearing tests here:

1. **Versions are derived, not restated.** ``test_a_bumped_pin_is_picked_up``
   feeds a synthetic workflow whose pins differ from the repository's and
   requires the built commands to carry the synthetic values *and not* the real
   ones. A hardcoded default that happened to match today's pin would satisfy a
   weaker assertion and fail this one.
2. **The gate list cannot silently fall behind CI.** The registry names which
   ``ci.yml`` steps each gate reproduces, and
   ``test_every_ci_run_step_is_claimed`` requires every named ``run:`` step in
   the real workflow to be claimed by a gate or declared a non-gate. Adding a
   gate to CI reddens this until the runner learns it. Its converse,
   ``test_every_claimed_step_exists``, catches the registry pointing at a step
   that was renamed away.

A third property is weaker but worth stating precisely.
``test_every_local_gate_names_a_runnable_program`` checks that each gate
claiming to be runnable names a program this machine can actually start. It
does **not** prove the gate passes, and it cannot: what it prevents is a gate
mislabelled ``local`` being counted in "all N gates passed" when it could never
have run — the silent-partial green the runner exists to stop.

Nothing below hardcodes a gate name where the registry can supply it. A literal
list would give a newly added gate no coverage at all, silently, which is the
same drift the runner is built to remove.
"""

from __future__ import annotations

import re
import shutil
import sys
from pathlib import Path
from typing import Any, cast

import pytest
import run_gates

REPO_ROOT = Path(__file__).resolve().parent.parent
CI_TEXT = run_gates.CI_WORKFLOW.read_text(encoding="utf-8")

LOCAL_GATES = [gate for gate in run_gates.GATES if gate.local]
CI_ONLY_GATES = [gate for gate in run_gates.GATES if not gate.local]
PINNED_GATES = [gate for gate in run_gates.GATES if gate.version_pin]
GATES_IN_ORDER = [gate.name for gate in run_gates.GATES if gate.local]


_CANARY_DIR_RE = re.compile(r"^\s*CANARY_CAPTURE_DIR:\s*(\S+)", re.MULTILINE)


def _ci_canary_dir_name() -> str:
    """The worker-sink directory name CI sets, read from ci.yml.

    Ground truth for the runner's own value. Restating "canary-logs" here would
    be the second copy this whole module exists to delete, and it would also go
    stale silently — the failure it is being used to catch.
    """
    match = _CANARY_DIR_RE.search(CI_TEXT)
    if match is None:
        raise AssertionError("ci.yml no longer sets CANARY_CAPTURE_DIR")
    return match.group(1)


def gate_named(name: str) -> run_gates.Gate:
    """Look a gate up by name rather than by position.

    `LOCAL_GATES[0]` reads as "some gate" but silently means "whichever gate is
    first", so a test needing particular *properties* — more than one step, no
    `always_run` — gets them by accident and loses them to a reordering.
    """
    return run_gates.gate_by_name()[name]


def _context(tmp_path: Path | None = None) -> run_gates.Context:
    """A context over the real workflow, with scratch pinned into tmp_path.

    Presetting ``scratch`` keeps the test gate from calling ``mkdtemp`` and
    leaving directories behind; it is the only state a build touches.
    """
    env = {
        name: value
        for name in run_gates.WORKFLOW_ENV_NAMES
        if (value := run_gates.read_workflow_env(CI_TEXT, name)) is not None
    }
    return run_gates.Context(
        pins=run_gates.read_pins(CI_TEXT),
        gitleaks_version=run_gates.read_gitleaks_version(CI_TEXT),
        scratch=tmp_path,
        workflow_env=env,
    )


# --------------------------------------------------------------------------
# 1. Versions are derived, not restated
# --------------------------------------------------------------------------

# Deliberately unlike anything the repository pins, so a command carrying a real
# version is unambiguously a hardcoded value rather than a coincidence.
SYNTHETIC = """
env:
  RUFF_VERSION: "99.98.97"
  PYRIGHT_VERSION: "99.98.96"
  PYTEST_VERSION: "99.98.95"
  PYTEST_XDIST_VERSION: "99.98.94"
  PIP_AUDIT_VERSION: "99.98.93"
  PYMARKDOWN_VERSION: "99.98.92"
"""


@pytest.mark.parametrize("gate", PINNED_GATES, ids=lambda g: g.name)
def test_a_bumped_pin_is_picked_up(gate: run_gates.Gate, tmp_path: Path) -> None:
    """Bumping a pin in ci.yml changes the command, with no edit here.

    The negative half is the point: the real version must be absent. Asserting
    only that the synthetic version appears would still pass if the builder
    emitted both, or if it fell back to a constant on a parse miss.
    """
    synthetic = run_gates.read_pins(SYNTHETIC)
    ctx = run_gates.Context(pins=synthetic, gitleaks_version=None, scratch=tmp_path)
    real = run_gates.read_pins(CI_TEXT)[gate.version_pin or ""]

    rendered = " ".join(step.shown() for step in gate.steps(ctx))

    assert synthetic[gate.version_pin or ""] in rendered
    assert real not in rendered, (
        f"{gate.name} emitted the repository's own {gate.version_pin} despite "
        "being built from a workflow that pins a different value — the version "
        "is restated somewhere rather than derived"
    )


def test_a_missing_pin_is_an_error_naming_the_variable() -> None:
    """A renamed or deleted pin fails loudly; it never falls back to a default.

    This is the accepted cost of parsing ci.yml, and the direction matters: a
    gate that fails on a cosmetic edit costs one commit, where a gate that
    passes against a stale pin is the defect being prevented.
    """
    with pytest.raises(run_gates.GateError) as excinfo:
        run_gates.require({}, "RUFF_VERSION")
    assert "RUFF_VERSION" in str(excinfo.value)


def test_conflicting_duplicate_pins_are_refused() -> None:
    """Two values for one pin is unanswerable, so it raises rather than picks."""
    text = 'env:\n  RUFF_VERSION: "1.0.0"\njobs:\n  RUFF_VERSION: "2.0.0"\n'
    with pytest.raises(run_gates.GateError) as excinfo:
        run_gates.read_pins(text)
    assert "RUFF_VERSION" in str(excinfo.value)


def test_an_identical_duplicate_pin_is_accepted() -> None:
    """Repetition is only a problem when the values disagree."""
    text = 'env:\n  RUFF_VERSION: "1.0.0"\nother:\n  RUFF_VERSION: "1.0.0"\n'
    assert run_gates.read_pins(text)["RUFF_VERSION"] == "1.0.0"


def test_a_version_inside_a_comment_is_not_mistaken_for_a_pin() -> None:
    """The pin regex is line-anchored, so prose about a version cannot win."""
    text = '# RUFF_VERSION: "0.0.1" was the old pin\nenv:\n  RUFF_VERSION: "1.2.3"\n'
    assert run_gates.read_pins(text)["RUFF_VERSION"] == "1.2.3"


def test_every_pin_a_gate_needs_is_present_in_the_live_workflow() -> None:
    """The runner's day job: today's ci.yml answers every version it asks for."""
    pins = run_gates.read_pins(CI_TEXT)
    missing = [g.name for g in PINNED_GATES if (g.version_pin or "") not in pins]
    assert missing == []


def test_the_gitleaks_version_is_read_from_its_install_step() -> None:
    """Even the pin that lives outside the env block is derived, not restated."""
    version = run_gates.read_gitleaks_version(CI_TEXT)
    assert version is not None
    assert version in CI_TEXT


# --------------------------------------------------------------------------
# 2. The gate list cannot silently fall behind CI
# --------------------------------------------------------------------------

_STEP_START = re.compile(r"^      - ")
_STEP_NAME = re.compile(r"^      - name: (.+?)\s*$")


def _named_run_steps(text: str) -> list[str]:
    """Names of every ``run:`` step in the workflow.

    A hand parser rather than a YAML dependency: the scripts in this repository
    are stdlib-only, and the shape being read is two fixed indents deep. Steps
    are delimited by their leading ``- ``; a block counts only if it carries
    both a ``name:`` and a ``run:``, so ``uses:`` steps and the env block cannot
    leak in.
    """
    names: list[str] = []
    current: list[str] = []

    def flush() -> None:
        if not current:
            return
        match = _STEP_NAME.match(current[0])
        if match and any(line.startswith("        run:") for line in current):
            names.append(match.group(1))

    for line in text.splitlines():
        if _STEP_START.match(line):
            flush()
            current = [line]
        elif current:
            # A line at job indent or shallower ends the steps block.
            if line.strip() and not line.startswith("       "):
                flush()
                current = []
            else:
                current.append(line)
    flush()
    return names


CLAIMED_STEPS = {step for gate in run_gates.GATES for step in gate.ci_steps}


def test_the_step_parser_finds_the_workflows_steps() -> None:
    """Guard the guard: a parser returning nothing would pass every test below.

    Without this, a shape change in ci.yml that broke `_named_run_steps` would
    read as "no unclaimed steps" — a vacuous pass on the one test whose whole
    job is to notice new gates.
    """
    steps = _named_run_steps(CI_TEXT)
    assert len(steps) > 10
    assert "ruff format --check" in steps


@pytest.mark.parametrize("step", sorted(_named_run_steps(CI_TEXT)))
def test_every_ci_run_step_is_claimed(step: str) -> None:
    """Every gate CI runs is either reproduced here or declared a non-gate.

    This is what stops the runner quietly covering less than CI. When it fails,
    the fix is to add the gate to the registry — or, if the step is setup rather
    than a gate, to NON_GATE_STEPS with a reason.
    """
    if step in run_gates.NON_GATE_STEPS:
        return
    if step.startswith(run_gates.NON_GATE_STEP_PREFIXES):
        return
    assert step in CLAIMED_STEPS, (
        f"ci.yml runs a step the local runner does not know about: {step!r}. "
        "Add a gate that reproduces it, or register it as a non-gate step."
    )


@pytest.mark.parametrize("step", sorted(CLAIMED_STEPS))
def test_every_claimed_step_exists(step: str) -> None:
    """The registry cannot point at a ci.yml step that no longer exists.

    The converse direction of the test above: a step renamed in CI leaves this
    gate's claim dangling, and without this the forward check would still pass
    by declaring the *new* name unclaimed — two failures reported as one.
    """
    assert step in _named_run_steps(CI_TEXT)


def test_ci_only_gates_are_declared_with_a_reason() -> None:
    """A gate that cannot run locally has to say why, because --list prints it."""
    for gate in CI_ONLY_GATES:
        assert gate.ci_only_reason.strip(), f"{gate.name} is CI-only with no reason"


def test_gate_names_are_unique() -> None:
    """Selection is by name, so a duplicate would make one gate unreachable."""
    names = [gate.name for gate in run_gates.GATES]
    assert sorted(names) == sorted(set(names))


# --------------------------------------------------------------------------
# 3. A gate claiming to be local must actually be runnable
# --------------------------------------------------------------------------


@pytest.mark.parametrize("gate", LOCAL_GATES, ids=lambda g: g.name)
def test_every_local_gate_names_a_runnable_program(
    gate: run_gates.Gate, tmp_path: Path
) -> None:
    """Each local gate's commands name a program this machine can start.

    Scope, stated honestly: this proves the executable resolves, not that the
    gate passes. What it prevents is the failure that would otherwise be
    invisible — a gate marked ``local`` that needs a binary this machine lacks
    still being counted in the "all N gates passed" summary.
    """
    for step in gate.steps(_context(tmp_path)):
        program = step.argv[0]
        assert Path(program).exists() or shutil.which(program), (
            f"{gate.name} is registered as locally runnable but its command "
            f"starts with {program!r}, which does not resolve on this machine"
        )


@pytest.mark.parametrize("gate", CI_ONLY_GATES, ids=lambda g: g.name)
def test_a_ci_only_gate_refuses_to_build_steps(
    gate: run_gates.Gate, tmp_path: Path
) -> None:
    """Building a CI-only gate raises rather than returning an empty list.

    An empty list would render as a gate that ran and passed without executing
    anything — the exact false green being designed out.
    """
    with pytest.raises(run_gates.GateError):
        gate.steps(_context(tmp_path))


# --------------------------------------------------------------------------
# Selection, listing and failure behaviour
# --------------------------------------------------------------------------


def test_the_lockfile_gate_checks_rather_than_syncs(tmp_path: Path) -> None:
    """It must not mutate the developer's environment to answer a question.

    CI's step is `uv sync --locked`, which installs into a runner it discards.
    Both forms fail on a stale lockfile, so a regression to the CI spelling
    keeps the gate working and quietly acquires a side effect — invisible to
    every generic assertion about this gate.
    """
    argv = [
        arg
        for step in gate_named("lockfile").steps(_context(tmp_path))
        for arg in step.argv
    ]
    assert "lock" in argv
    assert "--check" in argv
    assert "sync" not in argv, "the lockfile gate would now mutate the environment"


def test_a_group_expands_to_its_gates() -> None:
    """`ruff` is the alias the runner was asked for by name — "just ruff"."""
    assert [g.name for g in run_gates.resolve(["ruff"])] == [
        "ruff-check",
        "ruff-format",
    ]


def test_every_job_is_selectable_as_a_group() -> None:
    """Groups are derived from the ci.yml job each gate belongs to."""
    for gate in run_gates.GATES:
        assert gate.name in run_gates.groups()[gate.job]


def test_selection_preserves_registry_order() -> None:
    """Cheap gates run before slow ones however the selectors were typed."""
    order = [g.name for g in run_gates.GATES]
    selected = [g.name for g in run_gates.resolve(["pytest", "ruff-check"])]
    assert selected == [name for name in order if name in set(selected)]


def test_a_gate_named_twice_runs_once() -> None:
    """Overlapping selectors — a group and one of its members — do not duplicate."""
    assert [g.name for g in run_gates.resolve(["ruff", "ruff-check"])] == [
        "ruff-check",
        "ruff-format",
    ]


def test_an_unknown_selector_lists_what_is_known() -> None:
    """The error is the discovery path for someone who guessed a name."""
    with pytest.raises(run_gates.GateError) as excinfo:
        run_gates.resolve(["nonsense"])
    message = str(excinfo.value)
    assert "nonsense" in message
    assert "ruff-check" in message


def test_list_names_every_gate_and_marks_the_ci_only_ones(tmp_path: Path) -> None:
    """--list is the answer to "what gates exist", so it may omit none of them."""
    rendered = run_gates.render_list(_context(tmp_path))
    for gate in run_gates.GATES:
        assert gate.name in rendered
    for gate in CI_ONLY_GATES:
        assert gate.ci_only_reason in rendered


def test_list_rejects_an_unknown_selector_through_main() -> None:
    """`--list` resolves selectors like every other path.

    Driven through `main` deliberately: `render_list` called directly can never
    see this, and moving `if args.list:` back above `resolve` restores the
    exit-0-on-a-nonexistent-name behaviour silently.
    """
    assert run_gates.main(["--list", "nonsense-gate"]) == 1


def test_list_shows_only_the_selected_gates(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A selector narrows `--list`, rather than being accepted and ignored."""
    assert run_gates.main(["--list", "lockfile"]) == 0
    out = capsys.readouterr().out
    assert "lockfile" in out
    assert "ruff-check" not in out


def test_list_respects_fast(capsys: pytest.CaptureFixture[str]) -> None:
    """`--list --fast` must answer "what will --fast run", not over-advertise.

    The filter used to sit below the `--list` return, so the two paths disagreed
    about the selection — the same class of split the resolve-before-branch move
    was made to close.
    """
    assert run_gates.main(["--list", "--fast"]) == 0
    out = capsys.readouterr().out
    for slow in sorted(run_gates.SLOW_GATES):
        assert f"\n  {slow} " not in out, f"--list --fast advertised {slow}"


def test_print_reports_an_empty_selection_rather_than_succeeding_silently(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`--print` over an empty selection is silence indistinguishable from an
    answer, so it takes the same guard the run path does."""
    assert run_gates.main(["--print", "pytest", "--fast"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "dropped by --fast" in captured.err


def test_a_gate_that_examined_nothing_is_not_a_zero_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """EMPTY has to reach the exit code, not just the summary text.

    `/land` reads the exit code and its rule is "a failing gate stops the
    landing". Printing "they prove nothing" while returning 0 let a markdown
    pathspec that silently stopped matching land anyway.
    """

    def fake_run_gate(
        _gate: run_gates.Gate, _ctx: run_gates.Context
    ) -> run_gates.GateResult:
        return run_gates.GateResult.EMPTY

    monkeypatch.setattr(run_gates, "run_gate", fake_run_gate)
    assert run_gates.main(["markdown-lint"]) == 1


def test_run_step_sets_nothing_the_workflow_no_longer_sets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When ci.yml stops setting a key, the runner stops setting it too.

    This is the half a "derives the right value" test cannot see. Hardcoding
    `PYTHONUTF8: "1"` back into `run_step` satisfies every assertion that the
    derived value is correct, because the derived value and the literal agree
    today — verified by mutation, which passed 104/104 before this test existed.
    An empty workflow env is the ci.yml-dropped-it case (dated in ci.yml's own
    comment: PEP 686, Python 3.15), and the ambient value must survive it.
    """
    seen: dict[str, str] = {}

    class Completed:
        returncode = 0

    def fake_run(*args: Any, **kwargs: Any) -> Completed:
        env = kwargs.get("env")
        if isinstance(env, dict):
            seen.update(cast("dict[str, str]", env))
        return Completed()

    monkeypatch.setenv("PYTHONUTF8", "0")
    monkeypatch.setattr(run_gates.subprocess, "run", fake_run)
    run_gates.run_step(run_gates.Step(["x"]), {})
    assert seen["PYTHONUTF8"] == "0", (
        "run_step forced a value the workflow does not set — the literal is "
        "back, and it will outlive ci.yml's own setting"
    )


def test_the_workflow_env_is_derived_from_ci_yml() -> None:
    """PYTHONUTF8 is read from ci.yml, not restated in this module.

    The negative half is the point, as with the version pins: a workflow that
    does not set it must yield nothing, because that is how the runner stops
    forcing what CI stopped forcing instead of silently diverging.
    """
    assert run_gates.read_workflow_env(CI_TEXT, "PYTHONUTF8") == "1"
    assert run_gates.read_workflow_env("env:\n  OTHER: x\n", "PYTHONUTF8") is None


def test_the_runner_stops_at_the_first_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Fail-fast: a lint failure must not cost a full test run first.

    Ordering is the whole reason the registry is a sequence, and a regression
    here is silent — everything still passes, just slower and later.
    """
    attempted: list[str] = []

    def fake_run_step(step: run_gates.Step, _env: dict[str, str] | None = None) -> int:
        attempted.append(step.shown())
        return 1

    monkeypatch.setattr(run_gates, "run_step", fake_run_step)
    # A gate with more than one step, deliberately: against a single-step gate
    # the count assertion below holds whether or not the loop stops early, so it
    # could never fail. `pip-audit` has two ordinary steps and no `always_run`.
    gate = gate_named("pip-audit")
    assert len(gate.steps(_context(tmp_path))) > 1
    assert run_gates.run_gate(gate, _context(tmp_path)) is run_gates.GateResult.FAILED
    assert len(attempted) == 1, "a failed step must not be followed by the next"


def test_the_run_stops_at_the_first_failing_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cross-gate fail-fast: a lint failure must not cost a full test run first.

    This is `main`'s loop rather than `run_gate`'s, and it is the half the
    ordering argument actually rests on — deleting the early exit, or reordering
    GATES so pytest runs first, is invisible to the within-gate test above.
    """
    ran: list[str] = []

    def fake_run_gate(
        gate: run_gates.Gate, _: run_gates.Context
    ) -> run_gates.GateResult:
        ran.append(gate.name)
        return run_gates.GateResult.FAILED

    monkeypatch.setattr(run_gates, "run_gate", fake_run_gate)
    assert run_gates.main([]) == 1
    assert ran == [GATES_IN_ORDER[0]], (
        "every gate ran despite the first one failing — the run is not fail-fast"
    )


def test_fast_skips_the_slow_gates(monkeypatch: pytest.MonkeyPatch) -> None:
    """--fast is defined by which gates it drops, so name them explicitly."""
    ran: list[str] = []

    def fake_run_gate(
        gate: run_gates.Gate, _: run_gates.Context
    ) -> run_gates.GateResult:
        ran.append(gate.name)
        return run_gates.GateResult.PASSED

    monkeypatch.setattr(run_gates, "run_gate", fake_run_gate)
    assert run_gates.main(["--fast"]) == 0
    assert "pytest" not in ran
    assert "pip-audit" not in ran
    assert "ruff-check" in ran


def test_fast_over_an_explicit_slow_gate_does_not_call_it_ci_only(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The empty-selection message must name the real reason.

    Reporting "every selected gate is CI-only" for a local gate that `--fast`
    dropped tells the reader the gate cannot run on this machine, when it can.
    """
    assert run_gates.main(["pytest", "--fast"]) == 1
    err = capsys.readouterr().err
    assert "pytest" in err
    assert "--fast" in err
    assert "CI-only" not in err


def test_selecting_only_ci_only_gates_fails_rather_than_reporting_success() -> None:
    """Nothing ran, so exit 0 would be a green with no gate behind it."""
    assert run_gates.main(["gitleaks"]) == 1


def test_print_runs_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """--print is for reading a command, so it must not execute one."""

    def explode(step: run_gates.Step) -> int:
        raise AssertionError(f"--print executed {step.shown()}")

    monkeypatch.setattr(run_gates, "run_step", explode)
    assert run_gates.main(["--print", "ruff"]) == 0


# --------------------------------------------------------------------------
# The canary step: fail-closed arguments, and running after a failure
# --------------------------------------------------------------------------


def test_the_canary_keeps_the_literal_glob_when_no_worker_logs_exist(
    tmp_path: Path,
) -> None:
    """A dead sink must reach the scanner as an unreadable path, not as silence.

    `scan_log_canary.py` turns the unmatched literal `canary-logs/*.log` into
    exit 2, and its comment calls that load-bearing for the parallel leg's
    fail-closed property (ADR-0063). Expanding the glob in Python to an empty
    list instead would hand it only the controller stream, which it scans
    happily and exits 0 on — a broken capture reported as a green gate.
    """
    log = tmp_path / "pytest-output.log"
    canary_dir = tmp_path / "canary-logs"
    canary_dir.mkdir()

    argv = run_gates._canary_argv(log, canary_dir)  # pyright: ignore[reportPrivateUsage]

    assert argv[-1].endswith("*.log")
    assert not Path(argv[-1]).is_file(), "the scanner must find this path unreadable"


def test_the_canary_expands_worker_logs_when_the_sink_produced_them(
    tmp_path: Path,
) -> None:
    """With matches it behaves as a shell does — real paths, no literal left."""
    log = tmp_path / "pytest-output.log"
    canary_dir = tmp_path / "canary-logs"
    canary_dir.mkdir()
    (canary_dir / "canary-gw0.log").write_text("", encoding="utf-8")
    (canary_dir / "canary-gw1.log").write_text("", encoding="utf-8")

    argv = run_gates._canary_argv(log, canary_dir)  # pyright: ignore[reportPrivateUsage]

    assert not any("*" in arg for arg in argv)
    # Identity, not a count: asserting "three .log arguments" passes just as
    # readily when one worker's path is duplicated and the other dropped, which
    # is a realistic "a worker log never gets scanned" regression. The scanner
    # treats argv positions alike, so a set is the right shape.
    assert set(argv) >= {
        str(canary_dir / "canary-gw0.log"),
        str(canary_dir / "canary-gw1.log"),
    }
    assert str(log) in argv


def test_an_always_run_step_runs_after_an_earlier_step_failed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """CI runs the canary scan on test failure; so must this.

    ci.yml states the reason where it sets the condition: a failing test's
    traceback is the likeliest place for a leaked value. Short-circuiting on the
    pytest step drops exactly the case the scan exists for.
    """
    ran: list[str] = []

    def fake_run_step(step: run_gates.Step, _env: dict[str, str] | None = None) -> int:
        ran.append(step.shown())
        return 1

    monkeypatch.setattr(run_gates, "run_step", fake_run_step)
    gate = gate_named("pytest")
    assert run_gates.run_gate(gate, _context(tmp_path)) is run_gates.GateResult.FAILED
    assert len(ran) == 2, "the canary step did not run after pytest failed"
    assert "scan_log_canary.py" in ran[1]


def test_the_pytest_gate_marks_its_canary_step_always_run(tmp_path: Path) -> None:
    """Stated on the step, not inferred from its position in the list."""
    steps = gate_named("pytest").steps(_context(tmp_path))
    assert [step.always_run for step in steps] == [False, True]


def test_the_real_canary_step_defers_its_argv_until_the_worker_logs_exist(
    tmp_path: Path,
) -> None:
    """The *live* pytest gate must carry `rebuild`, not merely be able to.

    At gate-build time the canary directory is empty, so the argument list
    freezes at the literal glob — which `scan_log_canary.py` treats as
    unreadable and fails closed on. Dropping `rebuild` from `_pytest` therefore
    breaks every parallel run, and the suite could not see it: the mechanism
    test above uses a synthetic gate, so it stays green while the real registry
    regresses. This asserts against the registry itself.
    """
    test_step, canary = gate_named("pytest").steps(_context(tmp_path))
    assert canary.rebuild is not None, (
        "the pytest gate's canary step would freeze its argv at build time, "
        "before any worker log exists"
    )

    # Nothing had been written when the step was built...
    assert not any("canary-gw" in arg for arg in canary.argv)

    # Two anchors, and both are needed.
    #
    # Ground truth first: the sink directory's name comes from ci.yml, so a
    # mutation renaming the runner's own `canary_dir` is caught. Taking the
    # paths *only* from the test step (as an earlier version of this test did)
    # compares the two steps to each other and to nothing else — and because
    # both Step calls close over one `canary_dir` local, renaming it moves them
    # in lockstep and the comparison goes blind. Derived from ci.yml rather than
    # restated here, for the reason the whole module exists.
    assert Path(test_step.env["CANARY_CAPTURE_DIR"]).name == _ci_canary_dir_name()

    # Then agreement: the two steps are independent call sites, and nothing else
    # checks they were built from the same scratch paths. Decoupling them leaves
    # each step individually valid while the scan reads a directory nothing
    # writes to.
    assert test_step.capture_to is not None
    worker = Path(test_step.env["CANARY_CAPTURE_DIR"]) / "canary-gw0.log"
    worker.parent.mkdir(parents=True, exist_ok=True)
    worker.write_text("", encoding="utf-8")

    rebuilt = list(canary.rebuild())
    assert str(test_step.capture_to) in rebuilt, (
        "the canary scans a different log than the test step writes"
    )
    assert str(worker) in rebuilt, (
        "the canary globs a different directory than the worker sink writes to"
    )
    assert not any("*" in arg for arg in rebuilt)


def test_run_gate_rebuilds_a_step_and_keeps_its_env_and_capture(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`run_gate` calls `rebuild` and replaces argv *only*.

    Driven through `run_gate` rather than by calling `replace()` here: an
    earlier version of this test performed the production line itself, so
    disabling the rebuild branch in `run_gate` left the whole suite green.

    Scope, stated so it is not over-read: this is a property of the *mechanism*,
    proved on a synthetic gate that combines `rebuild` with `env` and
    `capture_to`. No step in the live registry combines all three today, so this
    guards the mechanism against a future step that does — it says nothing about
    whether the pytest gate uses `rebuild` at all. That is a separate claim and
    has its own test below.
    """
    seen: list[run_gates.Step] = []

    def fake_run_step(step: run_gates.Step, _env: dict[str, str] | None = None) -> int:
        seen.append(step)
        return 0

    monkeypatch.setattr(run_gates, "run_step", fake_run_step)
    step = run_gates.Step(
        ["original"],
        "display",
        env={"KEEP": "1"},
        capture_to=tmp_path / "log",
        rebuild=lambda: ["rebuilt"],
    )
    gate = run_gates.Gate(
        name="synthetic",
        job="test",
        summary="a gate that exists only for this test",
        ci_steps=(),
        build=lambda _: [step],
    )

    assert run_gates.run_gate(gate, _context(tmp_path)) is run_gates.GateResult.PASSED
    assert list(seen[0].argv) == ["rebuilt"], "run_gate did not call rebuild()"
    assert seen[0].env == {"KEEP": "1"}, "rebuild dropped the step's env"
    assert seen[0].capture_to == tmp_path / "log", "rebuild dropped the tee"


def test_main_removes_the_scratch_directory_after_a_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The cleanup is wired into `main`, not merely available on Context.

    `Context.cleanup` having its own unit test proves the method works; it does
    not prove anything calls it. Removing `main`'s `finally` left the suite
    green, which for a leak the module docstring calls a containment surface is
    the wrong thing to be silent about.
    """
    created: list[Path] = []

    def fake_run_gate(
        _gate: run_gates.Gate, ctx: run_gates.Context
    ) -> run_gates.GateResult:
        created.append(ctx.scratch_dir())
        return run_gates.GateResult.PASSED

    monkeypatch.setattr(run_gates, "run_gate", fake_run_gate)
    assert run_gates.main(["adr-index"]) == 0
    assert created, "the fake gate never asked for a scratch directory"
    assert not created[0].exists(), "main left the scratch directory behind"


def test_main_does_not_fold_an_empty_gate_into_the_passed_count(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The EMPTY outcome has to survive aggregation, not just exist.

    `run_gate` returning EMPTY is tested separately; this is `main`'s half.
    Counting EMPTY as passed left the suite green, which would put a gate that
    scanned nothing inside "All N selected gate(s) passed".
    """
    empty_gate = "markdown-lint"

    def fake_run_gate(
        gate: run_gates.Gate, _ctx: run_gates.Context
    ) -> run_gates.GateResult:
        if gate.name == empty_gate:
            return run_gates.GateResult.EMPTY
        return run_gates.GateResult.PASSED

    monkeypatch.setattr(run_gates, "run_gate", fake_run_gate)
    docs = run_gates.groups()["docs"]
    # Non-zero even though every gate that *ran* passed: the empty one examined
    # nothing, and `/land` reads this exit code.
    assert run_gates.main(["docs"]) == 1

    out = capsys.readouterr().out
    assert f"All {len(docs) - 1} selected gate(s) passed" in out
    # The distinctive half of the empty-gate summary. Matching "nothing to run"
    # would also match the unrelated all-gates-skipped message on stderr, so it
    # could pass without this branch ever running.
    assert "they prove nothing" in out
    assert empty_gate in out


def test_run_step_forces_utf8_in_the_child(monkeypatch: pytest.MonkeyPatch) -> None:
    """The tee decodes UTF-8, so the child has to encode it.

    On Windows a piped child defaults to the legacy code page (measured cp1252
    on this machine), which either crashes the gate on a non-ASCII character or
    lands it in the tee'd log as U+FFFD — mangled before the canary scans it.
    An ambient PYTHONUTF8=0 must not win, which is why this asserts the value
    rather than merely its presence.
    """
    seen: dict[str, str] = {}

    class Completed:
        returncode = 0

    # Same widening as the mkdtemp spy below, and for the same reason: this
    # rebinds `subprocess.run` on the shared module, so a narrow signature turns
    # any unrelated caller reached during the test into a TypeError.
    def fake_run(*args: object, **kwargs: object) -> Completed:
        env = kwargs.get("env")
        if isinstance(env, dict):
            seen.update(cast("dict[str, str]", env))
        return Completed()

    monkeypatch.setenv("PYTHONUTF8", "0")
    monkeypatch.setattr(run_gates.subprocess, "run", fake_run)
    run_gates.run_step(run_gates.Step(["x"]), {"PYTHONUTF8": "1"})
    assert seen["PYTHONUTF8"] == "1"


def test_the_captured_branch_forces_utf8_in_a_real_child(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The tee'd branch is the one the property is actually *for*.

    `capture_to` is what the pytest gate uses, and it is the path where a
    mismatch matters: the parent decodes the pipe as UTF-8 with
    errors="replace", so a child encoding cp1252 lands in the canary-scanned log
    as U+FFFD. The sibling test above covers only the uncaptured branch, and
    bypassing the merged env on *this* one left the suite green.

    A real child rather than a fake, so the assertion is about what the process
    actually saw rather than about a dict the test helped build.

    Two limits, both deliberate. `run_step` merges the env once for both
    branches, so a fault in *that* line reddens this and its sibling together;
    what each covers alone is its own branch's use of the merged value, which is
    the branch-specific regression (`env=os.environ` on one call site only).
    And the discriminating power is platform-dependent: on Windows the
    un-forced default is measured cp1252, so this separates fixed from reverted;
    on a POSIX leg whose locale is already UTF-8 it may pass either way. It is
    the sibling's faked-env assertion that holds everywhere.
    """
    log = tmp_path / "captured.log"
    monkeypatch.setenv("PYTHONUTF8", "0")
    step = run_gates.Step(
        [
            sys.executable,
            "-c",
            "import os, sys; print(os.environ.get('PYTHONUTF8')); "
            "print(sys.stdout.encoding.lower())",
        ],
        capture_to=log,
    )

    # The env comes from the same derivation main() uses, not a literal — the
    # point of this test is that the value CI sets reaches a real child.
    assert run_gates.run_step(step, _context().workflow_env) == 0
    lines = log.read_text(encoding="utf-8").split()
    assert lines[0] == "1", "the ambient PYTHONUTF8=0 reached the child"
    assert lines[1] == "utf-8", "the child encoded its pipe as something else"


# --------------------------------------------------------------------------
# Scratch lifecycle and the empty-gate outcome
# --------------------------------------------------------------------------


def test_listing_and_printing_create_no_scratch_directory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """--list and --print read commands; creating a directory to print its name
    is a side effect, and the ones this used to leak held captured test output.
    """
    made: list[str] = []
    real_mkdtemp = run_gates.tempfile.mkdtemp

    # `*args, **kwargs` rather than the narrower `(*, prefix)` this once had:
    # monkeypatch rebinds the attribute on the shared `tempfile` module, so any
    # other code reached during main() — a plugin, a library on the subprocess
    # path — calling `mkdtemp()` bare or positionally would raise TypeError from
    # somewhere unrelated. Delegating costs nothing that the assertion needs.
    def spy(*args: Any, **kwargs: Any) -> str:
        made.append("mkdtemp")
        return cast("str", real_mkdtemp(*args, **kwargs))

    monkeypatch.setattr(run_gates.tempfile, "mkdtemp", spy)
    assert run_gates.main(["--list"]) == 0
    assert run_gates.main(["--print", "pytest"]) == 0
    assert made == []


def test_cleanup_removes_the_scratch_directory() -> None:
    """It holds captured test output outside the repo, where the containment
    gate does not look — so it is removed rather than left to accumulate."""
    ctx = run_gates.Context(pins={}, gitleaks_version=None)
    scratch = ctx.scratch_dir()
    assert scratch.is_dir()
    ctx.cleanup()
    assert not scratch.exists()


def test_a_gate_with_nothing_to_run_is_not_counted_as_passed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An empty step list proves nothing, so it gets its own outcome.

    The markdown gate returns no steps when its pathspec matches nothing. Folded
    into the pass count, a pathspec that silently stopped matching would be
    indistinguishable from a clean scan of every file.
    """
    monkeypatch.setattr(run_gates, "_tracked_markdown", list)
    result = run_gates.run_gate(gate_named("markdown-lint"), _context(tmp_path))
    assert result is run_gates.GateResult.EMPTY
    assert result is not run_gates.GateResult.PASSED


# --------------------------------------------------------------------------
# The markdown gate's file list
# --------------------------------------------------------------------------


def test_the_markdown_gate_batches_long_file_lists(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A file list too long for one Windows command line is split, not truncated.

    CI gets this from ``xargs``; here it is explicit, so it needs a case. The
    assertion that matters is the count — every file appears in exactly one
    batch — because dropping files silently is a partial scan reported as clean.
    """
    files = [f"docs/{index:04d}-{'x' * 60}.md" for index in range(400)]
    monkeypatch.setattr(run_gates, "_tracked_markdown", lambda: files)

    steps = run_gates._markdown_lint(_context(tmp_path))  # pyright: ignore[reportPrivateUsage]

    assert len(steps) > 1, "a 400-file list should not fit in one command"
    scanned = [arg for step in steps for arg in step.argv if arg.endswith(".md")]
    assert scanned == files
    for step in steps:
        limit = run_gates._MAX_COMMAND_CHARS  # pyright: ignore[reportPrivateUsage]
        assert len(" ".join(step.argv)) <= limit


def test_the_markdown_gate_does_nothing_when_no_files_match(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An empty list is a no-op, never a bare `scan` that would walk everything."""
    monkeypatch.setattr(run_gates, "_tracked_markdown", list)
    built = run_gates._markdown_lint(_context(tmp_path))  # pyright: ignore[reportPrivateUsage]
    assert built == []


def test_the_markdown_gate_reads_the_repositorys_own_files() -> None:
    """The git query works here, and this repo has markdown for it to find."""
    files = run_gates._tracked_markdown()  # pyright: ignore[reportPrivateUsage]
    assert "CLAUDE.md" in files
    assert all(name.endswith(".md") for name in files)
    assert not any(name.startswith("specs/personal/") for name in files)
