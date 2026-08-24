#!/usr/bin/env python3
r"""Verify that every relative link in the repo's markdown resolves to a file.

Mechanizes the docs-integrity half of the docs-consistency CI gate
([ADR-0045](../specs/adr/0045-repository-workflow-and-ci-enforcement.md) §6,
extended by ADR-0061): a cross-file link in tracked prose that points at a
moved or deleted target is a dead link, and inside an immutable Accepted ADR
even the corrective edit carries governance ceremony. The ADR-index check
(check_adr_index.py) guards the index *table*; this guards the prose *links*.
(The filename keeps its original ``spec_links`` name for reference stability;
the crawl was widened from specs/ to all tracked markdown by ADR-0061's
BRIEF-1 revision.)

Scope and rules:
  - Crawls every ``*.md`` git considers part of the repo -- tracked, plus
    untracked-but-not-ignored (``git ls-files --cached --others
    --exclude-standard``), so a brand-new doc is gated before it is ever
    staged -- EXCEPT specs/personal/ (gitignored, absent in CI). Outside a
    git checkout -- the unit suite's tmp trees -- it falls back to an rglob
    of the repo root, pruned of vendored trees, with the same personal/
    exclusion. A link *target* is validated wherever it resolves -- a file
    linking ``../../scripts/foo.py`` is checked too.
  - A link is inline ``[text](target)``; an image's ``![alt](target)`` target is
    validated the same way (a dead local image is a real defect). Only relative
    targets are checked: a URI scheme (``http(s):``, ``mailto:``, ``tel:`` -- two
    or more scheme chars, matched case-insensitively), a pure ``#anchor``, and a
    root-absolute (``/x``) or protocol-relative (``//host/x``) target are all
    skipped -- the last two are resolved by GitHub against the repo root, which
    this gate does not model. A ``#fragment`` is stripped before resolving --
    file existence is validated, anchors are not (a ``#L123`` line anchor cannot
    be checked against a moving file).
  - Targets that resolve under specs/personal/ are skipped (unvalidatable --
    the tree is gitignored), never reported.
  - Code is removed before scanning, so an example link quoted in code -- e.g.
    an arc42-cell reference written `` `[adr/](adr/)` `` -- is not mistaken for
    a live link. **What counts as code is decided by a real CommonMark parser**
    (markdown-it-py), not by regexes over single lines: fenced blocks at any
    indent including inside a list item, tab-led fences, indented code blocks,
    and inline code spans, which may cross a line break. Hand-rolled parsing
    lived here from this gate's inception and was corrected repeatedly, each
    time for a silent defect and each time in a commit that also carried the
    test written for it -- a list-indented fence that opened nothing, a longer
    fence quoting a shorter one, a span that stopped at the newline.
    ``code_masked_lines`` is the one place that decision is made, and it is
    public for that reason: the rule for a second checker over this corpus is to
    import it, never to grow a private copy.

    (No generation count is quoted, and that is a correction rather than an
    omission. The family read "four generations" / "a fifth private copy" in
    six places across the source and twice in ADR-0061 until a review measured
    it: ``git log --follow`` on this file shows **two**
    landed commits before the parser change -- the gate's inception and a
    scope widening that touched enumeration only -- so the number described
    the abandoned branch's own iteration rather than anything in this file's
    history. The defect *shapes* above are the part that was always checkable,
    and they are what the argument rests on.)

Not handled -- accepted limitations for this corpus, documented so a future
widening is deliberate rather than a surprise:
  - Reference-style links (``[text][ref]`` with a ``[ref]: target`` definition):
    the corpus uses none; only inline links are matched.
  - Two CommonMark link shapes are silently missed (neither is in the corpus):
    a backslash-escaped ``]`` in link text (``[a \] b](t.md)``), and the
    *inner* destination of the image-badge nesting
    ``[![alt](img.png)](target.md)`` -- the balanced-bracket ``LINK_RE``
    consumes the whole badge and ``finditer`` resumes past it, so ``img.png``
    is never re-scanned. **This entry named the other one until a review
    measured it**: it described the flat pattern's behaviour, which captured
    ``img.png`` and left ``target.md`` unmatched. Exactly one of the two is
    checked either way, and the balanced revision swapped which -- ADR-0061 §3
    records the same swap, and was corrected for it one revision earlier than
    this line was.
  - Targets containing an *unescaped* ``)``, a space, ``<...>`` wrapping, or a
    ``%20`` escape: the tree uses none; such a target is reported dead
    *loudly*, not skipped. A **backslash-escaped** ``\)`` is not in that set --
    ``LINK_RE``'s destination reads it as one character, as CommonMark does.
    It said ``)`` unqualified while the destination was ``[^)]+``, which
    truncated ``[t](foo\(bar\).md)`` to the fragment ``foo\(bar\`` and reported
    *that* dead: a target the document does not contain.
  - **A target only Windows would resolve is reported loudly on every
    platform**, rather than resolving locally and failing in CI: a backslash
    anywhere in the path, and a path component ending in a dot or space (Win32
    strips both). ``windows_only_spelling`` decides this on the *rendered*
    form -- CommonMark's backslash escapes resolved, ``Path`` not yet consulted
    -- and ``.``/``..`` are excluded explicitly so ``./x.md`` is not swept up.
    The escape step is load-bearing rather than tidy: asked of the raw text the
    rule fired on ``[t](a\_b.md)``, a link valid on every platform, and printed
    a remedy that breaks it. The corpus contains neither spelling; the gate's
    authoritative run is CI's ubuntu-only docs job, so local is the only place
    they would ever be seen. ADR-0061 §3 records the same rule.
  - Links split across a hard line wrap are scanned as ordinary prose. Link
    *matching* stays line-local even though the block structure no longer is,
    because that is what keeps a reported ``file:line`` pointing at the line a
    reader will open.
  - A link inside an **HTML block** is skipped rather than scanned. This entry
    said the opposite -- "links inside an HTML comment are scanned as ordinary
    prose" -- and the opposite was the approximation: CommonMark does not parse
    an HTML block's contents as markdown, so a target written inside one is not
    a link. Measured against the renderer: ``<!-- [a](x.md) -->``, a link after
    a bare ``<br>`` line, and a link on the line *following* ``</div>`` each
    render zero ``<a>`` tags and are each skipped here, while the same link one
    blank line later renders one and is checked. So this is a case where
    skipping silently is the correct verdict rather than a tolerated gap --
    there is no dead link to report.

    **Where the block ends depends on which kind it is, and this entry got that
    wrong in the direction that reads as reassurance.** It said an HTML block
    "runs to the next blank line rather than to its closing tag" without
    qualification. True of the type-6 and type-7 blocks the evidence beside it
    exercises -- ``<!-- -->`` aside, everything measured there was a ``<div>``
    or a ``<br>`` -- and false of CommonMark's **condition 1**: ``<pre>``,
    ``<script>``, ``<style>`` and ``<textarea>`` run to their *closing tag* and
    swallow blank lines whole. Measured::

        <pre>\n[a](x.md)\n\n[b](y.md)\n</pre>\n\n[c](z.md)\n  ->  [(7, 'z.md')]
        <div> in the same shape                              ->  [(4, 'y.md'),
                                                                  (7, 'z.md')]

    So ``y.md`` -- a full blank line past the opener -- is masked inside a
    ``<pre>`` fold and scanned inside a ``<div>`` one. The masking is right in
    both cases; it is the *claim* that was wrong, and wrong in the silent
    direction: a reader who believed it would expect a target inside a ``<pre>``
    fold to be checked, so one that later dies is never reported and the gate
    stays green. ``tests/test_check_spec_links.py`` pins both shapes.
  - Source *enumeration* is git truth (``git ls-files``), but target
    *existence* is still checked against the working-tree filesystem. In CI
    the two coincide, so the gate's authoritative run validates git truth; a
    *local* run may diverge -- an untracked linked file, or a case-only
    mismatch on a case-insensitive filesystem, passes locally but fails CI.
    CI is authoritative.

Exit code 0 when every link resolves; 1 with one line per problem found (a dead
link, or a file that could not be read or decoded); 2 when the check could not
run at all -- a missing ``markdown-it-py``, a ``git ls-files`` failure, a mask
that broke its own line invariant. The third code exists because a caller
reading only the exit status cannot otherwise tell "the corpus is wrong" from
"this never ran", and reporting the second as the first is how a gate that
checked nothing passes for one that found nothing.
All files are read as UTF-8.

**No longer stdlib-only.** Two dependencies, each recorded where it was taken:
a ``subprocess`` call to PATH-resolved git for source enumeration (ADR-0061's
revised tradeoff), and ``markdown-it-py`` for the CommonMark parse. The second
is why this gate runs under ``uv run`` rather than a bare ``python3``.
"""

