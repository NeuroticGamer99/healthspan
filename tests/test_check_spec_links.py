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
