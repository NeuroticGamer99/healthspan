#!/usr/bin/env python3
"""Differential harness for ``check_spec_links.py`` — old link set vs new.

Given two revisions of the markdown parser, does any file in this repository
yield a different set of links?

**Why it exists.** ADR-0061's adoption of ``markdown-it-py`` rests on a measured
claim — that the new parser and the regexes it replaced agree on every markdown
file in the repository. That measurement was originally taken with a throwaway
script, which makes the claim unreproducible by anyone reading the ADR later. On
a gate whose markdown parsing was hand-rolled from inception and corrected
repeatedly for silent defects, an unreproducible "we measured it" is the weakest
sentence in the record. This script is that measurement, committed.

**What it compares.** ``link_targets`` returns ``(line number, raw target)`` for
every link outside code, so a diff of that list catches both halves of a parser
change: a link that stopped being found (something newly treated as code) and a
link that started being found (something no longer treated as code) — and,
because the line number is part of the tuple, a mask that silently shifted a
file's numbering while finding the same links.

The snapshot machinery lives in [`diff_harness.py`](diff_harness.py) rather than
here. It is written to be shared from the start: a second harness over a
different checker differs only in which module it interrogates and what it
renders, and a second copy of the snapshot logic is the drift this repository
has already paid for four times over in its markdown parsers.

Usage::

    # HEAD vs worktree; then an older baseline; then the fixtures alone.
    uv run --locked python scripts/diff_check_spec_links.py
    uv run --locked python scripts/diff_check_spec_links.py --base HEAD~5
    uv run --locked python scripts/diff_check_spec_links.py --no-live

Exit 0 when every input yields an identical link set on both sides, 1 when any
differ, 2 when the harness could not run. A difference is not automatically a
defect — it is a behaviour change that must be *chosen*, with the diff in hand.
"""

from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path
from typing import Any, cast

_SCRIPTS_DIR = str(Path(__file__).resolve().parent)
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

from diff_harness import (  # noqa: E402
    REPO_ROOT,
    HarnessError,
    Side,
    load_side,
    refuse,
    render_diff,
)

# The dependency closure this harness snapshots per revision. One entry today;
# it is spelled as a set rather than inlined because the closure is the unit
# `diff_harness` works in -- see its docstring for what snapshotting the file
# instead costs.
PARSER = "check_spec_links.py"
_SCRIPTS = (PARSER,)


def _repoint(side: Side) -> Side:
    """Point a snapshotted parser at the real repository.

    Each side is materialised into a staging directory, and `check_spec_links`
    derives `REPO_ROOT` from `__file__.parent.parent` at import — which is the
    staging directory, not this checkout. Left alone, `md_sources()` enumerates
    whatever markdown happens to sit beside the copies (nothing), so the harness
    compares the fixtures, reports "identical", and never touches the corpus it
    claims to cover. Measured: the run reported 16 inputs where it should have
    reported 16 plus every markdown file in the repository.

    That failure is silent and in the reassuring direction, which is the class
    this harness exists to catch — so it is corrected here rather than left to
    a reader to notice a suspiciously round input count.
    """
    # `cast` because these are module-level constants of a dynamically
    # imported module, which a static checker cannot know exist. The names are
    # load-bearing rather than incidental, so a rename in `check_spec_links`
    # must fail here.
    #
    # **It has to be checked, because plain assignment silently creates.** That
    # guarantee was credited to `test_repointing_reaches_the_real_corpus`, and
    # that test only ever saw one of the three names. Simulated per name --
    # repoint, then restore that one name to its staging-derived value --
    # `REPO_ROOT` made `md_sources()` return 0 files and reddened the test,
    # while `SPECS_DIR` and `PERSONAL_DIR` both left 131 files and the test
    # green. Neither is read by `md_sources` at call time, so a rename of either
    # would have left the harness comparing a differently-scoped corpus and
    # printing a reassuring "identical" -- this instrument's own failure class.
    # Demonstrated directly too: after `del parser.SPECS_DIR`, the assignment
    # re-created it.
    #
    # `SPECS_DIR` is not read anywhere in `check_spec_links` except at import
    # (`PERSONAL_DIR = SPECS_DIR / "personal"`), which has already run by the
    # time this does -- so its *assignment* is dead today. It is kept rather
    # than deleted because the guard below turns it into the live thing it was
    # only ever pretending to be: a rename detector for a name the module still
    # defines.
    #
    # Measured before adding the guard, because a refusal on a legitimately old
    # baseline would break the `--base <pre-adoption rev>` reproduction ADR-0061
    # prints: every revision of `check_spec_links.py` in this file's history
    # defines all three names, so this can only fire on a real rename.
    parser = cast(Any, side.module(PARSER))
    for name, value in (
        ("REPO_ROOT", REPO_ROOT),
        ("SPECS_DIR", REPO_ROOT / "specs"),
        ("PERSONAL_DIR", REPO_ROOT / "specs" / "personal"),
    ):
        if not hasattr(parser, name):
            raise HarnessError(
                f"{side.label}: {PARSER} has no {name} at "
                f"{side.rev or 'the working tree'}, so repointing would create "
                "a name nothing reads and compare a differently-scoped corpus"
            )
        setattr(parser, name, value)
    return side


