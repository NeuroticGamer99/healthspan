#!/usr/bin/env python3
"""Report the repository's size and shape: files, lines, and derived signal.

A permanent home for the ad-hoc "repo size so far" tally. Groups the tree into
the categories that carry meaning for this project -- enumerated once under
"Category membership" below rather than summarised here as well, because the
summary said "seven" while the list held nine -- and, per category, counts
files, physical lines, a code/comment/blank split, and bytes. It then derives
the ratios and the ADR-status breakdown that were previously eyeballed.

The code/comment/blank split -- not raw physical lines -- is the point: a
40-line module docstring and 40 statements are very different, and this repo
leans hard on long docstrings. Classification per language:

  - Python: comment = a whole-line ``#`` comment OR any line inside a *docstring*
    (module/class/function), the latter found by parsing the AST so that an
    assigned multi-line string -- ``x = \"\"\"...\"\"\"`` -- correctly counts as
    code, not comment. A trailing comment on a code line counts as code (the
    line does work). If a file does not parse, it degrades to the ``#``-only
    rule and is noted.
  - SQL: comment = a whole-line ``--`` comment. Block comments (``/* ... */``)
    are not modelled -- the migrations use none; such a line counts as code.
  - Markdown: no comment concept -- every non-blank line is "code" (content),
    comment is always 0. (HTML ``<!-- -->`` comments are not special-cased.)

  In every language a blank line (empty after strip) is counted blank first,
  including blank lines *inside* a Python docstring.

Category membership -- this docstring is where it is defined; ``SKILL.md``
points here rather than restating it. Matching is by repo-relative POSIX path,
**case-insensitively and identically on every platform**, first match wins, in
the order listed. (Case-insensitive by policy rather than by filesystem: git
preserves the casing a path was added with while Windows and macOS do not, so a
case-sensitive test makes one file answer differently per CI leg -- and a file
dropped that way is dropped from the "Uncounted" check too, because the same
comparison decides both.)
  - Python -- implementation: ``src/**/*.py``
  - Python -- tests: ``tests/**/*.py``
  - Python -- scripts: ``scripts/**/*.py`` and ``.github/scripts/**/*.py``
    (the CI half of the same harness)
  - SQL -- migrations: ``src/**/*.sql`` (the migration runner's numbered files)
  - Specs -- general: ``specs/*.md`` (top level only, non-recursive)
  - Specs -- reviews: ``specs/reviews/**/*.md``
  - ADRs: ``specs/adr/*.md`` (every file, incl. the README index and template)
  - Harness -- skills & agents: ``.claude/**/*.md``
  - Docs -- root & tooling config: ``*.md`` at the repo root, plus
    ``.github/**/*.md``, ``.gemini/**/*.md``, ``.greptile/**/*.md``
  ``__pycache__`` is excluded everywhere.

  Non-markdown configuration is deliberately NOT counted -- ``.claude/settings.json``
  no more than ``pyproject.toml`` or ``ci.yml``. The categories are source and
  prose only. This omission is deliberate and stated here so it does not read
  as the next instance of the gap the harness categories above just closed.

  ``specs/personal/`` is deliberately EXCLUDED and never counted: it must never
  exist (ADR-0079 — personal data lives outside the repository), so a
  recreation there is neither counted nor read. The exclusion lives in the
  enumeration layer, so it holds at *every* revision, not just the live tree.
  A footnote records it; no counts, no content. It matches **both recreation
  shapes** — the directory's descendants and a plain file at the bare path,
  which ``.gitignore``'s trailing-slash rule does not cover — case-folded, which
  is the same rule ``check_personal_containment.is_personal_path`` holds and is
  pinned against it. An accepted path would be enumerated, and an enumerated
  path nothing claims is printed *verbatim* in the uncounted footnote.

  Any tracked ``.py``/``.sql``/``.md`` file that no category claims is reported
  under "Uncounted". Coverage is therefore self-reporting: a newly added tree
  announces itself instead of silently sitting outside the table.

  That report reaches **every** output, which took a second pass to be true:
  it began life in the snapshot table, the snapshot JSON and the diff table
  only, so ``history``'s default CSV, its markdown table, its HTML chart and
  the diff CSV all rendered an uncovered tree as nothing at all. A detector
  silent in the default format is the defect it exists to find. Markdown and
  HTML carry it inline; CSV and JSON, having no slot for prose, carry it on
  stderr.

The ADR-status breakdown reads each numbered ``NNNN-*.md`` (excluding the
``0000-template.md``) ``## Status`` field, matching the convention that
``scripts/check_adr_index.py`` already relies on.

Three subcommands, all counting through the same categories above:

  - ``snapshot [<rev>]`` -- one point. With no rev, the working tree (so
    uncommitted edits count); with a rev, that commit, read straight out of the
    object database without checking anything out.
  - ``history`` -- a series over first-parent history, one point per commit by
    default (``--every week|month`` downsamples), emitted as CSV/JSON/markdown
    or a self-contained SVG chart.
  - ``diff <base> [<head>]`` -- the change between two points, category by
    category. The range spellings are git's: ``main HEAD`` and ``main..HEAD``
    compare those two commits, ``main...HEAD`` compares from the merge base
    (work done on this branch), and an omitted head means the working tree.

A bare invocation, and the legacy ``--json`` flag, still mean ``snapshot``.

Enumeration is git's, not the filesystem's: ``git ls-files`` for the working
tree and ``git ls-tree`` for a revision. That is what makes the live table and
every historical point measure the same set by the same rule -- and it takes
``.gitignore`` handling (``.venv/``, caches) for free rather than by a
hand-maintained denylist. The cost is that an *untracked* file is not counted,
and that git is required.

Exit 0 when the tree could be enumerated, and an individual unreadable file in
the **working tree** is reported as a warning and skipped: it is one file, the
operator can see it, and every other number stands.

Not every warning is a skip, and the distinction is reported rather than
flattened. A file that is unreadable or not valid UTF-8 is skipped and is
absent from the counts. A Python file that will not *parse* is kept and
counted in full -- only its docstrings move from ``comment`` to ``code``, so
``code`` comes out **over**stated while files and physical lines are
unaffected. Every diagnostic header once called both "skipped" and claimed the
numbers "understate the tree", printed directly above a line reading "could
not parse as Python; docstrings counted as code".

The working tree's counts are keyed on their **content**, never on the index
id of the path they came from. ``git diff-files`` looks like a proof that disk
and index agree and is not one: it compares converted content and it skips
``assume-unchanged``/``skip-worktree`` entries outright. Both holes were
reachable, so ``WorkTree`` supplies no blob id at all and the cache key is
git's own id for the bytes actually read.

A blob that cannot be read **at a revision** is not that, and exits 2. The
difference is deliberate and was a disputed call, settled here: the working
tree's unreadable file is a local, visible fact, while a missing or corrupt
object is git being unable to tell us what a commit contained -- and skipping
it does not produce a slightly incomplete number, it produces a *wrong* one.
The skipped file's lines are simply absent, so the point reports a smaller
repository than existed, `diff` renders that as deletions, and the chart draws
a cliff. Nothing downstream can tell that apart from real shrinkage. This tool
ranks a silently wrong number above a loud failure, so the walk stops and names
the object, the commit and the path. The triggers are rare and both actionable:
a blobless partial clone run offline, or a corrupt pack.

Exit 2 also covers the enumeration failing outright -- git missing, not a
repository, or a revision that does not resolve -- always with a one-line
message rather than a traceback.

Stdlib only; all files are read as UTF-8 (a leading BOM is tolerated). Physical
lines are split on ``\\n``/``\\r``/``\\r\\n`` only, matching the AST's newline set.
"""

from __future__ import annotations

import argparse
import ast
import contextlib
import csv
import hashlib
import io
import json
import shutil
import subprocess
import sys
import threading
from collections.abc import Callable, Generator
from dataclasses import dataclass, field
from datetime import datetime
from html import escape
from pathlib import Path
from typing import Any, Protocol, cast

REPO_ROOT = Path(__file__).resolve().parent.parent

ADR_DIR = "specs/adr/"
ADR_TEMPLATE = "0000-template.md"

# Bump when a category predicate changes meaning. It is stamped into every
# emitted series so a CSV gathered under an older set of categories is
# recognisable as stale rather than silently comparable with a newer one --
# the whole point of a history is that the lens is constant, and the one thing
# that can move the lens is an edit to this file.
CATEGORIES_VERSION = 2

# Category labels are defined once here and referenced from CATEGORIES (the
# producer), render_markdown (the consumer), and the tests, so a mismatch is a
# NameError, not a silently-empty dict.get that degrades a ratio to n/a.
LABEL_IMPL = "Python — implementation (src/)"
LABEL_TESTS = "Python — tests (tests/)"
LABEL_SCRIPTS = "Python — scripts (scripts/, .github/scripts/)"
LABEL_MIGRATIONS = "SQL — migrations"
LABEL_SPECS = "Specs — general (specs/*.md)"
LABEL_REVIEWS = "Specs — reviews (specs/reviews/)"
LABEL_ADR = "ADRs (specs/adr/)"
LABEL_HARNESS = "Harness — skills & agents (.claude/)"
LABEL_TOOLING = "Docs — root & tooling config"

# Suffixes the report claims to cover. A tracked file with one of these that no
# category matches is reported as uncounted; anything else (config, fixtures,
# images) is out of scope by construction and not reported.
COUNTED_SUFFIXES = (".py", ".sql", ".md")

# Never enumerated, so never read, at any revision. specs/personal/ must not
# exist at all (ADR-0079); keeping the rule here rather than in each caller is
# what makes the revision backend safe by construction.
PERSONAL_DIR = "specs/personal"


def _fold(path: str) -> str:
    """The form every path predicate in this module matches against.

    One folding function rather than a case rule per predicate, because the
    predicates are not independent: the *same* comparison decides both which
    category claims a file and -- through ``COUNTED_SUFFIXES`` -- whether a file
    nothing claimed is reported as uncounted. Spelled per-site and
    case-sensitively, a tracked ``TESTS/HELPER.PY`` falls out of the totals and
    out of the leak check that exists to announce it, so the report claims full
    coverage while missing the file. That is the one failure this module's
    coverage check cannot survive, so the rule has one implementation.

    Case-**insensitive**, identically on every platform, is the policy rather
    than the filesystem's own: git preserves the casing a path was added with
    while the Windows and macOS filesystems this project runs on do not, so the
    same file answers differently per leg under a case-sensitive test -- and a
    constant lens across platforms and revisions is the property the whole
    history rests on. It also matches `check_personal_containment.is_personal_path`,
    which case-folds for this same reason and which `_is_personal_path` below is
    pinned against. Every literal these predicates hold is already lowercase, so
    folding the path alone is sufficient.
    """
    return path.casefold()


