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

**Output is spooled, not streamed.** Every step's combined output goes to a file
in a scratch directory outside the repository; the console gets the command line
and, when a step fails, the last lines of what it printed plus the path to the
whole of it. The gate output itself is read once and then costs nothing, which
is the point: an agent session re-sends everything already in its window on
every later request, so a full pytest transcript is paid for hundreds of times
over. ``--verbose`` restores the streaming, and nothing about what a gate
asserts, what it exits, or what the summary says changes either way. ADR-0080
owns the policy, its defaults, and the one gate exempt from it.

Exit 0 when every selected gate passes; 1 on the first failure, having echoed
the exact command that failed. Stdlib only; files are read as UTF-8.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from collections import deque
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field, replace
from enum import Enum
from pathlib import Path, PurePath
from typing import TextIO

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

# Every step's output is written here and, by default, nowhere else. The prefix
# is a constant rather than two literals because `prune_scratch_dirs` globs for
# what `scratch_dir` created: a disagreement between them would not fail, it
# would silently prune nothing and let the directories accumulate forever.
SCRATCH_PREFIX = "run_gates-"

# Written into a run's directory once that run has finished. A prune considers
# only marked directories, because *recency cannot tell a finished run from a
# running one*: measured, a directory's mtime does not advance while a log
# inside it is appended, so five short runs would evict the live directory of a
# long one -- taking the canary worker sink with it and turning the canary scan
# into a failure with no failing test. ADR-0080 §2 records the ordering.
COMPLETE_MARKER = "run-complete"

# How long an *unmarked* directory is left alone before it is treated as
# abandoned rather than as a run still in progress. Longer than any run this
# script can produce -- the full suite is ~2 minutes -- by a margin wide enough
# to cover a machine suspended mid-run, and short enough that a run killed
# before it could mark itself does not hold disk forever.
ORPHAN_GRACE_SECONDS = 24 * 60 * 60

# How many *completed* runs' directories survive a prune (ADR-0080 §2). Enough
# to hold both legs of a Windows/WSL verification plus the green run a failure
# is being compared against; small enough that captured test output does not
# accumulate without bound. Retention is what makes the path printed on failure
# real -- see `Context.cleanup`, which used to remove this directory outright.
# In-progress runs are outside this count, so a bump here does not change how
# many concurrent invocations are safe.
RUNS_RETAINED = 5

# How much of a suppressed step's output is put back on the console when it
# fails (ADR-0080 §1). Chosen against the longest gate: pytest's
# `short test summary info` block -- the part naming which tests failed -- is
# the last thing it prints, so it survives a tail of this size even when the
# traceback above it does not.
FAILURE_TAIL_LINES = 40

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
    "whatever this gate is named for. If the replayed output is uv's rather "
    "than the command's, run `run_gates.py lockfile`."
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
    # When set, the step's combined output is written here. It reaches the
    # console as well only under `--verbose`, which is where this stopped being
    # CI's `| tee` and became the sink for the quiet default. Set by a builder
    # for a step whose log something else reads back — only the pytest gate,
    # for the canary scan — and otherwise assigned by `run_gate`.
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


