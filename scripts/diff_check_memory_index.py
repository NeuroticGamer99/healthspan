#!/usr/bin/env python3
"""Differential harness for ``check_memory_index.py`` — old output vs new.

**Why this exists, and why it is not another test.** Four generations of this
checker's parser shipped silent behaviour changes, and every one of them landed
*with a passing test written for it*. External review round 2 introduced five
parser regressions; two full local smoke rounds — nine isolated runs, mutation
tested — passed straight over all five. Mutation testing proves a test tracks
the code it was written against. It cannot prove the code still does what it
did before, because the test and the change arrive together.

The instrument that found them was a diff of old-vs-new output on the same
inputs, which is what this script mechanises. Recorded as item 5 of the
review-mechanization work items.

**Both scripts are snapshotted per revision.** ``check_memory_index.py`` no
longer carries its own CommonMark parser; it imports ``check_spec_links.py``
through ``_markdown()``, which resolves the sibling from ``__file__.parent``.
Comparing revisions therefore means materialising *both* files at each
revision into their own directory — otherwise the "old" checker is silently
driven by the *new* parser, and the regression class this harness exists to
catch is exactly the one that would hide.

Usage::

    # HEAD vs worktree; then an older baseline; then adding the real corpus.
    uv run --locked python scripts/diff_check_memory_index.py
    uv run --locked python scripts/diff_check_memory_index.py --base HEAD~3
    uv run --locked python scripts/diff_check_memory_index.py --live

Exit 0 when every corpus produces an identical report on both sides, 1 when any
differ, 2 when the harness could not run. A difference is not automatically a
defect — it is a behaviour change that must be *chosen*, with the diff in hand.
"""

from __future__ import annotations

import argparse
import importlib.util
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType

_SCRIPTS = ("check_memory_index.py", "check_spec_links.py")
_GIT = shutil.which("git") or "git"
# Both derived from `__file__`, like every other path in this module and its
# sibling. The working-tree side used to be read from a cwd-relative
# `Path("scripts")` and git was run with no `cwd=`, so the harness only worked
# from the repository root: from `specs/` it raised `FileNotFoundError` out of
# `shutil.copyfile` -- an uncaught traceback rather than the documented "exit 2
# when the harness could not run". The silent variant is worse, and is why this
# is anchored rather than merely guarded: run from another checkout that also
# has `scripts/check_memory_index.py` and both sides come from *that* tree while
# `diff_check_spec_links._repoint` points the parser at this one, and the run
# prints a reassuring "identical".
_SCRIPTS_DIR = Path(__file__).resolve().parent
REPO_ROOT = _SCRIPTS_DIR.parent

# --------------------------------------------------------------------------
# The corpus. Each entry is a whole memory directory, keyed by file name.
#
# These are the shapes that have actually broken, one per measured regression
# class, plus the ordinary cases whose silence is the point. A fixture earns a
# place here by discriminating between two implementations, not by covering a
# line -- see the derived-oracle trap in the round-3 report.
# --------------------------------------------------------------------------

_MEM = "---\nname: {name}\n---\n\n{body}\n"


def _memory(name: str, body: str = "x") -> str:
    return _MEM.format(name=name, body=body)


