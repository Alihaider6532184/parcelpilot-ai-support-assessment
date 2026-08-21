# ParcelPilot Customer Support - Architecture and Build Plan

## 1. Product decision

Build an **internal ParcelPilot support/operations staff chatbot**, rather than a
customer-facing bot.  The assessment is centered on staff making evidence-backed
operational decisions (including escalations and service credits), and an internal
surface can safely expose the account, order, ticket, contract, and authority
context required to do that.  It also makes role and account-scope simulation
straightforward without building a customer identity platform.  The agent will
answer with concise guidance, citations to retrieved source passages, and a clear
separation between a recommendation and an executed action.

The selected stretch goal is **Problem 2: trust and reliability**.  Source
precedence, document currency, agreement applicability, and uncertainty are core
requirements already; making them visible and enforceable produces a coherent,
high-value extension at considerably less risk than adding a weakly supported
proactive analytics feature.

## 2. Target architecture and repository layout

```text
.
├── PLAN.md
├── .gitignore
├── README.md                         # setup, demo roles, deployment and limits
├── data/
│   └── raw/                          # supplied PDFs and workbook; committed
├── backend/
│   ├── app/
│   │   ├── main.py                   # FastAPI application and CORS
│   │   ├── config.py                 # validated environment configuration
│   │   ├── api/                      # auth, chat, confirmation endpoints
│   │   ├── agent/                    # orchestration, prompt, model adapters
│   │   ├── tools/                    # retrieval, structured lookup, actions
│   │   ├── data/                     # ingest, SQLite queries, repository guards
│   │   ├── reliability/              # source ranking and evidence validation
│   │   ├── schemas/                  # Pydantic request/response models
│   │   └── services/                 # retry/backoff, audit and chat state
│   ├── tests/
│   ├── requirements.txt
│   ├── Dockerfile
│   └── render.yaml
├── frontend/
│   ├── app/                          # Next.js App Router pages
│   ├── components/                   # chat, tool timeline, evidence, confirm UI
│   ├── lib/                          # typed backend client and session helper
│   ├── public/
│   ├── package.json
│   └── vercel.json
└── docs/
    ├── architecture.md
    ├── source-catalog.md
    ├── threat-model.md
    └── demo-script.md
```

The backend is the trust boundary: only it can read source data, perform tool
calls, calculate decisions, or mutate the local mock-action store.  The frontend
only renders server-provided messages, tool events, citations, and confirmation
controls.

## 3. Data ingestion and source catalog

### Raw sources and time basis

`data/raw/` remains version-controlled.  The workbook README establishes the
dataset snapshot as **2026-08-16 11:00 Asia/Kolkata**; load it at ingestion and
expose it as `dataset_now` to calculations and responses.  Do not use the wall
clock for assessment-time calculations.  The application will normalize this
timestamp to an aware ISO-8601 value while preserving the source timezone.

On application startup (or first request after a cold start), the backend will:

1. Compute a SHA-256 manifest for all seven source files and compare it to the
   local Chroma collection metadata.
2. If missing or mismatched, extract each PDF page with page number and section
   heading where detectable; normalize whitespace; split on headings/paragraph
   boundaries into approximately 450-token chunks with 75-token overlap.
3. Embed chunks locally with `sentence-transformers` `all-MiniLM-L6-v2` and write
   vectors, text, metadata, and source hash to an embedded Chroma collection.
   This avoids an additional paid API and keeps document content local.
4. Read the three workbook data sheets into typed pandas frames, validate required
   columns and referential links (`orders.account_id`, `tickets.account_id` to
   `accounts.account_id`), then load them into a read-only SQLite database.
   `README` is parsed separately for snapshot metadata, rather than treated as a
   table.  SQLite parameter binding is mandatory for all queries.

The generated Chroma directory and SQLite files are ignored and recreated from
committed raw data.  On Render's ephemeral disk this happens after a cold start;
the small assessment pack keeps this acceptable, while the UI communicates that
the service may take time to wake.  The mock action ledger is intentionally
ephemeral too, because the action is a local assessment mock rather than a
production workflow.

