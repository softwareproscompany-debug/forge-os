# MAPS × ForgeOS — how the four layers map onto this system

ForgeOS implements the MAPS pattern (Memory, Agent, Pulse, Screen) as four
cooperating layers. This document maps each layer onto the modules, tables,
jobs, and routes that actually exist in this repo — no renames, no new
product surface, just the architecture made explicit.

Authoritative contracts: [`CONTRACTS.md`](../CONTRACTS.md).
System design: [`ARCHITECTURE.md`](../ARCHITECTURE.md).

## M — Memory

**What it is here:** everything the business has taught the system, persisted
in Postgres and retrievable per tenant.

| Memory kind | Where it lives | Written by |
|---|---|---|
| Brand identity (voice, tone, ICP, do/don't lists) | `brand_kits` (versioned) | Card 0 interview, onboarding UI |
| Interview Q&A history | `interview_sessions.answers` | `POST /interview/{id}/answer` |
| Audience (contacts, tags, consent, custom fields) | `contacts` | API, `contact_added` events |
| Content library + approval audit trail | `assets`, `asset_approvals` | generation job, approvals inbox |
| Prompt templates | `templates` | API |
| Performance record | `sends`, `events`, `weekly_summaries` | worker jobs, webhooks |

Rules the Memory layer follows:

- Tenant-scoped on every read (`business_id` from the JWT) — one business
  can never see another's memory.
- Append-mostly: new versions and new rows, not destructive edits
  (`brand_kits.version`, asset lineage via `parent_asset_id`).
- No duplicates: unique slugs, unique `(business_id, week_start)` on weekly
  summaries, idempotent seed.

## A — Agent

**What it is here:** the arq worker jobs plus the autopilot planner — the
parts of the system that *do* things on the business's behalf.

| Agent capability | Implementation | Trigger |
|---|---|---|
| Draft marketing copy from brand memory | `generate_asset` | `POST /assets/generate`, autopilot |
| Send across email/SMS/social with consent gates | `send_message` | `campaign_tick`, event triggers |
| Advance campaigns, enroll contacts | `campaign_tick` | arq cron, every 60s |
| React to inbound events | `handle_event` | `POST /events`, delivery webhooks |
| Summarize the week into evidence | `weekly_summary` | hourly cron, self-gates to Sunday 23:xx business-local |
| Draft next week's content plan | `autopilot_plan` | hourly cron, self-gates to Monday 06:xx business-local |

Agent rules, enforced in code rather than promised in docs:

- Every job is idempotent and re-runnable (the DB is the source of truth;
  Redis holds only transient queue state).
- Guardrails run on every generation (`check_guardrails`); violations block
  or flag before anything reaches a human or a contact.
- Consent is a hard gate in `send_message` — email needs `consent_email`,
  SMS needs `consent_sms`, `unsubscribed` blocks everything.
- The agent never invents telemetry: statuses come from real `sends` rows,
  costs from real `generation_logs` rows.

## P — Pulse

**What it is here:** the system's heartbeat — scheduled routines and the live
readouts that prove the agent layer is running.

- **Crons**: `campaign_tick` (60s), `weekly_summary` and `autopilot_plan`
  (hourly wakes with per-business timezone gating — one daily cron cannot
  honor per-business timezones).
- **Live activity**: `GET /api/v1/ops/activity` merges `generation_logs`,
  `sends`, and `events` into a single newest-first feed with per-stage
  counts and an `as_of` timestamp; the `/ops` page polls it every 5s.
- **Knowledge graph**: `GET /api/v1/ops/brain` renders brand kits → assets →
  campaigns → sends → events as one JSON document; the `/brain` page draws
  four views (rings, links, timeline, areas) from that single source.

Pulse rules:

- Read-only. Neither endpoint writes; dashboards display, they don't store.
- Every panel carries its own data timestamp (`as_of`); stale data is
  visible as stale, never presented as live.
- Poll failures keep the last good snapshot — a failed refresh degrades to
  "last known", never to a blank screen or fabricated zeros.

## S — Screen

**What it is here:** the React SPA — the only layer the user touches.

- Routes: `/` dashboard, `/ops` mission control, `/brain` knowledge graph,
  `/interview` guided interview, `/autopilot` plan review, plus campaigns,
  assets, approvals, calendar, analytics, outbox, onboarding.
- The Screen layer holds no business state of its own: auth token in
  `localStorage`, everything else fetched from the API per view.
- Loading, empty, error, and success states are first-class — every route
  handles all four; nothing renders a dead button or a fake number.

## How the layers talk

```
Screen ──HTTP──▶ API ──enqueue──▶ Agent (arq/Redis) ──writes──▶ Memory (Postgres)
  ▲                 │                                              │
  │                 └────────────── reads ─────────────────────────┘
  └──── polls Pulse endpoints (/ops/activity, /ops/brain) ─────────┘
```

Memory never calls the Agent. The Agent never renders the Screen. Pulse
only reads. That one-way discipline is what keeps the system debuggable:
when something looks wrong on Screen, the cause is always traceable down
through Pulse → Agent → Memory, never sideways.

## FORGE layers × MAPS — affiliate (Reach addition)

The FORGE framework (README) names five business layers; affiliate
marketing lands in **Reach** with a foot in **Growth**:

- **Reach** — affiliate links are a new distribution channel alongside
  email/SMS/social: trackable short links (`GET /r/{slug}`, public 302
  with click tracking), programs + links managed in the `/affiliates` UI,
  and earnings computed honestly from stored events. Stubs-first like the
  other channels: the whole loop works with zero network keys.
- **Growth** — weekly affiliate evidence (`affiliate_top_links`,
  `affiliate_earnings_usd` on `weekly_summaries`) feeds back into
  Origination: the generation brief names the top-converting offer so the
  business makes more of what already earns.
- **Guardrail (Agent-side)** — affiliate content ships with an FTC
  disclosure. The `is_affiliate_content` flag (API-set or auto-detected)
  triggers `check_guardrails` disclosure matching, and `generate_asset`
  auto-appends the disclosure rather than blocking — same pattern as the
  email-unsubscribe rule.