def _is_personal_path(path: str) -> bool:
    """Whether a path is the ADR-0079 containment directory or inside it.

    Behaviourally identical to `check_personal_containment.is_personal_path`,
    and `tests/test_repo_stats.py` pins the two together rather than letting a
    third copy of the rule drift -- the trade `check_personal_containment`'s own
    docstring records for its copy of `review_worktree._is_personal`, taken here
    for the same reason: this script is stdlib-only and standalone, and a stats
    report should not stop working because a 1,200-line gate is mid-edit.

    Both halves carry weight. The bare path is matched as well as the prefix
    because ``.gitignore``'s rule has a trailing slash and so matches
    *directories only*, leaving a plain file at exactly ``specs/personal``
    ignored by nothing; and the match case-folds for the reason `_fold` gives.
    """
    folded = _fold(path)
    return folded == PERSONAL_DIR or folded.startswith(PERSONAL_DIR + "/")


def _included(path: str) -> bool:
    """Whether a repo-relative POSIX path is eligible to be enumerated at all."""
    if _is_personal_path(path):
        return False
    return "__pycache__" not in _fold(path).split("/")


def _under(
    prefix: str, suffix: str, *, recursive: bool = True
) -> Callable[[str], bool]:
    """Predicate for ``<prefix>**/<*suffix>``, or one level when not recursive."""

    def match(path: str) -> bool:
        folded = _fold(path)
        if not folded.startswith(prefix) or not folded.endswith(suffix):
            return False
        return recursive or "/" not in folded[len(prefix) :]

    return match


def _any_of(*matchers: Callable[[str], bool]) -> Callable[[str], bool]:
    """A category spread over more than one directory."""

    def match(path: str) -> bool:
        return any(m(path) for m in matchers)

    return match


def _root_or_under(prefixes: tuple[str, ...], suffix: str) -> Callable[[str], bool]:
    """Predicate for ``<*suffix>`` at the repo root, or anywhere under prefixes."""

    def match(path: str) -> bool:
        folded = _fold(path)
        if not folded.endswith(suffix):
            return False
        return "/" not in folded or folded.startswith(prefixes)

    return match


@dataclass(frozen=True)
class Category:
    label: str
    lang: str
    match: Callable[[str], bool]


# Ordered as the report prints them, and matched first-match-wins in that same
# order so there is only one list to reason about. The predicates are in fact
# mutually exclusive -- specs/*.md is non-recursive, so specs/adr/ and
# specs/reviews/ cannot also land in "general" -- which means the ordering never
# actually arbitrates. It is stated as first-match-wins anyway, so that adding a
# tenth category that *does* overlap has a defined answer instead of a surprise.
CATEGORIES: list[Category] = [
    Category(LABEL_IMPL, "python", _under("src/", ".py")),
    Category(LABEL_TESTS, "python", _under("tests/", ".py")),
    Category(
        LABEL_SCRIPTS,
        "python",
        # .github/scripts/ is the CI half of the same harness -- the Gemini
        # review agent lives there rather than in scripts/ only because a
        # workflow invokes it. The uncounted check is what surfaced it.
        _any_of(_under("scripts/", ".py"), _under(".github/scripts/", ".py")),
    ),
    Category(LABEL_MIGRATIONS, "sql", _under("src/", ".sql")),
    Category(LABEL_SPECS, "markdown", _under("specs/", ".md", recursive=False)),
    Category(LABEL_REVIEWS, "markdown", _under("specs/reviews/", ".md")),
    Category(LABEL_ADR, "markdown", _under(ADR_DIR, ".md", recursive=False)),
    Category(LABEL_HARNESS, "markdown", _under(".claude/", ".md")),
    Category(
        LABEL_TOOLING,
        "markdown",
        _root_or_under((".github/", ".gemini/", ".greptile/"), ".md"),
    ),
]


class StatsError(Exception):
    """The tree could not be enumerated at all -- exit 2, not a traceback."""


# --- git plumbing ---------------------------------------------------------

_GIT = shutil.which("git") or "git"
# A hung git should fail the report, not hang it. The shape (timeout guard plus
# the OSError arm for an absent git, which `shutil.which` falling back to the
# bare name leaves possible) is `scripts/diff_harness.py:_git`'s, copied rather
# than re-derived.
_GIT_TIMEOUT = 30


def _git_out(*args: str) -> bytes:
    """Run git and return stdout, turning any failure into a StatsError."""
    try:
        proc = subprocess.run(  # noqa: S603
            [_GIT, *args],
            capture_output=True,
            check=False,
            cwd=REPO_ROOT,
            timeout=_GIT_TIMEOUT,
        )
    except OSError as exc:  # git missing, or not executable
        raise StatsError(f"could not run `git {' '.join(args)}`: {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise StatsError(
            f"git {' '.join(args)} did not finish within {_GIT_TIMEOUT}s"
        ) from exc
    if proc.returncode != 0:
        detail = proc.stderr.decode("utf-8", "replace").strip().splitlines()
        raise StatsError(
            f"git {' '.join(args)} failed: {detail[0] if detail else 'no output'}"
        )
    return proc.stdout


def _printable(path: str) -> str:
    """A path safe to put in a report, whatever bytes git gave for it.

    `_split_z` decodes with ``surrogateescape`` so a path whose bytes are not
    UTF-8 survives round-tripping back to the filesystem, which is what lets
    such a file be *read* at all. The cost is lone surrogates in the string, and
    those are not writable: ``Path.write_text(..., encoding="utf-8")`` uses the
    strict handler and raises ``UnicodeEncodeError: surrogates not allowed``. So
    a diagnostic naming such a file failed **only on the ``--out`` path** -- the
    scripted and CI one -- because stdout survives on `_use_utf8_io`'s
    ``errors="replace"``.

    Escaped here, at the boundary where a path stops being an address and
    becomes text, rather than by loosening the writer: a report is allowed to
    name a file it could not decode, and ``\\xff`` in the footnote is the
    honest rendering of a byte that is not a character.
    """
    return path.encode("utf-8", "surrogateescape").decode("utf-8", "backslashreplace")


def _split_z(data: bytes) -> list[str]:
    """Split git's NUL-separated output. ``-z`` emits raw bytes -- no quoting,
    no escaping -- so a path with a space, a quote or a newline survives intact,
    which is why every enumeration here asks for it."""
    return [
        chunk.decode("utf-8", "surrogateescape") for chunk in data.split(b"\0") if chunk
    ]


@dataclass(frozen=True)
class FileRef:
    """One enumerated file. ``blob`` is git's object id when the source knows it
    (a revision does; the working tree does not), and is what lets the series
    classify each distinct content once instead of once per commit it appears in."""

    path: str
    blob: str | None = None


class TreeSource(Protocol):
    label: str
    commit: str | None
    date: str | None

    def files(self) -> list[FileRef]: ...

    def read(self, ref: FileRef) -> bytes: ...


class WorkTree:
    """The checked-out tree: git decides membership, the filesystem supplies bytes.

    Enumerating with ``ls-files`` rather than ``rglob`` is what makes this
    source and GitRev measure the same set by the same rule, so the last point
    of a series lines up with the live table. It also inherits .gitignore, which
    is what keeps ``.venv/`` out of the root-level categories without a
    hand-maintained denylist that would rot the first time a tool adds a cache
    directory. The cost, stated in the module docstring: an untracked file is
    not counted.
    """

    # Annotated, not merely assigned: bare `commit = None` infers the type
    # `None`, which is invariant against the protocol's `str | None` and makes
    # this class fail to satisfy TreeSource at every call site.
    label: str = "working tree"
    commit: str | None = None
    date: str | None = None

    def files(self) -> list[FileRef]:
        # `blob` stays None on this side, always. An index id names what is in
        # the *index* while `read()` below returns what is on *disk*, and no
        # cheap test proves those are the same bytes. `diff-files` looks like
        # that test and is not: it compares *converted* content and it skips
        # entries flagged `assume-unchanged` or `skip-worktree` outright.
        # Measured, both holes are reachable here -- five lines added to an
        # assume-unchanged file gave `snapshot` 8 physical lines and
        # `diff HEAD` +0 for the same tree in the same second, and a file
        # written CRLF then staged under this repo's own `* text=auto eol=lf`
        # sat 21 bytes on disk against an 18-byte index blob with `diff-files`
        # silent. An id that can lie is worse than no id: it is consulted
        # *before* the read, so the wrong answer is the one that costs nothing
        # to produce.
        #
        # The optimization that id was added for is kept, and moved to where it
        # is sound: `build_report` derives the cache key from the bytes it just
        # read (`_content_id`), which is git's own blob id for that content, so
        # the cache `_run_diff` shares between its two ends still answers a
        # working-tree file with the revision side's entry whenever the content
        # really is identical. What is no longer skipped is the *read*; what is
        # still skipped is the AST parse, which is what the 297-ref measurement
        # was actually about.
        #
        # Deduplicated, and that is not defensive: `ls-files` emits one line per
        # *index stage*, so during an unresolved merge a conflicted path arrives
        # three times (stages 1/2/3, measured) and an undeduplicated list counts
        # a one-line file as three files and three lines -- exit 0, no warning,
        # every total silently inflated. Mid-merge is routine in the workflow
        # this harness drives. Deduplicated here rather than with git's own
        # `--deduplicate` so the rule does not depend on a git version, which is
        # also what `check_personal_containment._distinct` decided, for this
        # same reason on this same command. A set is the whole mechanism: the
        # three stages of a conflicted path differ only in content, which this
        # side no longer reads an id from, so they collapse to the one path
        # `read()` will open.
        paths = {
            path
            for path in _split_z(_git_out("ls-files", "-z"))
            if path and _included(path)
        }
        return [FileRef(p, None) for p in sorted(paths)]

    def read(self, ref: FileRef) -> bytes:
        return (REPO_ROOT / ref.path).read_bytes()


@contextlib.contextmanager
def _as_stats_error(sha: str) -> Generator[None]:
    """Turn any pipe `OSError` into a `StatsError`, for the reading half.

    The companion to the write-side conversion in `BlobReader.read`, and needed
    for the same reason: `readline`, `_read_exact` and the trailing-LF `read`
    all touch a pipe that may belong to a dead child, and an `OSError` escaping
    from here reaches `build_report`'s warn-and-skip guard -- which exists for
    an unreadable working-tree file and cannot tell one from a dead subprocess.
    Left unconverted, that guard turns a dead reader into a silently empty
    repository at exit 0.

    A context manager rather than a `try` around each call, because the three
    reads have to convert *identically*: the failure this closes came from two
    sibling pipe operations being guarded differently.
    """
    try:
        yield
    except (OSError, ValueError) as exc:
        raise StatsError(f"git cat-file died while reading {sha}: {exc}") from exc