from __future__ import annotations

import functools
import io
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # the parser itself is imported lazily, inside `_parser`
    from markdown_it import MarkdownIt

REPO_ROOT = Path(__file__).resolve().parent.parent
SPECS_DIR = REPO_ROOT / "specs"
PERSONAL_DIR = SPECS_DIR / "personal"

# Link text may contain balanced brackets -- CommonMark says so, and a flat
# `[^\]]*` matches nothing at all against one. Measured over this repository's
# markdown at adoption, because the sentence here first asserted the corpus
# wrote them and it does not: zero links with nested brackets, and the single
# image-badge nesting (`[![alt](img.png)](target.md)`) sits inside a code span
# in ADR-0061, so neither shape is live here today. This is fidelity bought
# ahead of need, and what makes it worth buying is the *direction* of the
# failure rather than its frequency: a link the pattern cannot see is a link
# the gate stops checking, so a target that dies later is never reported and
# the gate still reads green. One nesting level is what a regex can carry
# without ambiguity: the two branches start on disjoint character classes, so
# there is no backtracking cliff.
LINK_RE = re.compile(r"\[(?:[^\[\]]|\[[^\[\]]*\])*\]\(((?:\\.|[^)\\])+)\)")
# CommonMark's backslash escape: a backslash before an ASCII punctuation
# character produces that character literally and the backslash is gone by the
# time anything resolves the destination. This is the parser's list, not a
# subset of it, because the question asked below is "what did markdown-it
# render", and a shorter list answers it wrongly in the silent direction.
_ESCAPABLE = frozenset("!\"#$%&'()*+,-./:;<=>?@[\\]^_`{|}~")
_ESCAPE_RE = re.compile(r"\\(.)", re.DOTALL)
# A code span closes on a backtick run of the SAME length, guarded at both ends
# so a 1-backtick opener does not close on one backtick of a longer ``` run.
CODE_SPAN_RE = re.compile(r"(?<!`)(`+)(?!`)(?:.*?)(?<!`)\1(?!`)")
# A real [t](path "title") suffix. CommonMark spells a title three ways;
# accepting only the double-quoted one left the single-quoted form glued to the
# path, so `[a](x.md 'the title')` resolved against the literal target
# `x.md 'the title'` -- a filename that cannot exist, reported dead against a
# link that is alive. The third spelling -- a parenthesised title -- is
# deliberately absent rather than an oversight: `LINK_RE`'s destination is
# `[^)]+`, which stops at the title's own opening paren, so no such destination
# ever reaches here. It falls under the documented "a target containing `)` is
# reported dead loudly" limitation, and a branch nothing can reach is a claim
# no test can hold.
#
# **Escape-aware in the quoted run**, which the flat `[^"]*` was not: a title
# containing a CommonMark-escaped quote -- `[t](path.md "a\"b")`, which
# markdown-it renders as `href="path.md"` -- matched nothing, so the title was
# never stripped and the whole string became the path. Measured:
# `relative_path_part('path.md "a\"b"')` returned `path.md "a"b"`, a filename
# that cannot exist, reported dead against a live link. Same failure class as
# the single-quoted spelling this pattern was widened for one revision earlier,
# and found the same way -- by a reviewer asking whether a sibling claim was
# measured rather than plausible.
TITLE_RE = re.compile(r"""(\S+)\s+(?:"(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*')\Z""")
SCHEME_RE = re.compile(r"[a-zA-Z][a-zA-Z0-9+\-]+:")  # a URI scheme (2+ chars, no dot)
# What is left once SCHEME_RE has had its say: a one-letter "scheme", which on
# Windows is a drive-relative path. Anchored, so an editor-style `x.md:47`
# elsewhere in the target is untouched.
DRIVE_RELATIVE_RE = re.compile(r"[a-zA-Z]:")


