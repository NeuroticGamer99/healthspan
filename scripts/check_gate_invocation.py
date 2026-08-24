#!/usr/bin/env python3
"""PreToolUse hook: refuse local gate invocations that cannot work (RUNG-2).

``scripts/run_gates.py`` deleted the second copy of CI's gate commands. It did
not stop anyone reaching for the *first* copy from memory, and this repository
measured that happening twice in one session: ``uv run ruff`` was typed
unprompted (it fails in 0.42 s, exit 2, because ruff is not a project
dependency), and the full test suite was run serially until the 600 s command
timeout killed it, where ``-n auto`` finishes in ~112 s. Documentation at the
point of need did not prevent either — ``/land`` carried the ``-n auto``
instruction *with its reason* and it was not read.

So this hook refuses four command shapes, all of which are wrong rather than
merely suspicious:

1. ``uv run <tool>`` for a tool that is not a project dependency — exit 2,
   "Failed to spawn".
2. A bare ``<tool>`` for the same set — exit 127, not on PATH.
3. ``uvx <tool>`` with no ``@version`` for a tool ci.yml pins. This one is the
   dangerous member: it *succeeds*, against whatever is latest, and reports a
   confident green from a version CI does not run. Measured 2026-08-13,
   ``uvx ruff`` resolved 0.16.3 against ci.yml's pinned 0.15.21.
4. A full-suite ``pytest`` with no ``-n`` — the 600 s timeout above.

**The typed word is not the tool.** ``uv tool run ruff`` is ``uvx ruff``,
``python -m pytest`` is ``pytest``, ``uvx ruff@latest`` is an unpinned
``uvx ruff`` wearing the ``@`` of the correct form, and ``sudo``/``env``/
``nohup``/``xargs`` and their family all hand the trailing words to a shell.
Each is normalised to the executed tool before the rules run, so the four
shapes above stay four rules rather than growing a row per spelling.

**The dialect is not assumed.** The payload names the tool, and this hook is
registered for PowerShell as well as Bash — two shells that disagree about the
two characters this parser cares most about. A backslash is an escape in one
and an ordinary path separator in the other; a backtick is a command
substitution in one and an escape in the other. Reading PowerShell with bash
rules did both harms at once: it let ``.\\ruff.exe check .`` past every rule and
denied ``Write-Host "use `pytest` here"``, which runs nothing at all.

**Why deny rather than warn.** A PreToolUse hook that exits 0 shows the model
nothing (debug log only), and the model is the party reaching for the wrong
command, so a "warning" in that form is addressed to nobody. The only shape
that both warns and proceeds is ``permissionDecision: "allow"`` with
``additionalContext`` — and ``allow`` auto-approves, so warning would *cost* the
permission prompt the command would otherwise get. Denying loses nothing:
every shape above already fails, wastes ten minutes, or lies. ADR-0077 records
this and the reasoning behind it.

**Nothing here restates a tool list.** The tools are derived from
``run_gates.py``'s own registry by building each local gate and reading the
commands it produces: a ``uvx tool@version`` argument names a pinned uvx tool, a
``uv run --with tool==version`` names an ephemeral one, and subtracting
``pyproject.toml``'s dev dependency group leaves exactly those a bare or
``uv run`` invocation cannot find. When ci.yml gains a pinned tool, this hook
covers it with no edit here — the same property that makes the runner worth
having. The derivation found ``pip-audit`` on its first run, which the work item
scoping this hook had not listed.

**The one restatement, and why it is unavoidable.** ``pymarkdownlnt`` is the
*package* name; its console script is ``pymarkdown``. Nothing in ci.yml or
pyproject.toml records that mapping — it lives in the installed package's
metadata, which is not available to a hook that must not import the world — so
``_CONSOLE_SCRIPTS`` states it. It is one entry, it is tested, and a wrong entry
fails open (a missed deny) rather than closed.

**Applying it once is not enough, and that is the part that was got wrong.** A
typed name reaches the derived tool set as a ``uv run`` argument, a ``python
-m`` module, a ``uvx`` spec, or a bare command word — and the bare word did not
map. So ``uv run pymarkdownlnt``, ``python -m pymarkdownlnt`` and ``uvx
pymarkdownlnt`` were all refused while a bare ``pymarkdownlnt`` was allowed and
exited 127 — the *package* spelling, the one ``ci.yml``, ``pyproject.toml`` and
ADR-0062 use to name the tool they install or run, so the likelier of the two to
be reached for from memory. (All three also write ``pymarkdown``, as the
``[tool.pymarkdown]`` config table — a spelling nobody types at a shell.)

**Count the call sites, not those four shapes.** The two do not correspond, in
both directions: ``python -m`` maps at two separate ``return``\\ s (the attached
``-mfoo`` spelling and the spaced one), while ``uv tool run`` maps at none of
its own, being rewritten to ``uvx`` before any resolution happens. Five sites,
then, and ``test_both_spellings_of_a_tool_are_refused_at_every_call_site``
enumerates them by hand so that an omission is a failure rather than an absence.
Three of the five turned out to need it: the bare word, which was missing the
call outright, and then ``unwrap_python_module``'s attached ``-m`` return and
``parse_uv_run``, which had the call and nothing testing it — the mapping could
have been deleted at either and the suite would have stayed green. A site added
below inherits the same obligation.

**Fail-open is deliberate, and made visible.** A hook that cannot derive its
facts — or that hits an unexpected bug — allows the command and tells the user
why, rather than blocking a session on its own defect. That converts the
residual failure from "silently stopped firing" into "visibly absent", which is
the same trade ``run_gates.py`` makes for a gate it cannot run.

**Fail-open only covers code that runs.** If the registered command cannot
*launch* this script, nothing here executes and nothing can be emitted — this
module's whole fail-open design is unavailable, and whether the harness then
blocks the call or merely reports the error is not something this repository
has established (see ``specs/open-questions.md``). Either way it is not a
tolerable failure mode, so it is prevented rather than handled: the hook is
registered as an inline bootstrap that always starts and looks for this file in
exactly two places — under the root of the cwd's repository, then under
``CLAUDE_PROJECT_DIR`` — staying silent when neither has it. ADR-0077 §5 records
the worktree session that established the distinction:
``${CLAUDE_PROJECT_DIR}`` names the *primary* checkout, so a worktree's hook
could not open its own script.
"""

from __future__ import annotations

import json
import re
import shlex
import sys
from collections.abc import Generator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING, cast

sys.path.insert(0, str(Path(__file__).resolve().parent))

if TYPE_CHECKING:
    import run_gates


def _load_run_gates() -> ModuleType:
    """Import ``run_gates``, deferred so its failures are *this* hook's to catch.

    This was a module-level ``import run_gates``, which is outside every guard
    in the file: an import error there kills the process before ``main`` exists,
    so a broken or missing ``run_gates.py`` made the hook exit non-zero with a
    traceback on **every** shell call — precisely the launch-failure class
    ADR-0077 §5 says must not be reachable, arrived at from the other side.
    Measured: writing ``def broken(:`` into ``run_gates.py`` and running the
    registered bootstrap returned exit 1 with an empty stdout, and the next Bash
    call a session would use to *find* that error is the one that fails.

    Deferring it also means the ~60 ms import is not paid by a command that
    never reaches derivation. That is the real cost on this path — the
    derivation itself measures ~2 ms — and the three ``shutil.which`` PATH scans
    a review attributed to derivation actually happen here, at import.

    ``import`` is cached in ``sys.modules``, so repeat calls are free and every
    caller sees the same module object a test may have monkeypatched.
    """
    import run_gates as module

    return module


REPO_ROOT = Path(__file__).resolve().parent.parent
PYPROJECT = REPO_ROOT / "pyproject.toml"
RUNNER = "scripts/run_gates.py"

# Package name -> console script, for the cases where they differ. See the
# module docstring: this is the one fact that is not derivable from a file in
# this repository. Keep it minimal — an entry here is a copy, with a copy's
# obligations.
_CONSOLE_SCRIPTS = {"pymarkdownlnt": "pymarkdown"}

# The distribution name leading any spec this module reads — `ruff@0.15.21`,
# `ruff>=0.15`, a bare `ruff`. One grammar, because four sites were asking the
# same question with three spellings (`^(...)@`, `^(...)`, and two separate
# `partition("@")` calls on the same string), and this module has already been
# bitten by "two hand-rolled loops over the same argument shape drifted apart".
_SPEC_NAME_RE = re.compile(r"^([A-Za-z0-9_.\-]+)")

# Version words that carry an `@` without pinning anything. `latest` is the
# dangerous member: it wears the same `@` the correct form does, so reading the
# bare presence of the character as "pinned" waves through the exact shape rule
# 3 exists to catch — a confident green from whatever version is newest. An
# empty version (`ruff@`) is the same hole with a typo in front of it.
_UNPINNED_VERSIONS = frozenset({"", "latest"})

