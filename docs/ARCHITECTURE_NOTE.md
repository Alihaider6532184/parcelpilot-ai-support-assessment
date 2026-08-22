# Architecture note

## Running system

ParcelPilot is a two-service application: a Next.js 14 App Router frontend on Vercel and a FastAPI backend on Render. The frontend implements the demo-role login, chat transcript, per-turn tool badges, pending-confirmation cards, and the explicit confirmation request. It sends credentialed requests directly to the configured backend URL. FastAPI signs the selected demo identity into an HTTP-only cookie and creates one application-level `Runtime` during startup. That runtime owns the shared repository, document tool, analytics tool, and action ledger for the process; these objects are not rebuilt for every chat turn. A shared provider adapter and session-keyed minimal record context are also process-level objects.

On startup, the backend reads `ParcelPilot_Assessment_Data.xlsx`, validates that every order and ticket belongs to a known account, and loads the `accounts`, `orders`, and `tickets` sheets into indexed SQLite tables. It also records the workbook's dataset snapshot time. The six PDFs are extracted in layout mode and split at numbered headings and `KI-*` known-issue headings. Long sections are bounded to about 450 words with 75-word overlap. Every chunk receives its document, page, section, status, account, topic, effective-date, authority, manifest, and index-version metadata.

## Agent and real tools

Every chat turn selects exactly one of five implemented tool contracts:

- `search_documents` retrieves general policy, SOP, agreement, product-guide, and known-issue evidence.
- `lookup_records` reads account, order, or ticket facts and scope-aware listings from SQLite.
- `evaluate_entitlement` resolves an authorized real order and deterministically calculates cancellation eligibility/fees or failed-pickup service credits.
- `analyze_operations` queries actual SQLite ticket rows and groups similar ticket subjects to identify patterns that affect at least two distinct customer accounts; same-customer repeats are reported separately.
- `propose_escalation` creates only a short-lived mock escalation proposal in `pending_confirmation` state.

For ambiguous language, Groq receives all five native function declarations with required tool choice. The adapter retries retryable failures twice with short exponential backoff, then uses Gemini native function calling as the fallback. Hosted routing is capped by `MODEL_DAILY_LIMIT` (40 by default) per backend process-day. Explicit actions, cross-account reads, aggregate analytics, known-issue/policy reads, record IDs, and cancellation/credit scenarios have deterministic intent guards. These guards bypass hosted latency and quota when confidence is high, and correct an unsafe model selection before execution. If no provider is configured or routing fails, the local fallback still selects one of the same server tools. The model can select a tool and propose arguments, but it never reads SQLite or executes a state change itself.

`evaluate_entitlement` is a code path, not an LLM answer generator. An explicit order ID is fetched directly. For a customer-named scenario without an ID, the repository resolves the named authorized account, filters its real orders by the supplied status, timing, and fault facts, and rejects ambiguous matches. The evaluator then uses workbook timestamps and dataset time, applies the current SOP calculation, parses any applicable signed-agreement threshold, fixed credit, or cancellation waiver, and returns the real order facts plus governing clause IDs. Missing material facts return `needs_verification`; they never produce a guessed credit.

## Hybrid document retrieval and source reliability

The vector store is a persistent cosine-distance Chroma collection. Ingestion and querying both use the same stable 384-dimensional feature-hashed word, adjacent-word, and character embeddings. Supplying embeddings explicitly prevents Chroma from downloading or loading its default 79 MB ONNX model during startup and again in a request worker, which had caused Render memory restarts. The index manifest and `INDEX_VERSION` force a rebuild when source files or the chunk/embedding format changes. A JSON lexical sidecar is always written, and search falls back to lexical mode if Chroma is unavailable.

Search is hybrid rather than vector-only. Chroma similarity is combined with per-chunk lexical rarity, synonyms, heading matches, inferred topics, exact `KI-*` identifiers, customer/account matches, and plan-capability boosts. Ranking is relevance-first; authority is a tie-breaker rather than a reason to promote generic boilerplate. Customer names are resolved from agreement metadata. Account-specific agreements are included only when they match the authorized query account.

Reliability rules are deterministic: the active signed customer agreement overrides a general current SOP or policy for that customer; current sources supersede deprecated policy; the current product guide is lower-authority operational guidance; and ticket-history/context-only material is excluded from current answers unless explicitly requested as unverified context. Entitlement responses cite the selected agreement and SOP sections actually used.

## Access control and confirmation

Access control is enforced after intent classification and again where data or actions are touched:

- The signed session contains the role, allowed account IDs, `all_accounts`, and a unique session ID.
- `Repository._allowed` checks the resolved account for every ID-based lookup and entitlement resolution. Listing requests reject `other_accounts` or `all_accounts` for scoped roles instead of silently substituting another tool.
- `DocumentTool` excludes account-specific agreement chunks that do not match the authorized account scope; an account argument may narrow scope but cannot widen it.
- `AnalyticsTool` rejects all-account analytics unless `session.all_accounts` is true and otherwise filters SQL by allowed account IDs.
- The escalation dispatcher rejects viewers before argument resolution, then resolves order, ticket, or account IDs through the scoped repository.
- `ActionTool` independently rechecks role and account scope when proposing and confirming. Confirmation also requires the same user that created the proposal.

The action flow is deliberately two-step. `propose_escalation` stores a proposal for ten minutes and returns its preview, expiry, and `pending_confirmation` status without writing an action. Only `POST /api/actions/{proposal_id}/confirm` with `confirmed: true` writes the mock action. False confirmation is rejected, expired proposals cannot execute, another user cannot confirm the proposal, and repeating an already successful confirmation returns the same action ID rather than creating a duplicate.

## Trade-offs and limits

Authentication and role assignments are assessment-only demo identities. SQLite, Chroma, conversation context, and the action ledger are process-local; Render replacement or restart rebuilds source data and loses pending/mock actions. The recurring-issue detector is deterministic token-overlap grouping over the supplied ticket subjects rather than a learned clustering system. The lightweight feature-hashed embeddings trade some semantic depth for deterministic startup, no external embedding cost, and reliable operation within hosted memory. The frontend is intentionally a compact non-streaming chat rather than a full support console.
