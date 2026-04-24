# Claude Code Companion Spec — Financial Data Layer

Terse, implementation-ready. Cross-references to modular briefs under `/technical/financial-data-layer/`.

**Repos in scope**
- `github.com/dac-uk/curo-backend` (Vapor 4 / Swift 5.9 / Postgres 16 / Fluent / Fly.io LHR)
- `github.com/dac-uk/curo-ios` (SwiftUI)

**Reading order**
1. `/technical/financial-data-layer-brief.md` (master context — read fully first)
2. `/technical/financial-data-layer/00–08*.md` (modular briefs)
3. `/technical/financial-data-layer/diagrams/*.mermaid`
4. This spec (action-oriented addendum)

---

## Backend module layout

```
Sources/App/
  Domain/
    Canonical/              # canonical types (structs)
      CanonicalAccount.swift
      CanonicalTransaction.swift
      CanonicalHolding.swift
      CanonicalInvestmentTransaction.swift
      CanonicalBalance.swift
      Money.swift                 # minor-unit integers + currency
      ISOCurrencyCode.swift
      SyncCursor.swift
    Ports/
      LinkPort.swift
      AccountsPort.swift
      TransactionsPort.swift
      HoldingsPort.swift
      InvestmentTransactionsPort.swift
      BalancesPort.swift
      IdentityPort.swift
      PaymentsPort.swift
      StatementIngestPort.swift
      WebhookReceiverPort.swift
    Models/                 # Fluent models = canonical persistence
      Institution.swift
      ProviderLink.swift
      Account.swift
      Balance.swift
      Transaction.swift
      Security.swift
      Holding.swift
      InvestmentTransaction.swift
      Category.swift
      StatementArtifact.swift
      SyncRun.swift
  Services/
    LinkService.swift
    AccountsService.swift
    TransactionsService.swift
    HoldingsService.swift
    InvestmentTransactionsService.swift
    BalancesService.swift
    InstitutionService.swift
    CategorizationService.swift
    CanonicalisationService.swift
    StatementIngestService.swift
    ManualEntryService.swift
    SyncOrchestrator.swift
    WebhookRouter.swift
    PaymentsService.swift
  Integrations/
    Plaid/
      PlaidAdapter.swift
      PlaidClient.swift            # raw HTTP
      PlaidCanonicaliser.swift     # pure mapping
      PlaidCategoryMap.swift
      PlaidWebhookVerifier.swift
      PlaidConfig.swift
    SnapTrade/
      SnapTradeAdapter.swift
      SnapTradeClient.swift
      SnapTradeCanonicaliser.swift
      SnapTradeHMAC.swift
      SnapTradeWebhookVerifier.swift
      SnapTradeConfig.swift
    Manual/
      ManualEntryAdapter.swift
    Statement/
      StatementIngestAdapter.swift
      ClaudeStatementParser.swift
      AzureDocumentIntelligenceParser.swift   # optional, hybrid mode
      CSVParser.swift
      PIIMinimiser.swift
      ConfidenceScorer.swift
    PaymentsDormant/
      PlaidPaymentsAdapter.swift              # stub returning .unavailable
      YapilyPaymentsAdapter.swift             # stub returning .unavailable
      NoopPaymentsAdapter.swift
  Routes/
    V1/
      LinkRoutes.swift
      StatementRoutes.swift
      WebhookRoutes.swift
      AccountsRoutes.swift
      TransactionsRoutes.swift
      HoldingsRoutes.swift
  Migrations/
    20260425_001_canonical_core.sql
    20260425_002_institutions_seed.sql
    20260425_003_statements.sql
    20260425_004_indexes.sql
  configure.swift
```

## Implementation order

Phased. Commit per module; no PR should cross port/adapter boundaries.

### Sprint 1 — Foundations
1. Canonical domain types (`Domain/Canonical/*`).
2. Fluent models + migrations (schema matches `02-canonical-schema.md` exactly).
3. Port protocols (`Domain/Ports/*`) — empty signatures, no implementations.
4. `InstitutionService` with seed (200 UK institutions).
5. `NoopPaymentsAdapter` wired into `PaymentsService`; `payments.enabled=false`.

### Sprint 2 — Plaid adapter
6. `PlaidClient` (raw HTTP with retry + rate limiter).
7. `PlaidCanonicaliser` (pure functions; covered by unit tests).
8. `PlaidAdapter` conforming to `LinkPort`, `AccountsPort`, `TransactionsPort`, `BalancesPort`, `IdentityPort`, `WebhookReceiverPort`.
9. `PlaidWebhookVerifier` — JWT RS256 with `/webhook_verification_key/get` cache.
10. `LinkService` + `POST /api/v1/providers/plaid/link-token` + `/exchange` (gated on `providers.plaid.enabled`).
11. `SyncOrchestrator` handling `SYNC_UPDATES_AVAILABLE` webhook.
12. Integration tests against Plaid Sandbox (see `Tests/AppTests/Integrations/PlaidTests.swift`).

