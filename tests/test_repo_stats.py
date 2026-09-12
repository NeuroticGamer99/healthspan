"""Unit tests for the repo-size reporter (scripts/repo_stats.py).

Three layers, narrowest first:

  - the pure counters (python_docstring_lines, classify) over constructed bytes;
  - categorising, build_report and every renderer over `DictSource`, an
    in-memory TreeSource, so a count never depends on the live repo's size and
    these stay stable as it grows;
  - the git-backed sources (WorkTree, GitRev, BlobReader, the series) against a
    throwaway repository built per test, because enumeration, the personal-path
    exclusion and blob framing are exactly what an in-memory source cannot model.

A few tests deliberately touch the real tree, and each states why in its own
docstring rather than being counted here -- this paragraph said "the one
deliberate exception" while there were four, which is the hand-count rot this
repository keeps re-finding. The load-bearing one is
`test_real_repository_has_no_uncounted_files`, which asserts a property *of*
this repository: that every tracked .py/.sql/.md file has a category. It is the
mechanization of the gap this module's coverage was widened to close. The rest
reach the live tree only because what they pin cannot be reached otherwise --
the `main()` shim's two legacy spellings, and a subprocess whose `REPO_ROOT` no
monkeypatch can redirect across a process boundary.
"""

import csv
import io
import json
import os
import re
import shutil
import subprocess
import sys
import threading
from pathlib import Path
from typing import cast

import check_personal_containment
import pytest
import repo_stats as rs

# --- python_docstring_lines ----------------------------------------------


def test_docstring_lines_marks_module_docstring() -> None:
    lines, ok = rs.python_docstring_lines('"""a\nb\nc"""\nx = 1\n')
    assert ok is True
    assert lines == {1, 2, 3}


def test_docstring_lines_ignores_assigned_multiline_string() -> None:
    # An assigned string is code, not a docstring -- the AST distinguishes it,
    # a naive triple-quote scanner would not. This is the reason for using ast.
    lines, ok = rs.python_docstring_lines('x = """a\nb"""\n')
    assert ok is True
    assert lines == set()


def test_docstring_lines_reports_parse_failure() -> None:
    lines, ok = rs.python_docstring_lines("def broken(:\n")
    assert ok is False
    assert lines == set()


# --- classify -------------------------------------------------------------


def test_classify_python_splits_docstring_comment_code_blank(tmp_path: Path) -> None:
    # A docstring line is comment; a trailing comment on a statement is code
    # (the line does work); a whole-line # is comment; empty is blank.
    src = tmp_path / "m.py"
    src.write_text(
        '"""doc\nline2\n"""\nx = 1  # trailing\n# pure\n\n', encoding="utf-8"
    )
    fc, warn = rs.classify(src.read_bytes(), "python", display="m.py")
    assert warn is None
    assert (fc.physical, fc.code, fc.comment, fc.blank) == (6, 1, 4, 1)


def test_classify_python_unparseable_warns_and_uses_hash_rule(tmp_path: Path) -> None:
    src = tmp_path / "broken.py"
    src.write_text("def f(:\n# still a comment\ncode\n", encoding="utf-8")
    fc, warn = rs.classify(src.read_bytes(), "python", display="broken.py")
    assert warn is not None
    # The message names the file it is about -- `display` is the only thing the
    # counter knows about provenance, and a warning that names the wrong file
    # sends a reader to the wrong place.
    assert warn == "broken.py: could not parse as Python; docstrings counted as code"
    # Falls back to the #-only rule: one comment, two code, no docstring credit.
    assert (fc.comment, fc.code) == (1, 2)


def test_classify_sql_uses_double_dash_comment(tmp_path: Path) -> None:
    src = tmp_path / "m.sql"
    src.write_text("-- header\nSELECT 1;\n\n", encoding="utf-8")
    fc, _ = rs.classify(src.read_bytes(), "sql")
    assert (fc.code, fc.comment, fc.blank) == (1, 1, 1)


def test_classify_markdown_has_no_comment_column(tmp_path: Path) -> None:
    # A '#' heading is content, not a comment -- markdown has no comment concept.
    src = tmp_path / "d.md"
    src.write_text("# Title\n\ntext\n", encoding="utf-8")
    fc, _ = rs.classify(src.read_bytes(), "markdown")
    assert (fc.code, fc.comment, fc.blank) == (2, 0, 1)


def test_classify_reports_bytes(tmp_path: Path) -> None:
    src = tmp_path / "d.md"
    src.write_bytes(b"abc\n")
    fc, _ = rs.classify(src.read_bytes(), "markdown")
    assert fc.nbytes == 4


# --- /apply-review regressions (findings 1 and 2) -------------------------


def test_classify_python_form_feed_keeps_docstring_aligned(tmp_path: Path) -> None:
    # Finding 1: a form feed (legal Python whitespace) is a line boundary for
    # str.splitlines but NOT for the AST. Splitting on it would desync the two
    # and count the post-FF docstring line as code. _physical_lines splits on
    # \n/\r/\r\n only, so the whole docstring stays comment.
    src = tmp_path / "m.py"
    src.write_bytes(b'"""line1\x0cline2\nreal2"""\nx = 1\n')
    fc, warn = rs.classify(src.read_bytes(), "python", display="m.py")
    assert warn is None
    # Two physical lines of docstring (comment), one statement (code), no blank.
    assert (fc.physical, fc.code, fc.comment, fc.blank) == (3, 1, 2, 0)


def test_classify_python_strips_utf8_bom(tmp_path: Path) -> None:
    # Finding 2: a BOM-prefixed but otherwise valid file must parse cleanly (no
    # spurious "could not parse" warning) and the first line must not carry a
    # glued U+FEFF that misclassifies a docstring/comment as code.
    src = tmp_path / "m.py"
    src.write_bytes(b"\xef\xbb\xbf" + b'"""doc"""\nx = 1\n')
    fc, warn = rs.classify(src.read_bytes(), "python", display="m.py")
    assert warn is None
    assert (fc.code, fc.comment) == (1, 1)


# (The _is_comment branches are exercised through classify above: docstring,
# whole-line and trailing # in the Python test, -- in the SQL test, and the
# markdown-heading-as-content case in the markdown test.)


# --- a TreeSource over constructed input ----------------------------------


class DictSource:
    """A TreeSource backed by an in-memory ``{path: bytes}`` map.

    Lets counting, categorising and rendering be tested as pure transforms over
    constructed input -- the in-memory shape `specs/testing-strategy.md` calls
    for -- with no git and no filesystem. An `OSError` instance as a value is
    raised on read, which is how the unreadable-file paths are reached without
    chmod games.

    The git-backed sources get their own tests against a real repository at the
    bottom of this file, because what can go wrong *there* -- enumeration,
    exclusions, blob framing -- is exactly what a dict cannot model. This class
    is deliberately not a stand-in for them.
    """

    def __init__(
        self,
        files: dict[str, object],
        *,
        blobs: dict[str, str] | None = None,
        label: str = "test",
        commit: str | None = None,
        date: str | None = None,
    ) -> None:
        self._files = files
        self._blobs = blobs or {}
        self.label = label
        self.commit = commit
        self.date = date
        self.reads: list[str] = []

    def files(self) -> list[rs.FileRef]:
        return [rs.FileRef(p, self._blobs.get(p)) for p in sorted(self._files)]

    def read(self, ref: rs.FileRef) -> bytes:
        self.reads.append(ref.path)
        value = self._files[ref.path]
        if isinstance(value, OSError):
            raise value
        assert isinstance(value, bytes)
        return value


# --- category membership --------------------------------------------------


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("src/pkg/mod.py", rs.LABEL_IMPL),
        ("src/pkg/migrations/0001.sql", rs.LABEL_MIGRATIONS),
        ("tests/test_x.py", rs.LABEL_TESTS),
        ("scripts/s.py", rs.LABEL_SCRIPTS),
        # The CI half of the harness, which the uncounted check found sitting
        # outside every category.
        (".github/scripts/gemini_review_agent.py", rs.LABEL_SCRIPTS),
        ("specs/top.md", rs.LABEL_SPECS),
        ("specs/reviews/r.md", rs.LABEL_REVIEWS),
        ("specs/reviews/angle-ledger/digests/0/a.md", rs.LABEL_REVIEWS),
        ("specs/adr/0001-a.md", rs.LABEL_ADR),
        ("specs/adr/README.md", rs.LABEL_ADR),
        (".claude/reviewer-isolation.md", rs.LABEL_HARNESS),
        (".claude/skills/land/SKILL.md", rs.LABEL_HARNESS),
        ("CLAUDE.md", rs.LABEL_TOOLING),
        ("README.md", rs.LABEL_TOOLING),
        (".github/ISSUE_TEMPLATE/bug.md", rs.LABEL_TOOLING),
        (".gemini/styleguide.md", rs.LABEL_TOOLING),
        (".greptile/notes.md", rs.LABEL_TOOLING),
    ],
)
def test_category_membership_by_path(path: str, expected: str) -> None:
    # Asserts *which* category claimed it, and that exactly one did -- a test
    # that only checked a count would pass with the file in the wrong row.
    buckets, uncounted = rs.categorize([rs.FileRef(path)])
    assert uncounted == []
    assert [label for label, refs in buckets.items() if refs] == [expected]


@pytest.mark.parametrize(
    "path",
    [
        "specs/sub/notes.md",  # one level below specs/ is not "general"
        "src/pkg/notes.md",  # markdown inside an implementation tree
        "docs/guide.md",  # a tree no category knows about
    ],
)
def test_categorize_reports_a_counted_file_no_category_claims(path: str) -> None:
    # The positive half of the leak check. Without it, the "no leaks" assertion
    # below passes just as happily when the check never runs at all -- an
    # assertion whose passing value is an empty list proves nothing on its own.
    buckets, uncounted = rs.categorize([rs.FileRef(path)])
    assert uncounted == [path]
    assert all(refs == [] for refs in buckets.values())


@pytest.mark.parametrize(
    "path", [".github/workflows/ci.yml", "pyproject.toml", ".claude/settings.json"]
)
def test_categorize_ignores_files_outside_the_counted_suffixes(path: str) -> None:
    # Config is out of scope by construction, not an oversight: it must not be
    # counted and must not be reported as a leak either. Both halves are
    # asserted -- discarding `buckets` would pin half the stated contract and
    # pass with the file quietly counted inside a category.
    buckets, uncounted = rs.categorize([rs.FileRef(path)])
    assert uncounted == []
    assert all(refs == [] for refs in buckets.values())


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("tests/TEST_UPPER.PY", rs.LABEL_TESTS),
        ("TESTS/helper.py", rs.LABEL_TESTS),
        ("src/pkg/0001.SQL", rs.LABEL_MIGRATIONS),
        ("specs/README.MD", rs.LABEL_SPECS),
        ("SPECS/ADR/0001-a.md", rs.LABEL_ADR),
        (".CLAUDE/skills/x/SKILL.MD", rs.LABEL_HARNESS),
        ("Readme.Md", rs.LABEL_TOOLING),
        (".GitHub/ISSUE_TEMPLATE/bug.md", rs.LABEL_TOOLING),
    ],
)
def test_membership_is_case_insensitive_on_every_platform(
    path: str, expected: str
) -> None:
    """One case policy, the same on all three CI legs.

    Git preserves the casing a path was added with while the Windows and macOS
    filesystems this project runs on do not, so a case-sensitive predicate makes
    the same file answer differently per leg -- against the constant lens the
    whole history rests on. The old `Path.rglob("*.py")` matched case-blind on
    Windows, so an uppercase-extension file that *was* counted silently stopped
    being when enumeration moved to git and matching moved to `str.endswith`.
    """
    buckets, uncounted = rs.categorize([rs.FileRef(path)])
    assert uncounted == []
    assert [label for label, refs in buckets.items() if refs] == [expected]


