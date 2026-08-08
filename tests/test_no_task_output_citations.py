"""No `.claude/` file cites a `tasks/*.output` path.

The harness writes each subagent's transcript pointer as a `tasks/<id>.output`
file, and on this project those files are reliably empty -- 0 bytes, measured
on three separate multi-agent review passes (two recorded in the
review-handoff limitations diagnosis, a third on the ADR-0068 pass-10 round,
where all ten finder agents returned 0-byte output files and ~25 findings
survived only because a transcribing session captured them as they arrived).
A skill or agent file that tells a session to *read* such a path sends it to
an empty file, and whatever the pointer promised is silently lost. The
review-pipeline skills are being rewritten against this rule; the durable
records are CLAUDE.md (Subagent output pointers) and the open-questions entry
carrying the expiry trigger (if a future harness version starts populating
those files, the rule loses its basis), and this module is what keeps a later
edit from reintroducing the pattern.

The scan is textual, so a `.claude/` file cannot even *quote* the banned path
shape -- unlike the `except`-convention gate there is no AST to separate a
rule statement from a live citation. That is deliberate and cheap: state the
prohibition by description ("the harness's 0-byte agent output files") and
cite this test or CLAUDE.md for the reasoning, rather than writing the
literal path. Files outside `.claude/` (specs, CLAUDE.md, this module) may
quote the pattern freely; only the files a session executes as instructions
are gated.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

# Keep the developer's global/system git config out of fixture repos (a global
# init.templateDir or commit.gpgsign breaks them for environmental reasons) --
# same neutralization as the sibling gate test modules.
_NEUTRAL_GIT_ENV = {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull}

# The tree the live scan runs against. Module-level so the fixture tests below
# can point the scan at a synthetic tmp repo (the same monkeypatch pattern as
# test_check_spec_links.py) instead of writing probe files into the real,
# shared .claude/ tree mid-session.
SCAN_ROOT = REPO_ROOT

# `tasks/<anything>.output` with either separator: a forward-slash path, a
# Windows backslash path, and the literal glob form `tasks/*.output` all match
# (\S covers `*`). The trailing \b keeps `tasks/foo.outputs` clean while still
# catching `tasks/foo.output.md` -- a suffixed citation still points a reader
# into the ephemeral tasks directory.
CITATION_RE = re.compile(r"tasks[/\\]\S*\.output\b")


def task_output_citations(text: str) -> list[int]:
    """1-based line numbers of every `tasks/*.output` citation in `text`."""
    return [
        lineno
        for lineno, line in enumerate(text.splitlines(), start=1)
        if CITATION_RE.search(line)
    ]


def claude_files() -> list[Path]:
    """Every file git considers part of `SCAN_ROOT/.claude/`, plus new ones.

    Same contract as the `except`-convention gate's file set: `--cached` is the
    tracked set, `--others --exclude-standard` adds files written but not yet
    added -- a fresh skill draft is exactly where a new citation is most likely
    -- while git's ignore rules keep `settings.local.json` (machine-local,
    gitignored) out without a prune list here.
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
            ".claude/",
        ],
        cwd=SCAN_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    if proc.returncode != 0:
        # Attach git's own stderr: a bare CalledProcessError says only "exit
        # status 128", which blames this rule for what is a git failure.
        raise RuntimeError(
            f"git ls-files failed (exit {proc.returncode}): {proc.stderr.strip()}"
        )
    paths = (SCAN_ROOT / name for name in proc.stdout.split("\0") if name)
    # `--cached` still names a file staged for deletion, which is not on disk.
    return sorted({path for path in paths if path.is_file()})


