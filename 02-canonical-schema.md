# 02 — Canonical Schema

The canonical schema is the single source of truth. Every adapter maps its provider types into these entities. The iOS app, the Financial Decision Engine, Tribe, Ask Eyrie, and the analytics layer read canonical types only.

## Conventions

- **Money in minor units** (pence/cents) as `BIGINT`. No floats, ever.
- **Signed transaction amounts**: negative = outflow from the account. Plaid uses positive-for-outflow — the `PlaidAdapter` normalises.
- **`iso_currency_code`** on every monetary field that can vary per-record.
- **`source_provider`** + **`source_record_id`** on every canonical row for provenance.
- **UUIDv7** for all IDs (time-sortable, DB-friendly).
- **Soft-delete via `closed_at` / `is_hidden`** for provider-driven removals. Hard delete only on explicit user erasure.

## Entities

### `institution`
Shared registry of banks, brokers and pension providers.

| Column | Type | Notes |
|---|---|---|
| id | UUID | PK |
| display_name | TEXT | |
| country_code | CHAR(2) | ISO-3166-1 alpha-2 |
| logo_url | TEXT | nullable |
| website_url | TEXT | nullable |
| primary_colour | TEXT | hex, nullable |
| plaid_institution_id | TEXT | nullable, unique when set |
| snaptrade_slug | TEXT | nullable, unique when set |
| manual_only | BOOL | default false |
| created_at | TIMESTAMPTZ | |
| updated_at | TIMESTAMPTZ | |

Pre-seed ~200 UK institutions at launch.

### `provider_link`
One authenticated connection between a user and a provider.

| Column | Type | Notes |
|---|---|---|
| id | UUID | PK |
| user_id | UUID | FK users(id) ON DELETE CASCADE |
| provider | TEXT | ENUM: 'plaid' \| 'snaptrade' \| 'manual' \| 'statement' \| 'yapily' |
| provider_user_id | TEXT | e.g. Plaid `item_id`, SnapTrade `userId` |
| provider_credentials | BYTEA | AES-256-GCM encrypted payload |
| status | TEXT | ENUM: 'active' \| 'expiring' \| 're_auth_required' \| 'revoked' \| 'error' |
| consent_expires_at | TIMESTAMPTZ | nullable |
| last_sync_at | TIMESTAMPTZ | nullable |
| cursor | TEXT | Plaid sync cursor |
| metadata | JSONB | provider-specific extras |
| created_at | TIMESTAMPTZ | |
| updated_at | TIMESTAMPTZ | |

Indexes: `(user_id, provider, status)`, `(provider, provider_user_id)` UNIQUE.

### `account`
A canonical account.

| Column | Type | Notes |
|---|---|---|
| id | UUID | PK |
| user_id | UUID | FK |
| institution_id | UUID | FK |
| provider_link_id | UUID | FK, nullable (purely manual accounts) |
| source_provider | TEXT | |
| source_account_id | TEXT | |
| display_name | TEXT | user-editable |
| mask | TEXT | last-4 |
| type | TEXT | 'depository' \| 'credit' \| 'loan' \| 'investment' \| 'pension' \| 'other' |
| subtype | TEXT | 'checking' \| 'savings' \| 'credit_card' \| 'isa' \| 'cash_isa' \| 'sipp' \| 'pension' \| 'brokerage' \| 'crypto' \| 'mortgage' \| ... |
| tax_wrapper | TEXT | 'isa' \| 'sipp' \| 'lifetime_isa' \| 'junior_isa' \| 'gia' \| 'us_401k' \| ... nullable |
| iso_currency_code | CHAR(3) | |
| is_manual | BOOL | |
| is_hidden | BOOL | |
| closed_at | TIMESTAMPTZ | nullable |
| created_at | TIMESTAMPTZ | |
| updated_at | TIMESTAMPTZ | |

Indexes: `(user_id, is_hidden, closed_at)`, `(source_provider, source_account_id)` UNIQUE where source_provider ≠ 'manual'.

### `balance`
Immutable balance snapshots.

| Column | Type | Notes |
|---|---|---|
| id | UUID | PK |
| account_id | UUID | FK |
| current_minor_units | BIGINT | |
| available_minor_units | BIGINT | nullable |
| limit_minor_units | BIGINT | nullable |
| iso_currency_code | CHAR(3) | |
| as_of | TIMESTAMPTZ | |
| source_provider | TEXT | |
| source_record_id | TEXT | nullable |
| created_at | TIMESTAMPTZ | |

Index: `(account_id, as_of DESC)`.

### `transaction`
Signed: negative = outflow.

| Column | Type | Notes |
|---|---|---|
| id | UUID | PK |
| account_id | UUID | FK |
| source_provider | TEXT | |
| source_txn_id | TEXT | |
| posted_date | DATE | |
| authorized_date | DATE | nullable |
| amount_minor_units | BIGINT | SIGNED |
| iso_currency_code | CHAR(3) | |
| description_raw | TEXT | as provider reported |
| description | TEXT | normalised |
| merchant_name | TEXT | nullable |
| merchant_logo_url | TEXT | nullable |
| category_primary | TEXT | canonical slug |
| category_detailed | TEXT | nullable |
| counterparty | TEXT | nullable |
| counterparty_type | TEXT | nullable enum |
| location_city / region / country | TEXT | nullable |
| is_pending | BOOL | |
| is_user_edited | BOOL | |
| user_edited_fields | JSONB | `{field: {old, new, edited_at}}` |
| is_hidden | BOOL | |
| is_reviewed | BOOL | |
| tags | TEXT[] | |
| notes | TEXT | nullable |
| created_at | TIMESTAMPTZ | |
| updated_at | TIMESTAMPTZ | |