# Deliberately not tied to `==`: `--with ruff>=0.15`, `--with ruff~=0.15` and a
# bare `--with ruff` all genuinely supply the tool, and reading only the pinned
# form denied three working commands with the false reason "uv cannot spawn it".
# That requirement is served by `_SPEC_NAME_RE` above, which stops at the first
# character that cannot be part of a distribution name — `@`, `>`, `~` alike.

# Options whose *next* token is a value rather than the command word. Getting
# this wrong is not cosmetic: `uv run -p 3.12 ruff check .` read `-p`'s value as
# the tool and returned without ever inspecting `ruff`, which defeated both the
# ephemeral-tool rule and the serial-pytest rule with one ordinary flag. One
# shared set per program, read by every function that walks these arguments —
# the previous code spelled the set three times and two spellings disagreed.
_UV_RUN_VALUE_FLAGS = frozenset(
    {
        "--with",
        "--with-editable",
        "--with-requirements",
        "--python",
        "--directory",
        "--project",
        "--extra",
        "--group",
        "--no-group",
        "--only-group",
        "--index",
        "--index-url",
        "--extra-index-url",
        "--find-links",
        "--constraint",
        "--override",
        "--exclude-newer",
        "--refresh-package",
        "--cache-dir",
        "--config-file",
    }
)

# The subset of the above that names a package this run supplies for itself.
_UV_RUN_SUPPLY_FLAGS = frozenset({"--with", "--with-editable"})

# A supply flag whose value names packages this hook cannot read: a requirements
# file, or an editable install pointed at a directory. The right response is to
# stop claiming the tool is missing — see `UvRun.supplies_unknown`.
_UV_RUN_OPAQUE_SUPPLY_FLAGS = frozenset({"--with-requirements"})

_UVX_VALUE_FLAGS = _UV_RUN_VALUE_FLAGS | {"--from"}

# Short options are single characters so a cluster (`-vp no:cacheprovider`) can
# be walked one character at a time. Matching whole tokens against a set of
# spellings cannot see a value-taking short flag hiding behind a boolean one,
# which was measured allowing a full serial pytest run through `-vp` and
# denying a properly scoped one through `-vkcanary`.
#
# Read from `uv run --help` (uv 0.11.28): -C, -P, -f, -i, -p, -w.
_UV_RUN_VALUE_SHORTS = frozenset("CPfipw")
# `-w` is the short spelling of `--with`, so it supplies a package too.
_UV_RUN_SUPPLY_SHORTS = frozenset("w")
_UVX_VALUE_SHORTS = _UV_RUN_VALUE_SHORTS

# pytest: -p/-o/-c/-W/-r take a value; -n/-k/-m mean the run is not the whole
# suite (and -m takes a value, which is why scoping is checked first).
_PYTEST_VALUE_SHORTS = frozenset("pocWr")
# `h` and `V` are here because `-h` and `-V` are in `_PYTEST_SCOPING_FLAGS` and
# a single-dash token never reaches that set — it goes to the cluster walker.
# Without them, `pytest -h` was denied for "exceeding the 600 s timeout": a
# command that prints help in under a second, refused with a reason that could
# not be true of it. `test_every_short_spelling_of_a_long_flag_is_reachable`
# now derives this correspondence rather than trusting it.
_PYTEST_SCOPING_SHORTS = frozenset("nkmhV")

# Options whose *value* decides whether they scope anything. xdist's `-n 0`
# means "no distribution" — an in-process serial run, the exact shape killed at
# 600 s — so reading the flag's presence as scoping waved it straight through.
# Measured allowed: `pytest -n 0`, `pytest -n0`, `pytest --numprocesses=0`.
_PYTEST_PARALLEL_FLAGS = frozenset({"-n", "--numprocesses"})
_PYTEST_SERIAL_VALUES = frozenset({"0"})

# pytest options that mean "this run is deliberately not the whole suite":
# either it selects a subset, or it parallelises, or it does not run tests.
#
# `--deselect` is deliberately absent. It *subtracts* from a full run rather
# than selecting a subset, so `pytest --deselect one::test` is the whole serial
# suite minus one test — measured allowed, and it is the 600 s case.
_PYTEST_SCOPING_FLAGS = frozenset(
    {
        "-n",
        "--numprocesses",
        "-k",
        "-m",
        "--lf",
        "--last-failed",
        "--ff",
        "--failed-first",
        "--sw",
        "--stepwise",
        "--co",
        "--collect-only",
        "--fixtures",
        "--markers",
        "--help",
        "-h",
        "--version",
        "-V",
    }
)

# pytest options whose *next* token is a value, not a test path.
# Long forms only: a single-dash token never reaches this set, because
# `_pytest_is_full_suite` routes it to the cluster walker instead. `-p`, `-o`,
# `-c`, `-W` and `-r` lived here until a set-content assertion caught them
# sitting unreachable, which is the shape of config that reads as cover and is
# not. Their short spellings are in `_PYTEST_VALUE_SHORTS`.
_PYTEST_VALUE_FLAGS = frozenset(
    {
        # Moved here from the scoping set, where it did two wrong things at
        # once: it marked the run scoped, and its node-id value was read as a
        # positional test path — so removing it from scoping alone still left
        # `pytest --deselect a::b` allowed, by the second route.
        "--deselect",
        "--override-ini",
        "--tb",
        "--junitxml",
        "--junit-xml",
        "--rootdir",
        "--basetemp",
        "--ignore",
        "--ignore-glob",
        "--maxfail",
        "--capture",
        "--durations",
        "--import-mode",
        "--confcutdir",
        "--dist",
        "--tx",
        "--log-level",
        "--log-cli-level",
        "--log-file",
        "--log-format",
        "--color",
    }
)

# The two shells this hook is registered for. They differ lexically in ways
# that change the verdict — `\` and `` ` `` mean opposite things — so the
# parser takes one rather than assuming bash. See `_scan_shell`.
BASH = "bash"
POWERSHELL = "powershell"

# Each dialect's escape character. Naming it once is what lets the line
# continuation be handled in one place: both shells spell it "escape + newline"
# and both *remove* it, and handling only bash's spelling left PowerShell's
# stray backtick in argv, where it read as a pytest test path.
_ESCAPE = {BASH: "\\", POWERSHELL: "`"}

# Emitted where a substitution or process substitution was removed. It has to
# be a *word*, not the space it used to be: `pytest $(git diff --name-only)`
# collapsed to a bare `pytest`, which `_pytest_is_full_suite` then read as an
# unscoped whole-suite run and **denied** — a scoped run refused with a reason
# ("exceeds the 600 s timeout") that is false of it. A word keeps the argument
# visible as an argument without claiming to know what it expands to. Chosen so
# no rule can match it and no shell would produce it.
_SUBSTITUTION_PLACEHOLDER = "substituted"

# Claude Code's tool name -> the dialect that tool's command string is written
# in. Anything unlisted is treated as bash.
#
# **That default is not uniformly conservative, and saying so was wrong.** It
# over-inspects on the substitution axis — bash scans `` ` `` and `$()`, so an
# unknown tool yields *more* commands there — and under-inspects on the path
# axis, because bash consumes `\` as an escape: `.\ruff.exe` lexes to
# `.ruff.exe` and matches no rule. Measured both ways. For bash that is the
# correct reading, since bash really would look for a file called `.ruff.exe`;
# the residual is that a *third*, unmapped tool whose shell treats `\` as a
# separator would be under-inspected until it is added here. That fails open,
# which ADR-0077 §5 accepts, but it is a gap rather than a safety margin.
_SHELL_BY_TOOL = {"PowerShell": POWERSHELL}


# Commands whose trailing arguments are themselves a command. Each was measured
# hiding a full serial pytest run, and every one is an ordinary thing to type.
# `time` is handled by `_LEADING_KEYWORDS` instead, being a shell keyword.
#
# **Only the bare form is unwrapped, and that is a deliberate retreat.** An
# earlier version modelled each wrapper's own option grammar — which flags take
# a separate value, which merely describe, how many positionals the wrapper
# owns — and it produced a defect in each of four consecutive review rounds:
# a missing value flag ate the command word; an *optional*-argument flag
# (`xargs -e[END]`) ate it a different way; `sudo -h` turned out to be `--help`
# and was denied as a false positive; and the test written to prove that fix
# could not fail. The grammar varies by implementation too — this machine's
# `sudo-rs` has no `--host` at all, while classic sudo does.
#
# So the option grammar is gone. A wrapper is unwrapped only when the very next
# token is not an option, which needs no per-tool knowledge and cannot
# misidentify the command word. Anything carrying flags falls through
# unexamined and **fails open**, the direction ADR-0077 §5 already accepts. That
# covers `sudo pytest`, `env pytest`, `exec`, `nohup`, `command` — five of the
# six shapes the finding named — and gives up `xargs -a files pytest`.
#
# Four names are deliberately absent, for three different reasons.
#
# `timeout` owns a mandatory *positional* (the duration), so `timeout 30 pytest`
# would read `30` as the command. Modelling that is the class of knowledge this
# retreat exists to stop carrying.
#
# `xargs` is further outside it still: its arguments arrive on **stdin**, which
# is not in the command text at all. `git ls-files "*_test.py" | xargs pytest`
# is a *scoped* run, and unwrapping it left a bare `pytest` that was denied for
# "exceeding the 600 s timeout" — false of a run that finishes in seconds.
# Only the empty-stdin case is genuinely a full suite, and nothing in the
# command distinguishes the two. The retreat's principle is that this hook
# stops carrying knowledge of another program's interface; `xargs` would
# require knowing another program's input stream.
#
# `su` and `watch` are absent for a separate reason — their trailing words are
# not a command list at all. ADR-0077's Negative Consequences records it; this
# is a pointer rather than a second copy, which is the convention the rest of
# this module already follows for decision content.
_WRAPPERS = frozenset(
    {
        "sudo",
        "doas",
        "env",
        "exec",
        "nohup",
        "setsid",
        "stdbuf",
        "nice",
        "command",
    }
)

