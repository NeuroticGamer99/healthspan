"""The PreToolUse gate-invocation hook (scripts/check_gate_invocation.py).

The hook refuses four command shapes that cannot work here. Three properties
make it worth having rather than merely present, and each is invisible by
inspection:

1. **The tool list is derived, not restated.** The hook never names ruff,
   pyright, pymarkdownlnt or pip-audit as things to police; it builds
   ``run_gates``'s own registry and reads the tools out of the commands. The
   derivation found ``pip-audit`` on its first run, which the work item scoping
   the hook had not listed. ``test_a_newly_pinned_uvx_tool_is_policed_with_no_edit``
   is the load-bearing test: it introduces a tool the hook has never heard of and
   requires a denial.
2. **A legitimate command is never denied.** A hook with false positives is one
   an operator disables, and this repository has already written that reasoning
   down — in ADR-0070's rejection of mechanising the containment rule's
   *content* half, the part it declined rather than the part it shipped. The allow
   corpus is therefore as load-bearing as the deny corpus, and includes the
   shapes most likely to be caught by accident: a tool name quoted inside a
   ``grep`` pattern, a targeted ``pytest``, and a correctly pinned ``uvx``.
3. **It fails open, visibly.** A hook that cannot derive its facts must not
   block the session, and must not silently stop policing either.
   ``test_a_broken_derivation_allows_the_command_and_says_so`` pins both halves.

The settings wiring is tested here too, because it is the half that cannot be
observed from the script: a hook registered for Bash alone would leave the whole
class reachable through the PowerShell tool, which is the per-tool-enumeration
failure ``specs/open-questions.md`` records the repository already paying once.

Nothing below hardcodes a tool name where the derivation can supply it, except
where the test's whole point is that a specific real tool is or is not covered.
"""

from __future__ import annotations

import ast
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, cast

import check_gate_invocation as hook
import pytest
import run_gates

REPO_ROOT = Path(__file__).resolve().parent.parent
SETTINGS = REPO_ROOT / ".claude" / "settings.json"
HOOK_SCRIPT = REPO_ROOT / "scripts" / "check_gate_invocation.py"

# Shapes that must be refused. Each is a command this repository has either
# measured failing or measured succeeding against the wrong version.
MUST_DENY = [
    "uv run ruff check .",
    "uv run pyright",
    "uv run pymarkdown scan .",
    "ruff check .",
    "pyright",
    "pymarkdown scan docs/",
    # The package spelling of the entry above. Measured, it adds no detection
    # power over `test_both_spellings_of_a_tool_are_refused_at_every_call_site`,
    # which asserts both spellings and more besides: every mutation that
    # reddened this line reddened that test too, and none was found that only
    # this line catches. It is here because MUST_DENY is where a reader looks to
    # see what is refused, and a corpus that showed one of two spellings read as
    # a deliberate exemption of the other.
    "pymarkdownlnt scan docs/",
    "uvx ruff check .",
    "uvx pymarkdownlnt --config pyproject.toml scan x.md",
    "cd /some/repo && uv run ruff format --check .",
    "echo $(uvx ruff --version)",
    "FOO=bar ruff check .",
    "pytest",
    "pytest -v",
    "uv run --with pytest==9.1.1 pytest",
    # A flag's value is not the command word. `-p` is uv's short `--python`,
    # and reading its value as the tool let one ordinary flag defeat both the
    # ephemeral-tool rule and the serial-pytest rule at once.
    "uv run -p 3.12 ruff check .",
    "uv run -p 3.12 pytest",
    "uv run --python=3.12 pyright",
    # A flag's value is not a test path either. Each of these is the full
    # serial run that gets killed at 600 s, wearing a flag that takes a value.
    "pytest -p no:cacheprovider",
    "pytest -o cache_dir=/tmp/x",
    "pytest --tb long",
    "pytest -c pytest.ini",
    "pytest --junitxml report.xml",
    "pytest -W once::DeprecationWarning",
    "pytest -x",
    "pytest --maxfail=3",
    # `--from` carries the package; an unpinned one is still unpinned.
    "uvx --from ruff ruff check .",
    # Short spellings of the same value-taking options as above.
    "uv run -i https://pypi.example.org/simple pytest",
    "uv run -f ./wheels pyright",
    "uv run -P ruff pytest",
    # A value-taking short flag hiding behind a boolean one in a cluster. Each
    # is the full serial run, wearing two characters instead of one.
    "pytest -vp no:cacheprovider",
    "pytest -vc pytest.ini",
    "pytest -so no:cacheprovider",
    "pytest -xvs",
    # The end-of-options separator must not hide the command word.
    "uv run -- ruff check .",
    # Supplying one tool does not supply a different one. This is what keeps
    # `_REQUIREMENT_NAME_RE`'s loose specifier handling observable: reading only
    # the `==` form makes the supply unparseable, which the unknown-supply
    # escape would then turn into a blanket allow.
    'uv run --with "ruff>=0.15" pyright',
    # `@latest` wears the `@` of the correct form and pins nothing. Reading the
    # bare presence of the character as "pinned" exempted the one unpinned
    # spelling a reader is most likely to type deliberately.
    "uvx ruff@latest check .",
    "uvx ruff@ check .",
    "uvx --from ruff@latest ruff check .",
    # `uv tool run` is uvx's long spelling, and reached no rule at all.
    "uv tool run ruff check .",
    "uv tool run ruff@latest check .",
    "uv tool run pymarkdownlnt scan docs/",
    # The same serial suite wearing a different command word.
    "python -m pytest",
    "python3 -m pytest",
    "py -m pytest",
    # Python accepts the attached spelling of the flag too.
    "python -mpytest",
    # An interpreter option that takes a value must not stop the walk early.
    "python -X dev -m pytest",
    # ...including the LONG spelling, whose value test sat after the branch
    # that returns, making the only long entry in the set dead config.
    "python --check-hash-based-pycs always -m pytest",
    "python --check-hash-based-pycs=always -m pytest",
    "uv run python -m pytest",
    "python -m ruff check .",
    # A line continuation must not hide the tool or fake a test path. The
    # escape folded into the next token, which then did not start with `-` and
    # read as a pytest path — the same class as the `2>&1` fd digit, by a
    # second route. Both dialects: PowerShell's backtick did it too, and there
    # it also defeated rule 3 (`uvx` + continuation went unpinned).
    "pytest \\\n--verbose",
    "pytest \\\n--verbose \\\n--tb=short",
    "uvx \\\nruff check .",
    # A redirect is not a test path. The fd digit in `2>&1` survived into argv
    # and read as a positional, so the most ordinary capture idiom there is
    # made a full serial run look deliberately scoped.
    "pytest 2>&1 | tee out.log",
    "pytest 2> err.log",
    "pytest &> out.log",
    "pytest 1>out 2>err",
    "uv run pytest 2>&1 | tee log.txt",
    "pytest > out.log",
    "pytest >> out.log 2>&1",
    # xdist's `-n 0` is "no distribution" — an in-process serial run wearing
    # the parallel flag — and `--deselect` subtracts from a full run rather
    # than selecting a subset.
    "pytest -n 0",
    "pytest -n0",
    "pytest --numprocesses=0",
    "pytest --numprocesses 0",
    "pytest --deselect tests/test_pool.py::test_x",
    # CPython accepts single-character flags clustered with `-m`.
    "python -um pytest",
    "python -bm pytest",
    "python -Sum pytest",
    # A value-taking short clustered behind another character, with its value
    # as a SEPARATE token. The cluster walk stopped at the value flag without
    # consuming the value, so the next pass read `dev` as a script path and
    # abandoned the walk — the unclustered `-X dev -m pytest` was covered and
    # denied, which is what made this look tested.
    "python -uX dev -m pytest",
    "python -uW ignore -m pytest",
    "python -bX importtime -m pytest",
    # A wrapper occupies the leading slot; the command it runs is still a
    # command. `env FOO=bar pytest` is the portable spelling of the assignment
    # case, which is what shows skipping the wrapper word alone is not enough.
    # One literal per wrapper name. Written out rather than derived from
    # `_WRAPPERS`, because a test that generates its cases from the set it
    # checks cannot notice an entry leaving it — measured: dropping `setsid`
    # and dropping `xargs` each left the whole suite green.
    "sudo pytest",
    "doas pytest",
    "env pytest",
    "exec pytest",
    "nohup pytest",
    "setsid pytest",
    "stdbuf pytest",
    "nice pytest",
    "command pytest",
    "env FOO=bar pytest",
    "nohup sudo env pytest",
    "sudo uvx ruff check .",
]

# Shapes refused only when the command came from the PowerShell tool, where a
# backslash is an ordinary path character rather than an escape.
MUST_DENY_POWERSHELL = [
    r".\ruff.exe check .",
    r"C:\tools\ruff.exe check .",
    r".\venv\Scripts\pytest.exe",
    r"C:\Python\Scripts\pyright.exe",
    # The package-name spelling reaches the bare path through a Windows path
    # too, so the two normalisations have to compose: basename and `.exe` first
    # (lexical), then the console-script mapping.
    r".\pymarkdownlnt.exe scan .",
    # PowerShell's line continuation is a backtick, and leaving it in argv did
    # two harms rather than bash's one: it faked a pytest test path *and* it
    # separated `uvx` from its tool, so rule 3 stopped firing as well.
    "pytest `\n--verbose",
    "uvx `\nruff check .",
]

# Shapes refused only under bash, where a backtick really does substitute.
MUST_DENY_BASH_ONLY = [
    'git commit -m "Explain why `uvx ruff` is wrong"',
    'echo "`pytest`"',
]

