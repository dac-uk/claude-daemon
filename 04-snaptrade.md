# 04 — SnapTrade Adapter

Full research: [`/outputs/research/snaptrade-research.md`](../../outputs/research/snaptrade-research.md). This file is the design for `SnapTradeAdapter`.

## Role

Primary provider for investment/wealth/pension data where broker has SnapTrade integration. Complements Plaid Investments (which is opportunistic in UK).

## Confirmed UK coverage (April 2026)
- **Live:** AJ Bell, Interactive Brokers UK, Trading 212.
- **Verify at contract:** Vanguard UK, Freetrade, Hargreaves Lansdown.

## Confirmed US coverage
Robinhood (read-only), Schwab, Fidelity, Vanguard, IBKR, E*TRADE, Coinbase, Kraken. Mature market.

## Not covered by SnapTrade
Workplace pensions (Nest, Aviva, L&G, Standard Life). → Statement/Manual channel.

## Lifecycle

### First-time link
1. Backend `POST /api/v1/snapTrade/registerUser` with `userId = user.id`.
2. Response returns system-generated `userSecret`. **Persist atomically, encrypted.** Loss is unrecoverable (must delete + re-onboard).
3. Backend `POST /api/v1/snapTrade/login` with `{userId, userSecret, connectionType: "read"}`.
4. Response returns short-lived `redirectURL` (~15 min validity).
5. iOS opens URL in **`SFSafariViewController`** — not WKWebView.
6. User authenticates with broker inside portal.
7. Portal redirects back to Eyrie via Universal Link → iOS closes Safari VC → notifies backend.
8. Backend `GET /api/v1/connections` to enumerate new `brokerageAuthorizations`.
9. For each auth: create `provider_link` row + fetch accounts + holdings.

### Re-auth
- SnapTrade does NOT proactively notify of expired connections.
- Detect on sync failure or periodic `Connections_detailBrokerageAuthorization` check.
- On expired status: call `loginSnapTradeUser` with `reconnect_id = authorization_id` → portal enters reconnect mode.

### Revocation
- User removes link → `DELETE /api/v1/connections/{id}` → cascade-hide canonical records.
- User deletion → `deleteSnapTradeUser(userId)` before Eyrie user purge. Irreversible.

## Data sync

### Accounts
- `GET /api/v1/accounts` for all accounts across all user's connections.
- Each returns `{id, brokerage, number, name, institution_name}`.

### Balances
- `GET /api/v1/accounts/{accountId}/balances` → `[{currency, cash}]` (multi-currency).

### Holdings
- `GET /api/v1/accounts/{accountId}/positions` → `[{symbol, units, price, currency, costBasis}]`.
- Daily cadence default. Real-time requires add-on.

### Activities (investment transactions)
- `GET /api/v1/activities?accountId=...&startDate=...&endDate=...` → normalised types.
- Initial pull: broker-dependent history (5–10 years).
- Incremental: from `last_sync_at - 3 days` (safety buffer for late-settling).

### Force refresh
- `POST /api/v1/connections/{id}/refresh` — user-triggered. Extra charge per call.
- Show "Syncing..." in UI; listen for `ACCOUNT_HOLDINGS_UPDATED` webhook for completion.

## Canonicalisation

### Account type mapping
SnapTrade's `type` field is broker-dependent and sometimes missing. Use heuristics:

| Signal | Canonical subtype | Tax wrapper |
|---|---|---|
| Account name contains "ISA" | isa or cash_isa | isa |
| Account name contains "SIPP" | sipp | sipp |
| Account name contains "401(k)" | us_401k | us_401k |
| Account name contains "IRA" | us_ira | us_ira |
| Account name contains "Roth" | us_roth_ira | us_roth |
| Brokerage slug = COINBASE/KRAKEN/BINANCE | crypto | null |
| Default | brokerage | gia |

**Surface to user** for confirmation on first ingestion. Store user override in `account.metadata.user_confirmed_tax_wrapper = true`.

