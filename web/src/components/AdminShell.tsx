/**
 * Internal admin portal shell — a distinct sidebar from the customer app, but the
 * same design system. Reached only by allow-listed admins (RequireAdmin). Future
 * sections are navigation placeholders only, per the brief.
 */
import { Link, NavLink, Outlet, useNavigate } from "react-router-dom";
import { useAuth } from "../auth/AuthContext";
import { BrandMark } from "./BrandMark";
import { ThemeToggle } from "./ThemeToggle";

const NAV = [
  { to: "/admin", label: "Dashboard", end: true },
  { to: "/admin/customers", label: "Customers", end: false },
  { to: "/admin/connectors", label: "Connectors", end: false },
  { to: "/admin/sync-history", label: "Sync history", end: false },
  { to: "/admin/errors", label: "Errors", end: false },
];

// Navigation placeholders only — no implementation yet (per the brief).
const PLACEHOLDERS = ["Organizations", "Accounts", "Audit logs", "Billing", "Feature flags"];

export function AdminShell() {
  const { user, logout } = useAuth();
  const navigate = useNavigate();

  return (
    <div className="app-shell">
      <aside className="sidebar admin-sidebar">
        <div className="sidebar-brand">
          <span className="brand">
            <BrandMark />
            Meter
          </span>
          <span className="admin-tag">Admin</span>
        </div>
        <nav className="sidebar-nav">
          {NAV.map((item) => (
            <NavLink
              key={item.to}
              to={item.to}
              end={item.end}
              className={({ isActive }) => (isActive ? "nav-link active" : "nav-link")}
            >
              {item.label}
            </NavLink>
          ))}
          <div className="admin-nav-section muted">Coming soon</div>
          {PLACEHOLDERS.map((label) => (
            <span key={label} className="nav-link nav-link-disabled" aria-disabled="true">
              {label}
            </span>
          ))}
        </nav>
        <div className="sidebar-foot">
          <Link to="/" className="link">
            ← Customer app
          </Link>
          <span className="sidebar-email muted">{user?.email}</span>
          <div className="sidebar-actions">
            <button className="link" onClick={() => logout().then(() => navigate("/login"))}>
              Sign out
            </button>
            <ThemeToggle />
          </div>
        </div>
      </aside>
      <main className="app-main">
        <Outlet />
      </main>
    </div>
  );
}
