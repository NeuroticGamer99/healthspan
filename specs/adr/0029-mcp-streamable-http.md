# ADR-0029: MCP Transport Refresh — Streamable HTTP

## Status
Accepted

## Context and Problem Statement
ADR-0007 chose HTTP/SSE as the MCP server transport. Since then, the MCP specification (2025-03-26 revision) deprecated the HTTP+SSE transport and replaced it with **Streamable HTTP**; subsequent spec revisions continue on Streamable HTTP, and `fastmcp` supports it as its standard HTTP transport. No implementation code exists yet, so the platform can adopt the current transport before any code or deployed client depends on the deprecated one. What transport should the MCP server actually implement?

This ADR extends ADR-0007. None of its architectural reasoning changes — the MCP server remains a long-lived, process-isolated HTTP server — only the wire transport named by the decision is refreshed.

## Decision Drivers
- Implementing a transport the MCP spec has already deprecated would create migration debt on day one
- Every architectural property ADR-0007 chose HTTP/SSE *for* must be preserved: long-lived process, any-MCP-client pluggability, uniform security model, identical local/Docker/LAN deployment
- `fastmcp` (ADR-0001) supports Streamable HTTP natively; no framework change is required
- There are no deployed clients, so backwards compatibility with the deprecated transport has no beneficiaries

## Considered Options
- Streamable HTTP (current MCP spec transport)
- HTTP+SSE (as originally written in ADR-0007, now deprecated by the MCP spec)
- Both, with legacy HTTP+SSE compatibility

## Decision Outcome
Chosen option: **Streamable HTTP**, with no legacy HTTP+SSE compatibility mode.

Mechanically: a single MCP endpoint served by the MCP Server process. The client sends JSON-RPC messages via HTTP POST; the server answers each request with either a single JSON response or an SSE stream opened on that same request when it needs to stream. Session continuity uses the `Mcp-Session-Id` header per the MCP spec. `fastmcp`'s HTTP transport implements this; the deprecated SSE transport mode is not enabled.

### Positive Consequences
- The implementation starts on the spec's current transport — no day-one migration debt
- All ADR-0007 properties carry over: process isolation, client pluggability, uniform security, deployment flexibility
- Simpler server surface than the deprecated transport (one endpoint instead of separate SSE and message endpoints)

### Negative Consequences / Tradeoffs
- Very old MCP clients that only speak the deprecated HTTP+SSE transport cannot connect — acceptable, as mainstream clients moved with the spec and this platform has no deployed users
- The MCP spec is still evolving; a future revision may force another refresh (mitigated: transport choice is contained in the MCP Server process behind ADR-0006's process boundary)

## Security Requirements — Carried Over from ADR-0007

ADR-0007's five security requirements apply to Streamable HTTP unchanged, with two amendments already made elsewhere:

1. Bearer token authentication on every request — with token storage per [ADR-0026](0026-named-scoped-tokens.md) (per-client keyring, named scoped tokens; not the shared TOML config as ADR-0007 originally stated). The MCP server's default token is read-only.
   - **Static bearer, no OAuth advertisement.** The credential AI clients present to the MCP Server ([ADR-0026](0026-named-scoped-tokens.md)'s client-facing secret) is a **static bearer token**, verified by a thin custom `fastmcp` `TokenVerifier` doing the hashed `SHA-256` + `secrets.compare_digest` compare — not the built-in `StaticTokenVerifier`, which holds token plaintext. The server does **not** advertise OAuth discovery (no protected-resource metadata, no `WWW-Authenticate` steering to OAuth); a missing or invalid credential returns a plain `401`. This is deliberate on two grounds: OAuth is disproportionate for a single-user localhost resource (ADR-0026 rejected it for the same reason), and — verified against the current ecosystem (July 2026) — advertising OAuth triggers a documented client-side failure in which mainstream MCP clients ignore a configured static `Authorization` header and fall into OAuth discovery, exposing only synthetic `authenticate` tools (e.g. Claude Code [#59467](https://github.com/anthropics/claude-code/issues/59467)). Mainstream desktop-class clients (Claude Desktop, Claude Code, Cursor) can present a static bearer header to a Streamable HTTP server, so **no transport extension to this ADR is required** — static bearer is viable end-to-end.
2. Host header validation — unchanged. The MCP spec itself now *requires* origin validation for Streamable HTTP servers as DNS-rebinding defense; the platform already meets this via requirements 2 and 3.
3. CORS allowlist, default deny — unchanged.
4. Configurable binding address, default `127.0.0.1` — unchanged.
5. HTTPS required for any non-localhost binding — unchanged.

## Explicit Non-Impact

[ADR-0011](0011-event-bus.md)'s internal event stream (`GET /v1/events`) is plain SSE and is **not** MCP transport. The MCP spec deprecating its HTTP+SSE *transport* says nothing about SSE as a protocol; the GUI and Automation Host event subscriptions are unaffected by this ADR.

## Pros and Cons of the Options

### Streamable HTTP (chosen)
- Pro: current spec transport; supported by `fastmcp` and mainstream MCP clients
- Pro: single-endpoint design is simpler to secure and reason about
- Con: none specific, beyond the spec-evolution risk noted above

### HTTP+SSE (deprecated)
- Pro: literally what ADR-0007 wrote
- Con: deprecated by the MCP spec since 2025-03-26; new clients are not required to support it
- Con: implementing it now guarantees a migration later, with users attached

### Both transports
- Pro: maximal client compatibility
- Con: double the transport surface to secure and test, for a legacy mode with zero existing users

## Links
- Extends: [ADR-0007](0007-mcp-transport.md) — same decision, refreshed transport; all reasoning and security requirements carry over
- Related: [ADR-0001](0001-mcp-server-language.md) — fastmcp as the MCP server framework
- Related: [ADR-0011](0011-event-bus.md) — internal SSE event stream, explicitly unaffected
- Related: [ADR-0026](0026-named-scoped-tokens.md) — token storage and scoping as applied to the MCP server
- Related: [specs/security.md](../security.md)
- Resolves: [architecture review 2026-06-10](../reviews/architecture-review-2026-06-10.md), item 3.B
- Resolves: [architecture review 2026-07-06](../reviews/architecture-review-2026-07-06.md), item 2.6 — ecosystem check: static bearer viable, OAuth not advertised, no transport extension required