# Shapes that must pass through untouched.
MUST_ALLOW = [
    "python3 scripts/run_gates.py",
    "python scripts/run_gates.py ruff --list",
    'uvx "ruff@0.15.21" check .',
    'uvx "pymarkdownlnt@0.9.39" --config pyproject.toml scan a.md',
    "uv run --with pyright==1.1.411 --with pytest==9.1.1 pyright",
    "uv run pytest tests/test_run_gates.py",
    "uv run pytest -k test_thing",
    "uv run pytest -n auto",
    "pytest tests/test_pool.py -k canary",
    "pytest -n auto",
    "uvx cowsay hello",
    "git status",
    'grep -rn "uv run ruff" .claude/',
    "echo 'ruff check .'",
    "uv lock --check",
    "uv run python -c 'print(1)'",
    # `--with` genuinely supplies the tool, whatever the specifier looks like.
    # Reading only the `==` form denied three working commands, and did it with
    # the false reason "uv cannot spawn it".
    'uv run --with "ruff>=0.15" ruff check .',
    "uv run --with ruff ruff check .",
    "uv run --with ruff~=0.15.0 ruff check .",
    "uv run --with=ruff ruff check .",
    # The pin can live on `--from` rather than on the command word.
    "uvx --from ruff@0.15.21 ruff check .",
    # Scoped by rerun-selection rather than by path.
    "pytest --lf",
    "pytest --ff",
    "pytest --stepwise",
    "pytest -n4",
    "pytest -kcanary",
    # A substitution in an *argument* still leaves the real command visible.
    "cd $(dirname $(which uv)) && python3 scripts/run_gates.py",
    # A scoping short flag hiding inside a cluster: genuinely scoped runs.
    "pytest -vkcanary",
    "pytest -vk canary",
    "pytest -xvs tests/test_pool.py",
    # Supply this hook cannot enumerate. The tool may well be in the file or the
    # editable directory, so claiming it is missing is a false accusation.
    "uv run --with-requirements reqs.txt ruff check .",
    "uv run --with-editable ./local-ruff ruff check .",
    "uv run --with-requirements=reqs.txt pyright",
    # `-w` is the short spelling of `--with`, so it supplies the tool too.
    "uv run -w ruff ruff check .",
    "uv run -wruff ruff check .",
    "pytest -- tests/test_pool.py",
    # A real pin is still a real pin through the long spelling.
    "uv tool run ruff@0.15.21 check .",
    # `python -m pytest` is refused for being serial, not for being `-m`.
    "python -m pytest -n auto",
    "python -m pytest tests/test_pool.py",
    "uv run python -m pytest -n auto",
    # `--with` supplies the module too. Re-entering `classify` for the unwrapped
    # module would drop the supply set and deny this with the false reason
    # "uv cannot spawn it" — the accusation `supplies_unknown` exists to stop.
    "uv run --with ruff python -m ruff check .",
    # After a script path, `-m` is the script's own flag. `run_gates.py -m ruff`
    # names a gate selector, and denying it would refuse the very command every
    # remedy in this hook tells the reader to run.
    "python scripts/run_gates.py -m ruff",
    "python3 scripts/run_gates.py -m pytest",
    # The redirect *target* is a filename, not a command. Treating `>` as a
    # separator made writing to a file named after a tool a denied tool run.
    "echo 'a' > pytest",
    "echo hello > ruff",
    "cat notes.md >> pyright.log",
    # A heredoc body is data the command reads. Every line became a standalone
    # command, so a commit message naming a tool was denied — the exact use
    # the user's own instructions sanction.
    "cat > /tmp/x <<'EOF'\npyright is not installed\nEOF",
    "git commit -m 'x' <<EOF\nruff check .\nEOF",
    "cat <<-EOF\nuvx ruff\nEOF",
    # A help or version flag prints in under a second. Denying it for
    # "exceeding the 600 s timeout" states something that cannot be true of it.
    "pytest -h",
    "pytest -V",
    # `-n` with real workers is genuinely parallel, whatever the spelling.
    "pytest -n 4",
    "pytest --numprocesses=auto",
    "pytest --deselect a::b -n auto",
    # Paths arriving through a channel the scanner erases still mean the run
    # is scoped. Each of these was DENIED, with the 600 s-timeout reason, for
    # a run that finishes in seconds.
    "pytest $(git diff --name-only)",
    "pytest `cat failing.txt`",
    "pytest <(gen)",
    'git ls-files "*_test.py" | xargs pytest',
    "find . -name 'test_*.py' | xargs pytest",
    # A line continuation is removed by the shell, not turned into a token.
    # Left in argv it read as a test path and the suite ran undenied.
    "pytest -n auto \\\n--verbose",
    # A heredoc body is data even when it contains a parenthesis: the paren
    # counter used to desync on it and lex the body as commands.
    "cat > /tmp/x <<'EOF'\nsee ) below\npytest\nEOF",
    # Process substitution does NOT expand inside double quotes — measured,
    # `bash -c 'echo "<(ls)"'` prints the literal. Scanning it there denied
    # prose that merely mentions the shape, which is the false-positive class
    # the quote handling exists to prevent, reopened by a new branch.
    'echo "delete via <(pytest)"',
    'git commit -m "see <(pytest) for context"',
    "echo '<(pytest)'",
    # The `>(cmd)` spelling in its quoted form. Measured to have real power:
    # removing the `not in_double` guard kills these. Its *unquoted* deny
    # counterparts were tried here and dropped — they survived both narrowing
    # the branch and deleting it outright, because `>` then falls to the
    # redirect path, `(` splits as a separator, and `pytest` is reached by a
    # coincidental route with the same verdict. The parametrised
    # `split_commands` test is the detector for that direction.
    'echo "note: >(pytest) is a thing"',
    "echo 'note: >(pytest)'",
    # These wrapper spellings describe rather than execute. Each is written
    # WITHOUT a trailing positional on purpose: `xargs -I{} pytest {}` stayed
    # green with the inhibitor deleted, because the trailing `{}` read as a
    # scoped test path once the wrapper was wrongly unwrapped. A corpus entry
    # that passes for the wrong reason is worse than none.
    "command -v pytest",
    "command -V ruff",
    # Asked to describe itself, a wrapper prints and exits; the trailing
    # words never run. `sudo -h pytest` prints usage - denying it was a
    # false positive an earlier round introduced and pinned in MUST_DENY.
    "sudo -h pytest",
    "sudo --help pytest",
    "sudo -V pytest",
    "xargs --help pytest",
    "env --version pytest",
    "timeout --help pytest",
    "xargs -I{} pytest",
    "xargs --replace={} pytest",
    "sudo -v pytest",
    "sudo -l pytest",
    # A wrapper with no command after it runs nothing.
    "env",
    "sudo -l",
]


@pytest.fixture(scope="module")
def facts() -> hook.Facts:
    return hook.derive_facts()


# --------------------------------------------------------------------------
# Derivation
# --------------------------------------------------------------------------


def test_a_newly_pinned_uvx_tool_is_policed_with_no_edit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A tool the hook has never heard of is covered the moment CI pins it.

    This is the property the whole design exists for. A hook carrying a literal
    tool list would pass every other test in this file and fail this one.

    The fake gate is appended to the registry rather than replacing it, so the
    real tools stay covered — a derivation that dropped them to pick this one up
    would be a regression the deny corpus catches.
    """
    invented = "sillyname-linter"
    fake = run_gates.Gate(
        name="invented",
        job="lint",
        summary="a tool that did not exist when the hook was written",
        ci_steps=(),
        build=lambda _ctx: [
            run_gates.Step([hook.program_name("uvx"), f"{invented}@9.9.9", "check"])
        ],
        version_pin=None,
    )
    monkeypatch.setattr(run_gates, "GATES", (*run_gates.GATES, fake))

    derived = hook.derive_facts()

    assert invented in derived.pinned_uvx, (
        "a tool pinned in the gate registry was not picked up — the hook is "
        "restating a tool list somewhere instead of deriving it"
    )
    assert hook.check_command(f"uvx {invented} check", derived) is not None
    assert hook.check_command(f"uvx {invented}@9.9.9 check", derived) is None


def test_the_derived_tools_are_exactly_what_the_registry_names(
    facts: hook.Facts,
) -> None:
    """The derivation agrees with the registry it reads, in both directions.

    Asserting only that the known tools appear would pass for a derivation that
    also invented extras; asserting only the count would pass for the wrong set.
    """
    expected: set[str] = set()
    with hook._cheap_file_discovery():  # pyright: ignore[reportPrivateUsage]
        ctx = _real_context()
        for gate in run_gates.GATES:
            if not gate.local:
                continue
            for step in gate.steps(ctx):
                tool = hook._tool_of(step)  # pyright: ignore[reportPrivateUsage]
                if tool is not None and tool[1]:
                    expected.add(tool[0])

    assert set(facts.pinned_uvx) == expected
    assert expected, "the registry names no uvx tool at all — derivation is broken"


def test_a_script_run_under_the_project_interpreter_names_no_tool() -> None:
    """`uv run python <script.py>` executes a script, not a dependency.

    The docs-consistency `spec-links` gate runs this way because it parses
    CommonMark with markdown-it-py and so needs the project environment. Reading
    the executed word as the *tool* put `python` into the ephemeral set, and the
    hook then denied `uv run python -c ...` and every bare
    `python scripts/run_gates.py` with the reason "python is not installed on
    this machine" — commands this module's own MUST_ALLOW list calls legitimate.
    A false positive is how a hook gets switched off.

    `-m` is the case that must still count: there the interpreter wraps a tool
    that genuinely has to be present.
    """
    script = run_gates.REPO_ROOT / "scripts" / "check_spec_links.py"
    plain = run_gates.Step(
        ["uv", "run", "python", str(script)], f"uv run python {script}"
    )
    module = run_gates.Step(
        ["uv", "run", "python", "-m", "ruff", "check", "."],
        "uv run python -m ruff check .",
    )

    assert hook._tool_of(plain) is None  # pyright: ignore[reportPrivateUsage]
    assert hook._tool_of(module) == (  # pyright: ignore[reportPrivateUsage]
        "ruff",
        False,
    )


@pytest.mark.parametrize(
    "spelling",
    [
        "python",
        "python3",
        "py",
        "python.exe",
        "PYTHON",
        "python3.12",
        "python3.14t",
        "python3.13t",
        "pyw",
        # Report finding 4's other direction. The predicate missed the whole
        # PyPy family, so `uv run pypy3 -m pytest` re-landed `pypy3` in
        # `facts.ephemeral` and restored the false "is not installed on this
        # machine" denial the carve-out exists to remove.
        "pypy",
        "pypy3",
        "pypy3.10",
        # The other side of the undotted-tail narrowing below: only the
        # *undotted* position is restricted to one digit, because that is the
        # only ambiguous one. A dotted major stays `\d+`, so this keeps
        # matching however long Python's major line runs.
        "python10.0",
    ],
)
def test_every_interpreter_spelling_reaches_the_carve_out(spelling: str) -> None:
    """Report finding 8: the carve-out compared a raw command word.

    `parsed.executed` is not normalized, so `python.exe`, `PYTHON` and
    `python3.12` all missed a bare `in _PYTHON_PROGRAMS` test and re-landed an
    interpreter in `facts.ephemeral` — restoring the measured denial of every
    bare `python scripts/run_gates.py` with the false reason "python is not
    installed on this machine".

    `python3.14t` is the free-threaded build and is a live spelling here, since
    this project sets `requires-python = ">=3.14"`.
    """
    step = run_gates.Step(
        ["uv", "run", spelling, "scripts/x.py"], f"uv run {spelling} scripts/x.py"
    )

    assert hook._tool_of(step) is None, spelling  # pyright: ignore[reportPrivateUsage]


@pytest.mark.parametrize(
    "word",
    [
        "ruff",
        "pyright",
        "pytest",
        "pymarkdown",
        "",
        None,
        # Report finding 4: the version pattern carried a free-floating `t?`
        # over an optional version, so every one of these matched. None spells
        # anything — CPython's free-threaded build is always fully qualified
        # (`python3.14t`), which the positive list above pins.
        "pyt",
        "pywt",
        "py2t",
        "python3t",
        "pythont",
        "pypyt",
        "pypy3t",
        # Greptile, PR #100: the same over-match one position to the left. The
        # `…t` fix bound the letter and left the number `\d+`, so an undotted
        # multi-digit tail still read as a version. A two-digit component is a
        # minor version and always follows a dot, so none of these spells
        # anything either.
        "py12",
        "pypy12",
        "python12",
        "pyw12",
    ],
)
def test_a_non_interpreter_is_not_swept_into_the_carve_out(word: str | None) -> None:
    """The boundary: widening the predicate must not swallow a real tool.

    Without this, `_is_python_program` returning True for everything would
    satisfy every assertion above while disabling the hook entirely — the
    carve-out's own failure mode, one layer up. `pyright` and `pymarkdown` are
    the pointed cases: both begin with `py`.

    The `…t` spellings are the measured half. Over-matching was argued to be the
    safe direction because it "denies more", and at `_tool_of` it does the
    opposite: classifying a word as an interpreter *drops* it from the enforced
    tool set, so a future gate whose console script matched the tail would
    silently stop being policed. That is fail-open, from a predicate defended as
    fail-closed.
    """
    assert hook._is_python_program(word) is False, word  # pyright: ignore[reportPrivateUsage]


def test_the_interpreter_is_never_an_ephemeral_tool(facts: hook.Facts) -> None:
    """The consequence of the above, asserted where it bites.

    `python` can never be a project dependency, so a derivation that lands it in
    the ephemeral set denies the interpreter itself. Measured with the docs gate
    moved onto `uv run` and before the carve-out: `ephemeral` came back as
    `['pip-audit', 'pymarkdown', 'pyright', 'python', 'ruff']`, and both a bare
    `python scripts/run_gates.py` and the `uv run python scripts/...` spelling
    that ADR-0061 prints as its reproduction command were denied.

    The second assertion is the discriminating half: the first alone would pass
    for a derivation that found no tools at all, which `derive_facts` is
    supposed to refuse outright.
    """
    assert "python" not in facts.ephemeral
    assert facts.ephemeral, "an empty tool set would satisfy the line above"
    assert hook.check_command("uv run python -c 'print(1)'", facts) is None


def test_a_dev_dependency_is_never_treated_as_missing(facts: hook.Facts) -> None:
    """pytest is reached through ``--with`` in CI *and* is a dev dependency.

    Denying ``uv run pytest <path>`` would be wrong, and the only thing that
    keeps it right is subtracting the dev group. Dropping that subtraction
    reddens here.
    """
    assert "pytest" not in facts.ephemeral
    assert hook.check_command("uv run pytest tests/test_pool.py", facts) is None


def test_the_dev_group_subtraction_maps_package_names_to_console_scripts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The subtraction is by console script, so the dev group must be mapped too.

    ``ephemeral`` is ``(pinned_uvx | executed) - dev_console``, and its left side
    is keyed by console script. Subtracting raw *package* names would therefore
    subtract nothing for any tool whose two names differ, leaving a genuinely
    installed tool refused as "not installed on this machine".

    This is the sixth ``_console_name`` call site — the one
    ``test_both_spellings_of_a_tool_are_refused_at_every_call_site`` correctly
    excludes, because it maps a dependency list rather than a typed word. Excluded
    from that test is not the same as covered, and it was not: dropping the
    mapping here left the whole suite green, because ``pymarkdownlnt`` is not
    actually a dev dependency, so today the site is a no-op against the real
    ``pyproject.toml``. It stops being one the moment a console-script-mismatched
    tool is added to the dev group, which is exactly when a silent miscomputation
    would be least expected. Hence a synthetic dev group rather than the real one.
    """
    real = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert "\ndev = [\n" in real, "the dev group's shape changed; update this test"
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        real.replace("\ndev = [\n", '\ndev = [\n    "pymarkdownlnt>=0.9",\n', 1),
        encoding="utf-8",
    )
    monkeypatch.setattr(hook, "PYPROJECT", pyproject)

    facts = hook.derive_facts()

    assert "pymarkdown" not in facts.ephemeral, (
        "a dev-installed pymarkdownlnt must not be reported as missing"
    )
    # The tool is still a pinned uvx tool, so rule 3 must keep policing it —
    # this asserts the subtraction narrowed one rule and not the others.
    assert "pymarkdown" in facts.pinned_uvx
    assert hook.check_command("pymarkdownlnt scan .", facts) is None
    assert hook.check_command("uvx pymarkdownlnt scan .", facts) is not None


