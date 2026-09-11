/**
 * Settings — administrative organization settings only.
 *
 * Deliberately NOT a place to connect sources or manage app functionality:
 * providers live in Cost sources, feature discovery in Features, and the
 * signed-in identity / Sign out live in the global navigation. This page is just
 * the organization profile, its budget, and privacy preferences, stored at the
 * tenant level.
 *
 * Four tabs, using the same tablist the Overview's breakdown uses. Every panel
 * stays mounted and inactive ones are hidden, rather than unmounted: each card
 * holds its own draft state and its own request, and switching tabs should not
 * discard a half-typed budget or re-fetch what is already on screen.
 */
import { useEffect, useState } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import { api, ApiError, type OrgSettings } from "../api";
import { BudgetCard } from "../components/BudgetCard";
import { DiscoveryLlmCard } from "../components/DiscoveryLlmCard";
import { PromptOptimizationCard } from "../components/PromptOptimizationCard";

/** Tab ids double as URL fragments, so /settings#budgets opens the right one —
 *  the Alerts form links straight here when a rule needs a budget. */
const TABS = [
  { id: "organization", label: "Organization" },
  { id: "budgets", label: "Budgets" },
  { id: "byok", label: "Bring your own key" },
  { id: "privacy", label: "Privacy & data" },
] as const;

type TabId = (typeof TABS)[number]["id"];

