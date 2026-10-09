import { useCallback, useEffect, useState } from "react";
import { analyticsApi, campaignApi } from "../lib/api";
import type { AnalyticsOverview, Campaign, FunnelStep } from "../lib/api";
import {
  formatDate,
  formatMoney,
  formatNumber,
  formatPercent,
  normalizeRatio,
} from "../lib/format";
import {
  Badge,
  EmptyState,
  ErrorBanner,
  Spinner,
} from "../components/ui";

const DAY_OPTIONS = [7, 14, 30, 90];

export default function AnalyticsPage() {
  const [days, setDays] = useState(30);
  const [campaignId, setCampaignId] = useState("");
  const [campaigns, setCampaigns] = useState<Campaign[]>([]);
  const [overview, setOverview] = useState<AnalyticsOverview | null>(null);
  const [funnel, setFunnel] = useState<FunnelStep[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<unknown>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const camps = await campaignApi.list().catch(() => [] as Campaign[]);
      setCampaigns(camps);
      const ov = await analyticsApi.overview({
        days,
        campaign_id: campaignId || undefined,
      });
      setOverview(ov);
      if (campaignId) {
        setFunnel(await analyticsApi.funnel(campaignId));
      } else {
        setFunnel([]);
      }
    } catch (err) {
      setError(err);
    } finally {
      setLoading(false);
    }
  }, [days, campaignId]);

  useEffect(() => {
    void load();
  }, [load]);

  if (loading) return <Spinner label="Loading analytics…" />;

  const kpis = overview
    ? [
        { label: "Sent", value: formatNumber(overview.sent) },
        { label: "Delivered", value: formatNumber(overview.delivered) },
        { label: "Opened", value: formatNumber(overview.opened) },
        { label: "Clicked", value: formatNumber(overview.clicked) },
        { label: "Converted", value: formatNumber(overview.converted) },
        {
          label: "Open rate",
          value: formatPercent(normalizeRatio(overview.open_rate)),
        },
        { label: "CTR", value: formatPercent(normalizeRatio(overview.ctr)) },
        {
          label: "Conversion rate",
          value: formatPercent(normalizeRatio(overview.conversion_rate)),
        },
        { label: "Spend", value: formatMoney(overview.spend_usd) },
      ]
    : [];

  return (
    <div>
      <div className="page-head">
        <h1>Analytics</h1>
        <div className="row-actions">
          <label className="filter">
            Campaign
            <select
              value={campaignId}
              onChange={(e) => setCampaignId(e.target.value)}
            >
              <option value="">All campaigns</option>
              {campaigns.map((c) => (
                <option key={c.id} value={c.id}>
                  {c.name}
                </option>
              ))}
            </select>
          </label>
          <label className="filter">
            Range
            <select
              value={days}
              onChange={(e) => setDays(Number(e.target.value))}
            >
              {DAY_OPTIONS.map((d) => (
                <option key={d} value={d}>
                  Last {d} days
                </option>
              ))}
            </select>
          </label>
        </div>
      </div>

      <ErrorBanner error={error} onRetry={load} />

      <div className="kpi-grid">
        {kpis.map((k) => (
          <div key={k.label} className="card kpi">
            <div className="kpi-label">{k.label}</div>
            <div className="kpi-value">{k.value}</div>
          </div>
        ))}
      </div>

      {campaignId && (
        <div className="card" style={{ marginTop: 16 }}>
          <div className="card-head">
            <h2>Campaign funnel</h2>
          </div>
          {funnel.length === 0 ? (
            <EmptyState
              title="No funnel data"
              hint="Steps appear here once sends go out for this campaign."
            />
          ) : (
            <div className="table-wrap">
              <table className="table">
                <thead>
                  <tr>
                    <th>Step</th>
                    <th>Sent</th>
                    <th>Opened</th>
                    <th>Clicked</th>
                    <th>Open rate</th>
                    <th>CTR</th>
                  </tr>
                </thead>
                <tbody>
                  {[...funnel]
                    .sort((a, b) => a.position - b.position)
                    .map((f) => (
                      <tr key={f.step_id}>
                        <td>Step {f.position + 1}</td>
                        <td>{formatNumber(f.sent)}</td>
                        <td>{formatNumber(f.opened)}</td>
                        <td>{formatNumber(f.clicked)}</td>
                        <td>
                          {formatPercent(
                            f.sent > 0 ? f.opened / f.sent : null,
                          )}
                        </td>
                        <td>
                          {formatPercent(
                            f.sent > 0 ? f.clicked / f.sent : null,
                          )}
                        </td>
                      </tr>
                    ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      )}

      <div className="card" style={{ marginTop: 16 }}>
        <div className="card-head">
          <h2>By day</h2>
          <Badge value={`${overview?.by_day.length ?? 0} days`} />
        </div>
        {!overview || overview.by_day.length === 0 ? (
          <EmptyState title="No daily data" hint="Send activity will show up here." />
        ) : (
          <div className="table-wrap">
            <table className="table">
              <thead>
                <tr>
                  <th>Date</th>
                  <th>Sent</th>
                  <th>Delivered</th>
                  <th>Opened</th>
                  <th>Clicked</th>
                  <th>Converted</th>
                </tr>
              </thead>
              <tbody>
                {[...overview.by_day].reverse().map((d) => (
                  <tr key={d.date}>
                    <td>{formatDate(d.date)}</td>
                    <td>{formatNumber(d.sent)}</td>
                    <td>{formatNumber(d.delivered ?? 0)}</td>
                    <td>{formatNumber(d.opened ?? 0)}</td>
                    <td>{formatNumber(d.clicked ?? 0)}</td>
                    <td>{formatNumber(d.converted ?? 0)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  );
}
