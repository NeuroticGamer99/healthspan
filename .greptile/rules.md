# Greptile review lens

Repo-wide review guidance for Greptile. The per-convention rules that carry a
file scope live in `config.json`'s `rules[]` — ADR governance, decision capture,
security invariants, the `except`-clause and PowerShell conventions. This file
carries what does not fit a scoped rule: how to weigh a finding, and what not to
report at all.

Sibling reviewer configs teach the same conventions to the other bots:
`.coderabbit.yaml` (CodeRabbit) and `.gemini/styleguide.md` (the Antigravity SDK
reviewer). A convention added or changed in one must be mirrored in the others.
The authoritative sources are `CLAUDE.md` and `specs/`; all three are distilled
lenses over those, kept lean on purpose.

## How to weigh a finding

- **Correctness and design first.** This is a local-first, single-owner system:
  one loopback service, one owner, no multi-tenancy. A concurrency, performance
  or hardening finding is real only at the scale and threat model the system
  actually has — state which you are applying, with numbers where they exist,
  rather than assuming a hostile multi-tenant deployment.
- **Check the owning ADR before proposing a change.** A fix that contradicts an
  Accepted ADR needs a superseding ADR, not a code edit. Say which ADR you
  checked when a finding touches a decided area.
- **Establish whether the code or the comment is the wrong one.** When code and
  its comment, docstring or spec disagree, say which you believe is authoritative
  and why. A reorder proposed against a correct implementation because its
  comment was stale is a regression, not a fix.
- **Report every instance of a pattern you flag, not the first.** If a
  convention is violated in three places, a finding naming one reads as though
  the other two are fine.

## What not to flag

- Style already gated by CI: `ruff` lint and format, `pyright` strict,
  PyMarkdown markdown style (ADR-0062), the ADR-index and spec-link checks.
  These block the merge on their own; a comment restating them is noise.
- Dev-tooling or reviewer configuration that carries no product, API, schema or
  security contract and gates no CI job — this file, `config.json`,
  `.coderabbit.yaml`, editor config. Per `CLAUDE.md` these route to rule 6:
  the config is its own record and the change's `Decisions:` section reads
  "none". Do not ask for an ADR for them.
- Restatements of the diff, and findings on lines the diff does not touch.
- Missing tests for a change whose tests are present elsewhere in the same PR —
  the suite is one tree (`tests/`), not colocated with the module.
