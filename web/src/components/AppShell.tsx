/**
 * Full-screen app shell: a fixed left sidebar (brand, nav, account) wrapping the
 * routed page content. Replaces the per-page top bars.
 *
 * When an allow-listed admin is impersonating a customer, a banner appears and the
 * entire customer UI runs in that tenant (context switched server-side) — no pages
 * are duplicated for the admin.
 */
import { useCallback, useEffect, useState } from "react";
import { Link, NavLink, Outlet, useLocation, useNavigate } from "react-router-dom";
import { api } from "../api";
import { useAuth } from "../auth/AuthContext";
import { ThemeToggle } from "./ThemeToggle";
import { REFRESH_ALERTS_EVENT } from "../pages/Dashboard";
import { Assistant } from "./Assistant";
import { BrandMark } from "./BrandMark";

type IconName =
  | "overview"
  | "sources"
  | "applications"
  | "features"
  | "traces"
  | "optimize"
  | "alerts"
  | "sdk"
  | "settings"
  | "help"
  | "prompts"
  | "reconciliation";

interface NavItem {
  to: string;
  label: string;
  end: boolean;
  icon: IconName;
}

/** The nav in sections, in the order the product is actually used: read the
 *  numbers, see where they come from, act on them, then set up and look things
 *  up. The first section has no heading — Overview is the front door of the
 *  product, not a member of a group. */
const NAV: { section?: string; items: NavItem[] }[] = [
  { items: [{ to: "/", label: "Overview", end: true, icon: "overview" }] },
  {
    section: "Analyze",
    items: [
      { to: "/cost-sources", label: "Cost sources", end: false, icon: "sources" },
      { to: "/applications", label: "Applications", end: false, icon: "applications" },
      { to: "/features", label: "Features", end: false, icon: "features" },
      { to: "/traces", label: "Traces", end: false, icon: "traces" },
    ],
  },
  {
    section: "Optimize",
    items: [
      // `end` matters on the first one: without it /optimize keeps matching
      // while the reader is on Prompts, and two rows look active at once.
      { to: "/optimize", label: "Recommendations", end: true, icon: "optimize" },
      { to: "/optimize/prompts", label: "Prompts", end: false, icon: "prompts" },
    ],
  },
  {
    section: "Act",
    items: [{ to: "/alerts", label: "Alerts", end: false, icon: "alerts" }],
  },
  {
    section: "Set up",
    items: [
      { to: "/install-sdk", label: "Install SDK", end: false, icon: "sdk" },
      { to: "/settings", label: "Settings", end: false, icon: "settings" },
    ],
  },
  {
    section: "Learn",
    items: [{ to: "/help", label: "Knowledge base", end: false, icon: "help" }],
  },
];

/** One family of line icons, all drawn on the same 20x20 grid at the same
 *  weight, so the column reads as a set rather than as eight borrowed glyphs. */
