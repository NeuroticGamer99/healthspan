"""Every git subprocess in the gate scripts is bounded and guarded.

Two facts about a family spread across several files, held by scanning rather
than by counting:

1. **Bounded.** Each `subprocess.run` whose argv begins with git carries a
   `timeout=`. Unbounded, a git that hangs -- a lock a concurrent client never
   releases, a credential helper waiting on a prompt -- hangs whatever invoked
   it, and every one of these runs inside a gate that a landing or a merge is
   waiting on.
2. **Guarded.** Each is wrapped in a `try` that turns both an absent git
   (`OSError`, which `FileNotFoundError` is) and the timeout itself
   (`subprocess.TimeoutExpired`) into the module's own error type. Neither is
   caught by the handlers these modules already had: `FileNotFoundError` is not
   a `RuntimeError` and `TimeoutExpired` is a `SubprocessError`, so both escaped
   to a traceback at exit 1 -- the code `check_spec_links` reserves for a real
   dead link, and the code the spec-links differential CLI reserves for "the
   revisions disagree". The exit-2 contract each module's docstring promises was
   prose the code did not keep. (That CLI is named by description rather than
   spelled out, deliberately: `test_diff_harness.py`'s suite sweep is textual,
   so a module merely *mentioning* it is read as one that loads a harness side
   and is required to opt into the shared import-state fixture. This module
   loads nothing.)

**Why this is a test and not a comment.** It was a comment, twice, and both were
wrong. `check_spec_links.md_sources` and `diff_harness._git` each carried a
hand-written enumeration of the sibling runners; the first named three as
covered and was wrong about the third, and the correction that replaced it at
both sites named `ledger`'s two as the only remaining gap -- missing
`run_gates._tracked_markdown`, a sixth runner that hangs `run_gates.py docs`
inside the gate *builder* with nothing printed. So the enumeration was corrected
once, at both sites, and stayed wrong at both. A reader who acted on the
corrected text would have closed `ledger` and believed the family closed. That
is the shape: a count of a family spread across several files is not checkable
by reading any one of them, and the fix for a claim nobody can check is to make
something check it. **And "six files" is what this sentence said until it was
re-read during `/land`** -- six is the number of *runners*; they live in five
files, because `ledger` holds two. The paragraph arguing that hand-written
counts rot was carrying one, wrong, in three files. The number is gone rather
than corrected: `_RUNNERS` below is the enumeration, and it is checked.

The exclusions, recorded rather than silent (and deliberately not counted, for
the reason the first of them gives):

- **`tests/`.** Several git runners there carry no timeout. A hang in a test
  helper stops the suite, which is loud -- pytest names the test it is inside --
  where a hang in a gate builder prints nothing at all. That is a real
  difference in the failure's visibility, so those sites are left as they are
  rather than swept in.

  **No count, and the deleted one is this module's own lesson landing on
  itself.** This read "Seven", which was wrong when written and then drifted
  further, because the scan below added a tenth git call *in this file* --
  `scanned_python_files`' own `git ls-files` -- so any number here is one the
  file invalidates by existing. The docstring above argues that a hand-written
  count of a family spread across several files is not checkable by reading any
  one of them, and then wrote one. Anyone who wants the number can run
  `git_runner_defects` over `tests/`, which is the point.
- **`subprocess.Popen`.** It takes no `timeout=`; a Popen child is bounded by
  `proc.wait(timeout=...)` instead, which this scan cannot see.
  `review_worktree._spawn` is the one git caller built that way, and
  `tests/test_review_worktree.py` holds its bound directly -- including the
  straggler case `subprocess.run(timeout=...)` mishandles, which is why it is
  spelled that way.
- **A second runner in a file already listed in `_RUNNERS`.** The completeness
  check below compares module *stems*, so it demands a behavioural row for a
  runner in a new file and not for a sibling of one already covered. Per-call-
  site attribution was built and then deliberately removed: deciding which
  function will *execute* a call means modelling when each part of a `def` runs
  -- decorators and defaults eagerly, annotations (PEP 649) and PEP 695 bounds
  lazily -- and keeping that correct across interpreter releases costs more than
  the residual risk is worth for six subprocess calls in five files. What
  remains uncovered needs three things at once: a second runner in a listed
  file, a guard whose body swallows rather than raises (the shape check still
  demands the `try` and both handler types), and nobody noticing. Revisit if a
  second runner ever lands in a listed file, which is the trigger.
"""

