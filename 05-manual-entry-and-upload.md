# 05 — Statement Upload & Manual Entry

A first-class channel, not a fallback. Handles everything the APIs can't reach: workplace pensions, some legacy ISAs, private assets, illiquid holdings.

## Two sub-channels

1. **Statement upload:** PDF/CSV → parser → canonical records.
2. **Manual entry:** native forms → validator → canonical records.

Both produce records with `source_provider='statement'` or `source_provider='manual'`. These sit alongside Plaid and SnapTrade records in the canonical layer.

## Parsing stack evaluation

### Options

**A. Azure Document Intelligence** — ~$0.01/page (Read), ~$0.065/page (prebuilt layout+tables). UK South region for residency. Strong on tables. Prebuilt bank-statement model is US-trained (custom model training likely for UK formats). Fast (seconds per page).

**B. AWS Textract** — AnalyzeDocument $0.065/page. Mature. No bank-statement specialist. We're on Fly.io, not AWS — adds egress complexity.

**C. Google Document AI** — Bank Statement Parser ~$0.75/doc. Marketed for US. Adds third cloud.

**D. Claude API (Sonnet 4.6, Files API + tool use)** — $3/M input + $15/M output. 50% batch discount. 90% prompt caching. Native PDF. Structured outputs validate directly against our canonical schema. Reasoning over layout and UK-specific tax wrappers (ISA/SIPP/GIA). ~$0.09/statement at list.

**E. Hybrid: Azure DI → Claude post-processor** — Azure layout extraction ($0.10 for 10-page statement), Claude schema-map from JSON (~$0.015). Blended ~£0.12/statement, <8 s latency, good accuracy. Two vendors.

### Recommendation

**MVP: Option D (Claude API alone).** One vendor, ship fast, best schema fidelity. Budget ~£0.15/statement at list. Good enough under 500 statements/month.

**At scale (>500/month or if PII review rejects raw-PDF-to-Claude): Option E (hybrid).** Add Azure DI UK South as a pre-pass. Reduces cost to ~£0.10/statement and keeps raw-PDF PII inside UK jurisdiction.

**CSV: native Swift parsing.** No API. Column-mapping UI on iOS. Detect common bank exports (HSBC, Barclays, Monzo, AJ Bell, IBKR) by header signature and auto-map.

### Decision gates
Move MVP → Hybrid when ANY is true:
- Monthly statement volume exceeds 500.
- Anthropic's DPA / data residency terms require review-blocked remediation.
- Observed parse success rate <85% at high-confidence band.
- Per-statement parse latency p95 > 15 seconds.

The mode is controlled by the `statement.parser.mode` feature flag (`"claude"` | `"hybrid"`). The high-confidence auto-accept threshold is controlled by `statement.parser.max_confidence` (default 0.95). See `CLAUDE_CODE_SPEC.md` feature-flags section.

## Upload pipeline

```
User uploads PDF/CSV/screenshot
    ↓
POST /api/v1/statements
    ↓
Stream to S3-compatible store; compute sha256 → reject if duplicate within user+24h
    ↓
Create statement_artifact row with parse_status='queued'
    ↓
Return 202 + artifactId to iOS
    ↓
Background worker (dedicated worker pool):
    • Load file
    • PII minimisation (redact NI numbers, collapse addresses to postcode prefix)
    • Call parser (Claude Files API + tool-use OR Azure DI → Claude)
    • Validate output against canonical schema
    • Compute confidence score
    • Stage records in pending_canonical bucket
    • Update artifact: parse_status='review', parser, confidence
    • Send APNs StatementParseReady
    ↓
User reviews in app
    • Confirm account mapping (new account OR merge with existing)
    • Spot-check highlighted rows (outliers, low-confidence fields)
    • Accept/Reject
    ↓
On Accept:
    • POST /api/v1/statements/{id}/accept with user overrides
    • CanonicalisationService commits staged records
    • artifact.parse_status='accepted', reviewed_at=now
    ↓
On Reject:
    • artifact.parse_status='rejected'
    • Invite user to re-upload or switch to manual entry
```

## Confidence bands

- **High (≥0.95):** One-tap accept; UI highlights 3 sampled rows.
- **Medium (0.80–0.95):** User must review all fields.
- **Low (<0.80):** Reject; suggest re-upload or manual entry.

## Parser prompt (Claude API, MVP)

System prompt (cached):
```
You are a financial statement parser. Extract account details, balances, transactions,
and holdings from UK and US statements (bank, investment, pension, credit card).

Identify the account tax wrapper from context:
  - "Cash ISA" → cash_isa
  - "Stocks & Shares ISA" / "S&S ISA" → isa
  - "Lifetime ISA" / "LISA" → lifetime_isa
  - "Junior ISA" / "JISA" → junior_isa
  - "Self-Invested Personal Pension" / "SIPP" → sipp
  - "General Investment Account" / "GIA" → gia
  - US 401(k) → us_401k; IRA → us_ira; Roth → us_roth

All monetary amounts: sign negative for outflows from the account holder's
perspective; positive for inflows.

Emit response via the `extract_statement` tool (schema enforced).
```

