/**
 * Alerts — a first-class product area: monitor AI cost and get notified. Read-only
 * reporting plus rule management; classification/editing happens in the form pages.
 */
import { useCallback, useEffect, useMemo, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { api, ApiError, type AlertActivityEvent, type AlertRule, type AlertSummary } from "../api";
import {
  CHANNEL_LABELS,
  conditionText,
  costLink,
  EVENT_LABELS,
  METRIC_LABELS,
  SCOPE_LABELS,
  STATUS_LABELS,
  statusClass,
  WINDOW_LABELS,
  ruleQuantity,
} from "../alertLabels";

type Tab = "rules" | "activity";

function timeAgo(iso: string | null): string {
  if (!iso) return "—";
  return new Date(iso).toLocaleString("en-US", {
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
  });
}

export function AlertsPage() {
  const navigate = useNavigate();
  const [tab, setTab] = useState<Tab>("rules");
  const [rules, setRules] = useState<AlertRule[] | null>(null);
  const [summary, setSummary] = useState<AlertSummary | null>(null);
  const [activity, setActivity] = useState<AlertActivityEvent[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);

  // Filters.
  const [search, setSearch] = useState("");
  const [statusFilter, setStatusFilter] = useState("all");
  const [metricFilter, setMetricFilter] = useState("all");
  const [channelFilter, setChannelFilter] = useState("all");

  const reload = useCallback(async () => {
    try {
      const data = await api.listAlerts();
      setRules(data.rules);
      setSummary(data.summary);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not load alerts.");
      setRules([]);
    }
  }, []);

  const reloadActivity = useCallback(async () => {
    try {
      setActivity((await api.alertsActivity()).events);
    } catch {
      setActivity([]);
    }
  }, []);

  useEffect(() => {
    reload();
    reloadActivity();
  }, [reload, reloadActivity]);

  const filtered = useMemo(() => {
    if (!rules) return [];
    const q = search.trim().toLowerCase();
    return rules.filter(
      (r) =>
        (!q || r.name.toLowerCase().includes(q)) &&
        (statusFilter === "all" || r.status === statusFilter) &&
        (metricFilter === "all" || r.metric === metricFilter) &&
        (channelFilter === "all" || r.channels.some((c) => c.channel === channelFilter)),
    );
  }, [rules, search, statusFilter, metricFilter, channelFilter]);

  async function act(fn: () => Promise<unknown>, message: string) {
    setError(null);
    setNote(null);
    try {
      await fn();
      setNote(message);
      await reload();
      await reloadActivity();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Action failed.");
    }
  }

  return (
    <div className="content alerts-page">
      <div className="dash-head alerts-head">
        <div>
          <h1>Alerts</h1>
          <p className="muted">Monitor AI costs and get notified when something needs attention.</p>
        </div>
        <button onClick={() => navigate("/alerts/new")}>Create alert</button>
      </div>

      {error && (
        <p className="error" role="alert">
          {error}
        </p>
      )}
      {note && <p className="muted alerts-note">{note}</p>}

      <div className="alerts-summary">
        <SummaryCard label="Triggered" value={summary?.triggered} tone="triggered" />
        <SummaryCard label="Healthy" value={summary?.healthy} tone="healthy" />
        <SummaryCard label="Delivery errors" value={summary?.delivery_errors} tone="error" />
        <SummaryCard label="Disabled" value={summary?.disabled} tone="muted" />
      </div>

      <div className="tabs" role="tablist" aria-label="Alerts views">
        <button
          role="tab"
          aria-selected={tab === "rules"}
          className={tab === "rules" ? "tab active" : "tab"}
          onClick={() => setTab("rules")}
        >
          Alert rules
        </button>
        <button
          role="tab"
          aria-selected={tab === "activity"}
          className={tab === "activity" ? "tab active" : "tab"}
          onClick={() => setTab("activity")}
        >
          Activity
        </button>
      </div>

      {tab === "rules" && (
        <RulesTab
          rules={rules}
          filtered={filtered}
          search={search}
          setSearch={setSearch}
          statusFilter={statusFilter}
          setStatusFilter={setStatusFilter}
          metricFilter={metricFilter}
          setMetricFilter={setMetricFilter}
          channelFilter={channelFilter}
          setChannelFilter={setChannelFilter}
          onAct={act}
          navigate={navigate}
        />
      )}

      {tab === "activity" && (
        <ActivityTab
          activity={activity}
          onMarkAll={() => act(() => api.markAllAlertsRead(), "Marked all as read.")}
          onMarkRead={(id) => act(() => api.markAlertsRead([id]), "Marked as read.")}
        />
      )}
    </div>
  );
}

function SummaryCard({
  label,
  value,
  tone,
}: {
  label: string;
  value: number | undefined;
  tone: string;
}) {
  return (
    <div className={`alert-summary-card tone-${tone}`}>
      <span className="alert-summary-value">{value ?? "—"}</span>
      <span className="alert-summary-label">{label}</span>
    </div>
  );
}

type ActFn = (fn: () => Promise<unknown>, message: string) => Promise<void>;

function RulesTab({
  rules,
  filtered,
  search,
  setSearch,
  statusFilter,
  setStatusFilter,
  metricFilter,
  setMetricFilter,
  channelFilter,
  setChannelFilter,
  onAct,
  navigate,
}: {
  rules: AlertRule[] | null;
  filtered: AlertRule[];
  search: string;
  setSearch: (v: string) => void;
  statusFilter: string;
  setStatusFilter: (v: string) => void;
  metricFilter: string;
  setMetricFilter: (v: string) => void;
  channelFilter: string;
  setChannelFilter: (v: string) => void;
  onAct: ActFn;
  navigate: (to: string) => void;
}) {
  if (rules === null) return <p className="muted">Loading…</p>;

  if (rules.length === 0) {
    return (
      <div className="empty-state">
        <p className="empty-title">No alerts yet</p>
        <p className="muted">
          Create an alert to be notified when AI costs spike, exceed a budget, or drift.
        </p>
        <button onClick={() => navigate("/alerts/new")}>Create your first alert</button>
      </div>
    );
  }

  return (
    <>
      <div className="alerts-filters">
        <input
          placeholder="Search by alert name"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          aria-label="Search alerts"
        />
        <select
          value={statusFilter}
          onChange={(e) => setStatusFilter(e.target.value)}
          aria-label="Filter by status"
        >
          <option value="all">All statuses</option>
          {Object.entries(STATUS_LABELS).map(([v, l]) => (
            <option key={v} value={v}>
              {l}
            </option>
          ))}
        </select>
        <select
          value={metricFilter}
          onChange={(e) => setMetricFilter(e.target.value)}
          aria-label="Filter by metric"
        >
          <option value="all">All metrics</option>
          {Object.entries(METRIC_LABELS).map(([v, l]) => (
            <option key={v} value={v}>
              {l}
            </option>
          ))}
        </select>
        <select
          value={channelFilter}
          onChange={(e) => setChannelFilter(e.target.value)}
          aria-label="Filter by channel"
        >
          <option value="all">All channels</option>
          {Object.entries(CHANNEL_LABELS).map(([v, l]) => (
            <option key={v} value={v}>
              {l}
            </option>
          ))}
        </select>
      </div>

      {filtered.length === 0 ? (
        <p className="muted">No alerts match your filters.</p>
      ) : (
        <div className="alerts-table-wrap">
          <table className="alerts-table">
            <thead>
              <tr>
                <th>Alert</th>
                <th>Metric &amp; scope</th>
                <th>Condition</th>
                <th>Status</th>
                <th>Last evaluated</th>
                <th>Channels</th>
                <th>Enabled</th>
                <th aria-label="Actions" />
              </tr>
            </thead>
            <tbody>
              {filtered.map((r) => (
                <tr key={r.id} className="alert-row">
                  <td>
                    <Link to={`/alerts/${r.id}`} className="alert-name">
                      {r.name}
                    </Link>
                  </td>
                  <td className="muted">
                    {METRIC_LABELS[r.metric] ?? r.metric}
                    <span className="alert-scope">
                      {" · "}
                      {r.scope_type === "organization"
                        ? SCOPE_LABELS.organization
                        : `${SCOPE_LABELS[r.scope_type]}: ${r.scope_label ?? r.scope_ref}`}
                    </span>
                  </td>
                  <td className="muted">
                    {conditionText(r)} · {WINDOW_LABELS[r.window]}
                  </td>
                  <td>
                    <span className={statusClass(r.status)}>
                      {STATUS_LABELS[r.status] ?? r.status}
                    </span>
                  </td>
                  <td className="muted">{timeAgo(r.last_evaluated_at)}</td>
                  <td className="muted">
                    {r.channels.map((c) => CHANNEL_LABELS[c.channel] ?? c.channel).join(", ")}
                  </td>
                  <td>
                    <label className="toggle">
                      <input
                        type="checkbox"
                        checked={r.enabled}
                        aria-label={`${r.enabled ? "Disable" : "Enable"} ${r.name}`}
                        onChange={(e) =>
                          onAct(
                            () => api.enableAlert(r.id, e.target.checked),
                            e.target.checked ? "Alert enabled." : "Alert disabled.",
                          )
                        }
                      />
                    </label>
                  </td>
                  <td>
                    <details className="row-actions">
                      <summary aria-label={`Actions for ${r.name}`}>⋯</summary>
                      <ul>
                        <li>
                          <Link to={`/alerts/${r.id}`}>View details</Link>
                        </li>
                        <li>
                          <Link to={`/alerts/${r.id}/edit`}>Edit</Link>
                        </li>
                        <li>
                          <button
                            onClick={() =>
                              onAct(() => api.duplicateAlert(r.id), "Alert duplicated.")
                            }
                          >
                            Duplicate
                          </button>
                        </li>
                        <li>
                          <button
                            onClick={() =>
                              onAct(
                                () => api.enableAlert(r.id, !r.enabled),
                                r.enabled ? "Alert disabled." : "Alert enabled.",
                              )
                            }
                          >
                            {r.enabled ? "Disable" : "Enable"}
                          </button>
                        </li>
                        <li>
                          <button
                            onClick={() =>
                              onAct(() => api.testAlert(r.id), "Test notification sent.")
                            }
                          >
                            Send test notification
                          </button>
                        </li>
                        <li>
                          <button
                            className="danger"
                            onClick={() => {
                              if (
                                window.confirm(`Delete alert "${r.name}"? This cannot be undone.`)
                              )
                                onAct(() => api.deleteAlert(r.id), "Alert deleted.");
                            }}
                          >
                            Delete
                          </button>
                        </li>
                      </ul>
                    </details>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </>
  );
}

function ActivityTab({
  activity,
  onMarkAll,
  onMarkRead,
}: {
  activity: AlertActivityEvent[] | null;
  onMarkAll: () => void;
  onMarkRead: (id: string) => void;
}) {
  if (activity === null) return <p className="muted">Loading…</p>;
  if (activity.length === 0)
    return (
      <div className="empty-state">
        <p className="empty-title">No activity yet</p>
        <p className="muted">Triggered, resolved, and delivery events will appear here.</p>
      </div>
    );

  return (
    <>
      <div className="activity-toolbar">
        <button className="secondary" onClick={onMarkAll}>
          Mark all as read
        </button>
      </div>
      <ul className="activity-feed">
        {activity.map((e) => (
          <li key={e.id} className={e.read ? "activity-item read" : "activity-item"}>
            <div className="activity-main">
              <span className={`event-badge event-${e.event_type}`}>
                {EVENT_LABELS[e.event_type] ?? e.event_type}
              </span>
              <Link to={`/alerts/${e.alert_id}`} className="activity-rule">
                {e.alert_name}
              </Link>
              <span className="muted activity-meta">
                {e.metric_label}
                {(e.scope_label ?? e.scope_ref) ? ` · ${e.scope_label ?? e.scope_ref}` : ""}
                {e.window ? ` · ${WINDOW_LABELS[e.window] ?? e.window}` : ""}
              </span>
            </div>
            <div className="activity-detail muted">
              {e.observed_value != null && (
                <span>
                  Observed {ruleQuantity(e, e.observed_value)}
                  {e.threshold != null ? ` vs threshold ${ruleQuantity(e, e.threshold)}` : ""}
                </span>
              )}
              {e.deliveries.length > 0 && (
                <span className="activity-delivery">
                  {e.deliveries
                    .map((d) => `${CHANNEL_LABELS[d.channel] ?? d.channel}: ${d.status}`)
                    .join(" · ")}
                </span>
              )}
              <span>{timeAgo(e.occurred_at)}</span>
              <Link to={costLink(e.scope_type, e.scope_ref)} className="link">
                View cost
              </Link>
              {!e.read && (
                <button className="link" onClick={() => onMarkRead(e.id)}>
                  Mark read
                </button>
              )}
            </div>
          </li>
        ))}
      </ul>
    </>
  );
}
