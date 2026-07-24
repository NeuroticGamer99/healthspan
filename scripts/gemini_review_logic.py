"""Pure logic for the Antigravity (Gemini) review agent — SDK-free, tested.

The agent itself (``.github/scripts/gemini_review_agent.py``) runs only in CI,
where its ``google-antigravity`` dependency exists; everything here has no SDK
or I/O dependency and is deliberately extracted so the correctness-critical
pieces get the same unit-test discipline as ``bot_review.py``
(``tests/test_gemini_review_logic.py``). The stakes are asymmetric:
:func:`anchorable_lines` mis-parsing a hunk produces a *wrong but valid* line
number, which posts successfully — no 422, no fallback, just a finding pinned
to the wrong line.

The one cross-module contract: :func:`review_body` writes the
``posted N inline finding(s)`` marker that ``bot_review.py``'s ``gemini``
BotSpec ``count`` regex reads back. The tests assert the two agree.
"""

from __future__ import annotations

import fnmatch
import re
from collections.abc import Iterable, Iterator
from typing import cast

import pydantic

# Mirrors .coderabbit.yaml `path_filters` (the sibling reviewer config): keep
# generated / data / secret-bearing artifacts out of the reviewed diff and out
# of the agent's reach. fnmatch-style, matched against repo-relative paths.
EXCLUDED_GLOBS = [
    "*.db",
    "*.db-shm",
    "*.db-wal",
    "*.db.lock",
    "*.sqlite*",
    "*recovery-kit*",
    "exports/*",
    "export/*",
    "data/*",
    "__pycache__/*",
    ".venv/*",
    "pytest-output.log",
    "parallel-output.log",
    "canary-logs/*",
    "specs/personal/*",
]


def iter_strings(value: object) -> Iterator[str]:
    """Every string reachable inside a tool-call argument value.

    The sensitive-path hook denies on any *nested* string matching an
    exclusion — a tool taking ``{"paths": [...]}`` or a request object must
    not bypass a check that only looked at top-level strings (that would fail
    open, the opposite of the hook's contract). Dict keys are walked too: a
    path used as a mapping key is still a path.
    """
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, item in cast("dict[object, object]", value).items():
            yield from iter_strings(key)
            yield from iter_strings(item)
    elif isinstance(value, (list, tuple, set, frozenset)):
        for item in cast("Iterable[object]", value):
            yield from iter_strings(item)


def exclusion_pathspecs() -> list[str]:
    """EXCLUDED_GLOBS as git `:(exclude,glob)` pathspecs, any-depth.

    Two pathspecs per glob, because git glob and fnmatch disagree on `*`:
    fnmatch's `*` crosses `/`, git's does not — so `**/exports/*` excludes
    only exports' immediate children and a nested `exports/2026/dump.csv`
    still reaches the diff (verified empirically; the containment gap of
    PR review 2026-07-23). The `**/{glob}/**` twin re-adds the subtree for
    the directory-shaped globs; for file-shaped ones it matches nothing and
    is harmless.
    """
    specs: list[str] = []
    for glob in EXCLUDED_GLOBS:
        specs.append(f":(exclude,glob)**/{glob}")
        specs.append(f":(exclude,glob)**/{glob}/**")
    return specs


# A commit SHA and nothing else: 40 hex today, 64 if the repository ever moves
# to SHA-256 object names. Anchored and grouped so it means the same thing
# under every match method — bare `A|B` would let a future `.match`/`.search`
# accept "<40 hex> --output=/tmp/leak", readmitting the dashed-argument
# smuggling diff_argv rejects.
COMMIT_SHA = re.compile(r"\A(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")

DIFF_BASE = "origin/main"


def verify_diff_base(default_branch: str) -> None:
    """Fail closed when :data:`DIFF_BASE` no longer names the repository's trunk.

    The workflow checks out ``github.event.repository.default_branch`` — it
    executes whatever GitHub currently calls the default branch — while the
    reviewed range is built from the literal ``DIFF_BASE``, matching
    ``ci.yml``'s ``branches: [main]`` and the `.claude/skills` that all say
    ``origin/main``. The two agree today, and a rename is a repo-wide edit
    event that breaks those loudly.

    They can diverge *silently* in exactly one shape: the default branch is
    redesignated while a stale ``main`` still exists. The job then executes the
    right code and diffs against the wrong base — a review full of unrelated
    changes, posted as an ordinary one. (A default branch renamed with no
    ``main`` left behind is already loud: ``git diff origin/main...`` exits
    non-zero.) One comparison closes the silent case, and the failure names its
    own fix.
    """
    expected = DIFF_BASE.removeprefix("origin/")
    if default_branch != expected:
        raise ValueError(
            f"the repository's default branch is {default_branch!r}, but the "
            f"review diffs against {DIFF_BASE!r} — the checked-out branch and "
            f"the diff base have diverged. Update DIFF_BASE (and the comments "
            f"quoting it in gemini-review.yml) to the current trunk."
        )