def offending_citations(files: list[Path]) -> list[str]:
    """`path:line` for every citation in `files` (paths relative to SCAN_ROOT)."""
    offenders: list[str] = []
    for path in files:
        relative = path.relative_to(SCAN_ROOT).as_posix()
        # utf-8-sig: a BOM is what CLAUDE.md's PowerShell encoding footgun
        # produces, and it should not turn this into a decode error.
        # errors="replace", not a skip-on-UnicodeDecodeError guard: the same
        # footgun writes cp1252 *text*, and a skip would fail OPEN on exactly
        # the files most likely to carry a mis-encoded citation (measured by
        # external review 2 -- a cp1252 SKILL.md with a live citation passed).
        # The citation regex is pure ASCII, so replacement characters cannot
        # fabricate a match in a binary; a binary that literally contains the
        # banned ASCII bytes is flagged loudly instead, which is the right
        # failure direction for a containment-style gate.
        text = path.read_text(encoding="utf-8-sig", errors="replace")
        offenders += [f"{relative}:{line}" for line in task_output_citations(text)]
    return offenders


# --- the detector, against fixtures ----------------------------------------
#
# A scan that silently matches nothing passes forever. These fixtures prove the
# live test reports zero because .claude/ is clean, not because the regex rotted.


def test_a_forward_slash_citation_is_reported() -> None:
    assert task_output_citations("read tasks/abc123.output for findings\n") == [1]


def test_a_backslash_citation_is_reported() -> None:
    assert task_output_citations("read tasks\\abc123.output\n") == [1]


def test_the_glob_form_is_reported() -> None:
    assert task_output_citations("collect tasks/*.output afterwards\n") == [1]


def test_a_suffixed_citation_is_still_reported() -> None:
    # A path continuing past `.output` still points into the tasks directory.
    assert task_output_citations("see tasks/abc.output.md\n") == [1]


def test_every_citing_line_is_reported_not_just_the_first() -> None:
    text = "tasks/a.output\nclean line\ntasks/b.output\n"
    assert task_output_citations(text) == [1, 3]


def test_unrelated_prose_is_not_reported() -> None:
    assert (
        task_output_citations(
            "the tasks directory holds output files\n"
            "write results to scratchpad/findings.md, never rely on agent "
            "output pointers\n"
            "tasks/foo.outputs is a different word\n"
        )
        == []
    )


# --- the file set ----------------------------------------------------------


