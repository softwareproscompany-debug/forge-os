# Growth Engine — Repository Audit

**Date:** 2026-10-09
**Scope:** Audit only. No implementation. Maps the Growth Engine product spec onto the existing ForgeOS architecture, identifying reuse vs. build, producing a file-level P1–P6 plan, and flagging risks.

**Critical orientation:** The existing affiliate module (`affiliate_programs` / `affiliate_links`, migration 0003) is the **inverse direction** of what the spec needs. Existing = *outbound*: "we promote third-party offers, we earn commissions." Spec = *inbound*: "partners promote OUR products, we pay THEM commissions." The tracking plumbing (short links, click events, conversion postbacks) is reusable as a *pattern*, but the domain tables must be new. Do not bolt inbound partner logic onto the outbound affiliate tables.

---

## 1. Existing architecture inventory

### 1.1 Multi-tenancy (REUSE AS-IS)
- Every table except `businesses`/`users` carries `business_id` (`packages/forge-db/forge_db/models.py`).
- `scoped(db, Model, user)` in `apps/api/app/core/deps.py` filters all queries by JWT `business_id`.
- `get_owned_or_404()` enforces per-row ownership.
- **Verdict:** Solid. All new Growth Engine tables must follow this convention. No gaps found.

### 1.2 Identity & roles (EXTEND)
- `UserRole`: `owner` | `admin` | `member` — all **internal** business users.
- `require_role(*roles)` dependency factory (`apps/api/app/core/deps.py:76`) — 403s on mismatch.
- **Gap:** No external identity. Partners cannot be `User` rows (a partner with a member account would see the whole business). The spec's 7 roles (customer referrer, affiliate, agency partner, partner manager, finance operator, administrator, AI assistant) do not exist.
- **Needed:** A separate `PartnerUser` (or portal-token) identity, plus at least two new internal roles (`finance` for payouts, `partner_manager` — or map to existing admin/owner with route-level guards).

### 1.3 Existing affiliate module (PATTERN REUSE ONLY)
- Tables: `affiliate_programs` (name, network, website_url, `default_commission_pct` Numeric(6,3), `cookie_days`, status) and `affiliate_links` (slug unique per business, destination_url, UTM fields, is_active). Migration `0003_affiliates.py`.
- Router: `apps/api/app/routers/affiliates.py` — tenant-scoped CRUD, `GET /r/{slug}` public redirect (302 + `affiliate_clicked` event, UTM append, no auth, 404-safe), `POST /affiliates/conversions` postback (commission = order_value × default_commission_pct unless explicit), `GET /affiliates/earnings` (aggregates `affiliate_converted` events on the fly).
- Frontend: `apps/web/src/pages/Affiliates.tsx` (627 lines, program/link forms, earnings view). Route `/affiliates`.
- Draven tools: `draven.affiliate_earnings`, `affiliate_link_create/update/delete`, `affiliate_program_create/update/delete` (in `draven_tools_parity.py`).
- **Reusable patterns:** short-link redirect with click capture, conversion postback shape, earnings aggregation, tenant-scoped CRUD structure, frontend form patterns.
- **Not reusable as domain:** tables, router, and UI are outbound-specific. The spec needs inbound equivalents.

### 1.4 Events table (REUSE)
- `events` (business_id, contact_id nullable, kind, payload JSONB, created_at). Kinds include `affiliate_clicked`, `affiliate_converted`, `converted`.
- **Verdict:** Reuse for attribution touchpoints (`partner_click`, `partner_conversion`). It is an append-only log, **not** a financial ledger — see §4 risks.

### 1.5 Webhook ingestion (REUSE PATTERN)
- `POST /webhooks/delivery` — shared-secret header auth, idempotent-ish send lookup.
- Alpha lead intake (`apps/api/app/alpha/`) — HMAC-SHA256 (`X-Signature-256`), idempotent replay, dedup, normalization. This is the gold-standard pattern in the repo.
- **Verdict:** Server-side conversion ingestion for the spec should copy the alpha HMAC + idempotency-key pattern, not the delivery-webhook shared-secret pattern.

