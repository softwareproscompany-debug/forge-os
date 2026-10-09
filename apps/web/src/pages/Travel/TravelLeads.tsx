import { useCallback, useEffect, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { travelApi } from "../../lib/api";
import type { TravelLead, TravelLeadInput, TravelLeadStatus } from "../../lib/api";
import {
  Badge,
  EmptyState,
  ErrorBanner,
  Field,
  Modal,
  PageHeader,
  Spinner,
} from "../../components/ui";

const STATUSES: { value: TravelLeadStatus; label: string }[] = [
  { value: "new", label: "New" },
  { value: "qualified", label: "Qualified" },
  { value: "quoted", label: "Quoted" },
  { value: "booked", label: "Booked" },
  { value: "lost", label: "Lost" },
];

function emptyForm(): TravelLeadInput {
  return { destination: "", trip_purpose: "", source: "", budget: "" };
}

export default function TravelLeadsPage() {
  const [searchParams] = useSearchParams();
  const [leads, setLeads] = useState<TravelLead[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<unknown>(null);
  const [statusFilter, setStatusFilter] = useState<TravelLeadStatus | "">("");
  const [showForm, setShowForm] = useState(false);
  const [editing, setEditing] = useState<TravelLead | null>(null);
  const [form, setForm] = useState<TravelLeadInput>(emptyForm());
  const [saving, setSaving] = useState(false);
  const [formError, setFormError] = useState<unknown>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const page = await travelApi.listLeads(
        statusFilter ? { status: statusFilter } : undefined
      );
      setLeads(page.items);
    } catch (err) {
      setError(err);
    } finally {
      setLoading(false);
    }
  }, [statusFilter]);

  useEffect(() => {
    void load();
  }, [load]);

  // Deep-link from the dashboard: ?lead=<id> opens the detail modal.
  const deepLeadId = searchParams.get("lead");
  const [detail, setDetail] = useState<TravelLead | null>(null);
  useEffect(() => {
    if (!deepLeadId) {
      setDetail(null);
      return;
    }
    travelApi
      .getLead(deepLeadId)
      .then(setDetail)
      .catch(() => setDetail(null));
  }, [deepLeadId]);

  const openCreate = () => {
    setEditing(null);
    setForm(emptyForm());
    setFormError(null);
    setShowForm(true);
  };

  const openEdit = (l: TravelLead) => {
    setEditing(l);
    setForm({
      destination: l.destination ?? "",
      trip_purpose: l.trip_purpose ?? "",
      source: l.source ?? "",
      budget: l.budget ?? "",
      date_start: l.date_start ?? "",
      date_end: l.date_end ?? "",
    });
    setFormError(null);
    setShowForm(true);
  };

  const save = async () => {
    setSaving(true);
    setFormError(null);
    try {
      const clean: TravelLeadInput = {
        ...form,
        budget: form.budget ? String(form.budget) : undefined,
        date_start: form.date_start || undefined,
        date_end: form.date_end || undefined,
      };
      if (editing) await travelApi.updateLead(editing.id, clean);
      else await travelApi.createLead(clean);
      setShowForm(false);
      await load();
    } catch (err) {
      setFormError(err);
    } finally {
      setSaving(false);
    }
  };

  const transition = async (l: TravelLead, status: TravelLeadStatus) => {
    try {
      const updated = await travelApi.setLeadStatus(l.id, status);
      setLeads((prev) => prev.map((x) => (x.id === l.id ? updated : x)));
      if (detail?.id === l.id) setDetail(updated);
    } catch (err) {
      setError(err);
    }
  };

  const remove = async (l: TravelLead) => {
    if (!window.confirm(`Delete lead to ${l.destination || "unknown"}?`)) return;
    await travelApi.removeLead(l.id);
    setDetail(null);
    await load();
  };

  return (
    <div>
      <PageHeader
        title="Travel leads"
        subtitle="New → qualified → quoted → booked. Lost leads can be reopened."
        actions={
          <button type="button" className="btn btn-primary" onClick={openCreate}>
            + New lead
          </button>
        }
      />

      <div className="filters">
        <select
          className="input"
          value={statusFilter}
          onChange={(e) => setStatusFilter(e.target.value as TravelLeadStatus | "")}
          aria-label="Filter by status"
        >
          <option value="">All statuses</option>
          {STATUSES.map((s) => (
            <option key={s.value} value={s.value}>
              {s.label}
            </option>
          ))}
        </select>
      </div>

      {error ? <ErrorBanner error={error} /> : null}
      {loading ? (
        <Spinner label="Loading leads…" />
      ) : leads.length === 0 ? (
        <EmptyState
          title="No leads yet"
          hint="Capture the first inquiry — destination, dates, and budget are enough to start."
          action={
            <button type="button" className="btn btn-primary" onClick={openCreate}>
              + New lead
            </button>
          }
        />
      ) : (
        <div className="table-wrap">
          <table className="table">
            <thead>
              <tr>
                <th>Destination</th>
                <th>Dates</th>
                <th>Budget</th>
                <th>Status</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {leads.map((l) => (
                <tr key={l.id}>
                  <td>
                    <button
                      type="button"
                      className="link-btn"
                      onClick={() => setDetail(l)}
                    >
                      {l.destination || "—"}
                    </button>
                    {l.trip_purpose && (
                      <div className="muted small">{l.trip_purpose}</div>
                    )}
                  </td>
                  <td className="muted">
                    {l.date_start || "—"}
                    {l.date_end ? ` → ${l.date_end}` : ""}
                  </td>
                  <td className="muted">
                    {l.budget ? `$${Number(l.budget).toLocaleString()}` : "—"}
                  </td>
                  <td>
                    <Badge value={l.status} />
                  </td>
                  <td className="row-actions">
                    <button
                      type="button"
                      className="btn btn-ghost btn-sm"
                      onClick={() => openEdit(l)}
                    >
                      Edit
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {showForm && (
        <Modal
          title={editing ? "Edit lead" : "New travel lead"}
          onClose={() => setShowForm(false)}
        >
          <div className="modal-body">
            {formError ? <ErrorBanner error={formError} /> : null}
            <Field label="Destination">
              <input
                className="input"
                value={form.destination ?? ""}
                onChange={(e) => setForm({ ...form, destination: e.target.value })}
                placeholder="Paris, Tokyo…"
              />
            </Field>
            <Field label="Trip purpose">
              <input
                className="input"
                value={form.trip_purpose ?? ""}
                onChange={(e) => setForm({ ...form, trip_purpose: e.target.value })}
                placeholder="Honeymoon, business, family…"
              />
            </Field>
            <div className="form-grid-2">
              <Field label="Start date">
                <input
                  className="input"
                  type="date"
                  value={form.date_start ?? ""}
                  onChange={(e) => setForm({ ...form, date_start: e.target.value })}
                />
              </Field>
              <Field label="End date">
                <input
                  className="input"
                  type="date"
                  value={form.date_end ?? ""}
                  onChange={(e) => setForm({ ...form, date_end: e.target.value })}
                />
              </Field>
            </div>
            <div className="form-grid-2">
              <Field label="Budget (USD)">
                <input
                  className="input"
                  type="number"
                  min={0}
                  step="0.01"
                  value={form.budget ?? ""}
                  onChange={(e) => setForm({ ...form, budget: e.target.value })}
                  placeholder="5000"
                />
              </Field>
              <Field label="Source">
                <input
                  className="input"
                  value={form.source ?? ""}
                  onChange={(e) => setForm({ ...form, source: e.target.value })}
                  placeholder="Website, referral…"
                />
              </Field>
            </div>
            <div className="modal-actions">
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
                disabled={saving}
                onClick={() => void save()}
              >
                {saving ? "Saving…" : editing ? "Save" : "Create lead"}
              </button>
            </div>
          </div>
        </Modal>
      )}

      {detail && (
        <Modal title={detail.destination || "Lead"} onClose={() => setDetail(null)}>
          <div className="modal-body">
            <div className="detail-row">
              <span className="muted">Status</span>
              <Badge value={detail.status} />
            </div>
            {detail.trip_purpose && (
              <div className="detail-row">
                <span className="muted">Purpose</span>
                <span>{detail.trip_purpose}</span>
              </div>
            )}
            {(detail.date_start || detail.date_end) && (
              <div className="detail-row">
                <span className="muted">Dates</span>
                <span>
                  {detail.date_start ?? "—"} → {detail.date_end ?? "—"}
                </span>
              </div>
            )}
            {detail.budget && (
              <div className="detail-row">
                <span className="muted">Budget</span>
                <span>${Number(detail.budget).toLocaleString()}</span>
              </div>
            )}
            {detail.source && (
              <div className="detail-row">
                <span className="muted">Source</span>
                <span>{detail.source}</span>
              </div>
            )}
            <div className="detail-row">
              <span className="muted">Move to</span>
              <div className="btn-row">
                {STATUSES.filter((s) => s.value !== detail.status).map((s) => (
                  <button
                    key={s.value}
                    type="button"
                    className="btn btn-ghost btn-sm"
                    onClick={() => void transition(detail, s.value)}
                  >
                    {s.label}
                  </button>
                ))}
              </div>
            </div>
            <div className="modal-actions">
              <button
                type="button"
                className="btn btn-danger"
                onClick={() => void remove(detail)}
              >
                Delete
              </button>
              <button
                type="button"
                className="btn btn-ghost"
                onClick={() => openEdit(detail)}
              >
                Edit
              </button>
            </div>
          </div>
        </Modal>
      )}
    </div>
  );
}