class BlobReader:
    """One long-lived ``git cat-file --batch`` for every blob in a run.

    Process creation on this platform is measured at ~149 ms, and a full walk of
    this repository reads about 30,000 path-instances; a spawn per blob would be
    on the order of an hour. Measured with one batch process instead: **~36 s**
    for all 212 first-parent commits.

    What that total is made of has been wrong here once, in the direction that
    matters. This paragraph read "dominated by the ``ls-tree`` spawns plus
    classification" -- naming the cost as unavoidable -- while the measured split
    was rev-parse 39% / ls-tree 42% / classification 9%, and the 39% was a
    redundant re-resolution of shas ``git log`` had already returned in full
    (see `GitRev.__init__`'s ``resolved`` flag, which removed it). Certifying a
    removable term as inherent is worse than quoting a stale total, because it
    tells the next reader not to look. The remaining shape is one ``ls-tree``
    spawn per commit plus classification; the per-blob-spawn claim above is the
    only one this docstring needs, and it is an order-of-magnitude argument that
    no re-measurement will overturn. Re-measure the rest rather than quoting it.
    The batch protocol is: write ``<sha>\\n``, read a header line
    ``<sha> <type> <size>\\n``, then exactly ``size`` bytes, then a single LF.
    A sha git does not have answers ``<sha> missing\\n`` and no payload.
    """

    def __init__(self) -> None:
        self._proc: subprocess.Popen[bytes] | None = None

    def _ensure(self) -> subprocess.Popen[bytes]:
        if self._proc is None:
            try:
                self._proc = subprocess.Popen(  # noqa: S603
                    [_GIT, "cat-file", "--batch"],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    cwd=REPO_ROOT,
                )
            except OSError as exc:
                raise StatsError(f"could not start `git cat-file`: {exc}") from exc
        return self._proc

    def read(self, sha: str) -> bytes:
        proc = self._ensure()
        if proc.stdin is None or proc.stdout is None:  # pragma: no cover
            # Unreachable given the PIPE arguments above, but a StatsError keeps
            # the promise that failures here exit 2 with a sentence rather than
            # surfacing as an AttributeError on None.
            raise StatsError("git cat-file was started without its pipes")
        try:
            proc.stdin.write(sha.encode("ascii") + b"\n")
            proc.stdin.flush()
        except (OSError, ValueError) as exc:
            # `OSError`, not `(BrokenPipeError, ValueError)`. A dead child does
            # not reliably raise BrokenPipeError: on Windows, writing to a killed
            # `cat-file` raises a bare `OSError: [Errno 22] Invalid argument`
            # (measured), which that narrower clause let through **as an
            # OSError** -- straight into `build_report`'s warn-and-skip guard,
            # which is for a genuinely unreadable *working-tree file* and has no
            # business seeing a dead subprocess.
            #
            # The consequence was the worst outcome available. `_ensure` never
            # restarts a dead child, so once it died every remaining blob in
            # every remaining commit was "unreadable, skipped": measured on a
            # 3-point walk, points 2 and 3 both reported physical=0 with 275
            # warnings each and the run exited **0**, reporting a repository
            # that had collapsed to nothing. Converting here is what keeps the
            # per-file guard and the per-run failure separate, so a dead reader
            # fails the run loudly instead of fabricating a shrinking history.
            raise StatsError(f"git cat-file died while reading {sha}: {exc}") from exc
        # Bounded, like every other git call here. `subprocess.run`'s `timeout=`
        # cannot reach a long-lived Popen, and a blocking pipe read has no
        # portable timeout of its own -- `select` does not work on pipes on
        # Windows -- so the bound is a watchdog that kills the child. Both reads
        # below were unbounded, against this module's own comment that "a hung
        # git should fail the report, not hang it": `_GIT_TIMEOUT` reached
        # `_git_out` and `close()`, and `close()` is on the shutdown path, never
        # reached while a read is blocked. Killing the child closes the pipe,
        # which turns the block into the empty-read arms already written below.
        with self._watchdog(proc, sha), _as_stats_error(sha):
            header = proc.stdout.readline()
            if not header:
                raise StatsError(f"git cat-file closed its output while reading {sha}")
            parts = header.decode("utf-8", "replace").split()
            # Two different faults, two messages. They were one condition and
            # one sentence, and the `missing` half could never fire on its own:
            # git answers exactly `<sha> missing` -- two fields (measured) -- so
            # `len(parts) < 3` always caught it first, which also meant no
            # mutation of the `missing` test could distinguish the branches. A
            # missing object is git answering correctly about an object it does
            # not have (a blobless partial clone offline, a corrupt pack); a
            # header that is neither is git answering something this protocol
            # reader does not understand, which is a different problem with a
            # different fix.
            if len(parts) >= 2 and parts[1] == "missing":
                raise StatsError(f"git has no blob {sha}")
            if len(parts) < 3:
                raise StatsError(
                    f"git cat-file gave an unreadable header for {sha}: "
                    f"{header.decode('utf-8', 'replace').strip()!r}"
                )
            size = int(parts[2])
            data = _read_exact(proc.stdout, size)
            proc.stdout.read(1)  # the LF git writes after every payload
        return data

    @contextlib.contextmanager
    def _watchdog(self, proc: subprocess.Popen[bytes], sha: str) -> Generator[None]:
        """Kill `proc` if the body has not finished within `_GIT_TIMEOUT`.

        The timer is cancelled on every exit path, so an ordinary read pays one
        `Timer` object and no wall-clock. `fired` is read after cancelling so a
        kill that already happened is reported as the timeout it was rather than
        as the truncated read it looks like from inside the pipe.
        """
        fired = threading.Event()

        def expire() -> None:
            fired.set()
            with contextlib.suppress(OSError):
                proc.kill()

        timer = threading.Timer(_GIT_TIMEOUT, expire)
        timer.start()
        try:
            yield
        except StatsError:
            if fired.is_set():
                raise StatsError(
                    f"git cat-file did not answer for {sha} within {_GIT_TIMEOUT}s"
                ) from None
            raise
        finally:
            timer.cancel()

    def close(self) -> None:
        proc, self._proc = self._proc, None
        if proc is None:
            return
        # Both pipes, not just stdin. An unclosed stdout leaks the file
        # descriptor for the life of the process, which for a long-lived
        # `--batch` child is the whole run.
        for pipe in (proc.stdin, proc.stdout):
            if pipe is not None:
                with contextlib.suppress(OSError):  # already gone; nothing to do
                    pipe.close()
        try:
            proc.wait(timeout=_GIT_TIMEOUT)
        except subprocess.TimeoutExpired:
            proc.kill()
            # `kill` only sends the signal; without the second wait the child is
            # left a zombie and `Popen.__del__` complains about it later, at a
            # point with no connection to what actually went wrong.
            with contextlib.suppress(subprocess.TimeoutExpired):
                proc.wait(timeout=_GIT_TIMEOUT)

    def __enter__(self) -> BlobReader:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def _read_exact(stream: Any, size: int) -> bytes:
    """Read exactly ``size`` bytes. A pipe is free to return a short read, so a
    bare ``read(size)`` truncates large blobs intermittently -- which would show
    up as a file that mysteriously shrinks in some runs and not others."""
    chunks: list[bytes] = []
    remaining = size
    while remaining > 0:
        chunk = stream.read(remaining)
        if not chunk:
            raise StatsError(f"git cat-file gave {size - remaining} of {size} bytes")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


class GitRev:
    """One commit, read out of the object database with nothing checked out.

    ``ls-tree -r -z`` yields ``<mode> <type> <sha>\\t<path>`` per entry, so the
    blob ids arrive with the paths and no content has to be read to learn them.
    """

    label: str
    commit: str | None
    date: str | None

    def __init__(
        self,
        rev: str,
        reader: BlobReader,
        *,
        date: str | None = None,
        resolved: bool = False,
    ) -> None:
        # `commit` is Optional to satisfy TreeSource, which the working-tree
        # source also implements; `_sha` is the same value known to be a str, so
        # git calls below need no narrowing at each site.
        #
        # `resolved=True` says the caller already holds a full commit sha and is
        # not guessing -- the same short-circuit `date` has carried since this
        # class was written, and for the same reason. `build_series` gets its
        # shas from `git log --format=%H`, so re-resolving each one spawned a
        # `rev-parse` per commit to re-answer a question already answered:
        # measured on a 35-commit walk, 16.97 s as shipped against 10.37 s with
        # the re-resolve skipped, output byte-identical, and `GIT_TRACE`
        # confirming rev-parse spawns matching ls-tree spawns 1:1. It is a flag
        # rather than a "looks like 40 hex digits" sniff, because a branch may
        # legally be named that and only the caller knows which it holds.
        self._sha = rev if resolved else resolve_rev(rev)
        self.commit = self._sha
        self.label = self._sha[:7]
        self._reader = reader
        self.date = date if date is not None else _commit_date(self._sha)

    def files(self) -> list[FileRef]:
        refs: list[FileRef] = []
        for entry in _split_z(_git_out("ls-tree", "-r", "-z", self._sha)):
            meta, _, path = entry.partition("\t")
            if not path or not _included(path):
                continue
            fields = meta.split()
            if len(fields) < 3 or fields[1] != "blob":
                continue  # a submodule (commit) or anything else with no content
            refs.append(FileRef(path, fields[2]))
        refs.sort(key=lambda r: r.path)
        return refs

    def read(self, ref: FileRef) -> bytes:
        if ref.blob is None:  # pragma: no cover - GitRev always sets it
            raise StatsError(f"no blob id for {ref.path}")
        try:
            return self._reader.read(ref.blob)
        except StatsError as exc:
            # The reader knows the object; only this layer knows which commit
            # and path were being measured when it failed. Without them the
            # message names a sha the operator cannot place, on a walk that may
            # have been running for half a minute -- and the whole reason this
            # fails the run rather than skipping the file is so the operator can
            # act on it.
            raise StatsError(
                f"{exc} (at {self.label}:{ref.path} — a blobless partial clone "
                "or a corrupt pack; the series would otherwise report this "
                "commit as an empty repository)"
            ) from exc


def resolve_rev(rev: str) -> str:
    """Full sha for a revision, or a StatsError naming the revision.

    The revision and the path are asked about separately on purpose (the lesson
    `diff_harness._git_show` records): git answers an unresolvable revision and
    an absent path with overlapping wording, so deciding by exit code here is
    what keeps a mistyped ``snapshot HEAD~999`` from being reported as something
    about a file.
    """
    try:
        out = _git_out("rev-parse", "--verify", "--quiet", f"{rev}^{{commit}}")
    except StatsError as exc:
        # git's own words are kept when it had any. Discarding them reported a
        # `.git`-less copy of the tree as `not a revision: 'HEAD'` -- true, and
        # useless: the revision is fine and the repository is missing. `--quiet`
        # silences the ordinary unknown-revision message and leaves the fatal
        # ones, so this appends detail exactly where there is some, and the
        # common mistyped-rev case keeps its short sentence.
        detail = str(exc)
        suffix = "" if detail.endswith("no output") else f" ({detail})"
        raise StatsError(f"not a revision in this repository: {rev!r}{suffix}") from exc
    sha = out.decode("ascii", "replace").strip()
    if not sha:
        raise StatsError(f"not a revision in this repository: {rev!r}")
    return sha


def _commit_date(sha: str) -> str:
    return _git_out("log", "-1", "--format=%cI", sha).decode("utf-8", "replace").strip()


