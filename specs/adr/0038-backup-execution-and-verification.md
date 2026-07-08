# ADR-0038: Backup Execution and Verification

## Status
Proposed

## Context and Problem Statement
The [2026-07-06 architecture review](../architecture-review-2026-07-06.md) (item 2.1) found that scheduled backups — the platform's entire recovery story (ADR-0027 rejects event sourcing partly because backups carry recovery; ADR-0019 makes backup output the only sync-safe artifact) — have **no process that can run them**. ADR-0019 prescribes "a scheduled `healthspan db backup`," but: heavyweight job children never receive the key (INV-1, ADR-0012); the Automation Host has no key and cannot run a CLI command that prompts for a passphrase; and the CLI path requires a human present. The only process that can open the database while the platform runs is the Core Service.

Two adjacent defects ride along: backup verification exists only as ADR-0028's mandatory pre-rekey check (a recovery story built on unverified backups is a hope, not a story), and ADR-0012's lightweight-job definition ("asyncio tasks within the Core Service process") predates [ADR-0037](0037-core-service-concurrency-and-driver.md) and would put a driver-blocking backup on the event loop — exactly what ADR-0037 prohibits.

## Decision Drivers
- Only Core Service holds the key (INV-1); backup must run where the key already is — the key still never crosses a process boundary
- The backup directory is a cloud-sync target (ADR-0019): a sync client must never observe a partial or unverified file under a final name
- An unverified backup is worse than no backup — it defers discovery of loss to the moment of recovery
- The scheduled path must not depend on a human, the Automation Host, or any token plumbing
- Backup work blocks a thread for minutes and must never occupy the event loop (ADR-0037) or starve the small request pool
- The recovery story must reuse existing machinery (jobs, events, scheduler) rather than invent a parallel one

## Considered Options
1. **First-party lightweight job inside Core Service on a dedicated worker thread, scheduler-triggered, verify-then-publish** (chosen)
2. CLI `healthspan db backup` on an OS scheduler (cron / Task Scheduler)
3. Heavyweight job child performing the backup
4. A dedicated admin endpoint (`POST /v1/admin/backup`) outside the job system

## Decision Outcome
Chosen: **option 1.**

Option 2 requires the passphrase without a human (full auto-unlock — a security regression) and contends with Core Service's live connections. Option 3 is structurally impossible: children never receive the key (INV-1), and granting it would demolish the invariant for the one job least in need of isolation. Option 4 re-implements what the job system already provides — persistence, progress, events, history — and still needs all of option 1's threading and verification design.

### Execution locus: the `backup.database` lightweight job

