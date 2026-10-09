import { useCallback, useEffect, useMemo, useState } from "react";
import { templateApi } from "../lib/api";
import type { Channel, Template } from "../lib/api";
import { ErrorBanner, PageHeader, Spinner } from "../components/ui";

function nicheOf(t: Template): string {
  const m = t.name.match(/^\[([^\]]+)\]/);
  return m ? m[1] : "General";
}

function shortName(t: Template): string {
  return t.name.replace(/^\[[^\]]+\]\s*/, "");
}

const CHANNEL_LABEL: Record<Channel, string> = {
  email: "Email",
  sms: "SMS",
  social: "Social",
};

/** Extract {{variables}} from a template string. */
function extractVars(...parts: Array<string | null>): string[] {
  const found = new Set<string>();
  const re = /\{\{(\w+)\}\}/g;
  for (const p of parts) {
    if (!p) continue;
    let m: RegExpExecArray | null;
    while ((m = re.exec(p))) found.add(m[1]);
  }
  return [...found].sort();
}

function TemplateCard({
  template,
  onPreview,
  onDelete,
}: {
  template: Template;
  onPreview: () => void;
  onDelete: () => void;
}) {
  const [confirming, setConfirming] = useState(false);
  return (
    <div className="panel" style={{ animation: "fade-slide-in 0.4s backwards" }}>
      <div style={{ display: "flex", justifyContent: "space-between", gap: 8, alignItems: "flex-start" }}>
        <div style={{ minWidth: 0 }}>
          <div style={{ fontWeight: 650, fontSize: 14 }}>{shortName(template)}</div>
          <div className="muted" style={{ fontSize: 12 }}>{nicheOf(template)}</div>
        </div>
        <span className="badge badge-gray" style={{ flexShrink: 0 }}>
          {CHANNEL_LABEL[template.channel]}
        </span>
      </div>
      <div className="muted" style={{ fontSize: 12, marginTop: 8 }}>
        {template.variables.length} variable{template.variables.length === 1 ? "" : "s"}:{" "}
        <span style={{ fontFamily: "var(--mono)", fontSize: 11 }}>
          {template.variables.slice(0, 4).join(", ")}
          {template.variables.length > 4 ? "…" : ""}
        </span>
      </div>
      <div style={{ display: "flex", gap: 8, marginTop: 12 }}>
        <button type="button" className="btn btn-sm btn-primary" onClick={onPreview}>
          Preview
        </button>
        {confirming ? (
          <>
            <button
              type="button"
              className="btn btn-sm btn-danger"
              onClick={() => {
                onDelete();
                setConfirming(false);
              }}
            >
              Confirm
            </button>
            <button
              type="button"
              className="btn btn-sm"
              onClick={() => setConfirming(false)}
            >
              Cancel
            </button>
          </>
        ) : (
          <button
            type="button"
            className="btn btn-sm"
            onClick={() => setConfirming(true)}
          >
            Delete
          </button>
        )}
      </div>
    </div>
  );
}

