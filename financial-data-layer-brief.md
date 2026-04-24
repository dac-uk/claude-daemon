# Eyrie — Financial Data Layer Product & Architecture Brief

**Version:** 1.0  
**Date:** 22 April 2026  
**Owner:** David Charnley  
**Status:** Ready for Claude Code to refine into detailed design, architecture, and implementation plans  
**Scope:** Complete replacement of Yapily (banking data) and Yodlee (investment/pension data) with a provider-agnostic financial data layer powered by Plaid + SnapTrade + a Statement/Manual-Entry channel, leaving Yapily abstractable as a future Payments/VRP adapter.

---

## How to Use This Document

This is the single-source, comprehensive brief. Three companion artefacts are derived from it:

- **`/technical/financial-data-layer/`** — the same content split into nine modular files (00-overview … 08-risks) for easier navigation and per-domain handoff.
- **`/technical/financial-data-layer/diagrams/`** — Mermaid diagram pack (C4 container, sequence diagrams for each flow, ERD for canonical schema).
- **`/technical/CLAUDE_CODE_SPEC.md`** — terse, code-ready companion spec for Claude Code, with file paths, module boundaries, concrete interfaces, and an implementation-order plan.

Read this document end-to-end once. Then use the modular files as working references.

---

## Table of Contents

1. Executive Summary
2. Why We're Changing Providers
3. Product Vision & Architectural Principles
4. Provider Comparison Matrix
5. Provider Capability Deep-Dive (Plaid, SnapTrade, Statement/Manual)
6. Canonical Financial Data Schema
7. Adapter Pattern & Provider-Agnostic Services
8. Statement Upload & Manual Entry — Parsing Stack Evaluation
9. Canonicalisation, Deduplication & Conflict Resolution
10. Payments Port — Abstracted for Yapily or Alternatives
11. iOS Integration Contract
12. Security, Privacy & Compliance
13. Migration & Rollout Plan
14. Testing Strategy
15. Observability, SLOs & Operational Runbook
16. Risks, Open Questions & Verification Checklist
17. Appendix A — Endpoint Inventory (New vs Replaced)
18. Appendix B — Glossary
19. Appendix C — References

---

## 1. Executive Summary

Eyrie is a UK-first personal finance iOS app (Vapor 4 backend, SwiftUI iOS, PostgreSQL 16 on Fly.io London). Its current data stack couples directly to two providers:

- **Yapily** — Open Banking AIS for current accounts, savings, credit cards. FCA FRN 827001.
- **Yodlee** — Investment, pension and ISA aggregation for UK wealth providers.

Both need to be replaced because (a) Yapily's banking data coverage is poor for our UK roadmap versus Plaid's maturing UK footprint, and (b) Yodlee declined to serve Eyrie at our current scale.

The **target state** is a provider-agnostic financial data layer consisting of:

- **Plaid** as primary banking-data provider (Transactions, Auth, Identity, Balance, Investments where coverage exists). FCA AISP FRN 804718.
- **SnapTrade** as primary investment/wealth/pension provider for brokers we can't reach via Plaid (AJ Bell, Interactive Brokers UK, Trading 212, plus all US brokers).
- **Statement/Manual channel** for the long tail of UK wealth providers not covered by either API (Hargreaves Lansdown subject to verification, Aviva, L&G, Standard Life, Nest, some workplace pensions).
- **Canonical data model** (`Institution`, `Account`, `Balance`, `Transaction`, `Holding`, `InvestmentTransaction`, `Security`, `Category`, `StatementArtifact`, `ProviderLink`, `SyncRun`) that all provider adapters map into.
- **Payments port**, initially empty, with Yapily available as a future VRP/PIS adapter if and when commercial VRP proves out.

The AI layer (categorisation, merchant enrichment, the Financial Decision Engine, Tribe, Ask Eyrie) reads only the canonical layer — never a provider-specific type. This is the key architectural decision that makes provider swaps cost days, not weeks.

**Backend effort estimate:** 6–8 weeks for end-to-end rollout (adapter implementation, canonicalisation service, statement/manual pipeline, testing, staged cutover). **iOS effort:** 2–3 weeks (Plaid LinkKit integration, SnapTrade Connection Portal via SFSafariViewController, statement upload UI, manual entry screens).

---

## 2. Why We're Changing Providers

### 2.1 Yapily

**Problem:**
- Coverage in UK consumer banking is narrower than Plaid's — particularly outside CMA9 and neobanks.
- The 90-day PSD2 bank-led re-consent was a live operational issue flagged in `/technical/backend-assessment.md` as our #1 scaling risk. (Plaid implements the FCA 2023 TPP-managed consent model, 180-day effective period, which materially reduces silent disconnections.)
- Webhook reliability anecdotally weaker than Plaid's published posture.
- Pricing at scale is less flexible than the Plaid startup track.

**What we keep from Yapily:** The VRP integration is production-grade (`VRPSafetyModel`, monthly caps, mandate CRUD) and we don't need to throw it away. Keep it behind a `PaymentsPort`. If commercial VRP proves out, either Yapily or Plaid can power payments — the app doesn't care.

### 2.2 Yodlee

**Problem:**
- Yodlee declined to onboard Eyrie (explicit rejection, likely minimum revenue/scale thresholds).
- No path to signing at our current stage. Alternative needed immediately.

**What we replace it with:** A two-provider strategy — SnapTrade for brokers with API coverage, and Statement/Manual for the rest. This is cheaper per user than Yodlee, covers a broader surface for UK investments, and gets us unblocked.

### 2.3 Why Plaid + SnapTrade is the right combination

- **Plaid** is best-in-class for bank transactions (cursor-based `/transactions/sync`, rich enrichment, Link SDK UX). FCA AISP. Growing UK investments coverage but uneven for legacy pension providers.
- **SnapTrade** is purpose-built for investment aggregation. Covers trader-focused brokers that banking-data APIs miss. Transparent pay-as-you-go pricing (~$2 per connected user per month).
- **Statement/Manual** covers the long tail that APIs will not solve near-term (workplace pensions, some legacy ISAs, illiquid assets).

The three channels are complementary, not duplicative. A single account lives in exactly one channel by policy — we prefer API over statement over manual, and we surface provenance to the user.

---

## 3. Product Vision & Architectural Principles

### 3.1 Vision

Eyrie is a complete picture of a UK household's financial life. That means:

- Current accounts, savings, credit cards — via Open Banking.
- ISAs, SIPPs, pensions, brokerage — via direct broker API where available, via statement upload or manual entry where not.
- Future-ready for US Apple App Store release (both Plaid and SnapTrade cover US).
- The long tail of "things users want to track" — workplace pensions, crypto, private assets, inheritance expectations — handled as first-class via manual entry with the same canonical schema.

### 3.2 Architectural Principles

1. **Canonical first, adapters second.** The app and the AI layer read the canonical schema. Adapters translate — they never leak provider-specific types upward.
2. **Provenance everywhere.** Every `Account`, `Transaction`, `Holding` has a `source_provider` and `source_record_id`. Conflicts are resolvable because we always know where a value came from.
3. **Ports/Adapters (Hexagonal).** `AccountsPort`, `TransactionsPort`, `HoldingsPort`, `InvestmentTransactionsPort`, `PaymentsPort`, `LinkPort`, `StatementIngestPort`. Each adapter implements one or more. New provider = new adapter, no schema change.
4. **Manual is a first-class channel.** Manual-entered data is treated like any other source — signed, versioned, editable, mergeable. "Manual" is a provider like "plaid" or "snaptrade".
5. **Idempotent canonicalisation.** Every sync produces a deterministic set of canonical records given the same provider output. Re-running the pipeline on the same input is safe.
6. **Fan-in merging is explicit and auditable.** When two providers report overlapping data for the same account (rare but possible), merge rules are declarative and logged.
7. **Read-only first, write later.** Everything in the data layer is read-only from the user's perspective except manual entry. Payments/VRP is a separate port with separate risk controls.
8. **Privacy by default.** Per-user AES-256-GCM encryption for all provider credentials. We pass through as little PII as possible. Manual entry data never leaves Eyrie.
9. **No vendor lock-in architecturally.** The test of the design: "If Plaid doubled pricing tomorrow, how long to swap to TrueLayer?" Target: 3 days of backend work.
10. **Don't paint ourselves into the Plaid UK investments corner.** Plaid supports UK investment account subtypes but institution coverage is uneven. SnapTrade is the primary investments path; Plaid Investments is opportunistic.

