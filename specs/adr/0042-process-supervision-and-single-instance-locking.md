# ADR-0042: Process Supervision and Single-Instance Locking

## Status
Proposed

## Context and Problem Statement
The [2026-07-06 architecture review](../architecture-review-2026-07-06.md) item 3.E found that the launcher's supervision story is weaker than the reliability claims built on it, in two related ways:

1. **The "supervised" language is not backed by a mechanism.** [ADR-0008](0008-process-lifecycle.md) concedes the launcher has "no automatic restart on crash," yet [ADR-0025](0025-plugin-host-process-matrix.md) sells the Automation Host as "a supervised resident process" whose honest reliability story justifies the fourth process. A crashed Automation Host — or Core Service — currently stays down until the user notices. Either the launcher gains restart-with-backoff, or the "supervised" language is downgraded and an OS service manager is documented as *the* reliability path.

2. **Second-instance detection via a lock file is racy and stale-prone.** [ADR-0019](0019-multi-device-sync.md) has Core Service "write a lock file alongside the database to detect a second instance." A plain lock file survives a crash or power loss as a stale file that blocks the next legitimate start, and its check-then-create window is racy. An **OS advisory lock** held for the process lifetime cannot go stale — the kernel releases it when the holder dies — and has no check-then-act race.

These are one design surface because both are about a process that owns something exclusive (the encryption key; the write connection) and what happens when it dies. They also collide with a decision this review already made: [ADR-0039](0039-startup-sequence-and-passphrase-handoff.md) has the launcher **drop its passphrase copy after handoff**, *because* ADR-0008's launcher never auto-restarts Core Service. ADR-0039 named this tension explicitly and parked it here:

> **Constraint on future supervision (T2.8):** dropping the launcher's copy is possible *because* ADR-0008's launcher does not auto-restart crashed children. A future supervisor that restarts Core Service must either re-prompt, direct users to full auto-unlock mode, or explicitly decide to retain the passphrase — that tension belongs to the supervision decision and is named here so it cannot be resolved by accident.

ADR-0008 is Accepted and immutable under governance; this ADR extends it, as ADR-0039 does.