def _temp_root() -> Path:
    """Where run directories are created and pruned.

    One named reader, so a test redirects one name. Redirecting
    `tempfile.gettempdir` instead reaches every user of the stdlib module in the
    process -- under `-n auto` that includes pytest's own machinery, while
    `capfd` holds the streams -- which is process-wide surgery for a
    module-local concern.

    **The root is checked rather than assumed.** `tempfile.gettempdir()`
    consults `TMPDIR`, `TEMP` and `TMP` before the platform default -- measured,
    setting them redirects this wholesale -- so "outside the repository", which
    this module's docstring states and `cleanup` rests on, is a property of the
    environment and not one the code established. A root inside the checkout
    would make every retained log a repository file and, because
    `review_worktree.py` copies untracked paths into each reviewer worktree,
    would replicate them into review snapshots too. Refusing is the only
    reading that keeps the docstring true; narrowing the docstring instead
    would leave the behaviour and describe it more carefully, which is the
    weaker of the two fixes.

    **Raw and resolved**, because the two catch different things and the second
    is not decoration: a junction, a symlink, an 8.3 short name or a *relative*
    root all reach the repository under a name that compares unequal to it, and
    only `.resolve()` sees through them. Two tests pin it between them, and the
    split matters because `os.path.abspath` covers one half and not the other:
    `test_the_guard_sees_through_a_root_that_only_resolves_into_the_repository`
    covers the textual half (a `..` segment), and
    `test_the_guard_dereferences_a_link_into_the_repository` covers the half
    that needs the disk, on any host that will let a symlink be created.
    Without them, dropping `.resolve()` left the whole suite green.

    Case is left to `is_relative_to`, which is already correct on both legs --
    `WindowsPath` casefolds, `PosixPath` does not, matching the filesystems.
    An earlier version normcased both sides and credited that for the
    cross-platform correctness; measured, `posixpath.normcase` is the identity
    function and `WindowsPath.is_relative_to` casefolds without help, so the
    call did nothing on either platform and the rationale named a mechanism
    that was not working. Raised by Copilot on PR #106.
    """
    root = Path(tempfile.gettempdir())
    for candidate in (root, root.resolve()):
        if candidate.is_relative_to(REPO_ROOT):
            raise GateError(
                f"the temp root is inside the repository ({candidate}). Gate "
                "output would become a repository file: unset TMPDIR/TEMP/TMP "
                "or point them somewhere outside the checkout"
            )
    return root


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
    # Paths a gate created as working material rather than as output for someone
    # to read afterwards. `cleanup` removes these and keeps the rest, which is
    # the whole distinction retention turns on: a step log survives because the
    # console no longer carries it, while the pytest canary's worker sink was
    # never on the console and is consumed by the scan within the same run.
    # Measured, and the reason this list exists: one full run's sink is ~104 MB
    # of DEBUG-level per-worker capture against ~530 KB of step logs, so
    # retaining it would keep half a gigabyte of captured test output per five
    # runs to no one's benefit.
    ephemeral: list[Path] = field(default_factory=list[Path])
    dry: bool = False
    # False spools every step's output to `scratch_dir` and keeps the console to
    # one line per command; True restores the streaming this script did before,
    # which also means no step but pytest's is captured at all.
    verbose: bool = False
    # Set when any gate fails, so `cleanup` can keep the working material a
    # failure report has just pointed at. The canary scan names the worker log
    # and line its hit came from; sweeping that file on the way out would delete
    # the evidence between printing the path and the operator opening it.
    failed: bool = False

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
            # Built from the prefix rather than spelled out: a literal here
            # would be a third writer of `run_gates-` paths, which the prefix
            # constant's own comment says cannot exist.
            return _temp_root() / f"{SCRATCH_PREFIX}scratch"
        if self.scratch is None:
            root = _temp_root()
            try:
                self.scratch = Path(tempfile.mkdtemp(prefix=SCRATCH_PREFIX, dir=root))
            except OSError as exc:
                # Same conversion as the sink's open and its writes: a full,
                # missing or unwritable temp directory is a gate that could not
                # start, not a traceback. This is the earliest of the three and
                # was the last to be converted. Raised by Copilot on PR #106.
                raise GateError(
                    f"could not create a run directory in {root}: {exc}"
                ) from exc
        return self.scratch

    def step_log(self, gate: str, position: int) -> Path:
        """Where one step's captured output is written.

        Per *step*, not per gate, because `capture_to` is a `Step` field: two
        steps of one gate sharing a sink would have the canary scan's own log
        written twice, which is the double-write the pytest gate's contract
        forbids. The gate name and the position within it are both in the
        filename so a directory listing reads as the run did.
        """
        return self.scratch_dir() / f"{gate}-{position:02d}.log"

    def mark_complete(self) -> None:
        """Stamp this run's directory as finished, so a prune may consider it.

        Written last, after the working material has been dealt with, because
        the mark is what makes the directory eligible for deletion by the next
        run. A directory that never gets marked is treated as in progress and
        left alone until `ORPHAN_GRACE_SECONDS` has passed.
        """
        if self.scratch is None:
            return
        # Suppressed because this is housekeeping, not a gate: an unmarked
        # directory ages out on the orphan path instead of pinning a slot, and
        # failing here would turn a green run red for a reason having nothing
        # to do with any gate.
        with contextlib.suppress(OSError):
            (self.scratch / COMPLETE_MARKER).touch()

    def _scratch_is_empty(self) -> bool:
        """Whether this run's directory was created and holds nothing at all.

        ``False`` on any error, so a reading that failed leaves the caller on
        its pre-existing path — mark and retain — rather than deciding a
        removal from an answer it does not have.
        """
        if self.scratch is None:
            return False
        try:
            return not any(self.scratch.iterdir())
        except OSError:
            return False

    def cleanup(self) -> None:
        """Drop this run's working material; keep its logs; prune older runs.

        This directory holds captured test output — exactly the material the
        canary gate exists to police — outside the repository, where the
        containment gate does not look, and it was once removed unconditionally
        for that reason. It is now the run's *product* rather than a working
        area: the console no longer carries a step's output, so a path printed
        beside a failure has to still resolve when someone goes to read it.
        Deleting it here would make that path a lie, which is the worse of the
        two failures.

        The leak the old removal answered is bounded rather than eliminated,
        and the bound differs per kind of file. **ADR-0080 §4 holds that audit**
        — every retained file, why retaining it is acceptable, and the one
        residual surface it does not close — and it is not repeated here. A
        one-line version of it ("every retained log is scanned by the canary
        gate") was written here and was false for all but one of them; the
        four-bullet version that replaced it was a second copy of the table,
        and this repository has now paid twice for a claim kept in two places.

        What belongs here is the one decision this method makes that the table
        does not: **`ephemeral` is swept on the green path and kept on the red
        one.** It is the bulk of the volume — the canary worker sink, measured
        at ~104 MB against ~530 KB of step logs — and nothing outside a passing
        run reads it. But a canary hit's report names the worker log and the
        line its match came from, and that report is the last thing printed
        before this runs, so sweeping it on a failure deletes the evidence
        between naming it and the operator opening it. That is the same rule
        that stopped this method deleting the run directory, one level in.

        All of it sits outside the repository, where no gate and no `git add`
        reaches, and a fixed number of completed runs survive.
        """
        if self.scratch is None:
            # Nothing was created, so there is nothing new to age out and no
            # reason for a `--list` to delete another run's logs.
            return
        if not self.failed:
            for path in self.ephemeral:
                shutil.rmtree(path, ignore_errors=True)
            self.ephemeral.clear()
        if self._scratch_is_empty():
            # An empty directory is not a product, and marking it complete
            # spends one of `RUNS_RETAINED` slots to keep nothing — evicting a
            # run whose logs someone may still want. The notice in `main` is
            # what creates this directory and it fires whenever *any* selected
            # gate spools, which is not the same as one having spooled: the run
            # can exit first. `containment` is the gate that makes that
            # ordinary rather than rare — it runs first and is the one gate
            # exempt from spooling (ADR-0080 §5), so every refused tree returns
            # through here with the directory still empty.
            #
            # Removed rather than left unmarked. An unmarked directory is
            # exempt from `keep`, so that alone would stop the eviction, but it
            # then lingers until `ORPHAN_GRACE_SECONDS` — trading a wrong
            # eviction for an accumulating pile of empty directories in the
            # shared temp root.
            shutil.rmtree(self.scratch, ignore_errors=True)
        else:
            self.mark_complete()
        prune_scratch_dirs()


