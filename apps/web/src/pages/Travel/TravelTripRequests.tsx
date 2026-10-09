import { useCallback, useEffect, useState } from "react";
import { travelApi } from "../../lib/api";
import type {
  TripRequest,
  TripRequestInput,
  TripRequestStatus,
} from "../../lib/api";
import {
  Badge,
  EmptyState,
  ErrorBanner,
  Field,
  Modal,
  PageHeader,
  Spinner,
} from "../../components/ui";

const STATUSES: { value: TripRequestStatus; label: string }[] = [
  { value: "draft", label: "Draft" },
  { value: "open", label: "Open" },
  { value: "in_progress", label: "In progress" },
  { value: "completed", label: "Completed" },
  { value: "cancelled", label: "Cancelled" },
];

function emptyForm(): TripRequestInput {
  return { destinations: [""], party_size: 1, status: "open" };
}

export default function TravelTripRequestsPage() {
  const [requests, setRequests] = useState<TripRequest[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<unknown>(null);
  const [statusFilter, setStatusFilter] = useState<TripRequestStatus | "">("");

  const [showForm, setShowForm] = useState(false);
  const [editing, setEditing] = useState<TripRequest | null>(null);
  const [form, setForm] = useState<TripRequestInput>(emptyForm());
  const [saving, setSaving] = useState(false);
  const [formError, setFormError] = useState<unknown>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const page = await travelApi.listTripRequests(
        statusFilter ? { status: statusFilter } : undefined
      );
      setRequests(page.items);
    } catch (err) {
      setError(err);
    } finally {
      setLoading(false);
    }
  }, [statusFilter]);

  useEffect(() => {
    void load();
  }, [load]);

  const openCreate = () => {
    setEditing(null);
    setForm(emptyForm());
    setFormError(null);
    setShowForm(true);
  };

  const openEdit = (r: TripRequest) => {
    setEditing(r);
    setForm({
      party_size: r.party_size,
      origin: r.origin ?? "",
      destinations: r.destinations.length ? [...r.destinations] : [""],
      date_start: r.date_start ?? "",
      date_end: r.date_end ?? "",
      flexibility: r.flexibility ?? "",
      status: r.status,
      lead_id: r.lead_id ?? undefined,
      customer_id: r.customer_id ?? undefined,
    });
    setFormError(null);
    setShowForm(true);
  };

  const save = async () => {
    setSaving(true);
    setFormError(null);
    try {
      const destinations = (form.destinations ?? [])
        .map((d) => d.trim())
        .filter(Boolean);
      const clean: TripRequestInput = {
        ...form,
        destinations,
        origin: form.origin || undefined,
        date_start: form.date_start || undefined,
        date_end: form.date_end || undefined,
        flexibility: form.flexibility || undefined,
      };
      if (editing) await travelApi.updateTripRequest(editing.id, clean);
      else await travelApi.createTripRequest(clean);
      setShowForm(false);
      await load();
    } catch (err) {
      setFormError(err);
    } finally {
      setSaving(false);
    }
  };

  const remove = async (r: TripRequest) => {
    if (!window.confirm("Delete this trip request?")) return;
    await travelApi.removeTripRequest(r.id);
    await load();
  };

  const setDestination = (i: number, value: string) => {
    const next = [...(form.destinations ?? [])];
    next[i] = value;
    setForm({ ...form, destinations: next });
  };

  return (
    <div>
      <PageHeader
        title="Trip requests"
        subtitle="Concrete trip plans derived from qualified leads."
        actions={
          <button type="button" className="btn btn-primary" onClick={openCreate}>
            + New request
          </button>
        }
      />

      <div className="filters">
        <select
          className="input"
          value={statusFilter}
          onChange={(e) => setStatusFilter(e.target.value as TripRequestStatus | "")}
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
        <Spinner label="Loading trip requests…" />
      ) : requests.length === 0 ? (
        <EmptyState
          title="No trip requests yet"
          hint="Turn a qualified lead into a trip request with party size, route, and dates."
          action={
            <button type="button" className="btn btn-primary" onClick={openCreate}>
              + New request
            </button>
          }
        />
      ) : (
        <div className="table-wrap">
          <table className="table">
            <thead>
              <tr>
                <th>Route</th>
                <th>Dates</th>
                <th>Party</th>
                <th>Status</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {requests.map((r) => (
                <tr key={r.id}>
                  <td>
                    {r.origin || "—"} → {r.destinations.join(" → ") || "—"}
                    {r.flexibility && (
                      <div className="muted small">{r.flexibility}</div>
                    )}
                  </td>
                  <td className="muted">
                    {r.date_start || "—"}
                    {r.date_end ? ` → ${r.date_end}` : ""}
                  </td>
                  <td className="muted">{r.party_size}</td>
                  <td>
                    <Badge value={r.status} />
                  </td>
                  <td className="row-actions">
                    <button
                      type="button"
                      className="btn btn-ghost btn-sm"
                      onClick={() => openEdit(r)}
                    >
                      Edit
                    </button>
                    <button
                      type="button"
                      className="btn btn-ghost btn-sm"
                      onClick={() => void remove(r)}
                    >
                      Delete
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
          title={editing ? "Edit trip request" : "New trip request"}
          onClose={() => setShowForm(false)}
        >
          <div className="modal-body">
            {formError ? <ErrorBanner error={formError} /> : null}
            <div className="form-grid-2">
              <Field label="Origin">
                <input
                  className="input"
                  value={form.origin ?? ""}
                  onChange={(e) => setForm({ ...form, origin: e.target.value })}
                  placeholder="IAH"
                />
              </Field>
              <Field label="Party size">
                <input
                  className="input"
                  type="number"
                  min={1}
                  value={form.party_size ?? 1}
                  onChange={(e) =>
                    setForm({ ...form, party_size: Number(e.target.value) || 1 })
                  }
                />
              </Field>
            </div>
            <Field label="Destinations (in order)">
              {(form.destinations ?? [""]).map((d, i) => (
                <div key={i} style={{ display: "flex", gap: 8, marginBottom: 8 }}>
                  <input
                    className="input"
                    value={d}
                    onChange={(e) => setDestination(i, e.target.value)}
                    placeholder={i === 0 ? "CDG" : "Next stop…"}
                  />
                  {(form.destinations ?? []).length > 1 && (
                    <button
                      type="button"
                      className="btn btn-ghost btn-sm"
                      onClick={() =>
                        setForm({
                          ...form,
                          destinations: (form.destinations ?? []).filter(
                            (_, j) => j !== i
                          ),
                        })
                      }
                      aria-label="Remove destination"
                    >
                      ✕
                    </button>
                  )}
                </div>
              ))}
              <button
                type="button"
                className="btn btn-ghost btn-sm"
                onClick={() =>
                  setForm({ ...form, destinations: [...(form.destinations ?? []), ""] })
                }
              >
                + Add stop
              </button>
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
            <Field label="Flexibility">
              <input
                className="input"
                value={form.flexibility ?? ""}
                onChange={(e) => setForm({ ...form, flexibility: e.target.value })}
                placeholder="Dates flexible ±3 days…"
              />
            </Field>
            <Field label="Status">
              <select
                className="input"
                value={form.status ?? "open"}
                onChange={(e) =>
                  setForm({ ...form, status: e.target.value as TripRequestStatus })
                }
              >
                {STATUSES.map((s) => (
                  <option key={s.value} value={s.value}>
                    {s.label}
                  </option>
                ))}
              </select>
            </Field>
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
                {saving ? "Saving…" : editing ? "Save" : "Create"}
              </button>
            </div>
          </div>
        </Modal>
      )}
    </div>
  );
}