### Activity type mapping
| SnapTrade type | Canonical type |
|---|---|
| BUY | BUY |
| SELL | SELL |
| DIVIDEND | DIV |
| INTEREST | INTEREST |
| CONTRIBUTION | CONTRIBUTION |
| WITHDRAWAL | WITHDRAWAL |
| TRANSFER | TRANSFER_IN or TRANSFER_OUT (sign-based) |
| FEE | FEE |
| TAX | TAX |
| REI | REINVEST |
| OPTIONEXPIRATION | OPTION_EXPIRATION |
| OPTIONASSIGNMENT | OPTION_ASSIGNMENT |
| OPTIONEXERCISE | OPTION_EXERCISE |
| SPLIT | SPLIT |
| (unmapped) | OTHER + log for review |

### Symbol mapping
- Yahoo convention: no suffix = NYSE/NASDAQ, `.TO` = TSX, `.L` = LSE (**verify** exact LSE convention).
- Symbols are not stable over time — store `symbol + fetched_at`.
- CUSIP/ISIN not directly returned. Resolve externally via ISIN lookup service if needed for long-term lookups.
- Upsert `security` by `(symbol, exchange_mic)` fallback when ISIN unavailable.

### Multi-currency
- Position has `position_currency` + security has `listing_currency`.
- A US stock held in a CAD-denominated account: `position_currency = CAD`, `listing_currency = USD`.
- Holding amounts in `position_currency`; displayed in user's display currency with daily FX.

## Webhooks

### Signature verification
HMAC-SHA256 of raw request body using **`clientSecret`** (not the deprecated webhook secret). Header: `Signature: {hex_hash}`. Constant-time compare.

### Events
| event | Action |
|---|---|
| USER_CONNECTION_RENEWED | Mark link `active`; enqueue full resync |
| ACCOUNT_HOLDINGS_UPDATED | Enqueue holdings sync |
| ACCOUNT_TRANSACTIONS_UPDATED | Enqueue activities sync |
| ACCOUNT_DELETED | Mark account `closed_at`, hide |
| CONNECTION_DELETED | Mark link `revoked`, cascade |

### Idempotency
Dedupe by `(userId, event, accountId?, timestamp)` in `webhook_log`.

## Errors

- Signature invalid → log; reject.
- 429 rate-limit (global 250/min) → exponential backoff.
- `connection.disabled = true` → mark `re_auth_required`; prompt user.
- IBKR multi-currency bug → warn user on IBKR-brokered accounts; note in metadata.

## Configuration

```swift
struct SnapTradeConfig {
    let clientId: String         // public
    let consumerKey: String      // secret
    let clientSecret: String     // for webhook verification
    let baseURL: URL
    let webhookUrl: URL
}
```

HMAC signing helper:
```swift
func signRequest(method: String, path: String, query: [String: String], body: Data, timestamp: Int) -> String {
    let payload: [String: Any] = [
        "content": String(data: body, encoding: .utf8) ?? "",
        "path": path,
        "query": query.sorted().map { "\($0.key)=\($0.value)" }.joined(separator: "&"),
        "timestamp": timestamp
    ].sorted()
    let json = try JSONSerialization.data(withJSONObject: payload, options: [.sortedKeys])
    let key = SymmetricKey(data: consumerKey.data(using: .utf8)!)
    let mac = HMAC<SHA256>.authenticationCode(for: json, using: key)
    return Data(mac).base64EncodedString()
}
```

## iOS

- No Swift SDK. All logic runs on Vapor backend.
- iOS uses `SFSafariViewController`, NOT WKWebView (OAuth + Passkeys break).
- Callback via Universal Link.

## Open items for Claude Code

- Verify HL, Vanguard UK, Freetrade coverage with SnapTrade sales.
- Obtain GDPR DPA.
- Confirm data residency (Canada, UK, US?) and execute UK transfer mechanism if needed.
- Confirm exact LSE symbol convention.
- Check if "tax lot" detail can be enabled (for more accurate capital gains).
- Request higher rate limit if projected volume exceeds 250 req/min.

---
*Source of truth: `/technical/financial-data-layer-brief.md` §5.2, §7.*