def _completed_at(run: Path) -> float | None:
    """When this run marked itself finished, or ``None`` while it is running.

    The whole activity guard is here. `Path.glob` and `Path.is_dir` swallow
    their own errors — measured on the pinned 3.14: an ACL-denied root, a
    missing root and a file used as a root all yield nothing rather than
    raising. That is a property of `glob` rather than of those three cases:
    `Path.iterdir` on the *same* denied directory raises `PermissionError`
    (measured), so the swallowing is real and is what leaves this stat as the
    one filesystem call in the prune whose failure is visible.

    **What an unreadable mark produces is `None` -- "unmarked" -- and not a
    verdict.** Liveness is then `_abandoned`'s to decide, and it collects an
    unmarked directory once it is past the grace period, treating unstattable
    as abandoned. An earlier version of this line claimed such a directory was
    "treated as still running rather than as prunable", which the caller
    contradicts two functions down. Raised by Copilot on PR #106, and a sibling
    of the same overclaim corrected in `prune_scratch_dirs` on this branch --
    the correction there was swept for siblings four times and this one, in the
    function immediately above, survived every pass.
    """
    try:
        return (run / COMPLETE_MARKER).stat().st_mtime
    except OSError:
        return None


def _abandoned(run: Path, cutoff: float) -> bool:
    """Whether an unmarked directory is old enough to be an orphan, not a run.

    An unmarked directory is normally a live run and must be left alone. One
    left behind by a kill, a power loss or a crash before `mark_complete` never
    becomes marked, so without this it would hold disk forever. Unstattable
    counts as abandoned, matching the mark's reading of the same failure from
    the other side: neither call can show the directory to be live.

    **Age is the only evidence here, and it cannot prove the owning invocation
    exited.** The measurement that rules out recency for *ordering* applies to
    this liveness question too: a directory's mtime advances when a step log is
    created in it, not while one is appended, so a run hung inside a single step
    past the cutoff — or one spanning a suspend longer than it — reads exactly
    like a run killed before it could mark itself, and a concurrent invocation
    collects it. That residual is deliberate and bounded rather than overlooked.
    ADR-0080 §2 owns the trade and the consequence it carries on each platform;
    `specs/open-questions.md` owns the deferred remedy and its trigger, and
    `test_a_live_run_past_the_grace_period_is_collected_like_an_orphan` pins
    what happens today. Not restated here — the argument had landed in three
    places by the time it was noticed, which is the duplication `cleanup`
    declines a few definitions up for the same reason.
    """
    try:
        return run.stat().st_mtime < cutoff
    except OSError:
        return True


