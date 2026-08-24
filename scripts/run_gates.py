#!/usr/bin/env python3
"""Run this repository's CI gates locally, in CI's own form.

Every gate command and every pinned tool version already exists in exactly one
place: ``.github/workflows/ci.yml``. Before this script that single copy was
unreachable at the moment of need, so sessions reconstructed the commands from
memory and got them wrong in three measured ways:

* ``uv run ruff`` — reached for unprompted, and it cannot work. ruff is not a
  dependency of this project; CI fetches it on demand with ``uvx`` at a pin.
  Fails in 0.4s, loudly. Same for ``uv run pymarkdown``.
* ``pytest`` run serially — the suite is isolated for worker parallelism and
  takes 112s under ``-n auto``; the serial form ran past a 600s cap and was
  killed. The right form was already written down and was not read.
* running three of the thirteen gates and reporting green — silent, and the
  worst of the three, because nothing distinguishes it from a real pass.

The fix is not to restate the commands somewhere more memorable. A second copy
of the pins is a thing this repository has measured drifting more than once
(``scripts/check_markdownlint_config_sync.py`` exists solely because two config
copies drift), and the drift is already here: ``/land`` and ``/ship`` disagreed
about the same gate commands, with ``/land``'s version being the one that
cannot run. So: **derive, do not restate.** The pins below are read out of
``ci.yml`` at every invocation, and there is no second copy to keep in sync.

**Discoverability is the point, not a convenience.** ``--list`` is what answers
"what gates exist and how do I run one", which is the question that produced
the failures above. Because it is derived, it cannot go stale the way a
hand-written list would.

**CI-only gates are listed, not hidden.** A gate this machine cannot run is
reported as skipped and named in the summary. Omitting them would let a clean
local run read as full CI green when it never was -- the silent-partial failure
above, reintroduced by the tool meant to fix it.

**Nothing here is authoritative; CI is.** Two of the six CI jobs cannot be
reproduced on one machine at all (the macOS leg, and gitleaks' hash-verified
binary). This script exists so a session does not burn 600 seconds and does not
mistake a partial run for a full one -- not to replace the gate.

Deliberate divergences from CI, each with a reason:

* **pytest runs ``-n auto``** here, always. CI's ubuntu/macOS legs run serially
  so a single tee'd stream feeds the log-canary scan; its windows-latest leg
  runs parallel (ADR-0063). Locally the parallel leg is the one worth
  reproducing -- it is 112s against >600s -- and this script reproduces it
  *including* the canary scan, through the same ``CANARY_CAPTURE_DIR`` variable
  CI uses -- but not the same value: CI sets the bare ``canary-logs``, safe
  because it writes into a workspace it discards, while this sets a
  scratch-rooted directory of that name so a local run does not drop
  ``canary-logs/`` into the repository. The *name* is read from ci.yml's own
  ``CANARY_CAPTURE_DIR`` rather than restated here, so the sink and the scan
  cannot disagree about it and a rename in ci.yml carries across.
  A local run that skipped the canary scan would be one of the partial greens
  described above.
* **containment runs ``--scope branch``**, where CI runs ``--scope history``.
  This one narrows the question rather than buying a side effect, and it is the
  only entry here that does. The local question is "is what I am about to commit
  clean"; the history walk is CI's backstop over every pushed branch, and
  ``ci.yml`` says so in that step's own comment -- "the backstop, not the
  control ... it detects rather than prevents; prevention is the same script run
  pre-push". Do not justify it by depth: a developer's clone is full-depth
  already and CI is the side that has to ask (``fetch-depth: 0``). ADR-0075 §1
  admits this as a *declared* narrowing, which is why ``--list`` renders the
  gate as "(branch scope)" rather than leaving the reduction implicit. The two
  are the same gate, which is why the registry maps this entry onto CI's step
  and the drift test compares gate *identity* rather than argv.
* **the lockfile gate runs ``uv lock --check``**, where CI's step runs
  ``uv sync --locked``. Both fail on a lockfile that disagrees with
  ``pyproject.toml`` — the property the gate is for — but CI is installing into
  a runner it then discards, while locally the ``sync`` form would mutate the
  developer's environment as a side effect of asking a question.

The first two of those were decided before this script existed and are only
*applied* here: ``-n auto`` locally is the pre-existing norm ADR-0063 records,
and ``--scope branch`` is the scope ADR-0070 §2 assigns to ``/land`` by name.
The third is this script's own, which is why it is stated here.

Known caveat: this parses ``ci.yml``. Bumping a pin is free -- the new value is
picked up -- but changing the *spelling* of a pin (``RUFF_VERSION: "1.2.3"`` to
some other form) breaks the parse. That is accepted, and it breaks loudly: a
missing pin is an error naming the variable, never a silent fallback to a stale
default. A gate that fails on a cosmetic edit costs one commit; a gate that
passes against a stale pin is the defect being prevented.

Exit 0 when every selected gate passes; 1 on the first failure, having echoed
the exact command that failed. Stdlib only; files are read as UTF-8.
"""

from __future__ import annotations

import argparse
import io
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field, replace
from enum import Enum
from pathlib import Path, PurePath

REPO_ROOT = Path(__file__).resolve().parent.parent
CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"

# `NAME: "value"` where NAME ends in _VERSION. Anchored to a line start so a
# version mentioned in a comment or a URL cannot be mistaken for a pin.
_PIN_RE = re.compile(r'^\s*([A-Z][A-Z0-9_]*_VERSION):\s*"([^"]+)"', re.MULTILINE)

# gitleaks is pinned in its install step's name and download URL rather than the
# env block, so it is derived from there for the same reason as everything else:
# restating "8.30.1" here would be the second copy this script exists to avoid.
_GITLEAKS_RE = re.compile(r"^\s*- name: Install gitleaks (\S+)", re.MULTILINE)

# Windows caps a command line near 32k characters. The markdown gate passes one
# path per file, so it is the only gate that can approach that; CI sidesteps it
# with `xargs`, which batches. Batching by assembled length rather than file
# count keeps the guard correct as paths get longer.
_MAX_COMMAND_CHARS = 28_000

# Resolved once, matching scripts/check_personal_containment.py. The `or` fallback
# keeps a missing tool failing where the command runs — as a gate that could not
# start, naming the command — rather than at import time, which would take out
# `--list` too and hide the other twelve gates behind one absent binary.
_GIT = shutil.which("git") or "git"
_UV = shutil.which("uv") or "uv"
_UVX = shutil.which("uvx") or "uvx"

