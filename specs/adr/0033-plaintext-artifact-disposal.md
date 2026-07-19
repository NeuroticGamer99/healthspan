# ADR-0033: Plaintext Artifact Disposal

## Status
Accepted

## Context and Problem Statement
The [architecture review](../reviews/architecture-review-2026-06-10.md) found three related defects in how the platform handles plaintext artifacts on disk:

1. **The secure-deletion requirement overpromises** (review 2.5). security.md required temporary files to be "overwritten with zeroes (or equivalent) before deletion," implying that overwriting erases data. On modern storage this is false: SSD wear leveling remaps writes to fresh physical blocks, copy-on-write and journaling filesystems (APFS, Btrfs, ZFS; NTFS journaling) preserve prior block contents, filesystem snapshots pin old versions, and cloud-sync version history retains deleted and overwritten files entirely outside the platform's reach ([ADR-0019](0019-multi-device-sync.md) already documents that last point). A security document that promises erasure it cannot deliver violates the platform's honesty standard ([ADR-0013](0013-encryption-at-rest.md)'s threat-model table, [ADR-0026](0026-named-scoped-tokens.md)'s limitations section, [ADR-0028](0028-key-derivation-and-rotation.md)'s zeroization caveat).
2. **`healthspan db encrypt` deliberately leaves a plaintext health database on disk** (review 2.4). security.md's migration path said the command "retains the original as a backup" — an unbounded-lifetime plaintext copy of the user's full health history, from a platform that applies owner-only permissions and disposal rules to plaintext it holds for seconds.
3. **Recovery Kit digital artifacts are unspecified** (review 2.6). `healthspan keys recovery-kit` — and the kit generation inside `healthspan init` and `healthspan keys rotate-secret-key` ([ADR-0028](0028-key-derivation-and-rotation.md)) — renders a printable document (PDF/PNG) whose QR code encodes the secret key. Nothing said where that digital file lands, how long it lives, or how it is disposed of. A kit file lingering on unencrypted or cloud-synced storage quietly undermines the two-factor model: the secret key was supposed to exist only in the OS keychain and on paper.

One policy underlies all three: what the platform honestly can and cannot do about plaintext once it has touched disk, and how every command that creates such an artifact must behave.

## Decision Drivers
- **Honesty standard**: never claim a protection the platform cannot deliver — the same bar as ADR-0013's threat model, ADR-0026's "What This Does Not Protect," and ADR-0028's best-effort zeroization
- The primary control must be **structural** (plaintext never written) rather than **remedial** (plaintext hopefully erased)
- `db encrypt` must preserve its safety property — never destroy the only verified-good copy of the database (mirrors ADR-0028's mandatory verified backup before rekey)
- Kit files contain the **secret key**: credential material, handled with the same no-persistence discipline as health data
- Disposal behavior must be implementable on Windows, macOS, and Linux, whose printing and filesystem realities differ
- Deliberate, user-requested plaintext (exports, [ADR-0015](0015-data-export.md)) is the user's own choice and their custody — this policy governs artifacts the *platform* creates as a side effect

## Considered Options
1. **Status quo** — overwrite-with-zeroes stated as effective erasure; `db encrypt` silently retains a plaintext backup; kit rendering unspecified
2. **Drop overwriting entirely** — treat disposal as plain unlink and rely solely on recommending full-disk encryption
3. **Layered policy — avoidance first, best-effort disposal, full-disk encryption as the backstop — applied uniformly to `db encrypt` and Recovery Kit rendering** (chosen)

## Decision Outcome
Chosen: **option 3**.

Option 2 throws away a cheap, genuinely useful control: on traditional filesystems and spinning disks, overwrite-before-unlink does defeat casual recovery, and it costs almost nothing. The defect was never the overwriting — it was claiming the overwrite is sufficient. Option 1 is the set of defects being fixed.

### The disposal policy (resolves 2.5)
Three layers, in order of actual protective value:

1. **Avoidance (primary).** Plaintext health data or key material that is never written to disk cannot need erasing. Commands prefer in-memory processing and streaming; a plaintext file is created only when a platform pathway genuinely requires one, with owner-only permissions, in a private directory, for the shortest workable lifetime.
2. **Best-effort disposal (defense-in-depth).** When a plaintext artifact is disposed of, it is overwritten with zeroes and then unlinked. This is stated everywhere — spec, user documentation, command output — as *best-effort*, defeated by SSD wear leveling, copy-on-write and journaling filesystems, snapshots, and cloud-sync version history. It is retained because it is nearly free and still raises the bar on the storage where it does work.
3. **OS full-disk encryption (backstop).** BitLocker, FileVault, or LUKS is the only control that actually bounds residual plaintext on modern storage. The platform recommends it prominently in user documentation, and every command that creates a plaintext artifact prints the recommendation alongside its disposal caveat. The platform does **not** attempt runtime FDE detection: the checks are platform-specific, unreliable (e.g. unencrypted external volumes beside an encrypted system volume), and a false "you're protected" is worse than a standing recommendation — the same honesty reasoning as ADR-0028's zeroization caveat.

### `healthspan db encrypt` (resolves 2.4)
The migration never silently leaves plaintext behind. Sequence:

1. **Exclusive access** — refuses to run while the Core Service is up, like `db migrate` and the rotation commands ([ADR-0006](0006-application-architecture.md), [ADR-0028](0028-key-derivation-and-rotation.md)).
2. **Export** — `sqlcipher_export()` from the plaintext database into a new encrypted file created with owner-only permissions.
3. **Verify** — open the encrypted copy with the derived key, run `PRAGMA integrity_check`, and compare per-table row counts against the source. If verification fails, the encrypted copy is deleted (it is ciphertext; disposal is trivial) and the plaintext original remains the live, untouched database.
4. **Swap** — the plaintext original is renamed aside (e.g. `healthspan.db.pre-encryption`); the verified encrypted file becomes the live database.
5. **Explicit decision about the plaintext original** — the command prompts: dispose now (the default), executing best-effort disposal and printing its caveat plus the FDE recommendation; or retain via an explicit `--keep-plaintext` flag, which prints a prominent warning naming the exact path, stating that it is a complete plaintext copy of the user's health history, and pointing at `healthspan db backup` as the correct way to get a backup — an *encrypted* one — of the now-migrated database.
6. **Non-interactive runs must choose** — scripted invocations must pass exactly one of `--dispose-plaintext` or `--keep-plaintext`; absent both, the command refuses rather than guess.

Disposal happens only *after* verification — the safety property that motivated "retain as a backup" is preserved by ordering, not by an indefinite plaintext copy.

### Recovery Kit rendering (resolves 2.6)
Applies to every kit-generating pathway: `healthspan init`, `healthspan keys recovery-kit`, `healthspan keys rotate-secret-key`, and `healthspan keys convert-mode` ([ADR-0028](0028-key-derivation-and-rotation.md) — either direction can render a kit: converting to two-factor generates the new kit; converting away offers a final kit for the outgoing secret key).

- **Render in memory first.** Where the OS print pathway accepts a stream (`lp`/`lpr` on macOS and Linux), the kit is printed directly from memory and no file ever exists.
- **Temp file only where the platform requires one** (Windows shell print pathways): written with owner-only permissions to the platform's private data directory — never a shared temp directory — printed, and then, after the user confirms the printout, disposed of under the policy above. The render must not outlive the command except by the explicit choice below.
- **Deliberate digital copies via `--output <path>` only.** The command warns that the file contains the secret key, that it must be stored only on encrypted storage (password manager attachment, encrypted volume), and that a digital kit lingering on unencrypted or synced storage collapses the two-factor model toward passphrase-only strength.
- **Recognizable naming and repo hygiene.** Default render names follow `healthspan-recovery-kit-<date>.<ext>`; `*recovery-kit*` patterns are added to the repository `.gitignore` so a kit generated during development or testing can never be committed.
- **Orphan sweep.** Kit renders are included in the startup failure-cleanup required by security.md: a crash between render and disposal is caught at the next start.

The *printed* kit remains governed by ADR-0013's existing physical-custody warning (safe, safety deposit box); this ADR governs only the digital artifact.

### Positive Consequences
- The spec stops promising erasure that modern storage cannot deliver; the honesty standard now covers disposal
- No plaintext health database is ever silently left on disk; `db encrypt` retention becomes a visible, deliberate, warned choice
- The secret key's digital footprint is bounded: keychain, paper, and (only by explicit choice) a warned-about file
- One policy, three applications — future commands that create plaintext artifacts have a rule to follow rather than a precedent to misread

### Negative Consequences / Tradeoffs
- `db encrypt` gains flags and a prompt — slightly more ceremony for a one-time migration command
- Users without full-disk encryption are told the truth: pre-encryption plaintext may be unrecoverable-in-principle to erase; some will find this unsettling (it was always true; now it is stated)
- Windows printing still requires a transient kit file — avoidance is a preference, not an absolute, and the spec says so

## Consequences for Other Documents
- **security.md**: Temporary Files section restated (avoidance-first bullet added; "Secure deletion" bullet replaced with best-effort disposal and its enumerated defeats; FDE backstop; kit renders added to the applies-to list); Migration path paragraph rewritten to the verified-then-dispose flow
- **ADR-0013** (Accepted): navigation link only — `Extended by ADR-0033`; content untouched per governance
- **ADR-0028** (Proposed): Related link — `rotate-secret-key` regenerates the kit, so rendering/disposal rules apply
- **.gitignore**: `*recovery-kit*` patterns
- **glossary.md**: "Best-effort disposal" term added; "Recovery Kit" entry notes the digital-render rules
- **Architecture review**: items 2.4, 2.5, 2.6 resolved

## Pros and Cons of the Options

### Status quo
- Pro: nothing to write
- Con: an overpromise in a security document, an indefinite plaintext health database presented as a feature, and an unspecified secret-key artifact — the review's findings stand unaddressed

### Drop overwriting entirely
- Pro: simplest honest position; no false comfort
- Con: discards a near-free control that still works on traditional filesystems; honesty does not require disarmament

### Layered policy applied uniformly (chosen)
- Pro: honest about every layer's real value; structural control first; safety-by-ordering for `db encrypt`; bounded secret-key footprint
- Con: more specified behavior (prompts, flags, print-pathway branching) to implement and test

## Links
- Extends: [ADR-0013](0013-encryption-at-rest.md) — Recovery Kit digital artifact handling; honest-limits standard
- Extended by: [ADR-0047](0047-crypto-surface-implementation-decisions.md) — WI-2 kit-render scope; print pathways and orphan sweep deferral
- Related: [ADR-0028](0028-key-derivation-and-rotation.md) — kit regeneration on secret-key rotation; the verify-before-destructive-step pattern
- Related: [ADR-0019](0019-multi-device-sync.md) — sync version history as one of the disposal defeats
- Related: [ADR-0015](0015-data-export.md) — deliberate plaintext exports are user custody, out of scope here
- Related: [ADR-0034](0034-clinical-document-storage.md) — applies this policy to import-source files and document renders
- Related: [specs/security.md](../security.md) — Temporary Files and Encryption at Rest sections carry the policy
- Resolves: [architecture review 2026-06-10](../reviews/architecture-review-2026-06-10.md), items 2.4, 2.5, 2.6