Tool schema (canonical-aligned):
```json
{
  "name": "extract_statement",
  "input_schema": {
    "type": "object",
    "properties": {
      "statement_period": { "type": "object", "properties": { "from": {"type": "string", "format": "date"}, "to": {"type": "string", "format": "date"} } },
      "account": {
        "type": "object",
        "properties": {
          "institution_name": {"type": "string"},
          "account_name": {"type": "string"},
          "account_mask": {"type": "string"},
          "subtype": {"enum": ["checking","savings","credit_card","isa","cash_isa","sipp","pension","brokerage","crypto","mortgage","student_loan","other"]},
          "tax_wrapper": {"type": ["string","null"]},
          "iso_currency_code": {"type": "string"}
        },
        "required": ["institution_name","account_name","subtype","iso_currency_code"]
      },
      "opening_balance_minor": {"type": "integer"},
      "closing_balance_minor": {"type": "integer"},
      "transactions": {
        "type": "array",
        "items": {
          "type": "object",
          "properties": {
            "posted_date": {"type":"string","format":"date"},
            "description": {"type":"string"},
            "amount_minor": {"type":"integer"},
            "iso_currency_code": {"type":"string"},
            "category_primary": {"type":"string"},
            "merchant_name": {"type":["string","null"]}
          },
          "required":["posted_date","description","amount_minor","iso_currency_code"]
        }
      },
      "holdings": {
        "type": "array",
        "items": {
          "type":"object",
          "properties":{
            "symbol":{"type":"string"},
            "isin":{"type":["string","null"]},
            "name":{"type":"string"},
            "quantity":{"type":"number"},
            "price_minor":{"type":"integer"},
            "value_minor":{"type":"integer"},
            "cost_basis_per_unit_minor":{"type":["integer","null"]},
            "position_currency":{"type":"string"}
          },
          "required":["symbol","name","quantity","price_minor","value_minor","position_currency"]
        }
      },
      "confidence": {"type":"number","minimum":0,"maximum":1}
    },
    "required":["account","confidence"]
  }
}
```

## Manual entry

Native SwiftUI forms. Everything funnels through `ManualEntryService`.

Supported flows:
- **Add account** — institution typeahead (against `institution` table), then type/subtype/tax wrapper/currency.
- **Log a transaction** — amount, date, description, merchant, category.
- **Batch CSV import** — column-mapping UI; preview rows; confirm import.
- **Add holding** — security typeahead (ISIN lookup), quantity, cost basis, as_of.
- **Log investment transaction** — type (BUY/SELL/DIV/CONTRIBUTION…), security, quantity, price.
- **Recurring entry** — "my workplace pension receives £500/month from my employer starting 2024-01". Generates future transactions.

### Validation
- Currency must match account's `iso_currency_code` OR flag as foreign-exchange.
- Transaction dates cannot be > 7 days in the future (except recurring schedules).
- Holdings cannot be negative quantities unless short positions are explicitly flagged.
- Amounts validated at currency-appropriate precision.

### Provenance
All manual records carry `source_provider='manual'`, and the UI marks them with a small "M" badge. User sees clearly which numbers come from an API vs. which they entered themselves.

## Manual ↔ API merging

If a user manually creates an account for "Hargreaves Lansdown SIPP" and later HL becomes available via SnapTrade:

1. SnapTrade adapter pulls the HL SIPP account.
2. CanonicalisationService detects candidate match (institution + mask/name similarity).
3. Prompt user: "We can now connect your HL SIPP automatically. Merge your manual entries with automatic data?"
4. If accepted: manual account absorbed; transactions `source_provider` flipped to `snaptrade` where matched by amount+date+security, else kept as `manual` records (preserving history).
5. If declined: two separate accounts in UI, user manages manually.

## Security

- Uploaded files encrypted at rest.
- Retained 90 days post-parse for debugging; then purged unless user explicitly keeps.
- On user account deletion: statement files purged within 30 days (GDPR erasure).

## Open items for Claude Code

- PII minimiser — regex + NER library choice (spaCy, Presidio, hand-rolled regex).
- Anthropic DPA terms — confirm residency + retention.
- CSV auto-mapping heuristics for top 20 UK providers (build a signature library).
- Error taxonomy for parse failures (corrupt PDF, OCR failure, schema mismatch).
- Reviewer UI design (defer to design-assessment.md).

---
*Source of truth: `/technical/financial-data-layer-brief.md` §5.3, §8.*