# A hung git fails the runner rather than hanging it. The gate *steps* below are
# deliberately unbounded -- a full pytest run legitimately takes minutes -- but
# this bound covers the git calls the runner makes to *build* those steps, which
# produce no output at all while they wait. `tests/test_git_runners.py` holds the
# invariant across every script, so no comment here has to enumerate the family.
_GIT_TIMEOUT = 30


def _uv_run(*args: str) -> list[str]:
    """A `uv run` argv, with `--locked` structural rather than remembered.

    Every `uv run` in this file goes through here, which is the point. The flag
    was added to the docs-gate builder alone and the two that build their own
    `[_UV, "run", "--with", ...]` -- pyright and pytest -- were left without it,
    because there was nothing to add it to; the one assertion covering it pinned
    the spec-links argv, so nothing objected. Without `--locked`, `uv run`
    re-locks whenever `pyproject.toml` has moved ahead of `uv.lock` and
    **rewrites that tracked file**, from a step that reads as read-only.

    Gate *ordering* bounds a full run -- the lockfile gate is ahead of both --
    but a selector is not ordered: `run_gates.py pyright`, `run_gates.py pytest`
    and `run_gates.py test` each run their own step with no lockfile gate in
    front of it, so `uv lock --check` afterwards certifies a file the typecheck
    gate just repaired. `test_every_uv_run_step_is_locked` holds the invariant
    for whatever gate is added next, which is the half prose could not do.
    """
    return [_UV, "run", "--locked", *args]


# The note every `uv run` step carries, for the same reason `--locked` is
# structural rather than remembered: the exposure belongs to the *mechanism*.
#
# It was written into `_uv_python_step` alone, in the same change that
# generalized `--locked` to every `uv run` — so the two builders that construct
# their own `uv run --with ...` argv were newly put at risk and told nothing.
# `run_gates.py pyright`, `run_gates.py pytest` and `run_gates.py test` each run
# their step with no lockfile gate ahead of them, so on a branch that edits
# `pyproject.toml` without re-locking — exactly what adding `markdown-it-py`
# did — the step now aborts inside `uv run --locked` before pytest or pyright
# starts, printing a bare `FAILED: pytest (exit N)` over uv's resolver output.
#
# And its wording named "a dead link" from inside a generic builder, which is
# only true of the docs gates. `_uv_step` below is what attaches it, so a gate
# added through `_uv_run` inherits the note the way it inherits the flag.
_UV_RUN_NOTE = (
    "`uv run --locked` syncs the project environment before running anything, "
    "so this can fail on a stale `uv.lock` or a failed install rather than on "
    "whatever this gate is named for. If the output above is uv's rather than "
    "the command's, run `run_gates.py lockfile`."
)


def _uv_step(
    argv: list[str],
    display: str,
    *,
    env: dict[str, str] | None = None,
    capture_to: Path | None = None,
) -> Step:
    """A `Step` for a `uv run` argv, carrying the shared misattribution note.

    One factory rather than three hand-set `on_failure=` fields, on the
    argument `_uv_run`'s own docstring makes about `--locked`: past two sites,
    the thing that must not be forgotten belongs in the mechanism rather than
    in the memory of whoever adds the next gate.

    The keyword arguments are spelled out rather than forwarded as `**kwargs`
    so that a `Step` field renamed underneath this stays a type error instead
    of a silently dropped argument -- `capture_to` going missing would turn the
    canary scan's input into an empty glob, which the scanner fails closed on
    but only after the test step has already run.
    """
    return Step(
        argv,
        display,
        env=env or {},
        capture_to=capture_to,
        on_failure=_UV_RUN_NOTE,
    )


class GateError(RuntimeError):
    """A gate could not be built or run — distinct from a gate that failed."""


class GateResult(Enum):
    """Three outcomes, because "ran and passed" and "had nothing to run" are
    different claims and only one of them is evidence."""

    PASSED = "passed"
    FAILED = "failed"
    EMPTY = "empty"


def read_pins(text: str) -> dict[str, str]:
    """Extract ``*_VERSION`` pins from ``ci.yml`` text.

    Raises on a variable defined twice with conflicting values: silently taking
    one of two answers is exactly the stale-pin failure this file prevents.
    """
    pins: dict[str, str] = {}
    for name, value in _PIN_RE.findall(text):
        if name in pins and pins[name] != value:
            raise GateError(
                f"{name} is defined twice in {CI_WORKFLOW.name} with different "
                f"values ({pins[name]!r} and {value!r}); no way to tell which "
                "one CI uses"
            )
        pins[name] = value
    return pins


def read_gitleaks_version(text: str) -> str | None:
    match = _GITLEAKS_RE.search(text)
    return match.group(1) if match else None


def read_workflow_env(text: str, name: str) -> str | None:
    """Read a plain workflow-level ``env:`` value, quoted or bare.

    Separate from ``read_pins`` because that one is deliberately restricted to
    ``*_VERSION`` names, and the values here are not versions. Returning None
    when the key is absent is the point rather than a fallback: the runner then
    stops setting what CI stopped setting, which is the whole contract. The
    expiry is already dated — ci.yml's own comment says PEP 686 lands in 3.15,
    at which point PYTHONUTF8 goes away there and must go away here too.
    """
    pattern = re.compile(rf'^\s*{re.escape(name)}:\s*"?([^"\s#]+)"?', re.MULTILINE)
    values = pattern.findall(text)
    if not values:
        return None
    # The same refusal `read_pins` makes, and for the same reason: taking the
    # first of two disagreeing answers is the silent-wrong-value failure this
    # module exists to prevent. One parser raising while its sibling guessed was
    # an asymmetry with no argument behind it.
    if len(set(values)) > 1:
        raise GateError(
            f"{name} is set more than once in {CI_WORKFLOW.name} with different "
            f"values ({sorted(set(values))!r}); no way to tell which CI uses"
        )
    return values[0]


