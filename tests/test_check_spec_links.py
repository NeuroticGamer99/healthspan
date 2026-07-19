"""Unit tests for the spec link-check gate (scripts/check_spec_links.py).

Two layers: the pure extractors (strip_code_spans, link_targets, resolve_target)
against constructed strings, and check()/main() against a throwaway specs tree
under tmp_path -- never specs/, so a test link can never depend on real repo
layout, and a real dead link in the repo is caught by the gate itself, not here.
"""

import os
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
