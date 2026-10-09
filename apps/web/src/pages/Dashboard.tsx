import { useCallback, useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import {
  analyticsApi,
  assetApi,
  autopilotApi,
  campaignApi,
  opsApi,
} from "../lib/api";
import type {
  AnalyticsOverview,
  Asset,
  BrainResponse,
  Campaign,
  ContentPlan,
  OpsActivityResponse,
} from "../lib/api";
import { formatNumber } from "../lib/format";
import { ErrorBanner } from "../components/ui";
import { LAYER_COLORS } from "../lib/brainTheme";
import {
  AreasView,
  CircleView,
  LinksView,
  OrbitView,
  RingsView,
  TimelineView,
} from "./Brain";
import type { BrainView } from "../lib/brainTheme";
import { BRAIN_VIEWS, BRAIN_VIEW_LABELS } from "../lib/brainTheme";

/* ------------------------------------------------------------------ */
/* Live clock                                                          */
/* ------------------------------------------------------------------ */

function useNow(intervalMs = 1000) {
  const [now, setNow] = useState(() => new Date());
  useEffect(() => {
    const t = window.setInterval(() => setNow(new Date()), intervalMs);
    return () => window.clearInterval(t);
  }, [intervalMs]);
  return now;
}

function timeAgo(iso: string): string {
  const s = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  if (s < 3600) return `${Math.max(1, Math.floor(s / 60))}m`;
  if (s < 86400) return `${Math.floor(s / 3600)}h`;
  return `${Math.floor(s / 86400)}d`;
}

/* ------------------------------------------------------------------ */
/* Today panel — clock, focus, week activity                           */
/* ------------------------------------------------------------------ */

function TodayPanel({
  overview,
  campaigns,
}: {
  overview: AnalyticsOverview | null;
  campaigns: Campaign[];
}) {
  const now = useNow(1000);
  const running = campaigns.filter((c) => c.status === "running").length;
  const days = (overview?.by_day ?? []).slice(-7);
  const max = Math.max(1, ...days.map((d) => d.sent));
  const dayNames = ["S", "M", "T", "W", "T", "F", "S"];

  return (
    <section className="panel" aria-label="Today">
      <div className="panel-head">
        <span className="panel-title">◷ Today</span>
      </div>
      <div className="today-clock">
        {now.toLocaleTimeString("en-US", {
          hour: "2-digit",
          minute: "2-digit",
          hour12: false,
        })}
      </div>
      <div className="today-date">
        {now.toLocaleDateString("en-US", {
          weekday: "long",
          month: "long",
          day: "numeric",
        })}
      </div>

      <div className="today-meta">
        <div>
          <div className="focus-row">
            <span className="muted">Live campaigns</span>
            <strong>
              {running}/{campaigns.length}
            </strong>
          </div>
          <div className="meter" role="progressbar" aria-valuenow={running} aria-valuemax={Math.max(1, campaigns.length)}>
            <span
              style={{
                width: `${campaigns.length ? (running / campaigns.length) * 100 : 0}%`,
              }}
            />
          </div>
        </div>

        <div>
          <div className="focus-row">
            <span className="muted">Sends this week</span>
            <strong>{formatNumber(days.reduce((n, d) => n + d.sent, 0))}</strong>
          </div>
          <div className="week-bars" aria-hidden="true">
            {days.map((d) => {
              const dt = new Date(d.date);
              const isToday = dt.toDateString() === now.toDateString();
              return (
                <div key={d.date} className={`week-day${isToday ? " today" : ""}`}>
                  <div className="week-bar">
                    <span
                      style={{
                        height: `${Math.max(6, (d.sent / max) * 100)}%`,
                        animationDelay: `${days.indexOf(d) * 0.06}s`,
                      }}
                    />
                  </div>
                  <span className="week-day-label">{dayNames[dt.getDay()]}</span>
                </div>
              );
            })}
          </div>
        </div>
      </div>
    </section>
  );
}

/* ------------------------------------------------------------------ */
/* Brain mini — animated rings, compact                                 */
/* ------------------------------------------------------------------ */

function BrainMini({ data }: { data: BrainResponse }) {
  const [view, setView] = useState<BrainView>("rings");
  const [query, setQuery] = useState("");
  const [layer, setLayer] = useState<string>("all");

  const layers = useMemo(() => {
    const q = query.trim().toLowerCase();
    return data.layers
      .map((l) => ({
        ...l,
        nodes: l.nodes.filter(
          (n) =>
            (layer === "all" || l.key === layer) &&
            (!q || n.label.toLowerCase().includes(q))
        ),
      }))
      .filter((l) => l.nodes.length > 0 || !q);
  }, [data, query, layer]);

  const total = data.layers.reduce((n, l) => n + l.count, 0);

  return (
    <section className="panel brain-panel" aria-label="Brain">
      <div className="brain-panel-head">
        <div className="brain-tabs" role="tablist" aria-label="Graph views">
          {BRAIN_VIEWS.map((v) => (
            <button
              key={v}
              type="button"
              role="tab"
              aria-selected={view === v}
              className={`brain-tab${view === v ? " brain-tab-active" : ""}`}
              onClick={() => setView(v)}
            >
              {BRAIN_VIEW_LABELS[v]}
            </button>
          ))}
        </div>
        <div className="brain-stats">
          <strong>{formatNumber(total)}</strong> nodes ·{" "}
          <strong>{formatNumber(data.links.length)}</strong> links
        </div>
      </div>

      <div className="brain-search-row">
        <input
          className="brain-search"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder="Search nodes, files, or campaigns…"
          aria-label="Search graph nodes"
        />
      </div>
      <div className="brain-chips">
        <button
          type="button"
          className={`chip${layer === "all" ? " chip-active" : ""}`}
          onClick={() => setLayer("all")}
        >
          all
        </button>
        {data.layers.map((l) => (
          <button
            key={l.key}
            type="button"
            className={`chip${layer === l.key ? " chip-active" : ""}`}
            onClick={() => setLayer(l.key)}
          >
            <span className="dot" style={{ background: LAYER_COLORS[l.key] }} />
            {l.label}
          </button>
        ))}
      </div>

      <div className="brain-canvas" key={view + layer + query} role="tabpanel">
        {view === "rings" && <RingsView layers={layers} />}
        {view === "circle" && <CircleView layers={layers} />}
        {view === "areas" && <AreasView layers={layers} />}
        {view === "links" && <LinksView layers={layers} links={data.links} />}
        {view === "timeline" && <TimelineView data={data} />}
        {view === "orbit" && <OrbitView layers={layers} />}
      </div>

      <div className="brain-foot">
        <span className="muted" style={{ fontSize: 12 }}>
          Live from your business data
        </span>
        <Link to="/brain" className="btn btn-sm">
          Open full brain →
        </Link>
      </div>
    </section>
  );
}

/* ------------------------------------------------------------------ */
/* Needs panel — approvals awaiting review                              */
/* ------------------------------------------------------------------ */

function NeedsPanel({ assets, total }: { assets: Asset[]; total: number }) {
  return (
    <section className="panel" aria-label="Needs attention">
      <div className="panel-head">
        <span className="panel-title">⚑ Needs you</span>
        <Link to="/approvals" className="link">
          View all →
        </Link>
      </div>
      <div className="needs-count">{total}</div>
      <div className="needs-label">Awaiting review</div>
      {assets.map((a, i) => (
        <Link
          key={a.id}
          to="/approvals"
          className="action-card"
          style={{ animationDelay: `${0.1 + i * 0.08}s`, textDecoration: "none", color: "inherit" }}
        >
          <span className="action-avatar" aria-hidden="true">
            {(a.title || "A").slice(0, 1).toUpperCase()}
          </span>
          <span className="action-body">
            <span className="action-title">{a.title}</span>
            <span className="action-detail" style={{ display: "block" }}>
              {a.kind} · v{a.version}
            </span>
          </span>
          <span className="action-age">{timeAgo(a.created_at)} ago</span>
        </Link>
      ))}
      {total === 0 && (
        <p className="muted" style={{ fontSize: 13, marginTop: 10 }}>
          Nothing waiting — the queue is clear.
        </p>
      )}
    </section>
  );
}

/* ------------------------------------------------------------------ */
/* Routines panel — autopilot jobs                                      */
/* ------------------------------------------------------------------ */

function RoutinesPanel({ plan }: { plan: ContentPlan | null }) {
  const items = plan?.items.slice(0, 5) ?? [];
  return (
    <section className="panel" aria-label="Routines">
      <div className="panel-head">
        <span className="panel-title">◔ Routines</span>
        <span className="muted" style={{ fontSize: 12 }}>
          {plan ? `${items.length} this week` : "autopilot"}
        </span>
      </div>
      {plan ? (
        <>
          <table className="routine-table">
            <thead>
              <tr>
                <th>Item</th>
                <th>Channel</th>
                <th>Status</th>
              </tr>
            </thead>
            <tbody>
              {items.map((it, i) => (
                <tr key={it.asset_id ?? `${it.day}-${i}`} className={i === 0 ? "routine-next" : undefined}>
                  <td>{it.title}</td>
                  <td className="muted">{it.channel}</td>
                  <td>
                    {plan.status === "approved" ? (
                      <span className="status-check">✓</span>
                    ) : (
                      <span className="action-age">next</span>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          <div className="routine-actions">
            <Link to="/autopilot" className="btn btn-sm">
              + Review plan
            </Link>
            <Link to="/autopilot" className="btn btn-sm btn-primary">
              ▶ Run now
            </Link>
          </div>
        </>
      ) : (
        <>
          <p className="muted" style={{ fontSize: 13 }}>
            No weekly plan yet. Autopilot drafts one every Monday — review it
            here and it becomes scheduled campaigns.
          </p>
          <div className="routine-actions">
            <Link to="/autopilot" className="btn btn-sm btn-primary">
              Open autopilot
            </Link>
          </div>
        </>
      )}
    </section>
  );
}

/* ------------------------------------------------------------------ */
/* System panel                                                         */
/* ------------------------------------------------------------------ */

function SystemPanel({
  ops,
  apiOk,
}: {
  ops: OpsActivityResponse | null;
  apiOk: boolean;
}) {
  const rows = [
    { label: "API", ok: apiOk, val: apiOk ? "live" : "down" },
    { label: "JOBS", ok: true, val: ops ? `${ops.counters.in_flight} in flight` : "—" },
    { label: "SENDS", ok: true, val: ops ? `${ops.counters.sends_today} today` : "—" },
  ];
  return (
    <section className="panel" aria-label="System">
      <div className="panel-head">
        <span className="panel-title">⬢ System</span>
        <span className="sys-nominal">
          <span className="dot" />
          {apiOk ? "All systems nominal" : "API unreachable"}
        </span>
      </div>
      {rows.map((r) => (
        <div className="sys-row" key={r.label}>
          <span className="sys-label">{r.label}</span>
          <div className="meter" style={{ flex: 1 }}>
            <span
              style={{
                width: r.ok ? "100%" : "8%",
                background: r.ok
                  ? "linear-gradient(90deg, var(--green), var(--blue))"
                  : "var(--red)",
              }}
            />
          </div>
          <span className="sys-val">{r.val}</span>
        </div>
      ))}
    </section>
  );
}

/* ------------------------------------------------------------------ */
/* Page                                                                 */
/* ------------------------------------------------------------------ */

export default function DashboardPage() {
  const [overview, setOverview] = useState<AnalyticsOverview | null>(null);
  const [campaigns, setCampaigns] = useState<Campaign[]>([]);
  const [approvals, setApprovals] = useState<{ items: Asset[]; total: number }>({
    items: [],
    total: 0,
  });
  const [brain, setBrain] = useState<BrainResponse | null>(null);
  const [plan, setPlan] = useState<ContentPlan | null>(null);
  const [ops, setOps] = useState<OpsActivityResponse | null>(null);
  const [apiOk, setApiOk] = useState(true);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<unknown>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const [ov, camps, inReview, br, pl, activity] = await Promise.all([
        analyticsApi.overview({ days: 30 }).catch(() => null),
        campaignApi.list().catch(() => [] as Campaign[]),
        assetApi.listPage({ status: "in_review", limit: 3 }).catch(() => ({ items: [], total: 0 })),
        opsApi.brain().catch(() => null),
        autopilotApi.plan().catch(() => null),
        opsApi.activity(5).catch(() => null),
      ]);
      setOverview(ov);
      setCampaigns(camps);
      setApprovals({ items: inReview.items, total: inReview.total });
      setBrain(br);
      setPlan(pl);
      setOps(activity);
      setApiOk(true);
    } catch (err) {
      setError(err);
      setApiOk(false);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  if (loading) {
    return (
      <div className="cmd-grid">
        {[0, 1, 2].map((i) => (
          <div className="cmd-col" key={i}>
            <div className="panel">
              <div className="skeleton" style={{ height: 280 }} />
            </div>
          </div>
        ))}
      </div>
    );
  }

  return (
    <div>
      <ErrorBanner error={error} onRetry={load} />
      <div className="cmd-grid">
        <div className="cmd-col">
          <TodayPanel overview={overview} campaigns={campaigns} />
        </div>
        <div className="cmd-col">
          {brain ? (
            <BrainMini data={brain} />
          ) : (
            <section className="panel brain-panel">
              <div className="brain-panel-head">
                <span className="panel-title">Brain</span>
              </div>
              <div style={{ padding: 24 }}>
                <p className="muted">
                  The knowledge graph will appear here once you have brand
                  kits, assets, or campaigns.
                </p>
                <Link to="/brain" className="btn btn-sm">
                  Open brain →
                </Link>
              </div>
            </section>
          )}
        </div>
        <div className="cmd-col cmd-col-right">
          <NeedsPanel assets={approvals.items} total={approvals.total} />
          <RoutinesPanel plan={plan} />
          <SystemPanel ops={ops} apiOk={apiOk} />
        </div>
      </div>
    </div>
  );
}
