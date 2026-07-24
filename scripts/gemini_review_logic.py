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


def review_body(inline: int, unanchored: list[Finding], note: str = "") -> str:
    """The review body, always carrying the ``posted N inline finding(s)``
    marker that bot_review.py's gemini count regex cross-checks."""
    lines = [
        "## Antigravity Gemini review",
        "",
        "Antigravity (Gemini 3 Pro class, `.gemini/styleguide.md` lenses) "
        f"reviewed this PR's filtered diff and posted {inline} inline finding(s).",
    ]
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
