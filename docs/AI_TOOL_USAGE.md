# AI tool usage

Codex was used for planning and implementation. Codex read the approved architecture specification, generated the initial plan, then implemented the FastAPI backend, ingestion/retrieval, guarded tools, reliability rules, confirmation flow, tests, Next.js UI, deployment files, and documentation. The access-control enforcement and conflict-resolution logic were manually reviewed against the supplied PDFs and workbook, with tests added for cross-account denial, agreement precedence, deprecated policy exclusion, unverified ticket context, and confirmation idempotency.