---

## 4. Provider Comparison Matrix

| Capability | Plaid | SnapTrade | Statement/Manual | Yapily (future payments) |
|---|---|---|---|---|
| UK Current accounts + transactions | ✅ Primary | — | Fallback | — |
| UK Savings + transactions | ✅ Primary | — | Fallback | — |
| UK Credit cards (balance/transactions) | ✅ (transactions); ❌ Liabilities | — | ✅ | — |
| UK ISA (Stocks & Shares) | ⚠️ Institution-dependent | ✅ AJ Bell, others verify | ✅ | — |
| UK Cash ISA | ⚠️ Institution-dependent | Limited | ✅ | — |
| UK SIPP / Pension | ⚠️ Institution-dependent | ✅ via broker | ✅ (workplace) | — |
| UK Workplace pension (Nest, L&G, Aviva) | ❌ | ❌ | ✅ Primary | — |
| US Bank + Credit | ✅ Primary | — | Fallback | — |
| US Brokerage (Schwab, Fidelity, Robinhood, IBKR, Vanguard, E*TRADE) | Partial via Investments | ✅ Primary | Fallback | — |
| Crypto (Coinbase, Kraken, Binance) | ❌ | ✅ | Manual | — |
| Payment Initiation (UK) | ✅ | — | — | ✅ |
| Sweeping VRP | ✅ | — | — | ✅ |
| Commercial VRP | Pilot Q1 2026 | — | — | Pilot (verify) |
| iOS SDK | ✅ SPM `plaid-link-ios-spm` | ❌ use SFSafariViewController | Native Eyrie UI | Web/OAuth SDK |
| Webhook model | HMAC-SHA256 JWT | HMAC-SHA256 body signature | N/A | HMAC-SHA256 |
| Startup-friendly pricing | Trial programme, negotiable | Pay-as-you-go $1.50–$2/user/mo | Internal cost only | Moderate |
| FCA posture | AISP FRN 804718, PISP | Canadian API (not FCA regulated itself) | N/A | AISP FRN 827001 |
| Data residency | Verify (US-hosted by default) | Verify (Canada-hosted) | Eyrie-controlled | UK |

---

## 5. Provider Capability Deep-Dive

### 5.1 Plaid

**What we use it for:**
- `/link/token/create` → `/item/public_token/exchange` → durable `access_token`
- `/transactions/sync` (cursor-based, the only transactions endpoint in 2026)
- `/auth/get` for sort code + account number (VRP setup, payer verification)
- `/identity/get` to close the existing account-name-verification TODO in the backend
- `/accounts/get` + `/accounts/balance/get`
- `/investments/holdings/get` + `/investments/transactions/get` — opportunistic UK coverage
- Webhooks: `SYNC_UPDATES_AVAILABLE`, `ITEM_ERROR`, `ITEM_LOGIN_REQUIRED`, `PENDING_EXPIRATION`, `HOLDINGS_DEFAULT_UPDATE`, `INVESTMENTS_TRANSACTIONS_DEFAULT_UPDATE`, `PAYMENT_STATUS_UPDATE` (if Plaid is selected for Payments)

**Key operational facts:**
- Cursor is ≤256 base64 chars, persisted per-Item, **losing it forces full re-sync** (cost + rate-limit impact).
- `/transactions/sync` rate-limited at 50 requests per Item per minute (note: the older `/transactions/get` endpoint is 30/min — we use `/transactions/sync` exclusively). Use webhook-driven sync to stay well below.
- FCA 2023 TPP-managed consent (180 days effective), smoother re-auth than the pre-2023 Yapily model.
- SOC 2 Type II, ISO 27001, FCA AISP + PISP.
- iOS: Swift Package Manager only (`plaid-link-ios-spm`), CocoaPods dropped from 7.x. WKWebView not supported.
- Universal Links required for OAuth redirects.

**Known gaps for us:**
- UK `/liabilities/get` not supported (US/CA only) — we have no API path to UK credit card APRs, student loans or mortgages. Fallback: statement upload.
- UK Investments coverage is institution-dependent. The product supports `isa`, `cash_isa`, `pension`, `sipp` subtypes, but whether HL, Vanguard UK, Nutmeg, Moneybox, or workplace pensions are live changes quarterly. **Do not commit to Plaid Investments for a specific broker without explicit coverage confirmation.**

Full research: `/outputs/research/plaid-research.md`.

### 5.2 SnapTrade

**What we use it for:**
- `registerSnapTradeUser` on first investment connection → returns `userSecret` (persist encrypted immediately; loss = delete + re-onboard).
- `loginSnapTradeUser` → short-lived Connection Portal URL.
- Connection Portal flow in **SFSafariViewController** (not WKWebView — Passkeys/OAuth break).
- `Connections_listBrokerageAuthorizations` to enumerate user's brokers.
- `Accounts_listAllUserAccounts`, `AccountInformation_getUserAccountBalance`, `AccountInformation_getUserAccountHoldings`, `TransactionsAndReporting_getActivities` for data.
- `Connections_refreshBrokerageAuthorization` for user-triggered refresh (extra per-call charge).
- Webhooks: `ACCOUNT_HOLDINGS_UPDATED`, `ACCOUNT_TRANSACTIONS_UPDATED`, `USER_CONNECTION_RENEWED`, `ACCOUNT_DELETED`, `CONNECTION_DELETED`.

**Key operational facts:**
- No Swift SDK — hand-roll REST client with HMAC-SHA256 request signing (simple; Node/Python SDKs are reference).
- Global rate limit 250 req/min per API key (request higher via account manager).
- Holdings/activities cached daily by default; real-time is an add-on.
- Pricing $1.50–$2 per connected user per month, pay-as-you-go.
- IBKR multi-currency bug — only family currency holdings returned. Warn users; monitor.
- Symbols (tickers) are not stable over time. Always store security reference by `(symbol, isin_if_available, fetched_at)`.

**UK coverage (April 2026):**
- **Live:** AJ Bell, Interactive Brokers UK, Trading 212.
- **Verify:** Vanguard UK, Freetrade, Hargreaves Lansdown (contact SnapTrade sales before GTM commitments).
- **US coverage is mature:** Robinhood, Schwab, Fidelity, Vanguard, IBKR, E*TRADE, Coinbase, Kraken.

**Gaps:**
- Workplace pensions (Nest, Aviva, L&G, Standard Life) are not covered and not on SnapTrade's roadmap — this is the Statement/Manual pipeline's job.
- GDPR data residency requires DPA before UK launch.

Full research: `/outputs/research/snaptrade-research.md`.

### 5.3 Statement / Manual Channel

This is a first-class provider in the architecture — not a "legacy fallback". It handles everything the APIs can't reach.

**What users can upload:**
- PDF statements from any provider (bank, investment, pension, mortgage, credit card).
- CSV exports.
- Screenshots (fallback for bank apps with no export — lower priority).

**What users can manually enter:**
- Accounts (name, institution, type, tax wrapper).
- Balances as of a date.
- Holdings (security, quantity, price, cost basis).
- Transactions (individual or batch via CSV).
- Periodic recurring items (e.g., "my pension contributes £500/month").

**Why this matters:** this is the single architectural feature that lets Eyrie say "we cover every UK pension provider, every workplace scheme, every mortgage, every private asset" without lying — because if we don't have an API, the user can still get full visibility in ~5 minutes with a PDF upload.