def test_the_dev_group_subtraction_reads_real_package_names() -> None:
    text = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    names = hook.dev_group_packages(text)
    assert "pytest" in names
    assert "pytest-xdist" in names
    # The version specifier must be stripped, not carried into the name.
    assert not any(">" in name or "=" in name for name in names)


def test_the_markdown_file_list_shortcut_still_binds() -> None:
    """The private name the hook patches for speed must still exist.

    ``_cheap_file_discovery`` suppresses a 141 ms ``git ls-files`` on a hook that
    runs before every Bash call. If ``run_gates`` renames that helper the patch
    silently stops applying and the hook gets slow rather than wrong — a
    degradation worth catching rather than absorbing.
    """
    assert hasattr(run_gates, "_tracked_markdown"), (
        "run_gates._tracked_markdown was renamed or removed; "
        "check_gate_invocation._cheap_file_discovery no longer suppresses the "
        "git call and the hook has silently become slow"
    )

    calls: list[int] = []
    original = run_gates._tracked_markdown  # pyright: ignore[reportPrivateUsage]

    def counting() -> list[str]:
        calls.append(1)
        return original()

    run_gates._tracked_markdown = counting  # pyright: ignore[reportPrivateUsage]
    try:
        with hook._cheap_file_discovery():  # pyright: ignore[reportPrivateUsage]
            ctx = _real_context()
            for gate in run_gates.GATES:
                if gate.local:
                    gate.steps(ctx)
    finally:
        run_gates._tracked_markdown = original  # pyright: ignore[reportPrivateUsage]

    assert not calls, "the real file-list discovery ran despite the shortcut"


def test_derivation_refuses_to_report_an_empty_tool_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A derivation that finds nothing must raise, not quietly police nothing.

    Silently allowing everything is the exact failure the runner series exists
    to remove: a clean pass that never ran anything.
    """
    monkeypatch.setattr(run_gates, "GATES", ())
    with pytest.raises(hook.DerivationError):
        hook.derive_facts()


# --------------------------------------------------------------------------
# Rules
# --------------------------------------------------------------------------


@pytest.mark.parametrize("command", MUST_DENY)
def test_a_broken_invocation_is_denied(command: str, facts: hook.Facts) -> None:
    assert hook.check_command(command, facts) is not None, (
        f"{command!r} is one of the shapes this hook exists to refuse"
    )


@pytest.mark.parametrize("command", MUST_ALLOW)
def test_a_legitimate_invocation_is_untouched(command: str, facts: hook.Facts) -> None:
    violation = hook.check_command(command, facts)
    assert violation is None, (
        f"{command!r} is legitimate but was denied as {violation and violation.tool!r} "
        "— a false positive is how a hook gets switched off"
    )


@pytest.mark.parametrize("command", MUST_DENY_POWERSHELL)
def test_a_backslash_path_is_policed_under_powershell(
    command: str, facts: hook.Facts
) -> None:
    """The matcher this PR added is the one where `\\` is the normal spelling.

    POSIX escape handling ate the separators — `.\\ruff.exe` lexed as
    `.ruffexe` — so every rule missed it while
    ``test_the_hook_is_registered_for_every_shell_tool`` asserted the coverage
    existed. Registering a matcher whose dialect the parser did not model is
    coverage on paper only.
    """
    assert hook.check_command(command, facts, hook.POWERSHELL) is not None, command


@pytest.mark.parametrize("command", MUST_DENY_BASH_ONLY)
def test_a_backtick_substitution_splits_by_dialect(
    command: str, facts: hook.Facts
) -> None:
    """The same characters run a command in one shell and quote one in the other.

    Bash substitutes inside double quotes, so denying is correct there — the
    command really would run. PowerShell's backtick is the *escape* character,
    so the identical string runs nothing and denying it is a false positive on
    ordinary prose. One parser answering both shells had to pick one, and it
    picked wrong for the shell it had just been registered against.
    """
    assert hook.check_command(command, facts, hook.BASH) is not None, command
    assert hook.check_command(command, facts, hook.POWERSHELL) is None, command


def test_the_wrapper_set_still_names_what_it_claims_to() -> None:
    """A literal oracle, because the behavioural test below derives its cases.

    ``test_a_wrapper_is_unwrapped_only_in_its_bare_form`` iterates
    ``_WRAPPERS``, so removing a name removes the case that would catch it —
    measured, twice: dropping ``setsid`` and dropping ``xargs`` each left the
    whole suite green. That is the third time this session a derived test has
    been blind in exactly this direction, so the oracle is written out.

    ``timeout`` is asserted **absent** on purpose. It owns a mandatory
    positional (the duration), so ``timeout 30 pytest`` would read ``30`` as
    the command word — the failure mode the retreat exists to stop carrying.
    """
    expected = {
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
    actual = set(hook._WRAPPERS)  # pyright: ignore[reportPrivateUsage]
    assert actual == expected, (
        f"the wrapper set changed: added {sorted(actual - expected)}, "
        f"removed {sorted(expected - actual)}. A name leaving silently drops "
        "that wrapper's bare form from policing; a name arriving needs its own "
        "literal in MUST_DENY, since the behavioural test derives its cases."
    )
    assert "timeout" not in actual, (
        "timeout owns a mandatory positional, so unwrapping it reads the "
        "duration as the command word"
    )
    assert "xargs" not in actual, (
        "xargs takes its arguments from stdin, which is not in the command "
        "text — unwrapping it denied `... | xargs pytest`, a scoped run, for "
        "exceeding a timeout it never reaches"
    )


def test_a_wrapper_is_unwrapped_only_in_its_bare_form(facts: hook.Facts) -> None:
    """The retreat, asserted directly rather than through the corpora.

    Four consecutive review rounds each found a different defect in the option
    grammar this replaced — a missing value flag, an optional-argument flag, a
    false positive on ``sudo -h``, and a test for that fix which could not
    fail. The rule now needs no knowledge of any wrapper's own options: the
    next token is either a command word or it is not.

    Both directions matter. Denying the bare form is the value; leaving the
    flagged form alone is what makes the rule safe to hold, because every past
    defect was the walker mis-identifying which token was the command.
    """
    for wrapper in sorted(hook._WRAPPERS):  # pyright: ignore[reportPrivateUsage]
        bare = f"{wrapper} pytest"
        violation = hook.check_command(bare, facts)
        assert violation is not None, (
            f"{bare!r} was allowed; the bare form is the whole point of the set"
        )
        assert violation.tool == "pytest", (violation.tool, bare)

        flagged = f"{wrapper} --some-flag value pytest"
        assert hook.check_command(flagged, facts) is None, (
            f"{flagged!r} was denied. A wrapper carrying options is left alone "
            "on purpose — modelling which of them take a value is the knowledge "
            "this rule exists to stop carrying, and it failed four rounds running"
        )


def test_the_wrapper_retreat_gave_up_only_the_flagged_shapes(
    facts: hook.Facts,
) -> None:
    """The cost of the retreat, pinned so it is visible rather than implied.

    Each of these was denied by the option-grammar version and is allowed now.
    They fail open, which ADR-0077 §5 accepts, but a reader should be able to
    see exactly what was traded away rather than infer it from a comment.

    The claim is measured, not assumed: external review round 2 ran the list
    against the pre-retreat parser and found one row that had been **allowed
    there too** — `timeout --help 30 pytest`, which was never given up because
    it was never denied. That row is gone, so the sentence above is true of
    every row that remains. A count is deliberately not stated: a number here
    would be one more thing to drift.
    """
    given_up = [
        # Wrapper flags carrying a separate value.
        "sudo -u someone pytest",
        "sudo -U bob pytest",
        "env -u PYTHONPATH pytest",
        "nice -n 10 pytest",
        "doas -a login pytest",
        "xargs -a files pytest",
        "xargs -n 4 -e pytest",
        # `--host` in both spellings: mandatory-value, and denied before.
        "sudo --host remotehost pytest",
        "sudo --host=remotehost pytest",
        # The optional-argument family, in both bare and attached spellings.
        "xargs -e pytest",
        "xargs --eof pytest",
        "xargs -l pytest",
        "xargs -eEND pytest",
        "xargs --eof=END pytest",
        # `timeout` left the set entirely: it owns a mandatory positional.
        "timeout 30 pytest",
    ]
    for command in given_up:
        assert hook.check_command(command, facts) is None, (
            f"{command!r} is denied, but the simplified rule is documented as "
            "giving it up — the comment and the behaviour have drifted apart"
        )


def test_every_short_spelling_of_a_long_flag_is_reachable() -> None:
    """A single-dash entry in a long-flag set is only reachable via the shorts.

    This is finding 4's rule rather than finding 4's two flags. ``-h`` and
    ``-V`` sat in ``_PYTEST_SCOPING_FLAGS`` where nothing could ever match
    them, because a single-dash token is routed to the cluster walker — the
    same dead-config shape a set-content assertion had already caught once in
    ``_PYTEST_VALUE_FLAGS``, reintroduced in the sibling set that had no such
    assertion.

    **Only one of the three pairs can currently fail, and saying otherwise was
    an over-claim.** ``_PYTEST_VALUE_FLAGS`` and ``_UV_RUN_VALUE_FLAGS`` hold
    ``--``-prefixed entries only — by design, the former's own comment records
    short entries being removed from it for exactly this unreachability — so
    emptying either shorts set leaves this test green. Measured. They are kept
    in the table as a *guard against a future addition*, which is a real job and
    a different one from "three pairs are checked": the day someone adds ``-x``
    to a long-flag set without ``x`` in its shorts, this fires.
    ``test_the_reachability_check_detects_a_planted_unreachable_flag`` is what
    proves the checker itself works, rather than the sets happening to be clean.
    """
    pairs = [
        (
            "_PYTEST_SCOPING_FLAGS",
            hook._PYTEST_SCOPING_FLAGS,  # pyright: ignore[reportPrivateUsage]
            hook._PYTEST_SCOPING_SHORTS,  # pyright: ignore[reportPrivateUsage]
        ),
        (
            "_PYTEST_VALUE_FLAGS",
            hook._PYTEST_VALUE_FLAGS,  # pyright: ignore[reportPrivateUsage]
            hook._PYTEST_VALUE_SHORTS,  # pyright: ignore[reportPrivateUsage]
        ),
        (
            "_UV_RUN_VALUE_FLAGS",
            hook._UV_RUN_VALUE_FLAGS,  # pyright: ignore[reportPrivateUsage]
            hook._UV_RUN_VALUE_SHORTS,  # pyright: ignore[reportPrivateUsage]
        ),
    ]
    for name, long_flags, shorts in pairs:
        unreachable = {
            flag
            for flag in long_flags
            if not flag.startswith("--") and flag[1:] not in shorts
        }
        assert not unreachable, (
            f"{name} holds {sorted(unreachable)}, which a single-dash token can "
            "never match — it is routed to the cluster walker, whose set does "
            "not carry those characters. Dead config that reads as cover."
        )


def test_every_long_value_flag_actually_consumes_its_value() -> None:
    """The other dead-config shape, in the set that had no guard.

    ``test_every_short_spelling_of_a_long_flag_is_reachable`` catches a
    *single-dash* entry sitting in a long-flag set. This catches the mirror:
    a **long** entry whose value is never consumed because the branch that
    handles long options returns before the value test runs. Measured —
    ``--check-hash-based-pycs`` was the only long member of
    ``_PYTHON_VALUE_FLAGS`` and was unreachable, so
    ``python --check-hash-based-pycs always -m pytest`` ran the full serial
    suite. External review found it; the sibling guard could not see it.

    Asserted behaviourally rather than structurally: the question is whether
    the walker *consumes* the value, which only running it can answer.
    """
    for flag in sorted(hook._PYTHON_VALUE_FLAGS):  # pyright: ignore[reportPrivateUsage]
        if not flag.startswith("--"):
            continue
        args = [flag, "some-value", "-m", "pytest"]
        assert hook.unwrap_python_module(args) == ("pytest", ()), (
            f"{flag!r} did not consume its value: {args} unwrapped to "
            f"{hook.unwrap_python_module(args)!r}. Its value was read as a "
            "script path, so the walk stopped and the module was never found."
        )


def test_the_reachability_check_detects_a_planted_unreachable_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A positive control for the check above.

    Two of that test's three pairs pass trivially because their long-flag sets
    hold no single-dash entry, so a green there is equally consistent with the
    check working and with the check being inert. Planting the defect it exists
    to find is the only thing that separates those.
    """
    monkeypatch.setattr(
        hook,
        "_PYTEST_VALUE_FLAGS",
        hook._PYTEST_VALUE_FLAGS | {"-Z"},  # pyright: ignore[reportPrivateUsage]
    )
    with pytest.raises(AssertionError, match="-Z"):
        test_every_short_spelling_of_a_long_flag_is_reachable()


