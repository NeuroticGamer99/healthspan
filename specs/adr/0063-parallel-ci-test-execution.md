# ADR-0063: Parallel Test Execution on the windows-latest CI Leg

## Status
Proposed

## Context and Problem Statement
The test matrix ([ADR-0045](0045-repository-workflow-and-ci-enforcement.md) §5) runs the full suite on `ubuntu-latest`, `windows-latest`, and `macos-latest` on every code PR. All three legs run **serially** — a deliberate choice recorded in [testing-strategy.md](../testing-strategy.md) (Test Execution and Performance, "CI stays serial") because the serial run feeds the mandatory **log-canary gate** (CI Gates), which mechanizes [observability.md](../observability.md)'s "never log health data values" prohibition ([security.md](../security.md) § Logging — "No health data values in logs"). The gate scans *everything the run captured* for leaked fixture values, and the serial invocation streams all captured stdout/stderr/logs (`--capture=tee-sys --log-cli-level=DEBUG`) into one file for the scanner.

Local full runs already use `pytest -n auto` (`pytest-xdist`), taking the suite from minutes to under a minute. CI stayed serial: `pytest-xdist`'s per-worker capture and its unsupported live-CLI logging would change what reaches the scanned file and could silently weaken a security gate. This was parked as an explicit open question ([open-questions.md](../open-questions.md), "Parallelize the CI test job under xdist"), with the trigger "CI wall-clock becomes a real constraint."

That trigger has arrived on one leg. `windows-latest` runs ~3× as long as the next-slowest leg — the suite's subprocess-spawn cost (each `test_cli_*`/`test_service*` case launches the real `healthspan` entrypoint, full-stack import per spawn) is slowest on Windows (testing-strategy.md's `--durations` breakdown). Because the matrix legs run concurrently, total CI latency is that single Windows outlier. The other two legs are not the pain point.

This ADR **extends [ADR-0045](0045-repository-workflow-and-ci-enforcement.md) §5** (how the matrix executes) and reverses none of its decisions: all three OSes still run the full suite on every code PR.

## Decision Drivers
- `windows-latest` is the sole wall-clock outlier (~3×); parallelizing it is what actually shortens CI, and parallelizing the other two would push already-fast legs below a pole that is no longer the complaint.
- The no-health-values-in-logs prohibition is **platform-independent in practice**: health values flow only through cross-platform business logic (import/read/compare), so a leak is an application-code/fixture defect that surfaces identically on every OS. A serial canary run on *either* remaining leg therefore completely backstops any such **platform-independent** leak, and the parallel leg can never become its sole guardian. The residual it does *not* cover is a leak emitted **only on a Windows-specific code path** (filesystem/permissions/locking — none of which touch health values today) **and** through one of the two narrow capture-loss modes below; that conjunction is an accepted residual risk — the deferred positive control (Negative Consequences) would address its out-of-window form, while its hard-crash form is inherently uncatchable; neither is closed today.
- The gate must not be silently weakened. Any parallel-capture rework has to be *demonstrated* to still catch a planted leak, not assumed to.
- Blast radius should be minimal: confine all new machinery (xdist, the capture sink, fd-level capture) to the one leg that needs it; leave the proven serial gate byte-for-byte untouched where it already works.
- [ADR-0045](0045-repository-workflow-and-ci-enforcement.md) §4: gates fan into `ci-ok`, so changing a leg's execution never touches branch protection.

## Considered Options
1. **Parallelize `windows-latest` only, keep `ubuntu-latest`/`macos-latest` serial** (chosen) vs. **parallelize all three** vs. **leave all serial (status quo)**. All-three is simpler (one code path) but changes the capture mechanism on every leg at once and spends the change's risk where there is no wall-clock problem; status quo leaves the Windows outlier as the standing CI pole. Windows-only gets the win where it matters and keeps two untouched serial gates as the backstop.
2. **Per-worker capture sink + fd-level capture** (chosen) vs. **port `--capture=tee-sys` to xdist** vs. **per-worker `--log-file` via stdlib logging**. Measured under xdist (see Decision Outcome §2): `tee-sys` **drops spawned-subprocess stdout/stderr entirely** — once a worker's real stdout is the execnet channel, `tee-sys` (sys-level) does not capture a child process's fd-1 writes, and they are lost rather than scanned. A stdlib `--log-file` misses non-logging stdout and subprocess output too, and the app resets `root.handlers` ([logging_setup.py](../../src/healthspan/logging_setup.py)), evicting any handler pytest adds. fd-level capture (pytest's default) records in-process **and** subprocess output into each test's report, which a `pytest_runtest_logreport` sink writes to a per-worker file — *stronger* than the serial `tee-sys` path on the subprocess dimension, not merely equivalent.
3. **`-n auto`** (chosen) vs. **a fixed worker count**. `auto` tracks the runner's core count (4 on GitHub's `windows-latest`); a fixed count would rot as runner sizing changes.

