# specs/reviews/ — Point-in-Time Review Records

Historical snapshots of project reviews — architecture/security reviews and ADR
consistency reviews produced at milestones. Each captures findings **as of its
date**; those findings have since been transferred into ADRs,
[open-questions.md](../open-questions.md), and the design documents. Read these
for provenance, not current state — they are immutable historical artifacts, not
living documents.

**New review reports belong here**, not in the top-level `specs/` directory.

One exception to the "immutable historical artifacts" description above: the
`angle-ledger/` subfolder is a **living** record, not a snapshot.
It accumulates one fragment per external review round while a branch is open, and
`/squash-merge` collapses a branch's fragments into a single per-PR digest — see
[ADR-0072](../adr/0072-review-pipeline-and-ledgers.md). It is indexed below as a
directory, never a row per round. The subfolder itself is created by the work item that
implements the pipeline; until then its index row below is forward-looking.

| Record | Date | Scope |
|--------|------|-------|
| [architecture-review-2026-06-10.md](architecture-review-2026-06-10.md) | 2026-06-10 | Architecture & Security Review |
| [architecture-review-2026-07-06.md](architecture-review-2026-07-06.md) | 2026-07-06 | Architecture & Security Review |
| [architecture-review-2026-07-06-worklist.md](architecture-review-2026-07-06-worklist.md) | 2026-07-06 | Work plan for the 2026-07-06 review |
| [architecture-review-2026-07-07.md](architecture-review-2026-07-07.md) | 2026-07-07 | Architecture & Security Review |
| [adr-review-2026-07-17.md](adr-review-2026-07-17.md) | 2026-07-17 | ADR & spec consistency sweep |
| [architecture-review-2026-07-19.md](architecture-review-2026-07-19.md) | 2026-07-19 | Architecture & Security Review (first against implemented code) |
| [architecture-review-2026-07-19-worklist.md](architecture-review-2026-07-19-worklist.md) | 2026-07-19 | Work plan for the 2026-07-19 review, with PR mapping |
| `angle-ledger/` | ongoing | Review-round ledger — angle roster and round record per external round ([ADR-0072](../adr/0072-review-pipeline-and-ledgers.md)) |
