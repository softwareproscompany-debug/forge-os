import { useEffect, useState } from "react";
import { opsApi } from "../lib/api";
import type { BrainLayer, BrainResponse } from "../lib/api";
import {
  DataStamp,
  EmptyState,
  ErrorBanner,
  PageHeader,
} from "../components/ui";

/* ------------------------------------------------------------------ */
/* Layer colors — one per FORGE layer, all inside the cyberpunk palette */
/* ------------------------------------------------------------------ */

const LAYER_COLORS: Record<string, string> = {
  foundation: "#00f5ff",
  origination: "#397bff",
  growth: "#9d4edd",
  reach: "#ff2bd6",
  evidence: "#39ff88",
};

const LINK_COLORS: Record<string, string> = {
  brand: "#00f5ff",
  asset: "#397bff",
  campaign: "#ff2bd6",
  engagement: "#39ff88",
};

const TIMELINE_COLORS: Record<string, string> = {
  sent: "#00f5ff",
  opened: "#397bff",
  clicked: "#ff2bd6",
  converted: "#39ff88",
  event: "#9aa8c7",
};

type View = "rings" | "links" | "timeline" | "areas";

const VIEWS: Array<{ key: View; label: string }> = [
  { key: "rings", label: "Rings" },
  { key: "links", label: "Links" },
  { key: "timeline", label: "Timeline" },
  { key: "areas", label: "Areas" },
];

const MAX_RING_DOTS = 40;
const MAX_COLUMN_NODES = 10;

/* ------------------------------------------------------------------ */
/* Rings — concentric layer rings, nodes as dots on their ring         */
/* ------------------------------------------------------------------ */

function RingsView({ layers }: { layers: BrainLayer[] }) {
  const size = 600;
  const cx = size / 2;
  const cy = size / 2;
  const baseR = 64;
  const step = 48;

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
          const color = LAYER_COLORS[layer.key] ?? "#9aa8c7";
          const dots = layer.nodes.slice(0, MAX_RING_DOTS);
          return (
            <g key={layer.key}>
              <circle
                cx={cx}
                cy={cy}
                r={r}
                fill="none"
                stroke={color}
                strokeOpacity={0.35}
                strokeWidth={1.5}
              />
              <text
                x={cx + r * Math.SQRT1_2}
                y={cy - r * Math.SQRT1_2 - 6}
                className="brain-meta-label"
                textAnchor="middle"
                fill={color}
              >
                {layer.label} · {layer.count}
              </text>
              {dots.map((node, j) => {
                const angle = (2 * Math.PI * j) / dots.length - Math.PI / 2;
                const x = cx + r * Math.cos(angle);
                const y = cy + r * Math.sin(angle);
                return (
                  <circle
                    key={node.id}
                    cx={x}
                    cy={y}
                    r={3.5}
                    fill={color}
                    opacity={0.9}
                  >
                    <title>{node.label}</title>
                  </circle>
                );
              })}
            </g>
          );
        })}
        <text
          x={cx}
          y={cy - 6}
          textAnchor="middle"
          fill="#f3f6ff"
          fontSize={34}
          fontWeight={700}
          fontFamily="Space Grotesk, Inter, sans-serif"
        >
          {layers.reduce((n, l) => n + l.count, 0)}
        </text>
        <text x={cx} y={cy + 20} textAnchor="middle" className="brain-meta-label">
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
/* Links — layered node-link columns, brand → asset → campaign → …      */
/* ------------------------------------------------------------------ */

