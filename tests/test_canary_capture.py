"""Unit tests for the parallel-run log-canary capture sink (conftest, ADR-0063).

The sink is exercised end-to-end by the windows-latest CI leg on every run; these
tests pin its pieces against regression without needing a real xdist run: which
process writes a capture file, what gets written (newline-separated, so a value
at a section boundary can't fuse to the next), lazy creation (so a dead sink
leaves no file and the CI glob fails closed), and the plugin hooks. The
controller-skip rule is the load-bearing one — get it wrong and the gate either
double-scans every line or (worse) scans nothing.
"""

from pathlib import Path

import pytest
from conftest import (
    CanaryCaptureSink,
    append_report_capture,
    extract_report_capture,
    resolve_canary_capture_path,
)


def _test_report(sections: list[tuple[str, str]]) -> pytest.TestReport:
    """A minimal call-phase TestReport carrying the given captured sections.
    Section-name prefixes match how real pytest populates capstdout/capstderr/
    caplog, so extract_report_capture sees exactly what it sees in production."""
    return pytest.TestReport(
        nodeid="tests/test_x.py::test_y",
        location=("tests/test_x.py", 0, "test_y"),
        keywords={},
        outcome="passed",
        longrepr=None,
        when="call",
        sections=sections,
    )


def _collect_report(sections: list[tuple[str, str]]) -> pytest.CollectReport:
    return pytest.CollectReport(
        nodeid="tests/test_x.py",
        outcome="passed",
        longrepr=None,
        result=[],
        sections=sections,
    )


class TestResolveCanaryCapturePath:
    def test_inactive_when_no_capture_dir(self) -> None:
        # The dormant case: unset CANARY_CAPTURE_DIR (ubuntu/macos CI, ordinary
        # local runs) must never write a file, whatever xdist is doing.
        assert resolve_canary_capture_path(None, "gw0", 4) is None
        assert resolve_canary_capture_path(None, None, None) is None

    def test_worker_writes_its_own_file(self) -> None:
        assert resolve_canary_capture_path("/d", "gw3", 4) == Path("/d/canary-gw3.log")

    def test_serial_run_controller_writes_main(self) -> None:
        # No xdist worker id and no worker count: the sole process writes.
        assert resolve_canary_capture_path("/d", None, None) == Path(
            "/d/canary-main.log"
        )
        assert resolve_canary_capture_path("/d", None, 0) == Path("/d/canary-main.log")

    def test_distributed_controller_stays_out(self) -> None:
        # Under -n auto the controller only relays worker reports; writing here
        # would double every captured line the workers already recorded.
        assert resolve_canary_capture_path("/d", None, 4) is None


class TestExtractReportCapture:
    def test_joins_stdout_stderr_and_log_with_newlines(self) -> None:
        report = _test_report(
            [
                ("Captured stdout call", "OUT"),
                ("Captured stderr call", "ERR"),
                ("Captured log call", "LOG"),
            ]
        )
        # A newline between the three streams, so a trailing value in one can't
        # fuse to the next stream's first character (phase sections within a
        # stream are pre-joined by pytest, as a serial tee also saw them).
        assert extract_report_capture(report) == "OUT\nERR\nLOG"

    def test_stdout_only_the_common_passing_case(self) -> None:
        # A passing test with a structlog line to stdout but no stderr/caplog:
        # no spurious leading/trailing separators around the single section.
        assert (
            extract_report_capture(_test_report([("Captured stdout call", "OUT")]))
            == "OUT"
        )

    def test_empty_when_no_captured_sections(self) -> None:
        assert extract_report_capture(_test_report([])) == ""

    def test_reads_collect_report_sections_too(self) -> None:
        # Collection reports carry the same capture properties (BaseReport).
        report = _collect_report([("Captured stdout", "COLLECT-OUT")])
        assert extract_report_capture(report) == "COLLECT-OUT"


class TestAppendReportCapture:
    def test_creates_file_and_parent_on_first_write(self, tmp_path: Path) -> None:
        # Lazy creation: the file (and its dir) appears only on a non-empty write.
        target = tmp_path / "canary-logs" / "canary-gw0.log"
        append_report_capture(target, _test_report([("Captured stdout call", "AAA")]))
        assert target.read_text(encoding="utf-8") == "AAA\n"

    def test_accumulates_reports_newline_terminated(self, tmp_path: Path) -> None:
        target = tmp_path / "canary-gw0.log"
        append_report_capture(target, _test_report([("Captured stdout call", "AAA")]))
        append_report_capture(target, _test_report([("Captured log call", "BBB")]))
        # Each report is newline-terminated, so reports never fuse across the seam.
        assert target.read_text(encoding="utf-8") == "AAA\nBBB\n"

    def test_empty_capture_leaves_no_file(self, tmp_path: Path) -> None:
        # The fail-closed guarantee: a report that captured nothing creates no
        # file, so a run that captures nothing anywhere leaves the glob empty.
        target = tmp_path / "canary-gw0.log"
        append_report_capture(target, _test_report([]))
        assert not target.exists()


class TestCanaryCaptureSink:
    def test_hooks_write_test_and_collect_reports(self, tmp_path: Path) -> None:
        target = tmp_path / "canary-gw0.log"
        sink = CanaryCaptureSink(target)
        sink.pytest_runtest_logreport(_test_report([("Captured stdout call", "RUN")]))
        sink.pytest_collectreport(_collect_report([("Captured stdout", "COLLECT")]))
        assert target.read_text(encoding="utf-8") == "RUN\nCOLLECT\n"

    def test_init_drops_a_stale_file(self, tmp_path: Path) -> None:
        # A file left by a previous run sharing the dir is removed at construction,
        # so its content can't bleed into this run's scan.
        target = tmp_path / "canary-gw0.log"
        target.write_text("stale content from a previous run", encoding="utf-8")
        CanaryCaptureSink(target)
        assert not target.exists()

    def test_init_tolerates_missing_file_and_dir(self, tmp_path: Path) -> None:
        # No stale file, and the parent dir may not exist yet — construction must
        # not raise; the file is created lazily on the first captured report.
        CanaryCaptureSink(tmp_path / "canary-logs" / "canary-gw0.log")