def test_a_case_variant_that_no_category_claims_is_still_reported_uncounted() -> None:
    """The compound half of the same defect, and the reason it ranks above a
    plain undercount.

    `categorize`'s leak check tested the suffix with the same case-sensitive
    comparison the category predicates used, so an uppercase-extension file fell
    out of the totals *and* out of the check that exists to announce it: full
    coverage reported, file missing. Pinned on a path no category claims, so the
    only thing keeping it out of the leak list would be the suffix comparison.
    """
    _, uncounted = rs.categorize([rs.FileRef("docs/GUIDE.MD")])
    assert uncounted == ["docs/GUIDE.MD"]


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("specs/personal/labs.md", True),
        ("specs/personal", True),  # a plain FILE at the bare path
        ("specs/personal/sub/deep.md", True),
        ("Specs/Personal/labs.md", True),  # POSIX legs, where the ignore misses it
        ("SPECS/PERSONAL", True),
        ("specs/personal-notes/x.md", False),  # sibling, must not over-match
        ("specs/personalise.md", False),
        ("specs/data-model.md", False),
        ("personal/labs.md", False),  # not under specs/
        ("", False),
    ],
)
def test_the_containment_predicate_agrees_with_the_gates_copy(
    path: str, expected: bool
) -> None:
    """ADR-0079 §3 names this module's exclusion as a containment guard, so the
    rule now exists in a third place and is pinned rather than trusted.

    It shipped as a case-sensitive, trailing-slash prefix test, which accepted a
    mixed-case spelling, an all-caps `specs` component and a plain file at the
    bare path -- and an accepted path is enumerated, so `render_markdown` printed
    it **verbatim** in the uncounted footnote. `check_personal_containment`
    already holds the correct rule and already pins it against
    `review_worktree._is_personal`; this extends that chain rather than starting
    a fourth independent spelling, and both expectations are asserted so the
    test fails on over-matching as loudly as on under-matching.
    """
    assert rs._is_personal_path(path) is expected  # pyright: ignore[reportPrivateUsage]
    assert (
        rs._is_personal_path(path)  # pyright: ignore[reportPrivateUsage]
        == check_personal_containment.is_personal_path(path)
    )


def _describe(match: object) -> str:
    """A category predicate as a string, **including the arguments bound into it**.

    The obvious fingerprint -- `match.__qualname__` -- records only which factory
    built the closure, never what it was built with, so it reads
    `_under.<locals>.match` for every `_under` category alike. Measured:
    flipping `recursive=False` to `True` on `specs/*.md` left a
    `__qualname__`-based pin green, while the docstring above claimed that flip
    was exactly what it caught. That is a pin asserting a property it does not
    hold, which is worse than no pin: it is a green light nobody re-checks.

    The bound values live in the closure cells, paired with `co_freevars` by
    position. `_any_of` holds other predicates, so this recurses into them.
    """
    if not callable(match):  # pragma: no cover - defensive
        return repr(match)
    code = getattr(match, "__code__", None)
    cells = getattr(match, "__closure__", None)
    factory = getattr(match, "__qualname__", "?").split(".", 1)[0]
    if code is None or cells is None:  # pragma: no cover - not a closure
        return factory
    args: list[str] = []
    freevars: tuple[str, ...] = code.co_freevars
    for name, cell in sorted(zip(freevars, cells, strict=True)):
        value: object = cell.cell_contents
        if isinstance(value, tuple):
            nested = cast("tuple[object, ...]", value)
            if nested and callable(nested[0]):
                args.append(", ".join(_describe(m) for m in nested))
                continue
            args.append(f"{name}={value!r}")
        else:
            args.append(f"{name}={value!r}")
    return f"{factory}({', '.join(args)})"


def test_the_categories_version_moves_when_the_categories_do() -> None:
    """Every other reference to the stamp compares it to itself.

    `render_history_csv` writes `CATEGORIES_VERSION` and the test asserts the
    column equals `rs.CATEGORIES_VERSION` -- true for any value, including a
    value left behind by a category change. So adding a tenth category or
    flipping a `recursive=` would keep the suite green while silently
    green-lighting the cross-version series merge `repo-history/SKILL.md`
    forbids, which is the one thing the stamp exists to prevent.

    A fingerprint of the category set is the only thing that can notice. It is
    deliberately not a *derivation* of the version -- that would make the stamp
    change without anyone deciding to, and the stamp is a decision. This fails
    loudly with the fingerprint to paste in, and the author bumps both.
    """
    fingerprint = [(cat.label, cat.lang, _describe(cat.match)) for cat in rs.CATEGORIES]
    expected = [
        (
            rs.LABEL_IMPL,
            "python",
            "_under(prefix='src/', recursive=True, suffix='.py')",
        ),
        (
            rs.LABEL_TESTS,
            "python",
            "_under(prefix='tests/', recursive=True, suffix='.py')",
        ),
        (
            rs.LABEL_SCRIPTS,
            "python",
            "_any_of(_under(prefix='scripts/', recursive=True, suffix='.py'), "
            "_under(prefix='.github/scripts/', recursive=True, suffix='.py'))",
        ),
        (
            rs.LABEL_MIGRATIONS,
            "sql",
            "_under(prefix='src/', recursive=True, suffix='.sql')",
        ),
        (
            rs.LABEL_SPECS,
            "markdown",
            "_under(prefix='specs/', recursive=False, suffix='.md')",
        ),
        (
            rs.LABEL_REVIEWS,
            "markdown",
            "_under(prefix='specs/reviews/', recursive=True, suffix='.md')",
        ),
        (
            rs.LABEL_ADR,
            "markdown",
            "_under(prefix='specs/adr/', recursive=False, suffix='.md')",
        ),
        (
            rs.LABEL_HARNESS,
            "markdown",
            "_under(prefix='.claude/', recursive=True, suffix='.md')",
        ),
        (
            rs.LABEL_TOOLING,
            "markdown",
            "_root_or_under(prefixes=('.github/', '.gemini/', '.greptile/'), "
            "suffix='.md')",
        ),
    ]
    assert fingerprint == expected, (
        "the category set changed: bump CATEGORIES_VERSION and update this list, "
        "or a series gathered under the old set stays silently comparable"
    )
    assert rs.CATEGORIES_VERSION == 2


@pytest.mark.parametrize(
    ("path", "included"),
    [
        ("src/pkg/mod.py", True),
        ("src/pkg/__pycache__/mod.cpython-314.pyc", False),
        ("src/pkg/__pycache__/mod.py", False),
        ("src/__PYCACHE__/mod.py", False),  # folded, like every other predicate
        # a path *segment*, not a substring
        ("src/pkg/not__pycache__here/mod.py", True),
    ],
)
def test_pycache_is_excluded_from_enumeration(path: str, included: bool) -> None:
    # The deleted `test_build_report_excludes_pycache_and_nested_specs` took the
    # __pycache__ arm's only coverage with it; the nested-specs half survived as
    # a category-membership row, this half did not.
    assert rs._included(path) is included  # pyright: ignore[reportPrivateUsage]


def test_real_repository_has_no_uncounted_files() -> None:
    """The negative half, against the live tree: every tracked .py/.sql/.md
    lands in some category.

    This is the mechanization the whole uncounted concept exists for. The gap it
    closes -- an entire `.claude/` tree outside every category -- survived four
    months precisely because nothing asserted this. If it fails, the fix is to
    give the named file a category (or to widen an existing one), not to delete
    the assertion.

    **The positive control is not decoration here.** `uncounted == []` passes
    identically when the enumeration returned nothing at all: measured, stubbing
    `WorkTree.files()` to `return []` left this green. That is the same
    fail-open shape the round this test shipped in was convened to fix, applied
    correctly in `test_personal_paths_are_never_enumerated_at_any_revision` and
    missed here -- in the test the module docstring calls load-bearing. So the
    enumeration is pinned before its complement is read: a floor on the count,
    and a file known to be in each of the three languages, so a predicate that
    silently stopped matching one language cannot hide behind the other two.
    """
    refs = rs.WorkTree().files()
    buckets, uncounted = rs.categorize(refs)
    assert len(refs) > 100, (
        "the live tree enumerated almost nothing; nothing below holds"
    )
    for label in (rs.LABEL_IMPL, rs.LABEL_TESTS, rs.LABEL_SCRIPTS, rs.LABEL_ADR):
        assert buckets[label], f"{label} came back empty against the live tree"
    # Named files, so a category that is non-empty for the wrong reason is still
    # caught. These three are load-bearing repo fixtures, not incidental.
    claimed = {ref.path for refs_ in buckets.values() for ref in refs_}
    assert {"scripts/repo_stats.py", "tests/test_repo_stats.py", "CLAUDE.md"} <= claimed
    assert uncounted == []


# --- build_report ---------------------------------------------------------


def test_build_report_counts_every_category() -> None:
    source = DictSource(
        {
            "src/pkg/mod.py": b"x = 1\n",
            "src/pkg/migrations/0001.sql": b"SELECT 1;\n",
            "tests/test_x.py": b"y = 2\n",
            "scripts/s.py": b"z = 3\n",
            "specs/top.md": b"prose\n",
            "specs/reviews/r.md": b"notes\n",
            "specs/adr/0001-a.md": b"## Status\n\nAccepted\n",
            ".claude/skills/land/SKILL.md": b"skill\n",
            "README.md": b"readme\n",
        }
    )
    report = rs.build_report(source)
    # Equality on the whole mapping, not a handful of lookups: a subset check
    # cannot see a category that quietly captured a file it should not have.
    assert {label: c.files for label, c in report.per_category.items()} == {
        rs.LABEL_IMPL: 1,
        rs.LABEL_TESTS: 1,
        rs.LABEL_SCRIPTS: 1,
        rs.LABEL_MIGRATIONS: 1,
        rs.LABEL_SPECS: 1,
        rs.LABEL_REVIEWS: 1,
        rs.LABEL_ADR: 1,
        rs.LABEL_HARNESS: 1,
        rs.LABEL_TOOLING: 1,
    }
    assert report.adr_status == {"Accepted": 1}
    assert report.warnings == []
    assert report.uncounted == []


def test_build_report_reuses_a_cached_blob_without_losing_the_second_file() -> None:
    """Two paths sharing one blob: read once, counted twice.

    The saving is the point of the cache and the double-count is the bug it
    invites, so both halves are asserted together.
    """
    source = DictSource(
        {"scripts/a.py": b"x = 1\n", "scripts/b.py": b"x = 1\n"},
        blobs={"scripts/a.py": "cafe1234", "scripts/b.py": "cafe1234"},
    )
    report = rs.build_report(source)
    assert source.reads == ["scripts/a.py"]  # b never re-read
    counts = report.per_category[rs.LABEL_SCRIPTS]
    assert (counts.files, counts.physical) == (2, 2)


def test_cached_unparseable_blob_warns_once_per_path() -> None:
    # The warning names a path, so it cannot be cached alongside the counts --
    # one blob at two paths owes two messages, each naming its own file.
    source = DictSource(
        {"scripts/a.py": b"def f(:\n", "scripts/b.py": b"def f(:\n"},
        blobs={"scripts/a.py": "beef5678", "scripts/b.py": "beef5678"},
    )
    report = rs.build_report(source)
    assert report.warnings == [
        "scripts/a.py: could not parse as Python; docstrings counted as code",
        "scripts/b.py: could not parse as Python; docstrings counted as code",
    ]


