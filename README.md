# healthspan

**Your health data, analyzed on your terms.**

healthspan is a local-first personal health data platform. It aggregates lab results, continuous glucose monitoring, body composition, wearable activity, and clinical history into a single encrypted database — then exposes that data to AI-driven analysis through an open, extensible, plugin-driven architecture.

Think of it as Home Assistant for personal health data: a stable, privacy-respecting core with a hackable plugin ecosystem where the line between built-in capability and your own extensions is intentionally blurred.

---

## Why this exists

Personal health data is spread across labs, patient portals, wearables, CGM apps, and clinical records — each in a different format, each siloed, none talking to the others. Making sense of it requires normalizing years of data from sources that don't agree on units, biomarker names, or reference ranges.

Beyond the data problem, there's an analysis problem. Fasting insulin from Quest and fasting insulin from a hospital lab are not the same number — different immunoassay platforms produce systematically different values. Any analytical tool that doesn't track lab source alongside every result will produce misleading trends. healthspan is built around this reality.

The goal: a single longitudinal record of your health data, queryable by AI and scriptable by you, with security and privacy treated as design requirements rather than afterthoughts.

---

## Who it's for

**Privacy-first users** — The database is AES-256 encrypted at rest (SQLCipher) from first run. The encrypted file is safe for cloud backup; the storage provider sees ciphertext. The MCP server supports any MCP-compatible AI client, including fully local LLMs — when you use a local model, your health data never leaves your machine.

**Technical and security-aware users** — The full REST API is exposed and documented. Every interface is versioned. The plugin system gives you the same extension points used by first-party code. You can script against the API, build your own importers, add custom analysis functions, or connect external tooling — without modifying the core.

**N=1 data analysts and biohackers** — Multi-source lab normalization with canonical biomarker names. Reference range frameworks that go beyond lab flags — compare your results against longevity-optimized targets or practitioner-specific optimal ranges alongside the standard lab ranges. Longitudinal trend analysis across years of data. CGM correlation with periodic labs. Clinical event and intervention timelines as first-class overlays on every analysis.

**Home Assistant users who want the same for health** — A micro-kernel architecture where first-party functionality ships as plugins against the same interfaces available to you. Replace, extend, or fork any component. Add import adapters for new data sources. Register new MCP tools that your AI client can call. Write automation rules that react to your health events. The installed package is a distribution mechanism, not a black box.

---

## Architecture

healthspan is built as a set of independent processes with well-defined, versioned interfaces:

```
GUI (PySide6)           ─┐
MCP Server (fastmcp)    ─┤→  Core Service (FastAPI, REST API v1)  →  SQLite (SQLCipher)
Import Pipeline         ─┤       ↑ auth, validation, event bus
CLI + plugins           ─┘
```

- **Core Service** — the only process that owns the database. Versioned REST API. Authentication on every endpoint. Input validation at the boundary.
- **MCP Server** — exposes named tools to any MCP-compatible AI client. AI-provider agnostic.
- **GUI** — standalone PySide6 desktop client. Replaceable: it's just another API client.
- **CLI (command-line interface)** — first-class scripting layer with a directory-scanning plugin system. Drop a `.py` file into the plugins directory; new commands appear.
- **Event bus** — SSE-based by default. ZeroMQ and MQTT available as adapter plugins for more complex deployments.

Full architectural documentation is in [`specs/`](specs/), including 20+ Architecture Decision Records covering every major design choice.

---

## Data sources

The platform is designed around these data types, with importers planned or in progress:

| Data type | Sources | Status |
|---|---|---|
| Lab results | Quest (via primary care and Function Health), patient portals | Partially designed |
| Body composition | InBody 120, InBody 580 (via Enara Health) | Partially designed |
| Continuous glucose | Levels (CSV export), Dexcom API | Partially designed |
| Metabolic context | Levels (zone scores, glucose response) | Not yet designed |
| Activity logs | Levels (food, exercise, notes) | Not yet designed |
| Nutrition logs | Levels (detailed macros/micros) | Not yet designed |
| Wearable / activity | Fitbit (Takeout), Apple Health XML | Partially designed |
| Clinical events | Manual entry | Table defined |
| Interventions | Manual entry (with full dose history tracking) | Table defined |
| Clinical documents | Manual entry; future: patient portal export, PDF import | Not yet designed |

