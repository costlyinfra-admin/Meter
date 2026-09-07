/**
 * Provider invoice reconciliation — the module's screens.
 *
 * Four views behind one route: the summary of past runs, the import workflow,
 * one run in detail, and the import history. Nothing here is imported by any
 * existing page, and every request it makes returns 404 unless this
 * organization has switched the module on — so a disabled module renders
 * nothing and asks for nothing.
 */
import { useCallback, useEffect, useMemo, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import {
  api,
  ApiError,
  type ReconImport,
  type ReconPreview,
  type ReconRun,
  type ReconSettings,
} from "../api";
import { money } from "../format";

const STATUS_LABEL: Record<string, string> = {
  pending: "Calculating",
  matched: "Matched",
  within_tolerance: "Within tolerance",
  discrepancy: "Discrepancy",
  incomplete_data: "Incomplete data",
  failed: "Failed",
};

/** Statuses that need a person to look, versus statuses that do not. */
const STATUS_TONE: Record<string, string> = {
  matched: "good",
  within_tolerance: "good",
  discrepancy: "error",
  incomplete_data: "warn",
  failed: "error",
  pending: "warn",
};

const CONFIDENCE_LABEL: Record<string, string> = {
  confirmed: "Confirmed",
  possible: "Possible",
  unknown: "Unknown",
};

/** Classifications, in the words a CFO would use. */
const CLASSIFICATION_LABEL: Record<string, string> = {
  matched: "Matched",
  provider_usage_missing_from_meter: "Billed, not tracked",
  meter_usage_absent_from_statement: "Tracked, not billed",
  pricing_version_mismatch: "Price difference",
  currency_mismatch: "Currency mismatch",
  billing_period_boundary: "Outside the period",
  duplicate_provider_row: "Duplicate statement row",
  unknown_model_mapping: "Unrecognised model",
  unattributed_provider_workspace: "Unconnected workspace",
  unsupported_line_item_type: "Unrecognised line type",
  incomplete_meter_data: "No tracked data",
  incomplete_provider_export: "Incomplete export",
  provider_tax: "Tax",
  provider_credit: "Credit or discount",
  provider_fee: "Fee",
  provider_adjustment: "Adjustment",
  unexplained_difference: "Unexplained",
};

function label(map: Record<string, string>, key: string): string {
  return map[key] ?? key.replace(/_/g, " ");
}

function StatusBadge({ status }: { status: string }) {
  return (
    <span className={`badge recon-status recon-${STATUS_TONE[status] ?? "warn"}`}>
      {label(STATUS_LABEL, status)}
    </span>
  );
}

function pctText(value: number | null): string {
  return value === null ? "—" : `${value >= 0 ? "" : ""}${value.toFixed(2)}%`;
}

// ---------------------------------------------------------------------------

export function ReconciliationPage() {
  const { view, runId } = useParams();
  const [settings, setSettings] = useState<ReconSettings | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api
      .reconSettings()
      .then(setSettings)
      .catch(() => setError("Could not load reconciliation settings."));
  }, []);

  if (error) {
    return (
      <div className="content">
        <p className="error" role="alert">
          {error}
        </p>
      </div>
    );
  }
  if (!settings) return <div className="content muted">Loading…</div>;
  if (!settings.enabled) return <NotEnabled settings={settings} onChange={setSettings} />;

  return (
    <div className="content">
      <div className="dash-head">
        <h1>Reconciliation</h1>
      </div>
      <p className="muted recon-intro">
        Compare an official provider billing export against the spend Meter tracked, and see
        what explains the difference. Nothing here changes your cost data — a statement is evidence,
        not a correction.
      </p>

      <div className="tabs" role="tablist" aria-label="Reconciliation views">
        <TabLink to="/reconciliation" active={!view}>
          Summary
        </TabLink>
        <TabLink to="/reconciliation/import" active={view === "import"}>
          Import a statement
        </TabLink>
        <TabLink to="/reconciliation/history" active={view === "history"}>
          Import history
        </TabLink>
      </div>

      {/* runId is matched by its own route, so it is checked before `view`. */}
      {runId ? (
        <RunDetail runId={runId} />
      ) : view === "import" ? (
        <ImportWorkflow settings={settings} />
      ) : view === "history" ? (
        <ImportHistory />
      ) : (
        <Summary />
      )}
    </div>
  );
}