@pytest.mark.parametrize("opener", ["<", ">"])
def test_both_process_substitution_spellings_take_the_substitution_branch(
    opener: str,
) -> None:
    """Pinned on the token stream, because the verdict cannot tell them apart.

    A corpus entry cannot catch this and it is worth saying why: narrowing the
    branch from ``"<>"`` to ``"<"`` leaves every *verdict* unchanged. Unquoted,
    ``tee >(pytest)`` still denies — the ``>`` falls through to the redirect
    path, ``(`` splits as a separator, and ``pytest`` is reached anyway.
    Quoted, it is allowed either way. Only the token stream differs, so that is
    what this asserts.

    The difference matters even though today's verdicts agree: the two spellings
    are one branch in the code and its comment claims parity, so a reader may
    rely on it. A mutation narrowing it survived the whole 304-test suite.
    """
    command = f"tee {opener}(pytest)"
    commands = hook.split_commands(command)
    assert ["tee", hook._SUBSTITUTION_PLACEHOLDER] in commands, (  # pyright: ignore[reportPrivateUsage]
        f"{command!r} did not take the substitution branch — the wrapper kept "
        f"no placeholder argument: {commands}"
    )
    assert ["pytest"] in commands, (
        f"{command!r} did not queue its inner command: {commands}"
    )


def test_a_quoted_paren_does_not_close_a_substitution_early(
    facts: hook.Facts,
) -> None:
    """The other half of `_close_paren`'s fix, which had no coverage at all.

    Its docstring claims two independent repairs — heredoc bodies and quoted
    parentheses. Only the heredoc half was tested: a reviewer removed the
    quote-tracking block entirely and the whole suite stayed green. A docstring
    making a falsifiable claim that nothing checks is the shape this module has
    been caught by repeatedly.

    The failure it guards is *silent*: a `)` inside a quoted string closed the
    substitution early, `_tokenize` then raised on the unbalanced quote, and
    `split_commands` discarded the entire chunk — taking a real command chained
    after it out of scope. So the assertion is that the chained command is
    still reached, not merely that nothing crashed.
    """
    command = 'pytest $(echo "a)b") tests/x.py; ruff check .'
    commands = hook.split_commands(command)
    assert ["ruff", "check", "."] in commands, (
        f"the chained command was lost: {commands}. A `)` inside the quoted "
        "substitution body closed it early and the whole chunk was discarded"
    )
    violation = hook.check_command(command, facts)
    assert violation is not None, "the chained `ruff check .` must still be denied"
    assert violation.tool == "ruff", violation.tool


def test_an_unterminated_heredoc_is_treated_as_body(facts: hook.Facts) -> None:
    """An accepted limitation, pinned rather than left to inspection.

    A heredoc with no closing delimiter swallows the rest of the string, so a
    tool named on a later line is never inspected. That matches the real shell —
    which would sit waiting for the delimiter and never run that line either —
    so it is not a bypass, but it is a branch nothing else exercises. Recorded
    the way the substituted-command-word limit already is.
    """
    assert hook.check_command("echo hi <<EOF\npytest\n", facts) is None
    # The command word *before* the heredoc is unaffected, which is what keeps
    # this a documented edge rather than a hole.
    assert hook.check_command("pytest <<EOF\nsome body\n", facts) is not None


def test_the_flag_sets_still_contain_what_the_tools_document() -> None:
    """The sets are asserted against literals, deliberately.

    The behavioural tests below parametrise *over* these sets, which makes them
    blind in the one direction that matters: shrinking a set shrinks the test
    with it, so deleting four short flags left every parametrised case passing.
    Measured — two mutations survived exactly that way.

    So the oracle has to live outside the code under test. These literals were
    read from ``uv run --help`` (uv 0.11.28) and pytest's own option list; a
    failure here means the tool's interface moved and the sets need re-reading,
    which is a real event worth a red rather than a nuisance.

    Subset rather than equality: a flag added here is at worst a redundant skip,
    while a flag *missing* is the live bypass this hook exists to close.
    """
    assert set("CPfipw") <= hook._UV_RUN_VALUE_SHORTS  # pyright: ignore[reportPrivateUsage]
    assert set("w") <= hook._UV_RUN_SUPPLY_SHORTS  # pyright: ignore[reportPrivateUsage]
    assert {
        "--with",
        "--with-editable",
        "--with-requirements",
        "--python",
        "--index-url",
        "--find-links",
        "--constraint",
        "--override",
    } <= hook._UV_RUN_VALUE_FLAGS  # pyright: ignore[reportPrivateUsage]
    assert {"--with", "--with-editable"} <= hook._UV_RUN_SUPPLY_FLAGS  # pyright: ignore[reportPrivateUsage]
    assert "--from" in hook._UVX_VALUE_FLAGS  # pyright: ignore[reportPrivateUsage]
    assert set("pocWr") <= hook._PYTEST_VALUE_SHORTS  # pyright: ignore[reportPrivateUsage]
    assert set("nkm") <= hook._PYTEST_SCOPING_SHORTS  # pyright: ignore[reportPrivateUsage]
    assert {"-p", "-o", "-c", "-n", "-k"} & hook._PYTEST_VALUE_FLAGS == set()  # pyright: ignore[reportPrivateUsage]
    assert {
        "--tb",
        "--junitxml",
        "--override-ini",
        "--rootdir",
        "--basetemp",
    } <= hook._PYTEST_VALUE_FLAGS  # pyright: ignore[reportPrivateUsage]
    assert {"--lf", "--ff", "--collect-only", "--numprocesses"} <= (
        hook._PYTEST_SCOPING_FLAGS  # pyright: ignore[reportPrivateUsage]
    )


@pytest.mark.parametrize("flag", sorted(hook._UV_RUN_VALUE_FLAGS))  # pyright: ignore[reportPrivateUsage]
def test_every_uv_run_value_flag_hides_its_value_from_the_tool_slot(
    flag: str, facts: hook.Facts
) -> None:
    """Each registered value flag must consume its value, in the spaced form.

    Parametrised over the set rather than spot-checked, because spot-checking is
    what left this hole: 18 of 21 entries had no coverage at all, and mutating
    ``--python`` to a nonsense spelling passed the whole module. A flag the
    walker fails to recognise makes its *value* the command word, so the real
    tool is never inspected.

    pytest is the probe because the serial-suite rule fires on it regardless of
    what a supply flag claims to provide, which keeps this test measuring flag
    consumption rather than the exemption logic.
    """
    assert hook.check_command(f"uv run {flag} somevalue pytest", facts) is not None, (
        f"`uv run {flag} somevalue pytest` was allowed — {flag} did not consume "
        "its value, so 'somevalue' was read as the tool and pytest never checked"
    )


@pytest.mark.parametrize("flag", sorted(hook._PYTEST_VALUE_FLAGS))  # pyright: ignore[reportPrivateUsage]
def test_every_pytest_value_flag_is_not_mistaken_for_a_test_path(
    flag: str, facts: hook.Facts
) -> None:
    """A flag's value is not a path, so it cannot make a full run look scoped."""
    assert hook.check_command(f"pytest {flag} somevalue", facts) is not None, (
        f"`pytest {flag} somevalue` was allowed — 'somevalue' was read as a test "
        "path, so the full serial run was mistaken for a scoped one"
    )


@pytest.mark.parametrize("short", sorted(hook._UV_RUN_VALUE_SHORTS))  # pyright: ignore[reportPrivateUsage]
def test_every_uv_run_short_value_flag_consumes_its_value(
    short: str, facts: hook.Facts
) -> None:
    """Short spellings are a separate code path from their long twins.

    `-i`, `-f`, `-P` and `-C` were all absent while their long forms were
    present, which let an ordinary index or find-links argument carry a full
    serial pytest run straight through.
    """
    assert hook.check_command(f"uv run -{short} somevalue pytest", facts) is not None


def test_a_tool_named_inside_an_argument_is_not_an_invocation(
    facts: hook.Facts,
) -> None:
    """Searching for a bad command must not trip the hook that forbids it.

    Writing about these commands — in a grep, a commit message, a doc edit — is
    ordinary work. Only the command word counts.
    """
    for command in (
        'grep -rn "uv run ruff" .',
        "echo 'run pytest to check'",
        'git commit -m "stop using uvx ruff"',
        'rg --glob "*.md" "uvx pymarkdownlnt"',
        # A separator *inside* quotes is text, not a chain. The tokenizer has to
        # honour quoting to tell this apart from `true; ruff format .`, which
        # must be denied — both shapes are pinned so a tokenizer swap cannot
        # trade one for the other.
        'echo "a; ruff check ."',
        "git commit -m 'ban uvx ruff && uv run pyright'",
    ):
        assert hook.check_command(command, facts) is None, command