def strip_code_spans(text: str) -> str:
    """Blank out inline code spans so a link quoted in code is not read as live.

    **Length-preserving, like every other blanking in this module.** It collapsed
    a whole span to one space until a review measured what that cost: this is the
    fallback `code_masked_lines` drops to whenever the block-wide pairing is not
    trustworthy, so a shortened line reaches every caller that reads the mask as
    a *column guide* to the source rather than as prose alone -- one that slices
    the real line at an offset the mask found. Shortening is the quiet direction
    there: the slice lands early, and what it drops is invisible to the caller.
    `code_masked_lines` now raises rather than returning a mask whose columns
    moved, and this is the routine that used to move them.
    """
    return CODE_SPAN_RE.sub(_blank_keeping_lines, text)


# The line breaks CommonMark recognizes, which are also the only three
# markdown-it normalizes. Deliberately not `str.splitlines()`, which additionally
# splits on vertical tab, form feed, the file/group/record separators, NEL, and
# the Unicode line and paragraph separators -- none of which starts a new line in
# CommonMark, so a document quoting any of them would number its lines
# differently from the parser and every reported `file:line` after it would be
# wrong.
LINE_BREAK_RE = re.compile(r"\r\n|\r|\n")
# A code span bounded to one block, so it may cross a newline. Same rule as
# CODE_SPAN_RE -- open on a run of N backticks, close on the next run of exactly
# N -- with DOTALL, which is safe only because the text handed to it is a single
# block rather than the document. An unterminated run therefore blanks the rest
# of its paragraph at worst, never the rest of the file.
#
# It is a *candidate* finder, never the decision: every match it proposes is
# checked against the parser's own `code_inline` tokens before anything is
# blanked (`_spans_match_parser`). Left to decide alone it was wrong in the
# silent direction -- measured, `see [a](x`y.md) and\n[b](nope.md) too `z``
# paired the backtick inside the *link destination* with the one opening the
# real span and blanked the dead `nope.md` out of existence, while markdown-it
# renders both as links; a backslash-escaped backtick, which CommonMark says
# opens nothing, did the same. A mask that contradicts the parser this module
# just adopted is worse than no mask, because it removes a finding rather than
# adding one.
_BLOCK_SPAN_RE = re.compile(r"(?<!`)(`+)(?!`)(?:.*?)(?<!`)\1(?!`)", re.DOTALL)


def _blank_keeping_lines(match: re.Match[str]) -> str:
    """The matched text as spaces, with its newlines kept.

    Both properties are load-bearing: the spaces keep every following column
    where it was, and the newlines keep every following *line* where it was.
    Blanking a newline away merges two source lines and shifts the rest of the
    file's numbering.
    """
    return "".join("\n" if ch == "\n" else " " for ch in match.group(0))


def _code_span_content(raw_inner: str) -> str:
    """A code span's raw inner text as CommonMark normalizes it.

    Line endings become spaces, then one space is stripped from each end when
    both ends carry one and the result is not all spaces. Reproduced here for
    one purpose: comparing a regex match against the `code_inline` token the
    parser produced, so the mask can be *checked* rather than trusted.
    """
    content = LINE_BREAK_RE.sub(" ", raw_inner)
    bounded = len(content) > 2 and content.startswith(" ") and content.endswith(" ")
    if bounded and content.strip():
        content = content[1:-1]
    return content


def _block_prefixes(raw_lines: list[str], inline: Any) -> list[int] | None:
    """How many leading characters the parser stripped from each block line.

    A list item, a nested list or a blockquote hands this module raw lines that
    still carry their `- ` / `  ` / `> ` markers, while the parser's own inline
    content has them removed. Comparing the two directly can therefore *never*
    agree inside one, so `_spans_match_parser` returned False for every
    multi-line span there and the block dropped to the line-local fallback, which
    cannot pair backticks across lines. Nothing was masked, and a link quoted in
    such a span was reported dead -- a blocking docs-consistency failure on
    correct markdown, in exactly the shape this repository writes most of its
    prose in. Measured before the fix: a plain paragraph returned no dead link
    and the list-item, blockquote and nested-list spellings of the same text each
    returned one.

    Returns None when the parser's content does not line up character for
    character with the raw lines -- a tab expanded, a lazy continuation, a line
    count that disagrees. The caller then keeps the line-local fallback rather
    than guessing at a column mapping: under-masking is a false positive a reader
    can see, while a wrong mapping would blank the wrong columns silently.
    """
    content_lines = str(getattr(inline, "content", "")).split("\n")
    if len(content_lines) != len(raw_lines):
        return None
    prefixes: list[int] = []
    last = len(raw_lines) - 1
    for index, (raw, content) in enumerate(zip(raw_lines, content_lines, strict=True)):
        # `rstrip()` before comparing, because markdown-it sets an inline
        # token's content with `.strip()`: a line with any trailing whitespace
        # therefore never ends with its own content, `None` came back, and the
        # block dropped to the line-local fallback that cannot pair backticks
        # across lines -- so a link inside a multi-line span was reported as a
        # dead link. Measured: `a `foo\n[bad](nope.md) bar` end  \n` returned
        # `[(2, 'nope.md')]` where the same text without the two trailing
        # spaces returned `[]`. Two trailing spaces are a legal CommonMark hard
        # break and a trailing tab or U+00A0 does it too, the last invisible to
        # MD009 -- and `spec-links` runs ahead of `markdown-lint`, so the author
        # met a blocking dead-link failure on correct markdown from the earlier
        # gate. That is the failure this function's docstring above says it
        # exists to prevent, reached by a sibling trigger.
        #
        # **Only the LAST line**, because `.strip()` is applied once to the
        # joined block rather than per line: an interior line keeps its whole
        # trailing run inside `content`, so `rstrip()`ing every line regressed
        # the exact shape the paragraph above says it repaired. A hard break is
        # by definition never a paragraph's last line, so the unconditional form
        # fixed only the placement that cannot occur and broke the one that
        # motivated it. Measured: `a `foo  \n[bad](nope.md) bar` end\n` --
        # the two trailing spaces on line 1 -- returned `[(2, 'nope.md')]`
        # under the unconditional `rstrip()` and `[]` both before it and here.
        # A leading run needs no such care: `.strip()` can only have taken it
        # from the first line, and the prefix arithmetic below absorbs it
        # wherever it was taken.
        #
        # The trailing run stays in the *body* rather than being dropped: the
        # prefix is measured against the stripped line, so `line[n:]` still
        # carries it and every length in the block is preserved.
        stem = raw.rstrip() if index == last else raw
        if not stem.endswith(content):
            return None
        prefixes.append(len(stem) - len(content))
    return prefixes


