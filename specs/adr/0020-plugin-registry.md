# ADR-0020: Plugin Registry / Marketplace

## Status
Proposed — stub

## Context and Problem Statement
As the plugin ecosystem grows, users need a way to discover, evaluate, and install community plugins without manually finding and copying files. A curated plugin registry (similar to Home Assistant's HACS, VS Code marketplace, or npm) would lower the barrier for both plugin authors and users.

## Decision Drivers
- The directory-scanning plugin model (ADR-0010) already supports installation by file drop — a registry is a discovery and distribution layer on top
- A registry should not be required for plugin use — it is a convenience, not a gating mechanism
- Community plugins carry different trust levels than first-party plugins — the registry must make this clear
- An official registry creates a maintenance responsibility; a community-run registry avoids this

## Decision Outcome
TBD — deferred until the plugin ecosystem has enough plugins to justify the infrastructure. The plugin architecture (ADR-0010) is designed to not require a registry; one can be added without architectural changes.

## Likely Approach When Addressed
- A curated index (JSON file in a public repository) listing plugin metadata: name, author, description, version, download URL, compatibility
- CLI command: `healthspan plugin search <query>`, `healthspan plugin install <name>`
- Trust levels: `official` (first-party), `verified` (reviewed by maintainers), `community` (unreviewed)
- Security warning displayed for non-official plugins at install time

## Design Requirement: Trust Tiers and Sandboxed Execution

The current plugin model (ADR-0010) treats all plugins as trusted-user code — they run in-process with full access to the bearer token, config, and filesystem. This is correct for self-authored and first-party plugins.

When a registry introduces community plugins from unknown authors, the trust model must differentiate. The registry design should account for:

- **Trust tiers**: `official` (first-party, full trust), `verified` (reviewed by maintainers, full trust), `community` (unreviewed, restricted by default)
- **Sandboxed execution path**: Community plugins could run in a restricted subprocess — no direct bearer token access, no filesystem access beyond their own directory, communication only via the event bus and REST API. This preserves the extensibility model while limiting the blast radius of untrusted code.
- **Promotion path**: A community plugin that is reviewed and verified can be promoted to full trust, gaining in-process execution if needed for performance.

The directory-scanning loader (ADR-0010) does not need to change. The registry layer decides which execution mode a plugin gets based on its trust tier, then places it in the appropriate directory or launches it accordingly.

This is not needed for v1 — the trusted-user model is sufficient while the user base is small and plugins are self-authored. The registry design should reserve this capability so it can be added without architectural changes.

## Links
- Related: [ADR-0010](0010-cli-plugin-model.md) — plugin discovery, loading, and security boundary
- Related: [specs/security.md](../security.md) — plugin security boundary
