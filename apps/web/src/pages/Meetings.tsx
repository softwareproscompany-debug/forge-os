import { useCallback, useEffect, useMemo, useState } from "react";
import { meetingsApi } from "../lib/api";
import type { Meeting, MeetingInput } from "../lib/api";
import {
  EmptyState,
  ErrorBanner,
  Field,
  Modal,
  PageHeader,
  Spinner,
} from "../components/ui";

function fmtDateTime(iso: string): string {
  const d = new Date(iso);
  return d.toLocaleString("en-US", {
    weekday: "short",
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
  });
}

function toLocalInput(iso: string): string {
  const d = new Date(iso);
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(
    d.getHours()
  )}:${pad(d.getMinutes())}`;
}

export default function MeetingsPage() {
  const [meetings, setMeetings] = useState<Meeting[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<unknown>(null);
  const [showForm, setShowForm] = useState(false);
  const [editing, setEditing] = useState<Meeting | null>(null);
  const [title, setTitle] = useState("");
  const [startsAt, setStartsAt] = useState("");
  const [endsAt, setEndsAt] = useState("");
  const [attendees, setAttendees] = useState("");
  const [notes, setNotes] = useState("");
  const [saving, setSaving] = useState(false);
  const [formError, setFormError] = useState<unknown>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      setMeetings(await meetingsApi.list());
    } catch (err) {
      setError(err);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const { upcoming, past } = useMemo(() => {
    const now = Date.now();
    const up: Meeting[] = [];
    const pa: Meeting[] = [];
    for (const m of meetings) {
      (new Date(m.starts_at).getTime() >= now ? up : pa).push(m);
    }
    return { upcoming: up, past: pa.reverse() };
  }, [meetings]);

  const openCreate = () => {
    setEditing(null);
    setTitle("");
    setStartsAt("");
    setEndsAt("");
    setAttendees("");
    setNotes("");
    setFormError(null);
    setShowForm(true);
  };

  const openEdit = (m: Meeting) => {
    setEditing(m);
    setTitle(m.title);
    setStartsAt(toLocalInput(m.starts_at));
    setEndsAt(m.ends_at ? toLocalInput(m.ends_at) : "");
    setAttendees(m.attendees.join(", "));
    setNotes(m.notes ?? "");
    setFormError(null);
    setShowForm(true);
  };

  const save = async () => {
    if (!title.trim() || !startsAt) return;
    setSaving(true);
    setFormError(null);
    const input: MeetingInput = {
      title: title.trim(),
      starts_at: new Date(startsAt).toISOString(),
      ends_at: endsAt ? new Date(endsAt).toISOString() : null,
      attendees: attendees
        .split(",")
        .map((a) => a.trim())
        .filter(Boolean),
      notes: notes.trim() || null,
    };
    try {
      if (editing) {
        await meetingsApi.update(editing.id, input);
      } else {
        await meetingsApi.create(input);
      }
      setShowForm(false);
      await load();
    } catch (err) {
      setFormError(err);
    } finally {
      setSaving(false);
    }
  };

  const remove = async (m: Meeting) => {
    if (!window.confirm(`Delete "${m.title}"?`)) return;
    try {
      await meetingsApi.remove(m.id);
      await load();
    } catch (err) {
      setError(err);
    }
  };

  const renderRow = (m: Meeting) => (
    <div key={m.id} className="card" style={{ padding: 12 }}>
      <div style={{ display: "flex", justifyContent: "space-between", gap: 8 }}>
        <div>
          <strong>{m.title}</strong>
          <div className="muted" style={{ fontSize: 12, marginTop: 4 }}>
            {fmtDateTime(m.starts_at)}
            {m.ends_at ? ` – ${fmtDateTime(m.ends_at)}` : ""}
          </div>
          {m.attendees.length > 0 && (
            <div className="muted" style={{ fontSize: 12 }}>
              {m.attendees.join(", ")}
            </div>
          )}
          {m.notes && (
            <div style={{ fontSize: 13, marginTop: 6 }}>{m.notes}</div>
          )}
        </div>
        <div style={{ display: "flex", gap: 4, alignItems: "flex-start" }}>
          <button
            type="button"
            className="btn btn-ghost btn-sm"
            onClick={() => openEdit(m)}
          >
            Edit
          </button>
          <button
            type="button"
            className="btn btn-ghost btn-sm"
            aria-label={`Delete ${m.title}`}
            onClick={() => void remove(m)}
          >
            ✕
          </button>
        </div>
      </div>
    </div>
  );

  if (loading) return <Spinner label="Loading meetings…" />;

  return (
    <div>
      <PageHeader
        title="Meetings"
        subtitle="Scheduled business meetings, upcoming first."
        actions={
          <button type="button" className="btn btn-primary" onClick={openCreate}>
            + Meeting
          </button>
        }
      />
      {error ? <ErrorBanner error={error} /> : null}

      {meetings.length === 0 ? (
        <EmptyState
          title="No meetings scheduled"
          hint="Add your first meeting to keep the calendar honest."
          action={
            <button type="button" className="btn btn-primary" onClick={openCreate}>
              + Meeting
            </button>
          }
        />
      ) : (
        <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
          <section>
            <h3 style={{ marginBottom: 8 }}>Upcoming ({upcoming.length})</h3>
            <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
              {upcoming.map(renderRow)}
              {upcoming.length === 0 && (
                <div className="muted">Nothing scheduled.</div>
              )}
            </div>
          </section>
          {past.length > 0 && (
            <section>
              <h3 style={{ marginBottom: 8 }}>Past ({past.length})</h3>
              <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
                {past.map(renderRow)}
              </div>
            </section>
          )}
        </div>
      )}

      {showForm && (
        <Modal
          title={editing ? "Edit meeting" : "New meeting"}
          onClose={() => setShowForm(false)}
        >
          {formError ? <ErrorBanner error={formError} /> : null}
          <Field label="Title">
            <input
              className="input"
              value={title}
              onChange={(e) => setTitle(e.target.value)}
              placeholder="Q4 planning call"
            />
          </Field>
          <div className="form-grid-2">
            <Field label="Starts">
              <input
                className="input"
                type="datetime-local"
                value={startsAt}
                onChange={(e) => setStartsAt(e.target.value)}
              />
            </Field>
            <Field label="Ends (optional)">
              <input
                className="input"
                type="datetime-local"
                value={endsAt}
                onChange={(e) => setEndsAt(e.target.value)}
              />
            </Field>
          </div>
          <Field label="Attendees (comma-separated)">
            <input
              className="input"
              value={attendees}
              onChange={(e) => setAttendees(e.target.value)}
              placeholder="sam@acme.com, jordan@acme.com"
            />
          </Field>
          <Field label="Notes">
            <textarea
              className="input"
              rows={3}
              value={notes}
              onChange={(e) => setNotes(e.target.value)}
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
              disabled={saving || !title.trim() || !startsAt}
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
