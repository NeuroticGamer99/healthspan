# ADR-0010: Plugin Architecture

## Status
Accepted

## Context and Problem Statement
The platform needs to be extensible without requiring users to fork the repository, build from source, or understand the full system. Beyond simple command-line interface (CLI) command additions, the plugin system must support extending the AI interface, adding import adapters, contributing analysis functions, and providing services that other plugins can depend on. How should the plugin architecture be designed to support this?

## Decision Drivers
- Extensions must be installable by dropping a file or directory into a plugins folder — no build step, no package manager
- The line between first-party shipping logic and user-contributed logic should be intentionally blurred — built-in capabilities ship as first-party plugins against the same interfaces
- Plugins must be able to provide services that other plugins depend on, enabling plugin ecosystems to develop
- Each plugin type has distinct responsibilities; interfaces should be separate and purpose-fit, with structural similarity where natural
- The architecture must not lock in an inheritance hierarchy prematurely — patterns should emerge from real implementations
- Security boundary must be clearly documented: plugins are trusted-user code, not sandboxed extensions

## Considered Options
- No plugins — CLI is closed; users script against the REST API directly
- Python entry points (setuptools) — plugins are installed Python packages
- Directory scanning — CLI scans a configured `plugins/` directory and loads `.py` files or packages

## Decision Outcome
Chosen option: **Directory scanning with plugin packages, a shared PluginContext, and a service registry**

Users place `.py` files or directories (plugin packages) in the plugins directory. The loader discovers them at startup, validates compatibility, resolves dependencies, and loads them in dependency order. All plugin types share a common `PluginContext` parameter and a trivial base class. The service registry built into `PluginContext` enables plugins to provide APIs that other plugins consume.

### Positive Consequences
- Zero friction for contributors: write a Python file or package, place it in the plugins directory, capability appears
- First-party built-in functionality ships as plugins against the same interfaces — no privileged distinction
- Provider plugins enable rich ecosystems (e.g. a Quest parser plugin used by multiple import adapter plugins)
- Separate interfaces per plugin type keep each interface focused and independently evolvable
- No build tooling required — works with any Python environment that can run the platform

### Negative Consequences / Tradeoffs
- Plugins execute arbitrary Python code — intentional, but requires clear security documentation
- Dependency ordering at load time adds complexity to the loader
- Each plugin type requires its own interface documentation and test helpers

---

## Plugin Unit: File or Package

The loader supports both forms:

- **Single file** (`my_plugin.py`) — suitable for simple plugins with one type and limited code
- **Package** (`my_plugin/` directory with `__init__.py`) — suitable for composite plugins, provider plugins with multiple services, or any plugin complex enough to benefit from internal structure

The package form is the natural choice for anything providing a named suite of services, where the filesystem name and the service namespace should match (`quest/` registers services under `quest.*`).

---

## Plugin Interface Contract

### Base class

Every plugin inherits from a trivial base class. It provides no behavior in v1 — its purpose is to reserve the seam for future shared behavior without a breaking change:

```python
from healthspan.plugin import HealthspanPlugin

class MyPlugin(HealthspanPlugin):
    pass
```

Inheriting from `HealthspanPlugin` is a declaration of intent. When common patterns emerge across plugin types, v2 can add shared behavior to the base class without changing the inheritance relationship.

### register function

Every plugin exposes a single `register` function regardless of how many plugin types it implements:

```python
def register(context: PluginContext, api_version: int) -> None:
    ...
```

`api_version` is the platform's current plugin API version, passed at load time. Plugins targeting a single version may ignore it. Plugins spanning multiple versions use it for conditional behavior:

```python
def register(context: PluginContext, api_version: int) -> None:
    if api_version >= 2:
        context.cli.command()(my_v2_command)
    else:
        context.cli.command()(my_v1_command)
```

### Type declaration

Plugins declare the types they implement. The loader routes registration accordingly and validates that required interface markers are present:

```python
PLUGIN_TYPES = ["cli", "mcp_tool"]          # composite plugin
PLUGIN_TYPES = ["import_adapter"]           # single-type plugin
PLUGIN_TYPES = ["provider"]                 # service provider only
```

### Compatibility declarations

```python
PLUGIN_API_MIN_VERSION = 1      # required — minimum platform API version
PLUGIN_API_MAX_VERSION = 2      # optional — omit if no known upper bound
PLUGIN_DEPENDENCIES = [         # optional — inter-plugin service dependencies
    "quest.parser >= 1",
    "quest.api_client >= 2",
]
```

Omitting `PLUGIN_API_MAX_VERSION` signals that the plugin author expects forward compatibility. A ceiling should only be set when a specific future incompatibility is known.

