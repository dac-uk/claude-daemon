# 06 — Payments Port

Read-only at launch. Payments is a **separate port** with dormant adapters, so we can switch it on without a rewrite when the product is ready.

## Why separate from data providers

Payments is a fundamentally different trust model:
- **Data**: user authorises a read. Worst case: stale data.
- **Payments**: user authorises money movement. Worst case: funds lost, regulatory exposure, FOS complaints.

Conflating them into a single adapter means every data-only change pulls payments regression risk along with it. Splitting them also lets us swap payments providers independently — the UK VRP market is moving fast (cVRP pilots launched early 2026) and locking into one provider at launch is premature.

## The port

```swift
protocol PaymentsPort {
    /// Create a VRP mandate (or single-payment authorisation).
    func createMandate(
        userId: UUID,
        parameters: MandateParameters
    ) async throws -> MandateHandle

    /// Initiate a payment under an existing mandate.
    func executePayment(
        mandate: MandateHandle,
        amount: Money,
        reference: String
    ) async throws -> PaymentIntent

    /// Revoke an active mandate.
    func cancelMandate(mandate: MandateHandle) async throws

    /// Poll/fetch payment status.
    func getPayment(paymentId: String) async throws -> PaymentIntent
}

struct MandateParameters {
    let type: MandateType          // .sweeping | .commercialVRP | .singlePayment
    let debtorAccount: AccountReference
    let creditorAccount: AccountReference
    let maxPerPayment: Money?
    let maxPerPeriod: Money?
    let period: VRPPeriod?         // .daily | .weekly | .monthly
    let reference: String
    let validUntil: Date?
}

struct MandateHandle {
    let id: UUID
    let provider: PaymentsProvider  // .plaid | .yapily | .truelayer | ...
    let providerMandateId: String
    let status: MandateStatus       // .pending | .active | .revoked | .expired
    let consentExpiresAt: Date?
}

enum MandateType { case sweeping, commercialVRP, singlePayment }
```

## Launch posture

**`PaymentsService` is instantiated with a `NoopPaymentsAdapter`** at launch. Every port call returns `PaymentsUnavailableError`. The app's UI never surfaces payment affordances — they're behind a feature flag (`payments.enabled = false`).

We ship the port shape, dormant adapters, and the safety model (below) so that the engineering cost of turning payments on is ~3-5 days of integration work plus regulatory sign-off, not a re-architecture.

## Dormant adapters

### `PlaidPaymentsAdapter` (UK)
- Conforms to `PaymentsPort`.
- Wraps Plaid's PIS + Sweeping VRP APIs.
- Live for Sweeping VRP (same-name account transfers) in UK today.
- Commercial VRP currently in pilot (Q1 2026); expected GA late 2026.
- Pros: one vendor for data + payments, shared creds/tokens.
- Cons: Plaid's UK payments coverage is narrower than Yapily at time of writing.

### `YapilyPaymentsAdapter` (UK)
- Conforms to `PaymentsPort`.
- Wraps Yapily PIS + VRP APIs.
- Broader UK VRP bank coverage (Yapily was ahead in 2024–2025).
- Pros: payment-specialist provider, mature.
- Cons: separate contract, separate webhook path.

### Selection
`PaymentsService` selects an adapter by:
1. User's debtor bank (some support VRP with Plaid, some only via Yapily/TrueLayer).
2. Mandate type requested.
3. Commercial preference (cost, SLAs).

The selection table lives in `PaymentsProviderRegistry.swift` and is config-driven.

## VRP safety model

Before any payment executes, `VRPSafetyModel` must return `.safe`. Checks:

1. **Mandate bounds**: requested amount ≤ remaining allowance in current period.
2. **Velocity**: no more than N payments per 24h (configurable per user tier).
3. **Novelty**: first-time creditor triggers a delay + user confirmation push.
4. **Balance check** (optional): pre-flight balance via `BalancesPort` — abort if payment would leave account below safety floor.
5. **User consent freshness**: mandate must have been re-confirmed if older than 90 days (configurable).
6. **Kill switch**: a global `payments.circuit_breaker` flag; if tripped, all payments halt.

Every decision logged to `payment_safety_log` with full reasoning trace.

## Data model additions (payment-on day)

When payments launches, migration adds:

```
mandate
  id, user_id, provider, provider_mandate_id, type, status,
  debtor_account_id, creditor_account_reference, max_per_payment,
  max_per_period, period, consent_expires_at, created_at, revoked_at

payment_intent
  id, mandate_id, amount_minor, currency, reference,
  status, provider_payment_id, initiated_at, settled_at,
  safety_trace JSONB
```

These are absent from the v1 schema. Keeps v1 DB clean.

## Commercial VRP (cVRP) watch

cVRP unlocks "pay Eyrie subscription via VRP" and "sweep to investments" flows. Milestones to watch:

- **2026 H1** — Pilot with 5-6 banks; Plaid in pilot.
- **2026 H2** — Broader rollout expected.
- **2027** — Regulatory mandate likely.

We will re-evaluate turning on `PaymentsService` once:
1. cVRP is live at ≥3 of our user-weighted top banks.
2. An FCA-registered payments provider (us or partner) can execute.
3. A compelling product moment exists (subscription billing migration; round-up-to-invest flow).

## iOS contract

When payments is off:
- No payments routes exposed in the Vapor API.
- The `PaymentsService` still compiles; calls return `.unavailable`.
- No `PaymentKit` import on iOS.

When payments turns on:
- Feature flag lit per-user.
- iOS receives `payments.enabled = true` in bootstrap config.
- Payment affordances appear; pay flow invokes `POST /api/v1/payments/mandates` → opens Plaid/Yapily portal via `SFSafariViewController`.

## Open items for Claude Code

- Confirm which of Plaid / Yapily / TrueLayer gives broadest UK VRP coverage at time of payments launch.
- FCA registration strategy: agent-of-PISP vs. own AISP/PISP authorisation.
- Map which iOS users' banks support VRP (affects launch scope).
- Decide default creditor-account handling (Eyrie pooled safeguarded account vs. direct-to-broker).

---
*Source of truth: `/technical/financial-data-layer-brief.md` §10.*