### Sprint 3 — SnapTrade adapter
13. `SnapTradeHMAC` — request signing, verified against SnapTrade's reference payloads.
14. `SnapTradeClient` + `Canonicaliser` + `Adapter`.
15. Connection callback route (Universal Link).
16. `SnapTradeWebhookVerifier` (HMAC-SHA256 with `clientSecret`).
17. `userSecret` atomic persistence + NOT NULL + encrypted-at-rest test.
18. Integration tests against SnapTrade sandbox.

### Sprint 4 — Statement ingest
19. `PIIMinimiser` (regex + Presidio or hand-rolled; decision tracked in 05.md open items).
20. `ClaudeStatementParser` (Files API + tool use, caching system prompt).
21. `ConfidenceScorer`.
22. `StatementIngestAdapter` + `StatementIngestService`.
23. `POST /api/v1/statements` + accept/reject routes.
24. CSV parser + auto-mapping heuristics for HSBC, Barclays, Monzo, AJ Bell, IBKR.

### Sprint 5 — Manual entry
25. `ManualEntryService` with full validation.
26. Recurring schedule generator.
27. Manual↔API merge logic in `CanonicalisationService`.

### Sprint 6 — Orchestration & polish
28. `CategorizationService` (rule-first; CoreML + Claude fallback TBD).
29. `SyncOrchestrator` cron + webhook fan-in.
30. Observability (`sync_run` telemetry → Grafana).
31. Per-user rate-limiting / token buckets.

### Sprint 7 — Migration
32. `YapilyCanonicaliser` + `YodleeCanonicaliser` for existing data.
33. Backfill scripts.
34. Parity dashboard.
35. Wave-cutover tooling.

## Interface contracts to honour

### `LinkPort.initiate`
Must return a `LinkSession` whose `redirectURL` or `linkToken` is short-lived. Implementations set expiry on the returned session.

### `TransactionsPort.syncTransactions`
Must be idempotent. Same `(link, cursor)` → same result. Callers persist cursor **within the same DB transaction** as the canonical writes. No cursor held in memory across process boundaries.

### `CanonicalisationService.commit`
Signature:
```swift
func commit(
  _ records: [CanonicalRecord],
  from source: SourceProvider,
  link: ProviderLink
) async throws -> CommitResult
```
- Dedup key: `(source_provider, source_record_id)` UNIQUE where `source_provider ≠ 'manual'`.
- Merge policy: declarative source priority table (see `05-manual-entry-and-upload.md` and `07-migration-and-rollout.md`).
- User-edited fields preserved; provider value stored in `user_edited_fields` history.
- Must run within a single DB transaction per record batch.

### `WebhookReceiverPort.verify`
- `PlaidAdapter`: JWT RS256 + `request_body_sha256` claim.
- `SnapTradeAdapter`: HMAC-SHA256 of raw body with `clientSecret`, constant-time compare.
- Reject → HTTP 401; log but do not retry.

## Security checklist

- [ ] Per-user provider credentials encrypted AES-256-GCM using `KMS_MASTER_KEY`.
- [ ] `snaptrade_user.userSecret` NOT NULL + encrypted.
- [ ] PII minimiser runs **before** any outbound Claude API call.
- [ ] Raw webhook bodies not logged in plaintext.
- [ ] No provider credentials in structured logs or error messages.
- [ ] SwiftLint rule forbidding `print()` in `Integrations/*`.
- [ ] Universal Links correctly served at `/.well-known/apple-app-site-association`.

## Environment & secrets

Secrets in Fly.io secret store (rotate KMS_MASTER_KEY migration before 5k MAU):

```
PLAID_CLIENT_ID
PLAID_SECRET_PRODUCTION
PLAID_ENV=production
PLAID_WEBHOOK_URL=https://api.eyrie.app/webhooks/plaid

SNAPTRADE_CLIENT_ID
SNAPTRADE_CONSUMER_KEY
SNAPTRADE_CLIENT_SECRET
SNAPTRADE_WEBHOOK_URL=https://api.eyrie.app/webhooks/snaptrade

ANTHROPIC_API_KEY
AZURE_DI_ENDPOINT         # optional, hybrid only
AZURE_DI_KEY

KMS_MASTER_KEY            # AES-256 key for credential encryption
DATABASE_URL
S3_STATEMENT_BUCKET
S3_ACCESS_KEY / S3_SECRET_KEY
```

## Feature flags (GrowthBook or equivalent)

```
providers.plaid.enabled          default: false
providers.plaid.investments      default: false
providers.snaptrade.enabled      default: false
providers.statement.enabled      default: true
providers.manual.enabled         default: true
providers.yapily.data_enabled    default: true (until migration phase 6)
providers.yodlee.enabled         default: true (until migration phase 6)
payments.enabled                 default: false
statement.parser.mode            "claude" | "hybrid"      default: "claude"
statement.parser.max_confidence  0..1                     default: 0.95
```