# --------------------------------------------------------------------------
# The fixture corpus. One entry per shape that has actually changed behaviour,
# each paired with the boundary case next to it -- a fixture that only ever
# proves "code is skipped" is satisfied by an implementation that skips
# everything, so the pair is what makes either one discriminating.
# --------------------------------------------------------------------------

FIXTURES: dict[str, str] = {
    # The four shapes the markdown-it adoption changed.
    "fence-in-list-item": "1. Example:\n\n    ```\n    [a](x.md)\n    ```\n",
    "fence-in-nested-list": "- item\n  - sub:\n\n    ```\n    [a](x.md)\n    ```\n",
    "fence-tab-indented": "text:\n\n\t```\n\t[a](x.md)\n\t```\n",
    "indented-code-block": "text:\n\n    [a](x.md)\n\nafter [b](y.md)\n",
    "span-across-newline": "a `[a](x.md)\nstill in span` b\n",
    # The same span with a trailing run on its *interior* line. `.strip()` runs
    # once over the block's joined content, so only the first line's leading run
    # and the last line's trailing one ever leave it -- an interior trailer stays
    # in `token.content`. A per-line `rstrip()` therefore repairs the one
    # placement a hard break can never occupy and breaks the one it always does,
    # and the harness reported 148/148 identical across that swap because no
    # fixture carried the shape.
    "span-across-hard-break": "a `[a](x.md)  \nstill in span` b\n",
    # The boundaries none of that may cross.
    "span-across-blank-line": "a `[a](x.md)\n\nnot a span` b\n",
    "ordinary-fence": "text:\n\n```\n[a](x.md)\n```\n\nafter [b](y.md)\n",
    "long-fence-quoting-short": "````\n```\n[a](x.md)\n```\n````\n\n[b](y.md)\n",
    "unbalanced-backtick": "an ` unpaired tick [a](x.md)\n\n[b](y.md)\n",
    "plain-list": "# H\n\n- [a](x.md)\n- [b](y.md)\n",
    "escaped-opener": "\\[not a link](x.md) and [real](y.md)\n",
    "image-and-link": "![alt](img.png) and [real](y.md)\n",
    "crlf": "# H\r\n\r\n- [a](x.md)\r\n",
    "html-block-with-a-span": "<div>\n[a](x.md) and `[b](y.md)` here\n</div>\n",
    # The shape the span fixture beside it does not reach. An HTML block emits
    # ONE token, so no `fence` token exists inside it, nothing entered the
    # covered set, and the uncovered-line fallback stripped spans only -- which
    # has no fence rule. Every link in the fence was scanned as live prose, and
    # an example link does not resolve by definition, so it was a hard gate
    # failure on correct markdown. The differential instrument reported
    # "identical" over the shape because only the span half was fixtured.
    "html-block-with-a-fence": (
        "<div>\n```\n[a](x.md)\n```\n</div>\n\nafter [b](y.md)\n"
    ),
    "link-with-title": '[a](x.md "the title") and [b](y.md)\n',
    "anchor-only": "[a](#section) and [b](y.md)\n",
}