CORPORA: dict[str, dict[str, str]] = {
    "clean": {
        "MEMORY.md": "# Index\n\n- [Alpha](project_alpha.md) — hook\n",
        "project_alpha.md": _memory("project_alpha"),
    },
    "orphan": {
        "MEMORY.md": "# Index\n\n- [Alpha](project_alpha.md) — hook\n",
        "project_alpha.md": _memory("project_alpha"),
        "project_orphan.md": _memory("project_orphan"),
    },
    "dangling-row": {
        "MEMORY.md": "# Index\n\n- [Gone](project_gone.md) — hook\n",
    },
    "duplicate-rows": {
        "MEMORY.md": (
            "# Index\n\n- [A](project_a.md) — one\n- [A again](project_a.md) — two\n"
        ),
        "project_a.md": _memory("project_a"),
    },
    # Round 3, finding 8a: a fence nested under a list item. The shared fence
    # regex admitted only a 0-3 space indent, so this opened no fence at all and
    # the wiki-link inside it was scanned as live prose. Fixed by adopting a real
    # CommonMark parser; this fixture is what showed that change landing.
    "fence-in-list-item": {
        "MEMORY.md": "# Index\n\n- [A](project_a.md) — hook\n",
        "project_a.md": _memory(
            "project_a",
            "1. Example:\n\n    ```\n    see [[project_nowhere]] here\n    ```\n",
        ),
    },
    # The same trade, reached by a tab rather than by four spaces.
    "fence-tab-indented": {
        "MEMORY.md": "# Index\n\n- [A](project_a.md) — hook\n",
        "project_a.md": _memory(
            "project_a", "text:\n\n\t```\n\tsee [[project_nowhere]] here\n\t```\n"
        ),
    },
    # Round 3, finding 8b: a code span crossing one newline. CommonMark allows
    # it; the shared regex is line-local, so the span stops being a span.
    "span-across-newline": {
        "MEMORY.md": "# Index\n\n- [A](project_a.md) — hook\n",
        "project_a.md": _memory(
            "project_a", "a `[[project_nowhere]]\nstill in span` b"
        ),
    },
    # The boundary the other side of it: a blank line ends a paragraph, so this
    # is NOT one span and the link inside it is live. The pair is what makes
    # either fixture discriminating -- one alone is satisfied by "never span".
    "span-across-blank-line": {
        "MEMORY.md": "# Index\n\n- [A](project_a.md) — hook\n",
        "project_a.md": _memory("project_a", "a `[[project_nowhere]]\n\nnot a span` b"),
    },
    "unbalanced-backtick": {
        "MEMORY.md": (
            "# Index\n\n- [A](project_a.md) — an ` unpaired tick\n"
            "- [B](project_b.md) — hook\n"
        ),
        "project_a.md": _memory("project_a"),
        "project_b.md": _memory("project_b"),
    },
    "long-backtick-run": {
        "MEMORY.md": "# Index\n\n- [A](project_a.md) — hook\n",
        "project_a.md": _memory("project_a", "`" * 200 + "\ntext\n"),
    },
    "html-comment-inline": {
        "MEMORY.md": "# Index\n\n- [A](project_a.md) — hook\n",
        "project_a.md": _memory(
            "project_a", "before <!-- [[project_nowhere]] --> after"
        ),
    },
    "html-comment-multiline": {
        "MEMORY.md": "# Index\n\n- [A](project_a.md) — hook\n",
        "project_a.md": _memory(
            "project_a", "before\n<!--\n[[project_nowhere]]\n-->\nafter"
        ),
    },
    # A comment opener inside a code span must not pair with a later closer.
    # The document-wide DOTALL pre-pass this replaced blanked every row between.
    "comment-opener-inside-a-span": {
        "MEMORY.md": (
            "# Index\n\n- [A](project_a.md) — a `<!--` in code\n"
            "- [B](project_b.md) — hook\n-->\n"
        ),
        "project_a.md": _memory("project_a"),
        "project_b.md": _memory("project_b"),
    },
    # Round 2 vs round 3 disagreed here; round 3 won. A different type prefix is
    # a different memory, so this is a forward reference (warning), not dead.
    "wrong-type-prefix": {
        "MEMORY.md": "# Index\n\n- [A](project_a.md) — hook\n",
        "project_a.md": _memory("project_a", "see [[reference_a]]"),
    },
    "forward-reference": {
        "MEMORY.md": "# Index\n\n- [A](project_a.md) — hook\n",
        "project_a.md": _memory("project_a", "see [[project_not_written_yet]]"),
    },
    "missing-type-prefix": {
        "MEMORY.md": "# Index\n\n- [A](project_a.md) — hook\n",
        "project_a.md": _memory("project_a", "see [[a]]"),
    },
    # Findings 1, 2 and 9b: case folding applied at some comparison sites and
    # not others. Every one of these must agree with the filesystem.
    "case-only-row-mismatch": {
        "MEMORY.md": "# Index\n\n- [A](Project_Alpha.md) — hook\n",
        "project_alpha.md": _memory("project_alpha"),
    },
    "case-only-wiki-link": {
        "MEMORY.md": "# Index\n\n- [A](project_alpha.md) — hook\n",
        "project_alpha.md": _memory("project_alpha", "see [[Project_Alpha]]"),
    },
    "uppercase-md-suffix": {
        "MEMORY.md": "# Index\n\n- [A](project_alpha.MD) — hook\n",
        "project_alpha.md": _memory("project_alpha"),
    },
    # Finding 2 proper: one memory, two rows differing only in case. Folded by
    # the orphan and dangling loops and *not* by the duplicate loop, so it fell
    # through all three checks and reported clean.
    "duplicate-rows-case-only": {
        "MEMORY.md": (
            "# Index\n\n- [A](project_a.md) — one\n- [A again](Project_A.md) — two\n"
        ),
        "project_a.md": _memory("project_a"),
    },
    # The boundary that repair must not cross: a destination keeps its anchor
    # even though a filename does not, so two rows into two sections of one
    # memory are an ordinary index rather than a concurrency incident.
    "rows-into-two-anchors": {
        "MEMORY.md": (
            "# Index\n\n- [Top](project_a.md#top) — one\n"
            "- [End](project_a.md#end) — two\n"
        ),
        "project_a.md": _memory("project_a"),
    },
    # NOTE: the *other* half of finding 1 — two memories on disk whose names
    # differ only in case — cannot be a fixture here. A corpus is a dict of
    # filenames, and on the Windows and macOS filesystems this harness runs on,
    # writing both spellings produces one file, so the fixture would silently
    # become a different corpus rather than the one it names. It is pinned in
    # `tests/test_check_memory_index.py` instead, against a directory made
    # case-sensitive at run time and skipped where that fails.
    "anchored-row-and-pathed-link": {
        "MEMORY.md": (
            "# Index\n\n- [A](project_a.md#top) — anchored\n"
            "- [B](sub/project_b.md) — pathed\n"
        ),
        "project_a.md": _memory("project_a"),
    },
    # Cut item 8: a one-character scheme, which is also a Windows drive-relative
    # path. The shared SCHEME_RE wants two or more characters before the colon,
    # so this passed the scheme test and was then read as a bare filename.
    "one-character-scheme": {
        "MEMORY.md": (
            "# Index\n\n- [A](project_a.md) — hook\n- [Drive](c:project_b.md)\n"
        ),
        "project_a.md": _memory("project_a"),
    },
    "uri-destinations": {
        "MEMORY.md": (
            "# Index\n\n- [m](mailto:project_a.md)\n"
            "- [h](https://example.invalid/project_a.md)\n- [A](project_a.md) — hook\n"
        ),
        "project_a.md": _memory("project_a"),
    },
    "bom-and-crlf": {
        "MEMORY.md": "﻿# Index\r\n\r\n- [A](project_a.md) — hook\r\n",
        "project_a.md": "﻿---\r\nname: project_a\r\n---\r\n\r\nx\r\n",
    },
    "frontmatter-with-blank-line": {
        "MEMORY.md": "# Index\n\n- [A](project_a.md) — hook\n",
        "project_a.md": "---\nname: project_a\n\ndescription: d\n---\n\nx\n",
    },
    "thematic-break-not-frontmatter": {
        "MEMORY.md": "# Index\n\n- [A](project_a.md) — hook\n",
        "project_a.md": "---\n\n# Heading\n\nname: not_frontmatter\n",
    },
    "name-field-in-the-body": {
        "MEMORY.md": "# Index\n\n- [A](project_a.md) — hook\n",
        "project_a.md": "---\nname: project_a\n---\n\nname: something_else\n",
    },
    "near-load-limit": {
        "MEMORY.md": "# Index\n\n"
        + "".join(f"- [M{i}](project_m{i}.md) — r\n" for i in range(170)),
        **{f"project_m{i}.md": _memory(f"project_m{i}") for i in range(170)},
    },
    "table-rows": {
        "MEMORY.md": (
            "# Index\n\n| Memory | Type |\n|---|---|\n| [A](project_a.md) | project |\n"
        ),
        "project_a.md": _memory("project_a"),
    },
    "lazy-continuation-row": {
        "MEMORY.md": (
            "# Index\n\n- [A](project_a.md) — a hook that wraps\n"
            "  onto [B](project_b.md)\n"
        ),
        "project_a.md": _memory("project_a"),
        "project_b.md": _memory("project_b"),
    },
    # --- The shapes this instrument could not see -----------------------------
    #
    # An external round measured the gap rather than guessing at it: the branch
    # changed four observable behaviours of `check()` and the corpus was 31
    # entries before and after, with **zero** carrying a `[[wiki-link]]`, a
    # single-quoted title, a nested-bracket link or an HTML block. `--base`
    # against the pre-branch revision reported "31 corpora, 28 identical, 3
    # diverged", all three being message rewordings. A fixture set that cannot
    # fail is the derived-oracle shape, and it is why the findings below were
    # invisible to the instrument built to catch exactly that class.
    "wiki-link-in-the-index-itself": {
        # The wiki-link scan now includes MEMORY.md, which nothing covered.
        "MEMORY.md": (
            "# Index\n\n- [A](project_a.md) — see [[project_not_written_yet]]\n"
        ),
        "project_a.md": _memory("project_a"),
    },
    "nested-bracket-link-text": {
        # `LINK_RE` admits one level of balanced brackets.
        "MEMORY.md": "# Index\n\n- [A [draft]](project_a.md) — hook\n",
        "project_a.md": _memory("project_a"),
    },
    "single-quoted-title": {
        # `TITLE_RE` admits `'...'`; without it the title glued to the path and
        # the row resolved to no memory at all.
        "MEMORY.md": "# Index\n\n- [A](project_a.md 'the title') — hook\n",
        "project_a.md": _memory("project_a"),
    },
    "duplicate-rows-one-spelling": {
        # The duplicate threshold gained `len(spellings) > len(distinct)`: two
        # rows reaching one memory by two spellings of the same path.
        "MEMORY.md": (
            "# Index\n\n- [A](project_a.md) — one\n- [A again](./project_a.md) — two\n"
        ),
        "project_a.md": _memory("project_a"),
    },
    "html-block-fold-around-rows": {
        # Rows inside a `<details>` fold that is not blank-separated from its
        # tag. CommonMark makes the whole run one html_block; the loader reads
        # `MEMORY.md` as text and sees the rows, so blanking them raised a hard
        # "orphaned memory" error against a corpus the loader reconciles fine.
        "MEMORY.md": (
            "# Index\n\n<details>\n- [A](project_a.md) — hook\n"
            "- [B](project_b.md) — hook\n</details>\n"
        ),
        "project_a.md": _memory("project_a"),
        "project_b.md": _memory("project_b"),
    },
    "row-led-by-an-html-comment": {
        # The comment is itself an html_block covering the whole line, so
        # blanking the block took the row with it.
        "MEMORY.md": "# Index\n\n<!-- keep sorted --> - [A](project_a.md) — hook\n",
        "project_a.md": _memory("project_a"),
    },
    "comment-closer-on-a-masked-line": {
        # The load-limit half: a code span on the comment's *closing* line
        # shortened the mask, the closer was never seen, and every row after it
        # was dropped before the limit was measured.
        "MEMORY.md": (
            "# Index\n\n<!-- a note\nstill inside `code` --> - [A](project_a.md) — h\n"
            "- [B](project_b.md) — hook\n"
        ),
        "project_a.md": _memory("project_a"),
        "project_b.md": _memory("project_b"),
    },
    "comment-quoted-in-a-code-span": {
        # `line` and `guide` desynchronized here, and the slice that truncates
        # at a later opener then cut the source line in the wrong place.
        "MEMORY.md": (
            "# Index\n\n- [A](project_a.md) — `<!-- x -->` and <!-- y --> tail\n"
        ),
        "project_a.md": _memory("project_a"),
    },
}


