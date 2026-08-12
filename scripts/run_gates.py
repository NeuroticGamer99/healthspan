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
  *including* the canary scan, by setting ``CANARY_CAPTURE_DIR`` exactly as CI
  does. A local run that skipped the canary scan would be one of the partial
  greens described above.
* **containment runs ``--scope branch``**, where CI runs ``--scope history``.
  The local question is "is what I am about to commit clean"; the history walk
  is CI's backstop and needs the full-depth checkout CI gives it. The two are
  the same gate, which is why the registry maps this entry onto CI's step and
  the drift test compares gate *identity* rather than argv.

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
from dataclasses import dataclass, field
from pathlib import Path

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


class GateError(RuntimeError):
    """A gate could not be built or run — distinct from a gate that failed."""


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

    def shown(self) -> str:
        return self.display or " ".join(self.argv)


# A gate's steps are built lazily: the markdown gate asks git for its file list
# and the test gate needs a scratch directory, neither of which should happen at
# import time or when only --list was asked for.
Builder = Callable[["Context"], list[Step]]


@dataclass
class Context:
    pins: dict[str, str]
    gitleaks_version: str | None
    # Created on first use; None while only listing/printing.
    scratch: Path | None = None

    def scratch_dir(self) -> Path:
        if self.scratch is None:
            self.scratch = Path(tempfile.mkdtemp(prefix="run_gates-"))
        return self.scratch


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
    version_pin: str | None = None  # which *_VERSION governs it, for --list

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


def _tracked_markdown() -> list[str]:
    """CI's own file set: tracked plus untracked-but-not-ignored ``*.md``."""
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
    )
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
        Step(
            [
                _UV,
                "run",
                "--with",
                f"pyright=={pyright}",
                "--with",
                f"pytest=={pytest}",
                "pyright",
            ],
            f'uv run --with "pyright=={pyright}" --with "pytest=={pytest}" pyright',
        )
    ]


def _pytest(ctx: Context) -> list[Step]:
    pytest = require(ctx.pins, "PYTEST_VERSION")
    xdist = require(ctx.pins, "PYTEST_XDIST_VERSION")
    scratch = ctx.scratch_dir()
    log = scratch / "pytest-output.log"
    canary_dir = scratch / "canary-logs"
    # CI sweeps a stale canary dir before the run; a fresh scratch directory per
    # invocation makes that impossible here, so there is nothing to sweep.
    canary_dir.mkdir(parents=True, exist_ok=True)
    return [
        Step(
            [
                _UV,
                "run",
                "--with",
                f"pytest=={pytest}",
                "--with",
                f"pytest-xdist=={xdist}",
                "pytest",
                "-v",
                "-n",
                "auto",
                "--log-level=DEBUG",
            ],
            f'uv run --with "pytest=={pytest}" --with "pytest-xdist=={xdist}" '
            "pytest -v -n auto --log-level=DEBUG",
            env={"CANARY_CAPTURE_DIR": str(canary_dir)},
            capture_to=log,
        ),
        Step(
            _canary_argv(log, canary_dir),
            _python_display("scan_log_canary.py", "<pytest log>", "<worker logs>"),
        ),
    ]


def _canary_argv(log: Path, canary_dir: Path) -> list[str]:
    # The worker sink creates files lazily, so the glob is expanded after the
    # test step has run — see run_gate, which rebuilds this step's argv.
    return _python(
        "scan_log_canary.py",
        str(log),
        *sorted(str(p) for p in canary_dir.glob("*.log")),
    )


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
            ]
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
        build=_docs_gate("check_spec_links.py"),
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
        summary="reviewer agent files obey ADR-0068's isolation rules",
        ci_steps=("Check the reviewer agent files obey ADR-0068's isolation rules",),
        build=_docs_gate("check_reviewer_agents.py"),
    ),
    Gate(
        name="doc-citations",
        job="docs",
        summary="citing skills still point at the documents owning their rules",
        ci_steps=(
            "Check the citing skills still point at the documents that own "
            "their rules (ADR-0073)",
        ),
        build=_docs_gate("check_doc_citations.py"),
    ),
    Gate(
        name="containment",
        job="secrets",
        summary="no personal-data path escapes specs/personal/ (branch scope)",
        ci_steps=("Scan full git history for personal-data paths",),
        build=_docs_gate("check_personal_containment.py", "--scope", "branch"),
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
        "Install project (locked)",  # appears in two jobs
        "All gates passed",  # the ci-ok aggregate
    }
)

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
    return "—"


def render_list(ctx: Context) -> str:
    lines = ["Gates derived from .github/workflows/ci.yml", ""]
    width = max(len(gate.name) for gate in GATES)
    for gate in GATES:
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


def run_step(step: Step) -> int:
    env = {**os.environ, **step.env}
    print(f"$ {step.shown()}", flush=True)
    if step.capture_to is None:
        completed = subprocess.run(  # noqa: S603 - fixed executable, no shell
            step.argv, cwd=REPO_ROOT, env=env, check=False
        )
        return completed.returncode

    # Reproduce CI's `| tee`: stream to the console and to a file at once, so a
    # failing test's traceback is visible *and* scannable by the canary gate.
    step.capture_to.parent.mkdir(parents=True, exist_ok=True)
    with (
        subprocess.Popen(  # noqa: S603 - fixed executable, no shell
            step.argv,
            cwd=REPO_ROOT,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        ) as process,
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


def run_gate(gate: Gate, ctx: Context) -> bool:
    print(f"\n=== {gate.name} — {gate.summary} ===", flush=True)
    steps = gate.steps(ctx)
    if not steps:
        print("  (nothing to do)")
        return True
    for index, step in enumerate(steps):
        # The canary step's argv depends on files the test step creates, so it
        # is rebuilt once its predecessor has run rather than at build time.
        if gate.name == "pytest" and index == 1:
            scratch = ctx.scratch_dir()
            step = Step(
                _canary_argv(scratch / "pytest-output.log", scratch / "canary-logs"),
                step.display,
            )
        code = run_step(step)
        if code != 0:
            print(f"\nFAILED: {gate.name} (exit {code})", file=sys.stderr)
            print(f"  command: {step.shown()}", file=sys.stderr)
            return False
    return True


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
        help="show every gate, its pinned version, and the exact command it runs",
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

    try:
        ctx = Context(
            pins=read_pins(text), gitleaks_version=read_gitleaks_version(text)
        )

        if args.list:
            print(render_list(ctx))
            return 0

        selected = resolve(args.selectors) if args.selectors else list(GATES)
        if args.fast:
            selected = [
                gate for gate in selected if gate.name not in {"pytest", "pip-audit"}
            ]

        runnable = [gate for gate in selected if gate.local]
        skipped = [gate for gate in selected if not gate.local]

        if args.print_only:
            for gate in runnable:
                print(f"# {gate.name}")
                for step in gate.steps(ctx):
                    print(step.shown())
            for gate in skipped:
                print(f"# {gate.name}: CI-only — {gate.ci_only_reason}")
            return 0

        if not runnable:
            print("nothing to run: every selected gate is CI-only.", file=sys.stderr)
            for gate in skipped:
                print(f"  - {gate.name}: {gate.ci_only_reason}", file=sys.stderr)
            return 1

        for gate in runnable:
            if not run_gate(gate, ctx):
                return 1
    except GateError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(f"\nAll {len(runnable)} selected gate(s) passed.")
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