# The one input every run must parse correctly, checked on both sides before
# anything is compared. `_targets` turns a raise into a comparable string, so an
# environment failure -- a missing `markdown_it` being the measured one, reached
# by the bare `python` this module's own Usage block used to spell -- makes both
# sides return the SAME string and the run reports every input identical and
# nothing diverged, at exit 0, having parsed nothing. An expected value rather
# than merely "did not raise", because an empty link set is the other way a
# broken mask reports agreement.
#
# No fixture count in that sentence, and the omission is deliberate. It read
# "16 inputs, 16 identical, 0 diverged" while `FIXTURES` held 17 -- the hunk
# that wrote the sentence was the hunk that added the 17th entry, so it was
# false on arrival. That is the third instance of one shape on this work item
# (ADR-0061's own "16 adversarial fixtures", a "41 commits back" that was 42),
# and the lesson each time is the same: a count beside the thing it counts is a
# measurement with a shelf life. The past-tense siblings above and in the tests
# keep their "16", because those record what a specific run printed at a time
# when 16 was right -- rewriting them would falsify a record rather than repair
# a claim.
_SENTINEL_TEXT = "# H\n\nsee [a](x.md) and `[b](y.md)` here\n"
_SENTINEL_EXPECTED = ["line 3: x.md"]


def _sentinel_failure(side: Side) -> str | None:
    """Why `side` cannot be trusted to have parsed at all, or None if it can."""
    found = _targets(side, _SENTINEL_TEXT)
    if found != _SENTINEL_EXPECTED:
        return (
            f"{side.label} returned {found!r} for the sentinel, "
            f"expected {_SENTINEL_EXPECTED!r}"
        )
    return None


def _targets(side: Side, text: str) -> list[str]:
    """One side's link set for ``text``, as comparable lines.

    The module *lookup* sits outside the catch, and the separation is the
    point. `Side.module` raises `HarnessError` for a script absent at this
    revision -- a refusal, which the exit contract routes to 2 -- and the
    broad catch below turned it into the ordinary result string
    ``RAISED HarnessError: ...``. That string differs from whatever the other
    side returned, so `render_diff` reported a **divergence** and the run
    exited 1: the code reserved for "the revisions disagree", produced by a
    side that was never there to disagree. `Side.module`'s own docstring makes
    exactly this argument for preferring it over a bare field, and this call
    was the one place the argument did not hold.

    That path became reachable on this work item rather than being theoretical:
    the closure fix made `Side.module` the accessor every call site uses, so
    every one of them now has a `HarnessError` to keep out of the results.
    """
    parser = side.module(PARSER)
    try:
        found = parser.link_targets(text)
    except (Exception, SystemExit) as exc:
        # A raise is part of the observed behaviour -- turning a parse into a
        # refusal, or the reverse, is exactly what ships silently. Not
        # `BaseException`: that would record an operator's Ctrl-C as a result.
        return [f"RAISED {type(exc).__name__}: {exc}"]
    return [f"line {lineno}: {target}" for lineno, target in found]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Diff check_spec_links' link sets between two revisions, over "
            "adversarial fixtures and every markdown file in the repository."
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
        "--no-live",
        action="store_true",
        help="fixtures only; skip the repository's own markdown",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """The documented exit contract: 0 identical, 1 differ, 2 could not run.

    `HarnessError` is raised from inside `load_side` and from the live-corpus
    guard below, several of them from *inside* the sentinel check that runs
    before `_run`'s own returns could see them — so it is caught here, at the
    outermost point, and turned into 2 rather than the 1 a bare
    `SystemExit(<str>)` produces.
    """
    try:
        return _run(argv)
    except HarnessError as exc:
        return refuse(str(exc))