def _spans_match_parser(spans: list[re.Match[str]], inline: Any) -> bool:
    """Whether `spans` is exactly the code-span list the parser found.

    The regex scans left to right with no knowledge of the constructs that bind
    tighter than a code span, so it can pair backticks the parser never paired
    -- inside a link destination, or after a backslash escape. Agreement is
    tested on all three observable properties (count, delimiter run, normalized
    content) rather than count alone, because count alone passes the measured
    link-destination case: one spurious span and one real span are both one.
    """
    # `Any` throughout: markdown-it-py's `Token` is only imported under
    # TYPE_CHECKING here, and the parser itself is a deferred import, so a
    # static checker has no token type to reach for at this call site.
    children: list[Any] = list(getattr(inline, "children", None) or [])
    parsed = [child for child in children if str(child.type) == "code_inline"]
    if len(spans) != len(parsed):
        return False
    for match, child in zip(spans, parsed, strict=True):
        marker = match.group(1)
        if marker != str(child.markup):
            return False
        inner = match.group(0)[len(marker) : -len(marker)]
        if _code_span_content(inner) != str(child.content):
            return False
    return True


@functools.cache
def _parser() -> MarkdownIt:
    """The CommonMark parser, constructed once.

    Imported inside the function rather than at module scope so that a missing
    or broken dependency is a failure of the *call*, reachable by a caller's
    guard, rather than one that kills the process at import -- `main` below is
    that guard, and it is the whole reason a bare `python3` invocation of this
    gate names its problem instead of printing a traceback.
    """
    from markdown_it import MarkdownIt as _MarkdownIt

    return _MarkdownIt("commonmark")


