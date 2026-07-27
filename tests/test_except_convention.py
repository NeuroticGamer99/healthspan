"""The multi-exception `except` convention holds across the repo's Python.

CLAUDE.md bans the PEP 758 parenthesis-free form `except A, B:` in favour of
`except (A, B):`. The two are semantically identical, so nothing about the
program breaks -- what breaks is review: the bare form visually aliases the
removed Python-2 `except A, e:` (which bound the second name), and it has cost
this project review attention repeatedly, once escalating to a false
import-breaking blocker.

The rule needs a gate because it has an *active adversary*. `ruff format` at the
pinned version strips the parentheses back out of a one-line catch-only clause,
which is why the conforming sites carry `# fmt: skip`. The existing gates do not
cover the resulting hole: `ruff format --check` catches parens written *without*
the guard (the file reads as unformatted), but the bare form itself is
formatter-stable and `ruff check`-clean. Deleting a `# fmt: skip` as apparent
noise and committing what the formatter then produces is green everywhere. This
is the only thing that sees it.

The check walks the AST rather than grepping. That is not a stylistic
preference: the three documents that *state* the rule quote the banned form as
prose, and this module quotes it in fixtures below. A text scan would flag the
rule's own definition; an AST walk sees only real handlers.
"""

from __future__ import annotations

import ast
import io
import subprocess
import tokenize
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

# Tokens that carry no bracket structure. Filtered out so "the last token" means
# the last one that could close a parenthesis.
_LAYOUT_TOKENS = frozenset(
    {
        tokenize.COMMENT,
        tokenize.NL,
        tokenize.NEWLINE,
        tokenize.INDENT,
        tokenize.DEDENT,
        tokenize.ENDMARKER,
    }
)


def wrapped_in_parentheses(segment: str) -> bool:
    """Whether one parenthesis pair encloses the whole of `segment`.

    A leading `(` is necessary but not sufficient. `except (A), (B):` opens and
    closes with a parenthesis, yet both belong to *elements* -- the tuple itself
    is unparenthesized, so the clause is the banned form. The question is
    therefore whether the parenthesis opened at the start is the one closed at
    the end.

    The scan runs over tokens rather than characters so that a `)` inside a
    comment or a string literal cannot fake either answer. That is reachable,
    not hypothetical: a formatter-wrapped clause spans several lines, and any
    comment on them falls inside the segment.
    """
    tokens = [
        token
        for token in tokenize.generate_tokens(io.StringIO(segment).readline)
        if token.type not in _LAYOUT_TOKENS
    ]
    if not tokens or tokens[0].string != "(":
        return False
    depth = 0
    for index, token in enumerate(tokens):
        if token.type != tokenize.OP or token.string not in {"(", ")"}:
            continue
        depth += 1 if token.string == "(" else -1
        if depth == 0:
            # The opening parenthesis just closed. It wraps the tuple only if
            # nothing follows it.
            return index == len(tokens) - 1
    return False


def bare_comma_handlers(source: str, filename: str = "<test>") -> list[int]:
    """Line numbers of parenthesis-free multi-exception `except` clauses.

    Both forms parse to an `ExceptHandler` whose `.type` is a `Tuple`, so the
    node type alone cannot tell them apart. What separates them is the tuple's
    reported extent: CPython folds the parentheses into it when they wrap the
    tuple, so `except (A, B):` reports `(A, B)` where `except A, B:` reports
    `A, B`. Deciding whether those parentheses really do wrap the tuple, rather
    than one of its elements, is `wrapped_in_parentheses`'s job. Covers
    `except*` for free -- PEP 758 allows the bare form there too, and it
    produces the same node.
    """
    tree = ast.parse(source, filename=filename)
    bare: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ExceptHandler):
            continue
        if not isinstance(node.type, ast.Tuple):
            continue  # a single exception type, parenthesized or not
        segment = ast.get_source_segment(source, node.type)
        # None means a node without position info, which ast.parse always
        # supplies. Flag it rather than let an unreadable node count as
        # conforming.
        if segment is None or not wrapped_in_parentheses(segment):
            bare.append(node.lineno)
    return bare


