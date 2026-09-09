/**
 * Alert detail — the full state of one rule plus its recent evaluation, event, and
 * delivery history, with the standard actions (edit / test / duplicate / toggle /
 * delete). Read-only aside from the actions.
 */
import { useCallback, useEffect, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { api, ApiError, type AlertRule } from "../api";
import {
  CHANNEL_LABELS,
  CONDITION_LABELS,
  COOLDOWN_LABELS,
  EVENT_LABELS,
  METRIC_LABELS,
  previewText,
  SCOPE_LABELS,
  STATUS_LABELS,
  statusClass,
  WINDOW_LABELS,
  ruleQuantity,
} from "../alertLabels";

interface Detail extends AlertRule {
  history: {
    events: {
      id: string;
      event_type: string;
      observed_value: number | null;
      threshold: number | null;
      window: string | null;
      occurred_at: string | null;
    }[];
    notifications: {
      channel: string;
      status: string;
      error: string | null;
      attempts: number;
      created_at: string | null;
    }[];
  };
}

function when(iso: string | null): string {
  return iso ? new Date(iso).toLocaleString() : "—";
}

export function AlertDetailPage() {
  const { id } = useParams();
  const navigate = useNavigate();
  const [rule, setRule] = useState<Detail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);

  const load = useCallback(async () => {
    if (!id) return;
    try {
      setRule((await api.getAlert(id)) as Detail);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not load alert.");
    }
  }, [id]);

  useEffect(() => {
    load();
  }, [load]);

  async function act(fn: () => Promise<unknown>, message: string) {
    setNote(null);
    setError(null);
    try {
      await fn();
      setNote(message);
      await load();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Action failed.");
    }
  }

  if (error && !rule)
    return (
      <div className="content">
        <p className="error" role="alert">
          {error}
        </p>
      </div>
    );
  if (!rule)
    return (
      <div className="content">
        <p className="muted">Loading…</p>
      </div>
    );

  return (
    <div className="content alert-detail-page">
      <div className="dash-head alerts-head">
        <div>
          <Link to="/alerts" className="link breadcrumb">
            ← Alerts
          </Link>
          <h1>
            {rule.name}{" "}
            <span className={statusClass(rule.status)}>{STATUS_LABELS[rule.status]}</span>
          </h1>
          {rule.description && <p className="muted">{rule.description}</p>}
        </div>
        <div className="detail-actions">
          <Link to={`/alerts/${rule.id}/edit`} className="secondary-link">
            Edit
          </Link>
          <button
            className="secondary"
            onClick={() => act(() => api.testAlert(rule.id), "Test sent.")}
          >
            Send test
          </button>
          <button
            className="secondary"
            onClick={() => act(() => api.duplicateAlert(rule.id), "Duplicated.")}
          >
            Duplicate
          </button>
          <button
            className="secondary"
            onClick={() => act(() => api.enableAlert(rule.id, !rule.enabled), "Updated.")}
          >
            {rule.enabled ? "Disable" : "Enable"}
          </button>
          <button
            className="secondary danger"
            onClick={() => {
              if (window.confirm(`Delete "${rule.name}"?`))
                act(async () => {
                  await api.deleteAlert(rule.id);
                  navigate("/alerts");
                }, "Deleted.");
            }}
          >
            Delete
          </button>
        </div>
      </div>

      {error && (
        <p className="error" role="alert">
          {error}
        </p>
      )}
      {note && <p className="muted alerts-note">{note}</p>}

      <div className="settings-card">
        <p className="alert-preview">{previewText(rule)}</p>
        <dl className="detail-grid">
          <Field label="Metric" value={METRIC_LABELS[rule.metric]} />
          <Field
            label="Scope"
            value={
              rule.scope_type === "organization"
                ? SCOPE_LABELS.organization
                : `${SCOPE_LABELS[rule.scope_type]}: ${rule.scope_label ?? rule.scope_ref}`
            }
          />
          <Field label="Condition" value={CONDITION_LABELS[rule.condition_type]} />
          <Field label="Threshold" value={ruleQuantity(rule, rule.threshold)} />
          <Field
            label="Current observed"
            value={rule.last_observed != null ? ruleQuantity(rule, rule.last_observed) : "—"}
          />
          <Field label="Evaluation window" value={WINDOW_LABELS[rule.window]} />
          <Field label="Cooldown" value={COOLDOWN_LABELS[rule.cooldown]} />
          <Field label="Recovery notifications" value={rule.recovery_notify ? "On" : "Off"} />
          <Field label="Channels" value={rule.channels.map((c) => c.label).join(", ")} />
          <Field label="Created by" value={rule.created_by ?? "—"} />
          <Field label="Created" value={when(rule.created_at)} />
          <Field label="Last evaluated" value={when(rule.last_evaluated_at)} />
          <Field label="Last triggered" value={when(rule.last_triggered_at)} />
        </dl>
      </div>

      <div className="settings-card">
        <h2>Recent events</h2>
        {rule.history.events.length === 0 ? (
          <p className="muted">No events yet.</p>
        ) : (
          <ul className="detail-history">
            {rule.history.events.map((e) => (
              <li key={e.id}>
                <span className={`event-badge event-${e.event_type}`}>
                  {EVENT_LABELS[e.event_type] ?? e.event_type}
                </span>
                <span className="muted">
                  {e.observed_value != null
                    ? `observed ${ruleQuantity(rule, e.observed_value)}`
                    : ""}
                  {e.threshold != null ? ` vs ${ruleQuantity(rule, e.threshold)}` : ""} ·{" "}
                  {when(e.occurred_at)}
                </span>
              </li>
            ))}
          </ul>
        )}
      </div>

      <div className="settings-card">
        <h2>Notification delivery</h2>
        {rule.history.notifications.length === 0 ? (
          <p className="muted">No delivery attempts yet.</p>
        ) : (
          <ul className="detail-history">
            {rule.history.notifications.map((n, i) => (
              <li key={i}>
                <span className={`event-badge delivery-${n.status}`}>
                  {CHANNEL_LABELS[n.channel] ?? n.channel}: {n.status}
                </span>
                <span className="muted">
                  {n.error ? `${n.error} · ` : ""}
                  {n.attempts} attempt{n.attempts === 1 ? "" : "s"} · {when(n.created_at)}
                </span>
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>
  );
}

function Field({ label, value }: { label: string; value: string }) {
  return (
    <div className="detail-field">
      <dt>{label}</dt>
      <dd>{value}</dd>
    </div>
  );
}
