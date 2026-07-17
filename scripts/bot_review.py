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

* **Clean runs.** A fully clean CodeRabbit run posts *no review object at
  all* — the only artifact is the walkthrough issue comment stating "No
  actionable comments were generated". Watching the reviews endpoint alone
  therefore polls a clean PR to its timeout (PR #29, 2026-07-17), and the
  timeout message then sends a human to discover manually what the API had
  already said.

Commands (exit 0 = findings review ready, 1 = failure or timeout,
2 = clean review — the bot reported no findings; nothing to triage):

* ``request --bot copilot --pr N`` — request a review, then verify it took.
* ``wait --bot B --pr N --since T`` — block until a findings review or a
  clean-run summary lands.
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
# Bounds one `gh`/`git` invocation. Without it a stalled call blocks forever and
# `wait --timeout` never fires: the deadline is only checked between calls.
COMMAND_TIMEOUT = 120
# `wait`/`fetch` exit status for "the bot ran and found nothing". Distinct from
# 0 so a caller never runs a findings triage against a review that does not
# exist, and from 1 so a clean run is never reported as a failure.
EXIT_CLEAN = 2


@dataclass(frozen=True)
class BotSpec:
    """How one review bot identifies itself and reports its finding count."""

    key: str
    # The exact login that authors this bot's reviews. Compared exactly (case
    # -folded, since GitHub logins are case-preserving but unique case-folded).
    # A substring match would be laxer than the contract deserves: `copilot-fan`
    # or `not-coderabbitai[bot]` would select an unrelated review, and — worse —
    # satisfy the requested-reviewer check that exists to prove a request took.
    review_login: str
    # What to POST to requested_reviewers, and the login GitHub displays there
    # afterwards. They differ for Copilot. Both None for a bot that reviews on
    # its own (CodeRabbit reviews every push; it is not requestable this way).
    request_login: str | None
    requested_display: str | None
    # Captures the number of findings the review body claims.
    count: re.Pattern[str]
    # Recognizes the bot's clean-run summary in an *issue comment* body, for a
    # bot whose clean run posts no review object at all (CodeRabbit). None for
    # a bot whose clean run is still a review (Copilot states "generated 0
    # comments" in a review body, which select_review already finds).
    clean_marker: re.Pattern[str] | None


# There is deliberately no *comment* author here. Comments are fetched through
# the review's own id, so they need no author filter — which is why Copilot's
# `Copilot` display login is only ever matched against requested_reviewers.
BOTS: dict[str, BotSpec] = {
    "coderabbit": BotSpec(
        key="coderabbit",
        review_login="coderabbitai[bot]",
        request_login=None,
        requested_display=None,
        count=re.compile(r"Actionable comments posted:\s*\**\s*(\d+)"),
        # Both halves are required: the HTML marker proves the comment is the
        # bot's own auto-generated summary (not the phrase quoted in prose —
        # e.g. by a human, or by CodeRabbit itself echoing a reply), and the
        # phrase is what distinguishes a clean run's summary from a findings
        # run's. Transcribed from PR #29 (2026-07-17), the first fully clean
        # run observed.
        clean_marker=re.compile(
            r"<!-- This is an auto-generated comment: summarize by coderabbit\.ai -->"
            r".*No actionable comments were generated",
            re.DOTALL,
        ),
    ),
    "copilot": BotSpec(
        key="copilot",
        review_login="copilot-pull-request-reviewer[bot]",
        request_login="copilot-pull-request-reviewer[bot]",
        requested_display="Copilot",
        count=re.compile(r"generated (\d+) comment"),
        clean_marker=None,
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


def same_login(left: str, right: str) -> bool:
    """Whether two GitHub logins denote the same account.

    Case-folded rather than byte-exact — GitHub preserves the case you typed but
    treats logins as unique case-folded — and whole-string rather than substring,
    so a lookalike cannot pass for the real bot.
    """
    return left.casefold() == right.casefold()


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
    if not same_login(_login_of(review), spec.review_login):
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


def comment_ts(comment: Comment) -> datetime:
    """When an issue comment last changed, as an instant.

    ``updated_at`` rather than ``created_at``: CodeRabbit *edits* its one
    walkthrough comment in place on every push, so on any PR past its first
    review the creation time predates every floor and a fresh clean run would
    be invisible. Falls back to ``created_at`` only if ``updated_at`` is
    absent.
    """
    raw = comment.get("updated_at") or comment.get("created_at") or ""
    return parse_ts(str(raw))


def is_clean_comment(comment: Comment, spec: BotSpec) -> bool:
    """Whether this issue comment is the bot's own clean-run summary.

    Exists because a fully clean CodeRabbit run posts *no review object* — its
    only artifact is the walkthrough comment saying no actionable comments were
    generated (PR #29). Author is matched whole-login like everything else;
    the body must carry the spec's ``clean_marker``, which requires both the
    bot's auto-generated-summary HTML marker and the no-findings phrase.
    """
    if spec.clean_marker is None:
        return False
    if not same_login(_login_of(comment), spec.review_login):
        return False
    return bool(spec.clean_marker.search(str(comment.get("body") or "")))


def select_clean_comment(
    comments: list[Comment], spec: BotSpec, since: datetime
) -> Comment | None:
    """The newest clean-run summary this bot updated after ``since``.

    Callers must check :func:`select_review` *first*: on a findings run the
    walkthrough comment carries no clean marker, but an edit racing the review
    submission is cheap insurance to keep — findings, when present, win.
    """
    candidates = [
        comment
        for comment in comments
        if is_clean_comment(comment, spec) and comment_ts(comment) > since
    ]
    if not candidates:
        return None
    return max(candidates, key=comment_ts)


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


def as_page(raw: Any, path: str) -> list[Any]:
    """A list endpoint's page, or a loud failure.

    An unexpected shape — GitHub's error object is a *dict* — must not degrade
    into "no results". Swallowing it makes ``wait`` poll a phantom until timeout
    and ``fetch`` print zero comments as though that were an answer: exactly the
    silent-failure mode this module exists to remove. An empty *list* is a real
    answer and passes through.
    """
    if not isinstance(raw, list):
        raise BotReviewError(
            f"gh api {path} returned {type(raw).__name__}, expected a list — "
            "refusing to report an unexpected payload as an empty result"
        )
    return cast("list[Any]", raw)


def run_cmd(argv: list[str], env: dict[str, str] | None = None) -> str:
    # encoding="utf-8" is load-bearing, not decoration: `text=True` alone decodes
    # with the *locale* codec, which is cp1252 on Windows, and both bots' bodies
    # are full of emoji (🐇 ✅ 📐). Without it this dies with a UnicodeDecodeError
    # from a reader thread — the Windows-1252 trap CLAUDE.md warns about, here
    # loud rather than silent.
    try:
        proc = subprocess.run(  # noqa: S603 - fixed executable, no shell
            argv,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
            env=env,
            timeout=COMMAND_TIMEOUT,
        )
    except subprocess.TimeoutExpired as exc:
        raise BotReviewError(
            f"{' '.join(argv[:2])} did not return within {COMMAND_TIMEOUT}s"
        ) from exc
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
        chunk = as_page(gh(f"{path}{joiner}per_page={PAGE_SIZE}&page={page}"), path)
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


def issue_comments(repo: str, pr: int) -> list[Comment]:
    """The PR's issue comments — where CodeRabbit's clean-run summary lives."""
    raw = gh_all(f"repos/{repo}/issues/{pr}/comments")
    return [cast("Comment", c) for c in raw if isinstance(c, dict)]


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------


def cmd_request(repo: str, pr: int, spec: BotSpec) -> int:
    if spec.request_login is None or spec.requested_display is None:
        raise BotReviewError(
            f"{spec.key} cannot be requested — it reviews automatically on push"
        )
    # Stamp the floor *before* the request, and print it, so the caller has no
    # reason to improvise one afterwards. A floor taken after the request can
    # exclude the very review it triggered; leaving the caller to mint their own
    # is how that bug arrives, and it is not theirs to get right.
    floor = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
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
    if not any(same_login(login, spec.requested_display) for login in logins):
        raise BotReviewError(
            f"requested {spec.request_login!r} but requested_reviewers is "
            f"{logins!r} — the request was accepted and dropped. Do not wait."
        )
    print(f"requested {spec.key}; requested_reviewers now: {', '.join(logins)}")
    print(f"since: {floor}")
    print(f"  pass that to: wait/fetch --bot {spec.key} --pr {pr} --since {floor}")
    return 0


def _clean_verdict(spec: BotSpec, comment: Comment) -> str:
    return (
        f"CLEAN: {spec.key} reported no findings (summary comment "
        f"{comment.get('id')}, updated {comment.get('updated_at')}). "
        "Nothing to fetch or triage."
    )


def cmd_wait(repo: str, pr: int, spec: BotSpec, since: datetime, timeout: int) -> int:
    deadline = time.monotonic() + timeout
    while True:
        # Findings first, every iteration: a review after the floor always
        # outranks a clean summary, so a walkthrough edit racing its own
        # review's submission cannot misreport a findings run as clean.
        review = select_review(list_reviews(repo, pr), spec, since)
        if review is not None:
            print(
                f"{spec.key} findings review {review.get('id')} "
                f"({review.get('submitted_at')}) is ready"
            )
            return 0
        if spec.clean_marker is not None:
            comment = select_clean_comment(issue_comments(repo, pr), spec, since)
            if comment is not None:
                print(_clean_verdict(spec, comment))
                return EXIT_CLEAN
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
        # Same precedence as cmd_wait: only after establishing that no
        # findings review exists may a clean summary answer for the run.
        if spec.clean_marker is not None:
            comment = select_clean_comment(issue_comments(repo, pr), spec, since)
            if comment is not None:
                print(_clean_verdict(spec, comment))
                return EXIT_CLEAN
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
