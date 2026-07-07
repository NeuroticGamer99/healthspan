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

The current plugin model (ADR-0010) treats all plugins as trusted-user code — they run in-process within their assigned host process (ADR-0025) with full access to that process's bearer token, config, and filesystem. This is correct for self-authored and first-party plugins.

When a registry introduces community plugins from unknown authors, the trust model must differentiate. The registry design should account for:

- **Trust tiers**: `official` (first-party, full trust), `verified` (reviewed by maintainers, full trust), `community` (unreviewed, restricted by default)
- **Sandboxed execution path**: Community plugins could run in a restricted subprocess — no direct bearer token access, no filesystem access beyond their own directory, communication only via the event bus and REST API. This preserves the extensibility model while limiting the blast radius of untrusted code.
- **Promotion path**: A community plugin that is reviewed and verified can be promoted to full trust, gaining in-process execution if needed for performance.

The directory-scanning loader (ADR-0010) does not need to change. The registry layer decides which execution mode a plugin gets based on its trust tier, then places it in the appropriate directory or launches it accordingly.

This is not needed for v1 — the trusted-user model is sufficient while the user base is small and plugins are self-authored. The registry design should reserve this capability so it can be added without architectural changes.

## Design Requirement: Publication Age Gate (Supply-Chain Cooldown)

Registry installs and updates must enforce a configurable **minimum publication age** on the plugin version being installed *and on every version in its transitive pip dependency set* (`PLUGIN_PACKAGES`, ADR-0024). A version younger than the threshold is not installed — even when the user explicitly runs a refresh or first-time install — until it has aged past the gate.

```toml
[registry]
min_release_age_days = 14   # default; user-configurable
```

**Why:** The dominant real-world supply-chain pattern is fast-burn — a maintainer account is compromised or a malicious version is published, and the ecosystem detects and yanks it within hours to days. An age gate keeps users out of that window entirely: by the time a version is installable, it has survived the period in which such attacks are typically discovered. This is freshness protection; it is complementary to (not a substitute for) the authenticity protections of the pinned catalog (ADR-0024) and hash pinning (security review item 2.7). It does not defend against patient, long-dormant attacks — that remains the role of trust tiers and review.

**Requirements:**
- The gate applies to first-time installs, updates, and dependency resolution performed at install time. Dependency versions must be resolved and age-checked at install time, then pinned, so the runtime loader (ADR-0024 loader step 4) never pulls an unvetted fresh version later. [ADR-0036](0036-plugin-package-installation-integrity.md) strengthens this pin to content level (sha256) and moves the gate's discipline upstream for catalog-governed packages: the release-time resolution that produces the hash-pinned catalog must itself enforce `min_release_age_days`.
- Override is per-invocation only (e.g. `healthspan plugin install <name> --allow-fresh`) with a prominent warning naming each version that fails the gate and its age. There is no global config option to disable the gate — setting `min_release_age_days = 0` is the deliberate, greppable opt-out.
- The version's publication timestamp must come from the registry index / package index metadata, not from the artifact itself (an attacker controls the artifact's contents, including any self-declared dates).
- A security-fix exception is legitimate (a fresh version may *fix* a vulnerability); the warning text should acknowledge this and direct the user to verify the release before overriding.

## Links
- Related: [ADR-0010](0010-cli-plugin-model.md) — plugin discovery, loading, and security boundary
- Related: [ADR-0024](0024-plugin-extensions.md) — `PLUGIN_PACKAGES` and the pinned package catalog; the age gate constrains install-time dependency resolution
- Related: [ADR-0036](0036-plugin-package-installation-integrity.md) — hash-pinned catalog; complementary integrity control (temporal vs. content), and the catalog-generation age requirement
- Related: [ADR-0025](0025-plugin-host-process-matrix.md) — host-process matrix; trust tiers are the future path to relaxing it
- Related: [specs/security.md](../security.md) — plugin security boundary