from __future__ import annotations

import ast
import subprocess
from collections.abc import Callable
from pathlib import Path
from types import ModuleType

import check_personal_containment
import check_spec_links
import diff_harness
import ledger
import pytest
import run_gates

REPO_ROOT = Path(__file__).resolve().parent.parent

# The directories the invariant binds. See the module docstring for why `tests/`
# is not among them.
SCANNED_DIRS = ("scripts", "src")

# Names that resolve to the git executable at a call site. A bare `"git"` is the
# PATH-resolved spelling; `_GIT` is the `shutil.which("git") or "git"` constant
# four of these modules share.
GIT_PROGRAM_NAMES = frozenset({"git"})
GIT_PROGRAM_IDENTIFIERS = frozenset({"_GIT"})

# Handler types that catch an absent or non-executable git. `FileNotFoundError`
# is the one that actually arrives; the broader names are accepted because they
# genuinely do catch it.
ABSENCE_HANDLERS = frozenset(
    {"OSError", "IOError", "EnvironmentError", "FileNotFoundError", "Exception"}
)
# Handler types that catch the timeout. `TimeoutExpired` is a `SubprocessError`
# and not an `OSError`, which is the whole reason this half is checked
# separately from the one above.
TIMEOUT_HANDLERS = frozenset({"TimeoutExpired", "SubprocessError", "Exception"})


def _handler_names(handler: ast.ExceptHandler) -> set[str]:
    """The bare type names one `except` clause catches.

    `subprocess.TimeoutExpired` and a bare `TimeoutExpired` are the same catch,
    so the attribute's last component is what is compared -- matching on the
    dotted spelling would report a conforming site as unguarded purely for
    importing the name differently.
    """
    if handler.type is None:
        return {"Exception"}  # a bare `except:` catches everything relevant
    nodes = handler.type.elts if isinstance(handler.type, ast.Tuple) else [handler.type]
    names: set[str] = set()
    for node in nodes:
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
    return names


def _is_git_call(call: ast.Call) -> bool:
    """Whether `call` is a `subprocess.run` whose argv begins with git.

    The argv's *first element* rather than any element: `git` appearing later is
    an argument -- a pathspec, a subcommand name, a branch called `git` -- and
    matching on membership would sweep those in.
    """
    func = call.func
    if not isinstance(func, ast.Attribute) or func.attr != "run":
        return False
    if not isinstance(func.value, ast.Name) or func.value.id != "subprocess":
        return False
    if not call.args:
        return False
    argv = call.args[0]
    if not isinstance(argv, (ast.List, ast.Tuple)) or not argv.elts:
        return False
    first = argv.elts[0]
    if isinstance(first, ast.Constant) and isinstance(first.value, str):
        return first.value in GIT_PROGRAM_NAMES
    if isinstance(first, ast.Name):
        return first.id in GIT_PROGRAM_IDENTIFIERS
    return False


def git_runner_defects(source: str, filename: str = "<test>") -> list[str]:
    """One entry per git `subprocess.run` that is unbounded or unguarded.

    The `try` is searched for by walking *down* from each `Try` node rather than
    up from the call, so a call guarded by an enclosing function's handler -- a
    guard that is real but sits a stack frame away -- is not credited here. That
    is deliberate: this asks whether the runner converts its own failures, which
    is what makes the converted error reach the module's `main` at all.
    """
    tree = ast.parse(source, filename=filename)

    guarded: dict[int, set[str]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Try):
            continue
        caught: set[str] = set()
        for handler in node.handlers:
            caught |= _handler_names(handler)
        for stmt in node.body:
            for inner in ast.walk(stmt):
                if isinstance(inner, ast.Call):
                    guarded.setdefault(id(inner), set()).update(caught)

    defects: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not _is_git_call(node):
            continue
        caught = guarded.get(id(node), set())
        if not any(k.arg == "timeout" for k in node.keywords):
            defects.append(f"{filename}:{node.lineno}: git run with no `timeout=`")
        if not (caught & ABSENCE_HANDLERS):
            defects.append(
                f"{filename}:{node.lineno}: git run not guarded against an "
                "absent git (OSError)"
            )
        if not (caught & TIMEOUT_HANDLERS):
            defects.append(
                f"{filename}:{node.lineno}: git run not guarded against "
                "subprocess.TimeoutExpired"
            )
    return defects