def require_dir_name(key: str, value: str) -> str:
    """Return ``value`` if it is a single relative directory name, else raise.

    Deriving a value from ci.yml instead of hardcoding it is the right trade,
    but it turns a controlled constant into external input, and the caller joins
    it onto the scratch directory. ``Path`` makes that join unsafe two ways,
    both measured: an absolute right-hand side *replaces* the base outright
    (``scratch / "C:/outside"`` is ``C:\\outside``), and ``..`` walks out of it.
    Either lands the captured worker logs where ``Context.cleanup`` cannot reach
    them — the leak this module already closed once, re-entered through the
    derivation.

    A separator alone does not escape (``a/b`` stays inside) and is rejected
    anyway: the contract is a *name*, and ``scan_log_canary.py``'s glob assumes
    one directory level. Rejecting the whole class is simpler to state than the
    subset that escapes, and cannot be wrong in the unsafe direction.

    Not a security boundary — ci.yml is tracked and reviewed, and anyone able to
    edit it can already run arbitrary commands in CI. What this catches is an
    ordinary edit, such as a value carrying a path separator, that would move
    the capture silently rather than loudly.
    """
    candidate = PurePath(value)
    if (
        not value
        or candidate.is_absolute()
        or len(candidate.parts) != 1
        or value in {os.curdir, os.pardir}
        or any(sep in value for sep in ("/", "\\"))
    ):
        raise GateError(
            f"{key} in {CI_WORKFLOW.name} is {value!r}, which is not a single "
            "relative directory name. The runner joins it onto its scratch "
            "directory, so an absolute path, a `..`, or a separator would put "
            "captured test output where cleanup cannot remove it"
        )
    return value


def require(pins: dict[str, str], name: str) -> str:
    try:
        return pins[name]
    except KeyError:
        raise GateError(
            f"{name} is not pinned in {CI_WORKFLOW.name}. Either the pin was "
            "renamed or the env block changed shape — this script derives every "
            "version from that file and will not guess a default"
        ) from None


@dataclass(frozen=True)
class Step:
    """One command inside a gate. ``display`` is what --print/--list shows."""

    argv: Sequence[str]
    display: str = ""
    # `dict[str, str]` rather than `dict` as the factory: under pyright --strict
    # the bare builtin resolves to dict[Unknown, Unknown].
    env: dict[str, str] = field(default_factory=dict[str, str])
    # When set, the step's combined output is streamed to the console *and*
    # written here, reproducing CI's `| tee`. The canary scan reads it back.
    capture_to: Path | None = None
    # Recomputes argv immediately before the step runs, for a command whose
    # arguments depend on files an earlier step created. Carried on the step
    # rather than keyed on a gate name and position in `run_gate`: that coupling
    # silently rewrote the wrong step — dropping its env and capture_to — the
    # moment anything was inserted into or reordered within a gate.
    rebuild: Callable[[], Sequence[str]] | None = None
    # Runs even when an earlier step in the same gate has failed, reproducing
    # CI's `if: ${{ !cancelled() && ... }}` on the canary steps. ci.yml states
    # the reason where it sets it: "Must run on test FAILURE too — a failing
    # test's traceback is the likeliest place for a leaked value."
    always_run: bool = False
    # Printed under the command when this step fails. For a step whose failure
    # can have a cause *outside* what its gate is named for, naming that cause
    # at the moment of failure is what stops the author hunting the wrong
    # thing. `_lockfile`'s own comment records the same class from the other
    # direction -- an old uv "naming the lockfile for a toolchain problem" --
    # and answered it with `tool_pin`, which only helps someone reading
    # `--list` before the failure rather than the failure itself.
    on_failure: str = ""

    def shown(self) -> str:
        return self.display or " ".join(self.argv)


# A gate's steps are built lazily: the markdown gate asks git for its file list
# and the test gate needs a scratch directory, neither of which should happen at
# import time.
Builder = Callable[["Context"], list[Step]]


@dataclass
class Context:
    pins: dict[str, str]
    gitleaks_version: str | None
    # The worker-sink directory name, read from ci.yml's own CANARY_CAPTURE_DIR.
    # Held here rather than exported through `workflow_env`: CI's value is the
    # bare `canary-logs`, correct in a workspace it discards and wrong locally,
    # so the runner takes the *name* and roots it in its own scratch directory.
    canary_dir_name: str | None = None
    # Workflow-level env CI exports to every step, derived rather than restated.
    # `None` when ci.yml no longer sets it, which is how the runner stops
    # forcing what CI stopped forcing instead of silently diverging.
    workflow_env: dict[str, str] = field(default_factory=dict[str, str])
    # Where a gate may write working files. Created on first use and removed by
    # `cleanup`. `--list` and `--print` build every gate's steps in order to show
    # them, so they reach `scratch_dir` too; they pass `dry=True`, which hands
    # back a path that is never created. An earlier version documented those two
    # as not reaching here at all, which was measured false — one `--list` left a
    # `mkdtemp` directory behind, and nothing ever removed any of them.
    scratch: Path | None = None
    dry: bool = False

    def scratch_dir(self) -> Path:
        if self.dry:
            # A placeholder that is never created and, as it happens, never
            # printed either — every step under --list/--print renders its
            # `display` string, and none of those contains a scratch path. It
            # exists only so a builder joining onto it gets a *legal* path:
            # angle brackets in an earlier version raised WinError 123 on
            # Windows when `_pytest` joined the canary directory name onto it.
            # Nothing is
            # opened here, so this never touches the shared temp directory.
            return Path(tempfile.gettempdir()) / "run_gates-scratch"
        if self.scratch is None:
            self.scratch = Path(tempfile.mkdtemp(prefix="run_gates-"))
        return self.scratch

    def cleanup(self) -> None:
        """Remove the scratch directory, if one was created.

        It holds captured test output — exactly the material the canary gate
        exists to police — outside the repository, where the containment gate
        does not look. Leaving it is both an unbounded leak and a containment
        surface, so removal is unconditional rather than best-effort on success.
        """
        if self.scratch is not None:
            shutil.rmtree(self.scratch, ignore_errors=True)
            self.scratch = None


@dataclass(frozen=True)
class Gate:
    name: str
    job: str  # the ci.yml job that owns it — also its group name
    summary: str
    # ci.yml step names this gate reproduces. The drift test asserts every
    # named `run:` step in ci.yml is claimed here or listed as setup.
    ci_steps: tuple[str, ...]
    build: Builder | None = None  # None => cannot run locally
    ci_only_reason: str = ""
    # The pin that appears *in this gate's command*, e.g. `uvx "ruff@0.15.21"`.
    version_pin: str | None = None
    # The pin of a tool the gate *needs* but does not name — `uv` is resolved
    # from PATH, so nothing embeds its version. Kept separate from version_pin
    # because the derivation test asserts a version_pin shows up in the rendered
    # command, and this class of pin by definition does not.
    tool_pin: str | None = None

    @property
    def local(self) -> bool:
        return self.build is not None

    def steps(self, ctx: Context) -> list[Step]:
        """Build this gate's commands, refusing rather than silently doing nothing.

        A CI-only gate reaching here is a caller bug, not a user error: every
        path that runs or prints commands filters on ``local`` first. Raising
        keeps that invariant checkable instead of returning an empty list, which
        would render as a gate that passed without executing anything.
        """
        if self.build is None:
            raise GateError(f"{self.name} cannot run locally: {self.ci_only_reason}")
        return self.build(ctx)


