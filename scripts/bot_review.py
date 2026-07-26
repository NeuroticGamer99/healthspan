"""Request, await, fetch automated PR reviews (CodeRabbit, Copilot, Gemini, Greptile).

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
* **Re-reviews that move nothing.** Greptile goes further: its findings review
  has an *empty body*, and a re-review edits its summary comment in place
  without resubmitting the review or posting a new comment. Both its outcomes
  are therefore read from that one summary comment, never from the reviews
  endpoint — which a re-review leaves frozen at the original ``submitted_at``.
* **Reviews of the wrong commit.** A bot that reviews on PR creation and then
  stays quiet leaves a report that no timestamp can distinguish from a fresh
  one: the summary is newer than any floor minted at PR creation, however many
  commits have landed since. Greptile names the commit it reviewed, so that is
  checked against the PR head and a mismatch is reported as *stale*, not ready.

Commands. For the per-bot commands (``request``/``wait``/``fetch``): exit 0 =
findings review ready, 1 = failure or timeout, 2 = clean review — the bot
reported no findings; nothing to triage, 3 = empty range — there was nothing to
review at all; nothing to triage.

``outstanding`` inverts 0 and keeps 2 narrower, because it answers a merge
question rather than a triage one: **0 = unanswered findings exist, do not
merge**; 1 = failure, or a zero the sweep could not prove; 2 = nothing
outstanding. Its 2 covers two states it prints differently and never conflates
— every finding answered, versus no bot having posted one at all:

* ``request --bot B --pr N`` — ask the bot for a review, then verify the ask
  took. Copilot is asked through ``requested_reviewers``; CodeRabbit — manual
  since ``auto_review.enabled: false`` — is asked by posting its
  ``@coderabbitai review`` trigger comment; Gemini (the Antigravity SDK
  workflow, ``.github/workflows/gemini-review.yml``) is asked by dispatching
  that workflow and confirming a run actually started — the dispatch endpoint
  answers 204 whether or not a run will ever exist. Greptile is asked with its
  own ``@greptileai review`` trigger comment — needed only for a *re*-review,
  since the App reviews new PRs on its own. All paths stamp the floor *before*
  asking and print it on success, so the caller never mints one; a failed ask
  prints no floor, because there is nothing to wait on.
* ``wait --bot B --pr N --since T`` — block until a findings review or a
  clean-run summary lands. For a dispatch bot, ``--run ID`` (the id request
  printed) makes a failed workflow run end the wait immediately — that run
  was the only thing that could have posted the review.
* ``fetch --bot B --pr N --since T`` — print that review and its own comments.
  For a summary-comment bot this answers "is anything outstanding": it prints
  the findings that have no threaded reply, counts the ones that do, and treats
  a review of a superseded commit as a note rather than a refusal — fix commits
  land after a review, so staleness is the ordinary end state of a triaged PR.
* ``outstanding --pr N --since T`` — the merge gate, asked of *every* bot at
  once (no ``--bot``): which findings on this PR still have no reply. Exit 2
  means none do. The per-bot commands answer "did this bot report"; a merge
  needs "is anything here unread", and those diverge precisely on the PR whose
  review was collected but whose findings were not.

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
from datetime import UTC, datetime, timedelta
from typing import Any, NoReturn, cast

PAGE_SIZE = 100
POLL_SECONDS = 30
DEFAULT_TIMEOUT = 1800
# Bounds one `gh`/`git` invocation. Without it a stalled call blocks forever and
# `wait --timeout` never fires: the deadline is only checked between calls.
COMMAND_TIMEOUT = 120
# `wait`/`fetch` exit status for "the bot ran and found nothing". Distinct from
# 0 so a caller never runs a findings triage against a review that does not
# exist, and from 1 so a clean run is never reported as a failure. For a
# summary-comment bot `fetch` widens this to "nothing outstanding" — a run whose
# findings all carry a threaded reply is as done as one that found none, and the
# question a merge gate asks is whether anything is unanswered.
EXIT_CLEAN = 2
# `wait`/`fetch` exit status for "there was nothing to review" (issue #59) —
# an empty diff range, not a reviewed-and-clean one. Distinct from EXIT_CLEAN:
# a clean review means the bot looked at real changes and found nothing; this
# means no changes ever reached the bot (an already-merged head, an empty PR,
# or every changed path excluded). Distinct from 0 for the same reason
# EXIT_CLEAN is — nothing to triage either way. Currently reachable only
# through a BotSpec that sets `empty_range_marker` (gemini); a bot that
# leaves it unset never returns this.
EXIT_EMPTY_RANGE = 3
# Confirming a workflow dispatch: run creation is asynchronous (the POST
# answers 204 before any run exists), so the read-back polls — briefly, since
# a run that has not appeared within this window is not coming.
DISPATCH_POLL_SECONDS = 5
DISPATCH_CONFIRM_TIMEOUT = 120
# How long after a summary comment its counted inline comments may still be
# in flight. Measured at ~4s (Greptile, PRs #67 and #69); two minutes is
# generous by more than an order of magnitude. Past it, comments that have not
# arrived are not late — they do not exist, and the summary is the only place
# those findings were ever written (PR #72).
COMMENT_GRACE = timedelta(minutes=2)


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
    # a bot whose clean run is still a review (Copilot states "generated no
    # comments" on a clean first pass and "generated no new comments" on a
    # clean re-review — both nonempty-bodied reviews select_review already
    # finds, and both counted as 0 by stated_count; see the count regex below).
    clean_marker: re.Pattern[str] | None
    # The workflow file to dispatch, for a bot that is a repo-owned GitHub
    # Actions workflow rather than a GitHub App (Gemini/Antigravity). Its
    # reviews are authored by github-actions[bot]. A third ask channel,
    # mutually exclusive with the other two. Defaulted (unlike every field
    # above) so the specs and tests that predate it stay valid as written.
    dispatch_workflow: str | None = None
    # The login that authors this bot's inline *comments*, when it differs from
    # the one that authors its reviews. None means they are the same.
    #
    # A third identity, and the reason it exists now and did not before: this
    # module used to reach comments only through their review's id, so it never
    # needed a comment author at all. The gate that sweeps a PR for unanswered
    # findings has no review id to work from, and Copilot posts its reviews as
    # `copilot-pull-request-reviewer[bot]` while posting the comments *inside*
    # them as `Copilot`. Filtering its findings by `review_login` therefore
    # matches nothing and reports a confident "0 findings" — a gate that passes
    # by failing to look, which is worse than no gate at all. Verified on PR
    # #71, where Copilot's single finding is authored `Copilot`.
    comment_login: str | None = None
    # Whether this bot reviews every PR whether or not it was asked. True only
    # for Greptile, whose GitHub App triggers on PR creation. It makes silence
    # *anomalous* rather than merely uninformative: for every other bot, no
    # artifact means the chain was not spent, which is a legitimate state.
    always_reviews: bool = False
    # Captures the number of findings this bot renders in its review *body*
    # rather than as comment objects. Gemini does: anything it cannot anchor to
    # a diff line becomes a bullet in the body, and its HTTP-422 fallback
    # re-posts *every* finding that way — with the inline count restated as 0.
    # Such findings can never carry a threaded reply, so the unanswered sweep
    # cannot see them and must refuse to call the PR clear rather than mistake
    # "0 inline" for "nothing found".
    body_findings: re.Pattern[str] | None = None
    # Recognizes the "nothing to review" outcome in a *review* body (issue
    # #59) — a review stating 0 findings not because the diff was reviewed
    # and found clean, but because the range was empty (already-merged head,
    # empty PR, or every changed path excluded). None for a bot whose
    # empty-range case is not distinguished this way (Copilot always reviews
    # something real when it reviews at all; CodeRabbit's clean case is
    # already the separate `clean_marker` path — see issue #61 for a related
    # but distinct gap in Copilot's own count regex). Checked only after a
    # review already passed `is_findings_review`, i.e. only for a bot whose
    # empty-range case still posts a nonempty-body review.
    empty_range_marker: re.Pattern[str] | None = None
    # Recognizes this bot's own summary *issue comment* (Greptile). Setting it
    # switches the bot onto a different detection model entirely: the summary
    # comment — not the reviews endpoint — is what marks a run complete, and
    # `clean_marker` then classifies that one comment as clean or findings.
    #
    # Greptile needs this because all three assumptions the review-object path
    # rests on are false for it. Its findings review carries an EMPTY body, so
    # `is_findings_review` rejects it; a re-review EDITS the summary comment in
    # place and never resubmits the review, so `submitted_at` stays behind the
    # floor forever; and its findings live in inline comments rather than in a
    # body there is none of. A reviews-endpoint poller therefore times out on a
    # review that completed — the same silent failure `clean_marker` already
    # encodes for CodeRabbit, one step further along.
    summary_marker: re.Pattern[str] | None = None
    # Captures the commit SHA the summary comment reports it last reviewed.
    # Only meaningful alongside `summary_marker`. This is a freshness signal no
    # timestamp can give: a bot that reviews on PR creation and then stays quiet
    # leaves a summary comment whose `updated_at` is newer than any floor minted
    # at PR creation, even after three more commits have landed. Observed live
    # on PR #69 — the footer read `Reviews (1) … 72550f1` while the branch was
    # already three commits ahead. Compared against the PR head, it turns "a
    # review exists" into "a review of *this* commit exists".
    reviewed_commit: re.Pattern[str] | None = None

    @property
    def commenter(self) -> str:
        """The login that authors this bot's inline findings.

        Distinct from :attr:`requested_display` even where the two strings
        coincide (Copilot): one is what ``requested_reviewers`` echoes back
        after a request, the other is what appears on a comment. Keeping them
        separate means a change to either cannot silently redefine the other.
        """
        return self.comment_login or self.review_login

    @property
    def logins(self) -> tuple[str, ...]:
        """Every account this bot speaks as, deduplicated.

        One entry for most bots; two for Copilot. Used where "is this the bot
        itself" must hold across all of its identities rather than just the one
        that authors findings — a bot must not clear the gate by acking under
        whichever login the filter was not watching.
        """
        seen = (self.review_login, self.comment_login)
        return tuple(dict.fromkeys(login for login in seen if login))

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
        # A summary-comment bot classifies that one comment as clean or
        # findings, so without a clean_marker every run would read as findings
        # and a clean PR would be "triaged" against zero comments.
        if self.summary_marker is not None and self.clean_marker is None:
            raise ValueError(
                f"{self.key}: summary_marker requires clean_marker — the "
                "summary comment is the only thing that says which verdict it is"
            )
        # reviewed_commit is read out of the summary comment's footer; without
        # a summary comment to read there is nothing for it to match against,
        # and a staleness guard that never runs is worse than none at all.
        if self.reviewed_commit is not None and self.summary_marker is None:
            raise ValueError(
                f"{self.key}: reviewed_commit requires summary_marker — it is "
                "parsed out of the summary comment"
            )
        # comment_login exists only to record a *divergence*. Setting it to the
        # review login is the copy-paste this class is trying to make
        # impossible: it reads as "I checked, they differ" while meaning
        # nothing, and the next reader has no way to tell which.
        # Case-folded inline rather than through same_login: that helper is
        # defined below BOTS, which is built at import time by these very
        # constructors, so it does not exist yet when this runs.
        if (
            self.comment_login is not None
            and self.comment_login.casefold() == self.review_login.casefold()
        ):
            raise ValueError(
                f"{self.key}: comment_login duplicates review_login — leave it "
                "unset unless the two accounts genuinely differ"
            )


# A spec needs `comment_login` only when the account that authors the bot's
# inline comments differs from the one that authors its reviews. That was once
# nobody: comments were reached through their review's id, which needs no author
# filter at all. Two paths now read comments without a review id — the Greptile
# summary model, whose re-review posts under no new review, and the `outstanding`
# sweep, which has no single review to scope by — so the comment author became a
# real question, and Copilot answers it differently from every other bot.
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
        # Three shapes, all verified against live review bodies on this repo
        # (issue #61): a findings run states "generated N comment(s)" (digit,
        # captured); a clean first pass states "generated no comments" and a
        # clean re-review states "generated no new comments" — neither carries
        # a digit. Without the "no"/"no new" alternative both clean shapes made
        # `stated_count` return None, which `count_note` reports as "cross-check
        # skipped" — inert for exactly the clean re-review where a caller is
        # most tempted not to read the body. The alternation still captures the
        # digit into group 1 when present; the wordless clean shapes match with
        # an empty group 1, which `stated_count` reads as an asserted 0. The
        # trailing `comments?\b` (not a bare `comment`) keeps "generated no
        # commentary" — where "comment" is a mere prefix — from reading as an
        # asserted 0 and silencing the absent-count warning (CodeRabbit, PR #63).
        count=re.compile(r"generated (?:(\d+) comments?|no(?: new)? comments)\b"),
        clean_marker=None,
        # The third identity. Its reviews are authored by the bot login above;
        # the comments inside them are authored `Copilot`, the same string
        # requested_reviewers echoes back — coincidence, not a rule, so the two
        # are declared separately (PR #71).
        comment_login="Copilot",
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
        # Mirrors gemini_review_logic.EMPTY_RANGE_MARKER — the two are not
        # imported from a shared module (this file is stdlib-only; the
        # SDK-dependent agent lives under .github/scripts/) and are kept in
        # sync by a cross-check test, the same convention as the count regex
        # above and review_body's marker.
        empty_range_marker=re.compile(r"<!-- gemini-review: empty-diff-range -->"),
        # Mirrors gemini_review_logic.review_body's unanchored block. Kept in
        # sync by a cross-check test, the same convention as the count regex.
        body_findings=re.compile(r"(\d+) finding\(s\) could not be anchored"),
    ),
    # Greptile is a GitHub App like CodeRabbit, but it reports through a
    # different set of artifacts, transcribed from four live runs on this repo
    # (PRs #67 findings, #68 clean, #69 findings-then-clean, #70 clean):
    #
    #   findings run -> a review object (state COMMENTED, body length ZERO)
    #                   + its inline comments + a summary issue comment
    #   clean run    -> the summary issue comment ONLY; `pulls/N/reviews` and
    #                   `pulls/N/comments` both come back empty
    #   re-review    -> the summary comment is EDITED IN PLACE. No new comment,
    #                   no new review object, and the original review's
    #                   `submitted_at` never moves.
    #
    # So the summary comment is the only artifact present in every outcome, and
    # the only one that moves on a re-review — hence `summary_marker`. Unlike
    # every other bot here, Greptile is also asked for nothing at PR creation:
    # the App reviews new PRs on its own (`.greptile/README.md` explains why that
    # is kept), and `trigger_body` is for re-reviews only.
    "greptile": BotSpec(
        key="greptile",
        review_login="greptile-apps[bot]",
        request_login=None,
        requested_display=None,
        trigger_body="@greptileai review",
        # The one count Greptile states anywhere. Its review body is empty and
        # the summary comment has no "N findings" line; this sentence heads the
        # "Prompt To Fix All With AI" block, which appears only when there are
        # findings. Basis is thin on purpose-visible grounds — exactly ONE
        # observed findings summary (PR #67, "Fix the following 1 code review
        # issue") — but the failure is benign: an unmatched pattern makes
        # `stated_count` return None, which `count_note` reports as "cross-check
        # skipped" rather than asserting a wrong count.
        count=re.compile(r"Fix the following (\d+) code review issue"),
        # Deliberately NOT anchored to a full sentence. PR #70 wrote "No files
        # require special attention." and PR #68 wrote "No files require special
        # attention; the spec files are consistent with each other." — the
        # trailing clause is free prose. A period-anchored pattern would have
        # missed #68 and reported a clean run as findings, then triaged it
        # against zero comments.
        clean_marker=re.compile(r"No files require special attention"),
        # An HTML marker rather than the visible "Greptile Summary" heading:
        # both appear in all four observed bodies, but the HTML one is written
        # for machines and survives the section toggles in `.greptile/config.json`
        # that could restyle the heading. Same reasoning as CodeRabbit's
        # auto-generated-comment marker above.
        summary_marker=re.compile(r"<!-- greptile_other_comments_section -->"),
        # The footer, one line: `Reviews (N): Last reviewed commit:
        # ["<subject>"](https://github.com/<owner>/<repo>/commit/<sha>) |
        # [Re-trigger Greptile](...)`. Bounded to a single line so the capture
        # cannot wander into an unrelated commit link elsewhere in the summary.
        reviewed_commit=re.compile(
            r"Last reviewed commit:[^\n]*?/commit/([0-9a-f]{7,40})"
        ),
        # The only bot here that is never legitimately unspent.
        always_reviews=True,
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


def is_empty_range_review(review: Review, spec: BotSpec) -> bool:
    """Whether a findings-shaped review actually states "nothing to review".

    Applies only to a bot whose empty-range case is still a *review* with a
    nonempty body (currently gemini) — such a review already passes
    :func:`is_findings_review`, so callers check this only after
    :func:`select_review` has returned one, to separate "reviewed and found
    nothing" (genuinely clean, or real findings) from "there was nothing to
    review" (issue #59).
    """
    if spec.empty_range_marker is None:
        return False
    return bool(spec.empty_range_marker.search(str(review.get("body") or "")))


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


def is_summary_comment(comment: Comment, spec: BotSpec) -> bool:
    """Whether this issue comment is the bot's own per-run summary.

    Recognizing the summary *without* regard to its verdict is the point: for a
    ``summary_marker`` bot this comment is the completion signal for a findings
    run and a clean run alike, and :func:`is_clean_comment` then says which.
    """
    if spec.summary_marker is None:
        return False
    if not same_login(_login_of(comment), spec.review_login):
        return False
    return bool(spec.summary_marker.search(str(comment.get("body") or "")))


def select_summary_comment(
    comments: list[Comment], spec: BotSpec, since: datetime
) -> Comment | None:
    """The newest run summary this bot posted or edited after ``since``.

    Compared on :func:`comment_ts` (``updated_at``) rather than creation, which
    is what makes a re-review visible at all: Greptile rewrites this one comment
    in place, so on any PR past its first review the creation time predates
    every floor.
    """
    candidates = [
        comment
        for comment in comments
        if is_summary_comment(comment, spec) and comment_ts(comment) > since
    ]
    if not candidates:
        return None
    return max(candidates, key=comment_ts)


def reviewed_sha(body: str, spec: BotSpec) -> str | None:
    """The commit the summary says it last reviewed, if it names one."""
    if spec.reviewed_commit is None:
        return None
    found = spec.reviewed_commit.search(body)
    return found.group(1) if found else None


def same_commit(left: str, right: str) -> bool:
    """Whether two commit-ish hex strings denote the same commit.

    Prefix-compared on the shorter of the two, because the summary footer's
    link length is the bot's choice and not a contract; requiring 40 characters
    on both sides would turn a shortened SHA into a permanent false "stale".
    Below seven characters nothing is compared — an abbreviation that short is
    not evidence of anything, and answering ``True`` there would silently
    disable the staleness guard.
    """
    width = min(len(left), len(right))
    if width < 7:
        return False
    return left[:width].casefold() == right[:width].casefold()


def is_finding_comment(comment: Comment, spec: BotSpec) -> bool:
    """Whether this review comment is one of the bot's own top-level findings.

    Two filters, each removing a different impostor. The author check drops
    everyone else's comments, since these come from the pull-level endpoint
    rather than from one review's own — the review id cannot be used for
    scoping here, because a Greptile re-review posts under no new review at all.
    The ``in_reply_to_id`` check drops the bot's replies to a triage thread,
    which are authored by the same login and would otherwise be re-triaged as
    fresh findings on the next run.

    The author matched is :attr:`BotSpec.commenter`, **not** ``review_login``.
    For Copilot those differ, and matching the review's author here filters out
    every one of its findings — then answers "none outstanding" with complete
    confidence (PR #71).
    """
    if not same_login(_login_of(comment), spec.commenter):
        return False
    return comment.get("in_reply_to_id") in (None, 0)


def answered_ids(comments: list[Comment], not_by: tuple[str, ...] = ()) -> set[int]:
    """Ids of comments that carry a threaded reply, ignoring self-replies.

    A reply sets ``in_reply_to_id`` to the comment it answers, and that is the
    entire record GitHub keeps of a finding having been triaged. Verified on
    PRs #67 and #69, where the owner's replies point at the bot's findings
    exactly this way.

    ``not_by`` excludes replies from the finding's own author. A bot answering
    itself is not triage: CodeRabbit acks threads routinely and once withdrew
    its own finding that way (PR #65), and counting that as "answered" would let
    a bot clear the merge gate on its own say-so, which is exactly the review
    nobody read that the gate exists to stop.

    Callers pass every login belonging to *any* bot, not just the one whose
    finding is being judged. Two reasons, and the second is the load-bearing
    one: Copilot holds two logins and an ack under the unwatched one would slip
    through; and a reply from a *different* bot is no more triage than a
    self-ack, since neither is a person having read the finding. CodeRabbit
    auto-replies to findings as a matter of course — three of its own on PR #71
    — and nothing stops that landing on another bot's thread, where a
    per-spec exclusion would score it as answered.
    """
    ids: set[int] = set()
    for comment in comments:
        parent = comment.get("in_reply_to_id")
        if not parent:
            continue
        if any(same_login(_login_of(comment), login) for login in not_by):
            continue
        ids.add(int(str(parent)))
    return ids


def bot_logins() -> tuple[str, ...]:
    """Every login any configured bot speaks as."""
    return tuple(
        dict.fromkeys(login for spec in BOTS.values() for login in spec.logins)
    )


def unanswered(findings: list[Comment], comments: list[Comment]) -> list[Comment]:
    """The findings with no reply from anyone but a bot.

    This, not the freshness of the review, is what "is there outstanding review
    work" means. A PR that took fix commits *after* its review is the normal end
    state of triage, not an anomaly; a finding nobody answered is the anomaly.

    Takes no spec on purpose: the exclusion spans **every** bot, so there is
    nothing per-bot to pass. It briefly did take one, which promised per-spec
    filtering the body had stopped doing (Greptile, PR #72) — a signature that
    advertises a knob with no wire behind it is worse than none.
    """
    already = answered_ids(comments, not_by=bot_logins())
    return [f for f in findings if int(str(f.get("id", 0))) not in already]


def select_finding_comments(
    comments: list[Comment], spec: BotSpec, since: datetime
) -> list[Comment]:
    """This bot's top-level findings after ``since``, oldest first.

    The floor is the only scoping available (see :func:`is_finding_comment`), so
    an earlier run's findings are excluded by time. **What happens when a
    re-review re-reports the same finding has not been observed** — no live
    re-review has produced findings on this repo, only findings-then-clean. If
    the bot edits its existing comments instead of posting new ones, the edited
    ``updated_at`` brings them back in; if it silently leaves them, the summary's
    stated count will exceed what is fetched and :func:`count_note` says so.
    Either way the disagreement is reported rather than resolved by guessing.
    """
    return sorted(
        (
            comment
            for comment in comments
            if is_finding_comment(comment, spec) and comment_ts(comment) > since
        ),
        key=comment_ts,
    )


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
    """The finding count the review body claims, if it states one.

    Three outcomes, distinct on purpose: no match at all is ``None`` ("the body
    states nothing countable — cross-check skipped"); a match whose count group
    captured digits is that integer; a match whose count group is *empty* is a
    ``0`` explicitly asserted in words. That last case is Copilot's wordless
    clean review — "generated no comments" / "generated no new comments" (issue
    #61) — where the count regex matches the phrase but leaves group 1 unset. It
    must read as an asserted 0, not None, so the cross-check stays live for a
    clean re-review instead of skipping the one case a caller is most tempted to
    wave through. Bots whose regex has no wordless alternative always populate
    group 1 on a match, so this collapses to the digit path for them.
    """
    found = spec.count.search(body)
    if found is None:
        return None
    digits = found.group(1)
    return int(digits) if digits is not None else 0


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


def pull_comments(repo: str, pr: int) -> list[Comment]:
    """Every review comment on the PR, across all reviews.

    The deliberately unscoped fetch that :func:`review_comments` exists to
    avoid. Two callers need it, both because they have no single review id to
    scope by: a ``summary_marker`` bot, whose re-review posts under no new
    review, and :func:`cmd_outstanding`, which asks about every bot at once.
    Scoping is restored by author and floor in :func:`select_finding_comments`
    — which is why an author filter that matches nothing is a silent
    catastrophe here rather than a mild bug, and why :func:`unmatched_reviews`
    cross-checks it against the reviews endpoint.
    """
    raw = gh_all(f"repos/{repo}/pulls/{pr}/comments")
    return [cast("Comment", c) for c in raw if isinstance(c, dict)]


def pr_head_sha(repo: str, pr: int) -> str:
    """The PR's current head commit — what a fresh review must have reviewed."""
    pull = cast("dict[str, Any] | None", gh(f"repos/{repo}/pulls/{pr}"))
    head = cast("dict[str, Any]", (pull or {}).get("head") or {})
    sha = str(head.get("sha") or "")
    if not sha:
        raise BotReviewError(
            f"could not read the head SHA of {repo}#{pr} — refusing to check "
            "review freshness against nothing"
        )
    return sha


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

    The dispatched ref is the repository's default branch — not because the
    event guarantees it, but because it is the only safe choice. A
    workflow_dispatch run executes the workflow file from *the ref it is
    given*; the default branch merely has to carry a same-named file for the
    ask to be accepted. Dispatching anything else would run that ref's copy of
    a job holding an API key and a pull-requests:write token (ADR-0064). It
    also means a PR that *modifies* the workflow is reviewed by the merged
    version, not its own.

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


def _short(sha: str | None) -> str:
    return sha[:7] if sha else "unknown"


@dataclass(frozen=True)
class SummaryState:
    """What a ``summary_marker`` bot's latest summary comment says about a run.

    ``stale`` is deliberately *not* "the summary is old". It means the summary
    names a commit that is not this PR's head — i.e. the bot has reported, but
    on different code. That case is indistinguishable from a fresh review by
    timestamp alone, which is the whole reason this type exists.
    """

    comment: Comment
    # The summary's *own* prose verdict — the clean marker is present. Never
    # sufficient on its own: see `contradiction` below.
    clean: bool
    stale: bool
    reviewed: str | None
    head: str
    # The count the summary claims, if it states one at all.
    stated: int | None
    # This bot's top-level findings above the floor, and the subset still
    # lacking a threaded reply.
    findings: list[Comment]
    open_findings: list[Comment]

    @property
    def signals_conflict(self) -> str | None:
        """Set when the two verdict signals disagree irreconcilably.

        Two independent signals say whether a run found anything: the prose
        marker ("No files require special attention") and the stated count
        ("Fix the following N code review issue"). Neither is a fixed template —
        the first is model-written prose, proven variable when PR #68 appended a
        clause to it, and the second comes from the fix-prompt block, which is a
        *configurable* feature (`fixWithAI`). Trusting either alone makes a
        config or wording change classify every findings run as clean, silently.
        So they must agree, and a caller that cannot classify says so instead of
        guessing — the one outcome worse than either answer is a confident wrong
        one.

        Kept separate from :attr:`comments_pending` because the two want
        opposite handling: no amount of waiting resolves this one, so ``wait``
        must fail on it rather than poll.
        """
        if self.clean and self.stated:
            return (
                f"the summary reads clean but also states {self.stated} "
                "finding(s) — the two signals disagree, so this run cannot be "
                "classified. Read the summary on the PR before concluding."
            )
        return None

    @property
    def undercounted(self) -> bool:
        """Whether the summary counts findings the comments endpoint lacks."""
        return bool(self.stated and len(self.findings) < self.stated)

    @property
    def comments_pending(self) -> bool:
        """Whether those missing comments are plausibly *still arriving*.

        Greptile posts its summary about four seconds before the inline
        comments it counts (PRs #67 and #69), so a gap seen inside that window
        resolves itself on the next poll and ``wait`` should keep polling.

        The window is what makes this safe, and its absence was a live hang:
        on PR #72 Greptile stated one finding and posted **no** review object
        and **no** inline comment at all — the finding existed only as prose in
        the summary. Modelling a four-second race as unbounded meant ``wait``
        polled for comments that were never coming and would have burned its
        whole timeout. Past the grace period the same gap means something
        else entirely — findings that live only in the summary, which can never
        carry a reply — and that is a conclusion, not a reason to wait.
        """
        if not self.undercounted:
            return False
        return (datetime.now(UTC) - comment_ts(self.comment)) < COMMENT_GRACE

    @property
    def contradiction(self) -> str | None:
        """Why a single-shot caller cannot classify this summary, if it cannot.

        Composes both causes for ``fetch``. ``wait`` deliberately does not use
        this: it must distinguish the conflict it should fail on from the race
        it should wait out.
        """
        if self.signals_conflict is not None:
            return self.signals_conflict
        if not self.undercounted:
            return None
        gap = (
            f"the summary states {self.stated} finding(s) but only {len(self.findings)}"
        )
        if self.comments_pending:
            return (
                f"{gap} were fetched. Greptile posts its summary a few seconds "
                "before the inline comments (4s on PRs #67 and #69), so this is "
                "usually a review still landing — re-run rather than concluding "
                "it is clean."
            )
        # Past the grace window the gap is structural, not a race. Still a
        # refusal rather than a pass: the missing findings were written
        # somewhere no reply can reach, so no triage will ever clear them and
        # reporting the PR clean would bury them.
        return (
            f"{gap} were fetched, and the comments are no longer in flight. "
            "Those findings exist only in the summary text, where nothing can "
            "reply to them — read the summary on the PR and answer them there."
        )


def summary_state(
    repo: str, pr: int, spec: BotSpec, since: datetime
) -> SummaryState | None:
    """Read this bot's latest run summary, or ``None`` if it has not reported.

    Reads the findings alongside it, deliberately: the summary is prose the
    model wrote, and the comments endpoint is the ground truth about what was
    actually posted. Classifying a run without consulting both means the clean
    path — the one that ends a triage — is the only path that checks nothing.

    A summary that names no commit is *not* treated as stale: an unparsed
    footer is missing evidence, not evidence of staleness, and failing closed
    there would wedge every wait behind a reworded footer. Callers surface that
    case instead (``fetch`` prints a freshness-unverified note).
    """
    comment = select_summary_comment(issue_comments(repo, pr), spec, since)
    if comment is None:
        return None
    body = str(comment.get("body") or "")
    reviewed = reviewed_sha(body, spec)
    head = pr_head_sha(repo, pr) if spec.reviewed_commit is not None else ""
    posted = pull_comments(repo, pr)
    findings = select_finding_comments(posted, spec, since)
    return SummaryState(
        comment=comment,
        clean=spec.clean_marker is not None and bool(spec.clean_marker.search(body)),
        stale=reviewed is not None and not same_commit(reviewed, head),
        reviewed=reviewed,
        head=head,
        stated=stated_count(body, spec),
        findings=findings,
        open_findings=unanswered(findings, posted),
    )


def _timeout_message(spec: BotSpec, timeout: int, stale: SummaryState | None) -> str:
    if stale is not None:
        return (
            f"TIMEOUT: {spec.key} has reported on this PR, but its summary "
            f"last reviewed {_short(stale.reviewed)} while the head is "
            f"{_short(stale.head)} — the review is STALE, not missing. Nothing "
            "has looked at the current commit; ask for a re-review rather than "
            "reading the existing summary as this commit's verdict."
        )
    return (
        f"TIMEOUT: no {spec.key} findings review after {timeout}s. "
        "Silence is not a clean review — check the PR before concluding."
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
    stale: SummaryState | None = None
    while True:
        if spec.summary_marker is not None:
            # This bot's whole signal is the summary comment — the reviews
            # endpoint does not move on a re-review and its findings review
            # carries an empty body, so neither can be polled. A stale summary
            # is not an answer: keep waiting, but remember it, because "the bot
            # reviewed an older commit" is a far more useful timeout than "no
            # review arrived".
            state = summary_state(repo, pr, spec, since)
            if state is not None and state.stale:
                stale = state
            elif state is not None:
                # Signals that disagree end the wait *here*, with the same
                # verdict and message `fetch` would give. Reporting "ready" and
                # letting fetch refuse would make the two commands contradict
                # each other about the same state, and send a caller to triage
                # a run neither of them can classify.
                conflict = state.signals_conflict
                if conflict is not None:
                    print(f"error: {conflict}", file=sys.stderr)
                    return 1
                # Clean means clean *and* nothing left open: a run that reports
                # no files needing attention can still sit above an untriaged
                # finding from an earlier run, and ending the wait there is how
                # a finding nobody read reaches a merge. (A clean summary that
                # also stated a count was refused above, so `clean` here already
                # implies no stated count.)
                if state.clean and not state.open_findings:
                    print(_clean_verdict(spec, state.comment))
                    return EXIT_CLEAN
                # The summary is posted a few seconds before the inline
                # comments it counts (4s on PRs #67 and #69). Returning inside
                # that window hands `fetch` a review whose findings have not
                # landed, which it then reports as a count mismatch — a real
                # alarm for a non-problem. Keep polling until they are there.
                elif not state.comments_pending:
                    summary_only = ""
                    if state.undercounted and state.stated is not None:
                        missing = state.stated - len(state.findings)
                        summary_only = (
                            f", and {missing} that exist only in the summary text"
                        )
                    print(
                        f"{spec.key} findings summary {state.comment.get('id')} "
                        f"(updated {state.comment.get('updated_at')}, reviewed "
                        f"{_short(state.reviewed)}) is ready — "
                        f"{len(state.open_findings)} open finding(s){summary_only}"
                    )
                    return 0
        else:
            # Findings first, every iteration: a review after the floor always
            # outranks a clean summary, so a walkthrough edit racing its own
            # review's submission cannot misreport a findings run as clean.
            review = select_review(list_reviews(repo, pr), spec, since)
            if review is not None:
                if is_empty_range_review(review, spec):
                    print(
                        f"{spec.key} review {review.get('id')} "
                        f"({review.get('submitted_at')}) reports nothing to "
                        "review — no diff was ever reviewed. Not a clean review, "
                        "not findings; nothing to triage."
                    )
                    return EXIT_EMPTY_RANGE
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
            # conclusion says so long before the poll times out. Checked after
            # the review lookups so a posted review always outranks a red run.
            # With --run (the id request confirmed and printed), exactly that
            # run is polled — no timestamps to skew, no neighbouring PR's run to
            # match; without it (a recovered floor), fall back to the
            # PR-title-filtered scan of the runs list.
            if spec.dispatch_workflow is not None:
                if run_id is not None:
                    run = workflow_run(repo, run_id)
                    failed = run if run_has_failed(run) else None
                else:
                    runs = workflow_runs(repo, spec.dispatch_workflow)
                    failed = select_failed_run(runs, since, pr)
                if failed is not None:
                    print(
                        f"FAILED: {spec.dispatch_workflow} run "
                        f"{failed.get('id')} concluded "
                        f"{str(failed.get('conclusion'))!r} without posting a "
                        "review — check the run's logs. Silence is not a clean "
                        "review.",
                        file=sys.stderr,
                    )
                    return 1
        if time.monotonic() >= deadline:
            print(_timeout_message(spec, timeout, stale), file=sys.stderr)
            return 1
        time.sleep(POLL_SECONDS)


def _fetch_summary_bot(repo: str, pr: int, spec: BotSpec, since: datetime) -> int:
    """``fetch`` for a bot whose run is reported through a summary comment.

    Answers one question: **is there review work outstanding on this PR?** So
    the exit codes are about open findings rather than about the summary's
    prose — ``EXIT_CLEAN`` when nothing is unanswered (whether because the run
    was clean or because every finding already has a reply), ``0`` when
    something needs triage, and ``1`` when the signals disagree badly enough
    that no honest answer exists.

    Staleness is reported, never fatal: it is the ordinary end state of a
    triaged PR, since the fixes a review provokes are commits made after it.
    """
    state = summary_state(repo, pr, spec, since)
    if state is None:
        print(
            f"no {spec.key} summary after {since.isoformat()} — "
            "that is not the same as a clean review",
            file=sys.stderr,
        )
        return 1
    blocked = state.contradiction
    if blocked is not None:
        print(f"error: {blocked}", file=sys.stderr)
        return 1

    body = str(state.comment.get("body") or "")
    print(f"=== {spec.key} summary comment {state.comment.get('id')} ===")
    print(f"reviewed:  {state.reviewed[:7] if state.reviewed else 'not stated'}")
    print(f"updated:   {state.comment.get('updated_at')}")
    if state.reviewed is None:
        print(
            "NOTE: the summary names no reviewed commit — freshness could not "
            "be verified. Confirm it reviewed the current head before triaging."
        )
    elif state.stale:
        # Reported, not refused. Fix commits land *after* a review — that is
        # what triaging findings produces — so on any PR that took review
        # fixes, head has moved past the reviewed commit by merge time.
        # Refusing there would fire on the ordinary end state of every
        # reviewed PR while saying nothing about the case that matters,
        # which is a finding nobody answered.
        print(
            f"NOTE: this review looked at {_short(state.reviewed)}, not the "
            f"current head {_short(state.head)}. Anything it reported still "
            "stands, but nothing has reviewed the current code — re-trigger if "
            "you need a verdict on it."
        )
    # Skipped when there is nothing to cross-check: a clean run states no count
    # *by design*, and zero fetched findings agrees with it. Printing the
    # "cross-check skipped" warning on every clean run is how a NOTE becomes
    # something people learn to scroll past.
    if state.stated is not None or state.findings:
        note = count_note(state.stated, len(state.findings))
        if note:
            print(f"NOTE: {note}")

    if not state.open_findings:
        answered = len(state.findings)
        print()
        print(
            _clean_verdict(spec, state.comment)
            if state.clean
            else f"NOTHING OUTSTANDING: all {answered} finding(s) have a reply."
        )
        return EXIT_CLEAN

    print()
    print(body)
    print(f"=== {len(state.open_findings)} open finding(s) since the floor ===")
    for comment in state.open_findings:
        line = comment.get("line") or comment.get("original_line")
        print(f"\n--- {comment.get('path')}:{line} [id={comment.get('id')}] ---")
        print(comment.get("body"))
    return 0


def cmd_fetch(repo: str, pr: int, spec: BotSpec, since: datetime) -> int:
    if spec.summary_marker is not None:
        return _fetch_summary_bot(repo, pr, spec, since)
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
    empty_range = is_empty_range_review(review, spec)

    print(f"=== {spec.key} review {review_id} ===")
    print(f"commit:    {str(review.get('commit_id', ''))[:7]}")
    print(f"submitted: {review.get('submitted_at')}")
    if empty_range:
        print(
            "NOTE: EMPTY RANGE — nothing was reviewed; see the body below "
            "for which cause. Not a clean review, not findings; nothing to "
            "triage."
        )
    else:
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
    return EXIT_EMPTY_RANGE if empty_range else 0


@dataclass(frozen=True)
class BotFindings:
    """One bot's findings on a PR, and the subset still lacking a reply."""

    key: str
    findings: list[Comment]
    open_findings: list[Comment]


def outstanding_findings(comments: list[Comment], since: datetime) -> list[BotFindings]:
    """Split one pull-level comment fetch across every bot.

    Pure, and returns the buckets rather than printing them, so a caller — or a
    test — can assert *which* bot owns *which* unanswered finding rather than
    grep the rendered output for a substring.
    """
    report: list[BotFindings] = []
    for key, spec in sorted(BOTS.items()):
        found = select_finding_comments(comments, spec, since)
        report.append(
            BotFindings(
                key=key, findings=found, open_findings=unanswered(found, comments)
            )
        )
    return report


def reviews_by(reviews: list[Review], spec: BotSpec, since: datetime) -> list[Review]:
    """This bot's reviews submitted after ``since``, whatever their body.

    Unlike :func:`select_review` this does not require a non-empty body, because
    the question here is "did this bot report at all" rather than "is there a
    findings review to triage" — and Greptile's findings review has an empty
    body by design.

    A review with no ``submitted_at`` is a *pending* one — drafted and never
    submitted, which GitHub reports as a null stamp. It is not a report and is
    skipped deliberately rather than incidentally; a bot has never been seen to
    leave one.
    """
    found: list[Review] = []
    for review in reviews:
        if not same_login(_login_of(review), spec.review_login):
            continue
        stamp = str(review.get("submitted_at") or "")
        if stamp and parse_ts(stamp) > since:
            found.append(review)
    return found


def unmatched_reviews(
    report: list[BotFindings], reviews: list[Review], since: datetime
) -> list[str]:
    """Bots that posted a review but whose comments the sweep matched none of.

    The identity-drift alarm. A review carrying findings always carries the
    comments that hold them, so matching *zero* of them while a review exists
    means the author filter missed — the Copilot failure this branch fixes,
    which would silently recur the next time a bot renames a login or adds one.

    Deliberately keyed on **zero** matched rather than on a count mismatch.
    CodeRabbit has consolidated three findings into one comment object (PR #67),
    so "stated 3, matched 1" is a known-benign shape; refusing on it would block
    merges routinely. Zero-matched-despite-a-review has no such benign form —
    except a review that states zero findings, which is Copilot's clean run and
    is excluded here.
    """
    alarms: list[str] = []
    for bot in report:
        if bot.findings:
            continue
        spec = BOTS[bot.key]
        for review in reviews_by(reviews, spec, since):
            body = str(review.get("body") or "")
            # An empty-bodied review is how GitHub models a bot *replying* to a
            # thread, and CodeRabbit acks routinely — so for most bots that is
            # not a report and must not raise the alarm. The exception is a
            # summary-comment bot: Greptile's findings review is empty-bodied by
            # design, and suppressing it here would disable this check for the
            # one bot whose findings it cannot otherwise see. `is_findings_review`
            # is deliberately not reused: it would collapse both cases the wrong
            # way, which is exactly the mistake this comment exists to prevent.
            if not body.strip() and spec.summary_marker is None:
                continue
            stated = stated_count(body, spec)
            if stated == 0:
                continue  # a review that says it found nothing, and did
            alarms.append(
                f"{bot.key}: review {review.get('id')} exists "
                f"({'no count stated' if stated is None else f'states {stated}'})"
                f" but 0 of its comments matched author {spec.commenter!r} — "
                "the filter, not the bot, is probably wrong"
            )
    return alarms


def body_only_findings(
    report: list[BotFindings], reviews: list[Review], since: datetime
) -> list[str]:
    """Reviews that put findings in the body, where no reply can reach them.

    Runs for **every** bot regardless of how many of its comments matched, which
    is what separates it from :func:`unmatched_reviews`. Gemini's ordinary path
    posts anchored findings as real comments *and* unanchored ones as body
    bullets **in the same review** (`gemini_review_agent.py`, `review_body(
    len(anchored), unanchored)`) — so a review with two answered comments can
    still carry a third finding the sweep can never see. Keying this on
    "matched nothing" would miss exactly that mixed shape, and the mixed shape
    is the common one; the all-body-only case is only the HTTP-422 fallback.

    A body-only finding is not a comment object, so it can never carry a
    threaded reply and no amount of triage will clear it. The sweep's contract
    does not reach it, and saying so is the only honest answer available.
    """
    alarms: list[str] = []
    for bot in report:
        spec = BOTS[bot.key]
        if spec.body_findings is None:
            continue
        for review in reviews_by(reviews, spec, since):
            found = spec.body_findings.search(str(review.get("body") or ""))
            if found and int(found.group(1)):
                alarms.append(
                    f"{bot.key}: review {review.get('id')} renders "
                    f"{found.group(1)} finding(s) in its body rather than as "
                    "comments. They cannot carry a reply, so this sweep can "
                    "never clear them — read the review itself"
                )
    return alarms


def undercounted_summaries(
    report: list[BotFindings], issue_comments_: list[Comment], since: datetime
) -> list[str]:
    """Summary-comment bots whose stated count exceeds what the sweep matched.

    Restores a check the per-bot ``fetch --bot greptile`` precondition used to
    perform and the sweep otherwise drops: `unmatched_reviews` only looks at
    bots that matched *nothing*, so a summary claiming three findings while one
    comment matched — and that one answered — would clear the gate with two
    never seen. The count lives in the summary comment, which the reviews
    endpoint does not carry.

    **Why this alarms on a count mismatch when :func:`unmatched_reviews`
    deliberately does not.** That one refuses to, because CodeRabbit is known to
    consolidate several findings into one comment object (PR #67), making
    "stated 3, matched 1" a routine benign shape for it. No such batching has
    been observed for Greptile — both its findings runs posted exactly one
    comment per stated finding — but "not observed" across two runs is thin
    evidence, so this errs toward stopping rather than passing and says which
    of the two causes it cannot distinguish. If Greptile turns out to batch,
    this will fire on a clean PR and should be re-keyed to zero-matched like
    its sibling.
    """
    alarms: list[str] = []
    for bot in report:
        spec = BOTS[bot.key]
        if spec.summary_marker is None:
            continue
        summary = select_summary_comment(issue_comments_, spec, since)
        if summary is None:
            continue
        stated = stated_count(str(summary.get("body") or ""), spec)
        if stated is not None and stated > len(bot.findings):
            alarms.append(
                f"{bot.key}: its summary states {stated} finding(s) but the "
                f"sweep matched {len(bot.findings)} — either "
                f"{stated - len(bot.findings)} were never seen and their "
                "replies cannot have been checked, or it batched several into "
                "one comment (unobserved for this bot). Read the summary"
            )
    return alarms


def silent_always_reviewers(
    report: list[BotFindings],
    reviews: list[Review],
    issue_comments_: list[Comment],
    since: datetime,
) -> list[str]:
    """Bots that always review, yet left no artifact on this PR at all.

    Greptile's App reviews every non-draft PR unasked, so its silence is never
    legitimate — it means the App did not run, or ran and its output was not
    found. Under a comments-only sweep that silence contributes zero findings
    and reads as green, which makes the one bot whose absence is always
    anomalous the one whose absence looks cleanest.
    """
    alarms: list[str] = []
    for bot in report:
        spec = BOTS[bot.key]
        if not spec.always_reviews or bot.findings:
            continue
        if reviews_by(reviews, spec, since):
            continue
        if select_summary_comment(issue_comments_, spec, since) is not None:
            continue
        alarms.append(
            f"{bot.key}: reviews every PR unasked, but posted no review and no "
            "summary comment since the floor — it did not run, or its output "
            "was not found. Silence here is never a clean verdict."
        )
    return alarms


def cmd_outstanding(repo: str, pr: int, since: datetime) -> int:
    """Report every bot finding on this PR that still has no reply.

    The merge gate, asked of *all* reviewers rather than one. It exists because
    the per-bot chains answer "did this bot report" while a merge needs "is
    anything on this PR unread" — and the two diverge exactly when it matters,
    on a PR whose review was collected but whose findings were not.

    Three fetches, not one: the comments carry the findings and the replies
    that resolve them, but a sweep that reads *only* comments cannot tell "no
    findings" from "findings I failed to match", and those differ by exactly
    the identity drift `comment_login` exists to absorb. The reviews endpoint
    and — for a bot that always reviews — the issue comments are what make a
    zero provable rather than merely reported.
    """
    comments = pull_comments(repo, pr)
    reviews = list_reviews(repo, pr)
    report = outstanding_findings(comments, since)
    for bot in report:
        print(
            f"{bot.key:<11} {len(bot.findings):>3} finding(s) since the floor, "
            f"{len(bot.open_findings):>3} unanswered"
        )
    found = sum(len(b.findings) for b in report)
    still_open = sum(len(b.open_findings) for b in report)

    # The four detectors are additive and may double-report: a Greptile review
    # that matched nothing while its summary states a count trips both
    # unmatched_reviews and undercounted_summaries. That is deliberate — each
    # states a different true fact about the same PR, and suppressing one to
    # tidy the output would drop evidence a human is about to act on. None can
    # mask another; any non-empty list blocks.
    unprovable = unmatched_reviews(report, reviews, since)
    unprovable += body_only_findings(report, reviews, since)
    # Fetched when some bot's evidence lives in an issue comment rather than in
    # a review — a summary-comment bot's stated count, or an always-reviews
    # bot's proof of having run. The guard names that dependency rather than a
    # bot: if no spec declares either property the fetch is skipped *and* so are
    # the two checks that need it, which is correct degradation rather than a
    # check that quietly stops applying.
    if any(s.summary_marker is not None or s.always_reviews for s in BOTS.values()):
        issues = issue_comments(repo, pr)
        unprovable += undercounted_summaries(report, issues, since)
        unprovable += silent_always_reviewers(report, reviews, issues, since)
    if unprovable:
        print("\nCANNOT CLEAR THE GATE:", file=sys.stderr)
        for line in unprovable:
            print(f"  {line}", file=sys.stderr)
        print(
            "\nThese are not 'no findings' — they are findings this sweep could "
            "not read.\nA zero it cannot prove must not clear a merge.",
            file=sys.stderr,
        )
        return 1

    if not found:
        # Distinct from "all answered" on purpose. This command reads *posted*
        # comments, so a PR nobody reviewed and a PR reviewed clean look
        # identical here, and only one of them is safe to merge quietly.
        # Saying "nothing outstanding" for both is how silence gets read as a
        # verdict — the mistake this module refuses everywhere else.
        print(
            "\nNO FINDINGS POSTED: no bot has left a finding on this PR since "
            "the floor.\nThat is not evidence any bot reviewed it — an unspent "
            "chain and a clean\nrun are indistinguishable here. Confirm which "
            "reviewers actually ran."
        )
        return EXIT_CLEAN
    if not still_open:
        print(f"\nNOTHING OUTSTANDING: all {found} finding(s) have a reply.")
        return EXIT_CLEAN

    print(f"\n{still_open} UNANSWERED finding(s) — a merge would bury them:")
    for bot in report:
        for comment in bot.open_findings:
            line = comment.get("line") or comment.get("original_line")
            print(
                f"  [{bot.key}] {comment.get('path')}:{line} "
                f"[id={comment.get('id')}]\n    {comment.get('html_url')}"
            )
    return 0


class _Parser(argparse.ArgumentParser):
    """An ``ArgumentParser`` whose usage errors do not look like a clean run.

    argparse exits **2** on any usage error, and 2 is ``EXIT_CLEAN`` here. So a
    gate invocation that never ran at all — a missing ``--pr`` value, a typo'd
    ``--bot``, an ISO-8601 ``--since`` pasted unquoted and split into two argv
    entries — hands its caller the same status as "every finding has a reply",
    and `/squash-merge` merges. That is the worst failure this module can have:
    silent, and triggered by a typo in a command the skills tell a human to
    copy and paste.

    Usage errors exit 1 instead, which every caller already treats as stop.
    """

    def error(self, message: str) -> NoReturn:
        self.print_usage(sys.stderr)
        print(f"error: {message}", file=sys.stderr)
        raise SystemExit(1)


def build_parser() -> argparse.ArgumentParser:
    parser = _Parser(
        description=(__doc__ or "").splitlines()[0],
    )
    parser.add_argument("command", choices=("request", "wait", "fetch", "outstanding"))
    # Not required at this level: `outstanding` sweeps every bot, so naming one
    # would be meaningless there. main() enforces it for the other commands,
    # which lets the error say *why* rather than just "expected --bot".
    parser.add_argument("--bot", default=None, choices=sorted(BOTS))
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
    command = str(args.command)
    try:
        repo = str(args.repo) if args.repo else default_repo()
        pr = int(cast("int", args.pr))
        if command == "outstanding":
            return cmd_outstanding(repo, pr, resolve_since(args))
        bot = cast("str | None", args.bot)
        if bot is None:
            raise BotReviewError(f"{command} needs --bot; only outstanding sweeps all")
        spec = BOTS[bot]
        if command == "request":
            return cmd_request(repo, pr, spec)
        since = resolve_since(args)
        if command == "wait":
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
