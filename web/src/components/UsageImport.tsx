/**
 * A month of per-feature adoption, pasted in.
 *
 * Cost per user and "Worth it?" are computed from active users, and no
 * connector supplies it until product-analytics connectors land (design doc
 * §11, Slice 2). The per-feature field on a feature's own page is for one
 * correction; this is for the way the number actually arrives — an export from
 * whatever analytics tool the company already runs, for every feature at once.
 *
 * Nothing is written unless every row resolves to a feature: half a month
 * loaded would show a column the reader believes they just filled.
 */
import { useState } from "react";
import { api, ApiError } from "../api";

/** YYYY-MM for the current month, which is what a fresh export is usually of. */
function thisMonth(): string {
  const now = new Date();
  return `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, "0")}`;
}

export function UsageImport({ onImported }: { onImported?: () => void }) {
  const [open, setOpen] = useState(false);
  const [csv, setCsv] = useState("");
  const [period, setPeriod] = useState(thisMonth);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);

  const load = async () => {
    setBusy(true);
    setError(null);
    setNote(null);
    try {
      const r = await api.importUsage(csv, period);
      setNote(
        `Recorded active users for ${r.features} feature${r.features === 1 ? "" : "s"}` +
          `${r.periods.length > 1 ? ` across ${r.periods.length} months` : ""}.`,
      );
      setCsv("");
      onImported?.();
    } catch (err) {
      // The server names the row and the reason; that is the whole value of it.
      setError(err instanceof ApiError ? err.message : "Could not import that CSV.");
    } finally {
      setBusy(false);
    }
  };

  return (
    <section className="usage-import">
      <div className="panel-head">
        <h3>Active users</h3>
        <button type="button" className="link" onClick={() => setOpen((was) => !was)}>
          {open ? "Hide" : "Import a CSV"}
        </button>
      </div>
      <p className="muted">
        Cost per user and the &ldquo;Worth it?&rdquo; indicator are calculated from this. Meter has
        no connector that can read it, so it comes from your own analytics — paste a month here, or
        set it on a single feature from that feature&rsquo;s page.
      </p>

      {open && (
        <>
          <p className="method-help">
            A header row of <code>feature,active_users</code>, then one row per feature.{" "}
            <code>feature</code> is the feature&rsquo;s name as it appears above (or its id).{" "}
            <code>events</code> and <code>period</code> are optional — a <code>period</code> of{" "}
            <code>YYYY-MM</code> on a row overrides the month chosen here, so one paste can backfill
            several months.
          </p>
          <textarea
            value={csv}
            onChange={(e) => setCsv(e.target.value)}
            rows={4}
            aria-label="Usage CSV"
            placeholder={"feature,active_users\nAI threat triage,540\nReport generator,120"}
          />
          <span className="inline">
            <label htmlFor="usage-period">Month</label>
            <input
              id="usage-period"
              type="month"
              value={period}
              onChange={(e) => setPeriod(e.target.value)}
            />
            <button onClick={load} disabled={busy || !csv.trim()}>
              {busy ? "Importing…" : "Import"}
            </button>
          </span>
        </>
      )}

      {error && (
        <p className="error" role="alert">
          {error}
        </p>
      )}
      {note && <p className="muted">{note}</p>}
    </section>
  );
}
