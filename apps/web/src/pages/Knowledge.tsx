import { useCallback, useEffect, useState } from "react";
import { knowledgeApi } from "../lib/api";
import type { KnowledgeDoc, KnowledgeDocInput } from "../lib/api";
import {
  EmptyState,
  ErrorBanner,
  Field,
  Modal,
  PageHeader,
  Spinner,
} from "../components/ui";

/**
 * Knowledge base: business context documents (company facts, FAQs,
 * policies, product notes). The store + UI; wiring documents into
 * Draven's prompt context is a later step.
 */
export default function KnowledgePage() {
  const [docs, setDocs] = useState<KnowledgeDoc[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<unknown>(null);
  const [query, setQuery] = useState("");
  const [showForm, setShowForm] = useState(false);
  const [editing, setEditing] = useState<KnowledgeDoc | null>(null);
  const [title, setTitle] = useState("");
  const [content, setContent] = useState("");
  const [source, setSource] = useState("");
  const [saving, setSaving] = useState(false);
  const [formError, setFormError] = useState<unknown>(null);
  const [expanded, setExpanded] = useState<string | null>(null);

  const load = useCallback(async (q?: string) => {
    setLoading(true);
    setError(null);
    try {
      setDocs(await knowledgeApi.list(q ? { q } : undefined));
    } catch (err) {
      setError(err);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const onSearch = () => {
    void load(query.trim() || undefined);
  };

  const openCreate = () => {
    setEditing(null);
    setTitle("");
    setContent("");
    setSource("");
    setFormError(null);
    setShowForm(true);
  };

  const openEdit = (d: KnowledgeDoc) => {
    setEditing(d);
    setTitle(d.title);
    setContent(d.content);
    setSource(d.source ?? "");
    setFormError(null);
    setShowForm(true);
  };

  const save = async () => {
    if (!title.trim()) return;
    setSaving(true);
    setFormError(null);
    const input: KnowledgeDocInput = {
      title: title.trim(),
      content,
      source: source.trim() || null,
    };
    try {
      if (editing) {
        await knowledgeApi.update(editing.id, input);
      } else {
        await knowledgeApi.create(input);
      }
      setShowForm(false);
      await load(query.trim() || undefined);
    } catch (err) {
      setFormError(err);
    } finally {
      setSaving(false);
    }
  };

  const remove = async (d: KnowledgeDoc) => {
    if (!window.confirm(`Delete "${d.title}"?`)) return;
    try {
      await knowledgeApi.remove(d.id);
      await load(query.trim() || undefined);
    } catch (err) {
      setError(err);
    }
  };

  if (loading) return <Spinner label="Loading knowledge base…" />;

  return (
    <div>
      <PageHeader
        title="Knowledge"
        subtitle="Business context the AI can draw on — facts, FAQs, policies, product notes."
        actions={
          <button type="button" className="btn btn-primary" onClick={openCreate}>
            + Document
          </button>
        }
      />
      {error ? <ErrorBanner error={error} /> : null}

      <div style={{ display: "flex", gap: 8, marginBottom: 12 }}>
        <input
          className="input"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") onSearch();
          }}
          placeholder="Search documents…"
          style={{ maxWidth: 320 }}
        />
        <button type="button" className="btn btn-ghost" onClick={onSearch}>
          Search
        </button>
      </div>

      {docs.length === 0 ? (
        <EmptyState
          title="Knowledge base is empty"
          hint="Add documents about your business — Draven will use them to answer better."
          action={
            <button type="button" className="btn btn-primary" onClick={openCreate}>
              + Document
            </button>
          }
        />
      ) : (
        <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
          {docs.map((d) => {
            const isOpen = expanded === d.id;
            return (
              <div key={d.id} className="card" style={{ padding: 12 }}>
                <div
                  style={{
                    display: "flex",
                    justifyContent: "space-between",
                    gap: 8,
                    cursor: "pointer",
                  }}
                  onClick={() => setExpanded(isOpen ? null : d.id)}
                  role="button"
                  tabIndex={0}
                  onKeyDown={(e) => {
                    if (e.key === "Enter") setExpanded(isOpen ? null : d.id);
                  }}
                >
                  <div>
                    <strong>{d.title}</strong>
                    <div className="muted" style={{ fontSize: 12, marginTop: 2 }}>
                      {d.source ? `${d.source} · ` : ""}
                      {d.content.length} chars
                    </div>
                  </div>
                  <div style={{ display: "flex", gap: 4, alignItems: "flex-start" }}>
                    <button
                      type="button"
                      className="btn btn-ghost btn-sm"
                      onClick={(e) => {
                        e.stopPropagation();
                        openEdit(d);
                      }}
                    >
                      Edit
                    </button>
                    <button
                      type="button"
                      className="btn btn-ghost btn-sm"
                      aria-label={`Delete ${d.title}`}
                      onClick={(e) => {
                        e.stopPropagation();
                        void remove(d);
                      }}
                    >
                      ✕
                    </button>
                  </div>
                </div>
                {isOpen && (
                  <pre
                    style={{
                      marginTop: 8,
                      whiteSpace: "pre-wrap",
                      fontSize: 13,
                      fontFamily: "inherit",
                    }}
                  >
                    {d.content}
                  </pre>
                )}
              </div>
            );
          })}
        </div>
      )}

      {showForm && (
        <Modal
          title={editing ? "Edit document" : "New document"}
          onClose={() => setShowForm(false)}
          wide
        >
          {formError ? <ErrorBanner error={formError} /> : null}
          <Field label="Title">
            <input
              className="input"
              value={title}
              onChange={(e) => setTitle(e.target.value)}
              placeholder="Refund policy"
            />
          </Field>
          <Field label="Source (optional)">
            <input
              className="input"
              value={source}
              onChange={(e) => setSource(e.target.value)}
              placeholder="handbook, website, founder…"
            />
          </Field>
          <Field label="Content">
            <textarea
              className="input"
              rows={10}
              value={content}
              onChange={(e) => setContent(e.target.value)}
              placeholder="Write the facts, FAQs, or policies Draven should know…"
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
              disabled={saving || !title.trim()}
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
