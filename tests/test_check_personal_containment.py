"""The personal-data containment gate (scripts/check_personal_containment.py).

The gate exists because six independent review lenses each found a *different*
hole in the prose scan it replaces, and every one of those holes read as clean.
A gate that cannot fail is that same defect one layer out, so the tests below
are organised around making it fail: each builds a repository that reproduces
one measured hole and requires the gate to notice.

The holes, and the test that reproduces each:

1. endpoint-diff blindness (add then delete) -- `test_a_file_added_and_deleted_...`
2. unguarded merge base -> `..HEAD` -> silence -- `test_an_unresolvable_base_...`
3. no paths reported for a merge commit -- `test_a_file_introduced_by_the_merge_...`
4. index path proved, content not -- `test_a_skip_worktree_entry_...`
5. case-sensitive prefix, bare path missed -- `test_case_and_bare_path_...`
6. history listed, current files inspected -- `test_the_patch_stream_command_...`

Four more were found while building the gate (untracked-directory collapse,
shallow-clone truncation, tracked-but-unmodified, non-ASCII path quoting) and
one by its first external review (`log.showSignature` welding gpg output onto
the next path). The module docstring carries the authoritative list; this file
carries a fixture for each.

That review also found the shape worth naming here, because it is a *test*
defect rather than a gate defect: every hole-4 test called
`staged_content_mismatches` directly, so deleting the call to it from `check()`
left all 59 tests passing. A function can be thoroughly pinned and reach
nothing. Tests added since reach through `check()` wherever the claim is that
the scan does something, and the ones that call a helper directly do so only to
pin that helper's own contract.

Throwaway repositories rather than mocks: the subject is git's own behaviour
under flags whose failure modes are silence, and a double that returns what the
author expected proves only that the author expected it. `specs/testing-strategy.md`
puts temp-file work inside the unit layer by its own terms.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import check_personal_containment as gate
import pytest
import review_worktree

# Machine git config is neutralized so a developer's `merge.ff`, `init.
# defaultBranch`, or hook path cannot change what these repos do. Function
# scoped for the same reason `test_review_worktree.py` gives: a session-scoped
# autouse fixture leaks the setting into every other module under a shared
# xdist worker.
_NEUTRAL_GIT_ENV = {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull}

_GIT_EXE = shutil.which("git") or "git"

_PERSONAL_FILE = "specs/personal/labs.md"


@pytest.fixture(autouse=True)
def _neutral_git_config(  # pyright: ignore[reportUnusedFunction] - autouse
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for key, value in _NEUTRAL_GIT_ENV.items():
        monkeypatch.setenv(key, value)


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(  # noqa: S603 - fixed executable, no shell
        [_GIT_EXE, "-C", str(repo), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
        env={**os.environ, **_NEUTRAL_GIT_ENV},
        check=False,
    )
    assert proc.returncode == 0, f"git {args} failed: {proc.stdout}{proc.stderr}"
    return proc.stdout


def _write(repo: Path, rel: str, text: str = "synthetic\n") -> Path:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")
    return path


@pytest.fixture(scope="session")
def repo_template(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The repository every test copies, built once per session.

    `origin` is the repository itself, fetched into a real remote-tracking ref,
    because the gate resolves `origin/main` exactly as it does in production. A
    fixture passing a local ref instead would exercise a path no caller uses.

    `core.autocrlf false` is load-bearing rather than hygiene: one test stages a
    deliberate CRLF file under `eol=lf` to prove the blob comparison does not
    false-alarm, and autocrlf would normalize the very bytes it plants.

    Built once for the reason `tests/test_review_worktree.py`'s equivalent
    records: the per-test alternative is ten git spawns, and that file measured
    1.12 s per test on Windows against 18 ms for the copy. "Once" means once per
    **worker** under `pytest -n auto`, not once per run.
    """
    root = tmp_path_factory.mktemp("containment-template") / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "test@example.invalid")
    _git(root, "config", "user.name", "Test")
    _git(root, "config", "commit.gpgsign", "false")
    _git(root, "config", "core.autocrlf", "false")
    _write(root, ".gitignore", "specs/personal/\n")
    _git(root, "add", ".gitignore")
    _git(root, "commit", "-qm", "base")
    _git(root, "remote", "add", "origin", str(root))
    _git(root, "fetch", "-q", "origin")
    return root


@pytest.fixture
def repo(repo_template: Path, tmp_path: Path) -> Path:
    """A private copy of the template — tests commit, branch, and merge in it.

    A freshly `git init`-ed repo records no absolute paths, so a plain directory
    copy relocates intact. The one exception is the **remote URL**, which still
    names the template: left alone, a test that fetches would pull from the
    shared template rather than from itself. Re-pointing it costs one spawn and
    keeps each copy self-consistent.
    """
    root = tmp_path / "repo"
    shutil.copytree(repo_template, root)
    _git(root, "remote", "set-url", "origin", str(root))
    return root


def _on_branch(repo: Path, name: str = "work") -> None:
    _git(repo, "checkout", "-q", "-b", name)


# --------------------------------------------------------------------------
# The predicate (hole 5), and the drift check against the launcher's copy.
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("specs/personal/labs.md", True),
        ("specs/personal", True),  # a plain FILE at the bare path
        ("specs/personal/sub/deep.csv", True),
        ("Specs/Personal/labs.md", True),  # POSIX legs, where the ignore misses it
        ("SPECS/PERSONAL", True),
        ("specs/personal-notes/x.md", False),  # sibling, must not over-match
        ("specs/personalise.md", False),
        ("specs/data-model.md", False),
        ("personal/labs.md", False),  # not under specs/
        ("", False),
    ],
)
def test_case_and_bare_path_are_both_matched(path: str, expected: bool) -> None:
    """Hole 5: the prose guard was case-sensitive and missed the bare path.

    Both halves are load-bearing rather than defensive. `.gitignore`'s rule has
    a trailing slash, so git matches it against directories only and a plain
    file at exactly `specs/personal` is ignored by nothing; and git preserves
    on-disk casing while Windows and macOS filesystems do not, so
    `Specs/Personal/` names the protected directory on the very legs where a
    case-sensitive test lets it through.
    """
    assert gate.is_personal_path(path) is expected


@pytest.mark.parametrize(
    "path",
    [
        "specs/personal/labs.md",
        "specs/personal",
        "Specs/Personal/labs.md",
        "specs/personal-notes/x.md",
        "specs/data-model.md",
        "specs/personal/",
        "",
    ],
)
def test_the_predicate_agrees_with_the_launchers_copy(path: str) -> None:
    """This rule now exists in two modules, so pin them together.

    `scripts/review_worktree.py` guards the same invariant for the reviewer
    worktrees and reached its version through four separate review findings.
    The gate keeps its own copy rather than importing that 2,000-line launcher,
    which is a deliberate trade: no runtime coupling, at the cost of a second
    copy. This test is what buys that trade back -- the copies cannot drift
    without a red test naming the input they disagree on.
    """
    assert gate.is_personal_path(path) == review_worktree._is_personal(  # pyright: ignore[reportPrivateUsage]
        path
    )


def test_the_tracked_pathspec_agrees_with_the_launchers_copy(repo: Path) -> None:
    """The *pathspec* is a second copy of the rule, and it drifts separately.

    The predicate test above compares `is_personal_path` against `_is_personal`
    on strings, which leaves `tracked_personal`'s `:(icase)specs/personal`
    pathspec — a whole second expression of the same rule — pinned by nothing.
    ADR-0070 asserted the two modules "are pinned together by a test that fails
    on any input they disagree about"; before this, widening
    `review_worktree.personal_tracked` (a second magic word, `--others`, a
    trailing slash) would have left the gate narrower with a green suite and an
    ADR saying that could not happen.

    Compared by *behaviour* rather than by argument list, so a rewrite that
    reaches the same answer differently is allowed and a rewrite that changes
    the answer is not.

    The case-varied entry is planted straight into the **index** rather than
    written to disk, and that is the whole reason this test detects anything.
    Written through the filesystem it does not survive contact with either
    development platform: `git init` sets `core.ignorecase=true` on Windows and
    macOS, and `mkdir(parents=True, exist_ok=True)` then resolves
    `Specs/Personal/` onto the lowercase directory an earlier line already made
    — so the file lands as `specs/personal/varied.csv`, the casing under test
    never reaches git, and dropping `:(icase)` leaves this test **green**
    (measured, on Windows; APFS is the same shape). `update-index --cacheinfo`
    records the bytes and the path git is told, with no filesystem in between.
    """
    _on_branch(repo)
    for planted in (
        _PERSONAL_FILE,
        "specs/personal-notes/sibling.md",  # must NOT match
        "specs/personalise.md",  # must NOT match
    ):
        _write(repo, planted)
        _git(repo, "add", "-f", planted)
    _write(repo, "varied-content", "synthetic\n")
    blob = _git(repo, "hash-object", "-w", "--", "varied-content").strip()
    _git(
        repo,
        "update-index",
        "--add",
        "--cacheinfo",
        f"100644,{blob},Specs/Personal/varied.csv",
    )
    assert "Specs/Personal/varied.csv" in _git(repo, "ls-files"), (
        "precondition: the case-varied path really is in the index as written"
    )

    ours = gate.tracked_personal(repo)
    theirs = review_worktree.personal_tracked(repo)
    assert sorted(ours) == sorted(theirs), (ours, theirs)
    assert _PERSONAL_FILE in ours, "precondition: the fixture plants a real match"
    assert "Specs/Personal/varied.csv" in ours, "the icase half must actually fire"
    assert not any("sibling" in path for path in ours), ours


# --------------------------------------------------------------------------
# The parsers, against the shapes measured from git.
# --------------------------------------------------------------------------


def test_porcelain_rename_binds_both_paths() -> None:
    """`status --porcelain -z` emits `R  <new>\\0<old>` -- new first.

    Both paths are returned: a rename *out of* the personal directory is as
    much a containment event as one into it, and a parser that consumed only
    the record field would silently swallow the second path and desynchronize
    every record after it.
    """
    data = "R  renamed.txt\0base.txt\0A  specs/personal/labs.md\0?? untracked.txt\0"
    assert gate.parse_status_z(data) == [
        "renamed.txt",
        "base.txt",
        "specs/personal/labs.md",
        "untracked.txt",
    ]


def test_name_status_rename_binds_the_destination() -> None:
    """`diff --cached --name-status -z` emits `R100\\0<old>\\0<new>` -- old first.

    The opposite order from the porcelain above, which is the trap this pair of
    tests exists to pin. The destination holds the staged bytes, so it is what
    a content comparison must be run against; binding the source would compare
    a path that exists in neither the index nor the working tree and fail on
    every ordinary rename.
    """
    data = "R100\0old.md\0new.md\0A\0added.md\0D\0gone.md\0"
    assert gate.parse_name_status_z(data) == [
        ("R100", "new.md"),
        ("A", "added.md"),
        ("D", "gone.md"),
    ]


@pytest.mark.parametrize(
    ("data", "reason"),
    [
        ("R  only-the-new-path\0", "a rename record with no source field"),
        ("XY\0", "a record too short to hold a path"),
        ("A_no_space_at_index_two\0", "a record whose third character is not a space"),
    ],
)
def test_an_unparseable_porcelain_record_is_not_read_as_clean(
    data: str, reason: str
) -> None:
    """A shape the parser cannot read must raise, never return fewer paths.

    Returning a short list is the exact failure this whole gate exists to
    prevent: a truncated path list is indistinguishable from a clean one, so
    the scan reports success having examined less than it thinks it did.
    """
    with pytest.raises(gate.ContainmentError):
        gate.parse_status_z(data)


def test_a_truncated_name_status_record_is_not_read_as_clean() -> None:
    with pytest.raises(gate.ContainmentError):
        gate.parse_name_status_z("R100\0only-the-source\0")


def test_split_nul_drops_the_empty_records_git_emits() -> None:
    """`git log --format=` emits an empty record per commit beside the paths."""
    assert gate.split_nul("a\0\0b\0") == ["a", "b"]