function NavIcon({ name }: { name: IconName }) {
  return (
    <svg
      viewBox="0 0 20 20"
      width="17"
      height="17"
      aria-hidden
      className="nav-icon"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.5"
      strokeLinecap="round"
      strokeLinejoin="round"
    >
      {name === "overview" && (
        <>
          <rect x="2.75" y="2.75" width="6" height="6" rx="1.5" />
          <rect x="11.25" y="2.75" width="6" height="6" rx="1.5" />
          <rect x="2.75" y="11.25" width="6" height="6" rx="1.5" />
          <rect x="11.25" y="11.25" width="6" height="6" rx="1.5" />
        </>
      )}
      {name === "sources" && (
        <>
          <path d="M10 2.5 17.5 6.5 10 10.5 2.5 6.5Z" />
          <path d="M2.5 10 10 14l7.5-4" />
          <path d="M2.5 13.5 10 17.5l7.5-4" />
        </>
      )}
      {/* Applications: stacked surfaces, the layer above a feature. */}
      {name === "applications" && (
        <>
          <rect x="2.75" y="2.75" width="6" height="6" rx="1.5" />
          <rect x="11.25" y="2.75" width="6" height="6" rx="1.5" />
          <rect x="2.75" y="11.25" width="6" height="6" rx="1.5" />
          <rect x="11.25" y="11.25" width="6" height="6" rx="1.5" />
        </>
      )}
      {/* Traces: a waterfall of spans, which is what the page draws. */}
      {name === "traces" && (
        <>
          <path d="M3 5h9" />
          <path d="M6 10h8" />
          <path d="M9 15h5" />
        </>
      )}
      {name === "features" && (
        <>
          <path d="M9.4 2.75H16A1.25 1.25 0 0 1 17.25 4v6.6c0 .33-.13.65-.37.89l-5.4 5.4a1.25 1.25 0 0 1-1.76 0l-6.6-6.6a1.25 1.25 0 0 1 0-1.76l5.4-5.4c.23-.24.55-.38.88-.38Z" />
          <circle cx="13.35" cy="6.65" r="1.1" />
        </>
      )}
      {name === "optimize" && (
        <>
          <path d="M2.75 5.5 7.5 10.25 11 6.75l6.25 6.25" />
          <path d="M13 13h4.25V8.75" />
        </>
      )}
      {name === "prompts" && (
        <>
          <path d="M4.75 3.75h10.5a2 2 0 0 1 2 2v5.25a2 2 0 0 1-2 2H9.4l-3.65 2.9v-2.9H4.75a2 2 0 0 1-2-2V5.75a2 2 0 0 1 2-2Z" />
          <path d="M6.4 7.1h7.2M6.4 9.9h4.4" />
        </>
      )}
      {name === "alerts" && (
        <>
          <path d="M6 8.25a4 4 0 0 1 8 0c0 3.4 1.1 4.4 1.45 4.9a.4.4 0 0 1-.33.6H4.88a.4.4 0 0 1-.33-.6c.35-.5 1.45-1.5 1.45-4.9Z" />
          <path d="M8.6 16.1a1.9 1.9 0 0 0 2.8 0" />
        </>
      )}
      {name === "sdk" && (
        <>
          <path d="M7.25 6.5 3.75 10l3.5 3.5" />
          <path d="M12.75 6.5 16.25 10l-3.5 3.5" />
        </>
      )}
      {name === "settings" && (
        <>
          <path d="M2.75 6.75h3.5M10.75 6.75h6.5" />
          <circle cx="8.5" cy="6.75" r="1.6" />
          <path d="M2.75 13.25h7.5M14.75 13.25h2.5" />
          <circle cx="12.5" cy="13.25" r="1.6" />
        </>
      )}
      {name === "reconciliation" && (
        <>
          <path d="M4.5 2.75h7.2l3.8 3.8v10.7a.75.75 0 0 1-.75.75H4.5a.75.75 0 0 1-.75-.75V3.5a.75.75 0 0 1 .75-.75Z" />
          <path d="M11.5 2.9v3.9h3.9" />
          <path d="m6.6 12.4 1.8 1.8 3.5-3.5" />
        </>
      )}
      {name === "help" && (
        <>
          <path d="M10 5.6S8.4 3.4 3 3.4v10.9c5.4 0 7 2.2 7 2.2s1.6-2.2 7-2.2V3.4c-5.4 0-7 2.2-7 2.2Z" />
          <path d="M10 5.6v10.9" />
        </>
      )}
    </svg>
  );
}

