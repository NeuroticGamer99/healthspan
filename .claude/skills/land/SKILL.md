---
name: land
description: Pre-commit landing checklist — run the local gates, verify personal-data containment and ADR governance, draft the Decisions: section, and propose the commit message. Use when a work item is ready to commit.
---

# /land — pre-commit landing procedure

Walk these steps in order for the change currently in the working tree. Report each step's outcome briefly; stop and report if any step fails.

## 1. Survey the change

`git status` and `git diff` (plus `git diff --cached` if anything is staged). Confirm the set of files about to land matches the work item — no stray edits, no leftover scratch files.

## 2. Run the gates that exist locally

Run whatever the repository currently has; skip what doesn't exist yet and say so:

- `python scripts/check_adr_index.py` — ADR index consistency (always, if `specs/adr/` or its README changed).
- `python scripts/check_spec_links.py` — spec cross-link integrity (**always** — it validates link targets anywhere in the repo, so a rename or deletion *outside* `specs/` can break a spec link; CI runs it unconditionally in the docs-consistency job, ADR-0061).
- Once the code phases land (see `specs/development-plan.md` Phase 0): `ruff check`, `ruff format --check`, `pyright`, `pytest -n auto` — run each if configured in the repo. The `-n auto` (pytest-xdist) is the intended local invocation — the suite is isolated for worker parallelism (see the dev-dependency comment in `pyproject.toml`); only CI runs serial, for its ordered `tee-sys` log capture. Don't add `-n` to `addopts` in `pyproject.toml` — that would leak into CI's invocation.

A failing gate stops the landing; fix or escalate before proceeding.

## 3. Personal-data containment check

- Verify nothing under `specs/personal/` is staged or would be committed: `git status --porcelain` must show no `specs/personal/` paths (it is gitignored; its appearance means the ignore broke — treat as critical).
- For every added or modified file outside `specs/personal/`, confirm it contains no personal health values, lab results, diagnoses, medications, or owner-identifying information. Test fixtures must be synthetic.

## 4. ADR governance check (if `specs/adr/` is touched)

- No Accepted ADR's decision content is modified (only status-field corrections, `## Links` navigation additions, typo/link fixes are permitted in place).
- New or status-changed ADRs are reflected in the `## Index` table of `specs/adr/README.md`.

## 5. Draft the `Decisions:` section

Walk the CLAUDE.md decision-capture routing rules (1–6) against the change. For every design decision the change embodies that the specs left open, confirm the owning record was created or updated *in this same change*, and list the links. If the change genuinely surfaces no such decision, the section reads `Decisions: none`. Never omit the section.

## 6. Review invocation (when warranted)

If the change includes non-trivial code or spec-conformance risk and the `spec-reviewer` / `test-reviewer` agents have not already run on it, recommend running them before the commit. Note when a phase boundary or security-critical change (encryption, key derivation, tokens, process boundaries) warrants suggesting `/code-review` or `/code-review ultra` to the user — `ultra` is user-triggered and billed separately; only the user launches it.

## 7. Propose the commit message — then stop

Compose the commit message:

- Imperative-mood title summarizing the change.
- Body explaining what and why, referencing the ADRs/specs involved.
- The `Decisions:` section from step 5.
- The co-author trailer naming the model running *this* session (read it from the system prompt; never carry one forward).

Present the message and **stop**. `/land` proposes; `/ship` disposes.

The user lands it by invoking **`/ship`**, which commits with this message, pushes, opens the PR, waits for CodeRabbit's review, and triages it. If the user instead replies "commit" (the pre-`/ship` habit), treat that as the go and run `/ship`.

**Never commit or push from this skill**, and never without that explicit go.