Parsing stack details in §8.

---

## 6. Canonical Financial Data Schema

Every provider adapter produces records in this schema. The AI layer and the app read from here.

### 6.1 Entities

#### `Institution`
```
id               UUID  (stable, Eyrie-owned)
display_name     TEXT
country_code     TEXT    ISO-3166-1 alpha-2 ("GB", "US")
logo_url         TEXT?
website_url      TEXT?
primary_colour   TEXT?   HEX ("#005EB8")
plaid_institution_id  TEXT?
snaptrade_slug        TEXT?
manual_only      BOOL    true if no API provider covers it
created_at       TIMESTAMPTZ
```

Institutions are a shared registry. When a user manually adds "Aviva Workplace Pension" we resolve it to an existing `Institution` or create one. We pre-seed ~200 UK institutions at launch to avoid cold-start UX.

#### `ProviderLink`
```
id                       UUID
user_id                  UUID
provider                 ENUM('plaid','snaptrade','manual','statement','yapily')
provider_user_id         TEXT      e.g. Plaid item_id, SnapTrade userId
provider_credentials     BYTEA     AES-256-GCM encrypted; provider-specific payload
status                   ENUM('active','expiring','re-auth-required','revoked','error')
consent_expires_at       TIMESTAMPTZ?
last_sync_at             TIMESTAMPTZ?
cursor                   TEXT?     Plaid transactions cursor
metadata                 JSONB     provider-specific extras
created_at               TIMESTAMPTZ
updated_at               TIMESTAMPTZ
```

Each `ProviderLink` represents one connection (e.g. one Plaid Item = one bank, one SnapTrade brokerage authorisation = one broker, one uploaded statement series = one "manual link").

#### `Account`
```
id                   UUID
user_id              UUID
institution_id       UUID  FK Institution
provider_link_id     UUID? FK ProviderLink   NULL if purely manual
source_provider      ENUM
source_account_id    TEXT       provider's ID for this account
display_name         TEXT       user-editable; defaults to provider-reported
mask                 TEXT?      last 4 digits
type                 ENUM('depository','credit','loan','investment','pension','other')
subtype              ENUM('checking','savings','credit_card','isa','cash_isa','sipp','pension','brokerage','crypto','mortgage','student_loan','auto_loan','line_of_credit','other')
tax_wrapper          ENUM?      'isa','sipp','lifetime_isa','junior_isa','gia','us_401k','us_ira','us_roth_ira', etc.
iso_currency_code    TEXT       ISO-4217 ("GBP", "USD")
is_manual            BOOL
is_hidden            BOOL       user-driven
closed_at            TIMESTAMPTZ?
created_at           TIMESTAMPTZ
updated_at           TIMESTAMPTZ
```

#### `Balance`
```
id                   UUID
account_id           UUID FK
current_minor_units  BIGINT     pence/cents (no floats)
available_minor_units BIGINT?
limit_minor_units    BIGINT?
iso_currency_code    TEXT
as_of                TIMESTAMPTZ
source_provider      ENUM
source_record_id     TEXT?
created_at           TIMESTAMPTZ
```

Balances are immutable snapshots. We never update; we insert a new one. History is navigable.

#### `Transaction`
```
id                        UUID
account_id                UUID FK
source_provider           ENUM
source_txn_id             TEXT
posted_date               DATE
authorized_date           DATE?
amount_minor_units        BIGINT     SIGNED: negative = outflow from account
iso_currency_code         TEXT
description_raw           TEXT       exactly as provider reported
description               TEXT       normalised
merchant_name             TEXT?
merchant_logo_url         TEXT?
category_primary          TEXT       canonical primary category slug
category_detailed         TEXT?      canonical detailed slug
counterparty              TEXT?
counterparty_type         ENUM('merchant','financial_institution','marketplace','payment_processor','person','other')?
location_city             TEXT?
location_region           TEXT?
location_country          TEXT?
is_pending                BOOL
is_user_edited            BOOL       true if any user edit has been applied
user_edited_fields        JSONB      {field: {old, new, edited_at}}  — preserves provider data underneath
is_hidden                 BOOL
is_reviewed               BOOL
tags                      TEXT[]
notes                     TEXT?
created_at                TIMESTAMPTZ
updated_at                TIMESTAMPTZ
```

Sign convention: negative = money leaves the account. Positive = money enters. This is explicitly different from Plaid's convention (Plaid is positive for outflows) — adapter normalises.

#### `Security` (reference)
```
id                 UUID
symbol             TEXT
isin               TEXT?
cusip              TEXT?
sedol              TEXT?
name               TEXT
type               ENUM('equity','etf','mutual_fund','fixed_income','derivative','cryptocurrency','cash','other')
exchange_mic       TEXT?
listing_currency   TEXT       ISO-4217
figi               TEXT?
last_refreshed_at  TIMESTAMPTZ
```

Securities are an upsert target. Prefer ISIN for UK (primary identifier), fall back to CUSIP+exchange for US, FIGI as strongest global key where available.

#### `Holding`
```
id                           UUID
account_id                   UUID FK
security_id                  UUID FK
quantity                     NUMERIC(24,10)
institution_price_minor      BIGINT     per-unit price at as_of time
institution_value_minor      BIGINT     quantity * price (stored denormalised for auditability)
cost_basis_per_unit_minor    BIGINT?
position_currency            TEXT       currency of the position at broker (may differ from listing currency)
as_of                        TIMESTAMPTZ
source_provider              ENUM
source_record_id             TEXT?
created_at                   TIMESTAMPTZ
```

Holdings are also immutable snapshots. Current holdings = latest per `(account_id, security_id)`.

#### `InvestmentTransaction`
```
id                   UUID
account_id           UUID FK
security_id          UUID? FK      null for cash events (contribution, fee, etc.)
type                 ENUM('BUY','SELL','DIV','INTEREST','FEE','TAX','CONTRIBUTION','WITHDRAWAL','TRANSFER_IN','TRANSFER_OUT','REINVEST','OPTION_EXPIRATION','OPTION_ASSIGNMENT','OPTION_EXERCISE','SPLIT','OTHER')
trade_date           DATE
settled_date         DATE?
quantity             NUMERIC(24,10)?
price_minor          BIGINT?       per-unit
amount_minor         BIGINT        net cash effect, signed (negative = outflow)
fees_minor           BIGINT?
iso_currency_code    TEXT
description          TEXT?
source_provider      ENUM
source_txn_id        TEXT
created_at           TIMESTAMPTZ
```

#### `Category` (canonical taxonomy, seed data)
```
slug             TEXT PK     "groceries", "salary", "dining_out"
display_name     TEXT
parent_slug      TEXT?
icon             TEXT
colour           TEXT
system           BOOL         true if Eyrie-managed; false if user-created
user_id          UUID?        for user-created categories
```

Category slugs are stable. Plaid's `personal_finance_category` (primary+detailed) maps to our slugs via a static table.

#### `StatementArtifact`
```
id                     UUID
user_id                UUID
provider_link_id       UUID? FK ProviderLink (the "manual" link for that account)
file_uri               TEXT             S3/Fly.io volume
mime_type              TEXT
sha256                 TEXT             dedupe key
page_count             INT?
uploaded_at            TIMESTAMPTZ
parse_status           ENUM('queued','parsing','review','accepted','rejected','failed')
parser                 ENUM('azure_di','claude_api','csv_native','manual_override')
parser_confidence      NUMERIC(4,3)?    0.000–1.000
parsed_payload         JSONB?           raw normalised output pre-canonicalisation
parsed_at              TIMESTAMPTZ?
rejected_reason        TEXT?
reviewer_user_id       UUID?
reviewed_at            TIMESTAMPTZ?
```

