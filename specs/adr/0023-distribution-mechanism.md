# ADR-0023: Distribution Mechanism

## Status
Accepted

## Context and Problem Statement
ADR-0001 selected Python as the implementation language and listed Nuitka as the distribution mechanism — compiling Python to a C-based executable so end users would not need Python installed. Designing the plugin architecture (ADR-0010) made clear that this approach is fundamentally unviable: Nuitka produces a compiled binary that cannot dynamically import arbitrary Python files dropped into a plugins directory at runtime. The plugin model is the primary delivery mechanism for business logic; any distribution strategy that breaks it is a non-starter. `uv tool install` installs the application into a fully isolated, self-contained environment, resolving all dependencies automatically — achieving the same user-facing goal (no Python knowledge required) without the constraint that breaks plugins.

## Decision Drivers
- End users (non-technical, health-focused) must be able to install healthspan without understanding Python environments
- Distribution must work reliably across macOS, Linux, and Windows
- Build and release process should be as simple as possible for maintainers
- Installation should be fast and reproducible
- `uv` is already a required dependency for development (ADR-0001)

## Considered Options
- Nuitka — compile Python to a C-based native executable
- `uv tool install` — install into a `uv`-managed isolated environment
- PyInstaller — bundle Python interpreter and dependencies into a single executable

## Decision Outcome
Chosen option: **`uv tool install healthspan`**

Users install with a single command. `uv` manages the isolated environment, dependency resolution, and upgrades. No compilation, no bundled interpreter, no platform-specific binary artifacts.

### Positive Consequences
- Single install command: `uv tool install healthspan` — no Python version management required by the user
- `uv` itself is a single static binary easily installed on any platform; it becomes the only prerequisite
- Upgrade path is trivial: `uv tool upgrade healthspan`
- Plugin pip dependencies (`PLUGIN_PACKAGES`, see ADR-0024) can be added to the tool environment via `uv tool upgrade healthspan --with <package>` or installed automatically by the loader at runtime
- No compilation step in CI/CD — release is publishing to PyPI
- Reproducible installs via `uv.lock`

### Negative Consequences / Tradeoffs
- Requires `uv` to be installed (replaces the "requires nothing" promise of a native binary) — mitigated by `uv`'s own trivial installation (`curl … | sh` or OS package managers)
- The installed application is not a single file; it is a managed environment — acceptable given uv's abstraction of that complexity

## Pros and Cons of the Options

### Nuitka
- Pro: Produces a true native binary — no runtime dependency at all
- Pro: Can be distributed as a single downloadable file
- Con: Compilation is slow and adds significant CI/CD complexity
- Con: Platform-specific binaries must be built and maintained for each target OS/arch
- Con: Incompatible with the dynamic plugin model (plugins are Python files loaded at runtime; a compiled binary cannot import arbitrary new Python modules)
- Con: PySide6 GUI compilation with Nuitka is fragile and version-sensitive

### `uv tool install` (chosen)
- Pro: No compilation; release is a standard PyPI publish
- Pro: Fully compatible with the runtime plugin model
- Pro: uv manages isolation, upgrades, and dependency resolution
- Con: Requires uv as a prerequisite

### PyInstaller
- Pro: Mature, widely used for Python application distribution
- Con: Same compilation and platform matrix problems as Nuitka
- Con: Notoriously fragile with dynamic imports — incompatible with the plugin model
- Con: Produces large bundles; slow startup

## Links
- Supersedes: [ADR-0001](0001-mcp-server-language.md) — replaces the Nuitka distribution decision; language choice (Python) is unchanged
- Related: [ADR-0024](0024-plugin-extensions.md) — `PLUGIN_PACKAGES` integrates with the `uv tool` environment
- Related: [ADR-0022](0022-semver.md) — version policy governs what `uv tool upgrade` delivers
