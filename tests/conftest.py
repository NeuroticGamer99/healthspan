"""Test-suite bootstrap: scripts/ importability, keychain isolation, helpers."""

import os
import sys
from collections.abc import Callable, Iterator
from pathlib import Path

import keyring
import keyring.backend
import keyring.errors
import pytest
from hypothesis import settings

from healthspan.config import (
    AuthConfig,
    BackupConfig,
    CliConfig,
    Config,
    ConfigSource,
    DatabaseConfig,
    LoggingConfig,
    ServiceConfig,
)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

# Hypothesis profiles (testing-strategy.md): `dev` is a fast inner loop; `ci`
# runs more examples and derandomizes so a failure reproduces deterministically.
# CI selects `ci` automatically via the CI env var GitHub Actions always sets;
# HYPOTHESIS_PROFILE overrides either way. The deadline is disabled suite-wide:
# these property targets do real work (KDF hashing, UCUM parsing) whose per-example
# timing is noise, and a one-time lazy engine build must not fail an example.
settings.register_profile("dev", max_examples=25, deadline=None)
settings.register_profile("ci", max_examples=300, derandomize=True, deadline=None)
settings.load_profile(
    os.environ.get("HYPOTHESIS_PROFILE") or ("ci" if os.environ.get("CI") else "dev")
)


class InMemoryKeyring(keyring.backend.KeyringBackend):
    """Isolated keyring backend so tests never touch the OS keychain."""

    priority = 1  # pyright: ignore[reportAssignmentType] - classproperty upstream

    def __init__(self) -> None:
        super().__init__()
        self.store: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, username: str) -> str | None:
        return self.store.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        self.store[(service, username)] = password

    def delete_password(self, service: str, username: str) -> None:
        if (service, username) not in self.store:
            raise keyring.errors.PasswordDeleteError(username)
        del self.store[(service, username)]


@pytest.fixture(autouse=True)
def fake_keychain() -> Iterator[InMemoryKeyring]:
    """Every test runs against an in-memory keychain (never the real one)."""
    previous = keyring.get_keyring()
    backend = InMemoryKeyring()
    keyring.set_keyring(backend)
    try:
        yield backend
    finally:
        keyring.set_keyring(previous)


@pytest.fixture
def make_config(tmp_path: Path) -> Callable[[], Config]:
    """An effective Config rooted in tmp_path (no file on disk needed)."""

    def _make() -> Config:
        return Config(
            config_version=1,
            database=DatabaseConfig(path=tmp_path / "healthspan.db"),
            backup=BackupConfig(
                directory=tmp_path / "backups", schedule="daily", retention_count=14
            ),
            logging=LoggingConfig(level="INFO"),
            service=ServiceConfig(
                host="127.0.0.1", port=8464, passphrase_file=None, page_cap=100
            ),
            auth=AuthConfig(failure_threshold=5, max_backoff_seconds=60),
            cli=CliConfig(token_name="cli-admin"),
            path=tmp_path / "config.toml",
            source=ConfigSource.FLAG,
            loaded_from_file=True,
        )

    return _make


@pytest.fixture
def write_file() -> Callable[[Path, str], Path]:
    """Write a UTF-8 text file and return its path (shared authoring helper)."""

    def _write(path: Path, content: str) -> Path:
        path.write_text(content, encoding="utf-8")
        return path

    return _write


# --- Log-canary capture under parallel execution (ADR-0063) --------------------
# The log-canary CI gate (testing-strategy.md CI Gates; mechanizes
# observability.md's "never log health data values" prohibition) scans everything
# the test run captured for leaked fixture health values. A serial run streamed
# one tee'd stdout/stderr into a single file for the scanner. Under `pytest -n
# auto` each xdist worker's real stdout IS the execnet control channel, so the
# controller echoes captured output only for FAILING tests — passing tests' logs
# would never reach that stream.
#
# This per-worker sink recovers exactly that gap: each test's captured
# stdout/stderr/log, appended to canary-<workerid>.log, which the CI scan reads
# alongside the controller stream ci.yml tees (ADR-0063 §2 owns the two-source
# split). fd-level capture (pytest's default) means a spawned subprocess's stdout
# is captured into the report rather than lost on the worker's execnet channel.
#
# The sink is inactive unless CANARY_CAPTURE_DIR is set, so ordinary local runs
# (parallel or not) are untouched; a non-distributed run writes canary-main.log.
# Files are created lazily on first write, so a run that captures nothing leaves
# no file and the CI scan's literal glob fails closed. See scripts/scan_log_canary.py.

