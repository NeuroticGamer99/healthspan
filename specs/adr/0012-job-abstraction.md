# ADR-0012: Job Abstraction for Long-Running Operations

## Status
Accepted

## Context and Problem Statement
Some operations — large bulk imports, multi-year data backfills, complex analytical computations — take seconds to minutes. Running these synchronously in a REST endpoint blocks the client and provides no progress visibility. The GUI must not freeze waiting for completion. What is the architecture for submitting, tracking, and receiving results from long-running operations?

## Decision Drivers
- The GUI must remain responsive during long-running operations
- Users need progress visibility — not just "working" but how far along
- Operations must survive a GUI restart — a job submitted and then the GUI closed should still complete
- Operations must be cancellable
- Large operations should run detached from the Core Service process where appropriate (heavy imports, intensive analysis)
- Results and errors must be retrievable after completion, not just at the moment of completion

## Considered Options
- Synchronous REST — block until complete, return result
- Async REST with polling — submit returns a job ID; client polls for status
- Async REST with event-driven progress — submit returns a job ID; progress arrives via event bus (ADR-0011)
- Hybrid — polling as fallback, events as primary

## Decision Outcome
Chosen option: **Hybrid — event-driven progress as primary, REST polling as fallback**

`POST /v1/jobs` submits a job and returns a job ID immediately. Progress and completion arrive via the event bus (`job.progress`, `job.complete`, `job.failed`). `GET /v1/jobs/{id}` provides current status for clients not subscribed to the event stream, and for querying completed job results after the fact.

### Positive Consequences
- GUI submits and returns to responsive state immediately
- Progress events drive real-time progress bars without polling
- Jobs survive GUI restarts — status is queryable at any time
- Detached execution model naturally supports heavy operations in separate processes
- Cancellation is a first-class operation: `DELETE /v1/jobs/{id}`

### Negative Consequences / Tradeoffs
- More complex than synchronous endpoints — appropriate only for genuinely long-running operations; short operations should remain synchronous
- Job state must be persisted (in the database) to survive process restarts

## Job Lifecycle

```text
POST /v1/jobs  →  queued  →  running  →  progress (0–100%)  →  complete
                                                              →  failed
                                       ↑  cancelled (via DELETE /v1/jobs/{id})
```

A job's status is one of exactly four states — `queued`, `running`, `complete`, `failed`. Each state transition publishes a `job.*` event on the event bus (ADR-0011); the event names `job.started` and `job.complete` (ADR-0011 catalog) mark the transitions *into* the `running` and `complete` states — the event vocabulary and the state vocabulary are deliberately distinct. `failed` carries a reason distinguishing an ordinary handler failure from `cancelled` (cooperative cancel), `timed_out` (a liveness or wall-clock bound fired), and `interrupted` (a startup sweep reclaimed a job orphaned by a prior Core Service exit) — see [Job Lifetime Bounds](#job-lifetime-bounds).

## REST API

```text
POST   /v1/jobs                    Submit a job; returns job_id immediately
GET    /v1/jobs/{id}               Current status, progress %, result or error
GET    /v1/jobs?status=running     List jobs by status
DELETE /v1/jobs/{id}               Cancel a job (if cancellable)
```

### Job submission payload

```json
{
  "type": "import.quest_labs",
  "params": { "file": "quest/export_2026.csv", "conflict_policy": "reject" },
  "priority": "normal"
}
```

`file` is a **relative path resolved inside a configured import directory** — see File Path Validation below. Absolute paths and paths escaping the configured directories are rejected.

### Job status response

```json
{
  "id": "uuid",
  "type": "import.quest_labs",
  "status": "running",
  "progress": 42,
  "submitted_at": "2026-03-21T14:00:00Z",
  "started_at": "2026-03-21T14:00:01Z",
  "completed_at": null,
  "result": null,
  "error": null
}
```

## Job Persistence

Job state is stored in a `jobs` table in the database. This means:
- Jobs survive Core Service restarts
- Completed job results are queryable after the fact
- Job history is auditable