All data enters through a structured import pipeline: full-batch validation before any write, atomic transactions (all-or-nothing per import), dry-run mode, and explicit conflict policies (reject, skip, or upsert) — no silent data mutation.

Multi-source lab handling is a first-class design concern: every result row carries its lab source, reference ranges are stored per-row rather than per-biomarker, and canonical biomarker names normalize naming inconsistencies across labs.

Clinical documents and visit notes are a prioritized data type — clinician narrative captures reasoning, differential diagnoses, and interpretation context that structured lab values cannot express. MCP tools can surface relevant visit notes alongside lab trends, enabling an AI client to answer questions like "what did my cardiologist say about my LDL trajectory?" or "summarize all provider guidance on my insulin resistance."

---

## Plugin system

The plugin architecture is designed for the same kind of extensibility that made Home Assistant the dominant home automation platform.

- Drop a `.py` file or Python package into the plugins directory — no build step required. Plugins that need pip packages (pandas, numpy, etc.) declare them and the loader installs from a curated, version-locked catalog automatically.
- Plugins can register: CLI commands, MCP tools, import adapters, analysis functions, query patterns, reference range frameworks, automation rules, notification channels
- Plugins can provide services to other plugins via a namespaced service registry (`quest.parser`, `quest.api_client`, etc.)
- Built-in functionality ships as first-party plugins against the same interfaces — there is no privileged internal API
- Plugin API versioning with compatibility range declarations (`PLUGIN_API_MIN_VERSION`, `PLUGIN_API_MAX_VERSION`)

---

## Security model

- **Encryption at rest** — SQLCipher AES-256. Two-factor key model: a randomly generated secret key (stored in the OS keychain) combined with a user master passphrase, derived via Argon2id. A printable Recovery Kit enables cross-device recovery. Passphrase-only mode available for headless or cross-device use cases.
- **Local-first** — no cloud service required. All data stays on your machine by default.
- **AI client agnostic** — works with any MCP-compatible AI client. Use a local LLM and your health data never leaves the machine.
- **Authenticated API** — bearer token on every endpoint. Host header validation and CORS allowlist protect against DNS rebinding attacks even on localhost.
- **Plugins are trusted-user code** — they run with your privileges and are not sandboxed. Only install plugins you have read and trust.

See [`specs/security.md`](specs/security.md) for the full security requirements and threat model.

---

## Project status

**Early development — design phase in-progress, implementation not yet started.**

The full architecture is currently documented across 20+ ADRs covering application architecture, plugin system, event bus, job abstraction, encryption, data model, and more. Work continues on the basic architecture specifications. No installable release exists yet.

If you want to follow along or contribute, the [`specs/`](specs/) directory is the place to start. [`specs/design-rationale.md`](specs/design-rationale.md) covers the core design philosophy. [`specs/open-questions.md`](specs/open-questions.md) lists known items that need to be decided.

---

## Important notes

### Not medical advice

This is a personal data organization and analysis tool. It is not a medical device or clinical decision support system. Nothing this software produces — including AI-assisted analysis — constitutes medical advice. Consult a qualified healthcare provider before making health decisions.

### Lab data has inherent limitations

Laboratory results are not directly comparable across different platforms and assay methods. The platform tracks lab source alongside every result for this reason, and the documentation covers cross-lab variability in detail. Even so, no software can account for all sources of analytical variability. Do not treat trend analysis as clinically definitive.

### AI analysis has limitations

AI-generated analysis can be incorrect, incomplete, or misinterpreted. When using a cloud-based AI client, your health data leaves your machine during analysis — review the privacy policy of your chosen AI client. When using a local LLM, no data leaves your machine.

### No warranty

This software is provided as-is. See [NOTICE](NOTICE) and [LICENSE](LICENSE) for full terms.