## Decision Outcome

### 1. Only the `windows-latest` leg runs `pytest -n auto`
The matrix is unchanged; the two Linux/macOS legs keep the exact serial invocation and single-file `tee` capture they run today — the proven gate, untouched. `windows-latest` runs `pytest -v -n auto --log-level=DEBUG` under fd-level capture, and its scan reads two complementary sources (§2). The no-health-values-in-logs prohibition remains guarded on every OS: the serial legs by the original gate, the Windows leg by a capture that scans a superset of the serial stream. The two serial legs are a full backstop for any platform-independent leak regardless.

### 2. Two capture sources: the tee'd controller stream and a per-worker sink
Under `-n auto` a worker's real stdout *is* the execnet control channel, so the serial legs' `2>&1 | tee` cannot capture worker output — and the controller echoes captured output only for *failing* tests. Two sources together reconstruct (and exceed) what one serial `tee` scans:
- **The controller's own stream, tee'd exactly as on the serial legs** (`pytest … 2>&1 | tee parallel-output.log`). This carries the `-v` node-id lines, the failures/errors summary with rendered tracebacks (a failing assertion's `longrepr` — the likeliest leak site — lives here, *not* in any test's captured sections), the warnings summary, and xdist's crash/collection-error relays. `shell: bash`'s pipefail keeps a pytest failure fatal through the tee.
- **A per-worker sink in `tests/conftest.py`** that recovers the one thing the controller drops: each test's captured stdout/stderr/log, appended to `canary-<workerid>.log`. It also hooks `pytest_collectreport`, so a collection-time leak (a module-import side effect, a collection-error traceback) on a worker is captured too.

Key properties of the sink:
- **fd-level capture** (pytest's default — no `--capture` override): a spawned subprocess's stdout/stderr is captured into the report rather than leaking onto the worker's execnet channel and vanishing. Empirically, under `-n 2` a subprocess marker printed to inherited fd-1/fd-2 appears under fd capture and is **absent** under `tee-sys`.
- **`--log-level=DEBUG`** keeps `caplog` at the sensitivity the serial legs get from `--log-cli-level=DEBUG`; live-CLI logging (`--log-cli`) is unsupported under xdist and is not used.
- **Sections newline-joined**: captured sections and successive reports are joined/terminated with `\n`, so a value at one section's unterminated end cannot fuse to the next section's first character and slip past the scanner's digit-boundary lookarounds.
- **Controller-skip**: the sink writes on each xdist worker (authoritative for its own tests) and, only when the run is *not* distributed, on the sole process (`canary-main.log`). Under `-n auto` the controller merely relays worker reports, so it writes nothing — otherwise it would double every captured line. The rule is a pure function (`resolve_canary_capture_path`) unit-tested in `tests/test_canary_capture.py`, as are the sink's hooks (via a directly-instantiated `CanaryCaptureSink`).
- **Dormant by default**: the sink is a plugin registered from `pytest_configure` *only* when `CANARY_CAPTURE_DIR` resolves a path, so the serial CI legs and ordinary local runs (parallel or not) never load it. Only the Windows CI leg sets `CANARY_CAPTURE_DIR: canary-logs`.

### 3. The scan step fails closed
The Windows leg's canary step scans `parallel-output.log canary-logs/*.log`. Worker files are created **lazily**, on first captured write, so a run whose sink produced nothing anywhere leaves no `canary-*.log`; the glob then stays literal, `scan_log_canary.py` reports a missing file and exits non-zero, and the build fails. A dead sink cannot present as a green gate. The scan runs on test failure too (a failing test's traceback is a likely leak site), matching the serial legs.

### 4. Configuration and pins
`CANARY_CAPTURE_DIR` (env var, unset ⇒ sink dormant) is the one new knob, owned here. `pytest-xdist==3.8.0` is pinned in `ci.yml`'s `env` block per testing-strategy.md's "Workflow provenance" rule and installed on the Windows leg via `uv run --with`, matching the existing `pytest` pin pattern.

