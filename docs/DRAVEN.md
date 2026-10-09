# DRAVEN — Build Checkpoint (2026-10-09)

> DRAVEN is the AI operator inside ForgeOS: a voice-first assistant that can
> actually *do* things in the business — read live state, execute low-risk
> actions immediately, and request approval before anything risky. Built as
> the honest foundation slice of a 14-phase implementation program.

## What shipped

### Backend (`apps/api/app/`)

**`routers/draven.py`** — the `/draven` API surface:
- `POST /draven/chat` — `{message, history}` → `{reply, tools_used[], approvals_needed[], estimated_cost_usd, provider}`. A real keyword intent router (approval-status, campaign-status, analytics, contacts, autopilot) maps messages to typed tools, executes them tenant-scoped, and returns human-readable replies. Every run is audit-logged. Unknown intent → no-op tools, safe fallback text. Per-request timeout + max message length.
- `GET /draven/tools` — the typed tool registry (**67 tools**): the original 9 (`draven.business_summary`, `draven.approvals_pending`, `draven.assets_pending_review`, `draven.campaigns_status`, `draven.analytics_summary`, `draven.contacts_search`, `draven.autopilot_status`, `draven.campaign_pause`, `draven.asset_approve`), 3 market-intel tools, 5 alpha workflow tools, and 50 voice-parity tools covering every app capability (see "Voice parity" below). Medium/high-risk tools are **approval-gated** — they return a structured approval request and never execute.
- `GET /draven/provider` / `PUT /draven/provider` (admin) / `POST /draven/provider/test` (admin) — chat LLM provider config: `stub` | `anthropic` | `openai-compatible`. Keys Fernet-encrypted at rest (`DRAVEN_CONFIG_KEY`), fail-closed 400 when missing. Secrets never returned in any response or log.
- `GET /draven/audit?limit` — the tool-run audit log (execution timeline source).
- `POST /draven/stop` — sets the session stop flag the frontend checks between messages (best-effort; real in-flight cancellation is out of scope for the stub provider).
- `GET /draven/tts/voices` — ElevenLabs voice library (live from `api.elevenlabs.io`, cached per business 1h, 15s timeout). Unconfigured → `{configured: false}`. Upstream failure → 502, never a fabricated list.
- `POST /draven/tts/speak` — server-side TTS: `{text (1–2000 chars), voice_id, model_id=default eleven_multilingual_v2}` → `audio/mpeg`. Host hard-allowlisted to `api.elevenlabs.io` (SSRF-safe), strict voice-ID format + membership validation, 20 req/min per-business rate limit, per-call audit row with character count + estimated cost (ElevenLabs published rates, marked as estimates). Raw text is **not** stored in audit.

**`draven_tools.py`** — the typed tool registry: each tool declares ID, description, args schema, risk level (`low` = execute immediately, `medium`/`high` = approval-gated, never execute), and a tenant-scoped executor. Every query filters by the caller's `business_id` — no exceptions. (The 50 voice-parity tools live in `draven_tools_parity.py`, imported last by `draven_tools.py` into the same `TOOLS` registry — one module was approaching the GitHub push single-file size cap.)

**`draven_crypto.py`** — Fernet encrypt/decrypt helpers for provider credentials.

**Migrations** (`packages/forge-db/alembic/versions/`):
- `0004_draven.py` — `draven_tool_runs` (audit) + `draven_provider_config` tables.
- `0005_draven_tts.py` — `tts_provider` + `tts_api_key_enc` columns.
- Single chain `0001 → 0005` verified with `alembic heads`.

### Frontend (`apps/web/src/`)

**`pages/Draven.tsx`** — the Draven workspace (`/draven`, nav "Draven ⬢"):
- Mic button (◉ live / ◈ idle / amber pulse while speaking), interrupt button, spoken-reply toggle with live rate slider (voice speed, 0.5–1.5×).
- Tool-run chips under every reply (tool ID + status + duration), expandable to full JSON results.
- Execution timeline (audit log feed, auto-refresh after each message).
- Approvals queue (asset approvals flow from the campaign asset review work).
- Provider status pill, session cost accumulator, ⏹ Stop-all button.
- **Voice engine selector: Browser ↔ ElevenLabs.** When ElevenLabs is configured, replies route through `/draven/tts/speak` and play as audio; voice picker dropdown lists the live ElevenLabs voice library (name + language). API keys never touch the browser.
- Admin provider panel: stub / anthropic / openai-compatible / elevenlabs (TTS), base URL + API key fields, test-connection button. API-key capture for ElevenLabs goes through the secure admin flow.