Jobs older than a configurable retention period are pruned automatically.

## Execution Model

**Lightweight jobs** (small internal operations) — run within the Core Service process. Low overhead, immediate scheduling. Restricted to **first-party internal handlers** shipped in the `healthspan` package: plugin-provided handler code never executes in the Core Service process (ADR-0025, INV-2). *Lightweight* means in-process and first-party, **not** on-the-event-loop: the job is coordinated as an asyncio task, but any blocking work — database access above all — goes through [ADR-0037](0037-core-service-concurrency-and-driver.md)'s threadpool bridge (or a dedicated worker thread for minutes-long work that would monopolize a request-pool slot). The canonical example is the `backup.database` job ([ADR-0038](0038-backup-execution-and-verification.md)): in-process because only Core Service holds the key, on a dedicated worker thread because the copy and verification block for minutes.

**Heavyweight jobs** (large backfills, intensive computation, and **all plugin-provided handlers**) — separate processes spawned by the Core Service:

- Children are created with the multiprocessing `spawn` start method, set explicitly on all platforms — never `fork`. A forked child inherits a copy of the parent's memory, including the derived encryption key (violating ADR-0025 INV-1); a spawned child starts from a fresh interpreter. Do not rely on platform defaults (`fork` is the historical default on Linux).
- The child receives an ephemeral, single-job bearer token for Core REST API access — minted at spawn, scoped to the job type's declared scopes, delivered via stdin, expired when the job reaches a terminal state (ADR-0026). It never receives the encryption key and has no direct database access — all reads and writes go through the REST API.
- The child reports progress via the dedicated `POST /v1/jobs/{id}/progress` endpoint — its single-job token is bound to that job ID. The Core Service validates the report, emits the corresponding `job.progress` events, and fans them out to subscribers. Job children cannot publish arbitrary events (ADR-0026).

The job type declaration (in the plugin that registers the job handler) specifies which execution model to use:

```python
context.register_service("jobs.import.quest_labs", QuestLabsImportJob(),
                          version=1, execution="heavyweight")
```

For plugin-registered handlers, `execution="heavyweight"` is mandatory — the loader rejects a plugin-provided handler declaring lightweight execution. Handler *registrations* reach the Core Service as data (job type name, execution declaration, owning plugin); the handler code itself is loaded only by the spawned child process at execution time, subject to ADR-0025's host allowlists. The Core Service orchestrates job metadata; it never imports handler code.

