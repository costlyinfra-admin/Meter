/**
 * Create / edit an alert rule. Full-page form (the app's form pattern), with
 * quick-start templates, a dynamic plain-language preview, condition/scope lists
 * that adapt to the metric, org-timezone context, and masked channel secrets.
 */
import { useEffect, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import {
  api,
  ApiError,
  type AiApplication,
  type AlertInput,
  type AlertMeta,
  type Feature,
} from "../api";
import {
  CHANNEL_LABELS,
  CONDITION_LABELS,
  COOLDOWN_LABELS,
  METRIC_LABELS,
  METRIC_UNITS,
  previewText,
  SCOPE_LABELS,
  UNIT_LABELS,
  WINDOW_LABELS,
} from "../alertLabels";
import { useAuth } from "../auth/AuthContext";

const EMPTY: AlertInput = {
  name: "",
  description: "",
  metric: "inference_cost",
  scope_type: "organization",
  scope_ref: "",
  condition_type: "exceeds",
  threshold: 100,
  budget_amount: null,
  window: "daily",
  cooldown: "day",
  recovery_notify: true,
  enabled: true,
  channels: [{ channel: "in_app" }],
};

export function AlertFormPage() {
  const { id } = useParams();
  const editing = Boolean(id);
  const navigate = useNavigate();
  const { user } = useAuth();
  const [meta, setMeta] = useState<AlertMeta | null>(null);
  const [features, setFeatures] = useState<Feature[]>([]);
  const [applications, setApplications] = useState<AiApplication[]>([]);
  const [form, setForm] = useState<AlertInput>(EMPTY);
  const [timezone, setTimezone] = useState<string>("UTC");
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    api
      .alertsMeta()
      .then(setMeta)
      .catch(() => setMeta(null));
    api
      .getSettings()
      .then((s) => setTimezone(s.timezone))
      .catch(() => {});
    api
      .listFeatures()
      .then(setFeatures)
      .catch(() => setFeatures([]));
    api
      .aiApplications()
      .then((r) => setApplications(r.applications))
      .catch(() => setApplications([]));
    if (id) {
      api
        .getAlert(id)
        .then((r) =>
          setForm({
            name: r.name,
            description: r.description ?? "",
            metric: r.metric,
            scope_type: r.scope_type,
            scope_ref: r.scope_ref ?? "",
            condition_type: r.condition_type,
            threshold: r.threshold,
            budget_amount: r.budget_amount,
            window: r.window,
            cooldown: r.cooldown,
            recovery_notify: r.recovery_notify,
            enabled: r.enabled,
            // Secrets never come back; keep existing non-secret channels, user re-adds secrets.
            channels: r.channels.map((c) => ({
              channel: c.channel,
              target: c.channel === "email" ? (c.target ?? "") : "",
            })),
          }),
        )
        .catch((err) => setError(err instanceof ApiError ? err.message : "Could not load alert."));
    }
  }, [id]);

  const validConditions = meta?.valid_conditions[form.metric] ?? ["exceeds"];
  const validScopes = meta?.valid_scopes[form.metric] ?? ["organization"];

  function patch(fields: Partial<AlertInput>) {
    setForm((f) => {
      const next = { ...f, ...fields };
      // Keep condition/scope valid for the chosen metric.
      if (meta) {
        if (!(meta.valid_conditions[next.metric] ?? []).includes(next.condition_type))
          next.condition_type = (meta.valid_conditions[next.metric] ?? ["exceeds"])[0];
        if (!(meta.valid_scopes[next.metric] ?? []).includes(next.scope_type)) {
          next.scope_type = "organization";
          next.scope_ref = "";
        }
      }
      return next;
    });
  }

  function toggleChannel(channel: string, on: boolean) {
    setForm((f) => ({
      ...f,
      channels: on
        ? [...f.channels, { channel, target: channel === "email" ? (user?.email ?? "") : "" }]
        : f.channels.filter((c) => c.channel !== channel),
    }));
  }

  function patchChannel(channel: string, fields: { target?: string; secret?: string }) {
    setForm((f) => ({
      ...f,
      channels: f.channels.map((c) => (c.channel === channel ? { ...c, ...fields } : c)),
    }));
  }

  function applyTemplate(tid: string) {
    const t = meta?.templates.find((x) => x.id === tid);
    if (t) setForm((f) => ({ ...EMPTY, channels: f.channels, ...(t.rule as Partial<AlertInput>) }));
  }

  async function save() {
    if (needsBudget && !hasBudget) {
      // The server refuses this too; stopping here keeps the message beside the
      // control that caused it rather than at the top of the page.
      setError(meta?.budget_required_message ?? "Configure a budget before saving this alert.");
      return;
    }
    setSaving(true);
    setError(null);
    try {
      const body: AlertInput = {
        ...form,
        scope_ref: form.scope_type === "organization" ? null : form.scope_ref,
      };
      const saved = editing ? await api.updateAlert(id!, body) : await api.createAlert(body);
      navigate(`/alerts/${saved.id}`);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not save alert.");
    } finally {
      setSaving(false);
    }
  }

  const selected = (ch: string) => form.channels.some((c) => c.channel === ch);
  // A percentage CONDITION overrides the metric's units — an increase_pct rule
  // on cost per run is measured in %, not dollars.
  const isPct = form.condition_type === "increase_pct" || form.condition_type === "budget_pct";
  const unit = meta?.metric_units?.[form.metric] ?? METRIC_UNITS[form.metric] ?? "money";
  const thresholdLabel = isPct ? "Threshold (%)" : `Threshold (${UNIT_LABELS[unit] ?? unit})`;
  // Whether this rule needs a budget, and whether the organization has one. Both
  // come from the server: the form does not decide which conditions are
  // budget-backed, and it never falls back to a figure of its own.
  const needsBudget = (meta?.budget_conditions ?? ["budget_pct"]).includes(form.condition_type);
  const hasBudget = meta?.has_budget ?? true;
  const blockedOnBudget = needsBudget && !hasBudget;

  return (
    <div className="content alert-form-page">
      <div className="dash-head">
        <div>
          <Link to="/alerts" className="link breadcrumb">
            ← Alerts
          </Link>
          <h1>{editing ? "Edit alert" : "Create alert"}</h1>
        </div>
      </div>

      {error && (
        <p className="error" role="alert">
          {error}
        </p>
      )}

      {!editing && meta && (
        <div className="settings-card">
          <h2>Quick-start templates</h2>
          <div className="template-grid">
            {meta.templates.map((t) => (
              <button
                key={t.id}
                className="secondary template-btn"
                onClick={() => applyTemplate(t.id)}
                disabled={t.requires_budget && !hasBudget}
                title={
                  t.requires_budget && !hasBudget
                    ? "Needs an organization budget — set one in Settings → Budgets."
                    : undefined
                }
              >
                {t.label}
                {t.requires_budget && !hasBudget && (
                  <span className="template-note"> · needs a budget</span>
                )}
              </button>
            ))}
          </div>
        </div>
      )}

      <div className="settings-card">
        <p className="alert-preview" aria-live="polite">
          {previewText(form)}
        </p>

        <div className="settings-field">
          <label htmlFor="a-name">Alert name</label>
          <input id="a-name" value={form.name} onChange={(e) => patch({ name: e.target.value })} />
        </div>
        <div className="settings-field">
          <label htmlFor="a-desc">Description (optional)</label>
          <input
            id="a-desc"
            value={form.description ?? ""}
            onChange={(e) => patch({ description: e.target.value })}
          />
        </div>

        <div className="settings-field">
          <label htmlFor="a-metric">Metric</label>
          <select
            id="a-metric"
            value={form.metric}
            onChange={(e) => patch({ metric: e.target.value })}
          >
            {Object.entries(METRIC_LABELS).map(([v, l]) => (
              <option key={v} value={v}>
                {l}
              </option>
            ))}
          </select>
        </div>

        <div className="settings-field">
          <label htmlFor="a-scope">Scope</label>
          <select
            id="a-scope"
            value={form.scope_type}
            onChange={(e) => patch({ scope_type: e.target.value })}
          >
            {validScopes.map((s) => (
              <option key={s} value={s}>
                {SCOPE_LABELS[s] ?? s}
              </option>
            ))}
          </select>
          {form.scope_type === "feature" && (
            <select
              className="scope-ref"
              value={form.scope_ref ?? ""}
              onChange={(e) => patch({ scope_ref: e.target.value })}
              aria-label="Feature to scope to"
            >
              <option value="">Select a feature…</option>
              {features.map((f) => (
                <option key={f.id} value={f.id}>
                  {f.name}
                </option>
              ))}
            </select>
          )}
          {form.scope_type === "application" && (
            <select
              className="scope-ref"
              value={form.scope_ref ?? ""}
              onChange={(e) => patch({ scope_ref: e.target.value })}
              aria-label="Application to scope to"
            >
              <option value="">Select an application…</option>
              {applications.map((a) => (
                <option key={a.id} value={a.id}>
                  {a.name}
                </option>
              ))}
            </select>
          )}
          {(form.scope_type === "provider" || form.scope_type === "model") && (
            <input
              className="scope-ref"
              placeholder={`Enter the ${form.scope_type} (e.g. ${
                form.scope_type === "provider" ? "anthropic" : "claude-sonnet-4-6"
              })`}
              value={form.scope_ref ?? ""}
              onChange={(e) => patch({ scope_ref: e.target.value })}
              aria-label={`${form.scope_type} to scope to`}
            />
          )}
        </div>

        <div className="settings-field">
          <label htmlFor="a-cond">Condition</label>
          <select
            id="a-cond"
            value={form.condition_type}
            onChange={(e) => patch({ condition_type: e.target.value })}
          >
            {validConditions.map((c) => (
              <option key={c} value={c}>
                {CONDITION_LABELS[c] ?? c}
              </option>
            ))}
          </select>
        </div>

        <div className="settings-field">
          <label htmlFor="a-threshold">{thresholdLabel}</label>
          <input
            id="a-threshold"
            type="number"
            min="0"
            step="any"
            value={form.threshold}
            onChange={(e) => patch({ threshold: Number(e.target.value) })}
          />
        </div>
        {/* The denominator is the organization's stored budget, so there is no
            budget field here any more — only a block when there is none to
            measure against. */}
        {needsBudget && !hasBudget && (
          <p className="error alert-needs-budget" role="alert">
            {meta?.budget_required_message ??
              "This condition measures spend against your organization's AI budget, and no budget is configured."}{" "}
            <Link to="/settings#budgets">Set a budget →</Link>
          </p>
        )}

        <div className="settings-field">
          <label htmlFor="a-window">Evaluation window</label>
          <select
            id="a-window"
            value={form.window}
            onChange={(e) => patch({ window: e.target.value })}
          >
            {Object.entries(WINDOW_LABELS).map(([v, l]) => (
              <option key={v} value={v}>
                {l}
              </option>
            ))}
          </select>
          <span className="settings-hint muted">
            Windows reset on calendar boundaries in {timezone}.
          </span>
        </div>

        <div className="settings-field">
          <label htmlFor="a-cooldown">Cooldown</label>
          <select
            id="a-cooldown"
            value={form.cooldown}
            onChange={(e) => patch({ cooldown: e.target.value })}
          >
            {Object.entries(COOLDOWN_LABELS).map(([v, l]) => (
              <option key={v} value={v}>
                {l}
              </option>
            ))}
          </select>
        </div>
      </div>

      <div className="settings-card">
        <h2>Notification channels</h2>
        {(["in_app", "email", "slack", "webhook"] as const).map((ch) => (
          <div key={ch} className="channel-row">
            <label className="toggle">
              <input
                type="checkbox"
                checked={selected(ch)}
                onChange={(e) => toggleChannel(ch, e.target.checked)}
              />
              <span>{CHANNEL_LABELS[ch]}</span>
            </label>
            {selected(ch) && ch === "email" && (
              <input
                className="channel-input"
                type="email"
                placeholder="you@company.com"
                value={form.channels.find((c) => c.channel === "email")?.target ?? ""}
                onChange={(e) => patchChannel("email", { target: e.target.value })}
                aria-label="Email recipient"
              />
            )}
            {selected(ch) && (ch === "slack" || ch === "webhook") && (
              <input
                className="channel-input"
                type="url"
                placeholder={
                  ch === "slack" ? "https://hooks.slack.com/services/…" : "https://your-endpoint/…"
                }
                value={form.channels.find((c) => c.channel === ch)?.target ?? ""}
                onChange={(e) => patchChannel(ch, { target: e.target.value })}
                aria-label={`${CHANNEL_LABELS[ch]} URL`}
              />
            )}
          </div>
        ))}
        <p className="settings-hint muted">
          Slack/webhook URLs are stored encrypted and never shown again — re-enter to change them.
        </p>
      </div>

      <div className="settings-card">
        <div className="settings-field settings-field-inline">
          <label className="toggle">
            <input
              type="checkbox"
              checked={form.recovery_notify}
              onChange={(e) => patch({ recovery_notify: e.target.checked })}
            />
            <span>Send a recovery notification when it returns to normal</span>
          </label>
        </div>
        <div className="settings-field settings-field-inline">
          <label className="toggle">
            <input
              type="checkbox"
              checked={form.enabled}
              onChange={(e) => patch({ enabled: e.target.checked })}
            />
            <span>Enabled</span>
          </label>
        </div>
        <div className="settings-actions">
          <button
            onClick={save}
            disabled={saving || !form.name.trim() || form.channels.length === 0 || blockedOnBudget}
          >
            {saving ? "Saving…" : editing ? "Save changes" : "Create alert"}
          </button>
          <Link to="/alerts" className="link">
            Cancel
          </Link>
        </div>
      </div>
    </div>
  );
}