### 1.6 Worker / queue (REUSE)
- arq, Redis queue `forge` (`apps/worker/worker/settings.py`). Crons: `campaign_tick` every 60s, `autopilot_plan` 4×/hour, `weekly_summary` hourly.
- **Verdict:** Add `partner_commission_calc` and `partner_payout` jobs here. No new infra needed.

### 1.7 Approvals (REUSE PATTERN)
- `asset_approvals` with `ALLOWED_ASSET_TRANSITIONS` state machine (`models.py`), approval-gated Draven tools (`campaign_pause`, `asset_approve` return approval requests, never execute).
- **Verdict:** Commission approval queue and payout release should follow this exact pattern. AI proposes, human approves.

### 1.8 Secrets vault (REUSE)
- `business_secrets` table, Fernet-encrypted, `KNOWN_SECRETS` allowlist (`apps/api/app/settings_vault/service.py`).
- **Verdict:** Payout provider credentials go here as new `SecretSpec` entries.

### 1.9 Campaign engine (REUSE)
- `campaigns` → `campaign_steps` (position, channel, template/asset, `delay_hours`, `trigger_event`) → `campaign_enrollments`. Worker advances enrollments.
- **Verdict:** Partner outreach sequences (onboarding drips, re-engagement) can ride this engine directly. No new tables.

### 1.10 Billing / order system (DOES NOT EXIST — biggest gap)
- **There is no billing, no orders, no products, no Stripe integration anywhere in the repo.** Grep for `stripe|billing|order` in routers hits only unrelated words.
- The spec assumes "billing webhook verification and deduplication" and "server-side conversion ingestion from the billing or order system" as a P2 dependency.
- **This is the #1 architectural decision** (see §4, risk #1). Without an order source of truth, the commission engine has nothing honest to compute from.

---

## 2. Reuse vs. build matrix

| Spec requirement | Status | Reuse / Build |
|---|---|---|
| Tenant isolation (`business_id` scoping) | Exists | **Reuse** — `scoped()`, `get_owned_or_404()` |
| Internal roles (owner/admin/member) | Exists | **Reuse** — `require_role()` |
| Partner/external identity | Missing | **Build** — new `partner_users` table + portal auth |
| Finance/partner-manager roles | Missing | **Build** — extend `UserRole` or route-level guards |
| Referral link tracking (short links, clicks) | Pattern exists (outbound) | **Build new inbound tables**, reuse redirect pattern |
| Attribution (first/last touch, windows, conflicts) | Missing | **Build** — `partner_touchpoints` + attribution service |
| Commission plans (versioned, multi-type) | Missing (`default_commission_pct` only) | **Build** — `commission_plans`, `commission_rules` |
| **Commission ledger (immutable)** | **Missing** (earnings aggregated from events on the fly) | **Build** — `commission_ledger` with idempotency keys |
| Conversion ingestion (server-side, signed) | Pattern exists (alpha HMAC) | **Build** new endpoint, reuse HMAC pattern |
| Billing/order source of truth | **Missing entirely** | **Build or integrate** — decision required (§4.1) |
| Payout adapter + reconciliation | Missing | **Build** — `payouts`, `payout_items`, adapter interface |
| Partner portal (external UI) | Missing | **Build** — new frontend routes + simplified nav |
| Partner CRM (profiles, tiers, tasks) | Missing | **Build** — `partners`, `partner_tiers`, notes/tasks |
| Applications/onboarding | Missing | **Build** — `partner_applications` + approval flow |
| Marketing asset library for partners | Partial (assets exist, internal) | **Extend** — shareable asset views scoped to partners |
| Analytics dashboards | Partial (analytics router exists) | **Extend** — partner-specific aggregations |
| AI copilot (insights, Q&A, drafts) | Partial (Draven + 67 tools) | **Extend** — new `draven.partner_*` tools |
| Approval queues | Pattern exists | **Reuse pattern** — new approval kinds |
| Encrypted credential storage | Exists | **Reuse** — new `SecretSpec` entries |
| Outreach sequences | Exists (campaign engine) | **Reuse as-is** |

