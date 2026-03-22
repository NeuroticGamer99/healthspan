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
- CLI command: `biocontext plugin search <query>`, `biocontext plugin install <name>`
- Trust levels: `official` (first-party), `verified` (reviewed by maintainers), `community` (unreviewed)
- Security warning displayed for non-official plugins at install time

## Links
- Related: [ADR-0010](0010-cli-plugin-model.md) — plugin discovery and loading
- Related: [specs/security.md](../security.md) — plugin security boundary
