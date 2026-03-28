# ADR-0006: Application Architecture

## Status
Accepted

## Context and Problem Statement
The platform consists of multiple concerns: a database, a query/business logic layer, an AI client interface, a data import pipeline, a desktop GUI, and scripting/automation support. How should these be structured as a system? The choice shapes every other implementation decision — language, transport, deployment, extensibility.

## Decision Drivers
- Personal health data is sensitive; the architecture must support local-first deployment with no required cloud dependency
- Power users need bare-metal access; non-technical users need quality-of-life tooling — both must be first-class
- The project has open source intent; contributors should be able to replace or extend any component without understanding the entire system
- Security is easier to build in than retrofit; a clean process boundary makes security enforcement uniform
- Deployment flexibility: local, Docker, LAN — same codebase, configuration-driven

## Considered Options
- Monolithic — single process, all components together
- MCP-server-centric — MCP server as the foundation, everything else attached
- Layered process-isolated (Unix philosophy) — core service as stable contract, all other processes as clients

## Decision Outcome
Chosen option: **Layered process-isolated architecture (Unix philosophy)**

Each process has one job and communicates over a defined, versioned interface. No process has privileged status. The GUI, MCP server, import pipeline, and CLI are all clients of the core service — none of them is the foundation.

### Positive Consequences
- Any component can be replaced, forked, or reimplemented without touching the others
- Security model is uniform: the core service enforces auth and validation for all clients identically
- Processes can be started, stopped, updated, and scaled independently
- Contributors can work on one component without understanding the full system
- The same architecture supports local, Docker, and LAN deployments via configuration

### Negative Consequences / Tradeoffs
- More moving parts than a monolith — requires a launcher and shared configuration
- Inter-process communication adds latency vs. in-process calls (acceptable at personal-data scale)
- More upfront design work to define interface contracts before implementation begins

## Architecture

All four client processes connect independently to the Core Service via HTTP + bearer token.
No client process routes through another.

```
┌────────────────┐  ┌────────────────┐  ┌────────────────┐  ┌────────────────┐
│  GUI           │  │  MCP Server    │  │  Import        │  │  CLI           │
│  (PySide6)     │  │  (fastmcp)     │  │  Pipeline      │  │  (typer)       │
└───────┬────────┘  └───────┬────────┘  └───────┬────────┘  └───────┬────────┘
        │                   │                    │                   │
        └───────────────────┴────────────────────┴───────────────────┘
                                        │
                             HTTP + bearer token
                                        │
                        ┌───────────────┴───────────────┐
                        │      Core Service (FastAPI)   │
                        │      Versioned REST API /v1/  │
                        │      Business logic           │
                        │      Database abstraction     │
                        │      Auth · CORS · Validation │
                        └───────────────┬───────────────┘
                                        │
                        ┌───────────────┴───────────────┐
                        │      Database (pluggable)     │
                        │      SQLite default           │
                        │      See ADR-0003             │
                        └───────────────────────────────┘
```

**CLI exception:** `healthspan db migrate` and `healthspan db backup` access the database
directly. All other CLI operations use the Core REST API.

## Process Responsibilities

**Core Service** — the only process that owns the database connection for runtime queries. Exposes a versioned REST API. Enforces authentication and input validation for all writes. All other processes are clients.

**MCP Server** — translates AI client tool calls into Core REST API requests. Stateless. Has no direct database access. Language-model agnostic — "AI client" not "Claude." See ADR-0007.

**GUI** — desktop client built on PySide6. Calls the Core REST API identically to any other client. Standalone — forks and contributors may substitute their own GUI without any other changes. See ADR-0002.

**Import Pipeline** — per-source importer processes. All writes go through the Core REST API bulk import endpoint, which validates and applies them transactionally. No direct database access. See ADR-0004.

**CLI** — first-class scripting layer (typer). Wraps the Core REST API for power users and automation. Two exceptions where the CLI accesses the database directly: schema migrations (`healthspan db migrate`) and hot backup (`healthspan db backup`). Supports a plugin model for user-defined extensions. See ADR-0010.

## Shared Configuration
A single TOML file (versioned) is read by all processes. Contains: service ports, bearer token, database path, binding address, log level, and plugin directory path. Processes do not hardcode any of these values. See versioning surfaces in design-rationale.md.

## Micro-Kernel Principle

The platform follows a micro-kernel architecture: the compiled core is a thin host, and business logic is delivered as first-party plugins against the same interfaces available to third-party contributors. There is no privileged distinction between shipping logic and user-contributed logic — built-in capabilities ship as plugins.

This means:
- The Core Service hosts plugin interfaces and enforces contracts (auth, validation, transactions); it does not own analytical logic
- MCP tools, import adapters, analysis functions, reference range frameworks, and named query patterns are all plugin types
- A user who disagrees with a built-in analysis function replaces the plugin — they do not fork the codebase
- The compiled executable is a distribution mechanism, not a protection mechanism; logic that matters should be visible and replaceable

See [ADR-0010](0010-cli-plugin-model.md) for the full plugin architecture.

---

## What Does Not Belong in Any Process Other Than Core Service
- Database writes (except CLI migrations and backup)
- Reference data validation (canonical biomarker names, lab IDs, etc.)
- Conflict resolution for duplicate data

## Links
- Related: [ADR-0001](0001-mcp-server-language.md) — implementation language
- Related: [ADR-0002](0002-ai-provider-interface.md) — AI client pluggability
- Related: [ADR-0003](0003-database-backend.md) — database pluggability
- Related: [ADR-0004](0004-data-ingestion-strategy.md) — import pipeline
- Related: [ADR-0007](0007-mcp-transport.md) — MCP server transport
- Related: [ADR-0008](0008-process-lifecycle.md) — process lifecycle management
- Related: [ADR-0010](0010-cli-plugin-model.md) — CLI plugin system
