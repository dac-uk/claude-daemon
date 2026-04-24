# 01 — Architecture

## Principles

1. **Canonical first.** The app, AI layer, and business logic read canonical types — never provider types.
2. **Provenance everywhere.** Every record carries `source_provider` + `source_record_id`.
3. **Ports & Adapters (Hexagonal).** Interfaces live in the domain layer; implementations live in `Integrations/<Provider>`.
4. **Idempotent canonicalisation.** Same input → same canonical state; re-running is safe.
5. **Manual is a first-class provider** (`source_provider = 'manual'`).
6. **Explicit fan-in merging** with declarative source priority.
7. **Read-only first, write later.** Payments is a separate port.
8. **Privacy by default.** Per-user AES-256-GCM for credentials. PII minimisation before LLM calls.
9. **No vendor lock-in.** Target provider swap cost: 3 days of backend work.

## Layered architecture

```
┌──────────────────────────────────────────────────────────────────┐
│ iOS App (SwiftUI)                                                │
│  reads canonical Account/Transaction/Holding via Vapor API       │
└───────────────▲──────────────────────────────────────────────────┘
                │
┌───────────────┴──────────────────────────────────────────────────┐
│ HTTP API (Vapor routes) — canonical surface only                 │
└───────────────▲──────────────────────────────────────────────────┘
                │
┌───────────────┴──────────────────────────────────────────────────┐
│ Services (AccountsService, TransactionsService, HoldingsService, │
│   CanonicalisationService, SyncOrchestrator, StatementIngest,    │
│   ManualEntryService, CategorizationService, InstitutionService, │
│   LinkService, PaymentsService, WebhookRouter)                   │
└───────────────▲──────────────────────────────────────────────────┘
                │
┌───────────────┴──────────────────────────────────────────────────┐
│ Ports (protocols): LinkPort, AccountsPort, TransactionsPort,     │
│   HoldingsPort, InvestmentTransactionsPort, BalancesPort,        │
│   IdentityPort, PaymentsPort, StatementIngestPort,               │
│   WebhookReceiverPort                                            │
└───────────────▲──────────────────────────────────────────────────┘
                │
┌───────────────┴──────────────────────────────────────────────────┐
│ Adapters (concrete): PlaidAdapter, SnapTradeAdapter,             │
│   ManualEntryAdapter, StatementIngestAdapter,                    │
│   PlaidPaymentsAdapter (dormant), YapilyPaymentsAdapter (dormant)│
└───────────────▲──────────────────────────────────────────────────┘
                │
┌───────────────┴──────────────────────────────────────────────────┐
│ Canonical persistence (Postgres): Institution, ProviderLink,     │
│   Account, Balance, Transaction, Security, Holding,              │
│   InvestmentTransaction, Category, StatementArtifact, SyncRun    │
└──────────────────────────────────────────────────────────────────┘
```

## Ports (interfaces)

### `LinkPort`
```swift
protocol LinkPort {
    func initiate(userId: UUID, intent: LinkIntent) async throws -> LinkSession
    func complete(userId: UUID, callback: LinkCallback) async throws -> ProviderLink
    func renew(linkId: UUID) async throws -> LinkSession
    func revoke(linkId: UUID) async throws
}
```

### Data ports
```swift
protocol AccountsPort {
    func listAccounts(link: ProviderLink) async throws -> [CanonicalAccount]
}
protocol TransactionsPort {
    func syncTransactions(link: ProviderLink, since: SyncCursor?) async throws -> TransactionSyncResult
}
protocol HoldingsPort {
    func listHoldings(link: ProviderLink) async throws -> [CanonicalHolding]
}
protocol InvestmentTransactionsPort {
    func syncInvestmentTransactions(link: ProviderLink, since: SyncCursor?) async throws -> InvestmentTransactionSyncResult
}
protocol BalancesPort {
    func fetchBalances(link: ProviderLink) async throws -> [CanonicalBalance]
}
protocol IdentityPort {
    func fetchIdentity(link: ProviderLink) async throws -> CanonicalIdentity
}
```

### `PaymentsPort`
```swift
protocol PaymentsPort {
    func createMandate(userId: UUID, parameters: MandateParameters) async throws -> MandateHandle
    func executePayment(mandate: MandateHandle, amount: Money, reference: String) async throws -> PaymentIntent
    func cancelMandate(mandate: MandateHandle) async throws
    func getPayment(paymentId: String) async throws -> PaymentIntent
}
```

### `StatementIngestPort`
```swift
protocol StatementIngestPort {
    func uploadStatement(userId: UUID, file: Data, mime: String, hints: StatementHints?) async throws -> StatementArtifact
    func parse(artifact: StatementArtifact) async throws -> ParsedStatementPayload
    func accept(artifact: StatementArtifact, overrides: UserOverrides?) async throws -> [CanonicalRecord]
}
```

### `WebhookReceiverPort`
```swift
protocol WebhookReceiverPort {
    func verify(headers: HTTPHeaders, body: Data) throws -> VerifiedWebhook
    func handle(_ webhook: VerifiedWebhook) async throws -> [SyncSideEffect]
}
```

## Adapters

| Adapter | Implements |
|---|---|
| `PlaidAdapter` | Link, Accounts, Transactions, Holdings, InvestmentTransactions, Balances, Identity, WebhookReceiver |
| `SnapTradeAdapter` | Link, Accounts, Holdings, InvestmentTransactions, Balances, WebhookReceiver |
| `ManualEntryAdapter` | Accounts, Transactions, Holdings, InvestmentTransactions, Balances |
| `StatementIngestAdapter` | StatementIngest, Transactions, Holdings, InvestmentTransactions, Balances |
| `PlaidPaymentsAdapter` | Payments (dormant at launch) |
| `YapilyPaymentsAdapter` | Payments (dormant at launch) |

## Services

- **LinkService** — coordinates LinkPort across providers; owns re-auth queue.
- **AccountsService** — canonical account CRUD, hidden/display state.
- **TransactionsService** — read + edit; preserves provider data under user edits.
- **HoldingsService** — current + historical; FX-aware rollups.
- **InvestmentTransactionsService** — event stream.
- **BalancesService** — snapshot + history.
- **InstitutionService** — registry, resolver for manual entry.
- **CategorizationService** — rule → CoreML → Claude fallback.
- **CanonicalisationService** — commits adapter output with merge/dedupe/conflict resolution.
- **StatementIngestService** — orchestrates upload → parse → review → accept.
- **ManualEntryService** — validation + canonicalisation.
- **SyncOrchestrator** — webhook/schedule/user-triggered sync with rate-limit discipline.
- **PaymentsService** — thin wrapper over PaymentsPort; keeps `VRPSafetyModel`.
- **WebhookRouter** — dispatches verified webhooks to correct adapter.

## Adding a new provider (checklist)

1. Implement relevant ports in `Sources/App/Integrations/<NewProvider>/`.
2. Add enum case to `ProviderLink.provider` (migration).
3. Register adapter with `LinkService` and `SyncOrchestrator` in `configure.swift`.
4. Write `<NewProvider>Canonicaliser.swift` (pure mapping).
5. Add signature verification to `WebhookRouter`.
6. Seed institution mappings via `InstitutionService`.
7. Add sandbox integration tests in `Tests/AppTests/Integrations/<NewProvider>Tests.swift`.

No schema migration. No app-level change. No AI-layer change.

---
*Source of truth: `/technical/financial-data-layer-brief.md` §3, §7.*
