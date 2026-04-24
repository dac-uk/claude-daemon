# Plaid Product & API Research Brief
**For Eyrie/Curo — UK-first + US-ready Personal Finance iOS App**
**Research Date:** April 2026 | **Status:** Implementation-grade findings consolidated from Plaid docs and public sources

---

## Executive Summary

Plaid is a **viable replacement for Yapily banking data** and — contrary to earlier assumptions — **also covers UK investments** (brokerage, ISA, Cash ISA, Pension account types). Plaid UK is FCA-authorised (AISP FRN 804718) and PISP-licensed. Plaid's **UK institution coverage includes CMA9 banks, Monzo, Starling, Revolut and ~2,000 European institutions**; US coverage is ~12,000 institutions.

**The three product families relevant to Eyrie:**

1. **Data aggregation** (Transactions, Auth, Identity, Balance, Investments) — replaces Yapily AIS and a subset of Yodlee use cases (UK ISAs/pensions via Plaid Investments where coverage exists).
2. **Payment Initiation & VRP** (UK/EU) — sweeping VRP is live and mandated on CMA9; **commercial VRP (cVRP)** started first live payments Q1 2026 under the UK cVRP scheme, with FCA/PSR progress review end of 2026.
3. **Enrichment** (Enrich, Signal) — US-only today; not part of Eyrie's near-term plan.

**Critical UK update:** The FCA's 2023 reforms replaced bank-led 90-day reauth with **TPP-managed consent**, and the effective period is effectively 180 days (aligned with EU EBA RTS revision). Plaid implemented TPP-managed consent in the UK, giving smoother re-auth UX than the old Yapily model. This materially reduces the "silent bank disconnection" risk flagged in `Eyrie/technical/backend-assessment.md` Risk 1.

---

## A. Product Suite — UK vs US Availability

| Product | UK | US | CA | EU | Notes |
|---|---|---|---|---|---|
| **Transactions** | ✅ | ✅ | ✅ | ✅ | `/transactions/sync` (cursor-based) is primary; older `/transactions/get` deprecated for new integrations |
| **Auth** | ✅ | ✅ | ✅ | ✅ | UK returns sort code + account number; US returns routing + account |
| **Identity** | ✅ | ✅ | ✅ | ✅ | Account holder name/address/email/phone for KYC/AML |
| **Balance** | ✅ | ✅ | ✅ | ✅ | Real-time balance fetch |
| **Investments** | ✅ | ✅ | ✅ | Partial | UK: brokerage, ISA, Cash ISA, Pension, other — coverage is institution-dependent; verify list for HL, Vanguard UK, AJ Bell etc. before committing |
| **Liabilities** | ❌ | ✅ | Partial | ❌ | Credit cards, student loans, mortgages — **US/CA only**. UK gap. |
| **Income** | Partial | ✅ | ❌ | ❌ | Payroll-direct via Income product is US-first |
| **Signal** | ❌ | ✅ | ❌ | ❌ | ACH return risk scoring — not relevant for UK |
| **Enrich** | ❌ | ✅ | ❌ | Partial | Bring-your-own-transactions categorisation — US/EU selected |
| **Payment Initiation (PIS)** | ✅ | ❌ | ❌ | ✅ | PSD2 PISP via Plaid |
| **Variable Recurring Payments (VRP)** | Sweeping live; cVRP Q1 2026 pilot | ❌ | ❌ | ❌ | UK-only construct |
| **Transfer (ACH)** | ❌ | ✅ | ❌ | ❌ | US ACH; not UK |