**Supporting changes**: `lib/api.ts` — `templateApi` CRUD + preview; `apiFetch` gained `signal` support; `apiUrl()` helper for binary fetches. `lib/voice.ts` — `useVoice` gained `rate`. Route `/assistant` → `/draven`; nav updated. `pages/Templates.tsx` — full Templates section (the niche template packs had no UI; built it: niche chips, channel filter, search, live variable preview, create/delete).

### Tests
- `apps/api/tests/test_draven.py` — **44 passed** (tool/provider/audit + TTS with mocked ElevenLabs HTTP).
- `apps/api/tests/test_draven_parity.py` — **18 passed**: the 50 parity tools — real execution, tenant isolation across two businesses, approval gating for every medium/high-risk tool (no side effects), intent-routing spot checks, deterministic replies built from real tool outputs.
- Full suites green at ship time: API 82, worker 66, forge-llm 35, forge-channels 39. No regressions, no disabled tests.

### Verified live (sprite, 2026-10-09)
- Migrations applied (`0003_affiliates → 0004_draven → 0005_draven_tts`).
- `GET /draven/tools` → 67 tools. `POST /draven/chat` (intent "what needs my approval?") → executed `draven.approvals_pending`, returned real backend state, wrote audit row.
- New frontend bundle serving; `/draven/tools` responds through the public URL.

## Deliberate limitations (documented in code, not hidden)
- Chat uses a keyword intent router against the stub provider — it routes reliably, it is not an LLM agent. Swapping in a real provider only needs the provider config + a streaming adapter.
- The ML training, PQC, and security certifications from the program brief were **not** built — those claims would be fiction.
- Voice cache + rate-limiter bucket are in-process; multi-worker deployments should move them to Redis (noted in comments).
- `POST /draven/stop` sets a session flag; it does not cancel an in-flight LLM call (noted in the endpoint docs).
- TTS cost rates are estimates from ElevenLabs' published API rate card, not metered billing.

## 12-agent swarm (2026-10-09)

**`apps/api/app/draven_swarm.py`** — real multi-agent orchestration, not a simulation:
- **Agent registry**: 12 typed agents, each with id, name, role, system-prompt template, least-privilege tool subset (from the existing `draven_tools` registry — validated at import), and max delegation depth: `supervisor` (orchestrator), `researcher` (market intel), `brand_guardian` (voice consistency), `copywriter`, `channel_adapter` (email/SMS/social), `scheduler`, `analyst`, `optimizer`, `compliance` (guardrails), `outreach` (lead follow-up), `onboarder`, `asset_reviewer`.
- **Orchestrator**: decomposes a goal (LLM-based when a live provider is configured, deterministic heuristic fallback otherwise), fans out independent subtasks concurrently via `asyncio.gather`, runs dependent levels sequentially (topological levels), and synthesizes a deterministic result from real agent outputs.
- **Safety inheritance**: high-risk tools still return `approval_required` when called by an agent — the asset reviewer *proposes* approvals; it can never approve. Every tool call writes a `draven_tool_runs` row attributed with `agent_id` / `swarm_run_id`.
- **Honest degradation**: on the stub provider, agents run their real read tools and mark generative steps `degraded` with a plain-language note — no output is ever invented. Unconfigured/misconfigured provider → the run fails with "LLM provider misconfigured: …", never fake activity.
- **Stage-robust**: per-agent timeout (60s), per-tool timeout (30s), overall run ceiling (5 min); every failure path marks the run `failed` with an error instead of hanging.

**API surface** (`routers/draven.py`):
- `GET /draven/swarm/agents` — the 12 agent definitions.
- `POST /draven/swarm/run` `{goal, context, demo}` → `202 {run_id, status}` (background task with its own sessions; honors the emergency stop).
- `GET /draven/swarm/runs/{run_id}` — status, current phase, per-agent results, synthesized summary.
- `GET /draven/swarm/runs/{run_id}/events?after=&limit=` — append-only live event feed (`agent_start` / `tool_call` / `agent_done` / `error` / `run_done`), written to `draven_swarm_events` as things happen.

