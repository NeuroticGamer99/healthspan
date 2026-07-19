# ADR-0039: Startup Sequence — Migration Ownership and Passphrase Handoff

## Status
Accepted

## Context and Problem Statement
The [2026-07-06 architecture review](../reviews/architecture-review-2026-07-06.md) found two gaps on the startup path (items 1.C and 2.2) that are one design surface:

1. **Two documents describe two different migration designs.** [ADR-0008](0008-process-lifecycle.md) (launcher step 3: "runs any pending database migrations") and [ADR-0035](0035-migration-execution-semantics.md) ("the launcher runs migrations before the Core Service starts" — its exclusive-access argument depends on it) assume the launcher/CLI path; [observability.md](../observability.md) said "Core Service (runs migrations on startup)" and listed "migration pending" as a health-endpoint 503 cause. If Core Service ran migrations itself, the migration runner's foreign-keys-off connection would coexist with the runtime pool's foreign-keys-on factory in one process, and ADR-0035's exclusive-access reasoning would be fiction.
2. **Nothing specifies how the master passphrase reaches the process that derives the key.** [ADR-0028](0028-key-derivation-and-rotation.md) decides *derive once, at Core Service startup* but not how the passphrase gets there. The sequence between "user types passphrase," "migrations run against the encrypted database," and "Core Service derives and holds the key" must be one coherent story — reading even `schema_version` requires opening the encrypted database, so whoever checks for pending migrations must hold the key first.

ADR-0008 is Accepted and immutable under governance; this ADR extends it. ADR-0028 is Proposed; this ADR supplies the handoff channel its derive-once decision presupposes.

## Decision Drivers
- The passphrase must never be observable via OS-level process inspection: argv is world-readable on most platforms, and environment variables are inherited by children and dumped by diagnostics — the same reasoning that put job tokens on stdin in [ADR-0026](0026-named-scoped-tokens.md)
- ADR-0035's migration semantics (foreign keys off + exclusive access) and the runtime pool's pragma discipline must never coexist in one process
- The Core Service must never serve requests against a schema its code does not expect
- The derived key must never cross a process boundary — "never transmitted, never inherited by child processes" must stay true
- Every supported deployment (launcher, direct start, GUI launch, systemd, Docker) needs a specified variant, not an improvised one
- The story must not depend on how the human enters the passphrase — CLI and GUI entry are both first-class

## Considered Options

