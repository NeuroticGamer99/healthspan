# ADR-0066: Startup Permission Verification for the Database, Sidecar, and Passphrase File (extends ADR-0049)

## Status
Proposed

## Context and Problem Statement
[security.md](../security.md) requires the platform to warn on startup when the config file or the database file is readable beyond its owner, and describes the `passphrase_file` channel as "a permission-restricted file." What ships checks the **config file only, on POSIX only, and only warns**. Nothing ever stats the database, its `.keyparams` sidecar, or the passphrase file.

Owner-only protection is applied to files the platform *creates* ([ADR-0046](0046-filesystem-layout-and-config-discovery.md)'s writer obligation, `fsperm.set_owner_only`). That says nothing about a file that arrived some other way: a database restored from a tar or cloud-sync copy at mode 644, a sidecar hand-copied to a new machine, a passphrase file typed into a home directory, a file inheriting a permissive parent's ACL. The exposure is concrete — in passphrase-only mode the ciphertext plus the sidecar's salt and KDF parameters are the complete input to an offline attack, and the passphrase file is the master passphrase in plaintext.

The 2026-07-19 architecture review ([2.1](../reviews/architecture-review-2026-07-19.md)) raised this with a real health database now on the machine. The review fixes the mechanism (check at `build_runtime`/`exclusive_database_access` time; POSIX mode bits, Windows explicit-principal enumeration) and leaves four implementation defaults open: **what the platform does** on finding drift, **how deep** the Windows check goes, **whether there is an override**, and **which files** are in scope. All four are owned by [ADR-0049](0049-core-service-skeleton-implementation-decisions.md), which is Accepted — so they land as one Proposed extension ADR with the implementing change, the [ADR-0050](0050-token-store-and-auth-implementation-decisions.md)/[ADR-0051](0051-auth-lifecycle-and-rate-limiting-implementation-decisions.md) pattern.

## Decision Drivers
- A warning that scrolls past on service start protects nobody — the 644 database is still 644 on the next start, and every start after that
- A hard refusal on an inherited-ACL quirk locks the owner out of their own data, and the platform is the only way in
- Windows is where the real database lives; a POSIX-only check is a check that does not run
- The check sits on paths a user invokes constantly, so its cost must not become a per-command subprocess storm
- security.md's existing clauses are the contract; a decision that strengthens them must say so there, not only here

## Considered Options
1. Warn on everything — the literal reading of security.md's "should warn on startup"
2. Refuse on everything broader than owner-only
3. Warn on the platform's own files, refuse on the passphrase file
4. **Repair the platform's own files in place, refuse on the passphrase file** (chosen)

## Decision Outcome
Chosen: **option 4**. Options 1 and 3 leave the drift in place — the review's own critique of warn-only. Option 2 buys its strength with the lockout the review warns against. Option 4 dissolves the dilemma for every file the owner already owns: repair ends the exposure *and* reports it, and the one file repair cannot honestly fix is the one that gets refused.

### 1. Repair, don't narrate — the platform's own files
The database, its `.keyparams` sidecar, the backup directory, published backups, and the config file are **restored to owner-only in place** when they are found broader, and the repair is reported. The repair is `fsperm.set_owner_only` — the same call the writer side makes at creation, so the end state is exactly what `init` produces and nothing new can be reached by it. It is idempotent: the second start is silent because there is nothing left to fix.

The report is not softened. It names the file, states the exposure that was found, and says plainly that the repair **does not undo any read that already happened** while the file was exposed. Repair closes the hole; it does not rewrite history, and the message must not let the owner believe it did.

A repair that cannot be applied — a foreign-owned file, a filesystem without permissions, a read-only mount — **degrades to a warning naming the remedy, never to a refusal**. Every file in this class is ciphertext or configuration; blocking the owner from their own database over a mode bit the platform could not change is precisely the failure this ADR exists to avoid.

### 2. Refuse, never repair — `passphrase_file`
The passphrase file is refused, and the platform never touches its permissions. Two independent reasons, either sufficient:

- It may be an **OS-secret-facility file** ([ADR-0039](0039-startup-sequence-and-passphrase-handoff.md) channel c — systemd credentials, Docker secrets) that the platform has no business rewriting; its permissions are the orchestrator's contract, not ours.
- It holds a **plaintext secret**. A secret that has been readable beyond its owner is disclosed, and a quiet `chmod` would hide that fact behind a now-correct mode bit. The only honest response is to stop and force a rotation decision.

The refusal raises `ServiceStartupError` from `resolve_passphrase`'s file tier, before the read, naming the exposure, the remedy, and the rotation obligation. It covers both the `service.passphrase_file` config key and the `--passphrase-file` flag, since the check lives at the read, not at the resolution of which one wins.

**Forward note for the deployment snippets** ([ADR-0049](0049-core-service-skeleton-implementation-decisions.md) §5 defers them to the distribution milestone): systemd credentials land at mode `0400` and pass unchanged. **Docker Swarm secrets default to `0444` and will not** — the snippet, when written, must set `mode: 0400`. With no override (decision 4), that is the only remedy, so the constraint belongs in the snippet from its first draft.

### 3. What is checked, where, and how deep

| Surface | Posture | Checked at |
|---|---|---|
| Config file | Repair | `load_config` (mode bits only) **and** the startup sweep (full depth) |
| Database file | Repair | Startup sweep |
| `.keyparams` sidecar | Repair | Startup sweep |
| Backup directory | Repair | Startup sweep |
| Published backups + their sidecars | Repair | `db backup` (sweep after publish), `db restore` (the source pair) |
| `passphrase_file` | **Refuse** | `resolve_passphrase`, before the read |

The **startup sweep** is `permcheck.verify_startup_files`, called from two places — `build_runtime` (Core Service start, before anything is read) and `exclusive_database_access` (the sanctioned direct-database commands, under the advisory lock, before any prompt). Those are the two points where the platform takes hold of the database, which is what makes them the right join point rather than, say, `db.connect`.

**Windows depth.** POSIX reads the mode bits (`mode & 0o077`). Windows mode bits carry no ACL information — the reason `set_owner_only` exists at all — so the check enumerates ACL principals through `icacls` and reports any principal other than the current user, except a small benign set:

| SID | Identity | Why it says nothing about exposure |
|---|---|---|
| `S-1-5-18` | `NT AUTHORITY\SYSTEM` | Can take ownership and rewrite any DACL regardless |
| `S-1-5-32-544` | `BUILTIN\Administrators` | Same — a local admin is already past any file ACL |
| `S-1-3-4` | `OWNER RIGHTS` | Resolves to the object's own owner; rides ordinary user-profile files |
| `S-1-3-0` | `CREATOR OWNER` | Inheritable placeholder for a child's creator; names no third party |

Reporting any of them would be noise that trains the owner to ignore the check. Nothing else is exempt — `Everyone`, `Authenticated Users`, `BUILTIN\Users`, and any named account are all real exposures and all reported.

They are matched by *name*, because a name is what `icacls` prints — and those names are localized, so the well-known SIDs are resolved to this machine's names once per process via `LookupAccountSidW`. The English forms stay in the set as a lookup-failure backstop; no system can host a real account literally named `NT AUTHORITY\SYSTEM`, so keeping them costs nothing.

**Cost placement.** The Windows enumeration is one subprocess per path, which decides where each check runs:

- `load_config` runs on *every* CLI invocation, so its config-file check stays mode-bits-only (`acl_scan=False`, inert on POSIX). The config file gets its full-depth look in the startup sweep instead — checked twice, cheaply, rather than once expensively on a hot path.
- Published backups are checked **where they are touched** — the source of a restore (the tar-arrival case the check exists for) and a sweep after `db backup` republishes the archive — rather than on every start, where the default `retention_count = 14` would cost 28 enumerations per Windows CLI invocation to re-verify files nothing was about to read.

**Undeterminable is clean.** A check that *cannot* run — an `icacls` failure, an unreadable `stat` — reports no exposure rather than failing closed. The writer side still enforces owner-only at creation, and turning a transient tool error into a lockout would trade a rare exposure for a common one.

### 4. No override flag and no config key
There is no `--allow-broad-permissions`, and no `[service]` key that turns the check down. The refusal message names the exact remedy — `chmod 600 <path>` on POSIX, the equivalent `icacls` line on Windows — and the rotation obligation, so the owner is never left guessing what to type.

Because that message is the *only* route out of a refusal, it must run exactly as printed. The Windows line therefore interpolates the **resolved account name**, never `%USERNAME%`: the shell variable expands in `cmd.exe` alone, and pasting it into PowerShell hands `icacls` a literal string it cannot map. If the account lookup fails — the same failure that makes the check itself undeterminable — the message describes the end state in prose instead of printing a command that cannot be composed. [ADR-0049](0049-core-service-skeleton-implementation-decisions.md) §4's posture on the liveness cap governs: a knob is not shipped speculatively, and a deployment that genuinely needs one is a revisit trigger, not a switch shipped ahead of the need. A security check whose first documented response is a bypass flag is a check that will be bypassed.

### 5. Scope boundary
Deliberately **not** checked, each with its reason:

- **The per-client token config-file fallback** ([security.md](../security.md), Token storage). It does not exist: `keychain.py` and `cli_client.py` are keyring-only, and the fallback is designed-but-unbuilt, so there is no path to check. **Forward obligation** — whoever implements it adds the file to `permcheck` under the repair posture, with its testing-strategy target, in the same PR. It is a plaintext bearer credential on disk; it inherits this ADR's posture by construction, not by a later decision. Tracked in [open-questions.md](../open-questions.md) (Operations) with its trigger.
- **The `.lock` sentinel and log output.** The sentinel holds a PID for a human-readable message ([ADR-0042](0042-process-supervision-and-single-instance-locking.md)); log output is canary-gated against health values ([testing-strategy.md](../testing-strategy.md)). Neither carries a secret, and widening the sweep to them would spend startup cost on hygiene.
- **Import staging and temporary files.** Owned by [ADR-0033](0033-plaintext-artifact-disposal.md)'s disposal rules and [ADR-0012](0012-job-abstraction.md)'s path-traversal guards, which are creation-time and per-operation concerns rather than a startup sweep.

### Positive Consequences
- The clauses security.md has always carried are now enforced, on both platform families, for every file that carries the exposure rather than the one file that happened to be easy
- The drift is *ended*, not narrated: a database that arrives at 644 is owner-only before the first read, and the owner is told what was found and what it does not undo
- The one file where repair would be dishonest is the one file that refuses, so a disclosed master passphrase produces a rotation decision instead of a silent fix
- The startup sweep is one function with one posture table, so the Core Service and the direct-database commands cannot drift apart on what they check

### Negative Consequences / Tradeoffs
- The platform now *mutates* permissions on files it did not create. Bounded: the mutation is the same owner-only state `init` produces, it only ever narrows access, and it is always reported — but it is a real behavior change from a read-only check, and on Windows it strips inherited entries the owner may not have expected to lose
- Windows startup pays one `icacls` spawn per checked path (four on the sweep). Accepted at single-user scale beside key derivation and lock acquisition, and deliberately kept off the per-command config path
- Checking the config file twice at startup is redundant on POSIX. The alternative — one full-depth check on every CLI invocation — costs a subprocess per command on Windows, which is worse
- A localized-Windows `LookupAccountSidW` failure would report SYSTEM/Administrators as exposures and repair them away. The English-name backstop makes this unlikely, and the resulting state is still owner-only rather than insecure
- No override means an owner with a genuinely unfixable passphrase-file mode (a read-only 0444 mount) cannot start until they change the deployment. Named as a revisit trigger, and the Docker-secrets note in decision 2 exists so the first snippet never creates that situation

## Consequences for Other Documents
- **[security.md](../security.md)**: the config-file and database-file clauses restate the posture as repair-and-report rather than warn; the sidecar, backups, and passphrase file are named as covered surfaces with their postures
- **[testing-strategy.md](../testing-strategy.md)**: a Security-tests coverage target for the graded posture (repair, refuse, degrade-to-warning, undeterminable-is-clean); the Cross-Platform "File permissions" bullet gains the ACL-enumeration note
- **[ADR-0049](0049-core-service-skeleton-implementation-decisions.md)**: navigation link — this ADR fixes startup-check defaults ADR-0049's Phase-2 skeleton left open
- **[ADR-0046](0046-filesystem-layout-and-config-discovery.md)**: navigation link — the writer obligation gains a reader half
- **[open-questions.md](../open-questions.md)**: an Operations entry for the per-client token config-file fallback (decision 5's forward obligation), with its trigger

## Links
- Extends: [ADR-0049](0049-core-service-skeleton-implementation-decisions.md) — supplies the startup-check defaults (posture, Windows depth, override, scope) its Phase-2 skeleton leaves open
- Extends: [ADR-0046](0046-filesystem-layout-and-config-discovery.md) — adds the reader half to its owner-only writer obligation
- Related: [ADR-0039](0039-startup-sequence-and-passphrase-handoff.md) — the `passphrase_file` channel decision 2 refuses to read when exposed
- Related: [ADR-0038](0038-backup-execution-and-verification.md) — the published backups and backup directory decision 3 places in scope
- Related: [ADR-0028](0028-key-derivation-and-rotation.md) — the `.keyparams` sidecar whose salt and parameters make the exposure an offline-attack input
- Related: [security.md](../security.md) — the startup-warning clauses this ADR implements and strengthens
- Resolves: [architecture review 2026-07-19](../reviews/architecture-review-2026-07-19.md) §2.1 (worklist T2.2)
