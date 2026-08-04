"""Reviewer-isolation worktree launcher and teardown (ADR-0068).

Mechanizes the launch procedure in .claude/reviewer-isolation.md — the
snapshot-worktree isolation that lets the spec-reviewer/test-reviewer
subagents run in parallel against uncommitted work without ever touching the
live tree. The procedure shipped first as copy-paste bash and accumulated
five silent failure modes before its first commit; the repo convention for
glue whose failures are silent is a tested script, because prose cannot be
tested, while this can (see scripts/bot_review.py). Rationale lives at the
code that enforces it, and where a rule must also appear in prose the code
site says so and governs — this docstring is only the map.

setup --scratch DIR [--agents a,b] [--base REF]
  Snapshot the uncommitted tracked state (`git stash create`), materialize
  one detached worktree per agent under DIR, replicate untracked files into
  each, and print the manifest: snapshot SHA, base ref, per-agent worktree
  and venv paths, untracked list, and fidelity warnings — pass all of these
  to each reviewer.

teardown --scratch DIR
  Remove every worktree and venv dir the state file
  (DIR/review-worktrees.json) records — each de-registering itself, never a
  repo-global prune — verify nothing it created is still registered, and
  delete the state file.

Exit codes: 0 ready (setup) / clean (teardown); 2 nothing to review
(setup only); 1 abort, usage error, or verification failure. Stdlib only.
Defaults, the state-file name and the exit-code contract are recorded in
ADR-0068 §1.

Uncommitted-state layer — the part a WIP-commit workflow retires. ADR-0068
books that sweep as recorded debt, and its scope should be a boundary in this
file rather than prose in an open question, so: `snapshot`,
`untracked_manifest`, `machine_hidden_untracked`, `index_worktree_divergence`,
`nonstandard_index_flags`, `_copy_untracked` and the fidelity warnings that
hang off them exist ONLY because this workflow commits after review. Against
committed work `git worktree add` carries everything and none of them is
load-bearing. The isolation itself is: invariant 1 and the mutation sandbox
are indifferent to commit state.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import secrets
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import IO, NoReturn, cast

DEFAULT_AGENTS = ("spec-reviewer", "test-reviewer")
DEFAULT_BASE = "origin/main"
STATE_FILENAME = "review-worktrees.json"
# Every malformed-state-file abort ends with this. A teardown that refuses to
# act leaves the operator wedged — the state file also blocks the next setup —
# so each refusal has to say how to get out, not merely what is wrong.
_STATE_REMEDY = (
    " — inspect `git worktree list` by hand, remove any wt-* strays, then "
    "delete the state file"
)
WARN_LIST_CAP = 20

# The keyring backend the reviewers are told to export, printed by setup. Must
# match the value `tests/conftest.py` assigns: the two are the same belt, one
# inside the tree a reviewer may mutate and one outside it. keyring resolves
# this before any real backend, and this one cannot read anything.
_FAIL_BACKEND = "keyring.backends.fail.Keyring"

# Matches COMMAND_TIMEOUT in scripts/bot_review.py's run_cmd: a hung git
# child (a post-checkout hook, fsmonitor) must not block setup forever.
_COMMAND_TIMEOUT = 120

# How long ALL the pipe readers together get to finish once nothing should
# still be writing (`_settle` spends it against one deadline, not per
# stream). It bounds only the tail of a call, never the call: on the
# ordinary path the readers are already done and every join returns at once.
_KILL_GRACE = 5


class AbortError(Exception):
    """Setup/teardown cannot proceed; message is user-facing."""


_GIT = shutil.which("git") or "git"

# Windows job-object constants (winnt.h). A job is the only container on
# that platform whose membership every descendant inherits and which
# survives the process that created the descendant — see `_kill_tree`.
_JOB_EXTENDED_LIMIT_INFORMATION = 9
_JOB_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
_PROCESS_SET_QUOTA = 0x0100
_PROCESS_TERMINATE = 0x0001


def _open_job(pid: int) -> int | None:
    """Put `pid` in a fresh Windows job object; None off Windows or on error.

    `KILL_ON_JOB_CLOSE` is set so the tree is cleaned up even if this
    process dies without reaching `_kill_tree` — the handle closing is
    enough. `_kill_tree` still terminates the job explicitly, because the
    ordinary path must not depend on when a handle happens to be released.

    Assignment happens after `CreateProcess` has returned — closing that
    would need the child spawned suspended, and `subprocess.Popen` closes the
    thread handle before returning — which leaves two windows:

    * a descendant spawned inside that window escapes the job. The window is
      the microseconds before git's first instruction, while the hazard this
      guards — a hook backgrounding a daemon — is many milliseconds of
      process startup later.
    * **if the child has already EXITED, assignment fails and this returns
      None** (measured: `AssignProcessToJobObject` refuses a terminated
      process). A parent already inside a job may also fail to nest one —
      measured under `pytest -n auto`, where the xdist worker is itself in a
      job. Either way a short git command that left a straggler is
      unkillable from here, and `_spawn` aborts rather than return the
      partial output that follows.

    Neither applies on POSIX, where `start_new_session=True` makes the child
    a group leader before it runs and the group outlives it — so the tree
    stays killable however briefly the child lived. The asymmetry is worth
    stating rather than leaving to be rediscovered: it is the platform where
    the guard is weaker, not the one where it is unnecessary. ADR-0068's
    Consequences carry the record as an accepted gap; this docstring points
    at it rather than being it.
    """
    if sys.platform != "win32":  # pragma: no cover - POSIX uses the session
        return None
    import ctypes
    from ctypes import wintypes

    class _BasicLimits(ctypes.Structure):
        _fields_ = (
            ("PerProcessUserTimeLimit", ctypes.c_int64),
            ("PerJobUserTimeLimit", ctypes.c_int64),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_void_p),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        )

    class _IoCounters(ctypes.Structure):
        _fields_ = tuple(
            (name, ctypes.c_uint64)
            for name in (
                "ReadOperationCount",
                "WriteOperationCount",
                "OtherOperationCount",
                "ReadTransferCount",
                "WriteTransferCount",
                "OtherTransferCount",
            )
        )

    class _ExtendedLimits(ctypes.Structure):
        _fields_ = (
            ("BasicLimitInformation", _BasicLimits),
            ("IoInfo", _IoCounters),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        )

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.OpenProcess.restype = wintypes.HANDLE
    job = kernel32.CreateJobObjectW(None, None)
    if not job:
        return None
    limits = _ExtendedLimits()
    limits.BasicLimitInformation.LimitFlags = _JOB_LIMIT_KILL_ON_JOB_CLOSE
    handle = kernel32.OpenProcess(_PROCESS_SET_QUOTA | _PROCESS_TERMINATE, False, pid)
    try:
        assigned = bool(handle) and bool(
            kernel32.SetInformationJobObject(
                wintypes.HANDLE(job),
                _JOB_EXTENDED_LIMIT_INFORMATION,
                ctypes.byref(limits),
                ctypes.sizeof(limits),
            )
            and kernel32.AssignProcessToJobObject(
                wintypes.HANDLE(job), wintypes.HANDLE(handle)
            )
        )
    finally:
        if handle:
            kernel32.CloseHandle(wintypes.HANDLE(handle))
    if not assigned:
        kernel32.CloseHandle(wintypes.HANDLE(job))
        return None
    return int(job)


def _close_job(job: int | None) -> None:
    """Release the job handle. Off Windows, or with no job, a no-op."""
    if job is None or sys.platform != "win32":  # pragma: no cover - POSIX
        return
    import ctypes
    from ctypes import wintypes

    ctypes.WinDLL("kernel32", use_last_error=True).CloseHandle(wintypes.HANDLE(job))


def _kill_tree(proc: subprocess.Popen[bytes], job: int | None) -> None:
    """Kill `proc` AND every descendant it left behind.

    `Popen.kill()` reaches one process, and a parent-PID tree walk reaches
    only what is still parented — which is exactly the link the hazard here
    breaks. `git worktree add` runs the post-checkout hook, the hook
    backgrounds an indexer or an fsmonitor daemon, and git exits; the daemon
    is reparented and holds the inherited pipe write ends open. So both
    platforms get a container membership is inherited into and that outlives
    the intermediate process: a session (hence process group) on POSIX,
    a job object on Windows.

    Callers must only reach this while something in the tree is still
    running. On POSIX an empty group's pgid is a freed pid, and signalling
    it could reach an unrelated process that has since been given it.
    """
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        if job is not None:
            kernel32.TerminateJobObject(wintypes.HANDLE(job), 1)
            return
        # The job could not be created; one process beats none.
        with contextlib.suppress(OSError):  # pragma: no cover - already gone
            proc.kill()
        return
    # `start_new_session=True` made the child its own group leader, so its
    # pid is the pgid, and every descendant inherits the group.
    with contextlib.suppress(OSError):  # pragma: no cover - group may be empty
        os.killpg(proc.pid, signal.SIGKILL)


def _drain(pipe: IO[bytes], sink: dict[str, bytes], key: str) -> None:
    """Read one pipe to EOF into `sink`; never raise into the reader thread.

    An exception here would be lost with the thread and the caller would
    silently read an empty stream as an empty result — the same
    failed-query-reads-as-empty shape `_git_z` aborts on. Whatever arrived
    before the pipe was closed under us is what the caller gets, and on that
    path the caller is already raising.
    """
    try:
        with pipe:
            sink[key] = pipe.read()
    except (OSError, ValueError):  # fmt: skip
        pass


def _settle(readers: list[threading.Thread]) -> bool:
    """Join the pipe readers under ONE shared grace. True if all finished.

    A per-reader timeout would multiply by the number of streams — measured
    at 10.2s for the two here against a 5s grace — so the wait is against a
    single deadline. That keeps the whole call bounded by
    `_COMMAND_TIMEOUT + 2 * _KILL_GRACE`: the settle after a kill costs
    nothing, because killing the tree is what releases the write ends.

    Measured, not test-pinned, and deliberately: reverting this to
    `reader.join(_KILL_GRACE)` leaves
    `test_a_straggler_holding_the_pipes_cannot_outlast_the_timeout` green,
    because both forms are bounded and that test asserts boundedness. Pinning
    the constant needs a ceiling tight enough to separate 1x from 2x the
    grace, which on a loaded CI runner is a flaky test — a worse trade than
    an unpinned constant factor. What IS pinned there is that the call comes
    back at all, and with git's answer.
    """
    deadline = time.monotonic() + _KILL_GRACE
    for reader in readers:
        reader.join(max(0.0, deadline - time.monotonic()))
    return not any(reader.is_alive() for reader in readers)


def _spawn(argv: list[str], label: str) -> subprocess.CompletedProcess[bytes]:
    """Run `argv` to completion under a HARD wall-clock bound, in raw bytes.

    `subprocess.run(timeout=...)` mishandles a child that leaves a straggler
    holding the inherited pipe write ends — the shape `git worktree add`
    produces when the post-checkout hook backgrounds an indexer or an
    fsmonitor daemon and git then exits. EOF on those pipes does not arrive
    until every holder closes them, and `run` reads to EOF. Measured against
    a 60s straggler at `timeout=3`, on both legs, before this rewrite:

    * **Windows, 60.2s — unbounded.** `run`'s timeout handler kills the
      direct child and then calls `communicate()` a SECOND time with no
      deadline (its `if _mswindows:` branch). The call blocks for as long as
      the straggler lives, so the round hangs with no diagnostic instead of
      raising the AbortError the timeout triage in
      .claude/reviewer-isolation.md keys on.
    * **POSIX, 3.0s — bounded, and a FALSE abort.** That branch calls only
      `process.wait()`, so the bound holds. The damage is the answer: a git
      call that exited 0 comes back as TimeoutExpired, and a healthy round
      aborts with `did not return within 120s`.

    The wall-clock half is therefore Windows-only; the wrong-answer half is
    on both legs, and the report that found this described only the first.
    Three things this owns that the stdlib call does not:

    * the child goes into a container the whole tree lands in (`_kill_tree`);
    * the pipes are drained by daemon threads, so no inherited handle can
      block the thread doing the waiting;
    * a straggler is killed on the SUCCESS path too — the timeout never
      fires there, because the process being waited on is already gone.

    Both legs now return the output (measured: rc=0, stdout intact), bounded
    by `_COMMAND_TIMEOUT + 2 * _KILL_GRACE`.

    Returns raw bytes: `_run` decodes, `_git_z` splits on NUL first.
    """
    try:
        proc = subprocess.Popen(  # noqa: S603 - fixed executable, no shell
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            # POSIX-only and accepted-and-ignored on Windows, where
            # `_open_job` is the equivalent. Both hand `_kill_tree` a
            # container rather than a single pid.
            start_new_session=True,
        )
    except OSError as exc:
        raise AbortError(f"`{label} ...` could not be started: {exc}") from exc

    job = _open_job(proc.pid)
    captured: dict[str, bytes] = {"stdout": b"", "stderr": b""}
    readers = [
        threading.Thread(target=_drain, args=(pipe, captured, key), daemon=True)
        for key, pipe in (("stdout", proc.stdout), ("stderr", proc.stderr))
        if pipe is not None
    ]
    for reader in readers:
        reader.start()
    try:
        try:
            code = proc.wait(timeout=_COMMAND_TIMEOUT)
        except subprocess.TimeoutExpired as exc:
            _kill_tree(proc, job)
            _settle(readers)
            raise AbortError(
                f"`{label} ...` did not return within {_COMMAND_TIMEOUT}s"
            ) from exc
        if not _settle(readers):
            _kill_tree(proc, job)
            if not _settle(readers):
                # The straggler outlived the kill, so what was read is a
                # PREFIX of the output and there is no way to tell from here
                # how much is missing. Returning it would be the false-clean
                # shape `_git_z` aborts on one layer down: a truncated path
                # list is indistinguishable from a short one, and
                # `personal_tracked` reading `[]` means "nothing personal is
                # tracked, proceed". Reachable on Windows when git exits
                # before `_open_job` can assign it (see there); POSIX has no
                # such window, because the process group outlives its leader.
                raise AbortError(
                    f"`{label} ...` exited but something it spawned still "
                    f"holds its output pipes after {_KILL_GRACE}s, so its "
                    "output cannot be read completely — refusing to act on a "
                    "partial result. Check for a post-checkout hook or "
                    "fsmonitor daemon left running, then rerun"
                )
    finally:
        _close_job(job)
    return subprocess.CompletedProcess(
        argv, code, captured["stdout"], captured["stderr"]
    )


def _text(raw: bytes) -> str:
    """Decode as `text=True, encoding="utf-8", errors="replace"` used to.

    errors="replace" because a filename git emits in raw bytes must surface
    as a failed copy or a visible mangled path downstream — not kill the run
    here with a UnicodeDecodeError that no except clause owns. The newline
    collapse is what `text=True` did through `io.TextIOWrapper`, kept so
    moving off it changed the timeout bound and nothing else.
    """
    return (
        raw.decode("utf-8", errors="replace").replace("\r\n", "\n").replace("\r", "\n")
    )


def _run(*cmd: str, label: str) -> subprocess.CompletedProcess[str]:
    # Deliberately simpler than the run_cmd near-twins in bot_review.py and
    # gemini_review_agent.py (callers here inspect the CompletedProcess
    # instead of getting check semantics), but aligned on their hardening:
    # no shell, utf-8 decode, the shared timeout.
    #
    # `label` is required rather than sliced off `cmd`, because `cmd` is the
    # built argv and the builder puts global options first: a `cmd[:3]` slice
    # printed `git -C <root>` for every timeout and never once named the
    # subcommand that hung, which is exactly the split the timeout triage in
    # .claude/reviewer-isolation.md asks the operator to make. Making the
    # caller name its command is the only form that cannot drift when another
    # global option is added — the same drift `_LONGPATHS` itself caused.
    proc = _spawn(list(cmd), label)
    return subprocess.CompletedProcess(
        proc.args, proc.returncode, _text(proc.stdout), _text(proc.stderr)
    )


# Passed to EVERY git call, not to the sites thought to need it: `worktree
# remove` was fixed for Windows MAX_PATH while `worktree add` — the call that
# actually writes the deepest paths — stayed uncovered, which is what scoping
# this flag by hand buys. Accepted and inert on POSIX git. The measurement
# behind it is recorded once, in ADR-0068 §4.
_LONGPATHS = ("-c", "core.longpaths=true")


def _git_argv(root: Path | None, *args: str) -> list[str]:
    """Build every git command line this module runs, in one place.

    One builder, so `_LONGPATHS` above cannot be scoped to the calls someone
    believed needed it. `root` is None only for the repository-discovery
    probe, which has no root to pass yet.
    """
    located = ["-C", str(root)] if root is not None else []
    return [_GIT, *located, *_LONGPATHS, *args]


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return _run(*_git_argv(root, *args), label=f"git {' '.join(args[:2])}")


def _git_ok(root: Path, *args: str) -> str:
    """Run git, aborting with the full error on nonzero exit."""
    proc = _git(root, *args)
    if proc.returncode != 0:
        raise AbortError(
            f"`git {' '.join(args)}` exited {proc.returncode}:\n"
            f"{proc.stdout}{proc.stderr}".rstrip()
        )
    return proc.stdout


def _git_z(root: Path, *args: str, errors: str = "replace") -> list[str]:
    """Run a NUL-delimited git query and split it, byte-safely.

    Every place this module reads a list of paths out of git goes through
    here, because both of the obvious shortcuts are wrong on a path that is
    merely unusual rather than malicious:

    * Splitting `--porcelain` output with `str.splitlines()` splits on `\\x0b
      \\x0c \\x1c \\x1d \\x1e \\x85 \\u2028 \\u2029` as well as `\\n`, and git
      emits worktree paths **raw** — no C-quoting, even at the default
      `core.quotePath`. A path holding any of those bytes yields a truncated
      key that matches nothing, so the fail-loud stray check silently finds
      zero strays and teardown reports success over a live registration.
    * `text=True` applies universal-newline translation, which rewrites a CR
      *inside* a NUL-delimited name into LF — undoing the exact property `-z`
      was chosen for. Decoding the raw bytes here keeps every byte but the
      separators intact.

    `errors` defaults to "replace" for the same reason `_run` uses it: a
    non-UTF-8 name must surface downstream as a visibly mangled path or a
    failed copy, not kill the run with a UnicodeDecodeError no except clause
    owns. Callers that compare the result against a path this process already
    holds — `_registered_paths` is the one — must override it, because
    "replace" is lossy: it maps every undecodable byte to the same U+FFFD, so
    the decoded string can never equal the surrogate-carrying string `argv`
    and the state file hold, and an equality check across that boundary
    silently finds nothing.
    """
    proc = _spawn(_git_argv(root, *args), f"git {' '.join(args[:2])}")
    if proc.returncode != 0:
        detail = (proc.stdout + proc.stderr).decode("utf-8", errors="replace")
        raise AbortError(
            f"`git {' '.join(args)}` exited {proc.returncode}:\n{detail}".rstrip()
        )
    return [
        chunk.decode("utf-8", errors=errors)
        for chunk in proc.stdout.split(b"\0")
        if chunk
    ]


def repo_root() -> Path:
    proc = _run(*_git_argv(None, "rev-parse", "--show-toplevel"), label="git rev-parse")
    if proc.returncode != 0:
        raise AbortError("not inside a git repository")
    # Resolved so containment comparisons against a resolved scratch path
    # cannot be defeated by symlinks, mapped drives, or 8.3 short names.
    return Path(proc.stdout.strip()).resolve()


_AGENT_NAME_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-"
)


def parse_agents(raw: str) -> list[str]:
    """Validate --agents: worktree paths are built from these names.

    Duplicates would create worktrees the state file cannot record (dict
    keyed by name → the extra one is never torn down); separators in the
    wrong place would escape the scratch dir the containment guard exists to
    enforce. Duplicate-checked case-*insensitively* because the per-agent
    venv paths are fixed names on a filesystem that usually is too:
    `Reviewer` and `reviewer` pass a case-sensitive check and then share one
    venv dir, so both reviewers export the same UV_PROJECT_ENVIRONMENT and
    their concurrent `uv` builds race in it.

    `.` is not an allowed character, which is a stronger rule than rejecting
    the literal `.` and `..`. Win32 strips *trailing* dots from a path
    component, so `a` and `a.` are two names that pass the case-fold dedupe
    and then resolve to one directory (`...` collapses to `venv-`) —
    reintroducing that same shared-venv race on the axis case-folding does
    not cover. Excluding the character removes `.`/`..` as a side effect,
    with no special case left to keep in sync.
    """
    agents = [a.strip() for a in raw.split(",") if a.strip()]
    if not agents:
        raise AbortError("--agents named no agents")
    if len({a.casefold() for a in agents}) != len(agents):
        raise AbortError(f"--agents contains duplicates: {raw!r}")
    for agent in agents:
        if not set(agent) <= _AGENT_NAME_CHARS:
            raise AbortError(
                f"invalid agent name {agent!r}: allowed characters are "
                "letters, digits, '_' and '-'"
            )
    return agents


def parse_base(raw: str) -> str:
    """Validate --base: it is interpolated into git revision arguments.

    `f"{base}...HEAD"` is one argv element, but git parses argv elements
    beginning with `-` as its own options — so `--base=--output=<path>` made
    `git diff` write a file named `<path>...HEAD` INSIDE the repository
    (reproduced), which is the one thing invariant 1 says this launcher never
    does. A `--` separator is not the fix here: everything this value reaches
    is a revision, and revisions must precede `--`, not follow it.

    Empty is refused for the adjacent reason — `"...HEAD"` is a valid revision
    range meaning the empty tree, so it would silently review against
    something the operator did not name.
    """
    base = raw.strip()
    if not base:
        raise AbortError("--base is empty")
    if base.startswith("-"):
        raise AbortError(
            f"invalid base ref {base!r}: a value starting with '-' is parsed by "
            "git as an option, not a revision"
        )
    return base


_PERSONAL_DIR = "specs/personal"

# Exactly the characters `str.splitlines()` treats as terminators — the set
# `_git_z` reads paths byte-safely to survive on the way IN. The way OUT is
# line-oriented too: the launch procedure parses this script's stdout by line,
# so a path carrying one of these can forge a `state:` line aiming a later
# teardown at a state file of its choosing, or a bare `\r` can overwrite the
# preceding line — silently erasing the skip-worktree warning this module
# calls the one signal that a genuinely-changed tree can look clean. Hardening
# only the input side left the manifest printing raw.
_LINE_BREAKS = frozenset("\n\r\v\f\x1c\x1d\x1e\x85\u2028\u2029")


def _is_personal(rel: str) -> bool:
    """Whether a git-reported path is the personal directory or inside it.

    Case-folded because git preserves on-disk casing while the filesystems
    this project runs on (Windows, macOS) do not: `Specs/Personal/` is the
    very directory the containment rule names.

    The bare path is matched as well as the prefix, and that is not
    defensive. `.gitignore`'s rule is `specs/personal/` — trailing slash, so
    git matches it against *directories only* — which means a plain FILE at
    exactly `specs/personal` is not ignored, is reported by `ls-files
    --others`, and a prefix-only test does not match it. Measured: it was
    copied into both agent-readable worktrees with setup exiting 0.
    """
    folded = rel.casefold()
    return folded == _PERSONAL_DIR or folded.startswith(_PERSONAL_DIR + "/")


def personal_tracked(root: Path) -> list[str]:
    """Tracked paths at or under specs/personal/, matched case-insensitively.

    `:(icase)` rather than a plain pathspec: git's pathspec matching is
    case-sensitive even where the filesystem is not, so on Windows/macOS a
    force-added `Specs/Personal/labs.md` is invisible to `-- specs/personal/`
    while `git worktree add` cheerfully checks it out into every worktree.
    Measured: with no lowercase entry already in the index to case-correct
    against, the plain pathspec returns nothing for exactly that path. The
    untracked twin (`_is_personal`) is `casefold()`ed for the same reason — a
    miss on either guard replicates health data into two agent-readable
    worktrees.

    No trailing slash on the pathspec, matching `_is_personal`'s bare-path
    case: `-- ":(icase)specs/personal/"` misses a tracked file at exactly
    `specs/personal` (measured), and git matches a slashless pathspec at
    directory boundaries — so dropping it catches the file without matching
    a sibling like `specs/personal-notes/` (measured both ways).

    `-z` for the same reason every other path list here uses it (see
    `_git_z`): this one reaches an abort message and a `len()`.
    """
    return _git_z(root, "ls-files", "-z", "--", f":(icase){_PERSONAL_DIR}")


def tracked_entries(root: Path) -> list[tuple[str, str]]:
    """(mode, path) for every tracked entry, from one `ls-files -s -z`.

    The mode is read from the index rather than from disk because the two
    disagree on exactly the case that matters: git records a symlink as mode
    `120000` whatever the checkout did with it, while a Windows clone without
    `core.symlinks` materializes one as an ordinary file holding the target
    text — so an on-disk `is_symlink()` sweep finds nothing there. Reading the
    index also costs one git call instead of an lstat per tracked file.

    Format is `<mode> <sha> <stage>\\t<path>`, NUL-separated by `-z`.

    `errors="surrogateescape"`, overriding `_git_z`'s default, because these
    paths are turned into filesystem paths rather than merely counted:
    `_linked_dirs` builds `root / prefix` from the components and
    `_leaks_personal` gets `root / rel` for every mode-120000 entry. Under
    "replace" a name git emitted in raw bytes becomes U+FFFD, which names
    nothing on disk, so `.resolve()` finds nothing and BOTH personal-data
    predicates answer False — measured on the POSIX legs with a tracked
    symlink `panel-\\xff-2026 -> <repo>/specs/personal/labs.md`, which passed
    every guard and was checked out into both worktrees. Surrogateescape
    round-trips to the original bytes; "replace" cannot. Same reasoning, and
    the same one-line override, as `_registered_paths`.

    That leak was caught downstream by the post-materialization walk
    (`_artifact_breaches`) even before this override — which is the argument
    for the walk, not a reason to leave the guard blind: the walk refuses the
    round, while a working guard names the cause.
    """
    entries: list[tuple[str, str]] = []
    for entry in _git_z(root, "ls-files", "-s", "-z", errors="surrogateescape"):
        meta, _, rel = entry.partition("\t")
        if rel:
            entries.append((meta.split(" ", 1)[0], rel))
    return entries


def snapshot(root: Path) -> str | None:
    """`git stash create`: SHA of the uncommitted tracked state, None if clean.

    Any nonzero exit aborts. A mid-merge tree exits 1 with an error printed
    to *stdout* (which a shell would capture as a garbage "SHA"), and an
    intent-to-add entry exits 1 with empty stdout (which a `${SNAP:-HEAD}`
    fallback would silently misread as a clean tree). Neither may proceed.
    """
    proc = _git(root, "stash", "create", "review-snapshot")
    if proc.returncode != 0:
        detail = f"{proc.stdout}{proc.stderr}".rstrip()
        # A held `.git/index.lock` — a concurrent git, an IDE's git
        # integration, fsmonitor, a stale lock from a killed process, or a
        # second setup in the same repo — exits 1 here too. Volunteering
        # "conflicted or unmergeable index state?" for it was actively
        # harmful: reviewer-isolation.md files an unmergeable index under
        # **blocking**, so the orchestrator read that guess and dropped the
        # round to the unisolated fallback permanently, when the correct
        # remedy was to wait a second and rerun. Reproduced with two
        # concurrent setups. The two are distinguishable in git's own stderr,
        # so read it rather than guessing.
        if "index.lock" in detail or "could not write index" in detail:
            raise AbortError(
                "git stash create could not write the index — another git "
                "process holds .git/index.lock (a concurrent git, an editor's "
                "git integration, fsmonitor, or a stale lock from a killed "
                f"process). Wait a moment and rerun: {detail}"
            )
        raise AbortError(
            "git stash create failed (conflicted or unmergeable index state?) — "
            f"exit {proc.returncode}: {detail}"
        )
    sha = proc.stdout.strip()
    return sha or None


def untracked_manifest(root: Path) -> list[str]:
    return _git_z(root, "ls-files", "-z", "--others", "--exclude-standard")


def machine_hidden_untracked(root: Path, standard: list[str]) -> list[str]:
    """Untracked files hidden only by machine-local ignore rules.

    --exclude-standard honors .git/info/exclude and core.excludesFile, both
    invisible in the repo; --exclude-per-directory applies only the tracked
    .gitignore files. The difference is what this machine hides that another
    clone would list.
    """
    repo_only = set(
        _git_z(root, "ls-files", "-z", "--others", "--exclude-per-directory=.gitignore")
    )
    return sorted(repo_only - set(standard))


def _exists_cased(root: Path, rel: str) -> bool:
    """Whether `rel` is on disk under exactly the casing git reported.

    `Path.exists()` is case-INsensitive on Windows and macOS while git's own
    paths are case-sensitive, so the two disagree precisely when a file has
    been renamed by case. `git rm --cached docs/Setup.md` plus an on-disk
    rename to `docs/setup.md` made `.exists()` answer True for a path git
    considers gone, and setup then aborted claiming index/worktree divergence
    where index and snapshot agreed exactly — a false hard abort whose printed
    remedy makes it worse, and whose only exit is the unisolated fallback this
    module exists to avoid.

    Every component is checked, not just the leaf: a directory can be the part
    that differs in case, and `root / "Docs" / "setup.md"` would otherwise
    match a `docs/setup.md` on disk. `scandir` per component rather than a
    `resolve()` comparison, because `resolve()` canonicalizes the casing away
    — which is the very difference being measured.

    **`rel` must be surrogateescape-decoded**, which is why the caller
    overrides `_git_z`'s default. This compares git's spelling against
    `os.scandir` names, and the two decode undecodable bytes differently:
    scandir uses surrogateescape (PEP 383), while "replace" maps every such
    byte to the same U+FFFD. Measured: `b"a\\xffb"` is `"a\\ufffdb"` on the
    git side and `"a\\udcffb"` on the scandir side — never equal, so this
    silently answers False for every path with a non-UTF-8 name and the
    divergence abort it feeds cannot fire.
    """
    current = root
    for part in rel.split("/"):
        try:
            # `any` over the iterator, not a set of every sibling name: this
            # runs once per path component, and building the full set costs
            # the whole directory's size to answer a single membership
            # question. Short-circuits on the match.
            with os.scandir(_os_path(current)) as entries:
                found = any(entry.name == part for entry in entries)
        except OSError:
            return False
        if not found:
            return False
        current = current / part
    return True


def index_worktree_divergence(root: Path) -> list[str]:
    """Paths whose staged content differs from the worktree content.

    The snapshot holds worktree content, but a plain `git commit` (no -a)
    publishes the index — bytes the review then never saw. Setup aborts on
    this rather than warning: every call site treats warnings as advisory,
    and a pass must never be earnable for a state that cannot be the one
    that lands.
    """
    # `errors="surrogateescape"` throughout, overriding `_git_z`'s default,
    # because `_exists_cased` below takes these strings to the filesystem.
    # Under "replace" a name git emitted in raw bytes becomes U+FFFD, which
    # matches no `os.scandir` entry (those carry the same bytes as lone
    # surrogates, PEP 383) — so `_exists_cased` answered False for every
    # component after the bad byte, `still_present` came back empty, this
    # function returned `[]`, and the hard abort never fired: a staged
    # deletion shipped unreviewed. Unlike the containment guards, nothing
    # downstream backstops this one. Applied to all three queries rather than
    # only the one that needs it, so the returned list has a single spelling.
    staged = set(
        _git_z(root, "diff", "--cached", "--name-only", "-z", errors="surrogateescape")
    )
    unstaged = set(_git_z(root, "diff", "--name-only", "-z", errors="surrogateescape"))
    # A staged deletion of a file still on disk is divergence the
    # intersection alone cannot see, because `git rm --cached` drops the path
    # from the index entirely: `git diff --name-only` (index vs worktree)
    # stops reporting it and it reappears under `ls-files --others` instead.
    # Measured: the snapshot tree and the untracked copy-in then BOTH carry
    # the file, every reviewer sees it present and unremarkable, and the
    # plain `git commit` that follows publishes its deletion — a pass earned
    # for a state that cannot be the one that lands, which is precisely what
    # this abort exists to prevent.
    deleted = _git_z(
        root,
        "diff",
        "--cached",
        "--name-only",
        "--diff-filter=D",
        "-z",
        errors="surrogateescape",
    )
    still_present = {rel for rel in deleted if _exists_cased(root, rel)}
    return sorted((staged & unstaged) | still_present)


def nonstandard_index_flags(root: Path) -> list[str]:
    """ls-files -v entries flagged skip-worktree or assume-unchanged.

    Modifications to such files are invisible to `git stash create`, so the
    snapshot silently lacks them. Only the tags meaning those two flags
    count — `S` is skip-worktree, a lowercase tag is assume-unchanged;
    matching "anything but H" would mislabel unmerged entries (`M`) and any
    future tag letter as hidden-from-snapshot.

    Read through `_git_z` like every other path list here. Reading it with
    `splitlines()` instead was not merely untidy: that splits on `\\x0b \\x0c
    \\x1c \\x1d \\x1e \\x85 \\u2028 \\u2029` as well as `\\n`, so a tracked
    path holding any of them yields a fragment whose first character is then
    read as the tag — and any fragment starting with a lowercase letter
    becomes a *phantom* assume-unchanged warning, which rides into the state
    file and the reviewers' reports as evidence of a hazard that does not
    exist. `-v -z` keeps the `<tag><space><path>` shape, NUL-separated.
    """
    flagged: list[str] = []
    for entry in _git_z(root, "ls-files", "-v", "-z"):
        tag = entry[:1]
        if tag == "S" or (tag.isalpha() and tag.islower()):
            flagged.append(entry)
    return flagged


def branch_has_diff(root: Path, base: str) -> bool | None:
    """Whether base...HEAD is non-empty; None when git cannot answer.

    `git diff --quiet` exits 0 for no differences, 1 for differences, and
    >1 for fatal — 128 when the base does not resolve or shares no merge
    base with HEAD (orphan history, shallow clone, force-pushed base).
    Mapping the codes explicitly keeps a fatal exit from reading as "has
    differences", which would print a manifest advertising a diff command
    the reviewers cannot run either.
    """
    proc = _git(root, "diff", "--quiet", f"{base}...HEAD")
    if proc.returncode == 0:
        return False
    if proc.returncode == 1:
        return True
    return None


def _capped_after_personal_filters(paths: list[str]) -> str:
    """Join a path list for a message, truncated to `WARN_LIST_CAP`.

    The name is the precondition, and that is the point of it. This is the one
    helper in the module that echoes real path names into stderr, the session
    transcript, relayed reviewer reports and `state["warnings"]` — four
    publications — and printing names used to be what the *default*-named
    helper did, with redaction the thing each guard had to remember
    separately. Two of those guards leaked in separate rounds, each after the
    discipline was declared uniform. Inverting it means anyone who wants names
    has to type a sentence asserting where they are, and a reviewer reading a
    call site sees the claim being made rather than a neutral formatter.

    **The guards that earn it** are `personal_tracked`, the untracked
    `_is_personal` filter, the `_leaks_personal` link check over path
    components, tracked mode-120000 entries and untracked link leaves, and the
    hard-link identity check — all aborts placed earlier in `setup`.

    **One call site**: the files that vanished between the manifest and the
    copy, where the remedy is "rerun, or look at what moved" and a bare count
    is unusable. The two symlink messages that used to call this now print a
    count, because an untracked link's own leaf name is chosen by whoever made
    it and is exactly the provenance CLAUDE.md protects. Anything added later
    that cannot state the ordering prints `len(...)` instead, as every other
    path-bearing message here does.
    """
    shown = ", ".join(paths[:WARN_LIST_CAP])
    extra = len(paths) - WARN_LIST_CAP
    return shown + (f", +{extra} more" if extra > 0 else "")


def _is_link(path: Path) -> bool:
    """Whether a path is a link of any kind this module must not act through.

    `is_symlink()` alone is not that test on the primary platform. A Windows
    directory *junction* reports `is_symlink()` False (`os.lstat` sets no
    S_IFLNK) while `resolve()` follows it exactly as it follows a symlink —
    and `mklink /J` needs neither administrator rights nor Developer Mode,
    unlike `mklink /D`, so it is the link form a Windows user can actually
    create. Every guard here that was written against symlinks was therefore
    open on the case more likely to occur: measured, a junction recorded as a
    venv path had its target force-deleted from OUTSIDE the scratch dir while
    teardown printed success, and a junction in the tree replicated content
    from outside the repository into both agent-readable worktrees.

    `Path.is_junction` is 3.12+ and returns False on POSIX, where the
    concept does not exist.

    Probed through `_os_path` for the same reason `_force_delete_tree`'s other
    guards are: both calls below answer False rather than raising for a path
    past MAX_PATH, so a deep recorded link would read as "not a link" and be
    deleted *through*. Every guard in this module that must not be fooled by a
    link is one call to here, so this is the one place that has to know.
    """
    probe = _os_path(path)
    return probe.is_symlink() or probe.is_junction()


def _linked_dirs(root: Path, rels: list[str]) -> list[str]:
    """The unique directory components of git-reported paths that are links.

    git walks *through* a directory junction on Windows and reports the files
    beneath it as ordinary paths, so the link is never the leaf and never
    appears in the listing under its own name: `notes/labs.md` arrives with
    `notes` — the junction — mentioned only as a path component. Checking the
    components is the only way to see it, and doing it over the unique set
    keeps the cost proportional to the tree's directory count rather than its
    file count (measured on this repo: 34 prefixes for 251 tracked files).

    Leaves are deliberately not checked. A leaf that is itself a link is
    reported by git as a link and handled by the `_is_link` filters, which
    drop it before anything reads through it.
    """
    checked: set[str] = set()
    linked: list[str] = []
    for rel in rels:
        parts = rel.split("/")[:-1]
        for depth in range(1, len(parts) + 1):
            prefix = "/".join(parts[:depth])
            if prefix in checked:
                continue
            checked.add(prefix)
            if _is_link(root / prefix):
                linked.append(prefix)
    return linked


def _leaks_personal(path: Path, root: Path) -> bool:
    """Whether `path` resolves into specs/personal/, whatever name it wears.

    `_is_personal` reads the name git printed; this reads where that name
    actually goes. Both are needed and neither subsumes the other: git walks
    *through* a directory junction on Windows and reports `notes/labs.md` with
    no marker of any kind (`mklink /J` needs no privilege), while a path that
    is genuinely under specs/personal/ may not resolve at all if it has since
    been deleted.

    Routed back through `_is_personal` rather than comparing against a
    resolved `specs/personal` boundary, so the case-folding and the bare-file
    case stay in one place — a resolved-path comparison would be
    case-sensitive on exactly the filesystems where `Specs/Personal` is the
    same directory.

    **Not covered: a hard link.** `mklink /H` gives one file a second name
    with no target to resolve, so no predicate here can see it; `st_nlink`
    plus an identity comparison could, at the cost of walking the owner's real
    data directory on every setup. Out of scope deliberately — a hard link to
    an individual file under specs/personal/ is a deliberate act, whereas a
    directory junction is the form a build tool or a convenience link creates
    by accident. ADR-0068's Consequences carry the record — as an accepted
    gap rather than a verification-record entry, since nothing was measured
    and nothing was fixed — and this docstring points at it rather than being
    it, because the boundary of a containment guarantee has to be findable
    from the document a reader consults for the guarantee.
    """
    if path.is_symlink() and not Path(os.readlink(path)).is_absolute():
        # A RELATIVE tracked symlink is harmless and must not abort. It
        # resolves here against the live tree, where `specs/personal/` exists
        # — but git recreates the link verbatim in each worktree, where the
        # same relative target names `<worktree>/specs/personal`, which is
        # gitignored and therefore never checked out. The link dangles and
        # there is no data behind it: exactly what the comment above calls
        # harmless. Measured under WSL — `panel -> specs/personal/labs.md`
        # committed, then read from inside both worktrees: no such file.
        #
        # Resolving against the live tree is what made this abort, and the
        # abort is classified *blocking*, so it dropped the round to the
        # unisolated live-tree fallback this module exists to avoid — the
        # same false-hard-abort class `_exists_cased` was written to fix.
        # Only an ABSOLUTE target keeps pointing at the live repo from
        # inside a worktree, which is the case that leaks.
        return False
    try:
        rel = path.resolve().relative_to(root)
    except (ValueError, OSError):  # fmt: skip
        # Outside the repository entirely (the `_escapes` filter owns that
        # case), or unresolvable. Neither is this predicate's business.
        return False
    return _is_personal(rel.as_posix())


def _escapes(path: Path, boundary: Path) -> bool:
    """Whether `path` resolves outside `boundary` — the containment test.

    Testing the *resolved* path is what makes this cover a link anywhere in
    the path, not merely at the leaf. A junction is an ancestor component of
    everything git reports beneath it, so a leaf-level `_is_link` check sees
    nothing unusual about `linked-dir/labs.md`: the junction is `linked-dir`,
    and both it and the leaf answer False.

    The exposure is asymmetric, and worth stating so nobody "simplifies" this
    away after testing only POSIX. **Windows:** git walks *through* a junction
    and reports the files under it as ordinary untracked paths — measured —
    so this predicate is the only thing standing between an out-of-tree
    junction and both worktrees. **POSIX:** git never walks through a
    symlinked directory; it reports the link itself as one entry, which the
    leaf-level `_is_link` filter already catches. So here this is defense in
    depth rather than the load-bearing guard. Same predicate either way, and
    it needs no platform branch — only its reachability differs.
    """
    return not path.resolve().is_relative_to(boundary)


def _personal_identities(root: Path) -> set[tuple[int, int]]:
    """Every file identity under the live specs/personal/, for the walk below.

    `(st_dev, st_ino)` rather than a path, because identity is what survives a
    second name. A hard link gives one file another name with no target to
    resolve, so `_leaks_personal` cannot see it and no path-shaped predicate
    can — and one walk of one small directory answers it for every route at
    once, which is the cost ADR-0068's Consequences declined back when the
    only thing on offer was a hard-link-specific check.

    An unreadable entry is skipped rather than raised on. This set is one half
    of a fail-closed comparison and the escape half below does not depend on
    it, so a locked-down directory degrades the check instead of refusing the
    round.
    """
    identities: set[tuple[int, int]] = set()
    for dirpath, _dirs, files in os.walk(root / _PERSONAL_DIR):
        for name in files:
            try:
                stat = os.stat(os.path.join(dirpath, name))
            except OSError:
                continue
            identities.add((stat.st_dev, stat.st_ino))
    return identities


def _hard_linked_personal(
    root: Path, manifest: list[str], identities: set[tuple[int, int]]
) -> int:
    """How many manifest entries are a second name for one of the owner's files.

    The gap ADR-0068's Consequences booked as accepted. `mklink /H` (and
    `os.link`) give one file another name with no target to resolve, so
    `_leaks_personal` answers False and `_is_personal` sees only the name the
    operator chose — measured: the content of a hard link into
    `specs/personal/` landed in **both** worktrees at exit 0.

    Checked here, on the **source** side, and deliberately not on the
    materialized worktree: `copy2` and `git checkout` both write a fresh file,
    so the copy shares the content and not the inode (measured — the artifact
    -side identity check this replaces would have answered False on the very
    case it was written for). Identity survives only in the live tree, which
    is the side `_copy_untracked` actually reads.

    Counted, never named: a hard link into `specs/personal/` is named by
    whoever created it, and CLAUDE.md classifies which lab and which panel as
    personal with no values attached.
    """
    if not identities:
        return 0
    linked = 0
    for rel in manifest:
        try:
            stat = (root / rel).stat()
        except OSError:
            continue
        if (stat.st_dev, stat.st_ino) in identities:
            linked += 1
    return linked


def _artifact_breaches(worktree: Path) -> list[str]:
    """Check what actually landed, instead of predicting what could.

    Every other guard in this module is a precondition on an *input*: a
    pathspec, a manifest entry, a mode bit, a resolved component. That
    enumeration has been declared complete and then extended five times — a
    case-sensitive tracked pathspec, a Windows junction, a bare
    `specs/personal` file, an in-repo link on two platforms, an untracked leaf
    symlink whose own name was the leak. Each fix was one more predicate, and
    `specs/open-questions.md` says of this exact shape that *"containment
    should hold by construction, never by a path blocklist"*. This is the one
    check whose subject is the artifact, so a route nobody imagined still
    lands a file and the walk sees it.

    One question, asked of every entry at every depth: does it lead *out* of
    the worktree? That covers links of every kind, including the ones git
    reports as ordinary paths and the ones only an ancestor component reveals.
    It deliberately does **not** ask whether an entry is one of the owner's
    files under another name — the copy destroys that identity, so the
    question only has an answer on the source side, where
    `_hard_linked_personal` asks it.

    Counted, never named: these are the names on the far side of a breach, and
    the count is what tells an operator something is wrong without handing
    them the thing that must not be relayed.
    """
    boundary = worktree.resolve()
    escaped = sum(
        1
        for dirpath, dirs, files in os.walk(worktree)
        for name in dirs + files
        if _escapes(Path(dirpath) / name, boundary)
    )
    return [f"{escaped} path(s) resolve outside the worktree"] if escaped else []


def _copy_untracked(source: Path, worktree: Path, manifest: list[str]) -> list[str]:
    """Copy the manifest's untracked files into one worktree; return the skipped.

    The first call reads the live tree; later calls read the first worktree —
    so each live file is read exactly once and every worktree still receives
    identical bytes, with no intermediate staging copy to leak or clean up.

    `copy2`, not `copyfile`: it carries the mode bits, so an untracked
    executable script stays executable in the worktrees — a 0644 copy reads
    as the author's defect on the POSIX legs. The symlink re-check *narrows*
    a race: the manifest was filtered earlier, but `copy2` follows links at
    read time, and a path swapped to a symlink in between would replicate
    its TARGET's bytes — including content outside the repo. It covers the
    realistic accidental case (the gap since the manifest was taken) and not
    the residual in-process window, which is accepted: closing that needs
    open-then-fstat with `O_NOFOLLOW`, which Windows — the primary platform
    here — does not have, and a process deliberately racing the launcher on
    the author's own machine could attack the live tree directly anyway.
    """
    skipped: list[str] = []
    # Resolved once: the copy-time containment re-check below compares against
    # it, and `source` is the first worktree rather than the repo root for
    # every agent after the first.
    boundary = source.resolve()
    ensured: set[Path] = set()
    for rel in manifest:
        src = source / rel
        # Every EXISTENCE and LINK probe below goes through `_os_path`, not
        # only the copy. They used to read the bare path while `copy2` alone
        # was hardened, which made the hardening unreachable for exactly the
        # paths it was added for: `Path.exists()` delegates to
        # `os.path.exists()`, which returns False rather than raising for a
        # path past MAX_PATH, so such a file took the vanished branch below.
        # Worktrees 2..N read from worktree 1 — measured ~126 characters
        # deeper than the repo root — so worktree 1 got the file, worktree 2
        # did not, and the manifest described neither: the identical-bytes
        # guarantee broken silently, host by host.
        #
        # `probe` is the extended-length spelling, for the copy and for the
        # link check. The containment predicate below deliberately gets the
        # BARE `src`, and the comment above this loop used to claim otherwise
        # — "every probe below goes through `_os_path`" was true of the
        # existence and link probes and false of `_escapes`, which is the one
        # that decides containment.
        #
        # It has to be the bare path: `_escapes` resolves and then compares
        # against `boundary`, and a `\\?\` path resolves to a `\\?\` path
        # while the boundary is bare, so `is_relative_to` answers False for a
        # file sitting safely inside. Measured — a file two directories under
        # the boundary reported as escaping — so wrapping it does not harden
        # the check, it aborts every round. `_leaks_personal` is the same
        # shape and gets the same treatment.
        #
        # The residual gap that leaves is real and narrow: past MAX_PATH
        # `ntpath.realpath` walks components off the end until a short-enough
        # prefix resolves and joins the tail lexically, so a link in the
        # unresolved tail is never followed and `_escapes` can answer False
        # for a path that genuinely leaves the tree. It needs the link
        # component itself to sit past MAX_PATH, and it is now backstopped by
        # `_artifact_breaches`, which walks what actually landed rather than
        # predicting it.
        probe = _os_path(src)
        if "�" in rel:
            # Checked BEFORE the vanished-file tolerance below, which it would
            # otherwise fall into and silently swallow: git emitted this name
            # in bytes that are not valid UTF-8, `untracked_manifest` decoded
            # it with errors="replace", and the resulting string therefore
            # names no file on disk and never will. That is deterministic, not
            # transient — so it has to abort loudly rather than be reported as
            # "vanished", which would be a quiet lie about a file still sitting
            # in the tree, and would send the operator into a rerun that
            # produces the identical result. (U+FFFD in a genuine filename
            # would abort here too; it cannot be replicated either, because
            # this process has no way to tell the two apart.)
            raise AbortError(
                f"{rel} has a name that is not valid UTF-8, so it cannot be "
                "replicated into a worktree — the replacement characters are "
                "where the undecodable bytes were. Rename it, ignore it, or "
                "move it outside the repo; rerunning will not change this"
            )
        # Computed once: `_is_link` costs two syscalls (`is_symlink` plus
        # `is_junction`), and it was called again a few lines below on the
        # happy path, so every copied file paid four where two suffice.
        src_is_link = _is_link(probe)
        if not src_is_link and not _exists(src):
            # Vanished between `untracked_manifest()` and now — an editor's
            # swap or backup file, a build artifact, a test temp file.
            # Untracked files are the most volatile things in a tree, so a
            # background build during the materialization window would
            # otherwise raise FileNotFoundError, roll the whole round back,
            # and send the operator into a rerun that races identically.
            # Dropped with a warning instead, exactly as an untracked symlink
            # is — the caller removes it from the manifest it prints, so the
            # reviewers' `??` reconciliation still matches their worktree.
            skipped.append(rel)
            continue
        if src_is_link:
            raise AbortError(
                f"{rel} became a symlink or junction after the untracked "
                "manifest was taken — aborting rather than replicating its "
                "target's content"
            )
        if _escapes(src, boundary):
            # The manifest filter already dropped everything that resolved
            # outside the tree; this is the same race the link check above
            # narrows, on the form that check cannot see. A junction swapped
            # in for an ordinary directory *anywhere* in the path leaves both
            # the leaf and its parents answering `_is_link` False while
            # `copy2` happily reads through to a target outside the
            # repository — which is how content from outside a repo reached
            # two agent-readable worktrees with no warning printed at all.
            # Redacted like the setup-time filter this backstops, and for the
            # identical reason: `rel`'s leaf components come from the far side
            # of the link — outside the repository, and so outside review
            # scope, on a machine that holds a real encrypted health database —
            # and this message reaches stderr, the transcript, and every
            # reviewer report that relays the abort. The setup-time twin
            # already prints a bare count; this one named the path, so the
            # guard that only runs when the first one is bypassed was the one
            # that leaked. It is a race window rather than a routine path,
            # which is exactly why it went unnoticed and why the redaction has
            # its own test.
            raise AbortError(
                "an untracked path resolves outside the repository — a "
                "directory on its path became a link after the untracked "
                "manifest was taken. Aborting rather than replicating content "
                "from outside the tree; check the tree for linked directories"
            )
        if _is_dir(src):
            # git stops at a nested repository boundary and reports the
            # *directory* — `vendor/dep/`, trailing slash and all — for any
            # untracked embedded repo (a clone left in the tree, a vendored
            # dep). copy2 on it raises a bare IsADirectoryError (errno 13
            # "Permission denied" on Windows) that names neither the cause nor
            # a remedy, and the triage in reviewer-isolation.md files a copy
            # OSError as retryable, so the operator retries it forever.
            raise AbortError(
                f"{rel} is a directory, not a file — git reports an untracked "
                "nested repository as a single directory entry. Commit it, add "
                "it to .gitignore, or move it outside the repo; it cannot be "
                "replicated into a worktree"
            )
        # Extended-length for the copy too, not only for git and rmtree.
        # `core.longpaths=true` makes git use `\\?\` internally, so `worktree
        # add` SUCCEEDS at creating a worktree whose path plus a deep
        # untracked path exceeds MAX_PATH — and this copy would then fail
        # with a bare FileNotFoundError on any host where the OS
        # LongPathsEnabled flag is not set (no workflow in .github/workflows
        # sets it, so CI is the exposure). Hardening git alone left an
        # abort-and-rollback on exactly the deep-path case the rest of this
        # module was hardened for, dropping the round to the unisolated
        # fallback.
        dest = _os_path(worktree / rel)
        # Once per distinct parent, not once per file. `mkdir(exist_ok=True)`
        # against an existing directory still costs two syscalls — the
        # attempt that fails EEXIST, then an `is_dir()` verification — so a
        # flat manifest of M files under D parents paid 2*(M-D) avoidable
        # calls.
        if dest.parent not in ensured:
            dest.parent.mkdir(parents=True, exist_ok=True)
            ensured.add(dest.parent)
        shutil.copy2(probe, dest)
    return skipped


def _registered_worktrees(root: Path) -> dict[Path, str]:
    """Registered worktrees: resolved path -> the spelling git itself printed.

    Both halves are load-bearing, for opposite reasons.

    The resolved key is the only separator-safe way to compare against the
    OS-native paths a state file and `argv` hold — see `_registered_paths`.

    The value has to be handed straight back whenever git is asked to drop a
    registration whose directory is **gone**. With nothing on disk to
    canonicalize against, `git worktree remove` compares its argument as a
    string against exactly this list, and the list is forward-slashed: the
    OS-native `C:\\...\\wt-x` spelling then fails `is not a working tree`
    (exit 128) while the printed spelling exits 0 (both measured). It works
    at all today only because git canonicalizes through a surviving *parent*
    directory — so the moment the scratch dir itself is what went missing,
    passing our own spelling stops working, which is precisely the renamed-
    scratch case teardown has to survive.
    """
    return {
        # `.resolve()` runs per registered entry, and a developer's unrelated
        # worktrees are in this listing too — 8 strays turned teardown's five
        # reads into 40 resolves instead of ~10. The keys still have to be
        # resolved for the separator-safe comparison callers make, so this
        # cannot be dropped; it is simply not paid for entries that cannot
        # match. Callers only ever ask about paths under the scratch dir.
        Path(raw).resolve(): raw
        for raw in (
            field.removeprefix("worktree ")
            for field in _git_z(
                root, "worktree", "list", "--porcelain", "-z", errors="surrogateescape"
            )
            if field.startswith("worktree ")
        )
    }


def _registered_paths(root: Path) -> set[Path]:
    """Worktree paths git currently has registered, resolved for comparison.

    Kept as a named helper rather than inlined as
    `resolved in _registered_worktrees(root)` — which would be identical,
    since `in` on a dict tests keys — because the NAME is what carries the
    "resolved for comparison" contract to the two call sites, and this
    module's `_contained` docstring already records a drift between two
    same-purpose helpers that a shared name would have prevented.

    `git worktree list --porcelain` prints forward-slash paths; state files
    and callers hold OS-native ones. Comparing resolved `Path`s is the only
    separator-safe form — a substring check silently never matches on
    Windows, which would make every stray invisible.

    `errors="surrogateescape"`, overriding `_git_z`'s default, for the same
    fail-loud reason `-z` was chosen here: this is the one caller whose whole
    job is *exact* equality against strings that came from `argv` and the
    state file. On POSIX those carry undecodable bytes as lone surrogates
    (this is how `os.fsdecode` and `sys.argv` themselves represent them),
    while "replace" collapses every such byte to U+FFFD — so the two spellings
    of one real path could never compare equal, `strays` would come back
    empty, and teardown would report "no strays registered" and delete the
    state file over a live registration. Surrogateescape round-trips; replace
    cannot.
    """
    return set(_registered_worktrees(root))


def _extended_length(target: Path) -> Path:
    r"""The Windows extended-length (`\\?\`) form of a resolved path.

    Lifts MAX_PATH for rmtree. A UNC path needs the distinct
    `\\?\UNC\server\share` form — naively prefixing `\\server\share` yields
    a string Windows parses with `?` as the server name, breaking every
    delete on exactly the deep trees the fallback exists to remove (and
    `Path.resolve()` expands mapped drive letters to UNC form).
    """
    s = str(target)
    if s.startswith("\\\\?\\"):
        return target
    if s.startswith("\\\\"):
        return Path("\\\\?\\UNC" + s[1:])
    return Path("\\\\?\\" + s)


def _os_path(target: Path) -> Path:
    """The form to hand the OS for a path that may exceed MAX_PATH.

    One place, so a caller cannot get the platform test right and the UNC
    case wrong. Inert everywhere but Windows.

    A relative path is returned untouched: Windows does not normalize a
    `\\\\?\\` path, so prefixing one yields a string every call fails on — and
    `Path.exists()`/`Path.is_symlink()` answer False for a failed call rather
    than raising, which would turn the hardening into a silent "nothing here".
    Nothing in this module passes a relative path; the guard is here so that
    staying true is not a precondition every future caller has to know.
    """
    if os.name != "nt" or not target.is_absolute():
        return target
    return _extended_length(target)


# Every filesystem probe in this module goes through one of these, so that
# "which sites are covered by `_os_path`?" stops being a question anyone has
# to answer by reading. It was answered wrongly twice in opposite directions
# at once: `_force_delete_tree`'s docstring stated the rule while three
# sibling probes in the two removers read the bare `Path`, and one line
# double-applied the prefix (`_is_link(_os_path(path))`, where `_is_link`
# already applies it) — which is the tell that the coverage was not legible.
#
# Past MAX_PATH the bare calls do not raise; `Path.exists()` and
# `Path.is_file()` delegate to `os.path.*`, which answer **False**. So a deep
# recorded venv path that is a *file* skipped the unlink branch, reached
# `shutil.rmtree`, handed `onexc` a NotADirectoryError for both `os.scandir`
# and `os.rmdir` (neither retryable), deleted nothing, and left teardown
# returning 1 on every retry with the state file never unlinked — so every
# later setup was refused. `tests/test_review_worktree.py` gates this
# by AST: no gated method may be called on anything but `_os_path(...)`.


def _exists(target: Path) -> bool:
    return _os_path(target).exists()


def _is_file(target: Path) -> bool:
    return _os_path(target).is_file()


def _is_dir(target: Path) -> bool:
    return _os_path(target).is_dir()


def _unlink(target: Path, *, missing_ok: bool = False) -> None:
    _os_path(target).unlink(missing_ok=missing_ok)


def _rmdir(target: Path) -> None:
    _os_path(target).rmdir()


# rmtree hands `onexc` whichever function raised — not only the two that
# delete. The Windows walker also passes os.lstat, os.path.islink and
# os.scandir; the POSIX fd walker adds os.open and os.close. Retrying those is
# strictly worse than not retrying: the first three "succeed" without deleting
# anything (os.scandir even hands back an unclosed handle that can itself block
# the later rmdir) and then return early, swallowing the very failure the
# caller's list exists to name — while os.open and os.close raise TypeError on
# a lone str argument, which neither the clause below nor main's
# `except (AbortError, OSError)` catches, so teardown would die by traceback.
_RETRYABLE_DELETERS = (os.unlink, os.remove, os.rmdir)


def _force_delete_tree(path: Path, scratch: Path) -> list[str]:
    """Delete a directory tree; return failure descriptions instead of hiding them.

    The backstop for whatever `git worktree remove` cannot delete, and the
    only deleter for the reviewer venv dirs, which are not worktrees at all.
    The extended-length prefix lifts MAX_PATH for rmtree here; non-Windows
    platforms need no prefix. Read-only files (common inside venvs on
    Windows) get one chmod-and-retry; whatever still fails is returned so
    the caller's error names the *actual* obstacle rather than git's
    unrelated original complaint.

    **Containment is re-asserted here, on the resolved path, because this is
    the function that actually deletes.** Both callers check before calling,
    and one of them had the check and the other did not — a `..` in a recorded
    path passed the caller's lexical test, arrived here, resolved to the
    directory holding the repository, and was recursively deleted with
    teardown returning success (reproduced). Deciding containment anywhere but
    at the delete gives two places for it to be true and one for it to matter.
    `scratch` is therefore required, not optional: a default would let the next
    caller be written without it.

    Every probe goes through `_os_path`, not only the `rmtree`. `Path.exists()`
    and `Path.is_symlink()` delegate to calls that return False rather than
    raising past MAX_PATH, so a deep tree took the "nothing here" branch below
    and this function reported success over a directory still on disk — the
    identical partial hardening `_copy_untracked` already had fixed. Callers'
    post-delete verifications need the same treatment, and get it.

    **A non-empty return means nothing was deleted, and no caller may let an
    existence probe overrule it.** Both callers gated their error on
    `_os_path(located).exists()` alone — which is False for precisely the
    paths that earn a containment refusal, since `\\\\?\\` suppresses the
    normalization that made the bare spelling exist — so the refusal above was
    computed, discarded, and reported as success while the directory stayed on
    disk and teardown unlinked its only record (reproduced).

    Preconditions: `path` has a resolved parent and a real entry name (see
    `_locate` and `_names_syntax`) — `\\\\?\\` paths are not normalized by
    Windows, so a surviving `..` component would fail every call rather than
    walk up.
    """
    if _is_link(path):
        # Checked before exists(), which follows the link and is False for a
        # dangling one — returning [] there would report success for a path
        # still sitting on disk. `path.resolve()` below follows the link too,
        # so without this the delete lands on the link's TARGET while the link
        # itself survives, defeating the same refusal shutil.rmtree makes for
        # itself ("Cannot call rmtree on a symbolic link"). Callers unlink a
        # recorded link as itself; nothing here may delete through one.
        # Junctions included — see `_is_link`; this last-line guard had the
        # same blind spot as the caller it backstops, so both were open at
        # once and the backstop proved nothing.
        return [f"{path}: is a link, not a directory tree"]
    resolved = path.resolve()
    if not _inside_scratch(resolved, scratch):
        return [
            f"{path} resolves to {resolved}, which is not strictly inside the "
            f"scratch dir {scratch}" + _STATE_REMEDY
        ]
    target = _os_path(resolved)
    if not _exists(resolved):
        return []
    failures: list[str] = []

    def _onexc(func: object, failed: str, exc: BaseException) -> None:
        if callable(func) and func in _RETRYABLE_DELETERS:
            try:
                # Never chmod a link. rmtree hands back whichever entry
                # raised, including a link entry when the failure came from
                # its parent directory's permissions rather than the entry
                # itself — and chmod follows a link, so the retry would set
                # the mode of the TARGET: a shared uv cache, a file in the
                # live repo, something under $HOME. That is an unguarded write
                # outside the containment boundary (`_inside_scratch` was
                # checked only on the tree root) and a silent one, since the
                # retry returns without appending a failure. Skipping it costs
                # the retry nothing: unlinking a link never depended on the
                # link's own mode, only on its parent directory's.
                if not _is_link(Path(failed)):
                    os.chmod(failed, 0o700)
                func(failed)
                return
            except (OSError, TypeError):  # fmt: skip
                pass
        failures.append(f"{failed}: {exc}")

    shutil.rmtree(target, onexc=_onexc)
    return failures


def _resolved_scratch(scratch: Path) -> Path:
    """Resolve --scratch, refusing a relative path.

    A relative value is resolved against the invocation cwd, and cwd persists
    across Bash calls — so `setup --scratch tmp/x` from the repo root followed
    by any `cd` makes `teardown --scratch tmp/x` look somewhere else entirely,
    find no state file, and exit **0** ("nothing recorded to tear down") while
    both worktrees stay registered and the only record of them sits where
    nothing will look again. The state file records `root` precisely so
    teardown is cwd-independent; the path used to *find* that file has to be
    too, and being absolute is the only way it can be.
    """
    if not scratch.is_absolute():
        raise AbortError(
            f"--scratch must be an absolute path, got {scratch} — a relative "
            "one is resolved against the current directory, so a later cd "
            "would make teardown silently look in the wrong place"
        )
    return scratch.resolve()


def _inside_scratch(resolved: Path, scratch: Path) -> bool:
    """Whether a recorded path may be force-deleted at all.

    The one containment rule behind both removal helpers, kept in one place
    deliberately: this predicate is all that stands between a tampered state
    file and a recursive force-delete of real work, and a second copy would
    let a later fix — a case-insensitive comparison on Windows, a `resolve()`
    quirk — reach worktree deletion and venv deletion separately while both
    copies still read as complete. *Strictly* inside: the scratch dir itself
    is never a valid target, because deleting it takes the state file — the
    only record of everything else — with it.
    """
    scratch_resolved = scratch.resolve()
    return resolved != scratch_resolved and resolved.is_relative_to(scratch_resolved)


def _locate(path_str: str) -> Path:
    """Where a recorded path *is*, without following it if it is a link.

    `Path.resolve()` follows a final link, so a recorded path that is a link
    resolves to its TARGET — and every question this module asks of a recorded
    path (may it be deleted, which registration does it own) must be answered
    about the entry itself, never about wherever it happens to aim. Resolving
    only the parent locates the entry without following it.

    Preconditions on the final component are `_contained`'s to enforce: this
    returns a path with a resolved parent, which is only a real location if
    the name is a real entry name.
    """
    original = Path(path_str)
    return original.parent.resolve() / original.name


# `..` and the empty name are path *syntax*, not entry names, and a recorded
# worktree or venv path always ends in an entry name. Left to reach `_locate`
# they make the located path denote a directory that was never recorded while
# still passing the lexical containment test: `<scratch>/..` is the directory
# holding the repository, and `Path('C:/').name` is `''` with
# `scratch / '' == scratch`. Both were measured force-deleting real work with
# teardown returning success.
#
# `"."` is deliberately absent rather than defensively included: pathlib
# normalizes a `.` component away at construction, so `Path('a/.').name` is
# `'a'` and `Path('.').name` is `''` (measured). There is no recorded string
# whose name is `"."`, and the empty entry already covers the `Path('.')` case
# — a third member would be a guard that can never fire, reading as coverage.
_SYNTAX_NAMES = frozenset({"", ".."})


def _names_syntax(name: str) -> bool:
    """Whether a final component is path syntax rather than an entry name.

    Matched after `rstrip(". ")`, because Win32 strips trailing dots and
    spaces at the filesystem call rather than at parse time. pathlib keeps
    them, so `Path('<scratch>/.. ').name` is `'.. '` — outside the set above —
    and `is_relative_to` answers True, while every API that opens that path
    sees `..` and walks up. Reproduced: the bare set passed `<scratch>/.. `
    through `_contained` with `inside=True` and on to
    `git worktree remove --force`, which the docstring below promises runs
    only after containment. `..`, `.. `, `...` and `.. . ` are one spelling to
    Win32 and all collapse to the empty string here.

    `parse_agents` already excludes `.` from agent names for this reason. The
    state file is where these strings arrive as parsed *data* — the side with
    no adversary, since `_STATE_REMEDY` invites the operator to hand-edit it —
    and it was never given the same rule.
    """
    return name.rstrip(". ") in _SYNTAX_NAMES


def _contained(
    path_str: str, scratch: Path, *, relocatable: bool
) -> tuple[Path | None, bool]:
    """Locate a recorded path and say whether it may be acted on at all.

    Returns `(None, False)` for a path this module refuses to locate at all —
    callers must treat that as a refusal rather than as "outside scratch",
    because the outside-scratch branches carry tolerances (de-register a
    worktree whose directory is gone; treat a missing venv as done) that a
    malformed record must not be able to reach.

    The single containment preamble for both removers. It was two copies, and
    they had already drifted on the resolve step — `_remove_worktree`
    resolving the path itself while `_remove_plain_dir` resolved only the
    parent — which is exactly the failure `_inside_scratch`'s docstring
    predicts for a second copy: a fix reaching one deleter and not the other
    while both still read as complete. Measured, that drift was live in both
    directions at once.

    Locating goes through `_locate`, which resolves the *parent* and never the
    recorded path itself — see its docstring for why following a final link
    here would containment-check a link against its target.

    `relocatable` says whether the recorded basename is unique to one round,
    which is what makes the renamed-scratch fallback below safe. It is true of
    `wt-<agent>-<sha9>-<hex6>` and false of the deliberately fixed
    `venv-<agent>`, so only worktrees are relocated: retargeting a stale
    record's `venv-spec-reviewer` onto the current scratch dir would pass
    containment and force-delete a *live* round's venv mid-review. That
    asymmetry was asserted here as though it held for both, 470 lines from the
    fixed names that contradict it.

    Returns the located path and whether it is strictly inside `scratch`.
    Callers own what to do when it is not, because that genuinely differs —
    one de-registers a worktree, the other is simply finished.
    """
    if _names_syntax(Path(path_str).name):
        return None, False
    located = _locate(path_str)
    if _inside_scratch(located, scratch):
        return located, True
    # The scratch dir itself was renamed or moved after setup. git records
    # absolute paths at `add` time, so every recorded path now points at a
    # directory that is gone — but the trees are not gone, they moved with
    # their parent, and treating them as gone left two full copies of the repo
    # (carrying the uncommitted work under review, plus test-reviewer's
    # mutation edits) on disk permanently while teardown printed success and
    # deleted the only record of them. Looking for the same entry under the
    # CURRENT scratch dir finds them, and doing it this way keeps containment
    # structural: the relocated path is built from `scratch`, so it cannot
    # point anywhere else — but only a round-unique basename makes "the same
    # entry" mean the same round's entry, which is what `relocatable` gates.
    if relocatable:
        moved = scratch / located.name
        if _is_link(moved) or _exists(moved):
            return moved, True
    return located, False


def _outside_scratch_refusal(path_str: str, scratch: Path) -> str:
    """The one refusal sentence both removers return, remedy included.

    The remedy is folded in rather than spelled at each call site. It was
    appended identically at all four — so the split bought no flexibility,
    only a fifth place to forget it — and forgetting it is not cosmetic:
    teardown exits 1 on a refusal, a nonzero exit keeps the state file, and
    the state file makes every later setup refuse. A refusal with no way out
    is a permanent wedge, which is the failure this module's own comment
    calls "the one message in the module that left the operator with no way
    out".
    """
    return (
        f"refusing to remove {path_str}: not strictly inside the scratch dir "
        f"{scratch}" + _STATE_REMEDY
    )


def _is_generated_name(name: str) -> bool:
    """Whether `name` has the `wt-<agent>-<short>-<hex6>` shape setup writes.

    The one property of a recorded worktree that survives the scratch dir
    being renamed and that a hand-edited state file does not get for free.
    `_contained(relocatable=True)` already leans on this basename being
    unique to one round; this reads the same signature for the other
    destructive act, de-registration.

    `<short>` is `sha[:9]` or the literal `HEAD` when the tree had no
    uncommitted tracked change, so both are accepted; `<hex6>` is
    `secrets.token_hex(3)`. Agent names may themselves contain `-`
    (`spec-reviewer`), so only the outer shape is checked.
    """
    parts = name.split("-")
    if len(parts) < 4 or parts[0] != "wt":
        return False
    short, suffix = parts[-2], parts[-1]
    hexdigits = "0123456789abcdef"
    short_ok = short == "HEAD" or (
        len(short) == 9 and all(char in hexdigits for char in short)
    )
    return short_ok and len(suffix) == 6 and all(char in hexdigits for char in suffix)


def _deregister_missing(
    root: Path, path: str, resolved: Path, scratch: Path
) -> str | None:
    """Drop the registration of a worktree whose directory is already gone.

    Safer than a force-delete: there is nothing on disk left to destroy.
    Measured: `git worktree remove --force` on a registered-but-missing
    worktree exits 0 and de-registers just that one — no repo-global
    `git worktree prune`, which would de-register every worktree whose
    directory is momentarily missing (see `_remove_worktree`).

    **But "nothing to destroy" accounts for the tree and not for the
    REGISTRATION, which is what this call actually destroys**, and that was
    the whole safety argument for running it on a path outside the scratch
    dir. `git worktree prune` was removed from `_cleanup_worktrees` for
    exactly this: de-registering a worktree whose directory is momentarily
    missing leaves its owner needing `git worktree repair` — "a repo-global
    write on someone else's work, which invariant 2 does not sanction". Two
    ordinary ways to arrive there: a hand-repaired state file naming a
    worktree outside this scratch dir, which `_STATE_REMEDY` actively sends
    operators to write; and `exists()` answering False for a path that is
    **unreachable rather than deleted** — an unmounted volume, a
    disconnected share, another tool mid-rename — after which teardown
    de-registers, reports success, and unlinks the state file.

    So containment is re-asserted here, at the write, for the same reason
    `_force_delete_tree` re-asserts it at the delete: the caller checking is
    not the same as the acting function checking, and only one of those
    reaches every caller.

    git is handed back its own spelling of the path, never the caller's: with
    the directory gone there is nothing to canonicalize against, so git falls
    back to a string comparison against its forward-slashed registry. See
    `_registered_worktrees`.
    """
    as_git_spells_it = _registered_worktrees(root).get(resolved)
    if as_git_spells_it is None:
        # Nothing registered under this path, so there is no registration to
        # destroy and nothing for the guard below to protect. Checked first
        # for that reason: a recorded path that is gone AND unregistered is
        # the renamed-scratch tolerance teardown must keep, and refusing it
        # here would wedge teardown and block every later setup.
        return None
    if not _inside_scratch(resolved, scratch) and not _is_generated_name(resolved.name):
        return (
            f"refusing to de-register {path}: it resolves to {resolved}, "
            f"outside the scratch dir {scratch}, and does not carry a name "
            "this launcher generates — de-registering a worktree this round "
            "did not create is a repo-global write on someone else's work"
            + _STATE_REMEDY
        )
    proc = _git(root, "worktree", "remove", "--force", as_git_spells_it)
    if proc.returncode != 0 and resolved in _registered_paths(root):
        return (
            f"could not de-register {path}: {proc.stdout}{proc.stderr}".rstrip()
            + _STATE_REMEDY
        )
    return None


def _remove_worktree(root: Path, path: str, scratch: Path) -> str | None:
    """Remove one worktree; return an error string or None on success.

    Containment is validated BEFORE git runs: the state file is parsed
    data, and `git worktree remove --force` succeeds on a registered path
    wherever it lives — running it first would let a tampered state file
    destroy a real worktree (uncommitted work included) outside scratch.
    An outside-scratch path is refused up front, with one tolerance: already
    gone AND unregistered means there is nothing left to free (a scratch
    dir renamed after setup must not wedge teardown forever).

    Inside scratch, tolerates already-gone and falls back to deleting the
    tree directly when git still cannot — then asks git once more to drop the
    registration, which is scoped to this path (see below); no repo-global
    prune is ever run.
    """
    located, inside = _contained(path, scratch, relocatable=True)
    if located is None:
        return _outside_scratch_refusal(path, scratch)
    # Two different questions, two different paths, and conflating them breaks
    # one case each way. `located` is where the tree IS — possibly relocated
    # under a renamed scratch dir — and is what may be deleted. `recorded` is
    # the spelling git was handed at `worktree add` time, which is the only key
    # its registry answers to; after a rename the tree moved and the
    # registration did not, so de-registering by `located` silently leaves it.
    # Through `_locate` rather than `Path(path).resolve()` for the reason the
    # link branch below states: resolve() follows a final link, and this value
    # once carried a live worktree's directory into a force-remove.
    recorded = _locate(path)
    if not inside:
        if not _is_link(located) and not _exists(located):
            # Nothing is left on disk, so containment is moot: the only
            # remaining action is de-registration, which destroys nothing.
            # `_contained` has already looked for the tree under a renamed
            # scratch dir, so reaching here means it is genuinely gone rather
            # than merely moved — the distinction that made this tolerance
            # silently leak two worktrees before.
            #
            # `located`, never `Path(path).resolve()`: the registry is keyed by
            # a resolved path, and `located` already carries a resolved parent
            # — while `.resolve()` on the recorded string follows a final link,
            # which is how this call once received a LINK'S TARGET and
            # de-registered somebody else's live worktree.
            return _deregister_missing(root, path, recorded, scratch)
        return _outside_scratch_refusal(path, scratch)
    if _is_link(located):
        # A registered worktree path that is now a link is a tampered or
        # corrupted state file. Unlink the link itself and let the
        # de-registration below clear the registry entry: refusing outright
        # would wedge teardown forever (nonzero exit keeps the state file,
        # which the next setup refuses), and following it would aim
        # `worktree remove --force` at whatever it points to.
        try:
            _unlink(located)
        except OSError:
            try:
                _rmdir(located)
            except OSError as exc:
                return (
                    f"could not remove the link at {path}: {exc} — remove it by "
                    "hand, then delete the state file to unwedge teardown"
                )
        # `recorded` and emphatically not the link's resolved target. This line
        # read `Path(path).resolve()`, computed BEFORE the unlink above and so
        # following the link — handing `_deregister_missing` whatever it
        # pointed at. That helper's safety argument is "there is nothing on
        # disk left to destroy", which is true of the link we just removed and
        # false of its target: a developer's unrelated live worktree, matched
        # in the registry and force-removed with its uncommitted work, while
        # the stray check saw nothing (the path was never in `paths`) and
        # teardown exited 0.
        return _deregister_missing(root, path, recorded, scratch)
    proc = _git(root, "worktree", "remove", "--force", str(located))
    if proc.returncode == 0:
        return None
    delete_failures = _force_delete_tree(located, scratch)
    # The failure list first, and never the existence probe alone: a refusal
    # means nothing was deleted, while `_os_path(located).exists()` is False
    # for exactly the paths that earn one — a `\\?\` path gets no
    # normalization, so a component Win32 would have folded away names nothing
    # (reproduced). Gating on the probe alone read the containment refusal as
    # "already gone", returned success, and let teardown unlink the state file
    # over a directory still on disk.
    if delete_failures or _exists(located):
        detail = "; ".join(delete_failures) or f"{proc.stdout}{proc.stderr}".rstrip()
        # The remedy every sibling refusal carries and this one did not.
        # Teardown exits 1 here, which keeps the state file, which makes the
        # next setup refuse — so the one message in the module that left the
        # operator with no way out was the one guarding a permanent wedge.
        return (
            f"could not remove {path}: {detail} — remove it by hand, then "
            "delete the state file to unwedge teardown"
        )
    # The tree is gone; the registration is not. Ask git again rather than
    # reaching for `git worktree prune`: prune takes no path filter and
    # de-registers EVERY worktree whose directory is momentarily missing — an
    # unmounted drive, a renamed directory, another harness mid-cleanup —
    # leaving their owner a worktree that answers `fatal: not a git
    # repository` until `git worktree repair` runs. That is a repo-global
    # write on someone else's work, which invariant 2 does not sanction. The
    # retry succeeds where the first attempt failed because whatever git
    # could not traverse (a MAX_PATH-deep tree) is no longer on disk.
    #
    # Through the same helper as the gone-and-outside-scratch case, because
    # by here it IS that case: the directory has just been deleted, so the
    # retry has to carry git's own spelling of the path or it fails on the
    # separator alone. A refusal git makes for a non-traversal reason — a
    # locked worktree (which needs --force twice) or an index holding a
    # gitlink — is repeated verbatim there, with nothing left to inspect;
    # the remedy is what keeps that from being a permanent wedge.
    return _deregister_missing(root, path, recorded, scratch)


def _remove_plain_dir(path_str: str, scratch: Path) -> str | None:
    """Delete a recorded plain directory (a reviewer venv), containment-checked.

    Same trust model as `_remove_worktree`: the path is parsed data, so
    nothing outside scratch (or scratch itself) is ever deleted. Already
    gone is success either way — `_force_delete_tree` returns no failures
    for a path that does not exist. Locating and containment-checking the
    recorded path is `_contained`'s job, shared with `_remove_worktree`;
    re-checking containment on the *resolved* path is `_force_delete_tree`'s,
    which is where the delete happens and therefore the only place that check
    reaches every caller.

    `relocatable=False`: a venv's basename is the fixed `venv-<agent>`, so a
    stale record's venv would relocate onto the current round's live one.
    """
    located, inside = _contained(path_str, scratch, relocatable=False)
    if located is None:
        return _outside_scratch_refusal(path_str, scratch)
    if not inside:
        if not _is_link(located) and not _exists(located):
            return None
        return _outside_scratch_refusal(path_str, scratch)
    if _is_link(located) or _is_file(located):
        # Not a directory tree: rmtree would hand `onexc` a NotADirectoryError
        # for both os.scandir and os.rmdir, delete nothing, and leave teardown
        # returning 1 on every retry forever — with every later setup refused
        # by the state-file guard. A symlink is unlinked as itself; deleting
        # what it points at would destroy a tree nothing here recorded.
        try:
            _unlink(located)
        except OSError:
            # Fallback for a *directory* link the platform will not unlink,
            # which must be removed with rmdir instead. Either call removes
            # the link itself and never what it points at.
            #
            # Deliberately kept, but do not read it as the junction path: the
            # comment here used to assert that `os.unlink` raises
            # PermissionError on a Windows directory link, and **that is false
            # on this project's interpreter** — measured on Python 3.14.3 /
            # Windows 11, `Path.unlink()` removes a junction cleanly and
            # leaves its target intact, so every junction in the suite takes
            # the branch above and this one was never reached at all
            # (instrumented: zero hits across the whole suite). It survives as
            # a defensive path for interpreter and link-type combinations this
            # machine cannot produce — an unprivileged host cannot even create
            # a `mklink /D` symlink to test — and
            # `test_a_recorded_link_that_will_not_unlink_never_deletes_its_target`
            # pins it directly by making `unlink` raise, rather than through a
            # fixture that silently takes the primary path. `_remove_worktree`
            # carries the same fallback and it has its own pinning test, on the
            # `worktrees` key, for the same reason.
            try:
                _rmdir(located)
            except OSError as exc:
                return f"could not remove {path_str}: {exc}" + _STATE_REMEDY
        return None
    failures = _force_delete_tree(located, scratch)
    # The failure list first — see the twin in `_remove_worktree`. A refusal
    # that an existence probe can overrule is not a refusal.
    if failures or _exists(located):
        detail = "; ".join(failures) or "the directory is still present"
        return (
            f"could not remove {path_str}: {detail} — remove it by hand, then "
            "delete the state file to unwedge teardown"
        )
    return None


def _cleanup_worktrees(root: Path, paths: list[str], scratch: Path) -> list[str]:
    """Remove worktrees and verify no stray registration remains.

    Shared by setup's rollback and teardown so the two cannot drift. Each path
    de-registers itself inside `_remove_worktree`; there is deliberately no
    repo-global `git worktree prune` here — `_remove_worktree` holds the
    reason and this is the policy owner that honors it. The stray verification
    stays: a removal path that reports success while the registration survives
    must fail loud, not shrug.
    """
    errors = [
        error
        for path_str in paths
        if (error := _remove_worktree(root, path_str, scratch)) is not None
    ]
    registered = _registered_paths(root)
    # Through `_locate` like every other question this module asks of a
    # recorded path: `Path(p).resolve()` follows a final link, so a recorded
    # link would be looked up by its TARGET — reporting a stray that is not one
    # or, worse, missing the registration the link's own location still holds.
    strays = [p for p in paths if _locate(p) in registered]
    if strays:
        errors.append(
            f"still registered after removal: {', '.join(strays)}" + _STATE_REMEDY
        )
    return errors


def _refuse_repo_overlap(scratch: Path, root: Path) -> None:
    """Refuse a scratch dir that overlaps the repository, in either direction.

    Shared by setup and teardown deliberately, and load-bearing in teardown
    specifically: `_inside_scratch` is the one guard in front of every
    force-delete, and it is relative to `--scratch` alone — so it is only as
    strong as the scratch dir being somewhere the repository is not. With
    scratch an ancestor of the repo, every path in the repo is "inside
    scratch" and the force-delete guard covers the repository itself. Setup
    checked this and teardown did not, which left it missing from the half
    that actually deletes: `teardown --scratch C:\\Dev` against a state file
    recording a venv path of `C:\\Dev\\health\\healthspan` force-deleted the
    repository and printed success (reproduced).
    """
    if scratch == root or root in scratch.parents:
        raise AbortError(
            f"scratch dir {scratch} is inside the repo; use the scratchpad"
        )
    if scratch in root.parents:
        raise AbortError(f"scratch dir {scratch} contains the repo; use the scratchpad")


def setup(scratch: Path, agents: list[str], base: str) -> int:
    root = repo_root()
    scratch = _resolved_scratch(scratch)
    _refuse_repo_overlap(scratch, root)
    scratch.mkdir(parents=True, exist_ok=True)

    state_path = scratch / STATE_FILENAME
    if _exists(state_path):
        raise AbortError(
            f"a state file from a previous setup exists at {state_path} — "
            "run teardown first; overwriting it would orphan the worktrees "
            "it records, permanently"
        )

    tracked_personal = personal_tracked(root)
    if tracked_personal:
        # The count only, never the paths — the same redaction the untracked
        # twin below and the machine-local warning already apply, and for the
        # same reason: CLAUDE.md classifies "which lab, which panel, in what
        # order" as personal even with no values attached, and a
        # provider-named filename under specs/personal/ is exactly that. This
        # message reaches stderr, the session transcript, and every reviewer
        # report that relays it, so naming the files here would leak precisely
        # what the abort exists to protect. The operator can list them
        # locally; the command is in the remedy.
        raise AbortError(
            f"refusing to snapshot: {len(tracked_personal)} tracked file(s) "
            "exist under specs/personal/ — containment-by-construction holds "
            "only while nothing there is tracked; run `git ls-files -- "
            '":(icase)specs/personal/"` to see which, then fix that first'
        )

    has_diff = branch_has_diff(root, base)
    if has_diff is None:
        # Three conditions reach here and the triage in reviewer-isolation.md
        # routes them differently — an unresolvable ref is fixed by fetching,
        # an unborn HEAD by committing, while a resolvable ref sharing no
        # merge base is blocking — so the message has to say which, or the
        # reader cannot follow the triage. HEAD is probed first because
        # `diff base...HEAD` exits 128 when *either* side fails to resolve:
        # blaming the base for an unborn HEAD sent the operator to the
        # degraded fallback (that string is filed as blocking) when the fix
        # was to make a commit.
        head = _git(root, "rev-parse", "--verify", "--quiet", "HEAD^{commit}")
        if head.returncode != 0:
            raise AbortError(
                "HEAD does not resolve: this branch has no commits yet "
                "(a fresh `git init`, or `git checkout --orphan` before its "
                "first commit). Commit something first — the base ref is fine"
            )
        resolves = _git(root, "rev-parse", "--verify", "--quiet", f"{base}^{{commit}}")
        cause = (
            "shares no merge base with HEAD (orphan history, shallow clone, or "
            "a force-pushed base) — this is not fixed by fetching"
            if resolves.returncode == 0
            else "does not resolve — fetch it or pass --base"
        )
        raise AbortError(
            f"base ref {base!r} {cause}; both the nothing-to-review decision "
            "and the reviewers' diff scope depend on it"
        )

    diverged = index_worktree_divergence(root)
    if diverged:
        # The count only, never the paths — the same redaction the three
        # personal-data guards apply, and reachable with the same content.
        # `git rm --cached specs/personal/<file>` is the standard remediation
        # for the very state `personal_tracked` exists to catch, and it drops
        # the path from the index entirely: that guard then returns empty
        # while `--diff-filter=D` picks the same path back up here. Naming it
        # would print a provider- or panel-named filename to stderr, the
        # session transcript, and every reviewer report that relays the abort
        # — provenance under CLAUDE.md even with no values attached. The
        # operator lists them locally; the command is in the remedy.
        raise AbortError(
            f"staged index differs from the worktree for {len(diverged)} path(s) "
            "— the snapshot would review worktree bytes while a plain `git "
            "commit` (without -a) publishes the index, so those staged bytes "
            "would ship unreviewed; run `git status --short` to see which, then "
            "reconcile (`git add <paths>` to review the worktree bytes, or "
            "`git restore --staged <paths>`) and rerun"
        )

    # Printed as it is recorded, never in a loop later. A comment on that loop
    # used to claim the warnings were emitted "before anything can abort",
    # which was false in both directions: the tracked-personal abort three
    # lines above it had already fired past them, and so had the exit-2
    # gate's two filtered-everything aborts. Making the append BE the print
    # is what turns "every warning is emitted before any abort that follows
    # it" from a claim into a property. The list survives because the state
    # file records it as the durable copy.
    warnings: list[str] = []

    def warn(message: str) -> None:
        warnings.append(message)
        print(f"warning: {message}", file=sys.stderr)

    flagged = nonstandard_index_flags(root)
    if flagged:
        # The count, not the entries — the redaction all four sibling guards
        # already apply, and this one was the exception. A tracked,
        # skip-worktree-flagged file under specs/personal/ would have had its
        # provider- or panel-named path echoed to stderr, the transcript,
        # every relayed reviewer report and `state["warnings"]`. It was safe
        # only because `personal_tracked`'s abort happens to run first — an
        # ordering dependency nothing recorded and nothing enforced.
        warn(
            f"{len(flagged)} skip-worktree/assume-unchanged entry(ies) exist — "
            "their modifications are invisible to the snapshot; run `git "
            "ls-files -v` to see which"
        )

    manifest = untracked_manifest(root)
    # Refused before anything prints one. Stdout here is a line-oriented
    # protocol the launch procedure parses, so a path carrying a line
    # terminator can forge a whole line of it — a `state:` line aiming a later
    # teardown at a state file of the file's choosing, or a bare `\r` that
    # overwrites the line above and erases the skip-worktree warning. The
    # input side was hardened byte-for-byte and the output side printed raw.
    # Count only: the name is what is untrustworthy, so it is the last thing
    # to echo. POSIX-reachable — these characters are legal in a filename
    # there — and on Windows the filesystem refuses most of them anyway.
    forged = [rel for rel in manifest if _LINE_BREAKS.intersection(rel)]
    if forged:
        raise AbortError(
            f"{len(forged)} untracked path(s) contain a line-break character, "
            "which cannot be represented in this script's line-oriented "
            "manifest — rename them, ignore them, or move them outside the repo"
        )
    # `personal_tracked` above proves nothing under specs/personal/ is
    # *tracked*; containment-by-construction additionally requires it stay
    # *ignored*, and only the second keeps it out of this manifest. Delete
    # .gitignore's rule (or shadow it with a negation) and the directory
    # becomes ordinary untracked content: replicated into every agent-readable
    # worktree, printed into the transcript, and persisted in the state file.
    # Checking the manifest rather than the rule catches that whatever the
    # cause. The count only — naming the files here would leak exactly what
    # the abort exists to protect.
    # Case-folded: git emits forward slashes but preserves the on-disk casing,
    # and on the case-insensitive filesystems this project runs on (Windows,
    # macOS) `Specs/Personal/` is the same directory. A case-sensitive check
    # would pass it straight through — on the one guard where a miss means
    # replicating health data into two agent-readable worktrees.
    personal = [rel for rel in manifest if _is_personal(rel)]
    if personal:
        raise AbortError(
            f"refusing to snapshot: {len(personal)} untracked file(s) under "
            "specs/personal/ are visible to git, so they are no longer ignored "
            "— restore that .gitignore rule before running any review; "
            "containment holds by construction only while they stay ignored"
        )
    # The same directory, reached under a different name. A junction or
    # symlinked directory *inside* the repo pointing at specs/personal/
    # defeats every guard above and beside it simultaneously: `_is_personal`
    # reads `notes/labs.md` and sees no prefix, the leaf-level `_is_link`
    # filter sees no link (the link is `notes`), and `_escapes` answers False
    # because the target is still inside the repository — the `outside` filter
    # below only catches links *leaving* the tree. Measured end-to-end:
    # setup exited 0, printed `?? notes/labs.md` as ordinary in-scope content,
    # and put the file in both agent-readable worktrees.
    #
    # Tracked paths are checked too, and that is not defensive: `git add
    # notes` on such a junction stages `notes/labs.md` as an ordinary tracked
    # path (measured), which `personal_tracked`'s pathspec cannot match and
    # which `git worktree add` then checks out into every worktree from the
    # snapshot — no copy step involved, so the untracked guard never sees it.
    # One predicate over both lists, so the two cannot drift apart the way the
    # tracked and untracked prefix tests already did once.
    tracked = tracked_entries(root)
    # Two routes, one predicate. Which one is reachable depends on the platform,
    # and neither covers the other:
    #
    # * A **directory** link that git walks *through* — the Windows junction.
    #   git reports `notes/labs.md` as an ordinary path with the link appearing
    #   only as a component, so only the components can find it.
    # * A **tracked symlink entry** — the POSIX form. git records `notes` as
    #   mode 120000 and recreates it faithfully in every worktree; if its target
    #   is absolute it then points at the LIVE repo's specs/personal, and a
    #   reviewer reads the real data straight through it (measured under WSL:
    #   `notes -> <repo>/specs/personal`, contents readable from the worktree).
    #   There is no path component to inspect here — the link IS the leaf — so
    #   the directory sweep above cannot see it, and `_is_personal` sees only
    #   `notes`. A *relative* target is harmless (it points inside the worktree,
    #   where the gitignored directory was never checked out), and resolving is
    #   what distinguishes the two rather than a rule about link targets.
    linked_personal = [
        rel
        for rel in _linked_dirs(root, [rel for _, rel in tracked] + manifest)
        + [rel for mode, rel in tracked if mode == "120000"]
        + [rel for rel in manifest if _is_link(root / rel)]
        if _leaks_personal(root / rel, root)
    ]
    if linked_personal:
        # The count and never the names, like every guard around it: a link
        # into specs/personal/ is named by whoever created it, and the name
        # they chose is as much provenance as the directory it points at.
        raise AbortError(
            f"refusing to snapshot: {len(linked_personal)} path(s) inside the "
            "repository are junctions or symlinks resolving into "
            "specs/personal/ — git reports what is behind them under an "
            "ordinary in-scope name, so no other guard here can tell what they "
            "are; remove the link before running any review"
        )
    # The same data again, reached with no link to resolve at all. A hard link
    # gives one of the owner's files a second name and no target, so
    # `_leaks_personal` answers False and `_is_personal` sees only the name
    # the operator chose. ADR-0068 booked this as an accepted gap when the
    # only thing on offer was a hard-link-specific check; one walk of one
    # small directory answers it for the whole manifest instead, and the same
    # set is what makes the artifact walk after materialization possible.
    #
    # Measured before this guard existed: the content of a hard link into
    # specs/personal/ landed in BOTH worktrees with setup exiting 0.
    personal_identities = _personal_identities(root)
    hard_linked = _hard_linked_personal(root, manifest, personal_identities)
    if hard_linked:
        raise AbortError(
            f"refusing to snapshot: {hard_linked} untracked path(s) are a "
            "second name for a file under specs/personal/ — a hard link has no "
            "target to resolve, so no other guard here can tell what they are; "
            "remove the link before running any review"
        )
    # Taken HERE, below every guard whose message says "refusing to snapshot",
    # and that ordering is the message's only way of being true. `git stash
    # create` writes a tree and a commit into `.git/objects`, so for the
    # tracked-junction case (`git add notes` on a junction into
    # specs/personal/, which `test_a_tracked_path_under_such_a_junction_is_
    # refused_too` covers) the index differs from HEAD and the snapshot has
    # already written objects NAMING that content before the guard refuses —
    # and reviewer-isolation.md invariant 2 says teardown deliberately never
    # cleans the object database. The exposure is small (the objects are
    # unreachable and gc-collectable, and `git add` had already written the
    # blobs), but it is not nothing, and the sibling `personal_tracked` guard
    # was already deliberately placed before the snapshot — which is what
    # showed the intended ordering. Moving these costs nothing: not one of
    # them needs the SHA.
    sha = snapshot(root)
    hidden = machine_hidden_untracked(root, manifest)
    if hidden:
        # A bare count, never the paths — not the filenames and not their
        # directories either. Someone hiding personal material via
        # .git/info/exclude almost always hides a *directory*, so the parent
        # is the leak, not the protection: CLAUDE.md is explicit that "the
        # provenance or sequence of the owner's actual records — which lab,
        # which panel, in what order — is personal even with no values
        # attached", and a provider-named directory is exactly that. This
        # warning is echoed into transcripts, relayed into reviewer reports,
        # and persisted in the state file, so the count is all it may carry;
        # the operator can read their own exclude file to find out which.
        warn(
            "machine-local ignore rules (.git/info/exclude or core.excludesFile) "
            f"hide {len(hidden)} untracked file(s) from this review — check "
            "those files yourself if the count is unexpected"
        )
    symlinks = [rel for rel in manifest if _is_link(root / rel)]
    if symlinks:
        # A count, not the names, and this site used to print them. An
        # untracked symlink's own leaf name is operator-chosen —
        # `<provider>-2026-panel -> …` — which CLAUDE.md classifies as
        # personal with no values attached, and this message reaches stderr,
        # the transcript, every relayed reviewer report and the state file.
        # The link that resolves into specs/personal/ now aborts above; this
        # covers every other link, whose chosen name is no safer to publish.
        # `git status` names them for the operator without any of those hops.
        warn(
            f"{len(symlinks)} untracked symlink(s) are not replicated into "
            "worktrees — list them with `git status` if that count is unexpected"
        )
        skipped = set(symlinks)
        manifest = [rel for rel in manifest if rel not in skipped]
    # Files git reports as ordinary untracked content that are not actually
    # inside the tree. A directory junction or symlinked directory ANYWHERE on
    # the path puts them there, and the leaf-level check above cannot see it:
    # git walks through the link and reports `linked-dir/labs.md` with no
    # marker of any kind, while both `linked-dir` and the leaf answer
    # `_is_link` False for a junction. Measured — `mklink /J` needs no
    # privilege, so this is the form a Windows user can create by accident —
    # `copy2` then replicated bytes from outside the repository into both
    # agent-readable worktrees, printed them in the manifest as ordinary
    # in-scope content, and emitted no warning at all. Dropped like the
    # symlinks above rather than aborting: a junction to a build cache is an
    # ordinary thing to have in a tree, and out-of-repo content was never in
    # review scope to begin with.
    #
    # A bare count, never the paths: the leaf names here come from OUTSIDE the
    # repository, and so outside review scope, on a machine that holds a real
    # encrypted health database — and this warning reaches stderr, the
    # transcript, the relayed manifest and the state file. Same redaction,
    # same reason, as the guards above.
    outside = [rel for rel in manifest if _escapes(root / rel, root)]
    if outside:
        warn(
            f"{len(outside)} untracked path(s) resolve outside the repository "
            "(a junction or symlinked directory on the path) and are NOT "
            "replicated into worktrees or reviewed — check the tree for linked "
            "directories if that count is unexpected"
        )
        escaped = set(outside)
        manifest = [rel for rel in manifest if rel not in escaped]

    if sha is None and not manifest and not has_diff:
        if flagged:
            # `nonstandard_index_flags` exists solely because "modifications
            # to such files are invisible to `git stash create`, so the
            # snapshot silently lacks them" — which is positive evidence that
            # this tree is NOT clean, held by the launcher, while the gate
            # tested only `sha is None and not manifest and not has_diff` and
            # never consulted it. Exit 2 is the load-bearing code, the only
            # one that sanctions skipping the reviewers, so earning it on a
            # tree the snapshot is known to be blind to is a false pass. The
            # two neighbours below were promoted from exit 2 to hard aborts
            # for the same reason — "everything reviewable was filtered out,
            # not absent" — and this is that shape with strictly worse
            # content: tracked source rather than a symlink. For
            # `--assume-unchanged` the hidden edit can even land, because an
            # explicitly named `git add <path>` still stages it.
            raise AbortError(
                f"{len(flagged)} skip-worktree/assume-unchanged entry(ies) "
                "hide their modifications from the snapshot, so 'nothing to "
                "review' cannot be established for this tree — clear the "
                "flags (`git update-index --no-skip-worktree` / "
                "`--no-assume-unchanged`) and rerun; run `git ls-files -v` to "
                "see which"
            )
        if symlinks:
            # Everything reviewable was filtered out, not absent: exit 2's
            # "nothing to review" would ship the symlink-only change with a
            # stderr warning as its only trace.
            raise AbortError(
                f"the only uncommitted change is {len(symlinks)} untracked "
                "symlink(s), which are never replicated into worktrees — "
                "review them by hand or remove them"
            )
        if outside:
            # The same distinction for the same reason, on the filter added
            # beside it: "nothing to review" is false when there WERE
            # untracked files and every one of them resolved out of the tree.
            # Unnamed here, as in the warning above — the leaf names come from
            # outside the repository.
            raise AbortError(
                f"the only untracked content is {len(outside)} path(s) that "
                "resolve outside the repository through a junction or "
                "symlinked directory, which are never replicated into "
                "worktrees — review them by hand, or remove the link"
            )
        # The warnings still matter here — a skip-worktree edit is exactly
        # what can make a genuinely-changed tree look like nothing — and they
        # are already on stderr, printed as each was recorded.
        print("nothing to review: tree clean, no untracked files, no branch diff")
        return 2

    # No warning loop here: `warn` above already emitted each one as it was
    # recorded, which is what keeps them from being dropped by every failure
    # past this point — a failed `worktree add`, a copy error, a kill — and by
    # a successful rollback, which deletes the state file billed as their
    # durable copy. The skip-worktree warning is the one signal that a
    # genuinely-changed tree can look clean, so losing it on an abort path
    # loses it exactly when it matters.
    short = (sha or "HEAD")[:9]
    worktrees = {
        agent: str(scratch / f"wt-{agent}-{short}-{secrets.token_hex(3)}")
        for agent in agents
    }
    # This is the site that governs the venv redirect; everywhere else states
    # the instruction and points here. Fixed short names, deliberately outside
    # the worktrees, and the reason is MAX_PATH headroom for `uv` — nothing
    # else. Two rationales previously stated here and in four other documents
    # were measured false and removed: a venv inside a worktree produces
    # **zero** `??` entries (uv writes a `.gitignore` holding `*` into every
    # env it creates, and `.venv/` is ignored by this repo's own tracked
    # .gitignore), and `git worktree remove --force` deletes an ignored venv
    # for free in one pass, read-only files included. What survives is that
    # `_LONGPATHS` buys long-path support for *git* only: `uv` has no
    # equivalent flag, and a worktree path already ~135 characters deep plus
    # `.venv/Lib/site-packages/...` is how a build lands past MAX_PATH on a
    # host without the OS-wide flag. The scratch-level name is ~20 characters
    # shorter and, being outside the worktree, cannot lengthen with it.
    venvs = {agent: str(scratch / f"venv-{agent}") for agent in agents}
    # Validated before it is published, because publishing it IS the write.
    # Setup does not create these directories — it prints them with the
    # instruction to export each as UV_PROJECT_ENVIRONMENT — so a pre-existing
    # junction here (left by a hand-cleanup after a failed teardown) makes
    # every `uv run` materialize the environment *through* the link into its
    # target: a reviewer writing outside the isolation boundary, possibly into
    # the live tree, before anything else in the module gets a chance to
    # refuse. Teardown already unlinks such a link correctly; by then the
    # writes have happened. This is the only place that can refuse the name
    # rather than the aftermath. An ordinary leftover *directory* is fine and
    # deliberately allowed — uv reuses it, which is the point of a fixed name.
    linked_venvs = [agent for agent, venv in venvs.items() if _is_link(Path(venv))]
    if linked_venvs:
        raise AbortError(
            f"the venv path for {', '.join(linked_venvs)} is a symlink or "
            f"junction under {scratch} — every uv run would build through it, "
            "outside the isolation boundary. Remove it and rerun"
        )
    # The state file is written BEFORE any worktree exists: a process killed
    # mid-materialization then leaves a state file naming every planned path,
    # and teardown tolerates paths that never got created — the reverse order
    # leaves registered worktrees that nothing on disk records. It also
    # records the repo root: teardown must act on the repo that created the
    # worktrees, not whatever repository its own cwd happens to be inside.
    # Past the three keys teardown consumes (root, worktrees, venvs) this is
    # the durable copy of the manifest printed below — snapshot, base,
    # untracked and warnings outlive the stdout that carried them, which is
    # what keeps a half-finished round diagnosable once its transcript is
    # gone. It is not a machine-readable interface: the launch procedure
    # reads stdout, so nothing outside this file depends on the JSON shape.
    # `untracked` here is what was *planned*, necessarily — it is written
    # before the copy runs, so a file that vanishes mid-copy stays listed.
    # Stdout is what landed; the two differ only in that case, and only there
    # does the stderr warning above say so.
    state = {
        "root": str(root),
        # Identifies THIS round's file, so a teardown cannot delete a state
        # file some later setup wrote — see `_unlink_if_ours`. Written here
        # rather than derived from the worktree names because those are
        # recorded in the file itself, and a value a racing teardown could
        # recompute proves nothing about which round wrote it.
        "nonce": secrets.token_hex(8),
        "snapshot": sha,
        "base": base,
        "worktrees": worktrees,
        "venvs": venvs,
        "untracked": manifest,
        "warnings": warnings,
    }
    # Created EXCLUSIVELY, which is what actually reserves the scratch dir.
    # The `exists()` test far above is a courtesy — it fails early, before all
    # the git work — but it is check-then-write, and the window between it and
    # this line is seconds long: a re-launch after a hung reviewer, an
    # `/apply-review` re-running the round, two sessions sharing a scratchpad.
    # Both setups passed the check, the second clobbered the first's state
    # file, and the first's two worktrees were orphaned permanently carrying
    # the uncommitted work under review. Only the atomic create can refuse
    # that, so the guarantee lives here and the early check is only for the
    # error message's timing.
    #
    # This replaces a write-tmp-then-`os.replace` pair whose atomicity bought
    # nothing here: the state file is written BEFORE any worktree exists, so
    # the JSON a kill could truncate names nothing that is on disk yet.
    try:
        with state_path.open("x", encoding="utf-8") as handle:
            handle.write(json.dumps(state, indent=2))
    except FileExistsError as exc:
        raise AbortError(
            f"a state file appeared at {state_path} while this setup was "
            "running — another setup is using this scratch dir. Use a "
            "different --scratch, or run teardown first"
        ) from exc

    attempted: list[str] = []
    first_worktree: Path | None = None
    vanished: set[str] = set()
    try:
        for path_str in worktrees.values():
            attempted.append(path_str)
            _git_ok(root, "worktree", "add", "--detach", path_str, sha or "HEAD")
            vanished |= set(
                _copy_untracked(first_worktree or root, Path(path_str), manifest)
            )
            # Inside the loop rather than after it, so a breach in the first
            # worktree stops before a second copy of it exists. The abort is
            # raised in the materialization block on purpose: the rollback
            # below then removes both the worktrees and the state file, which
            # is what keeps a refused round from leaving the leak on disk.
            breaches = _artifact_breaches(Path(path_str))
            if breaches:
                raise AbortError(
                    "the materialized worktree fails the containment check — "
                    + "; ".join(breaches)
                    + ". This is the by-construction assertion the guards "
                    "above only approximate, so the round is refused rather "
                    "than reviewed; inspect the tree for links leaving it "
                    "(`git ls-files -s` shows tracked ones as mode 120000)"
                )
            if first_worktree is None:
                first_worktree = Path(path_str)
        if vanished:
            manifest = [rel for rel in manifest if rel not in vanished]
        if not (sha is not None or has_diff or manifest):
            # Raised INSIDE the try so the rollback below removes the
            # worktrees and deletes the state file: this abort tells the
            # operator to rerun, and setup refuses to run while a state file
            # from a previous setup survives.
            #
            # The exit-2 gate above proved something was reviewable, but it
            # ran BEFORE the vanished-file filter — so an untracked-only round
            # whose files all vanished mid-copy passed the gate and was
            # emptied afterwards. Measured: it printed `snapshot: HEAD
            # (reviewing )` and returned 0, and both reviewers then launched
            # against worktrees byte-identical to the base and reported clean.
            # That is a false pass, strictly worse than the skip exit 2
            # sanctions, and it is the class ADR-0068 exists to eliminate. The
            # check lives here rather than in a re-run of the gate so the
            # decision cannot depend again on the order of a later filter.
            raise AbortError(
                "every untracked file vanished between the manifest and the "
                "copy, and nothing else is in scope — the worktrees are "
                "identical to the base, so reviewing them would pass over "
                "nothing; rerun"
            )
    except (AbortError, OSError):  # fmt: skip
        # The unwind must never replace the abort that triggered it. A git
        # call inside _cleanup_worktrees can fail on its own (index.lock held
        # by a concurrent git, an fsmonitor hiccup, the _run timeout), and an
        # exception escaping here would skip the state-file decision AND the
        # `raise` below — printing the unwind's error as though it were the
        # cause, losing the real one to __context__, and leaving a state file
        # the next setup refuses. Whatever goes wrong in here becomes a
        # rollback error instead, which keeps the state file for a retry.
        try:
            rollback_errors = _cleanup_worktrees(root, attempted, scratch)
            if not rollback_errors:
                _unlink(state_path, missing_ok=True)
        except (AbortError, OSError) as unwind_exc:
            rollback_errors = [f"the unwind itself failed: {unwind_exc}"]
        if rollback_errors:
            # A worktree survived the rollback (or its registration did);
            # the state file is the only record of it — deleting it would
            # orphan the worktree permanently. Keep it for a teardown retry.
            for error in rollback_errors:
                print(f"error: rollback: {error}", file=sys.stderr)
            print(
                f"state file kept at {state_path} — run teardown to retry",
                file=sys.stderr,
            )
        raise

    if vanished:
        # Emitted here rather than folded into `warnings` above: those are
        # emitted before materialization on purpose, and this one cannot be
        # known until the copy has run. The manifest below drops these so it
        # describes what the worktrees actually hold — a reviewer reconciling
        # `??` entries against a manifest naming a file that is not there
        # would read the launcher's own tolerance as a fidelity failure.
        note = (
            f"{len(vanished)} untracked file(s) vanished between the "
            f"manifest and the copy and are not in the worktrees: "
            f"{_capped_after_personal_filters(sorted(vanished))}"
        )
        print(f"warning: {note}", file=sys.stderr)
        # ...and made durable, which it was not. Every other fidelity warning
        # goes through `warn()` into `state["warnings"]`; this was a bare
        # print, so the state file's `untracked` list — necessarily the
        # PRE-copy plan, written before materialization — stayed knowably
        # wrong in exactly one case, and the one line explaining that case was
        # the one line not recorded. The state-file comment bills the file as
        # "what keeps a half-finished round diagnosable once its transcript is
        # gone", and a half-finished round is precisely when a file vanishes
        # mid-copy.
        #
        # Rewritten via a temp file and `os.replace`, unlike the original
        # write: that one preceded every worktree, so a truncated JSON named
        # nothing that existed yet. This one does not, and a kill mid-write
        # would leave two materialized worktrees behind a state file teardown
        # cannot parse. The nonce is carried through unchanged — it still
        # identifies this round to `_unlink_if_ours`.
        state["warnings"] = [*warnings, note]
        state["untracked"] = [rel for rel in manifest if rel not in vanished]
        staging = state_path.with_name(state_path.name + ".tmp")
        with staging.open("w", encoding="utf-8") as handle:
            handle.write(json.dumps(state, indent=2))
        os.replace(staging, state_path)

    # Name every part of what is actually in scope. `sha is None` means only
    # that no tracked file is modified — the exit-2 gate above already proved
    # something is reviewable — so a fixed "reviewing branch diff only" would
    # tell a reviewer relaying this manifest that the untracked files listed
    # below are out of scope, which is the never-`git add`-ed blind spot the
    # copy-in exists to close. The all-vanished case cannot reach here; it
    # aborts inside the materialization block above, where the rollback is.
    reviewing = " and ".join(
        part
        for part in (
            "uncommitted tracked changes" if sha is not None else "",
            f"the {base}...HEAD branch diff" if has_diff else "",
            f"{len(manifest)} untracked file(s)" if manifest else "",
        )
        if part
    )
    print(f"snapshot: {sha or 'HEAD'} (reviewing {reviewing})")
    if sha is not None:
        print(
            "  (ephemeral by design: the SHA is unreachable once the worktrees "
            "are removed — it identifies the reviewed state only for this session)"
        )
    print(f"base: {base}")
    for agent, path_str in worktrees.items():
        print(f"worktree[{agent}]: {path_str}")
    for agent, venv in venvs.items():
        print(f"venv[{agent}]: {venv}")
    print(
        "  (each reviewer must export UV_PROJECT_ENVIRONMENT=<its venv line> "
        "for uv runs — it keeps uv clear of MAX_PATH inside an already-deep "
        "worktree path, which uv has no flag to survive)"
    )
    # The keychain belt, issued by the launcher rather than left to prose.
    #
    # The suite's other two layers — the autouse `fake_keychain` fixture and
    # the import-time assignment in `tests/conftest.py` — both live INSIDE the
    # tree test-reviewer is chartered to mutate, so the suite is hardened
    # against its own reviewer inside the artifact that reviewer edits. The
    # only layer genuinely outside the mutable tree was a sentence in
    # `test-reviewer.md` telling the reviewer to export the value itself, and
    # a sentence is the layer with no mechanism at all. § Confining already
    # records why prose loses here: Bash environment is per-call exactly as
    # cwd is, so an `export` in one call is gone by the next, and neither
    # absence announces itself. A miss is silent, and the fallout is a real OS
    # credential vault beside a real encrypted health database.
    #
    # Printing it does not enforce it either — nothing here can — but it makes
    # the correct command a copy rather than a recollection, and puts both
    # variables in one string so exporting the venv and forgetting the backend
    # stops being the easy mistake.
    for agent, venv in venvs.items():
        print(
            f"env[{agent}]: UV_PROJECT_ENVIRONMENT={venv} "
            f"PYTHON_KEYRING_BACKEND={_FAIL_BACKEND}"
        )
    print(
        f"  (prefix every command with its env[...] line. {_FAIL_BACKEND} makes "
        "any real backend resolution raise instead of reading this machine's "
        "credential store — the suite sets it too, but that copy is inside the "
        "tree a reviewer may mutate)"
    )
    print(f"untracked files copied into each worktree: {len(manifest)}")
    for rel in manifest:
        print(f"  ?? {rel}")
    print(f"state: {state_path}")
    return 0


def _recorded_paths(
    state_map: dict[str, object], key: str, state_path: Path, *, required: bool
) -> list[str]:
    """The paths recorded under `key`, validated; abort if the shape is wrong.

    One helper rather than a block per category, because the state file is
    built to grow — it gained `venvs` after `worktrees` already — and a
    per-category copy means the next one is pasted in and a validation fix
    has to be found N times. Values only: the dict is keyed by agent name,
    and teardown removes what was created regardless of whose it was.

    `required` is the one real difference and it is load-bearing in opposite
    directions. `worktrees` must be present AND non-empty: defaulting it to
    empty would let a truncated state file tear down nothing, report success,
    and delete the only record of the worktrees it just orphaned — and so
    would a present-but-empty `{}`, which the presence-and-type test accepted
    verbatim while the docstring claimed the opposite. `_STATE_REMEDY` invites
    the operator to hand-edit this file, so `{}` is a spelling that arrives
    without an adversary. `venvs` must be optional: a state file written by a
    setup from before that key existed — the script edited mid-flight, exactly
    what happens while this tool is developed — must still tear down.
    """
    raw = state_map.get(key) if required else state_map.get(key, {})
    if not isinstance(raw, dict):
        raise AbortError(
            f"state file {state_path} is malformed ({key})" + _STATE_REMEDY
        )
    paths: list[str] = []
    for value in cast("dict[str, object]", raw).values():
        if not isinstance(value, str):
            raise AbortError(
                f"state file {state_path} is malformed ({key})" + _STATE_REMEDY
            )
        paths.append(value)
    if required and not paths:
        raise AbortError(
            f"state file {state_path} records no {key} — a setup always writes "
            "one per agent, so an empty map means the file was truncated or "
            "hand-edited; tearing down nothing and deleting it would orphan "
            "whatever it used to name" + _STATE_REMEDY
        )
    return paths


def _stray_note(scratch: Path, *recorded: list[str]) -> str:
    """A closing clause naming `wt-*`/`venv-*` dirs the records do not cover.

    Teardown iterates the recorded strings only, and so does
    `_cleanup_worktrees`, so anything the scratch dir accumulated outside
    those records is deleted by no code path, named by no error, and counted
    by no success line. Two ways in, both ordinary rather than adversarial:
    setup does not create the venv dirs — it prints them and instructs each
    reviewer to export the path as `UV_PROJECT_ENVIRONMENT`, and nothing
    verifies that the export happened or that uv resolved to it — and a round
    orphaned by a racing teardown leaves a full worktree behind with its only
    record gone. Each stray is a complete copy of the repository carrying
    uncommitted work and, for test-reviewer, its mutation edits.

    `_STATE_REMEDY` says "remove any wt-* strays", naming neither `venv-*`
    nor a sweep, so even the manual escape hatch covered half the leak.

    Reports rather than deletes, deliberately. Everything else teardown
    removes is something it has a record of creating; a directory it cannot
    account for is exactly the thing this module must not force-delete on its
    own initiative — the recorded-path containment machinery exists because
    that judgment is where the data-loss defects came from.
    """
    known = {_locate(path) for group in recorded for path in group}
    try:
        strays = sorted(
            entry.name
            for entry in scratch.iterdir()
            if entry.name.startswith(("wt-", "venv-"))
            and _locate(str(entry)) not in known
        )
    except OSError:
        return ""  # the scratch dir went away under us; nothing to report
    if not strays:
        return "; no strays registered, none left in the scratch dir"
    return (
        f"; no strays registered, but {len(strays)} unrecorded wt-*/venv-* "
        f"director(ies) remain in {scratch} ({', '.join(strays)}) — this "
        "teardown has no record of them and will not delete them; remove "
        "them by hand once you are sure no round is using them"
    )


def _unlink_if_ours(state_path: Path, nonce: object) -> bool:
    """Delete the state file only if it is still the one this run read.

    `missing_ok=True` alone tolerates the racing-teardown case the launch
    procedure makes ordinary — it says to run teardown on every exit path, so
    a retry means two teardowns on one scratch dir — but it tolerates the
    wrong thing, because "gone" and "a different round's" are the same
    observation to it. Sequence: T1 and T2 both start; T1 finishes and
    unlinks; a new setup finds no state file, proceeds, writes its own and
    materializes two worktrees; T2 — still inside `_force_delete_tree` on a
    several-hundred-MB venv, tens of seconds — finishes with no errors and
    unlinks the NEW round's file. That round's worktrees are then on disk and
    registered, carrying the uncommitted work under review, with no record
    anywhere, and its own teardown later prints "nothing recorded to tear
    down" and exits 0. That is the silent-orphan outcome the reservation at
    `state_path.open("x")` exists to prevent, reached from the other end.

    The nonce is what makes the two observations distinguishable. Absent on
    both sides — a hand-edited file, which `_STATE_REMEDY` actively invites —
    compares equal and unlinks, preserving the old behavior for the format
    that has no nonce to check.

    Compare-then-unlink is not itself atomic, and deliberately: the window it
    leaves is the microseconds between the read and the unlink, against the
    tens of seconds a real cleanup takes, and no cross-platform "unlink only
    if the content still matches" primitive exists to close it.

    Returns whether the file was this run's.
    """
    try:
        current: object = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):  # fmt: skip
        # Gone, or no longer parseable. Either way there is nothing here this
        # run can claim, and it has already done its own cleanup.
        return True
    if (
        isinstance(current, dict)
        and cast("dict[str, object]", current).get("nonce") != nonce
    ):
        return False
    _unlink(state_path, missing_ok=True)
    return True