def prune_scratch_dirs(root: Path | None = None, keep: int | None = None) -> list[Path]:
    """Age out all but the ``keep`` most recently *completed* run directories.

    Completion is an explicit mark rather than an inference from recency, and
    that is the correctness of the whole retention scheme rather than a detail.
    Measured: a directory's mtime does not advance while a log inside it is
    appended, so ordering by mtime cannot distinguish the live directory of a
    long run from a stale one. Five short runs sharing a temp root would evict
    a running suite's directory mid-run — deleting the canary worker sink and
    leaving `scan_log_canary.py` to fail with no failing test, or worse, to scan
    a surviving subset of the worker logs and pass. `mkdtemp` names are random,
    so name order is no better.

    In-progress runs are not counted against ``keep``, so concurrent invocations
    do not compete for the retained slots. That exemption is bounded rather than
    absolute: `_abandoned` collects an unmarked directory once it is older than
    the grace period, which keeps "unmarked means leave it alone" from being an
    unbounded promise and costs a residual it documents — age cannot prove the
    owning invocation exited, so a run still live past the cutoff is collected
    like an orphan.

    Every filesystem call here tolerates failure. Two runs pruning at once will
    race, and losing that race means another process already removed the
    directory — the intended outcome, not an error worth failing a gate over.

    Returns what it removed — verified against the filesystem, not assumed from
    having called `rmtree`. `ignore_errors=True` means a directory holding an
    open log is left in place while the call still returns, so an unconditional
    append made every caller that reported the list a liar.

    Both settings resolve here rather than in the signature: a default argument
    binds its value once at import, so a `RUNS_RETAINED` read there could not be
    changed by anything — including a test that thought it had.
    """
    root = _temp_root() if root is None else root
    keep = RUNS_RETAINED if keep is None else keep
    cutoff = time.time() - ORPHAN_GRACE_SECONDS

    completed: list[tuple[float, Path]] = []
    stale: list[Path] = []
    for path in root.glob(f"{SCRATCH_PREFIX}*"):
        if not path.is_dir():
            continue
        finished = _completed_at(path)
        if finished is None:
            if _abandoned(path, cutoff):
                stale.append(path)
        else:
            completed.append((finished, path))

    # Newest completion first, so the tail past `keep` is the oldest work.
    completed.sort(key=lambda entry: entry[0], reverse=True)
    stale.extend(path for _, path in completed[keep:])

    removed: list[Path] = []
    for path in stale:
        shutil.rmtree(path, ignore_errors=True)
        if not path.exists():
            removed.append(path)
    return removed


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
    # False to stop `run_gate` assigning this gate's steps a sink, leaving them
    # on the inherited console. Exactly one gate sets it:
    # `check_personal_containment.py` prints the offending repository path on a
    # violation, and ADR-0079 §R2 makes a filename under the personal directory
    # provenance — personal data in its own right. Under the old console-only
    # behaviour that path was printed and gone; spooling it would put a copy of
    # the one thing the gate exists to find into a file that outlives the run,
    # which is the surface ADR-0079 §4 requires a guard to be audited against.
    # The gate prints two lines when it passes, so exempting it costs two lines
    # of console and buys the whole surface back.
    #
    # "Not assigned a sink" rather than "never written to disk", because those
    # differ for a gate that brings its own `capture_to` — the flag cannot take
    # one away. No gate does both today and the invariant test asserts none
    # can: a step's log is either the runner's to withhold or the gate's to
    # justify, never half of each. ADR-0080 §4 holds the per-gate audit of what
    # each one can print, including the one residual surface that audit does
    # not close — stated there rather than restated here, because a claim kept
    # in two places is one this repository has measured drifting.
    spool_output: bool = True
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
    # Working material, not output: the scan below consumes it inside this run,
    # and nothing reads it afterwards. Registered so `cleanup` removes it while
    # keeping the step logs beside it — see `Context.ephemeral` for the sizes
    # that make the distinction worth drawing.
    ctx.ephemeral.append(canary_dir)
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
# all read. Listed in execution order, under two principles that order the
# whole tuple and one pairwise constraint that does not:
#
# 1. `containment` first. It is the gate that refuses a tree holding a personal
#    path, and nearly every gate behind it walks that tree and names the
#    offending path on a violation (ADR-0080 §3 enumerates the six and which of
#    them add a line number -- `ruff format --check` does not). Running it first
#    means a run over such a tree stops before any other gate can name the path
#    it found, which is the bound ADR-0080 §4 rests on.
# 2. Then the cheap gates ahead of the slow ones (`SLOW_GATES`), so a lint typo
#    does not cost 112 seconds of pytest before it surfaces. Deliberately *not*
#    "cheapest first", which is what this said until a review round measured it:
#    `spec-links` runs ahead of gates that cost appreciably less (ADR-0080 §5
#    measures all of them; no figure is restated here, and the first draft of
#    this line restated one wrong), so a strict cost sort is a claim this tuple
#    does not earn -- and nothing could pin one without encoding a per-gate
#    timing, a machine-specific number the registry has no business holding.
#    Cheap-versus-slow is the same distinction `--fast` already acts on, so it
#    is pinnable against the module rather than against a stopwatch. No position
#    is quoted here, per `_uv_python_step`'s rule: an ordinal into a registry
#    that gains entries is wrong the moment one is inserted above it, and this
#    change is exactly such an insertion.
# 3. And `spec-links` ahead of `lockfile`, which neither principle above
#    implies: both gates are cheap and neither is the refusal gate, so a
#    maintainer re-sorting this tuple to (1) and (2) alone can legally swap
#    them. `spec-links` runs under `uv run`, which re-locks when
#    `pyproject.toml` has moved ahead of `uv.lock` — behind `uv lock --check`
#    it would repair the lockfile that gate has just certified, so local passes
#    while CI fails on a clean checkout (ADR-0061).
#    `test_the_docs_gate_runs_before_the_lockfile_gate` pins it, as a relation
#    rather than as two positions. It is a constraint on one pair, not a
#    principle over the tuple, which is why `--list` does not state it: nothing
#    an operator chooses between depends on it.
#
# ADR-0080 §5 owns the decision, what (1) costs a local run, and the two
# consequences it carries -- including that `containment` at `--scope branch`
# resolves a merge base, so a broken checkout now fails first. **The promotion's
# cost** is deliberately not repeated here: that figure was measured wrong once
# already, and a measurement at two sites with nothing pinning them together is
# one that gets corrected at whichever site the reader happened to open. Scoped
# to that figure rather than to every number, because principle 2's reason is
# pytest's 112 seconds, and a principle whose justification lives only in
# another document is one a reader of this tuple cannot weigh.
#
# `--list` states both principles, and
# `test_the_list_output_states_the_ordering_it_uses` pins both: `containment` at
# the front of this tuple, and -- over the *local* gates alone -- no cheap gate
# behind the first slow one. That narrowing is not a hedge: `gitleaks` and
# `test-matrix` are CI-only and not slow, and they sit behind `pytest`, so the
# partition is false of the whole tuple and true of what a local run executes.
# --------------------------------------------------------------------------