function LinksView({
  layers,
  links,
}: {
  layers: BrainLayer[];
  links: BrainResponse["links"];
}) {
  const W = 960;
  const H = 560;
  const colX = (i: number) => 80 + i * 200;

  // Position map: node id -> {x, y}
  const pos = new Map<string, { x: number; y: number }>();
  const shownNodes = new Map<string, number>(); // layer key -> shown count
  layers.forEach((layer, li) => {
    const nodes = layer.nodes.slice(0, MAX_COLUMN_NODES);
    shownNodes.set(layer.key, nodes.length);
    const top = 70;
    const bottom = H - 40;
    nodes.forEach((node, ni) => {
      const y =
        nodes.length === 1 ? (top + bottom) / 2 : top + ((bottom - top) * ni) / (nodes.length - 1);
      pos.set(node.id, { x: colX(li), y });
    });
  });
  const nodeById = new Map<string, { label: string; layer: string }>();
  layers.forEach((layer) =>
    layer.nodes.forEach((n) => nodeById.set(n.id, { label: n.label, layer: layer.key }))
  );

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
              stroke={LINK_COLORS[link.kind] ?? "#9aa8c7"}
              strokeOpacity={0.35}
              strokeWidth={1.2}
            />
          );
        })}
        {layers.map((layer, li) => {
          const color = LAYER_COLORS[layer.key] ?? "#9aa8c7";
          const x = colX(li);
          const nodes = layer.nodes.slice(0, MAX_COLUMN_NODES);
          return (
            <g key={layer.key}>
              <text x={x} y={30} textAnchor="middle" className="brain-meta-label" fill={color}>
                {layer.label} · {layer.count}
              </text>
              {nodes.map((node) => {
                const p = pos.get(node.id)!;
                const short =
                  node.label.length > 16 ? node.label.slice(0, 15) + "…" : node.label;
                return (
                  <g key={node.id}>
                    <circle cx={p.x} cy={p.y} r={6} fill={color} opacity={0.9}>
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

function TimelineView({ data }: { data: BrainResponse }) {
  const days: Array<{ key: string; label: string }> = [];
  const now = new Date();
  for (let i = 29; i >= 0; i--) {
    const d = new Date(now);
    d.setDate(d.getDate() - i);
    const key = d.toISOString().slice(0, 10);
    days.push({
      key,
      label: d.toLocaleDateString("en-US", { month: "numeric", day: "numeric" }),
    });
  }
  const kinds = ["sent", "opened", "clicked", "converted", "event"] as const;
  const buckets = new Map<string, Record<string, number>>();
  for (const p of data.timeline) {
    const key = new Date(p.at).toISOString().slice(0, 10);
    const b = buckets.get(key) ?? { sent: 0, opened: 0, clicked: 0, converted: 0, event: 0 };
    if (kinds.includes(p.kind as (typeof kinds)[number])) b[p.kind] += 1;
    buckets.set(key, b);
  }
  const max = Math.max(1, ...days.map((d) => kinds.reduce((n, k) => n + (buckets.get(d.key)?.[k] ?? 0), 0)));

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
          const total = kinds.reduce((n, k) => n + (b?.[k] ?? 0), 0);
          const x = padL + i * bw + 1;
          let y = 12 + chartH;
          return (
            <g key={d.key}>
              {kinds.map((k) => {
                const v = b?.[k] ?? 0;
                if (!v) return null;
                const h = (v / max) * chartH;
                y -= h;
                return (
                  <rect
                    key={k}
                    x={x}
                    y={y}
                    width={bw - 2}
                    height={h}
                    fill={TIMELINE_COLORS[k]}
                    opacity={0.85}
                  >
                    <title>{`${d.label} — ${k}: ${v}`}</title>
                  </rect>
                );
              })}
              {i % 5 === 0 || i === days.length - 1 ? (
                <text x={x} y={H - 8} className="chart-tick">
                  {d.label}
                </text>
              ) : null}
              {total > 0 && <title>{`${d.label}: ${total} events`}</title>}
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
/* Areas — proportional blocks per layer                               */
/* ------------------------------------------------------------------ */

function AreasView({ layers }: { layers: BrainLayer[] }) {
  const max = Math.max(1, ...layers.map((l) => l.count));
  return (
    <div className="brain-areas">
      {layers.map((layer) => {
        const color = LAYER_COLORS[layer.key] ?? "#9aa8c7";
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
                    background: `linear-gradient(90deg, ${color}55, ${color}22)`,
                    border: `1px solid ${color}66`,
                    color,
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
                {layer.nodes.slice(0, 12).map((n) => (
                  <span key={n.id} className="brain-chip" title={n.detail ?? n.label}>
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
/* Page                                                                */
/* ------------------------------------------------------------------ */

export default function BrainPage() {
  const [data, setData] = useState<BrainResponse | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [loading, setLoading] = useState(true);
  const [view, setView] = useState<View>("rings");

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

  const total = data?.layers.reduce((n, l) => n + l.count, 0) ?? 0;

  return (
    <div>
      <PageHeader
        title="Brain"
        subtitle="One knowledge graph — brand kits → assets → campaigns → sends → events — drawn four ways from the same live data."
        actions={data ? <DataStamp at={data.as_of} /> : undefined}
      />

      {loading ? (
        <div className="card">
          <div className="skeleton" style={{ height: 320 }} />
        </div>
      ) : error || !data ? (
        <ErrorBanner error={error} onRetry={() => window.location.reload()} />
      ) : total === 0 ? (
        <EmptyState
          title="No graph data yet"
          hint="Create a brand kit, generate an asset, or launch a campaign and the brain will start mapping your business."
        />
      ) : (
        <>
          <div className="brain-tabs" role="tablist" aria-label="Graph views">
            {VIEWS.map((v) => (
              <button
                key={v.key}
                type="button"
                role="tab"
                aria-selected={view === v.key}
                className={`brain-tab${view === v.key ? " brain-tab-active" : ""}`}
                onClick={() => setView(v.key)}
              >
                {v.label}
              </button>
            ))}
          </div>
          <div className="card brain-view" role="tabpanel">
            {view === "rings" && <RingsView layers={data.layers} />}
            {view === "links" && <LinksView layers={data.layers} links={data.links} />}
            {view === "timeline" && <TimelineView data={data} />}
            {view === "areas" && <AreasView layers={data.layers} />}
          </div>
        </>
      )}
    </div>
  );
}
