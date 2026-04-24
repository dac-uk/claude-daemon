# 08 — Risks & Open Questions

Live register. Every entry names who owns the follow-up.

## Risks

### R1 — Plaid UK investment coverage gap
**Description**: Plaid's UK Investments product is opportunistic. If an institution we target (e.g. HL, AJ Bell) isn't covered and SnapTrade also doesn't cover it, users fall to statement upload — worse UX.

**Mitigation**: SnapTrade is the primary investments provider by design. Statement/manual is the supported fallback and a first-class channel, not a fallback narrative.

**Owner**: David.
**Status**: Open. Verify at SnapTrade + Plaid contracting.

### R2 — SnapTrade `userSecret` loss
**Description**: A lost `userSecret` is unrecoverable; user must delete + re-onboard on SnapTrade. If our DB has a bug wiping the field, we lose all users' investment connections.

**Mitigation**:
- Atomic insert with transaction on registration.
- `userSecret` column `NOT NULL` with FK-level enforcement.
- Encrypted at rest (AES-256-GCM).
- Separate backup with stricter retention for this column.
- Alerting if any `snaptrade_user.userSecret IS NULL`.

**Owner**: Backend eng (Claude Code).
**Status**: Mitigated in design; verify in implementation.

### R3 — Claude API rejects raw PDFs (privacy review)
**Description**: If Anthropic's DPA or our internal review blocks sending raw statement PDFs (which may contain NI numbers, addresses, merchants), MVP parsing breaks.

**Mitigation**:
- PII minimisation before Claude call (redact NI numbers, collapse addresses to postcode prefix).
- Fallback: Option E (Azure DI → Claude) — Azure handles the raw document in UK South; Claude only sees extracted text + tables.
- Decision gate defined in `05-manual-entry-and-upload.md`.

**Owner**: David to raise with Anthropic at contract stage.
**Status**: Open.

### R4 — SnapTrade data residency
**Description**: SnapTrade is Canadian. UK users' investment data transits/resides outside UK. Requires valid international data transfer mechanism under UK GDPR.

**Mitigation**:
- Request UK/EU residency option at contract.
- If unavailable: execute Standard Contractual Clauses + transfer risk assessment.
- Disclose clearly in privacy policy.
- Worst case: swap to alternative (e.g. partnership with UK-resident broker aggregator, statement-upload-first for investments).

**Owner**: David.
**Status**: Open. Confirm at SnapTrade contracting.

### R5 — Plaid rate limits under scale
**Description**: 50 req/min/Item on `/transactions/sync`, 250 req/min global on SnapTrade. At 10k UK + 5k US MAU with aggressive webhook-driven sync, we could hit ceilings.

**Mitigation**:
- Per-item token bucket in `SyncOrchestrator`.
- Request higher limits before scaling past 5k MAU.
- Observability dashboard on sync queue depth.

**Owner**: Backend eng.
**Status**: Mitigated in design.

### R6 — Category mapping drift
**Description**: Plaid's `personal_finance_category` taxonomy evolves. Static mapping table goes stale; user-facing categories become inconsistent.

**Mitigation**:
- Version the mapping: `PlaidCategoryMap.v2.swift`.
- CI check that fails if Plaid returns an unknown category.
- Quarterly review.

**Owner**: Backend eng.
**Status**: Process defined; owner TBD for review cadence.

### R7 — Duplicate records during migration
**Description**: Dual-run produces overlapping transactions; naive dedup can either drop real records or leave dupes.

**Mitigation**:
- Dedup keys documented in 07.
- Canonical parity dashboard during Phases 4–5.
- Manual spot-check on 100 random accounts pre-cutover.

**Owner**: Backend eng + David.
**Status**: Process defined.

### R8 — IBKR multi-currency data quality
**Description**: Known SnapTrade bug around IBKR multi-currency accounts (balances misreported per currency).

**Mitigation**:
- Detect IBKR-brokered accounts at link time.
- Warn user: "Your IBKR account may show multi-currency anomalies. We're working with our provider on this."
- Flag in `account.metadata.known_issues = ['ibkr_multicurrency']`.
- Cross-check against statement upload when available.

**Owner**: Backend eng.
**Status**: Mitigated in design.

