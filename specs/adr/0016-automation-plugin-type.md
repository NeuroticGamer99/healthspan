# ADR-0016: Automation Plugin Type

## Status
Proposed — stub

## Context and Problem Statement
Users will want rule-based automated responses to health data events: "when fasting insulin is above X, run HOMA-IR analysis and flag the result"; "when a new Function Health panel is imported, send a summary notification"; "when body weight drops below Y, trigger a body composition analysis job." These automations require a trigger (an event condition), a condition (optional filter), and one or more actions.

This is a named plugin type in the plugin architecture (ADR-0010) but its interface is not yet designed.

## Decision Drivers
- Automations are event-driven — they subscribe to the event stream (ADR-0011, via SSE) and react to matching events
- Conditions may involve querying the database (e.g. "if the new value is more than 2 SD from the mean of the last 10 results")
- Actions may include: submitting a job, publishing an event, calling a REST endpoint, triggering a notification
- The automation engine is first-party code hosted in the Automation Host process — not in the Core Service (ADR-0025)
- Automations written by users should have the same expressive power as first-party automations

## Decision Outcome
TBD — design after the event bus (ADR-0011) and job abstraction (ADR-0012) are implemented and patterns are understood from real use.

## Placeholder: Plugin Type Declaration

The `automation` plugin type is reserved in ADR-0010. A plugin declaring `PLUGIN_TYPES = ["automation"]` will be recognized by the loader. The interface contract is TBD.

## Host Process and Execution Model (decided by ADR-0025)

Automation code executes in the **Automation Host** process — never in the Core Service. The host subscribes to the event stream (`GET /v1/events`, SSE with `Last-Event-ID` replay), evaluates triggers and conditions, and performs actions exclusively through the Core REST API with a scoped bearer token.

Automations take two forms with one execution locus:

- **Declarative rules** — trigger/condition/action definitions are *data* (config or database rows submitted via REST). The first-party rule engine in the Automation Host interprets them. The Core Service may store and validate rules; it never executes them.
- **Code automations** — `automation` plugins containing arbitrary Python, loaded by the Automation Host from the plugins directory.

Both forms see the same events and can invoke the same actions — equal expressive power, per the decision drivers. Condition queries ("is the new value more than 2 SD from the mean of the last 10 results?") run via the Core REST API, not direct database access.

## Design Requirements for Future Work

### Execution tracing

When the automation interface is designed, it must include an execution trace system. Each automation run should record which trigger fired, which conditions were evaluated (and their results), and which actions executed (with outcomes). Traces should be published as events on the bus (`automation.run.*`) and queryable via the REST API for debugging.

Home Assistant's automation trace system (recording variables, branch decisions, and step outcomes at each execution step, visible in the UI) is strong prior art. Without tracing, "why didn't my automation fire?" is unanswerable.

### Scheduled triggers

Time-based triggers (intervals and cron expressions) are supported by the event bus scheduler (ADR-0011, `schedule.*` events). The automation engine should treat these identically to any other event trigger — no special time-handling code in the automation layer.

### State machines

Health data has natural state machine patterns that go beyond simple trigger/condition/action rules:

- **Treatment protocols**: baseline → intervention → monitoring → reassessment → next decision
- **Alert escalation**: normal → elevated → critical → acknowledged → resolved
- **Lab monitoring sequences**: initial draw → follow-up ordered → follow-up received → reviewed
- **Medication titration**: starting dose → lab check → dose adjustment → lab check → stable

Home Assistant's lack of a state machine primitive is one of its most persistent community complaints — users simulate state machines with helper entities and complex automation chains.

When the automation interface is designed, consider whether state machines should be:
- A built-in primitive in the automation engine (states, transitions, guards, entry/exit actions)
- A separate plugin type (`state_machine`) that publishes state transition events to the bus
- A documented pattern for plugin authors using existing primitives

The choice depends on how frequently real use cases require multi-step stateful workflows versus simple reactive rules.

## Comparable Prior Art
- Home Assistant automations (YAML-based trigger/condition/action) — includes execution trace UI
- Node-RED flow-based programming
- AWS EventBridge rules

## Links
- Constrained by: [ADR-0025](0025-plugin-host-process-matrix.md) — automations execute in the Automation Host process; declarative rules are data, never code in the Core Service
- Related: [ADR-0010](0010-cli-plugin-model.md) — plugin type system
- Related: [ADR-0011](0011-event-bus.md) — automations are event subscribers
- Related: [ADR-0012](0012-job-abstraction.md) — automations may submit jobs as actions
- Related: [ADR-0017](0017-notification-channels.md) — notifications are a common automation action