### Required metadata on every source chunk

| Field | Example / use |
| --- | --- |
| `document_id`, `file_name`, `page`, `chunk_id` | Stable citation and deduplication |
| `source_type` | `policy`, `sop`, `product_guide`, `agreement`, `ticket_history` |
| `status` | `current`, `deprecated`, `active`, or `context_only` |
| `effective_date`, `updated_date`, `term_start`, `term_end` | Freshness and applicability checks |
| `account_id` | `ACCT-001` / `ACCT-002` for agreements; null for general documents |
| `authority_rank` | Agreement 400, current policy/SOP 300, current product guide 200, deprecated 0, ticket context 0 |
| `topics` | cancellation, service_credit, severity, SLA, product_issue |
| `supersedes` / `superseded_by` | Explicit v2/v3 relationship |

The agreement-to-account mapping is derived from the `accounts.contract_file`
column and validated against the agreement's own account identifier.  The
deprecated v2 document is indexed only for historical discovery/audit, always
labeled deprecated, and excluded from answer-evidence retrieval by default.
Ticket `historical_resolution` is loaded as `context_only`; it can help explain
what happened before, but can never be used as a rule or sole support for an
answer.

## 4. Agent, tool, and orchestration design

### Model strategy

The primary adapter calls a Groq-hosted Llama model that supports structured tool
calling.  A model-provider interface isolates request/response translation; a
Gemini free-tier adapter is the fallback after retryable Groq failures.  Provider
keys live only in Render environment variables (`GROQ_API_KEY`, optionally
`GEMINI_API_KEY`) and are never sent to Next.js.  Each provider call uses a short
exponential backoff with jitter (for example 1, 2, 4 seconds, maximum 3 retries),
honours `Retry-After`, and exposes a friendly retryable error after the budget is
exhausted.  Requests have an idempotency/chat-turn key so a retry cannot duplicate
an action proposal.

The system prompt is a behavioral layer, not a security control.  It instructs
the model to use tools for factual claims, cite approved evidence, disclose
snapshot time, never invent a record, distinguish recommendation from execution,
and call out unresolved conflicts.  It is given a compact, server-produced
evidence packet with authority labels rather than arbitrary raw database access.

### Exact tool contracts

The model sees the following four functions.  All arguments are validated with
Pydantic; each result is validated and redacted by the server before returning to
the model.  `account_id` is useful for routing but never expands authorization.

1. `search_documents`

   - Description: Search authoritative PDFs for a policy, contract, SOP, or
     product-operating question; return ranked, scope-checked passages and source
     metadata.
   - Parameters:
     ```json
     {"query":"string","account_id":"string|null","topics":["string"],"include_context_only":false}
     ```
   - Return shape:
     ```json
     {"results":[{"citation_id":"string","text":"string","document_id":"string","file_name":"string","page":1,"section":"string|null","source_type":"string","status":"string","account_id":"string|null","authority_rank":300,"applicable":true,"warnings":["string"]}],"retrieval_warnings":["string"]}
     ```

2. `lookup_records`

   - Description: Fetch source-pack account, order, and ticket facts and the
     dataset snapshot.  It is a structured lookup only; it returns no policy
     conclusion.
   - Parameters:
     ```json
     {"record_type":"account|order|ticket","record_id":"string","include_related":true}
     ```
   - Return shape:
     ```json
     {"dataset_now":"ISO-8601 string","record":{"record_type":"string","fields":{}},"related":{"account":{},"orders":[{}],"tickets":[{}]},"scope":{"account_id":"string","authorized":true}}
     ```

3. `evaluate_entitlement`

   - Description: Deterministically evaluate a cancellation or failed-pickup
     service-credit scenario from already authorized order/account facts and
     server-selected current governing clauses.  It returns calculations and
     unresolved facts, not a mutation.
   - Parameters:
     ```json
     {"order_id":"string","evaluation_type":"cancellation|service_credit","reported_pickup_at":"ISO-8601 string|null"}
     ```
   - Return shape:
     ```json
     {"order_id":"string","account_id":"string","evaluation_type":"string","result":"eligible|not_eligible|needs_verification","fee_inr":0,"credit_inr":0,"governing_sources":["citation_id"],"facts_used":{},"missing_or_conflicting_facts":["string"],"manager_approval_required":false,"recommended_next_step":"string"}
     ```