@dataclass
class Counts:
    files: int = 0
    physical: int = 0
    code: int = 0
    comment: int = 0
    blank: int = 0
    nbytes: int = 0

    def add(self, other: FileCount) -> None:
        self.files += 1
        self.physical += other.physical
        self.code += other.code
        self.comment += other.comment
        self.blank += other.blank
        self.nbytes += other.nbytes


@dataclass
class FileCount:
    physical: int
    code: int
    comment: int
    blank: int
    nbytes: int


def python_docstring_lines(text: str) -> tuple[set[int], bool]:
    """Line numbers (1-based) covered by any docstring, and whether it parsed.

    A docstring is the string literal that is the first statement of a module,
    class, or function -- exactly what ``ast.get_docstring`` recognizes, so an
    assigned or otherwise-positioned string is not mistaken for one. On a syntax
    error the caller falls back to the ``#``-only rule; the flag says so.
    """
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return set(), False
    lines: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(
            node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        ):
            continue
        if ast.get_docstring(node, clean=False) is None:
            continue
        stmt = node.body[0]  # get_docstring guarantees an Expr holding a str Constant
        if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant):
            doc = stmt.value
            if doc.end_lineno is not None:
                lines.update(range(doc.lineno, doc.end_lineno + 1))
    return lines, True


def _physical_lines(text: str) -> list[str]:
    """Split into physical lines on ``\\n``/``\\r``/``\\r\\n`` only -- the newlines
    Python's parser recognizes -- so the enumerate index stays aligned with the
    AST line numbers from ``python_docstring_lines``. ``str.splitlines`` also
    breaks on form feed, NEL, and other Unicode separators the parser ignores;
    using it here would desync the two sides and miscount post-separator
    docstring lines as code."""
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    parts = normalized.split("\n")
    if parts and parts[-1] == "":
        parts.pop()  # a trailing newline yields no extra line (splitlines parity)
    return parts


def _unparseable_warning(display: str) -> str:
    """The one spelling of this message, so `classify` and the cache-hit path in
    `build_report` cannot drift into two near-identical sentences."""
    return (
        f"{_printable(display)}: could not parse as Python; docstrings counted as code"
    )


def classify(
    data: bytes, lang: str, *, display: str = "<bytes>"
) -> tuple[FileCount, str | None]:
    """Count one file's bytes. Returns (counts, warning-or-None).

    Takes bytes rather than a path so that the counter is pure: the same
    function counts a file on disk and a blob streamed out of the object
    database, and nothing downstream of here knows which it was. ``display``
    only ever appears inside a warning message.
    """
    # utf-8-sig tolerates a leading BOM (harmless when absent): a BOM would
    # otherwise make ast.parse raise -> a spurious unparseable warning, and stay
    # glued to the first line (str.strip does not drop U+FEFF), miscounting it.
    text = data.decode("utf-8-sig")
    lines = _physical_lines(text)
    warning: str | None = None

    doc_lines: set[int] = set()
    if lang == "python":
        doc_lines, ok = python_docstring_lines(text)
        if not ok:
            warning = _unparseable_warning(display)

    code = comment = blank = 0
    for i, raw in enumerate(lines, start=1):
        s = raw.strip()
        if s == "":
            blank += 1
        elif _is_comment(s, lang, in_docstring=i in doc_lines):
            comment += 1
        else:
            code += 1
    return FileCount(len(lines), code, comment, blank, len(data)), warning


def _is_comment(stripped: str, lang: str, *, in_docstring: bool) -> bool:
    """Whether a non-blank line is a comment: inside a docstring, or a whole-line
    ``#`` (Python) / ``--`` (SQL) comment. A trailing comment on code is not one
    (the line does work); Markdown has no comment concept."""
    if in_docstring:
        return True
    if lang == "python":
        return stripped.startswith("#")
    if lang == "sql":
        return stripped.startswith("--")
    return False


def adr_status_breakdown(
    source: TreeSource,
    refs: list[FileRef],
    status_cache: StatusCache | None = None,
) -> dict[str, int]:
    """Bucket every numbered ADR by the first word of its ``## Status`` field.

    Takes the ADR category's already-enumerated refs rather than globbing the
    filesystem, so the breakdown is available at any revision -- the ADR mix
    over time being one of the more interesting things a history can show.

    Cached by blob sha across a whole series, for the same reason the counts
    are: an ADR's text changes far less often than the history has commits, so
    re-reading every ADR at every point made this the large majority of a walk's
    blob reads for an answer that had not changed. The cache is separate from
    `BlobCache` rather than folded into it because the value is a different
    thing -- a status word, not a count -- and one dict holding two value shapes
    keyed the same way is how the language half of `BlobCache`'s key came to be
    missing in the first place.

    Keyed on the ref's blob, so it is live for `GitRev` and inert for
    `WorkTree`, which carries no id -- deliberately, and for the reason
    `WorkTree.files` gives at length: an index id is consulted before the read
    and does not have to be what is on disk. This was the second site of that
    one defect. Nothing is lost by the exemption, because the working tree is
    one point rather than a series, and a status the cache would have saved a
    read for is a status this function has to read the file to learn anyway.
    """
    if status_cache is None:
        status_cache = {}
    buckets: dict[str, int] = {}
    for ref in refs:
        name = ref.path.rsplit("/", 1)[-1]
        if name == ADR_TEMPLATE or not name[:4].isdigit():
            continue
        cached = status_cache.get(ref.blob) if ref.blob is not None else None
        if cached is not None:
            status = cached
        else:
            try:
                status = _adr_status(source.read(ref)) or "Unknown"
            except (UnicodeDecodeError, OSError):  # fmt: skip
                # A non-UTF-8 or otherwise-unreadable ADR is skipped here
                # (dropping its status bucket) rather than crashing the run --
                # the docstring promises exit 0 once the tree enumerated. The
                # same file is also read by classify() for the ADRs category
                # count, which emits a warning, so the file is still reported;
                # no need to thread warnings through this function.
                continue
            if ref.blob is not None:
                status_cache[ref.blob] = status
        head = status.split()[0] if status.split() else "Unknown"
        buckets[head] = buckets.get(head, 0) + 1
    return buckets


def _adr_status(data: bytes) -> str | None:
    # utf-8-sig to match classify()'s BOM handling; raises UnicodeDecodeError on
    # a genuinely non-UTF-8 file, which adr_status_breakdown catches.
    lines = data.decode("utf-8-sig").splitlines()
    for i, line in enumerate(lines):
        if line.strip() == "## Status":
            for candidate in lines[i + 1 :]:
                if candidate.strip():
                    return candidate.strip()
            return None
    return None


@dataclass
class Report:
    per_category: dict[str, Counts]
    warnings: list[str]
    adr_status: dict[str, int]
    uncounted: list[str] = field(default_factory=list[str])
    source_label: str = "working tree"
    commit: str | None = None
    date: str | None = None


# (blob sha, language) -> (counts, whether it failed to parse as Python). Shared
# across every commit of a series: measured on this repo, all 212 first-parent
# commits hold 30,078 path-instances but only 1,410 distinct (blob, language)
# keys, so classifying by content instead of by (commit, path) is a ~21x
# reduction in AST parses.
#
# The language is half the key, not decoration. A git blob is content-addressed
# and path-independent, so byte-identical `scripts/a.py` and `specs/b.md` share
# one sha -- while `classify` counts them differently, because a `#` line is a
# comment in one language and content in the other. Keyed on the sha alone the
# first category to be reached wins, the other silently inherits its split, and
# a false "could not parse as Python" warning is attached to the markdown file
# (reproduced). One shared snippet or one extension-changing rename is all it
# takes, and `build_series` shares a single cache across the whole walk, so one
# collision would poison every point of the history.
BlobCache = dict[tuple[str, str], tuple["FileCount", bool]]

# blob sha -> the ADR's `## Status` line. Same sharing, same reason; see
# `adr_status_breakdown` for why it is a second dict rather than a second
# value shape in the one above.
StatusCache = dict[str, str]


def categorize(refs: list[FileRef]) -> tuple[dict[str, list[FileRef]], list[str]]:
    """Split enumerated files into category buckets, plus what nothing claimed.

    The second return value is the leak list: a tracked file with a counted
    suffix that no category matches. It is the mechanism that keeps this
    report's coverage honest -- the gap it was written to close (an entire
    ``.claude/`` tree outside every category, from this script's first commit
    until the categories were widened) was invisible precisely because nothing
    enumerated what was *not* being counted. The duration is stated by its
    endpoints rather than as a number of months: the number was wrong when
    written (it measures 55 days, not four months) and a hand-written interval
    in prose is exactly the class of claim this repository keeps finding rotted.
    """
    buckets: dict[str, list[FileRef]] = {c.label: [] for c in CATEGORIES}
    uncounted: list[str] = []
    for ref in refs:
        for cat in CATEGORIES:
            if cat.match(ref.path):
                buckets[cat.label].append(ref)
                break
        else:
            # Folded for the same reason the category predicates are, and it is
            # this site that makes the rule load-bearing rather than tidy: a
            # case-sensitive test here would let the very file the categories
            # just dropped slip past the check that exists to announce it.
            if _fold(ref.path).endswith(COUNTED_SUFFIXES):
                uncounted.append(_printable(ref.path))
    return buckets, uncounted


def _content_id(data: bytes) -> str:
    """Git's blob id for these exact bytes.

    The cache key for anything read off disk. Spelled as git spells it -- sha1
    over ``blob <len>\\0`` and the content -- so a working-tree file and the
    revision holding the same content land on one key and `_run_diff`'s shared
    cache answers across its two ends.

    Derived from the bytes that were actually classified, which is the whole
    point: `WorkTree` deliberately carries no index id, because an index id is
    consulted *before* the read and git will call a file unmodified whose disk
    bytes differ (eol conversion, `assume-unchanged`, `skip-worktree`).

    Not a security decision -- git's object names are sha1 and this has to
    match them, which is what `usedforsecurity=False` states. That argument is
    also what satisfies the linter here: a `noqa: S324` beside it is reported
    as unused, so do not add one back.
    """
    return hashlib.sha1(
        b"blob %d\0%b" % (len(data), data), usedforsecurity=False
    ).hexdigest()