# --------------------------------------------------------------------------
# Builders
# --------------------------------------------------------------------------


def _python(script: str, *args: str) -> list[str]:
    # CI invokes these as `python3`; sys.executable is the same interpreter that
    # is already running and avoids a PATH lookup that differs on Windows.
    return [sys.executable, str(REPO_ROOT / "scripts" / script), *args]


def _python_display(script: str, *args: str) -> str:
    # What is shown is what a human can paste, so it keeps CI's spelling rather
    # than the absolute interpreter path this process actually executes.
    return " ".join(["python3", f"scripts/{script}", *args])


def _python_step(script: str, *args: str) -> Step:
    return Step(_python(script, *args), _python_display(script, *args))


def _docs_gate(script: str, *args: str) -> Builder:
    def build(_: Context) -> list[Step]:
        return [_python_step(script, *args)]

    return build


def _uv_python_step(script: str, *args: str) -> Step:
    """A docs gate that needs the project's dependencies, so `uv run` supplies
    them.

    The sibling gates run under `sys.executable` -- this process's own
    interpreter, which is not the project environment either unless the runner
    itself was launched from it: measured, `import markdown_it` raises
    `ModuleNotFoundError` there on this machine. (What they *display* is CI's
    `python3` spelling, which is what the sentence here used to describe;
    `_python` above says why the two differ.) `uv run` is the same spelling CI
    uses for this step, and the docs-consistency job already installs uv for
    it -- so this is one line in each place rather than a new CI step.

    **`--locked`, and it is not optional.** Without it `uv run` re-locks and
    re-syncs whenever `pyproject.toml` has moved ahead of `uv.lock` -- rewriting
    a *tracked* file, from a gate that reads as read-only. The ordering makes it
    worse than untidy: this gate runs *before* the `lockfile` gate, so on a
    branch that edits `pyproject.toml` without re-locking -- which is exactly
    what adding `markdown-it-py` did -- the docs gate repairs `uv.lock` and
    `uv lock --check` then certifies the file it just repaired. Local green, CI
    red on a clean checkout: the inversion this runner exists to prevent.
    `--locked` turns that into a refusal naming the lockfile.

    The *relation* is the load-bearing fact and the ordinal is not, so only the
    relation is stated -- and it is pinned, by
    `test_the_docs_gate_runs_before_the_lockfile_gate`, rather than left to
    prose. This sentence read "runs sixth" until a reviewer measured it;
    the number was invented rather than derived, the report that raised the
    finding having handed over the real positions, and it was then copied
    verbatim into ADR-0061. Deliberately no positions are quoted here now: any
    ordinal into a registry that gains entries is wrong the moment one is
    inserted above it, which is the same failure one layer down.
    """
    # `uv run` syncs the project environment before it runs anything, so this
    # step can fail for a reason that has nothing to do with what its gate is
    # named for -- a stale lockfile, a wheel that will not build, an
    # interpreter uv has to fetch. Unqualified, the runner prints
    # `FAILED: spec-links` and sends the author hunting a dead link. The
    # `lockfile` gate is the one that names the commonest of those causes and
    # it runs *after* this one, so it cannot get there first. The note itself
    # lives on `_uv_step`, because the exposure is `uv run`'s and not this
    # builder's -- see `_UV_RUN_NOTE`.
    return _uv_step(
        _uv_run("python", str(REPO_ROOT / "scripts" / script), *args),
        " ".join(["uv", "run", "--locked", "python", f"scripts/{script}", *args]),
    )


def _uv_docs_gate(script: str, *args: str) -> Builder:
    def build(_: Context) -> list[Step]:
        return [_uv_python_step(script, *args)]

    return build


