# DRAVEN — Build Checkpoint (2026-10-09)

> DRAVEN is the AI operator inside ForgeOS: a voice-first assistant that can
> actually *do* things in the business — read live state, execute low-risk
> actions immediately, and request approval before anything risky. Built as
> the honest foundation slice of a 14-phase implementation program.

## What shipped

### Backend (`apps/api/app/`)

**`routers/draven.py`** — the `/draven` API surface:
- `POST /draven/chat` — `{message, history}` → `{reply, tools_used[], approvals_needed[], estimated_cost_usd, provider}`. A real keyword intent router (approval-status, campaign-status, analytics, contacts, autopilot) maps messages to typed tools, executes them tenant-scoped, and returns human-readable replies. Every run is audit-logged. Unknown intent → no-op tools, safe fallback text. Per-request timeout + max message length.
- `GET /draven/tools` — the typed tool registry (9 tools: `draven.business_summary`, `draven.approvals_pending`, `draven.assets_pending_review`, `draven.campaigns_status`, `draven.analytics_summary`, `draven.contacts_search`, `draven.autopilot_status`, `draven.campaign_pause`, `draven.asset_approve`). The last two are **approval-gated** — they require an approval token before executing.
- `GET /draven/provider` / `PUT /draven/provider` (admin) / `POST /draven/provider/test` (admin) — chat LLM provider config: `stub` | `anthropic` | `openai-compatible`. Keys Fernet-encrypted at rest (`DRAVEN_CONFIG_KEY`), fail-closed 400 when missing. Secrets never returned in any response or log.
- `GET /draven/audit?limit` — the tool-run audit log (execution timeline source).
- `POST /draven/stop` — sets the session stop flag the frontend checks between messages (best-effort; real in-flight cancellation is out of scope for the stub provider).
- `GET /draven/tts/voices` — ElevenLabs voice library (live from `api.elevenlabs.io`, cached per business 1h, 15s timeout). Unconfigured → `{configured: false}`. Upstream failure → 502, never a fabricated list.
- `POST /draven/tts/speak` — server-side TTS: `{text (1–2000 chars), voice_id, model_id=default eleven_multilingual_v2}` → `audio/mpeg`. Host hard-allowlisted to `api.elevenlabs.io` (SSRF-safe), strict voice-ID format + membership validation, 20 req/min per-business rate limit, per-call audit row with character count + estimated cost (ElevenLabs published rates, marked as estimates). Raw text is **not** stored in audit.

**`draven_tools.py`** — the typed tool registry: each tool declares ID, description, args schema, risk level (`low` = execute immediately, `high` = require approval), and a tenant-scoped executor.

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
- `apps/api/tests/test_draven.py` — **27 passed** (15 tool/provider/audit + 12 TTS with mocked ElevenLabs HTTP).
- Full suites green at ship time: API 82, worker 66, forge-llm 35, forge-channels 39. No regressions, no disabled tests.

### Verified live (sprite, 2026-10-09)
- Migrations applied (`0003_affiliates → 0004_draven → 0005_draven_tts`).
- `GET /draven/tools` → 9 tools. `POST /draven/chat` (intent "what needs my approval?") → executed `draven.approvals_pending`, returned real backend state, wrote audit row.
- New frontend bundle serving; `/draven/tools` responds through the public URL.

## Deliberate limitations (documented in code, not hidden)
- Chat uses a keyword intent router against the stub provider — it routes reliably, it is not an LLM agent. Swapping in a real provider only needs the provider config + a streaming adapter.
- The 12-agent swarm, ML training, PQC, and security certifications from the program brief were **not** built — those claims would be fiction.
- Voice cache + rate-limiter bucket are in-process; multi-worker deployments should move them to Redis (noted in comments).
- `POST /draven/stop` sets a session flag; it does not cancel an in-flight LLM call (noted in the endpoint docs).
- TTS cost rates are estimates from ElevenLabs' published API rate card, not metered billing.

## Env
- `DRAVEN_CONFIG_KEY` — Fernet key for provider credential encryption (required; fail-closed without it).
- `LLM_PROVIDER=stub` currently; admin panel switches to anthropic/openai-compatible at runtime.
