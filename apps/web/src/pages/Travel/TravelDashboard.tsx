import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { travelApi } from "../../lib/api";
import type { TravelDashboard, TravelLeadStatus } from "../../lib/api";
import {
  Badge,
  EmptyState,
  ErrorBanner,
  PageHeader,
  Spinner,
  StatCard,
} from "../../components/ui";

const STAGES: { value: TravelLeadStatus; label: string }[] = [
  { value: "new", label: "New" },
  { value: "qualified", label: "Qualified" },
  { value: "quoted", label: "Quoted" },
  { value: "booked", label: "Booked" },
];

export default function TravelDashboardPage() {
  const [data, setData] = useState<TravelDashboard | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<unknown>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      setData(await travelApi.dashboard());
    } catch (err) {
      setError(err);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  if (loading) return <Spinner label="Loading travel workspace…" />;
  if (error || !data)
    return (
      <div>
        <PageHeader title="Travel" subtitle="Travel agency workspace" />
        <ErrorBanner error={error ?? "Failed to load"} />
      </div>
    );

  const totalLeads = Object.values(data.lead_counts).reduce((a, b) => a + b, 0);

  return (
    <div>
      <PageHeader
        title="Travel"
        subtitle="Travel agency workspace — leads, customers, and trip requests."
        actions={
          <Link to="/travel/leads" className="btn btn-primary">
            + New lead
          </Link>
        }
      />

      <div className="stats-grid">
        {STAGES.map((s) => (
          <StatCard
            key={s.value}
            label={`${s.label} leads`}
            value={data.lead_counts[s.value] ?? 0}
          />
        ))}
        <StatCard label="Customers" value={data.customer_count} />
        <StatCard label="Trip requests" value={data.trip_request_count} />
      </div>

      <div className="card" style={{ marginTop: 20 }}>
        <div className="card-head">
          <h2>Recent leads</h2>
          <Link to="/travel/leads" className="btn btn-ghost">
            View all
          </Link>
        </div>
        {totalLeads === 0 ? (
          <EmptyState
            title="No travel leads yet"
            hint="Add your first lead to start the pipeline: new → qualified → quoted → booked."
            action={
              <Link to="/travel/leads" className="btn btn-primary">
                Add lead
              </Link>
            }
          />
        ) : (
          <div className="table-wrap">
            <table className="table">
              <thead>
                <tr>
                  <th>Destination</th>
                  <th>Purpose</th>
                  <th>Status</th>
                  <th>Created</th>
                </tr>
              </thead>
              <tbody>
                {data.recent_leads.map((l) => (
                  <tr key={l.id}>
                    <td>
                      <Link to={`/travel/leads?lead=${l.id}`}>
                        {l.destination || "—"}
                      </Link>
                    </td>
                    <td className="muted">{l.trip_purpose || "—"}</td>
                    <td>
                      <Badge value={l.status} />
                    </td>
                    <td className="muted">
                      {new Date(l.created_at).toLocaleDateString()}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      <div className="grid-2" style={{ marginTop: 20 }}>
        <Link to="/travel/customers" className="card card-link">
          <h3>Customers</h3>
          <div className="muted">
            {data.customer_count} customer
            {data.customer_count === 1 ? "" : "s"} on record
          </div>
        </Link>
        <Link to="/travel/trip-requests" className="card card-link">
          <h3>Trip requests</h3>
          <div className="muted">
            {data.trip_request_count} open request
            {data.trip_request_count === 1 ? "" : "s"}
          </div>
        </Link>
      </div>
    </div>
  );
}