def scanned_python_files() -> list[Path]:
    """Every Python file git considers part of `SCANNED_DIRS`, plus new ones.

    `--cached` is the tracked set; `--others --exclude-standard` adds files that
    are new but not ignored, which is exactly when a fresh unguarded runner is
    most likely to exist. Deferring to git's ignore rules is also what keeps
    `.venv` and build output out without a prune list here.

    A near-twin of `tests/test_except_convention.py`'s `repo_python_files`, and
    kept separate on purpose: that one enumerates the whole repo, this one two
    directories, and collapsing them would mean passing the scope in and giving
    each caller a way to widen the other's rule by accident. Two callers is the
    boundary at which a shared mechanism starts paying, not past it.
    """
    # S603 fires here and not on the near-twin in `test_except_convention.py`
    # for one reason: the pathspecs below are built from `SCANNED_DIRS` rather
    # than written as literals, so ruff can no longer see that the whole argv is
    # constant. It is -- `SCANNED_DIRS` is a module constant in this file and
    # nothing writes to it -- and building it is what keeps the pathspec and the
    # directory list from drifting apart.
    proc = subprocess.run(  # noqa: S603 - argv is constant; see above
        [  # noqa: S607 - PATH-resolved git, as every other repo tool runs it
            "git",
            "ls-files",
            "--cached",
            "--others",
            "--exclude-standard",
            "-z",
            "--",
            *(f"{directory}/*.py" for directory in SCANNED_DIRS),
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    if proc.returncode != 0:
        # Not `check=True`: git fails here for reasons that have nothing to do
        # with this rule -- dubious ownership on a container checkout is the
        # canonical one -- and a bare CalledProcessError says only "exit status
        # 128", with no sign that git rather than the repository is what broke.
        raise RuntimeError(
            f"git ls-files failed (exit {proc.returncode}): {proc.stderr.strip()}"
        )
    paths = (REPO_ROOT / name for name in proc.stdout.split("\0") if name)
    # `--cached` still names a file staged for deletion, which is not on disk.
    return sorted({path for path in paths if path.is_file()})


# --- the detector, against fixtures ----------------------------------------
#
# A scan that silently matches nothing passes forever, and this one has two ways
# to match nothing: the git-call recognizer and the guard lookup. These fixtures
# are what prove the live test below reports zero because the scripts are clean
# rather than because the detector is blind.

_CONFORMING = """
import subprocess
try:
    subprocess.run([_GIT, "ls-files"], timeout=30, check=False)
except OSError as exc:
    raise Err() from exc
except subprocess.TimeoutExpired as exc:
    raise Err() from exc
"""


def test_the_conforming_shape_is_not_reported() -> None:
    assert git_runner_defects(_CONFORMING) == []


def test_a_single_tuple_clause_catching_both_is_not_reported() -> None:
    # The spelling `ledger.py` uses. A tuple clause is one handler catching two
    # types, so a scan that read only `handler.type` as a Name would report both
    # halves of the guard missing on a site that has them.
    both = "except (OSError, subprocess.TimeoutExpired) as exc:\n"
    source = _CONFORMING.replace(
        "except OSError as exc:\n    raise Err() from exc\n"
        "except subprocess.TimeoutExpired as exc:\n    raise Err() from exc\n",
        both + "    raise Err() from exc\n",
    )
    assert source != _CONFORMING, "fixture rewrite did not apply"
    assert git_runner_defects(source) == []


def test_a_missing_timeout_is_reported() -> None:
    source = _CONFORMING.replace(", timeout=30", "")
    assert source != _CONFORMING, "fixture rewrite did not apply"
    assert git_runner_defects(source) == [
        "<test>:4: git run with no `timeout=`",
    ]


def test_an_unguarded_absent_git_is_reported() -> None:
    source = _CONFORMING.replace("except OSError as exc", "except ValueError as exc")
    assert source != _CONFORMING, "fixture rewrite did not apply"
    assert git_runner_defects(source) == [
        "<test>:4: git run not guarded against an absent git (OSError)",
    ]


def test_an_unguarded_timeout_is_reported() -> None:
    source = _CONFORMING.replace(
        "except subprocess.TimeoutExpired as exc", "except ValueError as exc"
    )
    assert source != _CONFORMING, "fixture rewrite did not apply"
    assert git_runner_defects(source) == [
        "<test>:4: git run not guarded against subprocess.TimeoutExpired",
    ]


def test_a_wholly_unguarded_runner_reports_all_three() -> None:
    source = 'import subprocess\nsubprocess.run(["git", "status"])\n'
    assert len(git_runner_defects(source)) == 3


def test_a_non_git_runner_is_not_reported() -> None:
    # The rule is about git specifically. A `uv` or `gh` runner has its own
    # bound, and sweeping them in here would make this test the wrong owner.
    source = 'import subprocess\nsubprocess.run(["uv", "run", "pytest"])\n'
    assert git_runner_defects(source) == []


def test_git_as_a_later_argv_element_is_not_reported() -> None:
    # A pathspec, a subcommand argument or a branch named `git`. Matching on
    # membership rather than on the first element would sweep these in.
    source = 'import subprocess\nsubprocess.run(["uv", "run", "git"])\n'
    assert git_runner_defects(source) == []


def test_a_popen_git_child_is_out_of_scope() -> None:
    # Popen takes no `timeout=`; its bound is `wait(timeout=...)`, which this
    # scan cannot see. Reporting it would be a false positive against
    # `review_worktree._spawn`, whose bound its own suite holds.
    source = 'import subprocess\nsubprocess.Popen(["git", "status"])\n'
    assert git_runner_defects(source) == []


# --- the live invariant ----------------------------------------------------


def test_every_git_runner_in_the_scripts_is_bounded_and_guarded() -> None:
    """The enumeration two hand-written comments got wrong, run instead of read.

    Report finding 3: `run_gates._tracked_markdown` was the runner both
    corrected comments missed, and `_markdown_lint` calls it at gate-build time,
    so `run_gates.py docs` -- which `/land` runs -- hung inside the builder with
    no output at all.
    """
    defects: list[str] = []
    for path in scanned_python_files():
        rel = path.relative_to(REPO_ROOT).as_posix()
        defects.extend(
            git_runner_defects(path.read_text(encoding="utf-8"), filename=rel)
        )
    assert defects == [], "\n".join(defects)


def test_the_scan_actually_reaches_the_known_runners() -> None:
    """An empty corpus would satisfy the invariant above just as well.

    The count is deliberately a floor rather than an equality: a new gate that
    shells out to git must not have to edit this number, and the invariant above
    is what covers it when it does. What this pins is that the scan is reaching
    real files at all -- the failure mode where `scanned_python_files` returns
    nothing and every rule in this module passes over an empty set.
    """
    found = [
        path
        for path in scanned_python_files()
        if any(
            _is_git_call(node)
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
            if isinstance(node, ast.Call)
        )
    ]
    assert len(found) >= 5, [p.name for p in found]


@pytest.mark.parametrize("directory", SCANNED_DIRS)
def test_each_scanned_directory_contributes_files(directory: str) -> None:
    """Neither half of `SCANNED_DIRS` is silently matching nothing.

    The pathspec is built by f-string, so a directory rename turns the whole
    scan over that tree into a no-op that reports clean.
    """
    names = {path.relative_to(REPO_ROOT).parts[0] for path in scanned_python_files()}
    assert directory in names


# --- the invariant's other half: what the handlers actually DO ---------------
#
# Everything above is a *static* scan. It proves a `try` is present and that its
# handlers name the right types; it cannot see what the handler body does, so a
# guard that swallowed its exception and returned a plausible value would satisfy
# every assertion above. Measured by a reviewer, and it did: replacing each
# `raise` below with a bare return left the whole suite green at all three sites
# it tried. A shape check is not a behaviour check, and this family's whole point
# is a *behaviour* -- an absent or hung git becomes the module's own error type,
# reaching that module's `main` as a refusal rather than a traceback.


def _ledger_current_branch() -> object:
    return ledger.current_branch(REPO_ROOT)


def _ledger_git() -> object:
    return ledger._git(REPO_ROOT, "status")  # pyright: ignore[reportPrivateUsage]


def _run_gates_tracked_markdown() -> object:
    return run_gates._tracked_markdown()  # pyright: ignore[reportPrivateUsage]


def _diff_harness_git() -> object:
    return diff_harness._git("status")  # pyright: ignore[reportPrivateUsage]


def _check_spec_links_md_sources() -> object:
    return check_spec_links.md_sources()


def _check_personal_containment_git() -> object:
    return check_personal_containment._git(REPO_ROOT, "status")  # pyright: ignore[reportPrivateUsage]


# One row per git runner in `SCANNED_DIRS`, written out by hand rather than
# derived from the scan above: a table generated from the thing it checks cannot
# notice a runner leaving, and "is every runner here" is what
# `test_the_behavioural_table_covers_every_scanned_runner` asks separately.
_RUNNERS = [
    ("ledger.current_branch", ledger, _ledger_current_branch, ledger.LedgerError),
    ("ledger._git", ledger, _ledger_git, ledger.LedgerError),
    (
        "run_gates._tracked_markdown",
        run_gates,
        _run_gates_tracked_markdown,
        run_gates.GateError,
    ),
    ("diff_harness._git", diff_harness, _diff_harness_git, diff_harness.HarnessError),
    (
        "check_spec_links.md_sources",
        check_spec_links,
        _check_spec_links_md_sources,
        RuntimeError,
    ),
    (
        "check_personal_containment._git",
        check_personal_containment,
        _check_personal_containment_git,
        check_personal_containment.ContainmentError,
    ),
]

_FAILURES = [
    subprocess.TimeoutExpired(cmd=["git", "status"], timeout=30),
    FileNotFoundError(2, "No such file or directory", "git"),
]


@pytest.mark.parametrize(
    ("label", "module", "call", "expected"),
    _RUNNERS,
    ids=[row[0] for row in _RUNNERS],
)
@pytest.mark.parametrize("failure", _FAILURES, ids=["timeout", "git-absent"])
def test_each_runner_converts_a_git_failure_into_its_own_error(
    label: str,
    module: ModuleType,
    call: Callable[[], object],
    expected: type[Exception],
    failure: Exception,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every guard raises its module's error type, rather than swallowing.

    The two failures are the two the static scan requires a handler for, driven
    for real: `TimeoutExpired` is a `SubprocessError` and `FileNotFoundError` is
    an `OSError`, and neither is caught by the `RuntimeError`-shaped handlers
    these modules' `main` functions already had -- so unguarded, both escaped as
    a traceback at exit 1, the code two of them reserve for a real finding.

    The message must name git, because the whole value of converting is telling
    the author what did not run; a refusal that says only "TimeoutExpired" sends
    them into the gate's own logic looking for a defect that is not there.
    """

    def boom(*_args: object, **_kwargs: object) -> object:
        raise failure

    monkeypatch.setattr(module.subprocess, "run", boom)

    with pytest.raises(expected) as excinfo:
        call()

    assert "git" in str(excinfo.value).lower(), (label, str(excinfo.value))


def test_the_behavioural_table_covers_every_scanned_runner() -> None:
    """The hand-written table above cannot silently fall behind the scan.

    `_RUNNERS` is written by hand for the reason its comment gives, which leaves
    a failure mode: a git runner added to `scripts/` gets the static shape check
    for free and no behavioural row at all. This is the check that makes that a
    failure rather than an absence -- the same obligation
    `test_both_spellings_of_a_tool_are_refused_at_every_call_site` carries in
    the gate-invocation suite, for the same reason.

    **It compares module stems, so it covers a runner in a new file and not a
    second runner in a file already represented.** That limit is deliberate and
    is the third entry in the module docstring's exclusion list; this paragraph
    once read "exactly one failure mode" and closed with it, which a reviewer
    measured as false -- `ledger` contributes two rows under one stem, so the
    table itself was the counterexample. Per-call-site attribution was built and
    then removed: closing the gap needs a model of when each part of a `def`
    executes (decorators and defaults eagerly, annotations and PEP 695 bounds
    lazily), and that model costs more to keep correct across interpreter
    releases than the residual risk is worth for six subprocess calls. The risk
    that remains needs three things at once -- a second runner in an
    already-listed file, a guard whose body swallows rather than raises (the
    shape check still demands the `try` and both handler types), and nobody
    noticing.
    """
    scanned = {
        path.stem
        for path in scanned_python_files()
        if any(
            _is_git_call(node)
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
            if isinstance(node, ast.Call)
        )
    }
    covered = {label.split(".")[0] for label, _module, _call, _expected in _RUNNERS}

    assert scanned - covered == set(), sorted(scanned - covered)
