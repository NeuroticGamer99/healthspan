# ADR-0036: Plugin Package Installation Integrity

## Status
Accepted

## Context and Problem Statement
[ADR-0024](0024-plugin-extensions.md) (Accepted) gave plugins `PLUGIN_PACKAGES` and a curated catalog of approved packages at pinned versions. The [architecture-review-2026-06-10.md](../architecture-review-2026-06-10.md) (item 2.7) found two gaps in it:

1. **Version pins don't authenticate content.** A pin says *which release* was meant, not *what bytes* arrive. A compromised package index (or a re-uploaded artifact) can serve different content under the same version string, and the loader would install it silently.
2. **The loader installs before it validates.** ADR-0024's loader sequence installs `PLUGIN_PACKAGES` (step 4) before building the dependency graph and checking cycles and conflicts (steps 5–7) — so a plugin that is about to fail validation gets its packages installed anyway, mutating the environment for nothing.

ADR-0024 is Accepted and immutable; this ADR extends it. It changes the catalog's form (hash-pinned lockfile), the installation mode (`--require-hashes`), and the loader's ordering (validate before install). It does not change ADR-0024's core decisions: the catalog-vs-off-catalog split, uniform catalog versions across plugins, the no-resolver scope boundary, and the trust boundary all stand.

## Decision Drivers
- A version pin must not be mistaken for content authentication — the platform's security posture should be honest about what each control actually verifies
- pip's `--require-hashes` mode is all-or-nothing: every package in a resolution, including transitive dependencies, must carry a hash or the install fails — so hashing cannot be bolted onto a name/version list; the catalog's form must change
- Producing and maintaining hashes must be mechanical (generated at release time), never hand-maintained
- A plugin that will fail validation should leave no trace — neither installed packages nor executed code
- Off-catalog installs are declared user's-own-risk territory (ADR-0024's security boundary); controls there should inform, not theater

## Decision Outcome

### 1. The catalog is a hash-pinned lockfile

