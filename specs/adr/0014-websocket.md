# ADR-0014: WebSocket / Bidirectional Communication

## Status
Proposed

## Context and Problem Statement
The SSE event stream (ADR-0011) provides server-to-client push over HTTP. This covers the primary use case: the Core Service notifying the GUI and other clients of events. However, SSE is one-directional — clients cannot send messages back over the event channel. Some future use cases may require bidirectional real-time communication: a CLI session that streams commands and receives streaming output, a plugin process that both subscribes to events and publishes them without going through the REST API, or a GUI component that sends incremental input and receives streaming responses.

## Decision Drivers
- SSE covers the current known use cases (GUI updates, event notification)
- Bidirectional communication is not required for v1 but the architecture should not make it hard to add
- WebSocket is the standard HTTP upgrade path for bidirectional communication
- FastAPI supports WebSocket natively alongside SSE
- The transport adapter architecture (ADR-0011) already accounts for multiple transports

## Decision Outcome
Chosen option: **SSE only for v1; WebSocket deferred to v2 with architecture reserved**

SSE is sufficient for all currently known use cases. WebSocket will be added when a concrete bidirectional use case drives it — not speculatively. The transport adapter architecture already reserves the slot: adding a WebSocket outbound adapter in v2 is an additive change, not a structural one.

### When WebSocket becomes justified
- Streaming plugin process communication without REST round-trips
- Live command/response interaction in a CLI-over-network scenario
- Streaming AI response tokens directly from the MCP server to the GUI

### Positive Consequences
- No premature complexity — WebSocket adds non-trivial connection lifecycle management
- FastAPI's WebSocket support means the addition is well-understood and low-risk when the time comes
- The transport adapter pattern means the internal event bus is unchanged when WebSocket is added

### Negative Consequences / Tradeoffs
- Bidirectional plugin processes must currently use REST API calls for client-to-server communication alongside SSE for server-to-client

## Links
- Related: [ADR-0011](0011-event-bus.md) — SSE is the current outbound transport; WebSocket would be added as an additional adapter
- Related: [ADR-0006](0006-application-architecture.md) — process architecture