def code_masked_lines(md_text: str) -> list[str]:
    """Every line of `md_text`, with code blanked and line positions preserved.

    The one place this repository decides what is code, and public for that
    reason: the rule for a second checker over the same corpus is to import
    this rather than keep a copy to drift from -- a private copy is what the
    history below cost. The return value has exactly one entry per source
    line, which is what keeps a reported `file:line` pointing where a reader
    will look.

    **Why a real parser.** The hand-rolled regexes this replaced were wrong in
    both directions and the errors were silent. `FENCE_OPEN_RE` admitted only a
    0-3 space indent, so a fence nested under a list item -- and a tab-led fence
    -- opened nothing, and every link inside such a block was scanned as live
    prose. In the other direction a code span could not cross a newline, so a
    span written across a line break stopped being a span. That shape is live
    here rather than hypothetical: measured over this repository's markdown at
    adoption, **12** code spans cross a line break.

    A companion figure -- lines carrying an odd backtick run -- is deliberately
    not quoted here as a bare number. It reads 27 excluding fence delimiters
    and 359 counting them, so it means nothing without the rule that produced
    it, and this sentence carried an unreproducible **57** until a review
    re-measured it against five different readings and reached none of them.

    **The multi-line span hazard, and why it is now affordable.** Tracking span
    state across lines was declined for years here because an unterminated
    backtick could swallow the rest of the file -- the exact failure the fence
    logic had already been hardened against. A block parser removes the hazard
    rather than accepting it: a span is blanked *within one block*, whose bounds
    the parser supplies, so the blast radius of an unpaired backtick is its own
    paragraph. Bounding it is not sufficient on its own, though, so every
    proposed span is also checked against the parser's `code_inline` tokens and
    the block drops to line-local blanking on any disagreement --
    `_spans_match_parser` records what that caught.

    Indented (four-space and tab) code blocks are masked too, which the regex
    walk never did -- CommonMark calls them code, and a link quoted in one is no
    more live than a link in a fence. HTML blocks are blanked for a different
    reason: their content is raw HTML rather than markdown, so a link written
    inside one is not a link at all.
    """
    lines = LINE_BREAK_RE.split(md_text)
    masked = list(lines)
    normalized = "\n".join(lines)
    # `parse` needs the normalized text so that its token maps index `lines`.
    tokens = _parser().parse(normalized)  # type: ignore[attr-defined]

    covered: set[int] = set()
    for token in tokens:
        if token.map is None:
            continue
        start, end = token.map
        if token.type in ("fence", "code_block", "html_block"):
            # Blanked to *spaces*, not to `""`. This function promises one entry
            # per source line, each as long as the line it describes, and a
            # caller reading the mask as a column guide -- slicing the real line
            # at an offset the mask found -- needs the second half as much as
            # the first. Collapsing a line to `""` broke it on 1029 lines across
            # 54 of this repository's 130 markdown files -- **the non-empty
            # lines in those blocks, which is what "broke" means here**: a line
            # already `""` is not shortened by being set to `""`. Counting the
            # blank ones too gives 1067, and a review that read the sentence
            # the second way reported the 1029 as stale. Both figures reproduce;
            # they answer different questions, so the sentence now says which
            # it asks. While the two
            # `RuntimeError` guards written to protect the invariant sit on the
            # inline branch below, which cannot violate it by construction --
            # so the rule was stated in two docstrings, guarded in the one place
            # it was safe, and broken everywhere else. Spaces cost nothing:
            # `LINK_RE` finds no link in a run of spaces either.
            #
            # An HTML block shares this branch for a different reason than the
            # two code types: its content is raw HTML rather than markdown, so
            # nothing quoted inside one -- including a whole fenced block, which
            # markdown-it emits no `fence` token for -- is a live link. It is
            # masked here rather than left to the fallback below because that
            # fallback strips spans only, so such a fence entered `covered`
            # nowhere and had every link in it scanned as live prose: a hard
            # gate failure on correct markdown.
            #
            # The motivating shape is a `<details>` fold written with **no
            # blank line** after `</summary>`, and this comment named "the
            # ordinary `<details>` fold" until a review measured the two.
            # Written the ordinary way -- a blank line after `</summary>` --
            # the parser emits `html_block`, a real `fence`, then `html_block`,
            # so the fence is masked by the branch above and the html rule is
            # not what saves it. Only the blank-line-free spelling collapses
            # the whole fold into one `html_block` with the fence inside it.
            for i in range(start, min(end, len(masked))):
                masked[i] = " " * len(masked[i])
                covered.add(i)
        elif token.type == "inline":
            # Blank spans across the whole block at once, then write the lines
            # back in place. Joining and re-splitting on "\n" is exact here:
            # `lines` came from LINE_BREAK_RE, so no entry contains one.
            end = min(end, len(masked))
            if start >= end:
                continue
            raw_lines = masked[start:end]
            # Strip whatever the parser stripped -- the list marker, the
            # blockquote's `> `, the continuation indent -- so the comparison
            # below sees the text the parser saw, then put it back column for
            # column. `_block_prefixes` records what comparing the raw lines
            # instead cost.
            prefixes = _block_prefixes(raw_lines, token)
            bodies = (
                raw_lines
                if prefixes is None
                else [line[n:] for line, n in zip(raw_lines, prefixes, strict=True)]
            )
            block = "\n".join(bodies)
            spans = list(_BLOCK_SPAN_RE.finditer(block))
            if prefixes is not None and _spans_match_parser(spans, token):
                block = _BLOCK_SPAN_RE.sub(_blank_keeping_lines, block)
                bodies_back = block.split("\n")
                # The line-count invariant, checked on what the blanking
                # *returned* rather than on what survived being paired with the
                # source lines. It used to be checked after the pairing, and the
                # pairing was `zip(..., strict=False)`, so one of the two
                # directions went straight through: `zip` truncates to its
                # shortest input, so a blanking that came back with too many
                # lines still produced exactly one entry per source line, the
                # count matched, and the block's tail vanished from the mask
                # with every remaining line still the right length. That is the
                # under-masking direction, and under-masking is not free -- a
                # link left unmasked is reported, and an example link does not
                # resolve by definition, so it fails a blocking gate on correct
                # markdown. Too *few* lines the old placement did catch.
                #
                # Raising rather than silently declining, which is the older
                # half of this guard's history: the first version of this code
                # replaced a multi-line span with spaces *including* its
                # newlines, merging two source lines into one, and an `if count
                # matches` guard then skipped the write-back and left the
                # cross-line span unmasked -- the exact half of the defect the
                # parser change exists to fix, hidden by the guard meant to make
                # it safe. Every reported `file:line` past this point depends on
                # the invariant, so a violation is not survivable.
                #
                # `_blank_keeping_lines` preserves newlines by construction, so
                # this is reachable only by injection -- and so was the earlier
                # form, whose historical defect is the reason "by construction"
                # is checked here rather than trusted.
                if len(bodies_back) != len(raw_lines):
                    raise RuntimeError(
                        f"code masking changed the line count of block "
                        f"{start}-{end}: {len(bodies_back)} lines back for "
                        f"{len(raw_lines)}"
                    )
                rewritten = [
                    line[:n] + body
                    for line, n, body in zip(
                        raw_lines, prefixes, bodies_back, strict=True
                    )
                ]
            else:
                # The candidates disagree with the parser, or the block's
                # columns could not be mapped. Either way the block-wide pairing
                # is not trustworthy here, so fall back to the line-local rule,
                # which cannot reach across a line to blank a link on another
                # one. Note what that costs, because the note this replaces had
                # it backwards: under-masking is *visible*, not free. A link left
                # unmasked is reported, and an example link -- which does not
                # resolve by definition -- then fails a blocking gate on correct
                # markdown. `_block_prefixes` exists because that was the routine
                # case rather than the exotic one. `raw_lines`, not the dedented
                # bodies: a fallback that dropped the list markers would shorten
                # every line in the block.
                rewritten = [strip_code_spans(line) for line in raw_lines]
            # No second line-count check here, and its absence is deliberate
            # rather than an oversight. One stood at this point and could see
            # only one of the two ways the count can be wrong, because it
            # counted what came *back* from a `zip` that had already truncated
            # the discrepancy away. Moving it above the pairing is what makes it
            # two-directional; leaving a copy here would be a second, weaker
            # encoding of the same invariant. Neither branch can reach it any
            # more in any case: above, the count is checked and then paired
            # `strict=True`; here, the comprehension is one entry per line in
            # `raw_lines` by construction.
            #
            # The same invariant one axis over, and it went unchecked while the
            # line count did not. A caller that reads the mask as a *column*
            # guide to the source -- one that slices the real line at an offset
            # the mask found -- needs each masked line to be exactly as long as
            # the line it describes, and the span-only fallback silently
            # shortened it for years. Raised rather than repaired for the same
            # reason as above:
            # every column derived from the mask past this point depends on it.
            for new, old in zip(rewritten, raw_lines, strict=True):
                if len(new) != len(old):
                    raise RuntimeError(
                        f"code masking changed a line length in block "
                        f"{start}-{end}: {len(new)} chars back for {len(old)}"
                    )
            masked[start:end] = rewritten
            covered.update(range(start, end))

    # Any line the parser attributed to no token handled above -- a link
    # reference definition, a table cell in a dialect it does not model -- keeps
    # the old line-local span blanking, so widening the parser never silently
    # *narrows* what counts as code. HTML blocks used to be this case's headline
    # example and are now handled above, because span-only stripping is not a
    # sound fallback for a region that can contain a whole fence.
    for i, line in enumerate(masked):
        if i not in covered and line:
            masked[i] = strip_code_spans(line)
    return masked


def link_targets(md_text: str) -> list[tuple[int, str]]:
    """Return (line number, raw target) for every markdown link outside code."""
    targets: list[tuple[int, str]] = []
    for lineno, stripped in enumerate(code_masked_lines(md_text), start=1):
        for match in LINK_RE.finditer(stripped):
            # An escaped opener (\[...]) is not a link. An odd run of backslashes
            # before the [ escapes it; an even run escapes the backslashes
            # themselves, leaving the [ live.
            before = stripped[: match.start()]
            if (len(before) - len(before.rstrip("\\"))) % 2 == 1:
                continue
            targets.append((lineno, match.group(1)))
    return targets