The catalog is no longer a list of `(name, pinned version)` pairs. It is a **fully resolved, hash-pinned requirements set**: every approved package *and its complete transitive dependency closure* at locked versions, each entry carrying sha256 hashes — multiple `--hash` values per package (one per published wheel platform plus the sdist; any match passes, per pip's requirements-file semantics).

The catalog is generated at Healthspan release time with `uv pip compile --generate-hashes` (or `uv export`) and ships with the release, under the same locked-version security model as Healthspan's own dependencies. It is never edited by hand.

### 2. Catalog-governed installs run in `--require-hashes` mode

To install a plugin's catalog-governed packages, the loader computes the transitive closure of the requested names *within the catalog* — well-defined because ADR-0024 locks all catalog packages to uniform versions across plugins — and passes exactly those hash-pinned lines to the installer with `--require-hashes`.

**A hash mismatch is a hard fail**: the install aborts with a loud, specific error naming the package, the expected hash, and what arrived. Never a warning, never a fallback to an unhashed install. A mismatch means either index compromise or a stale catalog; both demand a human, and an automatic downgrade path would quietly convert the control into decoration.

### 3. Off-catalog packages: hashes recommended, not required

Off-catalog packages have no catalog entry to hash against. The declaration syntax accepts optional pip-style inline hashes:

```python
PLUGIN_PACKAGES = ["some-obscure-lib==0.4.1 --hash=sha256:abc123..."]
```

A hash-bearing off-catalog declaration installs in require-hashes mode (which extends the requirement to its transitive dependencies — an author supplying hashes must supply them for the closure). A declaration without hashes installs under ADR-0024's existing warn-and-confirm gate, whose warning text now states explicitly: **a version pin does not authenticate content**.

Hashes are recommended rather than required off-catalog because requiring them would mostly train users to paste hashes from the same page they downloaded the package from — verification theater. The honest control set for off-catalog is: the confirmation gate (informed consent), the publication age gate (ADR-0020, see below), and optional hashes for authors who maintain them.

**Conflict with the platform's own lockfile.** All plugin packages — catalog and off-catalog alike — install into the single `uv tool` environment Healthspan itself runs in (ADR-0023/0024). An off-catalog pin (`cryptography==41.0.0`, `pandas==2.2.3`) can therefore collide with a version Healthspan's own release lockfile pinned, including a security-relevant transitive dependency of the Core Service — silently downgrading or displacing it for every plugin and for Healthspan itself, not just the plugin that declared it. Before installing an off-catalog package, the loader diffs its resolved name and version against Healthspan's own release lockfile (already available — it is what generates the catalog in §1); a conflicting version is refused outright, with an error naming the package, the version the plugin requested, and the version the platform requires. This is a hard fail, not the warn-and-confirm gate above: an off-catalog author may decide to risk their own plugin; they may not decide to weaken Healthspan's own dependency set.

### 4. The loader validates before it installs

ADR-0024's loader sequence is reordered — package installation moves after all validation:

1. Scan `plugins_dir` for `.py` files and directories with `__init__.py`
2. Read declaration metadata **statically** (see below); collect `PLUGIN_TYPES`, `PLUGIN_VERSION`, `PLUGIN_API_MIN_VERSION`, `PLUGIN_API_MAX_VERSION`, `PLUGIN_PACKAGES`, `PLUGIN_DEPENDENCIES`
3. Validate API version compatibility; skip and warn on mismatch
4. Build dependency graph from `PLUGIN_DEPENDENCIES`
5. Detect cycles; fail with a clear error if found
6. Detect version conflicts in `PLUGIN_DEPENDENCIES`; fail with a clear error — do not resolve
7. **Install packages declared in `PLUGIN_PACKAGES`** (catalog-governed in `--require-hashes` mode; off-catalog per §3)
8. Load in topological order (providers before consumers)
9. Call `register(context, api_version)` for each plugin
10. First-party plugins load first; user plugins may override built-in service registrations

A plugin that fails validation never gets its packages installed.

### 5. Metadata extraction is static — validation runs no plugin code

ADR-0024's step 2 said "import each module header," but importing executes top-level code. That was already latently impossible in the original ordering — headers were read (step 2) before packages were installed (step 4), so a plugin importing its own dependencies at module top level would have crashed the scan. This ADR makes the latent requirement explicit: **declaration metadata is extracted statically (AST-level), without importing the module**. Declarations must therefore be literal assignments readable without execution.

Combined with the reorder, the guarantee becomes: a plugin that fails validation neither executes any code nor installs any packages. The first plugin code to run is the module import at load time (step 8), after every gate has passed.

### Interaction with ADR-0020's publication age gate

The age gate ([ADR-0020](0020-plugin-registry.md)) and the hash lock are complementary controls on the same supply chain — temporal versus integrity. A version can pass the age gate and still be substituted at download time; a hash can match bytes that were malicious from the day they were published. Neither subsumes the other. Three consequences:

- **Catalog generation must honor the age gate.** A catalog-governed install never resolves anything at install time — it installs the closure frozen at Healthspan release time — so the runtime age check is vacuous for catalog packages. The gate's discipline therefore moves upstream: the release-time resolution that produces the catalog **must itself enforce `min_release_age_days`** on every version it locks. Otherwise a version published yesterday gets locked into the catalog and distributed to everyone — precisely the fresh-compromise attack the gate exists for, now with a hash certifying the malicious bytes.
- **The hash pin is the strong form of ADR-0020's "then pinned" requirement.** The gate requires versions to be age-checked at install time *then pinned* so the loader never pulls an unvetted fresh version later. A bare version pin delivers that only weakly (a compromised index can re-serve different bytes under the same version); the sha256 pin makes it content-level.
- **Off-catalog is where the gate stays load-bearing at install time.** Off-catalog installs resolve at the moment the user confirms — exactly the fresh-compromise window. There the two controls stack independently: the gate says "old enough," the optional hash says "these bytes."

**Scope note:** this ADR covers pip *package* integrity only. Integrity of the plugin artifact itself (registry-side hashing or signing of plugin downloads) is registry infrastructure and remains future ADR-0020 work.

### Positive Consequences
- Catalog-governed installs are content-authenticated end to end; a compromised index cannot substitute artifacts for locked versions
- A plugin that fails validation leaves the environment untouched — no installed packages, no executed code
- Catalog maintenance stays mechanical: hashes come from the release-time resolver, not human transcription
- ADR-0020's install-time pinning requirement is upgraded from version-level to content-level at no extra user-facing cost

### Negative Consequences / Tradeoffs
- The catalog grows from the approved list to its full transitive closure and must be regenerated each release (mechanical, but a release-process obligation)
- A stale catalog hash (e.g. an artifact legitimately re-uploaded — rare but not unknown) hard-fails installs until a catalog update ships; this is the correct failure mode but a support cost
- Static metadata extraction constrains declarations to literal assignments (no computed `PLUGIN_PACKAGES`); this is a real, if small, expressiveness loss — and the right one, since computed declarations would defeat pre-execution validation
- Off-catalog packages without hashes remain content-unauthenticated; the platform is explicit about this rather than pretending otherwise

## Links
- Extends: [ADR-0024](0024-plugin-extensions.md) — catalog form, installation mode, and loader ordering; core decisions unchanged
- Related: [ADR-0020](0020-plugin-registry.md) — publication age gate; catalog generation must honor it (see interaction section)
- Related: [ADR-0010](0010-cli-plugin-model.md) — the loader being reordered
- Related: [ADR-0023](0023-distribution-mechanism.md) — `uv` toolchain that generates the catalog and installs into the tool environment
- Related: [specs/security.md](../security.md) — plugin supply-chain paragraph
- Related: [specs/testing-strategy.md](../testing-strategy.md) — hash-mismatch rejection, validation-installs-nothing, and static-extraction test targets
- Resolves review item 2.7 from [architecture-review-2026-06-10.md](../architecture-review-2026-06-10.md)
- Resolves review item 2.8 from [architecture-review-2026-07-06.md](../architecture-review-2026-07-06.md)