function TabLink({ to, active, children }: { to: string; active: boolean; children: string }) {
  return (
    <Link to={to} role="tab" aria-selected={active} className={active ? "tab active" : "tab"}>
      {children}
    </Link>
  );
}

/** The module exists but this organization has not turned it on. */
function NotEnabled({
  settings,
  onChange,
}: {
  settings: ReconSettings;
  onChange: (s: ReconSettings) => void;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function enable() {
    setBusy(true);
    setError(null);
    try {
      onChange(await api.saveReconSettings({ enabled: true }));
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not enable reconciliation.");
      setBusy(false);
    }
  }

  return (
    <div className="content">
      <div className="dash-head">
        <h1>Reconciliation</h1>
      </div>
      <div className="source-section recon-empty">
        <h2>Compare your provider bill against what Meter tracked</h2>
        <p className="muted">
          Import an official billing export and Meter will line it up against the spend it
          already has — usage against usage, with tax, credits and fees kept separate — and explain
          what differs.
        </p>
        <p className="muted">
          This is off by default and entirely additive: turning it on adds a section and changes
          nothing about your existing cost data, dashboards or alerts. Turning it off again hides it
          without deleting anything.
        </p>
        {!settings.available && (
          <p className="hint">Reconciliation is disabled for this installation.</p>
        )}
        {error && (
          <p className="error" role="alert">
            {error}
          </p>
        )}
        <button onClick={enable} disabled={busy || !settings.available}>
          {busy ? "Enabling…" : "Enable reconciliation"}
        </button>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// A. Summary
// ---------------------------------------------------------------------------
function Summary() {
  const [runs, setRuns] = useState<ReconRun[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api
      .reconRuns()
      .then(setRuns)
      .catch(() => setError("Could not load reconciliation runs."));
  }, []);

  if (error) return <p className="error">{error}</p>;
  if (!runs) return <p className="muted">Loading…</p>;
  if (runs.length === 0) {
    return (
      <div className="source-section recon-empty">
        <h2>No statement reconciled yet</h2>
        <p className="muted">
          Import a provider billing export to see how it compares with the spend Meter tracked
          for the same period.
        </p>
        <Link className="button-link" to="/reconciliation/import">
          Import a statement
        </Link>
      </div>
    );
  }

  return (
    <section className="source-section">
      <div className="kb-table-wrap">
        <table className="features-table recon-table">
          <thead>
            <tr>
              <th>Period</th>
              <th>Provider</th>
              <th>Account</th>
              <th className="num">Provider usage</th>
              <th className="num">Meter tracked</th>
              <th className="num">Difference</th>
              <th className="num">%</th>
              <th>Status</th>
              <th className="num">Unmatched</th>
              <th>Calculated</th>
            </tr>
          </thead>
          <tbody>
            {runs.map((run) => (
              <tr key={run.id}>
                <td>
                  <Link to={`/reconciliation/runs/${run.id}`}>
                    {run.period_start} → {run.period_end}
                  </Link>
                </td>
                <td>{run.provider}</td>
                <td className="muted">{run.provider_account || "—"}</td>
                <td className="num">{money(run.provider_usage)}</td>
                <td className="num">{money(run.tracked_usage)}</td>
                <td className="num">{money(run.usage_difference)}</td>
                <td className="num">{pctText(run.usage_difference_pct)}</td>
                <td>
                  <StatusBadge status={run.status} />
                </td>
                <td className="num">
                  {run.unmatched_provider_count + run.unmatched_tracked_count}
                </td>
                <td className="muted">
                  {(run.completed_at ?? run.created_at ?? "").slice(0, 16).replace("T", " ")}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}

// ---------------------------------------------------------------------------
// B. Import workflow
// ---------------------------------------------------------------------------
function ImportWorkflow({ settings }: { settings: ReconSettings }) {
  const navigate = useNavigate();
  const providers = settings.providers ?? ["anthropic"];
  const [provider, setProvider] = useState(providers[0]);
  const [filename, setFilename] = useState("");
  const [content, setContent] = useState("");
  const [preview, setPreview] = useState<ReconPreview | null>(null);
  const [mapping, setMapping] = useState<Record<string, string | null>>({});
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const refresh = useCallback(async (text: string, chosen: Record<string, string | null>) => {
    setError(null);
    try {
      const result = await api.reconPreview(text, chosen);
      setPreview(result);
      setMapping(result.mapping);
    } catch (err) {
      setPreview(null);
      setError(err instanceof ApiError ? err.message : "Could not read that file.");
    }
  }, []);

  async function onFile(file: File | undefined) {
    if (!file) return;
    setFilename(file.name);
    const text = await file.text();
    setContent(text);
    await refresh(text, {});
  }

  function remap(field: string, column: string) {
    const next = { ...mapping, [field]: column || null };
    setMapping(next);
    void refresh(content, next);
  }

  async function commit() {
    setBusy(true);
    setError(null);
    try {
      const created = await api.reconImport({
        provider,
        filename: filename || "statement.csv",
        content,
        mapping,
      });
      const run = await api.runReconciliation(created.id);
      navigate(`/reconciliation/runs/${run.id}`);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not import that statement.");
      setBusy(false);
    }
  }

  return (
    <section className="source-section">
      <h2>Import a billing statement</h2>
      <p className="muted">
        Upload the CSV your provider gives you. Nothing is stored until you confirm, and only the
        columns you map are kept — the rest of the file is ignored rather than saved.
      </p>

      <div className="recon-upload">
        <label>
          Provider
          <select value={provider} onChange={(e) => setProvider(e.target.value)}>
            {providers.map((p) => (
              <option key={p} value={p}>
                {p}
              </option>
            ))}
          </select>
        </label>
        <label>
          Billing export (CSV)
          <input
            type="file"
            accept=".csv,text/csv"
            onChange={(e) => void onFile(e.target.files?.[0])}
          />
        </label>
      </div>

      {error && (
        <p className="error" role="alert">
          {error}
        </p>
      )}

      {preview && (
        <>
          <h3>Columns</h3>
          <p className="muted">
            Meter guessed these from your headers. Change anything it got wrong — a mapping is a
            statement about your file, not about the provider.
          </p>
          <div className="recon-mapping">
            {Object.entries(preview.field_help).map(([field, help]) => (
              <label
                key={field}
                className={preview.missing_required.includes(field) ? "missing" : ""}
              >
                <span className="recon-field">{field.replace(/_/g, " ")}</span>
                <select value={mapping[field] ?? ""} onChange={(e) => remap(field, e.target.value)}>
                  <option value="">— not in this file —</option>
                  {preview.headers.map((header) => (
                    <option key={header} value={header}>
                      {header}
                    </option>
                  ))}
                </select>
                <span className="muted recon-help">{help}</span>
              </label>
            ))}
          </div>

          {preview.missing_required.length > 0 && (
            <p className="hint">
              Map {preview.missing_required.join(" and ")} before importing — without them a line
              cannot be reconciled.
            </p>
          )}

          <h3>What would be imported</h3>
          <div className="recon-figures">
            <Figure label="Rows" value={String(preview.row_count)} />
            <Figure label="Readable" value={String(preview.accepted_count)} />
            <Figure label="Rejected" value={String(preview.rejected_count)} />
            <Figure label="Usage subtotal" value={money(preview.usage_subtotal)} />
            <Figure label="Credits" value={money(preview.credits)} />
            <Figure label="Tax" value={money(preview.tax)} />
            <Figure label="Fees" value={money(preview.fees)} />
            <Figure label="Invoice total" value={money(preview.billed_total)} />
            <Figure
              label="Period"
              value={preview.period_start ? `${preview.period_start} → ${preview.period_end}` : "—"}
            />
            <Figure label="Currency" value={preview.currencies.join(", ") || "—"} />
          </div>

          {preview.rejected_rows.length > 0 && (
            <>
              <h3>Rows that cannot be read</h3>
              <p className="muted">
                These are left out of the comparison. Everything else still imports.
              </p>
              <ul className="recon-errors">
                {preview.rejected_rows.map((row) => (
                  <li key={row.row_number}>
                    Row {row.row_number}: {row.errors.join(", ")}
                  </li>
                ))}
              </ul>
            </>
          )}

          <PreviewTable rows={preview.rows} />

          <div className="recon-actions">
            <button onClick={commit} disabled={busy || preview.missing_required.length > 0}>
              {busy ? "Importing…" : "Import and reconcile"}
            </button>
          </div>
        </>
      )}
    </section>
  );
}

function Figure({ label: name, value }: { label: string; value: string }) {
  return (
    <div className="recon-figure">
      <span className="recon-figure-label">{name}</span>
      <span className="recon-figure-value">{value}</span>
    </div>
  );
}

function PreviewTable({ rows }: { rows: ReconPreview["rows"] }) {
  if (rows.length === 0) return null;
  return (
    <div className="kb-table-wrap">
      <table className="features-table recon-table">
        <thead>
          <tr>
            <th>#</th>
            <th>Date</th>
            <th>Model</th>
            <th>Category</th>
            <th className="num">Usage</th>
            <th className="num">Credit</th>
            <th className="num">Tax</th>
            <th className="num">Fee</th>
            <th className="num">Billed</th>
            <th>Currency</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={row.row_number} className={row.status === "ok" ? "" : "recon-rejected"}>
              <td className="muted">{row.row_number}</td>
              <td>{row.service_date ?? "—"}</td>
              <td>{row.model || "—"}</td>
              <td>{row.usage_category || "—"}</td>
              <td className="num">{money(row.usage_subtotal)}</td>
              <td className="num">{money(row.credit)}</td>
              <td className="num">{money(row.tax)}</td>
              <td className="num">{money(row.fee)}</td>
              <td className="num">{money(row.billed_amount)}</td>
              <td>{row.currency}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

// ---------------------------------------------------------------------------
// C. Run detail
// ---------------------------------------------------------------------------
function RunDetail({ runId }: { runId: string }) {
  const navigate = useNavigate();
  const [run, setRun] = useState<ReconRun | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [filter, setFilter] = useState("all");
  const [busy, setBusy] = useState(false);

  const load = useCallback(() => {
    api
      .reconRun(runId)
      .then(setRun)
      .catch(() => setError("That reconciliation run is not available."));
  }, [runId]);

  useEffect(load, [load]);

  const matches = useMemo(() => {
    const all = run?.matches ?? [];
    if (filter === "all") return all;
    if (filter === "differences") return all.filter((m) => m.difference !== 0);
    return all.filter((m) => m.classification === filter);
  }, [run, filter]);

  const classifications = useMemo(
    () => [...new Set((run?.matches ?? []).map((m) => m.classification))].sort(),
    [run],
  );

  if (error) return <p className="error">{error}</p>;
  if (!run) return <p className="muted">Loading…</p>;

  async function recalculate() {
    if (!run?.import_id) return;
    setBusy(true);
    try {
      const next = await api.runReconciliation(run.import_id);
      // A new run, never an edit of this one — so the old answer stays readable.
      navigate(`/reconciliation/runs/${next.id}`);
    } catch {
      setError("Could not recalculate.");
      setBusy(false);
    }
  }

  return (
    <section className="source-section">
      <div className="recon-detail-head">
        <div>
          <h2>
            {run.provider} · {run.period_start} → {run.period_end}
          </h2>
          <p className="muted">
            <StatusBadge status={run.status} /> tolerance {money(run.tolerance_abs)} or{" "}
            {run.tolerance_pct}% · calculated{" "}
            {(run.completed_at ?? run.created_at ?? "").slice(0, 16).replace("T", " ")}
            {run.created_by ? ` by ${run.created_by}` : ""}
          </p>
        </div>
        <div className="recon-actions">
          <a className="secondary button-link" href={api.reconReportUrl(run.id)}>
            Export report
          </a>
          <button className="secondary" onClick={recalculate} disabled={busy}>
            {busy ? "Recalculating…" : "Recalculate"}
          </button>
        </div>
      </div>

      {run.failure_reason && (
        <p className="error" role="alert">
          This run failed: {run.failure_reason}
        </p>
      )}

      <div className="recon-figures">
        <Figure label="Provider usage subtotal" value={money(run.provider_usage)} />
        <Figure label="Meter tracked usage" value={money(run.tracked_usage)} />
        <Figure label="Difference" value={money(run.usage_difference)} />
        <Figure label="Difference %" value={pctText(run.usage_difference_pct)} />
        <Figure label="Credits and discounts" value={money(run.provider_credits)} />
        <Figure label="Tax" value={money(run.provider_tax)} />
        <Figure label="Fees and adjustments" value={money(run.provider_fees)} />
        <Figure label="Provider invoice total" value={money(run.provider_total)} />
      </div>
      <p className="muted recon-note">
        The comparison is usage against usage. Tax, credits and fees are shown because they are on
        the invoice, and excluded because they are not usage.
      </p>

      {run.breakdown && (
        <div className="recon-breakdowns">
          {Object.entries(run.breakdown).map(([key, rows]) => (
            <div key={key} className="recon-breakdown">
              <h3>{key.replace("by_", "By ")}</h3>
              <ul>
                {rows.slice(0, 8).map((row) => (
                  <li key={row.key}>
                    <span>{row.key}</span>
                    <span className="num">{money(row.usage)}</span>
                  </li>
                ))}
              </ul>
            </div>
          ))}
        </div>
      )}

      <div className="recon-filter">
        <label>
          Show
          <select value={filter} onChange={(e) => setFilter(e.target.value)}>
            <option value="all">Everything ({run.matches?.length ?? 0})</option>
            <option value="differences">Only differences</option>
            {classifications.map((c) => (
              <option key={c} value={c}>
                {label(CLASSIFICATION_LABEL, c)}
              </option>
            ))}
          </select>
        </label>
      </div>

      <div className="kb-table-wrap">
        <table className="features-table recon-table">
          <thead>
            <tr>
              <th>Classification</th>
              <th className="num">Provider</th>
              <th className="num">Meter</th>
              <th className="num">Difference</th>
              <th>Confidence</th>
              <th>Explanation and evidence</th>
            </tr>
          </thead>
          <tbody>
            {matches.map((match, i) => (
              <tr key={i}>
                <td>{label(CLASSIFICATION_LABEL, match.classification)}</td>
                <td className="num">{money(match.provider_amount)}</td>
                <td className="num">{money(match.tracked_amount)}</td>
                <td className="num">{money(match.difference)}</td>
                <td>
                  <span className={`badge recon-confidence recon-${match.confidence}`}>
                    {label(CONFIDENCE_LABEL, match.confidence)}
                  </span>
                </td>
                <td>
                  {match.explanation}
                  {match.evidence?.length > 0 && (
                    <ul className="recon-evidence">
                      {match.evidence.map((line, j) => (
                        <li key={j}>{line}</li>
                      ))}
                    </ul>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        {matches.length === 0 && <p className="muted">Nothing matches that filter.</p>}
      </div>
    </section>
  );
}

// ---------------------------------------------------------------------------
// D. Import history
// ---------------------------------------------------------------------------
function ImportHistory() {
  const [rows, setRows] = useState<ReconImport[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(() => {
    api
      .reconImports()
      .then(setRows)
      .catch(() => setError("Could not load import history."));
  }, []);

  useEffect(load, [load]);

  async function remove(row: ReconImport) {
    if (
      !window.confirm(
        `Remove ${row.filename}? Its reconciliation runs stay readable and the file's rows are ` +
          "kept, but it will no longer be used for new calculations.",
      )
    ) {
      return;
    }
    try {
      await api.removeReconImport(row.id);
      load();
    } catch {
      setError("Could not remove that import.");
    }
  }

  if (error) return <p className="error">{error}</p>;
  if (!rows) return <p className="muted">Loading…</p>;
  if (rows.length === 0) return <p className="muted">No statement has been imported yet.</p>;

  return (
    <section className="source-section">
      <div className="kb-table-wrap">
        <table className="features-table recon-table">
          <thead>
            <tr>
              <th>File</th>
              <th>Provider</th>
              <th>Period</th>
              <th className="num">Rows</th>
              <th className="num">Rejected</th>
              <th>Imported by</th>
              <th>Imported</th>
              <th>Status</th>
              <th className="num">Runs</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <tr key={row.id} className={row.status === "committed" ? "" : "recon-rejected"}>
                <td>{row.filename}</td>
                <td>{row.provider}</td>
                <td>
                  {row.period_start} → {row.period_end}
                </td>
                <td className="num">{row.row_count}</td>
                <td className="num">{row.rejected_count}</td>
                <td className="muted">{row.imported_by ?? "—"}</td>
                <td className="muted">{(row.imported_at ?? "").slice(0, 16).replace("T", " ")}</td>
                <td>{row.status}</td>
                <td className="num">{row.run_count}</td>
                <td>
                  {row.status === "committed" && (
                    <button className="link" onClick={() => remove(row)}>
                      Remove
                    </button>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}
