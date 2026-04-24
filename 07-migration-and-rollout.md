# 07 — Migration & Rollout

Zero-downtime migration from Yapily (banking) + Yodlee (wealth) to Plaid + SnapTrade + Statement/Manual, while keeping existing users whole.

## Guiding principles

1. **Dual-run before cutover.** Run new adapter alongside old for a period; compare canonical output.
2. **Canonicalise old data before anything else.** Get Yapily + Yodlee reads into the canonical schema first — then swap providers underneath, not from scratch.
3. **No forced re-linking of users who can keep their existing connections.** Only ask users to re-authenticate when we must.
4. **Feature-flag every switch.** Per-user and global kill switches.
5. **Keep a rollback path for 30 days post-cutover.**

## Phases

### Phase 0 — Groundwork (Week 0–1)

- Finalise canonical schema migrations.
- Stand up `InstitutionService` with pre-seeded 200 UK institutions.
- Stand up `WebhookRouter` routing by provider.
- Write `YapilyCanonicaliser` + `YodleeCanonicaliser` that read the existing data and emit canonical records. No provider API changes yet.
- Backfill canonical tables from existing production data. Verify row counts, balances reconcile.

**Exit criteria**: 100% of existing users' banking + wealth data represented in canonical tables. Canonical == legacy (spot-checked + full reconciliation of balances).

### Phase 1 — Plaid integration in sandbox (Week 1–3)

- Implement `PlaidAdapter` against Plaid Sandbox.
- Full test sweep: link flow, transactions/sync, accounts, balances, identity, webhook verification, re-auth flow, revocation.
- `PlaidAdapter` passes `AccountsPort`, `TransactionsPort`, `BalancesPort`, `IdentityPort` conformance test suite.
- Webhook delivery proven in Fly.io staging.
- iOS LinkKit integrated behind `providers.plaid.link_enabled` flag.

**Exit criteria**: 3 internal test users have Plaid-linked accounts in staging with ≥7 days of transactions flowing.

### Phase 2 — SnapTrade integration in sandbox (Week 2–4, parallel)

- Implement `SnapTradeAdapter` against SnapTrade sandbox.
- `userSecret` atomic persistence + encrypted at rest.
- SFSafariViewController portal flow on iOS.
- Holdings + activities pulls working; webhook HMAC verification live.
- Multi-currency accounts exercised (USD position in GBP account).
- Canonicalisation through `SnapTradeCanonicaliser` — account type heuristics surface to user for confirmation.

**Exit criteria**: 3 internal test users have SnapTrade-linked AJ Bell / IBKR / T212 accounts in staging with holdings + 1 year of activity backfilled.

### Phase 3 — Statement upload & manual entry (Week 3–5, parallel)

- Upload pipeline end-to-end: S3 store, PII minimiser, Claude parser, confidence scoring, review UI, accept flow.
- Manual entry SwiftUI flows wired into `ManualEntryService`.
- CSV auto-mapping for HSBC, Barclays, Monzo, AJ Bell, IBKR headers.
- Parse latency p95 measured.

**Exit criteria**: 10 internal statement uploads across banks, pensions, and brokers all reach review UI with confidence ≥0.85.

### Phase 4 — Dual-run on pilot cohort (Week 5–7)

- Recruit 50 pilot users (mix of banking + investing).
- For each: keep existing Yapily/Yodlee live; add new Plaid/SnapTrade link side by side.
- Both produce canonical records. Compare nightly:
  - Balance divergence per account.
  - Transaction set differences (adds, missing, description normalisation).
  - Holdings value divergence.
- Track bugs. Fix canonicalisers. Iterate category mappings.

**Exit criteria**:
- ≥95% transaction parity over 14-day window.
- No category-mapping regressions flagged by pilot users.
- Zero duplicate/phantom records surfaced to UI.

### Phase 5 — General cutover (Week 7–10)

User-by-user migration, in waves of 10% of MAU:

1. **Announce** via in-app banner + email 7 days ahead: "We're upgrading your bank and investment connections. You'll be asked to reconnect once. Your history stays intact."
2. **Soft cutover on next open**:
   - User opens app.
   - Bootstrap detects `needs_migration = true`.
   - Gentle modal: "Reconnect your bank / broker to keep syncing."
   - Deep-link into Plaid Link or SnapTrade Portal.
   - On success: new `provider_link` row. Old Yapily/Yodlee link marked `superseded_at=now`.
   - Historical data remains — it's already canonical — now flagged `source_provider='yapily_legacy'` / `'yodlee_legacy'`.
