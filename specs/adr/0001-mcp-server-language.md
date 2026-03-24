# ADR-0001: Implementation Language

## Status
Superseded by [ADR-0023](0023-distribution-mechanism.md)

## Context and Problem Statement
The platform consists of multiple processes: Core Service, MCP Server, CLI, GUI, and Import Pipeline (see ADR-0006). A single consistent language across all components reduces context-switching, enables code sharing, and simplifies the development and contribution experience. What language should the platform be implemented in?

Note: this ADR was originally scoped to the MCP server only. It has been broadened to cover the full platform as the architecture was refined.

## Decision Drivers
- MCP SDK availability and maturity
- Ecosystem depth for data ingestion, CSV/JSON parsing, and analysis tooling
- GUI framework availability (PySide6 for Qt — see ADR-0006)
- FastAPI for the Core Service HTTP layer
- Ease of open source contribution
- Distributable binary support (Nuitka for compiled output)
- Single language across all components

## Considered Options
- Node.js (TypeScript)
- Python

## Decision Outcome
Chosen option: **Python**

Python is the language for all platform components: Core Service (FastAPI), MCP Server (fastmcp), CLI (typer), GUI (PySide6), and Import Pipeline.

### Positive Consequences
- Single language across all components — no context switching, shared utilities, unified development environment
- `fastmcp` (wraps the official `mcp` SDK) provides ergonomic MCP server development
- FastAPI for the Core Service gives a high-quality async HTTP layer with automatic OpenAPI documentation
- PySide6 (Qt for Python, LGPL) for the GUI — native performance, excellent charting, cross-platform
- typer for the CLI — clean, declarative command definitions
- Rich ecosystem for data ingestion: CSV, JSON, PDF parsing, HTTP clients for external APIs
- Nuitka compiles Python to C-based executables — shippable binary without requiring Python installed
- `pip-audit` and `uv` for dependency management and security auditing

### Negative Consequences / Tradeoffs
- The official Anthropic MCP SDK is TypeScript-first; the Python SDK (`mcp` / `fastmcp`) is slightly less mature — accepted given the ecosystem tradeoffs
- Python packaging has historically been complex; mitigated by `uv` and Nuitka for distribution

## Pros and Cons of the Options

### Node.js (TypeScript)
- Pro: Anthropic's official MCP SDK is TypeScript-first and most actively maintained in that ecosystem
- Pro: Strong typing via TypeScript makes tool schemas explicit and less error-prone
- Con: No viable Qt binding — GUI would require a different framework (Electron, Tauri), adding a second language or runtime
- Con: Less natural for data ingestion scripting and analysis tooling
- Con: Would require Node.js and Python as separate runtimes if any Python tooling is used

### Python
- Pro: Single language across all components including GUI (PySide6), CLI (typer), Core Service (FastAPI), and MCP Server (fastmcp)
- Pro: Best-in-class data ecosystem for ingestion, parsing, and analysis
- Pro: PySide6 is LGPL — compatible with MIT project license when dynamically linked (default)
- Con: Python MCP SDK is less mature than TypeScript equivalent — acceptable tradeoff

## Links
- Superseded by: [ADR-0023](0023-distribution-mechanism.md) — replaces Nuitka with `uv tool install`
- Related: [ADR-0006](0006-application-architecture.md) — full process architecture
- Related: [ADR-0007](0007-mcp-transport.md) — MCP server uses fastmcp over HTTP/SSE
- Resolved from: [open-questions.md](../open-questions.md)
