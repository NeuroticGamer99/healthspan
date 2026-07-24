# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "google-antigravity==0.1.7",
#     "pydantic>=2",
# ]
# ///
# These pins are OUTSIDE uv.lock and Dependabot's reach (its pip ecosystem
# does not parse PEP 723 inline blocks): they are bumped BY HAND, like the
# gitleaks binary pin (ADR-0045 §7) and this workflow's UV_VERSION
# (testing-strategy.md "Workflow provenance").
"""Review a PR's diff with the Antigravity SDK (Gemini) and post a PR review.

Runs only inside .github/workflows/gemini-review.yml — it expects a checkout of
``main`` (never of the PR: see Trust boundary below) with the PR head fetched as
data and named by ``PR_HEAD_SHA``, plus ``GEMINI_API_KEY`` (free AI Studio key),
``GH_TOKEN``, ``PR`` and ``GITHUB_REPOSITORY``. It is deliberately not part of the
installable project: its dependencies are PEP 723 inline (CI-only, never in
uv.lock), and living under ``.github/`` keeps it outside the pyright gate's
scope (ruff still lints it). Its pure logic — diff parsing, sensitive-path
exclusion, review-body/count-marker composition — lives in
``scripts/gemini_review_logic.py``, which IS pyright-checked and unit-tested;
this file holds only the SDK calls and I/O that need CI to run.

Shape of the output — a real PR *review* authored by ``github-actions[bot]``,
mirroring what scripts/bot_review.py's ``gemini`` BotSpec expects:

* the body states ``posted N inline finding(s)`` (the count cross-check marker);
* each finding that anchors to a diff line becomes an inline review comment;
* findings that do not anchor are listed in the body instead, explicitly;
* a clean run is still a review, stating 0 findings (like Copilot, unlike
  CodeRabbit — so no clean-comment scanning is needed).

Trust boundary (ADR-0064): this job holds ``GEMINI_API_KEY`` and a
pull-requests:write token, so it runs only default-branch code — this script,
the module it imports, and the PEP 723 block above are all the ``main`` copies.
The PR is reviewed as data: its head is fetched, never checked out, and reaches
git only as ``PR_HEAD_SHA``. Two consequences are deliberate:

* the reviewed range is ``origin/main...$PR_HEAD_SHA`` rather than ``HEAD``,
  which here is ``main``.
* ``view_file`` reads ``main``'s worktree, i.e. the *pre-change* version of
  every file the PR touches. The system instructions say so and forbid findings
  that rest on a changed file's on-disk content; the diff is the sole authority
  on what the PR does. Materializing PR blobs into a read-only path was the
  alternative and was rejected in ADR-0064.

Residual, accepted there and not closed here: diff content can prompt-inject the
model into an *unfaithful review*. It cannot reach the token or execute code
once the surrounding Python is trusted, so the blast radius is a misleading
review a human triages — the standing rule that every bot finding is verified
against the code before it is acted on.

Containment: the diff is pre-computed here with the same sensitive-path
exclusions as .coderabbit.yaml's ``path_filters``, and a pre-tool hook refuses
any agent tool call whose arguments name an excluded path. The excluded files
are all gitignored, so they normally never reach the checkout — this is the
same defense-in-depth stance as the CodeRabbit config, not the primary guard.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

from google.antigravity import Agent, LocalAgentConfig, types
from google.antigravity.hooks import hooks, policy

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from gemini_review_logic import (  # noqa: E402
    DIFF_BASE,
    ReviewResult,
    anchorable_lines,
    diff_argv,
    finding_comment,
    is_excluded,
    iter_strings,
    review_body,
    verify_diff_base,
)

STYLEGUIDE = REPO_ROOT / ".gemini" / "styleguide.md"

SYSTEM_INSTRUCTIONS = """\
You are a code reviewer for the healthspan repository. Review ONLY the diff you
are given, against the styleguide included in the prompt. Report genuine
correctness, security, design, and spec-conformance findings — not style nits
already gated by CI (ruff, pyright strict, PyMarkdown), and not restatements of
the diff. Every finding must cite the file path and the NEW-side line number of
a line that appears in the diff. view_file reads the repository's DEFAULT BRANCH
(main), NOT this pull request: for any file the diff touches, disk holds the
version BEFORE these changes, so use it as before-and-after context only, and
never report a finding that depends on a changed file's on-disk content — the
diff is the sole authority on what this pull request does. If you encounter
personal health values or identifying information, report only the path and data
category — never quote or echo the values themselves; your findings are posted
publicly. Finish by returning the structured result.
"""


# Mirrors bot_review.COMMAND_TIMEOUT and exists for the same reason: a hung
# gh/git call would otherwise block to the job's timeout-minutes while later
# asks for this PR queue behind it (concurrency: cancel-in-progress false).
COMMAND_TIMEOUT = 120


def run_cmd(argv: list[str], stdin: str | None = None) -> str:
    """One subprocess call, UTF-8 in and out, bounded, loud on failure.

    encoding="utf-8" is load-bearing on any runner whose locale codec is not
    UTF-8 (the CLAUDE.md Windows-1252 hazard); harmless on ubuntu-latest.

    Deliberately a near-twin of ``bot_review.run_cmd`` rather than an import of
    it: this CI-only script stays independent of the installable project — it
    takes ``stdin`` where that one takes ``env``, and raises plain
    ``RuntimeError`` rather than ``BotReviewError`` (the 422-fallback in
    ``main`` keys on the message text, not the class). Keep the two subprocess
    hardenings — utf-8, ``COMMAND_TIMEOUT``, non-zero-exit raise — in sync by
    hand.
    """
    try:
        proc = subprocess.run(  # noqa: S603 - fixed executables, no shell
            argv,
            input=stdin,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
            timeout=COMMAND_TIMEOUT,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            f"{' '.join(argv[:3])} did not return within {COMMAND_TIMEOUT}s"
        ) from exc
    if proc.returncode != 0:
        detail = (proc.stderr or "").strip()
        raise RuntimeError(f"{' '.join(argv[:3])} failed: {detail}")
    return proc.stdout or ""


@hooks.pre_tool_call_decide
async def deny_sensitive_paths(data: types.ToolCall) -> types.HookResult:
    """Refuse any tool call whose arguments name an excluded path.

    Walks every string reachable in the args — nested lists, dicts, keys —
    rather than one known top-level parameter, so a reshaped tool schema in a
    future SDK version fails closed here instead of open.
    """
    for value in iter_strings(data.args or {}):
        if is_excluded(value):
            return types.HookResult(
                allow=False,
                message=f"path {value!r} is excluded from review (sensitive)",
            )
    return types.HookResult(allow=True)


async def review_diff(diff: str, styleguide: str) -> ReviewResult:
    config = LocalAgentConfig(
        system_instructions=SYSTEM_INSTRUCTIONS,
        response_schema=ReviewResult,
        policies=[
            policy.deny_all(),
            policy.allow("view_file"),
            policy.allow("finish"),
        ],
        hooks=[deny_sensitive_paths],
        api_key=os.environ["GEMINI_API_KEY"],
    )
    prompt = (
        "Review the following diff per the styleguide.\n\n"
        "=== STYLEGUIDE (.gemini/styleguide.md) ===\n"
        f"{styleguide}\n"
        f"=== DIFF ({DIFF_BASE}...<pull request head>, "
        "sensitive paths pre-excluded) ===\n"
        f"{diff}\n"
    )
    async with Agent(config) as agent:
        response = await agent.chat(prompt)
        data = await response.structured_output()
    return ReviewResult.model_validate(data)


def post_review(repo: str, pr: str, payload: dict[str, object]) -> None:
    run_cmd(
        ["gh", "api", f"repos/{repo}/pulls/{pr}/reviews", "--input", "-"],
        stdin=json.dumps(payload),
    )


def main() -> int:
    repo = os.environ["GITHUB_REPOSITORY"]
    pr = os.environ["PR"]
    # The workflow's fetched PR head, not `git rev-parse HEAD` — that is `main`
    # here. It is both the reviewed range's endpoint and the review's
    # commit_id, which GitHub anchors the inline comments to.
    head_sha = os.environ["PR_HEAD_SHA"].strip()
    # Before any git call: the workflow checks out whatever GitHub calls the
    # default branch, while the range below is the literal DIFF_BASE. The one
    # way those diverge silently is a redesignated default branch with a stale
    # `main` still present — right code, wrong base, ordinary-looking review.
    verify_diff_base(os.environ["DEFAULT_BRANCH"].strip())
    styleguide = STYLEGUIDE.read_text(encoding="utf-8")

    # The range, its fail-closed SHA check, and the sensitive-path exclusions
    # all live in diff_argv — the tested module, not this CI-only script. This
    # is head_sha's first use, so a malformed value fails here, before any
    # review is posted against it.
    diff = run_cmd(diff_argv(head_sha))
    if not diff.strip():
        post_review(
            repo,
            pr,
            {
                "commit_id": head_sha,
                "event": "COMMENT",
                "body": review_body(
                    0, [], note="The filtered diff is empty — nothing to review."
                ),
            },
        )
        print("empty filtered diff; posted a 0-finding review")
        return 0

    result = asyncio.run(review_diff(diff, styleguide))
    anchors = anchorable_lines(diff)
    anchored = [f for f in result.findings if f.line in anchors.get(f.file, set())]
    unanchored = [
        f for f in result.findings if f.line not in anchors.get(f.file, set())
    ]

    payload: dict[str, object] = {
        "commit_id": head_sha,
        "event": "COMMENT",
        "body": review_body(len(anchored), unanchored),
        "comments": [
            {
                "path": f.file,
                "line": f.line,
                "side": "RIGHT",
                "body": finding_comment(f),
            }
            for f in anchored
        ],
    }
    try:
        post_review(repo, pr, payload)
        posted_inline, posted_unanchored = len(anchored), len(unanchored)
    except RuntimeError as exc:
        # Only a 422 means an anchor fell outside the diff's hunks — the one
        # failure the body-only fallback recovers from, re-posting every
        # finding in the body with the inline count honestly restated to 0.
        # `gh api` stamps "(HTTP 422)" into stderr for that; match on the full
        # token, not bare "422", so PR #422's path in the message can't spoof
        # it. Any other gh failure (auth, 5xx, rate-limit) is systemic — let
        # it raise, rather than mask a transient error as a clean 0-inline
        # review that discarded the inline comments.
        if "HTTP 422" not in str(exc):
            raise
        print(f"inline anchor rejected ({exc}); falling back to body-only")
        post_review(
            repo,
            pr,
            {
                "commit_id": head_sha,
                "event": "COMMENT",
                "body": review_body(0, result.findings),
            },
        )
        posted_inline, posted_unanchored = 0, len(result.findings)
    print(
        f"posted review: {posted_inline} inline, {posted_unanchored} unanchored, "
        f"{len(result.findings)} total finding(s)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