# Command words that run a Python interpreter, so `-m <module>` names the tool.
_PYTHON_PROGRAMS = frozenset({"python", "python3", "py", "pypy", "pypy3"})
# A versioned spelling of the same interpreter: `python3.12`, `pyw`, `pypy3.10`,
# and the free-threaded `python3.14t`, which is a live spelling here because
# this project sets `requires-python = ">=3.14"`. Matched against an
# already-normalized (basename, lower-cased, `.exe`-stripped) word, so no
# IGNORECASE.
#
# **The `t` binds to a dotted version, and that is a correction.** It was
# written `\d*(?:\.\d+)*t?`, a free-floating optional `t` over an optional
# version, so it matched `pyt`, `pywt`, `pythont`, `py2t` and `python3t` --
# none of which is a spelling of anything. CPython's free-threaded build is
# always fully qualified (`python3.14t`, `python3.13t`), so requiring the dot
# is not a guess about the tail; it is the tail.
#
# **The undotted tail is one digit, and that is the same correction one round
# later.** It was `\d+`, so `py12`, `pypy12`, `python12` and `pyw12` all
# classified as interpreters: the `t` fix above bound the letter and left the
# number alone. Undotted is the only ambiguous position -- a two-digit
# component is a *minor* version and always follows a dot (`python3.14`) -- so
# the dotted major stays `\d+` and this requires no guess about how long
# Python's major line runs. Measured at the fail-open call site:
# `_tool_of(Step(['uv','run','py12','x.py'],''))` returned `None` before and
# `('py12', False)` after.
#
# **And over-matching is not the safe direction here**, which the comment this
# replaces asserted it was. It is at the *other* two call sites -- `classify`'s
# and `unwrap_python_module`'s -- where reading a word as an interpreter makes
# the hook look one level deeper. At `_tool_of` it *drops* the word from the
# enforced tool set, so any future gate whose console script matched the tail
# would silently stop being policed by rule 2: the fail-open direction this
# module says a blocking hook must never take. Measured:
# `_tool_of(Step(['uv','run','pyt','x.py'],''))` returned `None` where the same
# call for `ruff` returned `('ruff', False)`.
#
# The `pypy` family is in `_PYTHON_PROGRAMS` above and reachable through the
# `py` branch here for the opposite reason: missing a real interpreter puts it
# back in `facts.ephemeral` and restores the false "is not installed on this
# machine" denial this predicate exists to remove. A false positive is how a
# hook gets switched off.
_PYTHON_PROGRAM_RE = re.compile(r"(?:pypy|python|py)w?(?:\d+(?:\.\d+)+t?|\d)?\Z")


def _is_python_program(word: str | None) -> bool:
    """Whether `word` spells a Python interpreter, however it was written.

    One predicate for a question asked at three sites, each of which tested raw
    membership of `_PYTHON_PROGRAMS` against an un-normalized command word --
    so `python.exe`, `PYTHON` and `python3.12` answered no at all three.
    Measured before this existed, with the docs gate on `uv run python`:
    `_tool_of(['uv','run','python.exe','scripts/x.py'])` returned
    `('python.exe', False)`, putting an interpreter back into `facts.ephemeral`
    and restoring the denial of every bare `python scripts/run_gates.py` with
    the false reason "python is not installed on this machine". `PYTHON` and
    `python3.12` did the same.

    The three agreed only by arithmetic -- `_tool_of` had stopped emitting the
    one spelling they all recognised -- rather than by a shared rule, which is
    what "past two sites, prefer one named mechanism" is about.

    Normalization is `program_name`'s, so this inherits its limit: it strips
    `.exe` and not `.bat` or `.cmd`. Widening that belongs in `program_name`,
    where it would change every tool's matching rather than only this one's.
    """
    if not word:
        return False
    stem = program_name(word)
    return stem in _PYTHON_PROGRAMS or bool(_PYTHON_PROGRAM_RE.fullmatch(stem))


# Interpreter options whose *next* token is a value, not the script or module.
# Same failure shape as the uv and pytest sets above: miss one and the walker
# reads its value as a positional and stops before it reaches `-m`.
_PYTHON_VALUE_FLAGS = frozenset({"-X", "-W", "--check-hash-based-pycs"})

# The same options in their clustered spelling: `-Wignore`, `-uX dev`. A value
# option ends the cluster walk, because what follows belongs to it rather than
# being another flag.
_PYTHON_VALUE_SHORTS = frozenset("XW")

# Leading `FOO=bar` assignments, which precede the real command word.
_ASSIGNMENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")

# Shell operators that separate one command from the next. Splitting on these
# is what makes `cd x && uv run ruff` reachable; checking only the first word of
# the whole string would miss every chained invocation.
#
# `\n` is deliberately absent. It was listed here and was dead: shlex keeps the
# newline in `lexer.whitespace`, so it is never emitted as a token and the
# member could never match — every line but the first went unclassified.
# `_scan_shell` now rewrites an *unquoted* newline to `;` before lexing, which
# is where a line break can actually be told apart from one inside a quote.
#
# Redirect operators are deliberately **not** here. They were, and it made the
# redirect *target* a command word: `echo 'a' > pytest` was denied as a pytest
# run. `_consume_redirect` drops the operator with its target before lexing,
# which is also where the `2` in `2>&1` stops looking like a test path.
_SEPARATORS = {"&&", "||", "|", ";", "&", "(", ")", ";;", "|&"}

# Words that introduce a command rather than being one. `split_commands` breaks
# on operators, so the fragment after `then`/`do` starts with the keyword and
# `program_name(argv[0])` reads `then` — matching no rule, which let every
# denied shape through when wrapped in the most ordinary control flow.
# `strip_leading_noise` handles both prefixes in one pass, since either may
# precede the other.
_LEADING_KEYWORDS = frozenset(
    {
        "if",
        "then",
        "elif",
        "else",
        "fi",
        "do",
        "done",
        "while",
        "until",
        "for",
        "case",
        "esac",
        "select",
        "function",
        "time",
        "!",
        "{",
        "}",
    }
)


class DerivationError(RuntimeError):
    """The hook could not work out what to police. Allows, and says so."""


@dataclass(frozen=True)
class Facts:
    """What the hook needs to know, all of it derived from the repository."""

    # Tools ci.yml pins behind `uvx tool@version`.
    pinned_uvx: frozenset[str] = frozenset()
    # Tools no bare or `uv run` invocation can find, keyed by the name a person
    # would actually type (console script, not package).
    ephemeral: frozenset[str] = frozenset()
    # tool -> the run_gates selector that runs it properly.
    selector: dict[str, str] = field(default_factory=dict[str, str])


@dataclass(frozen=True)
class Violation:
    tool: str
    reason: str

    def remedy(self, facts: Facts) -> str:
        selector = facts.selector.get(self.tool, "")
        target = f"{RUNNER} {selector}".strip()
        return (
            f"{self.reason}\n\n"
            f"Use the local gate runner instead:  python3 {target}\n"
            # "how each one runs", not "the exact command": `markdown-lint` and
            # `pytest`'s canary step render a placeholder (`<N files>`,
            # `<pytest log>`) rather than a pasteable argv, so telling an author
            # the listing hands them a command to paste is false for two of the
            # thirteen. Cut item C7's sweep, paid at this site because this
            # change touches this file.
            f"`python3 {RUNNER} --list` shows every gate, its derived version, "
            "and how each one runs."
        )


# --------------------------------------------------------------------------
# Derivation — everything below reads the repository, nothing restates it
# --------------------------------------------------------------------------