# A report carries captured output whether it is a per-test or a collection report.
_CaptureReport = pytest.TestReport | pytest.CollectReport


def resolve_canary_capture_path(
    capture_dir: str | None, worker_id: str | None, numprocesses: int | None
) -> Path | None:
    """The per-process canary capture file, or ``None`` when this process must
    not write one.

    ``worker_id`` is xdist's ``PYTEST_XDIST_WORKER`` (``None`` off-worker);
    ``numprocesses`` is xdist's resolved worker count (``None``/0 when not
    distributed). A worker always writes its own ``canary-<id>.log``; the
    controller writes only when the run is *not* distributed (as
    ``canary-main.log``) — otherwise the workers already cover every test and a
    controller file would double every line it relayed.
    """
    if capture_dir is None:
        return None
    if worker_id is None:
        if numprocesses:
            return None
        worker_id = "main"
    return Path(capture_dir) / f"canary-{worker_id}.log"


def extract_report_capture(report: _CaptureReport) -> str:
    """Join a report's three captured streams — stdout, stderr, log — with
    newlines, so a value at the unterminated end of one stream can't fuse to the
    next stream's first character and slip past the scanner's digit-boundary
    checks. This separates the three *streams*, not phase sections within a
    stream: pytest already concatenates a stream's setup/call/teardown sections
    (with no separator), exactly the contiguous bytes a serial tee also saw, and
    each phase additionally fires its own logreport, so an early un-fused copy is
    always scanned regardless. The app's structlog JSON is written to stdout
    (``logging_setup.py``) so it lands in ``capstdout``; ``caplog`` additionally
    covers records emitted before the app installs its own handler. Overlap is
    harmless — a canary hit is a hit.
    """
    parts = (report.capstdout, report.capstderr, report.caplog)
    return "\n".join(p for p in parts if p)


def append_report_capture(path: Path, report: _CaptureReport) -> None:
    """Append a report's captured output to ``path``, creating it (and its parent)
    on first write. A report that captured nothing writes nothing — so a run that
    captures nothing anywhere leaves no file, and the CI scan fails closed."""
    payload = extract_report_capture(report)
    if payload:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(payload + "\n")


class CanaryCaptureSink:
    """Per-worker capture sink, registered from ``pytest_configure`` only when a
    capture path resolves — so its hooks never run on the dormant serial/local
    legs. Holding the path as instance state (not a module global) keeps a nested
    in-process pytest session from inheriting stale state and lets the hooks be
    unit-tested by instantiating the sink directly."""

    def __init__(self, path: Path) -> None:
        self._path = path
        # Drop a file left by a previous run sharing this CANARY_CAPTURE_DIR; do
        # not pre-create an empty one — that would defeat the fail-closed glob.
        path.unlink(missing_ok=True)

    def pytest_runtest_logreport(self, report: pytest.TestReport) -> None:
        append_report_capture(self._path, report)

    def pytest_collectreport(self, report: pytest.CollectReport) -> None:
        # Collection-time output — a module-import side effect, a collection-error
        # traceback — rides a CollectReport, not a TestReport; capture it too.
        append_report_capture(self._path, report)


def pytest_configure(config: pytest.Config) -> None:
    """Register the log-canary capture sink on this process, when active (ADR-0063)."""
    path = resolve_canary_capture_path(
        os.environ.get("CANARY_CAPTURE_DIR"),
        os.environ.get("PYTEST_XDIST_WORKER"),
        config.getoption("numprocesses", None),
    )
    if path is not None:
        # Pin the directory to an absolute path now, against the configure-time
        # CWD (the repo root). Resolving lazily at each write would follow a test
        # that monkeypatch.chdir()s, diverting its capture out of the scanned glob.
        config.pluginmanager.register(
            CanaryCaptureSink(path.resolve()), "canary_capture_sink"
        )
