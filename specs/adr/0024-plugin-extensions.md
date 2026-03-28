# ADR-0024: Plugin System Extensions — pip Dependencies and Versioning

## Status
Accepted

## Context and Problem Statement
ADR-0010 established the plugin architecture: directory scanning, `PluginContext`, the service registry, and inter-plugin dependency declarations. As the plugin ecosystem is designed to support third-party plugins installable from outside the healthspan repository, two gaps emerged:

1. **pip package dependencies**: A plugin requiring pandas, numpy, or any other pip package has no way to declare those dependencies. The base `uv tool install` environment contains only healthspan's own dependencies.
2. **Plugin versioning**: Third-party plugins can be installed and updated independently of healthspan. Without a declared version and a compatibility contract, breaking changes in one plugin silently break its dependents.

## Decision Drivers
- Non-technical users must be able to activate plugins that need pandas or numpy without manually managing pip packages
- Expert users must be able to use packages outside the healthspan-maintained catalog at their own risk
- Plugin authors need a clear rule for when a version increment is required
- The platform must fail loudly on incompatible plugin combinations rather than silently misbehaving
- healthspan's security model (locked versions, curated dependencies) must extend to plugin-introduced packages

## Decision Outcome
Extend the plugin interface contract (ADR-0010) with three additions: `PLUGIN_VERSION`, `PLUGIN_PACKAGES`, and a versioning policy.

---

## Extension 1: `PLUGIN_VERSION`

Third-party plugins must declare their own version using SemVer 2.0.0 (see ADR-0022):

```python
PLUGIN_VERSION = "1.2.0"
```

This field is required for any plugin distributed outside the healthspan repository. First-party plugins (shipped with healthspan) are exempt — they are versioned implicitly with the platform release and are always mutually compatible.

---

## Extension 2: `PLUGIN_PACKAGES`

Plugins declare pip packages they require:

```python
PLUGIN_PACKAGES = ["pandas", "numpy"]
```

The loader installs these before calling `register()`.

### Catalog-governed packages (default)

healthspan maintains a curated catalog of approved packages at pinned versions. A package declared by name without a version is resolved from the catalog:

```python
PLUGIN_PACKAGES = ["pandas", "numpy"]   # healthspan catalog picks the version
```

All catalog-governed packages resolve to the same version across all plugins, preventing inter-plugin conflicts. The catalog is part of the healthspan release and follows the same locked-version security model as healthspan's own dependencies.

### Off-catalog packages (expert override)

An explicit version pin signals intentional deviation from the catalog:

```python
PLUGIN_PACKAGES = ["pandas==2.2.3", "some-obscure-lib==0.4.1"]
```

healthspan treats any explicit version pin, or any package absent from the catalog entirely, as an off-catalog request:

- **CLI**: warns and requires `--yes` or interactive confirmation before installing
- **GUI**: modal warning — the user must explicitly accept before the plugin activates
- **Config flag**: `allow_uncatalogued_packages = true` in the TOML config skips prompts for users who opt in globally

Version conflicts between off-catalog packages from different plugins are the user's responsibility. healthspan reports installation failures clearly but does not mediate them.

### Security boundary

`PLUGIN_PACKAGES` installation carries the same trust boundary as the plugin itself. Installing a plugin already grants arbitrary code execution. Declared package dependencies are an extension of that trust — review both the plugin and its declared dependencies before installing from an untrusted source.

---

## Extension 3: Plugin Versioning Policy

Third-party plugins must follow SemVer 2.0.0. Breaking changes to a plugin's interface — changes that would cause a dependent plugin or consumer to fail — **must** increment the major version. Minor and patch increments must remain backwards-compatible within their major version.

This makes `PLUGIN_DEPENDENCIES` version constraints meaningful. A consumer declaring `"quest.parser >= 1"` can trust that any `1.x` service is compatible. A jump to `2` signals a breaking change.

### Conflict detection

When multiple `PLUGIN_DEPENDENCIES` declarations require incompatible versions of the same service, the loader fails at startup with a clear error naming:
- The conflicting requirement
- Each plugin that declared it
- The versions in conflict

**The loader does not attempt to resolve conflicts.** Resolution is left to the user (upgrade or remove a plugin). A dependency resolver is out of scope for v1 and is documented as future work. This is a deliberate scope boundary: building a resolver before the ecosystem exists would be premature.

### Updated loader sequence

The loader steps from ADR-0010 are extended:

1. Scan `plugins_dir` for `.py` files and directories with `__init__.py`
2. Import each module header; read `PLUGIN_TYPES`, `PLUGIN_VERSION`, `PLUGIN_API_MIN_VERSION`, `PLUGIN_API_MAX_VERSION`, `PLUGIN_PACKAGES`, `PLUGIN_DEPENDENCIES`
3. Validate API version compatibility; skip and warn on mismatch
4. Install packages declared in `PLUGIN_PACKAGES` (catalog-governed silently; off-catalog with warning/confirmation)
5. Build dependency graph from `PLUGIN_DEPENDENCIES`
6. Detect cycles; fail with a clear error if found
7. Detect version conflicts in `PLUGIN_DEPENDENCIES`; fail with a clear error — do not resolve
8. Load in topological order (providers before consumers)
9. Call `register(context, api_version)` for each plugin
10. First-party plugins load first; user plugins may override built-in service registrations

### Updated declarations reference

| Declaration | Required | Purpose |
|---|---|---|
| `PLUGIN_VERSION` | Third-party only | Plugin's own version; SemVer 2.0.0 |
| `PLUGIN_API_MIN_VERSION` | Yes | Lowest platform API version the plugin supports |
| `PLUGIN_API_MAX_VERSION` | No | Highest platform API version; omit to indicate no known upper bound |
| `PLUGIN_PACKAGES` | No | pip packages required by this plugin |
| `PLUGIN_DEPENDENCIES` | No | Inter-plugin service dependencies with version constraints |
| `api_version` in `register` | N/A | Current API version passed by loader; use for conditional behavior |

---

## Links
- Extends: [ADR-0010](0010-cli-plugin-model.md) — plugin architecture; this ADR adds to the interface contract without changing the core decision
- Related: [ADR-0022](0022-semver.md) — SemVer 2.0.0 policy that `PLUGIN_VERSION` must follow
- Related: [ADR-0023](0023-distribution-mechanism.md) — `uv tool install` environment into which `PLUGIN_PACKAGES` are installed
- Related: [ADR-0020](0020-plugin-registry.md) — registry/marketplace; `PLUGIN_VERSION` feeds registry metadata
- Related: [specs/security.md](../security.md) — plugin security boundary; `PLUGIN_PACKAGES` trust model
