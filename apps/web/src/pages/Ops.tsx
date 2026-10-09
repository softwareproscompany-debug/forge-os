import { useCallback, useEffect, useRef, useState } from "react";
import { opsApi } from "../lib/api";
import type { OpsActivityResponse } from "../lib/api";
import { timeAgo } from "../lib/format";
import {
  Badge,
  DataStamp,
  EmptyState,
  ErrorBanner,
  LiveDot,
  Spinner,
  StatCard,
} from "../components/ui";

const POLL_MS = 5000;

interface StageDef {
  key: string;
  letter: string;
  name: string;
  stats: Array<[string, string]>;
}

const STAGES: StageDef[] = [
  {
    key: "foundation",
    letter: "F",
    name: "Foundation",
    stats: [["brand_kits", "Brand kits"]],
  },
  {
    key: "origination",
    letter: "O",
    name: "Origination",
    stats: [
      ["in_review", "In review"],
      ["approved", "Approved"],
      ["draft", "Drafts"],
    ],
  },
  {
    key: "reach",
    letter: "R",
    name: "Reach",
    stats: [
      ["in_flight", "In flight"],
      ["outbox_24h", "Outbox 24h"],
    ],
  },
  {
    key: "growth",
    letter: "G",
    name: "Growth",
    stats: [
      ["campaigns_running", "Running"],
      ["enrollments_active", "Enrolled"],
    ],
  },
  {
    key: "evidence",
    letter: "E",
    name: "Evidence",
    stats: [
      ["delivered_24h", "Delivered"],
      ["opened_24h", "Opened"],
      ["converted_24h", "Converted"],
    ],
  },
];

/** A stage glows when it has actionable work flowing through it. */
function isHot(key: string, stats: Record<string, number>): boolean {
  if (key === "origination") return (stats["in_review"] ?? 0) > 0;
  if (key === "reach") return (stats["in_flight"] ?? 0) > 0;
  if (key === "growth") return (stats["campaigns_running"] ?? 0) > 0;
  return false;
}

export default function OpsPage() {
  const [data, setData] = useState<OpsActivityResponse | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [loading, setLoading] = useState(true);
  const seenRef = useRef<Set<string>>(new Set());
  const [freshIds, setFreshIds] = useState<Set<string>>(new Set());

  const load = useCallback(async (initial = false) => {
    try {
      const next = await opsApi.activity(40);
      setData(next);
      // Anything we haven't rendered before animates in.
      const fresh = new Set<string>();
      for (const item of next.activity) {
        if (!seenRef.current.has(item.id)) fresh.add(item.id);
        seenRef.current.add(item.id);
      }
      setFreshIds(fresh);
      setError(null);
    } catch (err) {
      // Poll failures keep the last good snapshot; only the first
      // load surfaces an error.
      if (initial) setError(err);
    } finally {
      if (initial) setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load(true);
    const timer = window.setInterval(() => void load(false), POLL_MS);
    return () => window.clearInterval(timer);
  }, [load]);

  if (loading) return <Spinner label="Establishing uplink…" />;
  if (!data) return <ErrorBanner error={error} onRetry={() => load(true)} />;

  const evidence = data.stages["evidence"] ?? {};

  return (
    <div>
      <div className="ops-head">
        <div>
          <div className="ops-title">Mission Control</div>
          <div className="ops-sub">
            Live view of the agent swarm — every panel reads, nothing writes.
          </div>
        </div>
        <div className="page-header-actions">
          <LiveDot />
          <DataStamp at={data.as_of} />
        </div>
      </div>

      <ErrorBanner error={error} onRetry={() => load(true)} />

      {/* Pipeline: the five FORGE stages with live counts flowing through */}
      <div className="ops-pipeline" role="list" aria-label="FORGE pipeline">
        {STAGES.map((stage, i) => {
          const stats = data.stages[stage.key] ?? {};
          const hot = isHot(stage.key, stats);
          return (
            <div key={stage.key} style={{ display: "contents" }}>
              {i > 0 && <div className="ops-link" aria-hidden="true" />}
              <div
                className={`ops-stage${hot ? " ops-stage-hot" : ""}`}
                role="listitem"
                aria-label={`${stage.name} stage`}
              >
                <div className="ops-stage-letter" aria-hidden="true">
                  {stage.letter}
                </div>
                <div className="ops-stage-name">{stage.name}</div>
                <div className="ops-stage-stats">
                  {stage.stats.map(([k, label]) => (
                    <span key={k}>
                      <b>{(stats[k] ?? 0).toLocaleString("en-US")}</b> {label}
                    </span>
                  ))}
                </div>
              </div>
            </div>
          );
        })}
      </div>

      {/* Headline counters */}
      <div className="ops-counters">
        <StatCard label="Sends today" value={data.counters.sends_today} />
        <StatCard
          label="Generations today"
          value={data.counters.generations_today}
          tone="magenta"
        />
        <StatCard label="In flight now" value={data.counters.in_flight} />
      </div>

      <div className="ops-grid">
        {/* Agent-swarm activity feed */}
        <div className="card">
          <div className="card-head">
            <h2>Agent activity</h2>
            <DataStamp at={data.as_of} />
          </div>
          {data.activity.length === 0 ? (
            <EmptyState
              title="No activity yet"
              hint="Generate an asset or launch a campaign and the swarm shows up here."
            />
          ) : (
            <ul className="ops-feed">
              {data.activity.map((item) => (
                <li
                  key={item.id}
                  className={`ops-feed-item${
                    freshIds.has(item.id) ? " ops-feed-item-new" : ""
                  }`}
                >
                  <span
                    className={`ops-feed-kind ops-feed-kind-${item.kind}`}
                    aria-hidden="true"
                  />
                  <div style={{ minWidth: 0 }}>
                    <div className="ops-feed-title">{item.title}</div>
                    {item.detail && (
                      <div className="ops-feed-detail">{item.detail}</div>
                    )}
                  </div>
                  <span className="ops-feed-time">
                    {item.status && (
                      <>
                        <Badge value={item.status} />{" "}
                      </>
                    )}
                    {timeAgo(item.at)}
                  </span>
                </li>
              ))}
            </ul>
          )}
        </div>

        {/* Evidence snapshot */}
        <div className="card">
          <div className="card-head">
            <h2>Evidence · last 24h</h2>
            <DataStamp at={data.as_of} />
          </div>
          <div className="kpi-grid" style={{ marginBottom: 0 }}>
            <div className="card kpi">
              <div className="kpi-label">Delivered</div>
              <div className="kpi-value">
                {(evidence["delivered_24h"] ?? 0).toLocaleString("en-US")}
              </div>
            </div>
            <div className="card kpi">
              <div className="kpi-label">Opened</div>
              <div className="kpi-value">
                {(evidence["opened_24h"] ?? 0).toLocaleString("en-US")}
              </div>
            </div>
            <div className="card kpi">
              <div className="kpi-label">Clicked</div>
              <div className="kpi-value">
                {(evidence["clicked_24h"] ?? 0).toLocaleString("en-US")}
              </div>
            </div>
            <div className="card kpi">
              <div className="kpi-label">Converted</div>
              <div className="kpi-value">
                {(evidence["converted_24h"] ?? 0).toLocaleString("en-US")}
              </div>
            </div>
          </div>
          <p className="muted" style={{ fontSize: 12, marginTop: 12 }}>
            Panels show data only — they never store it. Full breakdowns live
            under Analytics.
          </p>
        </div>
      </div>
    </div>
  );
}