function PreviewModal({
  template,
  onClose,
}: {
  template: Template;
  onClose: () => void;
}) {
  const [values, setValues] = useState<Record<string, string>>(() =>
    Object.fromEntries(template.variables.map((v) => [v, ""]))
  );
  const [rendered, setRendered] = useState<{ subject: string | null; body: string } | null>(null);
  const [rendering, setRendering] = useState(false);

  const render = useCallback(async () => {
    setRendering(true);
    try {
      const r = await templateApi.preview(template.id, values);
      setRendered(r);
    } finally {
      setRendering(false);
    }
  }, [template.id, values]);

  useEffect(() => {
    void render();
  }, [render]);

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div
        className="modal modal-wide"
        onClick={(e) => e.stopPropagation()}
        role="dialog"
        aria-label={`Preview ${template.name}`}
      >
        <div className="panel-head">
          <span className="panel-title">{shortName(template)}</span>
          <button type="button" className="btn btn-sm" onClick={onClose}>
            ✕ Close
          </button>
        </div>
        <div
          style={{
            display: "grid",
            gridTemplateColumns: "1fr 1fr",
            gap: 16,
          }}
          className="preview-grid"
        >
          <div>
            <h3 style={{ marginBottom: 8 }}>Variables</h3>
            <div style={{ display: "flex", flexDirection: "column", gap: 8, maxHeight: 320, overflowY: "auto" }}>
              {template.variables.map((v) => (
                <label key={v} style={{ fontSize: 12.5 }}>
                  <span className="muted" style={{ fontFamily: "var(--mono)", fontSize: 11.5, display: "block", marginBottom: 2 }}>
                    {`{{${v}}}`}
                  </span>
                  <input
                    className="input"
                    value={values[v] ?? ""}
                    onChange={(e) => setValues((s) => ({ ...s, [v]: e.target.value }))}
                    placeholder={`Enter ${v.replace(/_/g, " ")}…`}
                  />
                </label>
              ))}
              {template.variables.length === 0 && (
                <p className="muted" style={{ fontSize: 13 }}>No variables in this template.</p>
              )}
            </div>
          </div>
          <div>
            <h3 style={{ marginBottom: 8 }}>Rendered</h3>
            <div
              className="panel"
              style={{ background: "var(--bg)", minHeight: 200, fontSize: 13.5, whiteSpace: "pre-wrap" }}
            >
              {rendering ? (
                <span className="muted">Rendering…</span>
              ) : rendered ? (
                <>
                  {rendered.subject && (
                    <div style={{ fontWeight: 700, marginBottom: 8 }}>{rendered.subject}</div>
                  )}
                  {rendered.body}
                </>
              ) : null}
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}

