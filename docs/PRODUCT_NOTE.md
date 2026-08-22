# Product note

## Stretch problem addressed

The product addresses trust and reliability. It makes source status visible, applies customer-specific contract overrides only after an authorized account lookup, excludes deprecated policy from current evidence, marks historical ticket guidance unverified, and refuses confident entitlement calculations when material facts are missing.

## What comes next

1. Replace mock auth with SSO and durable per-account permissions.
2. Move the action ledger to a durable workflow/ticket system with approvals and notifications.
3. Add evaluation traces and a curated adversarial test set for conflict, staleness, and prompt-injection cases.
4. Extend the supplied-data recurring-ticket analysis into proactive issue detection once durable ticket history and operational telemetry are available.

## Intentionally left out

External ticketing, email, CRM mutation, production customer identity, and a persistent managed database are excluded because the assessment asks for a free, local mock and the supplied pack has no external integration credentials. Render restarts therefore reset the mock ledger.

## Useful product metric

Measure **verified first-response resolution rate**: the percentage of staff questions whose answer is accepted by a reviewer without correction and includes valid authoritative citations. Track it alongside escalation precision so the system is not rewarded for over-escalating every uncertain question.
