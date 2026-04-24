# 00 — Overview

**Project:** Eyrie Financial Data Layer  
**Version:** 1.0 | 22 April 2026  
**Related:** [`../financial-data-layer-brief.md`](../financial-data-layer-brief.md) — single comprehensive brief

## Problem

Eyrie's current data stack couples directly to Yapily (UK Open Banking) and Yodlee (UK investments/pensions). Yodlee declined to serve us; Yapily's UK coverage and re-consent UX underperform the 2026 market leaders. The AI layer, the Financial Decision Engine, Tribe, and Ask Eyrie all read provider-specific types, creating lock-in.

## Target state

A **provider-agnostic financial data layer** where:

- **Plaid** powers UK + US banking data (transactions, accounts, identity, balances, investments opportunistically).
- **SnapTrade** powers investment/wealth/pension data for brokers it supports (AJ Bell live, IBKR, Trading 212, plus full US broker roster).
- **Statement uploads + manual entry** handle everything neither API covers — workplace pensions, legacy ISAs, illiquid assets.
- **Canonical schema** (Institution, ProviderLink, Account, Balance, Transaction, Security, Holding, InvestmentTransaction, Category, StatementArtifact, SyncRun) is the single source of truth.
- **Ports & adapters** (Hexagonal) isolate provider APIs from the canonical layer.
- **Payments** lives in a separate port; Yapily stays available as a future VRP adapter.

## Why this matters

- **Swap cost:** provider replacement is adapter work (3 days), not schema work (weeks).
- **Coverage:** three complementary channels cover the entire UK household finance surface.
- **Compliance:** clearer data flow for ICO + FCA + GDPR posture.
- **AI quality:** the AI layer gets normalised, provenanced, conflict-resolved data.

## Non-goals

- Real-time trading (SnapTrade supports it; not on Eyrie's near-term roadmap).
- Plaid Liabilities for UK (unavailable; statement upload covers it).
- Crypto trading (SnapTrade supports read-only; trading deferred).
- Multi-tenant business banking (stays on current Business Mode path).

## Success criteria

- 100% of existing Yapily/Yodlee user data preserved through migration.
- p95 webhook → canonical write latency < 30 s.
- Statement parse success rate (high-confidence band) > 85%.
- Re-auth completion rate within 14 days of prompt > 80%.
- Zero incidents of user-edit loss after provider re-sync.
- New provider adapter additions land in ≤3 days of backend work.

## Reading order

1. `00-overview.md` — this file
2. `01-architecture.md` — principles, ports, adapters, services
3. `02-canonical-schema.md` — the core data model
4. `03-plaid.md` — Plaid adapter design
5. `04-snaptrade.md` — SnapTrade adapter design
6. `05-manual-entry-and-upload.md` — statement pipeline + manual
7. `06-payments-port.md` — Yapily migration story
8. `07-migration-and-rollout.md` — staged cutover plan
9. `08-risks-and-open-questions.md` — known gaps, verification checklist

Diagrams in `./diagrams/`.

---
*Source of truth: `/technical/financial-data-layer-brief.md`.*