---

## 3. File-level implementation plan

### P1 — Foundation (migrations, auth, service boundaries)

**New migration `0011_growth_engine_foundation.py`:**
- `partner_users` — id, business_id FK, email (unique per business), password_hash, display_name, partner_id FK nullable (linked after approval), is_active, created_at. *Separate from `users` — external identity.*
- `partners` — id, business_id FK, kind (`customer_referrer | affiliate | agency`), status (`pending | active | suspended | rejected`), tier_id FK nullable, contact info, audience/channels/geography JSONB, manager_user_id FK nullable, contract_status, notes, created_at.
- `partner_applications` — id, business_id FK, partner_id FK nullable, kind, form_data JSONB, status (`pending | approved | rejected`), reviewed_by FK nullable, reviewed_at, created_at.
- `partner_tiers` — id, business_id FK, name, rank, description, created_at.

**API:**
- `apps/api/app/routers/partners.py` — tenant-scoped CRUD for partners/tiers/applications; application review (approve → creates partner + optional partner_user invite).
- `apps/api/app/routers/partner_portal.py` — separate router with **partner JWT** auth (not business JWT): profile, links, earnings, payouts. Must not reuse `CurrentUser`.
- `apps/api/app/core/partner_deps.py` — `CurrentPartner` dependency, partner token issue/verify.

**Packages:**
- `packages/forge-db/forge_db/models.py` — new models (in migration + model definitions).

**Backlog:**
1. Extend `UserRole` with `finance` (or document mapping finance→admin).
2. Partner JWT mint/verify (reuse `draven_crypto` Fernet patterns or python-jose already in deps).
3. Tenant-isolation tests for all new tables (copy `test_affiliates.py` conventions).

### P2 — Enrollment & attribution

**New migration `0012_growth_attribution.py`:**
- `referral_links` — id, business_id FK, partner_id FK, code (unique per business, human-readable), destination_url, utm_* fields, is_active, created_at. **Do not reuse `affiliate_links`.**
- `partner_touchpoints` — id, business_id FK, referral_link_id FK, partner_id FK, contact_id FK nullable, fingerprint_hash (cookie-less dedup), utm_* , ip_hash, user_agent_hash, created_at. Indexed on (business_id, created_at).
- `attribution_windows` — per-business config: policy (`first_touch | last_touch`), window_days, stored on new `partner_program_settings` table (or extend autopilot_settings — prefer new table).

**API:**
- `apps/api/app/routers/partner_tracking.py` — `GET /p/{code}` public redirect (copy `/r/{slug}` pattern: 302 + touchpoint row, no auth, 404-safe).
- Attribution service: `apps/api/app/growth/attribution.py` — pure functions: `attribute(conversion, touchpoints, policy, window) → partner_id | None + reason`. Must return the *reason* (explainable attribution per spec).
- Conversion ingest: `POST /api/v1/partner-conversions` — HMAC-signed (copy alpha `verify_webhook_signature`), **idempotency key required** (new), payload: order_id, order_value, currency, customer ref.

**Worker:**
- `apps/worker/worker/jobs.py` — `partner_attribution_sweep` (cron): match unattributed conversions to touchpoints within window.

**Backlog:**
1. Decide the order source (§4.1) — blocks honest conversion values.
2. Attribution conflict-resolution rules + audit trail (store `attribution_decisions` rows).

### P3 — Commissions & financial integrity (the ledger)

**New migration `0013_growth_ledger.py`:**
- `commission_plans` — id, business_id FK, name, version, status (`draft | active | archived`), plan_type (`fixed | percentage | recurring | bonus | milestone`), config JSONB (rates, caps, holding_days, eligible products), effective_from/to, created_at. **Versioned: never update an active plan; create a new version.**
- `commission_ledger` — id, business_id FK, partner_id FK, plan_id FK, plan_version, conversion_ref (idempotency key, **unique per business**), amount (Numeric(12,2)), currency, status (`pending | approved | payable | paid | reversed`), holding_until, created_at. **Append-only: reversals are new rows with negative amounts, never updates.**
- `commission_reversals` — id, ledger_entry_id FK, reason, created_by, created_at.
- `payouts` — id, business_id FK, partner_id FK, period_start/end, total_amount, currency, status (`draft | approved | processing | completed | failed`), provider_ref nullable, created_at.
- `payout_items` — id, payout_id FK, ledger_entry_id FK (unique — a ledger entry pays out at most once).

