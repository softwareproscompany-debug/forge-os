import { useEffect, useState } from "react";
import { NavLink, Outlet, useNavigate } from "react-router-dom";
import { useAuth } from "../lib/auth";
import { businessApi } from "../lib/api";
import type { Business } from "../lib/api";
import { Spinner } from "./ui";

const NAV = [
  { to: "/", label: "Dashboard", icon: "◈", end: true },
  { to: "/ops", label: "Mission Control", icon: "⬢" },
  { to: "/brain", label: "Brain", icon: "◉" },
  { to: "/campaigns", label: "Campaigns", icon: "✉" },
  { to: "/autopilot", label: "Autopilot", icon: "✦" },
  { to: "/calendar", label: "Calendar", icon: "▦" },
  { to: "/approvals", label: "Approvals", icon: "✓" },
  { to: "/assets", label: "Assets", icon: "◫" },
  { to: "/analytics", label: "Analytics", icon: "◔" },
  { to: "/affiliates", label: "Affiliates", icon: "◆" },
  { to: "/outbox", label: "Outbox", icon: "⎋" },
  { to: "/onboarding", label: "Onboarding", icon: "⚙" },
  { to: "/interview", label: "Interview", icon: "❝" },
  { to: "/assistant", label: "Forge Voice", icon: "◍" },
] as const;

/** Redirects to /login when there is no authenticated user. */
export function RequireAuth({ children }: { children: React.ReactNode }) {
  const { user, loading } = useAuth();
  const navigate = useNavigate();

  useEffect(() => {
    if (!loading && !user) navigate("/login", { replace: true });
  }, [loading, user, navigate]);

  if (loading) {
    return (
      <div className="auth-loading">
        <Spinner label="Checking session…" />
      </div>
    );
  }
  if (!user) return null;
  return <>{children}</>;
}

/** Live clock for the topbar, ticking every 30s. */
function useClock() {
  const [now, setNow] = useState(() => new Date());
  useEffect(() => {
    const t = window.setInterval(() => setNow(new Date()), 30000);
    return () => window.clearInterval(t);
  }, []);
  return now;
}

export function Layout() {
  const { user, logout } = useAuth();
  const navigate = useNavigate();
  const [business, setBusiness] = useState<Business | null>(null);
  const [navOpen, setNavOpen] = useState(false);
  const [apiOk, setApiOk] = useState(true);
  const now = useClock();

  useEffect(() => {
    let cancelled = false;
    businessApi
      .getMe()
      .then((b) => {
        if (!cancelled) {
          setBusiness(b);
          setApiOk(true);
        }
      })
      .catch(() => {
        if (!cancelled) setApiOk(false);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const handleLogout = () => {
    logout();
    navigate("/login", { replace: true });
  };

  return (
    <div className="app-shell">
      {navOpen && (
        <button
          type="button"
          className="nav-scrim"
          aria-label="Close navigation"
          onClick={() => setNavOpen(false)}
        />
      )}
      <aside className={`sidebar${navOpen ? " open" : ""}`}>
        <div className="brand">
          <div className="brand-mark">F</div>
          <div className="brand-name">ForgeOS</div>
        </div>
        <nav className="nav" aria-label="Primary">
          {NAV.map((item) => (
            <NavLink
              key={item.to}
              to={item.to}
              end={"end" in item && item.end}
              onClick={() => setNavOpen(false)}
              className={({ isActive }) =>
                `nav-link${isActive ? " nav-link-active" : ""}`
              }
            >
              <span className="nav-icon" aria-hidden="true">
                {item.icon}
              </span>
              {item.label}
            </NavLink>
          ))}
        </nav>
        <div className="sidebar-foot">
          <button type="button" className="btn btn-ghost" onClick={handleLogout}>
            Sign out
          </button>
        </div>
      </aside>

      <div className="main-col">
        <header className="topbar">
          <button
            type="button"
            className="nav-toggle"
            aria-label="Open navigation"
            aria-expanded={navOpen}
            onClick={() => setNavOpen((v) => !v)}
          >
            <span aria-hidden="true">☰</span>
          </button>
          <div className="topbar-brand">
            <span className="hex-mark" aria-hidden="true">F</span>
            <span className="topbar-title">
              FORGE<span className="os-word">Marketing OS</span>
            </span>
          </div>
          <div className="status-pills" aria-label="System status">
            <span className={`pill${apiOk ? "" : " down"}`}>
              <span className="dot" />
              API
            </span>
            <span className="pill">
              <span className="dot" />
              Worker
            </span>
            <span className="pill">
              <span className="dot" />
              Sync
            </span>
          </div>
          <div className="topbar-right">
            <span className="topbar-clock">
              {now.toLocaleDateString("en-US", {
                weekday: "short",
                month: "short",
                day: "numeric",
              })}{" "}
              {now.toLocaleTimeString("en-US", {
                hour: "2-digit",
                minute: "2-digit",
                hour12: false,
              })}
            </span>
            <div className="topbar-user">
              <span className="topbar-user-name">
                {business ? business.name : user?.full_name || user?.email}
              </span>
              {user && <span className="badge badge-gray">{user.role}</span>}
            </div>
          </div>
        </header>
        <main className="page">
          <Outlet />
        </main>
      </div>
    </div>
  );
}
