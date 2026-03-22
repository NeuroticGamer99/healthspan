# ADR-0008: Process Lifecycle Management

## Status
Accepted

## Context and Problem Statement
The platform runs as multiple independent processes (Core Service, MCP Server, GUI, Import Pipeline, CLI — see ADR-0006). Users need a way to start, stop, and manage these processes without manually launching each one. What is the supported mechanism for process lifecycle management, and how does it extend to containerized and networked deployments?

## Decision Drivers
- Local-first deployment must be simple — a single command should bring the platform up
- Power users must be able to start individual processes manually for debugging or partial deployments
- Docker and LAN deployment must be supported without a different architecture
- Linux production deployment (systemd) should be documented as a path
- The solution should not require the user to understand process management internals to get started

## Considered Options
- Manual — user starts each process individually
- Launcher script — a single script starts and supervises all processes
- Docker Compose — container-based orchestration
- systemd units — Linux service manager integration

## Decision Outcome
Chosen option: **Launcher script as the default; Docker Compose as a supported alternative**

The launcher script is the out-of-the-box experience. Docker Compose is the supported path for containerized or multi-machine deployment. systemd is documented for users who want the platform to run as a background service on Linux.

### Positive Consequences
- Single command (`biocontext start`) brings the full stack up for new users
- No Docker required for local-first deployment
- Power users can still start individual processes directly
- Docker Compose path supports self-hosted and LAN deployment without architectural changes
- systemd path supports always-on Linux deployments

### Negative Consequences / Tradeoffs
- Launcher script is less robust than a proper process supervisor (no automatic restart on crash without additional tooling)
- Two supported deployment paths (script vs Docker) means two things to maintain and document

## Option Details

### Launcher script (default)
A Python script (`biocontext start`) that:
1. Reads the shared TOML config
2. Validates that required config values are present (generates a bearer token on first run if absent)
3. Runs any pending database migrations (`biocontext db migrate`)
4. Starts Core Service, MCP Server, and optionally GUI as subprocesses
5. Forwards stdout/stderr from each process with a process-name prefix
6. On SIGINT/SIGTERM, shuts all child processes down gracefully

Individual processes can also be started directly for partial deployments:
```
biocontext service start      # Core Service only
biocontext mcp start          # MCP Server only
biocontext gui                # GUI only
```

### Docker Compose (supported alternative)
A `docker-compose.yml` defines each process as a service. The shared TOML config is mounted as a volume. Suitable for:
- Multi-machine deployments (Core Service on one host, clients on others)
- Users who prefer container isolation
- CI/CD and testing environments

### systemd (documented path)
Unit files for each service are documented (not shipped as defaults). Suitable for Linux users who want the platform to start on boot and run as a background service. Depends on the launcher script or direct process invocation.

## First-Run Behavior
On first run, the launcher:
1. Checks for a config file; if absent, generates one with defaults and a new random bearer token
2. Checks for the database file; if absent, runs `biocontext init` to initialize encryption (generate secret key, prompt for master passphrase, prompt to save Recovery Kit — see ADR-0013), then runs the full migration sequence to create it
3. Prints the bearer token and instructions for configuring AI clients
4. Starts the stack

## Links
- Related: [ADR-0006](0006-application-architecture.md) — process isolation architecture
- Related: [ADR-0009](0009-database-migration.md) — migration runner
- Related: [ADR-0013](0013-encryption-at-rest.md) — encryption initialization on first run