### R9 — Consent expiry storm
**Description**: Plaid UK consent is 180 days. A cohort that onboarded in a short window all expires simultaneously — 10% of MAU forced to re-auth in one week.

**Mitigation**:
- `PENDING_EXPIRATION` webhook fires 7 days ahead — pre-notify user.
- Stagger onboarding cohorts where practical.
- Re-auth flow must be frictionless (one-tap re-link).

**Owner**: Backend eng + Product.
**Status**: Mitigated in design.

### R10 — LLM cost blowout on statement parsing
**Description**: Claude API costs scale with statement volume. At ~£0.15/statement and 1 statement/user/month for 5% of MAU (pensioners + landlords), ~£100–200/month — fine. But a user uploading 30 months of backdated pension statements at onboarding costs £4+.

**Mitigation**:
- Batch discount (50%).
- Prompt caching (90%).
- Per-user soft cap: 10 statements/24h; beyond requires Support approval.
- Move to Option E (hybrid) if cost exceeds £500/month.

**Owner**: Backend eng.
**Status**: Mitigated.

### R12 — Plaid unit economics unknown (GA BLOCKER)
**Description**: Plaid does not publish production pricing. Cost per connected Item at 10k UK + 5k US MAU is unknown. If Plaid's `/transactions/sync` + Investments combo prices above ~£0.60/user/month, stacked with SnapTrade's ~$2/user/month, we breach the per-user-cost envelope implied by our subscription tier plan.

**Mitigation**:
- Open Plaid commercial conversation **before** Week 6 of the migration (Phase 2 start). No production migration without a signed rate card.
- Model three tiers (base AISP + on-demand balances; add Investments; add webhooks-at-scale) so pricing drives scope, not the other way round.
- Fallback if pricing is untenable: launch UK on statement-upload-first for investments, hold Plaid Investments back to a later wave.

**Owner**: David (commercial).
**Status**: OPEN — blocks GA sign-off. See Q6.

### R13 — GTM timeline overlap with backend rebuild
**Description**: Migration is scoped at 6–8 weeks of backend engineering (Phase 0 → Phase 6 in `07-migration-and-rollout.md`). Existing Eyrie GTM/launch planning assumes a 6–8-week window for App Store submission, privacy/DPA updates, and marketing. If the two tracks run end-to-end sequentially we miss the launch window; if they overlap, the App Store submission may ship against a half-migrated backend with both Plaid and Yapily live, producing inconsistent user experiences.

**Mitigation**:
- Ring-fence a feature-flag state (`providers.plaid.enabled=false`, `providers.yapily.data_enabled=true`) such that a TestFlight build can go to App Review while backend rebuild continues.
- Gate the App Store public release behind the flag flip — no binary change required to cut over once backend is ready.
- Privacy policy + DPIA + processor listing updates land in the same App Review cycle (not a later update).

**Owner**: David + Backend eng.
**Status**: Mitigation design only; scheduling decision open.

### R11 — Fly.io secrets are plaintext env vars
**Description**: Fly.io secret store is plaintext environment variables. A compromised machine exposes Plaid + SnapTrade credentials + per-user access tokens.

**Mitigation for v1**:
- Per-user `access_token` encrypted at rest in DB via `KMS_MASTER_KEY` that lives in secrets.
- One compromised box exposes KMS_MASTER_KEY → can decrypt tokens.
- Accept for MVP (industry-standard posture).

**Mitigation for scale**:
- Migrate `KMS_MASTER_KEY` to AWS KMS or GCP KMS with envelope encryption.
- Tokens decrypted per-request via KMS call.
- Budget: 2 eng-weeks.

**Owner**: Backend eng.
**Status**: Deferred. Revisit at 5k MAU or first external security audit.

## Open questions (for Claude Code to run down)

