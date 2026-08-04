"""The reviewer-isolation launcher (scripts/review_worktree.py, ADR-0068).

Every case here is a failure mode the bash-prose predecessor shipped or a
fidelity guarantee the reviewers' verdicts silently depend on: the clean-tree
guard actually stopping, snapshot fidelity for tracked and untracked content,
identical bytes across both worktrees, the specs/personal/ tracking guard,
abort (never HEAD-fallback) on an unmergeable index, cwd independence,
teardown leaving no registrations behind, the containment guards around
everything teardown deletes, and the divergence abort plus machine-local
warnings that mark what the snapshot cannot carry.

**Run this file with `-n auto` when iterating on it.** Measured 2026-08-02:
230.57s serial against 50.33s parallel (4.6x), averaging ~1.7s per test —
which is process-creation cost, not I/O: most cases drive real git through
the module under test, at ~15-20 spawns each and ~150-250ms per spawn on
Windows. `specs/testing-strategy.md` leaves `-n auto` out of `addopts`
because spinning up workers costs more than it saves for a focused run; that
reasoning does not hold for this file and the doc does not caveat it, so the
caveat lives here, beside the cost. Only ~10 of these tests route through the
`git_double` seam that intercepts `_git` without spawning; the ~30 that
assert nothing but `_setup(scratch) == 1` (an abort before materialization)
could, and that is the available reduction if this file's wall-clock ever
needs to come down.
"""

from __future__ import annotations

import ast
import io
import json
import os
import shutil
import stat
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import pytest
import review_worktree
from review_worktree import STATE_FILENAME, main

_GIT = shutil.which("git") or "git"


def _inert_worktrees(scratch: Path) -> dict[str, str]:
    """A `worktrees` map for a teardown test whose subject is the venvs.

    `{}` is not a legal state file: a real setup writes one entry per agent, so
    an empty map means the file was truncated or hand-edited — and teardown
    accepting it removed nothing, printed success, and unlinked the only record
    of whatever it used to name. Tests focused on the venv half still need the
    key populated, and this is the harmless population: a worktree path under
    the scratch dir that was never created, which teardown tolerates (already
    gone, never registered) without acting on anything the caller cares about.

    Shared rather than spelled inline, because eight tests need it and eight
    literals is how the next change to the state-file contract ends up
    half-applied.
    """
    return {"spec-reviewer": str(scratch / "wt-never-created-abc123-def456")}


# The two categories of recorded path teardown deletes. They share one
# containment preamble (`_contained`) and one deleter (`_force_delete_tree`),
# so every containment property below is asserted against both — parametrized
# rather than written twice, because the twice-written version is exactly how
# a containment fix reached the venv deleter and not the worktree deleter while
# both still read as complete, and a `..` in a recorded path then force-deleted
# the directory holding the repository.
_RECORDED_KEYS = ("worktrees", "venvs")


def _state_recording(
    repo: Path, scratch: Path, key: str, path: Path
) -> dict[str, object]:
    """A state file recording `path` under `key`, the other category inert."""
    state: dict[str, object] = {
        "root": str(repo),
        "worktrees": _inert_worktrees(scratch),
        "venvs": {},
    }
    state[key] = {"x": str(path)}
    return state


def _write_state(scratch: Path, state: dict[str, object]) -> None:
    (scratch / STATE_FILENAME).write_text(json.dumps(state), encoding="utf-8")


# The machine's global and system git config, neutralized. Several assertions
# below compare EXACT untracked-file counts, and `--exclude-standard` honors
# `core.excludesFile` — so a developer whose `~/.config/git/ignore` holds
# `*.tmp` or `*.txt` gets a red suite here for a reason that has nothing to do
# with the code: `vanishes.tmp` never enters the manifest, so the warning it
# exists to prove never prints. `core.hooksPath` is the same class, running
# foreign hooks on the fixture's own commits. Green on this host and on the
# GitHub runners only because none of them sets either (verified). It is
# `os.devnull` rather than a missing path because git reads these as files.
#
# Applied in two places because one is not enough: as an explicit `env` here,
# which covers the session-scoped `repo_template` build, and as an autouse
# fixture below, which covers the spawns the module under test makes itself.
_NEUTRAL_GIT_ENV = {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull}