def teardown(scratch: Path) -> int:
    scratch = _resolved_scratch(scratch)
    state_path = scratch / STATE_FILENAME
    if not _exists(state_path):
        # A clean no-op, not an error: "run teardown on every exit path"
        # must not manufacture a false alarm when setup exited 2 or aborted
        # without leaving anything behind.
        print(f"no state file at {state_path}; nothing recorded to tear down")
        return 0
    try:
        state: object = json.loads(state_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        # UnicodeDecodeError as well, because it is a ValueError and NOT an
        # OSError: it escaped this clause and `main`'s
        # `except (AbortError, OSError)` alike, so a state file holding one
        # non-UTF-8 byte killed teardown with a traceback carrying none of the
        # remedy below — and since the state file is then never unlinked,
        # every later setup is refused too. Reachable without adversary on the
        # primary platform: `_STATE_REMEDY` tells the operator to inspect and
        # hand-edit this file, and an editor or a `Set-Content` without
        # `-Encoding UTF8` writing it back in the console codepage is exactly
        # the Windows-1252 corruption CLAUDE.md documents. (Binding `as exc`
        # keeps `ruff format` from stripping the parentheses, so this clause
        # needs no `# fmt: skip`.)
        raise AbortError(
            f"state file {state_path} is malformed ({exc})" + _STATE_REMEDY
        ) from exc
    if not isinstance(state, dict):
        raise AbortError(
            f"state file {state_path} is malformed (not an object)" + _STATE_REMEDY
        )
    state_map = cast("dict[str, object]", state)

    # The recorded root, not repo_root(): teardown must act on the repo that
    # created the worktrees. Deriving the repo from the invocation cwd would
    # let a teardown run from inside another repository "succeed" — deleting
    # the directories while the creating repo's registrations dangle with
    # their only on-disk record gone.
    root_raw = state_map.get("root")
    if not isinstance(root_raw, str) or not Path(root_raw).is_absolute():
        # Absolute as well as a string, for the reason `_resolved_scratch`
        # refuses a relative `--scratch`: this value's whole job is to make
        # teardown cwd-independent, and a relative one reintroduces exactly
        # the dependence it exists to remove. Measured, `"root": "."` and
        # `"root": ""` were both accepted — `Path(".")` passes the existence
        # and `.git` checks from inside ANY repository — after which every
        # `git -C .` acted on the cwd's repo: the directories deleted, the
        # wrong registry queried and found clean, "no strays registered"
        # printed, and the state file unlinked, while the real repository kept
        # both worktrees registered against deleted directories with their
        # only record gone.
        raise AbortError(f"state file {state_path} is malformed (root)" + _STATE_REMEDY)
    root = Path(root_raw)
    # The same overlap refusal setup applies, and this is the subcommand that
    # actually deletes: see `_refuse_repo_overlap`.
    _refuse_repo_overlap(scratch, root.resolve())
    # `.git` too, not just the directory: a repo re-cloned in place passes an
    # exists() check and then fails every git call below, where the first
    # AbortError discards the accumulated error list and returns 1 on every
    # retry forever. Both failures deserve the same guided escape.
    if not _exists(root) or not _exists(root / ".git"):
        missing = "no longer exists" if not _exists(root) else "is no longer a git repo"
        raise AbortError(
            f"state file {state_path} records repo root {root}, which "
            f"{missing} — remove any leftover worktree dirs under "
            f"{scratch} by hand, then delete the state file"
        )

    paths = _recorded_paths(state_map, "worktrees", state_path, required=True)
    venv_paths = _recorded_paths(state_map, "venvs", state_path, required=False)

    # Counted BEFORE anything is removed, because the success line's whole
    # job is to say what this run deleted. It counted `len(paths)` — the
    # *records* — and `_remove_plain_dir` returns success for an
    # already-absent path, so every ordinary round printed "removed 2 venv
    # dir(s)" for directories setup never creates (setup prints the venv
    # paths and leaves each reviewer to build one; spec-reviewer is told the
    # normal round builds nothing). The comment on that line says both counts
    # exist so a run cannot report success while naming nothing it deleted —
    # which a count of records cannot do, in exactly the direction it was
    # added to prevent. One test asserted the old reading directly, on a
    # deliberately never-created worktree.
    present = [path for path in paths if _exists(_locate(path))]
    present_venvs = [path for path in venv_paths if _exists(_locate(path))]

    try:
        errors = _cleanup_worktrees(root, paths, scratch)
    except (AbortError, OSError) as exc:
        # Guarded for the same reason setup guards its identical call, which
        # teardown had no twin for. A git call in there can fail on its own —
        # an index.lock held by a concurrent git, an fsmonitor stall, the
        # timeout — and an escaping exception discards every error already
        # accumulated AND skips venv cleanup entirely, leaving the operator
        # with the name of the query that failed instead of the obstacle, in
        # an exit-1 shape reviewer-isolation.md's teardown triage does not
        # enumerate. Becoming one more error keeps both.
        errors = [f"worktree cleanup failed: {exc}"]
    # Guarded like the worktree call above, and this loop is what that guard
    # was written for. `_remove_plain_dir` reaches unguarded `resolve()` calls
    # (`_force_delete_tree`, `_inside_scratch`) and `shutil.rmtree`, any of
    # which can raise OSError. An escape reached `main`'s
    # `except (AbortError, OSError)`, which discards every error already
    # accumulated above, skips the remaining venvs, never unlinks the state
    # file, and prints one bare `error: [Errno ...]` line — verbatim the
    # failure the comment above describes. The fix went to the call above and
    # not to the loop it was protecting. Per-venv rather than around the whole
    # loop, so one unreachable venv does not hide the rest.
    for venv in venv_paths:
        try:
            error = _remove_plain_dir(venv, scratch)
        except (AbortError, OSError) as exc:  # fmt: skip
            error = f"venv cleanup failed for {venv}: {exc}" + _STATE_REMEDY
        if error is not None:
            errors.append(error)

    if errors:
        for error in errors:
            print(f"error: {error}", file=sys.stderr)
        return 1

    # Tolerating a state file that vanished under us is deliberate — the
    # launch procedure says to run teardown on every exit path, so two
    # teardowns racing on one scratch dir is an ordinary outcome of one retry,
    # and raising FileNotFoundError *after* removing everything successfully
    # would surface as a bare `error: [Errno 2]` exit 1 that the teardown
    # triage does not enumerate, for a run that did its job. What is NOT
    # tolerable is deleting a file this run never read; `_unlink_if_ours`
    # separates the two.
    if not _unlink_if_ours(state_path, state_map.get("nonce")):
        print(
            f"left {state_path} in place: it belongs to a later round, not "
            "this one. Everything this teardown recorded has been removed",
            file=sys.stderr,
        )
    # Both counts: a run that force-deleted venv dirs and no worktrees
    # reported "removed 0 worktree(s)" and named nothing it had deleted,
    # which is what made the missing containment guard silent. They count
    # what was ON DISK before this run started and is gone now — not the
    # records, which cannot make that distinction at all.
    print(
        f"removed {len(present)} of {len(paths)} recorded worktree(s) and "
        f"{len(present_venvs)} of {len(venv_paths)} recorded venv dir(s)"
        f"{_stray_note(scratch, paths, venv_paths)}"
    )
    return 0


class _Parser(argparse.ArgumentParser):
    """An ``ArgumentParser`` whose usage errors do not look like a skip.

    argparse exits **2** on any usage error, and 2 here means "nothing to
    review — do not launch reviewers". A typo'd flag would therefore read to
    the orchestrating session as a sanctioned skip on a dirty tree — the
    silent-skip class ADR-0068 exists to prevent, and the same collision
    scripts/bot_review.py's ``_Parser`` fixes for its ``EXIT_CLEAN``.
    Usage errors exit 1 instead, which every caller treats as abort.

    A deliberate copy of that class, not an import of it — but not for the
    two reasons first recorded here, both of which measure false.
    ``bot_review``'s imports are **100% stdlib**, so importing it would add
    exactly zero dependencies; and both files live in ``scripts/``, so
    ``python scripts/review_worktree.py`` already puts that directory at
    ``sys.path[0]`` and a bare ``import bot_review`` resolves with no path
    manipulation. The repo's one cross-script import is not a precedent for
    that and was cited here as though it were: ``gemini_review_agent.py``
    imports ``gemini_review_logic`` only after a ``sys.path.insert(0, …)``
    under ``# noqa: E402`` — necessarily, because it lives in
    ``.github/scripts/`` and its sibling does not. Same-directory is the whole
    difference, and the one data point in the repo demonstrates the case that
    is not ours. What actually argues for the copy is narrower:
    importing 2,300 lines of GitHub-API tooling executes its module scope, so
    the isolation launcher's startup would depend on an unrelated tool staying
    import-clean, and this file is the one a reviewer round cannot afford to
    have fail for a reason outside itself.

    That is a weaker argument than the one it replaces, and it is why sharing
    these three helpers through a ``scripts/_cli_common.py`` is a live option
    rather than a closed question — deferred, with the venv redirect and the
    efficiency items, to the WIP-commit sweep that re-slices this module. Both
    copies are pinned by their own exit-code test, so a drift between them
    fails a suite rather than a review round. The full count, the
    ``scripts/repo_stats.py`` drift it has already produced, and the trigger
    are recorded in ``specs/open-questions.md`` § Development Workflow — here
    because it is where the duplication is, there because a deferral only a
    reader of this docstring can find is not one the next author will see.
    """

    def error(self, message: str) -> NoReturn:
        self.print_usage(sys.stderr)
        print(f"error: {message}", file=sys.stderr)
        raise SystemExit(1)


_MIN_PYTHON = (3, 12)


def _require_python(running: tuple[int, int]) -> None:
    """Refuse an interpreter too old for the stdlib calls this module makes.

    `shutil.rmtree(onexc=)` and `Path.is_junction` are both 3.12+, and
    everything else here works on 3.9 — so an older interpreter runs `setup`
    normally and only breaks on the delete paths, dying inside teardown by
    `TypeError`/`AttributeError` that no `except` clause owns: the state file
    is never unlinked, both worktrees stay registered, and every later setup
    is refused. That traceback matches none of the exit-1 shapes
    reviewer-isolation.md's teardown triage enumerates.

    pyproject's `requires-python` does not bind here. It constrains `uv`,
    while reviewer-isolation.md launches this file with a bare `python` —
    which on Windows commonly resolves to a Store shim or a system
    interpreter rather than the project venv.

    The remedy repeats the launch command's `$(git rev-parse --show-toplevel)`
    form rather than a repo-relative path, because a relative one is what the
    launch procedure spends a paragraph warning against: cwd persists across
    Bash calls, and Python's can't-open-file exit is **2**, which this
    contract reads as a sanctioned "nothing to review". A remedy that skips
    both reviewers on a dirty tree is worse than the refusal it answers.
    """
    if running < _MIN_PYTHON:
        raise AbortError(
            f"this script needs Python {'.'.join(map(str, _MIN_PYTHON))} or "
            f"newer (running {'.'.join(map(str, running))}) — teardown would "
            "fail after setup had already created the worktrees. Run it with "
            'the project interpreter: `uv run python "$(git rev-parse '
            '--show-toplevel)/scripts/review_worktree.py"`'
        )


def _use_utf8_io(streams: tuple[object, ...]) -> None:
    """Print through UTF-8 whatever the console codepage.

    The manifest is full of em dashes and CLAUDE.md's Windows-1252 hazard
    applies to the console too. `errors="replace"` because a surrogate
    smuggled in via argv must not turn a successful setup into an apparent
    failure at print time, after the worktrees and the state file exist.
    A deliberate copy of bot_review.py's `use_utf8_io` (stdlib-only,
    self-contained), not an oversight.

    Called from `__main__` and not from `main()`, and taking its streams as
    an argument, because the comment that used to sit here — that this was
    unreachable from an in-process pytest run, and therefore unpinnable — was
    false in both halves. `_pytest.capture.EncodedFile` and `TeeCaptureIO`
    ARE `TextIOWrapper` subclasses (measured), so every in-process `main()`
    call in the suite — which is most of `test_review_worktree.py`, directly
    or through its `_setup` helper — reconfigured pytest's own session-wide
    capture streams and left them at `errors="replace"` for every test that
    followed: a global encoding-strictness downgrade ordered by test execution
    order, landing on whichever innocent test wrote next. Moving the call out
    of `main()` makes the claim true, and passing the streams in makes the
    behavior testable, which is what the old comment said could not be done.

    **This paragraph used to name the log-canary stream as the victim, and
    that was wrong** — worth correcting rather than trimming, because it is
    the sentence a future author reads when deciding whether the `main()`
    call may come back, and it is copied into three other files.
    `TeeCaptureIO.write` writes to its own `TextIOWrapper` over a `BytesIO`
    and then forwards the **str** to `self._other`, the pre-capture
    `sys.stdout` — and `_other` is the stream `2>&1 | tee pytest-output.log`
    captures. Reconfiguring `sys.stdout` mutates the capture wrapper only.
    The deletion is still correct; the canary gate was not the reason.
    """
    for stream in streams:
        if isinstance(stream, io.TextIOWrapper):
            stream.reconfigure(encoding="utf-8", errors="replace")


def main(argv: list[str]) -> int:
    parser = _Parser(
        description="Reviewer-isolation worktree launcher and teardown (ADR-0068)."
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_setup = sub.add_parser("setup", help="snapshot and materialize worktrees")
    p_setup.add_argument("--scratch", required=True, type=Path)
    p_setup.add_argument("--agents", default=",".join(DEFAULT_AGENTS))
    p_setup.add_argument("--base", default=DEFAULT_BASE)

    p_teardown = sub.add_parser("teardown", help="remove recorded worktrees")
    p_teardown.add_argument("--scratch", required=True, type=Path)

    args = parser.parse_args(argv)
    try:
        _require_python(sys.version_info[:2])
        if args.command == "setup":
            return setup(
                args.scratch,
                parse_agents(str(args.agents)),
                parse_base(str(args.base)),
            )
        return teardown(args.scratch)
    except (AbortError, OSError) as exc:
        # OSError too: a copy racing an editor's delete, a missing git
        # binary, a locked file — all deserve the one-line error contract,
        # not a traceback.
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    _use_utf8_io((sys.stdout, sys.stderr))
    sys.exit(main(sys.argv[1:]))