## Decision Drivers
- The "supervised resident process" reliability claim ADR-0025 makes for the Automation Host must become true, not be quietly downgraded, wherever it can be made true safely
- Restarting Core Service is entangled with the master passphrase in a way that restarting the other processes is not — Core Service is the sole key-holder (INV-1); the Automation Host and MCP Server are keyless REST clients (ADR-0025/0026)
- ADR-0039's drop-after-handoff decision must not be silently reversed: a supervisor that retains the passphrase for its whole lifetime widens the passphrase's residency from "briefly at startup" to "the entire session"
- A crash-looping child must not be restarted forever — a bounded retry policy with loop detection is required, and each restart must be observable (`system.*` events, status)
- Single-instance enforcement must not go stale: a crash, kill, or power loss must never leave an artifact that blocks the next legitimate start
- The single-instance mechanism must work on Windows, macOS, and Linux — the platform's supported run targets
- Correctness must rest on an OS primitive, not on a best-effort identity check (the same discipline ADR-0012's job-lifetime bounds applied to token expiry vs. PID-kill)

## Considered Options

**Supervision:**
1. **No supervision; document systemd/OS service managers as the only reliability path** — downgrade the "supervised" language everywhere (the review's fallback option)
2. **Restart everything with backoff, retaining the passphrase in the launcher for its lifetime** so Core Service can always be restarted unattended
3. **Split by key custody: restart the keyless processes unconditionally; gate Core Service restart on unlock mode** (chosen)

**Single-instance lock:**
4. **Lock file** — write a file, check for its presence (ADR-0019's original sketch; racy and stale-prone)
5. **OS advisory lock held for process lifetime** — `fcntl` on POSIX, `msvcrt` on Windows (chosen)

## Decision Outcome
Chosen: **options 3 and 5.** The launcher supervises by key custody; single-instance enforcement is an OS advisory lock held for the holder's lifetime.

### Supervision, split by key custody

Supervision divides on one fact: **only Core Service holds the encryption key (INV-1).** The Automation Host and MCP Server are keyless — they hold a named REST token from config (ADR-0025/0026) and nothing that needs a human. So they can be restarted with no credential concern, while Core Service cannot.

**Keyless processes — Automation Host and MCP Server — get restart-with-backoff, unconditionally.** On unexpected exit the launcher restarts the child with bounded exponential backoff (base **1 s**, factor **2**, cap **30 s**) and reports the restart to the Core Service (`POST /v1/system/process-reports`, below), which emits `system.process.restarted`. If the child crash-loops — **5 restarts within a rolling 60 s window** — a circuit breaker declares the process failed, is reported the same way (Core emits `system.process.failed`), and retrying stops (it does not thrash forever). Status surfaces (`healthspan status`, the GUI indicator) show the degraded state. This is what makes ADR-0025's "supervised resident process" language honest: the process that claim is actually about — the Automation Host — genuinely gets supervised, and on restart it resumes from its SSE cursor and replays the retained window (ADR-0025), so a brief supervised outage loses no triggers.

**Core Service — the sole key-holder — has its auto-restart gated on unlock mode:**

- **Full-auto-unlock mode** (both key components in the OS keychain, [ADR-0013](0013-encryption-at-rest.md)): the launcher restarts Core Service with the same backoff-and-circuit-breaker policy. Core Service re-derives its key from the keychain on its own — no passphrase passes through the launcher, and ADR-0039's channel rules are untouched. Once the restarted Core Service passes its readiness probe, the launcher files a Core-restart report (below) and Core emits `system.core.restarted`.
- **Interactive / passphrase mode**: the launcher does **not** retain the passphrase to enable a silent restart. A Core Service exit surfaces loudly — the launcher's status file records the failed state (so `healthspan status` reports it), the GUI's SSE stream drops (its Core-down signal), and (if a TTY or GUI parent is attached) a re-prompt offers to restart. With no interactive parent, the stack comes down rather than silently degrading. Unattended zero-touch restart of the key-holder is, deliberately, **the job of full-auto-unlock mode or an OS service manager**, not of a launcher secretly holding the passphrase. A re-prompted restart, once ready, files the same Core-restart report as the full-auto-unlock path.

### Supervision reporting

An event about a process's supervision is a **statement of platform fact**: `system.*` is a reserved namespace, emitted only by the Core Service and never accepted through `/v1/events/inbound` for any token ([ADR-0011](0011-event-bus.md), [ADR-0026](0026-named-scoped-tokens.md) rule 2) — and the launcher holds no event-publication credential at all. Supervision facts therefore reach the bus the same way job progress does: through a purpose-built, validated endpoint from which **the Core Service itself emits** the reserved event.

**`POST /v1/system/process-reports`** accepts supervision reports from the launcher. The Core Service validates each report and emits the corresponding event, source-stamped by the bus:

| Report | Emitted event | Payload (illustrative) |
|---|---|---|
| child restarted | `system.process.restarted` | process name, restart count in window, backoff delay applied |
| circuit breaker tripped | `system.process.failed` | process name, restarts/window that tripped it, last exit code |
| child stable again (window elapsed without a restart) | `system.process.recovered` | process name |
| Core Service restart completed | `system.core.restarted` | prior exit code, downtime duration |

The endpoint applies the same payload-size cap as `/v1/events/inbound` (ADR-0011's 64 KiB → `413`); no per-token rate cap is needed beyond the auth limiter — the circuit breaker (5 restarts / 60 s, then stop) already bounds report volume structurally.

**The `launcher` token.** The endpoint declares a dedicated **`supervise`** scope, and [ADR-0026](0026-named-scoped-tokens.md)'s default set gains a `launcher` token whose only grant is that scope — deliberately not `events` (the generic-inbound publish path with its namespace allowlists; granting it would put the launcher one allowlist edit away from the laundering surface rule 2 closes), not `monitor`, not `admin`. The token lives in the launcher's keyring entry (`token:launcher`, per the client-side storage convention) and never appears in argv, environment, compose files, or unit files. [ADR-0040](0040-health-endpoint-authentication.md)'s decision is unaffected: the liveness probe stays credential-free; the launcher uses this token only for reports.

**Delivery semantics — events are notifications, the status file is the record.** Keyless-child restarts always happen while Core Service is up, so delivery normally succeeds; the launcher retries a failed POST a bounded number of times and otherwise drops it. This is acceptable because the launcher's status file (below) is the durable, authoritative supervision record — the event stream is the best-effort notification layer over it, the same honesty shape as ADR-0011's "events are best-effort beyond the replay window."

**Launcher status file — the Core-down surface.** The event bus lives inside the Core Service, so an event about Core Service being down structurally cannot exist until it is back up. The launcher therefore maintains an owner-only (0600) JSON status file alongside the lock sentinel — `<database-path>.launcher-status.json` — recording, per child: state (`running` / `restarting` / `failed` / `stopped`), restart count, breaker state, last exit code and time. `healthspan status` reads this file for supervision state (it works with Core Service down) and the credential-free liveness probes for current up/down; the GUI treats SSE disconnect as its Core-down signal. Like the PID recorded in the lock sentinel, the status file is **diagnostics, not a correctness guarantee** — it may be one write stale after a launcher crash; the advisory lock and liveness probes remain the correctness signals.

This resolves ADR-0039's parked constraint by choosing its "direct users to full auto-unlock mode" branch over its "retain the passphrase" branch. Retention would reverse the drop-after-handoff decision made in the same review and widen the passphrase's residency to the whole session, for a convenience full-auto-unlock already provides properly. The reliability tier of the key-holder therefore follows the security posture the user already chose, rather than being forced by the supervisor.

**Honest downgrade where it applies.** For the one case the launcher cannot cover — zero-touch restart of Core Service in interactive mode — this ADR does adopt the review's fallback: it documents full-auto-unlock mode and OS service managers (systemd, launchd, Windows services — ADR-0008 already sketches the systemd path) as *the* reliability path, and does not pretend the launcher supervises the key-holder unattended in that mode.

### Single-instance enforcement: an OS advisory lock

Core Service holds an **OS advisory lock on a sentinel file for its entire process lifetime**, replacing ADR-0019's plain lock file:

- **POSIX:** `fcntl.flock` (or `fcntl.lockf`) an exclusive, non-blocking lock on the sentinel file descriptor, held open for the life of the process.
- **Windows:** `msvcrt.locking` a byte range of the sentinel file, held for the life of the process.

The kernel releases the lock automatically when the holding process exits for any reason — clean shutdown, crash, `SIGKILL`, or power loss — so **the lock cannot go stale**. A second Core Service that tries to acquire it fails immediately (non-blocking) and exits with a clear "another Core Service instance is already running" error rather than corrupting the database via a second writer. This is the *local* enforcement of ADR-0019's single-writer rule; cross-machine single-writer remains ADR-0019's "one active Core Service, transfer by restore" discipline, since a local advisory lock says nothing about a second machine.

**The lock, not the file's contents, is the correctness guarantee.** The sentinel file also records the holder's PID as human-facing diagnostics; the error message resolves that PID to a process identity using `psutil` (`Process(pid).create_time()`, already an adopted dependency — see [ADR-0012](0012-job-abstraction.md)'s job-lifetime bounds) so a reused PID is not misreported as "still running." But nothing about *whether a second instance may start* depends on reading that PID: the kernel advisory lock decides it. This mirrors ADR-0012 exactly — an OS primitive is the guarantee; the `psutil` identity check is best-effort hygiene layered on top.

**Sentinel location:** `<database-path>.lock`, alongside the database (matching ADR-0019's "alongside the database" language; it travels with the data directory and is gitignored).

**Migration-phase handoff.** ADR-0039 has the launcher and Core Service hold the database *sequentially, never simultaneously* — the launcher runs migrations, closes, discards its key, then starts Core Service. The advisory lock follows the same baton: the launcher acquires the lock for the migration phase and releases it before starting Core Service, which then acquires it for its lifetime. Even the migration window is thereby single-instance-protected, and the two never contend because they never overlap.

### Positive Consequences
- ADR-0025's "supervised resident process" claim becomes true for the Automation Host and MCP Server — restart-with-backoff, Core-emitted `system.process.*` events, and crash-loop detection are real, with a reporting mechanism that keeps the event-forgery rules intact
- The key-holder's reliability follows the user's chosen unlock mode; ADR-0039's drop-after-handoff decision stays intact — no passphrase is retained for the launcher's lifetime
- Single-instance detection cannot go stale: a crash or power loss leaves no artifact that blocks the next start, and there is no check-then-create race
- Correctness rests on kernel advisory locks, not on a best-effort PID check — consistent with ADR-0012's discipline
- A crash-looping child is bounded, observable, and stops thrashing instead of restarting forever

### Negative Consequences / Tradeoffs
- Interactive-mode Core Service does not get unattended zero-touch restart — by design; users who want that run full-auto-unlock mode or an OS service manager
- The launcher grows a supervision loop with per-child backoff and circuit-breaker state (modest, but more than the current start-and-forward-stdout script)
- The advisory-lock wrapper carries the usual `fcntl`-vs-`msvcrt` cross-platform seams (byte-range vs whole-file semantics, NFS caveats), handled in a small first-party helper rather than a new dependency

## Pros and Cons of the Options

### No supervision; OS service managers only
- Pro: zero launcher complexity; a real service manager is the most robust supervisor
- Con: silently abandons the "supervised" reliability story ADR-0025 uses to justify the Automation Host; a keyless process that could safely restart stays down until noticed

### Restart everything, retaining the passphrase
- Pro: uniform policy; Core Service restarts unattended in every mode
- Con: reverses ADR-0039's drop-after-handoff decision; the passphrase lives in the launcher for the whole session, widening INV-1's exposure window for a convenience full-auto-unlock already provides

### Split by key custody (chosen)
- Pro: makes the "supervised" claim true where it can be true safely; keeps ADR-0039 intact; the key-holder's reliability tracks the user's own security posture
- Con: two policies to document (keyless vs key-holder); interactive-mode Core Service restart is deliberately not zero-touch

### Lock file
- Pro: trivial to write
- Con: survives a crash as a stale file that blocks the next start; check-then-create is racy

### OS advisory lock (chosen)
- Pro: cannot go stale (kernel releases on process death); no check-then-act race; stdlib on every supported platform
- Con: `fcntl`/`msvcrt` semantics differ and need a small cross-platform wrapper

## Links
- Extends: [ADR-0008](0008-process-lifecycle.md) — supplies the restart-with-backoff supervision ADR-0008 explicitly leaves out ("no automatic restart on crash"); launcher-as-default and process ordering unchanged
- Resolves: [ADR-0039](0039-startup-sequence-and-passphrase-handoff.md)'s parked "Constraint on future supervision (T2.8)" — chooses direct-to-full-auto-unlock over passphrase retention, so the launcher still drops its copy after handoff
- Replaces mechanism in: [ADR-0019](0019-multi-device-sync.md) — the single-instance lock file becomes an OS advisory lock held for the process lifetime; ADR-0019's single-writer *policy* is unchanged
- Backs the claim in: [ADR-0025](0025-plugin-host-process-matrix.md) — makes the "supervised resident process" language for the Automation Host honest
- Related: [ADR-0013](0013-encryption-at-rest.md) — full-auto-unlock mode is the zero-touch reliability path for the key-holder
- Related: [ADR-0012](0012-job-abstraction.md) — the OS-primitive-is-the-guarantee / `psutil`-is-best-effort discipline, and the shared `psutil` dependency
- Related: [ADR-0026](0026-named-scoped-tokens.md) — the keyless processes hold named REST tokens, which is why they restart without a credential prompt; supplies the `supervise` scope and `launcher` token behind the supervision-report endpoint
- Related: [ADR-0011](0011-event-bus.md) — the `system.process.*` / `system.core.restarted` catalog entries; the reserved-namespace rule this ADR's reporting mechanism respects
- Related: [ADR-0040](0040-health-endpoint-authentication.md) — liveness polling stays credential-free; the `launcher` token is a separate channel used only for supervision reports
- Related: [specs/security.md](../security.md) — INV-1 (sole key-holder) is the fact supervision splits on
- Resolves: [architecture review 2026-07-06](../architecture-review-2026-07-06.md), item 3.E
- Resolves: [architecture review 2026-07-07](../architecture-review-2026-07-07.md), item 1.A — supervision reporting via `POST /v1/system/process-reports` and the launcher status file