def dev_group_packages(text: str) -> set[str]:
    """Package names in pyproject's dev dependency group.

    These are installed in the project environment, so ``uv run <tool>`` finds
    them. pytest is the live example: it is reached in CI through
    ``--with pytest==`` for the pinned version, but it is *also* a dev
    dependency, so denying ``uv run pytest`` would be wrong.
    """
    # Deferred: `tomllib` is 3.11+, and this is the only version-sensitive
    # import in the module. At module scope it executes before any guard
    # exists, so on an interpreter older than 3.11 the hook exited 1 with a
    # traceback on *every* matched call — the launch-failure class ADR-0077
    # §5 says must not be reachable, arrived at through the interpreter
    # rather than through the path. Note the module's floor is **3.9**, not
    # lower: `dict[str, str]` as a `default_factory` (PEP 585) and
    # `str.removesuffix` are both 3.9+, and both run at import. Deferring
    # `tomllib` lowers the floor from 3.11 to 3.9; it does not remove it.
    # Deferred here it degrades to a visible
    # fail-open notice — via `evaluate`'s broad `except Exception`, not via
    # `derive_facts`'s narrower `(OSError, ValueError)`, which a
    # `ModuleNotFoundError` does not match. Both guards are load-bearing and
    # it is the outer one that catches this.
    import tomllib

    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise DerivationError(f"could not parse {PYPROJECT.name}: {exc}") from exc
    groups = data.get("dependency-groups", {})
    dev = groups.get("dev", [])
    names: set[str] = set()
    for entry in dev:
        if not isinstance(entry, str):
            continue
        # `hypothesis>=6.140` -> `hypothesis`
        name = re.split(r"[<>=!~\[;]", entry, maxsplit=1)[0].strip()
        if name:
            names.add(name)
    return names


def _console_name(package: str) -> str:
    return _CONSOLE_SCRIPTS.get(package, package)


@dataclass(frozen=True)
class Spec:
    """A package spec split into the two things this module ever asks of it."""

    # Console-script name, so callers compare against what a person types.
    name: str
    # The text after `@`, or None when the spec carried no `@` at all. The
    # distinction matters: `ruff` is unpinned, `ruff@` is a pin of nothing, and
    # only one of them should read as "no version was requested".
    version: str | None


def parse_spec(spec: str) -> Spec:
    """Split a spec into its distribution name and its `@version`, if any."""
    cleaned = spec.strip().strip('"').strip("'")
    match = _SPEC_NAME_RE.match(cleaned)
    name = match.group(1) if match else ""
    remainder = cleaned[len(name) :]
    version = remainder[1:] if remainder.startswith("@") else None
    return Spec(_console_name(name) if name else "", version)


def _requirement_name(spec: str) -> str | None:
    return parse_spec(spec).name or None


def _names_a_readable_package(spec: str) -> bool:
    """False when a supply value is a path or file this hook cannot resolve.

    ``--with-editable ./local-ruff`` installs whatever distribution lives in
    that directory, and ``--with-requirements reqs.txt`` installs whatever the
    file lists. Reading the leading characters as a distribution name gives the
    wrong answer for both, which denied two working commands.
    """
    value = spec.strip('"').strip("'")
    if not value:
        return False
    return not (
        value.startswith((".", "/", "~"))
        or "/" in value
        or "\\" in value
        or value.endswith((".txt", ".in", ".toml"))
    )


@dataclass(frozen=True)
class ShortCluster:
    """What a single-dash token such as ``-vp`` or ``-vkcanary`` asks for."""

    scoping: bool = False
    option: str | None = None
    attached: str = ""

    @property
    def consumes_next(self) -> bool:
        return self.option is not None and not self.attached


def read_short_cluster(
    arg: str,
    value_shorts: frozenset[str],
    scoping_shorts: frozenset[str] = frozenset(),
) -> ShortCluster:
    """Walk a clustered short-option token character by character.

    Stops at the first character that means something: a scoping option ends the
    question immediately, and a value-taking option takes the remainder of the
    cluster as its value, or the following token when the cluster ends there.
    """
    for index, char in enumerate(arg[1:], start=1):
        if char in scoping_shorts:
            # The character and its remainder are carried even here, because
            # some scoping options scope only for *some* values: `-n0` is the
            # serial run wearing the parallel flag, and the caller cannot tell
            # without the `0`.
            return ShortCluster(scoping=True, option=char, attached=arg[index + 1 :])
        if char in value_shorts:
            return ShortCluster(option=char, attached=arg[index + 1 :])
    return ShortCluster()


@dataclass(frozen=True)
class UvRun:
    """What a ``uv run`` argument list actually asks for."""

    # Packages the run supplies for itself via `--with`, so they *are* available
    # however the project's dependencies are configured.
    provided: frozenset[str] = frozenset()
    # True when the run supplies packages this hook cannot enumerate — a
    # requirements file, or an editable path. The missing-tool rule then has no
    # grounds to fire, because the tool may well be among them.
    supplies_unknown: bool = False
    # The command word uv will execute, console-script name.
    executed: str | None = None
    # Everything after the command word — the tool's own arguments.
    rest: tuple[str, ...] = ()


def parse_uv_run(args: Sequence[str]) -> UvRun:
    """Walk ``uv run``'s arguments, honouring options that take a value."""
    provided: set[str] = set()
    opaque = False
    pending: str | None = None

    def supply(value: str) -> None:
        nonlocal opaque
        if not _names_a_readable_package(value):
            opaque = True
            return
        name = _requirement_name(value)
        if name:
            provided.add(name)
        else:
            opaque = True

    for index, arg in enumerate(args):
        if pending is not None:
            if pending in _UV_RUN_OPAQUE_SUPPLY_FLAGS:
                opaque = True
            elif pending in _UV_RUN_SUPPLY_FLAGS:
                supply(arg)
            pending = None
            continue
        if arg.startswith("--"):
            name, sep, value = arg.partition("=")
            if sep:
                if name in _UV_RUN_OPAQUE_SUPPLY_FLAGS:
                    opaque = True
                elif name in _UV_RUN_SUPPLY_FLAGS:
                    supply(value)
                continue
            if name in _UV_RUN_VALUE_FLAGS or name in _UV_RUN_OPAQUE_SUPPLY_FLAGS:
                pending = name
            continue
        if arg.startswith("-") and len(arg) > 1:
            cluster = read_short_cluster(arg, _UV_RUN_VALUE_SHORTS)
            if cluster.option in _UV_RUN_SUPPLY_SHORTS:
                if cluster.attached:
                    supply(cluster.attached)
                else:
                    pending = "--with"
            elif cluster.consumes_next:
                pending = "-"
            continue
        return UvRun(
            provided=frozenset(provided),
            supplies_unknown=opaque,
            # `program_name` before `_console_name`, the same order and for the
            # same reason as `classify`'s own first line: a path- or
            # extension-bearing word is not yet a bare name when the mapping
            # runs. It was missing entirely here, so `executed` came back as
            # whatever was typed -- measured, `parse_uv_run(['pytest.exe','-v'])
            # .executed == 'pytest.exe'` and `['./pytest','-v']` gave
            # `'./pytest'`.
            #
            # Every comparison downstream is against a bare name, so all of them
            # missed together: `tool in facts.ephemeral`, `tool == "pytest"`
            # guarding the serial-full-suite denial, and the interpreter
            # question. Only the last had been repaired, inside
            # `_is_python_program` -- which is normalizing at the *predicate*
            # while its own docstring argues that past two sites you prefer one
            # named mechanism. This is that mechanism, at the source, and it
            # repairs the other two comparisons as a consequence rather than by
            # being extended to them.
            executed=_console_name(program_name(arg)),
            rest=tuple(args[index + 1 :]),
        )
    return UvRun(provided=frozenset(provided), supplies_unknown=opaque)


def parse_uvx_target(args: Sequence[str]) -> str | None:
    """The package spec uvx will resolve — the thing that carries the version.

    ``--from`` is the case that makes this more than "the first positional":
    in ``uvx --from ruff@0.15.21 ruff check .`` the pin lives on ``--from`` and
    the positional is merely the console script to run. Reading the positional
    denied that correctly pinned command.
    """
    pending: str | None = None
    for arg in args:
        if pending is not None:
            if pending == "--from":
                return arg
            pending = None
            continue
        if arg.startswith("--"):
            name, sep, value = arg.partition("=")
            if sep:
                if name == "--from":
                    return value
                continue
            if name in _UVX_VALUE_FLAGS:
                pending = name
            continue
        if arg.startswith("-") and len(arg) > 1:
            if read_short_cluster(arg, _UVX_VALUE_SHORTS).consumes_next:
                pending = "-"
            continue
        return arg
    return None