**Migration ownership:**
1. **Core Service runs migrations at startup** (observability.md's implied design)
2. **Launcher/CLI runs migrations before Core Service starts** (chosen)

**Passphrase transport:**
3. **argv or environment variables** — rejected outright (inspectable; inherited)
4. **TTY prompt, stdin pipe, or OS-secret-facility file** (chosen)
5. **Hand the *derived key* to Core Service instead of the passphrase** — launcher derives once, Core skips Argon2id

## Decision Outcome
Chosen: **options 2 and 4.** The launcher owns migrations; the passphrase moves only over a TTY prompt, a stdin pipe, or a permission-restricted file provided by an OS secret facility — never argv, never environment variables.

### Migration ownership: the launcher, definitively

The launcher's startup sequence (refining ADR-0008 steps 2–4):

1. Collect the passphrase (entry surfaces below); retrieve the secret key from the OS keychain (standard mode).
2. Derive the key ([ADR-0028](0028-key-derivation-and-rotation.md)) **in the launcher process**, open the database, and check `schema_version`. This derivation is unavoidable: the pending-migrations check itself requires opening the encrypted database.
3. Run any pending migrations in-process — the same runner `healthspan db migrate` invokes, under [ADR-0035](0035-migration-execution-semantics.md)'s execution semantics. Exclusive access holds because Core Service has not started.
4. Close the connection and **discard the derived key**. The launcher and Core Service hold the database *sequentially, never simultaneously* — consistent with security.md's single-database-owner rule.
5. Start Core Service; hand off the passphrase (below); after Core Service reports healthy, overwrite and drop the passphrase copy.

**The Core Service never runs migrations.** At startup — after deriving its own key and opening the database — it compares `schema_version` against the version its code expects. On mismatch it logs `CRITICAL` and **exits nonzero**. It does not come up in a degraded state, so the former "migration pending" health-503 is dead by design and has been removed from observability.md: a service that cannot trust its schema should not be up to say 503. A directly started Core Service (`healthspan service start`) with pending migrations refuses to start and names the fix (`healthspan db migrate`).

**Accepted cost — Argon2id runs twice per platform start:** once in the launcher (to check and migrate), once in Core Service (ADR-0028's derive-once, unchanged). The rejected alternative (option 5) — piping the *derived key* to Core Service — would save one KDF run (~1 s) but would falsify "the derived key is never transmitted, never inherited by child processes," the load-bearing clause of INV-1. The passphrase crosses the boundary; the key never does.

### The channel rule

The passphrase may enter a process only via:

- **(a) an interactive TTY prompt** with echo disabled (`getpass`),
- **(b) a stdin pipe** written by the parent process and closed after the single line, or
- **(c) a permission-restricted file provided by an OS secret facility** (systemd credentials, Docker secrets), whose *path* — not content — arrives via config or flag.

**Never argv. Never environment variables.** A process that finds no supported channel available fails with an error listing them; it never falls back to reading an environment variable.

### Entry surfaces vs. transport channels

The channel rule governs how the passphrase *moves between processes*, not how the human enters it. Two entry surfaces are sanctioned:

- **TTY prompt** — the CLI/launcher `getpass` path.
- **GUI masked dialog** — a PySide6 password-mode field. When the GUI starts the stack (desktop launch flow), it collects the passphrase in its dialog, spawns the launcher, and writes the passphrase to the **launcher's stdin pipe** — channel (b), one hop earlier. The launcher then proceeds exactly as above. The GUI overwrites and drops its copy once the launcher has read it.

When the GUI merely *attaches* to a running Core Service — the common case — no passphrase is involved at all: the GUI speaks REST with its named token, and Core Service already holds the key. The passphrase exists only at platform start.

**The GUI must not cache the passphrase** (e.g. for reconnect-on-crash convenience). Zero-prompt restarts are the documented full-auto-unlock mode's job ([ADR-0013](0013-encryption-at-rest.md)), which stores the passphrase properly in the OS keychain; the GUI must not quietly become an undocumented passphrase store.

### Deployment variants

1. **Launcher-started (default).** Launcher prompts via TTY (or receives the passphrase on its own stdin from a GUI parent), holds it as a mutable bytes buffer, uses it for the migration phase, writes it as a single line to Core Service's stdin and closes the pipe. Only Core Service receives it — the MCP Server, GUI, and Automation Host children never do.
2. **Direct-start Core Service** (`healthspan service start`). If stdin is a TTY: prompt. If stdin is piped: read one line — documented for credential-manager pipes, with the warning that `echo secret |` lands in shell history. Else if `passphrase_file` (config key or `--passphrase-file` flag) is set: read that file. Otherwise: fail listing the supported channels.
3. **systemd.** `LoadCredential=passphrase:<path>` in the unit; Core Service reads `$CREDENTIALS_DIRECTORY/passphrase` — the file channel, with systemd enforcing the permissions. A documented unit snippet ships with the deployment docs.
4. **Docker.** A compose secret mounted at `/run/secrets/<name>` — the file channel; formalizes the deployment variant ADR-0013 sketched for headless operation.
5. **Full auto-unlock mode** needs no channel: Core Service reads both components from the OS keychain directly, unchanged from ADR-0013.

### Retention and zeroization

The launcher (and a GUI parent) must not retain the passphrase after handoff: the buffer is overwritten and released once the receiving process has read it. This is best-effort by the same honest standard as ADR-0028's `SecretStr` discipline — Python cannot guarantee zeroization (interned copies, allocator behavior), and Qt string widgets carry the same caveat; the threat model already concedes same-user memory access. Holding the passphrase as `bytes`/`bytearray` rather than `str` wherever practical is the implementation norm.

**Constraint on future supervision (T2.8):** dropping the launcher's copy is possible *because* ADR-0008's launcher does not auto-restart crashed children. A future supervisor that restarts Core Service must either re-prompt, direct users to full auto-unlock mode, or explicitly decide to retain the passphrase — that tension belongs to the supervision decision and is named here so it cannot be resolved by accident. *Resolved by [ADR-0042](0042-process-supervision-and-single-instance-locking.md): the supervisor restarts Core Service unattended only in full-auto-unlock mode (Core re-derives from the keychain); in interactive mode it re-prompts or brings the stack down rather than retaining the passphrase — so the launcher still drops its copy after handoff.*

### Positive Consequences
- One coherent startup story: prompt → launcher derives → migrates (exclusive access, ADR-0035's argument now literally true) → discards key → Core derives and serves
- The passphrase is never inspectable via process listing or environment; every deployment variant has a specified channel
- Core Service can never serve requests against an unexpected schema, and the phantom "migration pending" 503 is gone
- GUI-launched desktop startup is first-class without weakening the transport rule
- The derived key still never crosses a process boundary

### Negative Consequences / Tradeoffs
- Argon2id runs twice per platform start (~1 s extra) — the price of never transmitting the derived key
- The launcher briefly holds passphrase + derived key + open database, concentrating what INV-1's single-process wording must accommodate (the sequential-ownership reword, review 1.D, covers this)
- Best-effort zeroization is honest but weak in Python/Qt — documented rather than overclaimed

## Pros and Cons of the Options

### Core Service runs migrations at startup
- Pro: one fewer key derivation; the health endpoint could report migration progress
- Con: foreign-keys-off migration connection and foreign-keys-on runtime pool in one process; ADR-0035's exclusive-access argument lost; a 503-serving service with an untrusted schema; contradicts two ADRs already on the record

### Launcher/CLI runs migrations before Core starts (chosen)
- Pro: ADR-0035's semantics hold by construction; Core Service's schema check becomes a simple refuse-to-start invariant
- Con: double KDF at startup; launcher briefly holds the key

### argv / environment variables
- Pro: trivially scriptable
- Con: argv is world-readable process metadata; env is inherited by every child and dumped by crash handlers and diagnostics — disqualifying for a credential

### TTY / stdin pipe / secret-facility file (chosen)
- Pro: none of the three is observable via process inspection; covers interactive, GUI, piped, systemd, and Docker deployments with the same rule
- Con: slightly more implementation surface (channel detection order in direct-start mode)

### Hand the derived key to Core Service
- Pro: single Argon2id per start
- Con: falsifies "the derived key is never transmitted"; the key would exist in two processes and a pipe buffer; saves ~1 s once per start — nowhere near worth the invariant

## Links
- Extends: [ADR-0008](0008-process-lifecycle.md) — refines launcher steps 2–4 (passphrase collection, migration phase, handoff); launcher-as-default and process ordering unchanged
- Extends: [ADR-0028](0028-key-derivation-and-rotation.md) — supplies the passphrase channel its derive-once-at-startup decision presupposes
- Related: [ADR-0035](0035-migration-execution-semantics.md) — the exclusive-access premise this ADR makes definitive
- Related: [ADR-0013](0013-encryption-at-rest.md) — key model, full-auto-unlock mode, Docker secret sketch formalized here
- Related: [ADR-0026](0026-named-scoped-tokens.md) — the stdin-not-argv/env precedent for job tokens
- Related: [ADR-0042](0042-process-supervision-and-single-instance-locking.md) — resolves the "Constraint on future supervision (T2.8)" named above; supervision restarts the key-holder unattended only in full-auto-unlock mode, so this ADR's drop-after-handoff decision stands
- Related: [specs/observability.md](../observability.md) — startup order corrected; "migration pending" 503 removed
- Resolves: [architecture review 2026-07-06](../reviews/architecture-review-2026-07-06.md), items 1.C and 2.2