def unescape_link_destination(path_part: str) -> str:
    """A link destination with CommonMark's backslash escapes resolved.

    What the renderer produces, so that every question asked of a destination
    downstream -- does it spell something only Windows resolves, and does it
    exist -- is asked of the path a reader's browser will follow rather than of
    the characters the author typed to write it.

    Skipping this was a false positive in the loudest possible form.
    ``windows_only_spelling`` returned a reason for ANY backslash, so
    ``[t](a\\_b.md)`` -- which markdown-it renders as ``<a href="a_b.md">``, the
    same file on every platform -- was reported dead, **and the printed remedy
    ("write it with `/`") turns it into a genuinely dead link**. A gate that
    fails on correct markdown is bad; one whose advice breaks the input is
    worse.

    An escaped *backslash* survives on purpose: ``a\\\\b.md`` renders with a real
    backslash in the destination, which is exactly the Windows-only spelling the
    caller is looking for. Resolving the escapes is what tells the two apart --
    a rule over the raw text cannot, because both are "a backslash".
    """
    return _ESCAPE_RE.sub(
        lambda m: m.group(1) if m.group(1) in _ESCAPABLE else m.group(0),
        path_part,
    )


def windows_only_spelling(path_part: str) -> str | None:
    """Why this target would resolve differently on Windows than on Linux.

    Returns the reason, or None when the spelling means the same thing on both.

    The gate's authoritative run is CI's ubuntu-only docs job, so a spelling
    Windows resolves and Linux does not passes every local gate and reddens
    `ci-ok` — the same local-green/CI-red shape the `DRIVE_RELATIVE_RE` guard
    above was written for, in the spellings that guard does not reach.
    Measured on Windows, both resolving with ``exists() == True`` while naming
    a file that does not exist on Linux::

        adr\\0061-markdown-link-check-gate.md    backslash read as a separator
        adr/0061-markdown-link-check-gate.md.    trailing dot stripped by Win32

    Reported *loudly* rather than skipped, which is the direction this module
    already takes for a target it cannot resolve honestly: neither spelling is
    a valid relative link on the platform that decides the verdict, so calling
    them dead is not a false positive. The corpus contains none today — the
    same "absent today" standing on which ``LINK_RE``'s comment argues fidelity
    is worth buying ahead of need.

    **That justification is true only of a backslash the renderer keeps**, and
    the sentence above stood while the code asked a question it does not
    survive. A CommonMark backslash *escape* means the same thing on every
    platform — ``[t](a\\_b.md)`` renders ``<a href="a_b.md">`` — so reporting it
    was a false positive of exactly the kind that paragraph disclaims, and the
    remedy it printed ("write it with ``/``") turns a live link into a dead one.
    The caller resolves the escapes before asking
    (``unescape_link_destination``), which is what makes the argument above hold
    again: what arrives here is the rendered destination, where a backslash
    really is a separator Linux will not read. ADR-0061 §3 carried the same
    sentence and is corrected with it.

    ``.`` and ``..`` are excluded explicitly: they end in a dot and are the
    ordinary way a relative link is written, so a rule that merely looked for a
    trailing dot would reject every ``./x.md`` in the repository.
    """
    if "\\" in path_part:
        return "backslash in a link target (write it with `/`)"
    for component in path_part.split("/"):
        if component in (".", ".."):
            continue
        if component.endswith((".", " ")):
            return "a path component ending in a dot or space"
    return None


def relative_path_part(target: str) -> str | None:
    """The path part of a checkable relative link, or None when there is none.

    Split out of `resolve_target` so `check()` can ask what a target *spells*
    before asking what it resolves to: `windows_only_spelling` has to see the
    written form, and by the time `Path` has normalized it the divergence it
    looks for is already gone.
    """
    stripped = target.strip()
    if not stripped:
        return None
    # Drop a trailing link title -- [t](path "title") -- but only when one is
    # actually present, so a path containing a space is not silently truncated
    # to its first token (it is reported dead loudly instead).
    title = TITLE_RE.match(stripped)
    url = title.group(1) if title else stripped
    # Resolve CommonMark's backslash escapes here, so that **everything** below
    # -- the anchor and scheme tests, the fragment split, the two skip guards,
    # `windows_only_spelling`, and `resolve_target`'s existence check -- asks
    # its question of the destination the renderer produces. Unresolved,
    # `[t](a\_b.md)` was reported as a backslash divergence against a link
    # valid on every platform, and had the divergence rule not caught it first
    # the *existence* check would have reported the same link dead for a
    # filename with a backslash in it.
    #
    # **Ahead of the fragment split, and that placement is a correction.** It
    # sat just below the split, so the one step the sentence above exempted was
    # the one a smoke round found broken: `[t](foo\#bar.md)` was cut at the raw
    # `\#` and came back as `foo\` -- a trailing backslash that nothing put
    # there, which `windows_only_spelling` then reported as a Windows-only
    # spelling. Measured against the renderer, markdown-it gives that link and
    # its unescaped twin the *same* href, `foo#bar.md`, so the correct answer is
    # `foo` for both; a rule stated as "everything below" has to mean it.
    #
    # Ahead of the two skip guards for the same reason, so `\/x.md` and
    # `c\:x.md` are recognized as the root-absolute and drive-relative
    # spellings they render as -- and ahead of the anchor and scheme tests, so
    # `\#foo` reads as the pure anchor it renders to and `http\://x` as the URI
    # it renders to. None of these is in the corpus; the ordering is free and
    # the alternative is a guard reading text the renderer has already changed.
    #
    # The title strip stays *above* this, and the reason is now measured rather
    # than plausible -- a reviewer asked, and the first version of this sentence
    # did not survive being asked. It claimed a `\"` would "end the title early"
    # under the flat `[^"]*` pattern, and that was false: on `path.md "a\"b"`
    # the flat pattern matched *neither* spelling, raw or decoded, so the order
    # made no difference and the title went unstripped either way. Fixing
    # `TITLE_RE` to be escape-aware is what makes the ordering matter, and now
    # it demonstrably does: escape-aware, the pattern captures `path.md` from
    # the raw form and matches nothing at all once the escape has been resolved
    # to a bare quote. So decoding first would reintroduce exactly the defect
    # that fix removes.
    url = unescape_link_destination(url)
    if url.startswith("#") or SCHEME_RE.match(url):
        return None
    path_part = url.split("#", 1)[0]  # drop the #fragment; anchors are not checked
    # A root-absolute (/specs/...) or protocol-relative (//host/x) target is not
    # a checkable relative link: GitHub resolves the former against the repo
    # root, which this gate does not model, so skip it rather than false-report.
    # (path_part is otherwise always non-empty here -- url is non-empty and not
    # #-leading -- so no empty-string guard is needed.)
    if path_part.startswith("/"):
        return None
    # A *leading* single letter and colon is a one-character scheme or a Windows
    # drive-relative path (`c:CLAUDE.md`), and neither is a checkable relative
    # link. Unguarded, `Path` reads the drive letter as part of the name on
    # POSIX and as a *drive* on Windows, so the same target resolved two ways:
    # measured, `c:CLAUDE.md` became `specs\CLAUDE.md` here and passed, while
    # CI's Linux legs reported it dead.
    #
    # Anchored, deliberately. Rejecting *every* colon is the simpler rule and
    # the wrong one here, because an ordinary repo link carries them: the
    # editor-style `data-model.md:47` must still resolve, and a bare
    # `":" in path_part` made it skip silently, which
    # `test_resolve_target_does_not_skip_pathlike_colon` exists to forbid.
    if DRIVE_RELATIVE_RE.match(path_part):
        return None
    return path_part