def test_a_chained_or_substituted_invocation_is_reached(facts: hook.Facts) -> None:
    """The natural way to type these is behind a ``cd &&``, so it must be seen."""
    for command in (
        "cd /repo && uv run ruff check .",
        "ruff check . || echo failed",
        "echo $(pyright)",
        "true; ruff format .",
    ):
        assert hook.check_command(command, facts) is not None, command


def test_nested_substitution_is_reached_where_the_command_word_is_literal(
    facts: hook.Facts,
) -> None:
    """Nesting in an *argument* is caught; nesting that hides the command is not.

    The boundary is pinned in both directions on purpose, because the second
    half is a real limitation and recording it as a passing test is how it stays
    honest rather than being quietly rediscovered.

    Caught: the substitution sits in an argument and the invocation is still
    literal text, which is the shape a chained command actually takes.

    Not caught: the *command word itself* is the output of a substitution. No
    static parser can know `$(echo ruff)` means `ruff` without running it, so
    the hook fails open. That is the correct direction for a convenience
    mechanism — it is the failure ADR-0077 accepts, not one it overlooked.
    """
    assert hook.check_command("cd $(dirname $(which uv)) && ruff check .", facts)
    assert hook.check_command("uv run ruff check $(pwd)", facts)

    assert hook.check_command("result=$(uvx $(echo ruff) check .)", facts) is None, (
        "this is expected to slip through — if it now denies, the limitation "
        "documented in ADR-0077 has been closed and this test should say so"
    )


def test_a_pinned_uvx_invocation_is_the_correct_form(facts: hook.Facts) -> None:
    """The pin is the whole difference; both spellings of it must pass."""
    assert hook.check_command('uvx "ruff@0.15.21" check .', facts) is None
    assert hook.check_command("uvx ruff@0.15.21 check .", facts) is None
    assert hook.check_command("uvx ruff check .", facts) is not None


def test_a_scoped_pytest_is_never_denied(facts: hook.Facts) -> None:
    """Only the unscoped whole-suite run is refused.

    The work item scoping this hook named targeted pytest as the false-positive
    machine to avoid, so each way of scoping a run is pinned separately.
    """
    for command in (
        "pytest tests/test_pool.py",
        "pytest -k canary",
        "pytest -n auto",
        "pytest -n 4",
        "pytest --numprocesses=auto",
        "pytest --collect-only",
        "pytest tests/test_pool.py::test_thing",
    ):
        assert hook.check_command(command, facts) is None, command


def test_the_remedy_names_the_gate_that_actually_runs_the_tool(
    facts: hook.Facts,
) -> None:
    """A denial is only useful if the command it suggests is the right one.

    Collecting ``--with`` packages as tools once mis-attributed pytest to the
    pyright gate, so a serial-pytest denial told the reader to run pyright.
    """
    assert facts.selector["pytest"] == "pytest"
    assert facts.selector["pyright"] == "pyright"
    assert facts.selector["pymarkdown"] == "markdown-lint"
    # The multi-gate case, and the only one the group-collapsing branch of
    # `_selectors` decides. `ruff` is produced by `ruff-check` *and*
    # `ruff-format`, and naming either alone is the historical bug that
    # function's docstring records — a denial of `uvx ruff format .` telling
    # the author to run the linter. Measured: with that branch forced dead the
    # selector becomes the joined `"ruff-check ruff-format"` and nothing else
    # in the suite noticed, so the fix for a real bug had no regression test.
    assert facts.selector["ruff"] == "ruff"

    violation = hook.check_command("pytest", facts)
    assert violation is not None
    remedy = violation.remedy(facts)
    assert "run_gates.py pytest" in remedy
    assert "pyright" not in remedy


def test_every_denial_points_at_the_runner(facts: hook.Facts) -> None:
    """No denial may be a dead end — each one names what to run instead."""
    for command in MUST_DENY:
        violation = hook.check_command(command, facts)
        assert violation is not None
        remedy = violation.remedy(facts)
        assert hook.RUNNER in remedy, command
        assert "--list" in remedy, command


# --------------------------------------------------------------------------
# Hook protocol
# --------------------------------------------------------------------------


def _payload(command: str, tool: str = "Bash") -> dict[str, Any]:
    return {
        "session_id": "test",
        "hook_event_name": "PreToolUse",
        "tool_name": tool,
        "tool_input": {"command": command},
    }


def test_a_denial_uses_the_documented_hook_json_shape() -> None:
    result = hook.evaluate(_payload("uv run ruff check ."))
    assert result is not None
    raw = result["hookSpecificOutput"]
    assert isinstance(raw, dict)
    specific = cast("dict[str, object]", raw)
    assert specific["hookEventName"] == "PreToolUse"
    assert specific["permissionDecision"] == "deny"
    assert "run_gates.py" in str(specific["permissionDecisionReason"])


def test_a_clean_command_produces_no_decision() -> None:
    """Silence is the contract: no JSON means the normal permission flow runs."""
    assert hook.evaluate(_payload("git status")) is None


def test_a_broken_derivation_allows_the_command_and_says_so(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A hook that cannot work must not block, and must not go quiet either.

    Both halves matter. Blocking would brick the session on the hook's own bug;
    silence would turn "stopped policing" into something nobody can observe.
    Crucially the notice carries no ``permissionDecision`` — emitting ``allow``
    would auto-approve the command, a downgrade the hook has no business making
    because it broke.
    """

    def broken() -> hook.Facts:
        raise hook.DerivationError("ci.yml went missing")

    monkeypatch.setattr(hook, "derive_facts", broken)

    result = hook.evaluate(_payload("uv run ruff check ."))
    assert result is not None
    assert "hookSpecificOutput" not in result
    message = str(result["systemMessage"])
    assert "ci.yml went missing" in message
    assert "unchecked" in message


def test_an_unexpected_crash_allows_the_command_too(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fail-open covers the bug the derivation guard did not anticipate.

    ``DerivationError`` is the failure the hook knows how to have. A parser bug
    raising ``IndexError`` is the one it does not, and to the person whose
    command stopped working the two are the same event. Anything escaping
    ``evaluate`` becomes a traceback on stderr and a non-zero exit, which is a
    hook error on every shell call.
    """

    def exploding(_text: str, _facts: hook.Facts, _shell: str = hook.BASH) -> None:
        raise IndexError("list index out of range")

    monkeypatch.setattr(hook, "check_command", exploding)

    result = hook.evaluate(_payload("uv run ruff check ."))
    assert result is not None
    assert "hookSpecificOutput" not in result
    message = str(result["systemMessage"])
    assert "IndexError" in message
    assert "unchecked" in message


@pytest.mark.parametrize(
    ("spec", "pinned"),
    [
        ("ruff@0.15.21", True),
        ("pymarkdownlnt@0.9.39", True),
        ("ruff@latest", False),
        ("ruff@LATEST", False),
        ("ruff@", False),
        ("ruff", False),
    ],
)
def test_only_a_version_that_cannot_drift_counts_as_pinned(
    spec: str, pinned: bool
) -> None:
    """The oracle lives outside the corpora, deliberately.

    ``MUST_DENY`` proves *some* unpinned spelling is refused; it cannot show
    that the pin test is the right predicate. ``"@" in spec`` passes every
    corpus entry that existed before ``@latest`` was added to it.
    """
    assert hook.is_pinned(spec) is pinned


@pytest.mark.parametrize(
    ("spec", "name", "version"),
    [
        ("ruff@0.15.21", "ruff", "0.15.21"),
        ("ruff@latest", "ruff", "latest"),
        ("ruff@", "ruff", ""),
        ("ruff", "ruff", None),
        ('"ruff@0.15.21"', "ruff", "0.15.21"),
        # The requirement grammar the same parser now serves.
        ("ruff>=0.15", "ruff", None),
        ("ruff~=0.15.0", "ruff", None),
        # The one restatement, as this caller applies it. "Applied consistently
        # through every caller" is what this comment used to say, and it was
        # false: the bare command word in `classify` did not map, which is the
        # hole `test_both_spellings_of_a_tool_are_refused_at_every_call_site` now
        # closes. A claim of universality in a comment is a checklist, and this
        # one had never been walked.
        ("pymarkdownlnt@0.9.39", "pymarkdown", "0.9.39"),
        ("", "", None),
    ],
)
def test_one_grammar_answers_which_distribution_a_spec_names(
    spec: str, name: str, version: str | None
) -> None:
    """Four sites asked this question in three spellings.

    ``^(...)@``, ``^(...)``, and two separate ``partition("@")`` calls on the
    same string. None of them was wrong, which is the point: the module's own
    comment records it having already been bitten by two hand-rolled loops over
    one argument shape drifting apart, and this is that shape again.

    ``None`` and ``""`` are deliberately distinct versions. ``ruff`` requested
    no version; ``ruff@`` requested one and named nothing — and only the second
    is a typo worth denying.
    """
    parsed = hook.parse_spec(spec)
    assert parsed.name == name
    assert parsed.version == version


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        (["-m", "pytest"], ("pytest", ())),
        (["-m", "pytest", "-n", "auto"], ("pytest", ("-n", "auto"))),
        (["-mpytest", "-x"], ("pytest", ("-x",))),
        (["-X", "dev", "-m", "pytest"], ("pytest", ())),
        (["-W", "ignore", "-m", "pytest"], ("pytest", ())),
        # CPython clusters single-character flags with `-m`. Matching `-m` only
        # in leading position let these through while the docstring claimed
        # both spellings were covered.
        (["-um", "pytest"], ("pytest", ())),
        (["-bm", "pytest", "-x"], ("pytest", ("-x",))),
        (["-umpytest"], ("pytest", ())),
        (["-Sum", "pytest"], ("pytest", ())),
        # A value-taking option ends the cluster: the rest is its value.
        (["-Wignore", "-m", "pytest"], ("pytest", ())),
        # ...and when the cluster ends there, the value is the NEXT token, which
        # must be consumed too or it reads as a script path and ends the walk.
        (["-uX", "dev", "-m", "pytest"], ("pytest", ())),
        (["-uW", "ignore", "-m", "pytest"], ("pytest", ())),
        (["-uXdev", "-m", "pytest"], ("pytest", ())),
        # `-c` inside a cluster still means "the rest is program text".
        (["-uc", "print(1)"], None),
        # `pymarkdownlnt` is the package; the module maps to its console script.
        (["-m", "pymarkdownlnt", "scan", "."], ("pymarkdown", ("scan", "."))),
        # Nothing to unwrap.
        (["-c", "print(1)"], None),
        (["script.py", "-m", "ruff"], None),
        (["-m"], None),
        ([], None),
    ],
)
def test_the_python_module_walker_finds_the_tool_and_stops_at_the_script(
    args: list[str], expected: tuple[str, tuple[str, ...]] | None
) -> None:
    assert hook.unwrap_python_module(args) == expected