def _synthetic_claude_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A tmp git repo with a .claude/ tree, and SCAN_ROOT pointed at it."""
    claude = tmp_path / ".claude" / "skills"
    claude.mkdir(parents=True)
    # S603/S607 waivers on the git calls: PATH-resolved git, literal args.
    subprocess.run(
        ["git", "init", "-q"],  # noqa: S607
        cwd=tmp_path,
        check=True,
        env={**os.environ, **_NEUTRAL_GIT_ENV},
    )
    monkeypatch.setattr(sys.modules[__name__], "SCAN_ROOT", tmp_path)
    return claude


def test_untracked_files_are_scanned_and_ignored_ones_are_not(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The `--others` half of the file set is load-bearing (a fresh skill draft
    # is exactly where a new citation is most likely) and the `--exclude-
    # standard` half is what keeps machine-local ignored files out. Without
    # this test, dropping `--others` leaves every other test green (measured
    # by the test-reviewer's mutation run on the first version of this module).
    claude = _synthetic_claude_repo(tmp_path, monkeypatch)
    (tmp_path / ".gitignore").write_text(".claude/local.md\n", encoding="utf-8")
    (claude / "tracked.md").write_text("clean\n", encoding="utf-8")
    subprocess.run(
        ["git", "add", ".claude/skills/tracked.md"],  # noqa: S607
        cwd=tmp_path,
        check=True,
        env={**os.environ, **_NEUTRAL_GIT_ENV},
    )
    (tmp_path / ".claude" / "draft.md").write_text("untracked draft\n", "utf-8")
    (tmp_path / ".claude" / "local.md").write_text("ignored\n", encoding="utf-8")
    found = {p.relative_to(tmp_path).as_posix() for p in claude_files()}
    assert found == {".claude/skills/tracked.md", ".claude/draft.md"}


def test_a_binary_file_does_not_crash_the_scan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # claude_files() enumerates every file type under .claude/, not just
    # markdown; a non-UTF-8 file (a pasted screenshot, an archive) must not
    # turn the gate red with a UnicodeDecodeError naming the wrong problem.
    claude = _synthetic_claude_repo(tmp_path, monkeypatch)
    binary = claude / "diagram.png"
    binary.write_bytes(b"\x89PNG\r\n\x1a\n\xff\xfe\x00\x01\xff\xd8\xda")
    citing = claude / "draft.md"
    citing.write_text("read tasks/abc.output\n", encoding="utf-8")
    assert offending_citations([binary, citing]) == [".claude/skills/draft.md:1"]


def test_a_binary_containing_the_banned_bytes_is_flagged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Fail-closed direction: a binary whose bytes literally spell the banned
    # ASCII path is reported rather than skipped. (The earlier skip-on-decode-
    # error guard hid it -- and with it any mis-encoded text file, the real
    # hazard the next fixture pins.)
    claude = _synthetic_claude_repo(tmp_path, monkeypatch)
    binary = claude / "blob.bin"
    binary.write_bytes(b"\x89PNG\r\n\xff tasks/x.output \xff")
    assert offending_citations([binary]) == [".claude/skills/blob.bin:2"]


def test_a_cp1252_text_file_is_still_scanned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The reproduced round-2 hole: CLAUDE.md's PowerShell footgun writes
    # cp1252 text, whose em dash is invalid UTF-8. A skip-on-decode-error
    # guard failed OPEN here -- a live citation in exactly the file class most
    # likely to be mis-encoded passed the gate. errors="replace" keeps the
    # ASCII citation matchable regardless of the mangled non-ASCII bytes.
    claude = _synthetic_claude_repo(tmp_path, monkeypatch)
    skill = claude / "SKILL.md"
    skill.write_bytes("Step 1 — read tasks/abc.output now\n".encode("cp1252"))
    assert offending_citations([skill]) == [".claude/skills/SKILL.md:1"]


def test_a_bom_prefixed_file_is_scanned_not_crashed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The PowerShell encoding footgun produces BOM-prefixed UTF-8; the scan
    # must still read the file and still find a citation on line 1 (a plain
    # utf-8 read would keep the BOM glued to the first line's text).
    claude = _synthetic_claude_repo(tmp_path, monkeypatch)
    bom_file = claude / "draft.md"
    bom_file.write_bytes(b"\xef\xbb\xbfread tasks/abc.output now\n")
    assert offending_citations([bom_file]) == [".claude/skills/draft.md:1"]


def test_the_scan_reaches_the_claude_tree() -> None:
    # Without this, a `git ls-files` returning nothing (wrong cwd, renamed
    # directory) would make the live test pass by scanning an empty set.
    found = [path.relative_to(SCAN_ROOT).as_posix() for path in claude_files()]
    for prefix in (".claude/skills/", ".claude/agents/"):
        assert any(path.startswith(prefix) for path in found), (
            f"no file under {prefix} is being scanned -- either the scan "
            "stopped reaching it, or that directory is gone and this list "
            "should drop it"
        )


def test_a_git_failure_carries_gits_own_explanation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # When the scan cannot run at all, the failure has to name git as the
    # cause; left to CalledProcessError it reads as a bare "exit status 128"
    # from a test about citation hygiene. Same contract as the sibling
    # `except`-convention gate's helper.
    def fake_run(*_args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        # Pin `check=False`: the helper handles the non-zero exit itself so it
        # can attach git's stderr; a revert to check=True must not hide here.
        assert kwargs["check"] is False
        return subprocess.CompletedProcess(
            args=["git", "ls-files"],
            returncode=128,
            stdout="",
            stderr="fatal: detected dubious ownership in repository at '/repo'\n",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(RuntimeError) as caught:
        claude_files()
    message = str(caught.value)
    assert "128" in message
    assert "dubious ownership" in message


# --- the live contract -----------------------------------------------------


def test_no_claude_file_cites_a_task_output_path() -> None:
    offenders = offending_citations(claude_files())
    assert not offenders, (
        "a .claude/ file cites the harness's agent output files at "
        + ", ".join(offenders)
        + " -- those files are reliably 0 bytes on this project (measured on "
        "three review passes), so a session following the citation reads an "
        "empty file and the pointed-at content is silently lost. Capture agent "
        "results into your own scratchpad file as they arrive instead. If the "
        "line is a rule *statement*, state it by description rather than the "
        "literal path (see this test's docstring)."
    )