---

## Plugin Types

The initial set of plugin types. Each type is a distinct interface. Structural similarity across types is intentional where natural; forced unification is not.

### `cli`
Registers commands with the CLI (typer). Access via `context.cli`.

```python
PLUGIN_TYPES = ["cli"]

def register(context: PluginContext, api_version: int) -> None:
    @context.cli.command()
    def my_command():
        ...
```

### `mcp_tool`
Registers tools with the MCP server, making them available to AI clients. Access via `context.mcp`.

```python
PLUGIN_TYPES = ["mcp_tool"]

def register(context: PluginContext, api_version: int) -> None:
    @context.mcp.tool()
    def get_my_analysis(biomarker: str) -> str:
        ...
```

### `import_adapter`
Implements the import pipeline interface: parse → validate → normalize → submit to Core REST API bulk import endpoint. The adapter is registered as a named service and invocable from the CLI and optionally as an MCP tool.

```python
PLUGIN_TYPES = ["import_adapter"]

def register(context: PluginContext, api_version: int) -> None:
    context.register_service("import.quest_labs", QuestLabsAdapter(), version=1)
```

### `analysis`
Registers named analysis functions (calculations, trend detection, anomaly flagging) available to other plugins and to MCP tools.

### `reference_ranges`
Registers a named reference range framework (see ADR-0005). The framework becomes queryable via the Core REST API and available to AI client tools.

### `query`
Registers named, reusable query patterns that both the CLI and MCP tools can invoke by name, without duplicating query logic across plugin types.

### `automation`
Registers trigger/condition/action rules that execute in response to event bus events. Trigger conditions may include database queries. Actions may include submitting jobs, publishing events, or triggering notifications. Interface TBD — see [ADR-0016](0016-automation-plugin-type.md).

### `notification_channel`
Registers a notification delivery channel (desktop, email, webhook, SMS, etc.). Notification channel plugins subscribe to alert events on the event bus and deliver them via their channel. Interface TBD — see [ADR-0017](0017-notification-channels.md).

### `provider`
A plugin whose sole purpose is to register services for other plugins. Carries no CLI commands or MCP tools of its own. Useful for shared parsers, API clients, and utility libraries that multiple plugins depend on.

---

## PluginContext

`PluginContext` is the single parameter passed to every `register` function. It carries the platform infrastructure every plugin needs and the service registry for inter-plugin communication. The exact API is defined when the first plugins are implemented; the intended shape:

```python
context.cli            # typer app — for cli plugins
context.mcp            # fastmcp server — for mcp_tool plugins
context.config         # parsed TOML config (read-only)
context.logger         # configured logger for this plugin
context.api            # pre-authenticated Core REST API client

# Service registry
context.register_service(name, service, version)
context.get_service(name, min_version=None)      # returns None if absent
context.require_service(name, min_version=None)  # raises on startup if absent
context.get_services(pattern)                    # namespace query, e.g. "quest.*"
```

`context.cli` and `context.mcp` are only populated for plugin types that use them. Accessing them from an incompatible plugin type raises a clear error.

---

## Service Registry and Provider Plugins

Any plugin can register services. A `provider` plugin registers only services — no CLI commands or MCP tools.

### Namespaced service names

Service names use dot notation as namespaces, mirroring Python package structure. A plugin package named `quest/` registers services under `quest.*`:

```python
# quest/__init__.py
PLUGIN_TYPES = ["provider"]

def register(context: PluginContext, api_version: int) -> None:
    context.register_service("quest.parser.labs",        QuestLabParser(),       version=1)
    context.register_service("quest.parser.demographics",QuestDemographicsParser(), version=1)
    context.register_service("quest.api_client",         QuestApiClient(),       version=2)
```

The filesystem name and service namespace are the same concept. Contributors immediately know where their services belong.

### Service versions are independent of plugin API version

A plugin at API version 3 may provide service version 1 of `quest.parser.labs`. These are different clocks:
- **Plugin API version** — governs the plugin-to-platform interface
- **Service version** — governs the service contract between plugins

Both must be declared and checked independently.

### Consuming services

```python
# Optional dependency
parser = context.get_service("quest.parser.labs", min_version=1)
if parser:
    ...

# Required dependency (declared in PLUGIN_DEPENDENCIES, fails at load time if absent)
parser = context.require_service("quest.parser.labs", min_version=1)
```

### No direct imports between plugins

Inter-plugin dependencies go through `PluginContext`. Direct Python imports between plugins (`from quest_plugin import QuestParser`) bypass version checking, skip dependency declaration, and create hidden coupling. The registry is the contract; imports are an implementation detail internal to a single plugin.

