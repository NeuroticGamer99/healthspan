"""Pure logic of the Gemini review agent (scripts/gemini_review_logic.py).

The agent itself runs only in CI (its google-antigravity dependency is PEP 723
inline, never installed locally), but its correctness-critical logic is
SDK-free and tested here. The stakes justify it: a bug in anchorable_lines
produces a *wrong but valid* line number, which posts successfully — no 422,
no fallback, just a finding pinned to the wrong line — and a bug in
is_excluded opens the review (public PR comments) to a sensitive path.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from bot_review import BOTS
from gemini_review_logic import (
    EXCLUDED_GLOBS,
    Finding,
    anchorable_lines,
    diff_argv,
    exclusion_pathspecs,
    finding_comment,
    is_excluded,
    iter_strings,
    review_body,
    verify_diff_base,
)

GEMINI = BOTS["gemini"]


def _finding(file: str = "src/x.py", line: int = 3, fix: str = "") -> Finding:
    return Finding(
        file=file,
        line=line,
        severity="major",
        category="correctness",
        description="off-by-one in the loop bound",
        proposed_fix=fix,
    )


# --------------------------------------------------------------------------
# anchorable_lines: which (file, new-side line) a review comment may attach to
# --------------------------------------------------------------------------

# A modified file: one hunk starting at new line 3, with a context line (3),
# a deletion (no new-side number), an addition (4), and a trailing context
# line (5).
MODIFIED_DIFF = """\
diff --git a/src/x.py b/src/x.py
index 1111111..2222222 100644
--- a/src/x.py
+++ b/src/x.py
@@ -3,3 +3,3 @@ def f():
 context_before
-removed_line
+added_line
 context_after
