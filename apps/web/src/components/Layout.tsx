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

export function Layout() {
  const { user, logout } = useAuth();
  const navigate = useNavigate();
  const [business, setBusiness] = useState<Business | null>(null);

  useEffect(() => {
    let cancelled = false;
    businessApi
      .getMe()
      .then((b) => {
        if (!cancelled) setBusiness(b);
      })
      .catch(() => {
        /* business name is decorative; a failure must not break layout */
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
      <aside className="sidebar">
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
          <div className="topbar-business">
            {business ? business.name : <span className="muted">…</span>}
          </div>
          <div className="topbar-user">
            <span className="topbar-user-name">
              {user?.full_name || user?.email}
            </span>
            {user && <span className="badge badge-gray">{user.role}</span>}
          </div>
        </header>
        <main className="page">
          <Outlet />
        </main>
      </div>
    </div>
  );
}