# Every `_console_name` call site a *typed* word can reach, one case each, with
# both spellings of the one tool whose package and console script differ.
#
# The unit is the **call site**, not the command shape, and that distinction is
# the whole point: a first draft of this list enumerated "routes" and got the
# pair wrong in both directions while still counting five. `uv tool run` looks
# like a route of its own and is not one — `classify` rewrites it to `uvx`
# *after* computing `program`, so it lands on the same `parse_spec` call — while
# the attached `-m` spelling looks like the spaced one and is a separate
# `return` with a separate call, which is how a surviving mutation hid there.
# Five sites, six entries: the sixth pins that the rewrite still happens before
# resolution, which is the one thing that would break if the two were reordered.
#
# Written out by hand rather than generated from `_CONSOLE_SCRIPTS` or from the
# branches in `classify`: a test that builds its cases from the thing it checks
# cannot see a case leave, and the case that mattered here had never been
# present at all.
_BOTH_SPELLINGS = [
    # (call site, package-name spelling, console-script spelling)
    ("classify bare word", "pymarkdownlnt scan .", "pymarkdown scan ."),
    ("parse_uv_run", "uv run pymarkdownlnt scan .", "uv run pymarkdown scan ."),
    (
        "unwrap_python_module spaced",
        "python -m pymarkdownlnt scan .",
        "python -m pymarkdown scan .",
    ),
    (
        "unwrap_python_module attached",
        "python -mpymarkdownlnt scan .",
        "python -mpymarkdown scan .",
    ),
    ("parse_spec", "uvx pymarkdownlnt scan .", "uvx pymarkdown scan ."),
    (
        "parse_spec via the uv-tool-run rewrite",
        "uv tool run pymarkdownlnt scan .",
        "uv tool run pymarkdown scan .",
    ),
]


@pytest.mark.parametrize(
    ("site", "package_spelling", "console_spelling"),
    _BOTH_SPELLINGS,
    ids=[site for site, _pkg, _console in _BOTH_SPELLINGS],
)
def test_both_spellings_of_a_tool_are_refused_at_every_call_site(
    site: str,
    package_spelling: str,
    console_spelling: str,
    facts: hook.Facts,
) -> None:
    """Neither spelling can work, so the hook has to refuse both on every route.

    ``pymarkdownlnt`` is the package and ``pymarkdown`` is its console script,
    and the mapping between them is the module's one restatement. The bug this
    pins is that the restatement was applied at four of its five reachable call
    sites and not at the fifth — ``parse_uv_run``, both ``return``\\ s in
    ``unwrap_python_module`` and ``parse_spec`` each mapped, while ``classify``'s
    bare command word did not. A bare ``pymarkdownlnt`` was therefore allowed and
    exited 127, and it is the *likelier* spelling to be typed from memory: it is
    the one ``ci.yml``, ``pyproject.toml`` and ADR-0062 use when naming the tool
    to install or run. (All three also write ``pymarkdown``, as the
    ``[tool.pymarkdown]`` config table — a spelling nobody types at a shell.)

    The site list is the oracle. Deriving it from the branches in ``classify``
    would have reproduced the omission exactly, since the omission was a branch
    that did not do the mapping rather than a branch that was missing.

    Mutations this survives, each measured rather than reasoned about. Dropping
    ``_console_name`` from ``classify`` fails ``classify bare word`` and no other
    case here — though it reddens tests elsewhere in this file too, so "no other"
    is an accounting of these cases and not of the blast radius, which is
    deliberately left un-numbered because it grows with the suite.
    Dropping it at each of the other four sites fails that site's case, and the
    two interesting ones fail **nothing else in the suite**: the *attached*
    ``return`` in ``unwrap_python_module``, and ``parse_uv_run``. Both were
    unwatched before this test — the corpus carried ``uv run pymarkdown`` but
    never ``uv run pymarkdownlnt``, and no fixture anywhere used the attached
    spelling with this tool — so the mapping could have been deleted at either
    one and the file would have stayed green. Emptying ``_CONSOLE_SCRIPTS`` fails
    every case, since the derived set is then keyed by the package name;
    inverting the mapping fails every case too.

    That last one is why the ``violation.tool`` and remedy assertions are here
    rather than a bare "was it denied". Under the inverted mapping **both**
    corpus entries still deny — ``classify`` and the derivation move together,
    so the set is keyed by ``pymarkdownlnt`` and every site reaches it — and
    the only observable damage is that the denial names a tool
    ``facts.selector`` has no entry for, degrading the remedy from
    ``run_gates.py markdown-lint`` to a bare runner call. A corpus of commands
    cannot see that; it is a property of the message.
    """
    for command in (package_spelling, console_spelling):
        violation = hook.check_command(command, facts)
        assert violation is not None, f"{site}: {command!r} was allowed"
        # The denial names the console script at every site, which is what
        # keeps `facts.selector` — keyed by console script — reachable, so the
        # remedy names the gate rather than degrading to a bare runner call.
        assert violation.tool == "pymarkdown"
        assert "run_gates.py markdown-lint" in violation.remedy(facts)


@pytest.mark.parametrize(
    "spelling",
    [
        "pytest",
        "pytest.exe",
        "PYTEST",
        "./pytest",
        "bin/pytest",
        # Quoted, because an *unquoted* backslash is eaten by the lexer before
        # any of this runs -- `uv run .\\pytest` reaches `program_name` as
        # `.pytest`. That is a property of shell quoting rather than of the
        # normalization under test, and `program_name`'s own separator cases
        # below cover the backslash directly.
        r'".\pytest"',
        r'"C:\Tools\pytest.exe"',
    ],
)
def test_a_path_or_extension_bearing_uv_run_word_still_reaches_every_rule(
    spelling: str, facts: hook.Facts
) -> None:
    """Report finding 5: only the *python* question was normalized.

    `classify` normalizes its own bare command word on its first line, but the
    `uv run` branch compares `parsed.executed` — which `_console_name` alone
    produced, and `_console_name` strips nothing. Measured:
    `parse_uv_run(['pytest.exe','-v']).executed == 'pytest.exe'` and
    `['./pytest','-v']` gave `'./pytest'`, while `program_name('pytest.exe')`
    has always been `'pytest'`.

    So `uv run pytest.exe` and `uv run ./pytest` never reached
    `tool == "pytest" and _pytest_is_full_suite(tool_args)` — the denial whose
    own message says such a run "exceeds the 600 s command timeout and is
    killed" — and the same words escaped the `facts.ephemeral` comparison
    beside it. The interpreter question was the one comparison already repaired,
    inside `_is_python_program`; normalizing at the source repairs all three.

    The `-n auto` control is the discriminating half: a normalization that
    denied everything would satisfy the first assertion alone.
    """
    denied = hook.check_command(f"uv run {spelling}", facts)
    assert denied is not None, f"{spelling}: a serial full suite was allowed"
    assert denied.tool == "pytest"

    scoped = hook.check_command(f"uv run {spelling} -n auto", facts)
    assert scoped is None, f"{spelling}: a distributed run was denied"


@pytest.mark.parametrize(
    "spelling", ["pymarkdownlnt.exe", "./pymarkdownlnt", "PyMarkdownLnt"]
)
def test_the_ephemeral_comparison_sees_through_a_path_or_extension(
    spelling: str, facts: hook.Facts
) -> None:
    """The second comparison finding 5's one-line fix repairs.

    `tool in facts.ephemeral` is keyed by console-script name, so every spelling
    below missed it and `uv run pymarkdownlnt.exe scan .` was allowed — a
    command that exits 2, since uv cannot spawn a tool that is not a project
    dependency. Kept separate from the pytest case above because the two
    comparisons sit on different branches and a fix could repair one alone.
    """
    violation = hook.check_command(f"uv run {spelling} scan .", facts)

    assert violation is not None, f"{spelling} was allowed"
    assert violation.tool == "pymarkdown"


@pytest.mark.parametrize(
    ("word", "expected"),
    [
        # Lower-casing must come *before* the suffix strip, or an upper-cased
        # extension survives it. Reversed, `removesuffix(".exe")` sees `.EXE`,
        # matches nothing, and `FOO.EXE` normalises to `foo.exe` — a name no
        # rule can match.
        ("FOO.EXE", "foo"),
        ("Ruff.Exe", "ruff"),
        # One case per separator, because the split has to honour both on every
        # platform: narrowing the pattern to `[\\]` is caught only by `./ruff`,
        # and narrowing it to `[/]` only by the backslash path. Neither is
        # padding, and a bare `ruff` case was: measured, it survives even a
        # `return word` identity mutation, so it detected nothing at all.
        (r"C:\Tools\Ruff.EXE", "ruff"),
        ("./ruff", "ruff"),
    ],
)
def test_a_command_word_is_lowercased_before_its_extension_is_stripped(
    word: str, expected: str
) -> None:
    """Asserted directly, because nothing else asserts it on purpose.

    Reversing the two operations does redden this suite today — but only by
    accident, and only here: ``shutil.which("uv")`` on the machine this was
    written on resolves to ``uv.EXE``, so the reversal broke ``derive_facts``
    outright and took 241 tests down with it as collateral. That is coverage
    resting on one developer's binary casing. A runner whose ``uv`` is installed
    lower-case would see the same reversal pass every gate, and the two POSIX
    legs have no ``.exe`` to expose it at all — so the regression could reach
    ``main`` green on all three.

    ``program_name`` is not changed by the work that added this test; the gap is
    older than the branch. It is closed here because the branch's own subject is
    the order in which a command word is normalised, and this is the same
    composition one layer down.

    **What this cannot catch on Windows, stated because a local green here is
    not evidence.** The function's docstring names ``Path(word).name`` as the
    spelling it deliberately avoids, and substituting it *is* caught by the
    backslash case above — on the two POSIX legs, where ``Path`` leaves a
    backslash path whole. On Windows ``Path`` splits it correctly, so the
    substitution is invisible to every case here and to the rest of the suite
    (measured). That is inherent: no input distinguishes the two on Windows
    while still asserting a contract worth having, since where they differ —
    trailing separators, drive-relative ``C:ruff`` — ``Path``'s answer is
    arguably the better one. The property is held by CI's POSIX legs and cannot
    be held locally on this machine.
    """
    assert hook.program_name(word) == expected


@pytest.mark.parametrize("word", ["./pymarkdownlnt", "pymarkdownlnt.exe"])
def test_the_bare_word_is_mapped_after_the_lexical_normalisation(
    word: str, facts: hook.Facts
) -> None:
    """`classify` composes `_console_name(program_name(w))`, in that order.

    Reversed, the lookup runs against a word that is not yet a bare name —
    ``"./pymarkdownlnt"`` and ``"pymarkdownlnt.exe"`` are not
    ``_CONSOLE_SCRIPTS`` keys, so the mapping misses and the command is allowed.
    Measured: under the reversal both spellings below resolve to
    ``pymarkdownlnt`` instead of ``pymarkdown``.

    This needs its own test because the case that *names* this call site cannot
    reach the defect. A bare ``pymarkdownlnt`` is already lexically normal, so
    the two orders coincide on it and
    ``test_both_spellings_of_a_tool_are_refused_at_every_call_site[classify bare
    word]`` stays green under the reversal — measured. The regression was caught
    only by ``MUST_DENY_POWERSHELL``'s ``.\\pymarkdownlnt.exe`` entry, which is
    there to pin that a Windows path composes with the mapping at all; resting a
    named site's coverage on an unrelated entry's incidental reach means an edit
    to that entry silently un-covers this.

    Both forms are bash-legal, so this does not duplicate the PowerShell entry:
    that one pins the dialect, this one pins the order.
    """
    assert hook.check_command(f"{word} scan .", facts) is not None


def test_malformed_input_is_ignored_rather_than_fatal() -> None:
    assert hook.evaluate({}) is None
    assert hook.evaluate({"tool_input": "not-a-dict"}) is None
    assert hook.evaluate({"tool_input": {}}) is None
    assert hook.evaluate({"tool_input": {"command": "   "}}) is None
    assert hook.evaluate({"tool_input": {"command": 17}}) is None


def test_an_unparseable_command_is_allowed_rather_than_guessed() -> None:
    """A quoting error is the shell's to report; guessing risks a false deny."""
    assert hook.split_commands('ruff check "unterminated') == []