def unfiltered_diff_argv(head_sha: str) -> list[str]:
    """The git argv for one PR's UNFILTERED range — existence-only, no content.

    Used only when :func:`diff_argv`'s filtered diff comes back empty, to tell
    apart the two ways that happens: the range itself carries no changes (a
    tree-identical head — the truest reading of "already merged" or "an empty
    PR"; note that under a squash-merge workflow a merged PR's head is
    usually *not* an ancestor of main, since squashing rewrites history, so
    ``origin/main...head_sha`` still lands on the PR's original diff rather
    than an empty range — verified live against PR #60 while building this
    check) versus every changed path being swallowed by
    :data:`EXCLUDED_GLOBS`. Only whether the range is empty is ever asked, so
    this is ``--name-only``, not a full unfiltered patch: when the cause is
    "every path excluded", the excluded paths are exactly the sensitive ones
    (``specs/personal/*``, ``*.db``, ...), and materializing their full
    content in this process's memory — even transiently, even if never
    logged — is exposure this check has no reason to risk. Same ``head_sha``
    validation as :func:`diff_argv`; no exclusion pathspecs.
    """
    if not COMMIT_SHA.fullmatch(head_sha):
        raise ValueError(f"head_sha must be a commit SHA, got: {head_sha!r}")
    return ["git", "diff", "--name-only", f"{DIFF_BASE}...{head_sha}", "--", "."]


def diff_argv(head_sha: str) -> list[str]:
    """The git argv for one PR's filtered diff, ``origin/main...head_sha``.

    The head is *passed in*, never taken from ``HEAD``. The workflow keeps the
    worktree on ``main`` — only trusted code may run beside the API key and the
    write token (ADR-0064) — and fetches the PR head as data, so ``HEAD`` is
    ``main`` and diffing it would review nothing while posting a clean review.

    ``head_sha`` must be a bare commit SHA. Refusing anything else is a
    fail-closed check, not a formality: a ref name would silently review the
    wrong commits, a value starting with ``-`` would be read by git as an
    option rather than a revision, and both would be reported as a normal
    review. It is the only caller-supplied element of this argv — the base is
    fixed rather than a parameter, so there is exactly one thing to validate.
    """
    if not COMMIT_SHA.fullmatch(head_sha):
        raise ValueError(f"head_sha must be a commit SHA, got: {head_sha!r}")
    return [
        "git",
        "diff",
        f"{DIFF_BASE}...{head_sha}",
        "--",
        ".",
        *exclusion_pathspecs(),
    ]


class Finding(pydantic.BaseModel):
    """One review finding, anchored to a new-side line of the diff."""

    file: str
    line: int
    severity: str
    category: str
    description: str
    proposed_fix: str = ""


class ReviewResult(pydantic.BaseModel):
    findings: list[Finding]


def is_excluded(path: str) -> bool:
    """Whether a repo-relative path matches a sensitive-path exclusion.

    Globs are matched at any depth (the same any-depth semantics as the
    `!**/...` CodeRabbit filters): against the whole path, each basename, and
    each subpath suffix.
    """
    # removeprefix, not lstrip("./"): lstrip strips a character *set*, which
    # would also eat the leading dot of ".venv/..." and unmatch its own glob.
    normalized = path.replace("\\", "/").removeprefix("./")
    parts = normalized.split("/")
    candidates = {normalized, *parts}
    candidates.update("/".join(parts[i:]) for i in range(len(parts)))
    return any(
        fnmatch.fnmatch(candidate, glob)
        for candidate in candidates
        for glob in EXCLUDED_GLOBS
    )