@dataclass
class Side:
    """One revision's pair of scripts, materialised and imported."""

    label: str
    module: ModuleType | None
    parser: ModuleType


class HarnessError(RuntimeError):
    """The harness could not run — **not** "the revisions disagree".

    The distinction is the whole exit contract: 0 identical, 1 differ, 2 could
    not run. Every refusal in this module used to be `raise SystemExit(<str>)`,
    which exits **1** — so a harness that never compared anything reported the
    same status as a real divergence, and `main`'s own `return 2` guards sat
    downstream of raises that fired first and could never be reached. Measured:
    `--base 3d7af0ac` printed the sentinel refusal with no "harness could not
    run:" prefix and exited 1.

    `diff_check_spec_links` imports `load_side` from here, so it inherits both
    the raise and the obligation to catch it.
    """


def _git_show(rev: str, path: str, into: Path) -> bool:
    """Write ``rev:path`` to ``into``. False when the path does not exist there.

    A missing path is a legitimate answer rather than a failure: a script added
    on this branch does not exist at the baseline the ADR documents, and
    aborting on that made every baseline old enough to be interesting
    unreachable -- so the one reproduction command ADR-0061 prints could not
    run. Any *other* git failure still raises.
    """
    proc = subprocess.run(  # noqa: S603
        [_GIT, "show", f"{rev}:{path}"],
        capture_output=True,
        check=False,
        cwd=REPO_ROOT,
    )
    if proc.returncode != 0:
        message = proc.stderr.decode("utf-8", "replace").strip()
        if "exists on disk, but not in" in message or "does not exist in" in message:
            return False
        raise HarnessError(f"cannot read {path} at {rev}: {message}")
    into.write_bytes(proc.stdout)
    return True