def test_a_shared_blob_is_classified_once_per_language() -> None:
    """A git blob is content-addressed and path-independent, so one sha can be
    reached as two languages that count it differently.

    Keyed on the sha alone, the first category reached won and the other
    silently inherited its split: `# Title` is a comment in Python and content
    in Markdown, so the markdown file reported code=1/comment=1 instead of
    code=2/comment=0 -- and picked up a **false** "could not parse as Python"
    warning naming a `.md` file. `CATEGORIES` iteration order decided which
    language won, and `build_series` shares one cache across the whole walk, so
    a single collision would poison every point of the history.

    The oracle is the independent classification, not a hand-written pair: it is
    what the cache is claiming to be equivalent to.
    """
    shared = b"# Title\n\nsome prose\n"
    source = DictSource(
        {"scripts/a.py": shared, "specs/b.md": shared},
        blobs={"scripts/a.py": "d00d1234", "specs/b.md": "d00d1234"},
    )
    report = rs.build_report(source)
    py_alone, _ = rs.classify(shared, "python")
    md_alone, _ = rs.classify(shared, "markdown")
    got_py = report.per_category[rs.LABEL_SCRIPTS]
    got_md = report.per_category[rs.LABEL_SPECS]
    assert (got_py.code, got_py.comment) == (py_alone.code, py_alone.comment)
    assert (got_md.code, got_md.comment) == (md_alone.code, md_alone.comment)
    # The two splits genuinely differ, or the assertions above would hold under
    # the defect too -- the fixture has to make the languages disagree.
    assert (py_alone.code, py_alone.comment) != (md_alone.code, md_alone.comment)
    # And no warning is attributed to the markdown file.
    assert [w for w in report.warnings if w.startswith("specs/")] == []


def test_adr_statuses_are_read_once_per_blob_across_a_series() -> None:
    """`adr_status_breakdown` re-read every ADR blob at every point.

    It sits outside the counts cache, so on a full walk it was the large
    majority of all blob reads -- re-deriving a `## Status` line that had not
    changed, once per commit the file survived into. Asserting the *reads*,
    because the answer is identical either way and a wrong answer is not the
    failure mode here.
    """
    adr = b"# 1\n\n## Status\n\nAccepted\n"
    source = DictSource(
        {"specs/adr/0001-a.md": adr}, blobs={"specs/adr/0001-a.md": "ad70001"}
    )
    statuses: rs.StatusCache = {}
    first = rs.build_report(source, {}, statuses)
    reads_after_first = list(source.reads)
    second = rs.build_report(source, {}, statuses)

    assert first.adr_status == {"Accepted": 1}
    assert second.adr_status == first.adr_status
    # The second pass re-reads for the *counts* (its own cache is empty here),
    # but not for the status: one status read in total, not two.
    assert reads_after_first.count("specs/adr/0001-a.md") == 2
    assert source.reads.count("specs/adr/0001-a.md") == 3
    assert statuses == {"ad70001": "Accepted"}


def test_blob_reader_close_releases_both_pipes(git_repo: Path) -> None:
    # stdout was left open for the life of the process -- which, for a
    # long-lived `--batch` child, is the whole run.
    reader = rs.BlobReader()
    with rs.BlobReader() as probe:
        blob = next(r.blob for r in rs.GitRev("HEAD", probe).files() if r.blob)
    reader.read(blob)
    proc = reader._proc  # pyright: ignore[reportPrivateUsage]
    assert proc is not None
    assert proc.stdin is not None
    assert proc.stdout is not None
    reader.close()
    assert proc.stdin.closed
    assert proc.stdout.closed
    assert proc.returncode is not None, "the child was never reaped"


def test_build_report_warns_on_non_utf8_without_crashing() -> None:
    # A Windows-1252 file (the corruption CLAUDE.md warns of) is skipped with a
    # clean warning, not a traceback.
    source = DictSource({"specs/bad.md": b"\xff\xfe title\n"})
    report = rs.build_report(source)
    assert report.warnings == ["specs/bad.md: not valid UTF-8 (skipped)"]
    assert report.per_category[rs.LABEL_SPECS].files == 0


def test_build_report_skips_an_unreadable_file() -> None:
    # PermissionError, a file removed mid-scan: warn and skip, never crash --
    # the docstring promises exit 0 once the tree enumerated.
    source = DictSource(
        {
            "src/pkg/mod.py": b"x = 1\n",
            "src/pkg/gone.py": PermissionError(13, "Permission denied"),
        }
    )
    report = rs.build_report(source)
    assert report.per_category[rs.LABEL_IMPL].files == 1
    assert any("gone.py" in w and "unreadable" in w for w in report.warnings), (
        report.warnings
    )


def test_build_report_carries_source_identity() -> None:
    source = DictSource(
        {}, label="abc1234", commit="abc1234" * 5, date="2026-08-10T00:00:00+00:00"
    )
    report = rs.build_report(source)
    assert (report.source_label, report.commit, report.date) == (
        "abc1234",
        "abc1234" * 5,
        "2026-08-10T00:00:00+00:00",
    )


# --- adr_status_breakdown -------------------------------------------------


def _adr_source(files: dict[str, object]) -> tuple[DictSource, list[rs.FileRef]]:
    source = DictSource(files)
    return source, [rs.FileRef(p) for p in sorted(files)]


def test_adr_status_breakdown_buckets_by_first_word() -> None:
    source, refs = _adr_source(
        {
            "specs/adr/0000-template.md": b"## Status\n\nProposed\n",
            "specs/adr/0001-a.md": b"## Status\n\nAccepted\n",
            "specs/adr/0002-b.md": b"## Status\n\nProposed\n",
            # A link in the status cell must not change the first-word bucket.
            "specs/adr/0003-c.md": (
                b"## Status\n\nSuperseded by [ADR-0009](0009-x.md)\n"
            ),
            "specs/adr/README.md": b"index\n",  # non-numbered: skipped
        }
    )
    assert rs.adr_status_breakdown(source, refs) == {
        "Accepted": 1,
        "Proposed": 1,  # the template's Proposed is excluded
        "Superseded": 1,
    }


def test_adr_status_breakdown_survives_a_non_utf8_adr() -> None:
    source, refs = _adr_source(
        {
            "specs/adr/0001-good.md": b"## Status\n\nAccepted\n",
            "specs/adr/0002-bad.md": b"## Status\n\n\xff\xfe\n",
        }
    )
    assert rs.adr_status_breakdown(source, refs) == {"Accepted": 1}


def test_adr_status_breakdown_skips_an_unreadable_adr() -> None:
    source, refs = _adr_source(
        {
            "specs/adr/0001-a.md": b"## Status\n\nAccepted\n",
            "specs/adr/0002-x.md": PermissionError(13, "Permission denied"),
        }
    )
    assert rs.adr_status_breakdown(source, refs) == {"Accepted": 1}


# --- commit sampling ------------------------------------------------------

# W10, W10, W12, W14, W18 / months 03, 03, 03, 04, 04 -- so weeks 11, 13 and
# 15-17 are empty periods, and the month buckets each hold more than one commit.
_COMMITS = [
    ("aaa", "2026-03-02T10:00:00+00:00"),
    ("bbb", "2026-03-03T10:00:00+00:00"),
    ("ccc", "2026-03-16T10:00:00+00:00"),
    ("ddd", "2026-04-01T10:00:00+00:00"),
    ("eee", "2026-04-30T10:00:00+00:00"),
]


def test_sample_commits_every_commit_keeps_all_of_them() -> None:
    assert rs.sample_commits(_COMMITS, "commit") == _COMMITS


def test_sample_commits_week_takes_the_last_commit_of_each_week() -> None:
    # bbb, not aaa: the state at period *end*. And no point at all for weeks
    # 11, 13, 15-17 -- an empty period is a gap, not a repeat of the last value.
    assert [sha for sha, _ in rs.sample_commits(_COMMITS, "week")] == [
        "bbb",
        "ccc",
        "ddd",
        "eee",
    ]


def test_sample_commits_month_takes_the_last_commit_of_each_month() -> None:
    assert [sha for sha, _ in rs.sample_commits(_COMMITS, "month")] == ["ccc", "eee"]


def test_sample_commits_preserves_chronological_order() -> None:
    # The order comes from dict insertion, not from sorting formatted keys, so
    # a year boundary must not reorder anything.
    across = [
        ("old", "2025-12-29T10:00:00+00:00"),
        ("new", "2026-01-05T10:00:00+00:00"),
    ]
    assert [sha for sha, _ in rs.sample_commits(across, "week")] == ["old", "new"]


# --- renderers ------------------------------------------------------------


def _small_report(
    *, label: str = "test", commit: str | None = None, date: str | None = None
) -> rs.Report:
    source = DictSource(
        {
            "src/pkg/mod.py": b"x = 1\n",
            "specs/adr/0001-a.md": b"## Status\n\nAccepted\n",
            ".claude/skills/land/SKILL.md": b"skill\n",
        },
        label=label,
        commit=commit,
        date=date,
    )
    return rs.build_report(source)


def test_render_markdown_has_table_totals_ratios_and_footnotes() -> None:
    out = rs.render_markdown(_small_report())
    assert "## Repo size so far" in out
    assert "**Total**" in out
    assert "Tests : implementation" in out
    assert "1 Accepted" in out
    assert "_Uncounted tracked files: none._" in out
    # The personal-data exclusion is stated in the output, not just the docstring.
    assert "specs/personal/" in out
    assert "excluded" in out


def test_render_markdown_names_the_revision_when_there_is_one() -> None:
    out = rs.render_markdown(
        _small_report(
            label="9d83b17", commit="9d83b17", date="2026-08-10T12:00:00+00:00"
        )
    )
    assert "## Repo size at 9d83b17 (2026-08-10)" in out
    assert "## Repo size so far" not in out


def test_render_markdown_lists_uncounted_files_when_there_are_some() -> None:
    source = DictSource({"docs/guide.md": b"x\n"})
    out = rs.render_markdown(rs.build_report(source))
    assert "Uncounted tracked files (1)" in out
    assert "`docs/guide.md`" in out


def test_docs_ratio_counts_every_markdown_category() -> None:
    """The label says "all markdown lines", so the number must include the
    harness and tooling rows -- the defect that made this ratio wrong was a
    hand-kept list of three category names beside that label."""
    source = DictSource(
        {
            "src/pkg/mod.py": b"x = 1\n",  # 1 code line
            "specs/top.md": b"a\n",
            "specs/reviews/r.md": b"b\n",
            "specs/adr/0001-a.md": b"c\n",
            ".claude/skills/land/SKILL.md": b"d\n",
            "README.md": b"e\n",
        }
    )
    report = rs.build_report(source)
    assert rs.docs_physical(report) == 5  # all five markdown files, not three
    assert rs.code_total(report) == 1
    assert "Docs : code — 5.00:1" in rs.render_markdown(report)


def test_render_json_is_valid_and_structured() -> None:
    payload = json.loads(rs.render_json(_small_report()))
    assert set(payload) == {
        "categories",
        "adr_status",
        "warnings",
        "uncounted",
        "source",
        "commit",
        "date",
        "categories_version",
    }
    impl = payload["categories"][rs.LABEL_IMPL]
    assert set(impl) == {"files", "physical", "code", "comment", "blank", "bytes"}
    assert impl["files"] == 1


