import { useCallback, useEffect, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { autopilotApi } from "../lib/api";
import type { AutopilotSettings, ContentPlan } from "../lib/api";
import {
  Badge,
  DataStamp,
  EmptyState,
  ErrorBanner,
  Field,
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

function hourLabel(hour: number): string {
  const suffix = hour < 12 ? "AM" : "PM";
  const h = hour % 12 === 0 ? 12 : hour % 12;
  return `${h}:00 ${suffix}`;
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

  // Planner schedule (per business).
  const [settings, setSettings] = useState<AutopilotSettings | null>(null);
  const [settingsLoading, setSettingsLoading] = useState(true);
  const [planDay, setPlanDay] = useState(0);
  const [planHour, setPlanHour] = useState(6);
  const [planCadence, setPlanCadence] = useState<"weekly" | "biweekly">(
    "weekly",
  );
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);

  // Manual run-now trigger.
  const [running, setRunning] = useState(false);
  const [runError, setRunError] = useState<string | null>(null);
  const [runNotice, setRunNotice] = useState<string | null>(null);

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

  const loadSettings = useCallback(async () => {
    setSettingsLoading(true);
    setSaveError(null);
    try {
      const s = await autopilotApi.get();
      setSettings(s);
      setPlanDay(s.plan_day);
      setPlanHour(s.plan_hour);
      setPlanCadence(s.plan_cadence);
    } catch {
      // Schedule editing is secondary; the plan view still works.
      setSettings(null);
    } finally {
      setSettingsLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
    void loadSettings();
  }, [load, loadSettings]);

  const scheduleDirty =
    settings !== null &&
    (planDay !== settings.plan_day ||
      planHour !== settings.plan_hour ||
      planCadence !== settings.plan_cadence);

  const onSaveSchedule = async () => {
    setSaveError(null);
    setSaved(false);
    setSaving(true);
    try {
      const s = await autopilotApi.update({
        plan_day: planDay,
        plan_hour: planHour,
        plan_cadence: planCadence,
      });
      setSettings(s);
      setSaved(true);
    } catch (err) {
      setSaveError(errorMessage(err));
    } finally {
      setSaving(false);
    }
  };

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

  const onRunNow = async () => {
    setRunError(null);
    setRunNotice(null);
    setRunning(true);
    try {
      const out = await autopilotApi.runNow();
      setPlan(out.plan);
      setRunNotice(
        out.created
          ? "A fresh plan was drafted for this week."
          : "This week's plan already exists — showing it below.",
      );
    } catch (err) {
      setRunError(errorMessage(err));
    } finally {
      setRunning(false);
    }
  };

  if (loading) return <Spinner label="Loading autopilot plan…" />;

  const scheduleSummary = settings
    ? `Drafts ${settings.plan_cadence} on ${dayName(settings.plan_day)} at ${hourLabel(settings.plan_hour)} business time.`
    : "Drafts weekly on Monday at 06:00 business time.";

  return (
    <div>
      <PageHeader
        title="Autopilot"
        subtitle={`The planner drafts this week's content on your schedule — ${scheduleSummary} You approve the plan; the week runs itself.`}
        actions={
          <div style={{ display: "flex", gap: 8 }}>
            <button
              type="button"
              className="btn"
              onClick={onRunNow}
              disabled={running}
              title="Draft this week's plan right now (owner/admin)"
            >
              {running ? "Drafting…" : "Run now"}
            </button>
            {plan?.status === "draft" && (
              <button
                type="button"
                className="btn btn-primary"
                onClick={onApprove}
                disabled={approving}
              >
                {approving ? "Approving…" : "Approve plan"}
              </button>
            )}
          </div>
        }
      />

      <ErrorBanner error={error} onRetry={load} />

      {(runError || runNotice) && (
        <div
          className={`alert ${runError ? "alert-error" : "alert-info"}`}
          role={runError ? "alert" : "status"}
          style={{ marginBottom: 16 }}
        >
          <div>
            <strong>{runError ? "Run now failed." : "Plan ready."}</strong>
            <div className="alert-detail">{runError ?? runNotice}</div>
          </div>
        </div>
      )}

      {approvedCampaignId && (
        <div className="alert alert-info" role="status">
          <div>
            <strong>Plan approved.</strong>
            <div className="alert-detail">
              A running autopilot campaign was created for the week — sends go
              out on the plan's days, no manual launch needed.
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

      <div className="card" style={{ marginBottom: 16 }}>
        <div className="card-head">
          <div>
            <div className="card-title">Planner schedule</div>
            <div className="muted">
              When the worker drafts your weekly content plan (business-local
              time). Changes apply from the next draft onward.
            </div>
          </div>
        </div>
        {settingsLoading ? (
          <Spinner label="Loading schedule…" />
        ) : settings === null ? (
          <div className="muted">
            Schedule settings are unavailable right now — the plan view above
            still works.
          </div>
        ) : (
          <div>
            <div
              style={{
                display: "grid",
                gridTemplateColumns: "repeat(auto-fit, minmax(160px, 1fr))",
                gap: 12,
              }}
            >
              <Field label="Draft day">
                <select
                  className="input"
                  value={planDay}
                  onChange={(e) => {
                    setPlanDay(Number(e.target.value));
                    setSaved(false);
                  }}
                >
                  {DAY_NAMES.map((name, i) => (
                    <option key={name} value={i}>
                      {name}
                    </option>
                  ))}
                </select>
              </Field>
              <Field label="Draft time">
                <select
                  className="input"
                  value={planHour}
                  onChange={(e) => {
                    setPlanHour(Number(e.target.value));
                    setSaved(false);
                  }}
                >
                  {Array.from({ length: 24 }, (_, h) => (
                    <option key={h} value={h}>
                      {hourLabel(h)}
                    </option>
                  ))}
                </select>
              </Field>
              <Field
                label="Cadence"
                hint="Biweekly drafts at most once every ~13 days."
              >
                <select
                  className="input"
                  value={planCadence}
                  onChange={(e) => {
                    setPlanCadence(e.target.value as "weekly" | "biweekly");
                    setSaved(false);
                  }}
                >
                  <option value="weekly">Weekly</option>
                  <option value="biweekly">Biweekly</option>
                </select>
              </Field>
            </div>
            <div style={{ marginTop: 12, display: "flex", gap: 8, alignItems: "center" }}>
              <button
                type="button"
                className="btn btn-primary"
                onClick={onSaveSchedule}
                disabled={saving || !scheduleDirty}
              >
                {saving ? "Saving…" : "Save schedule"}
              </button>
              {saved && !scheduleDirty && (
                <span className="muted" role="status">
                  Schedule saved.
                </span>
              )}
              {saveError && (
                <span className="alert-detail" role="alert">
                  {saveError}
                </span>
              )}
            </div>
          </div>
        )}
      </div>

      {plan === null && (
        <div className="card">
          <EmptyState
            title="No draft plan yet"
            hint="The planner drafts on your schedule above — or draft one right now."
            action={
              <div style={{ display: "flex", gap: 8 }}>
                <button
                  type="button"
                  className="btn btn-primary"
                  onClick={onRunNow}
                  disabled={running}
                >
                  {running ? "Drafting…" : "Run now"}
                </button>
                <button type="button" className="btn" onClick={load}>
                  Check again
                </button>
              </div>
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
                This plan was approved and turned into a running campaign.
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
            This plan was rejected. The planner will draft a fresh plan on
            your next scheduled draft day.
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
                Review the draft below. Approving creates a running campaign
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