def build_report(
    source: TreeSource,
    cache: BlobCache | None = None,
    status_cache: StatusCache | None = None,
) -> Report:
    buckets, uncounted = categorize(source.files())
    report = Report(
        per_category={},
        warnings=[],
        adr_status={},
        uncounted=uncounted,
        source_label=source.label,
        commit=source.commit,
        date=source.date,
    )
    if cache is None:
        cache = {}

    def read_or_warn(ref: FileRef) -> bytes | None:
        try:
            return source.read(ref)
        except OSError as exc:
            # PermissionError, a file removed mid-scan, a directory that
            # matched as a file: skip with a warning rather than crash -- the
            # docstring promises exit 0 once the tree enumerated. A closure so
            # the two call sites below cannot word this warning differently.
            report.warnings.append(
                f"{_printable(ref.path)}: unreadable ({exc.strerror or exc}); skipped"
            )
            return None

    for cat in CATEGORIES:
        counts = Counts()
        for ref in buckets[cat.label]:
            # A pre-read key only where the source can vouch for it: `GitRev`
            # names a blob it is about to stream out of the object database, so
            # the id *is* the content. `WorkTree` supplies none, so its key is
            # taken from the bytes that were actually read -- the two meet in
            # the same cache, because `_content_id` is git's own blob id.
            data: bytes | None = None
            if ref.blob is not None:
                key = (ref.blob, cat.lang)
            else:
                data = read_or_warn(ref)
                if data is None:
                    continue
                key = (_content_id(data), cat.lang)
            hit = cache.get(key)
            if hit is not None:
                fc, unparseable = hit
            else:
                if data is None:
                    data = read_or_warn(ref)
                    if data is None:
                        continue
                try:
                    fc, warn = classify(data, cat.lang, display=ref.path)
                except UnicodeDecodeError:
                    report.warnings.append(
                        f"{_printable(ref.path)}: not valid UTF-8 (skipped)"
                    )
                    continue
                unparseable = warn is not None
                cache[key] = (fc, unparseable)
            counts.add(fc)
            if unparseable:
                # Re-formatted here rather than carried in the cache, because the
                # same blob can live at two paths and the message names one.
                report.warnings.append(_unparseable_warning(ref.path))
        report.per_category[cat.label] = counts
    report.adr_status = adr_status_breakdown(source, buckets[LABEL_ADR], status_cache)
    return report


def warning_lines(reports: list[Report]) -> list[str]:
    """Every warning across a set of reports, each tagged with the point it came
    from.

    The snapshot table printed its warnings and nothing else did: `diff` and all
    four `history` formats dropped them, so a report that had *skipped* a file
    -- a blob that stopped being valid UTF-8, an unreadable path -- presented its
    numbers with no marker, and `diff` went further and rendered the skipped
    file's absence as **negative growth**. That is a known-degraded measurement
    shown as a confident one, which is the failure this tool's whole ranking bar
    is aimed at.

    The source label is part of each line rather than a heading above a group,
    because the two ends of a diff can warn about the same path for different
    reasons and a reader needs to know which end is degraded.
    """
    return [f"{r.source_label}: {w}" for r in reports for w in r.warnings]


#: How a degraded measurement is described, in one place.
#:
#: Every earlier spelling said the files were "skipped" and the numbers
#: "understate" the tree. Both halves are wrong for the commonest warning
#: there is. An unparseable Python file is *retained* and fully counted --
#: `classify` returns real counts beside its warning -- with only its
#: docstrings moved from `comment` to `code`. Measured on one file: files
#: 1 -> 1, physical 5 -> 6, code 1 -> **6**, comment 4 -> **0**. So the run
#: printed "some files were skipped, so the numbers below understate the
#: tree" directly above a line reading "could not parse as Python;
#: docstrings counted as code" -- contradicting itself in two adjacent
#: lines, and overstating `code` while claiming an undercount.
#:
#: "Understate" was not safe even for a genuine skip. A file skipped at a
#: diff's *base* makes the base smaller and the delta **larger**: growth
#: overstated, not understated.
#:
#: Neutral, therefore, and the per-file lines below say which happened.
_DEGRADED = (
    "measured in a degraded way — a skipped file is absent from the numbers "
    "entirely; a file that would not parse is present, with its docstrings "
    "counted as code"
)


def uncounted_lines(reports: list[Report]) -> list[str]:
    """Every uncounted path across a set of reports, tagged with its point.

    The companion to `warning_lines`, and it exists because the leak check was
    reporting to nobody in three of its formats. `Report.uncounted` reached the
    snapshot table, the snapshot JSON and the diff table -- and not `history`'s
    default CSV, its markdown table, its HTML chart, or the diff CSV, with
    `_emit_warnings` reading only `Report.warnings` so stderr stayed silent
    too. Measured with a planted `docs/guide.md`: 0 mentions in any of the
    four, nothing on stderr, exit 0.

    That is the failure this whole change set exists to end, reappearing inside
    its own detector: an uncovered tree is exactly what nobody notices, and the
    default output format was where it went unannounced.
    """
    return [f"{r.source_label}: {p}" for r in reports for p in r.uncounted]


def _emit_diagnostics(reports: list[Report]) -> None:
    """Put the degraded-measurement and coverage markers somewhere a machine
    format has no slot for them.

    CSV has fixed columns and adding a diagnostics column to a tidy-long table
    would put prose in a numeric pivot, so the markers go to stderr -- which
    also keeps them out of a redirected file, the reason the walk-progress
    notice already goes there. Called for every format, not only the ones with
    no inline section: this is worth saying twice more than it is worth
    missing, and when `--out` is in play the terminal is the *only* place the
    operator is looking.
    """
    warnings = warning_lines(reports)
    if warnings:
        print(f"repo_stats: {len(warnings)} file(s) {_DEGRADED}:", file=sys.stderr)
        for line in warnings:
            print(f"  {line}", file=sys.stderr)
    uncounted = uncounted_lines(reports)
    if uncounted:
        print(
            f"repo_stats: {len(uncounted)} tracked file(s) in no category, so "
            "absent from every number reported:",
            file=sys.stderr,
        )
        for line in uncounted:
            print(f"  {line}", file=sys.stderr)


def _ratio(numer: int, denom: int) -> str:
    return f"{numer / denom:.2f}:1" if denom else "n/a"


def _kib(nbytes: int) -> str:
    return f"{nbytes / 1024:,.1f}"


def _md_table(header: list[str], rows: list[list[str]]) -> list[str]:
    """A padded markdown table: first column left-aligned, the rest right.

    Shared by the snapshot table and the history table so column padding has one
    implementation rather than two that drift.
    """
    widths = [len(h) for h in header]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))
    aligns = ["<"] + [">"] * (len(header) - 1)

    def fmt(row: list[str]) -> str:
        cells = [f"{c:{a}{w}}" for c, a, w in zip(row, aligns, widths, strict=True)]
        return "| " + " | ".join(cells) + " |"

    sep = (
        "|"
        + "|".join(
            (":" + "-" * (w + 1)) if a == "<" else ("-" * (w + 1) + ":")
            for a, w in zip(aligns, widths, strict=True)
        )
        + "|"
    )
    return [fmt(header), sep, *(fmt(row) for row in rows)]


def _totals(report: Report) -> Counts:
    """The whole-report column sums.

    One implementation, because `render_markdown` carried an inline copy of this
    loop and `render_diff` called this -- two spellings of "add up every
    category", which is exactly the pair that goes out of step when a seventh
    field is added to `Counts`.
    """
    total = Counts()
    for c in report.per_category.values():
        total.files += c.files
        total.physical += c.physical
        total.code += c.code
        total.comment += c.comment
        total.blank += c.blank
        total.nbytes += c.nbytes
    return total


def render_markdown(report: Report) -> str:
    header = ["Category", "Files", "Lines", "Code", "Comment", "Blank", "Size (KiB)"]
    rows: list[list[str]] = []
    for label, c in report.per_category.items():
        rows.append(
            [
                label,
                f"{c.files:,}",
                f"{c.physical:,}",
                f"{c.code:,}",
                f"{c.comment:,}",
                f"{c.blank:,}",
                _kib(c.nbytes),
            ]
        )
    total = _totals(report)
    rows.append(
        [
            "**Total**",
            f"**{total.files:,}**",
            f"**{total.physical:,}**",
            f"**{total.code:,}**",
            f"**{total.comment:,}**",
            f"**{total.blank:,}**",
            f"**{_kib(total.nbytes)}**",
        ]
    )

    per = report.per_category
    impl = per[LABEL_IMPL]
    tests = per[LABEL_TESTS]
    migrations = per[LABEL_MIGRATIONS]
    title = (
        "## Repo size so far"
        if report.commit is None
        else f"## Repo size at {report.source_label}"
        + (f" ({report.date[:10]})" if report.date else "")
    )
    lines_out: list[str] = [title, ""]
    lines_out.extend(_md_table(header, rows))
    lines_out.append("")
    lines_out.append("**Ratios**")
    lines_out.append("")
    lines_out.append(
        f"- Tests : implementation — {_ratio(tests.physical, impl.physical)} "
        f"by physical lines, {_ratio(tests.code, impl.code)} by code lines"
    )
    lines_out.append(
        f"- Docs : code — {_ratio(docs_physical(report), code_total(report))} "
        f"(all markdown lines vs all Python + SQL code lines)"
    )
    lines_out.append(f"- Migrations — {migrations.files} file(s)")

    if report.adr_status:
        order = ["Accepted", "Proposed", "Superseded", "Deprecated", "Rejected"]
        parts: list[str] = []
        for key in order:
            if key in report.adr_status:
                parts.append(f"{report.adr_status[key]} {key}")
        for key in sorted(report.adr_status):
            if key not in order:
                parts.append(f"{report.adr_status[key]} {key}")
        total_adr = sum(report.adr_status.values())
        lines_out.append(
            f"- ADR status — {total_adr} numbered ADRs: " + ", ".join(parts)
        )

    lines_out.append("")
    if report.uncounted:
        shown = ", ".join(f"`{p}`" for p in report.uncounted[:10])
        more = (
            f", and {len(report.uncounted) - 10} more"
            if len(report.uncounted) > 10
            else ""
        )
        lines_out.append(
            f"_Uncounted tracked files ({len(report.uncounted)}): {shown}{more} — "
            "no category claims them, so they are absent from every total above._"
        )
    else:
        lines_out.append("_Uncounted tracked files: none._")
    lines_out.append("")
    lines_out.append(
        "_`specs/personal/` is excluded (it must never exist — ADR-0079) — "
        "a recreation there is neither counted nor read here._"
    )

    if report.warnings:
        lines_out.append("")
        lines_out.append("**Warnings**")
        lines_out.append("")
        for w in report.warnings:
            lines_out.append(f"- {w}")

    # No trailing newline (symmetric with render_json); main()'s print adds the
    # single terminating one -- returning "...\n" would double it under print.
    return "\n".join(lines_out)


def docs_physical(report: Report) -> int:
    """Physical lines across every markdown category.

    Derived from CATEGORIES rather than a hand-kept label list: the label beside
    this number says "all markdown lines", and the two are only guaranteed to
    agree if nobody has to remember to update a list when a category is added.
    """
    return sum(
        report.per_category[cat.label].physical
        for cat in CATEGORIES
        if cat.lang == "markdown"
    )


def code_total(report: Report) -> int:
    """Code lines across every Python and SQL category, same derivation."""
    return sum(
        report.per_category[cat.label].code
        for cat in CATEGORIES
        if cat.lang in ("python", "sql")
    )