def load_side(label: str, rev: str | None, staging: Path) -> Side:
    """Materialise both scripts for one side and import the checker.

    ``rev is None`` means the working tree. The two files land in a directory of
    their own so ``_markdown()``'s ``__file__.parent`` sibling lookup resolves
    to *this* side's parser rather than to whichever copy is on ``sys.path``.

    Public, and imported by ``scripts/diff_check_spec_links.py`` rather than
    copied: that sibling needs exactly this materialisation and differs only in
    which of the two loaded modules it interrogates. A second copy of the
    snapshot machinery is the drift this repository has already paid for four
    times over in its markdown parsers.
    """
    home = staging / label
    home.mkdir(parents=True)
    present: set[str] = set()
    for name in _SCRIPTS:
        if rev is None:
            shutil.copyfile(_SCRIPTS_DIR / name, home / name)
            present.add(name)
        elif _git_show(rev, f"scripts/{name}", home / name):
            present.add(name)
    if "check_spec_links.py" not in present:
        raise HarnessError(
            f"{rev}: scripts/check_spec_links.py does not exist there, so this "
            "side has no parser to compare"
        )

    # Load this side's parser under a name of its own, and hand it to `Side` so
    # `_render` can bind it to the *shared* name around each call.
    #
    # Putting each side's files in their own directory is necessary and not
    # sufficient: `_markdown()` does a plain `import check_spec_links`, and
    # Python consults `sys.modules` before `sys.path`. So the first side to run
    # caches its parser under the bare name and the second side silently gets
    # it -- the old checker driven by the new parser, which is the exact failure
    # this harness exists to catch, reproduced inside the harness. It stayed
    # invisible while the two revisions' parsers were byte-identical, and
    # surfaced the moment one of them gained a function the other lacked.
    parser = _import(f"_diff_{label}_check_spec_links", home / "check_spec_links.py")
    module = (
        _import(f"_diff_{label}_check_memory_index", home / "check_memory_index.py")
        if "check_memory_index.py" in present
        else None
    )
    return Side(label=label, module=module, parser=parser)