def test_render_history_csv_is_tidy_long() -> None:
    reports = [
        _small_report(commit="aaaaaaaaaaaa", date="2026-03-02T10:00:00+00:00"),
        _small_report(commit="bbbbbbbbbbbb", date="2026-04-01T10:00:00+00:00"),
    ]
    rows = list(csv.reader(io.StringIO(rs.render_history_csv(reports))))
    assert rows[0] == rs.HISTORY_COLUMNS
    # One row per (commit, category) -- nine categories, two commits.
    assert len(rows) == 1 + 2 * len(rs.CATEGORIES)
    assert rows[1][:3] == ["aaaaaaa", "2026-03-02", rs.LABEL_IMPL]
    assert {r[0] for r in rows[1:]} == {"aaaaaaa", "bbbbbbb"}
    assert {r[-1] for r in rows[1:]} == {str(rs.CATEGORIES_VERSION)}


def test_render_history_json_wraps_the_same_point_shape() -> None:
    point = _small_report(commit="a" * 40, date="2026-03-02T00:00:00+00:00")
    payload = json.loads(rs.render_history_json([point]))
    assert payload["categories_version"] == rs.CATEGORIES_VERSION
    assert len(payload["points"]) == 1
    # The full sha survives in JSON even though the CSV abbreviates it.
    assert payload["points"][0]["commit"] == "a" * 40


def test_render_history_md_is_one_row_per_point() -> None:
    reports = [
        _small_report(commit="aaaaaaaaaaaa", date="2026-03-02T10:00:00+00:00"),
        _small_report(commit="bbbbbbbbbbbb", date="2026-04-01T10:00:00+00:00"),
    ]
    out = rs.render_history_md(reports).splitlines()
    assert out[0] == "## Repo shape over 2 point(s)"
    body = [line for line in out if line.startswith(("| aaaaaaa", "| bbbbbbb"))]
    assert len(body) == 2


def _degraded_pair() -> tuple[rs.Report, rs.Report]:
    """Two reports where the head skipped a file it could previously read.

    The shape the diagnostics findings are about: the file's absence renders as
    negative growth, which is a confident number describing a measurement that
    is known to be incomplete.
    """
    base = rs.build_report(DictSource({"specs/a.md": b"one\ntwo\n"}, label="aaaaaaa"))
    head = rs.build_report(
        DictSource({"specs/a.md": b"\xff\xfe not utf-8\n"}, label="bbbbbbb")
    )
    return base, head


def test_a_skipped_file_is_named_in_the_diff_not_shown_as_shrinkage() -> None:
    base, head = _degraded_pair()
    assert head.warnings, "the fixture did not actually degrade, so this proves nothing"
    out = rs.render_diff(base, head)
    # The delta itself still reads as a loss -- that is what the numbers say --
    # so the marker beside it is the whole remedy.
    assert "-2" in out
    assert "**Warnings**" in out
    assert "bbbbbbb: specs/a.md: not valid UTF-8 (skipped)" in out


def test_a_skipped_file_is_named_in_the_history_table_and_chart() -> None:
    base, head = _degraded_pair()
    base.date = head.date = "2026-03-02T10:00:00+00:00"
    base.commit, head.commit = "a" * 40, "b" * 40
    md = rs.render_history_md([base, head])
    assert "were measured in a degraded way" in md
    assert "bbbbbbb: specs/a.md: not valid UTF-8 (skipped)" in md
    assert "measured in a degraded way" in rs.render_history_html([base, head])


def _uncovered_pair() -> tuple[rs.Report, rs.Report]:
    """Two reports where the head gained a file no category claims.

    Not a *degraded* measurement -- nothing failed to read or parse -- but an
    incomplete one, and the two are reported through separate channels because
    they mean different things to a reader.
    """
    base = rs.build_report(DictSource({"specs/a.md": b"one\n"}, label="aaaaaaa"))
    head = rs.build_report(
        DictSource(
            {"specs/a.md": b"one\n", "docs/guide.md": b"x\ny\n"}, label="bbbbbbb"
        )
    )
    return base, head


def test_an_uncounted_file_reaches_every_history_output() -> None:
    """The leak check reported to nobody in three of its four formats.

    `Report.uncounted` reached the snapshot table, the snapshot JSON and the
    diff table -- and not history's default CSV, its markdown table, its HTML
    chart, or the diff CSV, because the stderr emitter read `Report.warnings`
    alone. Measured with a planted `docs/guide.md`: zero mentions in any of
    them, nothing on stderr, exit 0.

    Asserted per format rather than once, because the defect was *per format*:
    a single check would have passed against the two that already worked.
    """
    base, head = _uncovered_pair()
    assert head.uncounted == ["docs/guide.md"], "the fixture planted no leak"
    assert base.uncounted == []
    base.date = head.date = "2026-03-02T10:00:00+00:00"
    base.commit, head.commit = "a" * 40, "b" * 40

    assert "bbbbbbb: docs/guide.md" in rs.render_history_md([base, head])
    assert "in no category" in rs.render_history_html([base, head])
    assert "docs/guide.md" in rs.render_history_json([base, head])