export default function TemplatesPage() {
  const [templates, setTemplates] = useState<Template[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<unknown>(null);
  const [niche, setNiche] = useState("All");
  const [channel, setChannel] = useState<Channel | "all">("all");
  const [query, setQuery] = useState("");
  const [previewing, setPreviewing] = useState<Template | null>(null);
  const [creating, setCreating] = useState(false);

  // Create form
  const [fName, setFName] = useState("");
  const [fNiche, setFNiche] = useState("General");
  const [fChannel, setFChannel] = useState<Channel>("email");
  const [fSubject, setFSubject] = useState("");
  const [fBody, setFBody] = useState("");
  const [saving, setSaving] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const all = await templateApi.list();
      setTemplates(all);
    } catch (e) {
      setError(e);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const niches = useMemo(() => {
    const s = new Set(templates.map(nicheOf));
    return ["All", ...[...s].sort()];
  }, [templates]);

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    return templates.filter(
      (t) =>
        (niche === "All" || nicheOf(t) === niche) &&
        (channel === "all" || t.channel === channel) &&
        (!q || t.name.toLowerCase().includes(q) || t.body_template.toLowerCase().includes(q))
    );
  }, [templates, niche, channel, query]);

  const remove = async (id: string) => {
    await templateApi.remove(id);
    setTemplates((ts) => ts.filter((t) => t.id !== id));
  };

  const create = async () => {
    if (!fName.trim() || !fBody.trim()) return;
    setSaving(true);
    try {
      const name = fNiche === "General" ? fName.trim() : `[${fNiche}] ${fName.trim()}`;
      const variables = extractVars(fSubject, fBody);
      const t = await templateApi.create({
        name,
        channel: fChannel,
        subject_template: fSubject.trim() || null,
        body_template: fBody.trim(),
        variables,
      });
      setTemplates((ts) => [t, ...ts]);
      setCreating(false);
      setFName("");
      setFSubject("");
      setFBody("");
    } finally {
      setSaving(false);
    }
  };

  if (loading) return <Spinner label="Loading templates…" />;

  return (
    <div>
      <PageHeader
        title="Templates"
        subtitle={`${templates.length} proven templates across ${niches.length - 1} business niches — preview with live variables, or create your own.`}
        actions={
          <button type="button" className="btn btn-primary" onClick={() => setCreating((v) => !v)}>
            {creating ? "Cancel" : "+ New template"}
          </button>
        }
      />
      <ErrorBanner error={error} onRetry={load} />

      {creating && (
        <section className="panel" style={{ marginBottom: 16 }}>
          <div className="panel-head">
            <span className="panel-title">New template</span>
          </div>
          <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr 1fr", gap: 12 }} className="preview-grid">
            <label style={{ fontSize: 13 }}>
              <span className="muted" style={{ display: "block", marginBottom: 4 }}>Name</span>
              <input className="input" value={fName} onChange={(e) => setFName(e.target.value)} placeholder="Abandoned Cart" />
            </label>
            <label style={{ fontSize: 13 }}>
              <span className="muted" style={{ display: "block", marginBottom: 4 }}>Niche</span>
              <input className="input" value={fNiche} onChange={(e) => setFNiche(e.target.value)} placeholder="General" />
            </label>
            <label style={{ fontSize: 13 }}>
              <span className="muted" style={{ display: "block", marginBottom: 4 }}>Channel</span>
              <select className="input" value={fChannel} onChange={(e) => setFChannel(e.target.value as Channel)}>
                <option value="email">Email</option>
                <option value="sms">SMS</option>
                <option value="social">Social</option>
              </select>
            </label>
          </div>
          {fChannel === "email" && (
            <label style={{ fontSize: 13, display: "block", marginTop: 12 }}>
              <span className="muted" style={{ display: "block", marginBottom: 4 }}>Subject (use {"{{variable}}"} placeholders)</span>
              <input className="input" value={fSubject} onChange={(e) => setFSubject(e.target.value)} placeholder="We miss you, {{customer_name}}" />
            </label>
          )}
          <label style={{ fontSize: 13, display: "block", marginTop: 12 }}>
            <span className="muted" style={{ display: "block", marginBottom: 4 }}>Body (use {"{{variable}}"} placeholders)</span>
            <textarea
              className="input"
              rows={6}
              value={fBody}
              onChange={(e) => setFBody(e.target.value)}
              placeholder={"Hi {{customer_name}},\n\n…"}
              style={{ resize: "vertical" }}
            />
          </label>
          <div className="muted" style={{ fontSize: 12, marginTop: 8 }}>
            Variables detected: {extractVars(fSubject, fBody).join(", ") || "none"}
          </div>
          <div style={{ marginTop: 12 }}>
            <button type="button" className="btn btn-primary" onClick={create} disabled={saving || !fName.trim() || !fBody.trim()}>
              {saving ? "Saving…" : "Create template"}
            </button>
          </div>
        </section>
      )}

      <div className="brain-search-row" style={{ padding: "0 0 12px" }}>
        <input
          className="brain-search"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder="Search templates…"
          aria-label="Search templates"
        />
        <select
          className="input"
          value={channel}
          onChange={(e) => setChannel(e.target.value as Channel | "all")}
          aria-label="Filter by channel"
          style={{ width: 130 }}
        >
          <option value="all">All channels</option>
          <option value="email">Email</option>
          <option value="sms">SMS</option>
          <option value="social">Social</option>
        </select>
      </div>

      <div className="brain-chips" style={{ padding: "0 0 16px" }}>
        {niches.map((n) => (
          <button
            key={n}
            type="button"
            className={`chip${niche === n ? " chip-active" : ""}`}
            onClick={() => setNiche(n)}
          >
            {n}
            <span className="muted">
              {n === "All" ? templates.length : templates.filter((t) => nicheOf(t) === n).length}
            </span>
          </button>
        ))}
      </div>

      {filtered.length === 0 ? (
        <div className="panel">
          <p className="muted">No templates match these filters.</p>
        </div>
      ) : (
        <div
          style={{
            display: "grid",
            gridTemplateColumns: "repeat(auto-fill, minmax(300px, 1fr))",
            gap: 16,
          }}
        >
          {filtered.map((t, i) => (
            <div key={t.id} style={{ animationDelay: `${Math.min(i * 0.03, 0.6)}s` }}>
              <TemplateCard
                template={t}
                onPreview={() => setPreviewing(t)}
                onDelete={() => remove(t.id)}
              />
            </div>
          ))}
        </div>
      )}

      {previewing && (
        <PreviewModal template={previewing} onClose={() => setPreviewing(null)} />
      )}
    </div>
  );
}