"""


def test_added_and_context_lines_anchor_deleted_lines_do_not() -> None:
    anchors = anchorable_lines(MODIFIED_DIFF)
    assert anchors == {"src/x.py": {3, 4, 5}}


def test_deleted_lines_do_not_shift_the_new_side_numbering() -> None:
    # The wrong-but-valid hazard: counting the `-` line would label
    # `context_after` as 6, a *valid* anchor for some other line — GitHub
    # accepts it and the finding lands on the wrong code, silently.
    anchors = anchorable_lines(MODIFIED_DIFF)
    assert 6 not in anchors["src/x.py"]


def test_a_bare_empty_line_is_not_a_new_side_anchor() -> None:
    # git renders a blank context line as `" "`, never `""`, so an empty
    # string is not a real diff line. If a malformed producer injected one
    # mid-hunk, counting it would add a phantom anchor and shift every later
    # line by one — the silent-wrong-line hazard. It must be skipped without
    # advancing the new-side counter.
    diff = (
        "diff --git a/src/x.py b/src/x.py\n"
        "--- a/src/x.py\n"
        "+++ b/src/x.py\n"
        "@@ -1,1 +1,2 @@\n"
        " context\n"
        "\n"  # bare empty line — not emitted by git, must not anchor
        "+added\n"
    )
    # context=1, added=2; the empty line neither anchors nor shifts `added`.
    assert anchorable_lines(diff) == {"src/x.py": {1, 2}}


def test_a_second_hunk_restarts_numbering_from_its_own_header() -> None:
    diff = MODIFIED_DIFF + "@@ -40,2 +40,3 @@ def g():\n context\n+new_line\n"
    anchors = anchorable_lines(diff)
    assert {40, 41} <= anchors["src/x.py"]
    assert 42 not in anchors["src/x.py"]


def test_a_new_file_anchors_every_added_line() -> None:
    diff = (
        "diff --git a/docs/new.md b/docs/new.md\n"
        "new file mode 100644\n"
        "--- /dev/null\n"
        "+++ b/docs/new.md\n"
        "@@ -0,0 +1,2 @@\n"
        "+first\n"
        "+second\n"
    )
    assert anchorable_lines(diff) == {"docs/new.md": {1, 2}}


def test_a_deleted_file_has_no_new_side_to_anchor_to() -> None:
    diff = (
        "diff --git a/gone.py b/gone.py\n"
        "deleted file mode 100644\n"
        "--- a/gone.py\n"
        "+++ /dev/null\n"
        "@@ -1,2 +0,0 @@\n"
        "-first\n"
        "-second\n"
    )
    assert anchorable_lines(diff) == {}


def test_files_do_not_bleed_anchors_into_each_other() -> None:
    diff = MODIFIED_DIFF + (
        "diff --git a/src/y.py b/src/y.py\n"
        "--- a/src/y.py\n"
        "+++ b/src/y.py\n"
        "@@ -10,1 +10,2 @@\n"
        " ctx\n"
        "+added\n"
    )
    anchors = anchorable_lines(diff)
    assert anchors["src/x.py"] == {3, 4, 5}
    assert anchors["src/y.py"] == {10, 11}


def test_an_added_line_that_looks_like_a_file_header_does_not_repoint() -> None:
    # An added content line whose text begins `++ b/...` renders in the raw
    # diff as `+++ b/...`. A prefix-only parse re-points at a phantom file
    # mid-hunk and corrupts every later anchor; header lines are only honored
    # inside a `diff --git` header block.
    diff = (
        "diff --git a/src/x.py b/src/x.py\n"
        "--- a/src/x.py\n"
        "+++ b/src/x.py\n"
        "@@ -1,1 +1,2 @@\n"
        " context\n"
        "+++ b/phantom.py\n"
    )
    anchors = anchorable_lines(diff)
    assert "phantom.py" not in anchors
    # The `+++ b/phantom.py` line IS an added line of src/x.py (its content is
    # `++ b/phantom.py`), so it anchors as line 2 of the real file.
    assert anchors == {"src/x.py": {1, 2}}


def test_a_quotepath_header_is_recognized_not_misco_counted() -> None:
    # core.quotePath=true renders a non-ASCII path as `+++ "b/..."`. Missing
    # the header would count it as an anchor line of the *previous* file and
    # attribute the new file's lines to it.
    diff = MODIFIED_DIFF + (
        'diff --git "a/caf\\303\\251.md" "b/caf\\303\\251.md"\n'
        '--- "a/caf\\303\\251.md"\n'
        '+++ "b/caf\\303\\251.md"\n'
        "@@ -1,1 +1,2 @@\n"
        " ctx\n"
        "+added\n"
    )
    anchors = anchorable_lines(diff)
    assert anchors["src/x.py"] == {3, 4, 5}  # untouched by the second file
    assert anchors["caf\\303\\251.md"] == {1, 2}


# --------------------------------------------------------------------------
# is_excluded: the .coderabbit.yaml path_filters mirror
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "specs/personal/manual-entry-notes.md",
        "health.db",
        "nested/deep/health.db",
        "backups/owner-recovery-kit.txt",
        "some/dir/exports/2026/dump.csv",
        "data/raw.json",
        "app/data/raw.json",
        "canary-logs/gw0.log",
        "pytest-output.log",
        r"specs\personal\notes.md",  # Windows separators normalize first
        "./specs/personal/notes.md",
        ".venv/lib/site.py",  # leading-dot dir survives the ./ prefix strip
    ],
)
def test_sensitive_paths_are_excluded_at_any_depth(path: str) -> None:
    assert is_excluded(path) is True


@pytest.mark.parametrize(
    "path",
    [
        "specs/api-reference.md",
        "src/healthspan/data_model.py",  # `data` as a name fragment, not a dir
        "tests/test_bot_review.py",
        "scripts/gemini_review_logic.py",
        ".gemini/styleguide.md",
    ],
)
def test_ordinary_repo_paths_are_not_excluded(path: str) -> None:
    assert is_excluded(path) is False


def test_iter_strings_reaches_nested_shapes_and_keys() -> None:
    # The hook denies on any *reachable* string: a tool taking {"paths": [...]}
    # or a nested request object must not bypass a top-level-only check —
    # that fails open, the opposite of the hook's contract.
    args = {
        "paths": ["a.py", "specs/personal/notes.md"],
        "request": {"inner": ("b.py",), "specs/personal/key.md": 1},
        "count": 3,
    }
    found = set(iter_strings(args))
    assert "specs/personal/notes.md" in found
    assert "specs/personal/key.md" in found  # dict keys are paths too
    assert {"a.py", "b.py", "paths", "request", "inner", "count"} <= found


# --------------------------------------------------------------------------
# exclusion_pathspecs: the git-side translation, tested against real git —
# git glob `*` does not cross `/` (unlike fnmatch), the containment gap that
# let nested sensitive paths reach the Gemini prompt
# --------------------------------------------------------------------------


def _git(cwd: Path, *args: str) -> str:
    proc = subprocess.run(  # noqa: S603 - fixed executable, test-owned repo
        ["git", *args],  # noqa: S607 - PATH-resolved git, as the agent runs it
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )
    return proc.stdout


def _init_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "r"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.invalid")
    _git(repo, "config", "user.name", "t")
    return repo


def test_pathspecs_exclude_nested_sensitive_paths_in_a_real_repo(
    tmp_path: Path,
) -> None:
    repo = _init_repo(tmp_path)
    (repo / "base.txt").write_text("base\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "base")
    # The nested shapes the single-star pathspecs leaked (verified live):
    # a file one level under an excluded dir, one nested deeper, a nested
    # db-suffixed file — plus a file that must survive the filter.
    for rel in (
        "exports/2026/dump.csv",
        "specs/personal/sub/notes.md",
        "nested/deep/health.db",
        "src/keep_me.py",
    ):
        target = repo / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("x\n", encoding="utf-8")
    _git(repo, "add", "-A", "-f", ".")
    out = _git(
        repo, "diff", "--cached", "--name-only", "--", ".", *exclusion_pathspecs()
    )
    assert out.split() == ["src/keep_me.py"]


# --------------------------------------------------------------------------
# diff_argv: the reviewed range names the PR head explicitly. The workflow
# keeps the worktree on `main` so only trusted code runs beside the API key and
# the write token (ADR-0064) — which makes HEAD `main`, and a review that
# diffed it would post a clean review of nothing.
# --------------------------------------------------------------------------

HEAD_SHA = "1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8f9a0b"


def test_the_reviewed_range_targets_the_given_head_not_the_worktree() -> None:
    argv = diff_argv(HEAD_SHA)
    assert argv[:3] == ["git", "diff", f"origin/main...{HEAD_SHA}"]
    # The regression this guards: any re-introduction of the worktree-relative
    # assumption. With the worktree on `main`, HEAD names the wrong commit.
    assert "HEAD" not in " ".join(argv)


def test_the_range_still_carries_the_sensitive_path_exclusions() -> None:
    argv = diff_argv(HEAD_SHA)
    assert argv[3:5] == ["--", "."]
    assert argv[5:] == exclusion_pathspecs()


@pytest.mark.parametrize(
    "value",
    [
        "HEAD",  # the pre-hardening assumption, now refused outright
        "origin/main",
        "refs/pull/57/head",
        "feature-branch",
        "--output=/tmp/leak",  # git would read a leading dash as an option
        "1a2b3c4",  # abbreviated: real, but not what the workflow passes
        "1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8f9a0",  # 39
        "1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8f9a0bc",  # 41
        "1A2B3C4D5E6F7A8B9C0D1E2F3A4B5C6D7E8F9A0B",  # uppercase
        f"{HEAD_SHA} --all",
        "",
    ],
)
def test_anything_that_is_not_a_bare_commit_sha_is_refused(value: str) -> None:
    # Fail closed: a ref name silently reviews the wrong commits and a dashed
    # value is read by git as an option — both would be reported as a normal
    # review, which is exactly the failure mode this check exists to prevent.
    with pytest.raises(ValueError, match="commit SHA"):
        diff_argv(value)


def test_a_sha256_object_name_is_accepted() -> None:
    # Not speculative plumbing: `git init --object-format=sha256` exists today,
    # and a 40-hex-only check would reject every SHA the workflow could pass on
    # such a repository — a total, mysterious failure of the reviewer.
    assert diff_argv("f" * 64)[2].endswith("f" * 64)


def test_a_default_branch_matching_the_diff_base_is_accepted() -> None:
    verify_diff_base("main")  # the repository as it stands; must not raise


@pytest.mark.parametrize(
    "default_branch", ["trunk", "master", "Main", "", "origin/main"]
)
def test_a_default_branch_that_is_not_the_diff_base_fails_closed(
    default_branch: str,
) -> None:
    # The workflow checks out github.event.repository.default_branch while the
    # range is built from the literal DIFF_BASE. If the default branch is
    # redesignated and a stale `main` survives, the job would execute the right
    # code and diff the wrong base — a review of unrelated changes, posted as
    # an ordinary one. Nothing else in the job can see that.
    with pytest.raises(ValueError, match="diverged") as excinfo:
        verify_diff_base(default_branch)
    # Self-describing: a CI failure has to say what diverged and what to edit,
    # or it reads as an unexplained crash in the reviewer.
    message = str(excinfo.value)
    assert repr(default_branch) in message
    assert "DIFF_BASE" in message


def test_the_pr_head_diffs_while_the_worktree_stays_on_main(tmp_path: Path) -> None:
    """The whole point of the hardening, proven end to end against real git.

    The worktree holds `main` (nothing of the PR is checked out, so nothing of
    it can be executed) and the PR head exists only as a fetched object — and
    the diff is still the PR's.
    """
    repo = _init_repo(tmp_path)
    (repo / "base.txt").write_text("base\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "base")
    base_sha = _git(repo, "rev-parse", "HEAD").strip()
    # Stand in for the remote-tracking ref the workflow's checkout provides.
    _git(repo, "update-ref", "refs/remotes/origin/main", base_sha)

    (repo / "pr_only.py").write_text("print('from the PR')\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "the PR's commit")
    head_sha = _git(repo, "rev-parse", "HEAD").strip()

    # The hardened shape: worktree back on main's content, PR head fetched-only.
    _git(repo, "checkout", "-q", "--detach", base_sha)
    assert not (repo / "pr_only.py").exists()

    diff = _git(repo, *diff_argv(head_sha)[1:])
    assert "pr_only.py" in diff
    assert "from the PR" in diff
    # And the bug the range replaces: HEAD is main, so its diff is empty — a
    # 0-finding review of a PR that was never looked at.
    assert _git(repo, "diff", "origin/main...HEAD") == ""


# --------------------------------------------------------------------------
# The workflow's trust boundary, asserted rather than commented (ADR-0064).
# The review job holds GEMINI_API_KEY and a pull-requests:write token, so any
# edit that puts another commit's files in its worktree — `gh pr checkout`, a
# `ref:` naming the PR — is arbitrary code execution with both, and it passes
# every other gate green. Same mechanization as the EXCLUDED_GLOBS mirror
# below, for the same reason: a containment boundary has to fail the build
# rather than depend on a reviewer noticing one line.
#
# Text-matched, not YAML-parsed: the project declares no YAML parser (the
# .coderabbit.yaml check below hand-parses for the same reason).
# --------------------------------------------------------------------------

WORKFLOW = (
    Path(__file__).resolve().parent.parent
    / ".github"
    / "workflows"
    / "gemini-review.yml"
)

# Every construct that can swap the worktree to another commit. The workflow's
# own header names `gh pr checkout` in prose, which is why the scan reads only
# non-comment text.
WORKTREE_SWAPS = (
    "gh pr checkout",
    "git checkout",
    "git switch",
    "git worktree",
    "git reset",
)


def _workflow_code() -> str:
    """The workflow source with every comment stripped.

    Cuts at the first `#` on each line, which is a comment in both YAML and in
    the shell of a `run:` block — the two languages this file mixes.
    """
    return "\n".join(line.split("#")[0] for line in _workflow_source().splitlines())


def _workflow_source() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


def _workflow_steps(source: str) -> list[str]:
    """The job's step blocks, split on the `      - ` step markers."""
    blocks: list[list[str]] = []
    for line in source.splitlines():
        if line.startswith("      - "):
            blocks.append([line])
        elif blocks:
            blocks[-1].append(line)
    return ["\n".join(block) for block in blocks]


