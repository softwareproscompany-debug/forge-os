import { useCallback, useEffect, useMemo, useState } from "react";
import type { FormEvent } from "react";
import { Link, useParams } from "react-router-dom";
import {
  assetApi,
  campaignApi,
  contactApi,
  templateApi,
} from "../lib/api";
import type {
  Asset,
  Campaign,
  CampaignStep,
  Channel,
  Enrollment,
  Template,
} from "../lib/api";
import { formatDateTime } from "../lib/format";
import {
  Badge,
  EmptyState,
  ErrorBanner,
  Field,
  Spinner,
  errorMessage,
} from "../components/ui";

interface StepDraft {
  channel: Channel;
  template_id: string | null;
  asset_id: string | null;
  delay_hours: number;
  trigger_event: string;
}

const EMPTY_DRAFT: StepDraft = {
  channel: "email",
  template_id: null,
  asset_id: null,
  delay_hours: 24,
  trigger_event: "",
};

function stepsFromUnknown(data: unknown): CampaignStep[] {
  if (data !== null && typeof data === "object") {
    const steps = (data as Record<string, unknown>)["steps"];
    if (Array.isArray(steps)) return steps as CampaignStep[];
  }
  return [];
}

export default function CampaignDetailPage() {
  const { id } = useParams<{ id: string }>();
  const [campaign, setCampaign] = useState<Campaign | null>(null);
  const [steps, setSteps] = useState<StepDraft[]>([]);
  const [templates, setTemplates] = useState<Template[]>([]);
  const [assets, setAssets] = useState<Asset[]>([]);
  const [enrollments, setEnrollments] = useState<Enrollment[]>([]);
  const [contactNames, setContactNames] = useState<Record<string, string>>({});
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<unknown>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const [draft, setDraft] = useState<StepDraft>(EMPTY_DRAFT);
  const [savingSteps, setSavingSteps] = useState(false);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    if (!id) return;
    setLoading(true);
    setError(null);
    try {
      const [c, tmpls, apprAssets, enrolls, contacts] = await Promise.all([
        campaignApi.get(id),
        templateApi.list().catch(() => [] as Template[]),
        assetApi.list({ status: "approved" }).catch(() => [] as Asset[]),
        campaignApi.enrollments(id).catch(() => [] as Enrollment[]),
        contactApi.list({ limit: 500 }).catch(() => []),
      ]);
      setCampaign(c);
      const loaded = stepsFromUnknown(c).sort((a, b) => a.position - b.position);
      setSteps(
        loaded.map((s) => ({
          channel: s.channel,
          template_id: s.template_id,
          asset_id: s.asset_id,
          delay_hours: s.delay_hours,
          trigger_event: s.trigger_event ?? "",
        })),
      );
      setTemplates(tmpls);
      setAssets(apprAssets);
      setEnrollments(enrolls);
      const names: Record<string, string> = {};
      for (const ct of contacts) {
        names[ct.id] = `${ct.first_name} ${ct.last_name}`.trim() || ct.email || ct.id;
      }
      setContactNames(names);
    } catch (err) {
      setError(err);
    } finally {
      setLoading(false);
    }
  }, [id]);

  useEffect(() => {
    void load();
  }, [load]);

  const templatesByChannel = useMemo(() => {
    const map = new Map<Channel, Template[]>();
    for (const t of templates) {
      const arr = map.get(t.channel) ?? [];
      arr.push(t);
      map.set(t.channel, arr);
    }
    return map;
  }, [templates]);

  const persistSteps = async (next: StepDraft[]) => {
    if (!id) return;
    setSavingSteps(true);
    setNotice(null);
    try {
      await campaignApi.saveSteps(
        id,
        next.map((s, i) => ({
          channel: s.channel,
          template_id: s.template_id,
          asset_id: s.asset_id,
          delay_hours: s.delay_hours,
          trigger_event: s.trigger_event.trim() || null,
          position: i,
        })),
      );
      setSteps(next);
      setNotice("Steps saved.");
    } catch (err) {
      setNotice(errorMessage(err));
    } finally {
      setSavingSteps(false);
    }
  };

  const addStep = (e: FormEvent) => {
    e.preventDefault();
    void persistSteps([...steps, draft]);
    setDraft(EMPTY_DRAFT);
  };

  const moveStep = (index: number, dir: -1 | 1) => {
    const j = index + dir;
    if (j < 0 || j >= steps.length) return;
    const next = [...steps];
    const [moved] = next.splice(index, 1);
    if (moved) next.splice(j, 0, moved);
    void persistSteps(next);
  };

  const removeStep = (index: number) => {
    const next = steps.filter((_, i) => i !== index);
    void persistSteps(next);
  };

  const doLaunch = async () => {
    if (!id) return;
    setBusy(true);
    try {
      const updated = await campaignApi.launch(id);
      setCampaign(updated);
    } catch (err) {
      setNotice(errorMessage(err));
    } finally {
      setBusy(false);
    }
  };

  const doPause = async () => {
    if (!id) return;
    setBusy(true);
    try {
      const updated = await campaignApi.pause(id);
      setCampaign(updated);
    } catch (err) {
      setNotice(errorMessage(err));
    } finally {
      setBusy(false);
    }
  };

  if (loading) return <Spinner label="Loading campaign…" />;
  if (error || !campaign) {
    return (
      <div>
        <Link to="/campaigns" className="link">
          ← All campaigns
        </Link>
        <ErrorBanner error={error ?? "Campaign not found."} onRetry={load} />
      </div>
    );
  }

  const canLaunch = campaign.status === "draft" || campaign.status === "paused";
  const canPause = campaign.status === "running" || campaign.status === "scheduled";

  return (
    <div>
      <Link to="/campaigns" className="link">
        ← All campaigns
      </Link>
      <div className="page-head">
        <div>
          <h1>{campaign.name}</h1>
          {campaign.description && (
            <p className="muted" style={{ marginTop: 4 }}>
              {campaign.description}
            </p>
          )}
        </div>
        <div className="row-actions">
          <Badge value={campaign.status} />
          {canLaunch && (
            <button type="button" className="btn btn-primary" onClick={doLaunch} disabled={busy}>
              {busy ? "…" : "Launch"}
            </button>
          )}
          {canPause && (
            <button type="button" className="btn" onClick={doPause} disabled={busy}>
              {busy ? "…" : "Pause"}
            </button>
          )}
        </div>
      </div>

      {notice && (
        <div className="alert alert-info" role="status" style={{ marginBottom: 16 }}>
          {notice}
        </div>
      )}

      <div className="grid-2">
        <div className="card">
          <div className="card-head">
            <h2>Steps ({steps.length})</h2>
            {savingSteps && <span className="muted">Saving…</span>}
          </div>
          {steps.length === 0 ? (
            <EmptyState
              title="No steps yet"
              hint="Add the first step below — pick a channel, template, and delay."
            />
          ) : (
            <ol className="steps-list">
              {steps.map((s, i) => {
                const tmpl = templates.find((t) => t.id === s.template_id);
                const asset = assets.find((a) => a.id === s.asset_id);
                return (
                  <li key={i} className="step-row">
                    <div className="step-num">{i + 1}</div>
                    <div className="step-main">
                      <div className="step-title">
                        <Badge value={s.channel} />
                        <span>
                          {tmpl?.name ?? asset?.title ?? "No content selected"}
                        </span>
                      </div>
                      <div className="muted step-meta">
                        Wait {s.delay_hours}h
                        {s.trigger_event ? ` · on “${s.trigger_event}”` : ""}
                        {asset ? ` · asset v${asset.version}` : ""}
                      </div>
                    </div>
                    <div className="step-actions">
                      <button
                        type="button"
                        className="btn btn-icon"
                        onClick={() => moveStep(i, -1)}
                        disabled={i === 0 || savingSteps}
                        aria-label="Move step up"
                        title="Move up"
                      >
                        ↑
                      </button>
                      <button
                        type="button"
                        className="btn btn-icon"
                        onClick={() => moveStep(i, 1)}
                        disabled={i === steps.length - 1 || savingSteps}
                        aria-label="Move step down"
                        title="Move down"
                      >
                        ↓
                      </button>
                      <button
                        type="button"
                        className="btn btn-icon btn-danger"
                        onClick={() => removeStep(i)}
                        disabled={savingSteps}
                        aria-label="Remove step"
                        title="Remove"
                      >
                        ✕
                      </button>
                    </div>
                  </li>
                );
              })}
            </ol>
          )}

          <h3 style={{ marginTop: 20 }}>Add step</h3>
          <form onSubmit={addStep} className="form">
            <div className="grid-2">
              <Field label="Channel">
                <select
                  value={draft.channel}
                  onChange={(e) => {
                    setDraft({
                      ...draft,
                      channel: e.target.value as Channel,
                      template_id: null,
                    });
                  }}
                >
                  <option value="email">Email</option>
                  <option value="sms">SMS</option>
                  <option value="social">Social</option>
                </select>
              </Field>
              <Field label="Delay (hours)">
                <input
                  type="number"
                  min={0}
                  value={draft.delay_hours}
                  onChange={(e) =>
                    setDraft({ ...draft, delay_hours: Number(e.target.value) })
                  }
                />
              </Field>
            </div>
            <Field label="Template">
              <select
                value={draft.template_id ?? ""}
                onChange={(e) =>
                  setDraft({ ...draft, template_id: e.target.value || null })
                }
              >
                <option value="">— none —</option>
                {(templatesByChannel.get(draft.channel) ?? []).map((t) => (
                  <option key={t.id} value={t.id}>
                    {t.name}
                  </option>
                ))}
              </select>
            </Field>
            <Field label="Approved asset (optional)">
              <select
                value={draft.asset_id ?? ""}
                onChange={(e) =>
                  setDraft({ ...draft, asset_id: e.target.value || null })
                }
              >
                <option value="">— none —</option>
                {assets.map((a) => (
                  <option key={a.id} value={a.id}>
                    {a.title} ({a.kind})
                  </option>
                ))}
              </select>
            </Field>
            <Field
              label="Trigger event"
              hint="e.g. contact_added for step 1. Leave blank for time-based steps."
            >
              <input
                value={draft.trigger_event}
                onChange={(e) =>
                  setDraft({ ...draft, trigger_event: e.target.value })
                }
                placeholder="contact_added"
              />
            </Field>
            <div className="form-actions">
              <span className="spacer" />
              <button type="submit" className="btn btn-primary" disabled={savingSteps}>
                Add step
              </button>
            </div>
          </form>
        </div>

        <div>
          <div className="card">
            <div className="card-head">
              <h2>Enrollments ({enrollments.length})</h2>
            </div>
            {enrollments.length === 0 ? (
              <EmptyState
                title="No enrollments"
                hint="Contacts enroll when the campaign runs or via the contact_added event."
              />
            ) : (
              <div className="table-wrap">
                <table className="table">
                  <thead>
                    <tr>
                      <th>Contact</th>
                      <th>Step</th>
                      <th>Status</th>
                      <th>Next run</th>
                    </tr>
                  </thead>
                  <tbody>
                    {enrollments.map((en) => (
                      <tr key={en.id}>
                        <td>{contactNames[en.contact_id] ?? en.contact_id}</td>
                        <td>{en.current_step + 1}</td>
                        <td>
                          <Badge value={en.status} />
                        </td>
                        <td>{formatDateTime(en.next_run_at)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </div>

          <div className="card" style={{ marginTop: 16 }}>
            <div className="card-head">
              <h2>Details</h2>
            </div>
            <dl className="details">
              <dt>Status</dt>
              <dd>
                <Badge value={campaign.status} />
              </dd>
              <dt>Timezone</dt>
              <dd>{campaign.timezone}</dd>
              <dt>Starts at</dt>
              <dd>{formatDateTime(campaign.starts_at)}</dd>
              <dt>Autopilot</dt>
              <dd>{campaign.autopilot ? "On" : "Off"}</dd>
            </dl>
          </div>
        </div>
      </div>
    </div>
  );
}