#### `SyncRun`
```
id                   UUID
provider_link_id     UUID FK
started_at           TIMESTAMPTZ
completed_at         TIMESTAMPTZ?
triggered_by         ENUM('user','webhook','schedule','backfill','manual_retry')
status               ENUM('running','success','partial','failed')
counts_added         INT
counts_modified      INT
counts_removed       INT
error_code           TEXT?
error_message        TEXT?
duration_ms          INT?
metadata             JSONB
```

Every provider interaction that changes canonical data produces a `SyncRun`. Full audit trail, free.

### 6.2 Indexes & constraints (non-exhaustive, for Claude Code to refine)

- `Transaction(account_id, posted_date DESC)` — hot path for timeline views.
- `Transaction(user_id, posted_date DESC)` — cross-account views.
- UNIQUE `(source_provider, source_txn_id)` where `source_provider != 'manual'` — dedupe guarantee.
- `Account(user_id, is_hidden, closed_at)` — default dashboard query.
- `Holding(account_id, as_of DESC)` — latest positions.
- UNIQUE `(account_id, security_id, as_of)` on `Holding` — prevents duplicate snapshots.
- `ProviderLink(user_id, provider, status)` — re-auth queue.
- `StatementArtifact(sha256)` — dedupe uploads.

### 6.3 Sign conventions & currencies

- All money in minor units (pence, cents). No floats, ever.
- All transactions signed: negative = outflow from the account.
- `iso_currency_code` is required on every monetary field that can legitimately differ per-record (multi-currency brokerage accounts).
- Account-level display currency is `Account.iso_currency_code`. App-level display currency is a user setting.
- FX conversion is a presentation concern — we never persist "converted" values. Rates come from a daily FX feed (ECB or fixer.io) stored in `fx_rate` table.

### 6.4 Multi-currency accounts

Brokerage accounts (especially IBKR) may hold positions in multiple currencies. Rule: `Account.iso_currency_code` is the "home currency of the account wrapper"; `Holding.position_currency` is the currency of the individual holding. UI reconciles.

---

## 7. Adapter Pattern & Provider-Agnostic Services

### 7.1 Ports (protocol definitions)

Swift protocols, implemented as Vapor service registrations. These are pure interfaces — no provider terms leak in.

```swift
// Core identity & connection
protocol LinkPort {
    func initiate(userId: UUID, intent: LinkIntent) async throws -> LinkSession
    func complete(userId: UUID, callback: LinkCallback) async throws -> ProviderLink
    func renew(linkId: UUID) async throws -> LinkSession
    func revoke(linkId: UUID) async throws
}

// Data ports
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

// Payments — separate, has its own lifecycle
protocol PaymentsPort {
    func createMandate(userId: UUID, parameters: MandateParameters) async throws -> MandateHandle
    func executePayment(mandate: MandateHandle, amount: Money, reference: String) async throws -> PaymentIntent
    func cancelMandate(mandate: MandateHandle) async throws
    func getPayment(paymentId: String) async throws -> PaymentIntent
}

// Statement & manual ingest
protocol StatementIngestPort {
    func uploadStatement(userId: UUID, file: Data, mime: String, hints: StatementHints?) async throws -> StatementArtifact
    func parse(artifact: StatementArtifact) async throws -> ParsedStatementPayload
    func accept(artifact: StatementArtifact, overrides: UserOverrides?) async throws -> [CanonicalRecord]
}

// Webhook handling
protocol WebhookReceiverPort {
    func verify(headers: HTTPHeaders, body: Data) throws -> VerifiedWebhook
    func handle(_ webhook: VerifiedWebhook) async throws -> [SyncSideEffect]
}
```

### 7.2 Adapters

| Adapter | Implements | Notes |
|---|---|---|
| `PlaidAdapter` | `LinkPort`, `AccountsPort`, `TransactionsPort`, `HoldingsPort`, `InvestmentTransactionsPort`, `BalancesPort`, `IdentityPort`, `WebhookReceiverPort` | Most surface. Owns Plaid Item lifecycle. |
| `SnapTradeAdapter` | `LinkPort`, `AccountsPort`, `HoldingsPort`, `InvestmentTransactionsPort`, `BalancesPort`, `WebhookReceiverPort` | No `TransactionsPort` — cash transactions on brokerage accounts are investment events. |
| `ManualEntryAdapter` | `AccountsPort`, `TransactionsPort`, `HoldingsPort`, `InvestmentTransactionsPort`, `BalancesPort` | Users write directly; adapter validates + canonicalises. |
| `StatementIngestAdapter` | `StatementIngestPort`, `TransactionsPort`, `HoldingsPort`, `InvestmentTransactionsPort`, `BalancesPort` | Parses uploads, surfaces as canonical. |
| `PlaidPaymentsAdapter` | `PaymentsPort` | Initially unconfigured. Plug in when cVRP proves out. |
| `YapilyPaymentsAdapter` | `PaymentsPort` | Kept warm in code, not wired at launch. |

### 7.3 Services (business logic that orchestrates adapters)

```
LinkService              Coordinates LinkPort across providers; manages re-auth queue.
AccountsService          Read+write canonical accounts, hidden/display state.
TransactionsService      Read+edit transactions, preserve provider data under user edits.
HoldingsService          Current + historical holdings; FX-aware rollup.
InvestmentTransactionsService
BalancesService          Snapshot + history.
InstitutionService       Registry; resolver for manual entry ("I bank with Barclays" → Institution).
CategorizationService    Rule-based → CoreML on-device → Claude fallback for unknowns.
CanonicalisationService  Takes adapter output + runs merge/dedupe/conflict resolution.
StatementIngestService   Orchestrates upload → parse → review → accept flow.
ManualEntryService       Validation + canonicalisation for user-submitted records.
SyncOrchestrator         Triggers syncs (webhook, schedule, user-initiated); rate-limit-aware.
PaymentsService          Thin wrapper over PaymentsPort; records to internal ledger.
WebhookRouter            Verifies + routes webhooks to correct adapter by source.
```

### 7.4 How a new provider is added

1. Implement relevant ports (new adapter class in `Sources/App/Integrations/<Provider>/`).
2. Add provider enum case to `ProviderLink.provider` (DB migration).
3. Register with `LinkService` and `SyncOrchestrator`.
4. Map provider's native types → canonical in `<Provider>Canonicaliser.swift`.
5. Add webhook signature verification to `WebhookRouter`.
6. Seed institution mappings (if provider has its own institution IDs).
7. Add integration tests using provider's sandbox.

That's the entire checklist. No schema changes, no service changes, no UI changes. The iOS app sees a new `LinkIntent.connectInvestmentAccount(via: .newProvider)` and otherwise keeps working.

---

## 8. Statement Upload & Manual Entry — Parsing Stack Evaluation

This is the biggest open design question. The choice shapes cost, reliability, and the statement UX.

### 8.1 Scope

- **Input formats:** PDF (primary), CSV, screenshots (deferred).
- **Output:** Canonical `Transaction`, `Holding`, `InvestmentTransaction`, `Balance` records with provenance.
- **Requirements:**
  - UK bank + investment + pension statements across diverse formats.
  - US bank + investment statements.
  - Accurate on tables (holdings, transactions) — forgiving on layout.
  - Fast enough for synchronous UX where possible (<10s for a monthly statement).
  - Reasonable cost at scale (target <£0.10 per statement).
  - PII-aware — statements contain account numbers, addresses.
  - Gracefully degrades to manual review for low-confidence parses.

### 8.2 Options considered

#### Option A — Azure Document Intelligence
**Pros:**
- Cheapest per-page (~$0.01/page Read, ~$0.065/page prebuilt layout+tables).
- Has a `prebuilt-bankStatement.us` model (US-trained; **VERIFY** coverage for UK statements).
- Strong on tables + key-value pairs.
- SOC 2 Type II, ISO 27001, HIPAA. EU data residency available (UK South region).
- Fast (~seconds per page).

**Cons:**
- UK prebuilt bank statement model unclear — may need custom-trained model for UK formats.
- Investment statement variety is high; no prebuilt model exists.
- Custom model training takes labelling effort.
- Extraction yields structured JSON but schema flexibility is limited.

