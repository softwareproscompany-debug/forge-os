import { useCallback, useEffect, useState } from "react";
import { assetApi } from "../lib/api";
import type { Asset } from "../lib/api";
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

type Action = "approve" | "reject";

export default function ApprovalsPage() {
  const [assets, setAssets] = useState<Asset[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<unknown>(null);
  const [selected, setSelected] = useState<Asset | null>(null);
  const [action, setAction] = useState<Action>("approve");
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const [modalError, setModalError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      setAssets(await assetApi.list({ status: "in_review", limit: 100 }));
    } catch (err) {
      setError(err);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const openAction = (asset: Asset, a: Action) => {
    setSelected(asset);
    setAction(a);
    setNote("");
    setModalError(null);
  };

  const confirm = async () => {
    if (!selected) return;
    if (action === "reject" && note.trim().length === 0) {
      setModalError("A rejection reason is required.");
      return;
    }
    setBusy(true);
    setModalError(null);
    try {
      if (action === "approve") {
        await assetApi.approve(selected.id, note.trim() || undefined);
      } else {
        await assetApi.reject(selected.id, note.trim());
      }
      setSelected(null);
      await load();
    } catch (err) {
      setModalError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  };

  if (loading) return <Spinner label="Loading approvals…" />;

  return (
    <div>
      <div className="page-head">
        <h1>Approvals</h1>
        <span className="muted">
          {assets.length} awaiting review
        </span>
      </div>

      <ErrorBanner error={error} onRetry={load} />

      {assets.length === 0 ? (
        <div className="card">
          <EmptyState
            title="Inbox zero"
            hint="Generated assets land here when autopilot auto-approve is off."
          />
        </div>
      ) : (
        <div className="card-grid">
          {assets.map((a) => (
            <div key={a.id} className="card">
              <div className="card-head">
                <h2>{a.title}</h2>
                <Badge value={a.kind} />
              </div>
              <div className="asset-preview">
                {a.body ? a.body.slice(0, 320) : <span className="muted">No content yet — generation may still be running.</span>}
                {a.body && a.body.length > 320 && "…"}
              </div>
              <div className="card-meta">
                <span>v{a.version} · brand kit v{a.brand_kit_version}</span>
                <span>{formatDateTime(a.created_at)}</span>
              </div>
              <div className="row-actions" style={{ marginTop: 12 }}>
                <button
                  type="button"
                  className="btn btn-sm btn-primary"
                  onClick={() => openAction(a, "approve")}
                >
                  Approve
                </button>
                <button
                  type="button"
                  className="btn btn-sm btn-danger-outline"
                  onClick={() => openAction(a, "reject")}
                >
                  Reject
                </button>
              </div>
            </div>
          ))}
        </div>
      )}

      {selected && (
        <Modal
          title={action === "approve" ? "Approve asset" : "Reject asset"}
          onClose={() => setSelected(null)}
        >
          <div className="form">
            <p>
              <strong>{selected.title}</strong>{" "}
              <Badge value={selected.kind} />
            </p>
            {modalError && (
              <div className="alert alert-error" role="alert">
                {modalError}
              </div>
            )}
            <Field
              label={action === "approve" ? "Note (optional)" : "Rejection reason"}
              hint={
                action === "approve"
                  ? "Visible in the approval history."
                  : "The creator sees this — be specific."
              }
            >
              <textarea
                rows={3}
                autoFocus
                value={note}
                onChange={(e) => setNote(e.target.value)}
                placeholder={
                  action === "approve"
                    ? "Looks good — on brand."
                    : "Tone is off: too formal for our audience…"
                }
              />
            </Field>
            <div className="form-actions">
              <span className="spacer" />
              <button
                type="button"
                className="btn"
                onClick={() => setSelected(null)}
                disabled={busy}
              >
                Cancel
              </button>
              <button
                type="button"
                className={`btn ${action === "approve" ? "btn-primary" : "btn-danger"}`}
                onClick={confirm}
                disabled={busy}
              >
                {busy
                  ? "Saving…"
                  : action === "approve"
                    ? "Approve asset"
                    : "Reject asset"}
              </button>
            </div>
          </div>
        </Modal>
      )}
    </div>
  );
}
