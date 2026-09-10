# Round 1 — spool-1-gate-output

## Angle record

- **Date:** 2026-09-09
- **Loop:** external
- **Effort:** max
- **Surface:** `scripts/run_gates.py`, `tests/test_run_gates.py`, ADR-0080, `specs/adr/README.md`
- **Base (resolved):** d0ca6d0eabc070e99d76355d99bfbfb2a28734b9
- **HEAD:** 0b23eabb9cd1b620ceb74e0a8df7a540808731d8
- **HEAD tree:** 6adf2c881433c4f19b3e9d078bed8925cbd85b22
- **Diff size:** 1042 lines (1042 excluding the ledger)
- **Lenses dispatched, and each one's status** (`not-run` / `ran-no-report` / `reported`)**:**
  `/code-review max` — **reported** (15 findings plus a below-cap list; emitted no
  `ReportFindings` call and no verdict labels, which is the `max` contract);
  `/codex:adversarial-review` — **reported** (1 finding, `[high]`, verdict needs-attention)
- **Brief revision stamp:** `cd25d737`
- **Angles briefed:** whole-artifact (no exclusion list); the remedy angle over three local
  smokes' fixes; the economy angle over the ADR and the added comment prose; the retention
  reversal and its containment surface; exit-code integrity through the quiet path
- **Angles executed:** eleven finder angles plus a dedup and a verification pass under lens A,
  with a separate agent running 26 mutations; a design-challenge pass under lens B answering the
  brief's seven uncertainties by number. One lens-A angle produced a phantom — a red `pytest`
  gate — that the reviewer itself refuted: it had run against a tree another agent was
  mutating, with reviewer isolation not in force.
- **Briefed but not executed:** the ADR's Context measurement paragraph was examined only by
  lens B, and only as far as "honestly labelled, not re-derivable"; neither lens checked the
  figures. Neither lens executed `/land`, `/ship` or `/savepoint` — the brief's uncertainty #7
  asked precisely this and it remains inspection-only. Lens B's Windows partial-deletion path is
  inferred from file lifetimes and scan logic, not reproduced end to end.
- **Examined:** ADR-0080's number, index row and reciprocal links; multi-exception `except`
  clauses; whether the personal directory's location is ever named; positional `Gate(...)` /
  `Context(...)` construction (AST-scanned); whether `ci.yml` invokes `run_gates.py`; whether
  `pytest-02.log` can enter the canary glob; whether any skill greps or pipes gate output, and
  `/land` step 3a's dependence on the exempted gate.

### Do-not-re-run, carried into this round

| Excluded | Paths | Cleared at | Cleared by | Evidence |
|---|---|---|---|---|
| _(none — first external round on this branch)_ | — | — | — | — |
