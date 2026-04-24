# 03 — Plaid Adapter

Full research: [`/outputs/research/plaid-research.md`](../../outputs/research/plaid-research.md). This file is the design for `PlaidAdapter`.

## Role

Primary provider for:
- UK + US bank transactions (`/transactions/sync`, cursor-based)
- UK + US account list (`/accounts/get`)
- UK sort code + account number (`/auth/get`)
- UK + US identity (`/identity/get`) — unblocks account name verification
- Balances (`/accounts/balance/get`)
- **Opportunistic** UK investments (`/investments/holdings/get`, `/investments/transactions/get`) — only where institution coverage confirmed; SnapTrade is preferred for investments.
- Webhooks (AIS + Payment webhooks when PaymentsPort is bound).

**Not used for:**
- UK Liabilities (unsupported). Statement upload fallback.
- UK Income (partial). Deferred.
- US Signal/Enrich. Deferred.

## Lifecycle

### First-time link
1. `POST /link/token/create` with `client_user_id = user.id`, `products = [transactions, auth, identity]` (+ `investments` on investment-intent links), `country_codes = [GB, US]`.
2. iOS opens LinkKit with the returned token.
3. User authenticates with their bank (OAuth for all CMA9).
4. LinkKit returns `public_token` + metadata.
5. Backend `POST /item/public_token/exchange` → durable `access_token` + `item_id`.
6. Encrypt `access_token` per-user (AES-256-GCM) and persist as `provider_link` row.
7. Initial pull: `/accounts/get`, `/accounts/balance/get`, first `/transactions/sync` (empty cursor).

### Re-auth (update mode)
1. Webhook `PENDING_EXPIRATION` fires ~7 days before consent expiry → notify user.
2. Webhook `ITEM_LOGIN_REQUIRED` fires on expiry or bank-driven invalidation.
3. `POST /link/token/create` with the existing `access_token` → token enters update mode.
4. iOS reopens LinkKit.
5. On success, same `access_token`, consent refreshed. Cursor preserved.

### Revocation
- User removes link → `POST /item/remove` → mark `provider_link.status = 'revoked'` → cascade-hide canonical records.

## Data sync

### Transactions
- `POST /transactions/sync` with `{access_token, cursor, count: 500}`.
- Response: `added`, `modified`, `removed`, `next_cursor`, `has_more`.
- **Persist cursor atomically** with canonical write (same DB transaction). Cursor loss = forced re-sync.
- Paginate until `has_more=false`.
- Trigger: `SYNC_UPDATES_AVAILABLE` webhook (primary); daily cron fallback.
- Rate limit on `/transactions/sync`: 50 req / Item / min (per Plaid docs, Apr 2026). The older `/transactions/get` endpoint is 30 req / Item / min — we do not use it. Enforce with per-Item token bucket regardless.