| # | Question | Blocks |
|---|---|---|
| Q1 | Does SnapTrade offer UK or EU residency for user data? | Contract, privacy policy |
| Q2 | Is Anthropic's DPA acceptable for sending raw statement PDFs with PII? | MVP parsing design |
| Q3 | Which of HL, Vanguard UK, Freetrade are actually live on SnapTrade today? | Onboarding UX copy |
| Q4 | Exact LSE symbol convention SnapTrade returns (`.L` vs `.LSE`)? | Security mapping |
| Q5 | Does Plaid UK Investments cover AJ Bell, HL, Vanguard UK, T212? | Provider-selection logic |
| Q6 | **Plaid pricing at 10k UK + 5k US MAU? (GA BLOCKER — see R12)** | **Unit economics; GA sign-off** |
| Q7 | SnapTrade pricing at scale ($2/user/month at volume)? | Unit economics |
| Q8 | iOS minimum version for `plaid-link-ios-spm` main branch? | iOS deployment target |
| Q9 | Which CSV formats are highest-priority for auto-mapping (user research)? | Parse quality |
| Q10 | Do we need a human-in-the-loop review for low-confidence statements? | Launch scope |
| Q11 | Webhook endpoint DoS protection (rate-limit bypass via signature-verified spoofs)? | Security |
| Q12 | What's our retention for raw webhook bodies (for replay)? | Storage / GDPR |
| Q13 | How do we handle a user's manual edit to a transaction that the provider later modifies? (Preserve edits or re-apply provider truth?) | UX spec |
| Q14 | When a SnapTrade account closes on the broker side, is history retained in our canonical tables? | Data model |
| Q15 | cVRP commercial availability timeline — when can we turn payments on? | Payments launch |
| Q16 | Confirm UK Plaid consent model framing — is the 180-day window an FCA-mandated maximum, a Plaid default, or a TPP-managed renewal semantics? (Web verification Apr 2026 could not resolve this from public docs.) | Consent-expiry UX copy, legal basis |
| Q17 | Full list of SnapTrade webhook event names beyond `ACCOUNT_HOLDINGS_UPDATED`, `ACCOUNT_TRANSACTIONS_UPDATED`, `USER_CONNECTION_RENEWED`, `ACCOUNT_DELETED`, `CONNECTION_DELETED`. Docs for new events (e.g. partial-sync, error states) couldn't be verified from public pages. | Webhook handler coverage |
| Q18 | Exact set of UK brokerages currently live on SnapTrade (HL, Vanguard UK, T212, Freetrade, AJ Bell, IBKR, Moneybox, Nutmeg — which are shipping vs. beta vs. unplanned?). | Onboarding UX, broker picker |
| Q19 | Complete `TransactionsAndReporting_getActivities` activity types enum (BUY / SELL / DIV / CONTRIBUTION / INTEREST / FEE / TRANSFER / STOCK\_SPLIT / …). Source of truth beyond our current mapping. | InvestmentTransaction mapping, CategorizationService |

## Pre-GA checklist

Before any user onboarded under the new stack:

- [ ] Plaid production credentials received; sandbox parity verified.
- [ ] SnapTrade production credentials; sandbox parity verified.
- [ ] Claude API production key; Anthropic DPA signed.
- [ ] Azure DI UK South provisioned (or deferred to Option D only).
- [ ] `KMS_MASTER_KEY` rotated; rotation runbook documented.
- [ ] Webhook endpoints deployed with signature verification tested against real payloads.
- [ ] Universal Links working on production domain.
- [ ] App Store Connect: associated-domains entitlement configured.
- [ ] Privacy policy updated: Plaid, SnapTrade, Claude (Anthropic), Azure DI (if hybrid) named as data processors with jurisdictions, transfer mechanisms (SCCs where applicable), and retention periods.
- [ ] Terms of service updated: data aggregation scope, statement processing consent, LLM-based processing disclosure.
- [ ] DPIA section on international transfers (Plaid US, SnapTrade Canada, Anthropic US) completed with transfer risk assessments.
- [ ] Plaid production pricing received and modelled against subscription tier plan (R12 / Q6 closed).
- [ ] Support macros written for top-10 expected tickets.
- [ ] Status page wired for Plaid / SnapTrade / Claude outages.
- [ ] Alerting: sync failure rate, webhook verification failures, Claude API errors.
- [ ] Backup/restore of `snaptrade_user.userSecret` tested.
- [ ] Security review of the adapter + canonicalisation layer.
- [ ] DPIA (Data Protection Impact Assessment) updated.
- [ ] Rollback procedure rehearsed.

## Escalation

Any `severity=high` risk that materialises triggers:
1. Slack `#eyrie-incidents` page.
2. 15-minute triage: accept / mitigate / roll back.
3. Decision logged in `docs/incidents/<date>.md`.

---
*Source of truth: `/technical/financial-data-layer-brief.md` §16.*
