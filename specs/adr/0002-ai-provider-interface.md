# ADR-0002: AI Client Interface

## Status
Accepted

## Context and Problem Statement
The MCP server exposes health data to an AI assistant for analytical conversations. The initial implementation targets Claude via the Model Context Protocol. Should the interface be designed for Claude specifically, or should the architecture support pluggable AI providers — including local LLMs for users who require full data privacy?

## Decision Drivers
- Privacy: personal health data is sensitive; some users will not accept any cloud transmission
- MCP is an open protocol — other LLMs are beginning to support it
- Abstraction adds complexity; premature abstraction is a real cost
- The project has open source intent — pluggability increases its value to others
- Local LLM quality for analytical reasoning is improving rapidly

## Considered Options
- Claude-only (no abstraction)
- MCP-based pluggability (any MCP-compatible LLM, including local)
- Full provider abstraction (MCP + direct API adapters for non-MCP providers)

## Decision Outcome
Chosen option: **MCP-based pluggability (any MCP-compatible LLM, including local)**

This decision was made in practice before it was recorded here: [ADR-0007](0007-mcp-transport.md) (Accepted) chose an AI-client-agnostic MCP transport, the glossary defines "AI client" as any MCP-compatible application, and the README asserts client agnosticism. This ADR makes the record match reality.

The MCP server is the provider interface. There is no adapter layer and no provider-specific code path — which AI client connects is a configuration concern for the user, not a platform concern. Users who require full data privacy can point a local MCP-compatible client (e.g. Ollama or LM Studio front-ends) at the same server.

### Positive Consequences
- No bespoke adapter code to design, build, or maintain — the MCP server already exists as the single interface
- Fully local operation is possible: a local MCP-compatible LLM means no health data leaves the machine
- The platform never references a specific AI product in interfaces, configuration, or documentation
- Open-source value: the project is useful to non-Claude users from day one

### Negative Consequences / Tradeoffs
- Providers that do not implement MCP are unsupported; adding one would require a new ADR introducing an adapter layer (deliberately deferred as premature)
- Analytical quality varies by client — local LLM reasoning over complex multi-step health analysis may be materially weaker than frontier hosted models; the platform does not compensate for this

## Pros and Cons of the Options

### Claude-only (no abstraction)
- Pro: Simplest implementation — no adapter layer, no interface design overhead
- Pro: Can use Claude-specific features (extended thinking, artifacts) without compatibility constraints
- Con: All health data leaves the local machine on every query
- Con: Limits the project's value to privacy-focused users and non-Claude users
- Con: Locks the project to Anthropic's pricing and availability

### MCP-based pluggability (any MCP-compatible LLM)
- Pro: MCP is an open standard — Claude, local models (Ollama, LM Studio), and others are adopting it
- Pro: Users can choose a fully local setup (e.g. Ollama + a capable open model) with no data leaving the machine
- Pro: No bespoke adapter code — the MCP server is already the interface; provider selection is a configuration concern
- Con: Local LLM quality for complex multi-step health analysis may be significantly lower than Claude
- Con: Some Claude-specific capabilities (tool use quality, reasoning) may not translate to other providers

### Full provider abstraction (MCP + direct API adapters)
- Pro: Maximum flexibility — supports providers that don't implement MCP
- Con: Significant abstraction overhead; direct API integration per provider is ongoing maintenance
- Con: Premature generalization — MCP adoption is growing and may make direct adapters unnecessary

## Links
- Related: [ADR-0007](0007-mcp-transport.md) — AI-client-agnostic MCP transport (the decision this ADR records)
- Related: [ADR-0001](0001-mcp-server-language.md)
- Related: [open-questions.md](../open-questions.md)