**Best fit for:** UK retail bank statements at volume, simple layouts.

#### Option B — AWS Textract
**Pros:**
- AnalyzeDocument (forms + tables) $0.065/page.
- AnalyzeExpense for receipts.
- Mature, integrates well if we're already on AWS.
- Good table extraction accuracy.

**Cons:**
- No bank-statement specialist model.
- More expensive than Azure for equivalent output.
- Weaker at reasoning over extracted content (pure extraction, not interpretation).
- We're on Fly.io not AWS — adds an egress.

**Best fit for:** If AWS were already in the stack, Textract would be the default. It isn't.

#### Option C — Google Document AI
**Pros:**
- `Bank Statement Parser` specialised processor exists (~$0.75 per processed document).
- Good at structured business documents.
- Works well for invoices, receipts.

**Cons:**
- More expensive per document than Azure or Textract.
- UK bank statement parser coverage unknown — marketed for US.
- Adds a third cloud dependency.

**Best fit for:** If we were committed to GCP and needed its specific processors.

#### Option D — Claude API (Sonnet 4.6 with Files API + tool use)
**Pros:**
- **Schema flexibility:** Structured outputs via tool use validates output against our canonical schema directly. One call returns `{accounts: [...], transactions: [...], holdings: [...]}` in exactly the shape we need.
- **Reasoning over layout:** Handles arbitrary formats without templates — a first-year ISA statement from a challenger broker parses as well as a Barclays statement.
- **UK-specific intelligence:** Can identify tax wrappers from context ("Cash ISA", "Stocks & Shares ISA", "SIPP", "General Investment Account"), distinguish ISAs from pensions correctly even when labels are ambiguous.
- **Audit trail:** Extended thinking + tool-use reasoning is inspectable — we can log why the model categorised something, helpful for support and compliance.
- **Cost control:** Sonnet 4.6 at $3/M input + $15/M output, 50% batch discount available, 90% prompt caching discount for the system prompt.
- **Native PDF handling:** Files API accepts PDFs directly; no OCR pre-step.
- **Consistent stack:** We already use Claude for other parts of Eyrie — one fewer vendor.

**Cons:**
- Most expensive per-page in nominal terms (a 10-page statement ≈ 30k input tokens ≈ $0.09 at list price, 50% less with batch).
- Higher latency than Azure/Textract for realtime UX (typically 5–15s for 10-page document).
- PII considerations — Anthropic's DPA and data residency posture must be reviewed.
- Model can hallucinate on ambiguous layouts (mitigation: structured outputs + post-validation).
- No native "retry with different model" for ensemble voting (easily bolted on).

**Best fit for:** Varied UK investment/pension statements where layout intelligence matters more than per-page cost.

#### Option E — Hybrid: Azure DI primary + Claude fallback
**Structure:**
1. Every PDF first goes to Azure DI `prebuilt-layout` (cheap, reliable tables + OCR).
2. Azure's structured output is passed to Claude API for schema mapping + canonicalisation + tax-wrapper classification. Claude input is the Azure JSON, not the raw PDF — much cheaper than sending raw PDFs.
3. If Azure confidence is very low (garbled PDF, complex pension statement), fall through to sending the raw PDF to Claude directly.

**Economics (illustrative):**
- Azure DI layout: 10 pages × $0.01 = $0.10.
- Claude API on Azure JSON: ~5k tokens input → ~$0.015 per statement.
- Total: ~$0.12 per statement, with 95% accuracy band and <8s latency.
- Fallback to direct-PDF-to-Claude for ~5% of statements: ~$0.10 extra per fallback.
- **Blended: ~£0.10–£0.12 per statement at list pricing.**

**Pros:**
- Best of both worlds: cheap OCR, intelligent schema mapping.
- Resilient to PDF quality variance.
- Easy to monitor: Azure confidence signals the fallback.
- Aligns with UK data residency (Azure UK South) and keeps one cloud primary.

**Cons:**
- Two vendors to manage.
- Pipeline complexity (worth it at scale).

### 8.3 Recommendation: Hybrid (Option E), Claude API (Option D) as the default for MVP

- **MVP (first 3 months post-launch):** Claude API alone, direct PDF → canonical JSON via tool use. Keeps us nimble, one vendor, fastest to ship. Budget ~£0.15 per statement at list price, within target.
- **Scale (>1,000 statements/month):** Introduce Azure DI as a pre-pass, reducing per-statement cost to ~£0.10 and improving latency.
- **CSV** parsed server-side with Swift's `CSVDecoder` + header-mapping heuristics. No API needed.

Decision trigger for the transition: monitor (a) PII review events — do we even want Claude seeing raw PDFs, or prefer Azure's UK-region extraction? — and (b) monthly statement volume crossing 500/month.

### 8.4 Parse pipeline (end-to-end)

```
User uploads PDF
        ↓
Upload to S3-compatible store (sha256 dedupe; reject duplicate within same user+24h)
        ↓
Create StatementArtifact (parse_status=queued)
        ↓
Background worker picks up:
    - PII minimisation step (redact national insurance numbers, etc. pre-LLM)
    - Call parser (Claude API for MVP; Azure DI → Claude for scale)
    - Structured output validated against canonical schema
    - Confidence score computed
        ↓
If confidence >= HIGH:
    - Stage records in a "pending acceptance" bucket
    - Notify user in-app ("Review 23 transactions from your March 2026 HL statement")
        ↓
User reviews:
    - Confirm account identity (match to existing Account or create new)
    - Spot-check 3 highlighted transactions
    - Accept or reject
        ↓
On accept:
    - Records flow to CanonicalisationService
    - Merge with existing data (respect unique constraints; statement-provided records mark source_provider='statement')
    - StatementArtifact.parse_status = accepted
```

### 8.5 Manual entry

- "Add account" wizard with institution typeahead.
- "Log a transaction" with category picker.
- "Batch import CSV" with column mapping UI.
- "Add a pension contribution series" recurring entry.
- Everything funnels through `ManualEntryService` → canonical records with `source_provider='manual'`.
- Manual records flag as such in UI — user sees a small "M" badge on transactions they entered.
- Edits to manual records create a new version, preserving history (for undo and for AI explanations of "where does this number come from?").

### 8.6 Statement quality guardrails

- **High-confidence bands** (>0.95): auto-accept with a "review highlights" CTA.
- **Medium** (0.80–0.95): require user review of all fields before accept.
- **Low** (<0.80): reject + prompt user to redo upload or switch to manual entry for the problem fields.
- **Hashed audit log**: the raw parser output is kept alongside the canonicalised records for 90 days, so we can diagnose misclassifications and improve the pipeline.

### 8.7 PII and data handling

- Files are encrypted at rest (AES-256 via Fly.io volume or S3 SSE).
- PII minimisation before any LLM call: redact NI numbers, full addresses collapsed to postcode prefix.
- Claude API's data handling: Zero Data Retention (ZDR) available on request at enterprise; for MVP, confirm retention posture in DPA.
- Deletion: user deleting an account deletes all associated StatementArtifacts within 30 days (GDPR erasure, documented in `/legal-compliance/compliance-checklist.md`).

---

## 9. Canonicalisation, Deduplication & Conflict Resolution

### 9.1 Idempotency

Every adapter output is wrapped in an idempotent canonicalisation step:

```swift
// Pseudocode
let incoming = plaidAdapter.syncTransactions(link: link, since: cursor)
let canonical = PlaidCanonicaliser.map(incoming)      // pure, deterministic
let result = CanonicalisationService.commit(canonical, provenance: .plaid(link))
// `commit` upserts on (source_provider, source_txn_id), respects user edits,
//  logs a SyncRun with counts_added, counts_modified, counts_removed
```

Running the same `syncTransactions` call twice produces zero writes on the second run (unique constraint + no-op updates).

### 9.2 Merging across providers (fan-in)

