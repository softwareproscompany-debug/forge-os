import { useCallback, useEffect, useMemo, useState } from "react";
import { pipelineApi } from "../lib/api";
import type { Opportunity, OpportunityInput, OpportunityStage } from "../lib/api";
import { formatMoney } from "../lib/format";
import {
  EmptyState,
  ErrorBanner,
  Field,
  Modal,
  PageHeader,
  Spinner,
} from "../components/ui";

const STAGES: { value: OpportunityStage; label: string }[] = [
  { value: "new", label: "New" },
  { value: "qualified", label: "Qualified" },
  { value: "proposal", label: "Proposal" },
  { value: "negotiation", label: "Negotiation" },
  { value: "closed", label: "Closed" },
  { value: "lost", label: "Lost" },
];

function emptyForm(): OpportunityInput {
  return { title: "", value_cents: 0, stage: "new", probability: 50, notes: "" };
}

export default function PipelinePage() {
  const [opps, setOpps] = useState<Opportunity[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<unknown>(null);
  const [showForm, setShowForm] = useState(false);
  const [editing, setEditing] = useState<Opportunity | null>(null);
  const [form, setForm] = useState<OpportunityInput>(emptyForm());
  const [saving, setSaving] = useState(false);
  const [formError, setFormError] = useState<unknown>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      setOpps(await pipelineApi.list());
    } catch (err) {
      setError(err);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const byStage = useMemo(() => {
    const map = new Map<OpportunityStage, Opportunity[]>();
    for (const s of STAGES) map.set(s.value, []);
    for (const o of opps) {
      const bucket = map.get(o.stage);
      if (bucket) bucket.push(o);
    }
    return map;
  }, [opps]);

  const totals = useMemo(() => {
    const map = new Map<OpportunityStage, number>();
    for (const s of STAGES) map.set(s.value, 0);
    for (const o of opps) {
      map.set(o.stage, (map.get(o.stage) ?? 0) + (o.value_cents || 0));
    }
    return map;
  }, [opps]);

  const openCreate = () => {
    setEditing(null);
    setForm(emptyForm());
    setFormError(null);
    setShowForm(true);
  };

  const openEdit = (o: Opportunity) => {
    setEditing(o);
    setForm({
      title: o.title,
      value_cents: o.value_cents,
      stage: o.stage,
      probability: o.probability,
      expected_close_date: o.expected_close_date,
      notes: o.notes ?? "",
    });
    setFormError(null);
    setShowForm(true);
  };

  const save = async () => {
    setSaving(true);
    setFormError(null);
    try {
      if (editing) {
        await pipelineApi.update(editing.id, form);
      } else {
        await pipelineApi.create(form);
      }
      setShowForm(false);
      await load();
    } catch (err) {
      setFormError(err);
    } finally {
      setSaving(false);
    }
  };

  const remove = async (o: Opportunity) => {
    if (!window.confirm(`Delete "${o.title}"?`)) return;
    try {
      await pipelineApi.remove(o.id);
      await load();
    } catch (err) {
      setError(err);
    }
  };

  const moveStage = async (o: Opportunity, stage: OpportunityStage) => {
    if (stage === o.stage) return;
    try {
      await pipelineApi.update(o.id, { stage });
      await load();
    } catch (err) {
      setError(err);
    }
  };

  if (loading) return <Spinner label="Loading pipeline…" />;

  return (
    <div>
      <PageHeader
        title="Pipeline"
        subtitle="Opportunities by stage. Move deals forward as they progress."
        actions={
          <button type="button" className="btn btn-primary" onClick={openCreate}>
            + Opportunity
          </button>
        }
      />
      {error ? <ErrorBanner error={error} /> : null}

      {opps.length === 0 ? (
        <EmptyState
          title="No opportunities yet"
          hint="Add your first deal to start tracking the pipeline."
          action={
            <button type="button" className="btn btn-primary" onClick={openCreate}>
              + Opportunity
            </button>
          }
        />
      ) : (
        <div
          style={{
            display: "grid",
            gridTemplateColumns: "repeat(auto-fit, minmax(240px, 1fr))",
            gap: 12,
            alignItems: "start",
          }}
        >
          {STAGES.map((s) => {
            const items = byStage.get(s.value) ?? [];
            return (
              <section key={s.value} className="card" style={{ padding: 12 }}>
                <div
                  style={{
                    display: "flex",
                    justifyContent: "space-between",
                    alignItems: "baseline",
                    marginBottom: 8,
                  }}
                >
                  <strong>{s.label}</strong>
                  <span className="muted" style={{ fontSize: 12 }}>
                    {items.length} · {formatMoney(totals.get(s.value) ?? 0)}
                  </span>
                </div>
                <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
                  {items.map((o) => (
                    <div
                      key={o.id}
                      className="card"
                      style={{ padding: 10, cursor: "pointer" }}
                      onClick={() => openEdit(o)}
                      role="button"
                      tabIndex={0}
                      onKeyDown={(e) => {
                        if (e.key === "Enter") openEdit(o);
                      }}
                    >
                      <div
                        style={{
                          display: "flex",
                          justifyContent: "space-between",
                          gap: 8,
                        }}
                      >
                        <strong style={{ fontSize: 14 }}>{o.title}</strong>
                        <button
                          type="button"
                          className="btn btn-ghost btn-sm"
                          aria-label={`Delete ${o.title}`}
                          onClick={(e) => {
                            e.stopPropagation();
                            void remove(o);
                          }}
                        >
                          ✕
                        </button>
                      </div>
                      <div className="muted" style={{ fontSize: 12, marginTop: 4 }}>
                        {formatMoney(o.value_cents)} · {o.probability}%
                      </div>
                      <div style={{ marginTop: 8 }} onClick={(e) => e.stopPropagation()}>
                        <select
                          className="input"
                          value={o.stage}
                          aria-label={`Move ${o.title} to stage`}
                          onChange={(e) =>
                            void moveStage(o, e.target.value as OpportunityStage)
                          }
                        >
                          {STAGES.map((st) => (
                            <option key={st.value} value={st.value}>
                              {st.label}
                            </option>
                          ))}
                        </select>
                      </div>
                    </div>
                  ))}
                  {items.length === 0 && (
                    <div className="muted" style={{ fontSize: 12 }}>
                      No deals here.
                    </div>
                  )}
                </div>
              </section>
            );
          })}
        </div>
      )}

      {showForm && (
        <Modal
          title={editing ? "Edit opportunity" : "New opportunity"}
          onClose={() => setShowForm(false)}
        >
          {formError ? <ErrorBanner error={formError} /> : null}
          <Field label="Title">
            <input
              className="input"
              value={form.title}
              onChange={(e) => setForm({ ...form, title: e.target.value })}
              placeholder="Acme Corp — Q4 rollout"
            />
          </Field>
          <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12 }}>
            <Field label="Value (USD)">
              <input
                className="input"
                type="number"
                min={0}
                value={(form.value_cents ?? 0) / 100}
                onChange={(e) =>
                  setForm({
                    ...form,
                    value_cents: Math.round(Number(e.target.value || 0) * 100),
                  })
                }
              />
            </Field>
            <Field label="Probability %">
              <input
                className="input"
                type="number"
                min={0}
                max={100}
                value={form.probability ?? 50}
                onChange={(e) =>
                  setForm({ ...form, probability: Number(e.target.value || 0) })
                }
              />
            </Field>
          </div>
          <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12 }}>
            <Field label="Stage">
              <select
                className="input"
                value={form.stage}
                onChange={(e) =>
                  setForm({ ...form, stage: e.target.value as OpportunityStage })
                }
              >
                {STAGES.map((s) => (
                  <option key={s.value} value={s.value}>
                    {s.label}
                  </option>
                ))}
              </select>
            </Field>
            <Field label="Expected close">
              <input
                className="input"
                type="date"
                value={form.expected_close_date ? form.expected_close_date.slice(0, 10) : ""}
                onChange={(e) =>
                  setForm({
                    ...form,
                    expected_close_date: e.target.value
                      ? new Date(e.target.value).toISOString()
                      : null,
                  })
                }
              />
            </Field>
          </div>
          <Field label="Notes">
            <textarea
              className="input"
              rows={3}
              value={form.notes ?? ""}
              onChange={(e) => setForm({ ...form, notes: e.target.value })}
            />
          </Field>
          <div style={{ display: "flex", gap: 8, justifyContent: "flex-end" }}>
            <button
              type="button"
              className="btn btn-ghost"
              onClick={() => setShowForm(false)}
            >
              Cancel
            </button>
            <button
              type="button"
              className="btn btn-primary"
              disabled={saving || !form.title.trim()}
              onClick={() => void save()}
            >
              {saving ? "Saving…" : editing ? "Save" : "Create"}
            </button>
          </div>
        </Modal>
      )}
    </div>
  );
}
