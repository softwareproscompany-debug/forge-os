import { useCallback, useEffect, useState } from "react";
import { travelApi } from "../../lib/api";
import type {
  TravelCustomer,
  TravelCustomerInput,
  TravelCustomerType,
  TravelerProfile,
  TravelerProfileInput,
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

function emptyCustomer(): TravelCustomerInput {
  return { name: "", email: "", phone: "", type: "individual", notes: "" };
}

function emptyTraveler(customerId: string): TravelerProfileInput {
  return { customer_id: customerId, full_name: "" };
}

export default function TravelCustomersPage() {
  const [customers, setCustomers] = useState<TravelCustomer[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<unknown>(null);
  const [query, setQuery] = useState("");

  const [showForm, setShowForm] = useState(false);
  const [editing, setEditing] = useState<TravelCustomer | null>(null);
  const [form, setForm] = useState<TravelCustomerInput>(emptyCustomer());
  const [saving, setSaving] = useState(false);
  const [formError, setFormError] = useState<unknown>(null);

  const [detail, setDetail] = useState<TravelCustomer | null>(null);
  const [travelers, setTravelers] = useState<TravelerProfile[]>([]);
  const [travelersLoading, setTravelersLoading] = useState(false);
  const [showTravelerForm, setShowTravelerForm] = useState(false);
  const [travelerForm, setTravelerForm] = useState<TravelerProfileInput>(
    emptyTraveler("")
  );

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const page = await travelApi.listCustomers(
        query ? { q: query } : undefined
      );
      setCustomers(page.items);
    } catch (err) {
      setError(err);
    } finally {
      setLoading(false);
    }
  }, [query]);

  useEffect(() => {
    void load();
  }, [load]);

  const openDetail = async (c: TravelCustomer) => {
    setDetail(c);
    setTravelersLoading(true);
    try {
      const page = await travelApi.listCustomerTravelers(c.id);
      setTravelers(page.items);
    } catch {
      setTravelers([]);
    } finally {
      setTravelersLoading(false);
    }
  };

  const openCreate = () => {
    setEditing(null);
    setForm(emptyCustomer());
    setFormError(null);
    setShowForm(true);
  };

  const openEdit = (c: TravelCustomer) => {
    setEditing(c);
    setForm({
      name: c.name,
      email: c.email ?? "",
      phone: c.phone ?? "",
      type: c.type,
      notes: c.notes ?? "",
    });
    setFormError(null);
    setShowForm(true);
  };

  const save = async () => {
    if (!form.name.trim()) {
      setFormError("Name is required.");
      return;
    }
    setSaving(true);
    setFormError(null);
    try {
      const clean = {
        ...form,
        email: form.email || undefined,
        phone: form.phone || undefined,
        notes: form.notes || undefined,
      };
      if (editing) {
        const updated = await travelApi.updateCustomer(editing.id, clean);
        setCustomers((prev) => prev.map((x) => (x.id === updated.id ? updated : x)));
        if (detail?.id === updated.id) setDetail(updated);
      } else {
        await travelApi.createCustomer(clean);
      }
      setShowForm(false);
      await load();
    } catch (err) {
      setFormError(err);
    } finally {
      setSaving(false);
    }
  };

  const remove = async (c: TravelCustomer) => {
    if (!window.confirm(`Delete customer ${c.name}?`)) return;
    await travelApi.removeCustomer(c.id);
    setDetail(null);
    await load();
  };

  const saveTraveler = async () => {
    if (!detail || !travelerForm.full_name.trim()) return;
    try {
      await travelApi.createTraveler({
        ...travelerForm,
        customer_id: detail.id,
      });
      setShowTravelerForm(false);
      const page = await travelApi.listCustomerTravelers(detail.id);
      setTravelers(page.items);
    } catch (err) {
      setFormError(err);
    }
  };

  const removeTraveler = async (t: TravelerProfile) => {
    if (!window.confirm(`Remove traveler ${t.full_name}?`)) return;
    await travelApi.removeTraveler(t.id);
    if (detail) {
      const page = await travelApi.listCustomerTravelers(detail.id);
      setTravelers(page.items);
    }
  };

  return (
    <div>
      <PageHeader
        title="Travel customers"
        subtitle="Individual and corporate customers of the agency."
        actions={
          <button type="button" className="btn btn-primary" onClick={openCreate}>
            + New customer
          </button>
        }
      />

      <div className="filters">
        <input
          className="input"
          placeholder="Search name, email, phone…"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          aria-label="Search customers"
        />
      </div>

      {error ? <ErrorBanner error={error} /> : null}
      {loading ? (
        <Spinner label="Loading customers…" />
      ) : customers.length === 0 ? (
        <EmptyState
          title="No customers yet"
          hint="Add customers so leads and trip requests can be attached to them."
          action={
            <button type="button" className="btn btn-primary" onClick={openCreate}>
              + New customer
            </button>
          }
        />
      ) : (
        <div className="table-wrap">
          <table className="table">
            <thead>
              <tr>
                <th>Name</th>
                <th>Contact</th>
                <th>Type</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {customers.map((c) => (
                <tr key={c.id}>
                  <td>
                    <button
                      type="button"
                      className="link-btn"
                      onClick={() => void openDetail(c)}
                    >
                      {c.name}
                    </button>
                  </td>
                  <td className="muted">
                    {c.email || c.phone || "—"}
                  </td>
                  <td>
                    <Badge value={c.type} />
                  </td>
                  <td className="row-actions">
                    <button
                      type="button"
                      className="btn btn-ghost btn-sm"
                      onClick={() => openEdit(c)}
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
          title={editing ? "Edit customer" : "New customer"}
          onClose={() => setShowForm(false)}
        >
          <div className="modal-body">
            {formError ? <ErrorBanner error={formError} /> : null}
            <Field label="Name">
              <input
                className="input"
                value={form.name ?? ""}
                onChange={(e) => setForm({ ...form, name: e.target.value })}
                placeholder="Jane Doe / Acme Corp"
              />
            </Field>
            <div className="form-grid-2">
              <Field label="Email">
                <input
                  className="input"
                  type="email"
                  value={form.email ?? ""}
                  onChange={(e) => setForm({ ...form, email: e.target.value })}
                />
              </Field>
              <Field label="Phone">
                <input
                  className="input"
                  value={form.phone ?? ""}
                  onChange={(e) => setForm({ ...form, phone: e.target.value })}
                />
              </Field>
            </div>
            <Field label="Type">
              <select
                className="input"
                value={form.type ?? "individual"}
                onChange={(e) =>
                  setForm({ ...form, type: e.target.value as TravelCustomerType })
                }
              >
                <option value="individual">Individual</option>
                <option value="corporate">Corporate</option>
              </select>
            </Field>
            <Field label="Notes">
              <textarea
                className="input"
                rows={3}
                value={form.notes ?? ""}
                onChange={(e) => setForm({ ...form, notes: e.target.value })}
              />
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

      {detail && (
        <Modal title={detail.name} onClose={() => setDetail(null)} wide>
          <div className="modal-body">
            <div className="detail-row">
              <span className="muted">Type</span>
              <Badge value={detail.type} />
            </div>
            {detail.email && (
              <div className="detail-row">
                <span className="muted">Email</span>
                <span>{detail.email}</span>
              </div>
            )}
            {detail.phone && (
              <div className="detail-row">
                <span className="muted">Phone</span>
                <span>{detail.phone}</span>
              </div>
            )}
            {detail.notes && (
              <div className="detail-row">
                <span className="muted">Notes</span>
                <span>{detail.notes}</span>
              </div>
            )}

            <h3 style={{ marginTop: 16 }}>Travelers</h3>
            {travelersLoading ? (
              <Spinner label="Loading travelers…" />
            ) : travelers.length === 0 ? (
              <div className="muted">No travelers attached yet.</div>
            ) : (
              <div className="table-wrap">
                <table className="table">
                  <thead>
                    <tr>
                      <th>Name</th>
                      <th>DOB</th>
                      <th />
                    </tr>
                  </thead>
                  <tbody>
                    {travelers.map((t) => (
                      <tr key={t.id}>
                        <td>{t.full_name}</td>
                        <td className="muted">{t.dob || "—"}</td>
                        <td className="row-actions">
                          <button
                            type="button"
                            className="btn btn-ghost btn-sm"
                            onClick={() => void removeTraveler(t)}
                          >
                            Remove
                          </button>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}

            {showTravelerForm ? (
              <div style={{ marginTop: 12 }}>
                <Field label="Full name">
                  <input
                    className="input"
                    value={travelerForm.full_name ?? ""}
                    onChange={(e) =>
                      setTravelerForm({ ...travelerForm, full_name: e.target.value })
                    }
                    placeholder="Full name as on passport"
                  />
                </Field>
                <Field label="Date of birth">
                  <input
                    className="input"
                    type="date"
                    value={travelerForm.dob ?? ""}
                    onChange={(e) =>
                      setTravelerForm({ ...travelerForm, dob: e.target.value })
                    }
                  />
                </Field>
                <div className="modal-actions">
                  <button
                    type="button"
                    className="btn btn-ghost"
                    onClick={() => setShowTravelerForm(false)}
                  >
                    Cancel
                  </button>
                  <button
                    type="button"
                    className="btn btn-primary"
                    onClick={() => void saveTraveler()}
                  >
                    Add traveler
                  </button>
                </div>
              </div>
            ) : (
              <button
                type="button"
                className="btn btn-ghost"
                style={{ marginTop: 12 }}
                onClick={() => {
                  setTravelerForm(emptyTraveler(detail.id));
                  setShowTravelerForm(true);
                }}
              >
                + Add traveler
              </button>
            )}

            <div className="modal-actions" style={{ marginTop: 16 }}>
              <button
                type="button"
                className="btn btn-danger"
                onClick={() => void remove(detail)}
              >
                Delete customer
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
