import { useCallback, useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { apiFetch, apiUrl, getToken } from "../lib/api";
import { useAuth } from "../lib/auth";
import { ttsSupported, useVoice } from "../lib/voice";
import { JarvisOrb } from "../components/JarvisOrb";
import { CalciferAvatar } from "../components/CalciferAvatar";
import { canTransition, type OrbState } from "../lib/orbState";
import {
  ASSISTANT_MODELS,
  clientTimeZone,
  greetingFor,
  readAssistantModel,
  type AssistantModel,
} from "../lib/assistantModel";

interface Msg {
  role: "user" | "assistant";
  content: string;
  tools?: ToolUse[];
  cost?: number;
}

interface ToolUse {
  tool: string;
  risk: string;
  status: string;
  duration_ms: number;
}

interface ApprovalNeeded {
  tool: string;
  description: string;
  input: Record<string, unknown>;
}

interface ChatReply {
  reply: string;
  tools_used: ToolUse[];
  approvals_needed: ApprovalNeeded[];
  estimated_cost_usd: number;
  provider: string;
}

interface ProviderInfo {
  provider: string;
  model: string | null;
  configured: boolean;
  latency_ms: number | null;
  tts?: {
    provider: string;
    configured: boolean;
    voice_count: number | null;
  };
}

interface TtsVoice {
  voice_id: string;
  name: string;
  language: string;
  category: string;
}

/** Manual voice-engine override. When set, the user's explicit choice wins
 *  over the automatic ElevenLabs-when-configured default. */
const VOICE_ENGINE_KEY = "forgeos.voiceEngine";

function readEngineOverride(): "browser" | "elevenlabs" | null {
  try {
    const v = window.localStorage.getItem(VOICE_ENGINE_KEY);
    return v === "browser" || v === "elevenlabs" ? v : null;
  } catch {
    return null;
  }
}

function writeEngineOverride(v: "browser" | "elevenlabs"): void {
  try {
    window.localStorage.setItem(VOICE_ENGINE_KEY, v);
  } catch {
    /* storage unavailable — the session default still applies */
  }
}

interface AuditRun {
  id: string;
  tool: string;
  risk: string;
  status: string;
  duration_ms: number;
  created_at: string;
  output_summary: string | null;
}

function timeAgo(iso: string): string {  const s = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return `${Math.floor(s / 86400)}d ago`;
}

const RISK_COLOR: Record<string, string> = {
  low: "var(--green)",
  medium: "var(--accent)",
  high: "var(--red)",
};

/** Mic button glyph: microphone / stop square / interrupt. No emoji. */
function MicGlyph({ mode }: { mode: "idle" | "listening" | "speaking" }) {
  if (mode === "listening") return <span className="dv-mic-stop" aria-hidden="true" />;
  if (mode === "speaking") return <span aria-hidden="true" className="dv-mic-int">⏹</span>;
  return (
    <svg viewBox="0 0 24 24" width="22" height="22" fill="none" aria-hidden="true">
      <rect x="9" y="2.5" width="6" height="11" rx="3" fill="currentColor" />
      <path
        d="M6.5 10.5a5.5 5.5 0 0 0 11 0"
        stroke="currentColor"
        strokeWidth="2"
        strokeLinecap="round"
      />
      <path
        d="M12 16v4.5"
        stroke="currentColor"
        strokeWidth="2"
        strokeLinecap="round"
      />
    </svg>
  );
}

/**
 * Real microphone level meter: analyzes the live mic stream (not a fake
 * animation). Shown beside the mic while listening.
 */
function MicMeter({ stream }: { stream: MediaStream | null }) {
  const ref = useRef<HTMLCanvasElement>(null);

  useEffect(() => {
    if (!stream) return;
    const canvas = ref.current;
    if (!canvas) return;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;
    let ac: AudioContext | null = null;
    let analyser: AnalyserNode | null = null;
    let src: MediaStreamAudioSourceNode | null = null;
    let raf = 0;
    let dead = false;
    try {
      const AC =
        window.AudioContext || (window as unknown as { webkitAudioContext: typeof AudioContext }).webkitAudioContext;
      ac = new AC();
      analyser = ac.createAnalyser();
      analyser.fftSize = 64;
      analyser.smoothingTimeConstant = 0.55;
      src = ac.createMediaStreamSource(stream);
      src.connect(analyser);
    } catch {
      return;
    }
    const data = new Uint8Array(analyser.frequencyBinCount);
    const draw = () => {
      if (dead || !analyser) return;
      raf = requestAnimationFrame(draw);
      analyser.getByteFrequencyData(data);
      const w = canvas.width;
      const h = canvas.height;
      ctx.clearRect(0, 0, w, h);
      const bars = 9;
      const bw = w / bars;
      for (let i = 0; i < bars; i++) {
        const v = data[Math.floor((i / bars) * data.length * 0.7)] / 255;
        const bh = Math.max(2, v * h);
        ctx.fillStyle = "rgba(101,223,255,0.92)";
        ctx.fillRect(i * bw + bw * 0.24, (h - bh) / 2, bw * 0.52, bh);
      }
    };
    draw();
    return () => {
      dead = true;
      cancelAnimationFrame(raf);
      try { src?.disconnect(); } catch { /* noop */ }
      try { analyser?.disconnect(); } catch { /* noop */ }
      try { void ac?.close(); } catch { /* noop */ }
    };
  }, [stream]);

  if (!stream) return null;
  return (
    <canvas
      ref={ref}
      className="dv-meter"
      width={76}
      height={22}
      aria-hidden="true"
    />
  );
}

/**
 * Draven — voice-first executive command workspace.
 * Talk or type; Draven routes through the typed tool registry, executes
 * low-risk operations against real backend state, and queues anything
 * consequential for human approval.
 */
export default function DravenPage() {
  const { user } = useAuth();
  const isAdmin = user?.role === "owner" || user?.role === "admin";

  const [messages, setMessages] = useState<Msg[]>([
    {
      role: "assistant",
      content: greetingFor(readAssistantModel()),
    },
  ]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [conversationMode, setConversationMode] = useState(true);
  const [speakReplies, setSpeakReplies] = useState(true);
  const [rate, setRate] = useState(1.02);
  const [provider, setProvider] = useState<ProviderInfo | null>(null);
  const [audit, setAudit] = useState<AuditRun[]>([]);
  const [totalCost, setTotalCost] = useState(0);
  const [stoppedAt, setStoppedAt] = useState<string | null>(null);

  // ElevenLabs voice engine — ElevenLabs is the default whenever it is
  // configured; a deliberate manual switch is persisted as an override.
  const [voiceEngine, setVoiceEngine] = useState<"browser" | "elevenlabs">(
    () => {
      const o = readEngineOverride();
      return o ?? "browser"; // loadVoices() applies the auto-default on mount
    }
  );
  const [ttsVoices, setTtsVoices] = useState<TtsVoice[]>([]);
  const [ttsConfigured, setTtsConfigured] = useState(false);
  const [selectedVoiceId, setSelectedVoiceId] = useState<string>("");
  const [speakingAudio, setSpeakingAudio] = useState(false);
  const audioRef = useRef<HTMLAudioElement | null>(null);
  /** Tracked in state (not just a ref) so the orb re-binds the real signal. */
  const [audioEl, setAudioEl] = useState<HTMLAudioElement | null>(null);

  // --- Cinematic orb state machine: every visual state derives from a real
  // --- application event, never from arbitrary timers.
  const [orbState, setOrbState] = useState<OrbState>("idle");
  const orbRef = useRef<OrbState>("idle");
  const setOrb = useCallback((next: OrbState) => {
    const cur = orbRef.current;
    if (cur === next) return;
    if (canTransition(cur, next)) {
      orbRef.current = next;
      setOrbState(next);
    } else if (canTransition(cur, "idle") && canTransition("idle", next)) {
      // Stay coherent: route invalid jumps through idle.
      orbRef.current = next;
      setOrbState(next);
    }
    // Invalid transitions are ignored — the orb never freezes or glitches.
  }, []);
  // MP3 HD default: universal device compatibility (WAV confused some
  // customer devices). pcm_44100 is still available for archival quality.
  const [audioQuality, setAudioQuality] = useState(
    "mp3_44100_192" as "pcm_44100" | "mp3_44100_192" | "mp3_44100_128"
  );

  // Provider admin form
  const [pProvider, setPProvider] = useState("gemini");
  const [pModel, setPModel] = useState("");
  const [pBaseUrl, setPBaseUrl] = useState("");
  const [pKey, setPKey] = useState("");
  const [pBusy, setPBusy] = useState(false);
  const [pMsg, setPMsg] = useState<string | null>(null);

  const historyRef = useRef<Msg[]>([]);
  const abortRef = useRef<AbortController | null>(null);
  const bottomRef = useRef<HTMLDivElement>(null);

  const [historyOpen, setHistoryOpen] = useState(false);
  const [railOpen, setRailOpen] = useState(false);
  const [voiceOpen, setVoiceOpen] = useState(false);

  // Assistant model identity: read on mount, re-read when returning from
  // Settings (tab regains focus) so a change takes effect immediately.
  const [model, setModel] = useState<AssistantModel>(() => readAssistantModel());
  const modelRef = useRef<AssistantModel>(readAssistantModel());
  const meta = ASSISTANT_MODELS[model];
  useEffect(() => {
    modelRef.current = model;
  }, [model]);
  useEffect(() => {
    const refresh = () => setModel(readAssistantModel());
    refresh();
    window.addEventListener("focus", refresh);
    document.addEventListener("visibilitychange", refresh);
    return () => {
      window.removeEventListener("focus", refresh);
      document.removeEventListener("visibilitychange", refresh);
    };
  }, []);

  // Escape closes overlays (activity rail, voice settings, history).
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Escape") return;
      setRailOpen(false);
      setVoiceOpen(false);
      setHistoryOpen(false);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  // Scroll the history into view when it expands.
  useEffect(() => {
    if (historyOpen) {
      requestAnimationFrame(() =>
        bottomRef.current?.scrollIntoView({ behavior: "smooth", block: "end" })
      );
    }
  }, [historyOpen]);

  const loadProvider = useCallback(async () => {
    try {
      const p = await apiFetch<ProviderInfo>("/draven/provider");
      setProvider(p);
      setPProvider(p.provider);
      if (p.model) setPModel(p.model);
    } catch {
      /* provider panel degrades gracefully */
    }
  }, []);

  const loadAudit = useCallback(async () => {
    try {
      const r = await apiFetch<{ items: AuditRun[] }>("/draven/audit?limit=12");
      setAudit(r.items ?? []);
    } catch {
      /* timeline degrades gracefully */
    }
  }, []);

  const loadVoices = useCallback(async () => {
    try {
      const r = await apiFetch<{ configured: boolean; voices: TtsVoice[] }>(
        "/draven/tts/voices"
      );
      setTtsConfigured(r.configured);
      setTtsVoices(r.voices ?? []);
      if (r.voices?.length && !selectedVoiceId) {
        setSelectedVoiceId(r.voices[0].voice_id);
      }
      // ElevenLabs is the default voice engine: auto-select it whenever it
      // is configured, unless the user has deliberately overridden the
      // choice (persisted in localStorage). Browser voice is only the
      // fallback when ElevenLabs is not set up. A stale override that can
      // no longer be satisfied (ElevenLabs chosen, then disconnected)
      // falls back to browser voice rather than a dead selection.
      const override = readEngineOverride();
      if (override === "elevenlabs" && r.configured) {
        setVoiceEngine("elevenlabs");
      } else if (override === "browser") {
        setVoiceEngine("browser");
      } else {
        setVoiceEngine(r.configured ? "elevenlabs" : "browser");
      }
    } catch {
      /* voice picker degrades gracefully */
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    void loadProvider();
    void loadAudit();
    void loadVoices();
  }, [loadProvider, loadAudit, loadVoices]);

  /** Stop any in-flight ElevenLabs audio. */
  const stopAudio = useCallback(() => {
    audioRef.current?.pause();
    audioRef.current = null;
    setAudioEl(null);
    setSpeakingAudio(false);
  }, []);

  /** Speak via ElevenLabs (server-side TTS, key never leaves the backend). */
  const speakElevenLabs = useCallback(
    async (text: string) => {
      if (!selectedVoiceId) return;
      stopAudio();
      setSpeakingAudio(true);
      try {
        const res = await fetch(apiUrl("/draven/tts/speak"), {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            Authorization: `Bearer ${getToken() ?? ""}`,
          },
          body: JSON.stringify({
            text: text.slice(0, 2000),
            voice_id: selectedVoiceId,
            output_format: audioQuality,
          }),
        });
        if (!res.ok) throw new Error(`TTS failed (${res.status})`);
        const blob = await res.blob();
        const url = URL.createObjectURL(blob);
        const audio = new Audio(url);
        audioRef.current = audio;
        setAudioEl(audio);
        audio.onended = () => {
          stopAudio();
          URL.revokeObjectURL(url);
          if (conversationMode) voice.startListening();
        };
        audio.onerror = () => stopAudio();
        await audio.play();
      } catch {
        setSpeakingAudio(false);
      }
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [selectedVoiceId, conversationMode, stopAudio, audioQuality]
  );

  /** Route spoken replies through the selected voice engine. */
  const speakReply = useCallback(
    (text: string) => {
      if (!speakReplies) return;
      if (voiceEngine === "elevenlabs" && ttsConfigured && selectedVoiceId) {
        voice.stopListening();
        void speakElevenLabs(text);
      } else {
        voice.speak(text);
      }
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [speakReplies, voiceEngine, ttsConfigured, selectedVoiceId, speakElevenLabs]
  );

  const send = useCallback(
    async (text: string) => {
      const clean = text.trim();
      if (!clean || busy) return;
      setBusy(true);
      setStoppedAt(null);
      setOrb("processing");
      const userMsg: Msg = { role: "user", content: clean };
      setMessages((m) => [...m, userMsg]);
      setInput("");
      const ctrl = new AbortController();
      abortRef.current = ctrl;
      try {
        const res = await apiFetch<ChatReply>("/draven/chat", {
          method: "POST",
          body: {
            message: clean,
            history: historyRef.current.slice(-8).map((m) => ({
              role: m.role,
              content: m.content,
            })),
            persona: modelRef.current,
            client_tz: clientTimeZone(),
          },
          signal: ctrl.signal,
        });
        const asst: Msg = {
          role: "assistant",
          content: res.reply,
          tools: res.tools_used,
          cost: res.estimated_cost_usd,
        };
        // Surface approval-required outcomes inline as their own block.
        const full =
          res.approvals_needed.length > 0
            ? {
                ...asst,
                content:
                  res.reply +
                  "\n\n⚠ Needs your approval:\n" +
                  res.approvals_needed
                    .map((a) => `• ${a.description}`)
                    .join("\n"),
              }
            : asst;
        setMessages((m) => [...m, full]);
        historyRef.current = [...historyRef.current, userMsg, full].slice(-16);
        setTotalCost((c) => c + (res.estimated_cost_usd || 0));
        speakReply(res.reply);
        if (!speakReplies) setOrb("success");
        void loadAudit();
      } catch (e: unknown) {
        if (e instanceof DOMException && e.name === "AbortError") {
          setMessages((m) => [
            ...m,
            { role: "assistant", content: "Stopped. What would you like instead?" },
          ]);
        } else {
          setOrb("error");
          setMessages((m) => [
            ...m,
            {
              role: "assistant",
              content:
                "I couldn't reach the backend just now. Check your connection and try again.",
            },
          ]);
        }
      } finally {
        setBusy(false);
        abortRef.current = null;
        requestAnimationFrame(() =>
          bottomRef.current?.scrollIntoView({ behavior: "smooth" })
        );
      }
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [busy, loadAudit, speakReply, speakReplies, setOrb]
  );

  const voice = useVoice({
    onResult: (t) => send(t),
    conversationMode,
    speakReplies,
    rate,
  });

  // Mic activity drives LISTENING.
  useEffect(() => {
    if (voice.listening) setOrb("listening");
    else if (orbRef.current === "listening") setOrb("idle");
  }, [voice.listening, setOrb]);

  // Real audio playback drives SPEAKING (ElevenLabs element or browser TTS).
  useEffect(() => {
    const speaking = voice.speaking || speakingAudio;
    if (speaking) setOrb("speaking");
    else if (orbRef.current === "speaking") setOrb("idle");
  }, [voice.speaking, speakingAudio, setOrb]);

  // Backend/permission failures surface as a restrained error state.
  useEffect(() => {
    if (voice.error) setOrb("error");
  }, [voice.error, setOrb]);

  const toggleMic = () => {
    if (voice.listening) voice.stopListening();
    else {
      setOrb("wake"); // energy pulse; auto-advances, mic effect confirms
      voice.stopSpeaking();
      voice.startListening();
    }
  };

  /** Interrupt: stop speech, cancel the in-flight request, stop listening. */
  const interrupt = useCallback(() => {
    voice.stopSpeaking();
    voice.stopListening();
    stopAudio();
    abortRef.current?.abort();
    setBusy(false);
    setOrb("interrupted");
  }, [voice, stopAudio, setOrb]);

  /** Emergency stop: halt new agent/tool work backend-side. */
  const stopAll = useCallback(async () => {
    interrupt();
    try {
      const r = await apiFetch<{ stopped: boolean; at: string }>("/draven/stop", {
        method: "POST",
      });
      if (r.stopped) setStoppedAt(r.at);
    } catch {
      /* surfaced via interrupt already */
    }
  }, [interrupt]);

  const saveProvider = async () => {
    setPBusy(true);
    setPMsg(null);
    try {
      await apiFetch("/draven/provider", {
        method: "PUT",
        body: {
          provider: pProvider,
          model: pModel || null,
          base_url: pBaseUrl || null,
          api_key: pKey || null,
        },
      });
      setPKey("");
      setPMsg("Saved. Keys stay server-side and are never returned to the browser.");
      void loadProvider();
    } catch (e) {
      setPMsg(e instanceof Error ? e.message : "Save failed.");
    } finally {
      setPBusy(false);
    }
  };

  const testProvider = async () => {
    setPBusy(true);
    setPMsg(null);
    try {
      const r = await apiFetch<{ ok: boolean; latency_ms: number; error: string | null }>(
        "/draven/provider/test",
        { method: "POST" }
      );
      setPMsg(
        r.ok
          ? `Connection OK — ${Math.round(r.latency_ms)}ms round trip.`
          : `Connection failed: ${r.error ?? "unknown error"}`
      );
      void loadProvider();
    } catch (e) {
      setPMsg(e instanceof Error ? e.message : "Test failed.");
    } finally {
      setPBusy(false);
    }
  };

  // ---- derived presentation state (all visuals follow real events) ----
  const isSpeaking = voice.speaking || speakingAudio;
  const stateLabel = voice.listening
    ? "Listening"
    : isSpeaking
      ? "Speaking"
      : busy
        ? "Working"
        : voice.error
          ? "Attention"
          : "Ready";
  const orbCaption = voice.listening
    ? "Listening — speak now"
    : isSpeaking
      ? "Speaking — tap the mic to interrupt"
      : busy
        ? "Working on it…"
        : voice.error
          ? "Something needs attention"
          : "Tap the orb and talk";
  const lastAssistant = [...messages].reverse().find((m) => m.role === "assistant");
  const contextMsg = (lastAssistant?.content ?? "").replace(/\s+/g, " ").slice(0, 120);
  const micMode = isSpeaking ? "speaking" : voice.listening ? "listening" : "idle";
  const onMicPress = () => {
    if (isSpeaking) interrupt();
    else toggleMic();
  };

  return (
    <div className="dv">
      {/* A — compact identity bar */}
      <header className="dv-bar">
        <div className="dv-id">
          <span className="dv-mark" aria-hidden="true">
            D
          </span>
          <span className="dv-name">{meta.name}</span>
          <span
            className={`dv-conn${provider && !provider.configured ? " warn" : ""}`}
            title={
              provider
                ? `AI provider: ${provider.provider}${provider.configured ? "" : " (no key)"}`
                : "Provider status loading"
            }
            aria-label={
              provider
                ? `AI provider ${provider.provider}`
                : "AI provider status loading"
            }
          >
            <span className="dv-conn-dot" aria-hidden="true" />
          </span>
        </div>
        <div className="dv-bar-actions">
          {totalCost > 0 && (
            <span className="dv-cost" title="Session cost">
              ${totalCost.toFixed(4)}
            </span>
          )}
          <button
            type="button"
            className="dv-iconbtn"
            onClick={() => setRailOpen(true)}
            aria-label="Open activity panel"
            title="Activity"
          >
            <svg viewBox="0 0 24 24" width="17" height="17" fill="none" aria-hidden="true">
              <path
                d="M4 6h16M4 12h16M4 18h10"
                stroke="currentColor"
                strokeWidth="2"
                strokeLinecap="round"
              />
            </svg>
          </button>
          <button
            type="button"
            className="dv-iconbtn dv-danger"
            onClick={stopAll}
            aria-label="Emergency stop all"
            title="Stop all"
          >
            <span aria-hidden="true">⏹</span>
          </button>
        </div>
      </header>

      {stoppedAt && (
        <div className="dv-stopnote" role="alert">
          Emergency stop engaged — no new tool work will run. Send a message to resume.
        </div>
      )}

      {/* B — compact conversational context */}
      <button
        type="button"
        className="dv-context"
        onClick={() => setHistoryOpen((o) => !o)}
        aria-expanded={historyOpen}
        aria-label={historyOpen ? "Collapse conversation history" : "Expand conversation history"}
      >
        <span className={`dv-state dv-state-${orbState}`}>{stateLabel}</span>
        <span className="dv-context-msg">{contextMsg}</span>
        <span className="dv-caret" aria-hidden="true">
          {historyOpen ? "▴" : "▾"}
        </span>
      </button>

      {historyOpen && (
        <div className="dv-history" role="log" aria-live="polite" aria-label="Conversation history">
          <div className="dv-history-inner">
            {messages.map((m, i) => (
              <div key={i} className="dv-msg">
                <div className={`dv-bubble ${m.role}`}>{m.content}</div>
                {m.tools && m.tools.length > 0 && (
                  <div className="tool-chips dv-chips">
                    {m.tools.map((t, j) => (
                      <span key={j} className="tool-chip" title={`${t.status} · ${t.duration_ms}ms`}>
                        <span
                          className="dot"
                          style={{ background: RISK_COLOR[t.risk] ?? "var(--muted)" }}
                        />
                        {t.tool.replace("draven.", "")}
                        <span className="muted"> · {t.duration_ms}ms</span>
                      </span>
                    ))}
                    {m.cost != null && m.cost > 0 && (
                      <span className="tool-chip">${m.cost.toFixed(4)}</span>
                    )}
                  </div>
                )}
              </div>
            ))}
            {busy && <div className="dv-bubble assistant thinking">…</div>}
            <div ref={bottomRef} />
          </div>
        </div>
      )}

      {/* C — hero orb (or Calcifer, when that model is selected) */}
      <main className="dv-stage">
        <div className="dv-stage-aura" aria-hidden="true" />
        {model === "calcifer" ? (
          <CalciferAvatar
            state={orbState}
            micStream={voice.micStream}
            audioElement={audioEl}
            onTap={toggleMic}
            disabled={!voice.stt}
            label={voice.listening ? "Stop listening" : `Talk to ${meta.name}`}
          />
        ) : (
          <JarvisOrb
            state={orbState}
            micStream={voice.micStream}
            audioElement={audioEl}
            onTap={toggleMic}
            disabled={!voice.stt}
            label={voice.listening ? "Stop listening" : `Talk to ${meta.name}`}
            theme={meta.theme}
          />
        )}
        <div className="dv-caption" aria-live="polite">
          {orbCaption}
        </div>
      </main>

      {/* D — floating command dock */}
      <footer className="dv-dockzone">
        {voice.interim && (
          <div className="dv-interim" aria-live="polite">
            {voice.interim}…
          </div>
        )}
        {voice.error && (
          <div className="dv-error" role="alert">
            {voice.error}
          </div>
        )}
        {!voice.stt && (
          <div className="dv-error" role="note">
            Voice input isn't available in this browser — you can still type below.
          </div>
        )}
        <form
          className="dv-dock"
          onSubmit={(e) => {
            e.preventDefault();
            send(input);
          }}
        >
          <button
            type="button"
            className={`dv-mic dv-mic-${micMode}`}
            onClick={onMicPress}
            disabled={micMode === "idle" && !voice.stt}
            aria-label={
              micMode === "speaking"
                ? `Interrupt ${meta.name}`
                : micMode === "listening"
                  ? "Stop listening"
                  : `Talk to ${meta.name}`
            }
            title={
              micMode === "speaking"
                ? "Interrupt"
                : micMode === "listening"
                  ? "Stop listening"
                  : "Talk"
            }
          >
            <MicGlyph mode={micMode} />
          </button>
          {voice.listening && <MicMeter stream={voice.micStream} />}
          <input
            className="dv-input"
            value={input}
            onChange={(e) => setInput(e.target.value)}
            placeholder={`Message ${meta.name}…`}
            aria-label="Type a message"
            enterKeyHint="send"
          />
          <button
            type="submit"
            className="dv-send"
            disabled={busy || !input.trim()}
            aria-label="Send message"
          >
            <svg viewBox="0 0 24 24" width="18" height="18" fill="none" aria-hidden="true">
              <path
                d="M12 19V5m0 0l-6 6m6-6l6 6"
                stroke="currentColor"
                strokeWidth="2"
                strokeLinecap="round"
                strokeLinejoin="round"
              />
            </svg>
          </button>
        </form>
        <button
          type="button"
          className="dv-voicetoggle"
          onClick={() => setVoiceOpen((o) => !o)}
          aria-expanded={voiceOpen}
        >
          Voice settings {voiceOpen ? "▴" : "▾"}
        </button>
        {voiceOpen && (
          <div className="dv-voicepanel">
            <label className="dv-toggle">
              <input
                type="checkbox"
                checked={conversationMode}
                onChange={(e) => setConversationMode(e.target.checked)}
              />
              <span>Back-and-forth mode</span>
            </label>
            <label className="dv-toggle">
              <input
                type="checkbox"
                checked={speakReplies && ttsSupported()}
                disabled={!ttsSupported()}
                onChange={(e) => setSpeakReplies(e.target.checked)}
              />
              <span>Speak replies aloud</span>
            </label>
            <label className="dv-field">
              <span>Voice engine</span>
              <select
                value={voiceEngine}
                onChange={(e) => {
                  const v = e.target.value as "browser" | "elevenlabs";
                  setVoiceEngine(v);
                  writeEngineOverride(v); // deliberate switch wins over the auto-default
                }}
                aria-label="Voice engine"
              >
                <option value="browser">Browser</option>
                <option value="elevenlabs" disabled={!ttsConfigured}>
                  ElevenLabs{ttsConfigured ? "" : " (not configured)"}
                </option>
              </select>
            </label>
            {!ttsConfigured && (
              <p className="dv-muted" style={{ marginTop: 6, fontSize: 12 }}>
                Using the browser voice for now. Review voice status in{" "}
                <Link to="/integrations" className="dv-link">
                  Integrations
                </Link>{" "}
                for the premium Draven voice. It becomes the default
                automatically once configured.
              </p>
            )}
            {voiceEngine === "elevenlabs" && ttsConfigured && (
              <>
                <label className="dv-field">
                  <span>Voice</span>
                  <select
                    value={selectedVoiceId}
                    onChange={(e) => setSelectedVoiceId(e.target.value)}
                    aria-label="ElevenLabs voice"
                  >
                    {ttsVoices.map((v) => (
                      <option key={v.voice_id} value={v.voice_id}>
                        {v.name} ({v.language})
                      </option>
                    ))}
                  </select>
                </label>
                <label className="dv-field">
                  <span>Audio quality</span>
                  <select
                    value={audioQuality}
                    onChange={(e) =>
                      setAudioQuality(
                        e.target.value as "pcm_44100" | "mp3_44100_192" | "mp3_44100_128"
                      )
                    }
                    aria-label="Audio quality"
                  >
                    <option value="mp3_44100_192">HD MP3 192kbps (default)</option>
                    <option value="pcm_44100">Lossless WAV 44.1kHz</option>
                    <option value="mp3_44100_128">Standard MP3 128kbps</option>
                  </select>
                </label>
              </>
            )}
            <label className="dv-field">
              <span>Voice speed</span>
              <span className="dv-range">
                <input
                  type="range"
                  min={0.7}
                  max={1.3}
                  step={0.05}
                  value={rate}
                  onChange={(e) => setRate(Number(e.target.value))}
                  aria-label="Voice speaking speed"
                />
                <em>{rate.toFixed(2)}×</em>
              </span>
            </label>
          </div>
        )}
      </footer>

      {/* Ops rail: executions, approvals, provider admin */}
      <div
        className={`dv-scrim${railOpen ? " show" : ""}`}
        onClick={() => setRailOpen(false)}
        aria-hidden="true"
      />
      <aside className={`dv-rail${railOpen ? " open" : ""}`} aria-label="Activity panel" aria-hidden={!railOpen}>
        <div className="dv-rail-head">
          <span className="dv-rail-title">Activity</span>
          <button
            type="button"
            className="dv-iconbtn"
            onClick={() => setRailOpen(false)}
            aria-label="Close activity panel"
          >
            <span aria-hidden="true">✕</span>
          </button>
        </div>

        <section className="dv-sec" aria-label="Execution timeline">
          <div className="dv-sec-head">
            <span className="dv-sec-title">Recent executions</span>
            <button type="button" className="dv-link" onClick={() => void loadAudit()}>
              Refresh
            </button>
          </div>
          {audit.length === 0 ? (
            <p className="dv-muted">No tool runs yet — ask {meta.name} something.</p>
          ) : (
            <ul className="dv-list">
              {audit.map((a) => (
                <li key={a.id} className="dv-row">
                  <span
                    className="dot"
                    style={{
                      background: RISK_COLOR[a.risk] ?? "var(--muted)",
                      width: 7,
                      height: 7,
                      borderRadius: "50%",
                      marginTop: 5,
                      flexShrink: 0,
                    }}
                  />
                  <span className="dv-row-main">
                    <span className="dv-row-title">{a.tool.replace("draven.", "")}</span>
                    <span className="dv-row-sub">
                      {a.status} · {a.duration_ms}ms · {timeAgo(a.created_at)}
                    </span>
                  </span>
                </li>
              ))}
            </ul>
          )}
        </section>

        <section className="dv-sec" aria-label="Approvals">
          <div className="dv-sec-head">
            <span className="dv-sec-title">Approvals</span>
            <Link to="/approvals" className="dv-link">
              Open queue →
            </Link>
          </div>
          <p className="dv-muted">
            High-impact actions {meta.name} proposes appear here for your explicit
            approval before anything executes. Nothing consequential runs on
            voice alone.
          </p>
        </section>

        {isAdmin && (
          <section className="dv-sec" aria-label="AI provider configuration">
            <div className="dv-sec-head">
              <span className="dv-sec-title">AI provider</span>
              {provider && (
                <span className={`dv-pill${provider.configured ? "" : " warn"}`}>
                  {provider.configured ? "connected" : "not configured"}
                </span>
              )}
            </div>
            <p className="dv-muted" style={{ marginTop: 0 }}>
              <Link to="/integrations" className="dv-link">
                Review integration status →
              </Link>{" "}
              Keys are stored encrypted in the central vault.
            </p>
            <div className="dv-form">
              <label className="dv-field">
                <span>Provider</span>
                <select value={pProvider} onChange={(e) => setPProvider(e.target.value)}>
                  <option value="gemini">Gemini (recommended)</option>
                  <option value="anthropic">Anthropic (Claude)</option>
                  <option value="openrouter">OpenRouter</option>
                  <option value="ollama">Ollama (local / custom)</option>
                  <option value="openai_compatible">OpenAI-compatible</option>
                  <option value="elevenlabs">ElevenLabs (voice/TTS)</option>
                </select>
              </label>
              <label className="dv-field">
                <span>Model</span>
                <input
                  value={pModel}
                  onChange={(e) => setPModel(e.target.value)}
                  placeholder={
                    pProvider === "gemini"
                      ? "e.g. gemini-3.5-flash-lite"
                      : pProvider === "openrouter"
                        ? "e.g. anthropic/claude-sonnet-4"
                        : pProvider === "ollama"
                          ? "e.g. llama3.1"
                          : "e.g. claude-opus-4-6"
                  }
                />
              </label>
              {(pProvider === "ollama" || pProvider === "openai_compatible") && (
                <label className="dv-field">
                  <span>
                    Base URL{pProvider === "ollama" ? " (optional — defaults to localhost)" : " (https only)"}
                  </span>
                  <input
                    value={pBaseUrl}
                    onChange={(e) => setPBaseUrl(e.target.value)}
                    placeholder={
                      pProvider === "ollama"
                        ? "http://localhost:11434/v1"
                        : "https://api.example.com/v1"
                    }
                    inputMode="url"
                  />
                </label>
              )}
              {pProvider !== "ollama" && (
                <label className="dv-field">
                  <span>API key (stored encrypted, server-side only)</span>
                  <input
                    type="password"
                    value={pKey}
                    onChange={(e) => setPKey(e.target.value)}
                    placeholder="Leave blank to keep existing"
                    autoComplete="off"
                  />
                </label>
              )}
              {pMsg && <div className="dv-note">{pMsg}</div>}
              <div className="dv-form-actions">
                <button
                  type="button"
                  className="dv-btn"
                  onClick={testProvider}
                  disabled={pBusy}
                >
                  Test connection
                </button>
                <button
                  type="button"
                  className="dv-btn dv-btn-primary"
                  onClick={saveProvider}
                  disabled={pBusy}
                >
                  Save
                </button>
              </div>
            </div>
          </section>
        )}
      </aside>
    </div>
  );
}
