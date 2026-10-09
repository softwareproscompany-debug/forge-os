import { useCallback, useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { analyticsApi, assetApi, campaignApi } from "../lib/api";
import type { AnalyticsOverview, Campaign } from "../lib/api";
import {
  formatMoney,
  formatNumber,
  formatPercent,
  normalizeRatio,
} from "../lib/format";
import { Badge, EmptyState, ErrorBanner, PageHeader, Spinner, StatCard } from "../components/ui";

function last7(byDay: AnalyticsOverview["by_day"]) {
  return byDay.slice(-7);
}

/** Hand-rolled SVG bar chart — no chart library. */
function SendChart({ overview }: { overview: AnalyticsOverview }) {
  const data = last7(overview.by_day);
  const W = 560;
  const H = 180;
  const PAD = 32;
  const max = Math.max(1, ...data.map((d) => d.sent));
  const innerW = W - PAD * 2;
  const innerH = H - PAD - 24;
  const n = Math.max(1, data.length);
  const slot = innerW / n;
  const barW = Math.min(44, slot * 0.55);

  return (
    <svg
      viewBox={`0 0 ${W} ${H}`}
      className="chart"
      role="img"
      aria-label="Sends over the last 7 days"
    >
      {[0.25, 0.5, 0.75, 1].map((f) => {
        const y = PAD + innerH * (1 - f);
        return (
          <g key={f}>
            <line x1={PAD} x2={W - PAD} y1={y} y2={y} className="chart-grid" />
            <text x={PAD - 6} y={y + 4} className="chart-tick" textAnchor="end">
              {formatNumber(max * f)}
            </text>
          </g>
        );
      })}
      {data.map((d, i) => {
        const h = (d.sent / max) * innerH;
        const x = PAD + slot * i + (slot - barW) / 2;
        const y = PAD + innerH - h;
        const label = new Date(d.date).toLocaleDateString("en-US", {
          month: "numeric",
          day: "numeric",
        });
        return (
          <g key={d.date}>
            <rect x={x} y={y} width={barW} height={h} rx={4} className="chart-bar">
              <title>{`${label}: ${formatNumber(d.sent)} sent`}</title>
            </rect>
            <text
              x={x + barW / 2}
              y={H - 8}
              className="chart-tick"
              textAnchor="middle"
            >
              {label}
            </text>
          </g>
        );
      })}
    </svg>
  );
}

export default function DashboardPage() {
  const [overview, setOverview] = useState<AnalyticsOverview | null>(null);
  const [campaigns, setCampaigns] = useState<Campaign[]>([]);
  const [pendingApprovals, setPendingApprovals] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<unknown>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const [ov, camps, inReview] = await Promise.all([
        analyticsApi.overview({ days: 30 }),
        campaignApi.list(),
        assetApi.listPage({ status: "in_review", limit: 1 }),
      ]);
      setOverview(ov);
      setCampaigns(camps.slice(0, 5));
      setPendingApprovals(inReview.total);
    } catch (err) {
      setError(err);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const kpis = useMemo(() => {
    if (!overview) return [];
    return [
      { label: "Sent (30d)", value: overview.sent },
      {
        label: "Open rate",
        value: normalizeRatio(overview.open_rate) ?? 0,
        format: (n: number) => formatPercent(n),
        tone: "cyan" as const,
      },
      {
        label: "CTR",
        value: normalizeRatio(overview.ctr) ?? 0,
        format: (n: number) => formatPercent(n),
      },
      { label: "Conversions", value: overview.converted },
      {
        label: "Spend",
        value: overview.spend_usd,
        format: (n: number) => formatMoney(n),
        tone: "cyan" as const,
      },
    ];
  }, [overview]);

  if (loading) return <Spinner label="Loading dashboard…" />;

  return (
    <div>
      <PageHeader
        title="Dashboard"
        subtitle="Thirty-day command view — launch, review, measure."
        actions={
          <Link to="/campaigns" className="btn btn-primary">
            New campaign
          </Link>
        }
      />

      <ErrorBanner error={error} onRetry={load} />

      <div className="kpi-grid">
        {kpis.map((k) => (
          <StatCard
            key={k.label}
            label={k.label}
            value={k.value}
            tone={"tone" in k ? k.tone : undefined}
            format={"format" in k ? k.format : undefined}
          />
        ))}
      </div>

      <div className="grid-2">
        <div className="card">
          <div className="card-head">
            <h2>Sends — last 7 days</h2>
            <Link to="/analytics" className="link">
              Full analytics →
            </Link>
          </div>
          {overview && overview.by_day.length > 0 ? (
            <SendChart overview={overview} />
          ) : (
            <EmptyState
              title="No send data yet"
              hint="Launch a campaign and sends will appear here."
            />
          )}
        </div>

        <div className="card">
          <div className="card-head">
            <h2>Needs attention</h2>
          </div>
          <div className="attention-row">
            <span>
              <strong>{pendingApprovals}</strong> asset
              {pendingApprovals === 1 ? "" : "s"} awaiting review
            </span>
            <Link to="/approvals" className="btn btn-sm">
              Open approvals
            </Link>
          </div>
          <div className="card-head" style={{ marginTop: 16 }}>
            <h2>Recent campaigns</h2>
            <Link to="/campaigns" className="link">
              View all →
            </Link>
          </div>
          {campaigns.length === 0 ? (
            <EmptyState
              title="No campaigns yet"
              hint="Create your first campaign to start automating outreach."
              action={
                <Link to="/campaigns" className="btn btn-primary btn-sm">
                  Create campaign
                </Link>
              }
            />
          ) : (
            <ul className="list">
              {campaigns.map((c) => (
                <li key={c.id} className="list-row">
                  <Link to={`/campaigns/${c.id}`} className="list-title">
                    {c.name}
                  </Link>
                  <Badge value={c.status} />
                </li>
              ))}
            </ul>
          )}
        </div>
      </div>
    </div>
  );
}