def _resolves_into_personal(source: Path, path_part: str, resolved: Path) -> bool:
    """Whether this link points into specs/personal/ under *either* separator.

    `resolved` alone is not enough, and the platform decides which way it is
    wrong. `Path` reads a backslash as a separator on Windows and as an
    ordinary filename character on Linux, so `personal\\notes.md` resolves under
    PERSONAL_DIR on one leg and to a single oddly-named file in `specs/` on the
    other. The gate's authoritative run is the Linux one, which is the leg where
    the containment skip would not fire.

    So the backslash spelling is normalized and asked a second time. Answering
    "yes" on either spelling is the safe direction here in a way it usually is
    not: the cost of a false yes is one unvalidatable link going unreported --
    which is what the skip does for every personal target anyway, the tree being
    gitignored and absent in CI -- and the cost of a false no is printing a
    personal filename to stdout.
    """
    if resolved.is_relative_to(PERSONAL_DIR):
        return True
    if "\\" not in path_part:
        return False
    slashed = Path(os.path.normpath(source.parent / path_part.replace("\\", "/")))
    return slashed.is_relative_to(PERSONAL_DIR)


def resolve_target(source: Path, target: str) -> Path | None:
    """Absolute (``..``-normalized, symlink-preserving) path a relative link
    points at, or None if it is not a checkable relative file link."""
    path_part = relative_path_part(target)
    if path_part is None:
        return None
    return Path(os.path.normpath(source.parent / path_part))


# Directories the non-git fallback walk must never enter: vendored and
# generated trees whose *.md are not this repo's prose. The git branch needs no
# such list -- git's ignore rules already exclude them -- which is the ADR's
# whole argument for git truth; this keeps the fallback from reintroducing the
# hazard on the one path git cannot police.
FALLBACK_PRUNE = frozenset(
    {".git", ".venv", ".venv-wsl", "node_modules", "__pycache__"}
)
# Seconds before a git call is treated as hung. Matches `diff_harness`, the
# sibling that grew one at the same time.
_GIT_TIMEOUT = 30


def md_sources() -> list[Path]:
    """Every markdown file the gate crawls, sorted for deterministic output.

    Git truth when REPO_ROOT is a checkout (``.git`` is a directory in a normal
    clone and a *file* in a linked worktree, so ``exists()`` covers both):
    ``--cached`` is the tracked set, ``--others --exclude-standard`` adds files
    written but not yet added -- /land runs this gate *before* staging, and a
    brand-new doc is exactly where a dead link is most likely -- while git's
    ignore rules keep ``.venv/`` and scratch out without a prune list. Outside
    a checkout (the unit suite's tmp trees) an rglob of REPO_ROOT stands in,
    pruned of the vendored trees git would have excluded.

    The ``is_file()`` filter drops a tracked path deleted from the working tree
    (``--cached`` still names it; there are no bytes to scan, and the dead
    links *in other files that pointed at it* are what the gate should report).
    The set dedupes an unmerged path, which ``ls-files`` names once per merge
    stage. Both branches exclude specs/personal/ via the explicit PERSONAL_DIR
    filter below -- that filter is the guard, and it holds even for a
    force-added tracked personal file, which ``--cached`` would list;
    gitignore only makes that case abnormal, it is not the exclusion
    mechanism.
    """
    if (REPO_ROOT / ".git").exists():
        try:
            proc = subprocess.run(
                [  # noqa: S607 - PATH-resolved git, as every repo tool runs it
                    "git",
                    "ls-files",
                    "--cached",
                    "--others",
                    "--exclude-standard",
                    "-z",
                    "--",
                    "*.md",
                ],
                cwd=REPO_ROOT,
                capture_output=True,
                text=True,
                encoding="utf-8",
                # surrogateescape, not strict and not "replace": `-z` output is
                # raw filename bytes, and a non-UTF-8 name (creatable on POSIX
                # filesystems) must neither crash the gate (strict) nor be
                # corrupted into a path that resolves to nothing and silently
                # drops the file from the scan (replace). This is os.fsdecode's
                # own convention, so the escaped path round-trips back to the
                # real file.
                errors="surrogateescape",
                check=False,
                # A hung git fails the gate rather than hanging it. Found as a
                # peer of the same omission in `diff_harness._git_show`.
                # `/land` runs this gate, so a hang *here* stops a landing with
                # no output rather than reporting anything.
                #
                # **No enumeration of the sibling runners here, and that is a
                # correction.** This comment named three as covered and was
                # wrong about the third; the correction that replaced it named
                # `ledger`'s two as the only remaining gap and was wrong in the
                # same direction, because `run_gates._tracked_markdown` was a
                # sixth runner neither reading found -- and that one hangs a
                # landing too. A count of a family that lives in several files
                # is not checkable by reading one of them, so it is now
                # `tests/test_git_runners.py`, which enumerates by scanning and
                # cannot go stale between edits.
                timeout=_GIT_TIMEOUT,
            )
        except OSError as exc:  # git missing, or not executable
            # `RuntimeError`, so `main()`'s guard converts it to exit 2. Both
            # this and the timeout below escaped that guard: `FileNotFoundError`
            # is not a `RuntimeError` and `TimeoutExpired` is a
            # `SubprocessError`, so the module docstring's promise that exit 2
            # covers "a `git ls-files` failure" was prose the code did not keep
            # -- the gate died at exit 1 with a traceback, which a caller
            # reading the status alone cannot tell from a real dead link.
            raise RuntimeError(f"could not run `git ls-files`: {exc}") from exc
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(
                f"`git ls-files` did not return within {_GIT_TIMEOUT}s"
            ) from exc
        if proc.returncode != 0:
            # Attach git's own stderr: a bare CalledProcessError says only
            # "exit status 128", which blames this gate for a git failure.
            raise RuntimeError(
                f"git ls-files failed (exit {proc.returncode}): {proc.stderr.strip()}"
            )
        files = [REPO_ROOT / p for p in proc.stdout.split("\0") if p]
    else:
        files = [
            p
            for p in REPO_ROOT.rglob("*.md")
            if not FALLBACK_PRUNE.intersection(p.relative_to(REPO_ROOT).parts)
        ]
    return sorted(
        {p for p in files if p.is_file() and not p.is_relative_to(PERSONAL_DIR)}
    )