---

## Plugin Discovery and Load Order

The plugins directory path is configured in the shared TOML config:

```toml
[plugins]
dir = "~/.healthspan/plugins"
```

At startup, the loader:

1. Scans `plugins_dir` for `.py` files and directories with `__init__.py` (non-recursive at the top level)
2. Imports each as a module; reads `PLUGIN_TYPES`, `PLUGIN_API_MIN_VERSION`, `PLUGIN_API_MAX_VERSION`, `PLUGIN_DEPENDENCIES`
3. Validates API version compatibility; skips and warns on mismatch
4. Builds a dependency graph from `PLUGIN_DEPENDENCIES`
5. Detects cycles; fails with a clear error if found
6. Loads plugins in topological order (providers before consumers)
7. Calls `register(context, api_version)` for each plugin
8. Built-in first-party plugins are loaded first; user plugins loaded after may override built-in service registrations by registering the same service name

A missing required dependency (`require_service` with no registered provider) fails at load time with a clear error naming the missing service and the plugin that requires it.

---

## Plugin API Versioning

The plugin API version is a single integer maintained by the platform. It increments on breaking changes to the plugin-to-platform interface. The current version is `1`.

Minor additions (new context properties, new plugin types, new helpers) do not increment the version — existing plugins remain compatible.

| Declaration | Required | Purpose |
|---|---|---|
| `PLUGIN_API_MIN_VERSION` | Yes | Lowest API version the plugin supports |
| `PLUGIN_API_MAX_VERSION` | No | Highest API version; omit to indicate no known upper bound |
| `PLUGIN_DEPENDENCIES` | No | Inter-plugin service dependencies with version constraints |
| `api_version` in `register` | N/A | Current API version passed by loader; use for conditional behavior |

---

## Security Boundary

Plugins execute arbitrary Python code in the platform's processes. This is intentional — it is what makes full extensibility possible. The consequences must be clearly communicated:

- Only install plugins you have read and trust
- Plugins from unknown sources must be audited before use
- The platform does not sandbox plugins
- A plugin has access to everything its host process can reach, including the bearer token and config
- Plugin authors must not transmit health data outside the local system without explicit user consent
- This is a **trusted-user feature**, not a sandboxed extension point

This boundary must be documented prominently in all user-facing plugin documentation.

---

## Future: Plugin Configuration Schema

Plugins currently read configuration from the shared TOML config via `context.config`. This is sufficient for v1 (power users editing TOML). When the GUI (PySide6) needs to render plugin settings forms, plugins will need a way to declare their configuration schema — what keys they expect, types, defaults, and validation rules.

The likely shape is a module-level declaration alongside existing ones:

```python
PLUGIN_CONFIG_SCHEMA = {
    "api_key":  {"type": "str",  "required": True,  "description": "Dexcom API key"},
    "interval": {"type": "int",  "default": 21600,  "description": "Poll interval in seconds"},
    "enabled":  {"type": "bool", "default": True,    "description": "Enable automatic polling"},
}
```

This would enable:
- **Validation at load time** — reject plugins with missing required config before `register()` is called
- **GUI form generation** — PySide6 settings panel renders appropriate widgets per type
- **Documentation generation** — plugin config requirements are self-describing

Design deferred until the GUI is implemented and real plugin configuration patterns emerge.

---

## What Plugins Can Do
- Register CLI commands, MCP tools, import adapters, analysis functions, query patterns, and reference range frameworks
- Provide services to other plugins via the service registry
- Consume services from other plugins via the service registry
- Call the Core REST API using the pre-authenticated client in `PluginContext`
- Read and write files
- Call external APIs and services

## What Plugins Cannot Do
- Bypass authentication when calling the Core REST API
- Access the database directly (not a supported extension point)
- Import directly from other plugins (use the service registry instead)

---

## Links
- Extended by: [ADR-0024](0024-plugin-extensions.md) — adds `PLUGIN_VERSION`, `PLUGIN_PACKAGES`, pip dependency management, and plugin versioning policy
- Related: [ADR-0006](0006-application-architecture.md) — micro-kernel architecture; plugins as the primary delivery mechanism for business logic
- Related: [ADR-0004](0004-data-ingestion-strategy.md) — import adapters are a plugin type
- Related: [ADR-0005](0005-reference-range-frameworks.md) — reference range frameworks are a plugin type
- Related: [ADR-0007](0007-mcp-transport.md) — MCP tools are a plugin type
- Related: [specs/security.md](../security.md) — plugin security requirements