def _report_payload(report: Report) -> dict[str, Any]:
    """The JSON shape of one point, shared by `--format json` on both
    subcommands so a snapshot and a history entry never describe the same
    report with different keys."""
    return {
        "categories": {
            label: {
                "files": c.files,
                "physical": c.physical,
                "code": c.code,
                "comment": c.comment,
                "blank": c.blank,
                "bytes": c.nbytes,
            }
            for label, c in report.per_category.items()
        },
        "adr_status": report.adr_status,
        "warnings": report.warnings,
        "uncounted": report.uncounted,
        "source": report.source_label,
        "commit": report.commit,
        "date": report.date,
        "categories_version": CATEGORIES_VERSION,
    }


def render_json(report: Report) -> str:
    return json.dumps(_report_payload(report), indent=2)


# --- history --------------------------------------------------------------

EVERY_CHOICES = ("commit", "week", "month")

HISTORY_COLUMNS = [
    "commit",
    "date",
    "category",
    "files",
    "physical",
    "code",
    "comment",
    "blank",
    "bytes",
    "categories_version",
]


def list_commits(
    ref: str, *, since: str | None = None, until: str | None = None
) -> list[tuple[str, str]]:
    """``(sha, ISO committer date)`` along first-parent history, oldest first.

    First-parent is deliberate: one point per merged PR, with the branch commits
    inside a merge left out. A full ``--all`` walk would interleave the states of
    concurrent branches into what reads as a single timeline, which is worse than
    coarse -- it is wrong.
    """
    args = ["log", "--first-parent", "--format=%H%x09%cI"]
    if since:
        args.append(f"--since={since}")
    if until:
        args.append(f"--until={until}")
    # Resolved, because `git log` disambiguates its trailing argument against
    # the *filesystem*: a bare `src` is not a revision, so git reads it as a
    # pathspec and answers with the commits that touched that directory.
    # Measured, `history --ref src` exited 0 with a 2-point series where the
    # walk is 205 -- a path-filtered answer wearing a series' clothes, with
    # nothing in the output saying so. `resolve_rev` makes a non-revision an
    # exit-2 StatsError naming it, which is what `snapshot` and `diff` already
    # do.
    #
    # The `--` guards something narrower, and it is worth stating exactly
    # because the obvious reading is wrong. It is *not* what saves a branch
    # sharing a directory's name: `resolve_rev` asks `rev-parse --verify
    # <ref>^{commit}`, which is revision-only, so a branch `src` beside a
    # directory `src` resolves cleanly with no ambiguity to break (measured).
    # By this line the argument is a 40-hex sha, and the one thing that can
    # still collide with it is a *file named that sha* -- at which point
    # `git log <sha>` exits 128 as ambiguous (measured) and `git log <sha> --`
    # exits 0. Pathological, cheap to hold, and pinned by a test rather than
    # left as an untested good intention.
    args.extend([resolve_rev(ref), "--"])
    commits: list[tuple[str, str]] = []
    for line in _git_out(*args).decode("utf-8", "replace").splitlines():
        sha, _, date = line.partition("\t")
        if sha and date:
            commits.append((sha, date))
    commits.reverse()  # git log is newest-first; a series reads chronologically
    return commits


def _bucket_key(date: str, every: str) -> str:
    dt = datetime.fromisoformat(date)
    if every == "month":
        return f"{dt.year:04d}-{dt.month:02d}"
    year, week, _ = dt.isocalendar()
    return f"{year:04d}-W{week:02d}"


def sample_commits(commits: list[tuple[str, str]], every: str) -> list[tuple[str, str]]:
    """Reduce a chronological commit list to one point per period.

    The *last* commit in each period, i.e. the state at period end. A period with
    no commits yields no point rather than a repeat of the previous one, so a
    flat stretch in a chart is a real flat stretch and a gap is a real gap.
    """
    if every == "commit":
        return list(commits)
    last: dict[str, tuple[str, str]] = {}
    for sha, date in commits:
        last[_bucket_key(date, every)] = (sha, date)
    # Input is chronological and re-assigning a key does not reorder a dict, so
    # first-seen key order is already period order; no sort of formatted keys.
    return list(last.values())


def build_series(
    commits: list[tuple[str, str]], cache: BlobCache | None = None
) -> list[Report]:
    """One report per commit, sharing a single cat-file process and blob cache."""
    reports: list[Report] = []
    shared: BlobCache = {} if cache is None else cache
    statuses: StatusCache = {}
    with BlobReader() as reader:
        for sha, date in commits:
            # Both short-circuits taken: `list_commits` returned the full sha
            # and the committer date together, so neither needs re-asking.
            source = GitRev(sha, reader, date=date, resolved=True)
            reports.append(build_report(source, shared, statuses))
    return reports


def render_history_csv(reports: list[Report]) -> str:
    """Tidy long format: one row per (commit, category).

    Long rather than wide because a category is data, not schema -- adding the
    tenth category must not change the column list and invalidate every chart
    built against the ninth.
    """
    buf = io.StringIO(newline="")
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(HISTORY_COLUMNS)
    for report in reports:
        for label, c in report.per_category.items():
            writer.writerow(
                [
                    (report.commit or "")[:7],
                    (report.date or "")[:10],
                    label,
                    c.files,
                    c.physical,
                    c.code,
                    c.comment,
                    c.blank,
                    c.nbytes,
                    CATEGORIES_VERSION,
                ]
            )
    return buf.getvalue().rstrip("\n")


def render_history_json(reports: list[Report]) -> str:
    payload = {
        "categories_version": CATEGORIES_VERSION,
        "points": [_report_payload(r) for r in reports],
    }
    return json.dumps(payload, indent=2)


def render_history_md(reports: list[Report]) -> str:
    """A compact one-row-per-point table, for reading a trend in the terminal.

    The code column is **narrower than the snapshot table's**, and says so in
    its header rather than leaving the reader to discover it. Both tables report
    Files and Lines over all nine categories, so they agree to the digit at a
    shared commit -- which is exactly what licenses a reader to compare the
    third column too, where `render_markdown`'s Total sums `code` across all
    nine (markdown content counts as code in its own row) and this one reports
    `code_total`, Python and SQL only. Measured at one commit: 55,694 against
    39,978, the difference being the markdown content lines this table reports
    separately under Docs. Both derivations are right for their own table --
    summing markdown into Code here would double-count it against Docs -- so the
    label is what carries the contract.
    """
    header = ["Commit", "Date", "Files", "Lines", "Code (py+sql)", "Docs", "ADRs"]
    rows: list[list[str]] = []
    for report in reports:
        files = sum(c.files for c in report.per_category.values())
        physical = sum(c.physical for c in report.per_category.values())
        rows.append(
            [
                (report.commit or "")[:7],
                (report.date or "")[:10],
                f"{files:,}",
                f"{physical:,}",
                f"{code_total(report):,}",
                f"{docs_physical(report):,}",
                f"{sum(report.adr_status.values()):,}",
            ]
        )
    out = [f"## Repo shape over {len(reports)} point(s)", ""]
    out.extend(_md_table(header, rows))
    warnings = warning_lines(reports)
    if warnings:
        out.append("")
        out.append(f"_{len(warnings)} file(s) across these points were {_DEGRADED}:_")
        out.append("")
        for w in warnings:
            out.append(f"- {w}")
    uncounted = uncounted_lines(reports)
    if uncounted:
        out.append("")
        out.append(
            f"_{len(uncounted)} tracked file(s) across these points are in no "
            "category, so absent from every row above:_"
        )
        out.append("")
        for u in uncounted:
            out.append(f"- {u}")
    return "\n".join(out)


# A fixed nine, one per category, in CATEGORIES order. Stated as literals rather
# than generated so a category's colour does not silently change meaning when a
# tenth is inserted ahead of it.
_CHART_COLORS = [
    "#2f6f9f",
    "#4f9ec4",
    "#7fc3d8",
    "#b08b4f",
    "#c46f6f",
    "#9f5f8f",
    "#6f7fb0",
    "#5f9f7f",
    "#8f8f6f",
]

_CHART_CSS = """
:root { color-scheme: light dark; }
body { margin: 0; padding: 24px; font: 14px/1.5 system-ui, sans-serif;
       background: #fbfbfa; color: #22201d; }
h1 { font-size: 18px; margin: 0 0 4px; }
p.sub { margin: 0 0 20px; color: #6b6660; }
p.warn { margin: -12px 0 20px; color: #a4562c; }
.legend { display: flex; flex-wrap: wrap; gap: 8px 18px; margin-top: 16px; }
.legend span { display: flex; align-items: center; gap: 6px; }
.legend i { width: 12px; height: 12px; border-radius: 2px; display: block; }
svg { max-width: 100%; height: auto; }
text { fill: #6b6660; font-size: 11px; }
@media (prefers-color-scheme: dark) {
  body { background: #1c1a18; color: #eeebe7; }
  p.sub { color: #a8a29a; }
  text { fill: #a8a29a; }
}
"""


def _x_positions(points: list[Report], left: float, plot_w: float) -> list[float]:
    """Where each point sits on the x axis: **proportional to elapsed time**.

    It was the commit *ordinal*, which made a six-month quiet stretch and a
    six-minute one occupy the same horizontal distance -- so anyone reading a
    rate of change off the chart read it wrong, and `sample_commits`' promise
    that "a gap is a real gap" was true of the data and false of the picture
    drawn from it. The tick labels are dates, which is what invites the reading.

    A single point, or several sharing one timestamp, is the degenerate case:
    the span is zero, so every x collapses to the same value. It is handled by
    the caller rather than smoothed over here, because a zero-width band is a
    drawing problem and not a positioning one.
    """
    stamps = [datetime.fromisoformat(p.date or "").timestamp() for p in points]
    span = stamps[-1] - stamps[0]
    if span <= 0:
        return [left + plot_w / 2] * len(points)
    return [left + plot_w * (t - stamps[0]) / span for t in stamps]


