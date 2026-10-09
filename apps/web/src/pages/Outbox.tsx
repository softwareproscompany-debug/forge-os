import { useCallback, useEffect, useMemo, useState } from "react";
import { outboxApi } from "../lib/api";
import type { OutboxMessage } from "../lib/api";
import { formatDateTime } from "../lib/format";
import {
  Badge,
  EmptyState,
  ErrorBanner,
  Modal,
  Spinner,
} from "../components/ui";

export default function OutboxPage() {
  const [messages, setMessages] = useState<OutboxMessage[]>([]);
  const [channelFilter, setChannelFilter] = useState("");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<unknown>(null);
  const [viewing, setViewing] = useState<OutboxMessage | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      setMessages(await outboxApi.list(100));
    } catch (err) {
      setError(err);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const channels = useMemo(
    () => Array.from(new Set(messages.map((m) => m.channel))).sort(),
    [messages],
  );

  const filtered = channelFilter
    ? messages.filter((m) => m.channel === channelFilter)
    : messages;

  if (loading) return <Spinner label="Loading outbox…" />;

  return (
    <div>
      <div className="page-head">
        <div>
          <h1>Dev outbox</h1>
          <p className="muted" style={{ marginTop: 4 }}>
            Messages “sent” by the stub channel providers — nothing leaves this
            machine while <code>CHANNEL_MODE=stub</code>.
          </p>
        </div>
        <label className="filter">
          Channel
          <select
            value={channelFilter}
            onChange={(e) => setChannelFilter(e.target.value)}
          >
            <option value="">All channels</option>
            {channels.map((c) => (
              <option key={c} value={c}>
                {c}
              </option>
            ))}
          </select>
        </label>
      </div>

      <ErrorBanner error={error} onRetry={load} />

      {filtered.length === 0 ? (
        <div className="card">
          <EmptyState
            title="Outbox is empty"
            hint="Run a campaign with the stub provider and sent messages will land here."
          />
        </div>
      ) : (
        <div className="card">
          <div className="table-wrap">
            <table className="table">
              <thead>
                <tr>
                  <th>When</th>
                  <th>Channel</th>
                  <th>To</th>
                  <th>Subject</th>
                  <th>Provider</th>
                  <th></th>
                </tr>
              </thead>
              <tbody>
                {filtered.map((m) => (
                  <tr key={m.id}>
                    <td className="nowrap">{formatDateTime(m.created_at)}</td>
                    <td>
                      <Badge value={m.channel} />
                    </td>
                    <td>{m.to_address}</td>
                    <td>{m.subject ?? <span className="muted">—</span>}</td>
                    <td>
                      <span className="muted">{m.provider}</span>
                    </td>
                    <td>
                      <button
                        type="button"
                        className="btn btn-sm"
                        onClick={() => setViewing(m)}
                      >
                        View
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {viewing && (
        <Modal
          title={`Outbox message — ${viewing.channel}`}
          onClose={() => setViewing(null)}
          wide
        >
          <dl className="details">
            <dt>To</dt>
            <dd>{viewing.to_address}</dd>
            {viewing.subject && (
              <>
                <dt>Subject</dt>
                <dd>{viewing.subject}</dd>
              </>
            )}
            <dt>Provider</dt>
            <dd>{viewing.provider}</dd>
            <dt>Sent at</dt>
            <dd>{formatDateTime(viewing.created_at)}</dd>
          </dl>
          <h3 style={{ marginTop: 16 }}>Body</h3>
          <pre className="asset-body">{viewing.body}</pre>
        </Modal>
      )}
    </div>
  );
}