**Migrations** (`0009_draven_swarm`): `draven_swarm_runs` + `draven_swarm_events` tables; `draven_tool_runs.agent_id` + `swarm_run_id` columns. Single chain verified with `alembic heads`.

**Frontend** (`pages/Swarm.tsx`, route `/swarm`, nav "Swarm ✦"): JARVIS cyan mission-control — agent cards with live running states (pulsing cyan, HUD corner brackets), real event feed polled every 1.5s, one-click **Run investor demo** (seeded scenario: "plan and draft this week's campaign"), custom goal input, stub-mode honesty banner, and a compact orb mirroring run state (processing → success/error).

**Tests**: `apps/api/tests/test_draven_swarm.py` — **9 passed** (registry integrity, decomposition levels, stub honest degradation, approval gating inherited, audit attribution, API flow with live events, tenant isolation, blank-goal rejection). Full API suite: **200 passed**.

## Orb — built on Three.js per the master spec (2026-10-09, rebuilt)

`components/JarvisOrb.tsx` implements the cinematic spec as a Three.js scene
graph (the previous raw-WebGL2 implementation was replaced outright, not
layered on):
- **Layers**: energy core (`THREE.SphereGeometry` + GLSL `ShaderMaterial` —
  domain-warped fbm smoke, ridged filament streams, fragmented fresnel rim,
  offset warm heart), two plasma fresnel shells, two wispy smoke coronas,
  three inclined orbital arc `THREE.Line` loops, GPU-orbit `THREE.Points`
  particle field, radial-gradient halo plane, contextual holo-marker arcs.
- **State machine**: the typed 9-state machine (`lib/orbState.ts`:
  idle/wake/listening/processing/speaking/interrupted/success/error/sleep,
  validated transitions, auto-advance) drives interpolated visual
  parameters every frame.
- **Audio-reactive**: the real `AudioAnalyzer` signal (`lib/audioAnalyzer.ts`)
  drives mic levels while listening and playback levels while speaking;
  state-driven shimmer when no source is attached. Nothing is faked.
- **Performance**: quality tiers (high/balanced/low) adapt pixel ratio, sphere
  tessellation, and particle counts for Android GPUs; render loop sleeps when
  the tab is backgrounded; `prefers-reduced-motion` renders one static frame.
- **No simplified fallback (per Michael's correction)**: WebGL is required.
  If the Three.js renderer cannot be created, the orb sets
  `dataset.orbStatus = "failed"` and logs honestly instead of rendering
  simplified visuals.
- **Reference image is law** (`~/workspace/user/files/4020_2_4ndm.jpg`): the
  orb is translucent cyan smoke, NOT a solid ball — dark near-black
  see-through center, no glossy highlight, no rings at rest (rings appear
  only as faint processing indicators), thin additive cyan wisps, a faint
  warm amber inner glow offset from center, and a ragged silhouette. The
  reference governs the LOOK; the JARVIS prompt governs the TECH
  (Three.js/GLSL, audio-reactive state machine).

## Voice defaults (2026-10-09, Michael's calls)
- **Engine**: ElevenLabs auto-loads as the voice engine whenever configured (checked on workspace load); browser voice is only the fallback. A deliberate manual switch is persisted in `localStorage` (`forgeos.voiceEngine`) and wins over the auto-default. Unconfigured → subtle hint to connect ElevenLabs in Settings → Voice.
- **Format**: MP3 HD 192kbps is the default everywhere (backend `SpeakIn`, frontend selector, docs) — WAV confused some customer devices. Lossless WAV 44.1kHz and MP3 128kbps remain as selector options.

## Assistant models: Draven vs Calcifer (2026-10-09)
- **Picker**: Settings → Assistant model (`forgeos.assistantModel` = `"draven" | "calcifer"`, device-local localStorage). The chat API takes a `persona` field (`POST /draven/chat`); the assistant names and introduces itself accordingly. Same tools, same safety, same audit — only identity/persona changes.
- **Draven**: the Three.js cinematic orb (above) in JARVIS cyan.
- **Calcifer**: Michael's exact reference image (`apps/web/src/assets/calcifer.jpg`, bundled into the build) with a talking-mouth overlay driven by the REAL TTS audio signal through the shared `AudioAnalyzer` (mouth opens/closes as he speaks; hidden at idle so the painted smile shows), plus gentle bob/flicker/breathing idle life.
- **IP flag**: Calcifer is a Studio Ghibli character. Fine for Michael's personal build; it needs original art before any customer-facing use.
- **Manners**: `client_tz` sent with each chat request drives time-aware greetings (Good morning/afternoon/evening); polished courteous tone in both personas.
- **Slot-filling**: `app/draven_conversation.py` — when the AI asks for missing info, the next message is treated as the answer and the task continues; "cancel"/"never mind" or a new command clears the pending slot.

## Voice parity (2026-10-09) — DONE

The voice AI can now do anything the app can do, conversationally. 50 tools
added to the typed registry in `apps/api/app/draven_tools.py`, reusing the
routers' own logic (template Jinja renderer + syntax check, asset approval
state machine, affiliate slug/URL validators, earnings aggregation, ops
activity/brain aggregates, analytics funnel) instead of duplicating it. Every
tool is tenant-scoped; every invocation is audit-logged. `route_intent` grew
keyword rules for all of them; `_deterministic_reply` summarizes every new
tool output from real data.

