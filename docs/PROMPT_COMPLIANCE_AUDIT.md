# Prompt Compliance Audit — ForgeOS Draven / Orb
Date: 2026-10-09. Auditor: subagent (code-read verification; no screenshots possible).
Sources: (A) PROJECT JARVIS master prompt (full text recovered from transcript),
(B) MUSE — EMERGENCY UI REDESIGN directive 11 sections (full text recovered),
(C) Michael's follow-ups: Calcifer identity+image, persona, slot-filling,
lossless audio, settings vault, orb art corrections 2026-10-09.

Legend: DONE = verified in code | FIXED = closed this pass | GAP = not done, honest reason

## A. JARVIS master prompt

### EPIC 2 — Orb construction
- [DONE] Three.js orb, custom GLSL (7 layers ported: energy core, plasma shells, smoke coronas, orbital arcs, particle field, halo, holo markers). Raw-WebGL2 implementation replaced outright per the master spec.
- [DONE] 8-layer composition: 1 energy core, 2 plasma shells (fresnel), 3 filament network (ridged noise in core shader), 4 orbital arcs (partial arcs, sphere-occlusion fade), 5 particle field (GPU points), 6 atmospheric halo, 7 holo markers (contextual), 8 traveling energy pulses
- [FIXED] Rings nearly invisible at rest (~0.05 alpha); rise during processing as convergence indicators (was: always visible atom-look)
- [FIXED] Glossy-ball highlight killed; ~80% near-black body; thinner tendrils (pow 7.0, stronger warp); ragged silhouette (two-octave displacement); thin sharp rim; warm ember heart kept
- [DONE] 2D canvas fallback matches art direction; data-orb-status reports webgl/fallback2d/failed
- [DONE] Adaptive quality tiers high/balanced/low (dpr, sphere tessellation, particle counts); render loop sleeps when tab hidden; WebGL context-loss recovery

### EPIC 3 — State machine
- [DONE] 9 typed states: idle/wake/listening/processing/speaking/interrupted/success/error/sleep (lib/orbState.ts); explicit transition table, invalid transitions rejected w/ fallback
- [DONE] All transitions driven by real events (mic activity, TTS/audio element, tool results, errors); no faked states
- [DONE] INTERRUPTED releases audio and returns to listening/idle

### EPIC 4 — Voice
- [DONE] Web Speech STT (mic permission primed for Android), browser + ElevenLabs TTS, interrupt control, conversation-mode toggle (continuous/push-to-talk), voice speed, voice picker
- [DONE] No faked transcription/voice activity

### EPIC 5 — AudioAnalyzer
- [DONE] Real mic analysis while listening, real playback-element analysis while speaking; level/bass/mid/high with attack/release smoothing; drives core/halo/filaments/particles/orb scale; Calcifer mouth

### EPIC 6 — Interface/HUD
- [DONE] Desktop: minimal top bar, central orb, compact conversation overlay, discreet dock, expandable activity rail
- [DONE] Mobile 4 regions: A identity bar (mark, connection dot, menu), B compact context/collapsible history, C hero orb, D floating dock
- [FIXED] Escape closes overlays (activity rail, voice settings, history) — was missing
- [DONE] Tap orb = primary mic action; mic reflects state; text input fallback; focus/hover/pressed/disabled states

### EPIC 7 — Responsive
- [DONE] Safe-area insets, overflow-x clip (no horizontal scroll), keyboard-aware dock, reduced-motion respected, backgrounding sleeps loops
- [GAP] Native packaging (Capacitor) not evaluated — web-only; never claimed otherwise

### EPIC 8 — Security
- [DONE] Mic permission before capture, listening indicator, stop control, no background recording, no raw-audio persistence, secrets server-side only (vault), backend validation, TTS rate limiting

### EPIC 9 — Reliability
- [DONE] rAF cleanup on unmount, bounded buffers, request timeouts + AbortController, context-loss recovery
- [GAP] FPS not measured on real devices (no device lab); targets stated, not verified