def repo_python_files() -> list[Path]:
    """Every Python file git considers part of the repo, plus new ones.

    `--cached` is the tracked set; `--others --exclude-standard` adds files that
    are new but not ignored. That second half matters: a module written and not
    yet `git add`ed is exactly when a fresh bare-comma clause is most likely to
    exist, and the tracked set alone would call it clean. Deferring to git's
    ignore rules is also what keeps `.venv`, `specs/personal/`, and build output
    out without this test maintaining a prune list of its own.
    """
    proc = subprocess.run(
        [  # noqa: S607 - PATH-resolved git, as every other repo tool runs it
            "git",
            "ls-files",
            "--cached",
            "--others",
            "--exclude-standard",
            "-z",
            "--",
            "*.py",
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
        # 128". Both tests that use this helper would then fail together with
        # no sign that git, rather than the repository, is what broke.
        raise RuntimeError(
            f"git ls-files failed (exit {proc.returncode}): {proc.stderr.strip()}"
        )
    paths = (REPO_ROOT / name for name in proc.stdout.split("\0") if name)
    # `--cached` still names a file staged for deletion, which is not on disk.
    return sorted({path for path in paths if path.is_file()})


# --- the detector, against fixtures ----------------------------------------
#
# A scan that silently matches nothing passes forever. These fixtures are what
# prove the live test below reports zero because the repo is clean, not because
# the detector is broken.


def test_the_bare_comma_form_is_reported() -> None:
    assert bare_comma_handlers("try:\n    pass\nexcept A, B:\n    pass\n") == [3]


def test_the_parenthesized_form_is_not_reported() -> None:
    assert bare_comma_handlers("try:\n    pass\nexcept (A, B):\n    pass\n") == []


def test_the_formatter_wrapped_form_is_not_reported() -> None:
    # What ruff format produces for a clause too long to fit on one line. It
    # needs no `# fmt: skip`, so it must not be flagged for lacking one.
    source = "try:\n    pass\nexcept (\n    A,\n    B,\n):\n    pass\n"
    assert bare_comma_handlers(source) == []


def test_a_single_exception_type_is_not_reported() -> None:
    # No tuple, no comma, nothing to parenthesize.
    assert bare_comma_handlers("try:\n    pass\nexcept A:\n    pass\n") == []


def test_parentheses_around_only_the_first_type_are_reported() -> None:
    # `except (A), B:` is the banned form -- the parentheses group one element,
    # not the tuple -- yet its source segment still opens with `(`. Both ruff
    # format and ruff check leave it alone, so nothing else in the repo sees it.
    source = "try:\n    pass\nexcept (A), B:\n    pass\n"
    assert bare_comma_handlers(source) == [3]


def test_parentheses_around_each_type_separately_are_reported() -> None:
    # The harder version: the segment opens *and* closes with a parenthesis, so
    # neither a first-character test nor a first-and-last-character test can
    # tell it from the conforming form. Only matching the opening parenthesis to
    # its own closer does.
    source = "try:\n    pass\nexcept (A), (B):\n    pass\n"
    assert bare_comma_handlers(source) == [3]


def test_a_parenthesized_pair_beside_a_third_type_is_reported() -> None:
    # `except (A, B), C:` is a two-element tuple whose first element is itself a
    # tuple. Bare at the top level, so banned.
    source = "try:\n    pass\nexcept (A, B), C:\n    pass\n"
    assert bare_comma_handlers(source) == [3]


def test_a_nested_parenthesized_type_is_not_reported() -> None:
    # Mirror of the two above: here the outer parentheses really do wrap the
    # tuple, and a redundant inner pair must not change that.
    source = "try:\n    pass\nexcept ((A), B):\n    pass\n"
    assert bare_comma_handlers(source) == []


def test_a_closing_parenthesis_in_a_comment_does_not_fake_the_scan() -> None:
    # A wrapped clause spans several lines, so a comment on one of them falls
    # inside the segment being scanned. Read as characters, that `)` closes the
    # group early and this conforming clause gets flagged.
    source = (
        "try:\n    pass\n"
        "except (\n    A,  # raised when ) is unbalanced\n    B,\n):\n    pass\n"
    )
    assert bare_comma_handlers(source) == []


def test_an_as_binding_is_not_reported() -> None:
    # `except (A, B) as exc:` keeps its parens through the formatter, so these
    # are conforming and guard-free.
    source = "try:\n    pass\nexcept (A, B) as exc:\n    pass\n"
    assert bare_comma_handlers(source) == []


def test_the_bare_comma_form_is_reported_under_except_star() -> None:
    assert bare_comma_handlers("try:\n    pass\nexcept* A, B:\n    pass\n") == [3]


def test_the_form_inside_a_string_or_comment_is_not_reported() -> None:
    # The property that lets the rule's own documentation -- and the fixtures in
    # this module -- quote the banned form without tripping the gate.
    source = 'x = "except A, B:"\n# except A, B:\n'
    assert bare_comma_handlers(source) == []


def test_a_handler_nested_in_a_function_is_reported() -> None:
    # ast.walk descends; a violation does not escape by living inside a def.
    source = "def f():\n    try:\n        pass\n    except A, B:\n        pass\n"
    assert bare_comma_handlers(source) == [4]


def test_every_handler_in_a_file_is_reported_not_just_the_first() -> None:
    source = (
        "try:\n    pass\nexcept A, B:\n    pass\n"
        "try:\n    pass\nexcept (C, D):\n    pass\n"
        "try:\n    pass\nexcept E, F:\n    pass\n"
    )
    assert bare_comma_handlers(source) == [3, 11]


def test_a_syntax_error_names_the_file_it_came_from() -> None:
    # The scan reads unstaged work in progress, which can be mid-edit. When it
    # cannot parse, the failure has to say which file rather than point at ast.
    with pytest.raises(SyntaxError) as caught:
        bare_comma_handlers("def f(\n", filename="scripts/half_written.py")
    assert caught.value.filename == "scripts/half_written.py"


# --- the file set ----------------------------------------------------------


def test_the_scan_reaches_every_directory_holding_python() -> None:
    # Without this, a `git ls-files` that returned nothing -- wrong cwd, a
    # renamed directory, a flag that stopped meaning what it means -- would make
    # the live test below pass by scanning an empty set.
    #
    # Directory prefixes, not named files: the proof wanted is that each tree
    # holding Python is still in reach, and naming individual files couples that
    # proof to their survival. `.github/scripts/` holds exactly one, owned by a
    # reviewer since demoted to best-effort; deleting it would have failed this
    # test with a message blaming the scan for an unrelated removal.
    found = [path.relative_to(REPO_ROOT).as_posix() for path in repo_python_files()]
    for prefix in ("src/healthspan/", "tests/", "scripts/", ".github/scripts/"):
        assert any(path.startswith(prefix) for path in found), (
            f"no Python file under {prefix} is being scanned -- either the scan "
            "stopped reaching it, or that directory no longer holds Python and "
            "this list should drop it"
        )


def test_a_git_failure_carries_gits_own_explanation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Sibling of the syntax-error test below: when the scan cannot run at all,
    # the failure has to name git as the cause. Left to CalledProcessError it
    # reads as a bare "exit status 128" from a test about `except` clauses, and
    # both tests using the helper fail at once with nothing pointing at git.
    def fake_run(*_args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        # `check=False` is the deliberate half of the arrangement: the helper
        # handles a non-zero exit itself so it can attach git's stderr. Pinning
        # it keeps a revert to `check=True` from hiding behind this fake, which
        # ignores the flag and hands back the failure either way.
        assert kwargs["check"] is False
        return subprocess.CompletedProcess(
            args=["git", "ls-files"],
            returncode=128,
            stdout="",
            stderr="fatal: detected dubious ownership in repository at '/repo'\n",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(RuntimeError) as caught:
        repo_python_files()
    message = str(caught.value)
    assert "128" in message
    assert "dubious ownership" in message


# --- the live contract -----------------------------------------------------


def test_no_python_file_uses_the_bare_comma_except_form() -> None:
    offenders: list[str] = []
    for path in repo_python_files():
        relative = path.relative_to(REPO_ROOT).as_posix()
        # utf-8-sig, not utf-8: a BOM is exactly what CLAUDE.md's PowerShell
        # encoding footgun produces, and ast.parse rejects one as an invalid
        # non-printable character. That failure is loud but names the wrong
        # problem -- a syntax error, in a test about `except` clauses.
        source = path.read_text(encoding="utf-8-sig")
        offenders += [
            f"{relative}:{line}" for line in bare_comma_handlers(source, relative)
        ]
    assert not offenders, (
        "parenthesis-free multi-exception `except` at "
        + ", ".join(offenders)
        + " -- write `except (A, B):` instead. If ruff format strips the "
        "parentheses back out, the clause is catch-only and fits on one line: "
        "add `# fmt: skip` to hold them, as src/healthspan/pool.py does. A "
        "longer clause needs no guard (the formatter wraps it), and `# fmt: "
        "skip` on one would fail ruff check E501. See CLAUDE.md."
    )