def anchorable_lines(diff: str) -> dict[str, set[int]]:
    """Every (file, new-side line) an inline review comment may attach to.

    GitHub rejects (HTTP 422) a review comment on a line outside the diff's
    hunks, and one bad anchor voids the whole review POST. Added and context
    lines on the new side are valid anchors; deleted lines are not, and a
    deleted *file* (``+++ /dev/null``) has no new side at all.

    ``+++`` is honored as a file header only inside a header block (opened by
    a ``diff --git`` line, closed by the first hunk): hunk *content* can start
    with ``+++`` too — an added line whose text begins ``++ b/...`` renders as
    ``+++ b/...`` — and a prefix-only parse would re-point mid-hunk at a
    phantom file, corrupting every later anchor. git's default core.quotePath
    renders a non-ASCII path as ``+++ "b/..."``; the quotes are stripped and
    the octal escapes kept as-is — that one file's anchors may 422 into the
    body-only fallback, but neighbouring files' anchors stay correct, which is
    the silent-wrong-line hazard this parser exists to avoid.
    """
    anchors: dict[str, set[int]] = {}
    current: str | None = None
    new_line = 0
    in_file_header = False
    for raw in diff.splitlines():
        if raw.startswith("diff --git "):
            in_file_header = True
            current = None
            new_line = 0
            continue
        if in_file_header:
            if raw.startswith("+++ "):
                target = raw[4:]
                if target.startswith('"') and target.endswith('"'):
                    target = target[1:-1]
                current = target[2:] if target.startswith("b/") else None
                if current is not None:
                    anchors.setdefault(current, set())
                continue
            if not raw.startswith("@@"):
                continue  # ---, index, mode, rename lines
            in_file_header = False  # first hunk header — fall through
        header = re.match(r"@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@", raw)
        if header:
            new_line = int(header.group(1))
            continue
        if current is None or new_line == 0:
            continue
        if raw.startswith("-"):
            continue
        # Added (`+`) and context (` `) lines both advance the new side; a
        # blank context line is `" "`, never `""`, so an empty string is not a
        # real diff line — declining to count it keeps a malformed producer
        # from injecting a phantom anchor that shifts every later line by one.
        if raw.startswith(("+", " ")):
            anchors[current].add(new_line)
            new_line += 1
    return anchors


def finding_comment(finding: Finding) -> str:
    body = f"**[{finding.severity}] {finding.category}** — {finding.description}"
    if finding.proposed_fix:
        body += f"\n\nProposed fix: {finding.proposed_fix}"
    return body


# The empty-range outcome's machine-readable marker — a third state alongside
# "findings" and "clean", both already carried by the posted-N-inline count.
# Mirrors the clean_marker/count split bot_review.py already has for
# CodeRabbit, but on a *review* body rather than an issue comment, since a
# Gemini review is posted either way (issue #59): without this, an empty
# filtered diff posts `posted 0 inline finding(s)` — byte-for-byte what a
# genuinely clean run posts — and bot_review.py's count cross-check cannot
# tell "reviewed the diff, found nothing" from "there was no diff to review".
EMPTY_RANGE_MARKER = "<!-- gemini-review: empty-diff-range -->"

# The two empty-range causes worth telling apart (issue #59's three collapse
# to two: an already-merged head and a genuinely empty PR both read as "no
# changes against main", and only the exclusion case needs its own wording —
# neither a human nor bot_review.py would act on the merged/empty split
# differently).
NOTE_NO_CHANGES_VS_MAIN = (
    "The PR head introduces no changes against main (already merged, or an "
    "empty PR) — nothing to review."
)
NOTE_ALL_PATHS_EXCLUDED = (
    "Every changed path is excluded from review (sensitive-path filters) — "
    "nothing to review."
)


def review_body(
    inline: int,
    unanchored: list[Finding],
    note: str = "",
    empty_range: bool = False,
) -> str:
    """The review body, always carrying the ``posted N inline finding(s)``
    marker that bot_review.py's gemini count regex cross-checks.

    ``empty_range=True`` additionally stamps :data:`EMPTY_RANGE_MARKER` —
    bot_review.py's gemini BotSpec recognizes it as a third outcome, distinct
    from both a findings review and a genuinely clean one. Always pair it with
    a ``note`` (:data:`NOTE_NO_CHANGES_VS_MAIN` or
    :data:`NOTE_ALL_PATHS_EXCLUDED`); the marker alone does not say which
    empty-range cause applied, and this is enforced rather than merely
    documented — a marked-but-unexplained empty-range review would be exactly
    the "unexplained empty result" bot_review.py's own count_note warns
    against reporting as clean.
    """
    if empty_range and not note:
        raise ValueError("empty_range=True requires a note naming the cause")
    lines = [
        "## Antigravity Gemini review",
        "",
    ]
    if empty_range:
        lines += [EMPTY_RANGE_MARKER, ""]
    lines.append(
        "Antigravity (Gemini 3 Pro class, `.gemini/styleguide.md` lenses) "
        f"reviewed this PR's filtered diff and posted {inline} inline finding(s)."
    )
    if note:
        lines += ["", note]
    if unanchored:
        lines += [
            "",
            f"{len(unanchored)} finding(s) could not be anchored to a diff "
            "line and appear here instead:",
            "",
        ]
        lines += [f"- `{f.file}:{f.line}` — {finding_comment(f)}" for f in unanchored]
    # The default clean line, only when no caller note already speaks for the
    # empty result — the empty-diff path supplies its own ("nothing to
    # review"), and appending both restates the clean case twice and slightly
    # contradicts it (nothing reviewed vs. reviewed-and-clean).
    if inline == 0 and not unanchored and not note:
        lines += ["", "No findings — clean per the styleguide lenses."]
    return "\n".join(lines)