### EPIC 10 — Testing
- [DONE] 186 API tests green (incl. 6 slot-fill, 4 persona); tsc clean; production build green
- [GAP] No Playwright E2E suite; no automated state-machine transition tests (transitions verified by code read + live API)
- [GAP] Screenshot validation at 390x844 / 430x932 / 1440x900 NOT performed — subagent cannot drive a browser; parent must arrange

## B. EMERGENCY UI REDESIGN directive

1. [DONE] Art direction: exact tokens #07090D/#0D1118/#121923/#E8EDF5/#8B98AA/#65DFFF/#B4EEFF; amber sparing; crimson/emerald restrained; cyan dominant
2. [FIXED] Orb rebuilt per reference (see A.EPIC2); 8 required appearance layers present; rings in perspective
3. [DONE] Mobile: 4 regions; oversized header/panels/white input removed; dock above nav area; no h-scroll
4. [DONE] Desktop: command environment, no card-per-component
5. [DONE] Typography: compact scale, no oversized body text
6. [DONE] Command dock: custom dark surface, circular mic, MicMeter audio-reactive indicator when listening, integrated input, compact send, interrupt-when-speaking
7. [DONE] Brand: Forge identity integrated; tool registry/approvals/auth/e-stop/safety untouched
8. [DONE] Micro-interactions: breathing, transitions, success pulse, history expand/collapse, reduced-motion
9. [DONE] Implementation: no duplicate components (one orb renderer + theme prop; Calcifer avatar is the persona's slot replacement per explicit instruction), no fake states, no suppressed errors
10. [GAP] Screenshots (see A.EPIC10)
11. Definition of done: all items DONE except screenshot verification

## C. Follow-ups

- [DONE] Audio quality: default mp3_44100_192 (MP3 HD — universal device compatibility), pcm_44100 WAV lossless + mp3_44100_128 options, quality selector in workspace
- [DONE] Settings vault: Fernet-encrypted, masked, test-connection/remove; 24 tests
- [DONE] Calcifer identity: forgeos.assistantModel single source; Settings model picker; Draven page dynamic naming; backend persona field (validated, default draven); identity reply + system prompt per persona; tools/safety identical
- [DONE] Calcifer exact image: bundled as calcifer-afRqeBaB.jpg (content-hashed, not hotlinked); CalciferAvatar in orb slot when persona=calcifer; mouth overlay at measured 50.8%/67.6%, driven by REAL TTS audio via AudioAnalyzer with attack/release envelope; idle shows original smile; gentle bob/flicker/glow by state; percentage-based alignment
- [NOTE] Redesign §2 "do not use a static image as the orb" vs "implement this exact image": Michael's later explicit instruction supersedes for the Calcifer persona only; Draven orb remains fully procedural
- [FIXED] Slot-filling: pending-intent store (business_id key, 15-min TTL, documented non-durable); ask→answer→execute with "Got it — researching X" confirmation; new command supersedes; cancel drops; unmappable answers re-ask specifically (never invents); identity questions don't consume pending; 6 tests green; VERIFIED LIVE on sprite (ask→fill→ok, cancel→dropped). Earlier live failure was a stale API server (PID 10385); killed properly, fresh server verified.
- [FIXED] Orb art corrections (2026-10-09 urgent): glossy highlight removed, darker, thinner tendrils, ragged silhouette, rings ~0.05 at rest, ember heart kept, 2D fallback matched. Deployed as index-eogkxJ_a.js. Screenshot-vs-reference verification still requires a browser (parent).

## Deferred with reason (evidence, not judgment)
1. Screenshots at specified viewports: subagent tooling cannot drive a browser or resize viewports (established limitation). Parent must arrange; exact shots needed: Draven/cyan orb, Calcifer avatar (mouth closed + during TTS), Settings model card, 390px mobile + 1440px desktop.
2. Playwright E2E + FPS measurements: no device/browser automation available in this environment.
3. Native packaging: out of scope for web delivery; never claimed.
4. Pending-intent durability: in-memory by explicit task authorization ("in-memory dict with 15-min TTL is fine for now"); documented in module docstring; Redis upgrade path noted.

## IP note
Calcifer is a Studio Ghibli character: fine for Michael's personal build; needs original art before any customer-facing use.
