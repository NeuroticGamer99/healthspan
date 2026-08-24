"""Unit tests for the spec link-check gate (scripts/check_spec_links.py).

Two layers: the pure extractors (strip_code_spans, link_targets, resolve_target)
against constructed strings, and check()/main() against a throwaway specs tree
under tmp_path -- never specs/, so a test link can never depend on real repo
layout, and a real dead link in the repo is caught by the gate itself, not here.
"""

import os
import re
import subprocess
from pathlib import Path

import check_spec_links as csl
import pytest

# --- pure extractors ------------------------------------------------------


def test_strip_code_spans_removes_single_and_multi_backtick() -> None:
    assert "x" in csl.strip_code_spans("x `[a](b)`")
    assert "[a](b)" not in csl.strip_code_spans("x `[a](b)`")
    # A double-backtick span (which may itself contain a backtick) is stripped.
    assert "[a](b)" not in csl.strip_code_spans("``[a](b)``")


def test_link_targets_finds_inline_links() -> None:
    text = "see [one](a.md) and [two](../b/c.md#frag)\n"
    assert csl.link_targets(text) == [(1, "a.md"), (1, "../b/c.md#frag")]


def test_link_targets_skips_inline_code_span() -> None:
    # The arc42-cell case: a link quoted in code is documentation, not a link.
    text = "the row now reads `[adr/](adr/)` with no number\n"
    assert csl.link_targets(text) == []


def test_link_targets_skips_fenced_code_block() -> None:
    text = "before [live](x.md)\n```\n[fenced](nope.md)\n```\nafter [also](y.md)\n"
    assert csl.link_targets(text) == [(1, "x.md"), (5, "y.md")]


def test_resolve_target_skips_external_and_anchor(tmp_path: Path) -> None:
    src = tmp_path / "doc.md"
    assert csl.resolve_target(src, "https://example.com") is None
    assert csl.resolve_target(src, "mailto:x@y.z") is None
    assert csl.resolve_target(src, "#section") is None
    assert csl.resolve_target(src, "") is None


def test_resolve_target_strips_fragment_and_title(tmp_path: Path) -> None:
    src = tmp_path / "sub" / "doc.md"
    assert csl.resolve_target(src, "../other.md#L12") == Path(
        os.path.normpath(tmp_path / "other.md")
    )
    assert csl.resolve_target(src, 'sibling.md "a title"') == Path(
        os.path.normpath(tmp_path / "sub" / "sibling.md")
    )


# --- check() / main() over a throwaway specs tree -------------------------


