# ADR-0046: Filesystem Layout and Configuration Discovery

## Status
Proposed

## Context and Problem Statement

Phase 1 makes the platform real on disk: a config file, an encrypted database with its `.keyparams` sidecar ([ADR-0028](0028-key-derivation-and-rotation.md)), and backup output ([ADR-0038](0038-backup-execution-and-verification.md)). [ADR-0006](0006-application-architecture.md) fixes the config file's *contents* (a single shared TOML file), [security.md](../security.md) fixes its *permissions* (owner-read-only, warn on broader), and [ADR-0008](0008-process-lifecycle.md) fixes *who creates it* (the launcher on first run; `healthspan init` in the CLI-only phases) — but no spec names *where any of these files live* or how processes find the config file. Every process reads the same file (ADR-0006: "Processes do not hardcode any of these values"), so discovery must be deterministic and identical across the CLI, Core Service, MCP server, and GUI.

## Decision Drivers

- Deterministic: every process on the same machine must resolve the same config file with no coordination
- OS citizenship: platform backup tooling, roaming profiles, and users' expectations differ per OS; fighting conventions creates support burden
- Secure by default (security.md): defaults must not place health data anywhere world-readable or synced by default (the *live* database must never sit in a cloud-synced folder — [ADR-0019](0019-multi-device-sync.md))
- Docker/LAN deployment mounts the config file explicitly (ADR-0008), so an override mechanism must exist
- The encryption key must never transit argv or env ([ADR-0039](0039-startup-sequence-and-passphrase-handoff.md)); config *paths* carry no secrets, so flag/env discovery of the file location is safe

## Considered Options

1. **Platform-conventional directories via `platformdirs`, with flag/env override** (chosen)
2. Single `~/.healthspan/` dot-directory, identical on all OSes
3. No default — every invocation requires an explicit `--config` path

## Decision Outcome

Chosen option: **platform-conventional directories via `platformdirs`**, resolved per-user, never per-machine.

### Directory layout

Resolved with `platformdirs` (`appname="healthspan"`, `appauthor=False`, `roaming=False`):

| Purpose | Resolver | Windows | macOS | Linux |
|---|---|---|---|---|
| Config | `user_config_dir` | `%LOCALAPPDATA%\healthspan` | `~/Library/Application Support/healthspan` | `$XDG_CONFIG_HOME/healthspan` (default `~/.config/healthspan`) |
| Data | `user_data_dir` | `%LOCALAPPDATA%\healthspan` | `~/Library/Application Support/healthspan` | `$XDG_DATA_HOME/healthspan` (default `~/.local/share/healthspan`) |
| Logs (reserved for the observability work) | `user_log_dir` | `%LOCALAPPDATA%\healthspan\Logs` | `~/Library/Logs/healthspan` | `$XDG_STATE_HOME/healthspan/log` |

Files within those directories:

- **Config file:** `<config-dir>/config.toml`
- **Database:** `<data-dir>/healthspan.db`, with its sidecar `healthspan.db.keyparams` always adjacent (ADR-0028's "next to the database" rule follows the database wherever `[database].path` points; it is not independently configurable)
- **Backups (default):** `<data-dir>/backups/` — this is the concrete default for the `[backup]` destination directory that ADR-0038 deferred to ADR-0019's "cloud-sync target" designation. The default is deliberately *not* a synced location: syncing is an explicit user opt-in, done by either pointing a sync client at the backup directory or repointing `[backup].directory` at a synced folder. The *live* database directory must never be the sync target (ADR-0019), and a default that separated them poorly would invite exactly that mistake.

On Windows and macOS the config and data directories coincide — accepted; the XDG split on Linux is preserved because Linux users expect it.

### Config discovery precedence

1. `--config <path>` — global CLI option (and the launcher's future mechanism for pointing subprocesses at the file)
2. `HEALTHSPAN_CONFIG` environment variable — path to the config file (a path is not a secret; the ADR-0039 env-var prohibition covers secret *values*, not file locations). This is the Docker/headless mechanism.
3. Platform default: `<config-dir>/config.toml`

The first source that yields a path wins; if the resolved file does not exist, that is an error for sources 1–2 (an explicit pointer to a missing file is a mistake, not a request for defaults) and **defaults-apply** for source 3.

### Config file semantics

- **Missing file = full defaults.** Every config value has a secure default; a fresh install works with no file. File *creation* is owned by `healthspan init` (CLI-only phases) and the launcher first-run (ADR-0008) — not by readers. Readers never write.
- **`config_version = 1`** is required in any existing file ([design-rationale.md](../design-rationale.md) versioning surface). A missing or unsupported `config_version` is a load error, not a guess.
- **Unknown keys are load errors.** A typo that silently falls back to a default is a misconfiguration that looks configured; strictness is the secure-by-default reading. (Plugin-owned config sections, when they arrive with ADR-0010 implementation, will claim an explicit namespace rather than weaken this rule.)
- **Relative paths in config values resolve against the config file's directory** — self-contained and portable (a mounted Docker config directory behaves predictably). Paths for a file that does not exist yet (e.g. `[database].path` before `init`) are legal; existence is the consuming command's concern.
- **Permissions warning on load:** per security.md, any reader warns (once per invocation, to stderr/log — never blocking) if the config file is readable beyond its owner. Creation with owner-only permissions is the writer's obligation (`init`, launcher). **Platform scope:** the read-side check inspects POSIX mode bits and therefore runs on POSIX only — on Windows, mode bits carry no ACL information and a faithful read-side check would require ACL enumeration; there, owner-only protection is enforced on the write side instead (`init` sets owner-only ACLs at creation). This is a deliberate narrowing of security.md's unconditional "warn on startup" wording, accepted until a Windows ACL read-side check proves worth its complexity.
- **Phase-1 key surface:** `config_version`, `[database] path`, `[backup] directory | schedule | retention_count` (defaults daily / 14 per ADR-0038; key names concretized here as the extension ADR-0038 anticipated), `[logging] level`. Value domains: `retention_count` must be an integer ≥ 1; `level` is one of `DEBUG | INFO | WARNING | ERROR | CRITICAL`, case-insensitive, normalized to upper case; `schedule`'s vocabulary belongs to the Core-internal scheduler (ADR-0038, Phase 2) — until that lands only non-emptiness is enforced. Later phases add their sections (ports, binding, CORS — ADR-0006/security.md) in the PRs that implement them, per the decision-capture convention.
- **No env-var overrides for individual values in v1.** One file, one truth; the file location is the only environment-sensitive input. Revisited only if a concrete deployment need surfaces (recorded then, per open-questions discipline).

### Inspection surface

`healthspan config path` prints the resolved config file path and which source resolved it; `healthspan config show` prints the effective configuration (defaults merged, values only — no secrets exist in config by construction, [ADR-0026](0026-named-scoped-tokens.md)). These exist so "which file am I actually reading?" is never a debugging exercise.

### New dependency

`platformdirs` — pure-Python, zero transitive dependencies, the ecosystem-standard implementation of exactly these conventions (used by pip itself). Sanctioned by this ADR per the implementation-decision-capture rules.

### Positive Consequences

- Every process resolves the same file the same way; the resolution is printable (`config path`) and testable
- Native conventions on each OS — platform backup tools and user expectations both work unmodified
- Zero-config first run: install → `init` → working encrypted database in conventional locations
- The backup-destination default finally has a concrete, safe value, recorded in the owning-ADR chain

### Negative Consequences / Tradeoffs

- One more runtime dependency (`platformdirs`) — accepted: small, stable, pure-Python; hand-rolling these rules is how subtle per-OS bugs are born
- Config and data coincide on Windows/macOS — cosmetic, documented
- Strict unknown-key rejection means config files do not survive downgrades gracefully — accepted for a pre-1.0 single-user platform; `config_version` is the upgrade path
- No per-value env overrides makes certain container patterns (12-factor style) less idiomatic — accepted; ADR-0008's Docker story mounts the file

## Pros and Cons of the Options

### Platform-conventional directories via `platformdirs` (chosen)
- Pro: OS citizenship; ecosystem-standard library; XDG compliance on Linux
- Pro: separates config (user-editable) from data (machine state) where the platform expects it
- Con: paths differ per OS (documentation must show all three); one new dependency

### Single `~/.healthspan/` dot-directory
- Pro: one path to document; no dependency; trivially predictable
- Con: unconventional on Windows (home-dir dotfiles are a Unix idiom) and macOS; hides data from platform backup/migration tooling conventions; mixes config and data in one directory

### Explicit `--config` always
- Pro: maximal determinism, zero magic
- Con: hostile first-run ergonomics for a tool whose default deployment is "one user, one machine"; every wrapper script must plumb the path

## Links

- Extends: [ADR-0006](0006-application-architecture.md) — supplies the location and discovery rules for the shared TOML file it defined
- Extends: [ADR-0038](0038-backup-execution-and-verification.md) — concrete default for the `[backup]` destination directory and its key names
- Extended by: [ADR-0047](0047-crypto-surface-implementation-decisions.md) — `init`'s skeleton-config creation and Windows ACL mechanics
- Related: [ADR-0028](0028-key-derivation-and-rotation.md) — sidecar adjacency rule; this ADR places the database it follows
- Related: [ADR-0019](0019-multi-device-sync.md) — the backup directory is the designated sync target; the default deliberately requires opt-in to sync
- Related: [ADR-0008](0008-process-lifecycle.md) — config file creation on first run (launcher); `healthspan init` covers the CLI-only phases
- Related: [ADR-0039](0039-startup-sequence-and-passphrase-handoff.md) — the env-var prohibition this ADR's `HEALTHSPAN_CONFIG` does not violate (paths, not secrets)
- Related: [specs/security.md](../security.md) — config file permission rules restated here
- Related: [specs/design-rationale.md](../design-rationale.md) — `config_version` versioning surface
