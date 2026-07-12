"""The auth-failure rate limiter (ADR-0026 rules 1-4, defaults ADR-0051).

Unit tests over an injected clock; the request-path behavior (429 answers,
audit rows, valid-credential bypass) is covered in test_api_auth.py.
"""

from healthspan.api_security import ADDRESS_THRESHOLD_MULTIPLIER, AuthFailureRateLimiter

ADDR = "127.0.0.1"


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def test_failures_within_threshold_are_free() -> None:
    limiter = AuthFailureRateLimiter(failure_threshold=5, clock=Clock())
    for _ in range(5):
        assert limiter.retry_after(ADDR, "gui") is None
        limiter.record_failure(ADDR, "gui")
    assert limiter.retry_after(ADDR, "gui") is None


def test_backoff_starts_at_one_second_and_doubles_to_the_cap() -> None:
    clock = Clock()
    limiter = AuthFailureRateLimiter(
        failure_threshold=1, max_backoff_seconds=60.0, clock=clock
    )
    limiter.record_failure(ADDR, "gui")  # the one free failure
    expected = [1.0, 2.0, 4.0, 8.0, 16.0, 32.0, 60.0, 60.0]  # doubling, capped
    for delay in expected:
        limiter.record_failure(ADDR, "gui")
        retry = limiter.retry_after(ADDR, "gui")
        assert retry is not None
        assert retry == delay
        clock.now += delay  # serve out the backoff before the next failure


def test_backoff_expires_with_time() -> None:
    clock = Clock()
    limiter = AuthFailureRateLimiter(failure_threshold=1, clock=clock)
    limiter.record_failure(ADDR, "gui")
    limiter.record_failure(ADDR, "gui")  # arms a 1 s window
    assert limiter.retry_after(ADDR, "gui") == 1.0
    clock.now = 1.5
    assert limiter.retry_after(ADDR, "gui") is None


def test_buckets_are_isolated_per_name_and_address() -> None:
    limiter = AuthFailureRateLimiter(failure_threshold=1, clock=Clock())
    for _ in range(3):
        limiter.record_failure(ADDR, "gui")
    assert limiter.retry_after(ADDR, "gui") is not None
    # Another name at the same address, and the same name elsewhere: clear.
    assert limiter.retry_after(ADDR, "mcp") is None
    assert limiter.retry_after("192.168.0.7", "gui") is None


def test_name_cycling_trips_the_address_aggregate() -> None:
    limiter = AuthFailureRateLimiter(failure_threshold=1, clock=Clock())
    aggregate_threshold = 1 * ADDRESS_THRESHOLD_MULTIPLIER
    # One failure per fresh name: no single bucket ever exceeds its
    # threshold, but the address total does (ADR-0026 rule 3).
    for i in range(aggregate_threshold + 1):
        limiter.record_failure(ADDR, f"name-{i}")
    assert limiter.retry_after(ADDR, "never-seen-before") is not None
    # A different address is unaffected.
    assert limiter.retry_after("192.168.0.7", "never-seen-before") is None


def test_success_clears_the_bucket_and_its_aggregate_share() -> None:
    clock = Clock()
    limiter = AuthFailureRateLimiter(failure_threshold=1, clock=clock)
    for _ in range(3):
        limiter.record_failure(ADDR, "gui")
    assert limiter.retry_after(ADDR, "gui") is not None
    clock.now = 100.0  # past the armed window; the counter is what matters
    limiter.record_success(ADDR, "gui")
    assert limiter.retry_after(ADDR, "gui") is None
    # The cleared failures no longer count toward the address aggregate.
    limiter.record_failure(ADDR, "other")
    assert limiter.retry_after(ADDR, "other") is None


def test_reset_clears_everything() -> None:
    limiter = AuthFailureRateLimiter(failure_threshold=1, clock=Clock())
    for _ in range(10):
        limiter.record_failure(ADDR, "gui")
    assert limiter.retry_after(ADDR, "gui") is not None
    limiter.reset()
    assert limiter.retry_after(ADDR, "gui") is None