4. `propose_escalation`

   - Description: Create a non-mutating, server-stored draft escalation/follow-up
     from authorized data.  It cannot update a ticket or create an external case.
   - Parameters:
     ```json
     {"account_id":"string","order_id":"string|null","ticket_id":"string|null","reason":"string","severity":"P1|P2|P3","evidence_citation_ids":["string"]}
     ```
   - Return shape:
     ```json
     {"proposal_id":"UUID","status":"pending_confirmation","summary":"string","payload_preview":{},"expires_at":"ISO-8601 string","confirmation_phrase":"Confirm escalation"}
     ```

The actual state change is deliberately **not model-callable**.  `POST
/api/actions/{proposal_id}/confirm` is invoked only when the authenticated user
presses Confirm (or submits the exact confirmation phrase).  Its server-side
contract is `{ "confirmed": true }` and it returns `{ "action_id": "UUID",
"status": "created", "created_at": "ISO-8601", "action": {} }`.  It checks
proposal ownership, freshness, authorized account scope, status, and explicit
confirmation before writing the local action ledger.  Replays return the existing
action by idempotency key rather than double-writing.

### Multi-step routing and response loop

For each turn, the backend records a correlation ID and sends the model the
allowed tool names plus current, server-side session scope.  The normal loop is:

```text
user request -> model chooses a tool -> server authorizes/executes it
             -> normalized result + timeline event -> model chooses next tool
             -> reliability gate -> cited answer or pending confirmation
```

The loop permits up to six sequential calls per turn, stops on a final answer,
and detects repeated identical calls.  A query such as a cancellation request
naturally follows `lookup_records(order)` -> related account ->
`search_documents(account/topic)` -> `evaluate_entitlement` -> optional
`propose_escalation`; no sample ID, customer name, or answer is hardcoded.  The
frontend receives streaming status events such as “Looking up order”, “Checking
Northstar agreement”, “Evaluating cancellation terms”, then the final cited
answer.  It never presents model chain-of-thought.

## 5. Authentication and data-layer authorization

This is a mock identity system, explicitly labeled in the UI and README.  A demo
sign-in endpoint selects one of seeded users and returns an httpOnly signed
session cookie.  Each session has `user_id`, `role`, and `allowed_account_ids`:

| Role | Scope | Permitted capabilities |
| --- | --- | --- |
| `support_agent` | Assigned account IDs | Read records/docs in scope; create escalation proposals |
| `ops_manager` | All supplied accounts | Read all; create and confirm proposals |
| `viewer` | Assigned account IDs | Read only; cannot propose or confirm |

Authorization occurs in repositories/tools, never by trusting an LLM argument or
prompt instruction:

* `lookup_records` first resolves the requested record server-side, derives its
  `account_id`, then rejects it unless `account_id in session.allowed_account_ids`
  (or the role has an explicit all-account grant).  SQL is parameterized and every
  related-record query adds that same server-derived account predicate.
* `search_documents` rewrites the filter from the session scope.  General current
  documents are available to authorized staff; customer agreements are included
  only for the derived/authorized account.  A model-supplied account ID can narrow
  a search, never widen it.  Deprecated/context-only material remains excluded by
  policy unless an explicitly authorized audit mode is added later.
* `evaluate_entitlement` resolves the order again through the guarded repository;
  it does not accept a fee, account, or policy clause from the model.
* `propose_escalation` and the confirmation endpoint independently re-check scope,
  role, evidence IDs, and proposal owner before a draft or ledger row is written.

The action ledger/audit log stores the authenticated user, account, timestamp,
input IDs, governing citation IDs, resulting status, and correlation ID; it does
not store secrets or unconstrained model prompts.

## 6. Source reliability and decision policy

The reliability module performs these deterministic checks before answer evidence
is passed to the model and before `evaluate_entitlement` returns a result:

1. Establish account identity from an authorized order/ticket/account record.
   No account means no customer-specific agreement may apply.
2. Consider an agreement only when its metadata account matches, its status is
   active, and `dataset_now` falls inside its stated term.  Its clause overrides
   a general clause only for that account and subject.
3. Use applicable signed customer agreement first; then the relevant **current**
   Support Policy v3 or current Cancellation & Service Credit SOP; then current
   Product Operations Guide for product facts/workarounds.  The general policy
   does not override a current SOP on a more specific cancellation/credit rule.
4. Never use Support Policy v2 for a current answer.  It is retained only as
   clearly labeled historical/audit material.  Historical ticket resolutions and
   internal notes are context only and are always marked unverified.
5. Require direct evidence for all material decision inputs.  For credits this
   includes the order/account, scheduled-window end, observed/reported pickup
   time, carrier fault, customer fault, and the applicable clause.  Missing,
   stale, or contradictory facts yield `needs_verification`, not an optimistic
   value.
6. If equally applicable authoritative sources materially conflict, the agent
   names the conflict, avoids a promise or action, and proposes escalation with
   the conflicting citations.  P1, confirmed credential exposure, and breached
   response targets are immediately recommended for escalation.  The UI displays
   source badges: “contract override”, “current policy”, “current product guide”,
   “deprecated - excluded”, and “historical context - unverified”.

This means the model can phrase a conclusion but cannot silently decide which
source wins.  For example, the evaluator applies Northstar's active
BOOKED-before-pickup cancellation waiver rather than the default INR 250 rule,
but applies a customer agreement only after guarded order/account lookup.

## 7. Explicit confirmation experience

When escalation is advisable, the agent explains why and invokes
`propose_escalation`.  The response renders a review card showing account,
linked order/ticket, severity, reason, sources, and a “Confirm escalation”
button.  The state is `pending_confirmation`; no persistent action exists yet.

On click, the frontend sends only `confirmed: true` with the session cookie to
the protected confirmation endpoint.  The server reauthorizes, checks the draft
has not expired or already been confirmed, writes the mock local ledger, and
returns a visible success event.  Cancelling/dismissing leaves the proposal
unexecuted.  A plain chat “confirm” is accepted only if it is bound to exactly one
visible pending proposal owned by that same session; otherwise the server asks
the user to choose.  This makes confirmation unambiguous and prevents model text
from triggering a state change.

## 8. User interface

The Next.js page contains: a compact mock-role selector/login; account-scope
badge; chat transcript; live tool-activity timeline; final answer with expandable
evidence cards; uncertainty/conflict callouts; and confirmation cards.  Each tool
event is rendered from a typed server event (not inferred from model prose), so
the user sees each lookup/retrieval/evaluation/action proposal in sequence.

Answers state their authoritative basis, snapshot time when time matters, and
whether the result is a recommendation, verified calculation, or a request for
verification.  Errors distinguish a cold-start/loading state, transient free-tier
rate limit, unauthorized record, and unavailable provider without leaking keys or
backend stack traces.

## 9. Hosting and operational plan

* **Backend:** Dockerized FastAPI on a Render free web service.  Set the source
  files in the repository, `GROQ_API_KEY` (and optional `GEMINI_API_KEY`) as
  Render secrets, restrict CORS to the deployed Vercel origin, expose a
  lightweight `/healthz`, and use `PORT` supplied by Render.  Do not expose
  provider keys or SQLite/Chroma files publicly.
* **Frontend:** Next.js on Vercel free tier.  `NEXT_PUBLIC_API_BASE_URL` contains
  only the Render backend URL; Vercel has no LLM secrets.  Configure the backend
  CORS allowed origin after the Vercel URL is known.
* **Cold starts:** Render free services can spin down.  The frontend shows a
  “Waking support service…” status with a bounded retry for idempotent chat
  submission and an explicit retry button.  The API health endpoint stays cheap;
  the data index loads lazily from the committed source pack.  This is a demo
  trade-off, not hidden availability behavior.
* **Rate limits:** one active turn/session; capped tool loop; provider retry with
  jitter; fallback to Gemini for eligible failures; clear “try again shortly”
  response on exhaustion.  No paid service, credit card, or Hugging Face Spaces
  is used.