@pytest.mark.parametrize("swap", WORKTREE_SWAPS)
def test_the_reviewer_workflow_never_swaps_its_worktree(swap: str) -> None:
    assert swap not in _workflow_code(), (
        f"{swap!r} in gemini-review.yml puts non-default-branch files in the "
        "worktree the agent script, its imports, and its PEP 723 dependencies "
        "resolve from — beside GEMINI_API_KEY and a write token (ADR-0064)"
    )


def test_the_checkout_step_pins_its_ref_to_the_default_branch() -> None:
    # Unpinned, the checkout follows the dispatched ref — and workflow_dispatch
    # runs the workflow file from that ref, not from the default branch. The
    # pin is what makes "only trusted code runs here" true of the job itself
    # rather than of whoever dispatched it.
    steps = _workflow_steps(_workflow_source())
    checkouts = [s for s in steps if "actions/checkout@" in s]
    assert len(checkouts) == 1
    assert "ref: ${{ github.event.repository.default_branch }}" in checkouts[0]


def test_the_review_step_is_handed_the_head_sha_and_the_default_branch() -> None:
    # Both are env-only inputs the agent requires. A missing one is a KeyError
    # on a live run — and this reviewer gets its first live run only after the
    # change merges, so the cheap place to catch it is here.
    review = [s for s in _workflow_steps(_workflow_source()) if "_agent.py" in s]
    assert len(review) == 1
    assert "PR_HEAD_SHA: ${{ steps.pr-head.outputs.sha }}" in review[0]
    assert "DEFAULT_BRANCH: ${{ github.event.repository.default_branch }}" in review[0]