# --------------------------------------------------------------------------
# Hole 2 -- the base must fail loudly, never leave an unexamined range.
# --------------------------------------------------------------------------


def test_an_unresolvable_base_raises_instead_of_scanning_nothing(repo: Path) -> None:
    """Hole 2: an empty substitution left `..HEAD`, which git answers with
    silence at exit 0 -- a gate reporting clean having examined nothing."""
    with pytest.raises(gate.ContainmentError) as excinfo:
        gate.resolve_merge_base(repo, "origin/does-not-exist")
    assert "refusing to report clean" in str(excinfo.value)


def test_unrelated_histories_raise_rather_than_returning_empty(
    repo: Path, tmp_path: Path
) -> None:
    """The quieter of the two failure modes: `git merge-base` exits **1** with
    no output here, where a missing ref exits 128. Shell substitution loses
    both identically, so both must be caught."""
    other = tmp_path / "unrelated"
    other.mkdir()
    _git(other, "init", "-q", "-b", "main")
    _git(other, "config", "user.email", "test@example.invalid")
    _git(other, "config", "user.name", "Test")
    _write(other, "other.txt")
    _git(other, "add", "other.txt")
    _git(other, "commit", "-qm", "unrelated root")
    _git(repo, "remote", "set-url", "origin", str(other))
    _git(repo, "fetch", "-q", "origin")

    with pytest.raises(gate.ContainmentError) as excinfo:
        gate.resolve_merge_base(repo, "origin/main")
    assert "refusing to report clean" in str(excinfo.value)


def test_the_scan_reports_which_sources_it_examined(repo: Path) -> None:
    """A clean run has to state its own evidence, per-source.

    A single total is the shape that hides hole 2: "1 path examined" reads as
    proof right up until the 1 turns out to be a working-tree entry while the
    history walk contributed nothing.
    """
    _on_branch(repo)
    _write(repo, "notes.md")
    _git(repo, "add", "notes.md")
    _git(repo, "commit", "-qm", "work")

    result = gate.check(root=repo, scope="branch")
    assert result.errors == []
    assert result.examined["branch-history"] >= 1
    assert "branch-history" in result.evidence()
    assert result.base == _git(repo, "rev-parse", "origin/main").strip()


# --------------------------------------------------------------------------
# Holes 1 and 3 -- what the branch history scan must see.
# --------------------------------------------------------------------------


def test_a_file_added_and_deleted_on_the_branch_is_still_caught(repo: Path) -> None:
    """Hole 1: the scan read the endpoint diff, so a file added by one
    checkpoint and deleted by a later one was invisible at both ends while its
    blob rode every push of those commits."""
    _on_branch(repo)
    _write(repo, _PERSONAL_FILE)
    _git(repo, "add", "-f", _PERSONAL_FILE)
    _git(repo, "commit", "-qm", "checkpoint that adds it")
    _git(repo, "rm", "-q", _PERSONAL_FILE)
    _git(repo, "commit", "-qm", "later checkpoint that removes it")

    # The endpoint diff -- what the original prose scan read -- is empty here.
    endpoint = _git(repo, "diff", "--name-only", "origin/main...HEAD").split()
    assert endpoint == [], "precondition: the file is invisible at both ends"

    result = gate.check(root=repo, scope="branch")
    assert any(_PERSONAL_FILE in error for error in result.errors), result.errors


def test_a_file_introduced_by_the_merge_commit_itself_is_caught(repo: Path) -> None:
    """Hole 3: `git log --name-only` reports **no** paths for a merge commit,
    so a file force-added while resolving a merge passed unexamined.

    The construction matters and is easy to get wrong: the file must be present
    in *neither* parent, which is the ordinary shape of resolving a conflict in
    favour of "keep the added file". A file added in a commit *after* the merge
    is found by the default walk too, and a test built that way would pass with
    the flag removed.
    """
    _git(repo, "checkout", "-q", "-b", "side")
    _write(repo, "side.txt")
    _git(repo, "add", "side.txt")
    _git(repo, "commit", "-qm", "side work")
    _git(repo, "checkout", "-q", "main")
    _on_branch(repo, "work")
    _write(repo, "work.txt")
    _git(repo, "add", "work.txt")
    _git(repo, "commit", "-qm", "work")
    _git(repo, "merge", "-q", "--no-ff", "--no-commit", "side")
    _write(repo, _PERSONAL_FILE)
    _git(repo, "add", "-f", _PERSONAL_FILE)
    _git(repo, "commit", "-qm", "merge side, force-adding a file neither parent has")

    base = gate.resolve_merge_base(repo, "origin/main")
    # The default rendering -- what the prose scan used -- cannot see it.
    default_walk = _git(repo, "log", "--name-only", "--format=", f"{base}..HEAD")
    assert _PERSONAL_FILE not in default_walk, "precondition: invisible by default"

    assert _PERSONAL_FILE in gate.branch_paths(repo, base)
    result = gate.check(root=repo, scope="branch")
    assert any(_PERSONAL_FILE in error for error in result.errors), result.errors


def test_the_worktree_scope_does_not_walk_history(repo: Path) -> None:
    """`/savepoint`'s scope is this chunk, deliberately not the branch.

    Asserted so the two scopes cannot quietly converge: the whole-branch
    backstop belongs to `/land`, and a savepoint that paid for a full history
    walk on every checkpoint is one an operator stops running.
    """
    _on_branch(repo)
    _write(repo, _PERSONAL_FILE)
    _git(repo, "add", "-f", _PERSONAL_FILE)
    _git(repo, "commit", "-qm", "committed, and now clean in the porcelain")
    _git(repo, "rm", "-q", "--cached", _PERSONAL_FILE)
    _git(repo, "commit", "-qm", "untrack it again")

    worktree = gate.check(root=repo, scope="worktree")
    assert "branch-history" not in worktree.examined
    assert worktree.base is None

    # ...while the branch scope, which does walk it, still catches the commit.
    branch = gate.check(root=repo, scope="branch")
    assert any(_PERSONAL_FILE in error for error in branch.errors), branch.errors


# --------------------------------------------------------------------------
# The tracked-file gap found while building the gate.
# --------------------------------------------------------------------------


def test_a_tracked_file_predating_the_base_is_caught_by_every_scope(
    repo: Path,
) -> None:
    """Neither the porcelain nor `merge-base..HEAD` sees an already-landed file.

    It is unmodified, so the porcelain is silent; its commit is behind the merge
    base, so the branch walk never reaches it. `tracked_personal` is why every
    scope catches it anyway -- this is the gap that made the check unconditional
    rather than part of the branch scope.
    """
    _write(repo, _PERSONAL_FILE)
    _git(repo, "add", "-f", _PERSONAL_FILE)
    _git(repo, "commit", "-qm", "force-added on main, before any branch exists")
    _git(repo, "fetch", "-q", "origin")
    _on_branch(repo)
    _write(repo, "unrelated.md")
    _git(repo, "add", "unrelated.md")
    _git(repo, "commit", "-qm", "ordinary work")

    assert _git(repo, "status", "--porcelain").strip() == "", "precondition: clean"
    base = gate.resolve_merge_base(repo, "origin/main")
    assert _PERSONAL_FILE not in gate.branch_paths(repo, base), (
        "precondition: the commit sits behind the merge base"
    )

    for scope in gate.SCOPES:
        errors = gate.check(root=repo, scope=scope).errors
        assert any("force-added" in error for error in errors), (scope, errors)


def test_a_case_varied_tracked_path_is_caught(repo: Path) -> None:
    """The `:(icase)` half of hole 5, which the predicate tests do not reach.

    `is_personal_path` case-folds, but it only ever sees paths git chose to
    report — and git's **pathspec** matching is case-sensitive even where the
    filesystem is not, so `git ls-files -- specs/personal` does not list a
    force-added `Specs/Personal/labs.csv` at all. Without `:(icase)` the
    predicate is never consulted, and dropping the flag leaves every other test
    in this file green (measured).
    """
    _on_branch(repo)
    varied = "Specs/Personal/labs.csv"
    _write(repo, varied)
    _git(repo, "add", "-f", varied)
    _git(repo, "commit", "-qm", "force-add under a case-varied path")

    tracked = gate.tracked_personal(repo)
    assert tracked, "the case-varied path must be listed at all"
    assert any(gate.is_personal_path(path) for path in tracked)
    assert any("force-added" in error for error in gate.check(root=repo).errors)


@pytest.mark.parametrize(
    ("scope", "source"),
    [
        ("worktree", "tracked-personal"),
        ("worktree", "worktree"),
        ("worktree", "staged-content"),
        ("branch", "branch-history"),
        ("history", "reachable-history"),
    ],
)
def test_every_examined_source_reports_a_real_count(
    repo: Path, scope: str, source: str
) -> None:
    """Each source's count must track reality, not be a decorative constant.

    `ScanResult`'s own docstring argues a per-source breakdown is what gives a
    zero somewhere to show up. That argument only holds if the numbers are
    load-bearing: zeroing three of the four counters left the whole suite green
    before this test existed, so the evidence line could have gone quietly wrong
    in exactly the direction that hides an unexamined range.

    `staged-content` was the fifth source and had **no counter at all** until
    the first external review of this gate. That is the shape this test exists
    to refuse, so it is now parametrized like the rest — see
    `test_the_staged_content_check_is_wired_into_the_scan` for the other half.
    """
    _on_branch(repo)
    _write(repo, "specs/personal/labs.md")
    _git(repo, "add", "-f", "specs/personal/labs.md")
    _write(repo, "notes.md")
    _git(repo, "add", "notes.md")
    _git(repo, "commit", "-qm", "one tracked personal file and one ordinary file")
    _write(repo, "dirty.md")  # an untracked file, so the worktree count is > 0
    _write(repo, "staged.md")
    _git(repo, "add", "staged.md")  # ...and a staged one, for staged-content

    result = gate.check(root=repo, scope=scope)
    assert result.examined[source] > 0, (source, result.examined)
    assert source in result.evidence()


def test_the_staged_content_check_is_wired_into_the_scan(repo: Path) -> None:
    """The call into `check()` is pinned, not just the function it calls.

    Measured: deleting `errors.extend(staged_content_mismatches(root))` from
    `check()` left **all 59 tests passing**, because every hole-4 test called
    the function directly. A clean run then printed an evidence line identical
    to a working one, and hole 4 rode through unexamined — the same
    unconfigured-control-passes-vacuously shape this module exists to end, one
    layer in. Reaching through `check()` is what makes that mutant die.
    """
    _on_branch(repo)
    _write(repo, "panel.md", "GLUCOSE 400\n")
    _git(repo, "add", "panel.md")
    _git(repo, "update-index", "--skip-worktree", "panel.md")
    _write(repo, "panel.md", "sanitized\n")

    result = gate.check(root=repo, scope="worktree")
    assert any("panel.md" in error for error in result.errors), result.errors
    assert result.examined["staged-content"] == 1


def test_one_path_touched_by_many_commits_is_reported_once(repo: Path) -> None:
    """A history walk names a path once per commit; the report is per path.

    Measured before the fix: five checkpoints touching one leaked file printed
    `personal-data containment violated (6):` with five byte-identical lines,
    and `branch-history 5` for a single distinct path. On a real branch a file
    across thirty savepoints prints thirty copies, burying any second, different
    offender — and `/land` step 3a tells the operator to read that count as
    paths examined.
    """
    _on_branch(repo)
    for n in range(5):
        _write(repo, _PERSONAL_FILE, f"synthetic v{n}\n")
        _git(repo, "add", "-f", _PERSONAL_FILE)
        _git(repo, "commit", "-qm", f"checkpoint {n}")

    result = gate.check(root=repo, scope="branch")
    touching = [e for e in result.errors if "rides every push" in e]
    assert len(touching) == 1, touching
    assert result.examined["branch-history"] == 1, result.examined


