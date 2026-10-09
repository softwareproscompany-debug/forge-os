import { useEffect } from "react";
import type { ReactNode } from "react";
import { ApiError } from "../lib/api";

/* ---------------------------------------------------------------- */
/* Primitives                                                        */
/* ---------------------------------------------------------------- */

export function Spinner({ label = "Loading…" }: { label?: string }) {
  return (
    <div className="spinner-wrap" role="status" aria-live="polite">
      <div className="spinner" />
      <span className="muted">{label}</span>
    </div>
  );
}

export function errorMessage(err: unknown): string {
  if (err instanceof ApiError) {
    if (err.status === 0) return String(err.detail);
    if (typeof err.detail === "string") return err.detail;
    return `Request failed (HTTP ${err.status})`;
  }
  if (err instanceof Error) return err.message;
  return "Something went wrong.";
}

export function ErrorBanner({
  error,
  onRetry,
}: {
  error: unknown;
  onRetry?: () => void;
}) {
  if (!error) return null;
  return (
    <div className="alert alert-error" role="alert">
      <div>
        <strong>Something went wrong.</strong>
        <div className="alert-detail">{errorMessage(error)}</div>
      </div>
      {onRetry && (
        <button type="button" className="btn btn-sm" onClick={onRetry}>
          Retry
        </button>
      )}
    </div>
  );
}

export function EmptyState({
  title,
  hint,
  action,
}: {
  title: string;
  hint?: string;
  action?: ReactNode;
}) {
  return (
    <div className="empty-state">
      <div className="empty-title">{title}</div>
      {hint && <div className="muted">{hint}</div>}
      {action && <div className="empty-action">{action}</div>}
    </div>
  );
}

/* ---------------------------------------------------------------- */
/* Status badges                                                     */
/* ---------------------------------------------------------------- */

const STATUS_CLASS: Record<string, string> = {
  draft: "badge-gray",
  in_review: "badge-amber",
  approved: "badge-green",
  rejected: "badge-red",
  scheduled: "badge-blue",
  running: "badge-green",
  paused: "badge-amber",
  completed: "badge-gray",
  queued: "badge-blue",
  sending: "badge-blue",
  sent: "badge-green",
  delivered: "badge-green",
  bounced: "badge-red",
  failed: "badge-red",
  active: "badge-green",
  unsubscribed: "badge-gray",
  email: "badge-blue",
  sms: "badge-purple",
  social: "badge-teal",
};

export function Badge({ value }: { value: string }) {
  const cls = STATUS_CLASS[value] ?? "badge-gray";
  const label = value.replace(/_/g, " ");
  return <span className={`badge ${cls}`}>{label}</span>;
}

/* ---------------------------------------------------------------- */
/* Modal                                                             */
/* ---------------------------------------------------------------- */

export function Modal({
  title,
  onClose,
  children,
  wide,
}: {
  title: string;
  onClose: () => void;
  children: ReactNode;
  wide?: boolean;
}) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    document.body.style.overflow = "hidden";
    return () => {
      window.removeEventListener("keydown", onKey);
      document.body.style.overflow = "";
    };
  }, [onClose]);

  return (
    <div
      className="modal-backdrop"
      onClick={onClose}
      role="presentation"
      aria-hidden="false"
    >
      <div
        className={`modal${wide ? " modal-wide" : ""}`}
        role="dialog"
        aria-modal="true"
        aria-label={title}
        onClick={(e) => e.stopPropagation()}
      >
        <div className="modal-header">
          <h2>{title}</h2>
          <button
            type="button"
            className="btn btn-icon"
            onClick={onClose}
            aria-label="Close"
          >
            ✕
          </button>
        </div>
        <div className="modal-body">{children}</div>
      </div>
    </div>
  );
}

/* ---------------------------------------------------------------- */
/* Form fields                                                       */
/* ---------------------------------------------------------------- */

export function Field({
  label,
  hint,
  children,
}: {
  label: string;
  hint?: string;
  children: ReactNode;
}) {
  return (
    <label className="field">
      <span className="field-label">{label}</span>
      {children}
      {hint && <span className="field-hint">{hint}</span>}
    </label>
  );
}
