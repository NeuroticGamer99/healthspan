"""Request, await, and fetch automated PR reviews (CodeRabbit, Copilot).

The glue the `/ship` and `/copilot-review` skills used to carry as shell
one-liners. It lives here because every rule it encodes is a fact about a live
API that fails *silently* when it is wrong — and prose cannot be tested, while
this can:

* **Identity.** Copilot is requested as ``copilot-pull-request-reviewer[bot]``
  but authors its inline comments as ``Copilot``. Guess wrong and GitHub
  answers HTTP 200 with no reviewer added, or hands back an empty comment list
  for a review that has findings.
* **Time.** GitHub stamps UTC (``...Z``); ``git log --format=%cI`` stamps a
  local offset. Compared as strings — which is what a ``jq`` filter does — a
  stale review sorts *newer* than the commit that superseded it, so the floor
  meant to exclude old reviews admits all of them.
* **Shape.** A bot's reply to a review comment is itself a *review*, with an
  empty body. Selecting "the newest review" picks the reply, and triage then
  treats "agreed, this is fixed" as a finding.
* **Scope.** The pull-level endpoints return every run's comments — findings
  already fixed, plus the bots' own replies. Triage must read one review by id.
* **Pagination.** The list endpoints return 30 per page and the newest review
  is on the *last* page. ``gh --paginate --jq`` applies the filter per page and
  concatenates, so ``length`` and ``sort_by | last`` silently answer once per
  page rather than once.

Commands (exit 0 on success, 1 on failure or timeout):

* ``request --bot copilot --pr N`` — request a review, then verify it took.
* ``wait --bot B --pr N --since T`` — block until a findings review lands.
* ``fetch --bot B --pr N --since T`` — print that review and its own comments.

``--since`` takes an ISO-8601 timestamp; ``--since-commit SHA`` derives the
floor from a commit in UTC, which is the safe way to recover a floor that was
not captured before the push.

Stdlib only. GitHub is reached through the ``gh`` CLI, so this inherits its
authentication and needs no token handling of its own.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast

PAGE_SIZE = 100
POLL_SECONDS = 30
DEFAULT_TIMEOUT = 1800


@dataclass(frozen=True)
class BotSpec:
    """How one review bot identifies itself and reports its finding count."""

    key: str
    # Matches the *review* author and the *comment* author. Copilot uses a
    # different login for each, so this is deliberately a loose, case-insensitive
    # match rather than an exact login: an exact login silently misses one half.
    author: re.Pattern[str]
    # The login to pass to the requested_reviewers endpoint, or None for a bot
    # that reviews on its own (CodeRabbit reviews every push).
    request_login: str | None
    # Captures the number of findings the review body claims.
    count: re.Pattern[str]


BOTS: dict[str, BotSpec] = {
    "coderabbit": BotSpec(
        key="coderabbit",
        author=re.compile("coderabbit", re.IGNORECASE),
        request_login=None,
        count=re.compile(r"Actionable comments posted:\s*\**\s*(\d+)"),
    ),
    "copilot": BotSpec(
        key="copilot",
        author=re.compile("copilot", re.IGNORECASE),
        request_login="copilot-pull-request-reviewer[bot]",
        count=re.compile(r"generated (\d+) comment"),
    ),
}

Review = dict[str, Any]
Comment = dict[str, Any]


class BotReviewError(Exception):
    """A step failed loudly enough that the caller must not proceed."""


# --------------------------------------------------------------------------
# Pure logic (no I/O) — the part that carries the silent-failure rules
# --------------------------------------------------------------------------


def parse_ts(value: str) -> datetime:
    """Parse an ISO-8601 stamp into an aware UTC datetime.

    GitHub emits ``2026-07-16T21:39:21Z``; ``git log --format=%cI`` emits
    ``2026-07-16T17:38:39-04:00``. Those are 42 seconds apart, but as *strings*
    the second sorts lower, so a ``> $SINCE`` filter keeps every stale review.
    Normalizing both to UTC makes the comparison mean what it reads like.
    """
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise BotReviewError(f"cannot parse timestamp {value!r}: {exc}") from exc
    if parsed.tzinfo is None:
        raise BotReviewError(
            f"timestamp {value!r} carries no timezone; refusing to guess one"
        )
    return parsed.astimezone(UTC)


def _login_of(review: Review) -> str:
    user = cast("dict[str, Any] | None", review.get("user"))
    return str(user.get("login", "")) if user else ""


def is_findings_review(review: Review, spec: BotSpec) -> bool:
    """Whether this is a review with findings, not the bot replying to a comment.

    GitHub models a bot's reply to a review comment as a review whose body is
    empty. Only the body distinguishes the two, so recency alone is not enough.

    **The evidence behind this rule is uneven, and the next reader should not
    assume otherwise.** It is verified for CodeRabbit, which acks every reply and
    produced three empty-bodied reply-reviews on PR #27. Copilot has never been
    observed acking at all — every Copilot review seen carried a body, and it
    stayed silent through two replies on PR #26 — so for Copilot this rule is
    inert rather than tested. Were Copilot ever to ack with a *non-empty* body,
    that ack would be misclassified as findings.

    That residual risk is accepted deliberately, because of which way it fails.
    The alternative discriminator — require the body's finding-count marker —
    is more precise, but a bot rewording its body would then make its review
    invisible and time the wait out: a silent wrong answer. This rule instead
    fails toward triaging an ack, which :func:`count_note` immediately flags
    ("states no finding count"). Loudly wrong beats quietly wrong.
    """
    if not spec.author.search(_login_of(review)):
        return False
    return bool(str(review.get("body") or "").strip())


def select_review(
    reviews: list[Review], spec: BotSpec, since: datetime
) -> Review | None:
    """The newest findings review this bot submitted after ``since``."""
    candidates = [
        review
        for review in reviews
        if is_findings_review(review, spec)
        and parse_ts(str(review.get("submitted_at", ""))) > since
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda r: parse_ts(str(r.get("submitted_at", ""))))


def stated_count(body: str, spec: BotSpec) -> int | None:
    """The finding count the review body claims, if it states one."""
    found = spec.count.search(body)
    return int(found.group(1)) if found else None


def count_note(stated: int | None, actual: int) -> str | None:
    """A warning when the body's claimed count disagrees with what was fetched.

    A mismatch is a reason to look, not a verdict. It can mean the fetch is
    wrongly scoped — or that the bot miscounted: CodeRabbit has been seen
    claiming 2 while posting 1, having counted findings before deduplicating two
    that landed on the same lines. Report the discrepancy; do not assert which
    side is wrong.
    """
    if stated is None:
        return "review body states no finding count — cross-check skipped"
    if stated == actual:
        return None
    return (
        f"count mismatch: body claims {stated}, fetched {actual}. Check the "
        "scoping before triaging — but note the bot itself may have miscounted "
        "(findings deduplicated after the count was written). Never report an "
        "unexplained empty result as a clean review."
    )


# --------------------------------------------------------------------------
# GitHub I/O (through the gh CLI)
# --------------------------------------------------------------------------


def run_cmd(argv: list[str], env: dict[str, str] | None = None) -> str:
    # encoding="utf-8" is load-bearing, not decoration: `text=True` alone decodes
    # with the *locale* codec, which is cp1252 on Windows, and both bots' bodies
    # are full of emoji (🐇 ✅ 📐). Without it this dies with a UnicodeDecodeError
    # from a reader thread — the Windows-1252 trap CLAUDE.md warns about, here
    # loud rather than silent.
    proc = subprocess.run(  # noqa: S603 - fixed executable, no shell
        argv,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
        env=env,
    )
    if proc.returncode != 0:
        # stderr can be None when a reader thread dies mid-decode; `or ""` keeps
        # the real failure visible instead of masking it behind an AttributeError.
        raise BotReviewError(
            f"{' '.join(argv[:2])} failed: {(proc.stderr or '').strip()}"
        )
    return proc.stdout or ""


def gh(path: str, *args: str) -> Any:
    """One ``gh api`` call, returning parsed JSON (``None`` for an empty body)."""
    out = run_cmd(["gh", "api", path, *args])
    if not out.strip():
        return None
    try:
        return json.loads(out)
    except json.JSONDecodeError as exc:
        raise BotReviewError(f"gh api {path} returned non-JSON: {exc}") from exc


def gh_all(path: str) -> list[Any]:
    """Every page of a list endpoint.

    Pages explicitly rather than via ``gh --paginate``: that flag combined with
    ``--jq`` applies the filter to each page separately, turning an aggregate
    into one answer per page.
    """
    items: list[Any] = []
    page = 1
    while True:
        joiner = "&" if "?" in path else "?"
        raw = gh(f"{path}{joiner}per_page={PAGE_SIZE}&page={page}")
        if not isinstance(raw, list):
            return items
        chunk = cast("list[Any]", raw)
        if not chunk:
            return items
        items.extend(chunk)
        if len(chunk) < PAGE_SIZE:
            return items
        page += 1


def default_repo() -> str:
    out = run_cmd(
        ["gh", "repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner"]
    )
    return out.strip()


def commit_floor(sha: str) -> str:
    """A commit's committer date in UTC, for use as the review floor.

    ``TZ=UTC`` plus ``--date=format-local`` is what makes this UTC; a plain
    ``%cI`` hands back a local offset, which is the bug :func:`parse_ts`
    documents.
    """
    out = run_cmd(
        [
            "git",
            "log",
            "-1",
            "--format=%cd",
            "--date=format-local:%Y-%m-%dT%H:%M:%SZ",
            sha,
        ],
        env={**os.environ, "TZ": "UTC"},
    )
    return out.strip()


def list_reviews(repo: str, pr: int) -> list[Review]:
    raw = gh_all(f"repos/{repo}/pulls/{pr}/reviews")
    return [cast("Review", r) for r in raw if isinstance(r, dict)]


def review_comments(repo: str, pr: int, review_id: int) -> list[Comment]:
    raw = gh_all(f"repos/{repo}/pulls/{pr}/reviews/{review_id}/comments")
    return [cast("Comment", c) for c in raw if isinstance(c, dict)]


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------


def cmd_request(repo: str, pr: int, spec: BotSpec) -> int:
    if spec.request_login is None:
        raise BotReviewError(
            f"{spec.key} cannot be requested — it reviews automatically on push"
        )
    gh(
        f"repos/{repo}/pulls/{pr}/requested_reviewers",
        "-f",
        f"reviewers[]={spec.request_login}",
    )
    # Never trust the 200: GitHub accepts a login it does not recognize and
    # silently adds no one, which would otherwise buy a full poll and a
    # "no review arrived" report about a review nobody ever requested.
    pull = cast("dict[str, Any] | None", gh(f"repos/{repo}/pulls/{pr}"))
    raw = cast("list[Any]", pull.get("requested_reviewers", []) if pull else [])
    logins = [
        str(cast("dict[str, Any]", r).get("login", ""))
        for r in raw
        if isinstance(r, dict)
    ]
    if not any(spec.author.search(login) for login in logins):
        raise BotReviewError(
            f"requested {spec.request_login!r} but requested_reviewers is "
            f"{logins!r} — the request was accepted and dropped. Do not wait."
        )
    print(f"requested {spec.key}; requested_reviewers now: {', '.join(logins)}")
    return 0


def cmd_wait(repo: str, pr: int, spec: BotSpec, since: datetime, timeout: int) -> int:
    deadline = time.monotonic() + timeout
    while True:
        review = select_review(list_reviews(repo, pr), spec, since)
        if review is not None:
            print(
                f"{spec.key} findings review {review.get('id')} "
                f"({review.get('submitted_at')}) is ready"
            )
            return 0
        if time.monotonic() >= deadline:
            print(
                f"TIMEOUT: no {spec.key} findings review after {timeout}s. "
                "Silence is not a clean review — check the PR before concluding.",
                file=sys.stderr,
            )
            return 1
        time.sleep(POLL_SECONDS)


def cmd_fetch(repo: str, pr: int, spec: BotSpec, since: datetime) -> int:
    review = select_review(list_reviews(repo, pr), spec, since)
    if review is None:
        print(
            f"no {spec.key} findings review after {since.isoformat()} — "
            "that is not the same as a clean review",
            file=sys.stderr,
        )
        return 1
    review_id = int(str(review.get("id")))
    body = str(review.get("body") or "")
    comments = review_comments(repo, pr, review_id)

    print(f"=== {spec.key} review {review_id} ===")
    print(f"commit:    {str(review.get('commit_id', ''))[:7]}")
    print(f"submitted: {review.get('submitted_at')}")
    note = count_note(stated_count(body, spec), len(comments))
    if note:
        print(f"NOTE: {note}")
    print()
    print(body)
    print(f"=== {len(comments)} comment(s) on this review ===")
    for comment in comments:
        line = comment.get("line") or comment.get("original_line")
        print(f"\n--- {comment.get('path')}:{line} [id={comment.get('id')}] ---")
        print(comment.get("body"))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(__doc__ or "").splitlines()[0],
    )
    parser.add_argument("command", choices=("request", "wait", "fetch"))
    parser.add_argument("--bot", required=True, choices=sorted(BOTS))
    parser.add_argument("--pr", required=True, type=int)
    parser.add_argument("--repo", default=None, help="owner/name; detected by default")
    parser.add_argument("--since", default=None, help="ISO-8601 review floor")
    parser.add_argument(
        "--since-commit", default=None, help="derive the floor from a commit, in UTC"
    )
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    return parser


def resolve_since(args: argparse.Namespace) -> datetime:
    raw = cast("str | None", args.since)
    commit = cast("str | None", args.since_commit)
    if raw and commit:
        raise BotReviewError("pass only one of --since / --since-commit")
    if commit:
        raw = commit_floor(commit)
    if not raw:
        raise BotReviewError(
            "a review floor is required (--since or --since-commit): without one, "
            "a stale review from an earlier push satisfies the wait"
        )
    return parse_ts(raw)


def use_utf8_io() -> None:
    """Print through UTF-8 regardless of the console's codepage.

    The mirror of the decode fix in :func:`_run`: Python encodes stdout with the
    locale codec, which is cp1252 here, and every review body this prints is
    full of emoji. Without this, `fetch` dies with a UnicodeEncodeError *after*
    the API work succeeded — the same Windows-1252 hazard CLAUDE.md flags for
    file I/O, reached through the console instead.
    """
    for stream in (sys.stdout, sys.stderr):
        if isinstance(stream, io.TextIOWrapper):
            stream.reconfigure(encoding="utf-8", errors="replace")


def main(argv: list[str] | None = None) -> int:
    use_utf8_io()
    args = build_parser().parse_args(argv)
    spec = BOTS[str(args.bot)]
    try:
        repo = str(args.repo) if args.repo else default_repo()
        pr = int(cast("int", args.pr))
        if args.command == "request":
            return cmd_request(repo, pr, spec)
        since = resolve_since(args)
        if args.command == "wait":
            return cmd_wait(repo, pr, spec, since, int(cast("int", args.timeout)))
        return cmd_fetch(repo, pr, spec, since)
    except BotReviewError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
