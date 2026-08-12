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
from pathlib import Path

import pytest
import run_gates

REPO_ROOT = Path(__file__).resolve().parent.parent
CI_TEXT = run_gates.CI_WORKFLOW.read_text(encoding="utf-8")

LOCAL_GATES = [gate for gate in run_gates.GATES if gate.local]
CI_ONLY_GATES = [gate for gate in run_gates.GATES if not gate.local]
PINNED_GATES = [gate for gate in run_gates.GATES if gate.version_pin]


def _context(tmp_path: Path | None = None) -> run_gates.Context:
    """A context over the real workflow, with scratch pinned into tmp_path.

    Presetting ``scratch`` keeps the test gate from calling ``mkdtemp`` and
    leaving directories behind; it is the only state a build touches.
    """
    return run_gates.Context(
        pins=run_gates.read_pins(CI_TEXT),
        gitleaks_version=run_gates.read_gitleaks_version(CI_TEXT),
        scratch=tmp_path,
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


def test_the_runner_stops_at_the_first_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Fail-fast: a lint failure must not cost a full test run first.

    Ordering is the whole reason the registry is a sequence, and a regression
    here is silent — everything still passes, just slower and later.
    """
    attempted: list[str] = []

    def fake_run_step(step: run_gates.Step) -> int:
        attempted.append(step.shown())
        return 1

    monkeypatch.setattr(run_gates, "run_step", fake_run_step)
    first = LOCAL_GATES[0]
    assert run_gates.run_gate(first, _context(tmp_path)) is False
    assert len(attempted) == 1


def test_fast_skips_the_slow_gates(monkeypatch: pytest.MonkeyPatch) -> None:
    """--fast is defined by which gates it drops, so name them explicitly."""
    ran: list[str] = []

    def fake_run_gate(gate: run_gates.Gate, _: run_gates.Context) -> bool:
        ran.append(gate.name)
        return True

    monkeypatch.setattr(run_gates, "run_gate", fake_run_gate)
    assert run_gates.main(["--fast"]) == 0
    assert "pytest" not in ran
    assert "pip-audit" not in ran
    assert "ruff-check" in ran


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
