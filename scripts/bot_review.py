"""Request, await, and fetch automated PR reviews (CodeRabbit, Copilot, Gemini).

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

* ``request --bot B --pr N`` — ask the bot for a review, then verify the ask
  took. Copilot is asked through ``requested_reviewers``; CodeRabbit — manual
  since ``auto_review.enabled: false`` — is asked by posting its
  ``@coderabbitai review`` trigger comment; Gemini (the Antigravity SDK
  workflow, ``.github/workflows/gemini-review.yml``) is asked by dispatching
  that workflow and confirming a run actually started — the dispatch endpoint
  answers 204 whether or not a run will ever exist. All paths stamp the floor
  *before* asking and print it on success, so the caller never mints one; a
  failed ask prints no floor, because there is nothing to wait on.
* ``wait --bot B --pr N --since T`` — block until a findings review or a
  clean-run summary lands. For a dispatch bot, ``--run ID`` (the id request
  printed) makes a failed workflow run end the wait immediately — that run
  was the only thing that could have posted the review.
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
# Confirming a workflow dispatch: run creation is asynchronous (the POST
# answers 204 before any run exists), so the read-back polls — briefly, since
# a run that has not appeared within this window is not coming.
DISPATCH_POLL_SECONDS = 5
DISPATCH_CONFIRM_TIMEOUT = 120


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
    # afterwards. They differ for Copilot. Both None for a bot that is not
    # requestable through requested_reviewers (CodeRabbit ignores it; its ask
    # is the trigger comment below).
    request_login: str | None
    requested_display: str | None
    # The issue-comment body that asks this bot to review, for a bot commanded
    # in-thread rather than through requested_reviewers (CodeRabbit, with
    # auto_review disabled). None for a bot with a real request channel.
    trigger_body: str | None
    # Captures the number of findings the review body claims.
    count: re.Pattern[str]
    # Recognizes the bot's clean-run summary in an *issue comment* body, for a
    # bot whose clean run posts no review object at all (CodeRabbit). None for
    # a bot whose clean run is still a review (Copilot states "generated 0
    # comments" in a review body, which select_review already finds).
    clean_marker: re.Pattern[str] | None
    # The workflow file to dispatch, for a bot that is a repo-owned GitHub
    # Actions workflow rather than a GitHub App (Gemini/Antigravity). Its
    # reviews are authored by github-actions[bot]. A third ask channel,
    # mutually exclusive with the other two. Defaulted (unlike every field
    # above) so the specs and tests that predate it stay valid as written.
    dispatch_workflow: str | None = None

    def __post_init__(self) -> None:
        # One ask channel per bot. cmd_request dispatches on trigger_body
        # first, so a spec setting both would carry request fields that read
        # as active config but never execute — refused here rather than
        # silently half-honored. (ValueError, not BotReviewError: a mis-built
        # spec is a programming error, caught at import when BOTS is built.)
        if self.trigger_body is not None and self.request_login is not None:
            raise ValueError(
                f"{self.key}: trigger_body and request_login are mutually "
                "exclusive ask channels — set exactly one"
            )
        # The request fields come as a pair: one is POSTed, the other is what
        # the read-back verifies. Half a pair would otherwise surface later in
        # cmd_request as the misleading "has neither ask channel" error.
        if (self.request_login is None) != (self.requested_display is None):
            raise ValueError(
                f"{self.key}: request_login and requested_display come as a "
                "pair — set both or neither"
            )
        # Same one-channel rule for the dispatch path: cmd_request tries
        # trigger_body, then dispatch_workflow, then requested_reviewers, so a
        # spec combining dispatch with either would carry config that reads as
        # active but never executes.
        if self.dispatch_workflow is not None and (
            self.trigger_body is not None or self.request_login is not None
        ):
            raise ValueError(
                f"{self.key}: dispatch_workflow is mutually exclusive with the "
                "other ask channels — set exactly one"
            )


# There is deliberately no *comment* author here. Comments are fetched through
# the review's own id, so they need no author filter — which is why Copilot's
# `Copilot` display login is only ever matched against requested_reviewers.
BOTS: dict[str, BotSpec] = {
    "coderabbit": BotSpec(
        key="coderabbit",
        review_login="coderabbitai[bot]",
        request_login=None,
        requested_display=None,
        trigger_body="@coderabbitai review",
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
        trigger_body=None,
        count=re.compile(r"generated (\d+) comment"),
        clean_marker=None,
    ),
    # The Antigravity SDK (Gemini) reviewer is not a GitHub App but a repo
    # workflow, so its ask channel is a workflow dispatch and its reviews are
    # authored by github-actions[bot]. Its clean run is still a review stating
    # 0 findings (like Copilot), so no clean-comment scanning is needed. The
    # count marker is the "posted N inline finding(s)" line the agent script
    # (.github/scripts/gemini_review_agent.py) writes into every review body.
    "gemini": BotSpec(
        key="gemini",
        review_login="github-actions[bot]",
        request_login=None,
        requested_display=None,
        trigger_body=None,
        count=re.compile(r"posted (\d+) inline finding"),
        clean_marker=None,
        dispatch_workflow="gemini-review.yml",
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
    walkthrough comment in place on every review run, so on any PR past its
    first review the creation time predates every floor and a fresh clean run
    would be invisible. Falls back to ``created_at`` only if ``updated_at`` is
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


Run = dict[str, Any]


def run_ts(run: Run) -> datetime:
    return parse_ts(str(run.get("created_at", "")))


def run_id_of(run: Run) -> int:
    return int(str(run.get("id", 0)))


def run_is_for_pr(run: Run, pr: int) -> bool:
    """Whether a dispatch run belongs to this PR, read from the run title.

    workflow_dispatch inputs appear in *no* runs payload, so the workflow
    stamps the PR number into its ``run-name`` ("Gemini review: PR N") and
    this reads it back from ``display_title``. Without the check, one PR's
    run answers for another's ask: a concurrent dispatch cross-confirms, and
    a neighbouring PR's quota failure aborts this PR's wait. Suffix-matched
    whole-token — "PR 5" does not match "...PR 56".
    """
    return str(run.get("display_title", "")).endswith(f"PR {pr}")


def run_has_failed(run: Run) -> bool:
    """Whether a run has completed without success.

    Only ``completed`` runs count — an in-progress run has no verdict yet —
    and ``success`` is excluded because a successful run's review is found by
    the caller's own review check.
    """
    return (
        str(run.get("status")) == "completed"
        and str(run.get("conclusion")) != "success"
    )


def select_failed_run(runs: list[Run], since: datetime, pr: int) -> Run | None:
    """This PR's newest dispatch run after ``since`` that ended unsuccessfully.

    Exists so ``wait`` on a workflow-based bot can fail fast: when the run that
    was supposed to post the review has itself failed (SDK error, exhausted
    Gemini quota), no review is ever coming, and polling the reviews endpoint
    for it would ride the full 30-minute timeout to say less than the run's
    conclusion already says. The PR filter keeps a *neighbouring* PR's failure
    from aborting this wait. This is the recovery path (floor minted from
    ``--since-commit``); a caller holding the run id from ``request`` passes
    ``--run`` instead, which needs neither the timestamp nor the title.
    """
    candidates = [
        run
        for run in runs
        if run_is_for_pr(run, pr) and run_has_failed(run) and run_ts(run) > since
    ]
    if not candidates:
        return None
    return max(candidates, key=run_ts)


def select_confirmed_run(runs: list[Run], pr: int, before: int | None) -> Run | None:
    """The run proving *this PR's* dispatch took: title-matched, newer id.

    Id-newer alone is not proof — two sessions dispatching different PRs
    within seconds would cross-confirm, handing one of them a floor for a run
    that was never created. The title match ties the confirmation to the PR
    that was actually asked for.
    """
    candidates = [
        run
        for run in runs
        if run_is_for_pr(run, pr) and (before is None or run_id_of(run) > before)
    ]
    if not candidates:
        return None
    return max(candidates, key=run_id_of)


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
    # `.github/scripts/gemini_review_agent.py` keeps a deliberate near-twin of
    # this (it takes `stdin` instead of `env` and raises RuntimeError, staying
    # independent of this installable module); keep the hardening below in sync.
    #
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


def workflow_runs(repo: str, workflow: str) -> list[Run]:
    """The workflow's dispatch runs, newest first (one page).

    Unlike the review/comment endpoints, the runs endpoint wraps its list in an
    envelope object, so :func:`gh_all`/:func:`as_page` do not apply — the
    envelope is unwrapped here, with the same refusal to degrade an unexpected
    shape into "no runs". One page suffices: every caller wants only the runs
    since a floor stamped minutes ago, and the endpoint sorts newest first.
    """
    path = (
        f"repos/{repo}/actions/workflows/{workflow}/runs"
        f"?event=workflow_dispatch&per_page={PAGE_SIZE}"
    )
    raw = gh(path)
    envelope = cast("dict[str, Any]", raw) if isinstance(raw, dict) else {}
    runs = envelope.get("workflow_runs")
    if not isinstance(runs, list):
        raise BotReviewError(
            f"gh api {path} returned no workflow_runs list — refusing to "
            "report an unexpected payload as an empty result"
        )
    return [cast("Run", r) for r in cast("list[Any]", runs) if isinstance(r, dict)]


def newest_run_id(repo: str, workflow: str) -> int | None:
    runs = workflow_runs(repo, workflow)
    return run_id_of(runs[0]) if runs else None


def workflow_run(repo: str, run_id: int) -> Run:
    """One workflow run by id — the ``--run`` fail-fast path in ``wait``."""
    path = f"repos/{repo}/actions/runs/{run_id}"
    raw = gh(path)
    if not isinstance(raw, dict):
        raise BotReviewError(
            f"gh api {path} returned {type(raw).__name__}, expected a run object"
        )
    return cast("Run", raw)


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------


def _print_floor(spec: BotSpec, pr: int, floor: str, run_id: int | None = None) -> None:
    print(f"since: {floor}")
    run_arg = f" --run {run_id}" if run_id is not None else ""
    print(
        f"  pass that to: wait/fetch --bot {spec.key} --pr {pr} "
        f"--since {floor}{run_arg}"
    )


def cmd_request(repo: str, pr: int, spec: BotSpec) -> int:
    # Stamp the floor *before* the ask, and print it, so the caller has no
    # reason to improvise one afterwards. A floor taken after the ask can
    # exclude the very review it triggered; leaving the caller to mint their own
    # is how that bug arrives, and it is not theirs to get right.
    floor = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    if spec.trigger_body is not None:
        return _request_by_trigger(repo, pr, spec, floor)
    if spec.dispatch_workflow is not None:
        return _request_by_dispatch(repo, pr, spec, floor)
    if spec.request_login is None or spec.requested_display is None:
        raise BotReviewError(
            f"{spec.key} has neither a request login nor a trigger comment — "
            "it cannot be asked for a review from here"
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
    if not any(same_login(login, spec.requested_display) for login in logins):
        raise BotReviewError(
            f"requested {spec.request_login!r} but requested_reviewers is "
            f"{logins!r} — the request was accepted and dropped. Do not wait."
        )
    print(f"requested {spec.key}; requested_reviewers now: {', '.join(logins)}")
    _print_floor(spec, pr, floor)
    return 0


def _request_by_trigger(repo: str, pr: int, spec: BotSpec, floor: str) -> int:
    """Ask an in-thread-commanded bot for a review by posting its trigger.

    The read-back check is not paranoia: ``gh api`` gives ``@``-prefixed field
    values a special meaning in some flag modes (read the value from a file),
    and every trigger body here starts with ``@``. GitHub then also renders the
    body it *received*, not the one intended. Comparing the created comment's
    body against the spec catches either mangling loudly, instead of buying a
    full poll for a review nobody managed to ask for.
    """
    created = cast(
        "dict[str, Any] | None",
        gh(
            f"repos/{repo}/issues/{pr}/comments",
            "-f",
            f"body={spec.trigger_body}",
        ),
    )
    posted = str(created.get("body", "")) if created else ""
    if created is None or posted != spec.trigger_body:
        raise BotReviewError(
            f"posted the {spec.key} trigger but the created comment body is "
            f"{posted!r}, expected {spec.trigger_body!r} — the ask did not "
            "reach the bot as written. Do not wait."
        )
    print(f"triggered {spec.key} via comment {created.get('id')}")
    _print_floor(spec, pr, floor)
    return 0


def _request_by_dispatch(repo: str, pr: int, spec: BotSpec, floor: str) -> int:
    """Ask a workflow-based bot for a review by dispatching its workflow.

    The dispatches endpoint answers 204 *before* any run exists — and answers
    204 for asks that will never produce one (e.g. a disabled workflow) — so
    the read-back polls for a run id newer than the newest pre-dispatch run.
    Run ids, not timestamps: the floor comes from this machine's clock and run
    creation from GitHub's, and a small skew could exclude the very run the
    dispatch created — the same clock trap :func:`parse_ts` documents, dodged
    rather than re-fought.

    The dispatched ref is the repository's default branch, because that is
    where workflow_dispatch resolves the workflow file — which also means a PR
    that *modifies* the workflow is reviewed by the merged version, not its
    own.

    The confirmed run must be title-matched to this PR (see
    :func:`select_confirmed_run`), and its id is printed and threaded into
    ``wait --run`` so the fail-fast there polls exactly this run.
    """
    workflow = cast("str", spec.dispatch_workflow)
    repo_info = cast("dict[str, Any] | None", gh(f"repos/{repo}"))
    default_branch = str((repo_info or {}).get("default_branch") or "")
    if not default_branch:
        raise BotReviewError(f"could not resolve the default branch of {repo}")
    before = newest_run_id(repo, workflow)
    gh(
        f"repos/{repo}/actions/workflows/{workflow}/dispatches",
        "-f",
        f"ref={default_branch}",
        "-f",
        f"inputs[pr]={pr}",
    )
    deadline = time.monotonic() + DISPATCH_CONFIRM_TIMEOUT
    while True:
        confirmed = select_confirmed_run(workflow_runs(repo, workflow), pr, before)
        if confirmed is not None:
            run_id = run_id_of(confirmed)
            print(f"dispatched {workflow} run {run_id} for PR {pr}")
            _print_floor(spec, pr, floor, run_id=run_id)
            return 0
        if time.monotonic() >= deadline:
            raise BotReviewError(
                f"dispatched {workflow} but no new run for PR {pr} appeared "
                f"within {DISPATCH_CONFIRM_TIMEOUT}s — the dispatch was "
                "accepted and dropped (a workflow_dispatch workflow must "
                f"exist on {default_branch} to be runnable). Do not wait."
            )
        time.sleep(DISPATCH_POLL_SECONDS)


def _clean_verdict(spec: BotSpec, comment: Comment) -> str:
    return (
        f"CLEAN: {spec.key} reported no findings (summary comment "
        f"{comment.get('id')}, updated {comment.get('updated_at')}). "
        "Nothing to fetch or triage."
    )


def cmd_wait(
    repo: str,
    pr: int,
    spec: BotSpec,
    since: datetime,
    timeout: int,
    run_id: int | None = None,
) -> int:
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
        # A workflow-based bot can fail without posting anything; its run's
        # conclusion says so long before the poll times out. Checked after the
        # review lookups so a posted review always outranks a red run. With
        # --run (the id request confirmed and printed), exactly that run is
        # polled — no timestamps to skew, no neighbouring PR's run to match;
        # without it (a recovered floor), fall back to the PR-title-filtered
        # scan of the runs list.
        if spec.dispatch_workflow is not None:
            if run_id is not None:
                run = workflow_run(repo, run_id)
                failed = run if run_has_failed(run) else None
            else:
                runs = workflow_runs(repo, spec.dispatch_workflow)
                failed = select_failed_run(runs, since, pr)
            if failed is not None:
                print(
                    f"FAILED: {spec.dispatch_workflow} run {failed.get('id')} "
                    f"concluded {str(failed.get('conclusion'))!r} without "
                    "posting a review — check the run's logs. Silence is not "
                    "a clean review.",
                    file=sys.stderr,
                )
                return 1
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
    parser.add_argument(
        "--run",
        type=int,
        default=None,
        help=(
            "the dispatched workflow run id printed by request (dispatch bots "
            "only); lets wait fail fast on exactly that run"
        ),
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
            run_id = cast("int | None", args.run)
            return cmd_wait(
                repo, pr, spec, since, int(cast("int", args.timeout)), run_id
            )
        return cmd_fetch(repo, pr, spec, since)
    except BotReviewError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