export function AppShell() {
  const { user, logout, refresh } = useAuth();
  const navigate = useNavigate();
  const location = useLocation();
  const [alertBadge, setAlertBadge] = useState(0);
  // Reconciliation is an opt-in module. This is the only thing the shell knows
  // about it: whether to offer it. The request is independent and its failure
  // is swallowed, so the module can never delay or break the navigation.
  const [reconciliation, setReconciliation] = useState(false);

  useEffect(() => {
    let live = true;
    // Wrapped, not just .catch()'d: a synchronous throw here would take the
    // whole navigation down with it, and an opt-in module must not be able to
    // do that to the shell that merely asks whether to show it.
    (async () => {
      try {
        const s = await api.reconSettings();
        if (live) setReconciliation(Boolean(s?.enabled));
      } catch {
        if (live) setReconciliation(false);
      }
    })();
    return () => {
      live = false;
    };
  }, []);

  const refreshBadge = useCallback(() => {
    api
      .alertsSummary()
      .then((s) => setAlertBadge(s.unread))
      .catch(() => setAlertBadge(0));
  }, []);

  // Refresh the unread badge on mount and whenever navigation lands on /alerts
  // (where the user may mark items read).
  useEffect(() => {
    refreshBadge();
  }, [refreshBadge, location.pathname]);

  // The Overview's refresh button re-polls the badge alongside its own data.
  useEffect(() => {
    window.addEventListener(REFRESH_ALERTS_EVENT, refreshBadge);
    return () => window.removeEventListener(REFRESH_ALERTS_EVENT, refreshBadge);
  }, [refreshBadge]);

  // The Analyze section gains one entry when the module is on, and is the
  // untouched constant otherwise.
  const sections = reconciliation
    ? NAV.map((group) =>
        group.section === "Analyze"
          ? {
              ...group,
              items: [
                ...group.items,
                {
                  to: "/reconciliation",
                  label: "Reconciliation",
                  end: false,
                  icon: "reconciliation" as IconName,
                },
              ],
            }
          : group,
      )
    : NAV;

  const exitImpersonation = async () => {
    await api.stopImpersonate();
    await refresh();
    navigate("/admin/customers");
  };

  return (
    <div className="app-shell">
      <aside className="sidebar">
        <div className="sidebar-brand">
          <span className="brand">
            <BrandMark />
            Meter
          </span>
        </div>
        <nav className="sidebar-nav">
          {sections.map((group, i) => (
            <div key={group.section ?? i} className="nav-group">
              {group.section && <span className="nav-group-label">{group.section}</span>}
              {group.items.map((item) => (
                <NavLink
                  key={item.to}
                  to={item.to}
                  end={item.end}
                  className={({ isActive }) => (isActive ? "nav-link active" : "nav-link")}
                >
                  <span className="nav-link-label">
                    <NavIcon name={item.icon} />
                    {item.label}
                  </span>
                  {item.to === "/alerts" && alertBadge > 0 && (
                    <span className="nav-badge" aria-label={`${alertBadge} unread alerts`}>
                      {alertBadge > 99 ? "99+" : alertBadge}
                    </span>
                  )}
                </NavLink>
              ))}
            </div>
          ))}
        </nav>
        <div className="sidebar-bottom">
          {user?.org_name && (
            <div className="sidebar-org">
              <span className="sidebar-org-label">Organization</span>
              <span className="sidebar-org-name">{user.org_name}</span>
            </div>
          )}
          <div className="sidebar-foot">
            {user?.is_admin && !user?.impersonating && (
              <Link to="/admin" className="link">
                Admin portal →
              </Link>
            )}
            <span className="sidebar-email muted">{user?.email}</span>
            <div className="sidebar-actions">
              <button className="link" onClick={() => logout().then(() => navigate("/login"))}>
                Sign out
              </button>
              <ThemeToggle />
            </div>
          </div>
        </div>
      </aside>
      <main className="app-main">
        {user?.impersonating && (
          <div className="impersonation-banner">
            <span>
              Viewing <strong>{user.impersonating.company}</strong> as admin.
            </span>
            <button className="link" onClick={exitImpersonation}>
              Exit impersonation
            </button>
          </div>
        )}
        <Outlet />
      </main>
      <Assistant />
    </div>
  );
}
