import { useCallback, useEffect, useState } from "react";
import type { FormEvent } from "react";
import { Link } from "react-router-dom";
import { campaignApi } from "../lib/api";
import type { Campaign } from "../lib/api";
import { formatDate } from "../lib/format";
import {
  Badge,
  EmptyState,
  ErrorBanner,
  Field,
  Modal,
  Spinner,
  errorMessage,
} from "../components/ui";

export default function CampaignsPage() {
  const [campaigns, setCampaigns] = useState<Campaign[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<unknown>(null);
  const [showCreate, setShowCreate] = useState(false);

  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [createError, setCreateError] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      setCampaigns(await campaignApi.list());
    } catch (err) {
      setError(err);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const onCreate = async (e: FormEvent) => {
    e.preventDefault();
    setCreateError(null);
    setCreating(true);
    try {
      await campaignApi.create({
        name: name.trim(),
        description: description.trim() || undefined,
      });
      setShowCreate(false);
      setName("");
      setDescription("");
      await load();
    } catch (err) {
      setCreateError(errorMessage(err));
    } finally {
      setCreating(false);
    }
  };

  if (loading) return <Spinner label="Loading campaigns…" />;

  return (
    <div>
      <div className="page-head">
        <h1>Campaigns</h1>
        <button
          type="button"
          className="btn btn-primary"
          onClick={() => setShowCreate(true)}
        >
          New campaign
        </button>
      </div>

      <ErrorBanner error={error} onRetry={load} />

      {campaigns.length === 0 ? (
        <div className="card">
          <EmptyState
            title="No campaigns yet"
            hint="Campaigns are multi-step, multi-channel sequences. Create one and add steps in the builder."
            action={
              <button
                type="button"
                className="btn btn-primary"
                onClick={() => setShowCreate(true)}
              >
                Create campaign
              </button>
            }
          />
        </div>
      ) : (
        <div className="card-grid">
          {campaigns.map((c) => (
            <Link key={c.id} to={`/campaigns/${c.id}`} className="card card-link">
              <div className="card-head">
                <h2>{c.name}</h2>
                <Badge value={c.status} />
              </div>
              {c.description && <p className="muted clamp-2">{c.description}</p>}
              <div className="card-meta">
                <span>Starts {formatDate(c.starts_at)}</span>
                {c.autopilot && <span className="badge badge-purple">autopilot</span>}
              </div>
            </Link>
          ))}
        </div>
      )}

      {showCreate && (
        <Modal title="New campaign" onClose={() => setShowCreate(false)}>
          <form onSubmit={onCreate} className="form">
            {createError && (
              <div className="alert alert-error" role="alert">
                {createError}
              </div>
            )}
            <Field label="Name">
              <input
                required
                autoFocus
                value={name}
                onChange={(e) => setName(e.target.value)}
                placeholder="Q4 onboarding drip"
              />
            </Field>
            <Field label="Description">
              <textarea
                rows={3}
                value={description}
                onChange={(e) => setDescription(e.target.value)}
                placeholder="What this campaign is for…"
              />
            </Field>
            <div className="form-actions">
              <span className="spacer" />
              <button
                type="button"
                className="btn"
                onClick={() => setShowCreate(false)}
              >
                Cancel
              </button>
              <button type="submit" className="btn btn-primary" disabled={creating}>
                {creating ? "Creating…" : "Create"}
              </button>
            </div>
          </form>
        </Modal>
      )}
    </div>
  );
}
