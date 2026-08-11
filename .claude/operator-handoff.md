# Operator handoff — printing something the user has to act on

The single authoritative statement of **how** a session presents a filesystem path or a command
the user has to open, copy, or run. The skills that hand one back — `/land`, `/review-prep`,
`/review-handoff`, `/apply-review`, `/ship` — cite this file rather than restating it. Decision
record: [ADR-0073](../specs/adr/0073-operator-handoff-presentation.md).

Named without step numbers on purpose. `scripts/check_doc_citations.py` gates that each of those
skills still cites this file; it cannot see a step *number*, so an ordinal here would be a
cross-reference nothing checks — and this change renumbered two skill sections while it was being
written, which is the drift arriving during its own remedy.

Each of those five already said *that* it should surface the thing, and each said it in its own
words: "print both paths", "resolved and absolute", "the path of the carrier file", "name the
file". **Three of them — `/land`, `/review-prep`, `/ship` — named neither absoluteness nor form**,
and `/apply-review` named absoluteness only in a clause quoting `/review-handoff`, while its own
fallback instruction named neither. That spread is the tell: the presentation was never decided
anywhere, only written five times.

## The contract

A **handoff target** is a path or a command the user has to act on: open it in the editor, paste
it into the next command, carry it to another session. When you emit one:

1. **Absolute**, whenever the target lives outside the workspace — which every scratchpad
   artifact does. A repository-relative path is fine for a repository file the user will open in
   the IDE; it is never fine for anything under the session scratchpad.
2. **Resolved — no placeholder of any kind survives.** Not `<scratchpad>`, `<branch>`,
   `<timestamp>` or `$sp`, and **not any other** angle-bracket, `$`-prefixed or fill-in-the-blank
   token: the rule is the shape, and the four names are examples rather than the list to check
   against. A closed enumeration is how `/code-review <effort>` reached a user as a literal
   template that satisfied every named rule. This file's recipes and the skills' recipes are
   templates; what reaches the user is not.
3. **Alone, inside its own fenced `text` block** — one target per block, nothing else in it, so
   a triple-click or the block's copy button selects exactly the thing and nothing more. Two
   paths means two blocks.
4. **Never elided.** No `…` standing in for a directory segment, and never folded into the
   middle of a prose sentence. Prose may say what the file *is*, before or after the block.

A command target obeys the same four, and its embedded paths obey them too: a fenced command
carrying an unresolved `<scratchpad>` is a template the user cannot run, which is worse than
prose, because it looks runnable.

## Why the fence

The VS Code chat webview renders assistant prose as unselectable text; a fenced block gets a
hover *copy* button. So a path in a sentence is not merely untidy in the surface the owner
actually reads — it is the difference between one click and retyping by hand. Long paths are
therefore not a reason to abbreviate: length costs the reader nothing once the block carries a
copy button, which is why `/review-handoff` states outright that its report path being long
"is fine".

## Why absolute, and why "resolved" is not pedantry

The session scratchpad root contains a **per-session UUID** —
`…/claude/<project-slug>/<session-uuid>/scratchpad`. It exists nowhere the reader can look it
up. An elided or templated scratchpad path is therefore not inconvenient but *unusable*: no
amount of care at the receiving end reconstructs the missing segment. A prose-embedded absolute
path is the milder version of the same failure — correct, present, and still requiring manual
re-typing to use.

Measured, 2026-08-10, `/land` step 7 on the branch that became PR #86: the composed commit
message was reported as `…/scratchpad/commit-msg/fix/copilot-request-confirmation.txt` in the
middle of a sentence. The user had to ask for it a second time. The reprint that worked — the
full path alone in a fenced block — is what the contract above generalizes.

## What this does not govern

- **A path named in explanation rather than in handoff.** "The gate lives in
  `scripts/check_personal_containment.py`" hands over nothing to copy; a code span is right
  there and a fenced block would be noise.
- **Paths written into an artifact** — a report, a commit message, a PR body. Those are read by
  an agent or by GitHub, not selected out of a chat webview, and their own skills fix their
  form.
- **A multi-line recipe a skill already fences.** It is a fenced block already; rule 3's
  one-target-per-block does not ask you to split a shell script into one block per path.

## What is gated, and what is not

`scripts/check_doc_citations.py` asserts that this file exists and that each of the callers above
still cites it — the one failure that citing rather than restating introduces, where a rewritten
skill keeps the behaviour in different words and quietly loses the pointer. It runs in CI's
`docs-consistency` job.

**That script is a registry of several owning documents, not this one's caller list.** It carries a
row per document — this file's callers, and separately `.claude/bot-review-triage.md` §1's, whose
list is one entry long and equally hand-maintained. Adding a citing site to *either* rule means
adding a row there as well as the citation; editing only the tuple that happens to be nearest is
how a new site ends up governed by nothing. Some rows also register a **needle** — an extra string
the citation must carry — for a document a caller could name for an unrelated reason. The gate
covers drift in a known set, not discovery.

Nothing gates the rule itself. Whether a path appearing in conversational output was one the
operator needed to act on is the same unmechanizable judgment
[ADR-0070](../specs/adr/0070-personal-data-containment-gate.md) declined for the personal-data
containment rule's content half — so a green gate says the citations survived, never that anyone
obeyed them. The check that matters is the author's, at the moment of writing:

> Could the user act on this by copying exactly what I printed, without editing it?

If not, reprint it.