| Area | Tools (risk) |
|---|---|
| Brand kits | `brandkit_list`, `brandkit_get`, `brandkit_create`, `brandkit_update` (low) |
| Contacts | `contacts_list`, `contact_get`, `contact_create`, `contact_update`, `contact_consent` (low); `contact_delete` (**high**) |
| Templates | `template_list`, `template_get`, `template_create`, `template_update`, `template_preview` (low); `template_delete` (**high**) |
| Assets | `assets_list`, `asset_get`, `asset_submit` (draft→in_review via the real state machine), `asset_versions` (low); `asset_generate` (**medium** — validates, then stages an approval: generation bills the LLM provider, so it never enqueues from voice); `asset_reject` (**high**) |
| Campaigns | `campaign_get`, `campaign_create` (draft), `campaign_update`, `campaign_steps_add`, `campaign_step_update`, `campaign_enrollments` (low); `campaign_launch` (**high** — validates launch-readiness, then approval) |
| Autopilot | `autopilot_update` (low — auto-approve, channel approvals, daily cap, quiet hours); `autopilot_plan_approve` (**medium** — validates the draft plan, then stages an approval: approving materializes a scheduled campaign, so it never executes from voice) |
| Affiliates | `affiliate_programs_list/get/create/update`, `affiliate_links_list/get/create/update`, `affiliate_earnings` (low, aggregated honestly from stored events); `affiliate_program_delete` (cascades), `affiliate_link_delete` (**high**) |
| Analytics | `analytics_funnel`, `analytics_weekly_summary` (low) |
| Interview | `interview_start`, `interview_status` (low) |
| Ops | `ops_activity`, `ops_brain` (low, read-only — same aggregates as `/ops` and `/brain`) |
| Business | `business_get`, `business_update` (low) |

Deliberately not exposed via voice: `auth` (sign-in is not a voice action),
`settings` (the secrets vault — API keys are never read or written through
chat), `webhooks` (external delivery callbacks), `events` ingest and the dev
outbox (covered conversationally through campaigns/analytics/ops instead).

## What's still ahead
- **ML pipeline** (training/fine-tuning loops) — not built; would be fiction to claim.
- **Security hardening program** (PQC, NIST/OWASP certifications, security sweeps) — not built.
- **Autopilot per-tenant scheduling + run-now** — in progress (separate workstream): `autopilot_settings.plan_day/plan_hour/plan_cadence`, 15-minute engine tick (`cron(autopilot_plan, minute={0,15,30,45})`), `POST /autopilot/plan/run-now`.

## Env
- `DRAVEN_CONFIG_KEY` — Fernet key for provider credential encryption (required; fail-closed without it).
- `LLM_PROVIDER=stub` currently; admin panel switches to anthropic/openai-compatible at runtime.
