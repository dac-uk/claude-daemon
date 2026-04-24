# SnapTrade Product & API Research Brief
**For Eyrie/Curo UK-first + US-ready Personal Finance iOS App**

**Research Date:** April 2026  
**Status:** Detailed implementation-grade findings

---

## Executive Summary

SnapTrade (by Passiv, Canadian fintech founded 2017, Series A funded at $7.45M) is a REST API for aggregating investment/wealth/pension account data and executing trades across 20+ brokerages globally. Core value proposition: unified OAuth-based broker connectivity, avoiding direct credential storage. 

**Verdict for Eyrie:** SnapTrade is production-ready for UK (AJ Bell live, others beta) and US (major brokers live: Robinhood, Schwab, Fidelity, Vanguard, Interactive Brokers, E*TRADE, Coinbase, Kraken). Pricing at $2/user/month (or $1.50 depending on tier) is accessible. No Swift SDK—you hand-roll REST or use Node/Python SDKs for the Vapor backend. WKWebView is **not recommended** for iOS connection flow; use SFSafariViewController instead.

---

## A. Onboarding Flow: registerSnapTradeUser → loginSnapTradeUser → Connection Portal

### Core Sequence

1. **Register User** ([`Authentication_registerSnapTradeUser`](https://docs.snaptrade.com/reference/Authentication/Authentication_registerSnapTradeUser))
   - Call with unique `userId` from Eyrie (email or internal ID).
   - Response: **system-generated `userSecret`** (random string, sensitive).
   - **Critical:** You must persist `(userId, userSecret)` in your backend immediately. If lost, only remedy is delete user and re-onboard.

2. **Login / Generate Connection Portal URL** ([`Authentication_loginSnapTradeUser`](https://docs.snaptrade.com/reference/Authentication/Authentication_loginSnapTradeUser))
   - Input: `userId` + `userSecret` (+ optional `connectionType="read"` or `"trade"`).
   - Response: **temporary `redirectURL`** (valid ~15 minutes; designed for immediate redirect).
   - This URL opens the Connection Portal in the user's browser/WebView.

3. **Connection Portal Flow**
   - Portal hosted by SnapTrade; handles OAuth redirects, username/password entry, MFA, credential validation.
   - User selects brokerage, authenticates, grants permissions.
   - Portal redirects back to `redirectUrl` after auth success or shows error.
   - No credentials are passed to Eyrie; SnapTrade retains OAuth token with broker.

### userSecret Management

- **Lifetime:** No automatic expiry documented. Persists until deleted.
- **Rotation:** [`Authentication_resetSnapTradeUserSecret`](https://docs.snaptrade.com/reference/Authentication/Authentication_resetSnapTradeUserSecret) endpoint available. **Warning:** If you call this and fail to persist the new secret, you lose access to the user's data. Must be implemented carefully (write-through atomicity).
- **Revocation:** No explicit revocation; deletion only via [`Authentication_deleteSnapTradeUser`](https://docs.snaptrade.com/reference/Authentication/Authentication_deleteSnapTradeUser), which wipes all connections and data.

### Backend Persistence Model

For Eyrie, per user:
```
eyrie_user → snaptrade_userId (index)
          → snaptrade_userSecret (encrypted in vault, e.g., AWS Secrets Manager, Vault)
          → snaptrade_connections[] (List all via Connections API, refresh on each session)
```

**Do NOT** store userSecret in plaintext or in logs. Treat as API key equivalent.

---

## B. Connection Portal: Embedded vs. Redirect, iOS WKWebView, Customization

### Portal Architecture & iOS Implementation

[Connection Portal Documentation](https://docs.snaptrade.com/docs/implement-connection-portal)

**Key Finding:** WKWebView is **not recommended**. OAuth and third-party credential flows (Google Sign-In, Passkeys) fail in WKWebView because it:
- Doesn't share cookies with Safari / system credential store
- Blocks third-party cookie delegation
- Fails Passkey API access

**Recommended iOS Flow:**
- Use **SFSafariViewController** (iOS built-in) OR
- Use **in-app browser SDK** (recommended by SnapTrade) for React Native / web.
- This preserves Safari cookie jar and OAuth state management.

**Why:** Brokerages often use OAuth2 and Passkey authentication; these require browser isolation and credential store access that WKWebView doesn't provide.

### Portal Customization

SnapTrade Portal UI is white-label capable; exact customization scope not fully detailed in search results. Recommended:
- Contact SnapTrade sales for branding options (logo, color scheme).
- Portal includes brokerage selection dropdown UI (pre-filtered by SnapTrade's coverage).
- Connection type selector: read-only vs. trade access.

### Portal Scope & Languages

- **Supported languages:** Likely English + others (not explicitly confirmed in docs; [request clarification from support](https://docs.snaptrade.com/docs/faq)).
- **Embedded mode:** Portal is designed for redirect flow (open in SFSafariViewController), not iframe embedding. No iframe support mentioned.
- **Redirect mode:** Post-auth redirect URL is configurable; you can route back to your app via deep link or web callback.

---

## C. Authentication Model: API Key, Signature Scheme, Clock Skew

### Request Signing

All server-to-server requests to SnapTrade API must be signed with HMAC-SHA256.

**Your API credentials (from SnapTrade dashboard):**
- `clientId` (public identifier)
- `consumerKey` (secret, rotate if compromised)

**Signing algorithm** ([Requests Documentation](https://docs.snaptrade.com/docs/requests)):
1. Build signature object: `{ content: JSON.stringify(body), path: request_path, query: query_params }`
2. Sort all keys alphabetically.
3. Stringify as JSON.
4. HMAC-SHA256 hash with `consumerKey` as key.
5. Add `Authorization: HMAC-SHA256 {signature}` header + `clientId` (via URL param or header).

**Clock Skew Tolerance:**
- **VERIFY:** Exact tolerance not documented in search results. Typical is ±5 minutes; assume that unless SnapTrade specifies otherwise. Test with `consumerKey` rotation / debug endpoint if signature fails.

### User-Level Authentication

After registering a user:
- All requests referencing that user must include `userId` + `userSecret` in request headers or path.
- **JWT option:** For client-side (web/mobile) direct API calls, SnapTrade can issue JWT tokens (encrypted per user). Format: `Authorization: JWT {token}`.
- **Backend pattern:** Vapor backend signs all requests; mobile app calls Vapor (never calls SnapTrade directly with userSecret exposed).

---

## D. Brokerage Authorizations: Multiple Connections, Reauth Flow

### Multiple Brokers Per User

[Connections API Documentation](https://docs.snaptrade.com/docs/connections)

- **One user can connect multiple brokers.** Example: Eyrie user connects AJ Bell + Vanguard US + Interactive Brokers.
- Each connection is 1:1 with a set of credentials at a brokerage.
- List all connections via [`Connections_listBrokerageAuthorizations`](https://docs.snaptrade.com/reference/Connections/Connections_listBrokerageAuthorizations).

### Credential Expiration & Reauth

- **OAuth tokens expire** at broker (duration varies, typically 90 days to 1 year).
- When expired, the connection status becomes **disabled**. SnapTrade detects this during sync/refresh.
- **To reauth:**
  - Detect disabled connection in your UI or via connection detail endpoint.
  - Generate a new Connection Portal URL with `reconnect_id` parameter (not create new).
  - Portal enters **reconnect mode**: shows broker selection with the expired connection pre-selected, prompts user to re-enter credentials.
  - Post-reauth, SnapTrade updates the token and re-enables the connection.

### De-duplication

- If a user tries to connect the same broker with the same credentials, SnapTrade returns the existing connection instead of creating a duplicate.

### Connection Management Endpoints

- [`Connections_detailBrokerageAuthorization`](https://docs.snaptrade.com/reference/Connections/Connections_detailBrokerageAuthorization) — get status, accounts, sync metadata.
- [`Connections_removeBrokerageAuthorization`](https://docs.snaptrade.com/reference/Connections/Connections_removeBrokerageAuthorization) — delete connection (triggers cascade: removes all accounts, holdings, transactions).
- [`Connections_refreshBrokerageAuthorization`](https://docs.snaptrade.com/reference/Connections/Connections_refreshBrokerageAuthorization) — **manually trigger holdings/activity sync**. Incurs additional charge per call.

---

## E. Accounts, Balances, Positions, Holdings: Shape & Refresh Cadence

### Data Model (Flat vs. Nested)

[Account Information Endpoints](https://docs.snaptrade.com/reference/Account%20Information)

**Structure returned by API:**
```
Account {
  id: UUID,
  brokerage: string (e.g., "ROBINHOOD", "IBKR", "SCHWAB"),
  number: string (account number at broker),
  name: string (e.g., "Jane Doe TFSA")
}

Balance {
  currency: string (ISO-4217, e.g., "USD", "CAD", "GBP"),
  cash: number
}

Position {
  symbol: string (ticker with exchange suffix, e.g., "AAPL", "VAB.TO"),
  units: number,
  price: number,
  currency: string (position currency, may differ from symbol listing),
  costBasis: number (average cost per share)
}
```

**Nesting:** Flat within a single account; you call separate endpoints for accounts, balances, positions, orders.

### Refresh Cadence

**Holdings & Balances:**
- **Default cadence:** Once per day.
- **Real-time access:** If your API key has real-time pricing enabled, endpoint returns live data; otherwise cached (staleness varies by broker, typically <24h).
- **Manual refresh:** [`Connections_refreshBrokerageAuthorization`](https://docs.snaptrade.com/reference/Connections/Connections_refreshBrokerageAuthorization) triggers async update. **Costs extra** (exact pricing on your dashboard).

**Transactions/Activities:**
- **Initial sync:** SnapTrade pulls all available history from broker (varies: 5–10 years depending on broker).
- **Incremental:** Daily sync for new transactions post-initial load.
- **Manual refresh:** Same endpoint triggers past-24h transaction sync if not yet run today.

### Cost Efficiency

- **Do not poll excessively.** SnapTrade recommends: max 4x/day per user background sync, or once per end-user login.
- Each manual refresh incurs a charge (variable by plan; check your customer dashboard).

---

## F. Transactions/Activities: Types, Cost Basis, Symbol Mapping, Multi-Currency

### Activity Types (Transaction Categories)

[Activities/Transactions Endpoint](https://docs.snaptrade.com/reference/Transactions%20And%20Reporting/TransactionsAndReporting_getActivities)

SnapTrade normalizes broker-specific transaction types to a common set:

| Type | Meaning |
|------|---------|
| **BUY** | Equity purchase |
| **SELL** | Equity sale |
| **DIVIDEND** | Cash dividend distribution |
| **INTEREST** | Interest deposited |
| **CONTRIBUTION** | Cash contribution into account |
| **WITHDRAWAL** | Cash withdrawal |
| **TRANSFER** | Asset transfer in/out of account |
| **FEE** | Account or trading fee |
| **TAX** | Tax-related fee |
| **REI** | Dividend reinvestment |
| **OPTIONEXPIRATION** | Option expired (settlement) |
| **OPTIONASSIGNMENT** | Option assignment (forced exercise) |
| **OPTIONEXERCISE** | Option exercised by holder |
| **SPLIT** | Stock split event |

**Note:** "SnapTrade does a best effort to categorize;" some brokers have edge cases. Recommend logging unmapped types and escalating to SnapTrade.

### Cost Basis & Lot Information

- **Per-share cost basis:** Included in Position object.
- **Tax lot details:** Disabled by default; available if you contact SnapTrade support to enable. Returns granular lot-level cost basis for tax reporting.
- **Multi-currency handling:** Each position has `position_currency` (currency held at broker) and symbol's `listing_currency` (e.g., USD stock held in CAD account = separate currencies).

### Symbol Mapping: Ticker, Raw Symbol, CUSIP/ISIN

[Reference Data Endpoints](https://docs.snaptrade.com/reference/Reference%20Data)

- **Primary format:** Yahoo Finance ticker convention.
  - NYSE/NASDAQ: no suffix (e.g., "AAPL").
  - TSX (Toronto): ".TO" suffix (e.g., "VAB.TO").
  - LSE (UK): variations exist; [verify exact format for Vanguard UK, AJ Bell holdings](https://docs.snaptrade.com/).
  - CME, ICE, other exchanges: exchange-specific suffixes.

- **Raw symbol:** Ticker stripped of exchange suffix.

- **CUSIP/ISIN:** Not directly returned by SnapTrade positions API. SnapTrade uses ticker as primary ID.
  - **Gotcha:** Symbols are **not stable over time.** Company mergers, spin-offs, ticker changes can invalidate symbol mappings.
  - **Recommendation for Eyrie:** Store symbol + original fetch date; if you need cost basis reconstruction years later, may need to re-query or use external CUSIP/ISIN mapping service for historical lookups.

### Multi-Currency & UK Account Types

- **Multi-currency support:** SnapTrade returns position_currency + listing_currency; handles GBP/USD/EUR/CAD mixtures.
- **ISA/GIA detection:** **VERIFY** — Search results suggest SnapTrade returns account type info, but explicit ISA/GIA/Stocks & Shares vs. Lifetime ISA categorization is not detailed. Confirm with SnapTrade support if you need to tag Eyrie accounts as ISA for tax logic.

---

## G. Reference Data: Brokerages, Asset Classes, Currencies, Exchanges

[Reference Data Endpoints](https://docs.snaptrade.com/reference/Reference%20Data)

### Brokerage List

Returned by [`ReferenceData_getPartnerInfo`](https://docs.snaptrade.com/reference/Reference%20Data/ReferenceData_getPartnerInfo):

```json
{
  "id": "f1234567-89ab-cdef-0123-456789abcdef",
  "name": "Robinhood",
  "slug": "ROBINHOOD",
  "description": "Online brokerage platform",
  "logoUrl": "https://passiv-brokerage-logos.s3.ca-central-1.amazonaws.com/robinhood-logo.png",
  "logoSquareUrl": "https://passiv-brokerage-logos.s3.ca-central-1.amazonaws.com/robinhood-logo-square.png"
}
```

**Use case for Eyrie:** Populate institution picker UI with logos and names.

### Currencies

ISO-4217 codes (e.g., USD, GBP, CAD, EUR, AUD).

### Exchanges

[`ReferenceData_getStockExchanges`](https://docs.snaptrade.com/reference/Reference%20Data/ReferenceData_getStockExchanges):

```json
{
  "id": "TSX",
  "name": "Toronto Stock Exchange",
  "code": "XTSE",
  "mic": "XTSE",
  "suffix": ".TO"
}
```

Includes Market Identifier Code (MIC) and trading suffix for symbol construction.

---

## H. Webhooks: Event Types, Signature Verification, Delivery Semantics

[Webhooks Documentation](https://docs.snaptrade.com/docs/webhooks)

### Event Types (Documented)

- **USER_CONNECTION_RENEWED** — Connection credentials re-authenticated (after expiry reauth).
- **ACCOUNT_TRANSACTIONS_UPDATED** — New/updated transactions synced. Sent after daily sync.
- **ACCOUNT_HOLDINGS_UPDATED** — Holdings refreshed. Sent after daily sync or manual refresh.
- **ACCOUNT_DELETED** — Account removed from connection.
- **CONNECTION_DELETED** — Entire connection removed by user or admin.

### Signature Verification (HMAC-SHA256)

**Critical distinction:** Use **client secret** (from dashboard), NOT the deprecated webhook secret.

**Signature header:** `Signature: {HMAC-SHA256 hash}`

**Verification:**
1. Extract request body (raw bytes).
2. HMAC-SHA256 hash with your `clientSecret`.
3. Compare to `Signature` header (constant-time compare to avoid timing attacks).

**Payload example:**
```json
{
  "userId": "user-123",
  "event": "ACCOUNT_HOLDINGS_UPDATED",
  "accountId": "acct-456",
  "timestamp": "2026-04-22T10:30:00Z"
}
```

### Delivery Semantics

- **At-least-once:** SnapTrade retries failed webhooks; you should be idempotent (store event ID, check for duplicates).
- **Async:** Webhooks fire after internal processing; may lag 5–30 minutes behind actual broker sync.
- **No delivery SLA:** VERIFY exact retry policy and max retry duration with SnapTrade support.

---

## I. Trading & Options: Scope Only

[Trading Documentation](https://docs.snaptrade.com/docs/trading-with-snaptrade)

**Eyrie current scope:** Read-only (holdings, transactions). Below is reference for future expansion.

### Trading Endpoints Exist

- **Place order:** `POST /accounts/{accountId}/orders` — stocks, ETFs, options, crypto.
- **Get order impact:** Pre-flight check (margin, buying power impact).
- **Workflows vary by broker:**
  - Stocks/ETFs: supported widely.
  - Multi-leg options: supported selectively (Schwab, Interactive Brokers).
  - Crypto: Coinbase, Kraken, Binance only.
  - Extended hours: broker-specific (not all).

### Options Endpoints

- [`Options_listOptionHoldings`](https://docs.snaptrade.com/reference/Options/Options_listOptionHoldings) — current option positions, cost basis per contract.
- Separate from stock positions endpoint (not nested).

**Note for Eyrie:** If you add trading later, review SnapTrade Brokerage Support Matrix to confirm feature support per broker.

---

## J. Rate Limits: Per-User, Per-Account, Global

[Rate Limiting Documentation](https://docs.snaptrade.com/docs/ratelimiting)

### Global Rate Limit

**Default:** 250 requests/minute per API key (across all users, all endpoints).

**Rolling window:** Trailing 60 seconds.

**Response headers:**
- `X-RateLimit-Limit: 250`
- `X-RateLimit-Remaining: 199` (requests left in current window)
- `X-RateLimit-Reset: 30` (seconds until window resets)

### Mitigation

- **Throttle aggressive syncs.** Batching all users' holdings calls at once can spike requests and trigger limit.
- **Request higher limit:** Contact your Customer Success Manager if you need >250 req/min (custom plan).

### Per-Endpoint Limits?

**Not documented.** Assume 250 req/min is a global cap; not differentiated per endpoint.

---

## K. Pricing Model: Per-User Monthly, Per-Sync Fee, Free Tier

[SnapTrade Pricing](https://snaptrade.com/pricing)

### Current Pricing (2026)

| Tier | Per Connected User/Month | Limits | Notes |
|------|-------------------------|--------|-------|
| **Pay-as-you-go** | $2.00 | Unlimited API calls, real-time data optional | Typical for Eyrie scale |
| **Alternative quote** | $1.50 | (May vary by plan negotiation) | — |
| **Custom Plan** | Custom (min $500/month) | Volume discounts, higher rate limits, dedicated Slack | For enterprise |

**Additional costs:**
- **Manual refresh endpoint calls** — Each [`Connections_refreshBrokerageAuthorization`](https://docs.snaptrade.com/reference/Connections/Connections_refreshBrokerageAuthorization) incurs an extra charge (pricing varies; check dashboard).
- **Real-time data add-on** — If you want sub-minute holdings updates (vs. daily cached). Check your dashboard.

**Free tier:** Not documented; assume no free tier for production.

**Connected user definition:** User with ≥1 active brokerage connection. Multiple accounts at one broker = 1 connected user.

---

## L. UK Coverage Reality: Live vs. Beta vs. Roadmap

[SnapTrade Brokerage Integrations](https://snaptrade.com/brokerage-integrations)

### Confirmed Live (UK)

| Broker | Status | Data Only? | Trading? | Notes |
|--------|--------|-----------|----------|-------|
| **AJ Bell** | LIVE | ✓ | ✓ | Fully supported |
| **Interactive Brokers (IBKR)** | LIVE | ✓ | ✓ | Global, UK arm supported |
| **Trading 212** | LIVE | ✓ | ✓ | Live & practice accounts |

### **VERIFY** (Not explicitly confirmed in search, assume beta or roadmap)

| Broker | Likely Status | Notes |
|--------|---------------|-------|
| **Vanguard UK** | Beta or partial | Large asset manager; often slower to integrate APIs |
| **Hargreaves Lansdown** | **VERIFY** | —— |
| **Freetrade** | **VERIFY** | Public API mentioned but integration status unclear |

**Recommendation:** Contact SnapTrade sales to confirm Hargreaves Lansdown, Vanguard UK, and Freetrade status before product launch. If any critical brokers are beta, plan fallback to manual statement upload for those users.

---

## M. US Coverage Reality: Live vs. Beta

[SnapTrade Brokerage Integrations](https://snaptrade.com/brokerage-integrations)

### Confirmed Live (US)

| Broker | Status | Data Only? | Trading? |
|--------|--------|-----------|----------|
| **Robinhood** | LIVE | ✓ | No trading via API (read-only) |
| **Charles Schwab (SCHW)** | LIVE | ✓ | ✓ |
| **Fidelity (US)** | LIVE | ✓ | ✓ |
| **Vanguard (US)** | LIVE | ✓ | No trading (read-only) |
| **Interactive Brokers (US)** | LIVE | ✓ | ✓ |
| **E*TRADE** | LIVE | ✓ | ✓ |
| **Coinbase** | LIVE | ✓ | ✓ (crypto) |
| **Kraken** | LIVE | ✓ | ✓ (crypto) |

**Coverage:** 20+ brokerages globally; US is mature market with strong support.

---

## N. Crypto: Relevance for Eyrie

[Crypto Trading Documentation](https://docs.snaptrade.com/docs/crypto-trading)

**Exchanges supported:**
- Coinbase
- Kraken
- Binance

**For Eyrie (personal finance, not trading app):**
- Include crypto holdings in portfolio view (Coinbase/Kraken users).
- Do **not** push trading yet; read-only data aggregation is low-risk first step.
- If users request trading, plan trading endpoints separately (requires UX for crypto volatility, custody risks).

**Cost basis:** Supported for crypto trades (purchase price, quantity, fees).

---

## O. Data Freshness: Holdings, Transactions, Overnight Reliability, Force-Refresh

### Holdings Data Freshness

- **Default:** Cached, refreshed once daily (24h cadence).
- **Timing:** Usually overnight (exact timing varies by broker). Not guaranteed to complete by specific hour.
- **Real-time option:** If your API key has real-time pricing enabled, endpoint returns live prices (no caching).
- **Staleness worst-case:** Up to 24h if you fetch data just after user login.

**For Eyrie:** Plan to show "data last synced at {timestamp}" to manage user expectations. Don't rely on overnight sync completing by morning.

### Transactions Freshness

- **Initial:** SnapTrade syncs all available history from broker (5–10+ years depending on broker).
- **Incremental:** Daily sync for new transactions (checked daily).
- **Latency:** Activities API may show transactions 1–3 days after trade settlement (broker-dependent).

### Force Refresh

[`Connections_refreshBrokerageAuthorization`](https://docs.snaptrade.com/reference/Connections/Connections_refreshBrokerageAuthorization)

- **Effect:** Triggers async re-sync of holdings + activities for all accounts in that connection.
- **Cost:** Extra charge per call (exact amount on your dashboard).
- **UX:** Show "Syncing..." to user, listen to webhook (`ACCOUNT_HOLDINGS_UPDATED`) to confirm completion.

### Reliability

- **Overnight sync:** **Not guaranteed** to complete. If broker API is down or rate-limited, sync may retry later or fail silently.
- **Fallback:** If you don't see updated data after 24h, manually trigger refresh or contact support.

---

## P. Gotchas: Documented & Community-Reported Issues

### Known Issues

1. **Interactive Brokers (IBKR) Multi-Currency Accounts:**
   - Only holdings in family currency (e.g., HKD) are returned; other currencies hidden.
   - Total account value may be incorrect.
   - **Mitigation:** Warn IBKR users; test heavily before shipping.

2. **Symbol Instability:**
   - Symbols (tickers) are not immutable. Company mergers, bankruptcies, or delistings can reassign tickers.
   - If you store symbol for historical cost basis lookup years later, verify it's still valid.
   - **Recommendation:** Store symbol + original fetch date; use SnapTrade reference data or external CUSIP/ISIN resolver for long-term lookups.

3. **WKWebView OAuth Failure:**
   - Connection Portal fails in WKWebView (Passkeys, third-party OAuth don't work).
   - **Fix:** Use SFSafariViewController on iOS.

4. **userSecret Loss:**
   - If you lose the persisted userSecret, only remedy is delete user + re-onboard.
   - **Mitigation:** Encrypt userSecret in vault (AWS Secrets Manager, HashiCorp Vault). Never log it.

5. **Connection Reauth Lag:**
   - When a connection is disabled (credentials expired), SnapTrade doesn't proactively notify you.
   - Disabled status is detected when you call detail endpoint or try to sync.
   - **UX impact:** Users may see stale data until they re-authenticate. Monitor connection detail in background.

6. **Signature Configuration:**
   - Most frequent support issue: invalid consumer key or incorrect signature algorithm.
   - **Solution:** Use SDK or test with SnapTrade debug endpoint. Ensure consumerKey is exact (no whitespace).

### Community Feedback

- Mixed reviews on G2; some report good support responsiveness, others note outages or feature gaps.
- Passiv's history (personal robo-advisor) is good signal for API reliability; product is not new.
- **Status:** SnapTrade is actively maintained (regular SDK updates, ongoing integrations).

---

## Q. Regulatory Posture: Licensing, GDPR, Data Residency

[SnapTrade Company Background](https://snaptrade.com)

### Company & Licensing

- **Parent:** Passiv Inc. (Canada).
- **Licensing:** SnapTrade Inc. (API business) is **not** a licensed broker or custodian. It acts as a data aggregator / connectivity layer.
  - Does NOT hold user funds or credentials directly.
  - Stores OAuth tokens issued by brokers; brokers maintain primary credential security.
- **Prior regulatory discussion:** Passiv founders engaged with Canadian regulators (FCNB, CSA) to clarify product positioning. No major regulatory blocks to API operations.

### GDPR & Data Residency

**VERIFY:** Exact GDPR compliance posture, data residency (EU vs. US), and DPA terms not found in search results.

**Recommended next steps:**
1. Request GDPR Data Processing Agreement (DPA) from SnapTrade.
2. Confirm data residency: where are UK user records stored (EU, Canada, US)?
3. Clarify data retention after user deletion (right to be forgotten).

**For Eyrie UK launch:** Ensure SnapTrade's DPA is signed before collecting UK user data. If SnapTrade stores data in US without adequacy framework, you may need UK transfer mechanism (SCCs or UK Standard Contractual Clauses).

---

## R. SDKs: iOS Swift, Node/Python for Vapor Backend

[SnapTrade SDKs Repository](https://github.com/passiv/snaptrade-sdks)

### Available SDKs

| Language | Status | Notes |
|----------|--------|-------|
| **Python** | ✓ LIVE | [snaptrade-python-sdk on PyPI](https://pypi.org/project/snaptrade-python-sdk/) |
| **Node.js / TypeScript** | ✓ LIVE | [snaptrade-typescript-sdk on npm](https://www.npmjs.com/package/snaptrade-typescript-sdk) |
| **Java** | ✓ LIVE | — |
| **Ruby** | ✓ LIVE | — |
| **PHP** | ✓ LIVE | — |
| **Go** | ✓ LIVE | — |
| **C#** | ✓ LIVE | — |
| **Swift / iOS** | ✗ NOT AVAILABLE | No official Swift SDK |

### Recommendation for Eyrie

**Backend (Vapor/Swift):**
- **Option A (Recommended):** Use hand-rolled REST client + HMAC signing. Vapor has built-in URLSession; minimal boilerplate to sign requests.
- **Option B:** Wrap the Node.js SDK (if you run Node alongside Vapor for SnapTrade integration). Adds operational complexity.
- **Snippet for Vapor signing:**
  ```swift
  import Crypto
  
  func signRequest(
    method: String,
    path: String,
    body: String?,
    consumerKey: String
  ) -> String {
    let content = body ?? ""
    let message = "\(content)\(path)"
    let signature = HMAC<SHA256>.authenticationCode(
      for: Data(message.utf8),
      using: SymmetricKey(data: Data(consumerKey.utf8))
    )
    return Data(signature).base64EncodedString()
  }
  ```

**Mobile (iOS):**
- Call your Vapor backend (never call SnapTrade directly with userSecret).
- Vapor backend holds all SnapTrade credentials (clientId, consumerKey, userSecret for each Eyrie user).
- Mobile app is credential-free; calls authenticated via your own session/JWT.

---

## S. Open Questions for Implementation

1. **Exact GDPR/data residency posture?** Where are SnapTrade's data centers (EU/CA/US)? Do they have a signed DPA ready for UK deployment?

2. **Hargreaves Lansdown, Vanguard UK, Freetrade integration status?** Roadmap vs. beta vs. live? Critical for UK-first product launch.

3. **userSecret rotation strategy:** If a user suspects compromise, what's the safe rollover procedure? Can you rotate atomically (old + new valid concurrently for X seconds)?

4. **Webhook delivery SLA & retry policy:** How many retries, over what duration? What's your idempotency key strategy?

5. **Cost basis on UI:** Does SnapTrade return cost basis for all asset types (stocks, ETFs, mutual funds, bonds)? Any blind spots?

6. **ISA/GIA account type tagging:** How does SnapTrade represent UK ISA vs. GIA vs. Stocks & Shares vs. Lifetime ISA distinctions? Does it surface this, or must Eyrie infer from account name?

7. **Symbol history mapping:** If you need to rebuild cost basis years later after a ticker change, what's the recommended recovery flow (use SnapTrade reference data, external CUSIP resolver, manual entry)?

8. **Multi-leg options workflows:** If Eyrie later adds trading, which US brokers support spreads, straddles, etc.? Reference the brokerage support matrix.

9. **Crypto holdings cost basis:** For Coinbase/Kraken crypto holdings in Eyrie, does SnapTrade surface acquisition cost and fees for tax reporting? Any limitations?

10. **Real-time pricing add-on cost:** What's the additional monthly cost to enable real-time holdings updates vs. daily cached? When does it make sense to upsell?

---

## Implementation Mapping: SnapTrade → Eyrie Canonical Types

### Data Model Bridge

Below is how to map SnapTrade API responses to Eyrie's internal data schema:

| SnapTrade Entity | SnapTrade Type | Eyrie Canonical Type | Notes |
|------------------|----------------|----------------------|-------|
| User | userId + userSecret | Account.provider_user_id | Store in encrypted vault; link to Eyrie user |
| Connection | BrokerageAuthorization | Institution | Represents one broker link; contains account list |
| Brokerage | Partner (from ref data) | InstitutionMetadata | Logo, name, slug for UI picker |
| Account | Account | Account.sub_account | e.g., "Jane TFSA", "John Checking"; brokerage + number |
| Balance | Balance (currency + cash) | Account.cash_balance | Per account, per currency (multi-currency possible) |
| Position | Position | Holding | ticker, units, price, cost_basis; linked to Account |
| Activity | Activity | Transaction | type, symbol, units, price, date, currency |
| Webhook Event | (e.g., ACCOUNT_HOLDINGS_UPDATED) | Event (internal queue) | Trigger sync, cache invalidation, push notification |

### Schema Example (Eyrie Pseudo-SQL)

```sql
-- User ↔ SnapTrade mapping
CREATE TABLE user_snaptrade_credentials (
  id UUID PRIMARY KEY,
  eyrie_user_id UUID NOT NULL,
  snaptrade_user_id VARCHAR NOT NULL,
  snaptrade_user_secret VARCHAR NOT NULL ENCRYPTED,  -- AES-256 in vault
  created_at TIMESTAMP,
  updated_at TIMESTAMP,
  UNIQUE(eyrie_user_id)
);

-- Brokerage connections
CREATE TABLE user_brokerage_connections (
  id UUID PRIMARY KEY,
  eyrie_user_id UUID NOT NULL,
  snaptrade_connection_id UUID NOT NULL,
  brokerage_slug VARCHAR,  -- "ROBINHOOD", "IBKR", etc.
  brokerage_name VARCHAR,
  status VARCHAR,  -- "active", "disabled", "pending_reauth"
  synced_at TIMESTAMP,
  webhook_received_at TIMESTAMP,
  FOREIGN KEY(eyrie_user_id) REFERENCES users(id)
);

-- Accounts (multi-account per connection possible)
CREATE TABLE user_accounts (
  id UUID PRIMARY KEY,
  eyrie_user_id UUID NOT NULL,
  snaptrade_account_id VARCHAR NOT NULL,
  snaptrade_connection_id UUID NOT NULL,
  account_type VARCHAR,  -- "TFSA", "RRSP", "GIA", "ISA" (inferred or from broker)
  account_name VARCHAR,
  account_number VARCHAR,
  currency_primary VARCHAR,
  total_value DECIMAL(15,2),
  synced_at TIMESTAMP,
  FOREIGN KEY(eyrie_user_id) REFERENCES users(id)
);

-- Holdings
CREATE TABLE user_holdings (
  id UUID PRIMARY KEY,
  eyrie_user_id UUID NOT NULL,
  snaptrade_account_id VARCHAR NOT NULL,
  symbol VARCHAR,
  symbol_type VARCHAR,  -- "cs" (common stock), "et" (ETF), "bnd" (bond), etc.
  units DECIMAL(15,6),
  price DECIMAL(15,2),
  price_currency VARCHAR,
  cost_basis_per_unit DECIMAL(15,4),
  market_value DECIMAL(15,2),
  synced_at TIMESTAMP,
  FOREIGN KEY(eyrie_user_id) REFERENCES users(id)
);

-- Transactions
CREATE TABLE user_transactions (
  id UUID PRIMARY KEY,
  eyrie_user_id UUID NOT NULL,
  snaptrade_account_id VARCHAR NOT NULL,
  snaptrade_activity_id VARCHAR,
  type VARCHAR,  -- "BUY", "SELL", "DIV", "CONTRIBUTION", "FEE", "TRANSFER", etc.
  symbol VARCHAR,
  units DECIMAL(15,6),
  price_per_unit DECIMAL(15,4),
  currency VARCHAR,
  fee DECIMAL(15,2),
  transaction_date DATE,
  settlement_date DATE,
  description VARCHAR,
  synced_at TIMESTAMP,
  FOREIGN KEY(eyrie_user_id) REFERENCES users(id)
);
```

---

## Implementation Checklist

**Pre-Launch (Week 1–2):**
- [ ] Request signed DPA from SnapTrade (GDPR compliance).
- [ ] Verify UK broker coverage (Hargreaves Lansdown, Vanguard UK, Freetrade status).
- [ ] Generate API credentials (clientId, consumerKey) from SnapTrade dashboard.
- [ ] Implement HMAC-SHA256 signing in Vapor backend.
- [ ] Test user registration → portal flow with SFSafariViewController on real iOS device.

**MVP Build (Week 3–6):**
- [ ] Implement `registerSnapTradeUser` + `loginSnapTradeUser` endpoints.
- [ ] Build Connection Portal UI (SFSafariViewController + redirect handling).
- [ ] Implement account list, holdings, balance sync (call `listUserAccounts`, `getUserHoldings`, `getUserAccountBalance`).
- [ ] Build activity/transaction sync with pagination.
- [ ] Set up webhook receiver (signature verification + idempotency).
- [ ] Add manual refresh UX (trigger `Connections_refreshBrokerageAuthorization`, show syncing state).

**Testing (Week 6–8):**
- [ ] Test with sandbox accounts from 3+ brokers (AJ Bell, Interactive Brokers, Coinbase).
- [ ] Test multi-currency portfolios (GBP/USD/EUR).
- [ ] Test symbol mapping (verify ticker suffixes, validate ISIN lookups).
- [ ] Test connection reauth (expire a token, verify UX flow).
- [ ] Load test: simultaneous user registrations, 250 req/min limit behavior.
- [ ] Data freshness: confirm overnight sync completion, measure latency.

**Launch (Week 9+):**
- [ ] Monitor webhook delivery (set up alerting for failed webhooks).
- [ ] Track sync failures per broker; escalate to SnapTrade.
- [ ] Gather user feedback on connection portal UX, missing brokers.
- [ ] Plan Phase 2: real-time data add-on, crypto support, tax reporting (cost basis UI).

---

## Summary Table: Source Documentation

| Topic | Primary Source | Alternative |
|-------|---|---|
| Onboarding | [Getting Started](https://docs.snaptrade.com/) | [Demo](https://docs.snaptrade.com/demo/getting-started) |
| Authentication | [Requests Docs](https://docs.snaptrade.com/docs/requests) | [API Ref](https://docs.snaptrade.com/reference/Authentication) |
| Connections | [Connections Guide](https://docs.snaptrade.com/docs/connections) | [Fix Broken Connections](https://docs.snaptrade.com/docs/fix-broken-connections) |
| Holdings/Transactions | [Account Data Docs](https://docs.snaptrade.com/docs/account-data) | [API Endpoints](https://docs.snaptrade.com/reference/Account%20Information) |
| Webhooks | [Webhooks Docs](https://docs.snaptrade.com/docs/webhooks) | [Signature Verification](https://docs.snaptrade.com/docs/webhooks) |
| Trading | [Trading Guide](https://docs.snaptrade.com/docs/trading-with-snaptrade) | [API Endpoints](https://docs.snaptrade.com/reference/Trading) |
| Data Refresh | [Account Data Docs](https://docs.snaptrade.com/docs/account-data) | [Connections Manual Refresh](https://docs.snaptrade.com/reference/Connections) |
| Rate Limits | [Rate Limiting Docs](https://docs.snaptrade.com/docs/ratelimiting) | Dashboard analytics |
| Pricing | [SnapTrade Pricing](https://snaptrade.com/pricing) | Sales consultation |
| Brokerages | [Integrations Page](https://snaptrade.com/brokerage-integrations) | [Notion Matrix](https://snaptrade.notion.site/SnapTrade-Brokerage-Integrations-f83946a714a84c3caf599f6a945f0ead) |
| FAQ | [FAQ Docs](https://docs.snaptrade.com/docs/faq) | Help center |

---

**End of Research Brief**

*Research compiled April 2026. Recommend re-verification of pricing, brokerage coverage, and GDPR terms 60 days before Eyrie UK launch. For questions, contact SnapTrade support at api@snaptrade.com.*