This is the scenario users find interesting: "I connected my Vanguard UK via Plaid AND via SnapTrade". What happens?

**Policy (declarative, in code):**

Preferred source priority for each canonical entity:
- `Account`: Plaid > SnapTrade > Statement > Manual.
- `Transaction`: always source-of-truth from its originating Account's preferred provider.
- `Holding`: SnapTrade > Plaid > Statement > Manual (SnapTrade is investment-first; broker data is more accurate).
- `InvestmentTransaction`: same as Holding.

**Mechanism:**
1. Two adapters each report an account with the same `institution_id` and overlapping `mask` or `account_number`.
2. `CanonicalisationService` detects the candidate match via a matching function (institution + mask + balance sanity check).
3. Chooses the preferred provider by policy.
4. Marks the non-preferred `Account` record as `is_hidden=true, closed_at=<now>, metadata.superseded_by=<id>`.
5. User is notified in-app: "We detected that your Vanguard UK ISA is linked via two providers. We're showing data from SnapTrade (more accurate for investments)."

User can override in settings.

### 9.3 User edits

If a user edits a transaction's category, merchant or notes, the edit is recorded in `Transaction.user_edited_fields` and the field is marked `is_user_edited=true`. Future provider syncs never overwrite these fields — even if Plaid re-enriches the merchant name differently next month.

The underlying provider value is preserved in `user_edited_fields[field].old`, so if the user resets the edit they get the fresh provider value.

### 9.4 Deletion

- Provider-driven deletion: Plaid `removed` array in `/transactions/sync` → canonical `Transaction.is_hidden=true, closed_at=<now>`. We never hard-delete provider data; we soft-hide it to preserve audit.
- User deletion: explicit action, hard-deletes the record with a 30-day soft-delete buffer.
- Account deletion: cascades to all dependent entities.

### 9.5 Conflict resolution examples

| Conflict | Resolution |
|---|---|
| Plaid says balance £1,234; SnapTrade says £1,220; gap of £14 | Both kept as balance snapshots. UI shows latest. History shows both. |
| User edits category; Plaid re-enriches next sync with a different category | User edit wins. Provider enrichment stored in `user_edited_fields`. |
| Two statements uploaded for same account + overlapping month | Later-dated statement's transactions override via `sha256`-dedupe + `(provider, source_txn_id)` match. Earlier statement kept as artifact. |
| User connects Hargreaves Lansdown via Plaid, then later adds a manual holding at HL | Manual holding tagged with `source_provider='manual'`; if Plaid later returns the same security, user prompted: "We now have automatic data from HL. Merge your manual holding with the automatic one?" |

---

## 10. Payments Port — Abstracted for Yapily or Alternatives

### 10.1 Why a separate port

Payments (PIS + VRP) have a fundamentally different risk and compliance profile from read-only data. Mixing them into the same interfaces would (a) over-complicate read-only adapters, (b) conflate data-aggregation PII risk with money-movement risk.

`PaymentsPort` is its own interface. Implementations are lazily registered — if no `PaymentsPort` implementation is bound, payments features in the app are hidden.

### 10.2 Adapter options

- **YapilyPaymentsAdapter** — kept as a sleeping adapter. Wire up if commercial VRP in Yapily matures and terms are right.
- **PlaidPaymentsAdapter** — implement when Plaid's cVRP production availability aligns with CMA9 coverage.
- **Manual payment logging** — user enters "I paid this myself" — this is a `Transaction` create, not a `PaymentsPort` usage. Distinct.

### 10.3 Interface (see §7.1)

Deliberately minimal: create mandate, execute payment, cancel, get status. The existing `VRPSafetyModel` logic (monthly caps, safety-first) remains in `PaymentsService` on top of whichever adapter is wired. **No auto-saving changes are required for users when swapping Yapily for Plaid.**

### 10.4 Launch posture

At launch of the new data layer, **no `PaymentsPort` is bound**. The VRP auto-saving feature is feature-flagged OFF for new users. Existing users with Yapily VRP mandates continue to function via the legacy Yapily adapter, which is kept alive for 6 months to avoid disrupting them.

This is acceptable because the new data layer's value to users is data richness, not payments. Payments returns as a separate workstream post-launch.

---

## 11. iOS Integration Contract

### 11.1 Link flows

- **Plaid Link:** Swift Package `https://github.com/plaid/plaid-link-ios-spm.git`. Universal Links configured for OAuth redirects (`/.well-known/apple-app-site-association` served by backend). App launches Link with a token from `LinkPort.initiate`. Handler calls `LinkPort.complete` on success.
- **SnapTrade Portal:** `SFSafariViewController` opened with the short-lived redirect URL from `loginSnapTradeUser`. Callback back to app via Universal Link or deep link. Handler calls `LinkPort.complete`.
- **Statement upload:** Native `UIDocumentPickerViewController` + drag-and-drop on iPad. File posted to backend; backend creates `StatementArtifact` and returns ID; iOS subscribes to push notification for parse completion.
- **Manual entry:** Native SwiftUI forms. All validation client-side + server-side.

### 11.2 State management

iOS holds zero provider-specific state. The app model tracks:
- `User`
- `[Account]` (canonical)
- `[ProviderLink]` (minimal — status, institution, last_sync_at)
- `[Transaction]` (paginated, canonical)
- `[Holding]`, `[InvestmentTransaction]`, `[Balance]`

When a provider adds a new capability (e.g., Plaid ships UK Liabilities), backend maps to canonical and iOS shows the new entity without changes.

### 11.3 Push notifications (APNs)

New `APNS` events required:
- `SyncCompleted` (link-level) → triggers quiet refresh.
- `ReauthRequired` (link-level) → surfaces banner in app.
- `StatementParseReady` (artifact-level) → prompts user to review.
- `ConflictDetected` (canonicalisation-level) → prompts user to resolve.

These are additive to the existing APNs workstream noted in `/technical/backend-assessment.md`.

### 11.4 Existing iOS feature impact

- **Home dashboard:** zero changes — it reads canonical `Account` + `Balance`.
- **Transactions view:** minor — show provenance badges (Plaid/SnapTrade/Manual/Statement).
- **Holdings view:** minor — same provenance badges; "Refresh" button calls refresh endpoint.
- **Tribe:** zero changes — reads canonical.
- **Ask Eyrie:** zero changes to interface; the underlying retrieval layer benefits from richer canonical data.
- **Financial Decision Engine:** zero changes — already operates on canonical.
- **Settings → Connected Accounts:** replaces Yapily/Yodlee UI with unified provider UI.

---

## 12. Security, Privacy & Compliance

### 12.1 Credentials & tokens

- Plaid `access_token` encrypted per-user with AES-256-GCM. Encryption keys: **review key derivation architecture** — if currently derived from a single Fly.io env-var master key, migrate to a dedicated secrets-management service (AWS KMS via Fly.io egress, or HashiCorp Vault self-hosted) to avoid a single compromise → all users exposed.
- SnapTrade `userSecret` encrypted identically. **Persist on first registration atomically — loss = delete + re-onboard, no recovery.**
- Manual statement files encrypted at rest (Fly.io volume SSE or S3 SSE-S3).

### 12.2 Webhook verification

- Plaid: HMAC-SHA256 JWT from `Plaid-Verification` header, public key cached from `/webhook_verification_key/get`.
- SnapTrade: HMAC-SHA256 of raw body with client secret, constant-time compare.
- Both wrapped in `WebhookReceiverPort.verify`.

### 12.3 FCA & ICO posture

- Plaid is FCA AISP FRN 804718 + PISP.
- SnapTrade is not FCA-regulated itself — it's a Canadian data aggregator using broker-issued OAuth tokens. SnapTrade is NOT the AISP for UK purposes; it passes through broker authentication. This is acceptable for read-only broker data but **needs explicit review with our solicitor** (flagged in `/legal-compliance/compliance-checklist.md`) to confirm no FCA authorisation is required for Eyrie's use of SnapTrade.
- ICO: both Plaid and SnapTrade are data processors under UK GDPR. Execute DPAs before GA.