def test_the_porcelain_naming_one_path_twice_is_counted_once(repo: Path) -> None:
    """`git status --porcelain` really can repeat a path, so the worktree
    source needs the same deduplication the history walk does.

    Measured: rename `a.md` to `b.md` and then create a *new* `a.md`, and the
    porcelain answers ``R  b.md\\0a.md\\0?? a.md`` — the old rename source and
    the new untracked file are the same string. Both `_distinct` call sites
    other than the branch walk were unpinned until this and the two tests
    below; removing any of them left the whole suite green.
    """
    _on_branch(repo)
    _write(repo, "a.md", "content here\n")
    _git(repo, "add", "a.md")
    _git(repo, "commit", "-qm", "add a.md")
    _git(repo, "mv", "a.md", "b.md")
    _write(repo, "a.md", "a brand new file at the old path\n")

    paths = gate.worktree_paths(repo)
    assert paths.count("a.md") == 2, f"precondition: git repeats it: {paths}"
    assert gate.check(root=repo, scope="worktree").examined["worktree"] == 2, (
        "b.md and a.md are two distinct paths, however many times git names them"
    )


def test_a_path_in_many_reachable_commits_is_counted_once(repo: Path) -> None:
    """The history scope walks every ref, so it repeats a path even harder
    than the branch walk does -- and its `_distinct` was unpinned."""
    _on_branch(repo)
    for n in range(4):
        _write(repo, _PERSONAL_FILE, f"synthetic v{n}\n")
        _git(repo, "add", "-f", _PERSONAL_FILE)
        _git(repo, "commit", "-qm", f"commit {n}")

    raw = gate.history_paths(repo)
    assert raw.count(_PERSONAL_FILE) == 4, f"precondition: once per commit: {raw}"

    result = gate.check(root=repo, scope="history")
    reachable = [e for e in result.errors if "reachable history" in e]
    assert len(reachable) == 1, reachable
    # Asserted against the distinct count rather than a literal: the template
    # contributes its own paths, and a hard number would pin the fixture's
    # shape instead of the deduplication this test is about.
    assert result.examined["reachable-history"] == len(set(raw)), result.examined


def test_an_unmerged_tracked_path_is_counted_once_not_once_per_stage(
    repo: Path,
) -> None:
    """`git ls-files` lists a conflicted path once per stage -- so the
    tracked-personal source repeats too, by a mechanism unlike the other two.

    The `history` scope is the one that reaches this: it runs
    `tracked_personal` but not the staged-content check, which would otherwise
    refuse an unmerged index before the count could be read.
    """
    _git(repo, "checkout", "-q", "-b", "side")
    _write(repo, _PERSONAL_FILE, "side value\n")
    _git(repo, "add", "-f", _PERSONAL_FILE)
    _git(repo, "commit", "-qm", "side force-adds it")
    _git(repo, "checkout", "-q", "main")
    _on_branch(repo, "work")
    _write(repo, _PERSONAL_FILE, "work value\n")
    _git(repo, "add", "-f", _PERSONAL_FILE)
    _git(repo, "commit", "-qm", "work force-adds it too")
    subprocess.run(  # noqa: S603 - the conflict is the point, so exit 1 is fine
        [_GIT_EXE, "-C", str(repo), "merge", "side"],
        capture_output=True,
        check=False,
        env={**os.environ, **_NEUTRAL_GIT_ENV},
    )
    raw = _git(repo, "ls-files", "--", f":(icase){gate.PERSONAL_DIR}").split()
    assert raw.count(_PERSONAL_FILE) > 1, f"precondition: repeated stages: {raw}"

    result = gate.check(root=repo, scope="history")
    forced = [e for e in result.errors if "force-added" in e]
    assert len(forced) == 1, forced
    assert result.examined["tracked-personal"] == 1, result.examined