def _import(module_name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise HarnessError(f"cannot import {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def _render(side: Side, memory_dir: Path) -> list[str]:
    """``check()``'s whole observable output, as comparable lines.

    A raised exception is part of the behaviour being compared, not a crash:
    turning a report into a refusal (or the reverse) is precisely the kind of
    change that shipped silently before.
    """
    if side.module is None:
        raise HarnessError(
            f"{side.label}: scripts/check_memory_index.py does not exist at that "
            "revision, so there is no checker to render"
        )
    # Bind this side's parser to the bare name `_markdown()` imports, for the
    # duration of the call. Without it the two sides share one parser and the
    # comparison is between a revision and itself.
    #
    # **For the duration of the call, and no longer.** `_markdown()` also
    # inserts its own directory at `sys.path[0]`, so leaving either in place
    # points the *live* process at a snapshot: measured, after one in-process
    # `main()` the real `check_memory_index` resolved its parser to a deleted
    # staging path, with two dead directories ahead of `scripts/` on the path.
    # Any later test in the same xdist worker would then parse with the
    # committed parser rather than the working tree's -- a working-tree
    # regression passing green, nondeterministically under `-n auto`. That is
    # the same `sys.modules` shadowing this harness exists to keep closed,
    # pointed outward instead of inward.
    saved_module = sys.modules.get("check_spec_links")
    saved_path = list(sys.path)
    sys.modules["check_spec_links"] = side.parser
    try:
        report = side.module.check(memory_dir)
    except (Exception, SystemExit) as exc:
        # A raise IS part of the observed behaviour -- turning a report into a
        # refusal, or the reverse, is exactly what shipped silently before. But
        # not `BaseException`: that swallows Ctrl-C and records the operator's
        # interrupt as a corpus's behaviour, which is the defect class an
        # external round found in the checker itself.
        return [f"RAISED {type(exc).__name__}: {exc}"]
    finally:
        if saved_module is None:
            sys.modules.pop("check_spec_links", None)
        else:
            sys.modules["check_spec_links"] = saved_module
        sys.path[:] = saved_path
    lines = [f"examined: {report.evidence()}"]
    # Unsorted, because `main()` prints them unsorted and `check()` carries the
    # comment "Emitted in three blocks, in the order the checks were originally
    # written, so the reported text of an unaffected corpus is byte-identical".
    # Sorting here compensated for no nondeterminism -- every producer already
    # iterates a sorted sequence -- and cost the one thing it hid: a revision
    # that reorders the blocks changes what every operator sees, and this
    # harness reported "identical".
    lines += [f"error: {e}" for e in report.errors]
    lines += [f"warning: {w}" for w in report.warnings]
    return lines


# The corpus every run must be able to report on. Any environment failure --
# a missing dependency being the measured one -- makes both sides raise the
# *same* exception, `_render` turns each into the same comparable string, and
# the run reports full agreement having parsed nothing. Two further shapes reach
# the same place: `--base HEAD` against a clean tree compares a revision with
# itself, and `raise SystemExit(<str>)` exits **1**, collapsing "the harness
# broke" into "the revisions disagree" -- which is why every such raise here is
# now a `HarnessError` that `main` turns into exit 2. So one case is required to
# produce a real report, and the run aborts with exit 2 rather than exit 0 when
# it does not.
_SENTINEL = "clean"


def _sentinel_failure(side: Side, memory_dir: Path) -> str | None:
    """Why `side` cannot be trusted to have run at all, or None if it can."""
    rendered = _render(side, memory_dir)
    raised = [line for line in rendered if line.startswith("RAISED ")]
    if raised:
        return f"{side.label} could not report on the {_SENTINEL!r} corpus: {raised[0]}"
    if not any(line.startswith("examined: ") for line in rendered):
        return f"{side.label} produced no evidence line for the {_SENTINEL!r} corpus"
    return None


def _materialise(files: dict[str, str], into: Path) -> Path:
    into.mkdir(parents=True, exist_ok=True)
    for name, content in files.items():
        target = into / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8", newline="")
    return into


def render_diff(label: str, base: list[str], head: list[str]) -> bool:
    """Print a divergence if there is one. True when the two agree.

    Shared with ``diff_check_spec_links``, which imports it rather than keeping
    a second copy: it is the routine that decides what an operator sees, so two
    of them is two answers to the same question.
    """
    if base == head:
        return True
    print(f"\n--- {label}")
    only_base = [line for line in base if line not in head]
    only_head = [line for line in head if line not in base]
    if not only_base and not only_head:
        # The comparison is `==` but the report was membership, so a divergence
        # in *multiplicity or order alone* printed a header and an empty body --
        # the operator is told something changed and shown nothing. Duplicated
        # link tuples are ordinary in this corpus, so this is reachable rather
        # than theoretical.
        print("  same lines, different order or count:")
        print(f"  base: {base}")
        print(f"  head: {head}")
        return False
    for line in only_base:
        print(f"  base only: {line}")
    for line in only_head:
        print(f"  head only: {line}")
    return False


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Diff check_memory_index's reports between two revisions over a "
            "corpus of adversarial fixtures."
        )
    )
    parser.add_argument(
        "--base", default="HEAD", help="baseline revision (default: HEAD)"
    )
    parser.add_argument(
        "--head",
        default=None,
        help="revision to compare against the baseline (default: the working tree)",
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="also run both sides over this machine's real memory directory",
    )
    parser.add_argument(
        "--only",
        default=None,
        help="run one named corpus rather than all of them",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """The documented exit contract: 0 identical, 1 differ, 2 could not run.

    A thin wrapper so `HarnessError` reaches exit **2** from wherever it is
    raised. `_run`'s own `return 2` guards only cover the refusals it can see
    coming; the raises fire from `load_side`, `_git_show`, `_import` and
    `_render`, several of them from *inside* the sentinel check that runs before
    those guards.
    """
    try:
        return _run(argv)
    except HarnessError as exc:
        print(f"harness could not run: {exc}")
        return 2


def _run(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    corpora = dict(CORPORA)
    if args.only:
        if args.only not in corpora:
            print(f"no such corpus: {args.only}. Known: {', '.join(sorted(corpora))}")
            return 2
        corpora = {args.only: corpora[args.only]}

    with tempfile.TemporaryDirectory(prefix="memindex-diff-") as raw:
        staging = Path(raw)
        base = load_side("base", args.base, staging)
        head = load_side("head", args.head, staging)

        sentinel_dir = _materialise(
            CORPORA[_SENTINEL], staging / "sentinel" / _SENTINEL
        )
        for side in (base, head):
            failure = _sentinel_failure(side, sentinel_dir)
            if failure is not None:
                print(f"harness could not run: {failure}")
                return 2

        cases: list[tuple[str, Path]] = []
        for name, files in sorted(corpora.items()):
            cases.append((name, _materialise(files, staging / "corpora" / name)))
        if args.live:
            if head.module is None:
                print(
                    "harness could not run: the head side has no checker to "
                    "resolve the live memory directory with"
                )
                return 2
            live = head.module.resolve_memory_dir(head.module.repo_root())
            cases.append((f"LIVE {live}", live))

        agreed = 0
        for name, memory_dir in cases:
            if render_diff(name, _render(base, memory_dir), _render(head, memory_dir)):
                agreed += 1

    diverged = len(cases) - agreed
    head_label = args.head or "worktree"
    print(
        f"\n{len(cases)} corpora, {agreed} identical, {diverged} diverged "
        f"({args.base} -> {head_label})"
    )
    return 1 if diverged else 0


if __name__ == "__main__":
    sys.exit(main())
