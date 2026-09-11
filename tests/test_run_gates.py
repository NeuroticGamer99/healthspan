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

import contextlib
import io
import os
import re
import shutil
import sys
import tempfile
import time
from collections.abc import Generator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import pytest
import run_gates

REPO_ROOT = Path(__file__).resolve().parent.parent
ADR_DIR = REPO_ROOT / "specs" / "adr"
CI_TEXT = run_gates.CI_WORKFLOW.read_text(encoding="utf-8")

LOCAL_GATES = [gate for gate in run_gates.GATES if gate.local]
CI_ONLY_GATES = [gate for gate in run_gates.GATES if not gate.local]
PINNED_GATES = [gate for gate in run_gates.GATES if gate.version_pin]
# Gates whose pin governs a tool they need but do not name. Kept as its own
# roster because `version_pin`'s parametrizations assert the version appears in
# the rendered command, which is by definition false for this class — and a gate
# carrying only a tool_pin would otherwise be covered by neither.
TOOL_PINNED_GATES = [gate for gate in run_gates.GATES if gate.tool_pin]
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


@pytest.fixture(autouse=True)
def temp_root(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> Path:
    """Point every run directory this module can create at the test's own tmp.

    Autouse rather than requested, because the tests that needed it most were
    the ones that did not know they touched the temp directory at all. Measured
    before this existed: four tests that faked ``run_gate`` out entirely still
    reached the real ``%TEMP%`` through ``main``'s capture notice, created an
    empty directory each and ran the real prune over it — one ``pytest``
    invocation evicted all five retained runs and left four of its own leaks in
    their place, so the feature's stated retention was one run, not five.
    Making it a property of the module means a new test cannot reintroduce that
    by omission.

    It redirects ``run_gates._temp_root``, the module's single named reader,
    rather than ``tempfile.gettempdir``: the latter is the stdlib module object
    and patching it redirects the whole process, pytest's own machinery
    included.

    **One test opts out**, via the ``unpatched_temp_root`` marker. Patching a
    function in every test in the file leaves its real body covered by nothing
    anywhere — measured: replacing it with a path that does not exist left all
    2,466 tests green. An autouse fixture with no way out is how a seam added
    for testability ends up being the one line the suite cannot see.
    """
    if "unpatched_temp_root" in request.keywords:
        return Path(tempfile.gettempdir())
    root = tmp_path / "temp-root"
    root.mkdir()
    monkeypatch.setattr(run_gates, "_temp_root", lambda: root)
    return root


def _mode_bits_refuse_deletion(parent: Path) -> bool:
    """Whether this filesystem really enforces a non-writable directory.

    `sys.platform` cannot answer it. Measured under one WSL install: `chmod
    0o500` refuses the unlink on native ext4 and refuses *nothing* on a DrvFs
    mount — a Windows drive seen from inside WSL — where the whole `rmtree`
    then succeeds. Both report `linux`, so the platform name selects the
    mechanism while saying nothing about whether it works.

    That matters because the caller's premise is "something here refuses
    deletion". Where it silently does not, every test built on `undeletable`
    passes for the wrong reason — the same shape as the Windows-only spelling
    that reached the POSIX leg green, one layer further in. Probing the
    property beats naming the filesystem, which is `specs/testing-strategy.md`'s
    standing rule for exactly this class of guard.
    """
    probe = parent / "probe"
    probe.mkdir()
    victim = probe / "x"
    victim.write_text("probe", encoding="utf-8")
    mode = probe.stat().st_mode
    os.chmod(probe, 0o500)
    try:
        victim.unlink()
    except OSError:
        return True
    else:
        return False
    finally:
        os.chmod(probe, mode)
        shutil.rmtree(probe, ignore_errors=True)


@contextlib.contextmanager
def undeletable(run: Path) -> Generator[None]:
    """Make ``run`` survive an ``rmtree(ignore_errors=True)``, on either platform.

    The two refuse deletion for different reasons and neither mechanism works on
    the other. Windows will not unlink a file that is open, so holding one open
    is enough. POSIX unlinks an open file happily and refuses instead when the
    *containing directory* is not writable.

    Written per platform because the Windows-only spelling did not fail on Linux
    — it passed its own premise assertion straight into a green suite until the
    POSIX leg ran it. That is the whole argument for the second leg, and it is
    why this helper asserts nothing itself: the caller checks the premise held.
    """
    sub = run / "sub"
    sub.mkdir(exist_ok=True)
    log = sub / "pytest-01.log"
    log.write_text("still being written", encoding="utf-8")
    if sys.platform == "win32":
        with log.open("a", encoding="utf-8"):
            yield
        return
    if os.geteuid() == 0:
        pytest.skip("running as root: directory permissions do not refuse anything")
    if not _mode_bits_refuse_deletion(run):
        pytest.skip(
            "this filesystem ignores directory mode bits, so nothing here "
            "would refuse the deletion this helper exists to provoke"
        )
    mode = sub.stat().st_mode
    os.chmod(sub, 0o500)
    try:
        yield
    finally:
        # Restored so pytest can clear its own tmp_path afterwards.
        os.chmod(sub, mode)


def completed(run: Path, stamp: float | None = None) -> Path:
    """Mark ``run`` finished at ``stamp``, the way a run reaching `cleanup` would.

    A prune only considers marked directories, so a fixture that seeds a "past
    run" without this is seeding an *in-progress* one — which is a different
    test, and one that would pass while the retention it means to check does
    nothing. The stamp goes on the mark rather than on the directory for the
    same reason the code reads it there: the directory's mtime is not when the
    run finished.
    """
    run.mkdir(parents=True, exist_ok=True)
    marker = run / run_gates.COMPLETE_MARKER
    marker.touch()
    if stamp is not None:
        os.utime(marker, (stamp, stamp))
    return run


@dataclass
class StepRecorder:
    """What `run_step` was handed, for a test driving `run_gate` without children."""

    steps: list[run_gates.Step]
    envs: list[dict[str, str] | None]
    # Every argument `run_gate` forwards, not only the ones a test happened to
    # want: a stand-in that drops one makes the call site that supplies it
    # untestable, which is how `echo=ctx.verbose` came to survive being
    # replaced by a literal with all 2,466 tests still green.
    echoes: list[bool]

    def shown(self) -> list[str]:
        return [step.shown() for step in self.steps]


def record_steps(
    monkeypatch: pytest.MonkeyPatch, *, exit_code: int = 0
) -> StepRecorder:
    """Stand in for `run_step`, recording every call and returning ``exit_code``.

    Eight copies of this stub differed only in what they appended and what they
    returned, and each restated `run_step`'s signature in full — so making
    ``echo`` a required argument meant either editing eight stubs or leaving
    eight encodings of a contract the function no longer has. Recording the
    whole `Step` and the workflow env covers what all eight wanted: the argv,
    the step's own env, and the sink are all reachable from what is kept here.
    """
    recorder = StepRecorder(steps=[], envs=[], echoes=[])

    def fake_run_step(
        step: run_gates.Step,
        env: dict[str, str] | None = None,
        *,
        echo: bool,
    ) -> int:
        recorder.steps.append(step)
        recorder.envs.append(env)
        recorder.echoes.append(echo)
        return exit_code

    monkeypatch.setattr(run_gates, "run_step", fake_run_step)
    return recorder


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
        canary_dir_name=run_gates.read_workflow_env(CI_TEXT, "CANARY_CAPTURE_DIR"),
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
  CANARY_CAPTURE_DIR: synthetic-logs
"""


@pytest.mark.parametrize("gate", PINNED_GATES, ids=lambda g: g.name)
def test_a_bumped_pin_is_picked_up(gate: run_gates.Gate, tmp_path: Path) -> None:
    """Bumping a pin in ci.yml changes the command, with no edit here.

    The negative half is the point: the real version must be absent. Asserting
    only that the synthetic version appears would still pass if the builder
    emitted both, or if it fell back to a constant on a parse miss.
    """
    synthetic = run_gates.read_pins(SYNTHETIC)
    ctx = run_gates.Context(
        pins=synthetic,
        gitleaks_version=None,
        scratch=tmp_path,
        # From the synthetic workflow too, not the real one — the premise here
        # is a workflow whose every value differs from the repository's.
        canary_dir_name=run_gates.read_workflow_env(SYNTHETIC, "CANARY_CAPTURE_DIR"),
    )
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
# `name:` may sit on the `- ` line or on any later line of the same block — YAML
# does not order a step's keys. Anchoring on the first line silently dropped a
# step written `- run:` first, and a dropped step never reaches the claim check,
# so the runner could fall behind CI for that gate with the suite green.
_STEP_NAME = re.compile(r"^      (?:- |  )name: (.+?)\s*$")
# `run:` needs the identical treatment, and for the identical reason — it is the
# *other* key whose position was assumed. A step written `- run:` first put the
# run on the `- ` line, where a check anchored at body indent could not see it.
_STEP_RUN = re.compile(r"^      (?:- |  )run:")


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
        # Scan the whole block, not just its first line.
        matches = [m for m in (_STEP_NAME.match(line) for line in current) if m]
        if matches and any(_STEP_RUN.match(line) for line in current):
            names.append(matches[0].group(1))

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


def test_the_step_parser_finds_a_name_that_is_not_the_first_key() -> None:
    """YAML does not order a step's keys, and a dropped step is a silent hole.

    A step written `- run:` first still carries both keys, but an anchor on the
    block's first line returns no name for it — so it never reaches the claim
    check below and the runner can fall behind CI for that gate with the suite
    green. `test_the_step_parser_finds_the_workflows_steps` cannot see this:
    `len(steps) > 10` and one known name both still hold when one step is lost.
    """
    yaml = (
        "      - run: ruff check\n"
        "        name: name written second\n"
        "      - name: name written first\n"
        "        run: echo hi\n"
    )
    assert _named_run_steps(yaml) == ["name written second", "name written first"]


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


@pytest.mark.parametrize("gate", LOCAL_GATES, ids=lambda g: g.name)
def test_every_uv_run_step_is_locked(gate: run_gates.Gate, tmp_path: Path) -> None:
    """Report finding 5: `uv run` without `--locked` rewrites the lockfile.

    The flag reached one builder and not the two that construct their own
    `uv run --with ...` argv, and the single assertion covering it pinned the
    spec-links argv alone — so nothing objected. Without it, `uv run` re-locks
    whenever `pyproject.toml` has moved ahead of `uv.lock` and rewrites that
    *tracked* file from a step that reads as read-only.

    Ordering is not the answer, which is why this is an invariant over every
    gate rather than another ordering test: the lockfile gate runs ahead of both
    in a *full* run, and `run_gates.py pyright` / `pytest` / `test` each run
    their step with nothing in front of it — so `uv lock --check` afterwards
    certifies the file the typecheck gate just repaired.

    The rendered line is asserted too. It is what `--print` and `--list` hand an
    operator to paste, so a rendering that omits the flag reintroduces the defect
    one copy-paste later.

    **Asserted on `shown()`, not on `display`, and that is cut item C4.**
    `display` is an *override* that defaults to `""`; `--list` and `--print`
    both call `shown()`, which falls back to the argv. So the next gate added
    through `_uv_run` without a hand-spelled display line would have reddened
    here while being perfectly correct — and the fix an author reaches for in
    that moment is to hand-spell a second copy of the argv, which is precisely
    the duplication `_uv_run` exists to delete. A test that pushes an author
    toward the defect it is guarding is worse than no test.
    """
    for step in gate.steps(_context(tmp_path)):
        if Path(step.argv[0]).stem != "uv" or step.argv[1:2] != ["run"]:
            continue
        assert "--locked" in step.argv, (
            f"{gate.name} builds a `uv run` without --locked: {step.argv}"
        )
        assert "--locked" in step.shown(), (
            f"{gate.name}'s rendered command omits --locked: {step.shown()!r}"
        )


@pytest.mark.parametrize("gate", LOCAL_GATES, ids=lambda g: g.name)
def test_every_uv_run_step_carries_the_misattribution_note(
    gate: run_gates.Gate, tmp_path: Path
) -> None:
    """Report finding 14: `--locked` generalized and its explanation did not.

    The note was written into `_uv_python_step` in the same change that put
    `--locked` on every `uv run` — so the two builders that construct their own
    `uv run --with ...` argv, pyright and pytest, were newly put at risk and
    told nothing. `run_gates.py pyright`, `run_gates.py pytest` and
    `run_gates.py test` each run their step with no lockfile gate ahead of them,
    so on a branch that edits `pyproject.toml` without re-locking — exactly what
    adding `markdown-it-py` did — the step aborts inside `uv run --locked`
    before the tool starts and prints a bare `FAILED: pytest (exit N)` over uv's
    resolver output.

    **This is an invariant over every gate because the obvious control test
    cannot see the fix.** The report flagged the trap explicitly:
    `test_the_uv_python_gate_names_the_cause_its_gate_name_hides` pins the
    absence of a note on `adr-index`, which `_docs_gate` builds with
    `sys.executable` rather than `uv run` — so moving the note onto the `uv run`
    mechanism does not redden it, and its staying green is no evidence at all.
    Only a sweep over the gates that actually build a `uv run` is.

    The wording is asserted not to name a dead link, which is the other half:
    the note was hard-coded to the docs gates' failure mode from inside a
    generic builder, so a pytest step would have told its author to go looking
    for one.
    """
    for step in gate.steps(_context(tmp_path)):
        if Path(step.argv[0]).stem != "uv" or step.argv[1:2] != ["run"]:
            continue
        assert step.on_failure, (
            f"{gate.name} builds a `uv run` step with no failure note: {step.display!r}"
        )
        assert "lockfile" in step.on_failure, step.on_failure
        assert "dead link" not in step.on_failure, (
            f"{gate.name}'s note names the docs gates' failure mode: "
            f"{step.on_failure!r}"
        )


@pytest.mark.parametrize("gate", LOCAL_GATES, ids=lambda g: g.name)
def test_a_step_that_is_not_a_uv_run_carries_no_note(
    gate: run_gates.Gate, tmp_path: Path
) -> None:
    """The boundary the sweep above needs, stated as the same kind of sweep.

    A runner that put the note on *every* step would satisfy the invariant
    above while making it meaningless — the note's whole value is that it
    appears where a project sync can be the real cause and nowhere else.
    Written as a sweep rather than as one hand-picked gate for the reason the
    trap above records: a control naming a single gate stops being a control
    the moment that gate's builder changes.
    """
    for step in gate.steps(_context(tmp_path)):
        if Path(step.argv[0]).stem == "uv" and step.argv[1:2] == ["run"]:
            continue
        assert step.on_failure == "", (
            f"{gate.name} puts a uv-sync note on a step that runs no uv: "
            f"{step.display!r}"
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


def test_the_reviewer_agents_summary_names_no_single_owning_adr() -> None:
    """That gate answers to two ADRs, so its `--list` line may blame neither.

    ADR-0076 §7 makes this a decision rather than wording: a gate that can fail
    for a reason its own name excludes teaches the reader to misfile the
    failure. Its sibling identity sites are already held — the failure banner by
    `tests/test_check_reviewer_agents.py`, the CI step name by
    `test_every_ci_run_step_is_claimed` against `ci.yml` — and review measured
    this one as the remaining code string that any literal could replace with
    the whole suite staying green.

    The assertion is the property, not the sentence, so a rewording that keeps
    the gate rule-neutral stays green.

    Both ADRs are rejected, and case-insensitively. "Neither" is what the first
    line claims and what this test's name promises, so checking only ADR-0068
    let a summary naming the *other* owner pass a test forbidding it — and a
    case-sensitive `isolation` was evaded by `Isolation`.
    """
    (gate,) = [g for g in run_gates.GATES if g.name == "reviewer-agents"]
    neutral = gate.summary.casefold()
    assert "adr-0068" not in neutral, gate.summary
    assert "adr-0076" not in neutral, gate.summary
    assert "isolation" not in neutral, gate.summary
    # Deliberately NOT casefolded: only the negatives need it. Folding the
    # positive too was measured to let a "required Invariants" typo through.
    assert "invariants" in gate.summary, gate.summary


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


@pytest.mark.parametrize("gate", TOOL_PINNED_GATES, ids=lambda g: g.name)
def test_a_tool_pin_names_a_pin_the_workflow_actually_sets(
    gate: run_gates.Gate,
) -> None:
    """`tool_pin` is derived like every other pin, not a decorative string.

    Unlike `version_pin`, nothing enforces it at build time — it only feeds the
    `--list` column — so a renamed or invented pin name would render `needs —`
    and pass everything. Measured: renaming it to a name absent from ci.yml left
    105/105 green before this test existed.
    """
    pins = run_gates.read_pins(CI_TEXT)
    assert gate.tool_pin in pins, (
        f"{gate.name}'s tool_pin {gate.tool_pin!r} is not set in ci.yml"
    )


@pytest.mark.parametrize("gate", TOOL_PINNED_GATES, ids=lambda g: g.name)
def test_a_tool_pinned_gate_renders_its_required_version(
    gate: run_gates.Gate, tmp_path: Path
) -> None:
    """The rendered column carries the derived value, not a constant.

    `--list` is where a lockfile failure that is really a toolchain problem gets
    diagnosed, so the number beside it has to be the real pin.
    """
    ctx = _context(tmp_path)
    rendered = run_gates.version_of(gate, ctx)
    # Equality against the whole column, not a substring and not a token.
    # A substring test passes on "10.11.28" — a version contains itself with any
    # digit prepended, measured. Token membership fixes that but stays blind to
    # the wording: losing the "needs " prefix, or leaking a second version
    # alongside the right one, still passes. The prefix is what tells an
    # operator this is a requirement rather than the version in the command, so
    # it is part of what the column has to say.
    assert rendered == f"needs {ctx.pins[gate.tool_pin or '']}", (
        f"{gate.name} rendered {rendered!r}. The whole column is pinned, not "
        "just the value: the prefix is what tells an operator this is a "
        "requirement rather than the version in the command. Rewording it is a "
        "deliberate change to that contract — update this expectation with it."
    )


def test_run_gate_hands_the_workflow_env_to_every_step(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The wiring, not just the behaviour it enables.

    Every other env test calls `run_step` directly with an explicit argument,
    and the tests that go through `run_gate` fake `run_step` with a signature
    that discards it — so dropping `ctx.workflow_env` at the single production
    call site left 105/105 green. This captures what `run_gate` actually passes.
    """
    recorder = record_steps(monkeypatch)
    ctx = _context(tmp_path)
    assert (
        run_gates.run_gate(gate_named("adr-index"), ctx) is run_gates.GateResult.PASSED
    )
    assert recorder.envs == [ctx.workflow_env], (
        "run_gate did not forward the derived workflow env to its steps"
    )


@pytest.mark.parametrize("verbose", [False, True], ids=["quiet", "verbose"])
def test_run_gate_hands_the_verbose_flag_to_every_step(
    verbose: bool, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The sibling of the env test above, and it was missing for the same reason.

    `echo` is only *observable* for a step that already carries its own sink —
    under `--verbose` `run_gate` assigns none, so an ordinary step takes
    `run_step`'s uncaptured branch where `echo` is never read. That leaves one
    behaviour-changing combination, `--verbose` with the pytest gate, which no
    test drove: measured, replacing `echo=ctx.verbose` with `echo=False` left
    the whole 2,466-test suite green.

    Parametrized over both values because a single value is satisfiable by a
    literal. Asserted on what `run_gate` forwards rather than on console output,
    which is what makes it independent of which gate happens to bring a sink.
    """
    recorder = record_steps(monkeypatch)
    ctx = _context(tmp_path)
    ctx.verbose = verbose

    assert (
        run_gates.run_gate(gate_named("adr-index"), ctx) is run_gates.GateResult.PASSED
    )

    assert recorder.echoes == [verbose], (
        "run_gate did not forward --verbose to its steps, so a gate carrying "
        "its own sink would be silently captured or silently replayed twice"
    )


def test_the_canary_glob_is_unchanged_by_a_directory_that_never_existed(
    tmp_path: Path,
) -> None:
    """Removing the eager mkdir rests on missing ≡ empty; assert it.

    `_pytest` no longer creates the canary directory — conftest's sink does, on
    first write. The claim that a never-created directory and an empty one
    produce the same arguments was true by inspection and by nothing else; the
    other canary tests all mkdir first.
    """
    never_created = tmp_path / "canary-logs"
    assert not never_created.exists()
    missing = run_gates._canary_argv(tmp_path / "out.log", never_created)  # pyright: ignore[reportPrivateUsage]

    never_created.mkdir()
    empty = run_gates._canary_argv(tmp_path / "out.log", never_created)  # pyright: ignore[reportPrivateUsage]

    assert missing == empty
    assert missing[-1].endswith("*.log")


def test_conflicting_workflow_env_values_are_refused() -> None:
    """The same refusal read_pins makes — first-match-wins is a silent guess."""
    text = 'env:\n  PYTHONUTF8: "1"\nother:\n  PYTHONUTF8: "0"\n'
    with pytest.raises(run_gates.GateError) as excinfo:
        run_gates.read_workflow_env(text, "PYTHONUTF8")
    assert "PYTHONUTF8" in str(excinfo.value)


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
    run_gates.run_step(run_gates.Step(["x"]), {}, echo=True)
    assert seen["PYTHONUTF8"] == "0", (
        "run_step forced a value the workflow does not set — the literal is "
        "back, and it will outlive ci.yml's own setting"
    )


@pytest.mark.parametrize(
    "value",
    ["C:/outside", "/outside", "../outside", "a/b", "a\\b", "..", ".", ""],
    ids=repr,
)
def test_a_canary_dir_name_that_could_escape_the_scratch_is_refused(
    value: str,
) -> None:
    """Deriving the name made it external input; the join assumes it is a name.

    Measured: `scratch / "C:/outside"` is `C:\\outside` — an absolute right-hand
    side replaces the base outright — and `..` walks out of it. Either puts the
    captured worker logs where `Context.cleanup` cannot reach them, which is the
    leak this module already closed once, re-entered through the derivation.

    Separator forms do not escape (`a/b` stays inside) and are refused anyway:
    the contract is a *name*, `scan_log_canary.py`'s glob assumes one directory
    level, and rejecting the class is simpler to state than the subset that
    escapes — and cannot be wrong in the unsafe direction.
    """
    with pytest.raises(run_gates.GateError) as excinfo:
        run_gates.require_dir_name("CANARY_CAPTURE_DIR", value)
    assert "CANARY_CAPTURE_DIR" in str(excinfo.value)


def test_the_repositorys_own_canary_dir_name_is_accepted() -> None:
    """The guard must not reject the value ci.yml actually sets."""
    name = _ci_canary_dir_name()
    assert run_gates.require_dir_name("CANARY_CAPTURE_DIR", name) == name


def test_the_pytest_gate_refuses_an_escaping_canary_dir_name(tmp_path: Path) -> None:
    """The guard is wired into the builder, not merely available.

    A validator nothing calls is the shape this session has been caught by
    repeatedly — the mechanism tested, its production use not.
    """
    ctx = _context(tmp_path)
    ctx.canary_dir_name = "../outside"
    with pytest.raises(run_gates.GateError):
        gate_named("pytest").steps(ctx)


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
    recorder = record_steps(monkeypatch, exit_code=1)
    # A gate with more than one step, deliberately: against a single-step gate
    # the count assertion below holds whether or not the loop stops early, so it
    # could never fail. `pip-audit` has two ordinary steps and no `always_run`.
    gate = gate_named("pip-audit")
    assert len(gate.steps(_context(tmp_path))) > 1
    assert run_gates.run_gate(gate, _context(tmp_path)) is run_gates.GateResult.FAILED
    assert len(recorder.steps) == 1, "a failed step must not be followed by the next"


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
    recorder = record_steps(monkeypatch, exit_code=1)
    gate = gate_named("pytest")
    assert run_gates.run_gate(gate, _context(tmp_path)) is run_gates.GateResult.FAILED
    assert len(recorder.steps) == 2, "the canary step did not run after pytest failed"
    assert "scan_log_canary.py" in recorder.shown()[1]


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
    recorder = record_steps(monkeypatch)
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
    rebuilt = recorder.steps[0]
    assert list(rebuilt.argv) == ["rebuilt"], "run_gate did not call rebuild()"
    assert rebuilt.env == {"KEEP": "1"}, "rebuild dropped the step's env"
    assert rebuilt.capture_to == tmp_path / "log", "rebuild dropped the tee"


def test_main_prunes_older_runs_after_a_run(
    monkeypatch: pytest.MonkeyPatch, temp_root: Path
) -> None:
    """The cleanup is wired into `main`, not merely available on Context.

    `Context.cleanup` having its own unit test proves the method works; it does
    not prove anything calls it. Removing `main`'s `finally` left the suite
    green, which for a leak the module docstring calls a containment surface is
    the wrong thing to be silent about. The assertion moved from "the directory
    is gone" to "the run before it is gone" when retention replaced removal; the
    wiring it covers is the same `finally`.
    """
    stale = completed(temp_root / f"{run_gates.SCRATCH_PREFIX}stale", stamp=1)

    created: list[Path] = []

    def fake_run_gate(
        _gate: run_gates.Gate, ctx: run_gates.Context
    ) -> run_gates.GateResult:
        created.append(ctx.scratch_dir())
        return run_gates.GateResult.PASSED

    monkeypatch.setattr(run_gates, "run_gate", fake_run_gate)
    monkeypatch.setattr(run_gates, "RUNS_RETAINED", 1)
    assert run_gates.main(["adr-index"]) == 0
    assert created, "the fake gate never asked for a scratch directory"
    assert created[0].is_dir(), "main deleted the output it had just pointed at"
    assert not stale.exists(), "main left an aged-out run behind"


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
    # Local gates only: `main` never runs a CI-only gate, so folding one into
    # the expected count would make this assertion track the registry's shape
    # rather than what ran. The docs group has held a CI-only gate since
    # ADR-0072's ledger check, which asserts a property of `main` alone.
    by_name = run_gates.gate_by_name()
    docs = [name for name in run_gates.groups()["docs"] if by_name[name].local]
    # Non-zero even though every gate that *ran* passed: the empty one examined
    # nothing, and `/land` reads this exit code.
    assert run_gates.main(["docs"]) == 1

    out = capsys.readouterr().out
    # No "All": the word would contradict the empty-gate report on the next
    # line, which is what a reader sees before they reach the exit code.
    assert f"\n{len(docs) - 1} selected gate(s) passed" in out
    assert "All " not in out
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
    run_gates.run_step(run_gates.Step(["x"]), {"PYTHONUTF8": "1"}, echo=True)
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
    assert run_gates.run_step(step, _context().workflow_env, echo=True) == 0
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


def test_cleanup_keeps_this_runs_directory(temp_root: Path) -> None:
    """The directory is the run's product now, so cleanup does not remove it.

    It once did, because it holds captured test output outside the repo where
    the containment gate does not look. What changed is that the console no
    longer carries a step's output: a failure prints this path, and deleting the
    directory on the way out would make that path a lie. The leak is bounded by
    the prune below instead of eliminated.

    The temp root is redirected by the module's autouse fixture, so both
    `mkdtemp` and the prune that cleanup runs stay inside tmp — the real prune
    therefore executes here, over a directory holding only this run, and must
    leave it alone. An earlier version stubbed the prune out and credited a
    tmp-rooting it did not do.
    """
    ctx = run_gates.Context(pins={}, gitleaks_version=None)
    scratch = ctx.scratch_dir()

    assert scratch.is_dir()
    assert scratch.parent == temp_root
    ctx.cleanup()

    assert scratch.is_dir(), "cleanup removed the run's own captured output"


def test_cleanup_marks_this_run_complete_so_a_later_run_may_age_it_out(
    temp_root: Path,
) -> None:
    """Retention's eligibility rule is a written mark, not an inference.

    Without the mark a finished run is indistinguishable from a running one and
    would be retained forever — the unbounded accumulation the original
    unconditional removal existed to prevent, reached from the other side.
    """
    ctx = run_gates.Context(pins={}, gitleaks_version=None)
    scratch = ctx.scratch_dir()

    assert not (scratch / run_gates.COMPLETE_MARKER).exists(), (
        "the directory was marked complete before the run finished"
    )
    ctx.cleanup()

    assert (scratch / run_gates.COMPLETE_MARKER).is_file(), (
        "cleanup left the run unmarked, so no later prune can ever age it out"
    )


def test_cleanup_prunes_older_runs_but_not_this_one(temp_root: Path) -> None:
    """Retention is bounded: cleanup ages out the runs before this one.

    Paired with the test above, which alone would be satisfied by never
    deleting anything.

    Seven seeded runs against a retained five, with the expected survivor count
    written as a literal rather than derived from `RUNS_RETAINED`: deriving it
    is what let the constant be changed to 500 with the suite still green.
    """
    older = [
        completed(temp_root / f"{run_gates.SCRATCH_PREFIX}{index:02d}", stamp=index)
        for index in range(7)
    ]

    ctx = run_gates.Context(pins={}, gitleaks_version=None)
    scratch = ctx.scratch_dir()
    ctx.cleanup()

    assert scratch.is_dir(), "the prune removed the run that ran it"
    survivors = sorted(p.name for p in temp_root.glob(f"{run_gates.SCRATCH_PREFIX}*"))
    # This run is marked complete by its own cleanup, so it is inside the count.
    assert len(survivors) == 5
    assert scratch.name in survivors
    assert older[0].name not in survivors, "the oldest run was kept"
    assert older[-1].name in survivors, "the newest of the older runs was pruned"


def test_a_run_still_in_progress_is_never_pruned(temp_root: Path) -> None:
    """The defect that made retention unsafe: mtime is not an activity guard.

    Measured — a directory's mtime does not advance while a log inside it is
    appended — so a long pytest run's directory looks as old as when it was
    created, and five short runs finishing after it evict it mid-run. The canary
    worker sink goes with it and `scan_log_canary.py` then either fails with no
    failing test or, worse on Windows where the open controller log survives its
    closed worker siblings, scans a surviving subset and passes.

    Seeded with *more* completed runs than are retained, so the live directory
    is protected by being unmarked rather than by there being room for it.
    """
    live = temp_root / f"{run_gates.SCRATCH_PREFIX}live"
    live.mkdir()
    sink = live / "canary-logs"
    sink.mkdir()
    (sink / "canary-gw0.log").write_text("worker output", encoding="utf-8")
    for index in range(run_gates.RUNS_RETAINED + 2):
        # Every one of them finished *after* the live run started.
        completed(temp_root / f"{run_gates.SCRATCH_PREFIX}{index:02d}", stamp=1_000)

    removed = run_gates.prune_scratch_dirs(temp_root)

    assert live not in removed
    assert (sink / "canary-gw0.log").is_file(), (
        "the prune deleted a running invocation's canary evidence"
    )


def test_an_unmarked_directory_ages_out_once_it_is_past_the_grace_period(
    temp_root: Path,
) -> None:
    """A run killed before it could mark itself must not hold disk forever.

    The counterweight to the test above: "unmarked means leave it alone" is
    safe only while something eventually collects the ones that will never be
    marked.
    """
    orphan = temp_root / f"{run_gates.SCRATCH_PREFIX}orphan"
    orphan.mkdir()
    stamp = time.time() - run_gates.ORPHAN_GRACE_SECONDS - 60
    os.utime(orphan, (stamp, stamp))
    fresh = temp_root / f"{run_gates.SCRATCH_PREFIX}fresh"
    fresh.mkdir()

    removed = run_gates.prune_scratch_dirs(temp_root)

    assert removed == [orphan]
    assert fresh.is_dir(), "an unmarked run inside the grace period was collected"


def test_the_mode_bit_probe_leaves_the_directory_as_it_found_it(
    tmp_path: Path,
) -> None:
    """The probe writes inside a caller's directory, so it must clean up.

    Both branches of its `finally` are load-bearing and neither was observable:
    dropping the `rmtree` leaves `probe` behind, and dropping the `chmod` that
    precedes it leaves a directory `rmtree` cannot empty, so the same residue
    survives by a second route. Measured, the residue is mode `0o500` and
    non-empty, which pytest's own `tmp_path` teardown cannot remove either --
    it is `rmtree(ignore_errors=True)` too, and swallows the refusal.

    Asserting the directory's whole listing rather than `probe`'s absence keeps
    this honest about a probe that leaves something under another name.
    """
    parent = tmp_path / "parent"
    parent.mkdir()
    (parent / "keep.log").write_text("the caller's own file", encoding="utf-8")
    before = sorted(entry.name for entry in parent.iterdir())

    _mode_bits_refuse_deletion(parent)

    assert sorted(entry.name for entry in parent.iterdir()) == before


def test_a_live_run_past_the_grace_period_is_collected_like_an_orphan(
    temp_root: Path,
) -> None:
    """The residual ADR-0080 §2 admits, pinned rather than left as prose.

    `_abandoned` decides liveness from the directory's age, and age cannot
    prove the owning invocation exited, so a run still writing past the cutoff
    is collected exactly like one killed before it could mark itself. This
    asserts the behaviour that *is*, not the one that should be: it is the
    executable half of a documented gap, and a remedy proving an exit instead
    of inferring one is expected to fail here. Failing is then the signal to
    close the open-questions entry, which is the whole reason to write it down
    in a form that can fail.

    It costs no second process and no wall-clock wait. The misclassification is
    a property of the classification alone, so `undeletable` -- already the
    file's model of a log a live run still holds -- is enough to reach it, and
    the entry claimed for one round that a live invocation was needed. Note the
    ordering: the files are created *before* the backdate, because creating one
    refreshes the parent's mtime and would undo it. That is the same mechanism
    `prune_scratch_dirs` documents from the other side.
    """
    live = temp_root / f"{run_gates.SCRATCH_PREFIX}live"
    live.mkdir()

    with undeletable(live):
        sibling = live / "ruff-01.log"
        sibling.write_text("a step that finished", encoding="utf-8")
        stamp = time.time() - run_gates.ORPHAN_GRACE_SECONDS - 60
        os.utime(live, (stamp, stamp))

        removed = run_gates.prune_scratch_dirs(temp_root)

        # The premise, first: something refused deletion, so what follows is
        # the partial shape rather than the whole one. Only this assertion is
        # about the fixture — the other two are behaviour, the order and the
        # split `test_prune_reports_only_what_it_actually_removed` sets.
        assert live.is_dir(), (
            "the premise failed: nothing refused deletion, so this run was "
            "removed entire and the partial shape was never reached"
        )
        assert removed == [], (
            "the run survived, so a prune reporting it removed is the "
            "verified-against-the-filesystem contract breaking"
        )
        assert not sibling.exists(), (
            "a live run past the cutoff was not collected — if that is now by "
            "design, this characterization test has done its job and the "
            "open-questions entry it pins should be closed with it"
        )


def test_cleanup_drops_the_canary_sink_and_keeps_the_step_logs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Retention covers output someone will read, not working material.

    The worker sink is the whole reason the distinction is drawn rather than
    assumed: measured at ~104 MB for one run against ~530 KB of step logs, and
    it was never on the console, so keeping it would be half a gigabyte of
    captured test output per five runs bought for nobody. The registration is
    asserted against the *real* pytest gate — a `cleanup` that honours the list
    proves nothing if nothing ever puts the sink on it.
    """
    monkeypatch.setattr(run_gates, "prune_scratch_dirs", lambda: None)
    ctx = _context(tmp_path)
    test_step, _ = gate_named("pytest").steps(ctx)

    sink = Path(test_step.env["CANARY_CAPTURE_DIR"])
    sink.mkdir(parents=True)
    (sink / "canary-gw0.log").write_text("worker output", encoding="utf-8")
    assert sink in ctx.ephemeral, "the pytest gate did not register its sink"
    assert test_step.capture_to is not None
    test_step.capture_to.write_text("controller output", encoding="utf-8")

    ctx.cleanup()

    assert not sink.exists(), "the worker sink survived the run that consumed it"
    assert test_step.capture_to.read_text(encoding="utf-8") == "controller output", (
        "the pytest step's own log was swept with the working material"
    )


def test_a_failed_run_keeps_the_working_material_its_report_pointed_at(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The green path's sweep is wrong on the red path, where the evidence is.

    A canary hit prints the worker log and the line its match came from, and
    that report is the last thing on the console before `cleanup` runs. Sweeping
    the sink there deletes the file between naming it and the operator opening
    it — the same "path printed beside a failure has to still resolve" rule that
    stopped `cleanup` deleting the run directory, one level in.
    """
    monkeypatch.setattr(run_gates, "prune_scratch_dirs", lambda: None)
    ctx = _context(tmp_path)
    test_step, _ = gate_named("pytest").steps(ctx)

    sink = Path(test_step.env["CANARY_CAPTURE_DIR"])
    sink.mkdir(parents=True)
    evidence = sink / "canary-gw3.log"
    evidence.write_text("the line the scan matched", encoding="utf-8")
    assert sink in ctx.ephemeral, "the pytest gate did not register its sink"

    ctx.failed = True
    ctx.cleanup()

    assert evidence.read_text(encoding="utf-8") == "the line the scan matched", (
        "the failure report's own evidence was deleted on the way out"
    )
    assert sink in ctx.ephemeral, (
        "the sink was dropped from the list, so a later sweep can never reach it"
    )


def test_prune_orders_by_completion_time_not_by_name(tmp_path: Path) -> None:
    """`mkdtemp` names are random, so a lexical sort evicts an arbitrary run.

    The directory named last alphabetically is made the *oldest*: a name-ordered
    prune keeps it, a completion-ordered prune removes it.
    """
    newest = completed(tmp_path / f"{run_gates.SCRATCH_PREFIX}aaa", stamp=200)
    oldest = completed(tmp_path / f"{run_gates.SCRATCH_PREFIX}zzz", stamp=100)

    removed = run_gates.prune_scratch_dirs(tmp_path, keep=1)

    assert removed == [oldest]
    assert newest.is_dir()
    assert not oldest.exists()


def test_prune_orders_by_the_mark_not_by_the_directorys_mtime(tmp_path: Path) -> None:
    """The two disagree, and the mark is the one that means "finished".

    A directory's mtime moves when its entries change — a partial `rmtree` bumps
    it, measured — so the directory the runner could least afford to keep can
    look like the newest thing in the root. Here the older *run* is given the
    newer directory stamp; ordering by the directory would keep it and evict the
    run that finished later.
    """
    early = completed(tmp_path / f"{run_gates.SCRATCH_PREFIX}early", stamp=100)
    late = completed(tmp_path / f"{run_gates.SCRATCH_PREFIX}late", stamp=200)
    os.utime(early, (900, 900))
    os.utime(late, (300, 300))

    assert run_gates.prune_scratch_dirs(tmp_path, keep=1) == [early]
    assert late.is_dir()


def test_prune_survives_a_temp_root_it_cannot_read(tmp_path: Path) -> None:
    """Pruning is housekeeping; it must not turn a green run red.

    There is no `except OSError` around the glob because there is nothing for it
    to catch: measured three ways on the pinned 3.14 — a missing root, a file
    used as a root, and an ACL-denied root — `Path.glob` swallows its own errors
    and yields nothing. `Path.iterdir` on that same denied directory *does*
    raise `PermissionError`, which is what shows the swallowing to be a property
    of `glob` rather than an accident of the cases tried. The guard that used to
    sit here was verified by a test that raised the exception itself, which
    proved only that `except OSError` catches `OSError`.

    The two portable cases are asserted; the denied one is not, because making a
    directory genuinely unreadable is platform-specific and a test that fakes it
    would be the same self-verifying shape this guard's removal was about.
    """
    missing = tmp_path / "not-a-directory"
    a_file = tmp_path / "a-file"
    a_file.write_text("x", encoding="utf-8")

    assert run_gates.prune_scratch_dirs(missing) == []
    assert run_gates.prune_scratch_dirs(a_file) == []


def test_prune_treats_an_unmarked_directory_it_cannot_stat_as_abandoned(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Neither call can show it to be live, so both readings agree on that.

    An unmarked directory is normally protected as a run in progress. One whose
    age cannot be read has no evidence of being live at all, and the opposite
    fallback would let a single unreadable directory sit in the root forever.
    """
    opaque = tmp_path / f"{run_gates.SCRATCH_PREFIX}opaque"
    opaque.mkdir()
    real_stat = Path.stat

    def refuse_one(self: Path, **kwargs: object) -> os.stat_result:
        if self.name == opaque.name:
            raise OSError("stat failed")
        return real_stat(self, **kwargs)  # pyright: ignore[reportArgumentType]

    monkeypatch.setattr(Path, "stat", refuse_one)

    assert run_gates.prune_scratch_dirs(tmp_path, keep=1) == [opaque]


def test_prune_reports_only_what_it_actually_removed(tmp_path: Path) -> None:
    """`Returns what it removed` has to survive the failure it tolerates.

    Verified against the filesystem: made undeletable by the means its platform
    actually respects, `rmtree(ignore_errors=True)` leaves the directory in
    place and returns normally. An unconditional append made the return value a
    list of what was *attempted*, so every caller that reported it was reporting
    a deletion that had not happened — and the concurrent-invocation case the
    function is built to tolerate is exactly when that is wrong.

    The premise is asserted before the behaviour, because `undeletable` cannot
    guarantee the filesystem cooperates and a prune over a directory that *was*
    removed would satisfy the second assertion for the wrong reason.
    """
    held = completed(tmp_path / f"{run_gates.SCRATCH_PREFIX}held", stamp=100)
    gone = completed(tmp_path / f"{run_gates.SCRATCH_PREFIX}gone", stamp=100)

    with undeletable(held):
        removed = run_gates.prune_scratch_dirs(tmp_path, keep=0)

    assert held.is_dir(), "the premise failed: the directory was removed after all"
    assert removed == [gone], "the prune claimed a directory that is still on disk"


def test_prune_leaves_directories_it_did_not_create(tmp_path: Path) -> None:
    """The glob is anchored to the runner's own prefix.

    It runs against the shared system temp directory, where everything else on
    the machine also keeps its working files.
    """
    stranger = tmp_path / "someone-elses-work"
    stranger.mkdir()
    os.utime(stranger, (1, 1))
    mine = completed(tmp_path / f"{run_gates.SCRATCH_PREFIX}old", stamp=2)

    assert run_gates.prune_scratch_dirs(tmp_path, keep=0) == [mine]
    assert stranger.is_dir()


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


def test_the_markdown_gate_reads_the_live_repository_files() -> None:
    """The git query works here, and this repo has markdown for it to find."""
    files = run_gates._tracked_markdown()  # pyright: ignore[reportPrivateUsage]
    assert "CLAUDE.md" in files
    assert all(name.endswith(".md") for name in files)
    assert not any(name.startswith("specs/personal/") for name in files)


# --------------------------------------------------------------------------
# 4. A gate that needs the project environment must be invoked through it
# --------------------------------------------------------------------------


def test_a_uv_python_gate_builds_a_complete_uv_run_invocation(tmp_path: Path) -> None:
    """The docs gate that needs project dependencies spells `uv run python`.

    `test_every_local_gate_names_a_runnable_program` proves only that
    `argv[0]` resolves on this machine, so it cannot see a malformed invocation
    of a program that *does* resolve: dropping `"run"` leaves `uv python <script>`,
    which is a real and unrelated uv subcommand, and the whole of this module
    stayed green under that mutation. The assertion is therefore on the argv the
    step actually carries, not on whether it starts.

    `spec-links` needs this because it parses CommonMark with markdown-it-py
    (ADR-0061, revised); a bare `python3` raises `ModuleNotFoundError` there.
    The gate is looked up rather than named literally, so that removing it from
    the registry fails here rather than silently skipping.
    """
    gate = next(g for g in run_gates.GATES if g.name == "spec-links")
    steps = gate.steps(_context(tmp_path))

    assert len(steps) == 1, steps
    argv = [str(part) for part in steps[0].argv]
    # `--locked` is part of the invocation, not decoration: without it `uv run`
    # re-locks whenever pyproject.toml has moved ahead of uv.lock, and this gate
    # runs *before* the lockfile gate — so it would repair the tracked uv.lock
    # that `uv lock --check` then certifies. Local green, CI red on a clean
    # checkout, from a gate that reads as read-only.
    assert [Path(argv[0]).stem, *argv[1:4]] == [
        "uv",
        "run",
        "--locked",
        "python",
    ], argv
    assert argv[4].endswith("check_spec_links.py"), argv
    assert steps[0].display == "uv run --locked python scripts/check_spec_links.py", (
        steps[0].display
    )


def test_the_uv_python_gate_names_the_cause_its_gate_name_hides(
    tmp_path: Path,
) -> None:
    """Report finding 4: `uv run` folds a project sync into a link check.

    The step syncs the project environment before running anything, so a stale
    lockfile or a failed wheel build fails *here* and the runner prints
    `FAILED: spec-links` — sending the author to hunt a dead link. The
    `lockfile` gate is the one that names the commonest such cause, and
    `test_the_docs_gate_runs_before_the_lockfile_gate` pins that it runs after
    this one, so it cannot get there first.

    Asserted on the step rather than on the printed output because the print is
    one line in `run_gate`; what can regress is the hint going missing from the
    step that needs it. The `lockfile` reference is asserted explicitly: a hint
    that does not name where to look next is decoration.
    """
    gate = next(g for g in run_gates.GATES if g.name == "spec-links")
    step = gate.steps(_context(tmp_path))[0]

    assert step.on_failure, "the uv-run docs gate carries no failure hint"
    assert "lockfile" in step.on_failure, step.on_failure

    # The control: a gate that runs under this process's own interpreter has no
    # sync to misattribute, so it carries no hint. Without this the assertion
    # above would pass for a runner that put the same hint on every step.
    plain = next(g for g in run_gates.GATES if g.name == "adr-index")
    assert plain.steps(_context(tmp_path))[0].on_failure == ""


def test_a_failing_step_prints_its_failure_note(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Smoke-round finding: the field was set but nothing read it.

    `test_the_uv_python_gate_names_the_cause_its_gate_name_hides` proves
    `_uv_python_step` *sets* `on_failure`; that leaves it data. `run_gate` is
    what turns it into something an operator sees, and deleting the two lines
    that do so left the whole of this module green — measured, 143 passed.
    A mechanism tested at its builder and not at its call site is a shape this
    repository has been caught by before.

    On stderr beside the command, because that is where the failure report
    goes; asserting on stdout would pass for a note nobody reading the failure
    would find.
    """

    record_steps(monkeypatch, exit_code=1)
    gate = next(g for g in run_gates.GATES if g.name == "spec-links")

    assert run_gates.run_gate(gate, _context(tmp_path)) is run_gates.GateResult.FAILED

    err = capsys.readouterr().err
    assert "FAILED: spec-links" in err, err
    assert "note: " in err, err
    assert "lockfile" in err, err


def test_a_failing_step_without_a_note_prints_none(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The boundary: the note is conditional, not decoration on every failure.

    Without this, the assertion above would pass for a `run_gate` that printed
    a `note:` line unconditionally — which would put an irrelevant remedy under
    every failing gate in the registry.
    """

    record_steps(monkeypatch, exit_code=1)
    gate = next(g for g in run_gates.GATES if g.name == "adr-index")

    assert run_gates.run_gate(gate, _context(tmp_path)) is run_gates.GateResult.FAILED

    err = capsys.readouterr().err
    assert "FAILED: adr-index" in err, err
    assert "note: " not in err, err


def test_the_docs_gate_runs_before_the_lockfile_gate() -> None:
    """The ordering `--locked` exists for, pinned as a relation not an ordinal.

    An unlocked `uv run` re-locks whenever `pyproject.toml` has moved ahead of
    `uv.lock`. That only matters because this gate runs *before* the lockfile
    gate: it would repair the tracked `uv.lock` that `uv lock --check` then
    certifies, so local passes while CI fails on a clean checkout.

    A relation, deliberately, and no positions. `_uv_python_step`'s docstring
    once claimed a specific one — "runs sixth", invented rather than measured,
    then copied into ADR-0061 — and any ordinal into a registry that gains
    entries is wrong the moment one is inserted above it. Its own test is what
    makes it findable: this lived inside the argv-construction test, where a
    reader looking for "what pins spec-links before lockfile" would not find it
    and a refactor of the argv assertions could take it along.
    """
    order = [gate.name for gate in run_gates.GATES]

    assert order.index("spec-links") < order.index("lockfile"), order


# --------------------------------------------------------------------------
# Spooled output
#
# The console stops carrying a step's output; the file starts holding all of
# it. What must not change is anything a gate *proves* — its exit code above
# all. The pairs below are what make each half meaningful: "absent from the
# console" is only interesting beside "present in the log", and the quiet
# default is only safe beside a failure that is still legible.
#
# Where a test here asserts on console output it reads `capfd`, never `capsys`.
# An uncaptured step inherits this process's file descriptor 1, which a
# Python-level capture does not see, so `capsys` reports the `--verbose`
# streaming case as silent — measured. For the quiet-path assertions either
# fixture would work, because there the parent does the writing; `capfd` is used
# throughout so the choice is not one to re-derive per test.
#
# Not every test here does that, and the sentence this replaced said they all
# did. Several assert on a file or on a `Step` field and read no capture at all,
# and two drive `run_gate` through a fake `run_step` rather than a child.
# --------------------------------------------------------------------------

# The oracle for both halves. Distinctive enough that finding it anywhere is
# unambiguous, and short enough not to wrap.
CHILD_LINE = "run-gates-spool-probe-line"


def _probe_gate(name: str, exit_code: int) -> run_gates.Gate:
    """A synthetic gate whose one step is a real child printing `CHILD_LINE`.

    A real subprocess rather than a stub: what is under test is the tee loop's
    two destinations and an exit code arriving back through a pipe, and a stub
    exercises neither.
    """
    return run_gates.Gate(
        name=name,
        job="synthetic",
        summary="prints a known line and exits with a chosen code",
        ci_steps=(),
        build=lambda _ctx: [
            run_gates.Step(
                [
                    sys.executable,
                    "-c",
                    f"print({CHILD_LINE!r}); raise SystemExit({exit_code})",
                ],
                display=f"probe {name}",
            )
        ],
    )


def test_the_quiet_path_returns_the_childs_own_exit_code(tmp_path: Path) -> None:
    """The load-bearing one: capturing must not swallow the failure.

    A `| tail` in a skill's prose would have answered the same context problem
    and would have returned the *pipe's* status instead of the command's —
    measured on this repository, as a confident false green. That is the whole
    reason the capture lives inside the runner rather than around it, so the
    property is asserted directly rather than inferred from the gate verdict.
    """
    log = tmp_path / "probe.log"
    step = run_gates.Step(
        [sys.executable, "-c", f"print({CHILD_LINE!r}); raise SystemExit(3)"],
        capture_to=log,
    )

    assert run_gates.run_step(step, echo=False) == 3


def test_a_failing_gate_still_exits_non_zero_through_main(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    """The same property end to end, where `/land` actually reads it.

    The unit test above proves `run_step` returns the code; this proves nothing
    between it and the process exit status drops it — and that the run does not
    go silent about why it failed.
    """
    monkeypatch.setattr(run_gates, "GATES", (_probe_gate("probe-fail", 5),))

    assert run_gates.main(["probe-fail"]) == 1
    assert CHILD_LINE in capfd.readouterr().out


def test_a_failing_steps_output_is_replayed_with_a_path_to_the_rest(
    tmp_path: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    """A red gate must be legible without going looking.

    Asserted on the distinctive line rather than on how many lines arrived: a
    count passes whenever *something* was printed, including the banner this
    replay is supposed to be adding output beneath.
    """
    ctx = _context(tmp_path)

    result = run_gates.run_gate(_probe_gate("probe-fail", 1), ctx)

    assert result is run_gates.GateResult.FAILED
    out = capfd.readouterr().out
    assert CHILD_LINE in out, "a failing step's output never reached the console"
    assert str(ctx.step_log("probe-fail", 1)) in out, (
        "the failure named no file to read the rest of the output from"
    )


def test_a_passing_steps_output_does_not_reach_the_console(
    tmp_path: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    """The saving half — and the assertion is an absence of a *known* line.

    Not a length threshold: a threshold passes whenever the run happened to be
    small, which is the wrong reason and would keep passing after the quiet
    path stopped working. The second assertion is what stops the first passing
    because nothing ran at all.
    """
    ctx = _context(tmp_path)

    result = run_gates.run_gate(_probe_gate("probe-pass", 0), ctx)

    assert result is run_gates.GateResult.PASSED
    out = capfd.readouterr().out
    assert CHILD_LINE not in out, "the child's output is still on the console"
    assert "probe probe-pass" in out, "the command line itself stopped being printed"


def test_the_log_holds_what_the_console_no_longer_carries(
    tmp_path: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    """The other half of the pair above: suppressed is not discarded."""
    ctx = _context(tmp_path)

    assert run_gates.run_gate(_probe_gate("probe-pass", 0), ctx) is (
        run_gates.GateResult.PASSED
    )

    assert CHILD_LINE not in capfd.readouterr().out
    log = ctx.step_log("probe-pass", 1)
    assert CHILD_LINE in log.read_text(encoding="utf-8"), (
        "the output left the console without arriving anywhere"
    )


def test_the_captured_log_lives_outside_the_repository() -> None:
    """Captured test output is the material the canary gate polices.

    Inside the tree it would also be staged by any `git add -A`, and this
    repository's checkpoint discipline exists because that has happened.
    Resolved as well as raw: the symlinked temp directory on some platforms
    makes the two different questions.
    """
    ctx = run_gates.Context(pins={}, gitleaks_version=None)
    try:
        log = ctx.step_log("probe", 1)

        assert REPO_ROOT not in log.parents
        assert REPO_ROOT not in log.resolve().parents
    finally:
        shutil.rmtree(ctx.scratch_dir(), ignore_errors=True)


def test_universal_capture_leaves_the_pytest_gates_own_sink_alone(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The canary contract is a separate, fail-closed consumer.

    `scan_log_canary.py` reads the controller log the pytest gate names and
    globs the worker directory `CANARY_CAPTURE_DIR` names; an empty glob is
    exit 2 by design. Assigning a sink to a step that already has one would
    point the scan at a file nothing writes and turn that fail-closed guard
    into a broken capture reported as clean.
    """
    recorder = record_steps(monkeypatch)
    ctx = _context(tmp_path)
    # Built from the same context, so the two agree on paths by construction
    # and any difference below is the runner's doing.
    built, _ = gate_named("pytest").steps(ctx)

    assert run_gates.run_gate(gate_named("pytest"), ctx) is run_gates.GateResult.PASSED

    test_step, canary = recorder.steps
    assert test_step.capture_to == built.capture_to, (
        "universal capture moved the log the canary scan is built from"
    )
    assert str(test_step.capture_to) in list(canary.argv), (
        "the canary scans a different log than the test step writes"
    )
    canary_dir = Path(test_step.env["CANARY_CAPTURE_DIR"])
    assert canary_dir.name == _ci_canary_dir_name()
    # The canary *scan* step has no sink of its own, so the runner assigns it
    # one. It must not land inside the directory the scan globs: an extra file
    # there is read as a worker log. Asserted on the assigned path rather than
    # on the directory's contents — `fake_run_step` writes nothing, so a
    # glob over `canary_dir` is empty whatever the paths say, which is an
    # oracle that cannot fail. It was here, and it was dead.
    assert canary.capture_to is not None
    assert canary_dir not in canary.capture_to.parents, (
        "the canary scan's own log was written into the directory it globs"
    )


def test_the_containment_gates_output_is_never_written_to_disk(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The one gate whose failure output names a path that is personal data.

    `check_personal_containment.py` prints the offending repository path on a
    violation, and ADR-0079 §R2 makes a filename under the personal directory
    provenance. Spooling it would leave a copy of the one thing the gate exists
    to find in a file that outlives the run. Console-only is the pre-change
    behaviour and costs one line.

    Asserted through `run_gate` against the **live registry entry**, not a
    synthetic gate: the flag being respected proves nothing if the gate that
    needs it stops setting it.
    """
    recorder = record_steps(monkeypatch)
    gate = gate_named("containment")
    assert gate.spool_output is False, "the gate stopped declaring its exemption"

    assert run_gates.run_gate(gate, _context(tmp_path)) is run_gates.GateResult.PASSED

    handed = recorder.steps
    assert handed, "the gate ran no steps, so the assertion below proves nothing"
    assert [step.capture_to for step in handed] == [None] * len(handed), (
        "the containment gate's output was given a file to be written to"
    )


def test_verbose_does_not_replay_a_step_it_already_streamed(
    tmp_path: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    """`--verbose` on a gate that brings its own sink is where `not echo` bites.

    Measured before this existed: dropping `and not echo` from the replay guard
    left the suite green. The uncaptured branch returns before reaching the
    clause, so it only discriminates when `capture_to` is set *and* `echo` is
    True — exactly one case, `--verbose` running the pytest gate, and the test
    covering `--verbose` drove a step with no sink at all. Under the mutation a
    failing verbose run streams its whole output and then prints the last lines
    of it again, and nothing notices.
    """
    log = tmp_path / "streamed.log"
    step = run_gates.Step(
        [sys.executable, "-c", f"print({CHILD_LINE!r}); raise SystemExit(1)"],
        capture_to=log,
    )

    assert run_gates.run_step(step, echo=True) == 1

    out = capfd.readouterr().out
    # Whole lines: the echoed `$ <argv>` line contains the probe string too, so
    # a substring count answers 2 whether or not anything was replayed.
    printed = [line for line in out.splitlines() if line.strip() == CHILD_LINE]
    assert len(printed) == 1, "the streamed output was replayed on top of itself"
    assert "line(s) of output" not in out, "a streamed step was replayed as if hidden"
    assert log.read_text(encoding="utf-8").strip() == CHILD_LINE, (
        "the gate's own sink stopped being written under --verbose"
    )


def test_no_gate_both_declines_the_runners_sink_and_brings_its_own(
    tmp_path: Path,
) -> None:
    """The exemption cannot take away a sink a builder already assigned.

    `spool_output=False` stops `run_gate` *assigning* a sink; it does not clear
    one a gate chose for itself. A gate declaring both — plausible for a future
    gate that needs a tee'd log for a downstream scan, the pattern the pytest
    gate already establishes, and can also print a personal path — would have
    its violation output written to a retained file and kept off the console,
    inverting the exemption exactly where it matters. Neither the set-membership
    test above nor the handed-steps test below can see it, because both hold one
    half of the pair fixed.
    """
    offenders = {
        gate.name
        for gate in run_gates.GATES
        if not gate.spool_output
        and any(step.capture_to is not None for step in gate.steps(_context(tmp_path)))
    }

    assert offenders == set()


def test_the_policy_defaults_are_pinned_here_and_stated_in_their_owning_adr() -> None:
    """Both halves of CLAUDE.md's routing rule 4, and both were open.

    **Unpinned:** `RUNS_RETAINED` 5 -> 500 and `FAILURE_TAIL_LINES` 40 -> 3 each
    left the suite green — measured — because every test derived its fixture
    size from the constant it was meant to be checking. Literals here are the
    point: an oracle that moves with the value under test cannot fail.

    **Unrecorded:** grepping ADR-0080 for these numbers matched exactly one
    line, the `### 5. Ownership` heading, while the ADR claimed to own the
    defaults and the code attributed both upward to it. The ADR's numbers are
    matched against the constants rather than against literals, so changing a
    constant reddens this until the document that owns it is changed too.

    **All three defaults, not the two the finding named.** The orphan grace
    period was added by the same change that pinned the other two and was left
    out of the pin — the same rule with a third site, which is how a rule gets
    written down as covering less than it does.
    """
    assert run_gates.RUNS_RETAINED == 5
    assert run_gates.FAILURE_TAIL_LINES == 40
    assert run_gates.ORPHAN_GRACE_SECONDS == 24 * 60 * 60

    adr = next(ADR_DIR.glob("0080-*.md")).read_text(encoding="utf-8")

    assert f"{run_gates.RUNS_RETAINED} most recently completed runs" in adr, (
        "ADR-0080 does not state the retention count it says it owns"
    )
    assert f"last {run_gates.FAILURE_TAIL_LINES} lines" in adr, (
        "ADR-0080 does not state the replay length it says it owns"
    )
    hours = run_gates.ORPHAN_GRACE_SECONDS // 3600
    assert f"{hours}-hour grace period" in adr, (
        "ADR-0080 does not state the orphan grace period it says it owns"
    )

    # The same drift, one category over: §2 cites tests by name as the things
    # that pin its claims, and a rename would leave those sentences quietly
    # false. Resolved against this module rather than spelled out, so a test
    # the ADR names later is covered without anyone remembering to add it.
    # Backtick-delimited and whole, so `tests/test_run_gates.py` — the module,
    # named in §2 for a different reason — is not mistaken for a function.
    cited = set(re.findall(r"`(test_[a-z0-9_]+)`", adr))
    assert cited, "ADR-0080 cites no test by name — did §2's citation move?"
    missing = sorted(name for name in cited if name not in globals())
    assert not missing, f"ADR-0080 names tests that no longer exist here: {missing}"


def test_containment_precedes_the_gates_that_lint_the_tree() -> None:
    """ADR-0080 §4 reasons from where `containment` sits; pin it, don't assert it.

    §4 bounds a residual by saying a full run reaches `containment` before a
    gate that could name a path under the containment directory, so the run
    stops first. That is true of `markdown-lint` and **false of `spec-links`**,
    which is ordered ahead of `containment` and runs on every full run before
    anything could halt it. The ADR said "both" for one commit, written from a
    truncated gate log rather than from the registry -- so the relationship is
    asserted here, where it is read from `GATES` and cannot be misremembered.

    This deliberately pins the order as it *is*, including the part §4 has to
    concede. Reordering `containment` ahead of `spec-links` would be a real
    improvement and would redden this test; that is the intended signal, and
    §4's bound is what should be rewritten when it fires.
    """
    order = [gate.name for gate in run_gates.GATES]
    for name in ("containment", "spec-links", "markdown-lint"):
        assert name in order, f"{name} left the registry; ADR-0080 §4 cites it"

    assert order.index("markdown-lint") > order.index("containment"), (
        "markdown-lint now runs before containment, so ADR-0080 §4's "
        "'a full run stops first' no longer holds for it either"
    )
    assert order.index("spec-links") < order.index("containment"), (
        "spec-links now runs after containment — an improvement ADR-0080 §4 "
        "does not yet claim; rewrite its bound rather than relaxing this"
    )


def test_the_gate_runner_is_not_reached_from_ci() -> None:
    """ADR-0080 §4 leans on CI never spooling; this pins the common spelling.

    Nothing is retained where this script does not run, and §4 uses that to
    bound where a lint log naming a mixed-case recreation can exist at all.
    CI invoking the runner would be a deliberate change and a reasonable one --
    it would simply also invalidate that bound, which is what this catches.

    **It reads `ci.yml`'s own text and nothing further, which is narrower than
    the sentence it defends.** Measured in review: a step running a wrapper
    script that itself invokes the runner leaves this green, because the string
    never appears in the workflow. Closing that means resolving every `run:`
    step's script and reading it, which is a lot of machinery for one conjunct
    of a defence-in-depth bound -- so the gap is stated in §4 rather than
    covered here, and this stays a tripwire on the spelling anyone would write.
    """
    workflow = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(
        encoding="utf-8"
    )

    assert "run_gates" not in workflow, (
        "ci.yml now invokes the gate runner, so ADR-0080 §4 can no longer say "
        "retention is local-only — update the ADR with this change"
    )


def test_containment_is_the_only_gate_exempt_from_spooling() -> None:
    """ADR-0080 §3 says "exactly one gate sets it", so assert the whole set.

    Asserting only that `containment` is exempt leaves a *second* gate quietly
    declaring itself exempt undetected — measured: adding `spool_output=False`
    to the lockfile gate left the suite green. An equality over the registry is
    what makes the ADR's word "exactly" load-bearing, and it fails loudly on a
    new exemption, which is the point: a new one needs the ADR updated with it.
    """
    exempt = {gate.name for gate in run_gates.GATES if not gate.spool_output}

    assert exempt == {"containment"}


def test_a_gate_without_the_exemption_is_still_spooled(tmp_path: Path) -> None:
    """The exemption's other half: `spool_output` defaults to on.

    Without this, `spool_output=False` everywhere would satisfy the test above
    and silently turn the whole change off.
    """
    ctx = _context(tmp_path)
    gate = _probe_gate("probe-pass", 0)
    assert gate.spool_output is True

    assert run_gates.run_gate(gate, ctx) is run_gates.GateResult.PASSED

    assert ctx.step_log("probe-pass", 1).is_file()


def test_verbose_streams_the_output_and_captures_nothing_extra(
    tmp_path: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    """The escape hatch restores what the script did before, not a variant.

    Including the absence of a *runner-assigned* capture: a tee'd child sees a
    pipe rather than a console, so it drops colour and changes its buffering,
    and capturing here would leave no way back to the original behaviour.

    The scope of that claim is the point. `--verbose` cannot restore the absence
    of a capture for a gate that brings its own sink — the pytest gate does, so
    the canary scan has something to read — and the unscoped version of this
    sentence was copied into ADR-0080 §1, where it was false. The sibling test
    for a gate carrying its own sink is what covers that case; this probe gate
    deliberately has none.
    """
    ctx = _context(tmp_path)
    ctx.verbose = True

    assert run_gates.run_gate(_probe_gate("probe-pass", 0), ctx) is (
        run_gates.GateResult.PASSED
    )

    assert CHILD_LINE in capfd.readouterr().out
    assert not ctx.step_log("probe-pass", 1).exists(), (
        "--verbose captured as well as streamed, which is not what it restores"
    )


def test_verbose_reaches_the_context_and_silences_the_capture_notice(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    """The flag has to be wired, not merely accepted by the parser."""
    seen: list[bool] = []

    def fake_run_gate(
        _gate: run_gates.Gate, ctx: run_gates.Context
    ) -> run_gates.GateResult:
        seen.append(ctx.verbose)
        return run_gates.GateResult.PASSED

    monkeypatch.setattr(run_gates, "run_gate", fake_run_gate)

    assert run_gates.main(["--verbose", "adr-index"]) == 0

    assert seen == [True]
    assert "Step output is captured to" not in capfd.readouterr().out


def test_the_replay_is_bounded_and_says_how_much_it_withheld(
    tmp_path: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    """A tail that silently starts mid-traceback reads as the whole failure."""
    log = tmp_path / "long.log"
    lines = run_gates.FAILURE_TAIL_LINES * 5
    step = run_gates.Step(
        [
            sys.executable,
            "-c",
            f"for i in range({lines}): print(f'line-{{i}}')\nraise SystemExit(1)",
        ],
        capture_to=log,
    )

    assert run_gates.run_step(step, echo=False) == 1

    out = capfd.readouterr().out
    kept = lines - run_gates.FAILURE_TAIL_LINES
    assert f"line-{lines - 1}" in out, "the end of the output was not replayed"
    assert f"line-{kept}" in out, "the replay was shorter than the tail it promises"
    assert f"line-{kept - 1}" not in out, "the replay was not bounded"
    assert f"{kept} earlier line(s) in the file" in out
    assert str(log) in out


def test_a_captured_child_is_given_no_stdin_to_block_on(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A captured child has no console to ask a question on, so it must not try.

    `uv`'s keyring prompt, a git credential helper: the question goes down the
    pipe into the log, nothing is on screen, and the child blocks on fd 0. Gate
    steps are deliberately unbounded, so that is an indefinite hang behind a
    `$ uv run ...` line indistinguishable from the normal two-minute test run —
    the one failure mode this runner cannot report, because it never returns.

    **Asserted on the call rather than on a prompting child, deliberately.**
    Measured: a child running `input()` fails under pytest whether or not this
    argument is passed, because pytest has already replaced the session's own
    stdin — so the behavioural spelling of this test passes with the fix
    removed. There is no black-box oracle for "would have hung forever" that a
    test suite can wait for, which leaves the argument itself as the thing to
    pin.
    """
    seen: list[object] = []
    real_popen = run_gates.subprocess.Popen

    def spy(*args: Any, **kwargs: Any) -> Any:
        seen.append(kwargs.get("stdin", "not passed"))
        return real_popen(*args, **kwargs)

    monkeypatch.setattr(run_gates.subprocess, "Popen", spy)
    step = run_gates.Step(
        [sys.executable, "-c", "pass"], capture_to=tmp_path / "quiet.log"
    )

    assert run_gates.run_step(step, echo=False) == 0

    assert seen == [run_gates.subprocess.DEVNULL], (
        "the captured child was left with a stdin it could block on"
    )


def test_a_sink_that_cannot_be_opened_is_reported_as_a_gate_that_could_not_start(
    tmp_path: Path,
) -> None:
    """`main` catches `GateError`, so anything else here is a raw traceback.

    Reachable in ordinary use — a full temp volume, a denied ACL, a concurrent
    run that removed the directory between the notice and the step. The comment
    on the sibling `Popen` guard states the promise this keeps: both paths
    report a step that could not start rather than one naming it and one
    crashing.
    """
    blocked = tmp_path / "not-a-directory"
    blocked.write_text("in the way", encoding="utf-8")
    step = run_gates.Step([sys.executable, "-c", "pass"], capture_to=blocked / "x.log")

    with pytest.raises(run_gates.GateError, match="could not capture the output"):
        run_gates.run_step(step, echo=False)


def test_the_replay_length_follows_the_constant_at_call_time(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    """The tail length is a default of this script, so something must pin it.

    Measured before this existed: `FAILURE_TAIL_LINES` 40 -> 3 left the suite
    green, because every assertion about the replay derived its own expectation
    from the constant — an oracle that moves with the thing it is checking.
    Written as literals here for that reason.

    It also pins the resolution site. Bound as a default argument the value is
    read once at import, so this monkeypatch would set the constant, change
    nothing, and the test would still pass by replaying all six lines.
    """
    monkeypatch.setattr(run_gates, "FAILURE_TAIL_LINES", 2)
    log = tmp_path / "short.log"
    step = run_gates.Step(
        [
            sys.executable,
            "-c",
            "for i in range(6): print(f'line-{i}')\nraise SystemExit(1)",
        ],
        capture_to=log,
    )

    assert run_gates.run_step(step, echo=False) == 1

    out = capfd.readouterr().out
    assert "--- last 2 line(s) of output, 4 earlier line(s) in the file ---" in out
    assert "line-5" in out
    assert "line-4" in out
    assert "line-3" not in out, "the replay ignored the constant it was given"


class _LegacyConsole(io.StringIO):
    """A console that refuses what its code page cannot represent.

    Which is what an un-reconfigured Windows stdout does. Faked rather than
    driven for real because the encoding of the console a test runs under is
    not the test's to choose.
    """

    encoding = "cp1252"

    def write(self, s: str) -> int:
        s.encode(self.encoding)
        return super().write(s)


def test_the_replay_prints_a_line_the_console_cannot_encode(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The failure reporter must not become the failure.

    `__main__` reconfigures stdout to UTF-8 with `errors="replace"`, so captured
    output is safe when this module is run as a script and unsafe when it is
    imported — which is what the HELM work items propose doing. The crash lands
    inside the code whose whole job is to make a failure legible, and it is the
    same outcome the `OSError` guard beside it exists to prevent.

    The fixture character is a **box-drawing** one, not an em dash. Measured:
    cp1252 encodes the em dash — it is 0x97 there — so a dash makes this test
    pass with the guard removed. Box drawing and arrows are what this
    repository's output actually carries and what cp1252 actually refuses.
    """
    console = _LegacyConsole()
    monkeypatch.setattr(run_gates.sys, "stdout", console)
    log = tmp_path / "wide.log"
    log.write_text("├─ a value of 5 → out of range\n", encoding="utf-8")

    run_gates.replay_tail(log)

    assert "out of range" in console.getvalue()


def test_the_replays_own_frame_survives_a_console_that_cannot_encode_the_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`_console_safe` guarded the child's lines and not the frame around them.

    The sibling above puts an unencodable character in the log's *contents*,
    which the replayed lines already routed through the helper. The path is the
    other half: `--- full output: <log> ---` and the read-error diagnostic both
    interpolate it, and a temp directory carries the account name, so a console
    that cannot encode it crashed the reporter on its own last line. The body
    here is deliberately ASCII, so only the frame can break this.

    Measured before the guard reached them: `_console_safe` was defined once
    and called at exactly one site, with four other prints interpolating a path
    or an exception. Raised by Copilot on PR #106.
    """
    console = _LegacyConsole()
    monkeypatch.setattr(run_gates.sys, "stdout", console)
    log = tmp_path / "→ wide.log"
    log.write_text("a line of plain ascii\n", encoding="utf-8")

    run_gates.replay_tail(log)

    assert "full output" in console.getvalue(), (
        "the frame naming the log is what the console could not encode"
    )


class _FullDisk(io.StringIO):
    """A sink that opens and then refuses its writes, the way a full volume does."""

    def write(self, s: str) -> int:
        raise OSError("No space left on device")


def test_a_capture_that_fails_after_the_sink_opened_is_a_gate_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The consequence is not the traceback; it is `cleanup` reading it as green.

    `main` sets `ctx.failed` only on the paths it recognises and `cleanup`
    sweeps `ephemeral` when `failed` is false -- so an `OSError` escaping the
    write loop deletes the canary worker sink, the evidence a failure report
    names. Converting here is what routes it through the handler that sets the
    flag. The open already converted and the write did not, which is the
    inconsistency that made this findable. Raised by CodeRabbit on PR #106.
    """
    target = tmp_path / "refused.log"
    real_open = Path.open

    def fake_open(self: Path, *args: Any, **kwargs: Any) -> Any:
        # Matched on the exact path, never on the suffix. `Path.open` is patched
        # process-wide for the duration, so a `self.suffix == ".log"` test also
        # catches pytest's own logging inside this xdist worker -- measured: it
        # crashed `gw8` and failed the run with `INTERNALERROR ... assert not
        # crashitem`, intermittently, depending on what else the worker did
        # while the patch was live. Found from the retained gate log, which is
        # the feature this branch adds.
        if self == target:
            return _FullDisk()
        # `cast` because `Path.open`'s overloads do not survive `*args: Any`,
        # and a bare passthrough is `Unknown` under --strict.
        return cast(Any, real_open(self, *args, **kwargs))

    monkeypatch.setattr(Path, "open", fake_open)
    step = run_gates.Step(
        [sys.executable, "-c", "print('a line the sink will refuse')"],
        capture_to=target,
    )

    with pytest.raises(run_gates.GateError, match="could not write the captured"):
        run_gates.run_step(step, echo=False)


def test_a_run_directory_that_cannot_be_created_is_a_gate_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The earliest of the three sites, and the last to be converted.

    A full, missing or unwritable temp directory made `mkdtemp` raise where
    `main` catches only `GateError`, so the runner died with a raw traceback
    before any gate ran. Raised by Copilot on PR #106.
    """

    def refuse(*args: Any, **kwargs: Any) -> str:
        raise OSError("No space left on device")

    monkeypatch.setattr(tempfile, "mkdtemp", refuse)
    ctx = run_gates.Context(pins={}, gitleaks_version=None)

    with pytest.raises(run_gates.GateError, match="could not create a run directory"):
        ctx.scratch_dir()


@pytest.mark.parametrize("locked", [False, True], ids=["missing", "unreadable"])
def test_the_replay_survives_a_log_it_cannot_read(
    locked: bool,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capfd: pytest.CaptureFixture[str],
) -> None:
    """Losing the replay must not turn a legible failure into a traceback.

    The step's exit code is already decided by the time this runs, so a read
    failure here can only make the failure *harder* to read — never change the
    verdict. Reachable in ordinary use: the log sits in a shared temp directory
    another process can sweep.

    **Two failures, because the guard catches `OSError` and a missing file is
    only one kind.** With the `missing` case alone, narrowing the guard to
    `except FileNotFoundError` left the whole suite green — measured. On Windows
    a concurrent sweep surfaces at least as often as a *locked* file, which is
    `PermissionError`, so the case the single test omitted is the likelier one.
    The parametrization is what makes the guard's breadth the thing under test
    rather than one instance of it.
    """
    log = tmp_path / "swept-away.log"
    if locked:
        # Forced rather than arranged: making a file genuinely unreadable is not
        # portable, and what is under test is the handler, not the filesystem.
        log.write_text("captured output", encoding="utf-8")

        def refuse(*_args: object, **_kwargs: object) -> object:
            raise PermissionError("used by another process")

        # Pointed at the call the replay actually makes. It read the whole file
        # through `read_text` until the tail was bounded with a `deque`, and a
        # forcing mechanism aimed at a call the code no longer makes is a test
        # that exercises nothing while still reporting green.
        monkeypatch.setattr(Path, "open", refuse)

    run_gates.replay_tail(log)

    out = capfd.readouterr().out
    assert "could not read" in out
    assert str(log) in out, "the message named no path to go looking at"


def test_list_and_print_neither_announce_nor_create_anything(
    temp_root: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    """The read-only branches run nothing, so they capture nothing.

    They return before the runner reaches a gate, which is why this is an
    assertion rather than an assumption — the notice and the directory are both
    reachable from `main` before that return if either is put in the wrong
    place.
    """
    assert run_gates.main(["--list"]) == 0
    assert run_gates.main(["--print", "pytest"]) == 0

    assert "Step output is captured to" not in capfd.readouterr().out
    assert list(temp_root.iterdir()) == []


@pytest.mark.unpatched_temp_root
def test_the_temp_root_resolves_to_the_system_temp_directory(temp_root: Path) -> None:
    """The one test that lets the real resolution run.

    Every other test in this module redirects `_temp_root`, which left its
    actual body — the line deciding where every run directory is created and
    pruned — exercised by nothing in the tree. Measured: pointing it at a
    subdirectory that does not exist left the whole suite green.
    """
    assert run_gates._temp_root() == Path(tempfile.gettempdir())  # pyright: ignore[reportPrivateUsage]
    assert temp_root == Path(tempfile.gettempdir()), (
        "the opt-out marker did not reach the fixture, so this test proved nothing"
    )


@pytest.mark.unpatched_temp_root
@pytest.mark.parametrize("inside", ["", "sub/deeper"])
def test_a_temp_root_inside_the_repository_is_refused(
    monkeypatch: pytest.MonkeyPatch, inside: str
) -> None:
    """The guarantee is enforced, not inherited from the machine's default.

    `tempfile.gettempdir()` honours `TMPDIR`/`TEMP`/`TMP`, so the module
    docstring's "outside the repository" was a property of this machine rather
    than of the code -- and a root inside the checkout turns every retained log
    into a repository file. Both the repository root itself and a path under it
    are refused: `is_relative_to` answers True for a path against itself, and a
    guard written with a bare `parents` check would let the exact root through.

    `gettempdir` is monkeypatched rather than the environment, because the
    stdlib caches its answer in `tempfile.tempdir` after the first call and an
    env-var test would pass or fail on whether something had already asked.
    """
    target = run_gates.REPO_ROOT / inside if inside else run_gates.REPO_ROOT
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(target))

    with pytest.raises(run_gates.GateError, match="inside the repository"):
        run_gates._temp_root()  # pyright: ignore[reportPrivateUsage]


@pytest.mark.unpatched_temp_root
def test_the_guard_sees_through_a_root_that_only_resolves_into_the_repository(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The `.resolve()` leg, which the two literal-path cases never reach.

    Measured in review: dropping `.resolve()` from the guard left all 220 tests
    green, because both other cases hand it an already-absolute, already-real
    path that the raw comparison catches by itself.

    **This pins one half of that leg and the test below pins the other**, which
    is worth stating because a first version claimed both. A `..` segment makes
    the raw path compare unequal to the repository while resolving into it, so
    this kills a guard that checks only the raw path -- but `os.path.abspath`
    collapses `..` too, so substituting it for `.resolve()` still passes here.
    Dereferencing a *link* is what only `.resolve()` does, and that is the next
    test's job.

    No `chdir`: an earlier version pointed `gettempdir` at `"."` and chdir'd to
    the repository root, which measured inert -- the suite already runs from
    there, and `Path(".").parts` is empty so the premise assertion was true
    unconditionally. Building the path from `REPO_ROOT` needs no ambient cwd.
    """
    root = run_gates.REPO_ROOT
    outside_looking = root.parent / "no-such-dir" / ".." / root.name
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(outside_looking))

    # The premise: raw, this is not under the repository -- so a guard checking
    # only the raw path would let it through, and the test would prove nothing.
    assert not outside_looking.is_relative_to(root)
    assert outside_looking.resolve() == root

    with pytest.raises(run_gates.GateError, match="inside the repository"):
        run_gates._temp_root()  # pyright: ignore[reportPrivateUsage]


@pytest.mark.unpatched_temp_root
def test_the_guard_dereferences_a_link_into_the_repository(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The half a `..` segment cannot reach: a filesystem-level indirection.

    Substituting `os.path.abspath` for `.resolve()` passes every other test in
    this group -- measured -- and silently reopens the hole the guard was built
    to close, because `abspath` normalises text and never touches the disk. A
    link is the only input that separates the two.

    Probed rather than gated on `sys.platform`, which is this repository's
    standing rule for a capability: creating a directory symlink needs a
    privilege Windows grants only in developer mode, and measured, this host
    refuses it. So this skips on one leg and runs on the other -- which is what
    the two-leg discipline is for, and is why it is worth having rather than
    narrowing the claim to what a single platform can prove.
    """
    link = tmp_path / "into-the-repository"
    try:
        os.symlink(run_gates.REPO_ROOT, link, target_is_directory=True)
    except OSError:
        # Only `OSError`: a host that refuses the privilege raises it, and one
        # without symlinks at all would not have `os.symlink` to call. A second
        # exception here would also meet the formatter's paren-stripping rule
        # that CLAUDE.md documents, for a branch that cannot be reached.
        pytest.skip("this host does not grant the privilege to create a symlink")

    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(link))

    assert not link.is_relative_to(run_gates.REPO_ROOT), (
        "the premise: raw, the link is outside the repository"
    )
    assert Path(os.path.abspath(link)) == link, (
        "the premise: abspath does not dereference, so only resolve can catch this"
    )

    with pytest.raises(run_gates.GateError, match="inside the repository"):
        run_gates._temp_root()  # pyright: ignore[reportPrivateUsage]


@pytest.mark.unpatched_temp_root
def test_a_temp_root_outside_the_repository_is_allowed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The other half: the guard refuses a location, not every location.

    Without this, deleting the `is_relative_to` condition and raising
    unconditionally would still satisfy the test above.
    """
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))

    assert run_gates._temp_root() == tmp_path  # pyright: ignore[reportPrivateUsage]


def test_the_dry_path_is_built_from_the_prefix_the_prune_globs_for(
    monkeypatch: pytest.MonkeyPatch, temp_root: Path
) -> None:
    """The two spellings agree today, which is exactly why nothing caught it.

    `SCRATCH_PREFIX` exists because a disagreement between what `scratch_dir`
    creates and what `prune_scratch_dirs` globs for would not fail — it would
    silently prune nothing forever. A literal dry path is a third writer of
    `run_gates-` paths that the constant cannot move, so the only oracle that
    can see the difference is one that changes the constant.
    """
    monkeypatch.setattr(run_gates, "SCRATCH_PREFIX", "moved-prefix-")
    ctx = run_gates.Context(pins={}, gitleaks_version=None, dry=True)

    assert ctx.scratch_dir().name.startswith("moved-prefix-"), (
        "the dry path did not follow the prefix the prune globs for"
    )


def test_a_run_whose_gates_all_decline_spooling_creates_no_directory(
    temp_root: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    """The notice is what creates the directory, so it has to be earned.

    `python3 scripts/run_gates.py containment` — the standalone command `/land`
    documents — selects the one gate that opts out of spooling. Announced
    unconditionally, the notice built an empty directory, told the reader output
    had been captured into it, and spent a retention slot evicting a real run to
    say so.
    """
    assert run_gates.main(["containment"]) == 0

    assert "Step output is captured to" not in capfd.readouterr().out
    assert list(temp_root.iterdir()) == [], (
        "a run that spools nothing still created a directory"
    )


def test_a_read_only_run_prunes_nothing(temp_root: Path) -> None:
    """`--list` and `--print` must not delete another run's captured output.

    The sibling above asserts the read-only branches leave an *empty* temp
    directory empty, which cannot tell "cleanup did nothing" from "cleanup
    pruned and found nothing to prune". Seeding it first is what separates
    them — and the second is a real outcome: `cleanup` reaches the prune
    unless its dry-mode guard stops it, and a read-only command evicting the
    logs of a failure someone is in the middle of reading is the worst version
    of that.
    """
    seeded = [
        completed(temp_root / f"{run_gates.SCRATCH_PREFIX}{index:02d}", stamp=index)
        for index in range(run_gates.RUNS_RETAINED + 2)
    ]

    assert run_gates.main(["--list"]) == 0
    assert run_gates.main(["--print", "pytest"]) == 0

    assert [path for path in seeded if not path.is_dir()] == [], (
        "a read-only command pruned another run's logs"
    )


def test_fast_selects_gates_without_changing_how_output_is_handled(
    temp_root: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    """`--fast` filters the selection before any of this, and still does.

    Run against a real gate rather than a probe: the interaction worth pinning
    is that a `--fast` run is an ordinary run, spooling included.
    """
    assert run_gates.main(["--fast", "adr-index"]) == 0

    assert "Step output is captured to" in capfd.readouterr().out
    assert [path.name for path in temp_root.rglob("adr-index-*.log")] == [
        "adr-index-01.log"
    ]