function tabFromHash(hash: string): TabId | null {
  const id = hash.replace(/^#/, "");
  return TABS.some((t) => t.id === id) ? (id as TabId) : null;
}

const TIMEZONES = [
  "UTC",
  "America/Los_Angeles",
  "America/Denver",
  "America/Chicago",
  "America/New_York",
  "America/Sao_Paulo",
  "Europe/London",
  "Europe/Paris",
  "Europe/Berlin",
  "Europe/Madrid",
  "Africa/Johannesburg",
  "Asia/Dubai",
  "Asia/Kolkata",
  "Asia/Singapore",
  "Asia/Shanghai",
  "Asia/Tokyo",
  "Australia/Sydney",
  "Pacific/Auckland",
];

const CUSTOMER_ID_OPTIONS: { value: OrgSettings["customer_id_storage"]; label: string }[] = [
  { value: "names", label: "Actual names" },
  { value: "aliases", label: "Aliases / anonymized" },
  { value: "hashed", label: "Hashed identifiers" },
];

const TRACE_RETENTION_OPTIONS: { value: OrgSettings["trace_retention_days"]; label: string }[] = [
  { value: 7, label: "7 days" },
  { value: 30, label: "30 days" },
  { value: 90, label: "90 days" },
];

const STALE_OPTIONS = [
  { value: 5, label: "5 minutes" },
  { value: 10, label: "10 minutes" },
  { value: 30, label: "30 minutes" },
  { value: 60, label: "1 hour" },
  { value: 240, label: "4 hours" },
];

const RETENTION_OPTIONS: { value: OrgSettings["data_retention"]; label: string }[] = [
  { value: "30d", label: "30 days" },
  { value: "90d", label: "90 days" },
  { value: "1y", label: "1 year" },
  { value: "indefinite", label: "Keep indefinitely" },
];

export function SettingsPage() {
  const [settings, setSettings] = useState<OrgSettings | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [orgSaved, setOrgSaved] = useState(false);
  const [privacySaved, setPrivacySaved] = useState(false);
  const [savingOrg, setSavingOrg] = useState(false);
  const [savingPrivacy, setSavingPrivacy] = useState(false);

  const location = useLocation();
  const navigate = useNavigate();
  const [tab, setTab] = useState<TabId>(() => tabFromHash(location.hash) ?? "organization");

  // A link that arrives with a fragment wins, including when the page is already
  // open and only the fragment changes.
  useEffect(() => {
    const fromHash = tabFromHash(location.hash);
    if (fromHash) setTab(fromHash);
  }, [location.hash]);

  function selectTab(next: TabId) {
    setTab(next);
    // replace, not push: flicking through tabs should not fill up the back button.
    navigate(`#${next}`, { replace: true });
  }

  useEffect(() => {
    api
      .getSettings()
      .then(setSettings)
      .catch((err) => setError(err instanceof ApiError ? err.message : "Could not load settings."));
  }, []);

  function patch(fields: Partial<OrgSettings>) {
    setSettings((s) => (s ? { ...s, ...fields } : s));
    setOrgSaved(false);
    setPrivacySaved(false);
  }

  async function save(
    fields: Partial<OrgSettings>,
    setSaving: (v: boolean) => void,
    setSaved: (v: boolean) => void,
  ) {
    setSaving(true);
    setError(null);
    try {
      setSettings(await api.updateSettings(fields));
      setSaved(true);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not save settings.");
    } finally {
      setSaving(false);
    }
  }

  if (error && !settings) {
    return (
      <div className="content">
        <div className="dash-head">
          <h1>Settings</h1>
        </div>
        <p className="error" role="alert">
          {error}
        </p>
      </div>
    );
  }

  if (!settings) {
    return (
      <div className="content">
        <div className="dash-head">
          <h1>Settings</h1>
        </div>
        <p className="muted">Loading…</p>
      </div>
    );
  }

  return (
    <div className="content settings-page">
      <div className="dash-head">
        <h1>Settings</h1>
      </div>

      {error && (
        <p className="error" role="alert">
          {error}
        </p>
      )}

      <div className="tabs settings-tabs" role="tablist" aria-label="Settings sections">
        {TABS.map((t) => (
          <button
            key={t.id}
            type="button"
            role="tab"
            id={`settings-tab-${t.id}`}
            aria-selected={tab === t.id}
            aria-controls={`settings-panel-${t.id}`}
            className={tab === t.id ? "tab active" : "tab"}
            onClick={() => selectTab(t.id)}
          >
            {t.label}
          </button>
        ))}
      </div>

      <div
        role="tabpanel"
        id="settings-panel-organization"
        aria-labelledby="settings-tab-organization"
        hidden={tab !== "organization"}
      >
        <section className="settings-card">
          <h2>Organization</h2>
          <div className="settings-field">
            <label htmlFor="org-name">Organization name</label>
            <input
              id="org-name"
              value={settings.org_name}
              onChange={(e) => patch({ org_name: e.target.value })}
            />
          </div>
          <div className="settings-field">
            <label htmlFor="org-tz">Time zone</label>
            <select
              id="org-tz"
              value={settings.timezone}
              onChange={(e) => patch({ timezone: e.target.value })}
            >
              {TIMEZONES.map((tz) => (
                <option key={tz} value={tz}>
                  {tz}
                </option>
              ))}
            </select>
          </div>
          <div className="settings-field">
            <label htmlFor="org-currency">Default currency</label>
            <select
              id="org-currency"
              value={settings.currency}
              onChange={(e) => patch({ currency: e.target.value })}
            >
              <option value="USD">USD</option>
            </select>
          </div>
          <div className="settings-actions">
            <button
              onClick={() =>
                save(
                  {
                    org_name: settings.org_name,
                    timezone: settings.timezone,
                    currency: settings.currency,
                  },
                  setSavingOrg,
                  setOrgSaved,
                )
              }
              disabled={savingOrg || !settings.org_name.trim()}
            >
              {savingOrg ? "Saving…" : "Save changes"}
            </button>
            {orgSaved && <span className="settings-saved">Saved ✓</span>}
          </div>
        </section>
      </div>

      <div
        role="tabpanel"
        id="settings-panel-budgets"
        aria-labelledby="settings-tab-budgets"
        hidden={tab !== "budgets"}
      >
        <BudgetCard currency={settings.currency} />
      </div>

      <div
        role="tabpanel"
        id="settings-panel-byok"
        aria-labelledby="settings-tab-byok"
        hidden={tab !== "byok"}
      >
        <DiscoveryLlmCard />
      </div>

      <div
        role="tabpanel"
        id="settings-panel-privacy"
        aria-labelledby="settings-tab-privacy"
        hidden={tab !== "privacy"}
      >
        <section className="settings-card">
          <h2>Privacy &amp; data</h2>
          <p className="muted">Control what Meter stores about your traffic and customers.</p>
          <div className="settings-field">
            <label htmlFor="cust-id">Customer identifiers</label>
            <select
              id="cust-id"
              value={settings.customer_id_storage}
              onChange={(e) =>
                patch({ customer_id_storage: e.target.value as OrgSettings["customer_id_storage"] })
              }
            >
              {CUSTOMER_ID_OPTIONS.map((o) => (
                <option key={o.value} value={o.value}>
                  {o.label}
                </option>
              ))}
            </select>
            <span className="settings-hint muted">
              How customer identifiers are stored once customer-level attribution ships.
            </span>
          </div>
          <div className="settings-field">
            <label htmlFor="trace-retention">Trace retention</label>
            <select
              id="trace-retention"
              value={settings.trace_retention_days}
              onChange={(e) =>
                patch({
                  trace_retention_days: Number(
                    e.target.value,
                  ) as OrgSettings["trace_retention_days"],
                })
              }
            >
              {TRACE_RETENTION_OPTIONS.map((o) => (
                <option key={o.value} value={o.value}>
                  {o.label}
                </option>
              ))}
            </select>
            <span className="settings-hint muted">
              How long request-level traces and their steps are kept. Your cost totals are not
              affected — deleting traces never changes what a month cost.
            </span>
          </div>
          <div className="settings-field">
            <label htmlFor="stale-after">Agent stale threshold</label>
            <select
              id="stale-after"
              value={settings.agent_stale_after_minutes}
              onChange={(e) => patch({ agent_stale_after_minutes: Number(e.target.value) })}
            >
              {STALE_OPTIONS.map((o) => (
                <option key={o.value} value={o.value}>
                  {o.label}
                </option>
              ))}
            </select>
            <span className="settings-hint muted">
              How long a running agent may go quiet before Traces marks it stale. A stale run has
              not failed and may still finish.
            </span>
          </div>
          {/* Stated, not offered. This is the trace path, and it stays
              content-free whatever is configured. Consented prompt collection
              is a separate store with its own terms: the Prompt optimization
              card below. */}
          <div className="settings-field">
            <span className="settings-label-static">Content capture</span>
            <span className="content-capture-value">
              <span className="badge">Disabled</span>
            </span>
            <span className="settings-hint muted">
              Traces record prompt identity, version, tokens and cost, never prompt or response
              content, and that cannot be changed. Prompts are collected only through Prompt
              optimization below, for the features you choose, with your consent.
            </span>
          </div>
          <div className="settings-field">
            <label htmlFor="retention">Data retention</label>
            <select
              id="retention"
              value={settings.data_retention}
              onChange={(e) =>
                patch({ data_retention: e.target.value as OrgSettings["data_retention"] })
              }
            >
              {RETENTION_OPTIONS.map((o) => (
                <option key={o.value} value={o.value}>
                  {o.label}
                </option>
              ))}
            </select>
          </div>
          <div className="settings-actions">
            <button
              onClick={() =>
                save(
                  {
                    customer_id_storage: settings.customer_id_storage,
                    data_retention: settings.data_retention,
                    trace_retention_days: settings.trace_retention_days,
                    agent_stale_after_minutes: settings.agent_stale_after_minutes,
                    // content_capture is deliberately absent: it has no control,
                    // and sending it would be the one path by which a UI could
                    // ever turn capture on.
                  },
                  setSavingPrivacy,
                  setPrivacySaved,
                )
              }
              disabled={savingPrivacy}
            >
              {savingPrivacy ? "Saving…" : "Save changes"}
            </button>
            {privacySaved && <span className="settings-saved">Saved ✓</span>}
          </div>
        </section>
        <PromptOptimizationCard />
      </div>
    </div>
  );
}