@contextmanager
def _cheap_file_discovery() -> Generator[None]:
    """Stop the markdown gate asking git for the repository's file list.

    The hook needs each gate's *command shape*, never its arguments, and
    ``_markdown_lint`` is the only builder that shells out to populate them —
    measured at 141 ms of the 149 ms a full derivation cost, on a hook that runs
    before every Bash call. Everything else builds in under a millisecond.

    Reaching for a private name in another module is a real coupling and is
    accepted here because its failure mode is benign in the direction that
    matters: if ``run_gates`` renames or drops ``_tracked_markdown``, ``setattr``
    simply creates an unused attribute, the real builder runs, and the hook is
    *slow* rather than wrong. ``test_the_markdown_file_list_shortcut_still_binds``
    fails if that happens, so the degradation is caught rather than absorbed.

    The placeholder is a name no glob would produce, so a build that somehow
    reached the filesystem with it would fail loudly rather than scan something.
    """
    run_gates = _load_run_gates()
    original = getattr(run_gates, "_tracked_markdown", None)
    if original is None:
        yield
        return
    # `setattr` rather than attribute assignment: the loader returns a plain
    # `ModuleType`, so pyright cannot know this attribute exists to be
    # rebound. The name is a string either way, and the test that pins this
    # shortcut still fails loudly if `run_gates` drops it.
    setattr(run_gates, "_tracked_markdown", lambda: ["<hook-placeholder>.md"])  # noqa: B010
    try:
        yield
    finally:
        setattr(run_gates, "_tracked_markdown", original)  # noqa: B010


def _tool_of(step: run_gates.Step) -> tuple[str, bool] | None:
    """The tool a built gate step invokes, and whether it came via ``uvx``.

    Only the *executed* command word counts. A package named in
    ``--with pytest==9.1.1`` is a dependency of the run, not the thing being
    run, and counting those was measured to mis-attribute pytest to the pyright
    gate — so the remedy for a serial-pytest denial told the reader to run the
    pyright gate.
    """
    argv = [str(part) for part in step.argv]
    if not argv:
        return None
    program = program_name(argv[0])

    if program == "uvx":
        spec = parse_uvx_target(argv[1:])
        if spec is None:
            return None
        parsed = parse_spec(spec)
        # Only a spec carrying a version names a *pinned* uvx tool; a bare name
        # in a built gate command would be the very shape rule 3 refuses.
        return (parsed.name, True) if parsed.version is not None else None

    if program == "uv" and argv[1:2] == ["run"]:
        # The same walker `classify` uses. Two hand-rolled loops over the same
        # argument shape drifted apart once already — one knew `-p`, the other
        # did not — so they read one implementation now.
        parsed = parse_uv_run(argv[2:])
        if not parsed.executed:
            return None
        # `uv run python <script.py>` runs a *script* under the project
        # interpreter. The thing executed is the interpreter, which is not a
        # project dependency and never will be -- so counting it puts `python`
        # in the ephemeral tool set, and the hook then denies every bare
        # `python scripts/run_gates.py` with the false reason "python is not
        # installed on this machine", as well as the `uv run python
        # scripts/...` spelling the docs gate and ADR-0061's reproduction
        # command both use. Measured before this guard, with the spec-links
        # gate moved onto `uv run`: `ephemeral` came back as `['pip-audit',
        # 'pymarkdown', 'pyright', 'python', 'ruff']` and both commands were
        # denied. A false positive is how a hook gets switched off, which this
        # module's own tests say in as many words.
        #
        # `uv run python -m <tool>` is the opposite case and must still count:
        # there the interpreter is a wrapper around a tool that genuinely has
        # to be present. `unwrap_python_module` is what tells the two apart,
        # and it is the same helper rule 1 uses below.
        if _is_python_program(parsed.executed):
            unwrapped = unwrap_python_module(list(parsed.rest))
            return (unwrapped[0], False) if unwrapped is not None else None
        return (parsed.executed, False)

    return None


def derive_facts() -> Facts:
    """Build every local gate and read the tool names out of the commands.

    Raises ``DerivationError`` rather than returning a partial answer: a
    silently short tool set is a hook that stopped policing what it claims to,
    which is the failure mode this whole series exists to remove.
    """
    # Its own step, and before the `except` clause below can name a type from
    # it: a failed import leaves `run_gates` unbound, so an `except
    # (OSError, run_gates.GateError)` guarding this call would itself raise
    # NameError while handling the error — turning the fail-open path into the
    # crash it exists to prevent.
    try:
        run_gates = _load_run_gates()
    except Exception as exc:
        raise DerivationError(f"could not import {RUNNER}: {exc}") from exc

    try:
        text = run_gates.CI_WORKFLOW.read_text(encoding="utf-8")
        pins = run_gates.read_pins(text)
        ctx = run_gates.Context(
            pins=pins,
            gitleaks_version=run_gates.read_gitleaks_version(text),
            canary_dir_name=run_gates.read_workflow_env(text, "CANARY_CAPTURE_DIR"),
            dry=True,
        )
    except (OSError, run_gates.GateError) as exc:  # fmt: skip
        raise DerivationError(
            f"could not read {run_gates.CI_WORKFLOW.name}: {exc}"
        ) from exc

    try:
        dev = dev_group_packages(PYPROJECT.read_text(encoding="utf-8"))
    # `OSError` only. Widening this to `ValueError` — to catch a
    # `TOMLDecodeError` whose module can no longer be named here — mislabelled
    # *any* unrelated `ValueError` from the call below as "could not read
    # pyproject.toml", blaming the input file for a bug in this hook. The parse
    # failure is named where the parsing happens instead, and anything else
    # reaches `evaluate`'s guard, which says plainly that the hook failed.
    except OSError as exc:
        raise DerivationError(f"could not read {PYPROJECT.name}: {exc}") from exc

    pinned_uvx: set[str] = set()
    executed: set[str] = set()
    gates_for_tool: dict[str, list[str]] = {}

    with _cheap_file_discovery():
        for gate in run_gates.GATES:
            if not gate.local:
                continue
            try:
                steps = gate.steps(ctx)
            except run_gates.GateError as exc:
                raise DerivationError(
                    f"gate {gate.name!r} would not build: {exc}"
                ) from exc
            for step in steps:
                tool = _tool_of(step)
                if tool is None:
                    continue
                name, is_uvx = tool
                if is_uvx:
                    pinned_uvx.add(name)
                else:
                    executed.add(name)
                gates_for_tool.setdefault(name, []).append(gate.name)

    dev_console = {_console_name(name) for name in dev}
    ephemeral = (pinned_uvx | executed) - dev_console

    if not ephemeral or not pinned_uvx:
        raise DerivationError(
            "derived an empty tool set from the gate registry; refusing to "
            "report a clean pass from a derivation that found nothing"
        )

    return Facts(
        pinned_uvx=frozenset(pinned_uvx),
        ephemeral=frozenset(ephemeral),
        selector=_selectors(gates_for_tool),
    )


def _selectors(gates_for_tool: dict[str, list[str]]) -> dict[str, str]:
    """Pick the run_gates selector to name in each tool's remedy.

    A tool run by more than one gate has no single right answer, and taking the
    first one seen was measured giving the wrong one: ``ruff`` is produced by
    both ``ruff-check`` and ``ruff-format``, so a denial of ``uvx ruff format .``
    told the author to run the linter. ADR-0077 §3 already says a denial whose
    remedy is wrong is worse than no denial.

    Where ``run_gates`` publishes a group under the tool's own name — it does
    for ``ruff`` — that group is the answer, because it selects every gate the
    tool appears in. Otherwise the gate names are listed, which the runner also
    accepts as positional selectors. Both branches are derived from the
    registry; neither restates a gate name.
    """
    groups = _load_run_gates().groups()
    selectors: dict[str, str] = {}
    for tool, gates in gates_for_tool.items():
        unique = list(dict.fromkeys(gates))
        if len(unique) == 1:
            selectors[tool] = unique[0]
        elif tool in groups and set(groups[tool]) == set(unique):
            selectors[tool] = tool
        else:
            selectors[tool] = " ".join(unique)
    return selectors


# --------------------------------------------------------------------------
# Command parsing
# --------------------------------------------------------------------------