**API:**
- `apps/api/app/routers/commissions.py` — plans CRUD (versioned), ledger read (immutable — no PATCH/DELETE), approve/reject queue (reuse approval pattern), payout draft/approve/release.
- `apps/api/app/growth/commissioning.py` — **deterministic** calculation engine: pure functions `calculate(plan, conversion) → amount`. No LLM anywhere in this path.

**Worker:**
- `partner_commission_calc` — cron: for each approved conversion without a ledger entry, run deterministic calc → insert ledger row (idempotent on conversion_ref).
- `partner_payout_process` — payout adapter interface (`apps/api/app/growth/payouts.py`: `PayoutAdapter` protocol; stub adapter writes to `dev_outbox` like `forge-channels` stubs).

**Backlog:**
1. Financial invariant tests: no duplicate ledger rows for one conversion_ref; sum(ledger) == sum(payout_items) + payable; reversal nets to zero.
2. Concurrency tests: two workers calculating the same conversion → exactly one ledger row (unique constraint as backstop).
3. Payout credentials → `settings_vault` `SecretSpec` entries.

### P4 — Partner management & analytics

**API:**
- Extend `partners.py`: notes, tasks, manager assignment, tier changes (audited).
- `apps/api/app/routers/partner_analytics.py` — clicks, conversions, revenue, commission liability, activation/retention. **Every metric defines numerator/denominator/date basis** (spec §E). Reuse `analytics.py` aggregation patterns.

**Frontend** (`apps/web/src/`):
- `pages/Partners.tsx` (+ route `/partners`) — admin: applications queue, partner list, profiles, tiers.
- `pages/PartnerPortal.tsx` (+ route `/portal`, separate layout, no admin nav) — partner-facing: overview, links, earnings, payouts. **Simplest nav per spec.**
- `pages/Commissions.tsx` (+ route `/commissions`) — plans, ledger (read-only table), approvals, payouts.
- `lib/api.ts` — `partnersApi`, `commissionsApi`, `partnerPortalApi` clients.

### P5 — AI partner intelligence (Draven tools)

- `apps/api/app/draven_tools_partners.py` (new module, registered like `draven_tools_parity.py`):
  - `draven.partner_top_earners` (read-only, trusted metrics from ledger)
  - `draven.partner_needs_onboarding` (read-only)
  - `draven.partner_draft_outreach` (draft only — campaign engine, needs approval)
  - `draven.partner_risk_flags` (read-only anomaly list)
- **AI operating contract enforcement:** tools are read-only or draft-only; commission approval, term changes, and payout release are **never** Draven tools (human-only routes). Model version + approver recorded on consequential actions (extend `DravenToolRun` or approval rows).

### P6 — Hardening & launch

- E2E: enrollment → click → conversion → attribution → ledger → approval → payout (automated suite, copy `test_alpha.py` E2E style).
- Load test on `/p/{code}` redirect + conversion ingest.
- Tenant-isolation fuzz on partner portal auth.
- Reconciliation drill: ledger vs. payout provider statement.

---

## 4. Risks

### Risk 1 — No billing/order source of truth (BLOCKER for P2/P3)
ForgeOS has no products, orders, or payment processing. The spec's commission engine is predicated on "server-side conversion ingestion from the billing or order system." Without it, `order_value_usd` on conversions is self-reported by whoever calls the endpoint — **commissions computed from self-reported values are not honest**.

Options:
- **A. Stripe integration** (recommended): add `POST /webhooks/stripe` (signature-verified) → creates `partner_conversions` from real charges. Scoped, honest, standard.
- **B. Minimal order-intent API**: ForgeOS gains a tiny `orders` table + API for recording sales; partners' customers check out through it. More build, more ownership.
- **C. Manual conversion entry**: admin enters conversions by hand. Honest but not scalable; acceptable for pilot only.