def test_the_script_runs_as_a_subprocess_and_denies_on_stdout() -> None:
    """End to end, the way Claude Code invokes it: JSON in, JSON out, exit 0.

    Exit 0 is part of the contract and easy to get wrong: exit 2 would block via
    the code path as well, producing a second denial with a different message,
    and any non-zero exit shows the user a hook error on a working hook.
    """
    proc = subprocess.run(  # noqa: S603 - fixed executable, no shell
        [sys.executable, str(HOOK_SCRIPT)],
        input=json.dumps(_payload("uvx ruff check .")),
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    emitted = json.loads(proc.stdout)
    specific = emitted["hookSpecificOutput"]
    assert specific["permissionDecision"] == "deny"
    assert "run_gates.py" in specific["permissionDecisionReason"]


def test_the_script_is_silent_for_a_clean_command() -> None:
    proc = subprocess.run(  # noqa: S603 - fixed executable, no shell
        [sys.executable, str(HOOK_SCRIPT)],
        input=json.dumps(_payload("git status")),
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == ""


def test_garbage_on_stdin_exits_cleanly() -> None:
    proc = subprocess.run(  # noqa: S603 - fixed executable, no shell
        [sys.executable, str(HOOK_SCRIPT)],
        input="not json at all",
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0
    assert proc.stdout.strip() == ""


# --------------------------------------------------------------------------
# Settings wiring — the half the script cannot observe about itself
# --------------------------------------------------------------------------


def _hook_entries() -> list[dict[str, Any]]:
    raw = cast("dict[str, Any]", json.loads(SETTINGS.read_text(encoding="utf-8")))
    hooks = cast("dict[str, Any]", raw.get("hooks", {}))
    entries = cast("list[dict[str, Any]]", hooks.get("PreToolUse", []))
    assert isinstance(entries, list)
    return entries


def test_the_hook_is_registered_for_every_shell_tool() -> None:
    """Bash alone leaves the whole class reachable through the other tool.

    This environment exposes a PowerShell tool beside Bash, and ``matcher``
    filters by tool name only. A deny list keyed per tool and missing one of
    them is the silently-routed-around failure ``specs/open-questions.md``
    already records this project paying once, with a different vendor's rules.
    """
    matchers = {entry.get("matcher") for entry in _hook_entries()}
    assert {"Bash", "PowerShell"} <= matchers, (
        f"the gate-invocation hook covers {sorted(m for m in matchers if m)} — a "
        "shell tool with no matcher is an uncovered route to every command the "
        "hook refuses"
    )


def test_every_registered_matcher_points_at_the_hook_script() -> None:
    for entry in _hook_entries():
        commands = [h.get("command", "") for h in entry.get("hooks", [])]
        assert any(HOOK_SCRIPT.name in str(c) for c in commands), entry


def test_the_registered_interpreter_is_a_bare_portable_name() -> None:
    """The shared settings file may not name a machine-specific interpreter.

    Two rules meet here. ADR-0071 requires content landing in the *tracked*
    ``settings.json`` to be free of machine-specific paths — anything local-only
    belongs in ``settings.local.json`` — so an absolute interpreter path is
    forbidden outright. And the name has to be one that resolves on every
    machine a contributor might use, which is why ``python3`` is registered
    despite being measurably slower here (396 ms against 159 ms for ``python``,
    a Store-shim artefact of this one machine); ADR-0077 §8 records that trade.

    Deliberately a property of the repository rather than of the host: asserting
    that the interpreter resolves *here* would run on all three CI legs and go
    red wherever the spelling happens to be absent, a failure about the runner
    image rather than about this change. The allowlist still kills a typo,
    which is the defect actually worth catching.
    """
    allowed = {"python3", "uv"}
    for entry in _hook_entries():
        for handler in entry.get("hooks", []):
            command = str(handler.get("command", ""))
            interpreter = command.split()[0].strip('"')
            assert interpreter in allowed, (
                f"the hook is registered to run under {interpreter!r}; the "
                f"tracked settings file may only name {sorted(allowed)}, since "
                "an absolute or machine-local interpreter is exactly what "
                "ADR-0071 keeps out of the shared file"
            )
            assert not Path(interpreter).is_absolute(), interpreter


def test_the_hook_script_is_the_one_the_settings_name() -> None:
    assert HOOK_SCRIPT.is_file()
    for entry in _hook_entries():
        for handler in entry.get("hooks", []):
            command = str(handler.get("command", ""))
            assert HOOK_SCRIPT.name in command, entry


# --------------------------------------------------------------------------
# Launch failure — the class that bricked a session
# --------------------------------------------------------------------------
#
# The hook was first registered as `python3 "${CLAUDE_PROJECT_DIR}/scripts/
# check_gate_invocation.py"`, and in a git-worktree session — this project's
# normal working mode — `${CLAUDE_PROJECT_DIR}` resolves to the *primary*
# checkout while the script exists only in the worktree. The interpreter could
# not open the file, and every shell call in that session ended in a hook error.
#
# Two things were wrong and only one was visible. The path was wrong, and
# `test_the_registered_interpreter_is_a_bare_portable_name` — written for
# exactly this class — checked the interpreter *name* and never that the script
# path resolved to anything. A hook that cannot be launched is not covered by
# the fail-open design in ADR-0077 §5: that governs a `DerivationError` inside a
# *running* script, and nothing runs here.
#
# So the vehicle changed. The registered command is now an inline bootstrap that
# always starts and does its own resolution: the cwd's repository first, then
# `CLAUDE_PROJECT_DIR`, and silence when neither has the script. The tests below
# run the registered string verbatim through a real shell, which is also what
# makes the shell-portability question answerable on each CI leg rather than
# assumed.


def _registered_command() -> str:
    """The Bash matcher's command string, exactly as Claude Code would run it."""
    for entry in _hook_entries():
        if entry.get("matcher") == "Bash":
            return str(entry["hooks"][0]["command"])
    raise AssertionError("no Bash matcher registered")


def _available_shells() -> list[tuple[str, str | None]]:
    """Every shell on this machine that can run the registered string.

    ``shell=True`` is **one** shell, not a portable one: ``cmd.exe`` on Windows
    and ``/bin/sh`` on POSIX. A suite that only used it would exercise cmd.exe
    on the Windows leg and a POSIX shell on the other two, so a bootstrap edit
    valid in one and not the other would pass on whichever leg happened to
    agree with it — while this module claimed to have answered the portability
    question on all three. Measured: ``echo hello; echo $0`` through
    ``shell=True`` on Windows returns the literal string, unsplit and unexpanded.

    So the default shell is exercised *and* a POSIX shell wherever one exists,
    which on Windows is the Git Bash that ships with this project's tooling.
    ``None`` means "the platform default via ``shell=True``"; a path means run
    that shell explicitly with ``-c``, because ``shell=True`` with ``executable``
    still passes Windows' ``/c`` convention and would hand ``bash`` a flag it
    does not take.
    """
    shells: list[tuple[str, str | None]] = [("platform-default", None)]
    bash = shutil.which("bash")
    if bash is not None:
        shells.append(("bash", bash))
    # The hook registers a PowerShell matcher, and until this leg existed
    # nothing ever dispatched the registered string through a PowerShell host:
    # `test_every_matcher_runs_the_same_bootstrap` proves the two entries are
    # byte-identical, which transfers the Bash leg's verdict only if the string
    # behaves the same under both — the very axis (quoting, escaping) this
    # module says the two shells disagree on.
    powershell = shutil.which("pwsh") or shutil.which("powershell")
    if powershell is not None:
        shells.append(("powershell", powershell))
    return shells


# Resolved once: the params and the ids must describe the same list, and two
# calls to a discovery function is how those quietly stop agreeing.
_SHELLS = _available_shells()


@pytest.fixture(
    params=[path for _name, path in _SHELLS],
    ids=[name for name, _path in _SHELLS],
)
def shell(request: pytest.FixtureRequest) -> str | None:
    return cast("str | None", request.param)


def _run_registered(
    cwd: Path,
    project_dir: Path | None,
    command: str = "uvx ruff check .",
    shell_path: str | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run the registered hook command through a shell, as the harness does.

    The leading interpreter word is swapped for ``sys.executable``. Asserting
    that ``python3`` resolves on the host is deliberately *not* part of this —
    ``test_the_registered_interpreter_is_a_bare_portable_name`` explains why
    that would go red on a runner image rather than on this change. What is
    under test is the bootstrap's resolution and its shell quoting, and both
    are exercised faithfully with any interpreter.
    """
    registered = _registered_command()
    interpreter, _, remainder = registered.partition(" ")
    assert interpreter == "python3", registered
    if shell_path is not None and Path(shell_path).name.lower().startswith(
        ("pwsh", "powershell")
    ):
        # PowerShell treats a quoted string in command position as a *string
        # literal*, not a program to run, so the call operator is required.
        # The registered command itself needs none of this — its interpreter
        # word is bare — this is an artefact of substituting an absolute path.
        shell_command = f'& "{sys.executable}" {remainder}'
    else:
        shell_command = f'"{sys.executable}" {remainder}'

    env = dict(os.environ)
    env.pop("CLAUDE_PROJECT_DIR", None)
    if project_dir is not None:
        env["CLAUDE_PROJECT_DIR"] = str(project_dir)

    payload = json.dumps(
        {
            "hook_event_name": "PreToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": command},
        }
    )
    if shell_path is not None:
        flag = (
            "-Command"
            if Path(shell_path).name.lower().startswith(("pwsh", "powershell"))
            else "-c"
        )
        return subprocess.run(  # noqa: S603
            [shell_path, flag, shell_command],
            cwd=cwd,
            env=env,
            input=payload,
            capture_output=True,
            text=True,
            check=False,
        )
    # S602: running the string through a shell is the property under test, not
    # an oversight. Claude Code hands a hook's `command` to a shell, so passing
    # an argument list here would exercise a launch path the harness never
    # takes — and the quoting of the inline bootstrap is exactly what could
    # differ between one shell and another.
    return subprocess.run(  # noqa: S602
        shell_command,
        shell=True,
        cwd=cwd,
        env=env,
        input=payload,
        capture_output=True,
        text=True,
        check=False,
    )


def test_the_registered_command_resolves_the_script_from_the_cwd_repository(
    shell: str | None,
) -> None:
    """The worktree case, which is the one that failed.

    ``CLAUDE_PROJECT_DIR`` is deliberately absent: a session whose cwd is a
    worktree must be policed by *that* worktree's script and *that* worktree's
    `ci.yml`, not by the primary checkout's.
    """
    proc = _run_registered(REPO_ROOT, project_dir=None, shell_path=shell)
    assert proc.returncode == 0, proc.stderr
    assert "permissionDecision" in proc.stdout, (
        f"the hook resolved nothing from cwd={REPO_ROOT}; stderr={proc.stderr!r}"
    )


def test_the_registered_command_falls_back_to_the_project_dir(
    tmp_path: Path, shell: str | None
) -> None:
    proc = _run_registered(tmp_path, project_dir=REPO_ROOT, shell_path=shell)
    assert proc.returncode == 0, proc.stderr
    assert "permissionDecision" in proc.stdout, proc.stderr


def test_an_unresolvable_hook_script_allows_rather_than_blocks(
    tmp_path: Path, shell: str | None
) -> None:
    """The launch-failure class, pinned.

    Nothing resolves from either source. The requirement is not merely that the
    hook declines to deny — it is that the process **exits 0 with empty stdout
    and empty stderr**, because anything else renders as a hook error, and a
    hook error on every shell call is what cost a session its shell.
    """
    proc = _run_registered(tmp_path, project_dir=tmp_path, shell_path=shell)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "", proc.stdout
    assert proc.stderr.strip() == "", (
        "a hook that cannot find its script must be silent, not noisy: stderr "
        f"renders as a hook error on every shell call — {proc.stderr!r}"
    )


def test_the_cwd_repository_is_preferred_over_the_project_dir(
    tmp_path: Path, shell: str | None
) -> None:
    """Precedence, observed rather than assumed.

    Both roots hold a script, so which one runs is visible in the output. The
    stand-in is a whole script rather than a copy of the real one, so the
    assertion cannot pass by accident: only the cwd's copy can produce this
    marker, and only the primary's can produce a real decision.
    """
    cwd_repo = tmp_path / "worktree"
    (cwd_repo / "scripts").mkdir(parents=True)
    (cwd_repo / ".git").write_text("gitdir: elsewhere\n", encoding="utf-8")
    (cwd_repo / "scripts" / HOOK_SCRIPT.name).write_text(
        "import sys\nsys.stdout.write('CWD-REPO-RAN')\n", encoding="utf-8"
    )

    proc = _run_registered(cwd_repo, project_dir=REPO_ROOT, shell_path=shell)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "CWD-REPO-RAN", (
        "the bootstrap ran the CLAUDE_PROJECT_DIR copy while the session's own "
        f"repository had one — that is the worktree defect — got {proc.stdout!r}"
    )


def test_the_search_stops_at_the_repository_root(
    tmp_path: Path, shell: str | None
) -> None:
    """An ancestor above the repository is not a place to find code to run.

    The first version of this bootstrap walked ``Path.cwd().parents`` to the
    drive root and executed the first ``scripts/check_gate_invocation.py`` it
    found anywhere along the way. That is a wider trust surface than "resolve
    the script from the cwd's repository" — it is "run whatever sits at that
    relative path anywhere above me" — and the ADR described the narrow version
    while the broad one shipped.

    Here the plant sits *above* the repository root and must never run: the
    walk stops at the first ancestor holding a ``.git`` entry. ``.git`` is a
    plain file in a worktree and a directory in a primary checkout, which is
    why the marker is tested with ``exists()`` rather than ``is_dir()``.
    """
    plant = tmp_path / "scripts"
    plant.mkdir(parents=True)
    (plant / HOOK_SCRIPT.name).write_text(
        "import sys\nsys.stdout.write('PLANTED-ANCESTOR-RAN')\n", encoding="utf-8"
    )

    repo = tmp_path / "nested" / "repo"
    repo.mkdir(parents=True)
    (repo / ".git").write_text("gitdir: elsewhere\n", encoding="utf-8")

    proc = _run_registered(repo, project_dir=None, shell_path=shell)
    assert proc.returncode == 0, proc.stderr
    assert "PLANTED-ANCESTOR-RAN" not in proc.stdout, (
        "the bootstrap executed a script from outside the session's repository"
    )
    assert proc.stdout.strip() == "", proc.stdout
    assert proc.stderr.strip() == "", proc.stderr


def test_a_broken_run_gates_allows_rather_than_crashing(tmp_path: Path) -> None:
    """The resolvable-but-broken case, which fail-open did not cover.

    ``import run_gates`` sat at module scope, outside every guard in the file,
    so a syntax error in ``run_gates.py`` killed the process before ``main``
    existed — exit 1 with a traceback, on *every* shell call. The trigger is
    ordinary and the timing is cruel: you save a broken ``run_gates.py``, and
    the next Bash call you would use to find the error is the one that fails.

    ``test_an_unresolvable_hook_script_allows_rather_than_blocks`` pins the
    script being *absent*. This pins it being present and its dependency being
    broken, which is a different path and was the reachable one.
    """
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    (repo / ".git").write_text("gitdir: elsewhere\n", encoding="utf-8")
    (repo / "scripts" / HOOK_SCRIPT.name).write_text(
        HOOK_SCRIPT.read_text(encoding="utf-8"), encoding="utf-8"
    )
    (repo / "scripts" / "run_gates.py").write_text("def broken(:\n", encoding="utf-8")

    proc = _run_registered(repo, project_dir=None)
    assert proc.returncode == 0, proc.stderr
    assert "Traceback" not in proc.stderr, proc.stderr
    # It must also say so rather than going quiet — a hook that stopped
    # policing and reported nothing is the failure ADR-0077 §5 rejects.
    assert "unchecked" in proc.stdout, proc.stdout


def test_a_broken_pyproject_allows_rather_than_crashing(tmp_path: Path) -> None:
    """The runtime half of the deferral, mirroring the `run_gates` precedent.

    `test_a_broken_run_gates_allows_rather_than_crashing` already established
    the treatment for this failure class: plant a broken dependency, run the
    *registered bootstrap* as a subprocess, and require exit 0 with no
    traceback. The `tomllib` deferral is the same class — a version-sensitive
    import moved behind a guard — and shipped with only a static AST check,
    which proves the import moved and not that the path it now takes fails
    open. A later refactor could reopen the defect with the AST test still
    green.

    Malformed TOML is the reachable trigger: it exercises the deferred import
    *and* the parse-error path in one command.
    """
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    (repo / ".git").write_text("gitdir: elsewhere\n", encoding="utf-8")
    (repo / "scripts" / HOOK_SCRIPT.name).write_text(
        HOOK_SCRIPT.read_text(encoding="utf-8"), encoding="utf-8"
    )
    (repo / "scripts" / "run_gates.py").write_text(
        (HOOK_SCRIPT.parent / "run_gates.py").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    # `derive_facts` reads `ci.yml` before it reaches the TOML, so the fixture
    # needs a real one or the run fails earlier and never exercises the parse.
    # The weaker "unchecked" assertion hid that; the specific one surfaced it.
    workflow = repo / ".github" / "workflows"
    workflow.mkdir(parents=True)
    (workflow / "ci.yml").write_text(
        (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    (repo / "pyproject.toml").write_text("this is not = = toml\n", encoding="utf-8")

    proc = _run_registered(repo, project_dir=None)
    assert proc.returncode == 0, proc.stderr
    assert "Traceback" not in proc.stderr, proc.stderr
    # The *specific* notice, not the trailing sentence both notices share.
    # Asserting "unchecked" alone passed with the parse guard deleted, because
    # the crash path says it too — a test that could not tell a handled error
    # from an unhandled one.
    assert "could not parse" in proc.stdout, proc.stdout
    assert "policing nothing" not in proc.stdout, (
        f"the parse error reached the crash guard instead of being named where "
        f"the parsing happens: {proc.stdout}"
    )


def test_an_unrelated_value_error_is_not_blamed_on_the_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bug in this hook must not be reported as a bad `pyproject.toml`.

    The read clause was briefly widened to `ValueError` so it could catch a
    `TOMLDecodeError` whose module is no longer named at import. That made
    *any* unrelated `ValueError` read as "could not read pyproject.toml",
    blaming the input for a defect in the hook. The parse failure is named
    where the parsing happens instead; this pins the other half, which nothing
    else exercises.
    """

    def exploding(_text: str) -> set[str]:
        raise ValueError("a bug in this hook, not a problem with the file")

    monkeypatch.setattr(hook, "dev_group_packages", exploding)

    result = hook.evaluate(_payload("uv run ruff check ."))
    assert result is not None
    message = str(result["systemMessage"])
    assert "policing nothing" in message, message
    assert "could not read" not in message, (
        f"a hook bug was attributed to the input file: {message}"
    )


def test_no_module_level_import_can_fail_on_a_supported_interpreter() -> None:
    """The launch-failure class, reached through the interpreter rather than the path.

    The bootstrap runs under a bare `python3` — whatever that resolves to on
    PATH, which this repository does not control. Anything imported at *module*
    scope executes before `main` exists, so it is outside every guard in the
    file: measured, an unavailable module-level import exits 1 with a traceback
    on **every** matched call, which is the failure ADR-0077 §5 says must not
    be reachable.

    `tomllib` was the one such import — 3.11+ — and it is now deferred into the
    function that parses TOML, inside the derivation guard. This test keeps it
    that way by asserting the module-level import list rather than by naming
    `tomllib`, so a future 3.12-only import is caught too.

    **What this test does NOT enforce, and an earlier version of this docstring
    wrongly implied it did.** It inspects *imports*. The module's real floor is
    set as much by language features, which an import list cannot see: `dict[str,
    str]` as a `default_factory` (PEP 585) and `str.removesuffix` are both 3.9+
    and both run at import. So the floor is **3.9**, this test holds only the
    import half of it, and nothing here mechanically checks the other half.
    Closing that needs compiling against a target version — larger work, and
    moot if the hook stops running under an uncontrolled interpreter at all
    (`specs/open-questions.md`).

    Deliberately not asserting a clean import on this host: that would test the
    runner image rather than the change, the same trap
    `test_the_registered_interpreter_is_a_bare_portable_name` documents.
    """
    tree = ast.parse(HOOK_SCRIPT.read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)

    # Every name here predates the module's 3.9 floor (see the docstring:
    # the floor comes from language features, not from these imports).
    # A name arriving in this set needs checking against the oldest `python3`
    # a contributor might have on PATH, not against this machine's.
    allowed = {
        "__future__",
        "json",
        "re",
        "shlex",
        "sys",
        "collections.abc",
        "contextlib",
        "dataclasses",
        "pathlib",
        "types",
        "typing",
    }
    assert imported <= allowed, (
        f"{sorted(imported - allowed)} is imported at module scope, where it "
        "runs before any guard exists. If it is unavailable on the interpreter "
        "`python3` happens to name, the hook exits 1 with a traceback on every "
        "matched call. Defer it into the function that uses it, behind the "
        "derivation guard, as `tomllib` is."
    )


def test_every_matcher_runs_the_same_bootstrap() -> None:
    """Only the Bash copy is executed by the tests, so the copies must match.

    The two matchers carry byte-identical ~300-character bootstraps, and every
    test that actually *runs* one reads the Bash entry. A typo in the
    PowerShell copy — the same class of edit that already cost a session —
    would leave that matcher silently non-functional while
    ``test_the_hook_is_registered_for_every_shell_tool`` still reported the
    coverage as present. Equality is what transfers the executed tests' verdict
    to the copy they do not execute.
    """
    commands = {
        str(handler.get("command", ""))
        for entry in _hook_entries()
        for handler in entry.get("hooks", [])
    }
    assert len(commands) == 1, (
        "the registered bootstraps differ between matchers, so the tests that "
        f"execute the Bash one say nothing about the other: {sorted(commands)}"
    )


def test_the_bootstrap_names_no_machine_specific_path() -> None:
    """ADR-0071's precondition survives the change of vehicle.

    The inline bootstrap is longer than the path it replaced, which is exactly
    the shape an absolute path can hide in.

    The home-directory check is **derived** from the running user rather than
    naming one. An earlier version asserted a literal username, which was wrong
    twice over: it caught exactly one machine, and it wrote an identifying
    string into a tracked file in a public repository to do it — the thing
    ADR-0071's precondition exists to keep out, committed by the test guarding
    the precondition.

    Only the full path is checked. A bare `home.name` substring test sat here
    too and was removed: it is username-dependent flakiness rather than
    coverage, since a home directory called `run`, `os`, `path` or `next` — or
    any single letter — matches the bootstrap's own `runpy`, `pathlib` and
    `os.environ`. The `str(home)` assertion above is what ADR-0071 actually
    asks for, and it is not sensitive to what the user is called.
    """
    home = Path.home()
    for entry in _hook_entries():
        for handler in entry.get("hooks", []):
            command = str(handler.get("command", ""))
            assert ":\\" not in command, command
            assert ":/" not in command, command
            assert "/home/" not in command, command
            assert "/Users/" not in command, command
            assert str(home) not in command, command


def _real_context() -> run_gates.Context:
    text = run_gates.CI_WORKFLOW.read_text(encoding="utf-8")
    return run_gates.Context(
        pins=run_gates.read_pins(text),
        gitleaks_version=run_gates.read_gitleaks_version(text),
        canary_dir_name=run_gates.read_workflow_env(text, "CANARY_CAPTURE_DIR"),
        dry=True,
    )