### Accounts + balances
- `/accounts/get` on link, then on every sync cycle.
- `/accounts/balance/get` on link + user-triggered refresh (expensive; don't poll).

### Investments (opportunistic)
- Only pull if user's `provider_link.metadata.investments_enabled = true`.
- `/investments/holdings/get` daily + on `HOLDINGS_DEFAULT_UPDATE`.
- `/investments/transactions/get` paginated from `last_sync_at`.
- Treat all cost-basis fields as nullable (UK brokers weak on cost basis).

## Canonicalisation

### Sign convention
Plaid positive-for-outflow → canonical negative.

```swift
func signedAmount(from plaidAmount: Double, isOutflow: Bool) -> Int64 {
    let minor = Int64((plaidAmount * 100).rounded())
    return isOutflow ? -minor : minor
}
```

### Account type mapping
| Plaid `type/subtype` | Canonical `type/subtype` |
|---|---|
| depository/checking | depository/checking |
| depository/savings | depository/savings |
| depository/cd | depository/savings |
| credit/credit card | credit/credit_card |
| loan/mortgage | loan/mortgage |
| loan/student | loan/student_loan |
| investment/brokerage | investment/brokerage |
| investment/isa | investment/isa |
| investment/cash isa | depository/cash_isa |
| investment/sipp | pension/sipp |
| investment/pension | pension/pension |
| investment/401k | investment/us_401k |
| investment/ira | investment/us_ira |
| investment/roth | investment/us_roth |

Tax wrapper inferred from Plaid subtype where applicable.

### Category mapping
Plaid `personal_finance_category.primary` (16 values) → canonical slug. `detailed` (~100 values) → canonical `category_detailed` via a static table in `PlaidCategoryMap.swift`. Preserve raw primary + detailed in `user_edited_fields` history for audit.

### Security mapping (Investments)
- Prefer ISIN (UK primary identifier).
- Fall back to CUSIP + exchange (US).
- FIGI as strongest cross-market key where Plaid returns it.
- Upsert on ISIN; create if new.

## Webhooks

### Signature verification
`Plaid-Verification` header carries JWT (alg RS256).  
Flow:
1. Extract `kid` from JWT header.
2. Cache `POST /webhook_verification_key/get` response keyed by `kid` (15 min TTL).
3. Verify JWT signature + `request_body_sha256` claim against body hash.

### Relevant codes
| webhook_type | webhook_code | Action |
|---|---|---|
| TRANSACTIONS | SYNC_UPDATES_AVAILABLE | Enqueue sync |
| TRANSACTIONS | RECURRING_TRANSACTIONS_UPDATE | Enqueue recurring-refresh |
| ITEM | ERROR | Mark link as `error`; log |
| ITEM | PENDING_EXPIRATION | Notify user; status = `expiring` |
| ITEM | USER_PERMISSION_REVOKED | Mark `revoked`; hide accounts |
| ITEM | LOGIN_REPAIRED | status = `active` |
| HOLDINGS | DEFAULT_UPDATE | Enqueue holdings sync |
| INVESTMENTS_TRANSACTIONS | DEFAULT_UPDATE | Enqueue investment-txn sync |
| PAYMENT_INITIATION | PAYMENT_STATUS_UPDATE | (only if PaymentsPort bound) |

### Idempotency
Webhook receipts keyed by `(item_id, webhook_code, fired_at)` with a 7-day dedupe window in a `webhook_log` table.

## Errors

- `RATE_LIMIT_EXCEEDED` → exponential backoff with jitter, max 3 retries; open circuit after 5 consecutive.
- `INVALID_ACCESS_TOKEN` → mark link `error`; require re-link.
- `ITEM_LOGIN_REQUIRED` → mark `re_auth_required`; surface to user.
- `INSTITUTION_DOWN` → retry in 15 min; up to 6h.

## Configuration

```swift
struct PlaidConfig {
    let clientId: String
    let secret: String
    let environment: PlaidEnvironment   // .sandbox | .production
    let webhookUrl: URL
    let redirectUri: URL                // Universal Link
    let products: [PlaidProduct]
    let countryCodes: [ISOCountryCode]
}
```

Secrets live in Fly.io secret store. **Consider KMS migration before scaling** (see `08-risks-and-open-questions.md`).

## iOS

- Swift Package: `https://github.com/plaid/plaid-link-ios-spm.git`
- `import LinkKit`
- `PLKPlaid.create(with:)` factory
- Universal Links: `apple-app-site-association` served at `https://curo-backend.fly.dev/.well-known/apple-app-site-association`
- Entitlement: `com.apple.developer.associated-domains = applinks:curo-backend.fly.dev`
- No WKWebView fallback.

## Open items for Claude Code

- Get Plaid UK institution coverage list for Investments (current as of contract).
- Confirm UK data residency terms in contract.
- Pricing quote for projected 10k UK + 5k US MAU.
- cVRP UK pilot bank coverage at time of launch (only if PaymentsPort moves to Plaid).
- iOS minimum version required by current main branch of `plaid-link-ios-spm`.

---
*Source of truth: `/technical/financial-data-layer-brief.md` §5.1, §7.*
