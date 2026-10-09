import { useEffect, useRef, useState } from "react";
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

/* ---------------------------------------------------------------- */
/* Design-system components (shared futuristic primitives)           */
/* ---------------------------------------------------------------- */

/** Consistent page header: title, optional subtitle, action buttons. */
export function PageHeader({
  title,
  subtitle,
  actions,
}: {
  title: string;
  subtitle?: string;
  actions?: ReactNode;
}) {
  return (
    <div className="page-header">
      <div>
        <h1>{title}</h1>
        {subtitle && <div className="page-header-sub">{subtitle}</div>}
      </div>
      {actions && <div className="page-header-actions">{actions}</div>}
    </div>
  );
}

/** Smoothly animates toward `target` whenever it changes. */
export function useCountUp(target: number, durationMs = 700): number {
  const [value, setValue] = useState(target);
  const fromRef = useRef(target);
  const rafRef = useRef<number>(0);

  useEffect(() => {
    const from = fromRef.current;
    if (from === target) return;
    const start = performance.now();
    const tick = (now: number) => {
      const t = Math.min(1, (now - start) / durationMs);
      const eased = 1 - Math.pow(1 - t, 3);
      const current = from + (target - from) * eased;
      setValue(current);
      fromRef.current = current;
      if (t < 1) rafRef.current = requestAnimationFrame(tick);
    };
    rafRef.current = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(rafRef.current);
  }, [target, durationMs]);

  return value;
}

/** KPI card with an animated numeric value. */
export function StatCard({
  label,
  value,
  hint,
  tone,
  format = (n: number) => Math.round(n).toLocaleString("en-US"),
}: {
  label: string;
  value: number;
  hint?: string;
  /** Accent color for the top edge: "ember" (default) or "cyan". */
  tone?: "ember" | "cyan";
  format?: (n: number) => string;
}) {
  const animated = useCountUp(value);
  return (
    <div
      className="card kpi stat-card"
      style={
        tone === "cyan" ? ({ "--stat-accent": "#22d3ee" } as React.CSSProperties) : undefined
      }
    >
      <div className="kpi-label">{label}</div>
      <div className="kpi-value">{format(animated)}</div>
      {hint && <div className="muted" style={{ fontSize: 12 }}>{hint}</div>}
    </div>
  );
}

/** Pulsing "system live" indicator. */
export function LiveDot({ label = "System live" }: { label?: string }) {
  return (
    <span className="live-dot" role="status" aria-label={label}>
      <span className="dot-pulse" aria-hidden="true" />
      {label}
    </span>
  );
}

/** Per-panel data timestamp (Card 5 dashboard rule: show, don't store). */
export function DataStamp({ at }: { at: string | null | undefined }) {
  if (!at) return null;
  const d = new Date(at);
  const label = Number.isNaN(d.getTime())
    ? at
    : d.toLocaleTimeString("en-US", { hour: "numeric", minute: "2-digit", second: "2-digit" });
  return (
    <span className="data-stamp" title={at}>
      as of {label}
    </span>
  );
}