Indexes: `(account_id, posted_date DESC)`, `(user_id, posted_date DESC)` via account join, `(source_provider, source_txn_id)` UNIQUE where source_provider ≠ 'manual'.

### `security`
Instrument reference (upsert target).

| Column | Type | Notes |
|---|---|---|
| id | UUID | PK |
| symbol | TEXT | |
| isin | TEXT | nullable, unique when set |
| cusip | TEXT | nullable |
| sedol | TEXT | nullable |
| figi | TEXT | nullable |
| name | TEXT | |
| type | TEXT | 'equity' \| 'etf' \| 'mutual_fund' \| 'fixed_income' \| 'derivative' \| 'cryptocurrency' \| 'cash' \| 'other' |
| exchange_mic | TEXT | nullable |
| listing_currency | CHAR(3) | |
| last_refreshed_at | TIMESTAMPTZ | |

Upsert key priority: `isin` > `figi` > `(symbol, exchange_mic)`.

### `holding`
Immutable position snapshots.

| Column | Type | Notes |
|---|---|---|
| id | UUID | PK |
| account_id | UUID | FK |
| security_id | UUID | FK |
| quantity | NUMERIC(24,10) | |
| institution_price_minor | BIGINT | per-unit |
| institution_value_minor | BIGINT | denormalised |
| cost_basis_per_unit_minor | BIGINT | nullable |
| position_currency | CHAR(3) | |
| as_of | TIMESTAMPTZ | |
| source_provider | TEXT | |
| source_record_id | TEXT | nullable |
| created_at | TIMESTAMPTZ | |

UNIQUE `(account_id, security_id, as_of)`. Index `(account_id, as_of DESC)`.

### `investment_transaction`

| Column | Type | Notes |
|---|---|---|
| id | UUID | PK |
| account_id | UUID | FK |
| security_id | UUID | FK nullable (cash events) |
| type | TEXT | 'BUY' \| 'SELL' \| 'DIV' \| 'INTEREST' \| 'FEE' \| 'TAX' \| 'CONTRIBUTION' \| 'WITHDRAWAL' \| 'TRANSFER_IN' \| 'TRANSFER_OUT' \| 'REINVEST' \| 'OPTION_EXPIRATION' \| 'OPTION_ASSIGNMENT' \| 'OPTION_EXERCISE' \| 'SPLIT' \| 'OTHER' |
| trade_date | DATE | |
| settled_date | DATE | nullable |
| quantity | NUMERIC(24,10) | nullable |
| price_minor | BIGINT | nullable |
| amount_minor | BIGINT | SIGNED cash effect |
| fees_minor | BIGINT | nullable |
| iso_currency_code | CHAR(3) | |
| description | TEXT | nullable |
| source_provider | TEXT | |
| source_txn_id | TEXT | |
| created_at | TIMESTAMPTZ | |

UNIQUE `(source_provider, source_txn_id)` where source_provider ≠ 'manual'.

### `category`
Canonical taxonomy (seed data) + user-created categories.

| Column | Type | Notes |
|---|---|---|
| slug | TEXT | PK |
| display_name | TEXT | |
| parent_slug | TEXT | nullable |
| icon | TEXT | |
| colour | TEXT | |
| system | BOOL | true for Eyrie-managed |
| user_id | UUID | nullable (null for system) |

Maintain a static mapping table from Plaid `personal_finance_category.primary` + `detailed` → canonical slug.

### `statement_artifact`

| Column | Type | Notes |
|---|---|---|
| id | UUID | PK |
| user_id | UUID | FK |
| provider_link_id | UUID | FK nullable |
| file_uri | TEXT | S3/volume key |
| mime_type | TEXT | |
| sha256 | TEXT | dedupe key |
| page_count | INT | nullable |
| uploaded_at | TIMESTAMPTZ | |
| parse_status | TEXT | 'queued' \| 'parsing' \| 'review' \| 'accepted' \| 'rejected' \| 'failed' |
| parser | TEXT | 'azure_di' \| 'claude_api' \| 'csv_native' \| 'manual_override' |
| parser_confidence | NUMERIC(4,3) | nullable |
| parsed_payload | JSONB | nullable |
| parsed_at | TIMESTAMPTZ | nullable |
| rejected_reason | TEXT | nullable |
| reviewer_user_id | UUID | nullable |
| reviewed_at | TIMESTAMPTZ | nullable |

Index: `(sha256)`, `(user_id, uploaded_at DESC)`.

### `sync_run`

| Column | Type | Notes |
|---|---|---|
| id | UUID | PK |
| provider_link_id | UUID | FK |
| started_at | TIMESTAMPTZ | |
| completed_at | TIMESTAMPTZ | nullable |
| triggered_by | TEXT | 'user' \| 'webhook' \| 'schedule' \| 'backfill' \| 'manual_retry' |
| status | TEXT | 'running' \| 'success' \| 'partial' \| 'failed' |
| counts_added | INT | |
| counts_modified | INT | |
| counts_removed | INT | |
| error_code | TEXT | nullable |
| error_message | TEXT | nullable |
| duration_ms | INT | nullable |
| metadata | JSONB | |

Index: `(provider_link_id, started_at DESC)`.

## FX rates

| Column | Type | Notes |
|---|---|---|
| date | DATE | PK with base+quote |
| base | CHAR(3) | |
| quote | CHAR(3) | |
| rate | NUMERIC(18,10) | |
| source | TEXT | 'ecb' \| 'exchangerate_host' |

Daily cron pull. Presentation-layer only — never persisted into transactions.

## Migration headers

Each migration: `YYYYMMDD_<seq>_<description>.sql`. Reversible. Generated via Fluent migrations.

---
*Source of truth: `/technical/financial-data-layer-brief.md` §6.*