def test_the_pull_request_ref_is_only_ever_fetched() -> None:
    # The PR reaches this job as git objects and a SHA. Every other verb —
    # checkout, merge, worktree — would make it code.
    uses = [
        line.strip()
        for line in _workflow_code().splitlines()
        if "refs/pull/" in line or "pull/$PR" in line
    ]
    assert uses, "the workflow no longer fetches the PR head — did the ref move?"
    assert all(line.startswith("git fetch ") for line in uses), uses


# --------------------------------------------------------------------------
# EXCLUDED_GLOBS mirrors .coderabbit.yaml path_filters — mechanized, not
# comment-enforced: this list is a containment boundary (it gates what the
# agent may read and post publicly), so drift must show up red
# --------------------------------------------------------------------------


def test_excluded_globs_match_coderabbit_path_filters() -> None:
    config = (Path(__file__).resolve().parent.parent / ".coderabbit.yaml").read_text(
        encoding="utf-8"
    )
    entries: list[str] = []
    in_filters = False
    for raw in config.splitlines():
        line = raw.strip()
        if line == "path_filters:":
            in_filters = True
            continue
        if in_filters:
            if not line.startswith("- "):
                break  # the block ends at the first non-list line
            entries.append(line[2:].strip().strip('"'))
    assert entries, "path_filters block not found in .coderabbit.yaml"
    # Normalize CodeRabbit's `!**/X/**` forms to the fnmatch forms used here:
    # strip the negation and the any-depth prefix; `/**` suffix -> `/*`
    # (fnmatch `*` crosses `/`, so `X/*` already covers the whole subtree).
    normalized = {
        e.lstrip("!").removeprefix("**/").removesuffix("/**") + "/*"
        if e.endswith("/**")
        else e.lstrip("!").removeprefix("**/")
        for e in entries
    }
    assert normalized == set(EXCLUDED_GLOBS)