Per-user JSONB flags on `user.feature_flags`:
- `migration.cohort` — "pilot" | "wave_1" | ... | "grace"
- `migration.required_for` — ["banking", "investments"]
- `migration.completed_at`

## Testing strategy

**Unit**
- All canonicalisers are pure functions. Test with provider JSON fixtures under `Tests/AppTests/Fixtures/<Provider>/`.
- Money math (minor units, rounding, sign conventions).
- PIIMinimiser regex coverage.

**Integration**
- Plaid Sandbox: link → sync → webhook round-trip. Requires Plaid sandbox client_id.
- SnapTrade sandbox: register → connect → positions → activities.
- Statement parser: 20 representative PDFs in `Tests/AppTests/Fixtures/Statements/`; run Claude API in dry-run mode against golden outputs.

**Contract**
- Each adapter tested against the shared `AdapterConformanceSuite` that verifies port protocol behaviour (idempotency, error taxonomy, cursor semantics).

**E2E**
- Swift test runner → Vapor on localhost → Postgres → hitting sandbox APIs.
- iOS UI tests for Link flow + statement upload (XCUITest).

## Observability

Emit Prometheus metrics from `SyncOrchestrator`, `CanonicalisationService`, `StatementIngestService`:

```
eyrie_sync_runs_total{provider, status, trigger}
eyrie_sync_duration_seconds{provider}
eyrie_webhook_received_total{provider, code}
eyrie_webhook_verification_failures_total{provider}
eyrie_statement_parse_duration_seconds{parser}
eyrie_statement_parse_confidence{parser}
eyrie_canonical_merge_conflicts_total{entity}
eyrie_provider_api_errors_total{provider, code}
```

Alerting rules in `infra/alerts/`:
- Sync failure rate >5% (1h window) → page.
- Webhook verification failures >1/hour → page.
- Claude API error rate >10% (15m) → warn; >25% → page.
- `userSecret IS NULL` in any row → page immediately.

## iOS integration points

- `PlaidLinkClient` wraps `plaid-link-ios-spm`. Single entry point: `PlaidLinkClient.present(linkToken:)`.
- `SnapTradeConnectClient` opens `SFSafariViewController` with the redirectURL; listens for Universal Link return.
- `StatementUploader` uses `URLSession` multipart upload with background session ID for reliability.
- `ManualEntryFormKit` — SwiftUI forms funnelling through `ManualEntryService` over HTTP.

All iOS code reads **canonical** types from the API — no provider types leak.

## API surface (v1)

```
POST   /api/v1/providers/plaid/link-token
POST   /api/v1/providers/plaid/exchange
POST   /api/v1/providers/snaptrade/connect
POST   /api/v1/providers/snaptrade/callback
DELETE /api/v1/providers/{linkId}

GET    /api/v1/accounts
PATCH  /api/v1/accounts/{id}              # display_name, is_hidden, tax_wrapper
DELETE /api/v1/accounts/{id}              # manual only

GET    /api/v1/transactions?from=&to=&account_id=
PATCH  /api/v1/transactions/{id}          # user edits
POST   /api/v1/transactions               # manual entry

GET    /api/v1/holdings?as_of=
GET    /api/v1/investment-transactions

POST   /api/v1/statements                 # multipart upload
GET    /api/v1/statements/{id}
POST   /api/v1/statements/{id}/accept
POST   /api/v1/statements/{id}/reject

POST   /webhooks/plaid
POST   /webhooks/snaptrade
```

All responses JSON, error bodies `{error: {code, message, details?}}`.

## Open items requiring user/product decision

These appear repeatedly in modular briefs; surface as a single decision log before Sprint 2:

| # | Decision | Blocker for |
|---|---|---|
| D1 | Does Anthropic DPA allow raw PDF upload with PII? | MVP parser mode |
| D2 | Preserve user edits on provider-driven re-sync (preserve vs re-apply) | CanonicalisationService |
| D3 | Retention policy for superseded provider_links | Migration phase 6 |
| D4 | Statement-parse human review SLA | Ops |
| D5 | cVRP go-live threshold (which banks at what %) | PaymentsService activation |
| D6 | KMS migration timing (Fly secrets → AWS KMS) | Scale milestone |
| D7 | Category taxonomy versioning policy | CategorizationService |

## Done =

- All canonical schema migrations applied in production.
- PlaidAdapter + SnapTradeAdapter + StatementIngestAdapter + ManualEntryAdapter live behind feature flags.
- Dual-run pilot complete with >95% parity on canonical output.
- Wave cutover hits ≥90% of MAU.
- Yapily + Yodlee data adapters decommissioned; payments adapter stubs retained.
- All pre-GA checklist items in `08-risks-and-open-questions.md` ticked.

---
*Companion to `/technical/financial-data-layer-brief.md` and `/technical/financial-data-layer/*.md`.*