### 12.4 Data residency

- Plaid: US-hosted by default; UK/EU residency terms to be confirmed in pricing negotiation.
- SnapTrade: Canadian-hosted. Canada has UK adequacy → no additional safeguards required. **Confirm with DPA.**
- Claude API (statements): Anthropic's enterprise ZDR available; confirm data residency options.
- Eyrie backend: Fly.io London region. PostgreSQL primary in London.

### 12.5 Zero-trust posture for AI layer

The AI layer (Ask Eyrie, Financial Decision Engine) reads from canonical tables only. It has no access to `ProviderLink.provider_credentials`, raw statement files, or webhook payloads. This is enforced at the service layer, not just by convention.

### 12.6 Consent & re-auth UX

- Plaid: 180-day consent. Surface re-auth prompt 14 days before expiry. Push notification 7 days before. Banner at expiry.
- SnapTrade: broker-dependent expiry (90 days to 1 year). Status detected on sync; prompt user via banner + push.
- Statement/Manual: no consent expiry.

### 12.7 Right to erasure

- User deletes account → cascades to `ProviderLink` → adapter calls `LinkPort.revoke` (Plaid Item removal, SnapTrade `deleteSnapTradeUser`) → all canonical records hard-deleted after 30-day soft-delete window → statement artifacts deleted with bucket purge.
- Documented in `/legal-compliance/compliance-checklist.md`.

---

## 13. Migration & Rollout Plan

### 13.1 Sequencing principle

New stack runs alongside old for the migration window. Readers of canonical tables don't care which adapter wrote the data. We move users in cohorts.

### 13.2 Phases

**Phase 0 — Foundations (Week 1–2):**
- Canonical schema migrations on staging.
- `CanonicalisationService`, `SyncOrchestrator`, `InstitutionService` implemented behind feature flags.
- `ProviderLink` table seeded with existing Yapily + Yodlee connections (no behaviour change — just migrating state).

**Phase 1 — Plaid adapter (Week 3–4):**
- `PlaidAdapter` implements `LinkPort`, `AccountsPort`, `TransactionsPort`, `BalancesPort`, `IdentityPort`, `WebhookReceiverPort`.
- iOS integrates LinkKit.
- Sandbox green-light tests.
- Internal dogfooding with 5 UK accounts across CMA9 + neobanks.

**Phase 2 — SnapTrade adapter (Week 4–5, parallel to Phase 1 iOS):**
- `SnapTradeAdapter` hand-rolled REST client + HMAC signing.
- iOS integrates SFSafariViewController portal flow.
- Sandbox tests; Test with AJ Bell + IBKR + Trading 212 production accounts.

**Phase 3 — Statement/Manual pipeline (Week 5–6):**
- Claude API parsing behind a feature flag.
- `StatementIngestService`, `ManualEntryService`.
- iOS upload screen + manual entry forms.
- Test on 20 real statements across target providers.

**Phase 4 — Canonicalisation hardening (Week 6):**
- Fan-in merge logic tested with dual-linked account scenarios.
- Conflict resolution UX on iOS.
- User-edit preservation verified.

**Phase 5 — Staged rollout (Week 7–8):**
- 5% cohort migrates from Yapily/Yodlee to Plaid/SnapTrade.
- Monitor error rates, re-auth frequency, support tickets.
- Ramp: 25% → 50% → 100% over three weeks.
- Yapily VRP adapter kept alive for existing mandates.

**Phase 6 — Decommission (Week 10+):**
- Yodlee code removed.
- Yapily banking-data code removed; Yapily payments code frozen but present (inside `YapilyPaymentsAdapter`).
- `wealth-data-providers.md` updated to reflect final state.

### 13.3 User-facing messaging

- Existing users see a one-time "We've upgraded how Eyrie connects to your accounts — please reconnect your banks. This takes 2 minutes."
- For each reconnection, their existing canonical Account records are preserved (no history loss) and the new `ProviderLink` points at them via `source_account_id` rematching.
- For Yodlee-connected wealth accounts (which were never that many), they're offered SnapTrade or statement upload.

### 13.4 Rollback

- Feature-flagged per user. Roll back a cohort in under 5 minutes by flipping the flag (reverts to legacy adapters; canonical data remains intact).
- Worst case: Plaid down for >6h → disable Plaid Link flow, existing users see "Last synced X hours ago" banner, no data loss.

---

## 14. Testing Strategy

### 14.1 Unit tests

- Canonicaliser pure functions: provider JSON → canonical records. Comprehensive property tests.
- Sign-convention tests: Plaid positive-for-outflow → canonical negative.
- Categorisation mapping: Plaid `personal_finance_category` → Eyrie slug for all 16 primaries + representative detaileds.
- Manual entry validation: boundaries, currencies, tax wrappers.
- Statement parser output schema validation.

### 14.2 Integration tests

- Plaid sandbox end-to-end: Link → sync → edit → re-sync.
- SnapTrade sandbox end-to-end: register → portal → list accounts → holdings.
- Statement upload end-to-end: PDF → parse → accept → canonical records present.
- Webhook replay: signed payload → route → canonical writes.

### 14.3 Scenario tests

- Dual-link merge: same account via Plaid + SnapTrade → correct priority wins.
- Re-auth cycle: simulate `ITEM_LOGIN_REQUIRED` → user reconnects → cursor preserved → no full re-sync.
- User edit preservation: edit category → Plaid re-enriches → user edit wins.
- Statement artifact conflict with existing Plaid data: correct merge rules applied.

### 14.4 Load tests

- 100 concurrent Plaid Link completions.
- 10,000 `SYNC_UPDATES_AVAILABLE` webhooks in 60 seconds → queue absorption + backpressure.
- 500 statement uploads simultaneously → worker pool scaling.

### 14.5 Production validation (pre-GA)

- Internal team uses the new stack exclusively for 2 weeks.
- Track: sync latency p95, re-auth rate, statement parse success rate, user-edit loss events (must be zero).

---

## 15. Observability, SLOs & Operational Runbook

### 15.1 Metrics

- `eyrie_sync_runs_total{provider,status}` — Prometheus counter.
- `eyrie_sync_duration_seconds{provider}` — histogram.
- `eyrie_webhook_received_total{provider,code}` — counter.
- `eyrie_webhook_signature_failures_total{provider}` — counter (alert if >0 over 1h).
- `eyrie_provider_link_status{provider,status}` — gauge.
- `eyrie_cursor_age_seconds{provider_link_id}` — gauge; alert at >48h.
- `eyrie_statement_parse_confidence` — histogram.
- `eyrie_canonicalisation_conflicts_total{resolution}` — counter.

### 15.2 SLOs

- p95 webhook → canonical write latency: <30 s.
- Sync success rate over 7 days: >99%.
- Re-auth completion rate within 14 days of prompt: >80%.
- Statement parse success rate (high-confidence band): >85%.

### 15.3 Runbook triggers

- Plaid circuit-break: 5xx rate >1% for 5 min → surface banner, queue syncs, retry with backoff.
- Webhook signature verification failures: alert on first occurrence — indicates either a bad rotation, attack, or Plaid change.
- Cursor stuck (>24h with `has_more=false` but user says missing transactions): run full resync via reset.
- Statement parse queue backlog >500: scale workers; page on-call if >2,000.

### 15.4 Alerting

Reuse Fly.io metrics + a lightweight self-hosted Grafana. Alert channels: PagerDuty (production) + Slack (dev).

---

## 16. Risks, Open Questions & Verification Checklist

### 16.1 Top risks