def test_the_notes_reach_the_caller_and_the_printed_output(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The note has to survive `check()` and `main()`, not just be produced.

    Exactly the defect external review 1 found for the *mismatches* half --
    a helper pinned thoroughly while nothing proved its result reached anyone
    -- reproduced on the sibling path the fix did not cover: deleting either
    `notes.extend(...)` in `check()` or the print loop in `main()` left all 77
    tests green. The note is what tells the content half to read
    `git show :0:<path>` instead of the working file, so a note that reaches
    nobody is the whole point of it lost.
    """
    _on_branch(repo)
    _write(repo, "notes.md", "first\n")
    _git(repo, "add", "notes.md")
    _git(repo, "commit", "-qm", "commit it so the next edit is a modification")
    _write(repo, "notes.md", "first\nsecond\n")
    _git(repo, "add", "notes.md")
    _write(repo, "notes.md", "first\nsecond\nthird\n")

    result = gate.check(root=repo, scope="worktree")
    assert result.errors == []
    assert any("notes.md" in note for note in result.notes), result.notes

    monkeypatch.setattr(gate, "REPO_ROOT", repo)
    assert gate.main(["--scope", "worktree"]) == 0
    out = capsys.readouterr().out
    assert "Note:" in out, out
    assert "git show :0:notes.md" in out, "the note must name the command to read"


def test_an_index_the_two_git_views_disagree_about_is_refused(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`git diff --cached` naming a path `git ls-files -s` does not.

    Unreachable from any natural fixture -- the two commands read the same
    index -- and therefore untested until now, the same shape as the empty
    merge base above. It must raise rather than skip: a staged path the entry
    map cannot classify is one whose hidden-ness is unknown, and treating
    unknown as "not hidden" silently reinstates hole 4 for that path.
    """
    _on_branch(repo)
    _write(repo, "real.md")
    _git(repo, "add", "real.md")
    real_git = gate._git  # pyright: ignore[reportPrivateUsage]

    def fake_git(root: Path, *args: str) -> str:
        if args[:2] == ("diff", "--cached"):
            return "A\0ghost.md\0"
        return real_git(root, *args)

    monkeypatch.setattr(gate, "_git", fake_git)
    with pytest.raises(gate.ContainmentError) as excinfo:
        gate.staged_content_mismatches(repo)
    assert "ghost.md" in str(excinfo.value)


def test_a_precondition_failure_still_names_what_it_had_already_found(
    repo: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A "could not run" must not retract a violation already in hand.

    `tracked_personal` needs no history, so a force-added personal file is
    knowable on a shallow clone and against an unresolvable base — precisely the
    states that raise. Discarding it told the operator to fix their setup while
    a personal file sat in the index unnamed, inverting which of the two is
    urgent.
    """
    _on_branch(repo)
    _write(repo, _PERSONAL_FILE)
    _git(repo, "add", "-f", _PERSONAL_FILE)
    _git(repo, "commit", "-qm", "force-add")

    with pytest.raises(gate.ContainmentError) as excinfo:
        gate.check(root=repo, scope="branch", base_ref="origin/does-not-exist")
    assert any(_PERSONAL_FILE in error for error in excinfo.value.found)

    monkeypatch.setattr(gate, "REPO_ROOT", repo)
    assert gate.main(["--scope", "branch", "--base", "origin/does-not-exist"]) == 1
    out = capsys.readouterr().out
    assert "could not run" in out
    assert _PERSONAL_FILE in out, "the violation must survive into the output"


def test_evidence_says_so_when_nothing_was_examined() -> None:
    """The empty case has to read as absent evidence, not as a clean result."""
    empty = gate.ScanResult(errors=[], examined={}, notes=[])
    assert empty.evidence() == "nothing examined"


def test_an_empty_merge_base_is_refused_even_at_exit_zero(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The empty-output guard, which no natural fixture can reach.

    Both of `git merge-base`'s real failure modes exit non-zero -- an unknown
    ref 128, unrelated histories 1 -- so `_git` raises before the emptiness
    test is ever consulted, and the guard survived mutation testing with the
    whole suite green. It is still the guard that matters most: an empty
    substitution is hole 2 itself, `..HEAD`, which git accepts and answers with
    silence at exit 0. Driving `_git` to return empty output *successfully* is
    the only way to pin it, and a future refactor that made `merge-base` exit 0
    on some new edge is exactly what it is there to survive.
    """
    real_git = gate._git  # pyright: ignore[reportPrivateUsage]

    def fake_git(root: Path, *args: str) -> str:
        return "\n" if args and args[0] == "merge-base" else real_git(root, *args)

    monkeypatch.setattr(gate, "_git", fake_git)
    with pytest.raises(gate.ContainmentError) as excinfo:
        gate.resolve_merge_base(repo, "origin/main")
    assert "refusing to report clean" in str(excinfo.value)


def test_a_merge_base_that_is_not_a_commit_is_refused(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`resolve_merge_base` verifies its answer names a real commit.

    Non-empty output is not the same as usable output. Without the `cat-file -e`
    line a truncated or garbage value reaches a revision range, where git's
    response is another silence — so the check is pinned by driving
    `merge-base` to answer with something that is not a commit.
    """
    real_git = gate._git  # pyright: ignore[reportPrivateUsage]

    def fake_git(root: Path, *args: str) -> str:
        if args and args[0] == "merge-base":
            return "0123456789abcdef0123456789abcdef01234567\n"
        return real_git(root, *args)

    monkeypatch.setattr(gate, "_git", fake_git)
    with pytest.raises(gate.ContainmentError):
        gate.resolve_merge_base(repo, "origin/main")


def test_a_staged_path_deleted_from_the_working_tree_is_a_note_not_a_failure(
    repo: Path,
) -> None:
    """Staged, then deleted from the working tree: git shows it, so it notes.

    The bytes that would commit are genuinely not the bytes in the working tree
    — there are none — which the content half has to know. It is not a
    *failure* because nothing conceals it: `git status` reports `AD`, and
    `/savepoint` step 2 reconciles `git diff --cached --name-only` against its
    enumerated path list. Concealment is the whole discriminator; see
    `test_the_gate_survives_the_ordinary_stage_then_keep_editing_state`.
    """
    _on_branch(repo)
    _write(repo, "vanishing.md")
    _git(repo, "add", "vanishing.md")
    (repo / "vanishing.md").unlink()

    staged = gate.staged_content_mismatches(repo)
    assert staged.mismatches == []
    assert any("vanishing.md" in note for note in staged.notes), staged.notes
    assert staged.examined == 1


def test_a_broken_ignore_rule_surfaces_in_the_worktree_scope(repo: Path) -> None:
    """An untracked personal file is normally invisible to the porcelain
    because the directory is gitignored -- so its appearance there means the
    ignore rule itself broke, which is the critical reading."""
    _on_branch(repo)
    _write(repo, ".gitignore", "# the containment rule was deleted\n")
    _write(repo, _PERSONAL_FILE)

    result = gate.check(root=repo, scope="worktree")
    assert any("ignore rule" in error for error in result.errors), result.errors


# --------------------------------------------------------------------------
# Hole 4 -- the index proves paths, not content.
# --------------------------------------------------------------------------


def test_a_skip_worktree_entry_keeps_a_dirty_blob_the_path_check_waves_through(
    repo: Path,
) -> None:
    """Hole 4: a file staged carrying personal data and then cleaned in the
    working tree keeps the **dirty blob** in the index.

    Every path-based check passes -- `git diff --cached --name-only` lists it,
    and `git diff --name-only` is explicitly told to ignore `skip-worktree` --
    while what commits is the version the containment scan never read.
    """
    _on_branch(repo)
    _write(repo, "panel.md", "GLUCOSE 400\n")
    _git(repo, "add", "panel.md")
    _git(repo, "update-index", "--skip-worktree", "panel.md")
    _write(repo, "panel.md", "sanitized\n")

    # The precondition, asserted rather than assumed: the index still holds the
    # dirty blob while the working tree reads clean.
    assert "GLUCOSE 400" in _git(repo, "cat-file", "-p", ":0:panel.md")
    assert "panel.md" in _git(repo, "diff", "--cached", "--name-only")
    assert _git(repo, "diff", "--name-only").strip() == ""

    staged = gate.staged_content_mismatches(repo)
    assert any("panel.md" in message for message in staged.mismatches), staged
    assert any("declined to update" in m for m in staged.mismatches), staged
    # ...and it reaches the caller, which is a separate claim from the function
    # working: see `test_the_staged_content_check_is_wired_into_the_scan`.
    assert any("panel.md" in error for error in gate.check(root=repo).errors)


def test_the_gate_survives_the_ordinary_stage_then_keep_editing_state(
    repo: Path,
) -> None:
    """`git add`, then keep typing — porcelain `MM` — must not fail the gate.

    The most common mid-session index state there is, and the first external
    review of this gate measured it failing the *whole* scan and blaming a
    `skip-worktree` entry that did not exist. Both callers reach it: this runs
    in `/savepoint` step 1 *before* that skill stages anything, and in `/land`
    step 3a over whatever the tree holds. A gate that fires on the normal case
    is one an operator turns off, which is the argument ADR-0070 used to refuse
    the content half — so this pins the fix as a behaviour, not a comment.
    """
    _on_branch(repo)
    _write(repo, "notes.md", "first\n")
    _git(repo, "add", "notes.md")
    _git(repo, "commit", "-qm", "so the next edit is a modification, not an add")
    _write(repo, "notes.md", "first\nsecond\n")
    _git(repo, "add", "notes.md")
    _write(repo, "notes.md", "first\nsecond\nthird\n")
    assert _git(repo, "status", "--porcelain").split()[0] == "MM", "precondition"

    staged = gate.staged_content_mismatches(repo)
    assert staged.mismatches == [], "an ordinary unstaged edit is not a violation"
    assert any("notes.md" in note for note in staged.notes), staged.notes
    assert gate.check(root=repo, scope="worktree").errors == []


def test_an_ordinary_staged_file_is_not_reported_as_a_mismatch(repo: Path) -> None:
    """The gate has to survive contact with a normal checkpoint."""
    _on_branch(repo)
    _write(repo, "notes.md")
    _git(repo, "add", "notes.md")
    staged = gate.staged_content_mismatches(repo)
    assert staged.mismatches == []
    assert staged.notes == []
    assert staged.examined == 1, "a clean staged path is still one path examined"


def test_a_staged_deletion_is_not_reported_as_a_mismatch(repo: Path) -> None:
    """A deletion has no index blob and no working-tree file, so both halves of
    the comparison fail on it -- without the `D` arm the check breaks on an
    ordinary checkpoint rather than on the case it was built for."""
    _on_branch(repo)
    _write(repo, "doomed.md")
    _git(repo, "add", "doomed.md")
    _git(repo, "commit", "-qm", "add it")
    _git(repo, "rm", "-q", "doomed.md")
    staged = gate.staged_content_mismatches(repo)
    assert staged.mismatches == []
    assert staged.examined == 0, "a deletion contributes no bytes to compare"


def test_an_unmerged_index_is_a_could_not_run_not_a_violation(repo: Path) -> None:
    """A conflicted path has no single staged version to compare against.

    Measured: `git rev-parse :0:f.txt` exits 128 there (*"is in the index, but
    not at stage 0"*), and the earlier version appended that to the violations
    list — telling an operator mid-merge that the tree was contaminated, in a
    message that never named the merge. `/land` step 3a promises the opposite
    split: "could not run" means fix the setup, not the tree. `/savepoint` is
    the skill built for messy in-progress states, so it is the one this shape
    blocked.
    """
    _conflicted_repo(repo)
    assert _git(repo, "status", "--porcelain").startswith("UU"), "precondition"

    with pytest.raises(gate.ContainmentError) as excinfo:
        gate.check(root=repo, scope="worktree")
    assert "unmerged" in str(excinfo.value)
    assert "f.txt" in str(excinfo.value)
    # This refusal fires before the staged comparison that produces notes, so
    # an empty `notes` is its contract rather than an accident -- the same
    # sibling-field claim `test_a_root_below_the_repository_top_level_is_refused`
    # makes, and the one both skills still state in prose.
    assert excinfo.value.notes == [], "it refuses before any note can exist"


def test_a_shallow_clone_does_not_cost_the_worktree_walk(
    repo: Path, tmp_path: Path
) -> None:
    """`require_full_history` is a claim about *history*, and the porcelain is
    not history.

    Measured before the fix: a `--depth 1` clone whose working tree held an
    untracked `specs/personal/labs.md` — the broken-ignore-rule case this
    module calls a critical finding — refused at `require_full_history` under
    `--scope branch` and reported `found: []`, while `worktree_paths` was
    naming the violation. The guard was hoisted above the index-free sources on
    the argument that a history precondition must not cost them, and the
    worktree walk was left below it.
    """
    _on_branch(repo)
    _write(repo, "f.txt", "content\n")
    _git(repo, "add", "f.txt")
    _git(repo, "commit", "-qm", "c1")
    _write(repo, "f.txt", "more\n")
    _git(repo, "add", "f.txt")
    _git(repo, "commit", "-qm", "c2")

    shallow = tmp_path / "shallow"
    _git(repo, "clone", "-q", "--depth", "1", f"file://{repo.as_posix()}", str(shallow))
    _git(shallow, "branch", "-q", "origin/main", "HEAD")
    assert gate.is_shallow(shallow), "precondition: the clone really is shallow"
    # The broken-ignore-rule case, which is the one the porcelain can see at
    # all: with the rule intact an untracked personal file is invisible there
    # by design, so leaving it in would make this test pass for the wrong
    # reason.
    _write(shallow, ".gitignore", "# the containment rule was deleted\n")
    _write(shallow, _PERSONAL_FILE, "synthetic\n")
    assert any(gate.is_personal_path(p) for p in gate.worktree_paths(shallow)), (
        "precondition: the porcelain sees it, so only the ordering can hide it"
    )

    with pytest.raises(gate.ContainmentError) as excinfo:
        gate.check(root=shallow, scope="branch")
    assert "shallow clone" in str(excinfo.value), "it still refuses overall"
    assert any(_PERSONAL_FILE in found for found in excinfo.value.found), (
        "and the worktree walk ran anyway, because it reads no history"
    )
    assert "worktree" in excinfo.value.examined


def test_a_mid_loop_index_inconsistency_keeps_the_mismatch_it_already_found(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The staged loop's raise fires *after* it may have found a real violation.

    A `skip-worktree` mismatch on an earlier path is the most urgent thing the
    scan can produce; a later path that `git diff --cached` names and
    `git ls-files -s` does not is a setup problem. Raising the second with an
    empty payload discarded the first — `check()`'s preservation machinery then
    had nothing to preserve.
    """
    _on_branch(repo)
    _write(repo, "panel.md", "clean\n")
    _git(repo, "add", "panel.md")
    _git(repo, "update-index", "--skip-worktree", "panel.md")
    _write(repo, "panel.md", "edited behind git's back\n")

    assert "ghost.md" not in gate.index_entries(repo), (
        "precondition: the second path really is absent from the index"
    )
    _real_git = gate._git  # pyright: ignore[reportPrivateUsage]

    def fake_git(root: Path, *args: str) -> str:
        if args[0] == "diff" and "--cached" in args:
            # Both paths, the real mismatch first so the loop accumulates it
            # before reaching the one the index cannot account for.
            return "M\0panel.md\0M\0ghost.md\0"
        return _real_git(root, *args)

    monkeypatch.setattr(gate, "_git", fake_git)
    with pytest.raises(gate.ContainmentError) as excinfo:
        gate.staged_content_mismatches(repo)

    assert "cannot be read consistently" in str(excinfo.value)
    assert any("panel.md" in found for found in excinfo.value.found), (
        "the confirmed mismatch survives the setup failure that stopped the loop"
    )


def test_a_deferred_refusal_still_names_the_patch_stream(repo: Path) -> None:
    """`base` was the one `ScanResult` field a refusal dropped.

    A deferred staged refusal fires *after* `resolve_merge_base` succeeded, so
    the scan knows where the content half must start reading — and printed
    every other field while withholding that one, on the exit where the tree is
    already known to be in trouble.
    """
    _conflicted_repo(repo, leaked_personal_file=True)

    with pytest.raises(gate.ContainmentError) as excinfo:
        gate.check(root=repo, scope="branch", base_ref="origin/main")
    assert excinfo.value.base is not None, "the resolved base survives the refusal"
    assert gate.patch_stream_command(excinfo.value.base).endswith("..HEAD")


def test_the_violation_path_states_its_evidence(
    repo: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every exit prints the counts, and this was the one that did not.

    `/land` step 3a tells the operator to read the evidence line rather than
    the exit code, and to treat a `branch-history 0` on a branch with commits
    as a scan that examined nothing. On the violation exit there was no line to
    apply that to — the exit where a second, unexamined source matters most.
    """
    _on_branch(repo)
    _write(repo, _PERSONAL_FILE, "synthetic\n")
    _git(repo, "add", "-f", _PERSONAL_FILE)

    monkeypatch.setattr(gate, "REPO_ROOT", repo)

    assert gate.main(["--scope", "worktree"]) == 1
    out = capsys.readouterr().out
    assert "containment violated" in out
    assert "Examined:" in out
    assert "worktree" in out


def test_the_violation_path_still_names_the_patch_stream(
    repo: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A violation does not excuse withholding the content half's instrument.

    Deliberately the `branch` scope: the sibling test above runs `worktree`,
    where `base` is `None` and no patch-stream line is due at all — so it
    passed with the printing removed, which a mutation caught. The instruction
    matters most here, since a violation is exactly when `/land` step 3b has to
    read the stream rather than the current files.
    """
    _on_branch(repo)
    _write(repo, "f.txt", "content\n")
    _git(repo, "add", "f.txt")
    _git(repo, "commit", "-qm", "a base to resolve")
    _git(repo, "branch", "-q", "-f", "origin/main")
    _write(repo, _PERSONAL_FILE, "synthetic\n")
    _git(repo, "add", "-f", _PERSONAL_FILE)
    monkeypatch.setattr(gate, "REPO_ROOT", repo)

    assert gate.main(["--scope", "branch"]) == 1
    out = capsys.readouterr().out
    assert "containment violated" in out
    assert "scan the patch stream for values" in out
    assert "--diff-merges=first-parent" in out
    # The intersection the sibling test cannot reach: the sibling proves the
    # `Examined:` line under `worktree`, this one proves the patch stream under
    # `branch`, and scope-gating the evidence print to skip `branch` survived
    # both until this line existed. `/land` step 3a's "a `branch-history 0` on
    # a branch that has commits" advice is about exactly this exit.
    assert "Examined:" in out
    assert "branch-history" in out


def test_a_refusal_that_resolved_a_base_prints_the_patch_stream(
    repo: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Through `main()`'s stdout, not through the exception object.

    Its sibling `test_a_deferred_refusal_still_names_the_patch_stream` asserts
    on `excinfo.value.base`, which proves the field is carried and nothing
    about whether anyone is ever shown it — deleting `_print_patch_stream` from
    the could-not-run handler left all 116 tests green. That is the same
    exception-versus-stdout split that produced a survivor in the previous
    round, in the fix for the previous round.

    A *deferred* refusal is what makes this reachable at all: the base has to
    resolve before the refusal fires, and every other could-not-run test uses
    an unresolvable base, so `base` is `None` and the line is a no-op there.
    """
    _conflicted_repo(repo, leaked_personal_file=True)
    monkeypatch.setattr(gate, "REPO_ROOT", repo)

    assert gate.main(["--scope", "branch", "--base", "origin/main"]) == 1
    out = capsys.readouterr().out
    assert "could not run" in out
    assert "scan the patch stream for values" in out


@pytest.mark.parametrize(
    "source", ["tracked-personal", "worktree", "branch-history", "reachable-history"]
)
def test_every_enumeration_source_stays_unannotated(source: str) -> None:
    """Each member of the allowlist, not just whichever one a sibling happens
    to use.

    The allowlist was pinned only through `worktree`, so *emptying* it was
    caught while **dropping a single member** was not: removing
    `branch-history` alone left all 116 tests green. A count that silently
    gains `(not a containment test)` is the annotation firing on a source that
    is one — the mirror image of the misreading the allowlist exists to end,
    and just as invisible.
    """
    evidence = gate._evidence({source: 4})  # pyright: ignore[reportPrivateUsage]
    assert evidence == f"{source} 4", "an enumeration source carries no annotation"


def test_an_unclassified_examined_source_is_annotated_by_default(repo: Path) -> None:
    """The allowlist runs the safe way round.

    A future source that counts something other than paths-tested-for-
    containment must be called out without anyone remembering to add it — the
    misreading the annotation exists to end, reproduced by omission rather than
    by drift.
    """
    evidence = gate._evidence(  # pyright: ignore[reportPrivateUsage]
        {"worktree": 3, "blob-bytes": 7}
    )
    assert "worktree 3," in evidence, "a known enumeration source stays bare"
    assert "blob-bytes 7 (not a containment test)" in evidence


def test_an_empty_toplevel_answer_is_refused(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Defence in depth, pinned without pretending the state is natural.

    No measured git state reaches this branch — a bare repository, a cwd inside
    `.git`, and an empty `GIT_WORK_TREE` all exit 128, which `_git` turns into
    a different error first. It is kept because an empty answer at exit 0 would
    compare equal to nothing and fall through to a scan, which is silence
    answering a precondition. Stubbed rather than constructed, and the module's
    one-negative-fixture-per-guard bar is met without claiming more than that.
    """
    _real_git = gate._git  # pyright: ignore[reportPrivateUsage]

    def fake_git(root: Path, *args: str) -> str:
        if args[:2] == ("rev-parse", "--show-toplevel"):
            return "\n"
        return _real_git(root, *args)

    monkeypatch.setattr(gate, "_git", fake_git)
    with pytest.raises(gate.ContainmentError) as excinfo:
        gate.require_repository_root(repo)
    assert "returned nothing" in str(excinfo.value)


def _conflicted_repo(repo: Path, *, leaked_personal_file: bool = False) -> None:
    """Leave `repo` on branch `work` holding an unresolved `UU` conflict.

    Four tests need this state and each carried its own copy of the same
    fifteen lines, three of them character-for-character identical. Any change
    to how a conflict is planted — git's merge defaults, the neutral-env dict,
    the precondition assertion — had to be made four times or the copies
    diverged.

    `leaked_personal_file` additionally creates an `origin/main` at the base
    commit and commits *then removes* a personal file on `work`: invisible in
    the working tree, alive in the branch's history, which is what a
    branch-scope walk has to find while the index is unusable. One knob rather
    than a second helper, because the two shapes differ only by what is
    interleaved into the same sequence.
    """
    _write(repo, "f.txt", "base\n")
    _git(repo, "add", "f.txt")
    _git(repo, "commit", "-qm", "a common ancestor, so the conflict is UU not AA")
    if leaked_personal_file:
        _git(repo, "branch", "-q", "-f", "origin/main")
    _git(repo, "checkout", "-q", "-b", "side")
    _write(repo, "f.txt", "side\n")
    _git(repo, "add", "f.txt")
    _git(repo, "commit", "-qm", "side")
    _git(repo, "checkout", "-q", "main")
    _on_branch(repo, "work")
    if leaked_personal_file:
        _write(repo, _PERSONAL_FILE, "synthetic\n")
        _git(repo, "add", "-f", _PERSONAL_FILE)
        _git(repo, "commit", "-qm", "a savepoint that leaked")
        _git(repo, "rm", "-q", _PERSONAL_FILE)
    _write(repo, "f.txt", "work\n")
    _git(repo, "add", "f.txt")
    _git(repo, "commit", "-qm", "work")
    subprocess.run(  # noqa: S603 - a conflict is the point, so a non-zero exit is fine
        [_GIT_EXE, "-C", str(repo), "merge", "side"],
        capture_output=True,
        check=False,
        env={**os.environ, **_NEUTRAL_GIT_ENV},
    )
    assert "UU f.txt" in _git(repo, "status", "--porcelain"), (
        "precondition: the merge really did conflict"
    )


def test_a_conflicted_index_does_not_cost_the_branch_history_walk(
    repo: Path,
) -> None:
    """The half that needs no index must still run when the index is unusable.

    Found by the first external review. The staged-content sub-check ran ahead
    of the branch walk, so an unresolved merge — which `/land` step 3a meets
    routinely, since merging `origin/main` before landing is ordinary — raised
    before `resolve_merge_base` and `branch_paths` ever executed. A personal
    file force-added by an earlier savepoint on that branch was then reported
    by **nothing**, even though the branch walk reads only history and is
    entirely independent of the conflicted index.

    The scan still refuses overall: an unmerged index is a real "could not
    run". What must survive is the enumeration it does not depend on, carried
    out on `ContainmentError.found`.
    """
    _conflicted_repo(repo, leaked_personal_file=True)

    with pytest.raises(gate.ContainmentError) as excinfo:
        gate.check(root=repo, scope="branch", base_ref="origin/main")
    assert "unmerged" in str(excinfo.value), "it still refuses overall"
    assert any(_PERSONAL_FILE in found for found in excinfo.value.found), (
        "the branch-history walk ran and its finding survived the refusal"
    )
    assert "branch-history" in excinfo.value.examined, (
        "and it says so in its evidence, so a zero has somewhere to show up"
    )


def test_a_second_precondition_failure_is_not_erased_by_the_one_that_wins(
    repo: Path,
) -> None:
    """Only one exception propagates, and ordering picks it -- not usefulness.

    Measured: an unresolved merge *and* an unresolvable base in one `branch`
    run reported only the base. The deferred staged failure was captured and
    then dropped when `resolve_merge_base` raised ahead of the re-raise, so the
    operator was sent after a `--base` fix while the merge conflict that
    actually blocks `git commit` went unmentioned -- the more actionable of the
    two, invisible.
    """
    _conflicted_repo(repo)

    with pytest.raises(gate.ContainmentError) as excinfo:
        gate.check(root=repo, scope="branch", base_ref="origin/does-not-exist")
    assert "merge base" in str(excinfo.value), "the base failure won the race"
    assert any("unmerged" in failure for failure in excinfo.value.also_failed), (
        "and the merge conflict is still reported rather than erased"
    )


def test_the_losing_refusal_reaches_the_operator_not_just_the_exception(
    repo: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Carrying it on the exception is half the fix; printing it is the half
    the operator sees.

    Found by mutation: silencing `main()`'s `also_failed` block left the test
    above green, because that one asserts on the exception object and never
    drives the CLI. A payload nothing prints is the "thoroughly pinned and
    reaching nothing" shape this file was reorganised around, arriving through
    the fix for it.
    """
    _conflicted_repo(repo)
    monkeypatch.setattr(gate, "REPO_ROOT", repo)

    assert gate.main(["--scope", "branch", "--base", "origin/does-not-exist"]) == 1
    out = capsys.readouterr().out
    assert "more than one reason" in out
    assert "unmerged" in out, "the merge conflict is named in the output"


def test_a_single_refusal_is_not_reported_twice(repo: Path) -> None:
    """The deferred failure *is* the propagating one on the ordinary path.

    Paired with the test above: listing `also_failed` unconditionally would
    print the same refusal under itself, so the identity test in the handler is
    what keeps one problem reading as one problem.
    """
    _conflicted_repo(repo)

    with pytest.raises(gate.ContainmentError) as excinfo:
        gate.check(root=repo, scope="worktree")
    assert "unmerged" in str(excinfo.value)
    assert excinfo.value.also_failed == [], "not listed underneath itself"


def test_a_force_added_personal_file_survives_a_shallow_clone_refusal(
    repo: Path, tmp_path: Path
) -> None:
    """`tracked_personal` reads the index and walks no history, which is the
    whole reason it runs before the history preconditions.

    The `found`-preservation guarantee was pinned only for an unresolvable
    base; a shallow clone is the other refusal it was designed for and had no
    test planting anything to preserve, so the two halves of the argument were
    covered one apart.
    """
    _on_branch(repo)
    _write(repo, _PERSONAL_FILE, "synthetic\n")
    _git(repo, "add", "-f", _PERSONAL_FILE)
    _git(repo, "commit", "-qm", "force-added, and now in history")

    shallow = tmp_path / "shallow"
    _git(repo, "clone", "-q", "--depth", "1", f"file://{repo.as_posix()}", str(shallow))
    assert gate.is_shallow(shallow), "precondition: the clone really is shallow"

    with pytest.raises(gate.ContainmentError) as excinfo:
        gate.check(root=shallow, scope="history")
    assert "shallow clone" in str(excinfo.value)
    assert any(_PERSONAL_FILE in found for found in excinfo.value.found), (
        "the index-only finding survives the history refusal"
    )


@pytest.mark.parametrize(
    ("name", "stages"),
    [
        ("both-modified", (1, 2, 3)),
        ("added-by-us", (2,)),
        ("added-by-them", (3,)),
        ("deleted-by-them", (1, 2)),
        ("deleted-by-us", (1, 3)),
        ("both-deleted", (1,)),
    ],
)
def test_every_unmerged_shape_stays_visible_to_the_staged_diff(
    repo: Path, name: str, stages: tuple[int, ...]
) -> None:
    """The empty-set early return rests on this, so it is pinned rather than
    measured once.

    `staged_content_mismatches` reads `git diff --cached --name-status` first
    and returns early when it is empty, which is only safe while *every*
    unmerged shape shows up there -- otherwise a conflicted index could slip
    past the refusal by looking like an empty staged set. The claim began as a
    single observation on an ordinary `UU` conflict; these are the shapes an
    ordinary merge cannot easily produce, planted through plumbing because
    `both-deleted` in particular auto-resolves cleanly under `git merge`.
    """
    _on_branch(repo)
    _write(repo, "f.txt", "content\n")
    _git(repo, "add", "f.txt")
    _git(repo, "commit", "-qm", "so there is a blob to point the stages at")
    blob = _git(repo, "rev-parse", "HEAD:f.txt").strip()
    _git(repo, "rm", "-q", "--cached", "f.txt")

    index_info = "".join(f"100644 {blob} {stage}\tf.txt\n" for stage in stages)
    subprocess.run(  # noqa: S603 - fixed executable, no shell
        [_GIT_EXE, "-C", str(repo), "update-index", "--index-info"],
        input=index_info.encode("utf-8"),
        capture_output=True,
        check=True,
        env={**os.environ, **_NEUTRAL_GIT_ENV},
    )

    staged = _git(repo, "diff", "--cached", "--name-status")
    assert staged.startswith("U"), f"{name}: shape is visible as U, got {staged!r}"
    with pytest.raises(gate.ContainmentError) as excinfo:
        gate.staged_content_mismatches(repo)
    assert "unmerged" in str(excinfo.value), f"{name}: and the refusal still fires"


def test_a_non_decimal_stage_field_is_refused_rather_than_crashing(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`str.isdigit` admits exactly the characters `int()` then rejects.

    `'²'.isdigit()` is True while `int('²')` raises `ValueError`, so the
    guard promising a `ContainmentError` handed back a traceback nothing owns
    — against a module docstring that states the opposite contract.
    `.isdecimal()` is the predicate `int()` implements.
    """
    record = "H 100644 " + ("0" * 40) + " ²\tb.md"

    def fake_git(root: Path, *args: str) -> str:
        if args[0] == "ls-files" and "-s" in args:
            return record + "\0"
        return _real_git(root, *args)

    _real_git = gate._git  # pyright: ignore[reportPrivateUsage]
    monkeypatch.setattr(gate, "_git", fake_git)
    with pytest.raises(gate.ContainmentError) as excinfo:
        gate.index_entries(repo)
    assert "unparseable" in str(excinfo.value)


def test_an_empty_index_is_not_charged_for_a_full_index_read(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`/savepoint` step 1 runs before anything is staged, so this is the
    ordinary invocation rather than an edge case.

    The full-index read and the `diff-files` spawn answer a loop that never
    executes when nothing is staged, and on Windows — this project's primary
    platform — each spawn costs the 30-50 ms `index_entries` cites, twice per
    checkpoint. Pinned by counting the git subcommands rather than by timing,
    which would be flaky.
    """
    _on_branch(repo)
    _write(repo, "tracked.md", "committed\n")
    _git(repo, "add", "tracked.md")
    _git(repo, "commit", "-qm", "nothing staged after this")
    assert _git(repo, "diff", "--cached", "--name-only") == "", "precondition"

    seen: list[str] = []
    _real_git = gate._git  # pyright: ignore[reportPrivateUsage]

    def counting_git(root: Path, *args: str) -> str:
        seen.append(args[0])
        return _real_git(root, *args)

    monkeypatch.setattr(gate, "_git", counting_git)
    assert gate.staged_content_mismatches(repo) == gate.StagedScan([], [], 0)
    assert seen == ["diff"], f"one spawn, not three: {seen}"


def test_a_staged_path_still_pays_for_the_index_read(repo: Path) -> None:
    """The early return must not cost the check it exists to speed up.

    The optimization above is only safe while a *non*-empty staged set still
    reaches the blob comparison, so this pins the other side of the branch —
    otherwise `return StagedScan([], [], 0)` unconditionally would pass every
    test the empty case has.
    """
    _on_branch(repo)
    _write(repo, "panel.md", "clean\n")
    _git(repo, "add", "panel.md")
    _git(repo, "update-index", "--skip-worktree", "panel.md")
    _write(repo, "panel.md", "edited behind git's back\n")

    staged = gate.staged_content_mismatches(repo)
    assert staged.examined == 1
    assert len(staged.mismatches) == 1


def test_a_hidden_entry_that_is_not_a_regular_file_is_reported_unverified(
    repo: Path,
) -> None:
    """A symlink's blob is its target string, which `hash-object` never yields.

    `git hash-object -- <link>` follows the link and hashes the *target's*
    bytes while the index stores the target *path*, so the two never agree; a
    symlink to a directory cannot be hashed at all, and a gitlink has no bytes
    in this repository. Only entries git hides from `git diff` are compared by
    hand at all, so this is reachable only for a symlink that is also
    `skip-worktree` — but reporting it unverified rather than mismatched keeps
    the message honest about which of the two happened.
    """
    _on_branch(repo)
    # Written as an index entry directly: `core.symlinks` is off on many
    # Windows checkouts, so creating a real link is not portable while planting
    # the mode-120000 entry that reproduces it is. The blob holds the link
    # target *string*, which is exactly what git stores for a symlink.
    _write(repo, "target-string", "README.md")
    target = _git(repo, "hash-object", "-w", "--", "target-string").strip()
    _git(repo, "update-index", "--add", "--cacheinfo", f"120000,{target},link.md")
    _git(repo, "update-index", "--skip-worktree", "link.md")

    entries = gate.index_entries(repo)
    assert entries["link.md"].mode == "120000"
    assert entries["link.md"].hidden

    staged = gate.staged_content_mismatches(repo)
    assert any("link.md" in message for message in staged.mismatches), staged
    assert any("mode 120000" in m for m in staged.mismatches), staged


def test_an_assume_unchanged_entry_is_hidden_too(repo: Path) -> None:
    """`git ls-files -v` lowercases the tag for `assume-unchanged`.

    The flag differs from `skip-worktree`'s `S` and git hides the divergence
    just the same, so a check keyed only on `S` would miss half the shape hole
    4 names. Pinned because the lowercase rule is a property of git's output
    table rather than anything visible in this module.
    """
    _on_branch(repo)
    _write(repo, "panel.md", "GLUCOSE 400\n")
    _git(repo, "add", "panel.md")
    _git(repo, "update-index", "--assume-unchanged", "panel.md")
    _write(repo, "panel.md", "sanitized\n")

    assert gate.index_entries(repo)["panel.md"].hidden
    staged = gate.staged_content_mismatches(repo)
    assert any("panel.md" in message for message in staged.mismatches), staged


@pytest.mark.parametrize(
    "record",
    ["H 100644 abc123 0 no-tab-here", "H 100644 0\tshort.md", "H 100644 abc x\tb.md"],
)
def test_an_unparseable_index_record_is_not_read_as_clean(
    repo: Path, record: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A shape the index parser cannot read must raise, never return fewer
    entries -- a short entry map silently reclassifies a hidden path as absent."""
    real_git = gate._git  # pyright: ignore[reportPrivateUsage]

    def fake_git(root: Path, *args: str) -> str:
        if args and args[0] == "ls-files" and "-v" in args:
            return record + "\0"
        return real_git(root, *args)

    monkeypatch.setattr(gate, "_git", fake_git)
    with pytest.raises(gate.ContainmentError):
        gate.index_entries(repo)


def test_a_staged_rename_is_compared_against_its_destination(repo: Path) -> None:
    """Binding the source path would fail on every ordinary rename: the source
    exists in neither the index nor the working tree."""
    _on_branch(repo)
    _write(repo, "before.md", "stable content\n")
    _git(repo, "add", "before.md")
    _git(repo, "commit", "-qm", "add it")
    _git(repo, "mv", "before.md", "after.md")

    status = _git(repo, "diff", "--cached", "--name-status")
    assert status.startswith("R"), f"precondition: a detected rename, got {status!r}"
    assert gate.staged_content_mismatches(repo).mismatches == []


def _normalizing_repo(repo: Path) -> None:
    """A committed `.gitattributes` forcing LF, as this repository has."""
    _on_branch(repo)
    _write(repo, ".gitattributes", "* text eol=lf\n")
    _git(repo, "add", ".gitattributes")
    _git(repo, "commit", "-qm", "normalize")


def test_a_visible_crlf_working_file_under_eol_lf_is_not_even_a_note(
    repo: Path,
) -> None:
    """An ordinary staged CRLF file is filter-equal all the way through.

    This pins the *visible* half: `git diff-files` applies the same filters, so
    such a path never reaches `visibly_dirty` and never becomes a note. It does
    **not** pin the `hash-object` comparison -- a non-hidden entry short-circuits
    before it -- which is what the test below is for, and what this test alone
    was mistaken for.
    """
    _normalizing_repo(repo)
    (repo / "crlf.md").write_bytes(b"line one\r\nline two\r\n")
    _git(repo, "add", "crlf.md")

    staged = gate.staged_content_mismatches(repo)
    assert staged.mismatches == []
    assert staged.notes == [], "a filter-equal file is not even worth a note"


def test_a_hidden_crlf_entry_is_compared_with_its_filters_applied(
    repo: Path,
) -> None:
    """`git hash-object` must apply the path's filters, and only a hidden entry
    reaches it.

    The docstring's stated reason for the blob-to-blob comparison -- that
    `git hash-object` applies `.gitattributes` by default, so a CRLF working
    file under `eol=lf` compares equal -- was pinned by nothing once
    non-hidden entries gained their `visibly_dirty` short-circuit. Measured by
    the **second** external review: adding `--no-filters` to that call left all
    82 tests passing. `skip-worktree` is what carries the file past the
    short-circuit to the comparison the claim is actually about.

    The attribution matters enough to be worth stating precisely, because the
    first review is what *introduced* the short-circuit this test exists to
    reach past — so crediting it with the discovery would have the same review
    finding and causing the same defect.

    Without filters the raw CRLF bytes hash differently from the LF blob `git
    add` stored, so the mutant produces a mismatch here and this test fails --
    which is the whole point of it.
    """
    _normalizing_repo(repo)
    (repo / "crlf.md").write_bytes(b"line one\r\nline two\r\n")
    _git(repo, "add", "crlf.md")
    _git(repo, "update-index", "--skip-worktree", "crlf.md")

    entries = gate.index_entries(repo)
    assert entries["crlf.md"].hidden, "precondition: the entry reaches hash-object"
    raw = _git(repo, "hash-object", "--no-filters", "--", "crlf.md").strip()
    assert entries["crlf.md"].blob != raw, (
        "precondition: the unfiltered hash differs, so filters are load-bearing"
    )

    staged = gate.staged_content_mismatches(repo)
    assert staged.mismatches == [], "filters applied: CRLF vs LF compares equal"
    assert staged.examined == 1, "the comparison ran rather than being skipped"


# --------------------------------------------------------------------------
# Hole 6 -- the content half's instrument, and the boundary the gate keeps.
# --------------------------------------------------------------------------


def test_the_patch_stream_command_starts_at_the_resolved_base(repo: Path) -> None:
    """Hole 6: the history scan listed paths while the content instruction
    inspected *current* files, so a value committed and then sanitized rode the
    push invisibly. The patch stream is the view that sees it.

    The command must name the **resolved base**, not the ref: on a branch that
    has merged its base back in, `origin/main..HEAD` and `<merge-base>..HEAD`
    are different commit sets, and the shorter one skips exactly the merged-in
    commits the scan already walked.
    """
    _on_branch(repo)
    _write(repo, "panel.md", "GLUCOSE 400\n")
    _git(repo, "add", "panel.md")
    _git(repo, "commit", "-qm", "a value lands")
    _write(repo, "panel.md", "sanitized\n")
    _git(repo, "add", "panel.md")
    _git(repo, "commit", "-qm", "and is cleaned up again")

    result = gate.check(root=repo, scope="branch")
    assert result.errors == [], "enumeration is clean -- the path was never personal"
    assert result.base is not None

    command = gate.patch_stream_command(result.base)
    assert result.base in command
    assert "--diff-merges=first-parent" in command

    # The stream really does carry the removed value, which is why the content
    # half is pointed at it rather than at the files as they now stand.
    stream = _git(repo, *command.split()[1:])
    assert "+GLUCOSE 400" in stream
    assert "GLUCOSE 400" not in (repo / "panel.md").read_text(encoding="utf-8")


def test_the_gate_does_not_judge_file_contents(repo: Path) -> None:
    """The boundary, asserted so a later change cannot quietly cross it.

    A file full of health-shaped values outside `specs/personal/` is *not* a
    finding here. Deciding whether a value is synthetic is judgement that stays
    with `/land` and `/savepoint`; a gate that guessed would produce false
    positives on every test fixture in the repository and be turned off.
    """
    _on_branch(repo)
    _write(repo, "tests/fixtures/panel.md", "Glucose 92 mg/dL\nHbA1c 5.4 %\n")
    _git(repo, "add", "tests/fixtures/panel.md")
    _git(repo, "commit", "-qm", "synthetic fixture")

    assert gate.check(root=repo, scope="branch").errors == []


# --------------------------------------------------------------------------
# The history scope, and the instrument it deliberately does not use.
# --------------------------------------------------------------------------


def test_the_history_scope_sees_a_commit_on_an_unmerged_branch(repo: Path) -> None:
    """CI's backstop is `--all`, not `HEAD`: a personal file sitting on another
    pushed branch is a live exposure whichever branch is being built."""
    _git(repo, "checkout", "-q", "-b", "abandoned")
    _write(repo, _PERSONAL_FILE)
    _git(repo, "add", "-f", _PERSONAL_FILE)
    _git(repo, "commit", "-qm", "left on a branch nobody merged")
    _git(repo, "checkout", "-q", "main")

    assert _git(repo, "status", "--porcelain").strip() == ""
    result = gate.check(root=repo, scope="history")
    assert any(_PERSONAL_FILE in error for error in result.errors), result.errors


def test_the_history_scope_sees_a_file_introduced_by_a_merge_commit(
    repo: Path,
) -> None:
    """Hole 3 again, in the scope that is CI's backstop rather than `/land`'s.

    `--diff-merges=first-parent` is set on **two** walks, and only the branch
    one was pinned: removing it from `history_paths` left the whole suite green
    (measured by mutation). The scopes fail differently -- the branch walk is
    bounded by a merge base and the history walk is `--all` -- so one test
    cannot stand for both, and the untested half is the one that runs on every
    pull request with nothing local ahead of it.
    """
    _git(repo, "checkout", "-q", "-b", "side")
    _write(repo, "side.txt")
    _git(repo, "add", "side.txt")
    _git(repo, "commit", "-qm", "side work")
    _git(repo, "checkout", "-q", "main")
    _on_branch(repo, "work")
    _write(repo, "work.txt")
    _git(repo, "add", "work.txt")
    _git(repo, "commit", "-qm", "work")
    _git(repo, "merge", "-q", "--no-ff", "--no-commit", "side")
    _write(repo, _PERSONAL_FILE)
    _git(repo, "add", "-f", _PERSONAL_FILE)
    _git(repo, "commit", "-qm", "merge side, force-adding a file neither parent has")
    # The file is present in neither parent, so the default rendering shows no
    # paths for the merge at all -- the precondition the flag exists to fix.
    default_walk = _git(repo, "log", "--all", "--name-only", "--format=")
    assert _PERSONAL_FILE not in default_walk, "precondition: invisible by default"

    assert _PERSONAL_FILE in gate.history_paths(repo)
    result = gate.check(root=repo, scope="history")
    assert any(_PERSONAL_FILE in error for error in result.errors), result.errors


def test_identical_content_does_not_hide_a_path_from_the_history_scope(
    repo: Path,
) -> None:
    """Why the history scope is diff-based rather than object-based.

    `git rev-list --objects --all` is the natural-looking instrument and is
    unsound here: it lists each *object* once, so a personal file whose content
    duplicates a file elsewhere collapses into a single line under whichever
    path git happens to emit. Measured, and pinned here because the unsound
    version passes every other test in this file.
    """
    _on_branch(repo)
    shared = "identical bytes\n"
    _write(repo, "public.md", shared)
    _write(repo, _PERSONAL_FILE, shared)
    _git(repo, "add", "-f", "public.md", _PERSONAL_FILE)
    _git(repo, "commit", "-qm", "two paths, one blob")

    objects = _git(repo, "rev-list", "--objects", "--all")
    blob = _git(repo, "hash-object", "--", "public.md").strip()
    listed = [line for line in objects.splitlines() if line.startswith(blob)]
    assert len(listed) == 1, f"precondition: one object line for both paths: {listed}"

    assert _PERSONAL_FILE in gate.history_paths(repo)
    history = gate.check(root=repo, scope="history")
    assert any(_PERSONAL_FILE in error for error in history.errors), history.errors


def test_a_non_ascii_path_survives_the_scan(repo: Path) -> None:
    """`-z` is load-bearing, not tidiness.

    Without it git *quotes* a non-ASCII path -- the name arrives wrapped in
    literal double quotes with octal escapes -- and a leading `"` defeats a
    prefix test anchored at the start of the path. The quoted form is a false
    negative in exactly the direction that matters.
    """
    _on_branch(repo)
    # Cyrillic, deliberately, and not the obvious `café.md`. macOS normalizes
    # filenames to NFD on write, so an `é` written as the NFC literal U+00E9
    # comes back from git as `e` + U+0301 and every comparison against the
    # literal fails on that leg alone — a red the other two legs cannot show.
    # These three characters have no canonical decomposition, so NFC and NFD
    # are the same bytes and the test measures quoting rather than
    # normalization. (This repo has already shipped one macOS-only filename
    # failure; the leg is unreachable locally, so the fixture avoids the class
    # instead of betting on it.)
    personal_utf8 = "specs/personal/лаб.md"
    _write(repo, personal_utf8)
    _git(repo, "add", "-f", personal_utf8)
    _git(repo, "commit", "-qm", "a name git would quote")

    base = gate.resolve_merge_base(repo, "origin/main")
    quoted = _git(repo, "log", "--name-only", "--format=", f"{base}..HEAD")
    assert '"specs/personal/' in quoted, "precondition: git quotes it without -z"
    assert not gate.is_personal_path(quoted.strip().splitlines()[0])

    assert personal_utf8 in gate.branch_paths(repo, base)
    assert any("лаб" in error for error in gate.check(root=repo, scope="branch").errors)


# --------------------------------------------------------------------------
# The path vocabulary every match in this module rests on.
# --------------------------------------------------------------------------


def _repo_with_a_subdirectory(repo: Path) -> Path:
    """A repository whose *subdirectory* holds the personal path.

    The nesting is the point: only a containment path that lives below `sub`
    gets two different spellings out of git, which is the inconsistency the
    refusal exists for. A personal file at the top level would be reported
    identically by both commands and prove nothing.
    """
    _on_branch(repo)
    _write(repo, "sub/" + _PERSONAL_FILE, "synthetic\n")
    _git(repo, "add", "-f", "sub/" + _PERSONAL_FILE)
    return repo / "sub"


def test_git_reports_paths_inconsistently_from_below_the_top_level(
    repo: Path,
) -> None:
    """The measurement the refusal rests on, pinned rather than asserted.

    Two commands this module treats as interchangeable sources of "a path"
    disagree once `root` is not the top level, and they disagree in different
    directions — which is why the fix is a refusal rather than a translation.
    The third assertion is the one that costs the invariant: the top-level
    spelling is what `is_personal_path` is given, and it does not match.
    """
    sub = _repo_with_a_subdirectory(repo)

    assert _PERSONAL_FILE + "\n" in _git(sub, "ls-files"), (
        "`ls-files` answers cwd-relative, dropping the `sub/` the file is under"
    )
    assert "sub/" + _PERSONAL_FILE in _git(sub, "status", "--porcelain"), (
        "while the porcelain answers top-level-relative"
    )
    assert not gate.is_personal_path("sub/" + _PERSONAL_FILE), (
        "and that spelling stops matching -- a containment path reading clean"
    )


def test_a_history_walk_from_below_the_top_level_would_not_match(
    repo: Path,
) -> None:
    """The sharp half, and the reason the refusal covers `history` too.

    That scope is CI's backstop and runs no index comparison, so nothing else
    in the scan would have objected: it walks the whole history, matches each
    top-level-relative path against a prefix that cannot match it, and exits
    **0** over a real containment path. Demonstrated on the enumeration itself
    rather than by disabling the guard — `history_paths` is what `check()`
    filters, so a walk whose every personal path fails `is_personal_path` is
    the false negative, stated without having to unbuild the fix.
    """
    sub = _repo_with_a_subdirectory(repo)
    _git(repo, "commit", "-qm", "the leak is now history, not the index")

    walked = gate.history_paths(sub)
    assert "sub/" + _PERSONAL_FILE in walked, "the path is in the walk"
    assert [path for path in walked if gate.is_personal_path(path)] == [], (
        "and not one path in it matches -- the scan would have exited 0"
    )


@pytest.mark.parametrize("scope", ["worktree", "branch", "history"])
def test_a_root_below_the_repository_top_level_is_refused(
    repo: Path, scope: str
) -> None:
    """Every scope, because the one that fails silently is the one CI runs.

    `history` is the sharp case: it runs no index comparison, so nothing else
    in the scan would have objected — it would have walked the whole history,
    matched `sub/specs/personal/labs.md` against a prefix that cannot match it,
    and exited **0** over a real containment path. `REPO_ROOT` is derived from
    `__file__`, so this is reachable by vendoring the checkout inside an outer
    repository, with no user doing anything unusual.
    """
    sub = _repo_with_a_subdirectory(repo)
    _git(repo, "commit", "-qm", "so the history walk has something to walk")

    with pytest.raises(gate.ContainmentError) as excinfo:
        gate.check(root=sub, scope=scope)
    assert "top level" in str(excinfo.value)
    # The ordering, pinned. This guard is the one precondition that must run
    # *before* `tracked_personal`, because unlike every other refusal it
    # invalidates what an earlier scan would have collected rather than leaving
    # it valid -- so the empty payload is the contract, not an oversight. Moving
    # the call after `tracked_personal` left all 99 tests passing until these
    # two lines existed: the fixture force-adds a personal file, so without them
    # nothing distinguishes "refused before collecting" from "collected and
    # threw it away".
    assert excinfo.value.found == [], "nothing was collected in a bad vocabulary"
    assert excinfo.value.examined == {}, "and nothing claims to have been examined"
    # The third sibling, completing the triple. `ContainmentError` gained
    # `found`, `notes` and `examined` together *because* an earlier round found
    # `found` carefully preserved while the other two were dropped on the
    # identical path -- so pinning two of three here reproduced that shape in
    # the assertions. This is not the retracted roster: it is a claim about
    # this one refusal, which the skills still make in prose, and it should
    # break if a reorder ever moves this guard past a note-producing step.
    assert excinfo.value.notes == [], "nor to have anything to say about content"


def test_the_top_level_itself_is_accepted(repo: Path) -> None:
    """The refusal must not fire on the ordinary case, which is every real run.

    Paired with the test above deliberately: a `require_repository_root` that
    raised unconditionally would satisfy all three parametrized cases.
    """
    _on_branch(repo)
    gate.require_repository_root(repo)


# --------------------------------------------------------------------------
# The CLI contract the skills and CI depend on.
# --------------------------------------------------------------------------


def test_the_live_repository_holds_no_containment_path() -> None:
    """The gate's day job, over the halves that hold whatever the index is in.

    Reached through `check()`, which this test stopped doing and the first
    external review caught. Calling the two helpers directly was the file
    header's own named anti-pattern — "a function can be thoroughly pinned and
    reach nothing" — reinstated against the one subject no fixture can stand in
    for, the real repository. The live-index flakiness that motivated the
    narrowing is real, so it is handled rather than avoided:

    * a **"could not run"** (an unresolved merge, most likely) is caught, not
      failed on, and its `found` list is asserted instead. That list is complete
      for this purpose only because the staged-content refusal is **deferred**
      past the enumeration — the same review's other finding — so the walks
      still run and their findings survive it;
    * a **staged-content mismatch** is not a containment finding, so the
      assertion filters on the containment wording rather than on emptiness.

    The `except` branch is an accommodation for a developer's live tree and has
    **no coverage anywhere this test actually runs** — CI checks out clean, so
    it takes the `try` path every time, and a local run only reaches the handler
    mid-conflict. Said plainly rather than left to be discovered, since an
    unexercised handler in a test whose subject is "a function can be pinned and
    reach nothing" is worth naming. What the handler reads is covered
    separately, though **narrowly**:
    `test_notes_and_evidence_survive_a_scan_that_could_not_run` pins that the
    payload merge happens at all, and a mutation removing it kills that test.
    It does not pin the merge's *direction* — reversing all three
    concatenations leaves the suite green, because no raise site in this module
    constructs a `ContainmentError` with a non-empty payload, so the ordering is
    a no-op for every input the code can currently produce. Said plainly because
    this docstring previously claimed the merge was mutation-proven, which read
    as a stronger guarantee than the evidence supports.

    The `examined` assertions are the half that does not pass vacuously. "No
    containment path" is satisfied just as well by a scan that looked at
    nothing, which is hole 2 and the reason `examined` exists at all; requiring
    both source keys is what makes this test fail if `check()` stops reaching
    them. A count is not asserted to be non-zero: a clean working tree
    legitimately reports `worktree 0`.

    Deliberately still **not** the `branch` or `history` scopes against the
    live repository. Those need `origin/main` and a full history, and CI's
    `test` job checks out at `fetch-depth: 1` — only `gitleaks` sets `0`.

    **What that costs is subtler than "it would fail", which is what this
    paragraph used to say.** A `branch`-scope call there does not error; it
    *refuses*, exiting 1 with the shallow-clone message. So a test asserting
    only `== 1` and a shared prefix **passes**, having exercised nothing —
    which is exactly what
    `test_an_empty_base_is_not_silently_swapped_for_the_default` did until it
    was given its own fixture repository. The failure mode to design against
    is a vacuous pass, not a red leg, and the natural "fix" of a skip is worse
    still: it would silently never run, the
    unconfigured-control-passes-vacuously shape this whole module exists to
    stop. Those two scopes are covered against the real repository where the
    history actually exists: the CI step in the `gitleaks` job.
    """
    try:
        result = gate.check(root=gate.REPO_ROOT, scope="worktree")
        errors, examined = result.errors, result.examined
    except gate.ContainmentError as exc:
        errors, examined = exc.found, exc.examined

    assert [error for error in errors if "containment" in error] == []
    assert "tracked-personal" in examined, "the tracked source ran"
    assert "worktree" in examined, "and so did the porcelain source"


def test_a_shallow_clone_is_refused_by_the_history_walking_scopes(
    repo: Path, tmp_path: Path
) -> None:
    """A truncated history must fail loudly, not report clean over what it
    cannot see.

    This is hole 2 reproduced *inside this gate*, which is why it is pinned.
    Measured on a `--depth 1` clone of the real repository before the guard
    existed: `--scope history` exited **0** announcing "620 paths examined"
    while holding 4 commits of roughly 80. Nothing in that output looks wrong,
    which is precisely the problem.
    """
    _on_branch(repo)
    _write(repo, "notes.md")
    _git(repo, "add", "notes.md")
    _git(repo, "commit", "-qm", "give the clone something to truncate")

    shallow = tmp_path / "shallow"
    _git(tmp_path, "clone", "-q", "--depth", "1", repo.as_uri(), str(shallow))
    assert gate.is_shallow(shallow), "precondition: the clone really is shallow"

    for scope in ("branch", "history"):
        with pytest.raises(gate.ContainmentError) as excinfo:
            gate.check(root=shallow, scope=scope)
        assert "shallow" in str(excinfo.value), scope


def test_a_shallow_clone_still_permits_the_worktree_scope(
    repo: Path, tmp_path: Path
) -> None:
    """`/savepoint` must keep working for anyone in a shallow checkout.

    The worktree scope walks no history, so truncation costs it nothing and
    refusing there would break the cheap per-checkpoint gate for no gain.
    """
    shallow = tmp_path / "shallow"
    _git(tmp_path, "clone", "-q", "--depth", "1", repo.as_uri(), str(shallow))
    assert gate.is_shallow(shallow)

    assert gate.check(root=shallow, scope="worktree").errors == []


def test_a_full_clone_is_not_reported_as_shallow(repo: Path) -> None:
    """The guard must not fire on the ordinary case, or it fires on everything."""
    assert gate.is_shallow(repo) is False
    gate.require_full_history(repo, "history")  # must not raise


def test_an_unknown_scope_is_refused() -> None:
    with pytest.raises(gate.ContainmentError):
        gate.check(scope="everything")


def test_main_exits_1_and_says_the_scan_never_ran(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A precondition failure and a violation both exit 1, and must read
    differently: one means fix the tree, the other means fix the setup."""
    monkeypatch.setattr(gate, "REPO_ROOT", repo)
    assert gate.main(["--scope", "branch", "--base", "origin/nope"]) == 1
    assert "could not run" in capsys.readouterr().out


def test_main_exits_1_and_names_the_offending_path(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(gate, "REPO_ROOT", repo)
    _on_branch(repo)
    _write(repo, _PERSONAL_FILE)
    _git(repo, "add", "-f", _PERSONAL_FILE)
    _git(repo, "commit", "-qm", "force-add")

    assert gate.main(["--scope", "branch"]) == 1
    out = capsys.readouterr().out
    assert "containment violated" in out
    assert _PERSONAL_FILE in out


def test_main_exits_0_and_states_its_evidence(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A clean run prints the per-source counts, so "clean" is never a claim
    the operator has to take on trust."""
    monkeypatch.setattr(gate, "REPO_ROOT", repo)
    _on_branch(repo)
    _write(repo, "notes.md")
    _git(repo, "add", "notes.md")
    _git(repo, "commit", "-qm", "work")

    assert gate.main(["--scope", "branch"]) == 0
    out = capsys.readouterr().out
    assert "containment holds" in out
    assert "branch-history" in out
    assert "git log --diff-merges=first-parent -p" in out


def test_the_default_scope_is_the_branch() -> None:
    """`/land` is the caller that must not be able to under-scope by omission."""
    assert gate.build_parser().parse_args([]).scope == "branch"


@pytest.mark.parametrize("scope", ["worktree", "history"])
def test_a_base_supplied_to_a_scope_that_ignores_it_is_refused(
    scope: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """Only the branch scope consults `--base`, so the rest must refuse it.

    Measured: `--scope history --base origin/does-not-exist-at-all` parsed
    cleanly, walked every ref, and exited **0** without ever resolving the ref
    — an operator or CI step asking for a bounded walk silently got an
    unbounded one. This module's own stated principle is that a precondition it
    cannot honour fails loudly rather than passes; the CLI was the one place it
    did not apply that to itself.
    """
    assert gate.main(["--scope", scope, "--base", "origin/main"]) == 1
    assert "consulted only by the branch scope" in capsys.readouterr().out


def test_an_omitted_base_still_defaults_for_the_branch_scope() -> None:
    """`default=None` is how "supplied" becomes distinguishable from
    "defaulted" — the default itself must survive that change."""
    assert gate.build_parser().parse_args([]).base is None
    assert gate.DEFAULT_BASE_REF == "origin/main"


@pytest.mark.parametrize("scope", ["worktree", "history"])
def test_the_base_scope_precondition_binds_importers_not_only_the_cli(
    scope: str,
) -> None:
    """The invariant has to live in `check()`, where every caller meets it.

    It shipped in `main()` alone, so `gate.check(scope="history",
    base_ref="origin/release")` parsed, ran the unbounded `--all` walk, and
    returned a clean `ScanResult` without ever touching the ref — the exact
    silent-ignore the CLI guard was added to end, reproduced with a green
    suite by anything that imports this module rather than shelling out to it.
    A guard in the CLI layer protects only CLI callers.
    """
    with pytest.raises(gate.ContainmentError) as excinfo:
        gate.check(scope=scope, base_ref="origin/main")
    assert "consulted only by the branch scope" in str(excinfo.value)


def test_an_empty_base_is_not_silently_swapped_for_the_default(
    repo: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """`--base ""` must fail loudly rather than become `origin/main`.

    `args.base or DEFAULT_BASE_REF` collapsed the `None`/empty distinction that
    `default=None` had just been introduced to create, so an unset shell
    variable interpolated into the command — hole 2's exact shape — reported
    clean against a base the operator never named. The same empty string was
    *refused* under the other scopes, so the CLI treated it two opposite ways.

    Measured: `git merge-base "" HEAD` exits 128, so passing it through is what
    produces the loud failure.

    **Two things here are the fix for a vacuity, not style.** This ran against
    the *live* repository and asserted only the shared `"could not run"` prefix,
    which CI's `test` job satisfies without ever reaching `resolve_merge_base`:
    that job checks out at `fetch-depth: 1` (only `gitleaks` sets `0`), so
    `require_full_history` refuses first, exits 1, and prints a message
    containing that same prefix. Restoring the `args.base or DEFAULT_BASE_REF`
    mutant left the test green on all three matrix legs — a test named for
    hole 2 defeated by hole 2's own shape. It now owns a full fixture
    repository so no checkout depth can pre-empt it, and asserts on wording
    only the *base* failure produces.
    """
    _on_branch(repo)
    _write(repo, "f.txt", "content\n")
    _git(repo, "add", "f.txt")
    _git(repo, "commit", "-qm", "so there is a history to have a base in")
    monkeypatch.setattr(gate, "REPO_ROOT", repo)
    assert not gate.is_shallow(repo), (
        "precondition: a full clone, so the shallow guard cannot answer instead"
    )

    assert gate.main(["--scope", "branch", "--base", ""]) == 1
    out = capsys.readouterr().out
    assert "could not run" in out
    assert "merge base" in out, "the base failure, not whichever refusal fires first"


def test_notes_survive_a_run_that_ends_in_a_violation(
    repo: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A note is no less true because something else on the branch failed.

    `main()` returned at the violation branch without ever reaching the note
    loop, so an operator whose tree held both a containment violation and an
    ordinary staged-then-edited file was told about the noted path only once
    the violation was fixed — losing the content half's one instruction for it
    exactly on the run where the tree was in trouble.
    """
    _on_branch(repo)
    _write(repo, "api.md", "staged\n")
    _git(repo, "add", "api.md")
    _write(repo, "api.md", "then edited\n")
    _write(repo, _PERSONAL_FILE, "synthetic\n")
    _git(repo, "add", "-f", _PERSONAL_FILE)
    monkeypatch.setattr(gate, "REPO_ROOT", repo)

    assert gate.main(["--scope", "worktree"]) == 1
    out = capsys.readouterr().out
    assert "containment violated" in out
    assert "Note:" in out
    assert "api.md" in out


def test_notes_and_evidence_survive_a_scan_that_could_not_run(
    repo: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """`found` was carried through the refusal path and its two siblings were
    not — the same asymmetry one field over.

    Here the branch scope cannot resolve its base, so the scan refuses; the
    note and the per-source counts it had already gathered are still true, and
    the counts are what tell the operator how much of the scan did run.
    """
    _on_branch(repo)
    _write(repo, "api.md", "staged\n")
    _git(repo, "add", "api.md")
    _write(repo, "api.md", "then edited\n")
    monkeypatch.setattr(gate, "REPO_ROOT", repo)

    assert gate.main(["--scope", "branch", "--base", "origin/nope"]) == 1
    out = capsys.readouterr().out
    assert "could not run" in out
    assert "Note:" in out
    assert "api.md" in out
    assert "Examined before stopping:" in out
    assert "worktree" in out


def test_the_staged_content_count_says_what_it_counted(repo: Path) -> None:
    """It is the one `examined` source that is not a containment test.

    The other four enumerate paths and ask `is_personal_path` of each; this one
    counts staged paths whose *bytes* were verified, over a set that overlaps
    `worktree`. `/land` step 3a described all of them alike as deduplicated
    distinct paths, and the success sentence folded this count into "none of
    the paths ... is under specs/personal/" — a test it never ran. The
    annotation is carried in the evidence line itself because that is where it
    was misread.
    """
    _on_branch(repo)
    _write(repo, "panel.md", "staged\n")
    _git(repo, "add", "panel.md")

    evidence = gate.check(root=repo, scope="worktree").evidence()
    assert "staged-content 1 (bytes verified)" in evidence
    assert "worktree 1," in evidence, "the enumeration sources stay unannotated"


def test_gpg_verification_lines_cannot_be_glued_onto_a_path(repo: Path) -> None:
    """`log.showSignature` is a user config that corrupts `git log`'s stdout.

    With it set, git interleaves gpg's verification lines into the same stream
    as the paths, and those lines carry no `-z` delimiter of their own — so the
    noise arrives *glued to the front of the next path* and
    `is_personal_path` answers False for a path that is under the containment
    directory. A false negative, produced by a setting the scanned repository
    chose, which is why `_git` pins it off rather than trusting the environment.
    """
    _on_branch(repo)
    _write(repo, _PERSONAL_FILE)
    _git(repo, "add", "-f", _PERSONAL_FILE)
    _git(repo, "commit", "-qm", "force-add")
    # A hand-built commit object carrying a bogus signature: making a real one
    # would need a gpg key on the machine, and what is under test is git's
    # output framing rather than any property of the signature itself.
    tree = _git(repo, "rev-parse", "HEAD^{tree}").strip()
    parent = _git(repo, "rev-parse", "HEAD~1").strip()
    raw = (
        f"tree {tree}\nparent {parent}\n"
        "author T <t@e.invalid> 1700000000 +0000\n"
        "committer T <t@e.invalid> 1700000000 +0000\n"
        "gpgsig -----BEGIN PGP SIGNATURE-----\n \n bm90LXJlYWw=\n"
        " -----END PGP SIGNATURE-----\n\nsigned\n"
    )
    # Bytes rather than `text=True`: subprocess wraps stdin in a TextIOWrapper
    # whose default newline translation rewrites every `\n` to `\r\n` on
    # Windows, and a commit object with CRLF headers is rejected outright
    # (exit 128, measured). The object's framing is exactly what is under test.
    written = subprocess.run(  # noqa: S603 - fixed executable, no shell
        [_GIT_EXE, "-C", str(repo), "hash-object", "-w", "-t", "commit", "--stdin"],
        input=raw.encode("utf-8"),
        capture_output=True,
        env={**os.environ, **_NEUTRAL_GIT_ENV},
        check=True,
    )
    signed = written.stdout.decode("utf-8").strip()
    _git(repo, "update-ref", "refs/heads/work", signed)
    _git(repo, "config", "log.showSignature", "true")

    base = gate.resolve_merge_base(repo, "origin/main")
    assert _PERSONAL_FILE in gate.branch_paths(repo, base), (
        "the path must arrive intact, not welded to gpg's output"
    )
    errors = gate.check(root=repo, scope="branch").errors
    assert any(_PERSONAL_FILE in error for error in errors), errors