**Recommendation:** Decide before P2 starts. P1 (partners, portal, links) can proceed without it; P2 attribution and P3 ledger cannot be *honest* without it.

### Risk 2 — Current conversion ingest has no idempotency (financial correctness)
`POST /affiliates/conversions` writes an `affiliate_converted` event with **no idempotency key**. A retried postback double-counts earnings. The new `commission_ledger` must have a **unique constraint on (business_id, conversion_ref)** and the ingest endpoint must require an idempotency key — copy the alpha module's idempotent-replay pattern, not the affiliates pattern.

### Risk 3 — Events table is not a ledger (do not build financials on it)
`events.payload` is mutable JSONB with no balance invariants. The spec's "one source of financial truth" requires the new append-only `commission_ledger` with:
- `Numeric(12,2)` amounts (never float — the current earnings code does `float(commission)`; the ledger must not),
- unique `(business_id, conversion_ref)`,
- reversals as new negative rows,
- no UPDATE/DELETE API surface on ledger rows.

### Risk 4 — Partner identity vs. User identity (tenant isolation)
Giving partners `users` rows would expose the business (all `scoped()` queries trust `business_id` from JWT). The partner portal needs its own token type whose queries scope by `partner_id` **and** `business_id`, with an explicit allowlist of tables. A confused-deputy bug here leaks one business's partners/revenue to another's partners. Test with cross-business partner tokens.

### Risk 5 — Scope: the spec is a 12-week team plan; the repo is a solo-dev + agents shop
The spec assumes "2–4 person product team" and sequential phases. Realistic MVP for ForgeOS: **P1 (partners + portal auth) → P2-lite (links + manual conversion entry, option C) → P3-lite (ledger + approval queue, payouts manual/CSV export)**. AI copilot (P5) and automated payouts can follow. Do not start P5 before the ledger invariants (P3) are tested — the spec itself says AI must not touch financial outcomes.

---

## 5. Prioritized backlog (engineering order)

1. **[P1]** Migration 0011: `partner_users`, `partners`, `partner_applications`, `partner_tiers`.
2. **[P1]** `partner_deps.py` (partner JWT) + `routers/partners.py` + `routers/partner_portal.py`.
3. **[P1]** Decide order source (Risk 1) — blocks P2/P3 honesty.
4. **[P2]** Migration 0012: `referral_links`, `partner_touchpoints`, program settings.
5. **[P2]** `GET /p/{code}` redirect + HMAC conversion ingest with idempotency keys.
6. **[P2]** `growth/attribution.py` (pure, explainable) + `attribution_decisions` audit.
7. **[P3]** Migration 0013: `commission_plans` (versioned), `commission_ledger` (append-only, unique conversion_ref), `payouts`, `payout_items`.
8. **[P3]** `growth/commissioning.py` (deterministic, no LLM) + `partner_commission_calc` worker job.
9. **[P3]** Commission approval queue + payout adapter (stub → `dev_outbox`).
10. **[P3]** Financial invariant + concurrency tests.
11. **[P4]** Frontend: `/partners`, `/commissions`, `/portal` + API clients.
12. **[P5]** `draven_tools_partners.py` (read-only + draft-only tools; never approvals/payouts).
13. **[P6]** E2E suite, load tests, reconciliation drill, pilot.

---

## 6. What explicitly NOT to build (avoid duplicates)

- Do **not** reuse `affiliate_programs`/`affiliate_links` for inbound partners — new tables.
- Do **not** put financial state in the `events` table — new ledger.
- Do **not** give partners `users` rows — separate identity.
- Do **not** put LLM calls in the commission calculation path — deterministic code only.
- Do **not** build a second campaign engine — reuse `campaigns`/`campaign_steps` for partner outreach.
- Do **not** build a second approval system — reuse the asset-approval state-machine pattern.
- Do **not** build a second secrets store — extend `settings_vault` `KNOWN_SECRETS`.