### Measured payoff
On the maintainer's 32-core Windows machine, full suite (CI-style flags): serial **372.8 s**; CI-representative `-n 4` (matching GitHub's 4-vCPU runner) **110.9 s** (~3.4×); `-n auto` (32 workers) **39 s**. The CI win tracks the runner's core count; ~3.4× brings the Windows leg roughly in line with the other legs, which was the goal.

### Positive Consequences
- CI's standing wall-clock pole (the Windows leg) drops ~3–4×, bringing the three legs into rough parity.
- The Windows leg scans a *superset* of a serial leg's **captured** stream: the tee'd controller stream (everything a serial `tee` renders) plus the per-worker sink (the captured per-test output xdist suppresses), and fd-level capture adds spawned-subprocess output the old `tee-sys` dropped. (Two narrow exceptions — out-of-window worker writes and a hard-crashed worker's buffered loss — are detailed in Negative Consequences.)
- The logged-value prohibition keeps two untouched serial guardians plus a more-complete parallel one; the change cannot silently weaken the gate.
- New machinery is confined to one leg and gated behind one env var; no branch-protection change (`ci-ok` fan-in).

### Negative Consequences / Tradeoffs
- The three legs no longer run an identical test command — a deliberate divergence, documented here and in the `ci.yml` comments, justified by the Windows-specific cost profile.
- The capture sink lives in `conftest.py` and is exercised in full only by the Windows CI leg; its helpers and hooks are unit-tested, but the assembled two-source path has one real consumer. Accepted: the serial legs remain the primary gate, and the fail-closed scan catches a silent capture break.
- `caplog`/`capstdout` overlap, and the tee'd controller stream re-carries some failing-test output the sink also records, so a record can appear more than once in the scanned corpus; harmless to the scanner (a hit is a hit) and not worth de-duplicating.
- The fail-closed glob catches a *total* capture failure (no worker file, no tee), not a *partial* one (the sink silently dropping a subset). A planted-canary positive control that asserts a known benign marker survives the round-trip would close that gap; deferred as a follow-up, with the two serial legs as the backstop meanwhile. That follow-up is also the natural trigger to **re-validate the parallel-leg capture when the E2E tests land** (Phase 4) — their long-lived spawned Core Service / MCP processes are the second, larger-scale form of the same capture-coupling concern (see [open-questions.md](../open-questions.md)); the positive control should then include an out-of-window planted marker.
- Two narrow capture-loss modes exist versus a serial `tee`: **out-of-window worker writes** (an `atexit`/`pytest_sessionfinish`/interpreter-shutdown emission) go to execnet's devnull, not to any report or the controller tee, so no source scans them; and a **hard-crashed worker** loses the in-flight test's buffered fd-capture (a serial `--capture=tee-sys` had already streamed those bytes live), the crash relay carrying only the traceback. For a *platform-independent* leak the serial legs still catch it on ubuntu/macos; the genuinely uncovered case is a leak emitted **only on a Windows-specific code path** through one of these modes — which the serial legs cannot see (they never run that path) — accepted as residual risk (no Windows-specific code touches health values today). The two modes are not equally addressable: the deferred out-of-window positive control would cover the out-of-window mode, but a hard crash destroys the worker's buffer before any control could read it — that mode is inherently uncatchable by capture and is mitigated only by the crash reddening the build (forcing a clean rerun). The residual stays accepted until both are covered.

## Links
- Extends: [ADR-0045](0045-repository-workflow-and-ci-enforcement.md) §5 — the runner matrix and how it executes; ADR-0045 gains an `Extended by: ADR-0063` navigation link, content otherwise untouched (Accepted, per governance)
- Related: [testing-strategy.md](../testing-strategy.md) — Test Execution and Performance (the section this ADR rewrites, formerly "CI stays serial") and CI Gates (the log-canary gate whose capture this reworks)
- Related: [observability.md](../observability.md) and [security.md](../security.md) § Logging — the "never log health data values" prohibition the log-canary gate mechanizes
- Related: [logging_setup.py](../../src/healthspan/logging_setup.py) — structlog routes to stdout via stdlib logging, which is why fd-level capture records the app's JSON
- Resolves: the "Parallelize the CI test job under xdist" entry in [open-questions.md](../open-questions.md)
