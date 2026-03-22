# ADR-0012: Job Abstraction for Long-Running Operations

## Status
Proposed

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

```
POST /v1/jobs  →  queued  →  started  →  progress (0–100%)  →  complete
                                                              →  failed
                                       ↑  cancelled (via DELETE /v1/jobs/{id})
```

Each state transition publishes a `job.*` event on the event bus (ADR-0011).

## REST API

```
POST   /v1/jobs                    Submit a job; returns job_id immediately
GET    /v1/jobs/{id}               Current status, progress %, result or error
GET    /v1/jobs?status=running     List jobs by status
DELETE /v1/jobs/{id}               Cancel a job (if cancellable)
```

### Job submission payload
```json
{
  "type": "import.quest_labs",
  "params": { "file": "export_2026.csv", "conflict_policy": "reject" },
  "priority": "normal"
}
```

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

**Lightweight jobs** (small imports, quick analysis) — asyncio tasks within the Core Service process. Low overhead, immediate scheduling.

**Heavyweight jobs** (large backfills, intensive computation) — separate processes spawned by the Core Service. The child process publishes progress events back via the REST API (`POST /v1/events/inbound`). The Core Service fans these out to subscribers. The child process has no direct database access — all writes go through the Core REST API.

The job type declaration (in the plugin that registers the job handler) specifies which execution model to use:

```python
context.register_service("jobs.import.quest_labs", QuestLabsImportJob(),
                          version=1, execution="heavyweight")
```

## Cancellation

Not all jobs are cancellable. A job handler declares whether it supports cancellation:
- Cancellable jobs respond to a cancellation signal by completing the current unit of work, rolling back any partial writes, and publishing `job.failed` with reason `cancelled`
- Non-cancellable jobs (e.g. schema migrations) ignore cancellation requests

## Links
- Related: [ADR-0006](0006-application-architecture.md) — process isolation
- Related: [ADR-0011](0011-event-bus.md) — job events flow through the event bus
- Related: [ADR-0004](0004-data-ingestion-strategy.md) — bulk import uses the job system