1. **Plaid UK institution coverage for investments is uneven.** Mitigation: SnapTrade for brokers we care about; statement upload for the rest.
2. **SnapTrade UK coverage is partly unverified.** Mitigation: confirm with SnapTrade sales before GTM; keep statement upload as backup for unsupported brokers.
3. **`userSecret` loss is unrecoverable.** Mitigation: atomic persist on registration; encrypted at rest; background reconciliation job that catches mismatched state.
4. **Commercial VRP timing is uncertain.** Mitigation: keep Payments port empty; re-evaluate quarterly.
5. **Statement parsing accuracy for niche UK pension providers.** Mitigation: hybrid Azure + Claude; user review required for medium-confidence.
6. **Per-user encryption key stored as Fly.io env-var.** Mitigation: migrate to KMS or Vault before scaling.
7. **Dual-link deduplication corner cases.** Mitigation: comprehensive scenario tests; clear user-facing UI for manual override.
8. **Cursor loss forces full re-sync.** Mitigation: daily backup of ProviderLink.cursor; alert on cursor age >48h.
9. **GDPR data residency for UK users sent to Claude/Anthropic US infrastructure.** Mitigation: Azure DI primary parser (UK South region), Claude fallback for complex cases only, document in compliance checklist.
10. **Plaid pricing at scale.** Mitigation: negotiate in advance of 10k MAU; have TrueLayer as a mental backup (same port implementation effort ~3 days).

### 16.2 Open questions for Claude Code

Same as Plaid research Q&A + SnapTrade research Q&A. Summarised:

- Plaid UK investment institution coverage list (current as of contract date)?
- Plaid DPA and UK data residency terms in writing?
- Plaid pricing quote for projected volume?
- SnapTrade coverage confirmation for HL, Vanguard UK, Freetrade?
- SnapTrade GDPR DPA?
- Claude API retention and residency posture confirmation (enterprise ZDR)?
- Azure DI coverage for UK bank statement formats — custom model needed?
- iOS minimum version currently required by `plaid-link-ios-spm` main branch?
- FCA implication of using SnapTrade — is our solicitor comfortable with passthrough broker OAuth?

### 16.3 Pre-GA checklist

- [ ] Plaid DPA signed
- [ ] SnapTrade DPA signed
- [ ] Anthropic DPA signed (for statement parsing)
- [ ] Encryption key architecture migrated off Fly.io env-var (or risk accepted in writing)
- [ ] Sandbox tests: 100% pass for both providers
- [ ] Production tests with 5 internal users each across UK + US providers
- [ ] Statement pipeline tested on ≥20 real statements
- [ ] iOS LinkKit Universal Links working end-to-end
- [ ] SnapTrade SFSafariViewController flow tested on iOS 17+ and iOS 18+
- [ ] Webhook signature verification active for both providers
- [ ] Metrics + Grafana dashboards live
- [ ] Runbook circulated with on-call
- [ ] Compliance checklist updated
- [ ] User communication email drafted

---

## 17. Appendix A — Endpoint Inventory

Current backend: 31 endpoints across auth, banking, accounts/transactions, subscriptions, VRP, verification, business mode, wealth (Yodlee), webhooks, legal.

**After migration:**

| Domain | Before | After | Delta |
|---|---|---|---|
| Auth | 7 | 7 | 0 |
| Subscriptions | 4 | 4 | 0 |
| Signup+ | 3 | 3 | 0 |
| Verification | 2 | 2 | 0 |
| Business Mode | 3 | 3 | 0 |
| Financial Decision Engine | 1 | 1 | 0 |
| Legal/Health | 3 | 3 | 0 |
| Yapily Banking | 4 | 0 | -4 |
| Yodlee Wealth | 10 | 0 | -10 |
| Yapily Webhooks | 1 | 0 | -1 |
| VRP | 6 | 6 | 0 (moved to PaymentsPort, same surface) |
| **New: Provider Links** | 0 | 5 | +5 |
| **New: Canonical Accounts** | 0 | 3 | +3 |
| **New: Canonical Transactions** | 0 | 4 | +4 |
| **New: Canonical Holdings** | 0 | 3 | +3 |
| **New: Canonical Investment Transactions** | 0 | 2 | +2 |
| **New: Statement Ingest** | 0 | 4 | +4 |
| **New: Manual Entry** | 0 | 4 | +4 |
| **New: Plaid Webhooks** | 0 | 1 | +1 |
| **New: SnapTrade Webhooks** | 0 | 1 | +1 |
| **Total** | 31 | 36 | +5 |

(Internal services grow more than the public API; many capabilities are absorbed into fewer, better endpoints.)

---

## 18. Appendix B — Glossary

- **AIS / AISP** — Account Information Service / Provider (PSD2).
- **Canonical** — the Eyrie-internal data model that all adapters normalise into.
- **CanonicalisationService** — the service that takes adapter output and commits to canonical tables with merge/dedupe.
- **Conflict resolution** — declarative rules for which source wins when two providers disagree.
- **cVRP** — Commercial Variable Recurring Payments (UK Open Banking).
- **FCA** — Financial Conduct Authority.
- **FIGI** — Financial Instrument Global Identifier (OpenFIGI); stable security ID.
- **Institution** — a bank, broker, or pension provider, as referenced in the canonical model.
- **Manual entry** — user-entered financial records, treated as a first-class provider.
- **Minor units** — smallest currency denomination (pence, cents).
- **PIS / PISP** — Payment Initiation Service / Provider.
- **Port (Hexagonal)** — an interface defined by the core domain that adapters implement.
- **Provenance** — the `source_provider` + `source_record_id` attached to every canonical row.
- **ProviderLink** — one authenticated connection to a provider for a user.
- **PSD2** — Payment Services Directive 2 (EU/UK regulation).
- **Statement artifact** — a user-uploaded PDF/CSV and its parsed payload.
- **SyncRun** — audit record of one adapter-triggered update.
- **Tax wrapper** — UK: ISA, SIPP, LISA, JISA, GIA. US: 401k, IRA, Roth.
- **TPP** — Third Party Provider (in PSD2 terms, e.g. Plaid as an AISP/PISP).
- **TPP-managed consent** — FCA 2023 reform that moves re-auth from bank-led 90-day to TPP-managed 180-day.
- **VRP** — Variable Recurring Payment (UK Open Banking).

---

## 19. Appendix C — References

**Internal:**
- `/outputs/research/plaid-research.md` — full Plaid findings
- `/outputs/research/snaptrade-research.md` — full SnapTrade findings
- `/Eyrie/technical/backend-assessment.md` — current backend state
- `/Eyrie/market/wealth-data-providers.md` — provider landscape
- `/Eyrie/product/feature-status.md` — what's live, what's missing
- `/Eyrie/legal-compliance/compliance-checklist.md` — pre-launch legal blockers
- `/Eyrie/finance/unit-economics.md` — pricing/cost model (needs Plaid refresh)

**External:**
- Plaid Docs: https://plaid.com/docs/
- Plaid iOS: https://plaid.com/docs/link/ios/
- Plaid-Link-iOS-SPM: https://github.com/plaid/plaid-link-ios-spm
- Plaid Investments: https://plaid.com/docs/investments/
- Plaid Webhooks: https://plaid.com/docs/api/webhooks/
- SnapTrade Docs: https://docs.snaptrade.com/
- SnapTrade SDKs: https://github.com/passiv/snaptrade-sdks
- SnapTrade Pricing: https://snaptrade.com/pricing
- SnapTrade Integrations: https://snaptrade.com/brokerage-integrations
- Azure Document Intelligence: https://learn.microsoft.com/en-gb/azure/ai-services/document-intelligence/
- AWS Textract: https://aws.amazon.com/textract/
- Google Document AI: https://cloud.google.com/document-ai
- Anthropic Claude API — Files & Tool Use: https://docs.anthropic.com/en/docs/build-with-claude/files
- UK Open Banking TPP-managed consent: https://www.openbanking.org.uk/

---

*End of brief. Derived artefacts in `/technical/financial-data-layer/`, `/technical/financial-data-layer/diagrams/`, and `/technical/CLAUDE_CODE_SPEC.md`.*