3. **Sync new data via new providers going forward.**
4. **Duplicate detection** during the overlap window (if new provider produces txns for the same day old provider already has): dedupe on (account, posted_date, amount, description_hash). Prefer new provider record.

Support team monitors sync failure rate, ticket volume. Wave size pauses if ticket rate spikes >2x baseline.

**Exit criteria**:
- ≥90% of MAU migrated.
- <2% of users reporting issues.
- Remaining 10% given 30-day grace window or manual concierge.

### Phase 6 — Decommission legacy (Week 10–14)

- Disable Yapily + Yodlee webhooks.
- Stop all legacy sync cron jobs.
- Cancel Yapily + Yodlee contracts (~30 days notice).
- Leave `YapilyAdapter.swift` in tree but remove from `SyncOrchestrator` registry.
- Keep `YapilyPaymentsAdapter` (dormant) for future payments flexibility; strip only the data adapter.
- Archive legacy data to cold storage (S3 Glacier), retained 7 years per AML obligations.
- Remove legacy DB tables after final audit (3 months post-cutover).

**Exit criteria**: no traffic to Yapily or Yodlee for 30 days; contracts terminated.

## Feature flags

Global (GrowthBook or equivalent):
- `providers.plaid.enabled`
- `providers.snaptrade.enabled`
- `providers.yapily.data_enabled` (default true until Phase 6)
- `providers.yodlee.enabled` (default true until Phase 6)
- `payments.enabled` (default false; see 06)

Per-user (JSONB on `user.feature_flags`):
- `migration.cohort` — "pilot" | "wave_1" | ... | "grace"
- `migration.required_for` — ["banking", "investments"]
- `migration.completed_at`

## Rollback

Each phase has a rollback:
- **Phase 4/5**: single-user rollback — flip `superseded_at=null` on legacy link; hide new link; resume legacy sync. Kept available for 30 days.
- **Phase 6**: if catastrophic issue discovered within 30 days, legacy can be re-engaged (contracts not cancelled until then).
- **Post-Phase 6**: no rollback. Forward-only.

## Communications plan

### Pre-migration (7 days before wave)
- Email: "Upgrading your connections — here's what's changing."
- In-app banner.
- Help centre article explaining what/why/when.

### During migration (in-app)
- Modal on open.
- Clear copy: which accounts need reconnecting, which are unaffected.
- Progress indicator during Link.
- Confirmation screen.

### After migration
- Push notification: "All caught up."
- Email confirming new link setup.
- History reassurance: "All your past transactions are preserved."

### Support prep
- Macros for common tickets:
  - "Why am I being asked to reconnect?"
  - "Can I keep my old connection?"
  - "Will my history be lost?"
  - "My bank isn't supported anymore — what now?"
- Escalation path for stuck users (statement-upload concierge).

## Data integrity checks (nightly during Phases 4–5)

Cron job runs per-user:
```
1. Sum of GBP-denominated balances — new provider vs legacy.
2. Transaction count by month — new vs legacy.
3. Holdings market value — new vs legacy.
4. Any divergence >2% flagged to dashboard.
```

Report posted to `#eyrie-migration` Slack channel daily.

## Success metrics

- **Migration completion**: % MAU on new providers (target 95% by Phase 6).
- **Sync reliability**: % links with `status='active'` 7 days post-migration (target 98%).
- **User-reported issues**: tickets per migrated user (target <0.05).
- **Canonical parity**: balance divergence p95 <£1 or <0.1% (whichever larger).
- **Time in re-auth state**: p95 <24h.

## Open items for Claude Code

- Exact cohorting mechanism (cohort flag on user model vs. separate table).
- Migration concierge UI — statement upload path for users whose bank isn't on Plaid/SnapTrade.
- Reconciliation dashboard (internal) — source-parity scorecards.
- Scripts for legacy-data backfill → canonical migration.
- Data retention policy for superseded `provider_link` rows.

---
*Source of truth: `/technical/financial-data-layer-brief.md` §13.*