def _run(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    with tempfile.TemporaryDirectory(prefix="speclinks-diff-") as raw:
        staging = Path(raw)
        base = _repoint(load_side("base", args.base, staging, _SCRIPTS, _SCRIPTS))
        head = _repoint(load_side("head", args.head, staging, _SCRIPTS, _SCRIPTS))

        for side in (base, head):
            failure = _sentinel_failure(side)
            if failure is not None:
                return refuse(failure)

        cases: list[tuple[str, str]] = sorted(FIXTURES.items())
        skipped: list[str] = []
        if not args.no_live:
            # The *head* side enumerates, so a revision that changed the source
            # set is honoured rather than silently read through the old rule.
            #
            # Guarded, and the enumeration is then required to be non-empty --
            # both halves of one failure, of which only the parsing half was
            # closed. (a) The call sat outside every `try` while `_targets` one
            # line down caught exactly this shape for the other call: measured
            # against a revision whose `check_spec_links.py` has `link_targets`
            # but no `md_sources`, the run died with `AttributeError` at exit 1,
            # not the documented 2. `md_sources()` also raises `RuntimeError` on
            # a git failure down the same path. (b) An enumeration that returns
            # `[]` printed a full-agreement line over the fixtures alone, at exit
            # 0 and byte-identical to `--no-live` -- the failure `_repoint`'s own
            # docstring says must not be "left to a reader to notice a
            # suspiciously round input count".
            try:
                live_sources = sorted(head.module(PARSER).md_sources())
            except (AttributeError, RuntimeError) as exc:
                raise HarnessError(
                    f"the head side could not enumerate the live corpus: "
                    f"{type(exc).__name__}: {exc}"
                ) from exc
            if not live_sources:
                raise HarnessError(
                    "the head side enumerated no markdown files for the live "
                    "corpus, so a run over it would report agreement having "
                    "compared only the fixtures"
                )
            for path in live_sources:
                try:
                    cases.append((str(path), path.read_text(encoding="utf-8")))
                except (UnicodeDecodeError, OSError) as exc:
                    # Named, not dropped: a file the harness could not read is a
                    # file it did not compare, and silence there would read as
                    # agreement.
                    skipped.append(f"{path}: {type(exc).__name__}: {exc}")

        agreed = 0
        for label, text in cases:
            if render_diff(label, _targets(base, text), _targets(head, text)):
                agreed += 1

    for note in skipped:
        print(f"\nskipped (unreadable): {note}")
    diverged = len(cases) - agreed
    head_label = args.head or "worktree"
    print(
        f"\n{len(cases)} inputs, {agreed} identical, {diverged} diverged "
        f"({args.base} -> {head_label})"
        + (f"; {len(skipped)} unreadable" if skipped else "")
    )
    # A real divergence outranks an incomplete run, and this ordering is a
    # correction. The refusal used to sit ahead of this branch, defended as
    # "a run that both diverged and skipped is still an *incomplete* run" --
    # true, but the conclusion does not follow, because **only exit 0 claims
    # completeness**. Exit 1 claims a difference was found, which is exactly
    # what happened, and the incompleteness is printed above and counted in the
    # summary line either way.
    #
    # What the old order cost: a caller branching on the status read a real,
    # printed behaviour change as an environment failure -- the one status that
    # invites "retry, nothing to see". Reproduced against the committed
    # harness: with one live corpus file made to raise `OSError`, the run
    # printed `147 inputs, 141 identical, 6 diverged; 1 unreadable` -- the
    # counts ADR-0061 records as its own reproduction -- and then exited 2 as
    # "could not run". The transient `PermissionError` a concurrent editor
    # produces on Windows is not exotic here; it is the case the sibling gate's
    # `OSError` branch was added for.
    if diverged:
        return 1
    if skipped:
        # A file the harness could not read is a file it did not compare, so
        # the run did not cover the corpus it claims to. Naming it was not
        # enough: the summary still ended `0 diverged` at exit 0, which is the
        # answer a caller reading the status alone acts on -- and this module
        # reserves 2 for "could not run" precisely so an incomplete run is not
        # mistaken for a clean one. `_repoint`'s docstring makes the same point
        # about a silently reduced input count: it must not be left to a reader
        # to notice.
        #
        # Reached only when nothing diverged, which is the whole set of runs
        # where 0 would otherwise be returned -- so the property that motivated
        # the refusal is untouched: a clean bill of health still requires that
        # every input was actually read.
        return refuse(
            f"{len(skipped)} input(s) could not be read, so this run did not "
            "cover the whole corpus"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