def test_the_machine_formats_announce_an_uncounted_file_on_stderr(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """CSV has no slot for prose, so stderr is the only channel it has.

    This is the half that made the leak check silent in the *default* output:
    `history` with no `--format` is CSV, and CSV is where an uncovered tree was
    least likely to be noticed.
    """
    base, head = _uncovered_pair()
    rs._emit_diagnostics([base, head])  # pyright: ignore[reportPrivateUsage]
    err = capsys.readouterr().err
    assert "1 tracked file(s) in no category" in err
    assert "bbbbbbb: docs/guide.md" in err


def test_a_retained_file_is_not_reported_as_a_skipped_one(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An unparseable Python file is counted, not skipped, and it inflates
    `code` rather than understating anything.

    Every diagnostic header once said the files were "skipped" and the numbers
    "understate the tree", printed directly above a line reading "could not
    parse as Python; docstrings counted as code" -- self-contradictory in two
    adjacent lines. Measured on one file: files 1 -> 1, physical 5 -> 6, code
    1 -> **6**, comment 4 -> **0**.

    The two claims are asserted separately: that the counts really do move the
    way the old prose denied, and that no output still makes either claim.
    """
    clean = rs.build_report(
        DictSource({"src/m.py": b'"""One\nTwo\nThree\n"""\nx = 1\n'}, label="aaaaaaa")
    )
    broken = rs.build_report(
        DictSource(
            {"src/m.py": b'"""One\nTwo\nThree\n"""\nx = 1\ndef (\n'}, label="bbbbbbb"
        )
    )
    assert broken.warnings, "the fixture did not actually fail to parse"
    assert clean.per_category[rs.LABEL_IMPL].files == 1
    assert broken.per_category[rs.LABEL_IMPL].files == 1, "retained, not skipped"
    assert clean.per_category[rs.LABEL_IMPL].code == 1
    assert broken.per_category[rs.LABEL_IMPL].code == 6, "code went UP"
    assert broken.per_category[rs.LABEL_IMPL].comment == 0

    broken.date = clean.date = "2026-03-02T10:00:00+00:00"
    broken.commit, clean.commit = "b" * 40, "a" * 40
    rs._emit_diagnostics([broken])  # pyright: ignore[reportPrivateUsage]
    surfaces = [
        capsys.readouterr().err,
        rs.render_history_md([clean, broken]),
        rs.render_history_html([clean, broken]),
        rs.render_diff(clean, broken),
    ]
    for text in surfaces:
        assert "docstrings counted as code" in text, "the per-file line must survive"
        assert "understate" not in text
        assert "file(s) skipped" not in text
        assert "files skipped" not in text


def test_warning_lines_attributes_each_warning_to_its_point() -> None:
    # The source label is on every line rather than as a heading over a group:
    # two ends of a diff can warn about one path for different reasons, and a
    # reader needs to know which end is the degraded one.
    base, head = _degraded_pair()
    assert rs.warning_lines([base, head]) == [
        "bbbbbbb: specs/a.md: not valid UTF-8 (skipped)"
    ]
    assert rs.warning_lines([base]) == []


def test_a_path_git_could_not_decode_is_escaped_before_it_reaches_a_report() -> None:
    """`_split_z` decodes with `surrogateescape`, so a non-UTF-8 path arrives as
    lone surrogates -- which `Path.write_text(encoding="utf-8")` refuses.

    The failure was `--out`-only, i.e. the scripted and CI path: stdout survived
    on `_use_utf8_io`'s `errors="replace"`. Escaping at the boundary where a
    path becomes text keeps the report able to *name* the file, which replacing
    the bytes at read time would not.
    """
    bad = "docs/caf\udcff.md"
    _, uncounted = rs.categorize([rs.FileRef(bad)])
    assert uncounted == ["docs/caf\\xff.md"]
    # The whole point: the rendered report is now writable as strict UTF-8.
    report = rs.build_report(DictSource({bad: b"hi\n"}))
    rs.render_markdown(report).encode("utf-8")
    rs.render_json(report).encode("utf-8")


def test_a_single_point_chart_draws_visible_geometry() -> None:
    """n = 1 produced a two-vertex polygon of zero area and no stroke.

    The reader got axes, a tick and a full legend with no data and nothing
    saying the series was too short to plot. Reached by `--every month` inside
    one calendar month, `--every week` inside one week, or a narrow `--since`.
    Asserting *geometry*, not element count: nine `<polygon>` elements were
    present under the defect too.
    """
    point = _small_report(commit="a" * 12, date="2026-03-02T10:00:00+00:00")
    out = rs.render_history_html([point])
    polygons = re.findall(r'<polygon points="([^"]+)"', out)
    assert len(polygons) == len(rs.CATEGORIES)
    xs = {vertex.split(",")[0] for poly in polygons for vertex in poly.split()}
    assert len(xs) > 1, "every vertex shares one x, so the bands have zero area"


def test_the_x_axis_is_proportional_to_elapsed_time() -> None:
    """It was the commit *ordinal*, so a six-month gap and a six-minute one
    occupied the same horizontal distance while the ticks were labelled with
    dates -- inviting exactly the rate-of-change reading it cannot support.
    """
    reports = [
        _small_report(commit="a" * 12, date="2026-01-01T00:00:00+00:00"),
        _small_report(commit="b" * 12, date="2026-07-01T00:00:00+00:00"),
        _small_report(commit="c" * 12, date="2026-07-02T00:00:00+00:00"),
    ]
    out = rs.render_history_html(reports)
    band = re.findall(r'<polygon points="([^"]+)"', out)[0]
    first = [float(v.split(",")[0]) for v in band.split()]
    # Three points, so the first three vertices are the upper edge left to right.
    a, b, c = first[0], first[1], first[2]
    assert b - a > 100 * (c - b), "the six-month gap is not wider than the one-day gap"


def test_a_full_length_series_labels_its_rightmost_point() -> None:
    # `range(0, n, step)` omits the last index whenever step does not divide
    # n - 1: at n = 100 the ticks landed at 0, 16, ... 96 and the reader took the
    # chart's right edge to be a date four commits earlier than it is.
    reports = [
        _small_report(commit=f"{i:012d}", date="2026-01-01T00:00:00+00:00")
        for i in range(99)
    ]
    reports.append(_small_report(commit="z" * 12, date="2026-12-31T00:00:00+00:00"))
    out = rs.render_history_html(reports)
    # Read off the tick elements, not off the whole document: the subtitle names
    # the span's end too, so a substring search over `out` passes under the
    # defect -- which is how this test first passed a mutation that removed the
    # fix entirely.
    ticks = re.findall(r'text-anchor="middle">([^<]+)</text>', out)
    assert ticks, "no ticks were rendered, so this proves nothing"
    assert ticks[-1] == "2026-12-31"


def test_render_history_html_draws_one_band_per_category() -> None:
    reports = [
        _small_report(commit="a" * 12, date="2026-03-02T10:00:00+00:00"),
        _small_report(commit="b" * 12, date="2026-04-01T10:00:00+00:00"),
    ]
    out = rs.render_history_html(reports)
    assert out.count("<polygon") == len(rs.CATEGORIES)
    assert "2026-03-02 → 2026-04-01" in out
    assert f"categories v{rs.CATEGORIES_VERSION}" in out
    # No network dependency: the chart must open offline.
    assert "http://" not in out
    assert "https://" not in out
    # Labels reach the SVG as text and one of them really does contain an
    # ampersand, so escaping is exercised by the live label set, not by a
    # contrived string: "Harness — skills & agents".
    assert "skills &amp; agents" in out
    assert "skills & agents" not in out


# --- diff -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("spec", "head", "expected"),
    [
        # Two explicit revisions, and the two-dot spelling of the same pair.
        ("main", "HEAD", ("main", "HEAD")),
        ("main..HEAD", None, ("main", "HEAD")),
        # An omitted side means HEAD, as it does in git.
        ("main..", None, ("main", "HEAD")),
        ("..main", None, ("HEAD", "main")),
        # No range at all: head stays None, meaning the working tree.
        ("main", None, ("main", None)),
    ],
)
def test_parse_rev_range_follows_gits_spellings(
    spec: str, head: str | None, expected: tuple[str, str | None]
) -> None:
    assert rs.parse_rev_range(spec, head) == expected


def test_parse_rev_range_refuses_a_range_plus_a_third_revision() -> None:
    # "main..HEAD v0.3" names three endpoints for a two-ended comparison; the
    # silent alternative is picking one and ignoring the other.
    with pytest.raises(rs.StatsError) as excinfo:
        rs.parse_rev_range("main..HEAD", "v0.3")
    assert "both ends" in str(excinfo.value)
    assert "v0.3" in str(excinfo.value)


def _pair() -> tuple[rs.Report, rs.Report]:
    base = rs.build_report(
        DictSource({"src/pkg/mod.py": b"x = 1\n"}, label="aaaaaaa", commit="a" * 40)
    )
    head = rs.build_report(
        DictSource(
            {
                "src/pkg/mod.py": b"x = 1\ny = 2\n",
                "tests/test_x.py": b"z = 3\n",
            },
            label="bbbbbbb",
            commit="b" * 40,
        )
    )
    return base, head


def test_a_byte_only_delta_is_not_reported_as_no_change() -> None:
    """`_is_zero` hand-listed five of `Counts`' six fields and omitted `nbytes`.

    Two reports differing only in bytes rendered as "_No category changed._" in
    `--format md` while `--format json` and `--format csv` reported the byte
    delta from the same pair -- three formats of one command disagreeing about
    whether anything happened. Any edit preserving line counts reaches it:
    reworded prose, a lengthened line, a file swapped for one of equal shape.
    """
    base = rs.build_report(DictSource({"specs/a.md": b"one\n"}))
    head = rs.build_report(DictSource({"specs/a.md": b"ones\n"}))
    delta = rs.category_deltas(base, head)[rs.LABEL_SPECS]
    assert (delta.physical, delta.files) == (0, 0)
    assert delta.nbytes == 1
    assert "_No category changed._" not in rs.render_diff(base, head)


def test_category_deltas_are_signed_both_ways() -> None:
    base = rs.build_report(DictSource({"specs/a.md": b"one\ntwo\n"}))
    head = rs.build_report(DictSource({"src/pkg/mod.py": b"x = 1\n"}))
    deltas = rs.category_deltas(base, head)
    # A category that grew and one that shrank, in the same comparison.
    assert deltas[rs.LABEL_IMPL].physical == 1
    assert deltas[rs.LABEL_SPECS].physical == -2
    assert deltas[rs.LABEL_SPECS].files == -1


def test_render_diff_shows_only_changed_categories_by_default() -> None:
    out = rs.render_diff(*_pair())
    assert "## Repo shape: aaaaaaa → bbbbbbb" in out
    assert rs.LABEL_IMPL in out  # grew by a line
    assert rs.LABEL_TESTS in out  # appeared
    # Seven categories were untouched and must not pad the table.
    assert rs.LABEL_ADR not in out
    assert "+1" in out


def test_render_diff_all_includes_untouched_categories() -> None:
    out = rs.render_diff(*_pair(), show_all=True)
    for cat in rs.CATEGORIES:
        assert cat.label in out


def test_render_diff_says_so_when_nothing_moved() -> None:
    # The honest answer to "what changed" is sometimes "nothing" -- an empty
    # table with only a Total row of zeros reads like a broken report instead.
    same = rs.build_report(DictSource({"src/pkg/mod.py": b"x = 1\n"}))
    out = rs.render_diff(same, same)
    assert "_No category changed._" in out


def test_render_diff_reports_ratios_as_a_transition() -> None:
    out = rs.render_diff(*_pair())
    assert "Tests : implementation — 0.00:1 → 0.50:1" in out
    assert "→" in out


@pytest.mark.parametrize(
    ("base", "head", "expected"),
    [
        ({"Accepted": 3}, {"Accepted": 3}, "no change (3 numbered ADRs)"),
        ({"Accepted": 3}, {"Accepted": 4}, "Accepted 3 → 4"),
        # A newly-appearing bucket counts as movement from zero.
        ({"Accepted": 3}, {"Accepted": 3, "Proposed": 1}, "Proposed 0 → 1"),
    ],
)
def test_adr_delta_names_only_the_buckets_that_moved(
    base: dict[str, int], head: dict[str, int], expected: str
) -> None:
    assert rs.adr_delta(base, head) == expected


def test_render_diff_csv_carries_base_head_and_delta() -> None:
    rows = list(csv.reader(io.StringIO(rs.render_diff_csv(*_pair()))))
    assert rows[0] == rs.DIFF_COLUMNS
    by_key = {(r[0], r[1]): r[2:] for r in rows[1:]}
    stamp = str(rs.CATEGORIES_VERSION)
    assert by_key[(rs.LABEL_IMPL, "physical")] == ["1", "2", "1", stamp]
    assert by_key[(rs.LABEL_TESTS, "files")] == ["0", "1", "1", stamp]
    # Every category appears, changed or not: a CSV is for pivoting, and a
    # missing row is indistinguishable from a zero once it is in a spreadsheet.
    assert len({r[0] for r in rows[1:]}) == len(rs.CATEGORIES)
    # The byte metric is spelled the way every other emitter spells it. It said
    # `nbytes` here and `bytes` in the history CSV and the snapshot payload --
    # the attribute name leaking onto the wire because `bytes` is a builtin --
    # so a consumer pivoting the two dropped the byte row silently.
    assert (rs.LABEL_IMPL, "bytes") in by_key
    assert (rs.LABEL_IMPL, "nbytes") not in by_key
    # The lens stamp reaches this CSV too. `repo-history/SKILL.md` promises it
    # is in "every CSV row" and DIFF_COLUMNS carried none, so a stored diff was
    # the one artifact that could not be recognised as measured under old
    # categories -- the exact staleness the stamp exists to make visible.
    assert rows[0][-1] == "categories_version"


def test_render_diff_json_holds_both_reports_and_the_delta() -> None:
    payload = json.loads(rs.render_diff_json(*_pair()))
    assert set(payload) == {"categories_version", "base", "head", "delta"}
    assert payload["base"]["commit"] == "a" * 40
    assert payload["head"]["commit"] == "b" * 40
    assert payload["delta"]["categories"][rs.LABEL_IMPL]["physical"] == 1
    assert payload["delta"]["total"]["physical"] == 2


# --- the git-backed sources, against a real repository --------------------

_GIT = shutil.which("git") or "git"


def _neutral_git_env(home: Path) -> dict[str, str]:
    """A git environment that ignores the developer's own configuration.

    This was a module-level constant carrying `"HOME": ""  # filled in
    per-fixture` -- never filled in, and never *used*: `_run_git` passed no
    `env=` at all, so every fixture repository was built under whatever global
    config the machine happened to have. A `core.autocrlf=true` or an
    `init.defaultBranch` there would change what these tests measure, on one
    developer's machine and not in CI, which is the environment-dependence
    `specs/testing-strategy.md` names and the fixture's own `git config` lines
    were already reaching for one setting at a time.

    A function rather than a constant because `HOME` has to be a real empty
    directory: git reads `~/.gitconfig`, and an empty string for `HOME` is not
    portably "no home".
    """
    return {
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": str(home / "gitconfig-absent"),
        "GIT_TERMINAL_PROMPT": "0",
        "HOME": str(home),
        "USERPROFILE": str(home),
        "PATH": os.environ.get("PATH", ""),
        "SYSTEMROOT": os.environ.get("SYSTEMROOT", ""),
    }


def _run_git(root: Path, *args: str) -> None:
    # `env=` is the half that was missing: the neutral environment was declared
    # and never passed, so every fixture repository inherited the developer's
    # global git config. `root.parent` is the tmp_path the fixture owns, which
    # makes HOME a real empty directory rather than the unusable `""`.
    subprocess.run(  # noqa: S603 - fixed executable, test-local paths, no shell
        [_GIT, "-C", str(root), *args],
        check=True,
        capture_output=True,
        timeout=60,
        env=_neutral_git_env(root.parent),
    )


@pytest.fixture
def git_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A throwaway repository with two commits, with rs.REPO_ROOT pointed at it.

    Never the real tree: every count here must be independent of how large this
    project has actually grown, and `repo_stats` runs git with `cwd=REPO_ROOT`,
    so repointing that one global is what redirects the whole module.
    """
    root = tmp_path / "repo"
    root.mkdir()
    _run_git(root, "init", "-b", "main")
    _run_git(root, "config", "user.email", "test@example.invalid")
    _run_git(root, "config", "user.name", "Test")
    _run_git(root, "config", "commit.gpgsign", "false")
    _run_git(root, "config", "core.autocrlf", "false")
    monkeypatch.setattr(rs, "REPO_ROOT", root)

    (root / "src").mkdir()
    (root / "src" / "mod.py").write_text("x = 1\n", encoding="utf-8")
    (root / "README.md").write_text("hello\n", encoding="utf-8")
    _run_git(root, "add", "-A")
    _run_git(root, "commit", "-m", "first")

    (root / "specs").mkdir()
    (root / "specs" / "design.md").write_text("a\nb\nc\n", encoding="utf-8")
    _run_git(root, "add", "-A")
    _run_git(root, "commit", "-m", "second")
    return root


def test_worktree_enumerates_tracked_files_only(git_repo: Path) -> None:
    (git_repo / "untracked.py").write_text("z = 9\n", encoding="utf-8")
    paths = [ref.path for ref in rs.WorkTree().files()]
    assert paths == ["README.md", "specs/design.md", "src/mod.py"]
    # The documented consequence of enumerating with git rather than rglob.
    assert "untracked.py" not in paths


def test_worktree_reads_uncommitted_edits(git_repo: Path) -> None:
    (git_repo / "src" / "mod.py").write_text("x = 1\ny = 2\n", encoding="utf-8")
    report = rs.build_report(rs.WorkTree())
    assert report.per_category[rs.LABEL_IMPL].physical == 2


def test_personal_paths_are_never_enumerated_at_any_revision(git_repo: Path) -> None:
    """ADR-0079: `specs/personal/` must not exist, and if it somehow does it is
    neither counted nor read -- including in history, where a past commit is
    beyond anyone's power to edit.

    The exclusion lives in the enumeration layer, so this asserts it for both
    sources at once. The content below is synthetic filler, not data.

    **Both halves, because `not any(...)` over an empty list is True.** This
    asserted only the absence, so breaking either source into returning `[]` --
    an `_included` that rejects everything, an `ls-files` pathspec typo, a
    predicate raising and swallowed upstream -- left it green while the
    guarantee it exists to hold went entirely unexercised. Pinning the ordinary
    files as present is what proves the enumeration ran at all.

    The case variants are deliberately *not* planted here as directories: the
    filesystems two of the three CI legs run on fold case, so `specs/Personal`
    is the same directory as `specs/personal` and the fixture would be testing
    the platform rather than the rule. That half is pinned where it can be
    pinned identically everywhere -- `test_the_containment_predicate_agrees_with
    _the_gates_copy`, over strings -- and this test pins the other half, that
    both enumerations actually apply the predicate.
    """
    personal = git_repo / "specs" / "personal"
    personal.mkdir()
    (personal / "notes.md").write_text("synthetic placeholder\n", encoding="utf-8")
    _run_git(git_repo, "add", "-A")
    _run_git(git_repo, "commit", "-m", "third")

    expected_ordinary = ["README.md", "specs/design.md", "src/mod.py"]
    for label, refs in (
        ("working tree", rs.WorkTree().files()),
        ("revision", _rev_files("HEAD")),
    ):
        paths = [ref.path for ref in refs]
        assert paths == expected_ordinary, (
            f"the {label} source did not enumerate the ordinary files, so the "
            "absence assertion below would prove nothing"
        )
        assert not any(rs._is_personal_path(p) for p in paths), (  # pyright: ignore[reportPrivateUsage]
            f"the {label} source enumerated an excluded path"
        )


def _rev_files(rev: str) -> list[rs.FileRef]:
    with rs.BlobReader() as reader:
        return rs.GitRev(rev, reader).files()


def test_a_conflicted_index_does_not_multiply_the_counts(git_repo: Path) -> None:
    """`git ls-files` emits one line per index stage, not one per path.

    During an unresolved merge a conflicted path arrives three times (stages
    1/2/3), so an undeduplicated enumeration reported a one-line file as three
    files and three lines -- exit 0, no warning, every total silently inflated.
    Mid-merge is routine in the squash-merge workflow this harness drives, and
    `GitRev.files()` can never produce a duplicate, so the two sources also
    disagreed about what `files()` means.
    """
    _run_git(git_repo, "checkout", "-b", "other", "HEAD~1")
    (git_repo / "src" / "mod.py").write_text("other = 1\n", encoding="utf-8")
    _run_git(git_repo, "commit", "-am", "other side")
    _run_git(git_repo, "checkout", "main")
    (git_repo / "src" / "mod.py").write_text("main = 1\n", encoding="utf-8")
    _run_git(git_repo, "commit", "-am", "main side")
    subprocess.run(  # noqa: S603 - the merge is expected to conflict, so check=False
        [_GIT, "-C", str(git_repo), "merge", "other"],
        check=False,
        capture_output=True,
        timeout=60,
    )

    staged = subprocess.run(  # noqa: S603 - fixed executable, test-local path
        [_GIT, "-C", str(git_repo), "ls-files", "-z"],
        check=True,
        capture_output=True,
        timeout=60,
    ).stdout.decode()
    assert staged.count("src/mod.py") == 3, (
        "the fixture is not actually mid-conflict, so this test proves nothing"
    )

    paths = [ref.path for ref in rs.WorkTree().files()]
    assert paths.count("src/mod.py") == 1
    assert rs.build_report(rs.WorkTree()).per_category[rs.LABEL_IMPL].files == 1


def test_build_series_does_not_re_resolve_the_shas_it_was_given(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`list_commits` returns full shas; re-resolving each one spawned a
    `rev-parse` per commit to re-answer a question already answered.

    Measured on a 35-commit walk before the fix: 16.97 s against 10.37 s with
    the re-resolve skipped, output byte-identical, `GIT_TRACE` showing rev-parse
    spawns matching ls-tree spawns 1:1 -- 39% of the walk. Counting the
    subcommands rather than timing them, because a timing assertion on a shared
    CI runner is a flake waiting to happen and the spawn count is the thing that
    actually changed.
    """
    calls: list[str] = []
    real = rs._git_out  # pyright: ignore[reportPrivateUsage]

    def counting(*args: str) -> bytes:
        calls.append(args[0])
        return real(*args)

    monkeypatch.setattr(rs, "_git_out", counting)
    commits = rs.list_commits("main")
    assert len(commits) == 2, "the fixture changed; this test counts per commit"
    calls.clear()
    reports = rs.build_series(commits)
    assert [r.commit for r in reports] == [sha for sha, _ in commits]
    assert calls.count("rev-parse") == 0
    assert calls.count("ls-tree") == len(commits)
    # And the date is not re-asked either -- the short-circuit this one mirrors.
    assert calls.count("log") == 0


def test_the_working_tree_is_cached_by_content_not_by_its_index_id(
    git_repo: Path,
) -> None:
    """The cache must still answer across a diff's two ends, without an index id.

    `WorkTree` once carried the index blob id so `_run_diff`'s shared cache had
    a key on this side -- measured, 0 of 297 refs had an id before that, every
    file was re-read and re-AST-parsed although the byte-identical blobs had
    just been classified into that cache, and `diff <base>` came out *slower*
    than `diff <base> <head>`.

    The id was unsound (see the sibling test below), so the key now comes from
    the bytes actually read. This pins what the id was for: identical content
    still lands on one cache entry, whichever side produced it.
    """
    assert all(ref.blob is None for ref in rs.WorkTree().files())

    cache: rs.BlobCache = {}
    with rs.BlobReader() as reader:
        rs.build_report(rs.GitRev("HEAD", reader), cache)
    entries_after_rev = len(cache)
    assert entries_after_rev, "the revision side cached nothing; test proves nothing"

    # The tree is clean, so every working-tree file has the same content as
    # HEAD: the second report must add no new entry, i.e. it hit all of them.
    rs.build_report(rs.WorkTree(), cache)
    assert len(cache) == entries_after_rev

    # And one changed file adds exactly one entry rather than re-caching the lot.
    (git_repo / "src" / "mod.py").write_text("x = 1\ny = 2\n", encoding="utf-8")
    rs.build_report(rs.WorkTree(), cache)
    assert len(cache) == entries_after_rev + 1


def test_an_edit_git_calls_unmodified_is_still_counted(git_repo: Path) -> None:
    """`diff-files` is not a byte-identity test, and the cache must not treat it
    as one.

    Measured on the shape this closes: five lines added to an
    `assume-unchanged` file gave `snapshot` 8 physical lines and `diff HEAD`
    **+0** for the same tree in the same second -- because `diff-files` skips
    those entries, so the path kept its index id and the cache answered the
    read with HEAD's counts. The committed-content answer was the *cheaper*
    one, which is why it won.

    Asserted through the cache rather than through `files()`: an id that is
    never consulted cannot lie, and it is being consulted that was the defect.
    """
    _run_git(git_repo, "update-index", "--assume-unchanged", "src/mod.py")
    (git_repo / "src" / "mod.py").write_text(
        "a = 1\nb = 2\nc = 3\nd = 4\ne = 5\n", encoding="utf-8"
    )
    assert not rs._git_out("diff-files", "--name-only").strip(), (  # pyright: ignore[reportPrivateUsage]
        "premise: git reports this tree clean"
    )

    # Prime the cache from HEAD, which is what a diff or a history walk does,
    # then measure the working tree through that same cache.
    cache: rs.BlobCache = {}
    with rs.BlobReader() as reader:
        head = rs.build_report(rs.GitRev("HEAD", reader), cache)
    tree = rs.build_report(rs.WorkTree(), cache)
    assert head.per_category[rs.LABEL_IMPL].physical == 1
    assert tree.per_category[rs.LABEL_IMPL].physical == 5


def test_the_content_id_is_gits_own_blob_id(git_repo: Path) -> None:
    """`_content_id` has to agree with git, or the two ends never share an entry.

    The whole reason a working-tree file can be keyed without an index id is
    that the id it gets instead is the one git would give the same bytes -- so
    `GitRev`'s entry and `WorkTree`'s entry for identical content are the same
    key. Pinned against `git hash-object` rather than against a literal: a
    hand-copied sha would pass while agreeing with nothing.

    Includes CRLF bytes deliberately. That is the quiet half of the defect this
    keying closes -- a file written CRLF and staged under `* text=auto eol=lf`
    sits on disk with more bytes than its index blob, and `diff-files` stays
    silent -- and it matters here that such content gets an id of its *own*
    rather than the normalized blob's.
    """
    for content in (b"", b"x = 1\n", b"a = 1\r\nb = 2\r\n", b"\x00\xff binary"):
        (git_repo / "probe.bin").write_bytes(content)
        expected = (
            subprocess.run(  # noqa: S603 - fixed executable, test paths
                [_GIT, "-C", str(git_repo), "hash-object", "--no-filters", "probe.bin"],
                check=True,
                capture_output=True,
                timeout=60,
                env=_neutral_git_env(git_repo.parent),
            )
            .stdout.decode()
            .strip()
        )
        assert rs._content_id(content) == expected  # pyright: ignore[reportPrivateUsage]


def test_a_wedged_cat_file_fails_the_report_rather_than_hanging_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every git call in this module is bounded; these two were not.

    `BlobReader` holds one long-lived `git cat-file --batch`, so the blocking
    call is a pipe read and `subprocess.run`'s `timeout=` cannot reach it --
    `_GIT_TIMEOUT` reached `_git_out` and `close()`, and `close()` is on the
    shutdown path, never reached while a read is blocked. A wedged child hung
    the whole report, against this module's own comment that a hung git should
    fail the report rather than hang it. `tests/test_git_runners.py` cannot see
    this: its scan matches `subprocess.run` only, and its `Popen` exclusion
    named `review_worktree._spawn` as the sole caller built this way.

    The timeout is shortened rather than waited out, and the assertion is on the
    *message* -- a plain "closed its output" would also be raised by a child
    that died for an unrelated reason, which is not what this pins.
    """
    monkeypatch.setattr(rs, "_GIT_TIMEOUT", 0.4)

    class Wedged:
        """Stands in for a `git cat-file` that accepts a request and answers
        nothing. `kill()` releases the read, exactly as killing the real child
        closes its pipe."""

        def __init__(self) -> None:
            self.stdin = io.BytesIO()
            self.stdout = _BlockingReader()
            self.killed = False

        def kill(self) -> None:
            self.killed = True
            self.stdout.release()

    wedged = Wedged()
    reader = rs.BlobReader()
    monkeypatch.setattr(reader, "_ensure", lambda: wedged)
    with pytest.raises(rs.StatsError, match=r"did not answer.*within"):
        reader.read("0" * 40)
    assert wedged.killed, "the watchdog never fired, so the read merely failed"


def test_a_blob_git_does_not_have_is_named_in_the_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`git cat-file --batch` answers `<sha> missing` for an object it lacks.

    Reachable in ordinary use: `ls-tree` names objects a blobless partial clone
    has never fetched, and a corrupt pack loses one from a repository that lists
    it. The branch had no test; the stub technique is the wedged-pipe test's,
    one row down.
    """
    sha = "0" * 40

    class Missing:
        def __init__(self) -> None:
            self.stdin = io.BytesIO()
            self.stdout = io.BytesIO(f"{sha} missing\n".encode("ascii"))

    reader = rs.BlobReader()
    monkeypatch.setattr(reader, "_ensure", lambda: Missing())
    with pytest.raises(rs.StatsError, match="has no blob"):
        reader.read(sha)


def test_an_unreadable_cat_file_header_is_a_different_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A header that is neither `<sha> <type> <size>` nor `<sha> missing`.

    Split from the missing-object case because they are different faults with
    different fixes -- git correctly reporting an absent object, against git
    saying something this protocol reader does not understand. They shared one
    condition and one sentence, and since git's real answer is exactly two
    fields the `missing` arm could never fire on its own: a mutation removing it
    changed nothing observable, which is how the conflation surfaced.
    """

    class Garbled:
        def __init__(self) -> None:
            self.stdin = io.BytesIO()
            self.stdout = io.BytesIO(b"not-a-header\n")

    reader = rs.BlobReader()
    monkeypatch.setattr(reader, "_ensure", lambda: Garbled())
    with pytest.raises(rs.StatsError, match="unreadable header"):
        reader.read("1" * 40)


def test_a_dead_cat_file_fails_the_run_instead_of_reporting_an_empty_repo(
    git_repo: Path,
) -> None:
    """The settled answer to the round's one disputed finding (§D3).

    A blob that cannot be read **at a revision** stops the run; it is not warned
    about and skipped the way an unreadable working-tree file is. Skipping does
    not make the number slightly incomplete, it makes it *wrong*: the point
    reports a smaller repository than existed, and nothing downstream can tell
    that from real shrinkage.

    The shape this pins is the one that was live and that neither review lens
    found. `BlobReader` guarded its write with `except (BrokenPipeError,
    ValueError)`, but a killed child raises a bare `OSError: [Errno 22]` on
    Windows -- so the failure escaped **as an OSError**, straight into
    `build_report`'s warn-and-skip guard, which exists for a working-tree file
    and cannot tell one from a dead subprocess. `_ensure` never restarts a dead
    child, so every remaining blob took the same path: measured, a 3-point walk
    reported physical=0 with 275 warnings for points 2 and 3 and exited **0**.

    Asserting the exception *and* the absence of the fabricated report, because
    the defect's signature was a successful-looking result, not an error.
    """
    with rs.BlobReader() as reader:
        source = rs.GitRev("HEAD", reader)
        proc = reader._ensure()  # pyright: ignore[reportPrivateUsage]
        proc.kill()
        proc.wait()
        with pytest.raises(rs.StatsError) as caught:
            rs.build_report(source)

    message = str(caught.value)
    # Names the object, the commit and the path -- a sha alone is unplaceable
    # after a walk that may have run for half a minute.
    assert "cat-file died" in message
    assert source.label in message
    assert "empty repository" in message


def test_a_pipe_that_fails_mid_read_is_also_a_run_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The read half of the same conversion, reached on its own.

    `test_a_dead_cat_file_fails_the_run_instead_of_reporting_an_empty_repo`
    cannot cover it: killing the child makes the *write* fail first, so the read
    guard is never entered and a mutation removing it survives that test
    (measured). The two pipe operations have to convert identically -- guarding
    sibling operations differently is the precise shape of the defect both
    guards exist to close -- so the read side needs a stub that gets past the
    write and fails afterwards.
    """

    class WritesThenBreaks:
        def __init__(self) -> None:
            self.stdin = io.BytesIO()

            class Stdout:
                def readline(self) -> bytes:
                    raise OSError(22, "Invalid argument")

                def read(self, _size: int = -1) -> bytes:  # pragma: no cover
                    raise OSError(22, "Invalid argument")

            self.stdout = Stdout()

    reader = rs.BlobReader()
    monkeypatch.setattr(reader, "_ensure", lambda: WritesThenBreaks())
    with pytest.raises(rs.StatsError, match="cat-file died"):
        reader.read("0" * 40)


def test_an_unreadable_working_tree_file_is_still_only_a_warning(
    git_repo: Path,
) -> None:
    """The other half of the same decision, and the reason it is a decision.

    The revision source fails the run; the working-tree source keeps its
    warn-and-skip. Pinned together so a later widening of one guard cannot
    quietly take the other with it -- the two behaviours differ deliberately,
    and a single `except OSError` covering both is exactly how they merged.
    """
    source = DictSource(
        {"src/a.py": PermissionError(13, "denied"), "specs/b.md": b"x\n"}
    )
    report = rs.build_report(source)
    assert report.warnings == ["src/a.py: unreadable (denied); skipped"]
    assert report.per_category[rs.LABEL_SPECS].files == 1


def test_gitrev_skips_a_submodule_entry(monkeypatch: pytest.MonkeyPatch) -> None:
    """`ls-tree` lists a submodule as mode 160000, type `commit`, with a sha
    that is not a blob -- reading it would fail, and its content is the
    submodule's business, not this superproject's.

    Driven through a fabricated `ls-tree` payload rather than a real submodule:
    the fixture cost of a second repository is large and the branch under test
    is one field comparison, so the cheap oracle is the exact bytes git emits.
    """
    payload = (
        b"100644 blob aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\tsrc/a.py\0"
        b"160000 commit bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb\tvendor/dep\0"
        b"100644 blob cccccccccccccccccccccccccccccccccccccccc\tspecs/x.md\0"
    )
    monkeypatch.setattr(rs, "_git_out", lambda *_a: payload)  # pyright: ignore[reportUnknownLambdaType, reportUnknownArgumentType]
    rev = rs.GitRev(
        "c" * 40, rs.BlobReader(), date="2026-01-01T00:00:00+00:00", resolved=True
    )
    paths = [ref.path for ref in rev.files()]
    assert paths == ["specs/x.md", "src/a.py"], "the blob entries must survive"
    assert "vendor/dep" not in paths


class _BlockingReader:
    """A stdout that blocks until released -- a wedged pipe, without a wedged
    process."""

    def __init__(self) -> None:
        self._gate = threading.Event()

    def release(self) -> None:
        self._gate.set()

    def readline(self) -> bytes:
        self._gate.wait(timeout=10)
        return b""

    def read(self, _size: int = -1) -> bytes:
        self._gate.wait(timeout=10)
        return b""


def test_gitrev_measures_a_past_commit_not_the_current_tree(git_repo: Path) -> None:
    with rs.BlobReader() as reader:
        first = rs.build_report(rs.GitRev("HEAD~1", reader))
        head = rs.build_report(rs.GitRev("HEAD", reader))
    # specs/design.md arrived in the second commit and must be absent from the
    # first -- the whole point of reading history.
    assert first.per_category[rs.LABEL_SPECS].files == 0
    assert head.per_category[rs.LABEL_SPECS].files == 1
    assert head.per_category[rs.LABEL_SPECS].physical == 3


def test_gitrev_and_worktree_agree_on_a_clean_tree(git_repo: Path) -> None:
    # Cleanliness is established by construction here rather than assumed of
    # whatever tree the suite happens to run in.
    with rs.BlobReader() as reader:
        at_head = rs.build_report(rs.GitRev("HEAD", reader))
    live = rs.build_report(rs.WorkTree())
    # Every field, not just `physical`. Comparing that one alone left `nbytes`
    # -- the field where the two sources genuinely can differ, since `GitRev`
    # reads the blob git stored and `WorkTree` reads the bytes on disk, which
    # `core.autocrlf` may have rewritten -- untested. The fixture pins
    # `autocrlf=false`, so equality is the right oracle here and a failure means
    # a real divergence rather than a line-ending artefact.
    assert at_head.per_category == live.per_category


def test_blob_reader_returns_a_payload_larger_than_one_pipe_buffer(
    git_repo: Path,
) -> None:
    # A pipe read is free to come back short, so a blob bigger than the buffer
    # is what distinguishes a correct reader from one that truncates.
    #
    # newline="\n" is load-bearing: Path.write_text otherwise translates to CRLF
    # on this platform, and the fixture sets core.autocrlf false, so the blob
    # would hold 240,000 bytes against the 200,000 this test means to write.
    source = git_repo / "src" / "big.py"
    source.write_text("line\n" * 40_000, encoding="utf-8", newline="\n")
    _run_git(git_repo, "add", "-A")
    _run_git(git_repo, "commit", "-m", "big")
    with rs.BlobReader() as reader:
        refs = {r.path: r for r in rs.GitRev("HEAD", reader).files()}
        data = reader.read(refs["src/big.py"].blob or "")
    # Equality on content, not on length: a reader that returned the right
    # number of wrong bytes would pass a length check.
    assert data == source.read_bytes()


def test_unresolvable_revision_names_the_revision(git_repo: Path) -> None:
    with pytest.raises(rs.StatsError) as excinfo:
        rs.resolve_rev("no-such-rev")
    # Names the revision, not a file: git's wording for an absent path and an
    # unresolvable revision overlap, and reporting one as the other sends the
    # reader hunting for a file that was never the problem.
    assert "no-such-rev" in str(excinfo.value)


def test_list_commits_is_chronological_and_first_parent(git_repo: Path) -> None:
    commits = rs.list_commits("main")
    assert len(commits) == 2
    assert commits[0][1] <= commits[1][1]  # oldest first


def test_a_directory_name_is_not_accepted_as_a_revision(git_repo: Path) -> None:
    """`git log <arg>` disambiguates against the filesystem, so an unresolvable
    revision that happens to name a directory becomes a *pathspec*.

    Measured on the live repository before the fix: `history --ref src` exited
    **0** with a 2-point series where the real walk is 205 -- a path-filtered
    answer presented as a series, with nothing in the output saying so. The
    assertion is on the message as well as the refusal, because `git log`'s own
    wording for an absent path and an absent revision overlap, and reporting a
    mistyped revision as something about a file is the confusion `resolve_rev`
    was written to end.
    """
    assert (git_repo / "src").is_dir(), "premise: the name resolves as a path"
    with pytest.raises(rs.StatsError, match="not a revision in this repository: 'src'"):
        rs.list_commits("src")

    # And the series a caller asked for is still the series they get.
    assert len(rs.list_commits("main")) == 2


def test_build_series_produces_one_report_per_commit(git_repo: Path) -> None:
    reports = rs.build_series(rs.list_commits("main"))
    assert len(reports) == 2
    # The series shows the tree growing, which is the thing it exists to show.
    assert [r.per_category[rs.LABEL_SPECS].files for r in reports] == [0, 1]
    assert all(r.commit for r in reports)


def test_build_series_shares_its_blob_cache_across_commits(git_repo: Path) -> None:
    # README.md is unchanged between the two commits, so its blob is classified
    # once for the whole walk -- the 20:1 saving on this repo's real history.
    cache: rs.BlobCache = {}
    rs.build_series(rs.list_commits("main"), cache)
    keys = set(cache)
    blobs = {blob for blob, _lang in keys}
    with rs.BlobReader() as reader:
        head_blobs = {r.blob for r in rs.GitRev("HEAD", reader).files()}
    assert head_blobs <= blobs
    # Three distinct paths across two commits, but README.md's single blob is
    # shared, so fewer cache entries than path-instances.
    assert len(keys) == 3
    # Keyed by (blob, language), not by blob alone: a git blob is
    # content-addressed and path-independent, so one sha can be reached as two
    # languages that classify differently. `test_a_shared_blob_is_classified_
    # once_per_language` holds the behaviour; this pins the key's shape.
    assert all(lang in ("python", "sql", "markdown") for _blob, lang in keys)


def test_three_dot_range_resolves_to_the_merge_base(git_repo: Path) -> None:
    """`base...HEAD` measures work done since the fork point, not since the tip
    of the other branch -- the distinction that matters once the other branch
    has moved on."""
    _run_git(git_repo, "branch", "feature")
    _run_git(git_repo, "checkout", "-q", "feature")
    (git_repo / "src" / "feat.py").write_text("f = 1\n", encoding="utf-8", newline="\n")
    _run_git(git_repo, "add", "-A")
    _run_git(git_repo, "commit", "-m", "feature work")
    # main moves on independently, so `main` and the merge base now differ.
    _run_git(git_repo, "checkout", "-q", "main")
    (git_repo / "src" / "other.py").write_text(
        "o = 1\n", encoding="utf-8", newline="\n"
    )
    _run_git(git_repo, "add", "-A")
    _run_git(git_repo, "commit", "-m", "main work")
    _run_git(git_repo, "checkout", "-q", "feature")

    fork_point, head = rs.parse_rev_range("main...feature", None)
    assert head == "feature"
    with rs.BlobReader() as reader:
        base_report = rs.build_report(rs.GitRev(fork_point, reader))
        head_report = rs.build_report(rs.GitRev("feature", reader))
    deltas = rs.category_deltas(base_report, head_report)
    # Only the feature's own file: main's independent commit is not in the delta.
    assert deltas[rs.LABEL_IMPL].files == 1

    # The two-dot form compares against main's tip instead, so main's file shows
    # up as a deletion. This is the whole reason both spellings exist.
    two_dot_base, _ = rs.parse_rev_range("main..feature", None)
    with rs.BlobReader() as reader:
        against_tip = rs.category_deltas(
            rs.build_report(rs.GitRev(two_dot_base, reader)), head_report
        )
    assert against_tip[rs.LABEL_IMPL].files == 0  # one added, one lost


def test_diff_against_the_working_tree_sees_uncommitted_edits(
    git_repo: Path,
) -> None:
    (git_repo / "src" / "mod.py").write_text(
        "x = 1\ny = 2\nz = 3\n", encoding="utf-8", newline="\n"
    )
    base_rev, head_rev = rs.parse_rev_range("HEAD", None)
    assert head_rev is None  # the working tree
    with rs.BlobReader() as reader:
        base = rs.build_report(rs.GitRev(base_rev, reader))
    head = rs.build_report(rs.WorkTree())
    assert rs.category_deltas(base, head)[rs.LABEL_IMPL].physical == 2


# --- main() ---------------------------------------------------------------


def test_main_prints_and_returns_zero(capsys: pytest.CaptureFixture[str]) -> None:
    # The two spellings that predate the subcommands. This test is the
    # regression test for the default-command shim: if it starts failing, the
    # shim broke, and the fix is the shim -- not this test.
    assert rs.main([]) == 0
    assert "## Repo size so far" in capsys.readouterr().out
    assert rs.main(["--json"]) == 0
    assert "categories" in capsys.readouterr().out


def test_main_snapshot_subcommand_matches_the_bare_form(
    git_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The shim's second spelling: `snapshot` and a bare invocation are one
    command.

    Against the **fixture** repository, not the live tree. It scanned the live
    tree twice and asserted the two outputs byte-identical, which makes the
    assertion depend on nothing changing on disk between them -- hostile under
    `-n auto`, where another worker is writing into the same checkout, and a
    standing candidate cause for this repo's open, unreproduced CLI test flake.
    The fixture is quiescent by construction, so what is left is the property
    the test is actually about.
    """
    assert rs.main(["snapshot"]) == 0
    explicit = capsys.readouterr().out
    assert rs.main([]) == 0
    assert capsys.readouterr().out == explicit
    assert "## Repo size so far" in explicit


def test_main_reports_an_unresolvable_revision_as_exit_2(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # The one class of failure that is not "exit 0 with a warning": nothing
    # could be enumerated, so every number would be a lie.
    assert rs.main(["snapshot", "definitely-not-a-rev"]) == 2
    assert "definitely-not-a-rev" in capsys.readouterr().err


def test_main_history_writes_csv_to_a_file(git_repo: Path, tmp_path: Path) -> None:
    out = tmp_path / "shape.csv"
    assert rs.main(["history", "--ref", "main", "--out", str(out)]) == 0
    rows = list(csv.reader(io.StringIO(out.read_text(encoding="utf-8"))))
    assert rows[0] == rs.HISTORY_COLUMNS
    assert len(rows) == 1 + 2 * len(rs.CATEGORIES)
    # No CRLF: the file must be byte-stable across platforms.
    assert b"\r\n" not in out.read_bytes()


@pytest.mark.parametrize(
    ("argv", "expected_start"),
    [
        (["snapshot", "--format", "md", "--json"], "{"),
        (["snapshot", "--json", "--format", "md"], "## Repo size so far"),
    ],
)
def test_the_legacy_json_flag_obeys_argument_order(
    argv: list[str], expected_start: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """`--json` and `--format` are two spellings of one setting, so the later
    one wins -- what a reader already expects of a command line.

    They were separate flags reconciled in the runner by
    `"json" if args.json else args.format`, which ignored order entirely: the
    hidden, `SUPPRESS`-ed legacy spelling beat the documented one in **both**
    orders, so `--format md --json` emitted JSON with no error and no warning.
    Both orders are pinned, because one of them passes under the defect.
    """
    assert rs.main(argv) == 0
    assert capsys.readouterr().out.startswith(expected_start)


def test_main_reports_an_unwritable_out_path_as_exit_2(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`--out` was outside the stated error contract.

    A mistyped directory raised a six-frame `FileNotFoundError` at exit 1 where
    the docstring promises exit 2 "with a one-line message rather than a
    traceback" -- and on `history` it discards the whole walk after every second
    of it has been spent.
    """
    assert rs.main(["snapshot", "--out", str(tmp_path / "no-such-dir" / "o.md")]) == 2
    err = capsys.readouterr().err
    assert err.startswith("repo_stats: could not write")
    assert len(err.strip().splitlines()) == 1


def test_stdout_is_lf_only_under_a_real_invocation(tmp_path: Path) -> None:
    """The byte-stability invariant, on the branch that actually defaults to it.

    `history`'s default is CSV to stdout, and that path translated every line
    ending: measured on Windows, a redirected run wrote 46 CRLF sequences while
    the identical data through `--out` wrote none. The invariant was enforced
    only where `Path.write_text` could be handed `newline="\\n"`.

    Driven as a subprocess deliberately, and against the live repository rather
    than the fixture, because both are forced: `_use_utf8_io` -- which owns the
    real invocation's stream setup, and is where the fix lives -- is never
    called from `main()`, so an in-process test runs through pytest's capture
    stream and cannot see the translation this pins; and `REPO_ROOT` is derived
    from `__file__`, which no monkeypatch crosses a process boundary to change.
    A degenerate `diff HEAD HEAD` keeps that cheap -- the second report is all
    cache hits -- while still exercising a CSV emitted to stdout.
    """
    out = tmp_path / "piped.csv"
    with out.open("wb") as sink:
        subprocess.run(  # noqa: S603 - fixed interpreter, test-local paths
            [
                sys.executable,
                str(rs.__file__),
                "diff",
                "HEAD",
                "HEAD",
                "--format",
                "csv",
            ],
            check=True,
            stdout=sink,
            stderr=subprocess.PIPE,
            timeout=180,
        )
    body = out.read_bytes()
    assert body.startswith(b"category,metric"), (
        "the subprocess did not emit the CSV, so this proves nothing"
    )
    assert b"\r\n" not in body


def test_main_history_every_week_reaches_sample_commits(
    git_repo: Path, tmp_path: Path
) -> None:
    """`--every` is well unit-tested at `sample_commits` and was untested at the
    CLI.

    That is the same gap `test_the_legacy_json_flag_obeys_argument_order` exists
    to close for `--json`: a pure function can be correct while the flag that
    selects it is wired to nothing. Both fixture commits land in one ISO week,
    so `week` must collapse them to a single point where `commit` keeps two --
    which is `sample_commits`' documented behaviour observed from outside.
    """
    per_commit = tmp_path / "commit.csv"
    per_week = tmp_path / "week.csv"
    assert rs.main(["history", "--ref", "main", "--out", str(per_commit)]) == 0
    assert (
        rs.main(["history", "--ref", "main", "--every", "week", "--out", str(per_week)])
        == 0
    )

    def points(path: Path) -> set[str]:
        rows = list(csv.reader(io.StringIO(path.read_text(encoding="utf-8"))))
        return {r[0] for r in rows[1:]}

    assert len(points(per_commit)) == 2
    assert len(points(per_week)) == 1
    # The point kept is the *last* of the period, not the first -- a period's
    # state is its end state, and taking the first would report a week's work as
    # having happened the week after.
    head = rs.list_commits("main")[-1][0][:7]
    assert points(per_week) == {head}


@pytest.mark.parametrize(
    "argv",
    [
        pytest.param(["history"], id="history-csv-the-default-format"),
        pytest.param(["diff", "HEAD~1"], id="diff-csv"),
    ],
)
def test_the_cli_announces_an_uncounted_file_on_stderr(
    git_repo: Path, capsys: pytest.CaptureFixture[str], argv: list[str]
) -> None:
    """The wiring, not the function — driven through `main`.

    `_emit_diagnostics` was unit-tested by calling it directly, which proves
    the emitter works and nothing about whether either subcommand calls it.
    Measured: replacing **both** call sites with `pass` left all 147 tests
    green. That is the original defect exactly — the machine formats silent
    about an uncovered tree — reachable again through the one seam the unit
    test cannot see.

    CSV both times on purpose: it is `history`'s default format and the one
    with no slot for prose, so stderr is the only channel it has.
    """
    (git_repo / "docs").mkdir()
    (git_repo / "docs" / "guide.md").write_text("a\nb\n", encoding="utf-8")
    _run_git(git_repo, "add", "-A")
    _run_git(git_repo, "commit", "-m", "add an uncovered tree")

    assert rs.main([*argv, "--format", "csv"]) == 0
    captured = capsys.readouterr()
    assert "docs/guide.md" not in captured.out, "CSV has no column for this"
    assert "tracked file(s) in no category" in captured.err
    assert "docs/guide.md" in captured.err


def test_a_file_named_like_the_resolved_sha_does_not_become_a_pathspec(
    git_repo: Path,
) -> None:
    """What the `--` terminator in `list_commits` actually defends against.

    Not a branch sharing a directory's name -- `resolve_rev` asks
    `rev-parse --verify <ref>^{commit}`, which is revision-only, so that case
    resolves cleanly and never reaches `git log` ambiguous. By that point the
    argument is a 40-hex sha, and the only thing left that can collide with it
    is a file *named* that sha: `git log <sha>` then exits 128 as ambiguous
    and `git log <sha> --` exits 0 (both measured).

    Pathological, and pinned anyway, because the alternative is a guard no
    test distinguishes from its neighbour -- which is what a review found
    here: dropping the `"--"` while keeping `resolve_rev(ref)` left every one
    of the 147 tests green.
    """
    sha = rs.resolve_rev("main")  # pyright: ignore[reportPrivateUsage]
    (git_repo / sha).write_text("a decoy named like a revision\n", encoding="utf-8")
    _run_git(git_repo, "add", "-A")
    _run_git(git_repo, "commit", "-m", "a file named like a sha")

    # The premise: git really cannot tell these apart unaided.
    with pytest.raises(rs.StatsError, match="failed"):
        rs._git_out("log", "--format=%H", sha)  # pyright: ignore[reportPrivateUsage]

    # Terminated, it is read as a revision: the walk ends at that commit and
    # carries the history behind it, rather than filtering on a path.
    commits = rs.list_commits(sha)
    assert len(commits) == 2, "the fixture's two commits, oldest first"
    assert commits[-1][0] == sha


def test_main_diff_subcommand_renders_a_delta(
    git_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert rs.main(["diff", "HEAD~1", "HEAD"]) == 0
    out = capsys.readouterr().out
    assert "## Repo shape:" in out
    # specs/design.md arrived in the second commit: +1 file, +3 lines.
    assert rs.LABEL_SPECS in out
    assert "+3" in out


def test_main_diff_reports_a_bad_range_as_exit_2(
    git_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert rs.main(["diff", "main..HEAD", "also-this"]) == 2
    assert "both ends" in capsys.readouterr().err
