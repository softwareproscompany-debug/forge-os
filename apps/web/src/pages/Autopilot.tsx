import { useCallback, useEffect, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { autopilotApi } from "../lib/api";
import type { ContentPlan } from "../lib/api";
import {
  Badge,
  DataStamp,
  EmptyState,
  ErrorBanner,
  PageHeader,
  Spinner,
  errorMessage,
} from "../components/ui";

const DAY_NAMES = [
  "Monday",
  "Tuesday",
  "Wednesday",
  "Thursday",
  "Friday",
  "Saturday",
  "Sunday",
];

function dayName(day: number): string {
  return DAY_NAMES[day] ?? `Day ${day}`;
}

export default function AutopilotPage() {
  const navigate = useNavigate();
  const [plan, setPlan] = useState<ContentPlan | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<unknown>(null);
  const [approving, setApproving] = useState(false);
  const [approveError, setApproveError] = useState<string | null>(null);
  const [approvedCampaignId, setApprovedCampaignId] = useState<string | null>(
    null,
  );

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    setApproveError(null);
    try {
      setPlan(await autopilotApi.plan());
    } catch (err) {
      setError(err);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const onApprove = async () => {
    if (!plan) return;
    setApproveError(null);
    setApproving(true);
    try {
      const out = await autopilotApi.approvePlan(plan.id);
      setApprovedCampaignId(out.campaign_id);
      setPlan(out.plan);
    } catch (err) {
      setApproveError(errorMessage(err));
    } finally {
      setApproving(false);
    }
  };

  if (loading) return <Spinner label="Loading autopilot plan…" />;

  return (
    <div>
      <PageHeader
        title="Autopilot"
        subtitle="The weekly planner drafts next week's content every Monday at 06:00 business time. You approve the plan; the week runs itself."
        actions={
          plan?.status === "draft" && (
            <button
              type="button"
              className="btn btn-primary"
              onClick={onApprove}
              disabled={approving}
            >
              {approving ? "Approving…" : "Approve plan"}
            </button>
          )
        }
      />

      <ErrorBanner error={error} onRetry={load} />

      {approvedCampaignId && (
        <div className="alert alert-info" role="status">
          <div>
            <strong>Plan approved.</strong>
            <div className="alert-detail">
              A scheduled autopilot campaign was created for the week.
            </div>
          </div>
          <Link
            className="btn btn-sm btn-primary"
            to={`/campaigns/${approvedCampaignId}`}
          >
            View campaign
          </Link>
        </div>
      )}

      {plan === null && (
        <div className="card">
          <EmptyState
            title="No draft plan yet"
            hint="The planner runs Monday 06:00 business time."
            action={
              <button type="button" className="btn" onClick={load}>
                Check again
              </button>
            }
          />
        </div>
      )}

      {plan !== null && plan.status === "approved" && (
        <div className="card">
          <div className="card-head">
            <div>
              <div className="card-title">
                Week of {plan.week_start} <Badge value={plan.status} />{" "}
                <DataStamp at={plan.created_at} />
              </div>
              <div className="muted">
                This plan was approved and turned into a scheduled campaign.
              </div>
            </div>
            {plan.campaign_id && (
              <Link
                className="btn btn-primary"
                to={`/campaigns/${plan.campaign_id}`}
              >
                View campaign
              </Link>
            )}
          </div>
        </div>
      )}

      {plan !== null && plan.status === "rejected" && (
        <div className="card">
          <div className="card-title">
            Week of {plan.week_start} <Badge value={plan.status} />
          </div>
          <div className="muted">
            This plan was rejected. The planner will draft a fresh plan next
            Monday at 06:00 business time.
          </div>
        </div>
      )}

      {plan !== null && plan.status === "draft" && (
        <div className="card">
          <div className="card-head">
            <div>
              <div className="card-title">
                Week of {plan.week_start} <Badge value={plan.status} />{" "}
                <DataStamp at={plan.created_at} />
              </div>
              <div className="muted">
                Review the draft below. Approving creates a scheduled campaign
                with one step per item, sent on the item's day.
              </div>
            </div>
          </div>

          {approveError && (
            <div className="alert alert-error" role="alert">
              <div>
                <strong>Approval failed.</strong>
                <div className="alert-detail">{approveError}</div>
              </div>
            </div>
          )}

          <div className="table-wrap">
            <table className="table">
              <thead>
                <tr>
                  <th scope="col">Day</th>
                  <th scope="col">Channel</th>
                  <th scope="col">Kind</th>
                  <th scope="col">Title</th>
                  <th scope="col">Brief</th>
                  <th scope="col">Asset</th>
                </tr>
              </thead>
              <tbody>
                {plan.items.map((item, i) => (
                  <tr key={i}>
                    <td>{dayName(item.day)}</td>
                    <td>
                      <Badge value={item.channel} />
                    </td>
                    <td>
                      <Badge value={item.kind} />
                    </td>
                    <td>{item.title}</td>
                    <td className="muted">{item.brief}</td>
                    <td>
                      {item.asset_id ? (
                        <code>{item.asset_id.slice(0, 8)}…</code>
                      ) : (
                        <span className="muted">—</span>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      <div className="muted" style={{ marginTop: 16, fontSize: 12 }}>
        Plans are drafts until approved — nothing is scheduled automatically.
        <button
          type="button"
          className="btn btn-ghost btn-sm"
          style={{ marginLeft: 8 }}
          onClick={() => navigate("/campaigns")}
        >
          Go to campaigns
        </button>
      </div>
    </div>
  );
}