def _tracked_markdown() -> list[str]:
    """CI's own file set: tracked plus untracked-but-not-ignored ``*.md``.

    Bounded and guarded, because this one runs at gate-*build* time rather than
    inside a step: `_markdown_lint` (`job="docs"`) calls it while assembling the
    docs gate, so an unbounded hang here stops `python3 scripts/run_gates.py
    docs` -- and therefore a landing -- inside the builder with nothing printed
    at all. It was the runner two sibling comments' hand-written enumerations
    both missed while claiming the family was closed; the enumeration is now a
    test rather than prose.
    """
    try:
        result = subprocess.run(  # noqa: S603 - fixed executable, no shell
            [
                _GIT,
                "ls-files",
                "-z",
                "--cached",
                "--others",
                "--exclude-standard",
                "--",
                "*.md",
                ":(exclude)specs/personal/**",
            ],
            cwd=REPO_ROOT,
            capture_output=True,
            check=False,
            timeout=_GIT_TIMEOUT,
        )
    except OSError as exc:  # git missing, or not executable
        raise GateError(
            f"could not run `git ls-files` while building the markdown gate's "
            f"file list: {exc}"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise GateError(
            "`git ls-files` did not return within "
            f"{_GIT_TIMEOUT}s while building the markdown gate's file list"
        ) from exc
    if result.returncode != 0:
        raise GateError(
            "git ls-files failed while building the markdown gate's file list: "
            + result.stderr.decode("utf-8", "replace").strip()
        )
    # Null-delimited so a path containing spaces stays one entry.
    return [p for p in result.stdout.decode("utf-8").split("\0") if p]


def _markdown_lint(ctx: Context) -> list[Step]:
    version = require(ctx.pins, "PYMARKDOWN_VERSION")
    base = [_UVX, f"pymarkdownlnt@{version}", "--config", "pyproject.toml", "scan"]
    shown = f'uvx "pymarkdownlnt@{version}" --config pyproject.toml scan'
    files = _tracked_markdown()
    if not files:
        # CI's `xargs -r` makes an empty list a no-op rather than a zero-path
        # run, which pymarkdown would read as "scan everything".
        return []

    steps: list[Step] = []
    batch: list[str] = []
    budget = sum(len(part) + 1 for part in base)
    for path in files:
        if batch and budget + len(path) + 1 > _MAX_COMMAND_CHARS:
            steps.append(Step([*base, *batch], f"{shown} <{len(batch)} files>"))
            batch, budget = [], sum(len(part) + 1 for part in base)
        batch.append(path)
        budget += len(path) + 1
    steps.append(Step([*base, *batch], f"{shown} <{len(batch)} files>"))
    return steps


def _ruff(subcommand: Sequence[str]) -> Builder:
    def build(ctx: Context) -> list[Step]:
        version = require(ctx.pins, "RUFF_VERSION")
        return [
            Step(
                [_UVX, f"ruff@{version}", *subcommand, "."],
                f'uvx "ruff@{version}" {" ".join(subcommand)} .',
            )
        ]

    return build


def _pyright(ctx: Context) -> list[Step]:
    pyright = require(ctx.pins, "PYRIGHT_VERSION")
    pytest = require(ctx.pins, "PYTEST_VERSION")
    # The --with pytest is not redundant: it is how CI resolves the test files'
    # imports, so dropping it typechecks differently than CI does.
    return [
        _uv_step(
            _uv_run(
                "--with",
                f"pyright=={pyright}",
                "--with",
                f"pytest=={pytest}",
                "pyright",
            ),
            f'uv run --locked --with "pyright=={pyright}" '
            f'--with "pytest=={pytest}" pyright',
        )
    ]


def _pytest(ctx: Context) -> list[Step]:
    pytest = require(ctx.pins, "PYTEST_VERSION")
    xdist = require(ctx.pins, "PYTEST_XDIST_VERSION")
    scratch = ctx.scratch_dir()
    log = scratch / "pytest-output.log"
    if ctx.canary_dir_name is None:
        raise GateError(
            "ci.yml no longer sets CANARY_CAPTURE_DIR, so the worker-sink "
            "directory name cannot be derived. Restating it here would be the "
            "second copy this module exists to delete"
        )
    canary_dir = scratch / require_dir_name("CANARY_CAPTURE_DIR", ctx.canary_dir_name)
    # CI sweeps a stale canary dir before the run; a fresh scratch directory per
    # invocation makes that impossible here, so there is nothing to sweep.
    # No mkdir here. conftest.py's capture sink does
    # `path.parent.mkdir(parents=True, exist_ok=True)` before its first write,
    # so the directory appears exactly when something writes to it — and a
    # missing directory and an empty one glob identically, both falling back to
    # the literal `*.log` the scanner fails closed on. Creating it eagerly
    # changed nothing observable and was the sole reason `dry` had to be
    # threaded into a builder at all; `dry` is now a concern of `scratch_dir`
    # alone.
    return [
        _uv_step(
            _uv_run(
                "--with",
                f"pytest=={pytest}",
                "--with",
                f"pytest-xdist=={xdist}",
                "pytest",
                "-v",
                "-n",
                "auto",
                "--log-level=DEBUG",
            ),
            f'uv run --locked --with "pytest=={pytest}" '
            f'--with "pytest-xdist=={xdist}" '
            "pytest -v -n auto --log-level=DEBUG",
            env={"CANARY_CAPTURE_DIR": str(canary_dir)},
            capture_to=log,
        ),
        Step(
            _canary_argv(log, canary_dir),
            _python_display("scan_log_canary.py", "<pytest log>", "<worker logs>"),
            # The worker sink creates its files lazily, so the argument list is
            # only knowable after the test step has run.
            rebuild=lambda: _canary_argv(log, canary_dir),
            # CI runs this on test failure too, and says why: a failing test's
            # traceback is the likeliest place for a leaked value.
            always_run=True,
        ),
    ]


def _canary_argv(log: Path, canary_dir: Path) -> list[str]:
    """Build the canary scan's arguments the way a shell would expand CI's glob.

    CI passes the literal ``canary-logs/*.log``. With matches, the shell expands
    it to real paths; with none it passes the pattern through unexpanded, and
    ``scan_log_canary.py`` turns that unreadable path into exit 2. That is the
    dead-sink case, and the scanner's own comment calls the behaviour
    load-bearing for the parallel leg's fail-closed property (ADR-0063).

    Expanding to an empty list instead would hand the scanner only the
    controller stream, which it would scan happily and exit 0 on — turning a
    broken capture into a green gate, the exact softening that comment forbids.
    """
    matches = sorted(str(path) for path in canary_dir.glob("*.log"))
    return _python(
        "scan_log_canary.py", str(log), *(matches or [str(canary_dir / "*.log")])
    )


def _lockfile(_: Context) -> list[Step]:
    # `--check` rather than CI's bare `uv sync --locked`: both fail on a lockfile
    # that disagrees with pyproject.toml, but CI's form is installing a project
    # into a runner it then throws away, while locally it would mutate the
    # developer's environment as a side effect of asking a question.
    #
    # This is the one gate whose *flag surface* depends on uv's own version, and
    # uv is resolved from PATH rather than pinned — nothing here can pin the
    # tool that runs the pins. An older uv answers `--check` with
    # `unexpected argument`, which surfaces as `FAILED: lockfile`, naming the
    # lockfile for a toolchain problem. Carrying UV_VERSION as this gate's
    # version_pin is what puts the real dependency in `--list` beside it.
    return [Step([_UV, "lock", "--check"], "uv lock --check")]


def _pip_audit(ctx: Context) -> list[Step]:
    version = require(ctx.pins, "PIP_AUDIT_VERSION")
    # requirements-audit.txt is gitignored, so writing it at the repo root
    # matches CI without dirtying the tree. Relative because every command runs
    # with cwd=REPO_ROOT, and a relative path is what a reader can paste.
    export = "requirements-audit.txt"
    return [
        Step(
            [
                _UV,
                "export",
                "--locked",
                "--no-emit-project",
                "--format",
                "requirements-txt",
                "-o",
                export,
            ],
            # Without this the resolved absolute path of `uv` is what gets
            # shown, which on this platform embeds the operator's username —
            # a paste-unfriendly command and an identifying string in output
            # that gets copied into PRs and handoffs.
            "uv export --locked --no-emit-project --format requirements-txt "
            f"-o {export}",
        ),
        Step(
            [_UVX, f"pip-audit@{version}", "-r", export],
            f'uvx "pip-audit@{version}" -r {export}',
        ),
    ]


# --------------------------------------------------------------------------
# The registry — the single structure the selector, --list, and the drift test
# all read. Listed in execution order: cheap gates first, so a lint typo does
# not cost 112 seconds of pytest before it surfaces.
# --------------------------------------------------------------------------

GATES: tuple[Gate, ...] = (
    Gate(
        name="adr-index",
        job="docs",
        summary="ADR index matches the files and their Status fields",
        ci_steps=("Check ADR index against files and their Status fields",),
        build=_docs_gate("check_adr_index.py"),
    ),
    Gate(
        name="spec-links",
        job="docs",
        summary="every relative markdown link in the repo resolves",
        ci_steps=(
            "Check every relative markdown link in the repo resolves (ADR-0061)",
        ),
        build=_uv_docs_gate("check_spec_links.py"),
        # The only docs gate that runs `uv run` against the *project*
        # environment, so uv's own version is a real dependency of its verdict
        # -- the same reason the lockfile gate carries it, and `--list` should
        # say so beside both.
        #
        # "The only docs gate that runs through uv" is what this said, and it
        # was false: `markdown-lint` is `job="docs"` too and runs
        # `uvx pymarkdownlnt@<pin>`. It needs no `tool_pin` because uvx
        # resolves an isolated ephemeral tool at a version the spec names, so
        # uv is a launcher there rather than part of the answer. Here it
        # resolves and syncs the project, which is what makes its version
        # load-bearing.
        tool_pin="UV_VERSION",
    ),
    Gate(
        name="markdownlint-config-sync",
        job="docs",
        summary="the two markdown-lint configs stay in sync",
        ci_steps=("Check the two markdown-lint configs stay in sync (ADR-0062)",),
        build=_docs_gate("check_markdownlint_config_sync.py"),
    ),
    Gate(
        name="reviewer-agents",
        job="docs",
        summary="reviewer agent files hold their required invariants",
        ci_steps=(
            "Check the reviewer agent files hold their required invariants "
            "(ADR-0068, ADR-0076)",
        ),
        build=_docs_gate("check_reviewer_agents.py"),
    ),
    Gate(
        name="doc-citations",
        job="docs",
        summary="citing callers still point at the documents owning their rules",
        ci_steps=(
            "Check the citing callers still point at the documents that own "
            "their rules (ADR-0073)",
        ),
        build=_docs_gate("check_doc_citations.py"),
    ),
    Gate(
        name="ledger-collapsed",
        job="docs",
        summary="main carries no uncollapsed review-ledger fragment",
        ci_steps=(
            "Check main carries no uncollapsed review-ledger fragment (ADR-0072)",
        ),
        build=None,
        ci_only_reason=(
            "it asserts a property of `main` alone (ADR-0072 §8); on a branch, "
            "uncollapsed fragments are the expected state, so a local run would "
            "fail every branch that has been reviewed"
        ),
    ),
    Gate(
        name="containment",
        job="secrets",
        summary="no personal-data path escapes specs/personal/ (branch scope)",
        ci_steps=("Scan full git history for personal-data paths",),
        build=_docs_gate("check_personal_containment.py", "--scope", "branch"),
    ),
    Gate(
        name="lockfile",
        job="typecheck",
        summary="uv.lock agrees with pyproject.toml (needs uv >= the pin)",
        ci_steps=("Install project (locked)",),
        build=_lockfile,
        tool_pin="UV_VERSION",
    ),
    Gate(
        name="ruff-check",
        job="lint",
        summary="ruff lint (ruleset in pyproject.toml)",
        ci_steps=("ruff check (ruleset in pyproject.toml [tool.ruff.lint])",),
        build=_ruff(["check"]),
        version_pin="RUFF_VERSION",
    ),
    Gate(
        name="ruff-format",
        job="lint",
        summary="ruff format --check",
        ci_steps=("ruff format --check",),
        build=_ruff(["format", "--check"]),
        version_pin="RUFF_VERSION",
    ),
    Gate(
        name="markdown-lint",
        job="docs",
        summary="PyMarkdown style lint over every tracked *.md",
        ci_steps=("Markdown style lint (PyMarkdown, ADR-0062)",),
        build=_markdown_lint,
        version_pin="PYMARKDOWN_VERSION",
    ),
    Gate(
        name="pyright",
        job="typecheck",
        summary="pyright --strict",
        ci_steps=("pyright --strict (config in pyproject.toml [tool.pyright])",),
        build=_pyright,
        version_pin="PYRIGHT_VERSION",
    ),
    Gate(
        name="pip-audit",
        job="audit",
        summary="audit locked runtime dependencies for known vulnerabilities",
        ci_steps=(
            "Export locked runtime dependencies",
            "Audit locked dependencies for known vulnerabilities",
        ),
        build=_pip_audit,
        version_pin="PIP_AUDIT_VERSION",
    ),
    Gate(
        name="pytest",
        job="test",
        summary="the suite under -n auto, plus the log-canary scan",
        ci_steps=(
            "Run tests serially, capturing all output for the canary scan",
            "Log canary scan — serial legs (testing-strategy.md CI Gates)",
            "Run tests in parallel, capturing all output for the canary scan",
            "Log canary scan — parallel leg (testing-strategy.md CI Gates)",
        ),
        build=_pytest,
        version_pin="PYTEST_VERSION",
    ),
    Gate(
        name="gitleaks",
        job="secrets",
        summary="scan full git history for committed credentials",
        ci_steps=("Scan full git history for committed credentials",),
        build=None,
        ci_only_reason=(
            "needs the hash-verified gitleaks binary CI downloads per run; "
            "installing an unverified copy locally would weaken the gate it checks"
        ),
    ),
    Gate(
        name="test-matrix",
        job="test",
        summary="the suite on ubuntu, windows and macOS",
        ci_steps=(),
        build=None,
        ci_only_reason=(
            "three operating systems; the macOS leg is unreachable from any one "
            "machine, and it has caught failures the other two could not"
        ),
    ),
)

# `run:` steps in ci.yml that are setup or aggregation rather than gates. The
# drift test requires every named run: step to be claimed either by a gate above
# or by this set, so a new CI gate cannot land unnoticed.
NON_GATE_STEPS: frozenset[str] = frozenset(
    {
        "All gates passed",  # the ci-ok aggregate
    }
)

# Gates --fast drops. Named here rather than inline so the message that reports
# them and the filter that removes them cannot disagree.
SLOW_GATES: frozenset[str] = frozenset({"pytest", "pip-audit"})

# Workflow-level `env:` keys CI exports to every step and the runner must too.
# The *names* are the second copy that cannot be avoided — something has to say
# which keys matter — but the values are derived, which is where drift lives.
WORKFLOW_ENV_NAMES: tuple[str, ...] = ("PYTHONUTF8",)

# Matched by prefix because the version is part of the step name; a gitleaks
# bump should not fail the drift test.
NON_GATE_STEP_PREFIXES: tuple[str, ...] = ("Install gitleaks",)

# Convenience selectors that are not ci.yml job names.
ALIASES: dict[str, tuple[str, ...]] = {
    "ruff": ("ruff-check", "ruff-format"),
}


def gate_by_name() -> dict[str, Gate]:
    return {gate.name: gate for gate in GATES}


def groups() -> dict[str, tuple[str, ...]]:
    found: dict[str, list[str]] = {}
    for gate in GATES:
        found.setdefault(gate.job, []).append(gate.name)
    resolved = {job: tuple(names) for job, names in found.items()}
    resolved.update(ALIASES)
    return resolved


def resolve(selectors: Iterable[str]) -> list[Gate]:
    """Expand names and groups to gates, in registry (execution) order."""
    by_name = gate_by_name()
    all_groups = groups()
    wanted: set[str] = set()
    unknown: list[str] = []
    for selector in selectors:
        if selector in all_groups:
            wanted.update(all_groups[selector])
        elif selector in by_name:
            wanted.add(selector)
        else:
            unknown.append(selector)
    if unknown:
        known = ", ".join(sorted({*by_name, *all_groups}))
        raise GateError(f"unknown gate(s): {', '.join(unknown)}. Known: {known}")
    return [gate for gate in GATES if gate.name in wanted]


def version_of(gate: Gate, ctx: Context) -> str:
    if gate.name == "gitleaks":
        return ctx.gitleaks_version or "—"
    if gate.version_pin:
        return ctx.pins.get(gate.version_pin, "—")
    if gate.tool_pin:
        # Marked so the column does not read as a version the command carries.
        # Without it `--list` shows "—" beside the one gate whose failure is
        # most often a toolchain problem wearing a lockfile's name.
        return f"needs {ctx.pins.get(gate.tool_pin, '—')}"
    return "—"


def render_list(ctx: Context, gates: Sequence[Gate] | None = None) -> str:
    gates = list(gates) if gates is not None else list(GATES)
    lines = ["Gates derived from .github/workflows/ci.yml", ""]
    width = max(len(gate.name) for gate in gates)
    for gate in gates:
        where = "local" if gate.local else "CI-only"
        heading = f"{gate.name:<{width}}  {version_of(gate, ctx):<9} {where:<8}"
        lines.append(f"  {heading} [{gate.job}]")
        lines.append(f"  {'':<{width}}  {gate.summary}")
        if gate.local:
            try:
                steps = gate.steps(ctx)
            except GateError as exc:
                lines.append(f"  {'':<{width}}  ! {exc}")
            else:
                for step in steps:
                    lines.append(f"  {'':<{width}}  $ {step.shown()}")
        else:
            lines.append(f"  {'':<{width}}  ! CI-only: {gate.ci_only_reason}")
        lines.append("")
    selectors = ", ".join(sorted(groups()))
    lines.append(f"Groups: {selectors}")
    lines.append(
        "No arguments runs every local gate, cheapest first, "
        "stopping at the first failure."
    )
    return "\n".join(lines)


def run_step(step: Step, workflow_env: dict[str, str] | None = None) -> int:
    # The workflow-level env comes from ci.yml, derived like everything else —
    # a literal "1" here was a second copy of a ci.yml value inside the module
    # written to delete second copies, and its expiry is already dated in
    # ci.yml's own comment (PEP 686, Python 3.15).
    #
    # It is not cosmetic. Measured on Windows/Python 3.14: a child on a pipe
    # defaults to the legacy code page (cp1252), while the tee below decodes
    # UTF-8 with errors="replace". Without PYTHONUTF8 a test printing a
    # non-ASCII character either crashes the local pytest gate — a red CI is
    # green for — or lands in the tee'd log as U+FFFD, mangled *before* the
    # canary gate scans it.
    #
    # Applied *after* os.environ, not before: an ambient PYTHONUTF8=0 would
    # otherwise win and put the mismatch straight back. A step may still
    # override it deliberately.
    env = {**os.environ, **(workflow_env or {}), **step.env}
    print(f"$ {step.shown()}", flush=True)
    if step.capture_to is None:
        try:
            completed = subprocess.run(  # noqa: S603 - fixed executable, no shell
                step.argv, cwd=REPO_ROOT, env=env, check=False
            )
        except OSError as exc:
            # The `or` fallback on _GIT/_UV/_UVX promises a missing tool fails
            # "as a gate that could not start, naming the command". Unwrapped,
            # it surfaced as a bare FileNotFoundError traceback — main() catches
            # only GateError — so the promise was prose the code did not keep.
            raise GateError(f"could not start `{step.shown()}`: {exc}") from exc
        return completed.returncode

    # Reproduce CI's `| tee`: stream to the console and to a file at once, so a
    # failing test's traceback is visible *and* scannable by the canary gate.
    step.capture_to.parent.mkdir(parents=True, exist_ok=True)
    try:
        started = subprocess.Popen(  # noqa: S603 - fixed executable, no shell
            step.argv,
            cwd=REPO_ROOT,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
    except OSError as exc:
        # Same conversion as the uncaptured branch above, so both paths report a
        # missing tool identically rather than one naming it and one crashing.
        raise GateError(f"could not start `{step.shown()}`: {exc}") from exc
    with (
        started as process,
        step.capture_to.open("w", encoding="utf-8") as sink,
    ):
        # `stdout=PIPE` above guarantees a pipe; the guard is here rather than an
        # assert because asserts are stripped under -O, and a silently skipped
        # capture would hand the canary scan an empty log and call it clean.
        if process.stdout is not None:
            for line in process.stdout:
                sys.stdout.write(line)
                sink.write(line)
            sys.stdout.flush()
    return process.returncode


def run_gate(gate: Gate, ctx: Context) -> GateResult:
    print(f"\n=== {gate.name} — {gate.summary} ===", flush=True)
    steps = gate.steps(ctx)
    if not steps:
        # Reported as its own outcome rather than as a pass: a gate that
        # executed nothing has proved nothing, and folding it into the passed
        # count is the false green `Gate.steps` refuses for CI-only gates.
        print("  (nothing to do — no work for this gate)")
        return GateResult.EMPTY

    failed = False
    for step in steps:
        if failed and not step.always_run:
            continue
        if step.rebuild is not None:
            step = replace(step, argv=step.rebuild())
        code = run_step(step, ctx.workflow_env)
        if code != 0:
            print(f"\nFAILED: {gate.name} (exit {code})", file=sys.stderr)
            print(f"  command: {step.shown()}", file=sys.stderr)
            if step.on_failure:
                print(f"  note: {step.on_failure}", file=sys.stderr)
            # Not an early return: a later `always_run` step still has to run,
            # and its own failure is folded into the same verdict.
            failed = True
    return GateResult.FAILED if failed else GateResult.PASSED


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="run_gates.py",
        description=(
            "Run this repository's CI gates locally, with every command and "
            "pinned version derived from .github/workflows/ci.yml."
        ),
    )
    parser.add_argument(
        "selectors",
        nargs="*",
        help="gate or group names; empty runs every locally-runnable gate",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        # Not "the exact command": two gates render a placeholder rather than a
        # pasteable argv -- `markdown-lint`'s `<N files>` and `pytest`'s canary
        # step, whose argument list is only knowable after the test step has
        # run. Cut item C7's sweep, paid here because this file is one of the
        # six sites it names and this change touches it.
        help="show every gate, its pinned version, and how it runs",
    )
    parser.add_argument(
        "--print",
        dest="print_only",
        action="store_true",
        help="echo the selected commands without running them",
    )
    parser.add_argument(
        "--fast",
        action="store_true",
        help="skip the slow gates (pytest, pip-audit)",
    )
    args = parser.parse_args(argv)

    try:
        text = CI_WORKFLOW.read_text(encoding="utf-8")
    except OSError as exc:
        print(f"cannot read {CI_WORKFLOW}: {exc}", file=sys.stderr)
        return 1

    # --list and --print read commands rather than running them, so they build
    # every step in dry mode and touch no directory.
    ctx = Context(
        pins={},
        gitleaks_version=None,
        dry=args.list or args.print_only,
    )
    try:
        ctx.pins = read_pins(text)
        ctx.gitleaks_version = read_gitleaks_version(text)
        ctx.canary_dir_name = read_workflow_env(text, "CANARY_CAPTURE_DIR")
        # Only the names the runner actually needs, not every workflow key: an
        # unfiltered sweep would export CI's own bookkeeping into local children.
        for name in WORKFLOW_ENV_NAMES:
            value = read_workflow_env(text, name)
            if value is not None:
                ctx.workflow_env[name] = value

        # The selection is completed *once*, before any output branch reads it.
        # Each branch used to finish it for itself, and they disagreed three
        # ways: `--list nonsense-gate` printed the full list and exited 0;
        # `--list --fast` advertised the gates --fast drops; and
        # `--print pytest --fast` printed nothing and exited 0. Resolving,
        # filtering and partitioning here is what makes those one bug.
        selected = resolve(args.selectors) if args.selectors else list(GATES)

        dropped: list[Gate] = []
        if args.fast:
            dropped = [gate for gate in selected if gate.name in SLOW_GATES]
            selected = [gate for gate in selected if gate.name not in SLOW_GATES]

        runnable = [gate for gate in selected if gate.local]
        skipped = [gate for gate in selected if not gate.local]

        if not runnable:
            # Reached by every branch, including the read-only ones: a --print
            # that emits nothing on exit 0 is the same confirmation-shaped
            # silence, and a reader asking what a gate runs cannot tell it from
            # an answer. Two reasons, never conflated — a local gate --fast
            # dropped is not a gate this machine cannot run.
            print("nothing to run.", file=sys.stderr)
            for gate in dropped:
                print(f"  - {gate.name}: dropped by --fast", file=sys.stderr)
            for gate in skipped:
                print(
                    f"  - {gate.name}: CI-only — {gate.ci_only_reason}", file=sys.stderr
                )
            return 1

        if args.list:
            print(render_list(ctx, selected))
            return 0

        if args.print_only:
            for gate in runnable:
                print(f"# {gate.name}")
                for step in gate.steps(ctx):
                    print(step.shown())
            for gate in skipped:
                print(f"# {gate.name}: CI-only — {gate.ci_only_reason}")
            return 0

        # Sequential with an early exit, not a comprehension over every gate:
        # the registry is ordered cheap-to-slow precisely so a lint failure does
        # not cost a full test run first, and collecting all results before
        # checking them throws that away.
        results: dict[str, GateResult] = {}
        for gate in runnable:
            results[gate.name] = run_gate(gate, ctx)
            if results[gate.name] is GateResult.FAILED:
                return 1
    except GateError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        ctx.cleanup()

    passed = [name for name, r in results.items() if r is GateResult.PASSED]
    empty = [name for name, r in results.items() if r is GateResult.EMPTY]
    # "All" only when it is true of the whole selection. Printing it above the
    # empty-gate report made the summary contradict itself: "All 12 selected
    # gate(s) passed." immediately followed by one that examined nothing.
    if empty:
        print(f"\n{len(passed)} selected gate(s) passed.")
    else:
        print(f"\nAll {len(passed)} selected gate(s) passed.")
    if empty:
        # Named *and* non-zero. Naming it while still exiting 0 was the false
        # green in a quieter register: `/land` reads the exit code and its rule
        # is "a failing gate stops the landing", so a markdown pathspec that
        # silently stopped matching would print "proves nothing" and land
        # anyway. The only gate that can be empty here is markdown-lint, and it
        # is empty only when `git ls-files` matches no tracked *.md — which in
        # this repository means the query broke, not that there is no markdown.
        print(f"{len(empty)} gate(s) had nothing to run — they prove nothing:")
        for name in empty:
            print(f"  - {name}")
        print("Exiting non-zero: a gate that examined nothing is not a pass.")
        return 1
    if skipped:
        print(f"Skipped {len(skipped)} CI-only gate(s) — this is not full CI green:")
        for gate in skipped:
            print(f"  - {gate.name}: {gate.ci_only_reason}")
    return 0


if __name__ == "__main__":
    # Inside the `__main__` guard on purpose; scripts/check_spec_links.py
    # documents the measured reasons, and they govern. Needed here because the
    # gate summaries and ci.yml step names contain em dashes.
    for stream in (sys.stdout, sys.stderr):
        if isinstance(stream, io.TextIOWrapper):
            stream.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main())