- **First-party lightweight job** (`backup.database`), registered by the `healthspan` package itself — never by a plugin (ADR-0012 already restricts lightweight execution to first-party handlers; this is its canonical example).
- **Scheduled trigger:** the Core-internal scheduler (an internal component per ADR-0011, not a plugin) submits the job in-process on the configured cadence. No token, no scopes, no dependency on any other process being alive.
- **On-demand trigger:** `POST /v1/jobs` with type `backup.database` — the job type declares `admin` as its required scope. No new endpoint: status, progress (mapped from the backup API's pages-remaining), completion events, and history all come free from ADR-0012.
- **Single-flight:** at most one backup job runs at a time; a submission while one is running returns the running job's ID rather than queuing a duplicate.
- **Threading:** the job is coordinated as an asyncio task but the copy and verification run on a **dedicated worker thread** — not one of ADR-0037's eight request-pool slots, which a minutes-long copy would monopolize. The thread takes its own thread-local connection from the shared factory; ADR-0037's affinity rules hold unchanged. This ADR also corrects ADR-0012's lightweight-job wording: lightweight means *in-process and first-party*, not *on the event loop* — blocking work inside a lightweight job goes through the ADR-0037 bridge.
- **Write contention, stated honestly:** the SQLite Online Backup API restarts the copy when another connection writes to the source database. At this platform's bimodal write volume (ADR-0027) restarts are rare; worst case, a backup racing a bulk import restarts and completes after the import commits. The stepped copy (`pages=N`, sleep between steps) never starves writers.
- **Configuration:** a `[backup]` config section — schedule, destination directory, retention count. The destination is the directory ADR-0019 designates as the cloud-sync target and is subject to the same containment validation as other configured directories (ADR-0012's file-path rules).

### Verification: part of every backup, gating publication

The pipeline is ordered so no partial or unverified artifact can ever appear under a final name — the backup directory is watched by sync clients, so publication *is* the commit point:

1. **Copy** the live database via the driver's native backup API to a temporary name in the destination directory (`<final-name>.partial`).
2. **Copy the `.keyparams` sidecar** alongside (ADR-0028's travel requirement), byte-comparing the copy against the original.
3. **Verify the copy**: open it with the currently held key (raw-key PRAGMA, in-process — INV-1 intact), run `PRAGMA integrity_check` (the full check, not `quick_check` — minutes on a large database is the right price for the artifact the entire recovery story rests on, and it runs on a background thread), and confirm the copy's `schema_version` matches the live database's.
4. **Publish atomically**: rename database copy and sidecar to their final timestamped names only after verification passes — the same verify-then-commit ordering as ADR-0028's pre-rekey backup and ADR-0033's verify-then-dispose flow.
5. **On any failure**: delete the partial files, transition the job to `failed` (the `job.failed` event is visible to Automation Host rules for notification routing). A backup that cannot be verified does not exist.
6. **Retention**: after a successful publish, prune verified backups beyond the configured count, oldest first (each with its sidecar). **Pruning never runs after a failed backup** — a failing backup pipeline must not eat the good copies it is failing to replace.

**Verification defined once:** "verified" means *opens with the current key + `PRAGMA integrity_check` passes + `schema_version` matches the source*. ADR-0028's mandatory pre-rekey backup adopts this definition by reference (it previously specified only key-open).

### CLI `healthspan db backup`: the offline path, exclusive

The CLI command remains for stopped-service use — the rekey flow, restores, and migration-time snapshots. New rule: **it refuses to run while Core Service is up**, with an error pointing at the in-service job (`POST /v1/jobs`, type `backup.database`). This is the same exclusive-access discipline as the rotation commands (ADR-0028) and resolves the review's noted tension: exactly one process holds a keyed connection against the live database at any time. The CLI path performs the identical verify-then-publish pipeline.

### Positive Consequences
- Scheduled backups have an execution locus that exists: the one process already holding the key, on infrastructure (jobs, scheduler, events) already decided
- Every backup is verified before it is visible — recovery is a tested property, not an assumption; sync clients can only ever pick up self-consistent, verified, atomically published artifacts
- INV-1 untouched: the key never crosses a process boundary; backup and verification both run inside Core Service
- The single-writer story sharpens: CLI and Core Service can no longer hold live-file connections simultaneously
- Backup failure is loud (failed job + event → notifiable), and a failing pipeline cannot silently destroy older good backups

### Negative Consequences / Tradeoffs
- A minutes-long full `integrity_check` per backup — accepted deliberately: it runs on a dedicated background thread, and the alternative (unverified or `quick_check`-verified backups) undermines the artifact's entire purpose
- Core Service gains a long-lived background thread and a maintenance responsibility (retention pruning) — small, first-party, and testable
- Backups only happen while Core Service runs — accepted: when Core Service is down nothing writes to the database, so the last verified backup remains current; the CLI covers deliberate offline snapshots
- A backup racing heavy writes can restart and lengthen the run — bounded by the platform's rare-write profile, and honest in the design

## Pros and Cons of the Options

### First-party lightweight job in Core Service (chosen)
- Pro: key already present (INV-1 intact); reuses jobs/scheduler/events; verify-then-publish is enforceable in one place; no human or token dependency
- Con: background thread + pruning responsibility in Core Service; backups require Core Service to be running

### CLI on an OS scheduler
- Pro: no Core Service changes
- Con: needs the passphrase without a human — full auto-unlock is a security regression; contends with Core Service's live connections; per-OS scheduler configuration pushed onto the user

### Heavyweight job child
- Pro: uniform with other heavy work
- Con: structurally impossible — children never receive the key (INV-1); granting it would demolish the platform's central invariant

### Dedicated admin endpoint
- Pro: superficially simpler than a job type
- Con: re-implements job persistence, progress, events, and history; still needs the same thread and verification design; adds an endpoint where a job type suffices

## Consequences for Other Documents
- **ADR-0019**: "scheduled `healthspan db backup`" language redirected — the in-service `backup.database` job is the scheduled producer; the CLI is the offline path
- **ADR-0012**: lightweight-job definition corrected (in-process ≠ on the event loop; blocking work uses the ADR-0037 bridge); `backup.database` recorded as the canonical first-party lightweight job
- **ADR-0028**: pre-rekey "verified backup" adopts this ADR's verification definition; CLI exclusivity rule extended to `db backup`
- **ADR-0027**: the pre-delete backup offer names the in-service `backup.database` job — the exclusivity rule means `healthspan db backup` can never be the delete-flow mechanism (a delete implies a live service)
- **security.md**: Encryption at Rest gains the backup execution + verification paragraph; single-database-owner wording covers the new exclusivity rule
- **testing-strategy.md**: verification-gate and contention test targets
- **Architecture review 2026-07-06**: item 2.1 (both checkboxes) resolved

## Links
- Extends: [ADR-0012](0012-job-abstraction.md) — first-party lightweight job; corrects the lightweight threading definition post-ADR-0037
- Extends: [ADR-0028](0028-key-derivation-and-rotation.md) — verification definition adopted by the pre-rekey backup; CLI exclusive-access discipline extended to `db backup`
- Related: [ADR-0019](0019-multi-device-sync.md) — backup output as the only sync-safe artifact; the destination directory is the sync target
- Related: [ADR-0037](0037-core-service-concurrency-and-driver.md) — the stepped-native-backup-on-a-worker-thread primitive this ADR builds on
- Related: [ADR-0033](0033-plaintext-artifact-disposal.md) — the verify-then-commit ordering pattern
- Related: [ADR-0011](0011-event-bus.md) — the Core-internal scheduler and `job.*` events
- Related: [ADR-0027](0027-audit-trail-and-corrections.md) — the delete flow's pre-delete backup offer runs through the `backup.database` job
- Related: [specs/security.md](../security.md) — INV-1; single database owner
- Resolves: [architecture review 2026-07-06](../architecture-review-2026-07-06.md), item 2.1 — scheduled backup execution locus and routine verification
