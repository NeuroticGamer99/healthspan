# Manual Entry Quickstart

How to go from a fresh checkout to entering real lab results with the Phase 3 CLI. This is
the pre-distribution path: there is no installer or release yet ([development-plan.md](development-plan.md)
Phase 8); `uv run` from a checkout is the supported way to run the platform until then.

Owning ADRs for the pieces this touches: [ADR-0046](adr/0046-filesystem-layout-and-config-discovery.md)
(config/data locations), [ADR-0047](adr/0047-crypto-surface-implementation-decisions.md) /
[ADR-0028](adr/0028-key-derivation-and-rotation.md) (init, keys, Recovery Kit),
[ADR-0049](adr/0049-core-service-skeleton-implementation-decisions.md) (service start),
[ADR-0059](adr/0059-cli-manual-entry-implementation-decisions.md) (the entry CLI).

## Prerequisites

- [uv](https://docs.astral.sh/uv/) installed.
- A clone of the repository. If you also develop in another checkout, see
  [One database, many checkouts](#one-database-many-checkouts) below — the database is
  per-machine, not per-checkout.

```
git clone <repo-url> healthspan-testing
cd healthspan-testing
uv sync
```

## Bootstrap: zero to first result

```
uv run healthspan config path     # where config resolves from (a file is optional;
uv run healthspan config show     #   defaults: loopback:8464, platform-dirs data path,
                                  #   backups daily / retain 14)
uv run healthspan init            # credentials + encrypted database + sidecar
uv run healthspan db migrate      # apply schema migrations; seeds the reference data
                                  #   (biomarker catalog, labs, categories, frameworks)
```

**Save the Recovery Kit.** `init` renders it to the terminal (add `--output <path>` for a
file). It is your only offline copy of the key material — store it *outside* any repository
checkout (password manager, or print it and delete the file, per
[ADR-0033](adr/0033-plaintext-artifact-disposal.md)). Two-factor mode (secret key in the OS
keychain + master passphrase) is the default; `--key-from-passphrase` is the single-factor
portable alternative.

Then two terminals:

```
# Terminal A — the Core Service (foreground; Ctrl+C stops it)
uv run healthspan service start
```

The first start mints the default token set and prints it once to stderr; the CLI's
`cli-admin` token is stored in the OS keyring (Windows Credential Manager) automatically —
no manual token handling is needed for the commands below.

```
# Terminal B — entry and readback
uv run healthspan enter           # draw-level template: lab + draw date once, then results
uv run healthspan results list    # readback; also: draws / biomarkers / labs / frameworks
```

`enter` resolves biomarker names against the canonical∪alias namespace and offers to
record a confirmed alias for an unrecognized name. Range flags come from the lab's own
reference range by default; `--framework <name>` opts into a seeded framework's ranges
instead. `--help` on any command is the authoritative surface.

## After each session

1. Stop the service (Ctrl+C in Terminal A).
2. `uv run healthspan db backup` — must report a **verified** backup. Real data deserves a
   backup before the next session. (`db migrate`/`db backup`/`db restore` refuse while the
   service is running — the single-instance guard, working as intended.)

## One database, many checkouts

Config and database live in per-user platform directories ([ADR-0046](adr/0046-filesystem-layout-and-config-discovery.md)),
**not** in the checkout — every checkout on the machine operates on the same database and
the same keyring credentials. That makes a dedicated testing clone safe *for running*, with
one discipline:

> **Run `db migrate` and `service start` against your real database only from `main`** (the
> testing clone). A development checkout on a work-in-progress branch may carry unreleased
> migrations; migrating the real database from there strands it ahead of what `main` can
> open. The service refuses on a schema mismatch either way — the guard catches the
> mistake, but the backup is what undoes it.

## While you use it: what to record

Real entry sessions are the design input several deliberately deferred decisions are
waiting on ([open-questions.md](open-questions.md)). Keep session notes in
`specs/personal/manual-entry-notes.md` (gitignored — personal values belong there and
nowhere else in the repository). Worth a line whenever:

- a command errors (exact command + full output);
- a range flag surprises you — especially: a below-detection `<x` flagged `indeterminate`,
  a value exactly on a range boundary, a non-fasting glucose flagged `above`, or a
  **first entry in molar units (mmol/L)** — each of those is a named open-questions
  trigger firing;
- a hormone result has no range and you wish it did (the cohort-dimension gap);
- the entry flow makes you re-type or fight it (manual-entry efficiency);
- readback disagrees with what you typed (value fidelity).

A session summary safe to share outside `specs/personal/`: number of draws and results,
flag distribution, categories touched, and which (if any) triggers fired — no values.
