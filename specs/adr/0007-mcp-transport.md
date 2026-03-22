# ADR-0007: MCP Server Transport

## Status
Accepted

## Context and Problem Statement
The MCP server exposes health data tools to AI clients via the Model Context Protocol. MCP supports multiple transport mechanisms. Which transport should this project use, and what are the security requirements that apply to it?

## Decision Drivers
- The architecture requires process isolation (ADR-0006) — the MCP server is a separate process from the core service
- The transport must be compatible with AI clients beyond the initial development target, supporting the pluggable AI interface goal (ADR-0002)
- Health data is sensitive; the transport must be secured against unauthorized access and browser-based attacks (DNS rebinding)
- The same transport must work for local, Docker, and LAN deployments without architectural changes
- Configuration-driven binding address (local or network) must not require different transport implementations

## Considered Options
- stdio (subprocess transport)
- HTTP/SSE (persistent HTTP server with Server-Sent Events)

## Decision Outcome
Chosen option: **HTTP/SSE**

### Positive Consequences
- Consistent with the process-isolated architecture — the MCP server is a long-lived process, not a subprocess of the AI client
- Any MCP-compatible AI client can connect, regardless of whether it supports subprocess launching
- Security model is uniform with the rest of the architecture (bearer token, host header validation, CORS)
- Works identically for local, Docker, and LAN deployments — only the binding address changes
- The MCP server can be restarted independently of the AI client session

### Negative Consequences / Tradeoffs
- More configuration than stdio (port, auth token) — mitigated by the shared TOML config file
- Requires the MCP server to be running before the AI client connects — mitigated by the process launcher (ADR-0008)
- HTTP/SSE is slightly more complex than stdio — justified by the architectural benefits

## Pros and Cons of the Options

### stdio (subprocess transport)
- Pro: Zero configuration — AI client launches the MCP server as a subprocess automatically
- Pro: Inherently local — no network socket, no auth required
- Con: Incompatible with process isolation — the MCP server lifecycle is controlled by the AI client, not the platform
- Con: Only works with AI clients that support subprocess MCP servers; limits pluggability
- Con: Cannot be used for Docker or network deployments without a different transport anyway
- Con: One AI client session = one MCP server process; no persistent server state

### HTTP/SSE
- Pro: Process-isolated — MCP server runs independently of the AI client
- Pro: Compatible with any MCP-capable AI client (Claude Desktop, Claude Code, local LLMs, custom clients)
- Pro: Uniform security model — same bearer token and host validation as the Core REST API
- Pro: Supports all deployment targets without transport changes
- Con: Requires explicit configuration and a running server before connection

## Security Requirements for HTTP/SSE Transport

These requirements apply regardless of binding address:

1. **Bearer token authentication** — every request must carry a valid `Authorization: Bearer <token>` header. Token is generated at first run and stored in the shared TOML config. Browsers cannot send custom headers cross-origin without CORS preflight, which the server denies for unknown origins — this breaks DNS rebinding attacks.

2. **Host header validation** — the server rejects any request whose `Host` header does not match an expected value (configured in TOML). A second line of defense against DNS rebinding.

3. **CORS allowlist** — only explicitly configured origins are permitted. Default: deny all.

4. **Binding address is configurable** — defaults to `127.0.0.1` (localhost only). Set to `0.0.0.0` for Docker or LAN deployment. Security is enforced at the application layer regardless of binding address.

5. **HTTPS** — required for any non-localhost binding. Recommended for localhost in development (mkcert). Required for LAN deployment.

## AI Client Terminology
The MCP server interface must not reference any specific AI product by name in configuration keys, tool descriptions, or documentation. "AI client" is the correct term. Any MCP-compatible client is a valid consumer.

## Links
- Related: [ADR-0002](0002-ai-provider-interface.md) — AI client pluggability
- Related: [ADR-0006](0006-application-architecture.md) — process isolation architecture
- Related: [ADR-0008](0008-process-lifecycle.md) — process lifecycle management
- Related: [specs/security.md](../security.md)
