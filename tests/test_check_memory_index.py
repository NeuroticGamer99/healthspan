"""The auto-memory index reconciler (scripts/check_memory_index.py).

The checker exists because two memories were orphaned on this project in one day
and neither write accomplished anything — an unindexed memory is never loaded.
That failure is silent, so every assertion below builds a directory that
reproduces one shape and requires the checker to notice it.

Two behaviours are pinned as *warnings on purpose*, and a future change that
promotes either to an error should have to delete a test saying why: a
forward-reference wiki-link is legitimate under the memory convention, and a
near-limit index is a heads-up rather than a fault.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import check_memory_index as gate
import pytest

_ROW = "| [{name}.md]({name}.md) | project | hook |"

# The four sibling test modules that shell out to git all carry this, and
# `tests/test_ledger.py` records why: a developer with `commit.gpgsign` set
# globally fails every fixture commit at exit 128. A rejecting `core.hooksPath`
# or `gpg.format=ssh` does the same. Without it the branch's two headline tests
# fail for a reason unrelated to the code they exercise.
_NEUTRAL_GIT_ENV = {
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_SYSTEM": os.devnull,
}


def _git_env() -> dict[str, str]:
    return {**os.environ, **_NEUTRAL_GIT_ENV}


def _memory(tmp_path: Path, files: dict[str, str], *, index: str | None = None) -> Path:
    """A memory directory holding `files`, with an index linking all of them.

    A body that does not open its own frontmatter gets a conforming one, so a
    fixture written to exercise some *other* check does not also trip the
    identity check. A body that opens `---` is left exactly as written — that
    is how the identity tests declare a mismatch.
    """
    root = tmp_path / "memory"
    root.mkdir()
    for name, body in files.items():
        if not body.startswith("---"):
            body = f"---\nname: {name}\n---\n\n{body}"
        (root / f"{name}.md").write_text(body, encoding="utf-8", newline="\n")
    if index is None:
        rows = "\n".join(_ROW.format(name=name) for name in files)
        index = f"# Memory Index\n\n| File | Type | Description |\n|--|--|--|\n{rows}\n"
    (root / gate.INDEX_NAME).write_text(index, encoding="utf-8", newline="\n")
    return root


def test_a_consistent_directory_reconciles(tmp_path: Path) -> None:
    report = gate.check(_memory(tmp_path, {"alpha": "a\n", "beta": "b\n"}))
    assert report.errors == []
    assert report.examined["files"] == 2
    assert report.examined["index-rows"] == 2


def test_the_evidence_line_names_each_source(tmp_path: Path) -> None:
    """A clean result has to state what it examined, not just that it passed."""
    report = gate.check(_memory(tmp_path, {"alpha": "a\n"}))
    assert "files 1" in report.evidence()
    assert gate.Report().evidence() == "nothing examined"


def test_an_orphaned_file_is_an_error(tmp_path: Path) -> None:
    """The measured failure: a memory written whose index row never landed.

    It is invisible rather than wrong — `MEMORY.md` is what loads at session
    start, so nothing ever recalls the file and the write did nothing.
    """
    root = _memory(tmp_path, {"alpha": "a\n"})
    (root / "orphan.md").write_text("never indexed\n", encoding="utf-8", newline="\n")

    errors = gate.check(root).errors
    assert any("orphan.md" in e for e in errors), errors
    assert any("never loaded" in e for e in errors), errors


def test_a_dangling_row_is_an_error(tmp_path: Path) -> None:
    root = _memory(tmp_path, {"alpha": "a\n"})
    (root / "alpha.md").unlink()
    assert any("dangling" in e for e in gate.check(root).errors)


@pytest.mark.parametrize("dimension", ["lines", "bytes"])
def test_an_index_past_the_load_limit_is_an_error(
    tmp_path: Path, dimension: str
) -> None:
    """Past the limit the tail stops loading while the file still looks whole.

    That is the same silent-truncation shape as an orphan: the index reads as
    complete to anyone opening it, and the rows past the cut simply never reach
    a session.
    """
    if dimension == "lines":
        index = "\n".join(f"| row {n} |" for n in range(gate.LINE_LIMIT + 5))
    else:
        index = "| " + ("x" * (gate.BYTE_LIMIT + 100)) + " |"
    root = _memory(tmp_path, {}, index=index)

    errors = gate.check(root).errors
    assert any("past the load limit" in e for e in errors), errors
    assert any("silently dropped" in e for e in errors), errors


def test_a_near_limit_index_warns_but_does_not_fail(tmp_path: Path) -> None:
    """Deliberately a warning: the useful signal has to arrive *before* the
    rows start disappearing, and an error here would fail a healthy index."""
    count = int(gate.LINE_LIMIT * gate.WARN_FRACTION) + 5
    root = _memory(tmp_path, {}, index="\n".join(f"| row {n} |" for n in range(count)))

    report = gate.check(root)
    assert report.errors == []
    assert any("nearing the load limit" in w for w in report.warnings), report.warnings


def test_frontmatter_and_comments_do_not_count_toward_the_limit(tmp_path: Path) -> None:
    """The loader strips both before measuring, so this must too.

    Counting them would fail an index that loads perfectly well — the harness
    itself had this bug and fixed it in v2.1.211.
    """
    filler = "\n".join(f"| row {n} |" for n in range(10))
    body = f"---\n{'x: y' * 200}\n---\n<!--\n{'c' * 30000}\n-->\n{filler}\n"
    assert gate.check(_memory(tmp_path, {}, index=body)).errors == []


def test_a_separator_typo_in_a_wiki_link_is_an_error(tmp_path: Path) -> None:
    """A link differing only in `-` vs `_` resolves to nothing, silently.

    The checker's first run against the live directory reported 12 of these and
    **7 were false** — they resolved by the target's frontmatter `name:`, which
    is what the convention resolves by. That is why the identity check exists;
    with `name:` pinned to the filename, a separator difference is once again a
    real typo, which is what this test holds.
    """
    root = _memory(tmp_path, {"alpha_beta": "a\n", "caller": "see [[alpha-beta]]\n"})

    errors = gate.check(root).errors
    assert any("dead wiki-link" in e for e in errors), errors
    assert any("alpha_beta" in e for e in errors), errors


def test_a_forward_reference_wiki_link_only_warns(tmp_path: Path) -> None:
    """Pinned as a warning permanently. The memory convention states that a
    link to a memory that does not exist yet is legitimate — it marks one worth
    writing later — so promoting this to an error breaks the convention it
    serves. A change that makes it fail should have to delete this test."""
    root = _memory(tmp_path, {"caller": "later: [[not_written_yet]]\n"})

    report = gate.check(root)
    assert report.errors == []
    assert any("not_written_yet" in w for w in report.warnings), report.warnings
    assert report.examined["wiki-links-forward"] == 1


def test_a_name_that_is_not_the_filename_stem_is_an_error(tmp_path: Path) -> None:
    """The defect that made the first draft's headline finding wrong.

    The convention resolves `[[name]]` by the target's frontmatter `name:`;
    this checker resolves by filename. While the two disagree, a link is dead
    for one reader and live for the other, and the checker reports the
    disagreement as damage. 17 of 90 live memories had diverged.
    """
    root = _memory(tmp_path, {"alpha_beta": "---\nname: alpha-beta\n---\n\nbody\n"})

    errors = gate.check(root).errors
    assert any("is not its filename stem" in e for e in errors), errors
    assert gate.check(root).examined["identity-mismatches"] == 1


def test_a_memory_with_no_name_field_is_an_error(tmp_path: Path) -> None:
    """Nothing can resolve a link to it by name, so it is unreachable the same
    way an orphan is — silently, and only from the other end."""
    root = _memory(tmp_path, {"alpha": "---\ndescription: no name here\n---\n\nb\n"})

    assert any("declares no frontmatter" in e for e in gate.check(root).errors)


@pytest.mark.parametrize(
    "body",
    [
        "the syntax is `[[foo-bar]]` where foo_bar.md exists\n",
        "a doubled fence:\n\n```\nsee [[foo-bar]]\n```\n\ndone\n",
        "indented fence:\n\n  ```md\n  [[foo-bar]]\n  ```\n\ndone\n",
    ],
    ids=["inline-span", "fenced-block", "indented-fence"],
)
def test_a_wiki_link_inside_code_is_an_example_not_a_link(
    tmp_path: Path, body: str
) -> None:
    """Measured false positive: the memory that *specifies* this checker writes
    `[[foo-bar]]` in backticks to document the syntax, and the first draft
    reported it as a dead cross-reference. A checker meant to run often cannot
    cry wolf — that trains the operator to skim past it, which is worse than
    having no check at all."""
    root = _memory(tmp_path, {"foo_bar": "real\n", "spec": body})

    report = gate.check(root)
    assert report.errors == []
    assert report.warnings == []


def test_a_link_missing_its_type_prefix_is_dead_not_a_forward_reference(
    tmp_path: Path,
) -> None:
    """Measured false *negative*: a link spelled with the type prefix dropped
    and hyphens for underscores matched nothing under separator flattening, so
    it landed in the warning list as a legitimate intention to write that memory
    later. Two live links of that shape were sitting in the corpus. A checker's
    silence is read as absence of a problem, which makes a false negative the
    more expensive direction."""
    root = _memory(tmp_path, {"reference_thing": "r\n", "caller": "[[thing]]\n"})

    report = gate.check(root)
    assert any("reference_thing" in e for e in report.errors), report
    assert report.warnings == []


def test_an_ambiguous_unprefixed_link_names_every_candidate(tmp_path: Path) -> None:
    """Still dead, so still an error — but it names both rather than guessing
    one, because a confident wrong suggestion costs more than a vague right
    one."""
    root = _memory(
        tmp_path,
        {"project_thing": "p\n", "feedback_thing": "f\n", "caller": "[[thing]]\n"},
    )

    errors = gate.check(root).errors
    assert any("feedback_thing, project_thing" in e for e in errors), errors


def test_stripping_code_does_not_blind_the_dead_link_check(tmp_path: Path) -> None:
    """The guard on the guard: a dead link in ordinary prose on the same line
    as an unrelated code span must still be caught."""
    root = _memory(
        tmp_path, {"alpha_beta": "a\n", "caller": "`code` then [[alpha-beta]]\n"}
    )

    assert any("dead wiki-link" in e for e in gate.check(root).errors)


@pytest.mark.parametrize(
    ("left", "right", "same"),
    [
        ("a-b-c", "a_b_c", True),
        ("Feedback_X", "feedback_x", True),
        ("alpha", "beta", False),
    ],
)
def test_normalize_only_flattens_separator_and_case(
    left: str, right: str, same: bool
) -> None:
    normalize = gate._normalize  # pyright: ignore[reportPrivateUsage]
    assert (normalize(left) == normalize(right)) is same


def test_the_project_slug_matches_the_observed_directory_name() -> None:
    """Derivation is observed, not documented, so pin the one known-good case."""
    assert gate.project_slug(Path(r"c:\Dev\health\healthspan")) == (
        "c--Dev-health-healthspan"
    )


def test_a_relocated_memory_directory_is_refused_not_guessed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`autoMemoryDirectory` moves the real directory elsewhere.

    Reconciling the derived default would then report on a directory nobody
    uses — a checker pointed at the wrong tree reports clean about it, which is
    the failure this whole class of script exists to stop.
    """
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"autoMemoryDirectory": "~/elsewhere"}), "utf-8")
    monkeypatch.setattr(gate, "_user_settings", lambda: (settings,))

    with pytest.raises(gate.ReconcileError) as excinfo:
        gate.resolve_memory_dir(Path.cwd())
    assert "autoMemoryDirectory" in str(excinfo.value)