Sources:
- [Plaid UK product page](https://plaid.com/en-gb/)
- [Plaid Institutions (Europe)](https://plaid.com/docs/institutions/europe/)
- [Plaid Investments docs](https://plaid.com/docs/investments/) — confirms brokerage/ISA/Cash ISA/pension account types in UK
- [VRP product page](https://plaid.com/en-gb/products/variable-recurring-payments/)
- [Commercial VRP whitepaper](https://assets.ctfassets.net/ss5kfr270og3/1Mrnyx8Pk8JfQCxHbkQH9E/d721cab77059d6fc12defe349e303fa7/Plaid-CommercialVRPsUK-Whitepaper-digital.pdf)

---

## B. Link Flow — Token Lifecycle and iOS SDK

**Flow (all markets):**
1. Backend `POST /link/token/create` → returns `link_token` (short-lived, single-use, tied to client_user_id)
2. iOS app opens `LinkKit` with `link_token`; user authenticates at bank (OAuth for CMA9 and most UK banks)
3. On success, Link returns `public_token` + `metadata` (institution, accounts)
4. Backend `POST /item/public_token/exchange` → returns durable `access_token` + `item_id`
5. All subsequent data pulls reference `access_token` (per-Item) and use product-specific endpoints

**Re-auth flow:**
1. Webhook `ITEM_LOGIN_REQUIRED` (or `PENDING_EXPIRATION`) fires
2. Backend `POST /link/token/create` with `access_token` set → returns re-auth `link_token` (Link in update mode)
3. iOS opens Link with that token; user re-authenticates
4. No new `access_token` issued — same Item, consent refreshed

**iOS SDK specifics:**
- **Package:** [plaid/plaid-link-ios-spm](https://github.com/plaid/plaid-link-ios-spm) — Swift Package Manager
- **CocoaPods dropped** from 7.x onward; 6.4.7 was last CocoaPods release (March 2026)
- **Minimum iOS:** Historically iOS 11+ for LinkKit 2.x; confirm current minimum in Package.swift before integration
- **Universal Links** required for OAuth redirects — when UK bank OAuth flow returns user to app, Universal Link dispatches back to LinkKit handler
- **LinkKit integration** is the only supported way to launch Link; WKWebView is NOT supported (will break OAuth + Passkeys on many banks)

Sources:
- [Plaid Link iOS docs](https://plaid.com/docs/link/ios/)
- [plaid-link-ios-spm](https://github.com/plaid/plaid-link-ios-spm)

---

## C. Transactions — Sync Semantics (Most Important Endpoint)

`/transactions/sync` is **cursor-based, incremental**, and is the intended endpoint for all new integrations.

**Request:**
```json
{
  "access_token": "access-...",
  "cursor": "base64-cursor-string-or-empty",
  "count": 500
}
```

**Response (shape):**
- `added`: new transactions since cursor
- `modified`: transactions that changed
- `removed`: transaction_ids no longer present (user deleted, bank correction)
- `next_cursor`: use in next call
- `has_more`: paginate until false

**Key properties:**
- Cursor max 256 base64 chars, store per-Item in DB
- Cursor validity: ≥1 year (still must be persisted carefully — losing it forces a full re-sync which triggers billing + rate-limit exposure)
- Initial pull: ≥30 days usually within ~10s after Link; full history (up to 24 months) within ~minutes for most institutions

**Webhooks (critical set):**
- `SYNC_UPDATES_AVAILABLE` — fires whenever transactions change for an Item. `initial_update_complete` + `historical_update_complete` booleans in payload
- `DEFAULT_UPDATE` (legacy, for `/transactions/get`) — ignore for new integrations
- `TRANSACTIONS_REMOVED` (legacy) — handled via `removed` field in sync response
- `ITEM_ERROR` — Item has entered error state
- `ITEM_LOGIN_REQUIRED` — user must re-authenticate
- `PENDING_EXPIRATION` — ~7 days before consent expires; prompt user to re-auth

**Enrichment fields** (Plaid-managed, no extra call):
- `personal_finance_category` (primary + detailed; 16 primary categories, ~100 detailed)
- `merchant_name` (normalised), `logo_url`, `website`
- `counterparties` (recipients, merchants)
- `location` (city, region, country)
- `payment_meta` (reference, payer/payee)
- `iso_currency_code`, `unofficial_currency_code`

Sources:
- [Plaid Transactions API](https://plaid.com/docs/api/products/transactions/)
- [Transactions sync migration](https://plaid.com/docs/transactions/sync-migration/)
- [Transactions webhooks](https://plaid.com/docs/transactions/webhooks/)

---

## D. Investments — UK Support Is Real but Coverage Varies

Plaid Investments supports UK account subtypes per [Plaid Investments product page](https://plaid.com/en-gb/products/investments/):
- `brokerage`
- `ira` (US)
- `401k` (US)
- `roth` / `roth 401k` (US)
- `cash isa` (UK)
- `isa` (UK — Stocks & Shares ISA)
- `pension` (UK)
- `sipp` (UK — Self-Invested Personal Pension)
- `other` (catch-all)

**Endpoints:**
- `/investments/holdings/get` — current positions: security_id, ticker, CUSIP, ISIN, SEDOL, quantity, cost_basis (where available), institution_price, iso_currency_code, account_id
- `/investments/transactions/get` — historical investment transactions: buy/sell/dividend/fee/transfer/cancel/cash/contribution
- `/investments/refresh` — force refresh

**Security taxonomy:** Plaid normalises securities across institutions. CUSIP (US), ISIN (global), SEDOL (UK) all surfaced where broker provides. Security types: `cash`, `cryptocurrency`, `derivative`, `equity`, `etf`, `fixed income`, `loan`, `mutual fund`, `other`.

**Cost basis:** Best-effort — availability varies by institution. UK brokers historically weaker on cost basis than US. Treat `cost_basis` as nullable.

**Webhooks:**
- `HOLDINGS_DEFAULT_UPDATE` — new holdings detected
- `INVESTMENTS_TRANSACTIONS_UPDATE`

**Coverage reality check (VERIFY before commit):** Plaid does support UK investment account types, but actual coverage of Hargreaves Lansdown, AJ Bell, Vanguard UK, Fidelity UK, Nutmeg, Moneybox, Standard Life, L&G, Aviva, Nest needs to be confirmed institution-by-institution via Plaid's UK coverage list. Known strong categories: neobanks with investment features (Revolut, Monzo investments if enabled), some robo-advisors. Weaker categories: traditional pension providers (Aviva/L&G/Standard Life), which is where SnapTrade's direct broker API approach may win, or we fall back to manual/statement upload.

Sources:
- [Plaid Investments API](https://plaid.com/docs/api/products/investments/)
- [Plaid Investments intro](https://plaid.com/docs/investments/)

---

## E. Liabilities — UK Gap

`/liabilities/get` returns credit card APRs/balances/minimum payments, student loans, mortgages. **US and Canada only**. UK credit card/mortgage data must come from another source (statement upload, or a UK-specific credit data vendor — not in scope for the first cut).

---

## F. Auth / Identity

- `/auth/get` — UK sort code + account number (GBP accounts); also SEPA IBAN/BIC for EU; US routing/account
- `/identity/get` — names, emails, phones, addresses as reported by bank

Used for: VRP setup verification (matching account holder name to Eyrie user), direct-debit set-up, KYC augmentation. Current Eyrie backend has a "account name verification" TODO (DTOs defined, implementation missing) — Plaid `/identity/get` plus a fuzzy name-match can close this.

---

## G. Enrich — Deferred

`/transactions/enrich` accepts your own transaction list and returns categorisation + merchant enrichment without needing an Item. US-first; UK support is limited. Eyrie already has on-device CoreML categorisation and merchant logos — this is NOT a day-1 priority, though it's an interesting fallback for transactions that come via statement upload (where there's no Plaid Item).

---

## H. Payments — PIS and VRP

**Payment Initiation (PIS, UK/EU):**
- `/payment_initiation/recipient/create` — create a recipient (IBAN or sort-code+account)
- `/payment_initiation/payment/create` — initiate a single payment with amount + reference
- User is redirected via Link (Plaid-hosted) to bank's SCA flow for consent
- Plaid is the PISP of record; merchant (Eyrie) is the beneficiary's principal or a TPP
- Webhooks: `PAYMENT_STATUS_UPDATE` with statuses `PAYMENT_STATUS_INITIATED` → `PAYMENT_STATUS_EXECUTED` / `PAYMENT_STATUS_FAILED` / `PAYMENT_STATUS_BLOCKED` / `PAYMENT_STATUS_AUTHORISING` / `PAYMENT_STATUS_REJECTED`

**Variable Recurring Payments (VRP):**
- Sweeping VRP (between accounts of same customer): live on all CMA9 banks — mandated since 2022. This is what Eyrie currently uses for VRP auto-saving.
- Commercial VRP (cVRP, merchant payments): **Pilot phase**, first live payments Q1 2026 under the UK cVRP industry scheme. Production availability is subject to each bank's readiness and commercial terms. FCA/PSR progress review end of 2026.
- Plaid's VRP API:
  - `/payment_initiation/consent/create` — consent window with limits (amount per payment, total, frequency, valid_from/to)
  - Payment executions reference the consent — no further SCA per payment

Implication for Eyrie VRP-based auto-saving: this is a **candidate workstream** for Plaid to replace Yapily. But the Yapily VRP implementation is already live in production (`VRPSafetyModel`, safety-first design, monthly caps) and the user's stated position is that Yapily may remain as the payments provider later. So the brief treats VRP as an **abstracted Payments port**, initially unpopulated in the new architecture, and Yapily OR Plaid Payments can plug in later.

Sources:
- [Plaid VRP docs](https://plaid.com/docs/payment-initiation/variable-recurring-payments/)
- [FCA cVRP scheme announcement](https://www.openbankingexpo.com/news/fca-announces-commercial-vrp-scheme-to-get-underway-in-2025/)

---

## I. Webhooks

**Delivery:**
- Webhook endpoint registered per client (not per Item)
- HMAC-SHA256 JWT verification — `Plaid-Verification` header carries signed JWT; public key fetched from `/webhook_verification_key/get` and cached
- At-least-once delivery; retries happen if non-2xx; no strict SLA published beyond "best effort"
- Idempotency: every webhook carries a stable set of fields (item_id, webhook_code, webhook_type) — implement receiver as idempotent on (item_id, webhook_code, first-seen-at)

**Full webhook code list (relevant):**
- **Item:** `ERROR`, `LOGIN_REPAIRED`, `NEW_ACCOUNTS_AVAILABLE`, `PENDING_EXPIRATION`, `USER_PERMISSION_REVOKED`, `WEBHOOK_UPDATE_ACKNOWLEDGED`
- **Transactions:** `SYNC_UPDATES_AVAILABLE`, `RECURRING_TRANSACTIONS_UPDATE`, plus legacy `INITIAL_UPDATE`, `HISTORICAL_UPDATE`, `DEFAULT_UPDATE`, `TRANSACTIONS_REMOVED`
- **Auth/Identity:** `VERIFICATION_EXPIRED`, `AUTOMATICALLY_VERIFIED`, `IDENTITY_VERIFICATION_STATUS_UPDATED`
- **Investments:** `HOLDINGS_DEFAULT_UPDATE`, `INVESTMENTS_TRANSACTIONS_DEFAULT_UPDATE`
- **Liabilities:** `LIABILITIES_DEFAULT_UPDATE` (US/CA only)
- **Payment Initiation:** `PAYMENT_STATUS_UPDATE`
- **Transfer (US):** `TRANSFER_EVENTS_UPDATE`

Source:
- [Plaid webhooks](https://plaid.com/docs/api/webhooks/)

---

## J. Rate Limits

Per-Item, per-product, documented on [Plaid rate limits](https://plaid.com/docs/errors/rate-limit-exceeded/). Representative production limits:
- `/transactions/sync`: **30 requests / Item / minute**
- `/auth/get`: 15 / Item / min
- `/identity/get`: 15 / Item / min
- `/investments/holdings/get`: 15 / Item / min
- `/accounts/get`: 60 / Item / min
- Per-client aggregate limits exist (negotiable with Plaid account manager at scale)

Rate-limit exhaustion returns HTTP 429 with `error_code: "RATE_LIMIT_EXCEEDED"`. Implement:
- Per-Item token-bucket in backend so we never exceed 30/min/item on `/transactions/sync`
- Exponential back-off with jitter on 429
- Webhooks-driven pull pattern (only sync when `SYNC_UPDATES_AVAILABLE` fires) minimises rate-limit pressure

---

## K. Sandbox

Plaid Sandbox provides:
- Fixed test institutions (`ins_109508` First Platypus Bank is the classic)
- UK sandbox institutions (representative set of CMA9)
- Custom user mode (`custom_user`) — seed specific transactions, balances, holdings for deterministic tests
- Sandbox test credentials: `user_good` / `pass_good`; `user_custom` + JSON fixture for custom mode
- Webhook firing helpers (`/sandbox/item/fire_webhook`, `/sandbox/item/reset_login`)

Use sandbox extensively to:
- Reproduce `ITEM_LOGIN_REQUIRED` for re-auth flow testing
- Test multi-currency investment accounts (ISA vs brokerage)
- Drive cursor-based sync end-to-end including `removed` transactions

Source:
- [Plaid Sandbox](https://plaid.com/docs/sandbox/)

---

## L. UK-Specific: Consent, FCA, Data Residency

**Consent model (2023+):**
- FCA replaced bank-led 90-day re-auth with **TPP-managed consent** in 2023
- Effective consent period: **180 days** (harmonised with EU EBA RTS)
- Plaid implements TPP-managed flow — re-auth is smoother than the old Yapily 90-day-forced-redirect model
- `PENDING_EXPIRATION` webhook ~7 days before expiry; we can surface in-app prompts well in advance

**CMA9 coverage:** Barclays, Lloyds, HSBC, Santander, NatWest (RBS), Nationwide, Bank of Ireland, Danske, AIB — all live.

**Non-CMA9 banks:** Monzo, Starling, Revolut, First Direct, Halifax, Metro, Virgin Money etc. — broad coverage, but verify each institution before launch via Plaid's coverage explorer.

**FCA authorisations:**
- AISP (Account Information Service Provider) — FRN 804718
- PISP (Payment Initiation Service Provider) — licensed

**Data residency:** **VERIFY with Plaid account manager.** Public docs don't clearly state UK/EU data residency; GDPR adequacy mechanisms for UK→US transfers may apply (SCCs). Important for Eyrie's compliance posture given ICO registration and the `Eyrie/legal-compliance/compliance-checklist.md`.

Sources:
- [Plaid blog on TPP-managed consent](https://plaid.com/blog/90-day-are-you-ready/)
- [180 days in EU](https://plaid.com/blog/eu-reauth-update/)

---

## M. Pricing Posture

Plaid does not publish exhaustive pricing publicly; key signals:
- **Per-Item** model for most products (one-off or monthly)
- **Transactions** is the highest-cost line — subscription per connected Item per month
- **Auth, Identity, Balance** are typically one-off per account verification
- **Payment Initiation** priced per initiated payment
- **Trial plan:** some published free production Items for new teams (exact figure varies)
- Startup programme historically exists — negotiate at signup

**Rule of thumb for budgeting:** Transactions + Auth + Identity for a UK personal-finance app is ballpark £3–£8 per user per month at small scale, negotiable downward materially at volume. This is **more expensive than Yapily's headline rate** but comes with richer enrichment and a larger coverage footprint. Update the `Eyrie/finance/unit-economics.md` cost model after the Plaid pricing call.

---

## N. Security & Compliance

- HMAC-SHA256 JWT webhook verification (see Section I)
- TLS 1.2+ required inbound and outbound
- Plaid is SOC 2 Type II certified, ISO 27001 certified
- FCA AISP + PISP (UK)
- GDPR data processor role; DPA available — request at contract stage
- Plaid's data handling: does NOT store user bank passwords (OAuth/App-to-App for all CMA9); older screen-scraping institutions are being migrated to OAuth under PSD2
- **Eyrie implication:** the existing per-user AES-256-GCM encryption of bank tokens (from `backend-assessment.md`) remains the right pattern for `access_token`; the encryption architecture migrates intact.

---

## O. iOS SDK Integration (Implementation Notes)

From [plaid-link-ios-spm](https://github.com/plaid/plaid-link-ios-spm):
- Swift Package: `https://github.com/plaid/plaid-link-ios-spm.git`
- `import LinkKit`
- `PLKPlaid.create(with:)` factory; handler invokes on success/exit/event
- Universal Link setup required for OAuth redirect from bank-hosted SCA
  - `apple-app-site-association` served by Eyrie backend at `/.well-known/apple-app-site-association`
  - `com.apple.developer.associated-domains` entitlement in iOS target
  - Plaid recommends `applinks:<your-domain>`; the redirect URI is registered in Plaid dashboard
- `completion` handler returns `LinkExit` or `LinkSuccess(publicToken, metadata)`

Source:
- [Plaid Link iOS](https://plaid.com/docs/link/ios/)

---

## P. Risks & Gotchas (Eyrie-specific)

1. **Plaid branding in Link flow** — "Powered by Plaid" is visible; less white-label than Yapily-hosted. Consider UX copy that positions this as a trust signal ("bank-grade connection via Plaid").
2. **UK Liabilities unavailable** — if we ever want credit-card APR visibility, mortgages, or student-loan balances for UK users, we can't get this from Plaid. Mitigation: statement upload pipeline covers this.
3. **UK Investments coverage is institution-dependent** — Plaid Investments *technically* supports UK, but Hargreaves Lansdown, AJ Bell, Vanguard UK etc. may or may not be live. This is exactly why the architecture needs both Plaid AND SnapTrade AND statement upload.
4. **Re-auth is better than Yapily but still user-facing** — 180-day TPP-managed consent is an improvement, but the user still sees a re-auth prompt every ~6 months. UX should prompt ~30 days before expiry, not at expiry.
5. **Cursor loss = full re-sync** — persistent, encrypted cursor storage is non-negotiable. Back it up. Version it. Monitor cursor age.
6. **Webhook reliability** — at-least-once, no strict SLA. Idempotent receiver + fallback poll once every 6h per Item minimises silent drift.
7. **Rate-limit planning** — 30/min/Item on `/transactions/sync` is plenty for normal use, but background historical backfill loops can exhaust this. Design bulk backfill to respect limits from day one.
8. **VRP uncertainty** — cVRP is in pilot. Don't architect a commitment to Plaid payments until cVRP proves out in production with real bank coverage. Keep payments abstracted.
9. **Trading platform coverage mismatch** — Plaid Investments + SnapTrade will have overlapping and non-overlapping broker support in UK and US. Fan-in merging needs deduplication: if both Plaid and SnapTrade return the same account (e.g. a US user with Robinhood connected both ways), we must pick one source-of-truth per account.
10. **Sandbox ≠ Production** — some CMA9 banks have production-only quirks (OAuth redirect loops, account-type subtypes not represented in sandbox). Plan 1–2 weeks of pre-launch production validation.

---

## Q. Open Questions for Claude Code Implementer

1. **Plaid UK investment coverage:** which UK brokers, SIPP providers, workplace pensions (Nest, L&G, Aviva, Standard Life) are actually live via Plaid in April 2026? Get a definitive list from the Plaid UK team before writing the `PlaidInvestmentsAdapter`.
2. **Confirm iOS minimum version** on current `plaid-link-ios-spm` main branch — align with Eyrie's iOS target.
3. **Confirm data residency** for UK user data (EU-based, US-based, with SCCs?). Document in `legal-compliance/compliance-checklist.md`.
4. **Get Plaid DPA** signed before first production UK Item.
5. **Pricing quote** in writing for projected 10k MAU UK + 5k MAU US, with per-product breakdown (Transactions, Auth, Identity, Investments, VRP).
6. **Sandbox UK investment accounts** — are these seedable, or do we need production-only validation?
7. **Webhook delivery SLO** — get Plaid's internal SLO for webhook latency and retry count, document for SRE alerting thresholds.
8. **VRP cVRP coverage rollout** — which banks are live in the Q1 2026 pilot, which timeline for full CMA9?
9. **Investments refresh** — is `/investments/refresh` rate-limited more strictly than `/transactions/sync`? Can we drive user-triggered refresh from the app?
10. **Dual-link risk** — for a user who has Plaid and SnapTrade connected to the same broker, what's Plaid's stance on our merge logic? Any contractual restrictions on deduplicating with a competitor's data?

---

## R. Canonical Mapping — Plaid Resource → Eyrie Canonical Type

| Plaid resource | Field examples | Eyrie canonical type | Notes |
|---|---|---|---|
| Item | item_id, institution_id, access_token | ProviderLink | Durable link to an institution-user pair |
| Institution | institution_id, name, country_codes, logo | Institution | Normalised per canonical schema |
| Account | account_id, type, subtype, mask, balances | Account | subtype maps to Eyrie's account taxonomy (current, savings, credit, isa, sipp, brokerage…) |
| Transaction | transaction_id, account_id, amount, iso_currency_code, date, personal_finance_category, merchant_name | Transaction | Sign convention: Plaid returns positive for outflows, Eyrie canonical is signed (negative = outflow) |
| Security | security_id, cusip, isin, sedol, ticker_symbol, type | Security (reference) | Upserted into reference store; referenced by Holding |
| Holding | account_id, security_id, quantity, institution_value, cost_basis | Holding | Joins Account ↔ Security |
| Investment transaction | investment_transaction_id, type, subtype, quantity, price, fees | InvestmentTransaction | Separate table from cash Transaction |
| Payment (PI) | payment_id, status, amount | PaymentIntent | Only used if Payments port is powered by Plaid |
| Webhook | webhook_type + webhook_code + item_id | SyncEvent | Drives CanonicalisationService |

---

*Research consolidated from Plaid docs, UK regulatory sources, and industry reporting. Re-verify pricing, institution coverage, and cVRP status at contract signing. Primary references linked inline.*
