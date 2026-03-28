# ADR-0022: Version Policy (SemVer 2.0.0)

## Status
Accepted

## Context and Problem Statement
healthspan exposes multiple versioned interfaces: a REST API, a plugin API, plugin service contracts, and a distributable application. Without a declared version policy, consumers of these interfaces — plugin authors, API clients, and users upgrading the application — cannot reason about compatibility or know when a change requires action on their part.

## Decision Drivers
- Plugin authors need to know what version increment a platform change requires
- Third-party plugins declare `PLUGIN_VERSION` (ADR-0010); that field needs a scheme
- Users and package managers need predictable compatibility guarantees
- The plugin ecosystem's `PLUGIN_DEPENDENCIES` version constraints are only meaningful if the underlying versioning convention is well-defined

## Decision Outcome
Chosen option: **SemVer 2.0.0 across all versioned surfaces**

healthspan follows [Semantic Versioning 2.0.0](https://semver.org/) for its application releases and requires third-party plugins to do the same. The rule is simple: breaking changes increment the major version; everything else does not.

### Positive Consequences
- Plugin authors have a clear, unambiguous rule for when to bump major/minor/patch
- `PLUGIN_DEPENDENCIES` version constraints such as `>= 1, < 2` carry a firm compatibility guarantee
- Users and package managers can safely accept minor and patch upgrades without fear of breakage
- Consistent with the broader Python ecosystem (PEP 440, PyPI)

### Negative Consequences / Tradeoffs
- Committing to SemVer means breaking changes require a major version bump even when the change is small — no taking shortcuts by hiding breakage in a minor release
- Early development (0.x) is exempt from the no-breaking-changes rule per SemVer spec; the project should move to 1.0.0 as soon as the core interfaces stabilize

---

## Versioned Surfaces

Each surface below is governed by SemVer. The version clock for each surface is independent.

### Application release version
The top-level version of the healthspan distribution (the `version` field in `pyproject.toml`). This is what users install with `uv tool install healthspan` and what `healthspan --version` reports.

**Breaking change examples**: removing a CLI command, changing a command's interface in a non-additive way, dropping support for a database migration path, changing the config schema in a non-backwards-compatible way.

### Plugin API version
The integer in `PLUGIN_API_MIN_VERSION` / `PLUGIN_API_MAX_VERSION`. While expressed as an integer for simplicity, increments follow SemVer major semantics: the integer increments only on breaking changes to the plugin-to-platform interface (`PluginContext` API, `register` signature, required declarations).

Additive changes (new context properties, new plugin types, new helpers) do not increment the version.

### Plugin service versions
The integer passed to `context.register_service(name, service, version=N)`. Increments follow SemVer major semantics: a service version increment signals a breaking change to that service's interface. Consumers can safely depend on `>= N` within a major service version.

### Third-party plugin versions (`PLUGIN_VERSION`)
Third-party plugins declare their own version using full SemVer 2.0.0 (e.g. `"1.2.0"`). The same rule applies: breaking changes to the plugin's own interface — changes that would cause a dependent plugin or consumer to fail — must increment the major version.

First-party plugins (shipped with healthspan) do not declare `PLUGIN_VERSION`. They are versioned implicitly by the application release version and are always mutually compatible.

### REST API version
The path prefix (`/v1/`, `/v2/`, etc.) increments on breaking changes to the REST API contract. Additive changes (new endpoints, new optional fields) do not require a new prefix. See `design-rationale.md` for the full list of REST versioning conventions.

---

## What Counts as a Breaking Change

A breaking change is any change that causes a correctly-written consumer of the previous version to fail or behave incorrectly without modification. Examples by surface:

| Surface | Breaking | Not breaking |
|---|---|---|
| REST API | Remove endpoint; rename field; change field type | Add optional endpoint; add optional response field |
| Plugin API | Remove `PluginContext` property; change `register` signature | Add new optional property to `PluginContext` |
| Plugin service | Change method signature; remove method | Add new method; add optional parameter |
| CLI | Remove command; rename command; change required arguments | Add new command; add optional flag |
| Config schema | Remove required key; change key type incompatibly | Add new optional key with default |

---

## Links
- Related: [ADR-0010](0010-cli-plugin-model.md) — `PLUGIN_VERSION`, `PLUGIN_DEPENDENCIES`, plugin service versioning
- Related: [ADR-0001](0001-mcp-server-language.md) — `uv tool install` as the distribution mechanism
- Related: [specs/design-rationale.md](../design-rationale.md) — full list of versioning surfaces
- External: [semver.org](https://semver.org/) — SemVer 2.0.0 specification