def _repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the module's globals at a tmp repo and return its specs/ dir."""
    specs = tmp_path / "specs"
    (specs / "personal").mkdir(parents=True)
    monkeypatch.setattr(csl, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(csl, "SPECS_DIR", specs)
    monkeypatch.setattr(csl, "PERSONAL_DIR", specs / "personal")
    return specs


def test_check_passes_on_resolving_links(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    specs = _repo(tmp_path, monkeypatch)
    (specs / "target.md").write_text("t", encoding="utf-8")
    (tmp_path / "CLAUDE.md").write_text("c", encoding="utf-8")
    (specs / "doc.md").write_text(
        "see [t](target.md) and [root](../CLAUDE.md) and [ext](https://x.io)\n",
        encoding="utf-8",
    )
    assert csl.check() == []
    assert csl.main() == 0


def test_check_reports_dead_link(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    specs = _repo(tmp_path, monkeypatch)
    (specs / "doc.md").write_text("see [gone](moved/away.md)\n", encoding="utf-8")
    errors = csl.check()
    assert len(errors) == 1
    assert "specs/doc.md:1" in errors[0]
    assert "moved/away.md" in errors[0]
    # The per-line label, not just the target. `main`'s summary deliberately
    # says "problem(s)" because `check()` also collects files it could not
    # read, and that distinction is only meaningful if the per-line text says
    # which kind each one is. Measured: replacing this wording left the whole
    # file green, because every other assertion here checks only the target.
    assert "dead link ->" in errors[0]
    assert csl.main() == 1


def test_check_skips_links_into_personal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A link into the gitignored, CI-absent tree must not be reported dead.
    specs = _repo(tmp_path, monkeypatch)
    (specs / "doc.md").write_text(
        "raw corpus in [inv](personal/source-inventory.md)\n", encoding="utf-8"
    )
    assert csl.check() == []


def test_check_does_not_crawl_personal_sources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A dead link that lives *inside* specs/personal/ is never scanned.
    specs = _repo(tmp_path, monkeypatch)
    (specs / "personal" / "notes.md").write_text(
        "[dead](../nonexistent.md)\n", encoding="utf-8"
    )
    assert csl.check() == []


def test_check_resolves_directory_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    specs = _repo(tmp_path, monkeypatch)
    (specs / "reviews").mkdir()
    (specs / "doc.md").write_text("see [dir](reviews/)\n", encoding="utf-8")
    assert csl.check() == []


def test_check_skips_personal_dir_target_when_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Mirrors CI, where specs/personal/ is gitignored and ABSENT. A link to the
    # personal directory itself -- specs/README.md's `[personal/](personal/)` --
    # must be skipped by the `resolved == PERSONAL_DIR` branch, not reported dead
    # because the directory does not exist. _repo() creates personal/, which is
    # exactly what masks this on a dev machine (and in the other personal tests),
    # so remove it first to reproduce the CI condition the branch exists for.
    specs = _repo(tmp_path, monkeypatch)
    (specs / "personal").rmdir()
    assert not (specs / "personal").exists()
    (specs / "doc.md").write_text("data lives in [p](personal/)\n", encoding="utf-8")
    assert csl.check() == []


def test_check_reports_missing_directory_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The directory case is not special-cased away: a trailing-slash target that
    # does not exist is still a dead link (guards against a future skip-by-shape).
    specs = _repo(tmp_path, monkeypatch)
    (specs / "doc.md").write_text("see [gone](missing-dir/)\n", encoding="utf-8")
    errors = csl.check()
    assert len(errors) == 1
    assert "missing-dir/" in errors[0]


def test_check_reports_every_dead_link(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The docstring promises one line per dead link; prove check() accumulates
    # rather than stopping at the first hit.
    specs = _repo(tmp_path, monkeypatch)
    (specs / "doc.md").write_text(
        "[a](gone-a.md) and [b](gone-b.md)\n", encoding="utf-8"
    )
    errors = csl.check()
    assert len(errors) == 2
    # Same-line links resolve left-to-right, so the order is deterministic;
    # two plain asserts pinpoint a miss where a set comparison would not.
    assert "gone-a.md" in errors[0]
    assert "gone-b.md" in errors[1]


# --- regression tests from an /apply-review pass (findings 1, 4, 6, 7) -----


def test_link_targets_longer_fence_quotes_shorter_without_inverting() -> None:
    # Finding 1: a 4-backtick fence quoting a 3-backtick example. A naive toggle
    # would treat the single inner ``` as a close and invert, silently skipping
    # every link after the block (a false negative -- the dangerous direction).
    text = "start [a](one.md)\n````\n```python\n````\nend [b](two.md)\n"
    assert csl.link_targets(text) == [(1, "one.md"), (5, "two.md")]


def test_link_targets_skips_tilde_fence() -> None:
    text = "[a](one.md)\n~~~\n[x](nope.md)\n~~~\n[b](two.md)\n"
    assert csl.link_targets(text) == [(1, "one.md"), (5, "two.md")]


def test_resolve_target_skips_scheme_case_insensitively(tmp_path: Path) -> None:
    # Finding 6: URI schemes are case-insensitive (RFC 3986); other schemes are
    # external too. None of these should be treated as a relative path.
    src = tmp_path / "doc.md"
    assert csl.resolve_target(src, "HTTPS://example.com") is None
    assert csl.resolve_target(src, "Mailto:x@y.z") is None
    assert csl.resolve_target(src, "tel:+15551234") is None


def test_resolve_target_keeps_spaced_path_instead_of_truncating(
    tmp_path: Path,
) -> None:
    # Finding 4: a title is stripped only when actually present, so a path with
    # a space is kept whole (reported dead loudly) rather than silently
    # truncated to its first token -- which could otherwise falsely resolve.
    src = tmp_path / "doc.md"
    assert csl.resolve_target(src, "my file.md") == Path(
        os.path.normpath(tmp_path / "my file.md")
    )


def test_check_reports_non_utf8_file_without_crashing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Finding 7: a Windows-1252 file (the corruption CLAUDE.md warns this
    # environment produces) must fail the gate with a clean line, not a
    # traceback.
    specs = _repo(tmp_path, monkeypatch)
    (specs / "bad.md").write_bytes(b"\xff\xfe title [x](nope.md)\n")
    errors = csl.check()
    assert len(errors) == 1
    assert "bad.md" in errors[0]
    assert "not valid UTF-8" in errors[0]


# --- regression tests from a second /apply-review pass (CommonMark fidelity) --

BT = chr(96)  # backtick, kept out of the string literals below for readability


def test_link_targets_info_string_line_does_not_close_fence() -> None:
    # A closing fence carries no info string (CommonMark), so a ```lang line
    # inside an open ``` block is content, not a close -- treating it as a close
    # would invert the state and silently skip every later link.
    f = BT * 3
    text = f"[a](one.md)\n{f}\ntext\n{f}python\nmore\n{f}\n[b](two.md)\n"
    assert csl.link_targets(text) == [(1, "one.md"), (7, "two.md")]


def test_link_targets_four_space_indented_fence_is_not_a_fence() -> None:
    # >3 spaces of indent is an indented code block, not a fence opener; a link
    # after it must still be scanned (the phantom fence would suppress it).
    f = BT * 3
    text = f"[a](one.md)\n    {f}\n[b](two.md)\n"
    assert csl.link_targets(text) == [(1, "one.md"), (3, "two.md")]


def test_link_targets_inline_triple_backtick_span_is_not_a_fence() -> None:
    # A prose line carrying a ```...``` inline span (backtick in the "info
    # string") must not open a phantom fence that eats the rest of the file.
    f = BT * 3
    text = f"{f}lang{f} is how, see [a](one.md)\n[b](two.md)\n"
    assert csl.link_targets(text) == [(1, "one.md"), (2, "two.md")]


def test_link_targets_code_span_needs_exact_length_close() -> None:
    # A 1-backtick opener must not close on one backtick of a longer ``` run;
    # per CommonMark the span never forms, so the link is live and checkable.
    line = f"{BT}[a](dead.md){BT * 3} tail"
    assert csl.link_targets(line) == [(1, "dead.md")]


def test_resolve_target_does_not_skip_pathlike_colon(tmp_path: Path) -> None:
    # A dotted name with a trailing :N (editor-style) or a single-letter drive
    # must not be mistaken for a URI scheme and silently skipped; it resolves
    # (and is reported dead loudly if absent). A real 2+-char, dotless scheme
    # is still skipped.
    src = tmp_path / "doc.md"
    assert csl.resolve_target(src, "data-model.md:47") is not None
    # ...but a *leading* single letter and colon is a drive-relative path, and
    # resolving it produced a different answer on each CI leg: `specs\CLAUDE.md`
    # on Windows (passing) against a dead link on Linux. The two assertions are
    # the pair — a guard that rejected every colon would satisfy the second and
    # break the first, which is how it was first written.
    assert csl.resolve_target(src, "c:CLAUDE.md") is None
    assert csl.resolve_target(src, "notes.md") is not None
    assert csl.resolve_target(src, "https://x") is None
    assert csl.resolve_target(src, "tel:+1") is None


def test_resolve_target_skips_root_absolute_and_protocol_relative(
    tmp_path: Path,
) -> None:
    # A leading / is not a checkable relative link -- GitHub resolves a
    # root-absolute target against the repo root, which this gate does not
    # model, and //host/x is protocol-relative. Both must be skipped, not
    # resolved against source.parent and falsely reported dead.
    src = tmp_path / "specs" / "doc.md"
    assert csl.resolve_target(src, "/specs/adr/0045.md") is None
    assert csl.resolve_target(src, "//example.com/x") is None


def test_check_validates_image_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # An image's target is validated the same as a text link: a dead local
    # image is a real defect and must be reported.
    specs = _repo(tmp_path, monkeypatch)
    (specs / "doc.md").write_text("![a diagram](missing.png)\n", encoding="utf-8")
    errors = csl.check()
    assert len(errors) == 1
    assert "missing.png" in errors[0]


@pytest.mark.parametrize(
    ("target", "reason"),
    [
        ("adr\\doc.md", "backslash"),
        ("adr/doc.md.", "dot or space"),
        # A trailing space *inside* the path, not at the end of the target: the
        # target is stripped before it is parsed, and rightly so, since
        # CommonMark strips a destination's surrounding whitespace too.
        ("sub /doc.md", "dot or space"),
    ],
)
def test_a_windows_only_spelling_is_reported_on_every_platform(
    target: str, reason: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Report finding 11: a target Windows resolves and Linux does not.

    Measured on Windows, both spellings resolved with `exists() == True` while
    naming a file that does not exist on Linux — and the docs-consistency job
    is ubuntu-only, so local is the only place they are seen. The next
    Windows-authored `specs\\foo.md` link would pass every local gate and
    redden `ci-ok`.

    Reported rather than skipped, because neither is a valid relative link on
    the platform that decides the verdict, so calling it dead is not a false
    positive. Asserted on the written spelling rather than on `exists()`, which
    is what makes the test mean the same thing on both platforms — an
    `exists()`-based assertion would itself diverge.
    """
    specs = _repo(tmp_path, monkeypatch)
    (specs / "doc.md").write_text(f"see [x]({target})\n", encoding="utf-8")

    errors = csl.check()

    assert len(errors) == 1, errors
    assert reason in errors[0], errors[0]


@pytest.mark.parametrize(
    ("tag", "scanned_past_the_blank_line"),
    [
        # CommonMark condition 1: runs to its CLOSING TAG and swallows blank
        # lines. Everything inside is masked.
        ("pre", False),
        ("script", False),
        ("style", False),
        ("textarea", False),
        # Condition 6: ends at the next blank line, so the link after it is
        # ordinary prose and is scanned.
        ("div", True),
        ("section", True),
    ],
)
def test_where_an_html_block_ends_depends_on_which_kind_it_is(
    tag: str, scanned_past_the_blank_line: bool
) -> None:
    """Report finding 7: the docstring's rule was true of one kind only.

    The entry said an HTML block "runs to the next blank line rather than to
    its closing tag" with no qualification, and the evidence listed beside it
    exercised only `<!-- -->`, `<br>` and `</div>` — all type 6 or 7, so it
    never reached the shape that contradicts the claim.

    The masking is correct either way; what was wrong was the *claim*, in the
    silent direction. A reader who believed it would expect a target one blank
    line inside a `<pre>` fold to be checked, so a target that later dies there
    is never reported and the gate stays green.

    Both rows are needed and neither is padding: with only the condition-1 rows
    a mask that swallowed every HTML block wholesale would pass, and with only
    the condition-6 rows so would today's misdescription.
    """
    text = f"<{tag}>\n[a](x.md)\n\n[b](y.md)\n</{tag}>\n\nafter [c](z.md)\n"

    found = csl.link_targets(text)

    assert ((4, "y.md") in found) is scanned_past_the_blank_line, found
    # The control on both rows: prose after the fold is always scanned, so a
    # mask that ran away to the end of the document fails here.
    assert (7, "z.md") in found, found
    # And the opener's own line is masked on both, which is what makes this a
    # test of where the block *ends* rather than of whether it starts.
    assert (2, "x.md") not in found, found


_BS = chr(92)  # written this way so no heredoc or editor can eat it


@pytest.mark.parametrize(
    ("written", "rendered"),
    [
        # (what the author typed inside the parens, what markdown-it renders)
        (f"a{_BS}_b.md", "a_b.md"),
        (f"x{_BS}*y.md", "x*y.md"),
        (f"foo{_BS}(bar{_BS}).md", "foo(bar).md"),
        (f"a{_BS}-b.md", "a-b.md"),
        # The other direction, kept in the same table so the two cannot drift:
        # an *escaped backslash* renders as a real backslash and must still be
        # reported, and a backslash before a non-punctuation character is not
        # an escape at all.
        (f"a{_BS}{_BS}b.md", f"a{_BS}b.md"),
        (f"adr{_BS}0061.md", f"adr{_BS}0061.md"),
    ],
)
def test_a_backslash_is_judged_on_what_the_renderer_produces(
    written: str, rendered: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Report finding 6: an escape is not a Windows-only spelling.

    `windows_only_spelling` returned a reason for ANY backslash, including a
    CommonMark backslash escape — which means the same thing on every platform.
    So `[t](a\\_b.md)`, rendered `<a href="a_b.md">` by markdown-it itself, was
    reported dead, **and the remedy the gate printed ("write it with `/`")
    produces `a/_b.md`, a genuinely dead link**. A gate failing on correct
    markdown is the failure this module keeps refusing; one whose advice breaks
    the input is a step past it.

    The oracle is markdown-it's own renderer rather than a second copy of the
    escape rule: `rendered` is asserted against `md.render` first, so a
    hand-written expectation that drifts from CommonMark fails here rather than
    silently agreeing with a wrong implementation. The last two rows are the
    discriminating half — a rule that simply stopped looking at backslashes
    satisfies the first four and fails these.
    """
    from markdown_it import MarkdownIt

    html = MarkdownIt("commonmark").render(f"[t]({written})")
    href = re.search(r'href="([^"]*)"', html)
    assert href is not None, written
    # markdown-it percent-encodes the backslash it keeps; compare on the
    # decoded form, which is the path a filesystem would be asked for.
    assert href.group(1).replace("%5C", _BS) == rendered, written

    # The decoded destination is asserted directly against the renderer's, which
    # is the property the whole fix rests on and which nothing else here pins.
    assert csl.relative_path_part(written) == rendered, written

    specs = _repo(tmp_path, monkeypatch)
    (specs / "doc.md").write_text(f"see [x]({written})\n", encoding="utf-8")

    errors = csl.check()
    flagged = [e for e in errors if "backslash" in e]

    # Filtered to the backslash verdict, and **no target file is created**. A
    # smoke round caught the earlier version doing so under a docstring claiming
    # it made the spelling rule "the only thing left that could be reported":
    # `windows_only_spelling` short-circuits with `continue` before
    # `resolved.exists()` is ever consulted, so whether the file exists cannot
    # change this verdict, and the filter was doing the isolating on its own.
    # A step justified by a false reason is worse than no step — the next reader
    # has to work out that it does nothing.
    if _BS in rendered:
        assert len(flagged) == 1, errors
    else:
        assert flagged == [], errors


@pytest.mark.parametrize(
    ("written", "expected"),
    [
        # An escaped `#` and a bare one render to the *same* href, so they must
        # reach the same path part. Measured against markdown-it: both
        # `[t](foo\#bar.md)` and `[t](foo#bar.md)` render `href="foo#bar.md"` —
        # it does not percent-encode the escape — and a browser reads that as
        # the file `foo` with the fragment `bar.md`.
        (f"foo{_BS}#bar.md", "foo"),
        ("foo#bar.md", "foo"),
        # The guards below the split, each written in its escaped spelling, so
        # the reordering cannot have moved the decode past one of them.
        (f"{_BS}/etc/passwd.md", None),
        (f"c{_BS}:x.md", None),
        (f"{_BS}#anchor", None),
        (f"http{_BS}://example.com", None),
        # The ordinary cases the reordering must not disturb.
        ("a.md#sec", "a.md"),
        ("plain.md", "plain.md"),
        # Both title spellings `TITLE_RE` accepts. The single-quoted row is here
        # because a reviewer measured that nothing in this file exercised that
        # alternation branch at all — not even in its plain, unescaped form.
        ('x.md "a title"', "x.md"),
        ("x.md 'a title'", "x.md"),
    ],
)
def test_every_step_below_the_title_strip_sees_the_rendered_destination(
    written: str, expected: str | None
) -> None:
    """Smoke-round finding: the fragment split was the one step still reading raw.

    `relative_path_part`'s comment promised that "everything below" the decode
    asks its question of the destination the renderer produces, and the decode
    sat one line *below* the `#` split — so the split was the exempt step, and
    it was the one that broke. `[t](foo\\#bar.md)` was cut at the raw `\\#` and
    came back as `foo\\`: a trailing backslash nothing put there, which
    `windows_only_spelling` then reported as a Windows-only spelling on a link
    that is valid everywhere.

    The first two rows are the whole point — the escaped and bare spellings
    render identically, so any answer that differs between them is wrong
    whichever one it favours.

    The four `None` rows are not padding. Moving a decode earlier is exactly the
    change that can push a destination *past* a skip guard, so each guard below
    the split is exercised in its escaped spelling; without them a decode moved
    to the top of the function would satisfy the first two rows while silently
    skipping links the gate is meant to check.
    """
    assert csl.relative_path_part(written) == expected, written


@pytest.mark.parametrize("quote", ['"', "'"], ids=["double", "single"])
def test_a_title_containing_an_escaped_quote_is_still_stripped(quote: str) -> None:
    """Smoke-round finding: `TITLE_RE`'s quoted run was escape-blind.

    `[t](path.md "a\\"b")` is a link markdown-it renders as `href="path.md"`.
    The flat `[^"]*` run matched nothing against it, so the title was never
    stripped and the whole string became the path — `path.md "a"b"`, a filename
    that cannot exist, reported dead against a live link. Same failure class as
    the single-quoted title spelling this pattern was widened for one revision
    earlier.

    It was found by a reviewer checking whether the *neighbouring* claim — that
    the title strip must stay above the escape decode — was measured or merely
    plausible. It was merely plausible, and the check that disproved it is the
    second assertion here: under the flat pattern the order made no difference,
    because neither spelling matched. Escape-aware, the order is real.

    **The case is a link markdown-it actually renders**, and that is deliberate.
    A second reviewer reached the same boundary from the test side and proposed
    `[t](foo \\"z\\")` as the row to add; measured, markdown-it renders that as
    literal text and emits no `<a>` at all, so an assertion about its "correct"
    path part would be asserting about a shape the renderer denies is a link.
    `path.md "a\\"b"` renders `href="path.md" title='a"b'`, so there is a right
    answer to hold the code to.

    **Both quote characters, and that is a third reviewer's finding.** The fix
    made `TITLE_RE`'s two alternation branches escape-aware and only the
    double-quoted one — the branch the defect happened to live in — was tested:
    reverting the *single*-quoted branch to its flat `'[^']*'` form left the
    whole suite green. CommonMark spells a title three ways and this pattern
    accepts two of them, so a fix applied to both has to be held to both.
    Measured, the single-quoted case is the same shape: `[t](path.md 'a\\'b')`
    renders `href="path.md" title="a'b"`.
    """
    target = f"path.md {quote}a{_BS}{quote}b{quote}"

    assert csl.relative_path_part(target) == "path.md", target

    # And the ordering justification, asserted rather than asserted-about: the
    # pattern must see the raw form. Once the escape is resolved to a bare
    # quote the title is unmatchable, which is why the decode runs below it.
    assert csl.TITLE_RE.match(target) is not None
    assert csl.TITLE_RE.match(csl.unescape_link_destination(target)) is None


def test_the_backslash_report_never_prints_a_personal_filename(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Report finding 10: the new error path ran ahead of the containment skip.

    `md_sources`' docstring calls the PERSONAL_DIR filter "the guard", and the
    Windows-spelling report was the first error path written past it: the error
    was appended and `continue`d three lines before the skip could run, so a
    link written `personal\\<name>.md` printed that filename to stdout and into
    CI's docs-consistency log. CLAUDE.md counts the provenance of the owner's
    records as personal with no values attached, so the filename alone is the
    breach.

    The forward-slash sibling in the same document is the control: it was
    skipped correctly throughout, which is exactly why nothing noticed.

    Both separators are asserted because `Path` disagrees about the backslash
    by platform — a separator on Windows, an ordinary filename character on
    Linux — and Linux is the leg whose run is authoritative, so it is the leg
    where the containment skip would not have fired on the resolved path.

    The filename below is synthetic. Nothing in this repository's real personal
    tree is named or referenced here.
    """
    specs = _repo(tmp_path, monkeypatch)
    (specs / "doc.md").write_text(
        f"see [a](personal{_BS}synthetic-panel.md) and "
        "[b](personal/synthetic-panel.md)\n",
        encoding="utf-8",
    )

    errors = csl.check()

    assert errors == [], errors
    assert not any("synthetic-panel" in e for e in errors), errors


def test_the_separator_normalisation_is_reachable_on_every_platform(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Smoke-round finding: the fallback branch is dead on Windows.

    `_resolves_into_personal` asks twice — once on the resolved path, once on the
    backslash-normalised one — because `Path` reads a backslash as a separator on
    Windows and as an ordinary filename character on Linux. That means the second
    question only *matters* on POSIX, and on Windows the first one already
    answers `True`, so the end-to-end test above never enters the branch here.
    Measured by a reviewer: replacing the fallback's return with `False` left the
    whole suite green on this Windows worktree.

    CI's Linux and macOS legs would catch it, but "the property is enforced on
    two of three legs" is inference from the host rather than a test of the
    property, which is the trade `specs/testing-strategy.md` warns about for
    platform-gated behaviour.

    So the branch is forced directly, by handing in a `resolved` that is
    deliberately *not* already under `PERSONAL_DIR` while `path_part` carries the
    backslash spelling. That is not a contrived pair — it is exactly the pair
    POSIX produces, since there `personal\\notes.md` resolves to one oddly-named
    file in `specs/`. The test simulates the authoritative leg on the leg that
    cannot reach it.
    """
    specs = _repo(tmp_path, monkeypatch)
    source = specs / "doc.md"
    # What POSIX's `Path` would hand the caller: a single file named with a
    # backslash, sitting in `specs/` rather than under `specs/personal/`.
    not_yet_personal = specs / "elsewhere.md"
    assert not not_yet_personal.is_relative_to(csl.PERSONAL_DIR)

    assert csl._resolves_into_personal(  # pyright: ignore[reportPrivateUsage]
        source, f"personal{_BS}notes.md", not_yet_personal
    )

    # The boundary: a backslash target that does not point into personal must
    # still answer False, or the fallback is just "any backslash is personal"
    # and the divergence rule below it is switched off.
    assert not csl._resolves_into_personal(  # pyright: ignore[reportPrivateUsage]
        source, f"adr{_BS}0061-x.md", not_yet_personal
    )
    # And a target with no backslash at all never reaches the fallback.
    assert not csl._resolves_into_personal(  # pyright: ignore[reportPrivateUsage]
        source, "personal-ish.md", not_yet_personal
    )


def test_a_non_personal_backslash_is_still_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The boundary for the skip above: it must not switch the rule off.

    Widening the containment skip until it swallowed every backslash target
    would satisfy the test above and disable the divergence guard entirely —
    the same shape as the interpreter carve-out's own failure mode.
    """
    specs = _repo(tmp_path, monkeypatch)
    (specs / "doc.md").write_text(f"see [x](adr{_BS}0061-x.md)\n", encoding="utf-8")

    errors = csl.check()

    assert len(errors) == 1, errors
    assert "backslash" in errors[0], errors[0]


@pytest.mark.parametrize("target", ["./doc.md", "../doc.md", "sub/doc.md"])
def test_an_ordinary_relative_spelling_is_not_swept_up(
    target: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The boundary, and the reason `.` and `..` are excluded explicitly.

    Both end in a dot and both are the ordinary way a relative link is written,
    so a rule that merely looked for a trailing dot would reject every
    `./x.md` in the repository — turning a divergence guard into a gate that
    fails on correct markdown, which is the trade this module keeps refusing.
    """
    specs = _repo(tmp_path, monkeypatch)
    (specs / "sub").mkdir()
    for path in (specs / "doc.md", specs / "sub" / "doc.md", tmp_path / "doc.md"):
        path.write_text("t", encoding="utf-8")
    (specs / "src.md").write_text(f"see [x]({target})\n", encoding="utf-8")

    assert csl.check() == []


@pytest.mark.parametrize("failure", [ImportError("no markdown_it"), RuntimeError("x")])
def test_a_check_that_could_not_run_exits_2_not_1(
    failure: Exception,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Report finding 10: the guard's own rationale required an exit code it
    did not use.

    Its comment justified existing because the unguarded form "exits 1 with a
    traceback, which a caller reading the exit status alone cannot tell from a
    real dead link" — and then returned 1, leaving that caller in exactly the
    same position with only stdout changed. `diff_harness.refuse`, added in the
    same change, states the rule: a run that checked nothing must be
    distinguishable from one that found a real difference.

    `RuntimeError` is here for the same reason rather than by analogy —
    `md_sources` raises it on a `git ls-files` failure and `code_masked_lines`
    on a mask that changed a line's length — and both used to escape as a
    traceback at exit 1.
    """

    def boom() -> list[str]:
        raise failure

    monkeypatch.setattr(csl, "check", boom)

    assert csl.main() == 2

    assert "could not run" in capsys.readouterr().out


@pytest.mark.parametrize(
    "failure",
    [
        subprocess.TimeoutExpired(cmd=["git", "ls-files"], timeout=30),
        FileNotFoundError(2, "No such file or directory", "git"),
    ],
    ids=["timeout", "git-absent"],
)
def test_a_git_ls_files_failure_exits_2_through_the_real_path(
    failure: Exception,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Report finding 2: the two git failures escaped the exit-2 guard.

    The sibling test above injects into `check` directly, so it drives
    `main`'s handler without ever reaching the `subprocess.run` whose failures
    the docstring's "a `git ls-files` failure" promise is *about*. Both of them
    escaped: `TimeoutExpired` is a `SubprocessError` and `FileNotFoundError` is
    an `OSError`, and `main` catches `(ImportError, RuntimeError)` -- so a
    missing or hung git produced a traceback at exit 1, the code reserved for a
    real dead link, and `/land` runs this gate.

    Driven through `md_sources` with a real `.git` marker present, because the
    guard sits inside the branch that marker selects; without it the function
    takes the rglob fallback and never calls git at all.
    """
    _repo(tmp_path, monkeypatch)
    (tmp_path / ".git").mkdir()

    def boom(*_args: object, **_kwargs: object) -> object:
        raise failure

    monkeypatch.setattr(csl.subprocess, "run", boom)

    assert csl.main() == 2

    out = capsys.readouterr().out
    assert "could not run" in out, out
    # The message names git rather than only the exception type: the whole
    # point of converting these is that the author is told what did not run.
    assert "git ls-files" in out, out


def test_check_reports_a_file_it_could_not_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Smoke-round finding: the `OSError` branch was never driven.

    `check()` guards two read failures. The `UnicodeDecodeError` half is
    covered; the `OSError` half — added because `md_sources()` filters on
    `is_file()` and the read happens later, so a concurrent editor makes a
    `PermissionError` routine on Windows — had no test at all, and rewriting
    its message to a nonsense string left the file green.

    The exception type is asserted because that is the whole content of the
    message: a reader needs to know *which* failure, and `type(exc).__name__`
    is the only thing distinguishing this line from the UTF-8 one.
    """
    specs = _repo(tmp_path, monkeypatch)
    (specs / "doc.md").write_text("see [a](x.md)\n", encoding="utf-8")

    def denied(*_args: object, **_kwargs: object) -> str:
        raise PermissionError(13, "permission denied")

    monkeypatch.setattr(Path, "read_text", denied)

    errors = csl.check()

    assert len(errors) == 1, errors
    assert "could not be read" in errors[0], errors[0]
    assert "PermissionError" in errors[0], errors[0]


def test_an_unexpected_exception_is_not_absorbed_into_exit_2(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Smoke-round finding: nothing pinned how *narrow* the guard is.

    `main` catches `(ImportError, RuntimeError)` deliberately — both mean the
    check did not run. Widening it to `except Exception` would convert any
    latent bug into a tidy "could not run" at exit 2, which is the failure the
    exit-code split exists to make visible rather than to hide. Measured: that
    widening left the whole suite green, because every test drove one of the
    two named types.

    So the assertion is that a third type *propagates*: the contract is the
    boundary, not the catch.
    """

    def boom() -> list[str]:
        raise ValueError("not a could-not-run")

    monkeypatch.setattr(csl, "check", boom)

    with pytest.raises(ValueError, match="not a could-not-run"):
        csl.main()


def test_a_link_split_across_a_hard_wrap_is_not_matched() -> None:
    """The docstring's "scanned as ordinary prose" claim, pinned.

    Link *matching* stays line-local even though the block structure no longer
    is, which is what keeps a reported `file:line` pointing at the line a
    reader will open. The consequence is that a link whose own syntax is split
    by a hard wrap is not matched — a documented, deliberate miss that nothing
    exercised.

    The unwrapped control is what stops this passing for a `link_targets` that
    found nothing at all.
    """
    assert csl.link_targets("see [te\nxt](nope.md) here\n") == []

    assert csl.link_targets("see [text](nope.md) here\n") == [(1, "nope.md")]


def test_an_escaped_bracket_in_link_text_is_silently_missed() -> None:
    """The other documented "silently missed" CommonMark shape.

    Its sibling — the image-badge nesting — is pinned; this one was not.
    CommonMark reads `[a \\] b](t.md)` as a link whose text contains an escaped
    `]`; `LINK_RE` stops at the first unescaped-looking bracket and matches
    nothing. Recorded as a known miss rather than a defect, and pinned so the
    docstring's claim is checkable rather than asserted.
    """
    assert csl.link_targets("[a \\] b](t.md)\n") == []

    assert csl.link_targets("[a b](t.md)\n") == [(1, "t.md")]


def test_a_file_that_could_not_be_read_is_not_counted_as_a_dead_link(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The summary line counted unparsed files among the dead links.

    `check()` collects a file it could not decode alongside the dead links it
    found, so the count in `spec link check failed (N dead link(s))` described
    something that was never parsed. Each reported line already says which it
    is; the summary no longer claims they are all links.
    """
    specs = _repo(tmp_path, monkeypatch)
    (specs / "bad.md").write_bytes(b"\xff\xfe not utf-8")

    assert csl.main() == 1

    printed = capsys.readouterr().out
    assert "dead link(s)" not in printed
    assert "problem(s)" in printed


def test_an_image_badge_nesting_checks_the_outer_target_not_the_image() -> None:
    """Report finding 3: which of the two destinations a badge gets checked on.

    `[![alt](img.png)](target.md)` carries two destinations and exactly one is
    checked. The balanced-bracket `LINK_RE` consumes the whole badge and
    `finditer` resumes past it, so the outer `target.md` is checked and the
    nested `img.png` is not; the flat pattern it replaced did the reverse. The
    module docstring named the flat pattern's behaviour until a review measured
    it, so this pins the live one rather than leaving it to prose.

    The standalone-image assertion is the control, and it is what stops this
    reading as "images are not checked": an image that is not wrapped in a link
    is still checked, which is the coverage the docstring calls a real defect
    worth catching.
    """
    assert csl.link_targets("[![alt](img.png)](target.md)\n") == [(1, "target.md")]

    assert csl.link_targets("![alt](img.png)\n") == [(1, "img.png")]


def test_check_crawls_markdown_outside_specs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The widened crawl (ADR-0061 revision): a dead link in tracked prose
    # OUTSIDE specs/ -- e.g. a .claude/ skill file -- is a real defect the old
    # specs-only crawl silently missed.
    _repo(tmp_path, monkeypatch)
    skill = tmp_path / ".claude" / "skills" / "x"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("run [gone](../../missing.md)\n", encoding="utf-8")
    errors = csl.check()
    assert len(errors) == 1
    assert ".claude/skills/x/SKILL.md:1" in errors[0]


# The developer's global/system git config must not reach fixture repos: a
# global `commit.gpgsign` fails every fixture commit at exit 128 (measured by
# external review 2), and `init.templateDir` / `core.autocrlf` are the same
# class. Same neutralization as test_check_personal_containment.py and
# test_review_worktree.py.
_NEUTRAL_GIT_ENV = {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull}


def _git(args: list[str], cwd: Path) -> None:
    """Run git in a tmp fixture repo. PATH-resolved, as every repo tool.

    S603/S607 waivers: every caller passes a fixed literal argument list
    against a tmp_path fixture -- nothing user-controlled reaches the shell.
    """
    import subprocess

    subprocess.run(  # noqa: S603
        ["git", *args],  # noqa: S607
        cwd=cwd,
        check=True,
        env={**os.environ, **_NEUTRAL_GIT_ENV},
    )


def test_md_sources_scans_untracked_but_not_ignored_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # In a git checkout, enumeration is `--cached --others --exclude-standard`:
    # tracked AND untracked-but-not-ignored files are scanned -- /land runs the
    # gate before staging, and a brand-new doc is exactly where a dead link is
    # most likely -- while an ignored file's dead links cannot fail the gate.
    specs = _repo(tmp_path, monkeypatch)
    (specs / "tracked.md").write_text("[dead](gone.md)\n", encoding="utf-8")
    (specs / "untracked.md").write_text("[dead](also-gone.md)\n", encoding="utf-8")
    (tmp_path / ".gitignore").write_text("specs/ignored.md\n", encoding="utf-8")
    (specs / "ignored.md").write_text("[dead](never-seen.md)\n", encoding="utf-8")
    _git(["init", "-q"], tmp_path)
    _git(["add", "specs/tracked.md"], tmp_path)
    sources = csl.md_sources()
    assert specs / "tracked.md" in sources
    assert specs / "untracked.md" in sources
    assert specs / "ignored.md" not in sources
    errors = csl.check()
    assert len(errors) == 2
    assert any("gone.md" in e for e in errors)
    assert any("also-gone.md" in e for e in errors)


def test_md_sources_skips_a_tracked_file_deleted_from_disk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # `--cached` still names a tracked path after `rm doc.md` (deletion not yet
    # staged); there are no bytes to scan, and without the is_file() filter the
    # gate died on FileNotFoundError instead of reporting a verdict -- during
    # exactly the ordinary mid-edit state a contributor passes through.
    specs = _repo(tmp_path, monkeypatch)
    (specs / "doomed.md").write_text("t\n", encoding="utf-8")
    (specs / "doc.md").write_text("[still here](doomed.md)\n", encoding="utf-8")
    _git(["init", "-q"], tmp_path)
    _git(["add", "specs/doomed.md", "specs/doc.md"], tmp_path)
    (specs / "doomed.md").unlink()
    sources = csl.md_sources()
    assert specs / "doomed.md" not in sources
    # The deletion is not swallowed: the link *to* the deleted file goes dead
    # and is reported as such, which is the verdict the crash was hiding.
    errors = csl.check()
    assert len(errors) == 1
    assert "doomed.md" in errors[0]


def test_md_sources_dedupes_merge_stage_duplicates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # For an unmerged path `ls-files` emits one entry per merge stage (base /
    # ours / theirs), so without the set the same file is read and its dead
    # links reported up to three times -- during a conflicted merge, when the
    # output is hardest to read.
    specs = _repo(tmp_path, monkeypatch)
    conflicted = specs / "doc.md"
    _git(["init", "-q", "-b", "main"], tmp_path)
    _git(["config", "user.email", "t@t"], tmp_path)
    _git(["config", "user.name", "t"], tmp_path)
    # Pin the conflict-marker style: the arithmetic below is written against
    # the two-side default. _NEUTRAL_GIT_ENV already keeps a developer's
    # global diff3/zdiff3 out (measured: zdiff3 adds a base section -- a third
    # dead-link line); the explicit pin additionally holds if a future git
    # changes the default style itself.
    _git(["config", "merge.conflictstyle", "merge"], tmp_path)
    conflicted.write_text("[dead](gone.md) base\n", encoding="utf-8")
    _git(["add", "specs/doc.md"], tmp_path)
    _git(["commit", "-q", "-m", "base"], tmp_path)
    _git(["checkout", "-q", "-b", "side"], tmp_path)
    conflicted.write_text("[dead](gone.md) side\n", encoding="utf-8")
    _git(["commit", "-q", "-am", "side"], tmp_path)
    _git(["checkout", "-q", "main"], tmp_path)
    conflicted.write_text("[dead](gone.md) main\n", encoding="utf-8")
    _git(["commit", "-q", "-am", "main"], tmp_path)
    import subprocess

    merge = subprocess.run(
        ["git", "merge", "side"],  # noqa: S607 - PATH-resolved git; conflict expected
        cwd=tmp_path,
        check=False,
        capture_output=True,
        env={**os.environ, **_NEUTRAL_GIT_ENV},
    )
    assert merge.returncode != 0, "fixture must produce a conflict"
    assert csl.md_sources().count(conflicted) == 1
    # The conflicted working file holds BOTH sides between the merge markers,
    # so its two dead-link lines are each reported once: 2 errors. Without the
    # dedupe the file is read once per stage and the same two lines report
    # three times each (6) -- the noise this fixture pins away.
    errors = csl.check()
    assert len(errors) == 2


def test_md_sources_raises_with_gits_own_explanation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # When git itself fails (dubious ownership is the canonical container
    # case), the error must carry git's stderr -- a bare CalledProcessError
    # says only "exit status 128" and blames the link checker for an
    # environment problem. Same contract as the citation gate's helper.
    import subprocess

    (tmp_path / ".git").mkdir()  # take the git branch, not the rglob fallback
    monkeypatch.setattr(csl, "REPO_ROOT", tmp_path)

    def fake_run(*_args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        # Pin `check=False`: md_sources handles the exit itself so it can
        # attach stderr; a revert to check=True must not hide behind this fake.
        assert kwargs["check"] is False
        return subprocess.CompletedProcess(
            args=["git", "ls-files"],
            returncode=128,
            stdout="",
            stderr="fatal: detected dubious ownership in repository at '/repo'\n",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(RuntimeError) as caught:
        csl.md_sources()
    message = str(caught.value)
    assert "128" in message
    assert "dubious ownership" in message


def test_md_sources_survives_a_non_utf8_filename(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # `git ls-files -z` emits raw filename bytes. A non-UTF-8 name (creatable
    # on ext4; NTFS and APFS refuse it, hence the skip guard) crashed the
    # strict decode before any verdict; "replace" would instead mangle the
    # path so the file silently dropped out of the scan. surrogateescape
    # round-trips, so the file is enumerated AND its links are checked.
    specs = _repo(tmp_path, monkeypatch)
    try:
        # Both statements belong inside the guard: on Windows os.fsdecode
        # itself raises (strict UTF-8 filesystem codec), before any write.
        weird = specs / os.fsdecode(b"caf\xe9.md")  # \xe9: invalid UTF-8 alone
        weird.write_text("[dead](gone.md)\n", encoding="utf-8")
    except (OSError, UnicodeEncodeError, UnicodeDecodeError):  # fmt: skip
        pytest.skip("platform cannot represent a non-UTF-8 filename")
    _git(["init", "-q"], tmp_path)  # untracked: the --others half enumerates it
    assert weird in csl.md_sources()
    errors = csl.check()
    assert len(errors) == 1
    assert "gone.md" in errors[0]


def test_the_stream_reconfigure_stays_out_of_main_and_fires_from_dunder_main() -> None:
    # Same pin as test_bot_review.py carries for its script, for the same two
    # invisible regressions: moving the reconfigure into main() mutates
    # pytest's own capture streams on every in-process call (every check()/
    # main() test in this module), and deleting it reinstates the
    # crash-on-surrogate-path bug on strict-locale consoles. Source-level
    # because the call site is the property.
    source = (
        Path(__file__).resolve().parent.parent / "scripts" / "check_spec_links.py"
    ).read_text(encoding="utf-8")
    main_body = source.split("\ndef main(")[1].split("\nif __name__")[0]
    assert "reconfigure" not in main_body, (
        "the reconfigure is back inside main(), where it mutates pytest's "
        "own capture streams for every in-process call"
    )
    dunder_main = source.split("\nif __name__")[1]
    assert 'reconfigure(encoding="utf-8", errors="replace")' in dunder_main, (
        "deleting this reinstates the UnicodeEncodeError crash when the gate "
        "reports a surrogate-escaped path on a strict-locale console"
    )


def test_fallback_walk_prunes_vendored_trees(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Outside a checkout the rglob fallback must not sweep installed-package
    # docs: a dead link inside .venv/ is not this repo's defect, and walking
    # it is the exact hazard the ADR cites as the reason for git truth.
    _repo(tmp_path, monkeypatch)
    venv_doc = tmp_path / ".venv" / "lib" / "pkg"
    venv_doc.mkdir(parents=True)
    (venv_doc / "README.md").write_text("[dead](nope.md)\n", encoding="utf-8")
    assert csl.check() == []


def test_link_targets_ignores_escaped_opener() -> None:
    # \[text](t.md) is escaped prose, not a link; but \\[text](t.md) has escaped
    # backslashes, leaving the [ live.
    bs = chr(92)  # backslash
    assert csl.link_targets(bs + "[a](one.md)") == []
    assert csl.link_targets(bs * 2 + "[a](one.md)") == [(1, "one.md")]


# --- local smoke, remedy angle: branches the mutation pass found unpinned ----


def test_a_link_inside_an_indented_code_block_is_not_live() -> None:
    """A four-space indented block is code, and the regex walk never masked it.

    The capability is claimed by `code_masked_lines`' own docstring, and nothing
    pinned it: removing `"code_block"` from the masked token types left the whole
    suite green. `test_link_targets_four_space_indented_fence_is_not_a_fence`
    reads as though it covers this and does not — it places no link *inside* the
    indented line, so it passes under both implementations for an unrelated
    reason. That is the fixture-that-cannot-fail shape, so this one puts the link
    where the difference actually shows.
    """
    text = "text:\n\n    [a](x.md)\n\nafter [b](y.md)\n"

    assert csl.link_targets(text) == [(5, "y.md")]


def test_a_link_inside_a_tab_indented_code_block_is_not_live() -> None:
    """The same block reached by a tab rather than by four spaces.

    Paired with the fixture above deliberately: one alone is satisfied by an
    implementation that special-cases exactly one indent spelling.
    """
    text = "text:\n\n\t[a](x.md)\n\nafter [b](y.md)\n"

    assert csl.link_targets(text) == [(5, "y.md")]


def test_a_code_span_on_a_line_the_parser_does_not_cover_is_still_stripped() -> None:
    """The fallback for lines belonging to no inline token.

    A link reference definition is consumed by the block parser and yields no
    token with a map at all, so the token-driven masking never touches its
    line. `code_masked_lines` falls back to line-local stripping there, which is
    what keeps widening the parser from silently *narrowing* what counts as
    code. Measured: disabling that fallback left the whole of this module's
    suite green, which is why the case is pinned here explicitly.

    The two assertions are the discriminating pair: the un-quoted link must
    survive, or a test asserting only the absence of `y.md` would also pass for
    an implementation that blanked the whole line.
    """
    text = "[ref]: x.md\n\nlive [a](x.md) and `[b](y.md)` here\n"

    assert csl.link_targets(text) == [(3, "x.md")]


def test_a_fence_inside_an_html_block_is_code_not_prose() -> None:
    """An HTML block's content is raw HTML, so nothing in it is a live link.

    The measured regression the span fallback above could not cover: markdown-it
    emits ONE token for the whole region, so a fence nested inside an HTML block
    produces no `fence` token, nothing enters the covered set, and the fallback
    strips spans only — it has no fence rule. Every link in the fence was then
    scanned as live prose, and an example link does not resolve by definition,
    so it was a hard `docs-consistency` failure on correct markdown. The
    `<details>` fold is the ordinary way to write this.

    The paired assertion is the link *after* the block: an implementation that
    blanked from the first `<div>` to end of file would also drop `y.md`.
    """
    text = "<div>\n```\n[a](x.md)\n```\n</div>\n\nafter [b](y.md)\n"

    assert csl.link_targets(text) == [(7, "y.md")]


def test_an_html_block_ends_at_a_blank_line_not_at_its_closing_tag() -> None:
    """Report finding 2: the boundary deciding whether a target is a link.

    An HTML block's contents are raw HTML rather than markdown, so a target
    written inside one is not a link and is correctly skipped. The block runs
    to the next *blank line*, not to the closing tag — so the line right after
    `</div>` is still inside it.

    The pair is what makes either half discriminating: a rule that skipped
    everything following any HTML would also satisfy the first assertion, and
    a rule that skipped nothing would satisfy the second.

    Both verdicts were checked against markdown-it's own renderer, which is the
    only authority that settles it: the two skipped spellings render zero `<a>`
    tags and the checked one renders exactly one. A review read the skipping as
    a silent loss of coverage; the renderer says there is nothing there to lose.
    """
    assert csl.link_targets("<div>\nx\n</div>\n[a](x.md)\n") == []
    assert csl.link_targets("<div>\nx\n</div>\n\n[a](x.md)\n") == [(5, "x.md")]

    # The comment spelling, and a bare `<br>` opening a type-6 block.
    assert csl.link_targets("<!-- [a](x.md) -->\n\n[b](y.md)\n") == [(3, "y.md")]
    assert csl.link_targets("<br>\nprose [a](x.md)\n\nafter [b](y.md)\n") == [
        (4, "y.md")
    ]


def test_a_span_the_parser_did_not_pair_does_not_blank_a_live_link() -> None:
    """The mask never contradicts the parser it was adopted to agree with.

    Block-wide pairing with `re.DOTALL` is a *candidate* finder: it scans left
    to right knowing nothing of the constructs that bind tighter than a code
    span. Measured, the backtick inside a link destination paired with the one
    opening the real span and blanked a genuinely dead link out of existence —
    markdown-it renders both of these as links, and `nope.md` does not resolve.
    A backslash-escaped backtick, which CommonMark says opens nothing, did the
    same. Both are silent: the gate stops reporting a dead link and exits 0.
    """
    destination = "see [a](x`y.md) and\n[b](nope.md) too `z`\n"
    escaped = "start \\` here\n[x](nope.md)\nend \\` there\n"

    assert csl.link_targets(destination) == [(1, "x`y.md"), (2, "nope.md")]
    assert csl.link_targets(escaped) == [(2, "nope.md")]
    # ...and the capability the guard must not cost: a real multi-line span.
    assert csl.link_targets("see `foo\n[a](nope.md) bar` end\n") == []


def test_masking_that_changes_a_block_line_count_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The line-count invariant fails loudly rather than silently declining.

    Reached only by injection, because `_blank_keeping_lines` preserves newlines
    by construction — but the guard is not decorative: the first version of this
    code blanked a multi-line span's newlines away, merged two source lines into
    one, and an `if count matches` guard then skipped the write-back and left the
    cross-line span unmasked. That is the exact half of the defect the parser
    change exists to fix, hidden by the check meant to make it safe. Every
    reported `file:line` past this point depends on the invariant, so a violation
    must not be survivable.

    **The guard this reaches has moved, and this note is here because the test
    did not move with it.** It was written against a check standing *after* the
    block-wide pairing; that check is gone, and the invariant is now asserted
    ahead of the pairing so both directions of a wrong count are visible. The
    test still passes and still discriminates — the new guard raises the same
    class with the same wording, earlier — but for one revision it named a site
    that no longer existed. Its pair,
    `test_masking_that_gains_a_line_raises_rather_than_dropping_the_tail`, is
    the direction the old placement structurally could not see.
    """

    def collapse_the_newline(_match: re.Match[str]) -> str:
        """A blank that drops the newline -- the bug the guard caught."""
        return " "

    monkeypatch.setattr(csl, "_blank_keeping_lines", collapse_the_newline)

    with pytest.raises(RuntimeError, match="changed the line count"):
        csl.code_masked_lines("a `x\ny` b\n")


def test_masking_that_gains_a_line_raises_rather_than_dropping_the_tail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The direction the count guard could not see while it stood after the zip.

    The pairing was `zip(..., strict=False)` and the check counted what came
    back from it. `zip` truncates to its shortest input, so a blanking that
    returned *more* lines than it was given still produced exactly one entry per
    source line: the count matched, nothing raised, and the block's tail simply
    vanished from the mask with every remaining line still the right length.

    That is the under-masking direction, which this module's own fallback
    comment records as the expensive one — a link left unmasked is reported, and
    an example link does not resolve by definition, so it fails a blocking gate
    on correct markdown.

    The test above injects the other direction and passed throughout, which is
    exactly why one-directional read as covered. Both now check the same guard,
    which is the point of moving it ahead of the pairing.
    """

    def split_the_span(match: re.Match[str]) -> str:
        """A blank that gains a line -- the direction the truncation hid."""
        return "\n" + " " * (len(match.group(0)) - 1)

    monkeypatch.setattr(csl, "_blank_keeping_lines", split_the_span)

    with pytest.raises(RuntimeError, match="changed the line count"):
        csl.code_masked_lines("a `x` b\n")


def test_a_span_bounded_by_spaces_is_reproduced_as_the_parser_normalizes_it() -> None:
    """`_code_span_content` strips one space from **each** end, or neither.

    This reproduces CommonMark's own normalization so a regex candidate can be
    compared against markdown-it's `code_inline` token. Get it wrong and the
    comparison fails on a *correct* span, `_spans_match_parser` returns False,
    the block drops to line-local blanking -- and a link that only exists
    mid-span, across lines, stops being masked. The gate then reports a dead
    link inside a code span: a false positive against valid CommonMark, on
    ordinary documentation prose.

    Measured, stripping only one end (`content[:-1]`) leaves the **rest of the
    suite** green while `link_targets` starts reporting `nope.md` from inside
    the span. The unit assertions below are what separate them, and the
    end-to-end case is asserted beside them because the unit ones alone were
    what the suite already lacked.

    No suite size is quoted, and that is the correction rather than an
    omission: this said "the entire 2368-test suite" and the suite collected
    2267 when a review checked, while the diff carrying the sentence only ever
    *adds* tests. The claim needs "everything else stayed green", which does
    not depend on the total; a total beside it is a measurement with a shelf
    life of one commit.
    """
    # The strip applies only when BOTH ends carry a space and the content is
    # not all spaces -- three boundaries, each of which a one-sided or
    # unconditional implementation gets wrong.
    assert csl._code_span_content(" x ") == "x"  # pyright: ignore[reportPrivateUsage]
    assert csl._code_span_content("x ") == "x "  # pyright: ignore[reportPrivateUsage]
    assert csl._code_span_content(" x") == " x"  # pyright: ignore[reportPrivateUsage]
    assert csl._code_span_content("  ") == "  "  # pyright: ignore[reportPrivateUsage]
    # A line ending inside a span becomes a space before any of that.
    assert csl._code_span_content(" a\nb ") == "a b"  # pyright: ignore[reportPrivateUsage]

    # ...and the consequence, end to end: a multi-line span whose inner text is
    # space-bounded must still mask the link inside it.
    raw = "multi:\n\n` line one\n[bad](nope.md)\n line two `\ntail\n"

    assert csl.link_targets(raw) == []


def test_a_candidate_matching_count_and_content_is_rejected_on_marker() -> None:
    """The third of `_spans_match_parser`'s three properties, and the last to be
    shown reachable.

    Count and normalized content are the two easy ones to construct a
    counter-example for. The delimiter-run comparison looked defensive: a
    smoke round mutated it out, found the suite green, and could not build an
    input where a spurious candidate matched the parser on both other
    properties while differing in run length. It reported that as unresolved
    rather than guessing, which was right -- the input exists.

    `\\`b`\\```b`` ` is it. The escape-blind regex sees the escaped backtick and
    the failed single-backtick opener as an ordinary one-backtick pair with
    content `b`. CommonMark excludes both from delimiter consideration and finds
    the real span two positions later, on the double-backtick run -- also with
    content `b`. So: one candidate, one token, contents equal, markers `` ` ``
    against ` `` `. Only the marker check separates them.

    Without it the block-wide substitution fires and blanks the wrong region,
    which is the silent direction: a link inside the real span stops being
    masked while a stretch of prose outside it is blanked instead.
    """
    raw = "\\`b`\\```b``"
    tokens = csl._parser().parse(raw)  # pyright: ignore[reportPrivateUsage]
    inline = next(token for token in tokens if token.type == "inline")
    parsed = [c for c in (inline.children or []) if c.type == "code_inline"]
    candidates = list(csl._BLOCK_SPAN_RE.finditer(raw))  # pyright: ignore[reportPrivateUsage]

    # The premise: the two agree on everything the other two properties check.
    assert len(candidates) == len(parsed) == 1, (candidates, parsed)
    marker = candidates[0].group(1)
    inner = candidates[0].group(0)[len(marker) : -len(marker)]
    assert csl._code_span_content(inner) == parsed[0].content  # pyright: ignore[reportPrivateUsage]
    assert marker != parsed[0].markup  # ...and differ only here

    # So the marker check is the only thing that can reject it, and must.
    assert csl._spans_match_parser(candidates, inline) is False  # pyright: ignore[reportPrivateUsage]
    # ...which sends the block to the line-local fallback rather than to the
    # block-wide substitution that would blank the wrong span. Three spaces, not
    # one: the fallback is length-preserving now, so this pins the blanked region
    # *and* the column invariant a caller reading the mask as a guide to the
    # source depends on. It said one space while the fallback collapsed a whole
    # span to a single character, which is exactly how a shortened line used to
    # reach such a caller unnoticed.
    assert csl.code_masked_lines(raw) == ["\\   \\```b``"]


def test_a_multi_line_span_behind_a_list_or_quote_marker_is_masked() -> None:
    """Report finding 2: the fallback un-masked every span a marker preceded.

    `_spans_match_parser` compares the regex's candidates against the parser's
    `code_inline` tokens. The block it was handed still carried the `- ` / `> ` /
    indent the parser had already stripped, so the content comparison could never
    agree inside a list item, a blockquote or a nested list. Every multi-line span
    there dropped to the line-local fallback, which cannot pair backticks across
    lines, and the link quoted inside one was reported dead -- a blocking
    docs-consistency failure on correct markdown, in the shape this repository
    writes most of its prose in.

    The paragraph case is the control: it passed throughout, which is why the
    defect read as "multi-line spans work" for as long as it did.
    """
    paragraph = "see `a\n[x](nope.md)\nb` end\n"
    assert csl.link_targets(paragraph) == []
    for marked in (
        "- see `a\n  [x](nope.md)\n  b` end\n",
        "> see `a\n> [x](nope.md)\n> b` end\n",
        "- outer\n  - see `a\n    [x](nope.md)\n    b` end\n",
    ):
        assert csl.link_targets(marked) == [], marked


@pytest.mark.parametrize(
    "trailer",
    ["  ", "\t", " ", "\u00a0"],
    ids=["hard-break", "tab", "one-space", "nbsp"],
)
@pytest.mark.parametrize(
    "placement",
    ["interior", "last", "both"],
)
def test_trailing_whitespace_does_not_unmask_a_multi_line_span(
    trailer: str, placement: str
) -> None:
    """Report finding 1: a trailing run made the span stop being a span.

    markdown-it sets an inline token's content with `.strip()`, so a line
    carrying any trailing whitespace does not end with its own content.
    `_block_prefixes` returned None, the block fell back to line-local
    blanking, which cannot pair backticks across lines, and the link quoted
    inside the span was reported dead -- a blocking gate failure on correct
    markdown, which is the outcome that function exists to prevent.

    Two trailing spaces are a legal CommonMark hard break and U+00A0 is
    invisible to MD009, so neither is caught by the style gate; `spec-links`
    runs ahead of `markdown-lint` in any case, so the author meets the bogus
    dead link first.

    **`placement` is the half a later review found missing, and it is the half
    that matters most.** `.strip()` runs once over the block's *joined* content,
    so it can only reach the first line's leading run and the last line's
    trailing one -- an interior line keeps its whole trailing run inside
    `token.content`. This test pinned the trailer on the last line only, which
    is the one placement an unconditional `rstrip()` repairs, so the repair
    passed here while regressing every interior line. A hard break is by
    definition never a paragraph's last line, which makes `interior` the
    placement the motivating shape actually has.

    The no-trailer control is the discriminating half: it passed throughout,
    which is why the defect read as "multi-line spans work".
    """
    assert csl.link_targets("a `foo\n[bad](nope.md) bar` end\n") == []

    interior = trailer if placement in ("interior", "both") else ""
    last = trailer if placement in ("last", "both") else ""
    assert csl.link_targets(f"a `foo{interior}\n[bad](nope.md) bar` end{last}\n") == []


def test_masking_preserves_every_line_length_not_only_the_line_count() -> None:
    """The invariant a caller reading the mask as a column guide depends on.

    `code_masked_lines` checked that it returned one entry per source line and
    did not check that each entry was as *long* as the line it describes, so the
    span-only fallback could shorten one silently. Asserted over the fallback
    path (a candidate the marker check rejects), the block-wide path, and the
    line-local path for a line the parser attributed to no token.
    """
    for raw in (
        "\\`b`\\```b``",  # marker check rejects -> line-local fallback
        "- see `a\n  [x](y.md)\n  b` end\n",  # block-wide, behind a list marker
        "see `a\n[x](y.md)\nb` end\n",  # block-wide, plain paragraph
        "a `b` c\n\n> quoted `d` e\n",  # several blocks in one document
        # Report finding 6: the three block types used to be blanked to "",
        # so the rule two docstrings state held everywhere except the branches
        # that carried most of the corpus -- measured, 1029 *non-empty* lines
        # across 54 of 130 files (a line already "" is not shortened by being
        # set to ""; counting the blanks too gives 1067). The guards protecting
        # it sit on the inline branch, which cannot break it by construction,
        # so nothing ever fired.
        "text:\n\n```\n[a](x.md)\n```\n\nafter\n",  # fenced block
        "text:\n\n    [a](x.md)\n\nafter\n",  # indented code block
        "<div>\n[a](x.md)\n</div>\n\nafter\n",  # html block
        "1. Example:\n\n    ```\n    [a](x.md)\n    ```\n",  # fence in a list
    ):
        lines = csl.LINE_BREAK_RE.split(raw)
        masked = csl.code_masked_lines(raw)
        assert len(masked) == len(lines), raw
        for got, source in zip(masked, lines, strict=True):
            assert len(got) == len(source), (raw, source, got)