The number of heavyweight children running concurrently is capped, and every heavyweight child is bounded by an execution timeout and a liveness heartbeat — see [Job Lifetime Bounds](#job-lifetime-bounds).

## File Path Validation

A caller-supplied filesystem path in a job payload is a path-traversal primitive: any token holding a job-submission scope — including a prompt-injected MCP client — could point an import job at `~/.ssh/id_rsa`, the `.keyparams` sidecar, or the database file itself, and leak content through parse-error echoes or by making it queryable as imported "data." A job type taking an *output* path (export jobs, ADR-0015) would be an arbitrary-file-**write** primitive, which is worse. The rules below therefore apply to every file-typed job parameter, read side and write side.

### Containment against configured roots

The config declares the only directories job file params may reach:

```toml
[jobs.files]
import_dirs = ["~/Healthspan/imports"]   # read side
export_dir  = "~/Healthspan/exports"     # write side
```

- File params are **relative paths only**. Absolute paths are rejected at validation, before any filesystem access.
- The server resolves the param against each allowed root with `Path.resolve()` — which collapses `..` *and* follows symlinks, so a symlink inside an import directory pointing outside it fails containment on the resolved real path — and requires `resolved.is_relative_to(resolved_root)`.
- Regular files only; special files are rejected.

### Validation is a framework concern

A job type's params schema declares a field as file-typed (`import_file`, `export_file`). The job framework performs containment validation centrally and hands the handler a **validated absolute path** — handler code never sees the raw caller string. This keeps the control structural: a plugin-provided handler cannot reintroduce the vulnerability, because raw paths never reach it.

Two hardenings ride along:

- **Re-validation at open time.** The heavyweight child re-runs the containment check when it opens the file — the file can be swapped between submission-time validation and child spawn (TOCTOU narrowing; the framework helper does this, not handler discipline).
- **No existence oracle.** A rejected path produces the same error — `not within a configured import directory` — whether the out-of-bounds target exists or not. Rejection happens without stat-ing the target.

### Ad-hoc local files: upload content, don't pass paths

`healthspan import ~/Downloads/export_2026.csv` must keep working, but the file is not in an import directory and the CLI cannot be exempted — the server cannot distinguish a CLI token used by the human from one used by injected instructions. The split:

- **Ad-hoc local imports upload content**: the CLI reads the file itself (as the user, under the user's own filesystem authority) and sends the bytes through the bulk import endpoint (ADR-0004). No server-side path involved.
- **Path-based file params are for automation flows** where the file already lives in a configured import directory — the Automation Host's watch-folder importer being the canonical case. A path crosses the API boundary only when the path is the point.

## Cancellation

Not all jobs are cancellable. A job handler declares whether it supports cancellation:
- Cancellable jobs respond to a cancellation signal by completing the current unit of work, rolling back any partial writes, and publishing `job.failed` with reason `cancelled`
- Non-cancellable jobs (e.g. schema migrations) ignore cancellation requests

## Job Lifetime Bounds

ADR-0026's ephemeral job token "expires automatically when the job reaches a terminal state." That guarantee is only as strong as the guarantee that every job *reaches* one. A heavyweight child that hangs, wedges, or is orphaned by a Core Service crash never reaches a terminal state on its own — so its single-job token (potentially `import`-scoped) would live indefinitely. The bounds below ensure every job terminates, and terminates *observably*, so the token expiry always fires.

These bounds are primarily about **heavyweight children** — the case with a standing credential to bound. A lightweight in-process job holds no token; the wall-clock cap still applies to it as resource hygiene, but the liveness and kill machinery below concerns child processes.

### Liveness heartbeat — the primary bound

The framework's child-side reporting helper emits a heartbeat to `POST /v1/jobs/{id}/progress` on a fixed interval (**default 15 s**), independent of whether progress percent advanced — so a job doing a long, quiet unit of work still proves liveness. The heartbeat fires from a **dedicated daemon thread** in the child, not the work thread: a CPU-bound analysis step must not starve the heartbeat and read as a hang. The Core Service records `last_heartbeat_at`; a child silent past the **liveness deadline (default 60 s — three missed beats, tolerating GC pauses and transient stalls)** is transitioned to `failed` (reason `timed_out`), which expires the token. This is the load-bearing bound: it catches a crashed child, a killed child, and a wedged event loop identically.

### Wall-clock cap — the backstop

A heartbeat alone does not catch a job that is *alive and beating but never finishing* — a stuck-but-looping handler keeps its heartbeat thread running. Each job type may therefore declare a maximum wall-clock runtime; a job exceeding it is force-killed and marked `failed` (reason `timed_out`). Job types that declare none inherit a framework fallback (**default 60 min**), deliberately generous — a backstop, not a working limit.

### Forced kill and escalation

Two paths terminate a child from the outside:

- **Cooperative cancellation** (`DELETE /v1/jobs/{id}` against a *responsive* child) follows the Cancellation contract above — the child rolls back partial work and publishes `job.failed` with reason `cancelled`.
- **Forced kill** (a timeout, or cancellation of a child that does not respond within the grace period) escalates: `SIGTERM` → grace period → `SIGKILL` on POSIX; a forceful terminate on Windows, which has no graceful process signal — acceptable, because a force-killed target is already hung. A heavyweight child holds no direct database handle — every write crosses the REST API in a transaction — so a forced kill's blast radius is bounded to whatever already committed through the API; no half-written row is left behind in the child.

### Startup sweep

On start, **before** the Core Service accepts any new job submission or mints any new token, it sweeps the `jobs` table: every job persisted in a non-terminal state (`queued`, `running`) from a previous run is transitioned to `failed` (reason `interrupted`), expiring its token. Sweeping *first* closes the window in which a stale token and a freshly minted one could coexist ambiguously. Swept jobs are not auto-resumed — jobs are not checkpointed; the `interrupted` reason distinguishes them from `cancelled` and `timed_out` so the user resubmits deliberately.

A spawned child can outlive a Core Service *crash* — on POSIX it is reparented to init and keeps running with a live token. Marking its job `failed` **expires the token**: the child's next API call gets 401, which neutralizes it on every platform identically. That is the correctness guarantee. As best-effort hygiene the sweep also persists each child's PID and spawn identity and attempts to kill an orphan directly, guarded against PID reuse by comparing the OS-reported process creation time (via `psutil` — see below) before signalling; if the identity cannot be confirmed it skips the kill rather than risk signalling an unrelated reused PID. Reclaiming the process sooner is a convenience; token expiry is what makes the orphan harmless.

### Concurrency cap

A configurable **maximum number of concurrent heavyweight children (default 2)** bounds resource use and, incidentally, token proliferation. At personal single-user scale the realistic overlap is one import running alongside one analysis job, or two imports (labs + CGM) submitted together; a submission beyond the cap stays `queued` until a slot frees. Each child is a full `spawn` interpreter and an intensive-analysis child is CPU-bound, so the default stays low to keep the machine responsive during interactive use.

### Configuration

```toml
[jobs.limits]
heartbeat_interval = "15s"   # child emits a liveness beat this often
liveness_deadline  = "60s"   # no beat within this window → failed (timed_out)
default_wall_clock = "60m"   # fallback cap for job types that declare none
kill_grace         = "10s"   # SIGTERM → (grace) → SIGKILL on forced kill
max_heavyweight    = 2       # concurrent heavyweight children; excess queues
```

Individual job types may declare their own (typically shorter) wall-clock cap; the liveness-heartbeat parameters are framework-global.

### Dependency: `psutil`

The orphan-reaping PID-reuse guard needs each process's creation time, for which there is **no uniform standard-library call** across Windows, macOS, and Linux (`/proc` on Linux, `kinfo_proc` on macOS, `GetProcessTimes` on Windows). The platform adopts **`psutil`** (`Process(pid).create_time()`) for this — a long-established, cross-platform process utility whose own maintainers exercise the macOS path this project cannot test on local hardware. `psutil` is also the natural home for the launcher-supervision process controls in review item 3.E, so it is a shared dependency rather than one this decision introduces alone. Correctness never rests on it: token expiry bounds the orphan on every platform with no dependency at all; `psutil` only sharpens the best-effort reclaim.

## Links
- Constrained by: [ADR-0025](0025-plugin-host-process-matrix.md) — plugin-provided handlers never execute in the Core Service process; children are spawned, never forked
- Related: [ADR-0026](0026-named-scoped-tokens.md) — ephemeral single-job tokens; job submission requires the job type's declared scopes. The lifetime bounds above guarantee every job reaches the terminal state that expires its token, so a hung or orphaned child cannot keep one alive
- Extended by: [ADR-0038](0038-backup-execution-and-verification.md) — corrects the lightweight threading definition post-ADR-0037; adds the `backup.database` first-party lightweight job
- Related: [ADR-0006](0006-application-architecture.md) — process isolation
- Related: [ADR-0011](0011-event-bus.md) — job events flow through the event bus
- Related: [ADR-0004](0004-data-ingestion-strategy.md) — bulk import uses the job system; ad-hoc local imports upload content through it rather than passing server-side paths
- Related: [ADR-0015](0015-data-export.md) — export output paths are file-typed params subject to the containment rules above
- Related: [specs/security.md](../security.md) — file path validation under Input Validation
- Resolves review item 2.8 from [architecture-review-2026-06-10.md](../reviews/architecture-review-2026-06-10.md)
- Resolves review item 2.3 from [architecture-review-2026-07-06.md](../reviews/architecture-review-2026-07-06.md) — job lifetime bounds so ephemeral tokens cannot live forever