@pytest.fixture(autouse=True)
def _neutral_git_config(  # pyright: ignore[reportUnusedFunction] - autouse, never referenced by name
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Neutralize machine git config for the spawns `review_worktree` makes.

    Function-scoped rather than session-scoped deliberately: a session-scoped
    autouse fixture is torn down at session end, so under a shared worker it
    would leave these set for every *other* module's tests too. See
    `_NEUTRAL_GIT_ENV`.
    """
    for key, value in _NEUTRAL_GIT_ENV.items():
        monkeypatch.setenv(key, value)


def _git_raw(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - fixed executable, no shell
        [_GIT, "-C", str(repo), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env={**os.environ, **_NEUTRAL_GIT_ENV},
        # Both of these are load-bearing in the module under test and for the
        # same reasons here: a non-UTF-8 path in git's output must surface as
        # a readable assertion failure rather than a UnicodeDecodeError no
        # assertion owns, and a hung git child must fail this test rather than
        # hang the suite.
        errors="replace",
        timeout=120,
    )


def _git(repo: Path, *args: str) -> str:
    proc = _git_raw(repo, *args)
    assert proc.returncode == 0, f"git {args} failed: {proc.stdout}{proc.stderr}"
    return proc.stdout


def _subcommand(args: tuple[str, ...]) -> tuple[str, ...]:
    """The git subcommand in `args`, past any leading `-c key=value` pair.

    The doubles below match on the subcommand, and `_git` calls legitimately
    carry global options ahead of it (`-c core.longpaths=true` on worktree
    removal). Matching `args[:2]` directly is what lets a double silently stop
    engaging the moment a global option is added — and a double that no longer
    engages hands the test to real git, where a "the fallback still works"
    assertion passes for entirely the wrong reason.
    """
    rest = list(args)
    while len(rest) >= 2 and rest[0] == "-c":
        del rest[:2]
    return tuple(rest[:2])


def _git_failure(
    args: tuple[str, ...], stderr: str
) -> subprocess.CompletedProcess[str]:
    """The failed-git result every double hands back (exit 128, stderr set)."""
    return subprocess.CompletedProcess(
        args=list(args), returncode=128, stdout="", stderr=stderr
    )


class _GitDouble:
    """Installs a double over `review_worktree._git` and restores it safely.

    Seven tests hand-rolled this, and the cost was not the ~60 lines of
    scaffold — it was that the hazard is spelled out in exactly one of the
    copies. **Never call `monkeypatch.undo()` to restore this seam.** The
    function-scoped `monkeypatch` instance is shared with the `repo` fixture,
    whose last act is `monkeypatch.chdir(root)`, so `undo()` also reverts the
    cwd — pointing the `teardown` that usually follows at pytest's invocation
    directory, which when the suite runs from the project root is the
    developer's live healthspan checkout, on the machine holding the real
    encrypted health database. `restore()` below puts back only what it took.

    `fail_on_nth` owns the counter three copies re-implemented with a private
    `adds = {"count": 0}` dict.
    """

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._monkeypatch = monkeypatch
        self.real = review_worktree._git  # pyright: ignore[reportPrivateUsage] - the test doubles the module's own seam
        self._counts: dict[tuple[str, ...], int] = {}

    def nth(self, args: tuple[str, ...]) -> int:
        """1-based count of calls to this subcommand, this test."""
        key = _subcommand(args)
        self._counts[key] = self._counts.get(key, 0) + 1
        return self._counts[key]

    def install(
        self, handler: Callable[..., subprocess.CompletedProcess[str] | None]
    ) -> None:
        """Route `_git` through `handler`; a None result falls through to git."""

        def _dispatch(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
            result = handler(root, *args)
            return self.real(root, *args) if result is None else result

        self._monkeypatch.setattr(review_worktree, "_git", _dispatch)

    def restore(self) -> None:
        """Put the real `_git` back — see the class docstring on `undo()`."""
        self._monkeypatch.setattr(review_worktree, "_git", self.real)


@pytest.fixture
def git_double(monkeypatch: pytest.MonkeyPatch) -> _GitDouble:
    return _GitDouble(monkeypatch)


class _SpawnedDouble:
    """A `subprocess.Popen` stand-in for the module's single spawn site.

    `_spawn` needs both halves of a process — readable byte pipes and a
    `wait` that can raise — so a double that only answers one of them makes
    the reader threads block on a stream nothing ever closes. Both pipes are
    handed over already at EOF, so `_settle` returns at once and no test pays
    `_KILL_GRACE`.
    """

    def __init__(
        self, argv: list[str], returncode: int, stderr: bytes, hang: bool
    ) -> None:
        self.args = argv
        # Never a real pid: see `_neutralize_kill_tree` for why that matters.
        self.pid = -1
        self.stdout = io.BytesIO()
        self.stderr = io.BytesIO(stderr)
        self.returncode = returncode
        self._hang = hang

    def wait(self, timeout: float | None = None) -> int:
        if self._hang:
            raise subprocess.TimeoutExpired(self.args, timeout or 0)
        return self.returncode

    def kill(self) -> None:
        """Reached only if `_kill_tree` falls back from a failed job open."""


def _neutralize_kill_tree(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Stop `_kill_tree` before it signals, and record the pids it was given.

    Mandatory for every `_SpawnedDouble` test, not hygiene. `_kill_tree`'s
    POSIX branch calls `os.killpg`, which signals a process **group** — hand
    it the double's fake pid and it signals whichever group owns that number,
    up to and including the one running this suite. The returned list is also
    the assertion surface for "the tree kill was attempted".
    """
    killed: list[int] = []

    def _record(proc: Any, job: object) -> None:
        killed.append(cast("int", proc.pid))

    monkeypatch.setattr(review_worktree, "_kill_tree", _record)
    return killed


def _spawn_double(
    monkeypatch: pytest.MonkeyPatch,
    *,
    match: str,
    returncode: int = 0,
    stderr: bytes = b"",
    hang: bool = False,
) -> tuple[list[list[str]], list[int]]:
    """Route git spawns whose argv contains `match` through `_SpawnedDouble`.

    Installed at `subprocess.Popen` rather than at `_spawn` or `_git_argv`: a
    seam inside the module only sees calls that already went through it, so a
    spawn that bypasses the builder — a hardcoded `"git"` somewhere — stays
    invisible. Returns (every argv observed, pids `_kill_tree` was handed).
    """
    real_popen: Callable[..., Any] = subprocess.Popen
    seen: list[list[str]] = []
    killed = _neutralize_kill_tree(monkeypatch)

    def _dispatch(*args: Any, **kwargs: Any) -> Any:
        argv = args[0] if args else kwargs.get("args")
        tokens = [str(part) for part in cast("list[Any]", argv)]
        seen.append(tokens)
        if match in tokens:
            return _SpawnedDouble(tokens, returncode, stderr, hang)
        return real_popen(*args, **kwargs)

    monkeypatch.setattr(review_worktree.subprocess, "Popen", _dispatch)
    return seen, killed


def _link_dir(link: Path, target: Path) -> None:
    """Create a directory link at `link` pointing at `target`, on any platform.

    A *junction* on Windows and a directory symlink elsewhere, which is what
    makes the containment tests below run on all three CI legs instead of
    skipping on the one that matters most.

    `Path.symlink_to` is not usable on Windows here: it raises `WinError 1314,
    A required privilege is not held` for an unprivileged process without
    Developer Mode, which is why every symlink test in this file skips on
    `windows-latest` and on the developer's own machine. `mklink /J` needs no
    privilege at all — so the junction is both the link form a Windows user
    can actually create by accident and the one a Windows runner can test.
    That asymmetry is the whole reason the guards these tests cover were
    written against symlinks and left open on junctions.
    """
    if os.name == "nt":
        # `mklink` is a cmd builtin, so it has to be invoked through the
        # interpreter — named by COMSPEC rather than a bare "cmd" so the
        # lookup is not PATH-dependent. Both paths are test-local and built
        # from `tmp_path`; no user input reaches this.
        subprocess.run(  # noqa: S603 - fixed argv, test-local paths, no shell
            [
                os.environ.get("COMSPEC", "cmd"),
                "/c",
                "mklink",
                "/J",
                str(link),
                str(target),
            ],
            capture_output=True,
            check=True,
            timeout=120,
        )
    else:
        link.symlink_to(target, target_is_directory=True)


@pytest.fixture(scope="session")
def repo_template(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The repo every test copies, built once per session.

    `core.autocrlf false` is load-bearing, not hygiene: several assertions
    below compare live-tree bytes against worktree bytes, and EOL
    normalization would make them differ legitimately.

    Built once because the per-test alternative is this fixture's eight git
    spawns, measured at 1.12 s per test on Windows against 18 ms for the copy
    — about 55 s of every run of this file before a single assertion, tripled
    across the 3-OS CI matrix.

    "Once" means once per **worker**, not once per run: xdist gives each
    worker its own process and therefore its own session-scoped fixture, so
    the documented local invocation (`pytest -n auto`) pays this build N
    times over. Still far cheaper than per-test, and sharing one build across
    workers needs the `tmp_path_factory.getbasetemp().parent` lockfile recipe
    — worth knowing before anyone re-derives the saving as a flat 55 s.
    """
    root = tmp_path_factory.mktemp("repo-template") / "repo"
    root.mkdir()
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.email", "test@example.invalid")
    _git(root, "config", "user.name", "Test")
    _git(root, "config", "commit.gpgsign", "false")
    _git(root, "config", "core.autocrlf", "false")
    (root / "tracked.txt").write_text("original\n", encoding="utf-8")
    _git(root, "add", "tracked.txt")
    _git(root, "commit", "-m", "base")
    _git(root, "branch", "base")
    return root


@pytest.fixture
def repo(repo_template: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A tmp git repo (one base commit, a `base` branch ref) with cwd inside it.

    A private copy, not a shared handle: tests here commit, branch, conflict a
    merge, set skip-worktree and register worktrees, all of which must stay
    isolated. A freshly `git init`-ed repo records no absolute paths, so a
    plain directory copy relocates intact — including the `.git/worktrees/`
    registrations each test then creates inside its own copy.
    """
    root = tmp_path / "repo"
    shutil.copytree(repo_template, root)
    monkeypatch.chdir(root)
    return root


@pytest.fixture
def scratch(tmp_path: Path) -> Path:
    return tmp_path / "scratch"


def _setup(scratch: Path) -> int:
    return main(["setup", "--scratch", str(scratch), "--base", "base"])


def _files_holding(where: Path, sentinel: str) -> list[Path]:
    """Every file under `where` whose text contains `sentinel`.

    Containment assertions read *content* rather than filenames because the
    leak routes differ in what they preserve: a link keeps the name, `copy2`
    keeps the bytes under whatever name the manifest carried, and a hard link
    keeps both while sharing an inode. One predicate over the bytes covers all
    three, and unlike an `rglob("labs.md")` it cannot pass merely because the
    leak arrived under a name the test did not predict.
    """
    if not where.exists():
        return []
    return [
        path
        for path in where.rglob("*")
        if path.is_file()
        and sentinel in path.read_text(encoding="utf-8", errors="replace")
    ]


def _state(scratch: Path) -> dict[str, object]:
    raw: dict[str, object] = json.loads(
        (scratch / STATE_FILENAME).read_text(encoding="utf-8")
    )
    return raw


def _worktrees(scratch: Path) -> dict[str, str]:
    worktrees = _state(scratch)["worktrees"]
    assert isinstance(worktrees, dict)
    return cast("dict[str, str]", worktrees)


def _warnings(scratch: Path) -> list[str]:
    warnings = _state(scratch)["warnings"]
    assert isinstance(warnings, list)
    return cast("list[str]", warnings)


def test_clean_tree_exits_2_and_materializes_nothing(repo: Path, scratch: Path) -> None:
    assert _setup(scratch) == 2
    assert not (scratch / STATE_FILENAME).exists()
    # "wt-", not "worktree": the word "worktree" never appears in plain
    # `git worktree list` output, so asserting on it proves nothing.
    assert "wt-" not in _git(repo, "worktree", "list")


def test_branch_diff_alone_is_enough_to_review(repo: Path, scratch: Path) -> None:
    """A clean tree with a non-empty `base...HEAD` is reviewable, and the
    worktrees must actually materialize **at HEAD**, carrying that diff.

    The exit code alone is not the property. A worktree silently created at
    any other ref would contain none of the work under review and every
    reviewer would report clean — the exact false-pass class this whole
    mechanism exists to prevent, and one an exit-code-only assertion cannot
    see. This is also the committed-work path, so it carries the same
    content-fidelity assertion the uncommitted path already has.

    The commit goes on a **feature branch**, which is what makes the claim
    general: with the work committed on `main`, `main` and `HEAD` name the
    same commit, so materializing at the literal ref `"main"` would be
    indistinguishable from materializing at `HEAD` and this test would pin
    only "not at `--base`". On a feature branch, `base`, `main`, and every
    other pre-existing ref are the parent commit, so any ref but `HEAD`
    fails the assertions below.
    """
    _git(repo, "checkout", "-b", "feature")
    (repo / "tracked.txt").write_text("committed change\n", encoding="utf-8")
    _git(repo, "commit", "-am", "ahead of base")
    assert _setup(scratch) == 0  # tree clean, but base...HEAD is non-empty
    assert _state(scratch)["snapshot"] is None
    head = _git(repo, "rev-parse", "HEAD").strip()
    for path in _worktrees(scratch).values():
        assert (Path(path) / "tracked.txt").read_text(
            encoding="utf-8"
        ) == "committed change\n"
        assert _git(Path(path), "rev-parse", "HEAD").strip() == head


def test_worktrees_carry_the_uncommitted_tracked_change(
    repo: Path, scratch: Path
) -> None:
    (repo / "tracked.txt").write_text("uncommitted edit\n", encoding="utf-8")
    assert _setup(scratch) == 0
    state = _state(scratch)
    assert state["snapshot"] is not None
    for path in _worktrees(scratch).values():
        content = (Path(path) / "tracked.txt").read_text(encoding="utf-8")
        assert content == "uncommitted edit\n"


def test_untracked_files_replicate_identically_into_both_worktrees(
    repo: Path, scratch: Path
) -> None:
    nested = repo / "sub dir" / "new file.txt"
    nested.parent.mkdir()
    nested.write_text("untracked content\n", encoding="utf-8")
    assert _setup(scratch) == 0
    copies = [
        (Path(path) / "sub dir" / "new file.txt").read_bytes()
        for path in _worktrees(scratch).values()
    ]
    assert len(copies) == 2
    # The guarantee is byte-fidelity to the live file, whatever bytes it holds
    # (on Windows the text-mode fixture write itself produced CRLF).
    assert copies[0] == copies[1] == nested.read_bytes()
    assert _state(scratch)["untracked"] == ["sub dir/new file.txt"]


def test_tracked_personal_file_aborts_setup(repo: Path, scratch: Path) -> None:
    personal = repo / "specs" / "personal" / "notes.md"
    personal.parent.mkdir(parents=True)
    personal.write_text("x\n", encoding="utf-8")
    _git(repo, "add", "-f", str(personal))
    assert _setup(scratch) == 1
    assert not (scratch / STATE_FILENAME).exists()


def test_unmergeable_index_aborts_rather_than_falling_back_to_head(
    repo: Path, scratch: Path
) -> None:
    _git(repo, "checkout", "-b", "left")
    (repo / "tracked.txt").write_text("left\n", encoding="utf-8")
    _git(repo, "commit", "-am", "left")
    _git(repo, "checkout", "main")
    (repo / "tracked.txt").write_text("right\n", encoding="utf-8")
    _git(repo, "commit", "-am", "right")
    merge = _git_raw(repo, "merge", "left")
    assert merge.returncode != 0  # conflicted, unmerged paths in the index
    assert _setup(scratch) == 1
    assert not (scratch / STATE_FILENAME).exists()


def test_intent_to_add_entry_aborts_rather_than_reading_as_clean(
    repo: Path, scratch: Path
) -> None:
    """`git add -N` makes `git stash create` fail with rc 1 and EMPTY stdout.

    This is the scenario the abort-guard exists for: a `${SNAP:-HEAD}`-style
    fallback reads the empty output as "clean tree" and silently reviews the
    wrong state (here it would exit 2, discarding the new file entirely).
    The launcher must abort instead. Distinct from the merge-conflict case,
    where stash's garbage stdout makes a later git call fail incidentally —
    this case fails ONLY if the stash exit status itself is checked.
    """
    (repo / "intended.txt").write_text("new content\n", encoding="utf-8")
    _git(repo, "add", "-N", "intended.txt")
    stash = _git_raw(repo, "stash", "create", "probe")
    assert stash.returncode != 0  # the trap, part 1: it fails...
    assert stash.stdout.strip() == ""  # ...part 2: with nothing on stdout
    assert _setup(scratch) == 1
    assert not (scratch / STATE_FILENAME).exists()


def test_cwd_in_a_subdirectory_changes_nothing(
    repo: Path, scratch: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (repo / "top-untracked.txt").write_text("top\n", encoding="utf-8")
    sub = repo / "sub"
    sub.mkdir()
    (sub / "deep-untracked.txt").write_text("deep\n", encoding="utf-8")
    monkeypatch.chdir(sub)
    assert _setup(scratch) == 0
    assert _state(scratch)["untracked"] == [
        "sub/deep-untracked.txt",
        "top-untracked.txt",
    ]
    for path in _worktrees(scratch).values():
        assert (Path(path) / "top-untracked.txt").exists()
        assert (Path(path) / "sub" / "deep-untracked.txt").exists()


def test_staged_index_divergence_aborts_with_a_remedy(
    repo: Path, scratch: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The snapshot reviews worktree bytes; a plain `git commit` (no -a)
    publishes the index. When the two differ, a pass could be earned for
    bytes that are not the ones that land — and every call site treats
    warnings as advisory, so this must abort, not warn."""
    (repo / "tracked.txt").write_text("staged version\n", encoding="utf-8")
    _git(repo, "add", "tracked.txt")
    (repo / "tracked.txt").write_text("worktree version\n", encoding="utf-8")
    assert _setup(scratch) == 1
    err = capsys.readouterr().err
    assert "staged index differs" in err
    assert "git add" in err  # the remedy, not just the diagnosis
    assert not (scratch / STATE_FILENAME).exists()


def test_a_fully_staged_change_does_not_trip_the_divergence_abort(
    repo: Path, scratch: Path
) -> None:
    """Staging alone is not divergence: index and worktree agree, and the
    snapshot (worktree bytes) is exactly what a plain commit would publish."""
    (repo / "tracked.txt").write_text("staged everywhere\n", encoding="utf-8")
    _git(repo, "add", "tracked.txt")
    assert _setup(scratch) == 0
    for path in _worktrees(scratch).values():
        content = (Path(path) / "tracked.txt").read_text(encoding="utf-8")
        assert content == "staged everywhere\n"
    assert main(["teardown", "--scratch", str(scratch)]) == 0


def test_machine_local_ignore_rules_are_warned_about_and_not_copied(
    repo: Path, scratch: Path
) -> None:
    (repo / ".git" / "info" / "exclude").write_text("hidden.txt\n", encoding="utf-8")
    (repo / "hidden.txt").write_text("machine-local\n", encoding="utf-8")
    (repo / "visible.txt").write_text("visible\n", encoding="utf-8")
    assert _setup(scratch) == 0
    assert _state(scratch)["untracked"] == ["visible.txt"]
    warnings = _warnings(scratch)
    assert any(
        "machine-local ignore rules" in w and "1 untracked" in w for w in warnings
    )
    # The redaction IS the contract: a machine-local rule is where a
    # deliberately-hidden filename would live, so the name must not leak
    # into transcripts or the state file.
    assert not any("hidden.txt" in w for w in warnings)
    for path in _worktrees(scratch).values():
        assert not (Path(path) / "hidden.txt").exists()


def test_scratch_inside_the_repo_is_refused(repo: Path) -> None:
    assert main(["setup", "--scratch", str(repo / "scratch"), "--base", "base"]) == 1


def test_teardown_removes_everything_and_deletes_state(
    repo: Path, scratch: Path
) -> None:
    (repo / "tracked.txt").write_text("edit\n", encoding="utf-8")
    assert _setup(scratch) == 0
    paths = list(_worktrees(scratch).values())
    assert main(["teardown", "--scratch", str(scratch)]) == 0
    # "wt-" rather than the OS-native paths: porcelain prints forward-slash
    # paths, so a backslash-path substring check is vacuously true on Windows
    # — the same trap _registered_paths documents in the script.
    assert "wt-" not in _git(repo, "worktree", "list", "--porcelain")
    for path in paths:
        assert not Path(path).exists()
    assert not (scratch / STATE_FILENAME).exists()


def test_teardown_tolerates_an_already_deleted_worktree_dir(
    repo: Path, scratch: Path
) -> None:
    (repo / "tracked.txt").write_text("edit\n", encoding="utf-8")
    assert _setup(scratch) == 0
    first = next(iter(_worktrees(scratch).values()))
    shutil.rmtree(first)  # simulate out-of-band cleanup of the directory
    assert main(["teardown", "--scratch", str(scratch)]) == 0
    assert "wt-" not in _git(repo, "worktree", "list", "--porcelain")


def test_teardown_without_state_file_is_a_clean_noop(repo: Path, scratch: Path) -> None:
    """Setup's exit-2 and abort paths record nothing, and the procedure says
    to run teardown on every exit path — so this must not raise a false alarm."""
    assert main(["teardown", "--scratch", str(scratch)]) == 0


def test_teardown_aborts_with_guidance_on_a_corrupt_state_file(
    repo: Path, scratch: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A truncated write must not deadlock the pair of subcommands: teardown
    gives a guided abort (not a raw traceback), so the operator can recover."""
    scratch.mkdir(parents=True, exist_ok=True)
    (scratch / STATE_FILENAME).write_text('{"worktrees": {"x', encoding="utf-8")
    assert main(["teardown", "--scratch", str(scratch)]) == 1
    captured = capsys.readouterr()
    assert "malformed" in captured.err
    assert "Traceback" not in captured.err


def test_setup_rollback_keeps_the_state_file_when_a_worktree_survives(
    repo: Path,
    scratch: Path,
    monkeypatch: pytest.MonkeyPatch,
    git_double: _GitDouble,
) -> None:
    """If rollback cannot remove a worktree, the state file is the only
    record of its path — unlinking it anyway would orphan it permanently."""
    (repo / "tracked.txt").write_text("edit\n", encoding="utf-8")
    real_delete = review_worktree._force_delete_tree  # pyright: ignore[reportPrivateUsage] - restored below without undo()

    def _breaking_git(
        root: Path, *args: str
    ) -> subprocess.CompletedProcess[str] | None:
        if _subcommand(args) == ("worktree", "add") and git_double.nth(args) == 2:
            return _git_failure(args, "boom\n")
        if _subcommand(args) == ("worktree", "remove"):  # rollback fails too
            return _git_failure(args, "locked\n")
        return None

    def _undeletable(path: Path, scratch: Path) -> list[str]:
        return ["simulated: directory in use"]

    git_double.install(_breaking_git)
    monkeypatch.setattr(review_worktree, "_force_delete_tree", _undeletable)
    assert _setup(scratch) == 1
    # The record of the surviving worktree must not be erased.
    assert (scratch / STATE_FILENAME).exists()
    # Restore each seam individually — NOT monkeypatch.undo(); see `_GitDouble`.
    git_double.restore()
    monkeypatch.setattr(review_worktree, "_force_delete_tree", real_delete)
    assert main(["teardown", "--scratch", str(scratch)]) == 0
    assert "wt-" not in _git(repo, "worktree", "list", "--porcelain")


def test_setup_leaves_no_extra_copies_in_the_scratch_dir(
    repo: Path, scratch: Path
) -> None:
    """Untracked replication reads live tree → first worktree → the rest;
    there must be no staging copy of the author's in-progress work lingering
    beside the worktrees (the venv dirs appear only once a reviewer builds
    one)."""
    (repo / "brand-new.txt").write_text("untracked\n", encoding="utf-8")
    assert _setup(scratch) == 0
    entries = {p.name for p in scratch.iterdir()}
    expected = {STATE_FILENAME} | {Path(p).name for p in _worktrees(scratch).values()}
    assert entries == expected
    assert main(["teardown", "--scratch", str(scratch)]) == 0


def test_an_oserror_gets_the_one_line_error_contract_not_a_traceback(
    repo: Path,
    scratch: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A replication copy racing an editor's delete raises plain OSError;
    main must still speak the one-line error contract."""
    (repo / "brand-new.txt").write_text("untracked\n", encoding="utf-8")

    def _raising_copy(source: Path, worktree: Path, manifest: list[str]) -> None:
        raise OSError("simulated: file vanished mid-copy")

    monkeypatch.setattr(review_worktree, "_copy_untracked", _raising_copy)
    assert _setup(scratch) == 1
    captured = capsys.readouterr()
    assert "error: simulated: file vanished mid-copy" in captured.err
    assert "Traceback" not in captured.err
    assert not (scratch / STATE_FILENAME).exists()


def test_scratch_containing_the_repo_is_refused(repo: Path, tmp_path: Path) -> None:
    """The symmetric direction of the inside-the-repo guard: with scratch an
    ancestor of the repo, the force-delete containment would cover the repo."""
    assert main(["setup", "--scratch", str(tmp_path), "--base", "base"]) == 1


@pytest.mark.skipif(os.name == "nt", reason="symlink creation (WSL/CI legs run this)")
def test_untracked_symlinks_are_skipped_with_a_warning(
    repo: Path, scratch: Path, tmp_path: Path
) -> None:
    """copy2 follows symlinks — replicating one as a regular file would pull
    the TARGET's content into the worktrees, including content outside the
    repo, which is the containment hole the skip exists to close.

    POSIX-only **by subject, not by omission**: this pins the POSIX
    *mechanism* — git reports a symlinked directory as one entry, which the
    leaf-level `_is_link` filter catches — and Windows reaches the same
    guarantee through a different guard, because git walks through a junction.
    `test_a_linked_directory_in_the_tree_is_never_replicated` runs the
    invariant on both legs via `_link_dir` and branches on the mechanism. Read
    the skip as "the other platform's half lives there", not as a coverage gap
    on the platform that matters most; a review round has now read it the
    second way once."""
    outside = tmp_path / "outside-secret.txt"
    outside.write_text("must never materialize\n", encoding="utf-8")
    (repo / "link-to-outside").symlink_to(outside)
    (repo / "regular.txt").write_text("reviewable\n", encoding="utf-8")
    assert _setup(scratch) == 0
    state = _state(scratch)
    assert state["untracked"] == ["regular.txt"]
    assert any("symlink" in w for w in _warnings(scratch))
    for path in _worktrees(scratch).values():
        assert not (Path(path) / "link-to-outside").exists()


def test_remove_falls_back_to_manual_delete_when_git_refuses(
    repo: Path,
    scratch: Path,
    monkeypatch: pytest.MonkeyPatch,
    git_double: _GitDouble,
) -> None:
    """`-c core.longpaths=true` handles the MAX_PATH case that first forced
    this fallback, so it is now a backstop for whatever else git may refuse.

    The double refuses only the **first** removal of each path, which is the
    real failure's shape: git chokes on a tree it cannot traverse, the
    fallback deletes that tree itself, and the retry then succeeds because
    there is nothing left to traverse. That retry is what de-registers the
    worktree — there is no repo-global prune to fall back on."""
    (repo / "tracked.txt").write_text("edit\n", encoding="utf-8")
    assert _setup(scratch) == 0
    refused: set[Path] = set()

    def _refusing_git(
        root: Path, *args: str
    ) -> subprocess.CompletedProcess[str] | None:
        # Keyed on the RESOLVED path, not on the argument string. The retry
        # deliberately re-spells the path the way git printed it (forward
        # slashes), because with the tree already deleted git has nothing to
        # canonicalize against and string-matches its own registry — so a
        # double keyed on the raw string sees the retry as a first attempt
        # and refuses it too, failing the fallback it exists to exercise.
        target = Path(args[-1]).resolve()
        if _subcommand(args) == ("worktree", "remove") and target not in refused:
            refused.add(target)
            return _git_failure(args, "refused\n")
        return None

    git_double.install(_refusing_git)
    assert main(["teardown", "--scratch", str(scratch)]) == 0
    # Without this, a double that stops matching (a global option added ahead
    # of the subcommand, say) hands teardown to real git, which removes the
    # worktrees cleanly — and every assertion below still passes while the
    # fallback this test exists for never ran once.
    assert refused, "the removal double never engaged; the fallback was not exercised"
    assert not (scratch / STATE_FILENAME).exists()
    assert "wt-" not in _git(repo, "worktree", "list", "--porcelain")


def test_teardown_leaves_an_unrelated_worktree_registered(
    repo: Path, scratch: Path, tmp_path: Path
) -> None:
    """Teardown must never touch a worktree it did not create.

    `git worktree prune` takes no path filter: it de-registers *every*
    worktree whose directory is momentarily missing — an unmounted drive, a
    renamed directory, another harness mid-cleanup. The owner is then left a
    worktree that answers `fatal: not a git repository` until
    `git worktree repair` runs, with their uncommitted work stranded outside
    git's view. That is a repo-global write on someone else's work, which
    `.claude/reviewer-isolation.md` invariant 2 does not sanction.
    """
    mine = tmp_path / "my-feature"
    _git(repo, "worktree", "add", "--detach", str(mine), "HEAD")
    (mine / "UNCOMMITTED.txt").write_text("hours of work\n", encoding="utf-8")
    moved = tmp_path / "my-feature-elsewhere"
    mine.rename(moved)  # the drive is unmounted / the directory is renamed

    (repo / "tracked.txt").write_text("edit\n", encoding="utf-8")
    assert _setup(scratch) == 0
    assert main(["teardown", "--scratch", str(scratch)]) == 0

    moved.rename(mine)  # it comes back
    listing = _git(repo, "worktree", "list", "--porcelain")
    assert "wt-" not in listing  # ours are gone
    assert "my-feature" in listing  # theirs is not
    assert _git_raw(mine, "status", "--porcelain").returncode == 0


def test_teardown_exits_1_when_a_worktree_stays_registered(
    repo: Path, scratch: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The fail-loud stray check must fire, not shrug (ADR-0068 Consequences).

    Simulates removal silently not happening (worst case: a removal path that
    reports success while the registration survives) by no-opping
    `_remove_worktree`; prune keeps a live on-disk worktree registered, so
    the stray verification is the only thing left to catch it.
    """
    (repo / "tracked.txt").write_text("edit\n", encoding="utf-8")
    assert _setup(scratch) == 0

    def _noop_remove(root: Path, path: str, scratch: Path) -> None:
        return None

    monkeypatch.setattr(review_worktree, "_remove_worktree", _noop_remove)
    assert main(["teardown", "--scratch", str(scratch)]) == 1
    # State file must survive a failed teardown so a retry can still find it.
    assert (scratch / STATE_FILENAME).exists()


def test_skip_worktree_entries_are_warned_about(repo: Path, scratch: Path) -> None:
    _git(repo, "update-index", "--skip-worktree", "tracked.txt")
    (repo / "other.txt").write_text("something to review\n", encoding="utf-8")
    assert _setup(scratch) == 0
    assert any("skip-worktree" in w for w in _warnings(scratch))


def test_assume_unchanged_entries_are_warned_about_too(
    repo: Path, scratch: Path
) -> None:
    """The other half of `nonstandard_index_flags`, and the untested one.

    The predicate is `tag == "S" or (tag.isalpha() and tag.islower())` — `S`
    for skip-worktree, a lowercase tag for assume-unchanged. Only the first
    had a test, so deleting the second clause left the whole suite green
    (measured: 91 passed) while the warning silently stopped firing for
    `--assume-unchanged`, which is the more commonly reached of the two flags
    the warning names.

    It matters for the same reason its twin does: modifications to a flagged
    file are invisible to `git stash create`, so the snapshot silently lacks
    them and the reviewers pass over a tree that is not the one on disk.
    """
    _git(repo, "update-index", "--assume-unchanged", "tracked.txt")
    (repo / "other.txt").write_text("something to review\n", encoding="utf-8")
    assert _setup(scratch) == 0
    warnings = _warnings(scratch)
    assert any("assume-unchanged" in w for w in warnings), warnings
    assert any("1 skip-worktree/assume-unchanged" in w for w in warnings), warnings
    # The tag letter, not the path, is what the predicate reads — so pin which
    # entry was flagged at the predicate, and pin at the message that the name
    # does NOT travel. A tracked, skip-worktree-flagged file under
    # specs/personal/ would otherwise have its provider- or panel-named path
    # echoed to stderr, the transcript, every relayed reviewer report and the
    # state file; that it does not is currently guaranteed only by
    # `personal_tracked` aborting 80 lines earlier, which is an ordering
    # nothing enforces.
    flagged = review_worktree.nonstandard_index_flags(repo)
    assert [entry for entry in flagged if entry.endswith("tracked.txt")], flagged
    assert not any("tracked.txt" in w for w in warnings), warnings


def _tree_digest(root: Path) -> dict[str, bytes]:
    return {
        str(p.relative_to(root)): p.read_bytes()
        for p in sorted(root.rglob("*"))
        if p.is_file() and ".git" not in p.parts
    }


def test_setup_and_teardown_leave_the_live_tree_byte_identical(
    repo: Path, scratch: Path
) -> None:
    """ADR-0068's central claim, pinned instead of hand-verified: a full
    cycle touches neither tracked content, untracked content, git status,
    nor the stash reflog of the live tree."""
    (repo / "tracked.txt").write_text("dirty edit\n", encoding="utf-8")
    (repo / "brand-new.txt").write_text("never added\n", encoding="utf-8")
    before_status = _git(repo, "status", "--porcelain")
    before_bytes = _tree_digest(repo)
    assert _setup(scratch) == 0
    assert main(["teardown", "--scratch", str(scratch)]) == 0
    assert _git(repo, "status", "--porcelain") == before_status
    assert _tree_digest(repo) == before_bytes
    assert _git(repo, "stash", "list") == ""


def test_setup_failure_rolls_back_worktrees_and_state(
    repo: Path,
    scratch: Path,
    monkeypatch: pytest.MonkeyPatch,
    git_double: _GitDouble,
) -> None:
    (repo / "brand-new.txt").write_text("untracked\n", encoding="utf-8")

    def _second_add_fails(
        root: Path, *args: str
    ) -> subprocess.CompletedProcess[str] | None:
        if _subcommand(args) == ("worktree", "add") and git_double.nth(args) == 2:
            return _git_failure(args, "boom\n")
        return None

    git_double.install(_second_add_fails)
    assert _setup(scratch) == 1
    listed = _git(repo, "worktree", "list", "--porcelain")
    assert "wt-" not in listed  # the first worktree was unwound
    assert not (scratch / STATE_FILENAME).exists()


def test_second_setup_without_teardown_is_refused(repo: Path, scratch: Path) -> None:
    """Overwriting the state file would orphan the first pair permanently —
    teardown could never learn their paths again."""
    (repo / "tracked.txt").write_text("edit\n", encoding="utf-8")
    assert _setup(scratch) == 0
    first_pair = set(_worktrees(scratch).values())
    assert _setup(scratch) == 1
    assert set(_worktrees(scratch).values()) == first_pair  # state untouched
    assert main(["teardown", "--scratch", str(scratch)]) == 0


def test_agent_list_is_validated(repo: Path, scratch: Path) -> None:
    (repo / "tracked.txt").write_text("edit\n", encoding="utf-8")
    base_args = ["setup", "--scratch", str(scratch), "--base", "base"]
    assert main([*base_args, "--agents", "a,a"]) == 1  # duplicate: unrecordable
    assert main([*base_args, "--agents", ""]) == 1  # no agents at all
    assert main([*base_args, "--agents", "a/../escape"]) == 1  # path chars
    assert not (scratch / STATE_FILENAME).exists()
    assert main([*base_args, "--agents", " a , b "]) == 0  # whitespace stripped
    assert set(_worktrees(scratch)) == {"a", "b"}
    assert main(["teardown", "--scratch", str(scratch)]) == 0


def test_missing_base_ref_aborts_instead_of_misreading_the_scope(
    repo: Path, scratch: Path
) -> None:
    """A missing base must never collapse into "no diff" (clean tree would
    exit 2 unreviewed) nor into a silent ready manifest (dirty tree would be
    reviewed against a scope the reviewers cannot even compute)."""
    args = ["setup", "--scratch", str(scratch), "--base", "no/such/ref"]
    assert main(args) == 1  # clean tree: abort, NOT exit 2
    (repo / "tracked.txt").write_text("edit\n", encoding="utf-8")
    assert main(args) == 1  # dirty tree: abort, NOT a ready manifest
    assert not (scratch / STATE_FILENAME).exists()


def test_a_snapshot_blind_tree_cannot_earn_the_nothing_to_review_exit(
    repo: Path, scratch: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Exit 2 is the only code that sanctions skipping the reviewers, so it
    may not be earned on a tree the launcher KNOWS the snapshot is blind to.

    `nonstandard_index_flags` exists solely because "modifications to such
    files are invisible to `git stash create`, so the snapshot silently lacks
    them" — positive evidence the tree is not clean — and the exit-2 gate
    tested only `sha is None and not manifest and not has_diff`, never
    consulting it. This test previously asserted exit **2** for exactly this
    tree, pinning the false pass; the two sibling cases in the same gate had
    already been promoted from exit 2 to hard aborts on the reasoning
    "everything reviewable was filtered out, not absent", and a skip-worktree
    edit is that shape with strictly worse content — tracked source rather
    than a symlink, and for `--assume-unchanged` an edit that can actually
    land, because an explicitly named `git add <path>` still stages it.

    The warning is still asserted: an abort that replaced the explanation
    would trade one silent failure for another.
    """
    _git(repo, "update-index", "--skip-worktree", "tracked.txt")
    (repo / "tracked.txt").write_text("hidden edit\n", encoding="utf-8")
    assert _setup(scratch) == 1
    captured = capsys.readouterr()
    assert "skip-worktree" in captured.err
    assert "'nothing to review' cannot be established" in captured.err
    assert "nothing to review" not in captured.out
    assert not (scratch / STATE_FILENAME).exists()


def test_a_warning_on_an_otherwise_clean_tree_still_exits_2(
    repo: Path, scratch: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The complement, so the abort above cannot be satisfied by refusing
    every clean tree.

    Named for what it exercises. A first version asserted only that a bare
    clean tree exits 2, which duplicated `test_clean_tree_exits_2_and_
    materializes_nothing` and left the interesting combination — a fidelity
    warning printed *and* exit 2 still firing — covered by nothing, while its
    name claimed otherwise. The distinction matters because the abort above
    is triggered by one particular warnable condition, and nothing else that
    warns may be swept into it.

    A machine-local ignore rule is the warnable state chosen here: it is
    invisible to `git stash create` in the same way a skip-worktree entry is,
    it warns, and it must NOT abort.
    """
    (repo / "hidden-by-machine-rule.txt").write_text("x\n", encoding="utf-8")
    exclude = repo / ".git" / "info" / "exclude"
    exclude.parent.mkdir(parents=True, exist_ok=True)
    exclude.write_text("hidden-by-machine-rule.txt\n", encoding="utf-8")

    assert _setup(scratch) == 2
    captured = capsys.readouterr()
    assert "machine-local ignore rules" in captured.err, captured.err
    assert "nothing to review" in captured.out
    assert not (scratch / STATE_FILENAME).exists()


def test_state_file_exists_before_any_worktree_is_created(
    repo: Path,
    scratch: Path,
    monkeypatch: pytest.MonkeyPatch,
    git_double: _GitDouble,
) -> None:
    """The ordering IS the property: written after, a kill between the git
    registration and the write leaves worktrees nothing on disk records."""
    (repo / "tracked.txt").write_text("edit\n", encoding="utf-8")
    state_existed_at_first_add: list[bool] = []

    def _spying_git(root: Path, *args: str) -> subprocess.CompletedProcess[str] | None:
        if _subcommand(args) == ("worktree", "add") and not state_existed_at_first_add:
            state_existed_at_first_add.append((scratch / STATE_FILENAME).exists())
        return None

    git_double.install(_spying_git)
    assert _setup(scratch) == 0
    assert state_existed_at_first_add == [True]
    assert main(["teardown", "--scratch", str(scratch)]) == 0


def test_a_hard_kill_mid_materialization_is_recoverable_by_teardown(
    repo: Path,
    scratch: Path,
    monkeypatch: pytest.MonkeyPatch,
    git_double: _GitDouble,
) -> None:
    """The kill window the ordering exists for, end to end: a signal-style
    interrupt (not AbortError/OSError, so no rollback runs) after the first
    worktree is registered must leave a state file teardown can act on."""
    (repo / "tracked.txt").write_text("edit\n", encoding="utf-8")

    def _killed_git(root: Path, *args: str) -> subprocess.CompletedProcess[str] | None:
        if _subcommand(args) == ("worktree", "add") and git_double.nth(args) == 2:
            raise KeyboardInterrupt  # the process dies here, mid-window
        return None

    git_double.install(_killed_git)
    with pytest.raises(KeyboardInterrupt):
        main(["setup", "--scratch", str(scratch), "--base", "base"])
    git_double.restore()
    # The first worktree is registered and nothing rolled it back — but the
    # pre-written state file names every planned path, so teardown recovers.
    assert (scratch / STATE_FILENAME).exists()
    assert main(["teardown", "--scratch", str(scratch)]) == 0
    assert "wt-" not in _git(repo, "worktree", "list", "--porcelain")


@pytest.mark.parametrize("key", _RECORDED_KEYS)
def test_teardown_refuses_to_force_delete_outside_the_scratch_dir(
    repo: Path, scratch: Path, tmp_path: Path, key: str
) -> None:
    """The state file is parsed data; an unvalidated recursive delete of
    whatever it names is the destructive class ADR-0068 exists to remove.
    A path outside --scratch must survive teardown, whatever the state says."""
    victim = tmp_path / "victim"
    victim.mkdir()
    (victim / "precious.txt").write_text("do not delete\n", encoding="utf-8")
    scratch.mkdir(parents=True, exist_ok=True)
    _write_state(scratch, _state_recording(repo, scratch, key, victim))
    assert main(["teardown", "--scratch", str(scratch)]) == 1
    assert (victim / "precious.txt").read_text(encoding="utf-8") == "do not delete\n"


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits (WSL/CI legs run this)")
def test_untracked_executable_keeps_its_mode_in_the_worktrees(
    repo: Path, scratch: Path
) -> None:
    """copyfile would deliver 0644 and the reviewer's run of the script would
    read as the author's defect; copy2 carries the mode."""
    script = repo / "new-script.sh"
    script.write_text("#!/bin/sh\necho ok\n", encoding="utf-8")
    script.chmod(0o755)
    assert _setup(scratch) == 0
    for path in _worktrees(scratch).values():
        mode = (Path(path) / "new-script.sh").stat().st_mode
        assert mode & stat.S_IXUSR


def test_usage_errors_exit_1_not_the_nothing_to_review_2() -> None:
    """argparse's default usage-error exit is 2 — this script's "nothing to
    review, skip the reviewers". A typo'd flag must read as abort, never as a
    sanctioned skip (the collision bot_review.py's _Parser fixes for its
    EXIT_CLEAN)."""
    with pytest.raises(SystemExit) as bad_flag:
        main(["setup", "--bogus"])
    assert bad_flag.value.code == 1
    with pytest.raises(SystemExit) as no_command:
        main([])
    assert no_command.value.code == 1


def test_a_base_with_no_merge_base_aborts_instead_of_reading_as_a_diff(
    repo: Path, scratch: Path
) -> None:
    """`git diff --quiet` exits 128 (not 1) when base...HEAD has no merge
    base — orphan history, shallow clone. Reading any nonzero as "has diff"
    would print a manifest advertising a diff command the reviewers cannot
    run either; it must abort like a missing ref does."""
    _git(repo, "checkout", "--orphan", "island")
    _git(repo, "commit", "-m", "unrelated root")
    _git(repo, "checkout", "main")
    (repo / "tracked.txt").write_text("edit\n", encoding="utf-8")
    assert main(["setup", "--scratch", str(scratch), "--base", "island"]) == 1
    assert not (scratch / STATE_FILENAME).exists()


def test_teardown_never_git_removes_a_registered_worktree_outside_scratch(
    repo: Path, scratch: Path, tmp_path: Path
) -> None:
    """`git worktree remove --force` succeeds on a REGISTERED path wherever
    it lives, uncommitted work included — so containment must be validated
    before git runs. The unregistered-victim test alone cannot prove this:
    there git fails on its own and the guard fires afterwards either way."""
    victim = tmp_path / "victim-wt"
    _git(repo, "worktree", "add", "--detach", str(victim))
    (victim / "uncommitted.txt").write_text("precious\n", encoding="utf-8")
    scratch.mkdir(parents=True, exist_ok=True)
    state = {"root": str(repo), "worktrees": {"x": str(victim)}}
    (scratch / STATE_FILENAME).write_text(json.dumps(state), encoding="utf-8")
    assert main(["teardown", "--scratch", str(scratch)]) == 1
    assert (victim / "uncommitted.txt").read_text(encoding="utf-8") == "precious\n"
    registered = review_worktree._registered_paths(repo)  # pyright: ignore[reportPrivateUsage] - separator-safe check the test needs too
    assert victim.resolve() in registered


@pytest.mark.parametrize("key", _RECORDED_KEYS)
def test_teardown_refuses_a_state_naming_the_scratch_dir_itself(
    repo: Path, scratch: Path, key: str
) -> None:
    """is_relative_to() is true for the path itself: without the equality
    guard, scratch would pass containment and the force-delete fallback
    would destroy the state file mid-teardown."""
    scratch.mkdir(parents=True, exist_ok=True)
    _write_state(scratch, _state_recording(repo, scratch, key, scratch))
    assert main(["teardown", "--scratch", str(scratch)]) == 1
    assert (scratch / STATE_FILENAME).exists()


@pytest.mark.parametrize("key", _RECORDED_KEYS)
@pytest.mark.parametrize("spelling", ["..", "wt-x/..", "", ".. ", "..."])
def test_teardown_refuses_every_spelling_that_reaches_outside_the_scratch_dir(
    repo: Path, scratch: Path, tmp_path: Path, key: str, spelling: str
) -> None:
    """The normalized spelling was the only one either remover was pinned on.

    `<scratch>/..` passes the lexical containment test — `WindowsPath('C:/s/..')
    != WindowsPath('C:/s')` satisfies the strict-inequality guard while
    `is_relative_to` matches component-wise — and `_force_delete_tree` then
    resolves it and recursively deletes the directory holding the repository,
    with teardown returning success (reproduced). `""` is the same class from
    the other end: `Path('C:/').name` is `''` and `scratch / '' == scratch`, so
    the renamed-scratch relocation handed back the scratch dir itself.

    Both are refused before anything locates, relocates or deletes, because
    neither is an entry name — they are path syntax. Parametrized over both
    recorded categories: the previous fix reached one remover and not the
    other, which is what this shape exists to stop happening again.

    `.. ` and `...` are the same class reached through Win32 rather than
    through pathlib: trailing dots and spaces are stripped at the filesystem
    call, so they open `..` while pathlib reports them as ordinary names. This
    test states the **outcome** invariant for every spelling — nothing outside
    is touched, the scratch dir survives, the record survives — and is
    deliberately redundant: two independent guards each produce it, so no
    single mutation turns it red. Which guard fires is pinned separately, by
    `test_a_name_win32_folds_away_never_reaches_git` and by
    `test_a_delete_refusal_is_never_overruled_by_the_absence_probe`.

    `"."` is not in the list because pathlib normalizes it away at
    construction (`Path('a/.').name` is `'a'`, `Path('.').name` is `''`), so no
    recorded string can produce it — see the sibling below for the control that
    keeps this refusal from widening onto ordinary names.
    """
    scratch.mkdir(parents=True, exist_ok=True)
    outer = scratch.parent
    (outer / "precious.txt").write_text("hours of work\n", encoding="utf-8")
    recorded = Path(scratch.anchor) if spelling == "" else scratch / spelling

    _write_state(scratch, _state_recording(repo, scratch, key, recorded))
    assert main(["teardown", "--scratch", str(scratch)]) == 1
    assert (outer / "precious.txt").read_text(encoding="utf-8") == "hours of work\n"
    assert scratch.is_dir(), "the scratch dir itself was deleted"
    assert (scratch / STATE_FILENAME).exists(), "the only record was deleted"


@pytest.mark.parametrize("key", _RECORDED_KEYS)
def test_an_ordinary_never_created_path_inside_scratch_is_still_tolerated(
    repo: Path, scratch: Path, key: str
) -> None:
    """The control for the refusal above, which is the guard's real risk.

    A syntax-name check that over-matches turns every teardown into an exit 1,
    which keeps the state file, which makes the next setup refuse — a wedge
    manufactured by the fix for a wedge. A plain `wt-x` under the scratch dir
    is an ordinary recorded path that was simply never created (setup writes
    the state file before materializing anything), and it must still tear down
    clean.
    """
    scratch.mkdir(parents=True, exist_ok=True)
    _write_state(scratch, _state_recording(repo, scratch, key, scratch / "wt-x"))
    assert main(["teardown", "--scratch", str(scratch)]) == 0
    assert not (scratch / STATE_FILENAME).exists()


@pytest.mark.parametrize("spelling", [".. ", "..."])
def test_a_name_win32_folds_away_never_reaches_git(
    repo: Path, scratch: Path, spelling: str, git_double: _GitDouble
) -> None:
    """`_SYNTAX_NAMES` was matched literally, and Win32 folds before it looks.

    Trailing dots and spaces are stripped at the filesystem call, not at parse
    time: pathlib keeps `'.. '` as an ordinary name, so `is_relative_to`
    answered True and `_contained` returned `inside=True`, while every API
    opening that path sees `..`. Reproduced on Windows.

    The assertion is that **git never saw it**, not that teardown exited 1,
    because those are different guards. `_remove_worktree`'s docstring promises
    *"Containment is validated BEFORE git runs"*, and this spelling reached
    `git worktree remove --force` with a lexical pass behind it. The exit code
    cannot tell the two apart: the sibling fix below — propagating
    `_force_delete_tree`'s refusal instead of discarding it — also produces
    exit 1 here, from one call later. Measured: with this guard reverted and
    that one in place, an outcome-only assertion stayed green.

    Not platform-branched. On POSIX `.. ` is a legal entry name and no boundary
    is crossed, so the refusal is a *uniformity* property there: one state file
    is read by both legs of this project's own gate run, and a recorded name
    meaning one directory under Windows and another under Linux is the
    ambiguity worth removing. The cost is refusing a name the launcher never
    generates — see the ordinary-name control above.
    """
    scratch.mkdir(parents=True, exist_ok=True)
    seen: list[tuple[str, ...]] = []

    def _record(root: Path, *args: str) -> None:
        seen.append(args)
        return None

    git_double.install(_record)
    _write_state(
        scratch, _state_recording(repo, scratch, "worktrees", scratch / spelling)
    )
    assert main(["teardown", "--scratch", str(scratch)]) == 1
    git_double.restore()

    removals = [args for args in seen if args[:2] == ("worktree", "remove")]
    assert not removals, f"a Win32-folded spelling was handed to git: {removals}"
    assert (scratch / STATE_FILENAME).exists(), "the only record was deleted"


@pytest.mark.parametrize("key", _RECORDED_KEYS)
def test_a_delete_refusal_is_never_overruled_by_the_absence_probe(
    repo: Path, scratch: Path, key: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The deleter's refusal reaches the operator, or it is not a refusal.

    Both removers gated their error on `_os_path(located).exists()` alone and
    threw the returned failure list away when it answered False — which is
    exactly what it answers for the paths that earn a containment refusal,
    since `\\\\?\\` suppresses the normalization that made the bare spelling
    resolve anywhere. Measured before the fix: `_force_delete_tree` returned
    its full *"resolves to … not strictly inside the scratch dir"* refusal,
    the caller returned success, and teardown unlinked the state file over a
    directory still on disk.

    `_force_delete_tree` is doubled rather than driven, because the sibling
    fix above closes the only spelling that reached this branch by accident.
    What is under test is the *caller contract* — a non-empty return means
    nothing was deleted — so the double states the contract directly instead
    of relying on a second defect to reach it. The recorded path is never
    created, which is what makes the absence probe answer False on both legs.

    The double refuses **only the path under test** and delegates everything
    else to the real deleter. A blanket refusal also refuses the inert
    `worktrees` entry `_state_recording` leaves in place, so the `venvs` case
    exited 1 by way of the *worktree* caller and stayed green with the venv
    caller mutated — measured, and the reason this is scoped by name.
    """
    scratch.mkdir(parents=True, exist_ok=True)
    recorded = scratch / "refused-target"
    real_delete = review_worktree._force_delete_tree  # pyright: ignore[reportPrivateUsage] - restored below without undo()

    def _refuses(path: Path, boundary: Path) -> list[str]:
        if path.name != recorded.name:
            return real_delete(path, boundary)
        return [f"{path} resolves outside the scratch dir {boundary}"]

    monkeypatch.setattr(review_worktree, "_force_delete_tree", _refuses)
    _write_state(scratch, _state_recording(repo, scratch, key, recorded))

    assert main(["teardown", "--scratch", str(scratch)]) == 1
    monkeypatch.setattr(review_worktree, "_force_delete_tree", real_delete)
    assert (scratch / STATE_FILENAME).exists(), (
        "the refusal was discarded and the only record deleted"
    )


@pytest.mark.parametrize("key", _RECORDED_KEYS)
def test_teardown_tolerates_a_gone_unregistered_outside_scratch_path(
    repo: Path, scratch: Path, tmp_path: Path, key: str
) -> None:
    """A scratch dir moved or renamed after setup leaves recorded paths
    outside the new scratch; when such a path is already gone AND
    unregistered there is nothing left to free, and refusing forever would
    wedge teardown and block every future setup."""
    scratch.mkdir(parents=True, exist_ok=True)
    ghost = tmp_path / "old-scratch" / "wt-gone"
    _write_state(scratch, _state_recording(repo, scratch, key, ghost))
    assert main(["teardown", "--scratch", str(scratch)]) == 0
    assert not (scratch / STATE_FILENAME).exists()


def test_teardown_acts_on_the_recorded_repo_not_the_cwd(
    repo: Path, scratch: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Agent threads reset cwd between calls: a teardown run from elsewhere
    must clean the repo that created the worktrees, not whatever repository
    (or non-repository) its own cwd happens to be inside."""
    (repo / "tracked.txt").write_text("edit\n", encoding="utf-8")
    assert _setup(scratch) == 0
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)  # not a git repository at all
    assert main(["teardown", "--scratch", str(scratch)]) == 0
    assert "wt-" not in _git(repo, "worktree", "list", "--porcelain")


def test_teardown_aborts_on_a_state_file_missing_the_repo_root(
    repo: Path, scratch: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    scratch.mkdir(parents=True, exist_ok=True)
    (scratch / STATE_FILENAME).write_text(
        json.dumps({"worktrees": {}}), encoding="utf-8"
    )
    assert main(["teardown", "--scratch", str(scratch)]) == 1
    assert "malformed (root)" in capsys.readouterr().err


def test_extended_length_prefix_handles_drive_unc_and_prefixed_forms() -> None:
    r"""A UNC path needs `\\?\UNC\server\share`; the naive `\\?\` prefix on
    `\\server\share` parses with `?` as the server name and breaks every
    delete on exactly the deep trees the fallback exists to remove."""
    ext = review_worktree._extended_length  # pyright: ignore[reportPrivateUsage] - pure helper under test
    assert str(ext(Path("C:\\deep\\tree"))) == "\\\\?\\C:\\deep\\tree"
    assert (
        str(ext(Path("\\\\server\\share\\deep"))) == "\\\\?\\UNC\\server\\share\\deep"
    )
    already = Path("\\\\?\\C:\\deep")
    assert ext(already) is already


def test_unmerged_entries_are_not_mislabeled_as_hidden_from_snapshot(
    repo: Path,
) -> None:
    """ls-files -v tags unmerged entries `M`; only `S` (skip-worktree) and
    lowercase (assume-unchanged) mean "invisible to the snapshot". A
    not-startswith-H match would report a conflicted file under the wrong
    warning."""
    _git(repo, "checkout", "-b", "left")
    (repo / "tracked.txt").write_text("left\n", encoding="utf-8")
    _git(repo, "commit", "-am", "left")
    _git(repo, "checkout", "main")
    (repo / "tracked.txt").write_text("right\n", encoding="utf-8")
    _git(repo, "commit", "-am", "right")
    assert _git_raw(repo, "merge", "left").returncode != 0  # conflicted index
    assert review_worktree.nonstandard_index_flags(repo) == []


def test_venv_redirect_is_emitted_recorded_and_torn_down(
    repo: Path, scratch: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The manifest's per-agent UV_PROJECT_ENVIRONMENT redirect is emitted,
    recorded in the state file, and torn down with everything else.

    The reason for the redirect is **MAX_PATH headroom for `uv`, and nothing
    else** — `scripts/review_worktree.py` governs it at the site that builds
    the paths. This docstring used to give a different reason, that a venv
    inside a worktree would surface as "thousands of untracked files in the
    tree under review": measured false on 2026-08-01 (uv writes a `.gitignore`
    holding `*` into every environment it creates, and `.venv/` is ignored by
    this repo's own tracked .gitignore, so the real count is **zero** `??`
    entries). ADR-0068 §4 records the correction and says it was applied
    everywhere; this was the surviving copy, which is what makes a
    "corrected everywhere" claim worth re-checking rather than believing.
    """
    (repo / "tracked.txt").write_text("edit\n", encoding="utf-8")
    assert _setup(scratch) == 0
    out = capsys.readouterr().out
    assert "venv[spec-reviewer]:" in out
    assert "UV_PROJECT_ENVIRONMENT" in out
    venvs_obj = _state(scratch)["venvs"]
    assert isinstance(venvs_obj, dict)
    venvs = cast("dict[str, str]", venvs_obj)
    assert set(venvs) == {"spec-reviewer", "test-reviewer"}
    built = Path(venvs["test-reviewer"])  # simulate the reviewer building one
    (built / "lib").mkdir(parents=True)
    (built / "lib" / "module.py").write_text("x = 1\n", encoding="utf-8")
    assert main(["teardown", "--scratch", str(scratch)]) == 0
    assert not built.exists()


def test_a_hung_git_child_gets_the_one_line_abort_not_a_hang(
    repo: Path,
    scratch: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _, killed = _spawn_double(monkeypatch, match="rev-parse", hang=True)
    assert _setup(scratch) == 1
    err = capsys.readouterr().err
    assert "did not return" in err
    assert "Traceback" not in err
    # The abort is only half of it: a hung child that is never killed keeps
    # holding the pipes it inherited, which is the wall-clock hang
    # `test_a_straggler_holding_the_pipes_cannot_outlast_the_timeout` bounds.
    assert killed, "the hung tree was never killed"


def _straggler_scripts(tmp_path: Path) -> tuple[Path, Path]:
    """A slow and a fast child, each leaving a straggler on the inherited pipes.

    This is the `git worktree add` shape: the post-checkout hook backgrounds
    an indexer or an fsmonitor daemon, the daemon inherits git's stdout and
    stderr write ends, and git's own exit closes neither of them.
    """
    straggler = tmp_path / "straggler.py"
    straggler.write_text(
        "import time\ntime.sleep(30)\n", encoding="utf-8", newline="\n"
    )
    slow = tmp_path / "slow.py"
    slow.write_text(
        "import subprocess, sys, time\n"
        f"subprocess.Popen([sys.executable, {str(straggler)!r}])\n"
        "time.sleep(30)\n",
        encoding="utf-8",
        newline="\n",
    )
    fast = tmp_path / "fast.py"
    fast.write_text(
        "import subprocess, sys, time\n"
        "sys.stdout.write('git-said-this\\n')\n"
        "sys.stdout.flush()\n"
        f"subprocess.Popen([sys.executable, {str(straggler)!r}])\n"
        # Stays alive briefly on purpose. On Windows the tree kill is a job
        # object, and `AssignProcessToJobObject` refuses a process that has
        # already terminated (measured) — so a child that exits instantly is
        # never assigned, its straggler is unkillable, and `_spawn` takes its
        # abort path instead. That path is real and documented at `_open_job`;
        # it is simply not what this test is about, and without this sleep
        # which branch runs is decided by scheduler luck, which showed up as
        # an intermittent failure under `-n auto`.
        "time.sleep(0.5)\n",
        encoding="utf-8",
        newline="\n",
    )
    return slow, fast


def test_a_straggler_holding_the_pipes_cannot_outlast_the_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`_COMMAND_TIMEOUT` has to bound WALL-CLOCK, and `subprocess.run` did not.

    The predecessor passed `timeout=` and was pinned only on having passed it
    — `test_every_git_invocation_carries_the_long_path_flag` asserts that and
    stayed green over the hang. Measured against the old shape with a 60s
    straggler at `timeout=3`, both legs, both paths:

    * Windows 60.2s, unbounded — `subprocess.run`'s timeout handler kills the
      direct child and then calls `communicate()` a second time with no
      deadline, so it blocks for as long as the straggler lives.
    * POSIX 3.0s, bounded but WRONG — that branch only calls `process.wait()`,
      and a git call that exited 0 comes back as TimeoutExpired, aborting a
      healthy round.

    So both legs need both halves asserted: a bound, and the right answer.
    Real subprocesses rather than a double, because the defect is in what the
    stdlib does with a real pipe that a double cannot reproduce.
    """
    slow, fast = _straggler_scripts(tmp_path)
    # Not shrunk further: the grace has to cover a real process teardown on a
    # loaded runner, and `-n auto` here is 24 workers deep.
    monkeypatch.setattr(review_worktree, "_KILL_GRACE", 4)
    # The straggler lives 30s, so both ceilings sit far below the unfixed
    # behaviour and far above the fixed one — never within a loaded runner's
    # noise of either.
    ceiling = 20.0

    # A short bound for the hung child, which is the case being timed out.
    monkeypatch.setattr(review_worktree, "_COMMAND_TIMEOUT", 2)
    start = time.monotonic()
    with pytest.raises(review_worktree.AbortError, match="did not return within"):
        review_worktree._run(sys.executable, str(slow), label="git probe")  # pyright: ignore[reportPrivateUsage] - the spawn wrapper is the subject
    hung = time.monotonic() - start
    assert hung < ceiling, f"a hung child held the call for {hung:.1f}s"

    # A generous one for the child that SUCCEEDS: the timeout must not be
    # what ends this call, or the assertion below would pass for the wrong
    # reason. Interpreter startup plus the child's deliberate half-second of
    # life is already most of a 2s budget on a loaded runner.
    monkeypatch.setattr(review_worktree, "_COMMAND_TIMEOUT", 15)
    # Records that the success-path kill was ATTEMPTED, and still performs it.
    # The outcome assertions below are a disjunction — a refusal is a correct
    # outcome where the platform cannot kill the tree — and a disjunction
    # cannot pin the call: deleting `_kill_tree` from the success path leaves
    # `_settle` timing out on its own schedule and the run taking the refusal
    # branch, which the disjunction accepts. Measured: with the call removed
    # the whole file stayed green. So the call is pinned as the property this
    # guard OWNS, separately from the outcomes it can produce.
    real_kill = review_worktree._kill_tree  # pyright: ignore[reportPrivateUsage] - the guard under test
    killed: list[int | None] = []

    def _recording_kill(proc: Any, job: int | None) -> None:
        killed.append(job)
        real_kill(proc, job)

    monkeypatch.setattr(review_worktree, "_kill_tree", _recording_kill)

    start = time.monotonic()
    stdout: str | None = None
    refusal = ""
    try:
        clean_proc = review_worktree._run(sys.executable, str(fast), label="git probe")  # pyright: ignore[reportPrivateUsage] - the spawn wrapper is the subject
        stdout = clean_proc.stdout
    except review_worktree.AbortError as exc:
        refusal = str(exc)
    clean = time.monotonic() - start
    assert killed, (
        "the straggler outlived the read and nothing tried to kill the tree — "
        "on the success path the timeout never fires, so this call is the only "
        "thing that releases the pipes"
    )
    assert clean < ceiling, f"a straggler held a SUCCEEDING call for {clean:.1f}s"
    assert stdout is not None or "cannot be read completely" in refusal, refusal

    # Two outcomes are correct here and which one occurs is decided by the
    # platform, so the assertion is the invariant they share: the call either
    # returns git's COMPLETE output or refuses — never a silent prefix, which
    # for `_git_z` would be a short path list indistinguishable from a real
    # one, and `personal_tracked` reading `[]` means "nothing personal is
    # tracked, proceed".
    #
    # POSIX always returns: `start_new_session` makes the child a group leader
    # before it runs, and the group outlives it, so the tree stays killable.
    # Windows depends on the job object, which `AssignProcessToJobObject`
    # refuses for an already-exited process — and, measured here, under
    # `pytest -n auto`, where the xdist worker is itself already in a job.
    # Branching on `os.name` would therefore assert the wrong thing on the
    # Windows leg that happens to succeed.
    assert stdout in (None, "git-said-this\n"), (
        f"a partial read was returned as if complete: {stdout!r}"
    )
    if os.name != "nt":
        assert stdout == "git-said-this\n", "POSIX has no job-assignment window"


@pytest.mark.skipif(os.name == "nt", reason="symlink creation (WSL/CI legs run this)")
def test_a_symlink_only_tree_aborts_instead_of_exiting_2(
    repo: Path, scratch: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """When the symlink filter empties the manifest, "nothing to review"
    would be factually false — the change would ship with a stderr warning
    as its only trace. Everything-filtered-out must abort, not skip.

    POSIX-only by subject: the Windows half of this invariant is
    `test_a_tree_of_only_linked_content_is_not_reported_as_nothing_to_review`,
    which runs on both legs and branches on which abort fires."""
    outside = tmp_path / "target.txt"
    outside.write_text("content\n", encoding="utf-8")
    (repo / "only-a-link").symlink_to(outside)
    assert _setup(scratch) == 1
    assert "symlink" in capsys.readouterr().err
    assert not (scratch / STATE_FILENAME).exists()


@pytest.mark.skipif(os.name == "nt", reason="symlink creation (WSL/CI legs run this)")
def test_copy_untracked_rechecks_symlinks_at_read_time(
    repo: Path, tmp_path: Path
) -> None:
    """The manifest filter and copy2's read are separate moments; a path
    swapped to a symlink in between would replicate its TARGET's bytes —
    including content outside the repo — so the copy re-checks at the read.

    POSIX-only by subject: the leaf-swap this models is the POSIX shape. The
    Windows shape is a *directory* on the path becoming a junction, which no
    leaf check can see, and
    `test_the_copy_time_containment_abort_redacts_the_path_it_refused` covers
    it on both legs."""
    outside = tmp_path / "outside-secret.txt"
    outside.write_text("must never materialize\n", encoding="utf-8")
    (repo / "racy.txt").symlink_to(outside)
    dest = tmp_path / "fake-worktree"
    dest.mkdir()
    with pytest.raises(review_worktree.AbortError):
        review_worktree._copy_untracked(repo, dest, ["racy.txt"])  # pyright: ignore[reportPrivateUsage] - the recheck under test lives here
    assert not (dest / "racy.txt").exists()


def test_git_z_aborts_on_a_hung_query_naming_the_subcommand(
    repo: Path,
    scratch: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`_git_z` has its own timeout handler, and nothing reached it.

    Both existing hung-git tests hang on the first `_run`-routed call
    (`rev-parse`, `stash create`), so they exercise `_run`'s handler and never
    this one — measured: gutting `_git_z`'s `except TimeoutExpired` to
    `return []` left the whole file green. That matters more here than
    anywhere else in the module, because `_git_z` is by its own docstring the
    single path for **every** path list this launcher reads, `personal_tracked`
    included: a `_git_z` that swallowed a failure as `[]` would report no
    tracked files under `specs/personal/` and let setup proceed — defeating
    the one precondition ADR-0068's containment-by-construction claim rests on.
    """
    (repo / "tracked.txt").write_text("edit\n", encoding="utf-8")
    # `ls-files` is read through `_git_z`; `rev-parse`/`stash` are not.
    _spawn_double(monkeypatch, match="ls-files", hang=True)
    assert _setup(scratch) == 1

    err = capsys.readouterr().err
    assert "did not return within" in err
    assert "git ls-files" in err, err
    assert "Traceback" not in err
    assert not (scratch / STATE_FILENAME).exists()


def test_git_z_aborts_on_a_failing_query_instead_of_reading_it_as_empty(
    repo: Path,
    scratch: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The other half of the same gap, and the dangerous direction.

    An empty result and a failed query are indistinguishable downstream —
    `personal_tracked` returning `[]` means "nothing personal is tracked,
    proceed" — so `_git_z` must abort on a nonzero exit rather than hand back
    what it managed to parse. Measured: replacing that check with `return []`
    left the suite green, so nothing pinned the difference between "the guard
    passed" and "the guard never ran".
    """
    (repo / "tracked.txt").write_text("edit\n", encoding="utf-8")
    # Bytes, not str: `_git_z` reads the raw stream on purpose.
    _spawn_double(
        monkeypatch, match="ls-files", returncode=128, stderr=b"fatal: bad thing\n"
    )
    assert _setup(scratch) == 1, "a failed path query was read as an empty list"

    err = capsys.readouterr().err
    assert "exited 128" in err
    assert "fatal: bad thing" in err
    assert "Traceback" not in err
    assert not (scratch / STATE_FILENAME).exists()


def test_a_recorded_link_that_will_not_unlink_never_deletes_its_target(
    repo: Path, scratch: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The rmdir fallback, on a REAL link, asserting the property it claims.

    Its comment used to say `os.unlink` raises on a Windows directory link,
    so a junction fixture would exercise it. That is false on this project's
    interpreter — measured on Python 3.14.3 / Windows 11, `Path.unlink()`
    removes a junction cleanly — and instrumenting the branch showed **zero**
    hits across the whole suite, including from the junction test written to
    cover this function. So the fallback has to be forced.

    Forcing it over a plain directory with `_is_link` stubbed True would test
    the `try/except → rmdir()` control flow and nothing else: the branch's
    actual contract is "removes the link itself and **never what it points
    at**", and a stand-in with nothing on the other side cannot check the
    second half. This uses a real link (`_link_dir`: junction on Windows,
    symlink on POSIX), leaves `_is_link` alone — it answers True on its own —
    and scopes the `unlink` refusal to that one path, because teardown unlinks
    the state file through the same method and a blanket refusal fails the run
    for the wrong reason.

    The two platforms then diverge, and both are asserted rather than one
    being skipped: `rmdir` removes a junction on Windows, so the fallback
    succeeds; on POSIX it raises `NotADirectoryError` on a symlink (measured),
    so the fallback fails and teardown reports it. **The target survives
    either way**, which is the invariant, and the failure path is the one that
    keeps a link the process could not remove from being reported as removed.
    """
    target = tmp_path / "precious"
    target.mkdir()
    (target / "keep.txt").write_text("must outlive teardown\n", encoding="utf-8")
    scratch.mkdir(parents=True)
    link = scratch / "venv-spec-reviewer"
    _link_dir(link, target)
    assert review_worktree._is_link(link)  # pyright: ignore[reportPrivateUsage] - the branch under test is gated on this

    state = {
        "root": str(repo),
        "worktrees": _inert_worktrees(scratch),
        "venvs": {"spec-reviewer": str(link)},
    }
    (scratch / STATE_FILENAME).write_text(json.dumps(state), encoding="utf-8")

    real_unlink = Path.unlink

    def _refuses(self: Path, missing_ok: bool = False) -> None:
        if self == link:
            raise PermissionError("directory links cannot be unlinked here")
        real_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", _refuses)
    exit_code = main(["teardown", "--scratch", str(scratch)])

    # The invariant, on both platforms: nothing on the far side was touched.
    assert target.exists(), "the fallback deleted through the link"
    assert (target / "keep.txt").read_text(
        encoding="utf-8"
    ) == "must outlive teardown\n"

    if os.name == "nt":
        assert exit_code == 0
        assert not link.exists(), "the rmdir fallback did not remove the link"
        assert not (scratch / STATE_FILENAME).exists()
    else:
        # rmdir cannot remove a symlink; the refusal must be reported, and the
        # state file kept, because the link is still on disk and this is its
        # only record.
        assert exit_code == 1
        assert link.is_symlink()
        assert (scratch / STATE_FILENAME).exists()


def test_the_copy_time_containment_abort_redacts_the_path_it_refused(
    repo: Path, tmp_path: Path
) -> None:
    """The backstop for the same race, on the form `is_symlink()` cannot see —
    and it must redact like the setup-time filter it backstops.

    A directory anywhere on the path becoming a link after the manifest was
    taken leaves every component answering `_is_link` False while `copy2`
    reads straight through to a target outside the repository. The setup-time
    filter prints a bare count for that, because the leaf names on the far
    side of the link come from outside the repo, and so outside review scope,
    on a machine that holds a real encrypted health database — and the abort
    reaches stderr, the transcript, and every reviewer report that relays it.

    This guard runs *only* when the setup-time one has been bypassed, which is
    exactly why it named the path unnoticed for a round: no test reached it.
    Reproduced by disabling the setup-time filter, which put the real leaf
    name on stderr.
    """
    outside = tmp_path / "outside-the-repo"
    outside.mkdir()
    (outside / "acme-panel-2026.md").write_text("SYNTHETIC\n", encoding="utf-8")
    _link_dir(repo / "linked-dir", outside)
    dest = tmp_path / "fake-worktree"
    dest.mkdir()

    with pytest.raises(review_worktree.AbortError) as excinfo:
        review_worktree._copy_untracked(  # pyright: ignore[reportPrivateUsage] - the backstop under test lives here
            repo, dest, ["linked-dir/acme-panel-2026.md"]
        )
    message = str(excinfo.value)
    assert "resolves outside the repository" in message
    assert "acme-panel-2026" not in message, "the abort named a path from outside"
    assert "linked-dir" not in message
    assert not (dest / "linked-dir").exists()


@pytest.mark.skipif(
    os.name == "nt", reason="non-UTF-8 filenames (WSL/CI legs run this)"
)
def test_a_non_utf8_filename_aborts_cleanly_instead_of_dying_by_traceback(
    repo: Path, scratch: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`git ls-files -z` emits such a name as raw bytes; the strict-decode
    alternative kills the run with a UnicodeDecodeError no except clause
    owns. With errors="replace" the mangled path fails the copy instead,
    which lands in the one-line abort contract and rolls back cleanly."""
    raw_name = os.path.join(os.fsencode(str(repo)), b"caf\xe9.txt")
    try:
        with open(raw_name, "wb") as f:
            f.write(b"content\n")
    except OSError:
        pytest.skip("filesystem rejects non-UTF-8 filenames")
    assert _setup(scratch) == 1
    err = capsys.readouterr().err
    assert "error:" in err
    assert "Traceback" not in err
    assert "wt-" not in _git(repo, "worktree", "list", "--porcelain")
    # And specifically NOT through the vanished-file tolerance. Both reach
    # this file by the same route — a manifest entry with no matching path on
    # disk — but only one of them is transient. Reporting an undecodable name
    # as "vanished" would drop a file that is still sitting in the tree,
    # downgrade a hard stop to an advisory warning, and send the operator
    # into a rerun that produces the identical result forever.
    assert "not valid UTF-8" in err
    assert "vanished" not in err


@pytest.mark.skipif(
    os.name == "nt", reason="non-UTF-8 filenames (WSL/CI legs run this)"
)
def test_a_worktree_registered_under_a_non_utf8_path_is_still_seen(
    repo: Path, tmp_path: Path
) -> None:
    """The stray check is an *exact* string comparison across a decode
    boundary, so the decode has to round-trip.

    `_git_z` defaults to `errors="replace"`, which maps every undecodable
    byte to the same U+FFFD; `_registered_paths` overrides it to
    `surrogateescape`, which is how `os.fsdecode` and `sys.argv` themselves
    carry those bytes. Without the override the two spellings of one real
    path can never compare equal, so `strays` comes back empty and teardown
    reports "no strays registered" and deletes the state file over a live
    registration — the identical silent-zero the `-z` choice was made to
    prevent, reintroduced by a lossy decode.
    """
    raw = os.path.join(os.fsencode(str(tmp_path)), b"wt-\xff")
    try:
        os.mkdir(os.path.join(os.fsencode(str(tmp_path)), b"probe-\xff"))
    except OSError:
        pytest.skip("filesystem rejects non-UTF-8 filenames")
    path = os.fsdecode(raw)
    _git(repo, "worktree", "add", "--detach", path)
    try:
        registered = review_worktree._registered_paths(repo)  # pyright: ignore[reportPrivateUsage] - the decode contract under test lives here
        assert Path(path).resolve() in registered, (
            "the registration decoded to a string that matches nothing — "
            "every stray would be invisible"
        )
    finally:
        _git(repo, "worktree", "remove", "--force", path)


def test_force_delete_records_callbacks_it_must_not_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """rmtree hands `onexc` whichever function raised, not only the deleting
    ones: os.lstat/os.path.islink/os.scandir on the Windows walker, os.open and
    os.close on the POSIX fd walker. Retrying those deletes nothing and returns
    early — swallowing the failure this list exists to name — and os.open and
    os.close raise TypeError on a lone str, which neither the retry's own
    except clause nor main's `except (AbortError, OSError)` catches, so
    teardown would die by traceback instead of the one-line error contract."""
    target = tmp_path / "tree"
    target.mkdir()
    handed = [os.lstat, os.path.islink, os.scandir, os.open, os.close]

    # Tolerant signature deliberately: `review_worktree.shutil` IS the stdlib
    # module (no local alias), so this double is installed process-wide for the
    # duration of the test. A signature naming only the two parameters under
    # test raises TypeError in any other caller that passes `ignore_errors`,
    # `onerror` or `dir_fd` — attributed to whatever unrelated test happened to
    # be running. Today that is contained by xdist process isolation and serial
    # execution inside a worker; neither is a property this test should depend
    # on to avoid corrupting its neighbours.
    def _fake_rmtree(path: object, *args: object, **kwargs: object) -> None:
        onexc = kwargs.get("onexc")
        assert callable(onexc)
        for func in handed:
            onexc(func, str(target), PermissionError(13, "Permission denied"))

    monkeypatch.setattr(review_worktree.shutil, "rmtree", _fake_rmtree)
    failures = review_worktree._force_delete_tree(target, tmp_path)  # pyright: ignore[reportPrivateUsage] - the retry policy under test lives here
    assert len(failures) == len(handed)
    assert all("Permission denied" in failure for failure in failures)


def test_force_delete_still_retries_the_callbacks_that_do_delete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The narrowed retry must still do the job it was written for: a
    read-only file (common inside venvs on Windows) whose unlink failed gets
    chmod-ed, unlinked on the retry, and reported as no failure at all."""
    target = tmp_path / "tree"
    target.mkdir()
    victim = target / "locked.txt"
    victim.write_text("x\n", encoding="utf-8")
    victim.chmod(stat.S_IREAD)

    # Tolerant signature for the reason its sibling above states.
    def _fake_rmtree(path: object, *args: object, **kwargs: object) -> None:
        onexc = kwargs.get("onexc")
        assert callable(onexc)
        onexc(os.unlink, str(victim), PermissionError(13, "Permission denied"))

    monkeypatch.setattr(review_worktree.shutil, "rmtree", _fake_rmtree)
    assert review_worktree._force_delete_tree(target, tmp_path) == []  # pyright: ignore[reportPrivateUsage] - the retry policy under test lives here
    assert not victim.exists()


def test_every_git_invocation_carries_the_long_path_flag(
    repo: Path, scratch: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The measurement behind the flag is recorded once, in ADR-0068 §4; a
    third near-verbatim copy of it here had already drifted from the other two.

    So the assertion is on **every** invocation, not the ones believed to need
    it: scoping the flag to the removal is exactly how the add went uncovered,
    and an `add` that fails this way aborts setup and drops the session to the
    unisolated fallback where test-reviewer cannot mutate at all.

    Intercepted at `subprocess.Popen`, not at the module's own argv builder: a
    builder-level assertion only sees calls that already went through the
    builder, so a spawn that bypasses it — a hardcoded `"git"` somewhere — is
    invisible to it. Matching on the executable's basename catches that too.
    """
    real_popen: Callable[..., Any] = subprocess.Popen
    argvs: list[list[str]] = []
    timeouts: list[object] = []

    class _Recording:
        """Wraps the real Popen so the wall-clock bound `_spawn` waits with
        is observable — `_spawn` applies it at `wait`, not at spawn time."""

        def __init__(self, inner: subprocess.Popen[bytes]) -> None:
            self._inner = inner
            self.stdout = inner.stdout
            self.stderr = inner.stderr
            self.pid = inner.pid

        def wait(self, timeout: float | None = None) -> int:
            timeouts.append(timeout)
            return self._inner.wait(timeout)

        def kill(self) -> None:
            self._inner.kill()

    def _recording_popen(*args: Any, **kwargs: Any) -> Any:
        argv = args[0] if args else kwargs.get("args")
        # A string command (a `shell=True` spawn) bypasses the builder just as
        # a hardcoded list does, and recording only lists would let that shape
        # through — so normalize both into a token list before matching.
        tokens = (
            argv.split()
            if isinstance(argv, str)
            else [str(part) for part in cast("list[Any]", argv)]
            if isinstance(argv, list) and argv
            else []
        )
        inner = cast("subprocess.Popen[bytes]", real_popen(*args, **kwargs))
        if tokens and Path(tokens[0].strip("\"'")).stem.lower() == "git":
            argvs.append(tokens)
            return _Recording(inner)
        return inner

    monkeypatch.setattr(review_worktree.subprocess, "Popen", _recording_popen)
    (repo / "tracked.txt").write_text("edit\n", encoding="utf-8")
    assert _setup(scratch) == 0
    assert main(["teardown", "--scratch", str(scratch)]) == 0

    assert argvs, "no git invocation was observed"
    assert any("add" in argv for argv in argvs), "the worktree add was not observed"
    for argv in argvs:
        assert "-c" in argv, argv
        assert "core.longpaths=true" in argv, argv
    # The same "every invocation, not the ones believed to need it" claim
    # applies to the timeout, and it was unpinned: deleting
    # `timeout=_COMMAND_TIMEOUT` left the whole suite green while restoring the
    # hang the constant exists to bound — a hung git child (a post-checkout
    # hook, fsmonitor) blocking the launcher, and with it the review round.
    # This asserts only that the bound is APPLIED to every spawn; that it
    # actually bounds wall-clock is a separate claim the stdlib call failed,
    # and `test_a_straggler_holding_the_pipes_cannot_outlast_the_timeout`
    # is what pins it.
    assert timeouts, "no git spawn was waited on"
    for timeout in timeouts:
        assert timeout == review_worktree._COMMAND_TIMEOUT, timeouts  # pyright: ignore[reportPrivateUsage] - the constant is the contract under test


def test_case_variant_agent_names_are_refused_as_duplicates() -> None:
    """They pass a case-sensitive duplicate check and then share one
    `venv-<agent>` dir on Windows and macOS, so both reviewers export the same
    UV_PROJECT_ENVIRONMENT and their concurrent uv builds race in it."""
    with pytest.raises(review_worktree.AbortError, match="duplicates"):
        review_worktree.parse_agents("Reviewer,reviewer")
    assert review_worktree.parse_agents("spec-reviewer,test-reviewer") == [
        "spec-reviewer",
        "test-reviewer",
    ]


def test_the_manifest_names_untracked_files_as_in_scope(
    repo: Path, scratch: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A change made purely of new files leaves the tracked tree clean, so the
    snapshot is HEAD — but those untracked files ARE the review. A fixed
    "reviewing branch diff only" would tell a reviewer relaying this manifest
    that the files listed directly beneath it are out of scope, which is the
    never-`git add`-ed blind spot the copy-in exists to close."""
    (repo / "brand-new.txt").write_text("untracked\n", encoding="utf-8")
    assert _setup(scratch) == 0
    out = capsys.readouterr().out
    snapshot_line = next(
        line for line in out.splitlines() if line.startswith("snapshot:")
    )
    assert "1 untracked file(s)" in snapshot_line
    assert "branch diff" not in snapshot_line  # the branch carries none
    # HEAD is a reachable commit, so the ephemeral-SHA caveat does not apply.
    assert "ephemeral by design" not in out
    assert main(["teardown", "--scratch", str(scratch)]) == 0


def test_a_state_file_without_the_worktrees_key_aborts(
    repo: Path, scratch: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`worktrees` is required where `venvs` is optional, and the asymmetry is
    load-bearing: defaulting it to empty would let a truncated state file tear
    down nothing, report success, and delete the only record of the worktrees
    it had just orphaned."""
    scratch.mkdir(parents=True, exist_ok=True)
    (scratch / STATE_FILENAME).write_text(
        json.dumps({"root": str(repo), "venvs": {}}), encoding="utf-8"
    )
    assert main(["teardown", "--scratch", str(scratch)]) == 1
    assert "malformed (worktrees)" in capsys.readouterr().err
    assert (scratch / STATE_FILENAME).exists()  # kept so a retry can act on it


def test_a_state_file_predating_the_venvs_key_still_tears_down(
    repo: Path, scratch: Path
) -> None:
    """The mirror of the above. This script gets edited while worktrees exist —
    it did, repeatedly, during its own development — so a state file written
    before `venvs` existed must still tear down rather than abort on it."""
    (repo / "tracked.txt").write_text("edit\n", encoding="utf-8")
    assert _setup(scratch) == 0
    state = _state(scratch)
    del state["venvs"]
    (scratch / STATE_FILENAME).write_text(json.dumps(state), encoding="utf-8")
    assert main(["teardown", "--scratch", str(scratch)]) == 0
    assert "wt-" not in _git(repo, "worktree", "list", "--porcelain")


def test_setup_outside_a_git_repository_aborts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`repo_root()`'s guard — the one abort in the module that no other test
    reaches. Deliberately skips the `repo` fixture: the condition under test is
    a cwd that is not inside any repository at all."""
    outside = tmp_path / "not-a-repo"
    outside.mkdir()
    if _git_raw(outside, "rev-parse", "--show-toplevel").returncode == 0:
        pytest.skip("the temp dir is itself inside a git repository")
    monkeypatch.chdir(outside)
    assert main(["setup", "--scratch", str(tmp_path / "scratch")]) == 1
    err = capsys.readouterr().err
    assert "not inside a git repository" in err
    assert "Traceback" not in err


def test_the_warning_list_cap_truncates_and_counts_the_remainder() -> None:
    """The cap arithmetic, plus the call-site count the helper's name asserts.

    Redaction used to be a per-guard convention with an unsafe default: the
    helper was called `_capped`, printing names was simply what it did, and
    seven guard blocks each decided count-vs-names independently under its own
    paragraph of reasoning. Two leaked in separate rounds, each after the
    discipline had been declared uniform — the tracked-`specs/personal/` abort
    printed the filenames it found, and the copy-time containment abort named
    a path whose leaf components come from the far side of a link.

    The default is now inverted: the helper's name states its precondition, so
    a call site is a claim rather than a formatting choice. The count below is
    what stops that claim widening again. The untracked-symlink warning and
    its symlink-only abort were the other two callers and both now print a
    bare count, because an untracked link's leaf name is chosen by whoever
    created it — `<provider>-2026-panel -> …` — which CLAUDE.md classifies as
    personal with no values attached.

    Derived with `ast` rather than a substring count, which would also match
    the definition, the docstring references and this test's own prose. The
    previous version of this docstring listed a "skip-worktree warning" among
    the callers; there has never been one.
    """
    cap = review_worktree.WARN_LIST_CAP
    capped = review_worktree._capped_after_personal_filters  # pyright: ignore[reportPrivateUsage] - the cap arithmetic under test lives here
    assert capped(["a", "b"]) == "a, b"
    exactly_at_cap = [str(i) for i in range(cap)]
    assert capped(exactly_at_cap) == ", ".join(exactly_at_cap)
    assert capped([str(i) for i in range(cap + 3)]) == (
        ", ".join(exactly_at_cap) + ", +3 more"
    )

    source = Path(review_worktree.__file__).read_text(encoding="utf-8")
    callers = [
        node
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "_capped_after_personal_filters"
    ]
    assert len(callers) == 1, (
        f"{len(callers)} sites print real path names into stderr, the "
        "transcript, relayed reports and the state file; each is a claim that "
        "every personal-data guard has already run, and the helper's docstring "
        "names the one site entitled to make it"
    )


def test_the_venv_deleter_has_no_git_refusal_in_front_of_it(
    repo: Path, scratch: Path, tmp_path: Path
) -> None:
    """Why the containment properties above are parametrized over both keys.

    The three containment tests near `_RECORDED_KEYS` now cover the `venvs`
    half by parametrization rather than by a hand-copied twin. What is worth
    stating once, and does not parametrize, is *why* the venv half is the
    harsher one: `_remove_plain_dir` goes straight to a `shutil.rmtree` with a
    chmod-and-retry, with no `git worktree remove` ahead of it to refuse an
    unregistered path. A state file naming the repository under `venvs` would
    force-delete the repository, and nothing but `_contained` and
    `_force_delete_tree` stands in the way.
    """
    victim = tmp_path / "victim"
    victim.mkdir()
    (victim / "precious.txt").write_text("do not delete\n", encoding="utf-8")
    scratch.mkdir(parents=True, exist_ok=True)
    # Unregistered, so git would never have been consulted about it anyway.
    assert victim.resolve() not in review_worktree._registered_paths(repo)  # pyright: ignore[reportPrivateUsage]
    error = review_worktree._remove_plain_dir(str(victim), scratch)  # pyright: ignore[reportPrivateUsage]
    assert error is not None
    assert "not strictly inside" in error
    assert (victim / "precious.txt").read_text(encoding="utf-8") == "do not delete\n"


def test_a_relative_scratch_path_is_refused(repo: Path) -> None:
    """Resolved against the invocation cwd, and cwd persists across Bash
    calls — so `setup --scratch tmp/x` then a `cd` then `teardown --scratch
    tmp/x` looks somewhere else, finds no state file, and exits **0**
    ("nothing recorded to tear down") while both worktrees stay registered
    and the only record of them is left where nothing will look again.
    Teardown records `root` precisely so it is cwd-independent; the path used
    to find that record has to be too."""
    for command in ("setup", "teardown"):
        assert main([command, "--scratch", "relative-scratch"]) == 1


def test_untracked_personal_files_abort_before_any_worktree(
    repo: Path, scratch: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`personal_tracked` proves nothing there is *tracked*; containment by
    construction also needs it *ignored*, and only that keeps it out of the
    manifest. With the .gitignore rule gone the directory is ordinary
    untracked content — replicated into every agent-readable worktree,
    printed into the transcript, persisted in the state file."""
    personal = repo / "specs" / "personal"
    personal.mkdir(parents=True)
    (personal / "notes.md").write_text("synthetic\n", encoding="utf-8")
    assert _setup(scratch) == 1
    err = capsys.readouterr().err
    assert "specs/personal/" in err
    assert "notes.md" not in err  # the count, never the names
    assert not (scratch / STATE_FILENAME).exists()
    assert "wt-" not in _git(repo, "worktree", "list", "--porcelain")


def test_the_machine_local_ignore_warning_carries_no_paths(
    repo: Path, scratch: Path
) -> None:
    """A bare count — not filenames, and not their directories either.
    Someone hiding personal material via .git/info/exclude almost always
    hides a *directory*, and CLAUDE.md is explicit that provenance ("which
    lab, which panel, in what order") is personal with no values attached.
    The warning is relayed into reviewer reports and persisted in the state
    file, so a provider-named parent directory is the leak."""
    hidden_dir = repo / "acme-labs-exports"
    hidden_dir.mkdir()
    (hidden_dir / "panel.pdf").write_text("synthetic\n", encoding="utf-8")
    (repo / ".git" / "info" / "exclude").write_text(
        "acme-labs-exports/\n", encoding="utf-8"
    )
    (repo / "tracked.txt").write_text("edit\n", encoding="utf-8")
    assert _setup(scratch) == 0
    warning = next(w for w in _warnings(scratch) if "machine-local" in w)
    assert "1 untracked file(s)" in warning
    assert "acme-labs-exports" not in warning
    assert "panel.pdf" not in warning
    assert main(["teardown", "--scratch", str(scratch)]) == 0


def test_an_untracked_nested_repo_aborts_with_a_remedy(
    repo: Path, scratch: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """git stops at a nested-repository boundary and reports the *directory*.
    `shutil.copy2` on it raises a bare IsADirectoryError (errno 13 "Permission
    denied" on Windows) naming neither cause nor remedy — and the triage in
    reviewer-isolation.md files a copy OSError as retryable, so the operator
    retries it forever."""
    nested = repo / "vendor" / "dep"
    nested.mkdir(parents=True)
    _git(nested, "init", "-b", "main")
    assert _setup(scratch) == 1
    err = capsys.readouterr().err
    assert "vendor/dep" in err
    assert "nested repository" in err
    assert "Traceback" not in err
    assert not (scratch / STATE_FILENAME).exists()


def test_a_worktree_path_with_a_newline_is_not_lost_by_the_stray_check(
    repo: Path, scratch: Path, tmp_path: Path
) -> None:
    """`git worktree list --porcelain` emits paths raw — no C-quoting, even
    at the default core.quotePath — and `str.splitlines()` splits on eight
    more characters than `\n`. A truncated key matches no recorded path, so
    the fail-loud stray check finds zero strays and teardown reports success
    over a live registration. `-z` plus a byte-safe split is what keeps the
    check able to fire at all."""
    if os.name == "nt":
        pytest.skip("Windows filenames cannot contain a newline")
    odd = tmp_path / "wt\nnewline"
    _git(repo, "worktree", "add", "--detach", str(odd), "HEAD")
    registered = review_worktree._registered_paths(repo)  # pyright: ignore[reportPrivateUsage] - the parser under test
    assert odd.resolve() in registered


def test_fidelity_warnings_reach_stderr_on_the_ready_path(
    repo: Path, scratch: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The three existing warning assertions all read the state file, and the
    one stderr assertion covers the *exit-2* print — so the exit-0 relay was
    unpinned, and deleting it left the suite green. Those lines are the only
    channel by which a reviewer ever sees a fidelity warning on the ready
    path, which reviewer-isolation.md's "Relay the warnings into your own
    report" depends on."""
    _git(repo, "update-index", "--skip-worktree", "tracked.txt")
    (repo / "brand-new.txt").write_text("untracked\n", encoding="utf-8")
    assert _setup(scratch) == 0
    err = capsys.readouterr().err
    assert "skip-worktree/assume-unchanged" in err
    assert main(["teardown", "--scratch", str(scratch)]) == 0


def test_fidelity_warnings_survive_an_abort(
    repo: Path,
    scratch: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    git_double: _GitDouble,
) -> None:
    """Warnings are computed before materialization but were printed only on
    the exit-0 and exit-2 paths, so any abort after that point dropped them —
    and a successful rollback deletes the state file billed as their durable
    copy. The skip-worktree warning is the one signal that a genuinely-changed
    tree can look clean, so losing it on an abort loses it when it matters."""
    _git(repo, "update-index", "--skip-worktree", "tracked.txt")
    (repo / "brand-new.txt").write_text("untracked\n", encoding="utf-8")

    def _add_fails(root: Path, *args: str) -> subprocess.CompletedProcess[str] | None:
        if _subcommand(args) == ("worktree", "add"):
            return _git_failure(args, "boom\n")
        return None

    git_double.install(_add_fails)
    assert _setup(scratch) == 1
    err = capsys.readouterr().err
    assert "skip-worktree/assume-unchanged" in err
    assert not (scratch / STATE_FILENAME).exists()


def test_teardown_unwedges_a_recorded_venv_path_that_is_a_file(
    repo: Path, scratch: Path
) -> None:
    """`shutil.rmtree` on a regular file hands `onexc` a NotADirectoryError
    for both os.scandir and os.rmdir, deletes nothing, and leaves teardown
    returning 1 on every retry forever — with every later setup then refused
    by the state-file guard."""
    scratch.mkdir(parents=True, exist_ok=True)
    stray = scratch / "venv-spec-reviewer"
    stray.write_text("not a directory\n", encoding="utf-8")
    state = {
        "root": str(repo),
        "worktrees": _inert_worktrees(scratch),
        "venvs": {"x": str(stray)},
    }
    (scratch / STATE_FILENAME).write_text(json.dumps(state), encoding="utf-8")
    assert main(["teardown", "--scratch", str(scratch)]) == 0
    assert not stray.exists()
    assert not (scratch / STATE_FILENAME).exists()


def test_teardown_aborts_with_guidance_when_the_recorded_root_lost_its_git_dir(
    repo: Path, scratch: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A repo re-cloned in place passes an `exists()` check and then fails
    every git call, where the first AbortError discards the accumulated error
    list and returns 1 on every retry forever. It deserves the same guided
    escape the missing-root case gets."""
    hollow = tmp_path / "hollow"
    hollow.mkdir()
    scratch.mkdir(parents=True, exist_ok=True)
    state: dict[str, object] = {"root": str(hollow), "worktrees": {}, "venvs": {}}
    (scratch / STATE_FILENAME).write_text(json.dumps(state), encoding="utf-8")
    assert main(["teardown", "--scratch", str(scratch)]) == 1
    err = capsys.readouterr().err
    assert "no longer a git repo" in err
    assert "by hand" in err
    assert (scratch / STATE_FILENAME).exists()


def test_a_failing_unwind_does_not_replace_the_original_abort(
    repo: Path,
    scratch: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    git_double: _GitDouble,
) -> None:
    """The unwind runs git of its own, and that git can fail independently —
    index.lock held by a concurrent process, an fsmonitor hiccup, a timeout.
    Unguarded, that exception escapes the handler: the state-file decision is
    skipped, the `raise` never runs, the real cause is lost to `__context__`,
    and the operator is told the unwind's error as though it were the cause —
    which reads as "any git failure" and routes them to the degraded fallback
    on a wrong diagnosis."""
    (repo / "tracked.txt").write_text("edit\n", encoding="utf-8")
    real_git_z = review_worktree._git_z  # pyright: ignore[reportPrivateUsage] - the unwind's own git runs through here

    def _add_fails(root: Path, *args: str) -> subprocess.CompletedProcess[str] | None:
        if _subcommand(args) == ("worktree", "add"):
            return _git_failure(args, "the original cause\n")
        return None

    def _unwind_fails(root: Path, *args: str, **kwargs: Any) -> list[str]:
        # `_cleanup_worktrees` reaches git through `_git_z`, not `_git` — the
        # stray check is a NUL-delimited query. Doubling the wrong seam leaves
        # the unwind succeeding and this test proving nothing.
        #
        # `**kwargs` because `_git_z` takes keyword-only options (`errors=`)
        # that individual call sites override: a double that fixes the
        # signature turns a caller's new keyword into a TypeError raised from
        # inside the double, which is a failure of the test rig masquerading
        # as a failure of the code under test.
        if _subcommand(args) == ("worktree", "list"):
            raise review_worktree.AbortError("index.lock held")
        return real_git_z(root, *args, **kwargs)

    git_double.install(_add_fails)
    monkeypatch.setattr(review_worktree, "_git_z", _unwind_fails)
    assert _setup(scratch) == 1
    err = capsys.readouterr().err
    assert "the original cause" in err  # the abort that actually happened
    assert "the unwind itself failed" in err  # named, not swallowed
    assert "state file kept" in err  # so a teardown retry can act
    assert "Traceback" not in err
    assert (scratch / STATE_FILENAME).exists()


def test_divergence_reads_paths_unmangled_and_the_abort_redacts_them(
    repo: Path, scratch: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Two properties that used to be pinned as one, wrongly.

    Without `-z`, git honors the default `core.quotePath` and octal-escapes
    any non-ASCII path. That must not happen, because the escaped form names
    no file — but the assertion for it belonged on the *return value*, not on
    the abort message: it was written as `assert "caf\\303\\251" not in err`,
    and Python parses `\\303\\251` as octal character escapes, so the literal
    evaluated to `cafÃ©` rather than git's backslash-3-0-3 output. It could
    never fire for the regression it named. Asserting equality against the
    real name here is the form that can.

    The abort message itself must NOT carry the path, which is why the old
    single assertion could not survive: `git rm --cached specs/personal/<f>`
    is the standard remediation for the state `personal_tracked` catches, and
    it moves the path out of the index and into exactly this abort — so the
    one guard that named its findings was the one reachable with a
    provider-named file, while its three siblings all redact to a count.
    """
    unicode_name = "café.txt"
    (repo / unicode_name).write_text("staged\n", encoding="utf-8")
    _git(repo, "add", unicode_name)
    (repo / unicode_name).write_text("then edited\n", encoding="utf-8")

    # The `-z` property, asserted where `-z` is: equality against the real
    # name, which the octal-escaped spelling cannot satisfy.
    assert review_worktree.index_worktree_divergence(repo) == [unicode_name]

    assert _setup(scratch) == 1
    err = capsys.readouterr().err
    assert "staged index differs from the worktree for 1 path(s)" in err
    assert unicode_name not in err
    assert "café" not in err
    # The remedy has to survive the redaction: without a path to paste, the
    # operator needs the command that lists them.
    assert "git status --short" in err
    assert not (scratch / STATE_FILENAME).exists()


def test_the_base_ref_abort_names_which_of_the_two_causes_occurred(
    repo: Path, scratch: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`branch_has_diff` returns None for two different conditions, and the
    triage in reviewer-isolation.md routes them oppositely: an unresolvable
    ref is fixed by fetching, while a resolvable ref sharing no merge base is
    blocking and fetching will not help. The operator can only follow that
    triage if the message says which one happened, so exit-code-only
    assertions are not enough here."""
    assert main(["setup", "--scratch", str(scratch), "--base", "no-such-ref"]) == 1
    missing = capsys.readouterr().err
    assert "does not resolve" in missing
    assert "shares no merge base" not in missing

    _git(repo, "checkout", "--orphan", "unrelated")
    _git(repo, "commit", "--allow-empty", "-m", "orphan root")
    _git(repo, "checkout", "main")
    assert main(["setup", "--scratch", str(scratch), "--base", "unrelated"]) == 1
    orphan = capsys.readouterr().err
    assert "shares no merge base" in orphan
    assert "not fixed by fetching" in orphan


def test_the_personal_guard_is_case_folded(
    repo: Path, scratch: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """git preserves on-disk casing, and on the case-insensitive filesystems
    this project runs on (Windows, macOS) `Specs/Personal/` is the very same
    directory the containment rule names. A case-sensitive check would pass it
    through — on the one guard whose miss means replicating health data into
    two agent-readable worktrees."""
    personal = repo / "Specs" / "Personal"
    personal.mkdir(parents=True)
    (personal / "notes.md").write_text("synthetic\n", encoding="utf-8")
    assert _setup(scratch) == 1
    err = capsys.readouterr().err
    assert "specs/personal/" in err
    assert "notes.md" not in err
    assert not (scratch / STATE_FILENAME).exists()


def test_the_tracked_personal_guard_is_case_folded_too(
    repo: Path, scratch: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The untracked guard's twin, which had the hazard fixed on one side only.

    git's pathspec matching is case-sensitive even where the filesystem is
    not, so a plain `ls-files -- specs/personal/` returns nothing for a
    force-added `Specs/Personal/labs.md` — measured, with no lowercase entry
    already in the index to case-correct it against. Setup then proceeded and
    `git worktree add` checked real health data out into BOTH agent-readable
    worktrees: the single precondition ADR-0068 calls load-bearing, defeated
    by casing alone.
    """
    personal = repo / "Specs" / "Personal"
    personal.mkdir(parents=True)
    (personal / "labs.md").write_text("synthetic\n", encoding="utf-8")
    _git(repo, "add", "-f", "Specs/Personal/labs.md")
    assert "Specs/Personal/labs.md" in _git(repo, "ls-files")
    assert _setup(scratch) == 1
    err = capsys.readouterr().err
    assert "tracked file(s)" in err
    assert not (scratch / STATE_FILENAME).exists()
    # Redacted like every sibling guard: a provider- or panel-named file under
    # specs/personal/ is provenance under CLAUDE.md even with no values in it,
    # and this message reaches stderr, the transcript and the relayed manifest.
    assert "labs.md" not in err


def test_a_staged_deletion_of_a_present_file_aborts_as_divergence(
    repo: Path, scratch: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`git rm --cached` drops the path from the index entirely, so the
    staged-vs-unstaged intersection cannot see it: `git diff --name-only`
    stops reporting the path and `ls-files --others` starts. Measured, the
    snapshot tree AND the untracked copy-in then both carry the file, every
    reviewer sees it present and unremarkable, and the plain `git commit`
    that follows publishes its deletion — a pass earned for a state that
    cannot be the one that lands, which is what this abort exists to stop."""
    _git(repo, "rm", "--cached", "tracked.txt")
    assert (repo / "tracked.txt").exists()
    assert "tracked.txt" in _git(repo, "ls-files", "--others", "--exclude-standard")
    assert _setup(scratch) == 1
    err = capsys.readouterr().err
    assert "staged index differs from the worktree" in err
    # Redacted to a count, like the three sibling personal-data guards. This
    # assertion was `"tracked.txt" in err` — pinning the leak as intended
    # behavior on the one abort `git rm --cached specs/personal/<file>`
    # routes a personal filename into.
    assert "tracked.txt" not in err
    assert "1 path(s)" in err
    assert not (scratch / STATE_FILENAME).exists()


def test_a_staged_deletion_of_an_absent_file_is_not_divergence(
    repo: Path, scratch: Path
) -> None:
    """The complement, so the guard above cannot be satisfied by refusing
    every staged deletion: with the file gone from disk too, index and
    worktree agree and an ordinary staged `git rm` must stay reviewable."""
    _git(repo, "rm", "tracked.txt")
    assert not (repo / "tracked.txt").exists()
    assert _setup(scratch) == 0
    assert main(["teardown", "--scratch", str(scratch)]) == 0


@pytest.mark.skipif(
    os.name == "nt", reason="NTFS refuses a non-UTF-8 name (WSL/CI legs run this)"
)
def test_a_staged_deletion_of_a_non_utf8_named_file_is_still_divergence(
    repo: Path, scratch: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The divergence abort must not be blind to a name git emits in raw bytes.

    `_exists_cased` compares git's spelling against `os.scandir` names, and
    the two decode an undecodable byte differently: scandir uses
    surrogateescape (PEP 383) while `_git_z`'s default "replace" maps every
    such byte to U+FFFD. Measured: `b"a\\xffb"` is `"a\\ufffdb"` on the git
    side and `"a\\udcffb"` on the scandir side, never equal — so every
    component after the bad byte answered False, `still_present` came back
    empty, and the abort in
    `test_a_staged_deletion_of_a_present_file_aborts_as_divergence` silently
    did not fire for this file. Nothing downstream backstops it: unlike the
    containment guards, there is no artifact walk for a staged deletion.

    Written with raw bytes rather than a str so the name really is
    undecodable — a `str` filename would round-trip through the filesystem
    encoding and prove nothing.
    """
    name = b"panel-\xff-2026.md"
    path = os.path.join(os.fsencode(str(repo)), name)
    with open(path, "wb") as handle:
        handle.write(b"synthetic\n")
    _git(repo, "add", "--", os.fsdecode(path))
    _git(repo, "commit", "-m", "add a file whose name is not utf-8")
    _git(repo, "rm", "--cached", "--", os.fsdecode(path))
    assert os.path.lexists(path), "the file must still be on disk"

    assert _setup(scratch) == 1, "a staged deletion of a present file was not seen"
    err = capsys.readouterr().err
    assert "staged index differs from the worktree" in err
    assert "1 path(s)" in err
    assert not (scratch / STATE_FILENAME).exists()


def test_the_retryable_deleter_set_matches_the_convention_it_documents() -> None:
    """`_RETRYABLE_DELETERS`' 9-line justification rests on **undocumented**
    CPython internals — `shutil.rmtree`'s `onexc` calling convention — and no
    test named itself as its pin.

    The comment asserts specific claims about which functions rmtree hands
    back: that the Windows walker also passes `os.lstat` / `os.path.islink` /
    `os.scandir`, that the POSIX fd walker adds `os.open` / `os.close`, that
    the first three "succeed without deleting anything" if retried, and that
    the last two raise TypeError on a lone str argument — which neither
    `_onexc` nor `main`'s `except (AbortError, OSError)` would catch, so
    teardown would die by traceback. Unlike this module's other unusual
    branches, nothing went red if a future CPython changed the convention:
    `_onexc` would keep running while the reasoning behind which functions
    are safe to retry went stale silently.

    What is checkable without pinning an implementation detail: the set holds
    exactly the deleters, every member is a real callable from `os`, and none
    of the functions the comment names as *unsafe* to retry has crept in.
    """
    retryable = review_worktree._RETRYABLE_DELETERS  # pyright: ignore[reportPrivateUsage] - the constant is the contract under test
    assert set(retryable) == {os.unlink, os.remove, os.rmdir}
    # The comment's own unsafe list. `os.scandir` in particular hands back an
    # unclosed handle that can itself block the later rmdir.
    unsafe = [os.lstat, os.path.islink, os.scandir, os.open, os.close]
    for func in unsafe:
        assert func not in retryable, (
            f"{func.__name__} is documented as unsafe to retry"
        )
    # And the claim that motivates excluding `os.open`/`os.close`: they take
    # more than a lone str, so retrying one raises TypeError rather than
    # OSError — an exception no clause on this path owns. Asserted rather
    # than trusted, since it is the one half of the comment that is about
    # observable behaviour rather than about rmtree's internals.
    for func in (os.open, os.close):
        with pytest.raises(TypeError):
            func("a-lone-str")  # pyright: ignore[reportArgumentType, reportCallIssue] - the wrong-arity call IS the assertion


_OS_PATH_GATED = frozenset({"exists", "is_file", "is_dir", "unlink", "rmdir"})
_OS_PATH_WRAPPERS = frozenset({"_exists", "_is_file", "_is_dir", "_unlink", "_rmdir"})


def test_no_filesystem_probe_bypasses_the_os_path_wrappers() -> None:
    """`_os_path` coverage must be structural, not remembered.

    Past MAX_PATH these methods do not raise — `Path.exists()` and
    `Path.is_file()` delegate to `os.path.*`, which answer **False**. A deep
    recorded venv path that is a *file* therefore skipped the unlink branch,
    reached `shutil.rmtree`, got a NotADirectoryError for both `os.scandir`
    and `os.rmdir` (neither retryable), deleted nothing, and left teardown
    returning 1 on every retry with the state file never unlinked — so every
    later setup was refused. `_force_delete_tree`'s docstring already stated
    the rule ("Every probe goes through `_os_path`, not only the `rmtree`")
    while three sibling probes in the two removers read the bare `Path` and
    one line applied the prefix *twice*. A rule stated in a docstring and
    contradicted five lines away is what this replaces.

    AST rather than grep, per the same rule the brief applies to counts: a
    regex cannot tell `located.unlink()` from `_os_path(located).unlink()`
    across a line break, and a miscounted gate is worse than none.

    Deliberately not a check that the *right* path is passed — that is a
    correctness question no gate can answer. It checks only that the
    hardening is reachable, which is the property that was silently false.
    """
    module = Path(review_worktree.__file__)
    tree = ast.parse(module.read_text(encoding="utf-8"))
    # **No exemption for the wrapper bodies.** An earlier version of this gate
    # skipped every line inside the five wrappers, which exempted the one
    # place the rule most needs to hold: rewriting `_exists` to
    # `return target.exists()` bypasses `_os_path` for every caller at once,
    # and the gate stayed green. The wrappers satisfy the rule as written —
    # their receiver *is* an `_os_path(...)` call — so the exemption bought
    # nothing and hid the widest regression available.
    present = {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name in _OS_PATH_WRAPPERS
    }
    assert present == _OS_PATH_WRAPPERS, (
        f"wrapper(s) renamed or removed: {sorted(_OS_PATH_WRAPPERS - present)} — "
        "callers would then reach the bare Path methods under another name"
    )

    offenders: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr not in _OS_PATH_GATED:
            continue
        receiver = node.func.value
        through = (
            isinstance(receiver, ast.Call)
            and isinstance(receiver.func, ast.Name)
            and receiver.func.id == "_os_path"
        )
        if not through:
            offenders.append(f"{module.name}:{node.lineno} .{node.func.attr}()")
    assert not offenders, (
        "these filesystem probes bypass `_os_path`, so they answer False "
        f"rather than raising past MAX_PATH: {offenders}"
    )


def test_setup_issues_the_keychain_belt_alongside_the_venv_path(
    repo: Path, scratch: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The one belt layer outside the tree the reviewer mutates had no
    mechanism — it was a sentence in `test-reviewer.md`.

    The other two layers (`conftest.py`'s autouse fixture and its import-time
    assignment) both live inside the tree test-reviewer is chartered to
    mutate, so the suite is hardened against its own reviewer inside the
    artifact that reviewer edits. Prose is the layer that loses: Bash
    environment is per-call exactly as cwd is, so an `export` in one call is
    gone by the next and neither absence announces itself, on a machine
    holding a real credential vault beside a real encrypted health database.

    Asserted against `conftest.py`'s own literal rather than a copy of the
    string, because two belts naming different backends is the drift this is
    supposed to remove.
    """
    (repo / "tracked.txt").write_text("edit\n", encoding="utf-8")
    assert _setup(scratch) == 0
    out = capsys.readouterr().out

    belt = os.environ["PYTHON_KEYRING_BACKEND"]
    assert belt == review_worktree._FAIL_BACKEND, (  # pyright: ignore[reportPrivateUsage] - the shared literal is the subject
        "the launcher's belt and conftest.py's have drifted apart"
    )
    env_lines = [line for line in out.splitlines() if line.startswith("env[")]
    assert len(env_lines) == 2, out
    for line in env_lines:
        assert f"PYTHON_KEYRING_BACKEND={belt}" in line, line
        assert "UV_PROJECT_ENVIRONMENT=" in line, line
    # One string per agent, so exporting the venv and forgetting the backend
    # is no longer the easy mistake.
    for agent, venv in (
        (agent, str(scratch / f"venv-{agent}"))
        for agent in ("spec-reviewer", "test-reviewer")
    ):
        assert f"env[{agent}]: UV_PROJECT_ENVIRONMENT={venv} " in out

    assert main(["teardown", "--scratch", str(scratch)]) == 0


def test_trailing_dot_agent_names_are_refused() -> None:
    """Win32 strips trailing dots from a path component, so `a` and `a.` pass
    the case-fold dedupe and then resolve to ONE `venv-<agent>` directory —
    both reviewers handed the same UV_PROJECT_ENVIRONMENT, their concurrent
    uv builds racing in it, which is bit-for-bit the failure the case-fold
    check exists to prevent, on the axis it does not cover."""
    with pytest.raises(review_worktree.AbortError, match="invalid agent name"):
        review_worktree.parse_agents("a,a.")
    # `...` is the same collision by another spelling: it would build `venv-`.
    with pytest.raises(review_worktree.AbortError, match="invalid agent name"):
        review_worktree.parse_agents("...")
    # Excluding the character subsumes the old literal `.`/`..` special case,
    # so that rejection must survive its removal.
    with pytest.raises(review_worktree.AbortError, match="invalid agent name"):
        review_worktree.parse_agents("..")


def test_teardown_refuses_a_scratch_dir_that_contains_the_repo(repo: Path) -> None:
    """Teardown is the half that deletes, and it applied neither of setup's
    two overlap guards. `_inside_scratch` is relative to `--scratch` alone, so
    with scratch an ancestor of the repo EVERY path in the repo is "inside
    scratch" and the force-delete guard covers the repository itself: a
    recorded venv path naming the repo root was force-deleted while teardown
    printed success (reproduced). The refusal must fire before anything goes.
    """
    outer = repo.parent
    precious = repo / "precious.txt"
    precious.write_text("hours of work\n", encoding="utf-8")
    state = {
        "root": str(repo),
        "worktrees": {},
        # An ancestor of the repo is what makes the containment check vacuous.
        "venvs": {"x": str(repo)},
    }
    (outer / STATE_FILENAME).write_text(json.dumps(state), encoding="utf-8")

    assert main(["teardown", "--scratch", str(outer)]) == 1
    assert precious.exists(), "teardown force-deleted inside the repository"
    assert (repo / ".git").exists()


def test_teardown_after_a_scratch_rename_deletes_the_trees_that_moved(
    repo: Path, scratch: Path
) -> None:
    """A renamed scratch dir must not turn teardown into a silent leak.

    git records absolute worktree paths at `add` time and has no idea a
    directory moved, so after a rename every recorded path is gone AND still
    registered. The first version of this tolerance de-registered and stopped
    there, justified — in the code and in reviewer-isolation.md alike — by
    "nothing is left on disk to protect". That was false: the directories
    moved, they did not disappear. Measured, teardown printed "removed 2
    worktree(s) and 2 venv dir(s); no strays registered", exited 0, unlinked
    the state file, and left two full copies of the repository — carrying the
    uncommitted work under review plus test-reviewer's mutation edits —
    permanently on disk with no record anywhere. That is exactly the "stale
    copy a later session could mistake for current state" the same document
    warns about.

    This test previously asserted de-registration and state-file removal and
    nothing else, which is what let the leak read as correct. A worktree's
    recorded basename carries a per-round random suffix, so looking for that
    name under the CURRENT scratch dir finds *this* round's tree and deletes
    it there.

    **Worktrees only.** The venv names are the fixed `venv-<agent>`, so the
    same lookup would match whatever round is running now — a stale state file
    torn down against a live scratch dir would force-delete a live reviewer's
    venv mid-review (pinned by
    `test_a_stale_venv_record_never_relocates_onto_a_live_rounds_venv`). This
    test therefore asserts a leftover venv, which is the deliberate cost: a
    rebuildable package cache under a scratch path, against a full copy of the
    repository carrying uncommitted work. The asymmetry is the decision, and
    asserting it here is what keeps a later "simplification" from restoring
    the symmetry and the hazard with it.
    """
    (repo / "tracked.txt").write_text("edit\n", encoding="utf-8")
    assert _setup(scratch) == 0
    # A venv dir the reviewer would have built, so the venv path is exercised
    # too — it took the same gone-and-outside branch and leaked the same way.
    (scratch / "venv-spec-reviewer").mkdir()
    (scratch / "venv-spec-reviewer" / "pyvenv.cfg").write_text("x\n", encoding="utf-8")
    renamed = scratch.with_name(scratch.name + "-moved")
    scratch.rename(renamed)
    assert "wt-" in _git(repo, "worktree", "list")
    assert [p.name for p in renamed.iterdir() if p.name.startswith("wt-")]

    assert main(["teardown", "--scratch", str(renamed)]) == 0
    assert "wt-" not in _git(repo, "worktree", "list"), "registration survived"
    assert not (renamed / STATE_FILENAME).exists(), "state file left wedging setup"
    leftovers = sorted(p.name for p in renamed.iterdir())
    assert not [name for name in leftovers if name.startswith("wt-")], (
        f"a relocated worktree survived teardown: {leftovers}"
    )
    assert leftovers == ["venv-spec-reviewer"], (
        "the venv should be the only leftover — worktrees relocate, venvs "
        f"deliberately do not; got {leftovers}"
    )


def test_teardown_unlinks_a_recorded_venv_symlink_and_never_its_target(
    repo: Path, scratch: Path
) -> None:
    """`Path.resolve()` follows a final symlink, which made the `is_symlink()`
    branch dead code AND aimed the delete at the link's target: a recorded
    path linking to a real tree inside scratch had that TREE deleted, the link
    survived dangling, and teardown reported "no strays" and unlinked the
    state file — the only record of the path it had not removed.

    Skips where `mklink /D` needs a privilege this host may not have, which is
    why the junction twin exists:
    `test_teardown_never_deletes_through_a_recorded_venv_link` asserts the
    same guarantee on both legs through `_link_dir`. Kept separately because
    `is_symlink()` and `is_junction()` are different predicates and `_is_link`
    has to answer for both."""
    scratch.mkdir(parents=True)
    real = scratch / "real-tree"
    real.mkdir()
    (real / "keep.txt").write_text("not this teardown's target\n", encoding="utf-8")
    link = scratch / "venv-spec-reviewer"
    try:
        link.symlink_to(real, target_is_directory=True)
    except (OSError, NotImplementedError):  # fmt: skip
        pytest.skip("symlink creation is not permitted on this host")
    state = {
        "root": str(repo),
        "worktrees": _inert_worktrees(scratch),
        "venvs": {"spec-reviewer": str(link)},
    }
    (scratch / STATE_FILENAME).write_text(json.dumps(state), encoding="utf-8")

    assert main(["teardown", "--scratch", str(scratch)]) == 0
    assert not link.is_symlink(), "the recorded link survived teardown"
    assert real.is_dir(), "teardown deleted through the link"
    assert (real / "keep.txt").exists()


def test_the_teardown_success_line_counts_the_venvs_it_deleted(
    repo: Path, scratch: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The one line an operator reads after a destructive subcommand omitted
    the destructive half — "removed 0 worktree(s)" for a run that recursively
    force-deleted directories — which is what made the missing containment
    guard silent.

    This test was named for the property and asserted its opposite. It records
    a worktree via `_inert_worktrees` that was **never created**, then required
    the line to say `removed 1 worktree(s)` — pinning a count of *records*, so
    the message could not distinguish a deletion from a no-op in exactly the
    direction the comment above it claims it exists to prevent. On every
    ordinary round that reads `removed 2 venv dir(s)` for directories setup
    never creates: setup prints the venv paths and leaves each reviewer to
    build one, and spec-reviewer is told the normal round builds nothing.

    Both halves are still always named — that part was right, and a zero is
    the informative case — but the counted thing is now what was on disk
    before the run and is gone after it.
    """
    scratch.mkdir(parents=True)
    venv = scratch / "venv-spec-reviewer"
    (venv / "Lib").mkdir(parents=True)
    state = {
        "root": str(repo),
        "worktrees": _inert_worktrees(scratch),
        "venvs": {"spec-reviewer": str(venv)},
    }
    (scratch / STATE_FILENAME).write_text(json.dumps(state), encoding="utf-8")

    assert main(["teardown", "--scratch", str(scratch)]) == 0
    assert not venv.exists()
    out = capsys.readouterr().out
    # The venv existed and is gone: a real deletion, counted as one.
    assert "1 of 1 recorded venv dir(s)" in out
    # The worktree never existed, so a truthful line says zero were removed
    # while still naming the record — the distinction the old assertion made
    # unavailable.
    assert "removed 0 of 1 recorded worktree(s)" in out


def test_the_teardown_success_line_counts_worktrees_it_really_deleted(
    repo: Path, scratch: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The positive complement of the test above, on the worktree half.

    That test only ever exercises the ZERO case for worktrees — its record
    names a directory setup never created — so an undercount would have gone
    unnoticed in the direction that matters. Measured: replacing the
    pre-removal `present` list with an unconditional `[]`, which reports zero
    real deletions however many worktrees were genuinely removed, left the
    whole file green. A count is only honest if both readings are pinned.
    """
    (repo / "tracked.txt").write_text("edit\n", encoding="utf-8")
    assert _setup(scratch) == 0
    assert len(list(scratch.glob("wt-*"))) == 2, "both worktrees must exist first"

    assert main(["teardown", "--scratch", str(scratch)]) == 0
    out = capsys.readouterr().out
    assert "removed 2 of 2 recorded worktree(s)" in out, out
    assert not list(scratch.glob("wt-*"))


def test_an_untracked_file_vanishing_mid_copy_warns_instead_of_aborting(
    repo: Path,
    scratch: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Untracked files are the most volatile things in a tree — editor swap
    files, build artifacts, test temp files — so a background build during the
    materialization window raised FileNotFoundError, rolled the whole round
    back, and sent the operator into a rerun that races identically. Dropped
    with a warning instead, exactly as an untracked symlink already is.

    The manifest must drop it too: a reviewer reconciling `??` entries against
    a manifest naming a file that is not in the worktree would read the
    launcher's own tolerance as a fidelity failure.
    """
    doomed = repo / "vanishes.tmp"
    doomed.write_text("a build artifact\n", encoding="utf-8")
    (repo / "survives.txt").write_text("real work\n", encoding="utf-8")
    real_copy = review_worktree._copy_untracked  # pyright: ignore[reportPrivateUsage] - the test doubles the module's own seam

    def _delete_then_copy(
        source: Path, worktree: Path, manifest: list[str]
    ) -> list[str]:
        # Races the copy exactly as an editor or a build would: the file is in
        # the manifest git already took, and gone by the time copy2 runs.
        doomed.unlink(missing_ok=True)
        return real_copy(source, worktree, manifest)

    monkeypatch.setattr(review_worktree, "_copy_untracked", _delete_then_copy)
    assert _setup(scratch) == 0

    captured = capsys.readouterr()
    assert "vanishes.tmp" in captured.err
    assert "vanished between the manifest and the copy" in captured.err
    assert "?? vanishes.tmp" not in captured.out, "manifest promised a missing file"
    assert "?? survives.txt" in captured.out
    for worktree in _worktrees(scratch).values():
        assert not (Path(worktree) / "vanishes.tmp").exists()
        assert (Path(worktree) / "survives.txt").exists()
    assert main(["teardown", "--scratch", str(scratch)]) == 0


def test_an_unborn_head_is_not_blamed_on_the_base_ref(
    repo: Path, scratch: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`git diff base...HEAD` exits 128 when EITHER side fails to resolve, and
    the discriminator probed only the base. On an orphan branch before its
    first commit the abort therefore read "shares no merge base with HEAD …
    not fixed by fetching" — a string reviewer-isolation.md files as
    **blocking**, so the operator abandoned isolation for the degraded
    sequential fallback when the fix was to make a commit."""
    _git(repo, "checkout", "--orphan", "fresh")
    assert _git_raw(repo, "rev-parse", "--verify", "HEAD").returncode != 0
    assert main(["setup", "--scratch", str(scratch), "--base", "base"]) == 1
    err = capsys.readouterr().err
    assert "HEAD does not resolve" in err
    assert "no commits yet" in err
    assert "shares no merge base" not in err, "blamed the base for an unborn HEAD"
    assert not (scratch / STATE_FILENAME).exists()


def test_a_timeout_abort_names_the_git_subcommand_that_hung(
    repo: Path,
    scratch: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`_run` sliced its message off the BUILT argv, where the builder puts
    global options first — so every timeout printed `git -C <root>` (or `git
    -c core.longpaths=true`) and never once named the subcommand. The triage
    in reviewer-isolation.md routes a hung git by *which* call hung, and an
    operator cannot tell a hung `stash create` from a hung `worktree add`."""
    (repo / "tracked.txt").write_text("edit\n", encoding="utf-8")
    _spawn_double(monkeypatch, match="stash", hang=True)
    assert _setup(scratch) == 1

    err = capsys.readouterr().err
    assert "did not return within" in err
    assert "git stash create" in err, err
    assert "Traceback" not in err


def test_a_linked_directory_in_the_tree_is_never_replicated(
    repo: Path, scratch: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The containment breach the untracked-symlink filter could not see.

    git walks *through* a directory junction and reports the files under it as
    ordinary untracked paths — `linked-dir/labs.md`, no marker of any kind —
    and `is_symlink()` answers False for a junction at both the link and the
    leaf. Measured before the fix: `copy2` replicated bytes from outside the
    repository into BOTH agent-readable worktrees, the manifest printed the
    path as in-scope repo content, the state file recorded it, setup exited 0
    and `warnings` was the empty list. This project runs on a machine that
    holds a real encrypted health database, so the content on the other side
    of such a link is unreviewed and of unknown sensitivity, and the guarantee
    it breaks is the by-construction containment claim ADR-0068 and
    specs/open-questions.md both rest on.

    Runs on every platform via `_link_dir` — the point being that the Windows
    form needs no privilege, so unlike every symlink test here this one does
    not skip on `windows-latest`.

    **The two platforms reach the same guarantee by different guards, and the
    assertions below say so rather than pinning one platform's mechanism as
    universal.** Windows git walks *through* a junction and reports the files
    beneath it as ordinary untracked paths, so only the resolved-path
    containment filter catches them; POSIX git never walks through a symlinked
    directory — it reports the link itself as one entry — so the leaf-level
    link filter has already caught it and the containment filter is defense in
    depth. Asserting the Windows message on both is what made the first
    version of this test pass on Windows and fail under WSL.
    """
    outside = tmp_path / "outside-the-repo"
    outside.mkdir()
    (outside / "labs.md").write_text("SYNTHETIC-NOT-REAL\n", encoding="utf-8")
    _link_dir(repo / "linked-dir", outside)
    # Something real to review, so the run proceeds rather than taking the
    # nothing-to-review path (covered separately below).
    (repo / "real.txt").write_text("in the tree\n", encoding="utf-8")

    assert _setup(scratch) == 0
    # One call: `readouterr()` drains the buffer, so a second one reads empty.
    captured = capsys.readouterr()
    out, err = captured.out, captured.err

    # The invariant, identical on both platforms: the operator is warned, and
    # nothing from the far side of the link is named. The leaf names there
    # come from OUTSIDE the repository, which is where personal material lives.
    assert "not replicated into worktrees" in err.lower()
    assert "labs.md" not in err
    assert "labs.md" not in out
    # The mechanism, which legitimately differs. Both spellings are counts:
    # the leaf name on either side of a link is operator-chosen, so no message
    # here may carry it (see the cap helper's call-site assertion).
    if os.name == "nt":
        assert "1 untracked path(s) resolve outside the repository" in err
    else:
        assert "1 untracked symlink(s) are not replicated" in err

    worktrees = [p for p in scratch.iterdir() if p.name.startswith("wt-")]
    assert len(worktrees) == 2
    for worktree in worktrees:
        assert not (worktree / "linked-dir").exists(), "outside content replicated"
        assert (worktree / "real.txt").exists(), "in-tree content was dropped too"
    state = json.loads((scratch / STATE_FILENAME).read_text(encoding="utf-8"))
    assert not any("linked-dir" in rel for rel in state["untracked"])
    assert main(["teardown", "--scratch", str(scratch)]) == 0


def test_a_tree_of_only_linked_content_is_not_reported_as_nothing_to_review(
    repo: Path, scratch: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The complement, and the reason the filter could not simply drop them.

    Dropping every escaping path empties the manifest, which walks straight
    into the exit-2 "nothing to review" gate — and exit 2 is the one code that
    sanctions skipping the reviewers. "Tree clean, no untracked files" would
    be a false statement about a tree that has untracked files, so this takes
    the same shape the symlink-only case beside it already had: abort, name
    the count, say what to do.

    Both platforms must refuse; which of the two aborts fires depends on
    whether git walked through the link (see the test above).
    """
    outside = tmp_path / "outside-the-repo"
    outside.mkdir()
    (outside / "notes.md").write_text("SYNTHETIC-NOT-REAL\n", encoding="utf-8")
    _link_dir(repo / "linked-dir", outside)

    assert _setup(scratch) == 1
    out, err = capsys.readouterr()
    # Never exit 2, and never the claim that there was nothing there.
    assert "nothing to review" not in out
    assert "notes.md" not in err
    if os.name == "nt":
        assert "resolve outside the repository" in err
    else:
        assert "the only uncommitted change is 1 untracked symlink(s)" in err
    assert not (scratch / STATE_FILENAME).exists()


def test_teardown_never_deletes_through_a_recorded_venv_link(
    repo: Path, scratch: Path, tmp_path: Path
) -> None:
    """The force-delete containment hole, on the link form Windows can create.

    `_remove_plain_dir` containment-checked the unresolved path and then
    force-deleted `located.resolve()`, which follows a junction straight out
    of the scratch dir. `is_symlink()` and `is_file()` are both False for a
    junction, so it never entered the unlink branch whose own comment claimed
    to cover it. Measured: a tree outside the scratch dir was recursively
    deleted, teardown printed "removed 0 worktree(s) and 1 venv dir(s); no
    strays registered", exited 0, unlinked the state file, and left the
    junction itself on disk — bit for bit the failure the docstring there
    claims to prevent, on the guard that already force-deleted this
    repository once.

    The link must be removed as *itself*, and its target must survive intact.
    """
    precious = tmp_path / "precious"
    (precious / "sub").mkdir(parents=True)
    (precious / "keep.txt").write_text("hours of work\n", encoding="utf-8")
    (precious / "sub" / "more.txt").write_text("more work\n", encoding="utf-8")
    scratch.mkdir(parents=True)
    link = scratch / "venv-spec-reviewer"
    _link_dir(link, precious)

    state = {
        "root": str(repo),
        "worktrees": _inert_worktrees(scratch),
        "venvs": {"spec-reviewer": str(link)},
    }
    (scratch / STATE_FILENAME).write_text(json.dumps(state), encoding="utf-8")

    assert main(["teardown", "--scratch", str(scratch)]) == 0
    assert precious.exists(), "teardown deleted through the link"
    assert (precious / "keep.txt").read_text(encoding="utf-8") == "hours of work\n"
    assert (precious / "sub" / "more.txt").exists()
    assert not link.exists(), "the link itself survived, dangling and unrecorded"
    assert not (scratch / STATE_FILENAME).exists()


def test_force_delete_refuses_a_link_rather_than_following_it(tmp_path: Path) -> None:
    """`_force_delete_tree`'s own last-line guard had the same blind spot.

    It is the backstop the caller above depends on, and it tested
    `is_symlink()` too — so both the guard and its backstop were open on
    junctions at once, which is how a "proven in three helpers" containment
    property proved nothing on the primary platform.
    """
    target = tmp_path / "target"
    target.mkdir()
    (target / "keep.txt").write_text("not this delete's target\n", encoding="utf-8")
    link = tmp_path / "link"
    _link_dir(link, target)

    failures = review_worktree._force_delete_tree(link, tmp_path)  # pyright: ignore[reportPrivateUsage]
    assert failures, "a link was accepted as a directory tree"
    assert "is a link" in failures[0]
    assert (target / "keep.txt").exists()
    assert link.exists(), "the refusal deleted it anyway"


def test_the_personal_guards_catch_a_bare_file_at_the_directory_path(
    repo: Path, scratch: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`.gitignore`'s rule is directory-only, so a FILE at that exact path is
    ordinary untracked content.

    `specs/personal/` with a trailing slash matches directories only —
    measured, `git check-ignore specs/personal` exits 1 for a plain file
    there. Both guards missed it in the same way: the untracked one tested
    `startswith("specs/personal/")`, which cannot match `specs/personal`, and
    the tracked one passed git the pathspec `:(icase)specs/personal/`, which
    does not match it either. Reproduced end to end — setup exited 0 and
    copied the file into both agent-readable worktrees.
    """
    (repo / "specs").mkdir()
    (repo / "specs" / "personal").write_text("SYNTHETIC-NOT-REAL\n", encoding="utf-8")

    assert _setup(scratch) == 1
    err = capsys.readouterr().err
    assert "specs/personal/" in err
    assert "no longer ignored" in err
    assert not (scratch / STATE_FILENAME).exists()

    # The tracked half of the same hole, on the same path.
    _git(repo, "add", "-f", "specs/personal")
    assert review_worktree.personal_tracked(repo) == ["specs/personal"]
    assert _setup(scratch) == 1
    assert "tracked file(s)" in capsys.readouterr().err


def test_the_personal_pathspec_does_not_over_match_a_sibling(repo: Path) -> None:
    """The bare-path fix widens a pathspec, so its blast radius needs pinning.

    Dropping the trailing slash makes `:(icase)specs/personal` match the file
    at that exact path — but git matches a slashless pathspec at directory
    boundaries, so it must NOT start matching `specs/personal-notes/`. Without
    this, a later "simplification" to a prefix test would refuse to snapshot
    any repo holding a similarly-named directory, and the abort it produces is
    one a reader has every reason to trust.
    """
    sibling = repo / "specs" / "personal-notes"
    sibling.mkdir(parents=True)
    (sibling / "x.md").write_text("public\n", encoding="utf-8")
    _git(repo, "add", "-f", "specs/personal-notes/x.md")

    assert review_worktree.personal_tracked(repo) == []
    assert not review_worktree._is_personal("specs/personal-notes/x.md")  # pyright: ignore[reportPrivateUsage]
    assert review_worktree._is_personal("specs/personal")  # pyright: ignore[reportPrivateUsage]
    assert review_worktree._is_personal("Specs/Personal/labs.md")  # pyright: ignore[reportPrivateUsage]


def test_setup_aborts_rather_than_reviewing_a_tree_where_everything_vanished(
    repo: Path,
    scratch: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The false pass that routed around exit 2 instead of taking it.

    The "nothing to review" gate runs before the vanished-file filter, so an
    untracked-files-only round whose files all vanish mid-copy passes the
    gate and is emptied afterwards. Measured: setup printed `snapshot: HEAD
    (reviewing )` — the join collapsing to the empty string — reported "0
    untracked files copied", and returned **0**. Per reviewer-isolation.md
    exit 0 means ready, so both reviewers launch against worktrees
    byte-identical to the base, both report clean, and /land counts that as a
    pass on the current diff. Exit 2 is the only code that sanctions skipping
    the reviewers; this produced something strictly worse than a skip.

    The abort must also roll back, because it tells the operator to rerun and
    setup refuses to run while a previous state file survives.
    """
    doomed = repo / "vanishes.txt"
    doomed.write_text("here for the manifest, gone for the copy\n", encoding="utf-8")
    real_copy = review_worktree._copy_untracked  # pyright: ignore[reportPrivateUsage]

    def _vanish_first(source: Path, worktree: Path, manifest: list[str]) -> list[str]:
        doomed.unlink(missing_ok=True)
        return real_copy(source, worktree, manifest)

    monkeypatch.setattr(review_worktree, "_copy_untracked", _vanish_first)
    # Base ref equal to HEAD and no tracked edit, so the untracked file is the
    # only thing in scope — the shape the gate was outflanked in.
    assert main(["setup", "--scratch", str(scratch), "--base", "HEAD"]) == 1

    err = capsys.readouterr().err
    assert "every untracked file vanished" in err
    assert "reviewing )" not in err
    assert "Traceback" not in err
    assert not (scratch / STATE_FILENAME).exists(), "abort left the state file"
    assert "wt-" not in _git(repo, "worktree", "list"), "abort left a registration"


def test_a_non_utf8_state_file_aborts_with_guidance_not_a_traceback(
    repo: Path, scratch: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`UnicodeDecodeError` is a ValueError, so it escaped every handler.

    The read is `read_text(encoding="utf-8")` inside a `try` that caught only
    `json.JSONDecodeError`, and `main`'s net is `except (AbortError,
    OSError)` — neither owns a ValueError. Teardown therefore died by
    traceback with none of the `_STATE_REMEDY` guidance, and because the
    state file is never unlinked on that path, every later setup was refused
    too: both subcommands permanently wedged.

    Reachable without an adversary on this project's primary platform. The
    remedy text sends the operator to inspect and hand-edit this very file,
    and an editor or a `Set-Content` without `-Encoding UTF8` writing it back
    in the console codepage is precisely the Windows-1252 corruption
    CLAUDE.md documents.
    """
    scratch.mkdir(parents=True)
    good = json.dumps({"root": str(repo), "worktrees": {}, "venvs": {}})
    # One cp1252 em-dash byte, the shape a Windows editor round-trip produces.
    (scratch / STATE_FILENAME).write_bytes(good.encode("utf-8") + b"\x97")

    assert main(["teardown", "--scratch", str(scratch)]) == 1
    err = capsys.readouterr().err
    assert "Traceback" not in err, "the error escaped every handler"
    assert "is malformed" in err
    assert "delete the state file" in err


def test_a_relative_recorded_root_is_refused(
    repo: Path, scratch: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`--scratch` must be absolute for a reason that applies to `root` too.

    The recorded root's whole job is to make teardown cwd-independent, so a
    relative one reintroduces the dependence it exists to remove. Measured,
    `"root": "."` and `"root": ""` were both accepted: `Path(".")` passes the
    existence and `.git` checks from inside ANY repository, after which every
    `git -C .` acted on the cwd's repo — directories deleted, the wrong
    registry queried and found clean, "no strays registered" printed, the
    state file unlinked, while the real repository kept both worktrees
    registered against deleted directories with their only record gone.
    """
    scratch.mkdir(parents=True)
    for bad_root in (".", "", "repo"):
        state: dict[str, object] = {"root": bad_root, "worktrees": {}, "venvs": {}}
        (scratch / STATE_FILENAME).write_text(json.dumps(state), encoding="utf-8")
        assert main(["teardown", "--scratch", str(scratch)]) == 1, bad_root
        err = capsys.readouterr().err
        assert "malformed (root)" in err
        assert "delete the state file" in err
        # Refused, so the record of whatever it names survives for a real fix.
        assert (scratch / STATE_FILENAME).exists()


def test_a_held_index_lock_is_not_reported_as_an_unmergeable_index(
    repo: Path,
    scratch: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    git_double: _GitDouble,
) -> None:
    """A transient lock must not be triaged as a blocking condition.

    `git stash create` exits 1 with "could not write index" when another git
    holds `.git/index.lock` — a concurrent git, an IDE's git integration,
    fsmonitor, a stale lock from a killed process, or a second setup in the
    same repo. The abort volunteered "conflicted or unmergeable index state?"
    for every nonzero exit, and reviewer-isolation.md files an unmergeable
    index under **Blocking — route to the fallback**. So the orchestrator read
    the guess, believed it, and permanently degraded a round whose correct
    remedy was to wait a second and rerun. Reproduced with two concurrent
    setups.
    """

    def _locked_index(
        root: Path, *args: str
    ) -> subprocess.CompletedProcess[str] | None:
        if _subcommand(args) == ("stash", "create"):
            return subprocess.CompletedProcess(
                args=list(args),
                returncode=1,
                stdout="",
                stderr=(
                    "error: could not write index\n"
                    "fatal: Unable to create "
                    f"'{repo}/.git/index.lock': File exists."
                ),
            )
        return None

    (repo / "tracked.txt").write_text("edit\n", encoding="utf-8")
    git_double.install(_locked_index)
    assert _setup(scratch) == 1

    err = capsys.readouterr().err
    assert "index.lock" in err
    assert "Wait a moment and rerun" in err
    # The misdiagnosis that routed the round to the fallback.
    assert "unmergeable index state" not in err


def test_an_unremovable_worktree_says_how_to_unwedge(
    repo: Path,
    scratch: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    git_double: _GitDouble,
) -> None:
    """The one refusal in the module that broke the module's own rule.

    `_STATE_REMEDY`'s comment states it: a teardown that refuses to act
    leaves the operator wedged, because the state file it keeps also blocks
    the next setup — so every refusal has to say how to get out. This branch
    returned the bare OS error. reviewer-isolation.md promised the opposite,
    telling the operator that for a directory it could not remove the message
    would say to remove it by hand and delete the state file.
    """
    (repo / "tracked.txt").write_text("edit\n", encoding="utf-8")
    assert _setup(scratch) == 0

    def _removal_fails(
        root: Path, *args: str
    ) -> subprocess.CompletedProcess[str] | None:
        if _subcommand(args) == ("worktree", "remove"):
            return _git_failure(args, "fatal: validation failed, cannot remove")
        return None

    git_double.install(_removal_fails)

    # A file rmtree cannot delete either, so the fallback also fails and the
    # branch under test is the one that reports.
    def _undeletable(path: Path, scratch: Path) -> list[str]:
        return [f"{path}: in use"]

    monkeypatch.setattr(review_worktree, "_force_delete_tree", _undeletable)
    assert main(["teardown", "--scratch", str(scratch)]) == 1

    err = capsys.readouterr().err
    assert "could not remove" in err
    assert "remove it by hand" in err
    assert "delete the state file" in err
    assert (scratch / STATE_FILENAME).exists(), "the record of the stray was deleted"


def test_an_old_interpreter_is_refused_before_it_can_strand_worktrees(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The undeclared 3.12 floor, which only bit on the delete paths.

    `shutil.rmtree(onexc=)` and `Path.is_junction` are 3.12+; everything else
    works on 3.9. So an older interpreter completed `setup` normally and
    failed only in teardown, by a TypeError no `except` clause owns — state
    file never unlinked, both worktrees left registered, every later setup
    refused, and a traceback matching none of the exit-1 shapes the teardown
    triage enumerates. `requires-python` in pyproject does not bind here: it
    constrains uv, while reviewer-isolation.md launches this file with a bare
    `python`, which on Windows commonly resolves to a Store shim.
    """
    with pytest.raises(review_worktree.AbortError, match=r"3\.12 or newer"):
        review_worktree._require_python((3, 11))  # pyright: ignore[reportPrivateUsage]
    # The running interpreter must pass, or every command aborts. Asserted
    # against `sys.version_info`, never against `_MIN_PYTHON` itself: that
    # spelling is `x < x`, False for any value the constant ever takes, so it
    # adds a name and no detection power — raising the floor to (3, 15) on a
    # 3.14 interpreter would strand every real invocation with this line still
    # green.
    review_worktree._require_python(sys.version_info[:2])  # pyright: ignore[reportPrivateUsage]
    assert capsys.readouterr().err == ""


def test_the_utf8_stream_reconfigure_is_pinned_and_stays_out_of_pytest() -> None:
    """The block whose comment claimed it could not be reached or tested.

    Both halves were false. `_pytest.capture.EncodedFile` and `TeeCaptureIO`
    ARE `io.TextIOWrapper` subclasses, so running this inside `main()` meant
    every in-process call in this file (direct `main([...])` plus the `_setup`
    helper that wraps it) reconfiguring pytest's own capture streams and
    leaving them at `errors="replace"` for every test that followed — a global
    encoding-strictness downgrade ordered by test execution order, landing on
    whichever innocent test wrote next. Moving the call to `__main__` makes
    the claim true, and passing the streams in makes the behavior assertable,
    which is what the comment said could not be done.

    This docstring also named the log-canary stream as the victim, and that
    was wrong: `TeeCaptureIO` forwards the **str** to `self._other`, the
    pre-capture `sys.stdout`, which is what `tee pytest-output.log` captures,
    while the reconfigure touches only the capture wrapper. Corrected in all
    four copies of the sentence.

    Both subclasses are asserted, not just `EncodedFile`. `ci.yml` runs the
    serial legs with `--capture=tee-sys`, so `TeeCaptureIO` — not
    `EncodedFile` — is what `sys.stdout` is bound to there; asserting only
    the one that is *not* live on the leg the claim is about would let a
    pytest release that reparents the other silently falsify the recorded
    justification for this whole change with every test still green.
    """
    import _pytest.capture

    assert issubclass(_pytest.capture.EncodedFile, io.TextIOWrapper)
    assert issubclass(_pytest.capture.TeeCaptureIO, io.TextIOWrapper)

    raw = io.BytesIO()
    wrapper = io.TextIOWrapper(raw, encoding="ascii", errors="strict")
    passthrough = object()
    review_worktree._use_utf8_io((wrapper, passthrough))  # pyright: ignore[reportPrivateUsage]
    assert wrapper.encoding.lower().replace("-", "") == "utf8"
    assert wrapper.errors == "replace"

    # And it is no longer wired into main(), which is what kept it out of the
    # suite's own capture streams. Deleting the __main__ call must fail this.
    source = Path(review_worktree.__file__).read_text(encoding="utf-8")
    main_body = source.split("def main(")[1].split("\nif __name__")[0]
    assert "_use_utf8_io" not in main_body
    assert "_use_utf8_io((sys.stdout, sys.stderr))" in source.split("\nif __name__")[1]


# ---------------------------------------------------------------------------
# Pass-10 containment guards. Every one of these reproduced end-to-end against
# the pre-fix launcher before the fix was written; each was then re-checked by
# reverting its own guard in a scratch copy of the module and confirming the
# scenario goes back to destroying, leaking, or lying.
# ---------------------------------------------------------------------------


def test_a_recorded_worktree_link_never_deregisters_its_target(
    repo: Path, scratch: Path, tmp_path: Path
) -> None:
    """The link branch unlinks the link, then de-registers — what, exactly?

    It computed `Path(path).resolve()` at the top of the function, BEFORE the
    unlink, so that value followed the link to its target. `_deregister_missing`
    then found the target in git's registry and ran `git worktree remove
    --force` on it — and that helper's safety argument, "there is nothing on
    disk left to destroy", is true of the link just removed and false of a
    developer's live worktree. Measured: the victim's directory and its
    uncommitted work were destroyed, the stray check saw nothing (the path was
    never in `paths`), and teardown printed success and exited 0.
    """
    scratch.mkdir(parents=True, exist_ok=True)
    victim = tmp_path / "someones-live-worktree"
    _git(repo, "worktree", "add", "--detach", str(victim))
    (victim / "uncommitted.txt").write_text("hours of work\n", encoding="utf-8")
    link = scratch / "wt-spec-reviewer-abc123456-def456"
    _link_dir(link, victim)

    _write_state(scratch, _state_recording(repo, scratch, "worktrees", link))
    assert main(["teardown", "--scratch", str(scratch)]) == 0

    assert (victim / "uncommitted.txt").read_text(encoding="utf-8") == "hours of work\n"
    assert victim.resolve() in review_worktree._registered_paths(repo), (  # pyright: ignore[reportPrivateUsage]
        "a live worktree was de-registered through a recorded link"
    )
    assert not review_worktree._is_link(link), "the recorded link itself survived"  # pyright: ignore[reportPrivateUsage]


def test_a_recorded_worktree_link_that_will_not_unlink_never_deletes_its_target(
    repo: Path, scratch: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`_remove_worktree` carries its own copy of the rmdir fallback.

    Its twin in `_remove_plain_dir` has a pinning test; this one had none —
    every link test registered its link under `venvs`, so the `worktrees` clone
    was reached by nothing at all. The invariant either way: neither call may
    remove what the link points at.

    **The two had then drifted, which is the cost of hand-duplicating what
    this file elsewhere parametrizes over `_RECORDED_KEYS`.** The venv-side
    test asserts the platform-conditional exit code and whether the state
    file survives; this one, added later to close the coverage gap, asserted
    only that the target was untouched — so the worktree-side fallback's exit
    code and state-file behavior were unverified on both platforms. Those
    assertions are now here too.

    Still two tests rather than one parametrized case, deliberately: the
    recorded name matters on the `worktrees` side and not on the `venvs` one
    (`_is_generated_name` gates de-registration, and `_contained`'s
    `relocatable` differs between them), so a shared body would have to
    branch on the key anyway and would hide exactly the asymmetry that makes
    the two functions separate.
    """
    scratch.mkdir(parents=True, exist_ok=True)
    precious = tmp_path / "precious"
    precious.mkdir()
    (precious / "keep.txt").write_text("hours of work\n", encoding="utf-8")
    link = scratch / "wt-spec-reviewer-abc123456-def456"
    _link_dir(link, precious)

    real_unlink = Path.unlink

    def _refuse_this_link(self: Path, missing_ok: bool = False) -> None:
        # Scoped to the link: a global refusal also catches teardown's own
        # state-file unlink, which fails the test for the wrong reason.
        if self == link:
            raise OSError(13, "simulated: the platform will not unlink it")
        real_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", _refuse_this_link)
    _write_state(scratch, _state_recording(repo, scratch, "worktrees", link))
    exit_code = main(["teardown", "--scratch", str(scratch)])
    monkeypatch.setattr(Path, "unlink", real_unlink)

    assert precious.is_dir(), "the fallback deleted through the link"
    assert (precious / "keep.txt").read_text(encoding="utf-8") == "hours of work\n"

    # The half that had drifted away from the venv-side twin.
    if os.name == "nt":
        assert exit_code == 0
        assert not link.exists(), "the rmdir fallback did not remove the link"
        assert not (scratch / STATE_FILENAME).exists()
    else:
        # rmdir cannot remove a symlink; the refusal must be reported, and the
        # state file kept, because the link is still on disk and this is its
        # only record.
        assert exit_code == 1
        assert link.is_symlink()
        assert (scratch / STATE_FILENAME).exists()


def test_a_junction_into_specs_personal_is_refused_before_replication(
    repo: Path, scratch: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The one shape that defeats all three untracked guards simultaneously.

    A junction inside the repo pointing at `specs/personal/`: `_is_personal`
    reads `notes/labs.md` and sees no prefix, the leaf-level `_is_link` filter
    sees no link (the link is `notes`), and `_escapes` answers False because the
    target is still inside the repository — the outside filter only catches
    links *leaving* the tree. Measured against the pre-fix launcher: setup
    exited 0, printed `?? notes/labs.md` as ordinary in-scope content, and put
    the file in both agent-readable worktrees with no warning of any kind.

    The two platforms used to end differently here and no longer do. On POSIX
    git reports `notes` as a single symlink entry, which the leaf-level filter
    dropped with a warning — containment held one guard earlier, at exit 0,
    but the warning **named the link**, and an untracked link's leaf name is
    chosen by whoever created it (`<provider>-2026-panel -> …`), which
    CLAUDE.md classifies as personal with no values attached. That name
    reached stderr, the transcript, `state["warnings"]` on disk and every
    relayed reviewer report. `_leaks_personal` now runs over untracked link
    *leaves* as well as path components, so both platforms reach the same
    count-only refusal by their own route and the branch that used to sit at
    the bottom of this test is gone.
    """
    personal = repo / "specs" / "personal"
    personal.mkdir(parents=True)
    (personal / "labs.md").write_text("SYNTHETIC-NOT-REAL 1.0\n", encoding="utf-8")
    (repo / ".gitignore").write_text("specs/personal/\n", encoding="utf-8")
    _git(repo, "add", ".gitignore")
    _git(repo, "commit", "-m", "ignore personal")
    _link_dir(repo / "notes", personal)

    exit_code = _setup(scratch)
    captured = capsys.readouterr()

    # The invariant, identical on both platforms: no byte of specs/personal/
    # reaches a worktree, and no name from behind the link is echoed anywhere.
    assert not list(scratch.rglob("labs.md")), "health data reached a worktree"
    assert "labs.md" not in captured.err
    assert "labs.md" not in captured.out

    # One outcome, two routes. Windows: git walks THROUGH the junction, so only
    # a path *component* reveals it. POSIX: git reports the link as one entry,
    # so only the leaf does. Both now reach `_leaks_personal`, and asserting
    # the same three things on both legs is what would catch a fix that
    # reached one route and not the other — which has happened here before.
    assert exit_code == 1
    assert "specs/personal/" in captured.err
    assert "notes" not in captured.err, "the refusal named the link"
    assert not (scratch / STATE_FILENAME).exists()


def test_a_tracked_path_under_such_a_junction_is_refused_too(
    repo: Path, scratch: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The tracked twin, which the untracked copy-in guard cannot reach at all.

    `git worktree add` materializes tracked content straight from the snapshot,
    with no copy step for the untracked filter to sit in front of — so whatever
    git tracks here lands in every worktree. What git tracks differs by
    platform, and both spellings had to be measured rather than assumed:

    * **Windows** — git walks through the junction, so `git add notes` stages
      `notes/labs.md` as an ordinary tracked path, invisible to
      `personal_tracked`'s `:(icase)specs/personal` pathspec.
    * **POSIX** — git stages `notes` itself as a symlink entry (mode 120000)
      and recreates it in each worktree. With an absolute target that link then
      points at the LIVE repo's `specs/personal`, and the data is readable
      through it from inside the worktree (measured under WSL). There is no
      component to inspect and no copy to intercept; only the index entry's
      mode reveals it.

    Two routes, one predicate — which is what keeps them from drifting the way
    the tracked and untracked prefix tests already did once.
    """
    personal = repo / "specs" / "personal"
    personal.mkdir(parents=True)
    (personal / "labs.md").write_text("SYNTHETIC-NOT-REAL 1.0\n", encoding="utf-8")
    (repo / ".gitignore").write_text("specs/personal/\n", encoding="utf-8")
    _git(repo, "add", ".gitignore")
    _git(repo, "commit", "-m", "ignore personal")
    _link_dir(repo / "notes", personal)
    _git(repo, "add", "notes")

    # The premise, per platform: git really does track something no prefix test
    # matches, and `personal_tracked` really is blind to it either way.
    tracked = _git(repo, "ls-files")
    assert ("notes/labs.md" in tracked) if os.name == "nt" else ("notes" in tracked)
    assert review_worktree.personal_tracked(repo) == []

    assert _setup(scratch) == 1
    captured = capsys.readouterr()
    assert "specs/personal/" in captured.err
    assert "labs.md" not in captured.err
    assert not list(scratch.rglob("labs.md")), "health data reached a worktree"
    assert not (scratch / STATE_FILENAME).exists()


def test_a_hard_link_into_specs_personal_is_refused_before_replication(
    repo: Path, scratch: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The gap ADR-0068 booked as accepted, closed on the source side.

    `os.link` (and `mklink /H`) give one of the owner's files a second name
    with no target to resolve: `_leaks_personal` answers False, `_is_personal`
    sees only the name the operator chose, and `_linked_dirs` finds no link in
    any path component. Measured before this guard — the content of the linked
    file landed in **both** worktrees with setup exiting 0.

    Checked against the live tree's inodes and deliberately not the worktree's.
    `copy2` and `git checkout` each write a fresh file, so the copy shares the
    content and not the identity: the artifact-side check the review proposed
    for this answers False on the very case it was written for (measured).

    Runs on every platform — hard links need no privilege on NTFS or POSIX,
    unlike the symlink routes most containment tests here have to skip for.
    """
    personal = repo / "specs" / "personal"
    personal.mkdir(parents=True)
    secret = personal / "labs.md"
    secret.write_text("SYNTHETIC-NOT-REAL 1.0\n", encoding="utf-8")
    (repo / ".gitignore").write_text("specs/personal/\n", encoding="utf-8")
    _git(repo, "add", ".gitignore")
    _git(repo, "commit", "-m", "ignore personal")
    os.link(secret, repo / "second-name.md")

    # The premise: every path-shaped guard is blind to it.
    assert review_worktree.personal_tracked(repo) == []
    assert not review_worktree._leaks_personal(repo / "second-name.md", repo)  # pyright: ignore[reportPrivateUsage] - the predicate whose blindness is the premise

    assert _setup(scratch) == 1
    captured = capsys.readouterr()
    assert "second name for a file under specs/personal/" in captured.err
    assert "second-name.md" not in captured.err, "the guard named the link"
    assert not _files_holding(scratch, "SYNTHETIC-NOT-REAL 1.0"), (
        "health data reached a worktree"
    )
    assert not (scratch / STATE_FILENAME).exists()


@pytest.mark.skipif(os.name == "nt", reason="symlink creation (WSL/CI legs run this)")
def test_a_tracked_symlink_leaving_the_repo_is_caught_by_the_artifact_walk(
    repo: Path, scratch: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A route no input predicate covers, found by building the artifact check.

    `notes -> /abs/outside`, committed as mode 120000. `_leaks_personal`
    answers False because the target is not under `specs/personal/` at all;
    `_linked_dirs` inspects directory *components* and here the link is the
    leaf; `_escapes` runs over the untracked manifest, which a tracked entry
    never enters. `git worktree add` then recreates the link faithfully, and
    everything behind it is readable from inside the worktree.

    Measured with the walk disabled: setup exited 0 and the out-of-repo
    sentinel was readable through **both** worktrees. That is why this check
    is a walk over what landed rather than an eleventh predicate over what
    went in — the review argued a route nobody had imagined would still land
    a file, and building the walk turned one up.
    """
    outside = tmp_path / "outside-the-repo"
    outside.mkdir()
    (outside / "exports.md").write_text("SYNTHETIC-NOT-REAL 2.0\n", encoding="utf-8")
    (repo / "notes").symlink_to(outside)
    _git(repo, "add", "notes")
    _git(repo, "commit", "-m", "track a link out of the tree")

    assert not review_worktree._leaks_personal(repo / "notes", repo)  # pyright: ignore[reportPrivateUsage] - the predicate this route walks past

    assert _setup(scratch) == 1
    err = capsys.readouterr().err
    assert "resolve outside the worktree" in err
    assert "notes" not in err, "the walk named the path it refused"
    assert not _files_holding(scratch, "SYNTHETIC-NOT-REAL 2.0"), (
        "out-of-repo content was readable through a worktree"
    )
    assert not (scratch / STATE_FILENAME).exists()


@pytest.mark.skipif(
    os.name == "nt", reason="NTFS refuses a non-UTF-8 name (WSL/CI legs run this)"
)
def test_a_non_utf8_named_tracked_symlink_into_personal_is_named_by_the_guard(
    repo: Path, scratch: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The containment half of the decode mismatch, asserted at the guard.

    `tracked_entries` reads mode-120000 entries and `_leaks_personal` takes
    each to the filesystem as `root / rel`. Under `_git_z`'s default
    "replace" a raw-byte name arrives as U+FFFD, names nothing on disk,
    `.resolve()` finds nothing, and the guard answers False — measured: this
    exact link passed every personal-data guard and was checked out into
    both worktrees with setup exiting 0.

    The assertion is on the **specs/personal guard** firing, not merely on
    exit 1, and that distinction is the whole point. With the decode left
    lossy the round still fails, because `_artifact_breaches` refuses any
    worktree holding a path that resolves out of it — so an exit-code-only
    assertion passes with the guard blind, and would pin the walk twice
    while pinning this fix not at all. The walk is the backstop; naming the
    cause is this guard's job.
    """
    personal = repo / "specs" / "personal"
    personal.mkdir(parents=True)
    (personal / "labs.md").write_text("SYNTHETIC-NOT-REAL 3.0\n", encoding="utf-8")
    (repo / ".gitignore").write_text("specs/personal/\n", encoding="utf-8")
    _git(repo, "add", ".gitignore")
    # Absolute target: a relative one points inside the worktree, where the
    # gitignored directory was never checked out, which is the harmless
    # dangling case `_leaks_personal`'s own comment describes.
    link = os.path.join(os.fsencode(str(repo)), b"panel-\xff-2026")
    os.symlink(os.fsencode(str(personal / "labs.md")), link)
    _git(repo, "add", "--", os.fsdecode(link))
    _git(repo, "commit", "-m", "track a link with a name that is not utf-8")

    assert _setup(scratch) == 1
    err = capsys.readouterr().err
    assert "resolving into" in err, err
    assert "specs/personal/" in err, err
    assert "1 path(s)" in err
    # Redacted like every sibling guard: the operator-chosen name of a link
    # into specs/personal/ is provenance even with no values behind it.
    assert "panel-" not in err
    assert not _files_holding(scratch, "SYNTHETIC-NOT-REAL 3.0")
    assert not (scratch / STATE_FILENAME).exists()


def test_an_empty_worktrees_map_is_refused_rather_than_reported_as_success(
    repo: Path, scratch: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`required=True` checked presence and type, never emptiness.

    `state_map.get(key)` followed by `isinstance(raw, dict)` accepts `{}`
    verbatim, so teardown removed nothing, printed `removed 0 worktree(s) … no
    strays registered`, unlinked the state file and returned 0 — while both
    worktrees stayed registered with their only on-disk record gone. That is
    the outcome the `required` docstring says it exists to prevent, and
    `_STATE_REMEDY` invites the hand-editing that produces the spelling.
    """
    scratch.mkdir(parents=True, exist_ok=True)
    _write_state(scratch, {"root": str(repo), "worktrees": {}, "venvs": {}})
    assert main(["teardown", "--scratch", str(scratch)]) == 1
    assert (scratch / STATE_FILENAME).exists(), "the only record was deleted"
    err = capsys.readouterr().err
    assert "records no worktrees" in err
    assert "delete the state file" in err, "a refusal with no way out is a wedge"


def test_a_base_ref_starting_with_a_dash_is_refused_before_git_sees_it(
    repo: Path, scratch: Path
) -> None:
    """`--base` is interpolated into a revision argument with no validation.

    git parses an argv element beginning with `-` as its own option, so
    `--base=--output=<path>` made `git diff` create `<path>...HEAD` INSIDE the
    repository (reproduced) — the one thing invariant 1 says this launcher
    never does. A `--` separator is not the fix: revisions must precede it.
    """
    before = {p.name for p in repo.iterdir()}
    target = repo / "PWNED.txt"
    assert main(["setup", "--scratch", str(scratch), f"--base=--output={target}"]) == 1
    assert {p.name for p in repo.iterdir()} == before, "setup wrote inside the repo"
    assert not (scratch / STATE_FILENAME).exists()


def test_a_state_file_appearing_mid_setup_is_never_clobbered(
    repo: Path, scratch: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The early `exists()` test is check-then-write, seconds wide.

    A re-launch after a hung reviewer, `/apply-review` re-running the round, two
    sessions sharing a scratchpad: both setups pass the check, and `os.replace`
    clobbers rather than failing, so B overwrites A's state and A's worktrees
    are orphaned permanently carrying the uncommitted work. The window is
    reproduced deterministically here by having a call inside it create the
    file, which is what a concurrent setup would do.
    """
    (repo / "tracked.txt").write_text("edit\n", encoding="utf-8")
    scratch.mkdir(parents=True, exist_ok=True)
    marker: dict[str, object] = {
        "root": "the other setup wrote this",
        "worktrees": {"a": "x"},
    }
    real_snapshot = review_worktree.snapshot

    def _racing_snapshot(root: Path) -> str | None:
        _write_state(scratch, marker)
        return real_snapshot(root)

    monkeypatch.setattr(review_worktree, "snapshot", _racing_snapshot)
    assert _setup(scratch) == 1
    assert _state(scratch) == marker, "the other setup's state file was clobbered"


def _filesystem_is_case_insensitive(where: Path) -> bool:
    """Probe rather than infer from `os.name`.

    macOS defaults to case-insensitive APFS but can be formatted either way,
    and a Linux checkout on a mounted NTFS/DrvFs volume is case-insensitive
    too — so the platform does not decide this and a `skipif(os.name == ...)`
    would gate the wrong hosts in both directions.
    """
    probe = where / "CaseProbe.tmp"
    probe.write_text("x", encoding="utf-8")
    try:
        return (where / "caseprobe.tmp").exists()
    finally:
        probe.unlink()


def test_a_case_only_rename_is_not_read_as_index_worktree_divergence(
    repo: Path,
) -> None:
    """`Path.exists()` is case-insensitive here; git's paths are not.

    After `git rm --cached docs/Setup.md` plus an on-disk rename to
    `docs/setup.md`, the `--diff-filter=D` query yields `docs/Setup.md`, which
    is not on disk — but `.exists()` said True because `docs/setup.md` is. Setup
    then aborted claiming divergence where index and snapshot agreed exactly,
    and the printed remedy either re-stages the wrong-cased path or undoes the
    deletion, leaving the operator only the unisolated fallback.

    **Gated on the filesystem, not the platform, and gated at all only after
    it was caught proving nothing.** On a case-sensitive filesystem the buggy
    and the fixed implementation agree — `.exists()` answers False for the
    wrong-cased path there too — so this ran on the Linux leg as a green test
    with zero detection power, which is worse than a skip because the pass
    reads as coverage. The scenario cannot occur there either, so a skip is
    the honest outcome; `_exists_cased`'s per-component walk is asserted
    unconditionally below, which is the part that *is* platform-independent.
    """
    docs = repo / "docs"
    docs.mkdir()
    (docs / "Setup.md").write_text("a\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "docs")
    _git(repo, "rm", "--cached", "-q", "docs/Setup.md")
    (docs / "Setup.md").rename(docs / "tmp")
    (docs / "tmp").rename(docs / "setup.md")

    # Platform-independent: the predicate must answer on the *reported*
    # casing whatever the filesystem does, including for a directory
    # component rather than the leaf — a whole-path `.exists()` and a
    # leaf-only check both get this wrong on a case-insensitive volume.
    cased = review_worktree._exists_cased  # pyright: ignore[reportPrivateUsage]
    assert cased(repo, "docs/setup.md") is True
    assert cased(repo, "docs/Setup.md") is False
    assert cased(repo, "Docs/setup.md") is False
    assert cased(repo, "docs/absent.md") is False

    if not _filesystem_is_case_insensitive(repo):
        pytest.skip(
            "case-sensitive filesystem: the wrong-cased path does not exist "
            "under either implementation, so this scenario cannot arise"
        )
    assert review_worktree.index_worktree_divergence(repo) == []


def test_an_untracked_name_carrying_a_line_break_cannot_forge_a_manifest_line(
    repo: Path,
    scratch: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The input side was hardened byte-for-byte; the output side printed raw.

    Stdout here is a line-oriented protocol the launch procedure parses, so a
    filename holding a line terminator forges a line of it — a `state:` line
    aiming a later teardown at a state file of the file's choosing, or a bare
    `\r` that overwrites the line above and erases the skip-worktree warning
    this module calls the one signal that a changed tree can look clean.
    """
    forged = "notes\nstate: /tmp/attacker/review-worktrees.json"

    # Stubbed at `untracked_manifest` rather than created on disk, and that is
    # what makes this test exist at all: NTFS refuses a newline in a filename,
    # and so does the DrvFs mount WSL sees this checkout through — so an
    # end-to-end version skips on *both* legs of the local matrix and pins
    # nothing anywhere. The guard is a pure filter over the manifest, so the
    # manifest is the honest seam: this proves the filter fires and redacts,
    # and deliberately does not claim to prove a POSIX filesystem will hand
    # such a name over (the CI legs run on ext4, where it will).
    def _forged_manifest(root: Path) -> list[str]:
        return [forged]

    monkeypatch.setattr(review_worktree, "untracked_manifest", _forged_manifest)
    (repo / "tracked.txt").write_text("edit\n", encoding="utf-8")

    assert _setup(scratch) == 1
    captured = capsys.readouterr()
    assert "line-break character" in captured.err
    # Count only — the untrustworthy name is the last thing to echo.
    assert "attacker" not in captured.err
    assert "state:" not in captured.out
    assert not (scratch / STATE_FILENAME).exists()


def test_a_pre_existing_link_at_a_venv_path_is_refused_before_it_is_published(
    repo: Path, scratch: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Publishing the path IS the write, because setup never creates the dir.

    It prints each venv path with the instruction to export it as
    `UV_PROJECT_ENVIRONMENT`, so a junction left there by a hand-cleanup after a
    failed teardown makes every `uv run` materialize the environment *through*
    the link into its target — a reviewer writing outside the isolation
    boundary, possibly into the live tree. Teardown unlinks such a link
    correctly; by then the writes have happened. Setup is the only place that
    can refuse the name rather than the aftermath.
    """
    (repo / "tracked.txt").write_text("edit\n", encoding="utf-8")
    scratch.mkdir(parents=True, exist_ok=True)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    _link_dir(scratch / "venv-spec-reviewer", elsewhere)

    assert _setup(scratch) == 1
    assert "symlink or junction" in capsys.readouterr().err
    assert not (scratch / STATE_FILENAME).exists()


def test_an_ordinary_leftover_venv_directory_is_still_allowed(
    repo: Path, scratch: Path
) -> None:
    """The complement, so the guard above cannot be widened into a wedge.

    A plain leftover venv dir is reused by `uv`, which is the entire point of a
    fixed name; only a *link* is refused.
    """
    (repo / "tracked.txt").write_text("edit\n", encoding="utf-8")
    scratch.mkdir(parents=True, exist_ok=True)
    (scratch / "venv-spec-reviewer").mkdir()
    assert _setup(scratch) == 0
    assert main(["teardown", "--scratch", str(scratch)]) == 0


def test_a_stale_venv_record_never_relocates_onto_a_live_rounds_venv(
    repo: Path, scratch: Path, tmp_path: Path
) -> None:
    """The renamed-scratch relocation is justified by round-unique basenames.

    True of `wt-<agent>-<sha9>-<hex6>` and false of the deliberately fixed
    `venv-<agent>`, which the docstring asserted for both. A stale state file
    torn down against a live scratch dir then has its venv path silently
    retargeted onto the CURRENT round's `venv-spec-reviewer`, which passes
    containment and is force-deleted mid-review.
    """
    scratch.mkdir(parents=True, exist_ok=True)
    live = scratch / "venv-spec-reviewer"
    live.mkdir()
    (live / "pyvenv.cfg").write_text("the live round's env\n", encoding="utf-8")
    stale = tmp_path / "a-previous-sessions-scratch" / "venv-spec-reviewer"

    assert review_worktree._remove_plain_dir(str(stale), scratch) is None  # pyright: ignore[reportPrivateUsage]
    assert (live / "pyvenv.cfg").exists(), "a live round's venv was deleted"


def test_two_teardowns_racing_on_one_scratch_dir_both_exit_clean(
    repo: Path, scratch: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ "Run teardown on every exit path" plus one retry produces exactly this.

    The state-file unlink was the only one in the module without `missing_ok`,
    so a teardown whose twin finished first raised FileNotFoundError *after*
    removing everything successfully — surfacing as a bare `error: [Errno 2] …`
    exit 1, a shape the teardown triage does not enumerate, carrying none of
    the remedy text, for a run that did its job.

    Two *sequential* teardowns do not reach it, and asserting them proved
    nothing: the second returns early on the missing state file and never gets
    as far as the unlink. The window is between this run's own `exists()` check
    and its own unlink, so it is reproduced by having a call inside that window
    remove the file — which is exactly what the other teardown would do.
    """
    (repo / "tracked.txt").write_text("edit\n", encoding="utf-8")
    assert _setup(scratch) == 0
    real_remove = review_worktree._remove_plain_dir  # pyright: ignore[reportPrivateUsage]

    def _racing_remove(path_str: str, scratch_dir: Path) -> str | None:
        (scratch / STATE_FILENAME).unlink(missing_ok=True)
        return real_remove(path_str, scratch_dir)

    monkeypatch.setattr(review_worktree, "_remove_plain_dir", _racing_remove)
    assert main(["teardown", "--scratch", str(scratch)]) == 0
    assert not (scratch / STATE_FILENAME).exists()

    # And the ordinary sequential retry the instruction actually produces.
    monkeypatch.setattr(review_worktree, "_remove_plain_dir", real_remove)
    assert main(["teardown", "--scratch", str(scratch)]) == 0


def test_teardown_reports_unrecorded_strays_in_the_scratch_dir(
    repo: Path, scratch: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Teardown iterates the RECORDS, so anything else in the scratch dir is
    deleted by no path, named by no error, and counted by no success line.

    Two ordinary ways in: setup does not create the venv dirs — it prints
    them and instructs each reviewer to export the path, with nothing
    verifying the export happened or that uv resolved to it — and a round
    orphaned by a racing teardown leaves a full worktree with its record
    gone. Each stray is a whole copy of the repository carrying uncommitted
    work. `_STATE_REMEDY` says "remove any wt-* strays", naming neither
    `venv-*` nor a sweep, so even the manual escape covered half of it.

    Reported and deliberately not deleted: a directory teardown has no record
    of creating is precisely what it must not force-delete on its own, which
    is the whole reason the recorded-path containment machinery exists.
    """
    (repo / "tracked.txt").write_text("edit\n", encoding="utf-8")
    assert _setup(scratch) == 0
    # A venv built somewhere the reviewer chose, and an orphan from a round
    # whose record is gone. Neither is in this round's state file.
    (scratch / "venv-somewhere-else" / "lib").mkdir(parents=True)
    (scratch / "wt-orphan-abc123def-a1b2c3" / "src").mkdir(parents=True)

    assert main(["teardown", "--scratch", str(scratch)]) == 0
    out = capsys.readouterr().out
    assert "2 unrecorded wt-*/venv-* director(ies) remain" in out, out
    assert "venv-somewhere-else" in out
    assert "wt-orphan-abc123def-a1b2c3" in out
    # Reported, not removed.
    assert (scratch / "venv-somewhere-else").is_dir()
    assert (scratch / "wt-orphan-abc123def-a1b2c3").is_dir()


def test_teardown_refuses_to_deregister_a_worktree_it_did_not_create(
    repo: Path, scratch: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """De-registration destroys the REGISTRATION, which the safety argument
    for running it outside the scratch dir never accounted for.

    `_deregister_missing`'s docstring said it is "safe where a force-delete
    would not be: there is nothing on disk left to destroy" — true of the
    tree, false of the registry entry. `git worktree prune` was removed from
    `_cleanup_worktrees` for exactly this: de-registering a worktree whose
    directory is momentarily missing leaves its owner needing `git worktree
    repair`, a repo-global write on someone else's work. Two ordinary routes
    in — a hand-edited state file, which `_STATE_REMEDY` actively invites,
    and `exists()` answering False for a path that is unreachable rather than
    deleted (an unmounted volume, a disconnected share).

    The discriminator is the generated `wt-<agent>-<short>-<hex6>` basename,
    not the location: a scratch dir renamed after setup leaves recorded paths
    outside the new scratch that teardown must still be able to finish, and
    that name is what survives the rename while a hand-written record does
    not get it for free.
    """
    outsider = tmp_path / "someone-elses" / "checkout"
    _git(repo, "worktree", "add", "--detach", str(outsider))
    assert outsider.is_dir()
    # Registered, outside scratch, and gone from disk — the exact combination
    # that reaches `_deregister_missing` past the boundary, and the one
    # combination the two sibling tolerance tests never cover.
    shutil.rmtree(outsider)
    scratch.mkdir(parents=True, exist_ok=True)
    _write_state(scratch, _state_recording(repo, scratch, "worktrees", outsider))

    assert main(["teardown", "--scratch", str(scratch)]) == 1
    err = capsys.readouterr().err
    assert "refusing to de-register" in err, err
    # Still registered: the refusal has to be the reason, not a coincidence.
    assert "checkout" in _git(repo, "worktree", "list", "--porcelain")
    assert (scratch / STATE_FILENAME).exists(), "a refusal must keep the record"


def test_a_teardown_does_not_unlink_a_later_rounds_state_file(
    repo: Path, scratch: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`missing_ok=True` cannot tell "gone" from "a different round's".

    The tolerance its sibling test pins is right, and it was reached with a
    hook that removed the file. That is only the benign half of the window.
    The harmful half is the same window with a **new setup** in it: T1
    finishes and unlinks, a new setup finds no state file and proceeds,
    writes its own and materializes two worktrees, and T2 — still inside
    `_force_delete_tree` on a several-hundred-MB venv, tens of seconds —
    finishes clean and unlinks the *new* round's file. Those worktrees are
    then on disk and registered, carrying the uncommitted work under review,
    with no record anywhere; the new round's own teardown prints "nothing
    recorded to tear down" and exits 0. That is the silent orphan the
    exclusive create exists to prevent, reached from the other end.

    Reproduced the same way as the sibling — a call inside the window does
    what the other process would — because the outcome, not the interleaving,
    is what has to hold. The new round is represented by its state file
    alone: `_unlink_if_ours` reads nothing else, and materializing a second
    real round would test `git worktree add` rather than this.
    """
    (repo / "tracked.txt").write_text("edit\n", encoding="utf-8")
    assert _setup(scratch) == 0
    state_path = scratch / STATE_FILENAME
    mine = json.loads(state_path.read_text(encoding="utf-8"))
    assert isinstance(mine, dict)
    later = {**cast("dict[str, object]", mine), "nonce": "a-later-round"}
    real_remove = review_worktree._remove_plain_dir  # pyright: ignore[reportPrivateUsage] - a call inside the unlink window

    def _racing_setup(path_str: str, scratch_dir: Path) -> str | None:
        # T1 unlinks, then a new setup claims the scratch dir.
        state_path.unlink(missing_ok=True)
        _write_state(scratch, later)
        return real_remove(path_str, scratch_dir)

    monkeypatch.setattr(review_worktree, "_remove_plain_dir", _racing_setup)
    assert main(["teardown", "--scratch", str(scratch)]) == 0, (
        "this teardown removed everything it recorded; it must still exit clean"
    )

    assert state_path.exists(), "a later round's state file was deleted"
    survived = json.loads(state_path.read_text(encoding="utf-8"))
    assert isinstance(survived, dict)
    assert cast("dict[str, object]", survived)["nonce"] == "a-later-round"


# Every teardown refusal must say how to unwedge, and the three below were the
# siblings of the already-fixed message that never got the remedy. Teardown
# exits 1, which keeps the state file, which makes the next setup refuse — so a
# refusal with no escape text is a permanent wedge, not a safety net.
#
# Three tests rather than one, and not for tidiness: each reaches its message
# through different machinery (a full `main()` teardown, a no-op remover, two
# refusing `Path` methods), so a single name would leave a CI failure unable to
# say which message regressed without reading the traceback, and would make the
# three impossible to skip or track apart. An earlier version named all three
# and exercised one — stripping the remedy from either of the others left the
# entire suite green.


def test_the_containment_refusal_says_how_to_unwedge(
    repo: Path, scratch: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A recorded path outside the scratch dir: refused, with a way out."""
    scratch.mkdir(parents=True, exist_ok=True)
    victim = tmp_path / "outside"
    victim.mkdir()
    _write_state(scratch, _state_recording(repo, scratch, "venvs", victim))

    assert main(["teardown", "--scratch", str(scratch)]) == 1
    err = capsys.readouterr().err
    assert "not strictly inside" in err
    assert "delete the state file" in err


def test_the_stray_registration_report_says_how_to_unwedge(
    repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A removal that reports success while the registration survives.

    Reached by making the remover a no-op, since nothing else produces a stray
    on demand — the fail-loud check exists precisely because no ordinary input
    reaches it.
    """
    (repo / "tracked.txt").write_text("edit\n", encoding="utf-8")
    stray_scratch = tmp_path / "stray-scratch"
    assert main(["setup", "--scratch", str(stray_scratch), "--base", "base"]) == 0

    def _remove_nothing(root: Path, path: str, scratch_dir: Path) -> str | None:
        return None

    patch = pytest.MonkeyPatch()
    patch.setattr(review_worktree, "_remove_worktree", _remove_nothing)
    try:
        assert main(["teardown", "--scratch", str(stray_scratch)]) == 1
    finally:
        patch.undo()

    err = capsys.readouterr().err
    assert "still registered after removal" in err
    assert "delete the state file" in err
    # And the real teardown still clears it, so the refusal is not itself a wedge.
    assert main(["teardown", "--scratch", str(stray_scratch)]) == 0


def test_the_rmdir_fallback_failure_says_how_to_unwedge(
    tmp_path: Path,
) -> None:
    """Both calls refusing — the branch that reports rather than deleting.

    Also pins the invariant underneath the message: neither `unlink` nor
    `rmdir` may remove what the link points at, so a refusal that reached the
    target would be worse than the wedge it reports.
    """
    link_scratch = tmp_path / "link-scratch"
    link_scratch.mkdir()
    target = tmp_path / "link-target"
    target.mkdir()
    (target / "keep.txt").write_text("hours of work\n", encoding="utf-8")
    link = link_scratch / "venv-spec-reviewer"
    _link_dir(link, target)

    def _refuse(self: Path, missing_ok: bool = False) -> None:
        raise OSError(13, "simulated: neither call will remove it")

    def _refuse_rmdir(self: Path) -> None:
        raise OSError(13, "simulated: neither call will remove it")

    patch = pytest.MonkeyPatch()
    patch.setattr(Path, "unlink", _refuse)
    patch.setattr(Path, "rmdir", _refuse_rmdir)
    try:
        error = review_worktree._remove_plain_dir(str(link), link_scratch)  # pyright: ignore[reportPrivateUsage]
    finally:
        patch.undo()

    assert error is not None
    assert "could not remove" in error
    assert "delete the state file" in error
    assert target.is_dir(), "the refusal deleted through the link"
    assert (target / "keep.txt").read_text(encoding="utf-8") == "hours of work\n"


def test_the_only_deleter_refuses_a_path_that_resolves_outside_the_scratch_dir(
    tmp_path: Path,
) -> None:
    """The last-line guard, pinned where it lives rather than through a caller.

    Containment now decides at `_force_delete_tree` — the one function that
    actually deletes — as well as at `_contained`. Reached through a state
    file the two are redundant: every spelling that gets past the syntax check
    also gets past this one, and mutating this guard alone leaves the suite
    green, which is precisely why it needs a direct test rather than a caller's.
    Without one it is an unpinned branch whose comment claims protection, the
    class this branch keeps finding.

    The redundancy is the point: `_remove_plain_dir` had this re-check and
    `_remove_worktree` did not, and a `..` in a recorded path then resolved to
    the directory holding the repository and was recursively deleted with
    teardown returning success. Deciding containment anywhere but at the delete
    gives two places for it to be true and one for it to matter.
    """
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    victim = tmp_path / "outside"
    victim.mkdir()
    (victim / "precious.txt").write_text("hours of work\n", encoding="utf-8")

    failures = review_worktree._force_delete_tree(victim, scratch)  # pyright: ignore[reportPrivateUsage]
    assert failures, "a path outside the scratch dir was accepted for deletion"
    assert "not strictly inside" in failures[0]
    assert "delete the state file" in failures[0], "a refusal with no way out"
    assert (victim / "precious.txt").exists()

    # And the scratch dir itself is not strictly inside itself: deleting it
    # takes the state file, the only record of everything else.
    assert review_worktree._force_delete_tree(scratch, scratch)  # pyright: ignore[reportPrivateUsage]
    assert scratch.is_dir()


def test_the_launcher_probes_deep_paths_through_the_extended_length_form(
    tmp_path: Path,
) -> None:
    """S1-6, at the seam rather than at a >260-character path.

    `_force_delete_tree` hardened only its `rmtree` call: its own `exists()` and
    link guards, and both callers' post-delete verifications, read the bare
    path. `Path.exists()` delegates to `os.path.exists()`, which returns False
    rather than raising past MAX_PATH, so a deep recorded directory took the
    "nothing here" branch, deleted nothing, passed the post-check, and teardown
    printed success over a tree still on disk. A host with the OS-wide
    LongPathsEnabled flag set cannot show the difference, so what is pinned
    here is `_os_path` itself: that it leaves a relative path alone, since
    extended-length paths are not normalized by Windows and a prefixed
    relative path fails every call silently, and that it is idempotent.

    **That every probe goes through it is no longer asserted here.** This test
    also matched two source substrings — `"_is_link(_os_path(path))"` and
    `"if not target.exists():"` — which is the golden-assertion shape its own
    sibling below criticises: each pinned one spelling of one site, leaving
    the others unpinned, and the first was pinning a **double application**
    (`_is_link` already calls `_os_path`) as though it were the requirement.
    `test_no_filesystem_probe_bypasses_the_os_path_wrappers` replaces both,
    checking every gated call in the module by AST.
    """
    assert review_worktree._os_path(Path("relative/path")) == Path("relative/path")  # pyright: ignore[reportPrivateUsage]
    absolute = tmp_path / "x"
    prefixed = review_worktree._os_path(absolute)  # pyright: ignore[reportPrivateUsage]
    if os.name == "nt":
        assert str(prefixed).startswith("\\\\?\\")
        # Idempotent: callers compose it with helpers that apply it again.
        assert review_worktree._os_path(prefixed) == prefixed  # pyright: ignore[reportPrivateUsage]
    else:
        assert prefixed == absolute


def test_the_delete_probes_answer_through_os_path_not_around_it(
    repo: Path, scratch: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The behavioural half of the test above, which was text-matching alone.

    That version asserted one substring and so pinned one of the four
    `_os_path` probes; mutating a *different* one in the same function
    (`target = _os_path(resolved)` back to `target = resolved`) left the whole
    suite green — a golden assertion standing in for the property.

    A >260-character path cannot show the difference on a host with the
    OS-wide LongPathsEnabled flag set, so the seam is exercised the other way
    round: redirect `_os_path` at somewhere that does not exist, and every
    probe genuinely routed through it must now answer "nothing here". A probe
    reading the bare path instead still sees the real directory and deletes
    it, which is exactly the divergence past MAX_PATH produces — the bare
    probe answering False for a path the hardened call can still reach.
    """
    scratch.mkdir(parents=True, exist_ok=True)
    tree = scratch / "venv-spec-reviewer"
    (tree / "Lib").mkdir(parents=True)
    (tree / "Lib" / "keep.txt").write_text("still on disk\n", encoding="utf-8")

    real_os_path = review_worktree._os_path  # pyright: ignore[reportPrivateUsage]
    seen: list[Path] = []

    def _redirected(target: Path) -> Path:
        seen.append(target)
        return real_os_path(target.with_name(target.name + "-elsewhere"))

    monkeypatch.setattr(review_worktree, "_os_path", _redirected)
    failures = review_worktree._force_delete_tree(tree, scratch)  # pyright: ignore[reportPrivateUsage]

    assert failures == [], f"the redirected probe still found something: {failures}"
    assert (tree / "Lib" / "keep.txt").exists(), (
        "a probe read the bare path and deleted a tree the hardened form "
        "could not see — the MAX_PATH divergence this hardening exists to close"
    )
    assert tree in seen, "the tree root was never handed to _os_path at all"

    # And the callers' post-delete verification, which is a separate probe the
    # text-matching version did not reach: it must ask through `_os_path` too,
    # or a deep tree reports removed while still on disk.
    # Behaviourally, not by recording that `_os_path` was called: the same
    # path already reaches it at the link guard, so a call-recording assertion
    # passes with this probe mutated away (measured). Under the redirect the
    # verification must answer "gone" and report success; a bare
    # `located.exists()` sees the real directory and returns "could not
    # remove" — a failure reported for a tree the hardened form cannot see,
    # which past MAX_PATH is the same divergence inverted into silent success.
    assert review_worktree._remove_plain_dir(str(tree), scratch) is None, (  # pyright: ignore[reportPrivateUsage]
        "_remove_plain_dir verified the delete against the bare path"
    )
    assert tree.is_dir(), "the redirected run deleted it after all"


def test_the_worktree_post_delete_check_asks_through_os_path_too(
    repo: Path, scratch: Path, monkeypatch: pytest.MonkeyPatch, git_double: _GitDouble
) -> None:
    """`_remove_worktree`'s verification, the fourth probe and the last unpinned.

    Recording that `_os_path` was *called* cannot distinguish this one: the
    same path already goes through it at the link guard, so a `seen`
    membership check passes with the probe mutated away. The behaviours differ
    instead. Under the same redirect, the correct code's post-check answers
    "gone" and proceeds to de-registration, returning success; a bare
    `located.exists()` still sees the directory and returns "could not
    remove" — reporting a failure for a tree the hardened form cannot see,
    which past MAX_PATH is precisely inverted into reporting success for a
    tree that is still there.

    `git worktree remove` has to fail first, or the function returns before
    reaching the probe at all — git does not route through `_os_path`. Only
    the **first** call is failed: the de-registration retry after the delete
    is the same subcommand, and failing that one too makes the function
    return a de-registration error whatever the probe did, which is a test
    that fails identically on correct and broken code.
    """
    (repo / "tracked.txt").write_text("edit\n", encoding="utf-8")
    assert _setup(scratch) == 0
    worktree = next(iter(_worktrees(scratch).values()))

    def _remove_fails(
        root: Path, *args: str
    ) -> subprocess.CompletedProcess[str] | None:
        if _subcommand(args) == ("worktree", "remove") and git_double.nth(args) == 1:
            return _git_failure(args, "simulated: git cannot traverse it\n")
        return None

    git_double.install(_remove_fails)
    real_os_path = review_worktree._os_path  # pyright: ignore[reportPrivateUsage]

    def _redirected(target: Path) -> Path:
        return real_os_path(target.with_name(target.name + "-elsewhere"))

    monkeypatch.setattr(review_worktree, "_os_path", _redirected)
    error = review_worktree._remove_worktree(repo, worktree, scratch)  # pyright: ignore[reportPrivateUsage]
    monkeypatch.setattr(review_worktree, "_os_path", real_os_path)
    git_double.restore()

    # The discriminating property, and only it. With the probe routed through
    # `_os_path` the redirect makes it answer "gone", so the function proceeds
    # past the could-not-remove branch; reading the bare path sees the real
    # directory and returns an error instead. What happens to the directory
    # afterwards is git's business — the de-registration retry runs for real
    # and removes it — so asserting on that would be asserting on git.
    assert error is None, f"the post-delete check read the bare path: {error}"
    assert main(["teardown", "--scratch", str(scratch)]) == 0
