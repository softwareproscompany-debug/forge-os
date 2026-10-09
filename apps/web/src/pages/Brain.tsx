import { useEffect, useMemo, useState } from "react";
import { opsApi } from "../lib/api";
import type { BrainLayer, BrainNode, BrainResponse } from "../lib/api";
import {
  BRAIN_VIEWS,
  BRAIN_VIEW_LABELS,
  LAYER_COLORS,
  LINK_COLORS,
  TIMELINE_COLORS,
  type BrainView,
} from "../lib/brainTheme";
import {
  DataStamp,
  EmptyState,
  ErrorBanner,
  PageHeader,
} from "../components/ui";

const MAX_RING_DOTS = 44;
const MAX_COLUMN_NODES = 10;

const reducedMotion =
  typeof window !== "undefined" &&
  window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;

function useFilteredLayers(
  data: BrainResponse | null,
  query: string,
  layer: string
): BrainLayer[] {
  return useMemo(() => {
    if (!data) return [];
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
}

/* ------------------------------------------------------------------ */
/* Rings — concentric layer rings, draw-in + staggered node pops       */
/* ------------------------------------------------------------------ */

export function RingsView({ layers }: { layers: BrainLayer[] }) {
  const size = 640;
  const cx = size / 2;
  const cy = size / 2;
  const baseR = 78;
  const step = 56;
  const total = layers.reduce((n, l) => n + l.count, 0);

  return (
    <div>
      <svg
        className="brain-svg"
        viewBox={`0 0 ${size} ${size}`}
        role="img"
        aria-label="Concentric rings of the business knowledge graph"
      >
        {layers.map((layer, i) => {
          const r = baseR + i * step;
          const color = LAYER_COLORS[layer.key] ?? "#9c978b";
          const dots = layer.nodes.slice(0, MAX_RING_DOTS);
          return (
            <g key={layer.key}>
              <circle
                cx={cx}
                cy={cy}
                r={r}
                fill="none"
                stroke={color}
                strokeOpacity={0.32}
                strokeWidth={1.4}
                className={reducedMotion ? undefined : "ring-draw"}
                style={{ animationDelay: `${i * 0.14}s` }}
              />
              <text
                x={cx + r * Math.SQRT1_2}
                y={cy - r * Math.SQRT1_2 - 8}
                textAnchor="middle"
                className="brain-meta-label"
                fill={color}
              >
                {layer.label} · {layer.count}
              </text>
              {dots.map((node, j) => {
                const angle = (2 * Math.PI * j) / dots.length - Math.PI / 2;
                return (
                  <circle
                    key={node.id}
                    cx={cx + r * Math.cos(angle)}
                    cy={cy + r * Math.sin(angle)}
                    r={4}
                    fill={color}
                    opacity={0.92}
                    className={reducedMotion ? undefined : "node-pop"}
                    style={{
                      animationDelay: `${0.35 + i * 0.14 + j * 0.018}s`,
                    }}
                  >
                    <title>{node.label}</title>
                  </circle>
                );
              })}
            </g>
          );
        })}
        <circle
          cx={cx}
          cy={cy}
          r={30}
          fill="none"
          stroke="var(--accent)"
          strokeOpacity={0.55}
          strokeWidth={1.6}
          className={reducedMotion ? undefined : "node-pulse"}
        />
        <text
          x={cx}
          y={cy - 2}
          textAnchor="middle"
          fill="var(--text)"
          fontSize={30}
          fontWeight={700}
          fontFamily="var(--heading)"
        >
          {total}
        </text>
        <text x={cx} y={cy + 22} textAnchor="middle" className="brain-meta-label">
          nodes in graph
        </text>
      </svg>
      <div className="brain-legend">
        {layers.map((layer) => (
          <span key={layer.key} className="legend-item">
            <span
              className="dot"
              style={{ background: LAYER_COLORS[layer.key] }}
            />
            {layer.label} ({layer.count})
          </span>
        ))}
      </div>
    </div>
  );
}

/* ------------------------------------------------------------------ */
/* Circle — every node on one ring, grouped into layer arcs            */
/* ------------------------------------------------------------------ */

export function CircleView({ layers }: { layers: BrainLayer[] }) {
  const size = 640;
  const cx = size / 2;
  const cy = size / 2;
  const r = 236;

  const all: Array<{ node: BrainNode; layer: BrainLayer }> = [];
  layers.forEach((l) => l.nodes.forEach((n) => all.push({ node: n, layer: l })));
  const n = Math.max(1, all.length);

  return (
    <div>
      <svg
        className="brain-svg"
        viewBox={`0 0 ${size} ${size}`}
        role="img"
        aria-label="All nodes arranged on a single circle"
      >
        <circle
          cx={cx}
          cy={cy}
          r={r}
          fill="none"
          stroke="var(--border-strong)"
          strokeWidth={1.2}
          className={reducedMotion ? undefined : "ring-draw"}
        />
        {layers.map((layer) => {
          const idx = all.findIndex((a) => a.layer.key === layer.key);
          if (idx < 0) return null;
          const color = LAYER_COLORS[layer.key] ?? "#9c978b";
          const mid = (2 * Math.PI * idx) / n - Math.PI / 2;
          return (
            <text
              key={layer.key}
              x={cx + (r + 34) * Math.cos(mid)}
              y={cy + (r + 34) * Math.sin(mid)}
              textAnchor="middle"
              className="brain-meta-label"
              fill={color}
            >
              {layer.label}
            </text>
          );
        })}
        {all.slice(0, 220).map((a, i) => {
          const angle = (2 * Math.PI * i) / n - Math.PI / 2;
          const color = LAYER_COLORS[a.layer.key] ?? "#9c978b";
          return (
            <circle
              key={a.node.id}
              cx={cx + r * Math.cos(angle)}
              cy={cy + r * Math.sin(angle)}
              r={4.5}
              fill={color}
              opacity={0.92}
              className={reducedMotion ? undefined : "node-pop"}
              style={{ animationDelay: `${0.2 + i * 0.008}s` }}
            >
              <title>{a.node.label}</title>
            </circle>
          );
        })}
        <text
          x={cx}
          y={cy + 8}
          textAnchor="middle"
          fill="var(--text)"
          fontSize={26}
          fontWeight={700}
          fontFamily="var(--heading)"
        >
          {n}
        </text>
      </svg>
    </div>
  );
}

/* ------------------------------------------------------------------ */
/* Areas — proportional blocks per layer                               */
/* ------------------------------------------------------------------ */

export function AreasView({ layers }: { layers: BrainLayer[] }) {
  const max = Math.max(1, ...layers.map((l) => l.count));
  return (
    <div className="brain-areas">
      {layers.map((layer, i) => {
        const color = LAYER_COLORS[layer.key] ?? "#9c978b";
        const pct = Math.max(layer.count > 0 ? 4 : 0, (layer.count / max) * 100);
        return (
          <div key={layer.key}>
            <div className="brain-area-row">
              <div className="brain-area-label" style={{ color }}>
                {layer.label}
              </div>
              <div className="brain-area-track">
                <div
                  className="brain-area-fill"
                  style={{
                    width: `${pct}%`,
                    background: `linear-gradient(90deg, ${color}44, ${color}18)`,
                    border: `1px solid ${color}55`,
                    color,
                    animationDelay: `${i * 0.08}s`,
                  }}
                >
                  {layer.count > 0 && layer.nodes.length > 0
                    ? layer.nodes.slice(0, 3).map((n) => n.label).join(" · ")
                    : ""}
                </div>
              </div>
              <div className="brain-area-count">{layer.count}</div>
            </div>
            {layer.nodes.length > 0 && (
              <div className="brain-nodes" style={{ paddingLeft: 142 }}>
                {layer.nodes.slice(0, 12).map((n, j) => (
                  <span
                    key={n.id}
                    className={`brain-chip${reducedMotion ? "" : " node-pop"}`}
                    style={{ animationDelay: `${0.2 + j * 0.03}s` }}
                    title={n.detail ?? n.label}
                  >
                    {n.label}
                  </span>
                ))}
                {layer.count > 12 && (
                  <span className="brain-chip">+{layer.count - 12} more</span>
                )}
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}

/* ------------------------------------------------------------------ */
/* Links — layered node-link columns                                   */
/* ------------------------------------------------------------------ */

export function LinksView({
  layers,
  links,
}: {
  layers: BrainLayer[];
  links: BrainResponse["links"];
}) {
  const W = 960;
  const H = 560;
  const colX = (i: number) => 80 + i * 200;

  const pos = new Map<string, { x: number; y: number }>();
  layers.forEach((layer, li) => {
    const nodes = layer.nodes.slice(0, MAX_COLUMN_NODES);
    const top = 70;
    const bottom = H - 40;
    nodes.forEach((node, ni) => {
      const y =
        nodes.length === 1
          ? (top + bottom) / 2
          : top + ((bottom - top) * ni) / (nodes.length - 1);
      pos.set(node.id, { x: colX(li), y });
    });
  });

  return (
    <div>
      <svg
        className="brain-svg"
        viewBox={`0 0 ${W} ${H}`}
        role="img"
        aria-label="Node-link graph of the business knowledge"
      >
        {links.map((link, i) => {
          const a = pos.get(link.source);
          const b = pos.get(link.target);
          if (!a || !b) return null;
          const mx = (a.x + b.x) / 2;
          return (
            <path
              key={i}
              d={`M ${a.x} ${a.y} C ${mx} ${a.y}, ${mx} ${b.y}, ${b.x} ${b.y}`}
              fill="none"
              stroke={LINK_COLORS[link.kind] ?? "#9c978b"}
              strokeOpacity={0.32}
              strokeWidth={1.2}
              className={reducedMotion ? undefined : "ring-draw"}
              style={{ animationDelay: `${Math.min(i * 0.02, 1.2)}s`, animationDuration: "0.9s" }}
            />
          );
        })}
        {layers.map((layer, li) => {
          const color = LAYER_COLORS[layer.key] ?? "#9c978b";
          const x = colX(li);
          const nodes = layer.nodes.slice(0, MAX_COLUMN_NODES);
          return (
            <g key={layer.key}>
              <text x={x} y={30} textAnchor="middle" className="brain-meta-label" fill={color}>
                {layer.label} · {layer.count}
              </text>
              {nodes.map((node, ni) => {
                const p = pos.get(node.id)!;
                const short =
                  node.label.length > 16 ? node.label.slice(0, 15) + "…" : node.label;
                return (
                  <g key={node.id}>
                    <circle
                      cx={p.x}
                      cy={p.y}
                      r={6}
                      fill={color}
                      opacity={0.92}
                      className={reducedMotion ? undefined : "node-pop"}
                      style={{ animationDelay: `${0.4 + li * 0.1 + ni * 0.05}s` }}
                    >
                      <title>{node.label}</title>
                    </circle>
                    <text x={p.x + 12} y={p.y + 4} className="brain-node-label">
                      {short}
                    </text>
                  </g>
                );
              })}
              {layer.count > nodes.length && (
                <text x={x} y={H - 12} textAnchor="middle" className="brain-meta-label">
                  +{layer.count - nodes.length} more
                </text>
              )}
            </g>
          );
        })}
      </svg>
      <div className="brain-legend">
        {Object.entries(LINK_COLORS).map(([kind, color]) => (
          <span key={kind} className="legend-item">
            <span className="dot" style={{ background: color }} />
            {kind}
          </span>
        ))}
      </div>
    </div>
  );
}

/* ------------------------------------------------------------------ */
/* Timeline — sends and engagement events over the last 30 days        */
/* ------------------------------------------------------------------ */

export function TimelineView({ data }: { data: BrainResponse }) {
  const days: Array<{ key: string; label: string }> = [];
  const now = new Date();
  for (let i = 29; i >= 0; i--) {
    const d = new Date(now);
    d.setDate(d.getDate() - i);
    days.push({
      key: d.toISOString().slice(0, 10),
      label: d.toLocaleDateString("en-US", { month: "numeric", day: "numeric" }),
    });
  }
  const kinds = ["sent", "opened", "clicked", "converted", "event"] as const;
  const buckets = new Map<string, Record<string, number>>();
  for (const p of data.timeline) {
    const key = new Date(p.at).toISOString().slice(0, 10);
    const b =
      buckets.get(key) ?? { sent: 0, opened: 0, clicked: 0, converted: 0, event: 0 };
    if (kinds.includes(p.kind as (typeof kinds)[number])) b[p.kind] += 1;
    buckets.set(key, b);
  }
  const max = Math.max(
    1,
    ...days.map((d) => kinds.reduce((n, k) => n + (buckets.get(d.key)?.[k] ?? 0), 0))
  );

  const W = 960;
  const H = 300;
  const padL = 36;
  const padB = 30;
  const chartW = W - padL - 12;
  const chartH = H - padB - 12;
  const bw = chartW / 30;

  return (
    <div>
      <svg
        className="brain-svg"
        viewBox={`0 0 ${W} ${H}`}
        role="img"
        aria-label="Sends and engagement over the last 30 days"
      >
        {[0.25, 0.5, 0.75, 1].map((f) => {
          const y = 12 + chartH * (1 - f);
          return (
            <g key={f}>
              <line x1={padL} y1={y} x2={W - 12} y2={y} className="chart-grid" strokeDasharray="3 3" />
              <text x={padL - 6} y={y + 4} textAnchor="end" className="chart-tick">
                {Math.round(max * f)}
              </text>
            </g>
          );
        })}
        {days.map((d, i) => {
          const b = buckets.get(d.key);
          const x = padL + i * bw + 1;
          let y = 12 + chartH;
          const segs: React.ReactNode[] = [];
          kinds.forEach((k) => {
            const v = b?.[k] ?? 0;
            if (!v) return;
            const h = (v / max) * chartH;
            y -= h;
            segs.push(
              <rect
                key={k}
                x={x}
                y={y}
                width={bw - 2}
                height={Math.max(h, 1.5)}
                fill={TIMELINE_COLORS[k]}
                opacity={0.88}
                className={reducedMotion ? undefined : "node-pop"}
                style={{ animationDelay: `${i * 0.02}s` }}
              >
                <title>{`${d.label} — ${k}: ${v}`}</title>
              </rect>
            );
          });
          return (
            <g key={d.key}>
              {segs}
              {(i % 5 === 0 || i === days.length - 1) && (
                <text x={x} y={H - 8} className="chart-tick">
                  {d.label}
                </text>
              )}
            </g>
          );
        })}
      </svg>
      <div className="brain-legend">
        {kinds.map((k) => (
          <span key={k} className="legend-item">
            <span className="dot" style={{ background: TIMELINE_COLORS[k] }} />
            {k}
          </span>
        ))}
      </div>
    </div>
  );
}

/* ------------------------------------------------------------------ */
/* 3D Orbit — nodes travelling tilted elliptical orbits (SMIL)         */
/* ------------------------------------------------------------------ */

export function OrbitView({ layers }: { layers: BrainLayer[] }) {
  const W = 960;
  const H = 560;
  const cx = W / 2;
  const cy = H / 2;

  const orbits = layers.slice(0, 5).map((layer, i) => ({
    layer,
    rx: 150 + i * 62,
    ry: (150 + i * 62) * 0.36,
    dur: 26 - i * 3.5,
    nodes: layer.nodes.slice(0, 14),
  }));

  return (
    <div>
      <svg
        className="brain-svg"
        viewBox={`0 0 ${W} ${H}`}
        role="img"
        aria-label="Nodes orbiting in 3D perspective"
      >
        {/* faint depth dots */}
        {Array.from({ length: 40 }).map((_, i) => (
          <circle
            key={i}
            cx={(i * 197) % W}
            cy={(i * 331) % H}
            r={1}
            fill="var(--muted)"
            opacity={0.25}
          />
        ))}
        {orbits.map((o, oi) => {
          const color = LAYER_COLORS[o.layer.key] ?? "#9c978b";
          const pathId = `orbit-path-${oi}`;
          return (
            <g key={o.layer.key}>
              <ellipse
                id={pathId}
                cx={cx}
                cy={cy}
                rx={o.rx}
                ry={o.ry}
                fill="none"
                stroke={color}
                strokeOpacity={0.28}
                strokeWidth={1.2}
              />
              <text
                x={cx + o.rx * 0.72}
                y={cy - o.ry * 0.72 - 24}
                textAnchor="middle"
                className="brain-meta-label"
                fill={color}
              >
                {o.layer.label}
              </text>
              {o.nodes.map((n, ni) => (
                <circle key={n.id} r={4.2} fill={color} opacity={0.95}>
                  <title>{n.label}</title>
                  {!reducedMotion && (
                    <animateMotion
                      dur={`${o.dur}s`}
                      begin={`${-(ni * o.dur) / o.nodes.length}s`}
                      repeatCount="indefinite"
                    >
                      <mpath href={`#${pathId}`} />
                    </animateMotion>
                  )}
                </circle>
              ))}
            </g>
          );
        })}
        {/* center mass */}
        <circle cx={cx} cy={cy} r={10} fill="var(--accent)" opacity={0.9} className={reducedMotion ? undefined : "node-pulse"} />
        <text x={cx} y={cy + 30} textAnchor="middle" className="brain-meta-label" fill="var(--accent)">
          {layers.reduce((n, l) => n + l.count, 0)} nodes in orbit
        </text>
      </svg>
      <div className="brain-legend">
        {orbits.map((o) => (
          <span key={o.layer.key} className="legend-item">
            <span className="dot" style={{ background: LAYER_COLORS[o.layer.key] }} />
            {o.layer.label} ({o.layer.count})
          </span>
        ))}
      </div>
    </div>
  );
}

/* ------------------------------------------------------------------ */
/* Page                                                                */
/* ------------------------------------------------------------------ */

export default function BrainPage() {
  const [data, setData] = useState<BrainResponse | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [loading, setLoading] = useState(true);
  const [view, setView] = useState<BrainView>("rings");
  const [query, setQuery] = useState("");
  const [layer, setLayer] = useState<string>("all");

  useEffect(() => {
    let cancelled = false;
    opsApi
      .brain()
      .then((d) => {
        if (!cancelled) {
          setData(d);
          setLoading(false);
        }
      })
      .catch((err) => {
        if (!cancelled) {
          setError(err);
          setLoading(false);
        }
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const layers = useFilteredLayers(data, query, layer);
  const total = data?.layers.reduce((n, l) => n + l.count, 0) ?? 0;

  return (
    <div className="page-wide" style={{ maxWidth: 1100, margin: "0 auto" }}>
      <PageHeader
        title="Brain"
        subtitle="One knowledge graph — brand kits → assets → campaigns → sends → events — drawn six ways from the same live data."
        actions={data ? <DataStamp at={data.as_of} /> : undefined}
      />

      {loading ? (
        <div className="panel">
          <div className="skeleton" style={{ height: 420 }} />
        </div>
      ) : error || !data ? (
        <ErrorBanner error={error} onRetry={() => window.location.reload()} />
      ) : total === 0 ? (
        <EmptyState
          title="No graph data yet"
          hint="Create a brand kit, generate an asset, or launch a campaign and the brain will start mapping your business."
        />
      ) : (
        <section className="panel brain-panel">
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
              <strong>{total}</strong> nodes ·{" "}
              <strong>{data.links.length}</strong> links ·{" "}
              <strong>{data.timeline.length}</strong> events
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
        </section>
      )}
    </div>
  );
}
