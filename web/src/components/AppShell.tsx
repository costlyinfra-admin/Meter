/**
 * Full-screen app shell: a fixed left sidebar (brand, nav, account) wrapping the
 * routed page content. Replaces the per-page top bars.
 *
 * When an allow-listed admin is impersonating a customer, a banner appears and the
 * entire customer UI runs in that tenant (context switched server-side) — no pages
 * are duplicated for the admin.
 */
import { useCallback, useEffect, useRef, useState } from "react";
import { Link, NavLink, Outlet, useLocation, useNavigate } from "react-router-dom";
import { api } from "../api";
import { useAuth } from "../auth/AuthContext";
import { ThemeToggle } from "./ThemeToggle";
import { REFRESH_ALERTS_EVENT } from "../pages/Dashboard";
import { AskMeterBubble } from "./AskMeter";
import { Assistant } from "./Assistant";
import { BrandMark } from "./BrandMark";
import { LineIcon, type IconName } from "./LineIcon";
import { Toaster } from "./Toaster";

interface NavItem {
  to: string;
  label: string;
  end: boolean;
  icon: IconName;
}

/** The nav in sections, in the order the product is actually used: read the
 *  numbers, act on what they say, watch for what changes, then set up and look
 *  things up. The first section has no heading — Overview is the front door of
 *  the product, not a member of a group. */
const NAV: { section?: string; items: NavItem[] }[] = [
  { items: [{ to: "/", label: "Overview", end: true, icon: "overview" }] },
  {
    section: "Analyze",
    items: [
      { to: "/applications", label: "Applications", end: false, icon: "applications" },
      { to: "/products", label: "Products", end: false, icon: "products" },
      { to: "/features", label: "Features", end: false, icon: "features" },
      { to: "/traces", label: "Traces", end: false, icon: "traces" },
      // Last in Analyze: the other four describe spend that has happened, and
      // this is the one that looks ahead of it.
      { to: "/forecast", label: "Forecast", end: false, icon: "forecast" },
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
    section: "Monitor",
    items: [{ to: "/alerts", label: "Alerts", end: false, icon: "alerts" }],
  },
  {
    // Connecting a provider is setup, not analysis: you do it once, and the
    // screens above are where you go afterwards.
    section: "Setup",
    items: [
      { to: "/cost-sources", label: "Connect sources", end: false, icon: "sources" },
      { to: "/install-sdk", label: "Install SDK", end: false, icon: "sdk" },
      { to: "/settings", label: "Settings", end: false, icon: "settings" },
    ],
  },
  {
    section: "Help",
    items: [
      { to: "/help", label: "Knowledge base", end: false, icon: "help" },
      // The price book, readable. Every measured saving in this product is
      // these rates times a counted number of tokens, so it belongs where a
      // reader goes to check something rather than behind a settings gate.
      { to: "/pricing", label: "Provider pricing", end: false, icon: "pricing" },
    ],
  },
];

function NavIcon({ name }: { name: IconName }) {
  return <LineIcon name={name} className="nav-icon" />;
}

export function AppShell() {
  const { user, logout, refresh } = useAuth();
  // The nav scrolls with no scrollbar, so a fade at its bottom edge is the only
  // sign the list continues. Applied ONLY while something really is below: no
  // cue when everything fits, and none once you have reached the end.
  const navRef = useRef<HTMLElement | null>(null);
  const [navHasMore, setNavHasMore] = useState(false);
  const navigate = useNavigate();
  const location = useLocation();
  const [alertBadge, setAlertBadge] = useState(0);
  // Bumped when the unread count goes UP, which re-mounts the badge so it
  // pulses once. Not on first load: arriving to two old alerts is not news.
  const [badgePulse, setBadgePulse] = useState(0);
  const lastBadge = useRef<number | null>(null);
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
      .then((s) => {
        const before = lastBadge.current;
        lastBadge.current = s.unread;
        if (before !== null && s.unread > before) setBadgePulse((n) => n + 1);
        setAlertBadge(s.unread);
      })
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

  // The Monitor section gains one entry when the module is on, and is the
  // untouched constant otherwise.
  const sections = reconciliation
    ? NAV.map((group) =>
        group.section === "Monitor"
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

  useEffect(() => {
    const el = navRef.current;
    if (!el) return;
    const update = () => setNavHasMore(el.scrollHeight - el.clientHeight - el.scrollTop > 1);
    update();
    el.addEventListener("scroll", update, { passive: true });
    // The case that started this: the window shortens, the nav's box shrinks,
    // and what fitted a moment ago no longer does.
    const observer = new ResizeObserver(update);
    observer.observe(el);
    return () => {
      el.removeEventListener("scroll", update);
      observer.disconnect();
    };
    // Re-measured whenever the list itself changes length: an admin sees one
    // row a customer does not, and Reconciliation appears when it is switched on.
  }, [user?.is_admin, user?.impersonating, reconciliation]);

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
        <nav className={navHasMore ? "sidebar-nav has-more" : "sidebar-nav"} ref={navRef}>
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
                    <span
                      key={badgePulse}
                      className={badgePulse ? "nav-badge pulse" : "nav-badge"}
                      aria-label={`${alertBadge} unread alerts`}
                    >
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
      {/* Sits above the assistant's launcher and points at it. Renders nothing
          once the chat has been found. */}
      <AskMeterBubble />
      <Toaster />
    </div>
  );
}