## 10. Ordered implementation tasks

1. Confirm clean repository state, retain the supplied `data/raw/` files, and add
   README, license, and local environment template without secrets.
2. Scaffold FastAPI and Next.js projects under the planned folders; pin Python and
   Node dependencies compatible with free hosting.
3. Define shared API/Pydantic schemas for sessions, chat events, citations, tool
   inputs/results, action proposals, and errors.
4. Implement a source manifest, document catalog, PDF extraction, chunking,
   metadata assignment, local embedding, and Chroma collection rebuild path.
5. Implement workbook validation, README snapshot parsing, typed SQLite loading,
   indexes, and repository query methods.
6. Implement seeded mock users, signed httpOnly sessions, role grants, logout,
   and a frontend role selector labeled as demo auth.
7. Implement repository-level authorization guards and tests proving a model/tool
   cannot read an out-of-scope order, ticket, account, or agreement.
8. Implement document retrieval filtering/ranking, citation construction, and
   tests for current/deprecated and general/customer-specific document behavior.
9. Implement deterministic entitlement evaluation with date/time arithmetic,
   agreement override selection, credit caps, missing-data detection, and
   table-driven tests using arbitrary fixture rows rather than hardcoded runtime
   answers.
10. Implement the local mock-action ledger, proposal lifecycle, confirmation
    endpoint, idempotency, expiry, audit fields, and tests that unconfirmed or
    replayed proposals do not create a second action.
11. Implement Groq and Gemini model adapters, structured tool dispatcher, loop
    limits, retry/backoff, and provider-failure tests with mocked clients.
12. Implement the system prompt and evidence-packet builder; ensure it contains
    no authority or access-control mechanism that is absent from server code.
13. Implement chat/session endpoints with server-sent event (or fetch streaming)
    tool-status messages and persisted per-session conversation/proposal state.
14. Build the Next.js chat interface, tool timeline, evidence cards, uncertainty
    labels, empty/loading/error states, and protected confirmation card.
15. Add the two provided scenarios plus cross-account denial, deprecated-policy,
    agreement-override, insufficient-evidence, conflict, rate-limit, and
    confirmation tests; add a manual demo script covering the full flow.
16. Run backend unit/integration tests, frontend lint/type checks and production
    builds; fix all failures and manually verify the UI with seeded roles.
17. Create Render and Vercel configuration, deploy with free tiers, set secrets
    and CORS, and run a deployed smoke test for chat, tool timeline, and a
    confirmed mock escalation.
18. Update README with architecture diagram, run/deploy instructions, demo users,
    known free-tier limits, safety decisions, and assessment checklist; commit
    the implementation in logical changes.

## 11. Assumptions made

1. The supplied raw pack is the full assessment corpus and should be committed;
   no external customer or policy source will be added.
2. The README's 2026-08-16 11:00 Asia/Kolkata snapshot, not the developer's
   machine time, is the authoritative “now” for every time-dependent scenario.
3. The assessment expects a local mocked action; it is acceptable for actions to
   reset on Render restart as long as the UI/README disclose that behavior.
4. Local sentence-transformer embeddings are permitted under the free/no-card
   constraint; the first cold start may need to fetch/cache the model artifact.
5. The GitHub CLI authentication has authority to create the requested public
   repository in the current authenticated user's account.
6. A staff user can be assigned one or more customer accounts; an ops manager may
   have an explicit all-account grant.  This demonstrates restricted access even
   though the target chatbot is internal.
7. “State-changing action” will mean creation of a local escalation/follow-up
   record.  It will not call external ticketing, email, or CRM systems.
8. The signed agreement is an override only while active and only for the named
   account; silent extrapolation to other customers is unsafe.
9. The standard support policy, current SOP, and current product guide cover
   different subjects; for the same subject an applicable agreement wins, while a
   more specific current SOP governs cancellation/credits over a generic policy.
10. The frontend and backend can be hosted as separate free services, with users
    accepting normal Render cold-start latency and free-tier LLM throughput limits.
