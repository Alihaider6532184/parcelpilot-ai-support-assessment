# Product note

## Stretch problem addressed

The implemented stretch feature is cross-customer operational issue detection. An operations manager can ask whether complaints or product problems are recurring across customers. The `analyze_operations` tool queries the real SQLite `tickets` table, normalizes ticket-subject terms, groups materially similar subjects, and counts distinct account IDs. A group is called cross-customer only when it reaches the requested minimum of at least two accounts; repeated tickets from one customer are shown separately. With the supplied snapshot, the tool reports that no significant issue currently recurs across two or more customers and identifies any same-customer repeats as such. Non-manager sessions cannot widen this analysis to all accounts.

The product also addresses answer trust. Structured facts and calculations are separated from document retrieval; customer agreements are scoped to the authenticated account; current sources override deprecated material; agreement clauses override general rules only for their customer; and state-changing escalations require an explicit second confirmation. Cancellation and service-credit answers are calculated from real records and timestamps, not composed by the language model.

## What I would build next, in priority order

1. Replace demo identities with company SSO, durable user/account assignments, audit logs, and server-side session storage.
2. Replace the SQLite action ledger with a durable ticketing/workflow integration that supports approval policies, notifications, cancellation, and retriable delivery.
3. Add a larger labeled evaluation set and production telemetry for routing accuracy, retrieval relevance, citation validity, access denials, entitlement correctness, latency, and provider fallback rates.
4. Upgrade recurring-issue detection from subject-token overlap to clustering over durable ticket history plus product telemetry, with time windows, severity weighting, trend baselines, and drill-down evidence.
5. Add an operator-facing conversation/history store after defining retention, tenant isolation, deletion, and redaction requirements.

## Intentionally left out

Real customer authentication, CRM/help-desk mutation, email or messaging, durable conversation storage, a managed production database, and automated external escalation delivery are not present. The supplied assessment data is a fixed snapshot, so the application does not pretend to provide real-time carrier or customer state. Render storage is ephemeral, making the confirmation tool a demonstrable safety workflow rather than a durable production action system. The UI is non-streaming and does not expose internal model reasoning; it shows only user-visible answers and tool/status badges.

## Success metric

The primary metric would be **verified first-response resolution rate**: the percentage of support questions accepted by a reviewer without factual correction, with the correct authorized scope and valid governing citations. It should be segmented by tool and monitored alongside unauthorized-data exposure rate (target: zero), entitlement calculation accuracy, and unnecessary-escalation rate so a higher resolution rate cannot be achieved by hiding uncertainty or over-escalating.
