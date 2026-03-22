# ADR-0017: Notification Channel Plugin Type

## Status
Proposed — stub

## Context and Problem Statement
Alert plugins (ADR-0010) and automation actions (ADR-0016) generate notifications. Where those notifications are delivered is a separate concern: desktop notification, email, SMS, Slack, webhook, in-app inbox. Different users have different delivery preferences, and new channels should be addable without modifying the alert or automation logic.

## Decision Drivers
- Notification delivery must be decoupled from the alert/automation logic that generates the notification
- The separation follows the event bus pattern: alerts publish events; notification channel plugins subscribe and deliver
- Multiple channels should be active simultaneously (e.g. desktop notification + email for critical alerts)
- Channels should be configurable (SMTP credentials, webhook URL, etc.) via TOML config
- The `notification_channel` plugin type is reserved for this purpose

## Decision Outcome
TBD — design after automation plugin type (ADR-0016) interface is defined, since notification channels are the primary action target.

## Placeholder: Plugin Type Declaration

The `notification_channel` plugin type is reserved in ADR-0010. Initial channels likely to ship as first-party plugins:
- Desktop notification (OS native, via `plyer` or PySide6)
- Webhook (HTTP POST to a configured URL)
- Email (SMTP)

## Links
- Related: [ADR-0010](0010-cli-plugin-model.md) — plugin type system
- Related: [ADR-0011](0011-event-bus.md) — channels subscribe to alert events on the bus
- Related: [ADR-0016](0016-automation-plugin-type.md) — notifications are a common automation action