def render_history_html(reports: list[Report]) -> str:
    """A self-contained stacked-area chart of physical lines over time.

    Inline SVG built by string formatting: no matplotlib, no CDN, no JavaScript,
    so the file opens offline and the script keeps its stdlib-only promise.
    """
    points = [r for r in reports if r.date]
    if not points:
        return "<h1>No points</h1>"

    width, height = 960, 460
    left, right, top, bottom = 64, 16, 16, 40
    plot_w, plot_h = width - left - right, height - top - bottom

    labels = [cat.label for cat in CATEGORIES]
    stacks = [[r.per_category[label].physical for label in labels] for r in points]
    peak = max(sum(s) for s in stacks) or 1
    n = len(points)
    xs = _x_positions(points, left, plot_w)
    # One point (or several at one instant) has no horizontal extent, so a
    # polygon through it has zero area and the browser draws nothing: the reader
    # got axes, a tick and a full legend with no data and no explanation that
    # the series was too short to plot (reproduced -- nine polygons, every
    # vertex at x=504). Reached by `--every month` inside one calendar month,
    # `--every week` inside one week, or a narrow `--since`. Drawn as a bar of
    # fixed width instead, so the point is visible as the single point it is.
    bar = 0.0 if len(set(xs)) > 1 else min(72.0, plot_w / 3)

    def x_at(i: int) -> float:
        return xs[i]

    def y_at(value: float) -> float:
        return top + plot_h - (plot_h * value / peak)

    # Bottom-up cumulative bands; each polygon is its own upper edge forward and
    # the band below it backward, so the areas tile with no seams or overlap.
    bands: list[str] = []
    lower = [0.0] * n
    for depth, label in enumerate(labels):
        upper = [lower[i] + stacks[i][depth] for i in range(n)]
        forward = " ".join(
            f"{x_at(i) - bar / 2:.1f},{y_at(upper[i]):.1f} "
            f"{x_at(i) + bar / 2:.1f},{y_at(upper[i]):.1f}"
            if bar
            else f"{x_at(i):.1f},{y_at(upper[i]):.1f}"
            for i in range(n)
        )
        backward = " ".join(
            f"{x_at(i) + bar / 2:.1f},{y_at(lower[i]):.1f} "
            f"{x_at(i) - bar / 2:.1f},{y_at(lower[i]):.1f}"
            if bar
            else f"{x_at(i):.1f},{y_at(lower[i]):.1f}"
            for i in reversed(range(n))
        )
        color = _CHART_COLORS[depth % len(_CHART_COLORS)]
        bands.append(
            f'<polygon points="{forward} {backward}" fill="{color}" '
            f'fill-opacity="0.92"><title>{escape(label)}</title></polygon>'
        )
        lower = upper

    grid: list[str] = []
    for frac in (0, 0.25, 0.5, 0.75, 1.0):
        value = peak * frac
        y = y_at(value)
        grid.append(
            f'<line x1="{left}" y1="{y:.1f}" x2="{width - right}" y2="{y:.1f}" '
            f'stroke="currentColor" stroke-opacity="0.12"/>'
            f'<text x="{left - 8}" y="{y + 4:.1f}" text-anchor="end">'
            f"{int(value):,}</text>"
        )

    # The last point is always labelled. `range(0, n, step)` alone omits it
    # whenever `step` does not divide `n - 1`: at n = 100 the ticks landed at
    # 0, 16, ... 96 and the rightmost point went unnamed, so a reader took the
    # chart's right edge to be a date four commits earlier than it is. Indices
    # are collected into a set with `n - 1` added, which also keeps a near-final
    # tick from being drawn on top of it.
    tick_at = set(range(0, n, max(1, (n - 1) // 6) if n > 1 else 1))
    tick_at.discard(n - 2)
    tick_at.add(n - 1)
    ticks: list[str] = []
    for i in sorted(tick_at):
        ticks.append(
            f'<text x="{x_at(i):.1f}" y="{height - bottom + 20:.0f}" '
            f'text-anchor="middle">{escape((points[i].date or "")[:10])}</text>'
        )

    legend = "".join(
        f'<span><i style="background:{_CHART_COLORS[i % len(_CHART_COLORS)]}"></i>'
        f"{escape(label)}</span>"
        for i, label in enumerate(labels)
    )
    span = f"{(points[0].date or '')[:10]} → {(points[-1].date or '')[:10]}"
    degraded = warning_lines(points)
    uncounted = uncounted_lines(points)
    note = ""
    if degraded:
        note += (
            f'<p class="sub warn">⚠ {len(degraded)} file(s) across these points '
            f"were {escape(_DEGRADED)}.</p>\n"
        )
    if uncounted:
        note += (
            f'<p class="sub warn">⚠ {len(uncounted)} tracked file(s) across these '
            "points are in no category, so absent from every band.</p>\n"
        )
    return (
        '<!doctype html>\n<html lang="en">\n<head>\n<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        "<title>Repo shape over time</title>\n"
        f"<style>{_CHART_CSS}</style>\n</head>\n<body>\n"
        "<h1>Repo shape over time</h1>\n"
        f'<p class="sub">{n} point(s), {escape(span)} — physical lines, '
        f"stacked by category, x axis proportional to elapsed time "
        f"(categories v{CATEGORIES_VERSION})</p>\n"
        f"{note}"
        f'<svg viewBox="0 0 {width} {height}" role="img" '
        'aria-label="Stacked area chart of repository lines by category over time">\n'
        f"{''.join(grid)}\n{''.join(bands)}\n{''.join(ticks)}\n</svg>\n"
        f'<div class="legend">{legend}</div>\n</body>\n</html>'
    )


# --- diff -----------------------------------------------------------------


def parse_rev_range(spec: str, head: str | None) -> tuple[str, str | None]:
    """Resolve git's range spellings into ``(base, head-or-None)``.

    The spellings are git's own, and mean here what they mean in ``git log`` and
    ``git diff``, because a reader who has to learn a second convention for one
    subcommand will simply assume the first one:

      ``diff main HEAD``     -- the two commits, literally
      ``diff main..HEAD``    -- the same pair, written as a range
      ``diff main...HEAD``   -- from the *merge base*: work done on this branch

    A ``None`` head means the working tree, so ``diff main`` includes
    uncommitted work. Either side of a range may be omitted and defaults to
    HEAD, again as git does.
    """
    for sep in ("...", ".."):  # longest first: "..." also contains ".."
        if sep in spec:
            if head is not None:
                raise StatsError(
                    f"{spec!r} already names both ends of the range; "
                    f"drop the extra revision {head!r}"
                )
            left, _, right = spec.partition(sep)
            left, right = left or "HEAD", right or "HEAD"
            return (_merge_base(left, right) if sep == "..." else left), right
    return spec, head


def _merge_base(left: str, right: str) -> str:
    try:
        out = _git_out("merge-base", resolve_rev(left), resolve_rev(right))
    except StatsError as exc:
        raise StatsError(
            f"no common ancestor of {left!r} and {right!r}: {exc}"
        ) from exc
    sha = out.decode("ascii", "replace").strip()
    if not sha:
        raise StatsError(f"no common ancestor of {left!r} and {right!r}")
    return sha


def category_deltas(base: Report, head: Report) -> dict[str, Counts]:
    """head minus base, per category. Fields are plain ints, so they go negative."""
    deltas: dict[str, Counts] = {}
    for cat in CATEGORIES:
        b, h = base.per_category[cat.label], head.per_category[cat.label]
        deltas[cat.label] = Counts(
            files=h.files - b.files,
            physical=h.physical - b.physical,
            code=h.code - b.code,
            comment=h.comment - b.comment,
            blank=h.blank - b.blank,
            nbytes=h.nbytes - b.nbytes,
        )
    return deltas


def _signed(value: int) -> str:
    return f"{value:+,}"


def _is_zero(counts: Counts) -> bool:
    """Whether *no field* moved, asked of the dataclass rather than of a list.

    It hand-listed five of the six fields and omitted `nbytes`, so two reports
    differing only in bytes rendered as "_No category changed._" in `--format
    md` while `--format json` and `--format csv` reported the byte delta from
    the same pair (reproduced) -- three formats of one command disagreeing about
    whether anything happened. Any change preserving line counts triggers it:
    reworded prose, a lengthened line, a file swapped for one of equal shape.
    `Counts` is an eq-bearing dataclass, so comparing against a zero instance
    states the rule once and cannot fall behind a seventh field.
    """
    return counts == Counts()


def render_diff(base: Report, head: Report, *, show_all: bool = False) -> str:
    """The change in shape between two points, category by category."""
    deltas = category_deltas(base, head)
    header = ["Category", "Δ Files", "Δ Lines", "Δ Code", "Δ Comment", "Δ Blank"]
    rows: list[list[str]] = []
    for cat in CATEGORIES:
        d = deltas[cat.label]
        if _is_zero(d) and not show_all:
            continue
        rows.append(
            [
                cat.label,
                _signed(d.files),
                _signed(d.physical),
                _signed(d.code),
                _signed(d.comment),
                _signed(d.blank),
            ]
        )
    tb, th = _totals(base), _totals(head)
    rows.append(
        [
            "**Total**",
            f"**{_signed(th.files - tb.files)}**",
            f"**{_signed(th.physical - tb.physical)}**",
            f"**{_signed(th.code - tb.code)}**",
            f"**{_signed(th.comment - tb.comment)}**",
            f"**{_signed(th.blank - tb.blank)}**",
        ]
    )

    out = [f"## Repo shape: {base.source_label} → {head.source_label}", ""]
    if len(rows) == 1:  # only the Total row survived the changed-only filter
        out.append("_No category changed._")
        out.append("")
    out.extend(_md_table(header, rows))
    out.append("")
    out.append("**Ratios**")
    out.append("")
    bi, hi = base.per_category[LABEL_IMPL], head.per_category[LABEL_IMPL]
    bt, ht = base.per_category[LABEL_TESTS], head.per_category[LABEL_TESTS]
    out.append(
        f"- Tests : implementation — {_ratio(bt.physical, bi.physical)} → "
        f"{_ratio(ht.physical, hi.physical)} by physical lines"
    )
    out.append(
        f"- Docs : code — {_ratio(docs_physical(base), code_total(base))} → "
        f"{_ratio(docs_physical(head), code_total(head))}"
    )
    out.append(f"- ADR status — {adr_delta(base.adr_status, head.adr_status)}")

    if head.uncounted or base.uncounted:
        out.append("")
        out.append(
            f"_Uncounted: {len(base.uncounted)} file(s) at {base.source_label}, "
            f"{len(head.uncounted)} at {head.source_label} — in no category, so "
            "absent from these deltas._"
        )

    # Both endpoints, because a delta is only as sound as the weaker of the two
    # measurements and this table's job is to be read as a change.
    warnings = warning_lines([base, head])
    if warnings:
        out.append("")
        out.append(f"**Warnings** — {len(warnings)} file(s) {_DEGRADED}")
        out.append("")
        for w in warnings:
            out.append(f"- {w}")
    return "\n".join(out)


def adr_delta(base: dict[str, int], head: dict[str, int]) -> str:
    """Only the buckets that moved -- an unchanged status is not news."""
    moved = [
        f"{key} {base.get(key, 0)} → {head.get(key, 0)}"
        for key in sorted(set(base) | set(head))
        if base.get(key, 0) != head.get(key, 0)
    ]
    if not moved:
        return f"no change ({sum(head.values())} numbered ADRs)"
    return ", ".join(moved)


DIFF_COLUMNS = ["category", "metric", "base", "head", "delta", "categories_version"]

# (attribute on Counts, name on the wire). Two columns because they genuinely
# differ for one field: the attribute is `nbytes` only because `bytes` is a
# builtin, and reusing the attribute list as a wire format leaked that private
# reason into the output -- `diff --format json` emitted `bytes` under
# `base.categories` and `nbytes` under `delta.categories` **in the same
# document**, while the history CSV's column said `bytes` and the diff CSV's
# metric value said `nbytes`. A consumer pivoting the two silently dropped the
# byte row, and `delta.total['bytes']` raised KeyError. `bytes` is the spelling
# every other emitter already uses and the one the suite pins, so it is the one
# that survives here.
_DIFF_METRICS = (
    ("files", "files"),
    ("physical", "physical"),
    ("code", "code"),
    ("comment", "comment"),
    ("blank", "blank"),
    ("nbytes", "bytes"),
)


def render_diff_csv(base: Report, head: Report) -> str:
    """Tidy long, like the history CSV: one row per (category, metric)."""
    buf = io.StringIO(newline="")
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(DIFF_COLUMNS)
    for cat in CATEGORIES:
        b, h = base.per_category[cat.label], head.per_category[cat.label]
        for attr, wire in _DIFF_METRICS:
            bv, hv = getattr(b, attr), getattr(h, attr)
            writer.writerow([cat.label, wire, bv, hv, hv - bv, CATEGORIES_VERSION])
    return buf.getvalue().rstrip("\n")


def render_diff_json(base: Report, head: Report) -> str:
    deltas = category_deltas(base, head)
    tb, th = _totals(base), _totals(head)
    payload = {
        "categories_version": CATEGORIES_VERSION,
        "base": _report_payload(base),
        "head": _report_payload(head),
        "delta": {
            "categories": {
                label: {wire: getattr(d, attr) for attr, wire in _DIFF_METRICS}
                for label, d in deltas.items()
            },
            "total": {
                wire: getattr(th, attr) - getattr(tb, attr)
                for attr, wire in _DIFF_METRICS
            },
        },
    }
    return json.dumps(payload, indent=2)


def _use_utf8_io() -> None:
    """Force UTF-8 on the real streams, for a real invocation only.

    The report embeds em dashes; without this a Windows cp1252 console (or a
    redirect) raises UnicodeEncodeError. See CLAUDE.md's encoding note.

    Called from `__main__` and NEVER from `main()`, which is the whole point.
    `tests/test_repo_stats.py` calls `main()` in-process, so a reconfigure
    inside it mutates **pytest's session-wide capture stream** rather than
    this script's stdout — the same defect this diff removed from
    `bot_review.py` and `review_worktree.py`, still live here.

    It was worse than those two, because this call passed `encoding=` with no
    `errors=` and `TextIOWrapper.reconfigure` **resets the error handler to
    `strict` when `errors` is omitted** (measured: `replace` -> `strict`). So
    `rs.main([])` left every test running afterwards in that worker writing
    through a *stricter* stream than the suite started with, and a later test
    emitting a lone surrogate then raised UnicodeEncodeError, ordered by test
    execution order, with the blame landing on the innocent test.

    Not the log-canary stream, though the sibling copies of this paragraph
    said so before being corrected: `TeeCaptureIO.write` forwards the **str**
    to `self._other`, the pre-capture `sys.stdout`, which is what
    `tee pytest-output.log` captures, while a reconfigure touches only the
    capture wrapper.

    `errors="replace"` is now explicit rather than implied, so the handler is
    stated at the one site that sets it instead of inherited from whatever the
    stream happened to carry.

    `newline="\\n"` for the same reason `_write_out` passes it to the file
    branch, and this is the site that was missing it: a redirected
    `history --format csv` translated every line ending to CRLF (measured: 46 of
    them) while the `--out` spelling of the same data stayed LF-only, so the
    byte-stability invariant held on the path nobody defaults to and broke on
    the one everybody does. Setting it here rather than in `_write_out` keeps
    the stream's configuration in one place, and keeps it out of `main()`, which
    is the whole point of this function.

    Streams are cast to reach `.reconfigure` without a strict-typing
    complaint, and guarded for exotic streams that lack it.
    """
    for stream in (sys.stdout, sys.stderr):
        wrapper = cast(Any, stream)
        if hasattr(wrapper, "reconfigure"):
            wrapper.reconfigure(encoding="utf-8", errors="replace", newline="\n")


def _write_out(text: str, out: str | None) -> None:
    """Emit a rendered report, to a file or to stdout, under the error contract.

    Two defects met at this function, and both were on the branch nobody looked
    at.

    **LF on *both* branches.** `newline="\\n"` was passed to `Path.write_text`
    and the invariant it protects -- byte-stability of a CSV and an SVG across
    machines -- was then violated by `print`, which translates through stdout's
    own newline handling: measured on Windows, `history --format csv > f` wrote
    46 CRLF sequences where the identical data through `--out` wrote none. The
    guarded path is the one nobody defaults to; `history`'s default format is
    CSV to stdout. Fixed at `_use_utf8_io`, which already owns the real
    invocation's stream setup and reconfigured encoding but not newline.

    **A write failure is part of the contract.** A mistyped `--out` directory
    raised a six-frame `FileNotFoundError` at exit 1, against a docstring
    promising exit 2 "with a one-line message rather than a traceback" -- and on
    `history` it discards the whole walk *after* every second of it is spent.
    """
    if out is None:
        print(text)
        return
    try:
        Path(out).write_text(text + "\n", encoding="utf-8", newline="\n")
    except OSError as exc:
        raise StatsError(f"could not write {out}: {exc.strerror or exc}") from exc


def _with_default_command(argv: list[str]) -> list[str]:
    """Route a bare invocation (and the legacy ``--json``) to ``snapshot``.

    `repo_stats.py` and `repo_stats.py --json` are the two spellings that
    predate the subcommands; both are documented in the repo-stats skill and
    pinned by the test suite, so they keep working unchanged. ``-h``/``--help``
    is exempted, or the top-level help would become snapshot's.
    """
    if argv and argv[0] in ("-h", "--help"):
        return argv
    if not argv or argv[0].startswith("-"):
        return ["snapshot", *argv]
    return argv


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="repo_stats.py", description="Report repository size and shape."
    )
    sub = parser.add_subparsers(dest="command")

    snap = sub.add_parser(
        "snapshot", help="one point: the working tree, or a given revision"
    )
    snap.add_argument(
        "rev",
        nargs="?",
        help="revision to measure, read from the object database with nothing "
        "checked out; default is the working tree",
    )
    snap.add_argument("--format", choices=("md", "json"), default="md")
    # The legacy spelling. Hidden from help so there is one documented way to
    # ask for JSON, but kept working forever -- it is in SKILL.md and in muscle
    # memory.
    #
    # Writing to `format`'s own dest rather than a separate flag, because a
    # separate flag needed a precedence rule and the one it got was wrong: a
    # `fmt = "json" if args.json else args.format` in the runner made the hidden
    # legacy spelling beat the documented one in *either* order, so
    # `--format md --json` emitted JSON with no error and no warning. Sharing
    # the dest hands ordering to argparse, where last-wins is what a reader
    # already expects, and deletes the precedence line entirely.
    snap.add_argument(
        "--json",
        dest="format",
        action="store_const",
        const="json",
        help=argparse.SUPPRESS,
    )
    snap.add_argument("--out", help="write to this file instead of stdout")

    hist = sub.add_parser(
        "history", help="a series of points over first-parent history"
    )
    hist.add_argument(
        "--every",
        choices=EVERY_CHOICES,
        default="commit",
        help="one point per commit (default), or the last commit of each ISO "
        "week or calendar month",
    )
    hist.add_argument("--since", help="git date, e.g. 2026-07-01")
    hist.add_argument("--until", help="git date")
    hist.add_argument(
        "--ref", default="HEAD", help="branch or revision to walk (default HEAD)"
    )
    hist.add_argument("--format", choices=("csv", "json", "md", "html"), default="csv")
    hist.add_argument("--out", help="write to this file instead of stdout")

    dif = sub.add_parser("diff", help="the change in shape between two points")
    dif.add_argument(
        "base",
        help="a revision, or a git range: main..HEAD for two commits, "
        "main...HEAD for work since the merge base",
    )
    dif.add_argument(
        "head",
        nargs="?",
        help="the second revision; default is the working tree, so `diff main` "
        "includes uncommitted work",
    )
    dif.add_argument("--format", choices=("md", "json", "csv"), default="md")
    dif.add_argument(
        "--all",
        action="store_true",
        help="include categories that did not change (default: only those that did)",
    )
    dif.add_argument("--out", help="write to this file instead of stdout")
    return parser


def _run_snapshot(args: argparse.Namespace) -> int:
    fmt = args.format
    if args.rev is None:
        report = build_report(WorkTree())
    else:
        with BlobReader() as reader:
            report = build_report(GitRev(args.rev, reader))
    text = render_json(report) if fmt == "json" else render_markdown(report)
    _write_out(text, args.out)
    return 0


def _run_history(args: argparse.Namespace) -> int:
    commits = list_commits(args.ref, since=args.since, until=args.until)
    if not commits:
        raise StatsError(f"no commits on {args.ref!r} in the requested range")
    points = sample_commits(commits, args.every)
    if len(points) > 20:
        # A full-history walk is tens of seconds of silence otherwise. stderr,
        # so it never lands in a redirected CSV.
        print(f"repo_stats: walking {len(points)} commits…", file=sys.stderr)
    reports = build_series(points)
    renderers = {
        "csv": render_history_csv,
        "json": render_history_json,
        "md": render_history_md,
        "html": render_history_html,
    }
    _write_out(renderers[args.format](reports), args.out)
    _emit_diagnostics(reports)
    return 0


def _run_diff(args: argparse.Namespace) -> int:
    base_rev, head_rev = parse_rev_range(args.base, args.head)
    # One reader and one cache for both ends: two commits close together share
    # most of their blobs, so the second report is largely cache hits.
    cache: BlobCache = {}
    with BlobReader() as reader:
        # The head report really does hit this cache when it is the working
        # tree -- it did not originally, which made `diff <base>` slower than
        # `diff <base> <head>` despite doing strictly less work.
        #
        # The *reason* changed and this comment did not, which a review caught:
        # it used to read "`WorkTree` carries index blob ids too", and that
        # stopped being true when those ids were removed for lying about disk
        # content. What makes the hit happen now is `_content_id` -- the
        # working-tree side is keyed on git's own blob id for the bytes it
        # read, so content identical to the revision side lands on that side's
        # entry. Re-measured after the change rather than carried over, and
        # measured for *both* mechanisms against one tree rather than quoting
        # the old comment's figure across two: on `diff HEAD` over this
        # repository, index-id keying and content keying each hit 275 of 275
        # head-side gets. Content keying costs nothing here; it does not gain
        # anything either, and an earlier draft of this comment claimed it did
        # by comparing today's number against one measured on a tree that had
        # a dirty file in it.
        base = build_report(GitRev(base_rev, reader), cache)
        head = (
            build_report(GitRev(head_rev, reader), cache)
            if head_rev is not None
            else build_report(WorkTree(), cache)
        )
    if args.format == "json":
        text = render_diff_json(base, head)
    elif args.format == "csv":
        text = render_diff_csv(base, head)
    else:
        text = render_diff(base, head, show_all=args.all)
    _write_out(text, args.out)
    _emit_diagnostics([base, head])
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(
        _with_default_command(list(sys.argv[1:] if argv is None else argv))
    )
    runners = {"history": _run_history, "diff": _run_diff}
    try:
        return runners.get(args.command, _run_snapshot)(args)
    except StatsError as exc:
        # The one class of failure that is not "exit 0 with a warning": the tree
        # could not be enumerated at all, so every number would be a lie.
        print(f"repo_stats: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    _use_utf8_io()
    sys.exit(main())
