import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { integrationsApi } from "../lib/api";
import type { IntegrationStatus } from "../lib/api";
import {
  EmptyState,
  ErrorBanner,
  PageHeader,
  Spinner,
} from "../components/ui";

/**
 * Integrations overview: one card per connected service, driven by the
 * existing status endpoints (settings vault sections + Draven provider
 * status). No new backend — read-only aggregation.
 */
export default function IntegrationsPage() {
  const [items, setItems] = useState<IntegrationStatus[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<unknown>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      setItems(await integrationsApi.status());
    } catch (err) {
      setError(err);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  if (loading) return <Spinner label="Loading integrations…" />;

  return (
    <div>
      <PageHeader
        title="Integrations"
        subtitle="Connected services and API keys. Manage credentials in Settings."
        actions={
          <Link to="/settings" className="btn btn-ghost">
            Open Settings
          </Link>
        }
      />
      {error ? <ErrorBanner error={error} /> : null}

      {items.length === 0 ? (
        <EmptyState
          title="No integrations found"
          hint="Connect your first service in Settings."
          action={
            <Link to="/settings" className="btn btn-primary">
              Open Settings
            </Link>
          }
        />
      ) : (
        <div
          style={{
            display: "grid",
            gridTemplateColumns: "repeat(auto-fit, minmax(260px, 1fr))",
            gap: 12,
          }}
        >
          {items.map((it) => (
            <div key={it.key} className="card" style={{ padding: 14 }}>
              <div
                style={{
                  display: "flex",
                  justifyContent: "space-between",
                  alignItems: "center",
                  gap: 8,
                }}
              >
                <strong>{it.label}</strong>
                <span
                  className={`pill${it.connected ? "" : " down"}`}
                  style={{ fontSize: 11 }}
                >
                  <span className="dot" />
                  {it.connected ? "Connected" : "Not connected"}
                </span>
              </div>
              {it.detail && (
                <div className="muted" style={{ fontSize: 12, marginTop: 6 }}>
                  {it.detail}
                </div>
              )}
              <div style={{ marginTop: 10 }}>
                <Link to={it.settingsPath} className="btn btn-ghost btn-sm">
                  {it.connected ? "Manage" : "Connect"} →
                </Link>
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