# --------------------------------------------------------------------------
# review_body: the count marker is a cross-module contract with bot_review
# --------------------------------------------------------------------------


def test_the_body_marker_matches_the_gemini_botspec_count_regex() -> None:
    # bot_review.py's gemini spec cross-checks the body's stated count against
    # the fetched inline comments; if the two modules ever disagree on the
    # marker wording, every review reports "states no finding count".
    body = review_body(2, [])
    found = GEMINI.count.search(body)
    assert found is not None
    assert int(found.group(1)) == 2


def test_a_clean_run_states_zero_and_says_clean() -> None:
    body = review_body(0, [])
    found = GEMINI.count.search(body)
    assert found is not None
    assert int(found.group(1)) == 0
    assert "No findings" in body


def test_a_caller_note_replaces_the_default_clean_line_not_doubles_it() -> None:
    # The empty-diff path supplies its own clean message; appending the default
    # "No findings" line too would state the clean case twice and contradict it
    # (nothing reviewed vs. reviewed-and-clean).
    body = review_body(0, [], note="The filtered diff is empty — nothing to review.")
    assert "nothing to review" in body
    assert "No findings — clean per the styleguide lenses." not in body
    found = GEMINI.count.search(body)  # marker still present for the cross-check
    assert found is not None
    assert int(found.group(1)) == 0


def test_unanchored_findings_are_listed_in_the_body_with_their_location() -> None:
    body = review_body(1, [_finding(file="src/y.py", line=9)])
    found = GEMINI.count.search(body)
    assert found is not None
    assert int(found.group(1)) == 1  # unanchored findings never inflate it
    assert "1 finding(s) could not be anchored" in body
    assert "`src/y.py:9`" in body
    assert "No findings" not in body


def test_finding_comment_includes_the_fix_only_when_one_exists() -> None:
    bare = finding_comment(_finding())
    assert "off-by-one in the loop bound" in bare
    assert "Proposed fix" not in bare
    fixed = finding_comment(_finding(fix="use range(n - 1)"))
    assert "Proposed fix: use range(n - 1)" in fixed