def _tokenize(text: str, shell: str = BASH) -> list[str]:
    """Split a shell string into words, with operators as their own tokens.

    ``shlex.split`` is not enough: it is whitespace-driven, so ``true; ruff
    format .`` lexes ``true;`` as a single word and the separator is never seen
    — measured, and it let a chained invocation through. ``punctuation_chars``
    makes shlex treat ``();<>|&`` as tokens in their own right while still
    honouring quotes, which is what keeps ``echo "a; ruff check ."`` a single
    ``echo`` command rather than two.
    """
    lexer = shlex.shlex(text, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    if shell == POWERSHELL:
        # PowerShell has no backslash escape — `\` is an ordinary path
        # character, and `.\ruff.exe` is the *normal* way to spell a local
        # program there. POSIX escape handling ate the separators and handed
        # `program_name` the word `.ruffexe`, so every rule missed it: measured
        # on `.\ruff.exe check .`, `C:\tools\ruff.exe check .` and
        # `.\venv\Scripts\pytest.exe`, all three allowed. This hook registers a
        # PowerShell matcher, so that is not a hypothetical shell.
        lexer.escape = ""
    return list(lexer)


def _strip_fd_prefix(out: list[str]) -> None:
    """Remove a trailing file-descriptor digit run from the emitted text.

    ``2>&1`` lexes as the three tokens ``2``, ``>&``, ``1`` once the operator is
    punctuation, and the bare ``2`` then reads as a positional — a *test path*,
    which is what made ``pytest 2>&1 | tee out.log`` look like a scoped run.

    Only digits written flush against the operator are taken. ``sleep 2 > f``
    has a space, so the ``2`` is a real argument and survives; the redirect is
    still dropped. That distinction is the reason this reads the emitted text
    rather than filtering tokens afterwards, where the spacing is gone.
    """
    text = "".join(out)
    stripped = text.rstrip("0123456789")
    if stripped == text:
        return
    if stripped and stripped[-1] not in " \t\n;|&()":
        return  # digits are the tail of a word, e.g. `log2>`
    del out[:]
    out.append(stripped)


def _consume_redirect(text: str, index: int, out: list[str]) -> int | None:
    """Swallow a redirect or heredoc at ``index``; return the index past it.

    ``None`` means there is nothing to consume here. A heredoc's body is dropped
    entirely rather than queued as commands: it is data the command reads, not
    commands the shell runs.
    """
    # Heredoc first — `<<` would otherwise read as two `<` redirects.
    if text.startswith("<<", index):
        cursor = index + 2
        if cursor < len(text) and text[cursor] == "-":
            cursor += 1
        end = _skip_word(text, cursor)
        delimiter = text[cursor:end].strip().strip("\"'")
        if not delimiter:
            return end
        for line_end, line in _lines_from(text, end):
            if line.strip() == delimiter:
                return line_end
        return len(text)  # unterminated: the rest is body

    for operator in ("&>>", "&>", ">>", ">&", "<&", ">", "<"):
        if text.startswith(operator, index):
            if operator.startswith("&") and not _fd_context(out):
                return None
            if not operator.startswith("&"):
                _strip_fd_prefix(out)
            return _skip_word(text, index + len(operator))
    return None


def _fd_context(out: list[str]) -> bool:
    """True when a leading ``&`` here begins a redirect rather than a separator.

    ``cmd &> log`` redirects; ``cmd & echo x`` backgrounds. The difference is
    only the character that follows, which the caller has already matched — this
    guards the remaining ambiguity, a trailing ``&&``.
    """
    return not "".join(out).rstrip().endswith("&")


def _lines_from(text: str, index: int) -> Generator[tuple[int, str]]:
    """Yield ``(index just past this line, the line)`` from ``index`` onward."""
    while index < len(text):
        newline = text.find("\n", index)
        if newline == -1:
            yield len(text), text[index:]
            return
        yield newline + 1, text[index:newline]
        index = newline + 1


def _skip_word(text: str, index: int) -> int:
    """Index just past the whitespace-and-word starting at ``index``.

    Used to swallow a redirect's target. Quotes count as part of the word, so
    ``> "my log.txt"`` consumes the whole quoted name rather than stopping at
    the space inside it.
    """
    length = len(text)
    while index < length and text[index] in " \t":
        index += 1
    quote = ""
    while index < length:
        char = text[index]
        if quote:
            if char == quote:
                quote = ""
        elif char in "\"'":
            quote = char
        elif char in " \t\n;|&()<>":
            break
        index += 1
    return index


def _close_paren(text: str, start: int) -> int | None:
    """Index just past the `)` closing a substitution opened before ``start``.

    ``None`` when it never closes. The depth counter honours quotes and skips
    heredoc bodies, which a bare count of parentheses does not — and that
    blindness was reachable in **both** directions. A `)` inside heredoc *data*
    closed the substitution early, so the remaining data lines lexed as
    commands and a line reading only ``pytest`` was denied as one. A `)` inside
    a quoted string desynced the other way: the scan ran past the real close,
    ``_tokenize`` then raised on the unbalanced quote, and the whole chunk was
    discarded — taking a genuine command chained after it out of scope.
    """
    depth = 1
    cursor = start
    length = len(text)
    quote = ""
    while cursor < length:
        char = text[cursor]
        if quote:
            if char == quote:
                quote = ""
            cursor += 1
            continue
        if char in "\"'":
            quote = char
            cursor += 1
            continue
        if text.startswith("<<", cursor):
            skipped = _consume_redirect(text, cursor, [])
            if skipped is not None:
                cursor = skipped
                continue
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return cursor + 1
        cursor += 1
    return None


def _scan_shell(text: str, shell: str = BASH) -> tuple[str, list[str]]:
    """Strip substitutions out of ``text``, honouring quotes; also fix newlines.

    Returns the outer text with each substitution replaced by a placeholder
    *word*, and the substitution bodies as commands in their own right.

    The placeholder is load-bearing rather than cosmetic: it used to be a
    bare space, which collapsed ``pytest $(git diff --name-only)`` to an
    argument-less ``pytest`` that then read as an unscoped whole-suite run
    and was **denied**. See ``_SUBSTITUTION_PLACEHOLDER``.

    **Single quotes are the whole point.** The previous version ran a regex over
    the raw string before lexing, so it could not tell a live ``$(…)`` from one
    sitting inside single quotes — and denied ordinary prose that merely *names*
    a gate tool. Measured: ``git commit -m 'Explain why `uvx ruff` is wrong'``
    and ``grep -F '$(pytest)' notes.md`` were both refused. That also made the
    module's own "lexes with quoting honoured" claim false, since it was true of
    ``_tokenize`` and not of the pass that ran before it.

    **Double quotes are not the same case under bash, and are deliberately still
    scanned there.** Bash performs command substitution inside ``"…"``, so
    ``echo "`pytest`"`` really does run pytest, and denying it is correct.

    **Under PowerShell the backtick is the escape character, not a
    substitution**, so the same spelling runs nothing and denying it is a false
    positive on ordinary prose — ``Write-Host "use `pytest` here"`` was measured
    denied. PowerShell's substitution is ``$(…)``, which is scanned for both
    shells. This is why the scan takes the shell rather than assuming one: the
    hook registers a PowerShell matcher, so both dialects reach this code.

    Heredoc bodies are consumed as data. Without that, every line of
    ``cat > f <<'EOF' … EOF`` became a command in its own right, so a heredoc
    whose text merely *names* a tool was denied — measured on a commit message,
    the exact use the user's own instructions sanction.

    Redirects and their targets are dropped rather than treated as separators.
    Both directions were wrong before: the fd digit in ``pytest 2>&1`` survived
    into argv and read as a *test path*, making a full serial run look scoped,
    while ``echo a > pytest`` turned the redirect **target** into a command word
    and denied writing to a file.

    Nested ``$(…)`` is matched by counting parentheses rather than by a regex
    character class, which the old pattern could not express. A command whose
    *command word* is itself a substitution stays unreachable — see ADR-0077.
    """
    out: list[str] = []
    inner: list[str] = []
    index = 0
    length = len(text)
    in_single = False
    in_double = False

    while index < length:
        char = text[index]

        if in_single:
            out.append(char)
            if char == "'":
                in_single = False
            index += 1
            continue

        if char == _ESCAPE[shell] and index + 1 < length:
            if text[index + 1] == "\n":
                # A line continuation. The shell *removes* it and joins the
                # lines, so copying it through is wrong in a way that reaches
                # the rules: the escape folded into the next token, which then
                # did not start with `-` and read as a pytest *test path* —
                # `pytest \<newline>--verbose` ran the whole suite undenied.
                # That is the same class as the `2>&1` fd digit an earlier
                # round fixed, arriving by a second route.
                index += 2
                continue
            out.append(text[index : index + 2])
            index += 2
            continue

        if char == "'" and not in_double:
            in_single = True
            out.append(char)
            index += 1
            continue

        if char == '"':
            in_double = not in_double
            out.append(char)
            index += 1
            continue

        # `$(…)` expands inside double quotes and is scanned there. Process
        # substitution does **not** — measured: `bash -c 'echo "<(ls)"'` prints
        # the literal string. Scanning it inside quotes reopened the exact
        # false-positive class this scan exists to prevent, denying prose that
        # merely mentions the shape; it fired on a reviewer's own tool call.
        if text.startswith("$(", index) or (
            not in_double and char in "<>" and text.startswith("(", index + 1)
        ):
            # `<(cmd)` and `>(cmd)` are process substitutions, not redirects —
            # reached here rather than by `_consume_redirect`, which would have
            # swallowed `pytest <(gen)`'s argument as a redirect target and left
            # a bare `pytest` to be denied as a whole-suite run.
            # `$(`, `<(` and `>(` are all two characters wide.
            cursor = _close_paren(text, index + 2)
            if cursor is not None:
                inner.append(text[index + 2 : cursor - 1])
                out.append(f" {_SUBSTITUTION_PLACEHOLDER} ")
                index = cursor
                continue

        if char == "`" and shell == BASH:
            close = text.find("`", index + 1)
            if close != -1:
                inner.append(text[index + 1 : close])
                out.append(f" {_SUBSTITUTION_PLACEHOLDER} ")
                index = close + 1
                continue

        if not in_double and not in_single:
            # After the substitution branches, so `<(cmd)` is read as a process
            # substitution rather than as a `<` redirect whose target is `(cmd)`.
            consumed = _consume_redirect(text, index, out)
            if consumed is not None:
                out.append(" ")
                index = consumed
                continue

        if char == "\n" and not in_double:
            # A real line break between commands. Rewritten to a separator the
            # lexer will actually emit — see `_SEPARATORS`.
            out.append(" ; ")
            index += 1
            continue

        out.append(char)
        index += 1

    return "".join(out), inner


def split_commands(text: str, shell: str = BASH) -> list[list[str]]:
    """Split a shell string into the individual commands it would run.

    Chained and substituted commands are reached because that is how they are
    actually typed: ``cd repo && uv run ruff check .`` is the natural shape, and
    a hook that only inspected the first word would never fire on it.
    Unparseable input yields no commands rather than raising — a quoting error
    is the shell's to report, not this hook's, and guessing at a broken string
    risks denying something that was never going to run.
    """
    commands: list[list[str]] = []
    pending = [text]
    seen: set[str] = set()

    while pending:
        chunk = pending.pop()
        if chunk in seen:
            continue
        seen.add(chunk)

        # Pull `$(...)` and backtick bodies out and queue them as commands in
        # their own right, leaving a placeholder word behind so the outer
        # command still parses *and* still shows an argument in that slot.
        # The scan is quote-aware: a substitution inside single quotes is
        # text, not a command.
        chunk, inner = _scan_shell(chunk, shell)
        pending.extend(part for part in inner if part.strip())

        try:
            tokens = _tokenize(chunk, shell)
        except ValueError:
            continue

        current: list[str] = []
        for token in tokens:
            if token in _SEPARATORS:
                if current:
                    commands.append(current)
                current = []
            else:
                current.append(token)
        if current:
            commands.append(current)

    normalized = [strip_leading_noise(cmd) for cmd in commands]
    return [cmd for cmd in normalized if cmd]


def strip_leading_noise(argv: list[str]) -> list[str]:
    """Drop leading assignments, shell keywords and wrappers, as a shell would.

    All three sit in front of the real command word, and each was measured
    hiding an invocation: ``FOO=bar ruff check .``, ``if true; then uvx ruff
    check .; fi`` (whose second fragment begins with ``then``), and the wrapper
    family — ``sudo pytest``, ``env pytest``, ``exec pytest``, ``nohup pytest``,
    ``command pytest`` were all allowed through.

    **Only wrapper *words* are skipped, and the bare-form restriction falls out
    of that rather than being enforced.** The loop stops at the first token that
    is not an assignment, a keyword or a wrapper — so ``sudo pytest`` reaches
    the rule, while ``sudo -u me pytest`` stops at ``-u`` and fails open. An
    explicit "is the next token an option?" guard was written here first and
    then deleted: mutation testing showed removing it changed no verdict,
    because the loop already breaks on the flag. See ``_WRAPPERS`` for why the
    option grammar it replaced was removed rather than repaired.

    This fixes a class by construction rather than by enumeration.
    ``sudo -h pytest`` prints usage and runs nothing, so it must not be denied;
    it is not, for the same reason every flagged form is left alone. No
    describe-and-exit list is needed to get that right.
    """
    index = 0
    while index < len(argv):
        word = argv[index]
        if _ASSIGNMENT_RE.match(word) or word in _LEADING_KEYWORDS:
            index += 1
            continue
        if program_name(word) in _WRAPPERS:
            index += 1
            continue
        break
    return argv[index:]


def program_name(word: str) -> str:
    """The comparable name of a command word: basename, no extension, lowered.

    Splits on **both** separators rather than deferring to ``Path``, which is
    platform-dependent in exactly the wrong direction: ``Path(r".\\ruff.exe").name``
    is ``ruff.exe`` on Windows and the whole string on Linux, so a backslash
    path would be policed on one CI leg and invisible on the other two. The
    PowerShell matcher makes that spelling ordinary rather than exotic.
    """
    tail = re.split(r"[\\/]", word)[-1]
    return tail.lower().removesuffix(".exe")


def spec_name(spec: str) -> str:
    """The tool a uvx spec names, with any version suffix removed."""
    return parse_spec(spec).name


def is_pinned(spec: str) -> bool:
    """True when a uvx spec names one version that cannot drift.

    ``ruff@0.15.21`` pins. ``ruff@latest`` does not — and the earlier test for
    a pin was ``"@" in spec``, which called it pinned and returned before rule 3
    ever ran. That is a live bypass rather than a cosmetic one: ``@latest`` is
    the *documented* uvx spelling for "whatever is newest", so the one shape a
    reader is most likely to type on purpose was the one shape exempted.
    """
    version = parse_spec(spec).version
    return version is not None and version.strip().lower() not in _UNPINNED_VERSIONS


def unwrap_python_module(args: Sequence[str]) -> tuple[str, tuple[str, ...]] | None:
    """Resolve ``python -m pytest ...`` to the tool it actually runs.

    ``python -m pytest`` is a full serial suite wearing a different command
    word, and it reached none of the rules: ``program_name`` read ``python``,
    which matches nothing. Both spellings of the flag are handled — ``-m pytest``
    and the attached ``-mpytest`` — because Python accepts both and enumerating
    only the spaced one is the shape this module has already been caught by.

    Every spelling CPython accepts is walked: ``-m pytest``, the attached
    ``-mpytest``, and ``-m`` *clustered* behind other single-character flags as
    in ``-um pytest``. That third one is why the walk is character-by-character
    rather than a prefix match — an earlier version claimed both spellings were
    handled while a third bypassed, which is the completeness claim this
    module's own history says to distrust.

    Returns ``None`` when there is no ``-m``, which keeps ``python -c ...`` and
    ``python scripts/run_gates.py`` untouched.

    The scan stops at the first positional, because after a script path every
    remaining token belongs to the script: ``python run_gates.py -m ruff`` names
    a *gate selector*, not a module, and reading it as one would deny the
    runner this hook's whole remedy tells the reader to use.
    """
    index = 0
    while index < len(args):
        arg = args[index]
        if not arg.startswith("-") or arg == "-":
            return None  # a script path: everything after it is the script's
        if arg.startswith("--"):
            # The value test comes first, or the only long entry in
            # `_PYTHON_VALUE_FLAGS` is unreachable: this branch returned to the
            # loop before the check below, so `--check-hash-based-pycs always`
            # read `always` as a script path and abandoned the walk — allowing
            # `python --check-hash-based-pycs always -m pytest`, a full serial
            # suite. Dead config in a set whose siblings are guarded.
            name, sep, _value = arg.partition("=")
            if not sep and name in _PYTHON_VALUE_FLAGS:
                index += 2
                continue
            index += 1
            continue
        if arg in _PYTHON_VALUE_FLAGS:
            index += 2
            continue
        # A single-dash token is a *cluster*: CPython accepts `-um pytest`, and
        # matching `-m` only in leading position let `python -um pytest` and
        # `python -bm pytest` through while the docstring above claimed both
        # spellings were handled. Walk it character by character, the way
        # `read_short_cluster` already does for uv and pytest.
        consumed = 1
        for offset, char in enumerate(arg[1:], start=1):
            if char == "m":
                attached = arg[offset + 1 :]
                if attached:
                    return _console_name(attached), tuple(args[index + 1 :])
                module = args[index + 1 : index + 2]
                if not module:
                    return None
                return _console_name(module[0]), tuple(args[index + 2 :])
            if char == "c":
                return None  # the rest is program text, not a module
            if char in _PYTHON_VALUE_SHORTS:
                # Its value is the cluster remainder, or the *next token* when
                # the cluster ends here. Breaking without consuming that token
                # left it to the next iteration, which read it as a script path
                # and abandoned the walk — so `python -uX dev -m pytest` ran the
                # full serial suite undenied while `-X dev -m pytest` (the
                # unclustered spelling the tests covered) was caught.
                consumed = 1 if arg[offset + 1 :] else 2
                break
        index += consumed
    return None


# --------------------------------------------------------------------------
# Rules
# --------------------------------------------------------------------------


def _pytest_is_full_suite(args: Sequence[str]) -> bool:
    """True when this pytest invocation would run everything, serially.

    ``-n``, a selection flag, or an explicit path all mark a deliberately scoped
    run, which is legitimate and must not be denied — the work item that scoped
    this hook named a targeted ``pytest path -k ...`` as the false-positive
    machine to avoid. Only the unscoped, unparallelised whole-suite run is
    refused.

    **A flag's value is not a path.** An earlier version scanned for any token
    not starting with ``-`` and read it as a positional, so ``pytest -p
    no:cacheprovider``, ``pytest --tb long`` and four sibling shapes were all
    read as scoped and allowed through — every one of them the full serial run
    that gets killed at 600 s. Options that consume the following token are
    therefore enumerated rather than assumed away.
    """
    skip_next = False
    for index, arg in enumerate(args):
        if skip_next:
            skip_next = False
            continue
        if not arg.startswith("-"):
            # A genuine positional: a path or a node id, so the run is scoped.
            return False
        if arg.startswith("--"):
            name, sep, value = arg.partition("=")
            if name in _PYTEST_PARALLEL_FLAGS:
                if not _parallelism_scopes(value if sep else _next(args, index)):
                    if not sep:
                        skip_next = True
                    continue
                return False
            if name in _PYTEST_SCOPING_FLAGS:
                return False
            if not sep and name in _PYTEST_VALUE_FLAGS:
                skip_next = True
            continue
        # A single-dash token may be a cluster: `-vp no:cacheprovider` hides a
        # value-taking option behind a boolean one, and `-vkcanary` hides a
        # scoping one. Both were measured misclassified by whole-token matching.
        cluster = read_short_cluster(arg, _PYTEST_VALUE_SHORTS, _PYTEST_SCOPING_SHORTS)
        if cluster.scoping:
            # `-n` scopes only if it asks for workers. `-n 0` and `-n0` are
            # serial in-process runs wearing the parallel flag.
            if cluster.option == "n":
                value = cluster.attached or _next(args, index)
                if not _parallelism_scopes(value):
                    if not cluster.attached:
                        skip_next = True
                    continue
            return False
        if cluster.consumes_next:
            skip_next = True
    return True


def _next(args: Sequence[str], index: int) -> str:
    following = args[index + 1 : index + 2]
    return following[0] if following else ""


def _parallelism_scopes(value: str) -> bool:
    """True when a ``-n`` value actually distributes the run."""
    return value.strip() not in _PYTEST_SERIAL_VALUES


def classify(argv: list[str], facts: Facts) -> Violation | None:
    """Return the rule this command breaks, or None if it is fine."""
    if not argv:
        return None

    # `_console_name` here, not inside `program_name`, which stays a purely
    # lexical operation (basename, extension, case). Order matters and only a
    # path- or extension-bearing word shows it: reversed, `.\pymarkdownlnt.exe`
    # misses the mapping because it is not yet a bare name when the lookup runs.
    # This is one of the five call sites the module docstring accounts for, and
    # the one that did not map — the accounting lives there, not here.
    program = _console_name(program_name(argv[0]))
    rest = list(argv[1:])

    # `uv tool run` is uvx's long spelling — same resolver, same unpinned
    # failure — and it reached no rule at all, because `program_name` read
    # `uv` and the next word was not `run`.
    if program == "uv" and rest[:2] == ["tool", "run"]:
        program, rest = "uvx", rest[2:]

    # `python -m pytest` is the same serial suite wearing a different command
    # word. Normalising here rather than adding a fourth rule keeps every rule
    # below written once, against the tool actually executed.
    via_python = False
    if _is_python_program(program):
        unwrapped = unwrap_python_module(rest)
        if unwrapped is not None:
            program, module_args = unwrapped
            rest = list(module_args)
            via_python = True

    # Rule 3 — `uvx tool` with no @version, for a tool ci.yml pins.
    if program == "uvx":
        spec = parse_uvx_target(rest)
        if spec is None:
            return None
        if is_pinned(spec):
            return None  # pinned; this is the correct form
        tool = spec_name(spec)
        if tool in facts.pinned_uvx:
            return Violation(
                tool,
                f"`uvx {spec}` runs whatever version is newest, not the "
                f"version ci.yml pins. It will very likely succeed and "
                f"report a green that CI does not agree with.",
            )
        return None

    # Rules 1 and 4 — `uv run ...`.
    if program == "uv" and rest[:1] == ["run"]:
        parsed = parse_uv_run(rest[1:])
        tool = parsed.executed
        if tool is None:
            return None
        tool_args = parsed.rest
        # `uv run python -m pytest` hides the tool one level further down.
        # Rebound rather than re-entered, deliberately: recursing would drop
        # `parsed.provided`, so `uv run --with ruff python -m ruff check .`
        # would be denied with the false reason "uv cannot spawn it" — the
        # exact false accusation `supplies_unknown` exists to prevent.
        uv_via_python = False
        if _is_python_program(tool):
            unwrapped = unwrap_python_module(list(tool_args))
            if unwrapped is not None:
                tool, tool_args = unwrapped
                uv_via_python = True
        if (
            tool in facts.ephemeral
            and tool not in parsed.provided
            and not parsed.supplies_unknown
        ):
            # uv spawns python perfectly well; what is missing is the *module*.
            # Rule 2 below already splits its cause by spelling, and this branch
            # did not — so `uv run python -m ruff` was refused for a PATH
            # problem that is not there, against this module's own standard that
            # a denial whose explanation is wrong is worse than no denial.
            cause = (
                f"there is no {tool} module in that environment"
                if uv_via_python
                else f"{tool} is not a project dependency, so uv cannot spawn "
                f"it (exit 2)"
            )
            invocation = (
                f"uv run python -m {tool}" if uv_via_python else f"uv run {tool}"
            )
            return Violation(tool, f"`{invocation}` fails here: {cause}.")
        if tool == "pytest" and _pytest_is_full_suite(tool_args):
            return _serial_pytest()
        return None

    # Rule 2 — a bare tool that is not on PATH.
    if program in facts.ephemeral:
        # The failure differs by spelling and the reason has to say which, or a
        # reader who typed `python -m ruff` goes looking for a PATH problem
        # that is not there. Same denial, same remedy, accurate cause.
        cause = (
            f"there is no {program} module installed here"
            if via_python
            else f"{program} is not installed on this machine (exit 127)"
        )
        return Violation(
            program,
            f"`{program}` cannot run here: {cause}; it is only ever run "
            f"through uv/uvx at ci.yml's pinned version.",
        )

    # Rule 4 — a bare full-suite pytest.
    if program == "pytest" and _pytest_is_full_suite(rest):
        return _serial_pytest()

    return None


def _serial_pytest() -> Violation:
    return Violation(
        "pytest",
        "A full-suite pytest with no `-n` runs serially. Measured in this "
        "repository, that exceeds the 600 s command timeout and is killed, "
        "where `-n auto` finishes in ~112 s. Scope it with a path or `-k`, or "
        "run the gate.",
    )


def check_command(text: str, facts: Facts, shell: str = BASH) -> Violation | None:
    for argv in split_commands(text, shell):
        violation = classify(argv, facts)
        if violation is not None:
            return violation
    return None


def shell_for_tool(tool_name: object) -> str:
    """Which shell dialect a tool's command string is written in.

    The payload names the tool, so the hook does not have to guess. Getting this
    wrong is not cosmetic in either direction: reading a PowerShell command with
    bash rules denied ordinary prose (a backtick is PowerShell's *escape*, not a
    substitution) and let backslash paths past every rule at the same time.

    An unmapped tool falls back to bash. See ``_SHELL_BY_TOOL`` for why that is
    a gap rather than a safety margin — the two dialects each catch something
    the other misses, so no single default is conservative on both axes.
    """
    return _SHELL_BY_TOOL.get(tool_name, BASH) if isinstance(tool_name, str) else BASH


# --------------------------------------------------------------------------
# Hook protocol
# --------------------------------------------------------------------------


def _deny(reason: str) -> dict[str, object]:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }


def _allow_with_notice(message: str) -> dict[str, object]:
    # No permissionDecision: the command proceeds through the normal permission
    # flow untouched. `allow` would auto-approve it, which is a downgrade the
    # hook has no business making just because it broke.
    return {"systemMessage": message}


def evaluate(payload: dict[str, object]) -> dict[str, object] | None:
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return None
    command = cast("dict[str, object]", tool_input).get("command")
    if not isinstance(command, str) or not command.strip():
        return None

    shell = shell_for_tool(payload.get("tool_name"))

    try:
        facts = derive_facts()
        violation = check_command(command, facts, shell)
    except DerivationError as exc:
        return _allow_with_notice(
            f"check_gate_invocation is not policing gate commands: {exc}. "
            "Local gate invocations are unchecked until this is fixed."
        )
    except Exception as exc:
        # Any other failure is a bug in this hook, and a bug in this hook must
        # not become a bug in the session. ADR-0077 §5 chose fail-open for the
        # derivation; the same argument covers the crash it did not anticipate,
        # because the two are indistinguishable to the person whose command
        # stopped working. Broad by intent: enumerating the exceptions a parser
        # bug can raise is the enumeration that rots.
        return _allow_with_notice(
            f"check_gate_invocation failed and is policing nothing: "
            f"{type(exc).__name__}: {exc}. "
            "Local gate invocations are unchecked until this is fixed."
        )

    if violation is None:
        return None
    return _deny(violation.remedy(facts))


def main() -> int:
    try:
        raw = json.load(sys.stdin)
    except (json.JSONDecodeError, UnicodeDecodeError):  # fmt: skip
        return 0
    if not isinstance(raw, dict):
        return 0

    result = evaluate(cast("dict[str, object]", raw))
    if result is not None:
        json.dump(result, sys.stdout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
