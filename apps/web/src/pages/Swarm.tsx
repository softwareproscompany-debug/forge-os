/**
 * Swarm — the DRAVEN 12-agent mission-control view.
 *
 * JARVIS cyan HUD identity (the AI's own domain inside the PAV OS app):
 * agent cards with live running states, a real event feed polled from the
 * backend (every row written by the orchestrator — never fabricated), a
 * one-click investor demo scenario, and a compact orb that mirrors run
 * state. Stage-robust: generous polling, honest stub-mode banner, no
 * crashes when the backend is unreachable.
 */
import { useCallback, useEffect, useRef, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { apiFetch } from "../lib/api";
import { JarvisOrb } from "../components/JarvisOrb";
import type { OrbState } from "../lib/orbState";

interface SwarmAgent {
  id: string;
  name: string;
  role: string;
  allowed_tools: string[];
  max_depth: number;
}

interface SwarmEvent {
  seq: number;
  agent_id: string | null;
  kind: string;
  message: string;
  data: Record<string, unknown>;
  created_at: string;
}

interface AgentRuntime {
  status: "idle" | "running" | "done" | "degraded" | "error";
  activity: string;
  tools: number;
  durationMs: number;
}

interface RunDetail {
  run_id: string;
  goal: string;
  status: string;
  current_phase: string | null;
  agent_results: Record<string, {
    agent_id: string;
    status: string;
    summary: string;
    tools_used: { tool: string; status: string; duration_ms: number }[];
    approvals_needed: { tool: string; description: string }[];
    degraded: boolean;
    duration_ms: number;
  }>;
  result_summary: string | null;
  error: string | null;
  created_at: string;
  completed_at: string | null;
}

const POLL_MS = 1500;

function kindGlyph(kind: string): string {
  switch (kind) {
    case "agent_start": return "▶";
    case "agent_done": return "■";
    case "tool_call": return "⚙";
    case "run_start": return "⬢";
    case "run_done": return "✔";
    case "error": return "⚠";
    default: return "·";
  }
}

export default function Swarm() {
  const navigate = useNavigate();
  const [agents, setAgents] = useState<SwarmAgent[]>([]);
  const [provider, setProvider] = useState<{ provider: string; configured: boolean } | null>(null);
  const [runtime, setRuntime] = useState<Record<string, AgentRuntime>>({});
  const [events, setEvents] = useState<SwarmEvent[]>([]);
  const [runStatus, setRunStatus] = useState<string>("idle");
  const [goal, setGoal] = useState("");
  const [detail, setDetail] = useState<RunDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [orbState, setOrbState] = useState<OrbState>("idle");
  const seqRef = useRef(0);
  const feedRef = useRef<HTMLDivElement>(null);
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);

  const resetRuntime = useCallback((list: SwarmAgent[]) => {
    const r: Record<string, AgentRuntime> = {};
    for (const a of list) r[a.id] = { status: "idle", activity: "standing by", tools: 0, durationMs: 0 };
    setRuntime(r);
  }, []);

  useEffect(() => {
    (async () => {
      try {
        const list = await apiFetch<SwarmAgent[]>("/draven/swarm/agents");
        setAgents(list);
        resetRuntime(list);
      } catch { /* agent grid degrades gracefully */ }
      try {
        const p = await apiFetch<{ provider: string; configured: boolean }>("/draven/provider");
        setProvider({ provider: p.provider, configured: p.configured });
      } catch { /* banner degrades gracefully */ }
    })();
    return () => { if (pollRef.current) clearInterval(pollRef.current); };
  }, [resetRuntime]);

  const applyEvent = useCallback((ev: SwarmEvent) => {
    setEvents((prev) => [...prev.slice(-399), ev]);
    if (!ev.agent_id) return;
    setRuntime((prev) => {
      const cur = prev[ev.agent_id!] ?? { status: "idle", activity: "", tools: 0, durationMs: 0 };
      const next = { ...cur };
      if (ev.kind === "agent_start") { next.status = "running"; next.activity = ev.message; }
      else if (ev.kind === "tool_call") {
        next.tools += 1;
        next.activity = ev.message;
        if (next.status === "idle") next.status = "running";
      } else if (ev.kind === "agent_done") {
        const st = (ev.data?.status as string) ?? "done";
        next.status = st === "ok" ? "done" : st === "degraded" ? "degraded" : st === "error" ? "error" : "done";
        next.activity = ev.message;
        next.durationMs = (ev.data?.duration_ms as number) ?? 0;
      } else if (ev.kind === "error") { next.status = "error"; next.activity = ev.message; }
      return { ...prev, [ev.agent_id!]: next };
    });
  }, []);

  const poll = useCallback(async (id: string) => {
    try {
      const r = await apiFetch<{ events: SwarmEvent[]; latest_seq: number }>(
        `/draven/swarm/runs/${id}/events?after=${seqRef.current}`
      );
      for (const ev of r.events) { seqRef.current = Math.max(seqRef.current, ev.seq); applyEvent(ev); }
      const d = await apiFetch<RunDetail>(`/draven/swarm/runs/${id}`);
      setRunStatus(d.status);
      setDetail(d);
      if (d.status === "completed" || d.status === "failed") {
        if (pollRef.current) { clearInterval(pollRef.current); pollRef.current = null; }
        setOrbState(d.status === "completed" ? "success" : "error");
      }
    } catch {
      /* polling is best-effort; the next tick retries */
    }
  }, [applyEvent]);

  const startRun = useCallback(async (demo: boolean) => {
    setError(null);
    setDetail(null);
    setEvents([]);
    seqRef.current = 0;
    resetRuntime(agents);
    setOrbState("processing");
    setRunStatus("starting");
    try {
      const r = await apiFetch<{ run_id: string; status: string }>("/draven/swarm/run", {
        method: "POST",
        body: JSON.stringify(demo ? { goal: "demo", demo: true } : { goal }),
      });
      setRunStatus(r.status);
      if (pollRef.current) clearInterval(pollRef.current);
      pollRef.current = setInterval(() => void poll(r.run_id), POLL_MS);
      void poll(r.run_id);
    } catch (e) {
      setError(e instanceof Error ? e.message : "failed to start swarm run");
      setOrbState("error");
      setRunStatus("idle");
    }
  }, [agents, goal, poll, resetRuntime]);

  useEffect(() => {
    const el = feedRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [events]);

  const running = runStatus === "running" || runStatus === "queued" || runStatus === "starting";
  const stubMode = provider?.provider === "stub";

  return (
    <div className="sw">
      {/* HUD header */}
      <header className="sw-head">
        <div className="sw-brand">
          <span className="sw-hex" aria-hidden="true">⬢</span>
          <div>
            <h1 className="sw-title">AGENT SWARM</h1>
            <p className="sw-sub">12 agents · live orchestration · every action audited</p>
          </div>
        </div>
        <div className="sw-pills">
          <span className={`sw-pill${running ? " live" : ""}`}>
            <span className="sw-dot" aria-hidden="true" />
            {runStatus.toUpperCase()}
          </span>
          {provider && (
            <span className={`sw-pill${provider.configured ? "" : " warn"}`}>
              {provider.provider.toUpperCase()}{provider.configured ? "" : " · STUB"}
            </span>
          )}
        </div>
      </header>

      {stubMode && (
        <div className="sw-note" role="note">
          Stub provider active — agents run their real tools and report real data; generative
          reasoning is skipped honestly (marked <em>degraded</em>, never invented). Connect a
          live provider in <Link to="/draven" className="sw-link">Draven → AI provider</Link> for
          full reasoning.
        </div>
      )}
      {error && <div className="sw-error" role="alert">{error}</div>}

      {/* Command deck: orb + demo trigger + custom goal */}
      <section className="sw-deck" aria-label="Swarm controls">
        <div className="sw-orbbox">
          <JarvisOrb
            state={orbState}
            micStream={null}
            audioElement={null}
            onTap={() => navigate("/draven")}
            label="Open Draven voice workspace"
            theme="jarvis"
          />
        </div>
        <div className="sw-controls">
          <button
            type="button"
            className="sw-demo-btn"
            onClick={() => void startRun(true)}
            disabled={running}
          >
            {running ? "SWARM RUNNING…" : "▶ RUN INVESTOR DEMO"}
          </button>
          <p className="sw-hint">
            One click: plans and drafts this week's campaign across the full swarm.
          </p>
          <div className="sw-goalrow">
            <input
              className="sw-goal"
              value={goal}
              onChange={(e) => setGoal(e.target.value)}
              placeholder="Or type a goal — e.g. “qualify this week's leads”…"
              maxLength={2000}
              aria-label="Custom swarm goal"
              disabled={running}
            />
            <button
              type="button"
              className="sw-btn"
              onClick={() => void startRun(false)}
              disabled={running || !goal.trim()}
            >
              Run
            </button>
          </div>
        </div>
      </section>

      {/* Agent cards */}
      <section aria-label="Agents">
        <h2 className="sw-sec-title">AGENTS</h2>
        <div className="sw-grid">
          {agents.map((a) => {
            const r = runtime[a.id] ?? { status: "idle", activity: "standing by", tools: 0, durationMs: 0 };
            return (
              <article key={a.id} className={`sw-card sw-${r.status}`} aria-label={`${a.name}: ${r.status}`}>
                <div className="sw-card-head">
                  <span className="sw-card-name">{a.name}</span>
                  <span className={`sw-badge sw-${r.status}`}>{r.status.toUpperCase()}</span>
                </div>
                <p className="sw-card-role">{a.role}</p>
                <p className="sw-card-activity">{r.activity}</p>
                <div className="sw-card-foot">
                  <span>{r.tools} tool{r.tools === 1 ? "" : "s"}</span>
                  {r.durationMs > 0 && <span>{(r.durationMs / 1000).toFixed(1)}s</span>}
                </div>
              </article>
            );
          })}
        </div>
      </section>

      {/* Live event feed */}
      <section aria-label="Live activity">
        <h2 className="sw-sec-title">LIVE ACTIVITY</h2>
        <div className="sw-feed" ref={feedRef} role="log" aria-live="polite">
          {events.length === 0 && (
            <p className="sw-feed-empty">
              No activity yet — run the investor demo or give the swarm a goal.
            </p>
          )}
          {events.map((ev) => (
            <div key={ev.seq} className={`sw-ev sw-ev-${ev.kind}`}>
              <span className="sw-ev-glyph" aria-hidden="true">{kindGlyph(ev.kind)}</span>
              <span className="sw-ev-agent">{ev.agent_id ?? "swarm"}</span>
              <span className="sw-ev-msg">{ev.message}</span>
            </div>
          ))}
        </div>
      </section>

      {/* Results */}
      {detail && (detail.status === "completed" || detail.status === "failed") && (
        <section aria-label="Results">
          <h2 className="sw-sec-title">RESULT</h2>
          {detail.error && <div className="sw-error" role="alert">{detail.error}</div>}
          {detail.result_summary && (
            <pre className="sw-result">{detail.result_summary}</pre>
          )}
          <div className="sw-agents-out">
            {Object.values(detail.agent_results).map((r) => (
              <details key={r.agent_id} className="sw-agent-out">
                <summary>
                  <span className={`sw-badge sw-${r.status === "ok" ? "done" : r.status}`}>{r.status.toUpperCase()}</span>
                  {" "}{r.agent_id}
                  <span className="sw-muted"> · {r.tools_used.length} tools · {(r.duration_ms / 1000).toFixed(1)}s</span>
                </summary>
                <p>{r.summary}</p>
                {r.approvals_needed.length > 0 && (
                  <ul>
                    {r.approvals_needed.map((ap, i) => (
                      <li key={i} className="sw-approval">Approval needed: {ap.description}</li>
                    ))}
                  </ul>
                )}
              </details>
            ))}
          </div>
        </section>
      )}
    </div>
  );
}
