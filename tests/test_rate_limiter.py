"""The auth-failure rate limiter (ADR-0026 rules 1-4, defaults ADR-0051).

Unit tests over an injected clock; the request-path behavior (429 answers,
audit rows, valid-credential bypass) is covered in test_api_auth.py.
"""

from typing import Any

import pytest

from healthspan import api_security
from healthspan.api_security import (
    ADDRESS_THRESHOLD_MULTIPLIER,
    MAX_BUCKETS_PER_ADDRESS,
    PRUNE_IDLE_SECONDS,
    AuthFailureRateLimiter,
)

ADDR = "127.0.0.1"


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def test_failures_within_threshold_are_free() -> None:
    limiter = AuthFailureRateLimiter(failure_threshold=5, clock=Clock())
    for _ in range(5):
        assert limiter.register_failure(ADDR, "gui") is None


def test_backoff_starts_at_one_second_and_doubles_to_the_cap() -> None:
    clock = Clock()
    limiter = AuthFailureRateLimiter(
        failure_threshold=1, max_backoff_seconds=60.0, clock=clock
    )
    assert limiter.register_failure(ADDR, "gui") is None  # the one free failure
    for delay in [1.0, 2.0, 4.0, 8.0, 16.0, 32.0, 60.0, 60.0]:
        # Each real failure arms the next window...
        assert limiter.register_failure(ADDR, "gui") is None
        # ...and an attempt inside it is throttled with the honest remainder.
        assert limiter.register_failure(ADDR, "gui") == delay
        clock.now += delay  # serve the backoff out before the next failure


def test_throttled_attempts_never_extend_the_block() -> None:
    # The Retry-After contract: hammering a 429 does not re-arm or double
    # the window, and a client that waits the advertised time escapes.
    clock = Clock()
    limiter = AuthFailureRateLimiter(failure_threshold=1, clock=clock)
    assert limiter.register_failure(ADDR, "gui") is None
    assert limiter.register_failure(ADDR, "gui") is None  # arms 1 s
    clock.now = 0.4
    for _ in range(5):  # repeated throttled attempts, same honest remainder
        assert limiter.register_failure(ADDR, "gui") == 0.6
    clock.now = 0.4 + 0.6  # wait exactly the advertised Retry-After
    assert limiter.register_failure(ADDR, "gui") is None  # escapes to a 401


def test_buckets_are_isolated_per_name_and_address() -> None:
    clock = Clock()
    limiter = AuthFailureRateLimiter(failure_threshold=1, clock=clock)
    limiter.register_failure(ADDR, "gui")
    limiter.register_failure(ADDR, "gui")  # arms gui's bucket
    assert limiter.register_failure(ADDR, "gui") is not None
    # Another name at the same address, and the same name elsewhere: clear.
    assert limiter.register_failure(ADDR, "mcp") is None
    assert limiter.register_failure("192.168.0.7", "gui") is None


def test_name_cycling_trips_the_address_aggregate() -> None:
    limiter = AuthFailureRateLimiter(failure_threshold=1, clock=Clock())
    aggregate_threshold = 1 * ADDRESS_THRESHOLD_MULTIPLIER
    # One failure per fresh name: no single bucket ever exceeds its
    # threshold, but the address total does (ADR-0026 rule 3).
    for i in range(aggregate_threshold + 1):
        limiter.register_failure(ADDR, f"name-{i}")
    assert limiter.register_failure(ADDR, "never-seen-before") is not None
    # A different address is unaffected.
    assert limiter.register_failure("192.168.0.7", "fresh") is None


def test_a_success_never_clears_accumulated_failures() -> None:
    # Security property (ADR-0051 §1): the advisory name is unauthenticated,
    # so a legitimate success under a name must NOT drain the failures an
    # attacker deposited under that same name (both share one loopback
    # address). There is no clear-on-success path; decay is time-based only.
    clock = Clock()
    limiter = AuthFailureRateLimiter(failure_threshold=1, clock=clock)
    limiter.register_failure(ADDR, "gui")
    limiter.register_failure(ADDR, "gui")  # arms gui's bucket
    assert limiter.register_failure(ADDR, "gui") is not None
    # The limiter exposes no success hook to reset it — only reset() (admin)
    # or the idle-prune decay can clear a bucket.
    assert not hasattr(limiter, "record_success")
    assert limiter.register_failure(ADDR, "gui") is not None  # still walled


def test_bucket_count_per_address_is_capped() -> None:
    limiter = AuthFailureRateLimiter(failure_threshold=10**6, clock=Clock())
    for i in range(MAX_BUCKETS_PER_ADDRESS + 50):
        assert limiter.register_failure(ADDR, f"cycled-{i}") is None
    state: Any = limiter.__dict__["_addresses"][ADDR]
    # Overflow shares the `invalid` bucket instead of allocating new ones.
    assert len(state.buckets) <= MAX_BUCKETS_PER_ADDRESS + 1
    assert state.total_failures == MAX_BUCKETS_PER_ADDRESS + 50


def test_overflow_failures_honor_the_shared_bucket_block(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Finding: a fresh name past the bucket cap reroutes into the shared
    # 'invalid' bucket — and the reroute is resolved BEFORE the throttle
    # check, so an overflowing attempt honors that bucket's armed window
    # instead of slipping a free 401 through and then recording into it.
    # A small cap and threshold=2 keep the aggregate (5x = 10) clear of the
    # few failures used, isolating the per-bucket behavior.
    monkeypatch.setattr(api_security, "MAX_BUCKETS_PER_ADDRESS", 2)
    clock = Clock()
    limiter = AuthFailureRateLimiter(
        failure_threshold=2, max_backoff_seconds=60.0, clock=clock
    )
    for _ in range(3):  # 2 free, the 3rd arms 'invalid' to blocked_until=1.0
        limiter.register_failure(ADDR, "invalid")
    limiter.register_failure(ADDR, "filler")  # buckets now {invalid, filler} = cap
    clock.now = 0.5  # inside 'invalid's armed window (aggregate total 4 < 10)
    # A never-seen name overflows into the armed 'invalid' bucket and is
    # throttled — pre-fix it returned None (a free 401) and extended the block.
    retry = limiter.register_failure(ADDR, "brand-new")
    assert retry is not None
    assert retry == 0.5


def test_idle_buckets_are_pruned_for_a_clean_slate() -> None:
    clock = Clock()
    limiter = AuthFailureRateLimiter(failure_threshold=1, clock=clock)
    for _ in range(4):
        limiter.register_failure(ADDR, "gui")
        clock.now += 60.0  # serve out every armed window as it appears
    # Long idle: past the prune window, the ratchet is forgotten.
    clock.now += PRUNE_IDLE_SECONDS + 1.0
    assert limiter.register_failure(ADDR, "gui") is None  # fresh free failure
    assert limiter.register_failure(ADDR, "gui") is None  # arms 1 s, not 60 s
    assert limiter.register_failure(ADDR, "gui") == 1.0


def test_reset_clears_everything() -> None:
    limiter = AuthFailureRateLimiter(failure_threshold=1, clock=Clock())
    limiter.register_failure(ADDR, "gui")
    limiter.register_failure(ADDR, "gui")
    assert limiter.register_failure(ADDR, "gui") is not None
    limiter.reset()
    assert limiter.register_failure(ADDR, "gui") is None
