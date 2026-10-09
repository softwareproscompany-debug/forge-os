import { useCallback, useEffect, useRef, useState } from "react";
import type { FormEvent } from "react";
import { assetApi, templateApi } from "../lib/api";
import type {
  Asset,
  AssetKind,
  AssetStatus,
  AssetVersion,
  Template,
} from "../lib/api";
import { formatDateTime } from "../lib/format";
import {
  Badge,
  EmptyState,
  ErrorBanner,
  Field,
  Modal,
  Spinner,
  errorMessage,
} from "../components/ui";

const KINDS: AssetKind[] = [
  "email_copy",
  "social_post",
  "sms",
  "blog",
  "ad",
  "image_prompt",
];
const STATUSES: AssetStatus[] = ["draft", "in_review", "approved", "rejected"];

function parseVariables(text: string): Record<string, string> {
  const out: Record<string, string> = {};
  for (const line of text.split("\n")) {
    const idx = line.indexOf("=");
    if (idx <= 0) continue;
    const key = line.slice(0, idx).trim();
    const value = line.slice(idx + 1).trim();
    if (key) out[key] = value;
  }
  return out;
}

export default function AssetsPage() {
  const [assets, setAssets] = useState<Asset[]>([]);
  const [kindFilter, setKindFilter] = useState<"" | AssetKind>("");
  const [statusFilter, setStatusFilter] = useState<"" | AssetStatus>("");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<unknown>(null);

  const [detail, setDetail] = useState<Asset | null>(null);
  const [versions, setVersions] = useState<AssetVersion[]>([]);
  const [versionsLoading, setVersionsLoading] = useState(false);
  const [detailError, setDetailError] = useState<string | null>(null);

  const [showGenerate, setShowGenerate] = useState(false);
  const [templates, setTemplates] = useState<Template[]>([]);
  const [genKind, setGenKind] = useState<AssetKind>("email_copy");
  const [genTitle, setGenTitle] = useState("");
  const [genTemplate, setGenTemplate] = useState("");
  const [genPrompt, setGenPrompt] = useState("");
  const [genVars, setGenVars] = useState("");
  const [genBusy, setGenBusy] = useState(false);
  const [genError, setGenError] = useState<string | null>(null);
  const [queued, setQueued] = useState<{
    assetId: string;
    jobId: string;
    status: string;
  } | null>(null);
  const pollTimer = useRef<number | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      setAssets(
        await assetApi.list({
          kind: kindFilter || undefined,
          status: statusFilter || undefined,
          limit: 100,
        }),
      );
    } catch (err) {
      setError(err);
    } finally {
      setLoading(false);
    }
  }, [kindFilter, statusFilter]);

  useEffect(() => {
    void load();
  }, [load]);

  useEffect(() => {
    return () => {
      if (pollTimer.current) window.clearInterval(pollTimer.current);
    };
  }, []);

  const openDetail = async (asset: Asset) => {
    setDetail(asset);
    setDetailError(null);
    setVersions([]);
    setVersionsLoading(true);
    try {
      setVersions(await assetApi.versions(asset.id));
    } catch {
      setVersions([]);
    } finally {
      setVersionsLoading(false);
    }
  };

  const submitForReview = async (asset: Asset) => {
    setDetailError(null);
    try {
      const updated = await assetApi.submit(asset.id);
      setDetail(updated);
      await load();
    } catch (err) {
      setDetailError(errorMessage(err));
    }
  };

  const openGenerate = async () => {
    setShowGenerate(true);
    setQueued(null);
    setGenError(null);
    try {
      setTemplates(await templateApi.list());
    } catch {
      setTemplates([]);
    }
  };

  const pollQueued = (assetId: string, jobId: string) => {
    let attempts = 0;
    pollTimer.current = window.setInterval(async () => {
      attempts += 1;
      try {
        const a = await assetApi.get(assetId);
        setQueued({ assetId, jobId, status: a.status });
        if (a.status !== "draft" || attempts >= 20) {
          if (pollTimer.current) window.clearInterval(pollTimer.current);
          pollTimer.current = null;
          void load();
        }
      } catch {
        if (attempts >= 20 && pollTimer.current) {
          window.clearInterval(pollTimer.current);
          pollTimer.current = null;
        }
      }
    }, 3000);
  };

  const onGenerate = async (e: FormEvent) => {
    e.preventDefault();
    setGenError(null);
    setGenBusy(true);
    try {
      const res = await assetApi.generate({
        kind: genKind,
        title: genTitle.trim(),
        template_id: genTemplate || undefined,
        prompt: genPrompt.trim() || undefined,
        variables: parseVariables(genVars),
      });
      setQueued({ assetId: res.asset_id, jobId: res.job_id, status: "queued" });
      pollQueued(res.asset_id, res.job_id);
    } catch (err) {
      setGenError(errorMessage(err));
    } finally {
      setGenBusy(false);
    }
  };

  if (loading) return <Spinner label="Loading assets…" />;

  return (
    <div>
      <div className="page-head">
        <h1>Assets</h1>
        <button type="button" className="btn btn-primary" onClick={openGenerate}>
          Generate
        </button>
      </div>

      <div className="filters">
        <label className="filter">
          Kind
          <select
            value={kindFilter}
            onChange={(e) => setKindFilter(e.target.value as "" | AssetKind)}
          >
            <option value="">All kinds</option>
            {KINDS.map((k) => (
              <option key={k} value={k}>
                {k.replace(/_/g, " ")}
              </option>
            ))}
          </select>
        </label>
        <label className="filter">
          Status
          <select
            value={statusFilter}
            onChange={(e) => setStatusFilter(e.target.value as "" | AssetStatus)}
          >
            <option value="">All statuses</option>
            {STATUSES.map((s) => (
              <option key={s} value={s}>
                {s.replace(/_/g, " ")}
              </option>
            ))}
          </select>
        </label>
      </div>

      <ErrorBanner error={error} onRetry={load} />

      {assets.length === 0 ? (
        <div className="card">
          <EmptyState
            title="No assets match"
            hint="Generate your first asset with the button above."
            action={
              <button
                type="button"
                className="btn btn-primary"
                onClick={openGenerate}
              >
                Generate asset
              </button>
            }
          />
        </div>
      ) : (
        <div className="card-grid">
          {assets.map((a) => (
            <button
              key={a.id}
              type="button"
              className="card card-link card-btn"
              onClick={() => openDetail(a)}
            >
              <div className="card-head">
                <h2>{a.title}</h2>
                <Badge value={a.status} />
              </div>
              <div className="asset-preview">
                {a.body ? a.body.slice(0, 220) : <span className="muted">Queued for generation…</span>}
                {a.body && a.body.length > 220 && "…"}
              </div>
              <div className="card-meta">
                <Badge value={a.kind} />
                <span>v{a.version}</span>
                <span>{formatDateTime(a.created_at)}</span>
              </div>
            </button>
          ))}
        </div>
      )}

      {detail && (
        <Modal title={detail.title} onClose={() => setDetail(null)} wide>
          <div className="row-actions" style={{ marginBottom: 12 }}>
            <Badge value={detail.status} />
            <Badge value={detail.kind} />
            <span className="muted">v{detail.version}</span>
            {detail.status === "draft" && (
              <button
                type="button"
                className="btn btn-sm btn-primary"
                onClick={() => submitForReview(detail)}
              >
                Submit for review
              </button>
            )}
          </div>
          {detail.rejection_reason && (
            <div className="alert alert-error" style={{ marginBottom: 12 }}>
              <strong>Rejected:</strong> {detail.rejection_reason}
            </div>
          )}
          {detailError && (
            <div className="alert alert-error" style={{ marginBottom: 12 }}>
              {detailError}
            </div>
          )}
          <pre className="asset-body">{detail.body ?? "No content yet."}</pre>
          <h3 style={{ marginTop: 16 }}>Version history</h3>
          {versionsLoading ? (
            <Spinner label="Loading versions…" />
          ) : versions.length === 0 ? (
            <p className="muted">No version history returned.</p>
          ) : (
            <ul className="list">
              {versions.map((v) => (
                <li key={v.id} className="list-row">
                  <span>
                    <strong>v{v.version}</strong> — {v.title}
                  </span>
                  <span className="row-actions">
                    <Badge value={v.status} />
                    <span className="muted">{formatDateTime(v.created_at)}</span>
                  </span>
                </li>
              ))}
            </ul>
          )}
        </Modal>
      )}

      {showGenerate && (
        <Modal
          title="Generate asset"
          onClose={() => {
            setShowGenerate(false);
            setQueued(null);
          }}
          wide
        >
          {queued ? (
            <div className="queued-state">
              <div className="spinner" aria-hidden="true" />
              <h3>Generation queued</h3>
              <p className="muted">
                Asset <code>{queued.assetId}</code> · job{" "}
                <code>{queued.jobId}</code>
              </p>
              <p>
                Status: <Badge value={queued.status} />
              </p>
              <p className="muted">
                The worker renders the prompt, runs guardrails, and saves the
                result. This dialog polls for completion; you can also close it
                — the asset will appear in the library.
              </p>
              <div className="form-actions">
                <span className="spacer" />
                <button
                  type="button"
                  className="btn btn-primary"
                  onClick={() => {
                    setShowGenerate(false);
                    setQueued(null);
                    void load();
                  }}
                >
                  Done
                </button>
              </div>
            </div>
          ) : (
            <form onSubmit={onGenerate} className="form">
              {genError && (
                <div className="alert alert-error" role="alert">
                  {genError}
                </div>
              )}
              <div className="grid-2">
                <Field label="Kind">
                  <select
                    value={genKind}
                    onChange={(e) => setGenKind(e.target.value as AssetKind)}
                  >
                    {KINDS.map((k) => (
                      <option key={k} value={k}>
                        {k.replace(/_/g, " ")}
                      </option>
                    ))}
                  </select>
                </Field>
                <Field label="Title">
                  <input
                    required
                    value={genTitle}
                    onChange={(e) => setGenTitle(e.target.value)}
                    placeholder="Welcome email #1"
                  />
                </Field>
              </div>
              <Field label="Template (optional)">
                <select
                  value={genTemplate}
                  onChange={(e) => setGenTemplate(e.target.value)}
                >
                  <option value="">— none —</option>
                  {templates.map((t) => (
                    <option key={t.id} value={t.id}>
                      {t.name} ({t.channel})
                    </option>
                  ))}
                </select>
              </Field>
              <Field
                label="Prompt"
                hint="Extra instructions for the generator, on top of the template and brand kit."
              >
                <textarea
                  rows={3}
                  value={genPrompt}
                  onChange={(e) => setGenPrompt(e.target.value)}
                  placeholder="Announce the spring sale, emphasize free shipping…"
                />
              </Field>
              <Field
                label="Variables"
                hint="One per line as key=value, e.g. first_name=Ada"
              >
                <textarea
                  rows={3}
                  value={genVars}
                  onChange={(e) => setGenVars(e.target.value)}
                  placeholder={"first_name=Ada\nproduct=ForgeOS Pro"}
                  spellCheck={false}
                />
              </Field>
              <div className="form-actions">
                <span className="spacer" />
                <button
                  type="button"
                  className="btn"
                  onClick={() => setShowGenerate(false)}
                  disabled={genBusy}
                >
                  Cancel
                </button>
                <button
                  type="submit"
                  className="btn btn-primary"
                  disabled={genBusy}
                >
                  {genBusy ? "Queueing…" : "Generate"}
                </button>
              </div>
            </form>
          )}
        </Modal>
      )}
    </div>
  );
}