def check() -> list[str]:
    errors: list[str] = []
    md_files = md_sources()
    for source in md_files:
        rel = source.relative_to(REPO_ROOT).as_posix()
        try:
            text = source.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            errors.append(f"{rel}: not valid UTF-8 (cannot check links)")
            continue
        except OSError as exc:
            # This read site guarded `UnicodeDecodeError` and not `OSError`, and
            # the gap is reachable in the ordinary way: `md_sources()` filters on
            # `is_file()` and the read happens later, over a set that includes
            # untracked files, so a concurrent editor rewriting one makes a
            # `PermissionError` or `FileNotFoundError` between the two routine
            # on Windows. `main()` catches only `ImportError`, so the gate
            # otherwise died with a traceback at exit 1 -- to a caller reading
            # the exit status alone, indistinguishable from a real dead link,
            # which is the confusion that guard exists to prevent.
            #
            # Two clauses rather than one: the two failures want different
            # sentences, and the UTF-8 one is pinned by a test.
            errors.append(f"{rel}: could not be read ({type(exc).__name__})")
            continue
        for lineno, target in link_targets(text):
            path_part = relative_path_part(target)
            if path_part is None:
                continue
            resolved = resolve_target(source, target)
            if resolved is None:  # pragma: no cover - path_part is not None
                continue
            # **The personal skip runs ahead of every error branch**, which is
            # what `md_sources`' docstring means by calling the PERSONAL_DIR
            # filter "the guard". The Windows-spelling report below was the
            # first error path written *past* it, and it printed a personal
            # filename to stdout and into CI's docs-consistency log --
            # CLAUDE.md counts the provenance of the owner's records as
            # personal with no values attached, so a filename is enough. The
            # forward-slash sibling of the same link was skipped correctly
            # throughout, which is why nothing noticed.
            if _resolves_into_personal(source, path_part, resolved):
                continue
            # Asked of the written form (escapes resolved, `Path` not yet
            # consulted). A spelling only Windows resolves passes every local
            # gate and reddens CI's ubuntu-only docs job, so it is reported here
            # rather than left to the platform to disagree about.
            divergent = windows_only_spelling(path_part)
            if divergent is not None:
                errors.append(f"{rel}:{lineno}: {divergent} -> {target}")
                continue
            if not resolved.exists():
                errors.append(f"{rel}:{lineno}: dead link -> {target}")
    return errors


def main() -> int:
    """0 every link resolves, 1 something is wrong with the corpus, 2 the check
    could not run.

    **Exit 2 is the point of the guard below, and it used to return 1.** The
    comment justified the guard as: unguarded, the failure "exits 1 with a
    traceback, which a caller reading the exit status alone cannot tell from a
    real dead link" -- and then returned 1, leaving that caller in exactly the
    same position with only stdout changed. `diff_harness.refuse`, added to
    this repository in the same change, states the rule it was missing: a run
    that checked nothing must be distinguishable from one that found a real
    difference.

    `RuntimeError` joins `ImportError` for the same reason rather than by
    analogy: `md_sources` raises it when `git ls-files` fails, and
    `code_masked_lines` raises it on a mask that changed a line's length or
    count. All three mean the check did not run; none of them is a dead link.
    """
    try:
        errors = check()
    except (ImportError, RuntimeError) as exc:
        # The other half of `_parser()`'s deferred import, which is justified as
        # "reachable by a caller's guard": this is that caller. A missing
        # `markdown-it-py` is what a bare `python3` invocation of this gate
        # produces, since only `uv run` supplies the project environment.
        print(f"spec link check could not run: {type(exc).__name__}: {exc}")
        print("Run the gate through `python3 scripts/run_gates.py spec-links`.")
        return 2
    if errors:
        # "problem(s)", not "dead link(s)". `check()` also collects files it
        # could not read or decode, and counting those as dead links reports a
        # number for something that was never parsed. Each line below says
        # which it is.
        print(f"spec link check failed ({len(errors)} problem(s)):")
        for e in errors:
            print(f"  - {e}")
        return 1
    print("spec links consistent: every relative link in repo markdown resolves.")
    return 0


if __name__ == "__main__":
    # Inside the `__main__` guard on purpose, and neither at module scope nor
    # in `main()` -- scripts/check_personal_containment.py documents the
    # measured reasons (pytest's capture streams are TextIOWrapper subclasses,
    # so the other placements reconfigure the test session's own streams).
    # Needed here because a reported path can carry surrogate-escaped filename
    # bytes (see md_sources), and whether printing one crashes depends on the
    # interpreter's UTF-8 mode -- measured: tolerated under WSL's UTF-8 mode,
    # strict-locale Pythons raise UnicodeEncodeError, the gate crashing
    # instead of naming the very file it found.
    for stream in (sys.stdout, sys.stderr):
        if isinstance(stream, io.TextIOWrapper):
            stream.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main())