def test_an_unresolvable_project_directory_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(gate, "_user_settings", tuple)

    def fake_home(_cls: type[Path]) -> Path:
        return tmp_path

    monkeypatch.setattr(Path, "home", classmethod(fake_home))
    with pytest.raises(gate.ReconcileError):
        gate.resolve_memory_dir(Path(r"c:\nowhere\at\all"))


def test_a_missing_index_raises_rather_than_passing_empty(tmp_path: Path) -> None:
    """No index is a broken memory directory, not a clean one."""
    root = tmp_path / "memory"
    root.mkdir()
    (root / "alpha.md").write_text("a\n", encoding="utf-8", newline="\n")
    with pytest.raises(gate.ReconcileError):
        gate.check(root)


def test_main_reports_errors_and_exits_1(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _memory(tmp_path, {"alpha": "a\n"})
    (root / "orphan.md").write_text("x\n", encoding="utf-8", newline="\n")

    assert gate.main(["--memory-dir", str(root)]) == 1
    out = capsys.readouterr().out
    assert "orphan.md" in out
    assert "never rewrite" in out, "the remedy must name the anchored-Edit rule"


def test_main_is_silent_when_clean_and_quiet(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`--quiet` is what makes this bearable for a caller that runs often: a
    clean run must cost nothing, or the caller stops running it."""
    root = _memory(tmp_path, {"alpha": "a\n"})
    assert gate.main(["--memory-dir", str(root), "--quiet"]) == 0
    assert capsys.readouterr().out == ""


def test_main_still_speaks_when_quiet_but_warning(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A warning under `--quiet` must not be swallowed — a silenced heads-up is
    the same as no heads-up."""
    root = _memory(tmp_path, {"caller": "[[not_written_yet]]\n"})
    assert gate.main(["--memory-dir", str(root), "--quiet"]) == 0
    assert "not_written_yet" in capsys.readouterr().out


# --------------------------------------------------------------------------
# --skip-if-absent — the flag an unattended caller adds, and the line it must not cross
# --------------------------------------------------------------------------


def test_an_absent_memory_directory_is_silent_under_skip_if_absent(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The worktree case, which is this project's ordinary working mode.

    Measured 2026-08-19: each `.claude/worktrees/` checkout gets its own Claude
    Code project directory and none of them holds a `memory/`. An absent
    directory is not an inconsistent one — no memory has been written, so there
    is no index for anything to disagree with — and reporting a fault on every
    run in the mode the repository uses most is how a check earns being
    switched off.
    """
    absent = tmp_path / "never-used" / "memory"
    assert gate.main(["--memory-dir", str(absent), "--skip-if-absent"]) == 0
    assert capsys.readouterr().out == ""


def test_skip_if_absent_does_not_excuse_a_directory_with_no_index(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The flag forgives *absence*, not *breakage*, and the two are one
    character apart in the code. A directory holding memories but no
    `MEMORY.md` is the broken case `check` refuses on purpose, and widening
    the skip to cover it would silence every memory in it at once."""
    root = tmp_path / "memory"
    root.mkdir()
    (root / "alpha.md").write_text("a\n", encoding="utf-8", newline="\n")
    assert gate.main(["--memory-dir", str(root), "--skip-if-absent"]) == 1
    assert "MEMORY.md" in capsys.readouterr().out


def test_an_absent_directory_is_still_an_error_without_the_flag(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A caller who named a directory deserves to hear that it is missing.
    Only a caller naming none and running unasked buys the silence."""
    absent = tmp_path / "never-used" / "memory"
    assert gate.main(["--memory-dir", str(absent)]) == 1
    assert "not a directory" in capsys.readouterr().out


REPO_ROOT = Path(__file__).resolve().parent.parent
HOOK_SCRIPT = REPO_ROOT / "scripts" / "check_memory_index.py"


def _init_fixture_repo(repo: Path) -> None:
    """A *real* repository holding both scripts the checker needs.

    An empty `.git` directory is not a gitdir, so `git rev-parse` walked past it
    to whatever real repository encloses `tmp_path` — resolving the fixture to
    the wrong root, and doing it invisibly, because the silence-asserting tests
    cannot tell "the checker ran and had nothing to say" from "the checker never
    ran". `git init` makes the fixture answer for itself.

    Both scripts, because the checker imports `check_spec_links` for its
    markdown rules rather than carrying a fifth private copy. A checkout has
    both; a fixture simulating one must too.
    """
    (repo / "scripts").mkdir(parents=True, exist_ok=True)
    for script in (HOOK_SCRIPT, HOOK_SCRIPT.parent / "check_spec_links.py"):
        (repo / "scripts" / script.name).write_bytes(script.read_bytes())
    subprocess.run(
        ["git", "init", "-q"],  # noqa: S607
        cwd=repo,
        check=True,
        capture_output=True,
        env=_git_env(),
    )


# --------------------------------------------------------------------------
# Review round 1 — the three deliberate branches whose own comments cite a
# measured failure mode, and which shipped with no coverage of it
# --------------------------------------------------------------------------


def test_a_case_only_directory_mismatch_is_matched(tmp_path: Path) -> None:
    """The Windows same-repo-two-casings branch, tested where it can be tested.

    `project_slug` keeps whatever casing the path was spelled with, so the same
    repository reached as `c:\\Dev\\...` and `C:\\Dev\\...` derives two slugs.
    Driving `resolve_memory_dir` end to end would prove this on one CI leg only:
    Windows and macOS's default volumes are case-insensitive, so `projects /
    <slug>` already resolves to the differently-cased directory and the function
    returns before the fallback runs. Measured 2026-08-19 — this machine's
    filesystem resolves `caseprobe` to a directory created as `CaseProbe`. The
    matcher is therefore tested directly, where its own casefold comparison is
    what answers on every platform.
    """
    projects = tmp_path / "projects"
    (projects / "C--Dev-Health-Healthspan").mkdir(parents=True)

    matched = gate._case_insensitive_match(  # pyright: ignore[reportPrivateUsage]
        projects, "c--dev-health-healthspan"
    )
    assert matched is not None
    assert matched.name == "C--Dev-Health-Healthspan"


def test_a_slug_matching_nothing_is_not_guessed_at(tmp_path: Path) -> None:
    """The other half of the same branch: no match must be `None`, not the
    first directory that happens to be there. A matcher that guesses points the
    reconciliation at another project's corpus, and `check` would then report
    every memory in it as orphaned — the loudest possible wrong answer."""
    projects = tmp_path / "projects"
    (projects / "C--Dev-something-else").mkdir(parents=True)

    assert (
        gate._case_insensitive_match(  # pyright: ignore[reportPrivateUsage]
            projects, "c--dev-health-healthspan"
        )
        is None
    )


@pytest.mark.parametrize(
    "body",
    [
        "~~~\n[[foo-bar]]\n~~~\n",
        "~~~\na ` tick inside\n[[foo-bar]]\n~~~\n",
        "  ~~~\n  [[foo-bar]]\n  ~~~\n",
        "```\n[[foo-bar]]\n",
        "~~~\n[[foo-bar]]\n",
    ],
    ids=["tilde", "tilde-inner-tick", "indented-tilde", "unclosed", "unclosed-tilde"],
)
def test_only_the_fence_pass_can_strip_these(tmp_path: Path, body: str) -> None:
    """The cases that make the fenced-block pass load-bearing, measured.

    The existing parametrized cases cannot show this: `_INLINE_CODE` pairs the
    opening and closing runs of a *balanced backtick* fence on its own, so every
    one of them is stripped whether or not the fence pass runs, and deleting
    that pass leaves the whole suite green. Measured 2026-08-19 over eight
    candidate bodies, exactly two shapes survive inline stripping as live text
    and reach the wiki-link scan: a **tilde** fence, which has no backticks to
    pair, and an **unclosed** fence running to end of file. Both are how this
    corpus writes examples, so both would cry wolf every session.

    A body that is *not* here and deliberately so: a backtick fence holding a
    shorter inner run. The module comment used to name that as the reason for
    the ordering; it is stripped either way, so it proves nothing.
    """
    root = _memory(tmp_path, {"foo_bar": "real\n", "spec": body})

    report = gate.check(root)
    assert report.errors == [], report.errors
    assert report.warnings == [], report.warnings


def test_declared_name_drops_a_carriage_return(tmp_path: Path) -> None:
    """The CRLF guard, driven where it is reachable.

    A fixture written through `check` cannot reach this whatever its line
    endings: every read in the module is `read_text(encoding="utf-8")`, and
    universal-newline translation turns \r\n into \n before the pattern runs
    (measured — a CRLF fixture through `check` leaves the guard's mutant green).
    So the guard is a property of `declared_name` as a function, and that is
    where it is asserted. The caller it protects is one reading with
    `newline=""`, which is what the 2026-08-19 sweep's one-shot scripts did when
    they counted 19 divergences where there were 17.
    """
    assert gate.declared_name("---\r\nname: alpha_beta\r\n---\r\n\r\nbody\r\n") == (
        "alpha_beta"
    )
    assert gate.declared_name("---\nname: alpha_beta\n---\n\nbody\n") == "alpha_beta"


def test_resolve_memory_dir_returns_what_the_matcher_found(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The wiring itself, on every platform, with nothing to skip over.

    This exists because the end-to-end test below **can** decline to run. That
    one needs the filesystem to keep a casefold-equal pair apart, and where a
    platform folds them it skips — which, measured, is invisible: this project's
    gate and both CI legs run pytest with `-v` and no `-r`, so a skip prints
    without its reason and reads as one of the 27 that are already there. A
    guard whose sense was wrong would look exactly the same, and the wiring
    would quietly have no cover at all.

    So the coverage does not live behind that guard. Here the matcher is
    replaced by a recording stand-in, which makes the question "does
    `resolve_memory_dir` call it, with the derived slug, and return its result?"
    — no filesystem folding involved, nothing to probe, and therefore nothing
    that can opt out. The root's derived slug exists nowhere, so the exact-match
    branch cannot answer and control has to reach the wiring on any platform.

    All three of those are asserted here on purpose. An earlier version checked
    only the first and the last, which left the *argument* — the one thing a
    replacement is most tempted to discard — resting on the skippable test
    below, and so reproduced round 3's invisibility in miniature.
    """

    def fake_home(_cls: type[Path]) -> Path:
        return tmp_path

    monkeypatch.setattr(gate, "_user_settings", tuple)
    monkeypatch.setattr(Path, "home", classmethod(fake_home))

    projects = tmp_path / ".claude" / "projects"
    found = projects / "whatever-the-matcher-picked"
    found.mkdir(parents=True)

    # Records what it was handed, rather than ignoring it. A stub that discards
    # its arguments proves only that the collaborator was called and its result
    # returned -- the *slug it was asked about* would then be checked nowhere
    # this test can see, and passing the wrong one is a live defect: measured,
    # `_case_insensitive_match(projects, "anything-else")` leaves an
    # argument-discarding version of this test green.
    received: list[tuple[Path, str]] = []

    def only_match(projects_arg: Path, wanted: str) -> Path:
        received.append((projects_arg, wanted))
        return found

    monkeypatch.setattr(gate, "_case_insensitive_match", only_match)

    root = Path(r"C:\Dev\no-such-project-anywhere")
    resolved = gate.resolve_memory_dir(root)

    assert resolved == found / "memory"
    assert received == [(projects, gate.project_slug(root))]


def test_resolve_memory_dir_actually_uses_the_case_insensitive_match(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same wiring, end to end against a real directory.

    Secondary by design: `test_resolve_memory_dir_returns_what_the_matcher_found`
    covers the wiring unconditionally, so what this adds is the *realistic*
    path — a real project directory on disk, found by the production matcher
    rather than a replacement — and what a skip here costs is that realism, not
    the coverage. That split is deliberate: this test can decline to run, and a
    test that can decline to run must not be the only thing holding a branch.

    The fallback is only reachable where the filesystem itself does *not* fold
    the two spellings together — otherwise `projects / <slug>` resolves to the
    differently-cased directory and `resolve_memory_dir` returns one branch
    earlier. That is Linux; Windows and macOS answer first. So the premise is
    probed at run time and the test skips with its reason rather than failing a
    leg over a filesystem property that is not what it is about.

    An earlier version reached the fallback everywhere by using `straße` against
    `strasse` — casefold-equal, filesystem-distinct. Correcting `project_slug`
    to replace every character outside `[A-Za-z0-9-]` destroyed that premise:
    `ß` now becomes `-` before `casefold` is ever consulted, so the two no longer
    match at all.

    **A previous version of this docstring said that coverage could not be
    restored, "by construction", and that a proposal to find a reachable pair
    "has no candidate to offer". That was measured false and is corrected here.**
    The argument reasoned about the characters that survive into a *slug* —
    `[A-Za-z0-9-]`, whose only casefold equivalences are ASCII case. But the slug
    is the `wanted` side; the other side is a real **directory name on disk**,
    which is under no such constraint. Any character that casefolds *into* the
    slug's ASCII while staying distinct to the filesystem is a reachable pair.

    `U+212A KELVIN SIGN` is one: `"\\u212a".casefold() == "k"`, and NTFS keeps it
    distinct from `k` because its uppercase form is itself rather than `K`
    (`U+017F LATIN SMALL LETTER LONG S` -> `s` works the same way; both measured
    on this machine, in an ordinary non-case-sensitive directory). So the on-disk
    name below spells the slug's `k` with the Kelvin sign: the exact-match branch
    misses, the fallback runs, and this test now exercises it on Windows too.

    The run-time probe is kept as a safety net rather than as the premise — a
    filesystem that *does* fold the pair skips with its reason instead of failing
    a leg over a property this test is not about. Kept *secondary* either way:
    `test_a_case_only_directory_mismatch_is_matched` and
    `test_resolve_memory_dir_returns_what_the_matcher_found` both redden if
    `_case_insensitive_match` breaks, on every platform.
    """

    def fake_home(_cls: type[Path]) -> Path:
        return tmp_path

    monkeypatch.setattr(gate, "_user_settings", tuple)
    monkeypatch.setattr(Path, "home", classmethod(fake_home))

    root = Path(r"C:\Dev\KiloProject")
    projects = tmp_path / ".claude" / "projects"
    slug = gate.project_slug(root)
    assert "K" in slug, slug  # the substitution below has nothing to bite on
    # U+212A KELVIN SIGN casefolds to "k" and is a different file to NTFS.
    on_disk = projects / slug.replace("K", "\u212a")
    on_disk.mkdir(parents=True)

    if (projects / slug).is_dir():
        pytest.skip(
            "this filesystem folds the Kelvin sign onto ASCII 'k', so the "
            "exact-match branch answers before the fallback is reached and this "
            "test could not tell the wiring from its absence"
        )

    assert gate.resolve_memory_dir(root) == on_disk / "memory"


# --------------------------------------------------------------------------
# External review round 1 — the defects, each pinned against a literal
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        (r"c:\Dev\health\healthspan", "c--Dev-health-healthspan"),
        (
            r"C:\Dev\health\healthspan\.claude\worktrees\rung-2-hook",
            "C--Dev-health-healthspan--claude-worktrees-rung-2-hook",
        ),
        (r"C:\Users\me\My Documents\repo", "C--Users-me-My-Documents-repo"),
        (r"C:\Dev\a_b\c+d", "C--Dev-a-b-c-d"),
        (r"C:\Dev\x=1", "C--Dev-x-1"),
    ],
    ids=["plain", "worktree-dot", "spaces", "underscore-plus", "equals"],
)
def test_the_slug_matches_the_observed_directory_names(
    path: str, expected: str
) -> None:
    """Literal expectations, because a derived one cannot see a wrong rule.

    Every end-to-end fixture in this file builds its project directory with
    `project_slug` itself, so all of them agreed with whatever the function
    computed and none could detect a wrong transformation. One did survive
    exactly that way: replacing only `:`, `\\` and `/` mis-derived the slug for
    every worktree session — the mode this repository works in most — and the
    suite stayed green.

    The `worktree-dot` row is the real directory name on the machine this was
    measured on; the others cover the character classes located in real project
    paths there (space, `_`, `+`, `=`). A literal is the only oracle that can
    fail here, which is the whole point.
    """
    assert gate.project_slug(Path(path)) == expected


def test_an_absent_project_directory_is_forgiven_by_skip_if_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Absence has two levels and the flag must forgive both.

    The first session in a fresh worktree has no `~/.claude/projects/<slug>`
    entry at all, so `resolve_memory_dir` raises before any test of the memory
    directory can run. Testing the flag only after resolution meant the hook
    reported a fault on exactly the condition the flag exists to silence.
    """

    def fake_home(_cls: type[Path]) -> Path:
        return tmp_path

    monkeypatch.setattr(gate, "_user_settings", tuple)
    monkeypatch.setattr(Path, "home", classmethod(fake_home))
    (tmp_path / ".claude" / "projects").mkdir(parents=True)

    argv = ["--root", r"C:\Dev\nothing-here", "--quiet"]
    assert gate.main([*argv, "--skip-if-absent"]) == 0
    assert capsys.readouterr().out == ""

    # Still loud for a caller that did not ask for silence.
    assert gate.main(argv) == 1
    assert "NOT reconciled" in capsys.readouterr().out


def test_the_unresolvable_project_error_names_no_sibling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The message lands in the session transcript, so it may not inventory the
    machine. It used to list every Claude Code project directory — the
    operator's username and the subject matter of unrelated work — and with the
    slug defect above it did that at every worktree session start."""

    def fake_home(_cls: type[Path]) -> Path:
        return tmp_path

    monkeypatch.setattr(gate, "_user_settings", tuple)
    monkeypatch.setattr(Path, "home", classmethod(fake_home))
    projects = tmp_path / ".claude" / "projects"
    projects.mkdir(parents=True)
    (projects / "C--Dev-someone-elses-private-thing").mkdir()

    with pytest.raises(gate.ProjectNotFoundError) as excinfo:
        gate.resolve_memory_dir(Path(r"C:\Dev\absent"))

    message = str(excinfo.value)
    assert "someone-elses-private-thing" not in message, message
    assert "C--Dev-absent" in message
    assert str(projects) in message


def test_the_project_settings_are_read_relative_to_the_root_not_the_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A caller's cwd need not be the project — which is why `--root` exists at
    all. Bound to the cwd instead, the project's own
    `autoMemoryDirectory` redirect went unread and the script reconciled the
    derived default the module docstring says it must refuse to touch."""

    def fake_home(_cls: type[Path]) -> Path:
        return tmp_path / "home"

    monkeypatch.setattr(gate, "_user_settings", tuple)
    monkeypatch.setattr(Path, "home", classmethod(fake_home))

    root = tmp_path / "project"
    (root / ".claude").mkdir(parents=True)
    (root / ".claude" / "settings.json").write_text(
        json.dumps({"autoMemoryDirectory": "~/elsewhere"}), encoding="utf-8"
    )
    monkeypatch.chdir(tmp_path)  # cwd is NOT the project

    with pytest.raises(gate.ReconcileError) as excinfo:
        gate.resolve_memory_dir(root)
    assert "autoMemoryDirectory" in str(excinfo.value)


def test_a_body_name_field_is_not_read_as_the_frontmatter_declaration() -> None:
    """`name:` is an ordinary word. Scanning the whole file for it meant a
    memory declaring none was reported as *mismatching* a value it never
    declared — and one whose body happened to carry its own stem passed while
    declaring nothing. The corpus holds a memory about agent frontmatter
    fields, which is precisely the document that writes such examples."""
    body_only = "---\ndescription: x\n---\n\n```yaml\nname: some-agent\n```\n"
    assert gate.declared_name(body_only) is None
    assert gate.declared_name("---\nname: real\n---\n\nname: decoy\n") == "real"


def test_a_code_span_does_not_cross_a_blank_line(tmp_path: Path) -> None:
    """Two unpaired backticks used to blank every wiki-link between them. One
    stray backtick typo anywhere in the corpus was enough to hide a genuine
    dead link two paragraphs away — the false-negative direction the module
    comment calls the more expensive one."""
    body = "we can`t do it\n\nprose with [[alpha-beta]] here\n\nand another ` tick\n"
    root = _memory(tmp_path, {"alpha_beta": "a\n", "caller": body})

    errors = gate.check(root).errors
    assert any("dead wiki-link" in e for e in errors), errors


def test_a_fence_closed_at_a_different_indent_does_not_swallow_the_file(
    tmp_path: Path,
) -> None:
    """CommonMark lets a closing fence be indented 0-3 spaces regardless of the
    opener. Requiring the opener's exact indent meant a flush-opened fence
    closed with an indented one found no close, ran to EOF, and blanked every
    link after it — hiding live text rather than examples."""
    body = "```\ncode\n  ```\n\n[[alpha-beta]] after\n"
    root = _memory(tmp_path, {"alpha_beta": "a\n", "caller": body})

    errors = gate.check(root).errors
    assert any("dead wiki-link" in e for e in errors), errors


def test_an_anchored_row_is_a_live_row_and_a_pathed_link_is_not_a_row(
    tmp_path: Path,
) -> None:
    """Two false errors in opposite directions from one regex. `[a](x.md#frag)`
    was missed, so a correctly indexed memory was reported orphaned — the
    loudest error this script has. `[b](../adr/0077.md)` was matched, then
    compared against bare filenames it could never equal, so every reference
    out of the corpus became a false dangling row."""
    index = (
        "# Index\n\n| [Alpha](alpha.md#background) |\n"
        "| [ADR](../adr/0077-local-invocation-hooks.md) |\n"
    )
    root = _memory(tmp_path, {"alpha": "a\n"}, index=index)

    report = gate.check(root)
    assert report.errors == [], report.errors
    assert report.examined["index-rows"] == 1


def test_a_fenced_or_commented_index_row_is_not_a_live_row(tmp_path: Path) -> None:
    """Both directions cost. A commented-out row kept its file from being
    reported orphaned — the exact defect this script exists for — and an
    example row inside a fence produced a false dangling row."""
    index = (
        "# Index\n\n| [Alpha](alpha.md) |\n\n```\n| [x](example_row.md) |\n```\n\n"
        "<!-- | [y](commented_out.md) | -->\n"
    )
    root = _memory(tmp_path, {"alpha": "a\n"}, index=index)

    report = gate.check(root)
    assert report.errors == [], report.errors
    assert report.examined["index-rows"] == 1


def test_a_duplicated_index_row_is_reported(tmp_path: Path) -> None:
    """The other half of the concurrent-write failure this script exists for.
    Auto memory has no merge and no conflict detection, so two sessions each
    adding a row — or one anchored Edit applied twice — produces a duplicate.
    Collapsed into a set, both difference checks came back empty and the row
    count silently under-reported."""
    index = "# Index\n\n| [Alpha](alpha.md) |\n| [Alpha again](alpha.md) |\n"
    root = _memory(tmp_path, {"alpha": "a\n"}, index=index)

    errors = gate.check(root).errors
    assert any("duplicate index row" in e for e in errors), errors
    assert any("alpha.md" in e for e in errors), errors


def test_a_leading_thematic_break_is_not_eaten_as_frontmatter(tmp_path: Path) -> None:
    """Pairing `---` with the next `---` anywhere deleted real content before
    the size measurement, so an index could measure shorter than it loads and
    pass a limit whose tail is already being dropped at session start."""
    assert "Hello" in gate.loaded_text("---\n\nHello\n\n---\n\nWorld\n")
    # And a genuine frontmatter block is still removed.
    assert "name:" not in gate.loaded_text("---\nname: x\n---\n\nBody\n")


def test_check_reads_the_directory_once_and_each_memory_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The property the read-once consolidation claims, which nothing pinned.

    Three independent `glob`s and two reads per memory was framed in the module
    as a *correctness* fix rather than an efficiency one: the scans could
    disagree, so `examined["files"]` counted one set while the identity and
    wiki-link results described another. That rationale went untested —
    reverting either loop to its own `glob` + `read_text` left the whole suite
    green (measured), so the claim rested on nobody having reverted it.

    Counting reads is the sharpest available form of the property: one read per
    memory means every check sees the same bytes, and a second scan cannot
    observe a file the first did not.
    """
    root = _memory(tmp_path, {"alpha": "a\n", "beta": "b\n", "gamma": "c\n"})

    reads: dict[str, int] = {}
    globs: list[str] = []
    real_read = Path.read_text
    real_glob = Path.glob

    def counting_read(self: Path, *args: object, **kwargs: object) -> str:
        reads[self.name] = reads.get(self.name, 0) + 1
        return real_read(self, *args, **kwargs)  # pyright: ignore[reportArgumentType]

    def counting_glob(self: Path, pattern: str, *args: object, **kwargs: object):
        globs.append(pattern)
        return real_glob(self, pattern, *args, **kwargs)  # pyright: ignore[reportArgumentType]

    monkeypatch.setattr(Path, "read_text", counting_read)
    monkeypatch.setattr(Path, "glob", counting_glob)

    gate.check(root)

    # The index is asserted rather than filtered out. It used to be excluded
    # from this dict before the comparison, which removed the one filename the
    # property is about: restoring the second index read -- the untranslated
    # `newline=""` read plus a translated one -- left the suite green, so the
    # TOCTOU fix this test exists for was pinned by nothing. A concurrent
    # session editing MEMORY.md between two reads is the event the whole script
    # exists to catch, and it would make the row reconciliation describe one
    # file while the load-limit numbers describe another.
    assert reads == {
        gate.INDEX_NAME: 1,
        "alpha.md": 1,
        "beta.md": 1,
        "gamma.md": 1,
    }, reads
    assert globs == ["*.md"], globs


def test_a_windows_pathed_link_is_not_a_row_either(tmp_path: Path) -> None:
    """Excluding a path component by `/` alone left the other separator in.

    A row written `[x](sub\\thing.md)` was kept as though it were a bare
    filename, then compared against `glob` output that never carries a
    separator — so it could never match, and every such reference produced a
    false "dangling index row". Same defect as the forward-slash case, one
    character apart, and latent rather than live only because no memory index
    happens to use a Windows path today.
    """
    index = "# Index\n\n| [Alpha](alpha.md) |\n| [Out](sub\\thing.md) |\n"
    root = _memory(tmp_path, {"alpha": "a\n"}, index=index)

    report = gate.check(root)
    assert report.errors == [], report.errors
    assert report.examined["index-rows"] == 1


def test_frontmatter_may_contain_a_blank_line() -> None:
    """A blank line is legal inside a YAML block, and the first fix forbade it.

    Requiring *every* inner line to be non-blank got the thematic-break case
    right — a document opening `---` then a blank line is not frontmatter — and
    in doing so lost the declared name of any memory whose frontmatter carried a
    blank line, reporting it as declaring none. The discriminator is narrower
    than that: only the line *immediately after* the opening `---` has to be
    non-blank, because that is where the two shapes actually differ.
    """
    with_blank = "---\nname: alpha\n\ndescription: after a blank line\n---\n\nbody\n"
    assert gate.declared_name(with_blank) == "alpha"

    # The thematic-break case the narrower rule must still reject.
    assert "Hello" in gate.loaded_text("---\n\nHello\n\n---\n\nWorld\n")
    assert gate.declared_name("---\n\nname: not-frontmatter\n---\n") is None


# --------------------------------------------------------------------------
# External review round 2 — the defects, each pinned
# --------------------------------------------------------------------------


def test_the_root_is_the_repository_not_the_worktree(tmp_path: Path) -> None:
    """Finding 1, and the deepest defect this branch produced.

    Auto memory keys on the **repository**: "The <project> path is derived from
    the git repository, so all worktrees and subdirectories within the same repo
    share one auto memory directory" (`code.claude.com/docs/en/memory`). Session
    *transcript* directories key on the working directory instead, and inside a
    main checkout the two are identical — which is why deriving from
    `--show-toplevel` looked right for five local rounds and one external one.

    In a linked worktree it is wrong, and wrong in the silent direction: the
    worktree's slug names a project directory holding no `memory/`, so nothing
    resolved, `--skip-if-absent` forgave it, and the hook stopped checking
    permanently in the working mode this repository uses most.
    """
    origin = tmp_path / "origin"
    origin.mkdir()
    subprocess.run(
        ["git", "init", "-q"],  # noqa: S607
        cwd=origin,
        check=True,
        env=_git_env(),
    )
    (origin / "f.txt").write_text("x\n", encoding="utf-8")
    subprocess.run(
        ["git", "add", "-A"],  # noqa: S607
        cwd=origin,
        check=True,
        env=_git_env(),
    )
    subprocess.run(
        ["git", "-c", "user.email=t@e", "-c", "user.name=T", "commit", "-qm", "c"],  # noqa: S607
        cwd=origin,
        check=True,
        env=_git_env(),
    )
    linked = tmp_path / "linked"
    subprocess.run(  # noqa: S603
        ["git", "worktree", "add", "-q", "--detach", str(linked)],  # noqa: S607
        cwd=origin,
        check=True,
        env=_git_env(),
    )

    assert gate.repo_root(origin).resolve() == origin.resolve()
    # The load-bearing assertion: from inside the worktree, the answer is still
    # the repository. `--show-toplevel` would return `linked` here.
    assert gate.repo_root(linked).resolve() == origin.resolve()


def test_no_resolvable_home_does_not_raise_at_import(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Finding 2. `Path.home()` raises when no home is resolvable, and at module
    scope that raise happens on *import* — before `main` is reached, so every
    guard inside it is unreachable and the caller sees a traceback instead of a
    message. The property outlived the hook that first made it urgent."""

    def no_home(_cls: type[Path]) -> Path:
        raise RuntimeError("Could not determine home directory.")

    monkeypatch.setattr(Path, "home", classmethod(no_home))
    user_settings = gate._user_settings  # pyright: ignore[reportPrivateUsage]
    settings_files = gate._settings_files  # pyright: ignore[reportPrivateUsage]
    assert user_settings() == ()
    assert settings_files(None) == ()


@pytest.mark.parametrize(
    ("body", "why"),
    [
        (
            "1. item\n\n    ```\n    code\n    ```\n\nTAIL [[alpha-beta]]\n",
            "list-indented",
        ),
        ("- item\n\n   ```\n   code\n   ```\n\nTAIL [[alpha-beta]]\n", "three-space"),
        (
            "```code``` is how you write it.\n\nSee [[alpha-beta]] later.\n",
            "line-leading span",
        ),
    ],
    ids=["list-indented", "three-space", "line-leading-span"],
)
def test_a_fence_does_not_swallow_the_document(
    tmp_path: Path, body: str, why: str
) -> None:
    """Finding 4, both directions. An opener admitted at any indent while the
    closer was held to CommonMark's 0-3 meant a fence nested under a list item
    never closed and blanked everything after it; and a line-leading ```code```
    span was read as a fence opener, doing the same. Either way the tail of the
    document vanishes and every row below it is reported orphaned.

    No tab-indented case, and its absence is deliberate: a tab is not one of the
    0-3 *spaces* CommonMark allows before a fence, so a tab-led marker never
    opens a fence under any implementation, correct or defective. A fixture
    using one exercises no fence path at all and stays green against every
    regression here — measured. Three spaces is the real boundary.
    """
    root = _memory(tmp_path, {"alpha_beta": "a\n", "caller": body})

    errors = gate.check(root).errors
    assert any("dead wiki-link" in e for e in errors), (why, errors)


def test_one_unbalanced_backtick_does_not_delete_the_rows_below_it(
    tmp_path: Path,
) -> None:
    """Finding 5. Index rows are contiguous lines with no blank between them,
    and a document-wide code span only refused to cross a *blank* line — so a
    single stray backtick in one row's description silently deleted the rows
    that followed, and each deleted row's memory was then reported orphaned."""
    # One *unpaired* backtick in the first row. Balanced spans are stripped the
    # same way by either implementation, so a fixture whose backticks pair
    # cannot tell them apart — measured, and it is why the first version of this
    # test passed against the defect it was written for.
    index = (
        "# Index\n\n"
        "- [Alpha](alpha.md) - a note about `foo\n"
        "- [Beta](beta.md) - plain row\n"
        "- [Gamma](gamma.md) - and `another` tick\n"
    )
    root = _memory(
        tmp_path, {"alpha": "a\n", "beta": "b\n", "gamma": "g\n"}, index=index
    )

    report = gate.check(root)
    assert report.errors == [], report.errors
    assert report.examined["index-rows"] == 3


@pytest.mark.parametrize(
    ("index", "why"),
    [
        (
            "# Index\n\nStart with [Alpha](alpha.md) if new.\n\n- [Alpha](alpha.md)\n",
            "prose cross-reference beside a real row",
        ),
        (
            "# Index\n\n- [Alpha bg](alpha.md#background)\n"
            "- [Alpha dec](alpha.md#decisions)\n",
            "two anchored rows into one memory",
        ),
        (
            "# Index\n\n- [Alpha](alpha.md)\n\nThis file is [MEMORY.md](MEMORY.md).\n",
            "the index's own self-reference",
        ),
    ],
    ids=["prose", "anchored", "self-link"],
)
def test_only_a_row_is_a_row(tmp_path: Path, index: str, why: str) -> None:
    """Finding 6. Every `.md` link in the document used to be a row, so ordinary
    prose raised a *duplicate index row* error asserting a cause — "two sessions
    adding a row for one memory" — that sends the reader after a concurrency
    incident which is really a cross-reference. The self-link case reported
    `MEMORY.md` itself as a file that does not exist."""
    root = _memory(tmp_path, {"alpha": "a\n"}, index=index)

    report = gate.check(root)
    assert report.errors == [], (why, report.errors)


@pytest.mark.parametrize(
    "destination",
    ["./alpha.md", " alpha.md ", "<alpha.md>", 'alpha.md "a title"', "alpha.md#frag"],
    ids=["dot-slash", "padded", "pointy", "titled", "anchored"],
)
def test_legal_destinations_pointing_into_the_corpus_are_rows(
    tmp_path: Path, destination: str
) -> None:
    """Finding 7. All five are legal CommonMark destinations naming a memory in
    this directory. Rejecting any target containing a separator — the rule's own
    comment reasoned only about `../adr/...` links pointing *out* — made an
    ordinary `./alpha.md` row not a row at all, and its memory a false orphan."""
    root = _memory(
        tmp_path, {"alpha": "a\n"}, index=f"# Index\n\n- [A]({destination})\n"
    )

    report = gate.check(root)
    assert report.errors == [], (destination, report.errors)
    assert report.examined["index-rows"] == 1


def test_a_link_out_of_the_corpus_is_not_a_row(tmp_path: Path) -> None:
    """The other half of finding 7: a target with a path component really is a
    reference out, and must not become a dangling row."""
    index = (
        "# Index\n\n- [Alpha](alpha.md)\n- [ADR](../adr/0077-x.md)\n- [Sub](sub/x.md)\n"
    )
    root = _memory(tmp_path, {"alpha": "a\n"}, index=index)

    report = gate.check(root)
    assert report.errors == [], report.errors
    assert report.examined["index-rows"] == 1


def test_a_row_matches_its_file_the_way_the_filesystem_does(tmp_path: Path) -> None:
    """Finding 8. Compared case-sensitively against a case-insensitive
    filesystem, a link that opens perfectly well was reported orphaned *and*
    dangling at once — both ends of one working link."""
    root = tmp_path / "memory"
    root.mkdir()
    (root / "Alpha_Thing.md").write_text(
        "---\nname: Alpha_Thing\n---\n\na\n", encoding="utf-8", newline="\n"
    )
    # Cased differently from the file *and* from its casefold, so a comparison
    # that folds only one side still fails.
    (root / gate.INDEX_NAME).write_text(
        "# Index\n\n- [A](ALPHA_THING.md)\n", encoding="utf-8", newline="\n"
    )

    report = gate.check(root)
    assert report.errors == [], report.errors


def test_a_bom_does_not_hide_the_frontmatter(tmp_path: Path) -> None:
    """Finding 9. A BOM is invisible to a reader and defeats `\\A---`, so a
    well-formed memory was reported as declaring no name — a false error
    against a corpus that was in fact correct."""
    root = tmp_path / "memory"
    root.mkdir()
    (root / "alpha.md").write_bytes("﻿---\nname: alpha\n---\n\nbody\n".encode())
    (root / gate.INDEX_NAME).write_bytes("﻿# Index\n\n- [A](alpha.md)\n".encode())

    report = gate.check(root)
    assert report.errors == [], report.errors


def test_an_unreadable_settings_file_is_a_refusal_not_a_skip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Finding 10. Skipping a settings file it could not parse meant the case
    where the redirect is *unknown* was treated as the case where it is absent,
    so the script reconciled the derived default — "a checker pointed at the
    wrong tree reports clean about it", reached through the guard against it.
    A UTF-16 file raised `UnicodeDecodeError`, which is neither `OSError` nor a
    JSON error, and escaped every handler in the module."""
    for payload, why in (
        (b"\xff\xfe{\x00}\x00", "utf-16"),
        (b"not json at all", "malformed"),
    ):
        settings = tmp_path / f"settings-{why}.json"
        settings.write_bytes(payload)
        monkeypatch.setattr(gate, "_user_settings", lambda s=settings: (s,))
        with pytest.raises(gate.ReconcileError) as excinfo:
            gate.resolve_memory_dir(Path.cwd())
        assert "cannot be known" in str(excinfo.value), why


def test_a_commented_out_wiki_link_in_a_body_is_not_live(tmp_path: Path) -> None:
    """Finding 11. HTML comments were stripped from the index but not from
    bodies, so the same markup meant two different things inside one run and a
    commented-out link produced an error-class false positive every session."""
    body = "live text\n\n<!--\nold note: [[alpha-beta]] was here\n-->\n\ndone\n"
    root = _memory(tmp_path, {"alpha_beta": "a\n", "caller": body})

    report = gate.check(root)
    assert report.errors == [], report.errors
    assert report.warnings == [], report.warnings


def test_a_long_backtick_run_does_not_blow_up(tmp_path: Path) -> None:
    """Finding 12: ~n^2.7 backtracking on a pathological run of backticks.

    A bound rather than a benchmark — a timing assertion generous enough not to
    flake, against a size the old form took seconds on. It was found while this
    ran from a `SessionStart` hook, where one degenerate file blocked every
    session until the hook timed out and no exception guard could rescue it; the
    bound is kept because a checker that hangs is useless to any caller.
    """
    body = "prose then " + ("`" * 6400) + " and [[alpha-beta]] after\n"
    root = _memory(tmp_path, {"alpha_beta": "a\n", "caller": body})

    started = time.monotonic()
    gate.check(root)
    # 0.5 s, not 5 s. The reverted regex measured ~3.4 s on this fixture, so
    # a 5 s bound stayed green against the exact regression this test is named
    # for -- a bound loose enough to pass the defect is not a bound. The
    # current implementation takes ~0.1 ms here, so this leaves about five
    # thousand times the headroom and cannot flake on a slow machine.
    assert time.monotonic() - started < 0.5


def test_a_directory_named_like_a_memory_is_not_read(tmp_path: Path) -> None:
    """Finding 13. A `*.md` directory raises `PermissionError` on Windows and
    `IsADirectoryError` on POSIX — two types for one input, so a handler written
    against either is wrong on the other CI leg. Excluded at the producing site
    instead."""
    root = _memory(tmp_path, {"alpha": "a\n"})
    (root / "notes.md").mkdir()

    report = gate.check(root)
    assert report.errors == [], report.errors


def test_a_link_missing_its_prefix_entirely_is_dead(tmp_path: Path) -> None:
    """The direction the second tier exists for: a target with **no** prefix.

    A hyphenated, prefix-dropped target against a real `reference_`-prefixed
    memory matches nothing under separator flattening alone, so without this
    tier it was reported as a legitimate forward reference and two live links
    of that shape sat in the warning list.
    """
    root = _memory(
        tmp_path,
        {"reference_thing": "r\n", "caller": "see [[thing]]\n"},
    )

    errors = gate.check(root).errors
    assert any("dead wiki-link" in e for e in errors), errors
    assert any("reference_thing" in e for e in errors), errors


def test_a_different_type_prefix_is_a_forward_reference_not_a_dead_link(
    tmp_path: Path,
) -> None:
    """The direction an over-applied fix broke, and the one the convention owns.

    A prefix encodes the memory's **type**, so `reference_thing` and
    `project_thing` are different memories. Un-prefixing the *target* as well as
    the stems made a link to the one that does not exist yet resolve as a
    near-miss of the one that does — a hard error at every session start, under
    a message that said "a dropped type prefix" when the prefix was present and
    different.

    Check 4 of the module docstring calls an unresolved wiki-link "warning only,
    permanently", because a link to a memory not written yet is legitimate. The
    live corpus carries 80 suffixes across 4 prefixes with no collision today,
    so the first reused suffix would have turned every cross-type forward
    reference into a permanent error.
    """
    root = _memory(
        tmp_path,
        {"project_thing": "p\n", "caller": "planned: [[reference_thing]]\n"},
    )

    report = gate.check(root)
    assert report.errors == [], report.errors
    assert any("reference_thing" in w for w in report.warnings), report.warnings


def test_a_normalize_collision_names_every_candidate(tmp_path: Path) -> None:
    """Finding 14, other half. The first tier was a dict comprehension over a
    set, so on a collision the surviving value was chosen by set iteration order
    — i.e. by `PYTHONHASHSEED`. Measured: ten seeds, two different error texts,
    five times each, for the same bytes. The second tier already named every
    candidate; the first now agrees with it."""
    root = _memory(
        tmp_path,
        {
            "project_a-b": "a\n",
            "project_a_b": "b\n",
            "caller": "see [[project-a-b]]\n",
        },
    )

    errors = [e for e in gate.check(root).errors if "dead wiki-link" in e]
    assert errors, gate.check(root).errors
    assert "project_a-b" in errors[0], errors
    assert "project_a_b" in errors[0], errors


def test_the_load_limit_counts_the_bytes_the_loader_reads(tmp_path: Path) -> None:
    """Finding 15, both directions.

    Silent: `read_text` translates newlines, so a CRLF index lost one byte per
    line and could pass a limit whose tail the loader is already dropping.
    Loud: `str.splitlines()` splits on seven characters the loader does not, so
    an index quoting one counted lines that are not there.
    """
    root = tmp_path / "memory"
    root.mkdir()
    rows = "".join(f"| row {n} |\r\n" for n in range(300))
    (root / gate.INDEX_NAME).write_bytes(rows.encode())

    report = gate.check(root)
    on_disk = (root / gate.INDEX_NAME).stat().st_size
    assert report.examined["index-bytes"] == on_disk, (
        report.examined["index-bytes"],
        on_disk,
    )
    assert report.examined["index-lines"] == 300

    # The loud direction: a vertical tab is not a line break to the loader.
    (root / gate.INDEX_NAME).write_bytes(b"| a \x0b b |\n")
    assert gate.check(root).examined["index-lines"] == 1


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(  # noqa: S603
        ["git", "-c", "user.email=t@e", "-c", "user.name=T", *args],  # noqa: S607
        cwd=cwd,
        check=True,
        capture_output=True,
        env=_git_env(),
    )


def test_the_script_reports_an_orphan_from_inside_a_worktree(tmp_path: Path) -> None:
    """The deployed path, which the unit test could not see.

    `test_the_root_is_the_repository_not_the_worktree` calls `repo_root()`
    directly and passes — while the caller did not reach it. `main` read
    `args.root or repo_root()` and the `SessionStart` bootstrap always supplied
    `--root` (the checkout holding the script, which in a linked worktree *is*
    the worktree), so the corrected resolution was truthy-shortcircuited away
    and never ran in production. Measured before the fix: from inside a
    worktree, against a corpus holding an orphan, the run was byte-identical to
    a healthy clean one.

    The bootstrap is gone; the property it exposed is not. Worktrees share the
    repository's memory directory, so resolving the *owning repository* from a
    worktree cwd is what any caller depends on, and a unit test of `repo_root`
    cannot stand in for it — the defect was the wiring between resolution and
    `main`, which is only observable end to end. So this runs the script as a
    process, from inside a real linked worktree, with no `--root` at all: the
    resolution now has to come from the cwd, which is the path a caller takes.

    Exit **1**, not 0. The hook inverted this — `SessionStart` adds stdout to
    context only on exit 0, so a finding had to exit 0 or be discarded — and
    that inversion is one of the things dropping the hook bought back.
    """
    origin = tmp_path / "origin"
    _init_fixture_repo(origin)
    _git("add", "-A", cwd=origin)
    _git("commit", "-qm", "c", cwd=origin)

    linked = tmp_path / "linked"
    _git("worktree", "add", "-q", "--detach", str(linked), cwd=origin)

    # The corpus lives under the *origin* repository's slug, as auto memory does.
    home = tmp_path / "home"
    memory = home / ".claude" / "projects" / gate.project_slug(origin) / "memory"
    memory.mkdir(parents=True)
    (memory / "MEMORY.md").write_text(
        "# Index\n\n- [Alpha](alpha.md)\n", encoding="utf-8", newline="\n"
    )
    for name in ("alpha", "orphan"):
        (memory / f"{name}.md").write_text(
            f"---\nname: {name}\n---\n\nx\n", encoding="utf-8", newline="\n"
        )

    env = dict(os.environ)
    env.pop("CLAUDE_PROJECT_DIR", None)
    # Path.home() reads USERPROFILE on Windows and HOME on POSIX.
    env["HOME"] = str(home)
    env["USERPROFILE"] = str(home)

    proc = subprocess.run(  # noqa: S603
        [sys.executable, str(linked / "scripts" / HOOK_SCRIPT.name), "--quiet"],
        cwd=linked,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert "orphan.md" in proc.stdout, (
        "the checker found nothing from inside a worktree; silence here is the "
        f"defect, not a pass. stdout={proc.stdout!r} stderr={proc.stderr!r}"
    )
    assert proc.returncode == 1, (proc.returncode, proc.stderr)


@pytest.mark.parametrize(
    "destination",
    ["mailto:contact.md", "https://example.invalid/a.md", "urn:isbn:0.md"],
    ids=["mailto", "https", "urn"],
)
def test_a_uri_destination_is_not_a_row(tmp_path: Path, destination: str) -> None:
    """`row_target`'s scheme branch, which nothing covered.

    Removing it left the whole suite green (measured). A `scheme:name.md` with no
    separator in it would then be read as a bare in-corpus filename and reported
    a dangling index row — one of the four things the function's own docstring
    says used to be conflated, and the only one of the four with no test.
    """
    index = f"# Index\n\n- [Alpha](alpha.md)\n- [Out]({destination})\n"
    root = _memory(tmp_path, {"alpha": "a\n"}, index=index)

    report = gate.check(root)
    assert report.errors == [], (destination, report.errors)
    assert report.examined["index-rows"] == 1


# --------------------------------------------------------------------------
# External review round 3 — the case-comparison cause behind findings 1 and 2
# --------------------------------------------------------------------------


def _case_sensitive_dir(path: Path) -> Path:
    """`path`, created and made case-sensitive, or a skip explaining why not.

    Two memories whose names differ only in case can only *exist* where the
    filesystem separates them. That is native on Linux; on NTFS it is a
    per-directory attribute, which this project's own review round set with
    `fsutil` and no elevation. Probed rather than assumed, and probed by
    writing the colliding pair rather than by reading the platform name --
    the attribute can be unavailable on a volume, in a container, or on macOS,
    and what the test needs to know is whether the pair survives, not which OS
    it is running on.
    """
    path.mkdir(parents=True)
    if sys.platform == "win32":
        subprocess.run(  # noqa: S603
            ["fsutil", "file", "setCaseSensitiveInfo", str(path), "enable"],  # noqa: S607
            capture_output=True,
            text=True,
            check=False,
        )
    (path / "probe-lower").write_text("lower", encoding="utf-8")
    (path / "PROBE-LOWER").write_text("upper", encoding="utf-8")
    survived = (path / "probe-lower").read_text(encoding="utf-8") == "lower"
    (path / "probe-lower").unlink()
    if survived:
        (path / "PROBE-LOWER").unlink()
    if not survived:
        pytest.skip(
            "this filesystem folds two names differing only in case into one "
            "file, so the colliding pair this test is about cannot be created"
        )
    return path


def test_case_colliding_memories_do_not_collapse_into_one(tmp_path: Path) -> None:
    """Finding 1. Two memories differing only in case, one of them indexed.

    `sources` was keyed by `Path`, and `Path('Alpha.md') == Path('alpha.md')`
    with equal hashes on Windows, so the dict kept the first key and the *other*
    file's text. Measured before the fix: `files` counted 1, the genuine orphan
    was never reported, and the identity check invented an error against a file
    whose `name:` was correct — "Alpha.md declares name 'alpha'".

    The orphan is the assertion that matters; `files == 2` is what makes it
    discriminating, since a checker that simply never folded would also report
    an orphan here while still miscounting the corpus.
    """
    root = _case_sensitive_dir(tmp_path / "memory")
    for name, declared in (("alpha.md", "alpha"), ("Alpha.md", "Alpha")):
        (root / name).write_text(
            f"---\nname: {declared}\n---\n\nbody\n", encoding="utf-8", newline="\n"
        )
    (root / gate.INDEX_NAME).write_text(
        "# Index\n\n- [A](alpha.md)\n", encoding="utf-8", newline="\n"
    )

    report = gate.check(root)

    assert report.examined["files"] == 2, report.examined
    assert report.examined["identity-mismatches"] == 0, report.errors
    assert [e for e in report.errors if "orphaned memory" in e] == [
        "orphaned memory -- no row in MEMORY.md points at it, so it is never "
        "loaded and the write accomplished nothing: Alpha.md"
    ], report.errors


def test_two_case_colliding_memories_each_indexed_are_reconciled(
    tmp_path: Path,
) -> None:
    """The other side of finding 1's fix, and the one a fold-everything repair
    breaks: where the filesystem really does hold two memories, an index
    carrying a correct row for each is reconciled, not duplicated.

    Without this the pair above is satisfied by "treat every case-colliding row
    as a duplicate", which trades one false silence for one false alarm.
    """
    root = _case_sensitive_dir(tmp_path / "memory")
    for name, declared in (("alpha.md", "alpha"), ("Alpha.md", "Alpha")):
        (root / name).write_text(
            f"---\nname: {declared}\n---\n\nbody\n", encoding="utf-8", newline="\n"
        )
    (root / gate.INDEX_NAME).write_text(
        "# Index\n\n- [a](alpha.md)\n- [A](Alpha.md)\n",
        encoding="utf-8",
        newline="\n",
    )

    report = gate.check(root)

    assert report.errors == [], report.errors
    assert report.examined["files"] == 2, report.examined
    assert report.examined["index-rows"] == 2, report.examined


def test_a_duplicate_row_differing_only_in_case_is_caught(tmp_path: Path) -> None:
    """Finding 2, and it needs no exotic filesystem — one file, two rows.

    Duplicate detection counted destinations case-sensitively while the orphan
    and dangling loops folded, so this fell through all three: the orphan loop
    saw the file linked, the dangling loop saw both rows resolve, the duplicate
    loop saw two distinct strings. Measured before the fix: `errors == []` with
    `index-rows: 2` against `files: 1`, and nothing read the mismatch. On
    Windows and macOS both rows open the same memory, which makes this exactly
    the "two sessions each adding a row for one memory" incident the error text
    describes.
    """
    index = "# Index\n\n- [A](alpha.md)\n- [B](Alpha.md)\n"
    root = _memory(tmp_path, {"alpha": "a\n"}, index=index)

    report = gate.check(root)

    assert len(report.errors) == 1, report.errors
    assert "duplicate index row" in report.errors[0], report.errors
    assert "Alpha.md, alpha.md" in report.errors[0], report.errors


def test_rows_into_two_anchors_of_one_memory_are_not_a_duplicate(
    tmp_path: Path,
) -> None:
    """The boundary finding 2's fix must not cross.

    A destination keeps its `#anchor` and a filename does not, so folding the
    two questions together turns an ordinary index — two rows naming different
    sections of one memory — into a fabricated concurrency incident. This is
    the assertion that caught exactly that while the fix was being written.
    """
    index = "# Index\n\n- [One](alpha.md#one)\n- [Two](alpha.md#two)\n"
    root = _memory(tmp_path, {"alpha": "a\n"}, index=index)

    report = gate.check(root)

    assert report.errors == [], report.errors
    assert report.examined["index-rows"] == 2, report.examined


def test_an_index_named_in_another_case_is_not_examined_as_a_memory(
    tmp_path: Path,
) -> None:
    """Cut item 2, first half. The index is excluded from `sources` by a folded
    comparison, and nothing covered that.

    Where the filesystem opens `Memory.md` as `MEMORY.md`, a case-sensitive `!=`
    left the index *in* the corpus, so it was examined as though it were a
    memory: reported orphaned, and reported as declaring no frontmatter `name:`.
    Two errors at every run against a directory with nothing wrong with it.

    Skipped rather than failed where the filesystem separates the two spellings,
    because there the index genuinely is absent and `check` is right to say so —
    a different behaviour, not this one.
    """
    root = tmp_path / "memory"
    root.mkdir()
    (root / "alpha.md").write_text(
        "---\nname: alpha\n---\n\nbody\n", encoding="utf-8", newline="\n"
    )
    (root / "Memory.md").write_text(
        "# Index\n\n- [A](alpha.md)\n", encoding="utf-8", newline="\n"
    )
    if not (root / gate.INDEX_NAME).is_file():
        pytest.skip(
            "this filesystem separates 'Memory.md' from 'MEMORY.md', so the "
            "index is genuinely absent and the folded exclusion is unreachable"
        )

    report = gate.check(root)

    assert report.errors == [], report.errors
    assert report.examined["files"] == 1, report.examined


def test_a_bom_before_the_first_row_does_not_orphan_it(tmp_path: Path) -> None:
    """Cut item 2, second half. The index is read `utf-8-sig`, and nothing
    covered the case the BOM actually breaks.

    A BOM is invisible to a reader and is a character to a parser. Where the
    index opens *directly* with a row rather than a heading, `\ufeff` sits in
    front of the list marker, `_ROW_LINE` does not match, and the row stops
    being a row — so every memory it named is reported orphaned while the file
    looks perfectly correct on screen. The existing BOM fixtures all open with a
    heading, where the stray character lands on a line nothing parses.
    """
    root = tmp_path / "memory"
    root.mkdir()
    (root / "alpha.md").write_text(
        "---\nname: alpha\n---\n\nbody\n", encoding="utf-8", newline="\n"
    )
    (root / gate.INDEX_NAME).write_text(
        "\ufeff- [Alpha](alpha.md) - hook\n", encoding="utf-8", newline="\n"
    )

    report = gate.check(root)

    assert report.errors == [], report.errors
    assert report.examined["index-rows"] == 1, report.examined


@pytest.mark.parametrize(
    "destination",
    ["c:x.md", "C:x.md", "z:project_a.md"],
    ids=["lower-drive", "upper-drive", "other-drive"],
)
def test_a_one_character_scheme_is_not_an_in_corpus_filename(destination: str) -> None:
    r"""Cut item 8. `SCHEME_RE` requires two or more characters before the colon,
    so that a Windows path `C:\dir` is not read as a URI. The cost is that
    `c:x.md` — a one-character scheme, and also a drive-relative Windows path —
    passes the scheme test and then has no separator in it, so it was read as a
    bare in-corpus filename and reported a dangling index row against a corpus
    with nothing wrong with it.

    Rejected on this side rather than by widening the shared regex, which is
    also the repository-wide link gate's definition of a URI.
    """
    assert gate.row_target(destination) is None, destination


def test_repo_root_of_a_separated_gitdir_is_the_working_tree(tmp_path: Path) -> None:
    """Cut item 1. `parent(--git-common-dir)` is the repository root only where
    the common dir is the checkout's own `.git`.

    With `--separate-git-dir` the common dir is an arbitrary path, so its parent
    is an arbitrary directory — whose slug names some **other** project's memory
    corpus, or none. That is the wrong-tree read this module's docstring calls
    its cardinal sin, and it is silent: the wrong root resolves, reconciles, and
    reports on a corpus that is not this repository's.

    Measured with real git before the fix: `repo_root(work)` returned `work`'s
    *grandparent* — the directory holding the separated gitdir — rather than the
    working tree.
    """
    gitdir = tmp_path / "elsewhere.git"
    work = tmp_path / "work"
    work.mkdir()
    subprocess.run(  # noqa: S603
        ["git", "init", "-q", f"--separate-git-dir={gitdir}", str(work)],  # noqa: S607
        check=True,
        capture_output=True,
        env=_git_env(),
    )

    assert gate.repo_root(work) == work.resolve(), (
        gate.repo_root(work),
        work.resolve(),
    )


def test_repo_root_of_a_linked_worktree_is_still_the_main_checkout(
    tmp_path: Path,
) -> None:
    """The boundary cut item 1's fix must not cross, and the reason the fallback
    is a fallback rather than the rule.

    `--show-toplevel` answers a linked worktree with the *worktree*, whose slug
    names a project directory holding no memory at all — the failure that made
    the checker go permanently silent in the working mode this repository uses
    most. So the working tree is consulted only where the common dir is not a
    `.git`; here it is, and the main checkout must still win.
    """
    main = tmp_path / "main"
    main.mkdir()
    for args in (
        ["init", "-q"],
        ["add", "-A"],
        [
            "-c",
            "user.email=t@e",
            "-c",
            "user.name=T",
            "commit",
            "-qm",
            "c",
            "--allow-empty",
        ],
    ):
        subprocess.run(  # noqa: S603
            ["git", *args],  # noqa: S607
            cwd=main,
            check=True,
            capture_output=True,
            env=_git_env(),
        )
    linked = tmp_path / "wt"
    subprocess.run(  # noqa: S603
        ["git", "worktree", "add", "-q", "--detach", str(linked)],  # noqa: S607
        cwd=main,
        check=True,
        capture_output=True,
        env=_git_env(),
    )

    assert gate.repo_root(linked) == main.resolve(), (
        gate.repo_root(linked),
        main.resolve(),
    )


def test_more_than_one_leftover_pair_folds(tmp_path: Path) -> None:
    """The fold-pairing loop runs to exhaustion, not once.

    `check()` pairs rows against files by exact spelling first and folds only
    what is left. Every fixture before this one had at most a single unpaired
    name facing a single leftover row, which cannot tell a loop that pairs one
    from a loop that pairs many: changing `while unpaired and rows:` to `if`
    left the whole suite green.

    Three files and two rows, none matching exactly and all folding alike, is the
    smallest corpus where the difference shows — two pairings must happen, so
    exactly one memory is left orphaned. Under a single-iteration pairing it
    reports two.

    In CI this runs on the Linux leg only: the Windows runner enables no
    per-directory case sensitivity and GitHub's macOS volumes are
    case-insensitive by default, so `_case_sensitive_dir` skips there. The
    coverage is real where it runs and absent where it does not — stated rather
    than left for a reader to infer from a silent skip.
    """
    root = _case_sensitive_dir(tmp_path / "memory")
    for name, declared in (
        ("alpha.md", "alpha"),
        ("Alpha.md", "Alpha"),
        ("ALPHA.md", "ALPHA"),
    ):
        (root / name).write_text(
            f"---\nname: {declared}\n---\n\nbody\n", encoding="utf-8", newline="\n"
        )
    (root / gate.INDEX_NAME).write_text(
        "# Index\n\n- [1](AlPhA.md)\n- [2](aLpHa.md)\n",
        encoding="utf-8",
        newline="\n",
    )

    report = gate.check(root)

    assert report.examined["files"] == 3, report.examined
    assert report.examined["index-rows"] == 2, report.examined
    orphans = [e for e in report.errors if "orphaned memory" in e]
    assert len(orphans) == 1, (orphans, report.errors)


# --------------------------------------------------------------------------
# External review round 4. One test per remedy, each written to fail against
# the code as it stood -- ADR-0078 section 6's obligation, which this branch has
# now twice measured a remedy shipping without.
# --------------------------------------------------------------------------


def test_a_comment_opener_in_a_code_span_does_not_eat_the_index() -> None:
    """The load limit is measured on the whole index, not on its first rows.

    `loaded_text` kept a document-wide `<!--.*?-->` with `re.DOTALL` after
    `prose_lines` had given one up for exactly this reason, and it is the worse
    of the two places to keep it: a `<!--` quoted in any row's hook pairs with
    any later `-->` and deletes most of the file *before* the limit is measured.
    Measured on a 190-row index, one such row took it from `index-lines 195` --
    a near-limit warning -- to `index-lines 3` and silence.

    The second assertion is what makes the first discriminating: an
    implementation that simply stopped stripping comments would satisfy the
    first and fail the second.
    """
    rows = "".join(f"- [M{i}](project_m{i}.md) - hook\n" for i in range(40))
    quoted = "# Index\n\n- [X](project_x.md) - a `<!--` in code\n" + rows + "-->\n"

    assert "project_m39" in gate.loaded_text(quoted)
    assert "dropped" not in gate.loaded_text("keep\n<!--\ndropped\n-->\nkeep2\n")


def test_every_line_break_form_counts_toward_the_load_limit(tmp_path: Path) -> None:
    """A CR-only file has no newline at all, so counting "\\n" answered 1.

    Against a 200-line limit that is silence on the one check whose failure the
    module docstring says arrives with no other signal.

    Asserted through `report.examined["index-lines"]` -- the value the limit is
    actually measured against -- and deliberately not by recomputing the count
    in the test. The first version of this did recompute it, and survived a
    mutation putting `body.count("\\n")` back: a test that reimplements the rule
    it is checking agrees with any implementation, including the broken one.
    """
    seen: dict[str, int] = {}
    for label, terminator in (("lf", "\n"), ("cr", "\r"), ("crlf", "\r\n")):
        root = tmp_path / label
        root.mkdir()
        rows = terminator.join(f"- [M{i}](project_m{i}.md) - hook" for i in range(5))
        (root / gate.INDEX_NAME).write_bytes(
            f"# Index{terminator}{terminator}{rows}{terminator}".encode()
        )
        for i in range(5):
            (root / f"project_m{i}.md").write_text(
                f"---\nname: project_m{i}\n---\n\nb\n", encoding="utf-8", newline="\n"
            )
        seen[label] = gate.check(root).examined["index-lines"]

    assert seen == {"lf": 7, "cr": 7, "crlf": 7}, seen


def test_a_row_written_twice_is_a_duplicate_even_beside_a_case_collision(
    tmp_path: Path,
) -> None:
    """N identical spellings can only ever name ONE memory.

    The threshold counted row *occurrences* against the fold group's file count,
    so on a case-sensitive directory holding `alpha.md` and `Alpha.md` an index
    carrying `- [1](alpha.md)` twice gave 2 rows against a threshold of 2, and
    `2 > 2` is False. That pair is literally "one anchored Edit applied twice",
    the incident the error text itself describes, and nothing else named it --
    the orphan loop reports only the unlinked `Alpha.md`.

    Paired with the legitimate case, because a threshold on distinct spellings
    alone would report *that* one as a duplicate.
    """
    root = _case_sensitive_dir(tmp_path / "memory")
    for name, declared in (("alpha.md", "alpha"), ("Alpha.md", "Alpha")):
        (root / name).write_text(
            f"---\nname: {declared}\n---\n\nbody\n", encoding="utf-8", newline="\n"
        )
    (root / gate.INDEX_NAME).write_text(
        "# Index\n\n- [1](alpha.md)\n- [2](alpha.md)\n", encoding="utf-8", newline="\n"
    )

    report = gate.check(root)

    assert any("duplicate index row" in e for e in report.errors), report.errors

    # The boundary: one correct row per real memory is reconciled, not duplicated.
    (root / gate.INDEX_NAME).write_text(
        "# Index\n\n- [1](alpha.md)\n- [2](Alpha.md)\n", encoding="utf-8", newline="\n"
    )

    clean = gate.check(root)

    assert not [e for e in clean.errors if "duplicate index row" in e], clean.errors


def test_a_row_whose_title_holds_brackets_is_still_a_row(tmp_path: Path) -> None:
    """A flat `[^\\]]*` link text made a bracketed row yield no targets at all.

    The memory it named was then reported orphaned -- a hard exit 1, on the
    headline check, against a correct corpus, which the module's own docstring
    calls the thing that gets a check switched off. A single-quoted CommonMark
    title was a second trigger through `TITLE_RE`, which accepted only the
    double-quoted spelling. (The third, a parenthesised title, cannot reach
    either regex: the destination pattern stops at its opening paren.)
    """
    indexes = {
        "brackets": "# Index\n\n- [A [x]](project_a.md) - hook\n",
        "single-quoted-title": "# Index\n\n- [A](project_a.md 'the title') - h\n",
        "double-quoted-title": '# Index\n\n- [A](project_a.md "the title") - h\n',
    }
    for label, index in indexes.items():
        parent = tmp_path / label
        parent.mkdir()
        root = _memory(parent, {"project_a": "a\n"})
        (root / gate.INDEX_NAME).write_text(index, encoding="utf-8", newline="\n")

        report = gate.check(root)

        assert report.examined["index-rows"] == 1, (label, report.examined)
        assert not [e for e in report.errors if "orphaned" in e], (label, report.errors)


def test_a_dead_wiki_link_in_the_index_itself_is_examined(tmp_path: Path) -> None:
    """The wiki-link scan iterated `sources`, which excludes `MEMORY.md`.

    That exclusion is right for the orphan and identity checks -- the index is
    not a memory -- and wrong for this one: a `[[link]]` in a row's hook was
    never examined at all, in the one file guaranteed to load every session.
    The identical link in any memory file was correctly examined.
    """
    root = _memory(tmp_path, {"project_a": "a\n"})
    (root / gate.INDEX_NAME).write_text(
        "# Index\n\n- [A](project_a.md) - see [[project_nope-here]]\n",
        encoding="utf-8",
        newline="\n",
    )

    report = gate.check(root)

    assert report.examined["wiki-links-forward"] == 1, report.examined
    assert any("project_nope-here" in w for w in report.warnings), report.warnings


def test_an_undecodable_memory_is_a_refusal_not_a_traceback(tmp_path: Path) -> None:
    """`UnicodeDecodeError` is a `ValueError`, so it was not a `ReconcileError`.

    One file saved by a Windows-1252 editor -- byte 0x97, the em dash this
    project's CLAUDE.md records PowerShell silently producing, and every memory
    here contains em dashes -- crashed the run with a traceback instead of the
    documented one-line exit 1. The surrounding code's own comment already
    assumes third-party editors touch these files.
    """
    body_dir = tmp_path / "memory-body"
    body_dir.mkdir()
    root = _memory(body_dir, {"project_a": "a\n"})
    (root / "project_b.md").write_bytes(b"---\nname: project_b\n---\n\nem \x97 dash\n")

    with pytest.raises(gate.ReconcileError) as excinfo:
        gate.check(root)

    assert "project_b.md" in str(excinfo.value)

    # The other read site: the index itself.
    index_dir = tmp_path / "index-itself"
    index_dir.mkdir()
    other = _memory(index_dir, {"project_a": "a\n"})
    (other / gate.INDEX_NAME).write_bytes(b"# Index\n\n- [A](a.md) em \x97 dash\n")

    with pytest.raises(gate.ReconcileError):
        gate.check(other)


def test_a_missing_parser_is_reported_in_the_documented_form(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A broken environment must not read as a finding.

    The script reaches markdown-it-py through `check_spec_links`, and `main`
    guarded only `ReconcileError` -- so an absent dependency exited 1 with a
    traceback and no "memory index NOT reconciled" line. A caller reading the
    exit status alone cannot tell that from the real orphan the script exists to
    surface, and ADR-0078 section 1 pins the one-line form.
    """
    root = _memory(tmp_path, {"project_a": "a\n"})

    def no_parser() -> object:
        raise ModuleNotFoundError("No module named 'markdown_it'")

    monkeypatch.setattr(gate, "_markdown", no_parser)

    assert gate.main(["--memory-dir", str(root)]) == 1

    printed = capsys.readouterr().out
    assert printed.startswith("memory index NOT reconciled:"), printed
    assert "markdown_it" in printed


def test_frontmatter_stops_at_its_own_close_not_at_a_later_thematic_break() -> None:
    """`_FRONTMATTER`'s middle quantifier is lazy, and that is load-bearing.

    A memory may open with real frontmatter and then use `---` again in its body
    as an ordinary thematic break. Making the middle quantifier greedy —
    `(?:[^\\n]*\\n)*` instead of `(?:[^\\n]*\\n)*?`, a one-character slip —
    matches through to the *last* such line, so everything between the real
    close and the break is deleted before the load limit is measured. Measured
    on the fixture below, `Body text.` disappears; the file then reads as
    smaller than it is, on the one check whose failure the module docstring says
    arrives with no other signal.

    Every other frontmatter fixture in this module carries at most one `---`
    pair, so none of them separates greedy from lazy. The sibling case that *is*
    tested — a document opening with a thematic break rather than frontmatter —
    is the mirror of this one and passes under both.
    """
    body = "---\nname: alpha\n---\n\nBody text.\n\n---\n\nMore body after a rule.\n"

    loaded = gate.loaded_text(body)

    assert "Body text." in loaded, loaded
    assert "name: alpha" not in loaded, loaded
    # The break itself survives: it is body, not a second frontmatter close.
    assert "More body after a rule." in loaded, loaded


# --------------------------------------------------------------------------
# Smoke round 6: the plausible-neighbour sweep. Each of these pins a quantifier
# or a set member that every existing fixture happened to agree on -- the shape
# five rounds kept producing, found here by sweeping a surface rather than by
# following a reported defect.
# --------------------------------------------------------------------------


def test_bold_prose_referencing_a_memory_is_not_a_row(tmp_path: Path) -> None:
    """`_ROW_LINE`'s marker must be followed by a space or end of line.

    Without that trailing group a `**bold**` opener reads as a list marker, so
    ordinary prose that happens to link a memory a second time is counted as a
    row -- and the corpus's dominant idiom is exactly that. The result is a
    `duplicate index row` error naming a concurrency incident that never
    happened, against a well-formed index.

    The module comment records this as an already-fixed regression; nothing
    guarded the fix. Measured, relaxing the group to `[ \\t]*` left all 108
    tests in this file green.
    """
    root = _memory(tmp_path, {"user_someone": "m\n"})
    (root / gate.INDEX_NAME).write_text(
        "# Index\n\n- [Someone](user_someone.md) - Background\n\n"
        "**Note:** see also [Someone](user_someone.md) for detail\n",
        encoding="utf-8",
        newline="\n",
    )

    report = gate.check(root)

    assert report.examined["index-rows"] == 1, report.examined
    assert report.errors == [], report.errors


def test_every_type_prefix_resolves_a_prefix_dropped_link(tmp_path: Path) -> None:
    """All four prefixes, not the three the other tests happened to use.

    The second resolution tier strips a type prefix so a link written without
    one reports *dead* rather than as a legitimate forward reference. Only
    `reference_`, `project_` and `feedback_` were exercised, so dropping
    `user_` from the tuple left the suite green while turning a real dead link
    into a warning -- silence, which this module calls the more expensive
    direction.
    """
    for prefix in ("user", "feedback", "project", "reference"):
        parent = tmp_path / prefix
        parent.mkdir()
        root = _memory(parent, {f"{prefix}_thing": "t\n", "caller": "[[thing]]\n"})

        report = gate.check(root)

        assert any(f"{prefix}_thing" in e for e in report.errors), (prefix, report)
        assert report.warnings == [], (prefix, report.warnings)


def test_a_declared_name_is_trimmed_of_trailing_whitespace(tmp_path: Path) -> None:
    """`_NAME_FIELD` trims trailing spaces from the value it captures.

    Without the trim, `name: alpha  ` declares `'alpha  '`, which matches no
    filename -- an identity-mismatch error against a memory a human reading the
    frontmatter would call correctly declared, and one no visible edit fixes.
    """
    assert gate.declared_name("---\nname: alpha  \n---\n\nbody\n") == "alpha"
    assert gate.declared_name("---\nname: alpha\t\n---\n\nbody\n") == "alpha"


def test_an_empty_wiki_link_is_not_a_link() -> None:
    """`_WIKI_LINK` requires at least one character between the brackets.

    Relaxing it to `*` makes `[[]]` in ordinary prose a zero-length forward
    reference, so the report carries a warning naming nothing at all.
    """
    assert gate._WIKI_LINK.findall("see [[]] for details") == []  # pyright: ignore[reportPrivateUsage]
    assert gate._WIKI_LINK.findall("see [[x]] for details") == ["x"]  # pyright: ignore[reportPrivateUsage]


def test_two_comments_on_one_line_keep_the_text_between_them() -> None:
    """`_INLINE_COMMENT` is lazy, and `loaded_text` is where that shows.

    Greedy, it spans from the first `<!--` to the last `-->` on the line and
    swallows whatever sits between the two comments -- under-counting the bytes
    and lines the load limit is measured against, which is the silent direction
    on the check whose failure the module docstring says arrives with no other
    signal.

    Asserted through `loaded_text` because that is the path the load limit is
    measured on. It once said `prose_lines` could not observe the difference at
    all, the shared parser having masked the HTML block before the regex ran;
    that stopped being true when `prose_lines` moved to `mask_html_blocks=False`
    to stop losing index rows, so the two now agree here rather than one being
    blind to it.
    """
    loaded = gate.loaded_text("text1 <!-- c1 --> KEEPME <!-- c2 --> text2\n")

    assert "KEEPME" in loaded, loaded
    assert "c1" not in loaded, loaded
    assert "c2" not in loaded, loaded


def test_a_comment_closing_on_a_masked_line_does_not_swallow_the_index() -> None:
    """Report finding 1: the quiet half of the length guard.

    `loaded_text` reads the mask as a column guide and gives up on any line whose
    length masking changed. That is the loud direction on a comment's *opening*
    line -- an unrecognized opener keeps text the loader would have dropped. On
    the *closing* line it is the silent one: the `-->` is never seen, `in_comment`
    never clears, and every remaining line of `MEMORY.md` is `continue`d away
    before the load limit is measured.

    The mask shortened such a line whenever the line-local fallback ran, which a
    code span on the closer line is enough to trigger. Measured before the fix on
    a 400-row index: a correct "past the load limit (406/200 lines)" error became
    `index-lines 2`, no errors, exit 0 -- a false pass on the one check whose
    failure this module's docstring says arrives with no other signal.
    """
    doc = (
        "# Memory Index\n"
        "\n"
        "<!-- a note\n"
        "still inside `code` --> - [Alpha](project_alpha.md) - hook\n"
        "- [Beta](project_beta.md) - hook\n"
        "- [Gamma](project_gamma.md) - hook\n"
    )
    loaded = gate.loaded_text(doc)

    # The comment's own text goes; every row after the closer survives.
    assert "a note" not in loaded, loaded
    for name in ("project_alpha.md", "project_beta.md", "project_gamma.md"):
        assert name in loaded, (name, loaded)


def test_loaded_text_keeps_code_content_when_a_comment_is_quoted_in_a_span() -> None:
    """Report finding 6: `line` and `guide` desynchronized, then a wrong slice.

    The inline-comment strip ran independently over the source line and over the
    masked guide. A `<!-- -->` written inside a code span exists in the first and
    not in the second, so the two lost a different number of characters and every
    column after them was off by the difference -- and the slice that truncates
    at a later opener then cut the source line in the wrong place.

    Both halves of the contract are asserted: the *content* of code is kept and
    counted (`loaded_text`'s docstring), and a live opener does not survive into
    text documented as having comments removed.
    """
    loaded = gate.loaded_text("`<!-- a -->` and <!-- b --> tail <!-- open\n")

    assert "`<!-- a -->`" in loaded, loaded  # the code span, kept whole
    assert "<!-- b -->" not in loaded, loaded  # the real comment, gone
    assert "<!-- open" not in loaded, loaded  # the opener truncates the line


def test_an_index_row_inside_an_html_block_is_still_a_row(tmp_path: Path) -> None:
    """Report finding 4: the two halves of `check()` disagreed about one file.

    `prose_lines` inherited `code_masked_lines`' html-block blanking, which is
    right for the link gate -- a link inside raw HTML is not a link -- and wrong
    for a module that models **the loader**, which reads `MEMORY.md` as text and
    never renders it. A `<details>` fold whose rows are not blank-separated from
    the tag, and a row led by an HTML comment, both vanished from `index-rows`
    and raised a hard "orphaned memory" error, while `loaded_text` went on
    counting those same lines toward the load limit.
    """
    fold = (
        "# Memory Index\n"
        "\n"
        "<details>\n"
        "- [Alpha](project_alpha.md) - hook\n"
        "- [Beta](project_beta.md) - hook\n"
        "</details>\n"
    )
    root = _memory(
        tmp_path, {"project_alpha": "a\n", "project_beta": "b\n"}, index=fold
    )
    assert gate.check(root).errors == [], gate.check(root).errors


def test_a_row_led_by_an_html_comment_is_still_a_row(tmp_path: Path) -> None:
    """Finding 4's second shape: the comment is an HTML block covering the line.

    CommonMark makes the whole line an html_block, so blanking the block took the
    row with it where the old inline-comment strip had kept it. The comment must
    still go and the row must still count.
    """
    index = "# Memory Index\n\n<!-- keep sorted --> - [Alpha](project_alpha.md) - x\n"
    root = _memory(tmp_path, {"project_alpha": "a\n"}, index=index)
    assert gate.check(root).errors == [], gate.check(root).errors


def test_a_wiki_link_inside_an_html_block_is_still_scanned(tmp_path: Path) -> None:
    """Finding 4's silent direction, which the orphan errors were the loud one of.

    `linkable_text` on a fold containing `[[project_nope_here]]` returned nothing
    but blank lines, so the dead cross-reference was never reported at all.
    Check 4 warns rather than errors, permanently, which is what makes silence
    here the whole cost.
    """
    body = "---\nname: caller\n---\n\n<details>\n[[project_nope_here]]\n</details>\n"
    root = _memory(tmp_path, {"caller": body})
    report = gate.check(root)

    assert report.errors == [], report.errors
    assert any("project_nope_here" in w for w in report.warnings), report.warnings