GATES: tuple[Gate, ...] = (
    Gate(
        name="containment",
        job="secrets",
        # The scope is in the summary, not only in the argv: ADR-0075 §1 admits
        # this gate as a *declared* narrowing and makes naming the reduction at
        # the gate the one checkable half of that class's two conditions. Both
        # that ADR and this module's header said `--list` rendered these words
        # while the summary did not carry them, so the condition was documented
        # as met by a string that did not exist.
        summary=(
            "no path at or under specs/personal is tracked, staged, or in "
            "history (branch scope)"
        ),
        ci_steps=("Scan full git history for personal-data paths",),
        build=_docs_gate("check_personal_containment.py", "--scope", "branch"),
        # A violation names the path it found. See `Gate.spool_output`.
        spool_output=False,
    ),
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
    # Derived from the constant that is actually read, not spelled again: the
    # header asserting a path while `CI_WORKFLOW` pointed somewhere else was a
    # lie no test could catch, because the only assertion on this line compared
    # it to the same literal it restated.
    lines = [f"Gates derived from {CI_WORKFLOW.relative_to(REPO_ROOT).as_posix()}", ""]
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
    # The ordering sentence belongs to the whole registry, so it is only
    # printed when the whole registry is what was listed. `--list lockfile`
    # rendered a one-gate list under "containment first", and `--list --fast`
    # claimed "cheap before slow" with every slow gate already dropped — both
    # scoped by the "No arguments runs" prefix and so not false, but an
    # operator following `/land` step 2's documented subset form reads a
    # sentence about gates that are not on the screen. It went unnoticed
    # because naming a gate here is new: the sentence named none until the
    # promotion, when the mismatch stopped being inert.
    if [gate.name for gate in gates] == [gate.name for gate in GATES]:
        lines.append(
            "No arguments runs every local gate — containment first, then cheap "
            "before slow — stopping at the first failure."
        )
    else:
        lines.append(
            f"Listed {len(gates)} of {len(GATES)} gates, in the order this "
            "selection runs. No arguments runs every local gate, stopping at "
            "the first failure."
        )
    return "\n".join(lines)


def _console_safe(line: str, stream: TextIO | None = None) -> str:
    """A line the current stdout can encode, whatever encoding it happens to be.

    The `__main__` guard reconfigures stdout to UTF-8 with `errors="replace"`,
    but only when this module is run as a script. Imported and called — which is
    what the HELM work items propose — stdout keeps the legacy code page on
    Windows and a single non-ASCII character in a captured traceback raises
    `UnicodeEncodeError` *inside the failure reporter*, turning a legible
    failure into an unhandled one. That is the same outcome the `OSError` guard
    below exists to prevent, reached through the encoder instead of the reader.
    **Takes the stream it is protecting.** It read `sys.stdout`'s encoding
    unconditionally for one release, which was right while its only caller
    printed to stdout and wrong the moment two of them printed to `stderr`:
    the two streams can carry different encodings, so guarding a `stderr`
    write against `stdout`'s is guarding the wrong one. `None` means stdout,
    resolved in the body rather than in the signature so a redirected stream is
    seen -- a default argument would bind whatever `sys.stdout` was at import.
    """
    target = sys.stdout if stream is None else stream
    encoding = getattr(target, "encoding", None) or "utf-8"
    return line.encode(encoding, "replace").decode(encoding, "replace")


def _say(line: str, stream: TextIO | None = None, *, flush: bool = False) -> None:
    """Print one operator-facing line, encodable on whatever console this is.

    The guard `_console_safe` provides, applied from one place rather than
    re-decided at every call site. Hand-wrapping missed five of `main`'s
    prints: the two literal em dashes in the summary tail, the `CI-only — …`
    line in the nothing-to-run branch and its twin under `--print`, and the
    whole of `--list`'s output — to which this branch then added two more em
    dashes in the footer. A review measured the last of those raising
    `UnicodeEncodeError` on a cp437 stdout under `import run_gates;
    run_gates.main(['--list'])`, which is the imported-and-called case
    `_console_safe` documents itself as existing for.

    So the rule is not "wrap the lines that look risky" — every one of these
    lines interpolates registry or builder text, and which of them can carry a
    non-ASCII character changes whenever a `summary`, a `ci_only_reason` or a
    `display` is edited.

    **The invariant is stated over the whole module, not over a list of
    functions, because three successive lists were each wrong.** Naming the
    guarded functions here is what failed: this docstring once excused
    `run_gate` as "reports a step's own captured output, where `replay_tail`
    already does this work" — true of the failure banner, never true of the
    gate-start banner — and when `run_gate` was added, `run_step`'s
    `$ {step.shown()}` line one call further down was still bare. Reviewers
    measured a crash at each. So the claim made here is deliberately narrow:
    *this function* applies the guard, and
    `test_every_console_write_in_the_runner_is_encoding_guarded` is what asserts
    that no `print` or `sys.std*.write` anywhere in this module emits text that
    did not pass through `_console_safe`. That question is asked of the AST, so
    a new writer is covered without anyone remembering to extend a list.

    Every console write here is a call to this function, with one deliberate
    exception that the invariant above still covers: `run_step`'s `--verbose`
    echo writes raw, because it reproduces a child's output byte for byte and
    this function appends a newline. It wraps its own argument instead.

    `flush` exists because ordering matters for an announcement: a step's
    `$ command` line precedes a child process that writes to the same terminal.
    It is pinned by `test_say_flushes_when_asked_and_not_otherwise` — dropping it
    left every console double in the suite indifferent, a `StringIO` behaving
    the same flushed or not.
    """
    target = sys.stdout if stream is None else stream
    print(_console_safe(line, target), file=target, flush=flush)


def replay_tail(log: Path, lines: int | None = None) -> None:
    """Put the end of a suppressed step's output back on the console.

    The compensating half of the quiet default, and it is called from the same
    function that does the suppressing so the two cannot drift apart. Written to
    stdout, which is where the line went before it was suppressed; the failure
    banner around it stays on stderr, exactly as it was.

    How much was withheld is named rather than implied. A tail that silently
    starts mid-traceback reads as the whole failure, and the path on the last
    line is what makes the rest recoverable.

    ``lines`` resolves here rather than in the signature, for the reason
    `prune_scratch_dirs` states about its own two settings: a default argument
    binds its value once at import, so a test that set `FAILURE_TAIL_LINES` and
    then called this would get 40 lines and believe it had asked for its own
    number.

    Bounded as it reads, rather than by slicing the whole file afterwards. The
    pytest gate's capture is the longest thing here — ~500 KB on this
    repository — and pulling all of it into memory to print the last 40 lines is
    work the `deque` does not do.
    """
    lines = FAILURE_TAIL_LINES if lines is None else lines
    total = 0
    tail: deque[str] = deque(maxlen=max(lines, 0))
    try:
        with log.open(encoding="utf-8", errors="replace") as captured:
            for line in captured:
                total += 1
                tail.append(line)
    except OSError as exc:
        # Not fatal: the step's exit code has already been decided, and losing
        # the replay must not turn a legible failure into an unhandled one.
        _say(f"  (could not read the captured output at {log}: {exc})", flush=True)
        return
    omitted = total - len(tail)
    suffix = f", {omitted} earlier line(s) in the file" if omitted else ""
    _say(f"--- last {len(tail)} line(s) of output{suffix} ---", flush=True)
    for line in tail:
        _say(line.rstrip("\n"))
    _say(f"--- full output: {log} ---", flush=True)


def run_step(
    step: Step, workflow_env: dict[str, str] | None = None, *, echo: bool
) -> int:
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
    #
    # `echo` governs the captured branch only. An uncaptured child inherits this
    # process's stdout, so its output reaches the console whatever this says —
    # a step that somehow arrives here without a sink is noisy, never silently
    # discarded.
    #
    # Required rather than defaulted, because nothing could pin a default: the
    # one production caller passes it explicitly, so flipping `= True` to
    # `= False` left the whole suite green (measured) while changing no
    # behaviour anyone could observe. An argument with no default cannot have a
    # wrong one, and every caller now says which half of the contract it wants.
    env = {**os.environ, **(workflow_env or {}), **step.env}
    # `step.shown()` is builder-supplied text, so this line carries the same
    # class of content as the banners above it. It was the bare `print` a second
    # review round found one call below the ones that had just been routed — the
    # enumeration had moved rather than closed, which is why the invariant is now
    # checked over this module's whole AST rather than over a list of functions.
    _say(f"$ {step.shown()}", flush=True)
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

    # Write the captured output to a file, and — under `--verbose` — to the
    # console as well, reproducing CI's `| tee` for the one step that reads its
    # own log back: a failing test's traceback stays scannable by the canary
    # gate either way.
    try:
        step.capture_to.parent.mkdir(parents=True, exist_ok=True)
        sink = step.capture_to.open("w", encoding="utf-8")
    except OSError as exc:
        # Converted for the same reason the two `Popen` guards are: `main`
        # catches `GateError`, so an unwrapped `PermissionError` here — a full
        # temp volume, a denied ACL, a concurrent run that removed the
        # directory — killed the runner with a raw pathlib traceback instead of
        # reporting a gate that could not start.
        raise GateError(
            f"could not capture the output of `{step.shown()}`: {exc}"
        ) from exc
    try:
        started = subprocess.Popen(  # noqa: S603 - fixed executable, no shell
            step.argv,
            cwd=REPO_ROOT,
            env=env,
            # No inherited console. A child that asks a question — `uv`'s
            # keyring prompt, a git credential helper — writes it down the pipe
            # into the log and then blocks on fd 0, where nothing can answer it
            # and nothing is on screen to say so. Gate steps are deliberately
            # unbounded, so that is an indefinite hang behind a `$ uv run` line
            # indistinguishable from the normal two-minute test run. Under the
            # old streaming behaviour the prompt was at least visible.
            stdin=subprocess.DEVNULL,
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
        sink.close()
        raise GateError(f"could not start `{step.shown()}`: {exc}") from exc
    try:
        with started as process, sink:
            # `stdout=PIPE` above guarantees a pipe; the guard is here rather
            # than an assert because asserts are stripped under -O, and a
            # silently skipped capture would hand the canary scan an empty log
            # and call it clean.
            if process.stdout is not None:
                for line in process.stdout:
                    if echo:
                        # Guarded, and written raw rather than through `_say`:
                        # this echoes a child's output byte for byte, newlines
                        # included, where `_say` would add one of its own. It is
                        # the one console write in this module that is not a
                        # `_say` call, which is why the invariant is stated as
                        # "passes through `_console_safe`" rather than "calls
                        # `_say`". A `print(`-substring scan could never see it.
                        sys.stdout.write(_console_safe(line))
                    sink.write(line)
                if echo:
                    sys.stdout.flush()
    except OSError as exc:
        # The third site of the conversion the two above already do, and the
        # one with a consequence beyond a traceback. `main` sets `ctx.failed`
        # only on the paths it recognises, and `cleanup` sweeps `ephemeral`
        # when `failed` is false -- so an `OSError` escaping here (a full
        # volume mid-write, a failed flush on close) reads to cleanup as a
        # green run and deletes the canary worker sink, which is the evidence
        # a failure report names. Converting is what routes it through the
        # handler that sets the flag. Raised by CodeRabbit on PR #106; an
        # earlier review found the same escape and stopped at the traceback.
        raise GateError(
            f"could not write the captured output of `{step.shown()}` to "
            f"{step.capture_to}: {exc}"
        ) from exc
    code = process.returncode
    if code != 0 and not echo:
        # The sink is closed by the `with` above, so the replay reads a complete
        # file rather than whatever had been flushed.
        replay_tail(step.capture_to)
    return code


def run_gate(gate: Gate, ctx: Context) -> GateResult:
    # Through `_say`, like every other operator-facing line. This banner carries
    # a literal em dash and a registry `summary`, and it is the *first* thing
    # printed on every real run — so an imported-and-called run on a console that
    # cannot encode U+2014 crashed here before reaching any gate. Measured by a
    # reviewer against a cp437 stdout. An earlier version of `_say`'s docstring
    # excused this function wholesale as "reports a step's own captured output",
    # which is true of the failure banner below and was never true of this line.
    _say(f"\n=== {gate.name} — {gate.summary} ===", flush=True)
    steps = gate.steps(ctx)
    if not steps:
        # Reported as its own outcome rather than as a pass: a gate that
        # executed nothing has proved nothing, and folding it into the passed
        # count is the false green `Gate.steps` refuses for CI-only gates.
        _say("  (nothing to do — no work for this gate)")
        return GateResult.EMPTY

    failed = False
    for position, step in enumerate(steps, start=1):
        if failed and not step.always_run:
            continue
        if step.rebuild is not None:
            step = replace(step, argv=step.rebuild())
        if not ctx.verbose and gate.spool_output and step.capture_to is None:
            # Only where a gate has not already chosen a sink. The pytest gate
            # has, and the canary scan is built from that same path, so
            # overwriting it here would point the scan at a file nothing writes.
            step = replace(step, capture_to=ctx.step_log(gate.name, position))
        code = run_step(step, ctx.workflow_env, echo=ctx.verbose)
        if code != 0:
            _say(f"\nFAILED: {gate.name} (exit {code})", sys.stderr)
            _say(f"  command: {step.shown()}", sys.stderr)
            if step.on_failure:
                # Registry prose, so it can gain a dash at any time — which is
                # the whole argument for routing these rather than auditing them
                # per line.
                _say(f"  note: {step.on_failure}", sys.stderr)
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
    parser.add_argument(
        "--verbose",
        action="store_true",
        help=(
            "stream every step's output to the console instead of spooling it "
            "to a file (the default prints one line per command and replays a "
            "failing step's last lines). A gate that brings its own sink still "
            "writes it -- the pytest gate does, for the canary scan to read"
        ),
    )
    args = parser.parse_args(argv)

    try:
        text = CI_WORKFLOW.read_text(encoding="utf-8")
    except OSError as exc:
        _say(f"cannot read {CI_WORKFLOW}: {exc}", sys.stderr)
        return 1

    # --list and --print read commands rather than running them, so they build
    # every step in dry mode and touch no directory.
    ctx = Context(
        pins={},
        gitleaks_version=None,
        dry=args.list or args.print_only,
        verbose=args.verbose,
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
            _say("nothing to run.", sys.stderr)
            for gate in dropped:
                _say(f"  - {gate.name}: dropped by --fast", sys.stderr)
            for gate in skipped:
                _say(f"  - {gate.name}: CI-only — {gate.ci_only_reason}", sys.stderr)
            return 1

        if args.list:
            _say(render_list(ctx, selected))
            return 0

        if args.print_only:
            for gate in runnable:
                _say(f"# {gate.name}")
                # Reported inline and the enumeration continued, which is what
                # `--list` does with the same error (`render_list`'s per-gate
                # handler). Left outside any handler, one gate whose builder
                # refuses aborted the loop through `main`'s `except GateError`:
                # the commands printed so far stayed on stdout with no marker
                # that the list had been truncated, so a script or an agent
                # reading `--print` to enumerate commands silently got a
                # partial set. The two read-only branches now answer a broken
                # builder the same way.
                try:
                    steps = gate.steps(ctx)
                except GateError as exc:
                    _say(f"# ! {exc}")
                else:
                    for step in steps:
                        _say(step.shown())
            for gate in skipped:
                _say(f"# {gate.name}: CI-only — {gate.ci_only_reason}")
            return 0

        if not args.verbose and any(gate.spool_output for gate in runnable):
            # Printed once, before anything runs, rather than per step: the
            # directory is the answer to "where did the output go", and a reader
            # who never needs it pays one line. A failing step names its own
            # file again at the point of failure, where the question is asked.
            #
            # Guarded on there being something to spool, because this line is
            # what *creates* the directory. `run_gates.py containment` selects
            # the one exempt gate, so an unguarded notice built an empty
            # directory, claimed output had been captured into it, and spent a
            # retention slot evicting a real run to do it. The guard also keeps
            # a test that fakes `run_gate` out from reaching the shared temp
            # directory through this line.
            #
            # It answers "will anything spool", which is not "has anything
            # spooled": with `containment` first and exempt, a refused tree
            # returns from the loop below having written nothing here. That
            # residue is `Context.cleanup`'s to remove, and the guard is not the
            # place to fix it — the run's first failure point is not knowable
            # before the run.
            _say(
                f"Step output is captured to {ctx.scratch_dir()} "
                "(--verbose streams it instead, though a gate bringing its "
                "own sink still writes it)."
            )

        # Sequential with an early exit, not a comprehension over every gate:
        # the registry puts `containment` first and then the cheap gates ahead
        # of the slow ones (the two principles above `GATES`) precisely so a
        # refused tree, or a lint failure, does not cost a full test run first,
        # and collecting all results before checking them throws that away.
        results: dict[str, GateResult] = {}
        for gate in runnable:
            results[gate.name] = run_gate(gate, ctx)
            if results[gate.name] is GateResult.FAILED:
                # Recorded before the return so `cleanup` in the `finally`
                # keeps the working material this failure's report points at.
                ctx.failed = True
                return 1
    except GateError as exc:
        _say(f"error: {exc}", sys.stderr)
        ctx.failed = True
        return 1
    finally:
        ctx.cleanup()

    passed = [name for name, r in results.items() if r is GateResult.PASSED]
    empty = [name for name, r in results.items() if r is GateResult.EMPTY]
    # "All" only when it is true of the whole selection. Printing it above the
    # empty-gate report made the summary contradict itself: "All 12 selected
    # gate(s) passed." immediately followed by one that examined nothing.
    if empty:
        _say(f"\n{len(passed)} selected gate(s) passed.")
    else:
        _say(f"\nAll {len(passed)} selected gate(s) passed.")
    if empty:
        # Named *and* non-zero. Naming it while still exiting 0 was the false
        # green in a quieter register: `/land` reads the exit code and its rule
        # is "a failing gate stops the landing", so a markdown pathspec that
        # silently stopped matching would print "proves nothing" and land
        # anyway. The only gate that can be empty here is markdown-lint, and it
        # is empty only when `git ls-files` matches no tracked *.md — which in
        # this repository means the query broke, not that there is no markdown.
        _say(f"{len(empty)} gate(s) had nothing to run — they prove nothing:")
        for name in empty:
            _say(f"  - {name}")
        _say("Exiting non-zero: a gate that examined nothing is not a pass.")
        return 1
    if skipped:
        _say(f"Skipped {len(skipped)} CI-only gate(s) — this is not full CI green:")
        for gate in skipped:
            _say(f"  - {gate.name}: {gate.ci_only_reason}")
    return 0


if __name__ == "__main__":
    # Inside the `__main__` guard on purpose; scripts/check_spec_links.py
    # documents the measured reasons, and they govern. Needed here because the
    # gate summaries and ci.yml step names contain em dashes.
    for stream in (sys.stdout, sys.stderr):
        if isinstance(stream, io.TextIOWrapper):
            stream.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main())
