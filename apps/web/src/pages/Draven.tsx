import { useCallback, useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { apiFetch, apiUrl, getToken } from "../lib/api";
import { useAuth } from "../lib/auth";
import { ttsSupported, useVoice } from "../lib/voice";

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

interface AuditRun {
  id: string;
  tool: string;
  risk: string;
  status: string;
  duration_ms: number;
  created_at: string;
  output_summary: string | null;
}

function timeAgo(iso: string): string {
  const s = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
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
      content:
        "I'm Draven. Tap the mic and talk to me — ask about approvals, campaigns, performance, or tell me what to prepare. I'll show my work as I go.",
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

  // ElevenLabs voice engine
  const [voiceEngine, setVoiceEngine] = useState<"browser" | "elevenlabs">("browser");
  const [ttsVoices, setTtsVoices] = useState<TtsVoice[]>([]);
  const [ttsConfigured, setTtsConfigured] = useState(false);
  const [selectedVoiceId, setSelectedVoiceId] = useState<string>("");
  const [speakingAudio, setSpeakingAudio] = useState(false);
  const audioRef = useRef<HTMLAudioElement | null>(null);

  // Provider admin form
  const [pProvider, setPProvider] = useState("stub");
  const [pModel, setPModel] = useState("");
  const [pBaseUrl, setPBaseUrl] = useState("");
  const [pKey, setPKey] = useState("");
  const [pBusy, setPBusy] = useState(false);
  const [pMsg, setPMsg] = useState<string | null>(null);

  const historyRef = useRef<Msg[]>([]);
  const abortRef = useRef<AbortController | null>(null);
  const bottomRef = useRef<HTMLDivElement>(null);

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
          }),
        });
        if (!res.ok) throw new Error(`TTS failed (${res.status})`);
        const blob = await res.blob();
        const url = URL.createObjectURL(blob);
        const audio = new Audio(url);
        audioRef.current = audio;
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
    [selectedVoiceId, conversationMode, stopAudio]
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
        void loadAudit();
      } catch (e: unknown) {
        if (e instanceof DOMException && e.name === "AbortError") {
          setMessages((m) => [
            ...m,
            { role: "assistant", content: "Stopped. What would you like instead?" },
          ]);
        } else {
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
    [busy, loadAudit, speakReply]
  );

  const voice = useVoice({
    onResult: (t) => send(t),
    conversationMode,
    speakReplies,
    rate,
  });

  const toggleMic = () => {
    if (voice.listening) voice.stopListening();
    else {
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
  }, [voice, stopAudio]);

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

  return (
    <div className="page-wide" style={{ maxWidth: 1200, margin: "0 auto" }}>
      <div className="page-head" style={{ marginBottom: 16 }}>
        <div>
          <h1 style={{ display: "flex", alignItems: "center", gap: 10 }}>
            <span className="hex-mark" aria-hidden="true">
              D
            </span>
            Draven
          </h1>
          <p className="muted" style={{ margin: "4px 0 0" }}>
            Voice-first command workspace — every action runs through the audited tool registry.
          </p>
        </div>
        <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
          <span className={`pill${provider && !provider.configured ? " warn" : ""}`}>
            <span className="dot" />
            {provider
              ? `${provider.provider}${provider.configured ? "" : " · no key"}`
              : "provider…"}
          </span>
          {totalCost > 0 && (
            <span className="pill">
              ${totalCost.toFixed(4)} this session
            </span>
          )}
          <button type="button" className="btn btn-sm btn-danger" onClick={stopAll}>
            ⏹ Stop all
          </button>
        </div>
      </div>

      {stoppedAt && (
        <div className="notice error" style={{ marginBottom: 12 }}>
          Emergency stop engaged — no new tool work will run. Resume by sending a new message.
        </div>
      )}

      <div className="cmd-grid" style={{ gridTemplateColumns: "minmax(0, 1fr) 320px" }}>
        {/* Conversation column */}
        <div className="cmd-col">
          <section className="panel" aria-label="Conversation">
            {!voice.stt && (
              <div className="notice" style={{ marginBottom: 12 }}>
                Voice input isn't available in this browser — you can still type below.
              </div>
            )}
            <div className="chat-log" role="log" aria-live="polite">
              {messages.map((m, i) => (
                <div key={i}>
                  <div className={`chat-bubble ${m.role}`}>{m.content}</div>
                  {m.tools && m.tools.length > 0 && (
                    <div className="tool-chips">
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
              {voice.interim && (
                <div className="chat-bubble user interim">{voice.interim}…</div>
              )}
              {busy && <div className="chat-bubble assistant thinking">…</div>}
              <div ref={bottomRef} />
            </div>

            {voice.error && (
              <div className="notice error" style={{ marginTop: 8 }}>
                {voice.error}
              </div>
            )}

            <div className="voice-bar">
              <button
                type="button"
                className={`mic-btn${voice.listening ? " live" : ""}${voice.speaking || speakingAudio ? " speaking" : ""}`}
                onClick={toggleMic}
                aria-label={voice.listening ? "Stop listening" : "Start talking"}
                disabled={!voice.stt}
              >
                <span aria-hidden="true">{voice.listening ? "◉" : "◈"}</span>
              </button>
              {(voice.speaking || speakingAudio || busy) && (
                <button
                  type="button"
                  className="btn btn-sm btn-danger"
                  onClick={interrupt}
                  aria-label="Interrupt Draven"
                >
                  ⏹ Interrupt
                </button>
              )}
              <div className="voice-status">
                {voice.listening
                  ? "Listening… speak now"
                  : voice.speaking || speakingAudio
                    ? `Speaking${voiceEngine === "elevenlabs" ? " (ElevenLabs)" : ""}… (interrupt anytime)`
                    : busy
                      ? "Working…"
                      : "Tap the mic and talk"}
              </div>
            </div>

            <form
              className="chat-input-row"
              onSubmit={(e) => {
                e.preventDefault();
                send(input);
              }}
            >
              <input
                className="input"
                value={input}
                onChange={(e) => setInput(e.target.value)}
                placeholder='Or type — try "summarize today" or "what needs approval?"'
                aria-label="Type a message"
                enterKeyHint="send"
              />
              <button type="submit" className="btn btn-primary" disabled={busy || !input.trim()}>
                Send
              </button>
            </form>

            <div className="voice-toggles">
              <label className="toggle">
                <input
                  type="checkbox"
                  checked={conversationMode}
                  onChange={(e) => setConversationMode(e.target.checked)}
                />
                Back-and-forth mode
              </label>
              <label className="toggle">
                <input
                  type="checkbox"
                  checked={speakReplies && ttsSupported()}
                  disabled={!ttsSupported()}
                  onChange={(e) => setSpeakReplies(e.target.checked)}
                />
                Speak replies aloud
              </label>
              <label className="toggle" style={{ gap: 8 }}>
                Voice engine
                <select
                  className="input"
                  value={voiceEngine}
                  onChange={(e) => setVoiceEngine(e.target.value as "browser" | "elevenlabs")}
                  style={{ width: 130 }}
                  aria-label="Voice engine"
                >
                  <option value="browser">Browser</option>
                  <option value="elevenlabs" disabled={!ttsConfigured}>
                    ElevenLabs{ttsConfigured ? "" : " (not configured)"}
                  </option>
                </select>
              </label>
              {voiceEngine === "elevenlabs" && ttsConfigured && (
                <label className="toggle" style={{ gap: 8 }}>
                  Voice
                  <select
                    className="input"
                    value={selectedVoiceId}
                    onChange={(e) => setSelectedVoiceId(e.target.value)}
                    style={{ width: 150 }}
                    aria-label="ElevenLabs voice"
                  >
                    {ttsVoices.map((v) => (
                      <option key={v.voice_id} value={v.voice_id}>
                        {v.name} ({v.language})
                      </option>
                    ))}
                  </select>
                </label>
              )}
              <label className="toggle" style={{ gap: 8 }}>
                Voice speed
                <input
                  type="range"
                  min={0.7}
                  max={1.3}
                  step={0.05}
                  value={rate}
                  onChange={(e) => setRate(Number(e.target.value))}
                  aria-label="Voice speaking speed"
                  style={{ width: 90 }}
                />
                <span className="muted">{rate.toFixed(2)}×</span>
              </label>
            </div>
          </section>
        </div>

        {/* Ops column */}
        <div className="cmd-col">
          <section className="panel" aria-label="Execution timeline">
            <div className="panel-head">
              <span className="panel-title">◔ Recent executions</span>
              <button type="button" className="link" onClick={() => void loadAudit()}>
                Refresh
              </button>
            </div>
            {audit.length === 0 ? (
              <p className="muted" style={{ fontSize: 13 }}>
                No tool runs yet — ask Draven something.
              </p>
            ) : (
              <ul className="list" style={{ maxHeight: 260, overflowY: "auto" }}>
                {audit.map((a) => (
                  <li key={a.id} className="list-row" style={{ alignItems: "flex-start" }}>
                    <span
                      className="dot"
                      style={{
                        background: RISK_COLOR[a.risk] ?? "var(--muted)",
                        width: 8,
                        height: 8,
                        borderRadius: "50%",
                        marginTop: 6,
                        flexShrink: 0,
                      }}
                    />
                    <span style={{ minWidth: 0 }}>
                      <span className="list-title" style={{ fontSize: 12.5 }}>
                        {a.tool.replace("draven.", "")}
                      </span>
                      <span className="muted" style={{ display: "block", fontSize: 11.5 }}>
                        {a.status} · {a.duration_ms}ms · {timeAgo(a.created_at)}
                      </span>
                    </span>
                  </li>
                ))}
              </ul>
            )}
          </section>

          <section className="panel" aria-label="Approvals">
            <div className="panel-head">
              <span className="panel-title">⚑ Approvals</span>
              <Link to="/approvals" className="link">
                Open queue →
              </Link>
            </div>
            <p className="muted" style={{ fontSize: 13 }}>
              High-impact actions Draven proposes appear here for your explicit
              approval before anything executes. Nothing consequential runs on
              voice alone.
            </p>
          </section>

          {isAdmin && (
            <section className="panel" aria-label="AI provider configuration">
              <div className="panel-head">
                <span className="panel-title">⬢ AI provider</span>
                {provider && (
                  <span className={`pill${provider.configured ? "" : " warn"}`}>
                    <span className="dot" />
                    {provider.configured ? "connected" : "not configured"}
                  </span>
                )}
              </div>
              <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
                <label style={{ fontSize: 12.5 }}>
                  <span className="muted" style={{ display: "block", marginBottom: 4 }}>
                    Provider
                  </span>
                  <select
                    className="input"
                    value={pProvider}
                    onChange={(e) => setPProvider(e.target.value)}
                  >
                    <option value="stub">stub (built-in, no key)</option>
                    <option value="anthropic">Anthropic</option>
                    <option value="openai-compatible">OpenAI-compatible</option>
                    <option value="elevenlabs">ElevenLabs (voice/TTS)</option>
                  </select>
                </label>
                <label style={{ fontSize: 12.5 }}>
                  <span className="muted" style={{ display: "block", marginBottom: 4 }}>
                    Model
                  </span>
                  <input
                    className="input"
                    value={pModel}
                    onChange={(e) => setPModel(e.target.value)}
                    placeholder="e.g. claude-opus-4-6"
                  />
                </label>
                {pProvider === "openai-compatible" && (
                  <label style={{ fontSize: 12.5 }}>
                    <span className="muted" style={{ display: "block", marginBottom: 4 }}>
                      Base URL (https only)
                    </span>
                    <input
                      className="input"
                      value={pBaseUrl}
                      onChange={(e) => setPBaseUrl(e.target.value)}
                      placeholder="https://api.example.com/v1"
                      inputMode="url"
                    />
                  </label>
                )}
                {pProvider !== "stub" && (
                  <label style={{ fontSize: 12.5 }}>
                    <span className="muted" style={{ display: "block", marginBottom: 4 }}>
                      API key (stored encrypted, server-side only)
                    </span>
                    <input
                      className="input"
                      type="password"
                      value={pKey}
                      onChange={(e) => setPKey(e.target.value)}
                      placeholder="Leave blank to keep existing"
                      autoComplete="off"
                    />
                  </label>
                )}
                {pMsg && (
                  <div className="notice" style={{ fontSize: 12.5 }}>
                    {pMsg}
                  </div>
                )}
                <div style={{ display: "flex", gap: 8 }}>
                  <button
                    type="button"
                    className="btn btn-sm"
                    onClick={testProvider}
                    disabled={pBusy}
                  >
                    Test connection
                  </button>
                  <button
                    type="button"
                    className="btn btn-sm btn-primary"
                    onClick={saveProvider}
                    disabled={pBusy}
                  >
                    Save
                  </button>
                </div>
              </div>
            </section>
          )}
        </div>
      </div>
    </div>
  );
}
