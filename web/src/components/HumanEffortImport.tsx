/**
 * Loading a month of timesheet data onto the Customer economics screen.
 *
 * Meter cannot fetch this. There is no connector for what a person spent their
 * Tuesday on, and inferring it from commits would turn a stated fact into a
 * guess about somebody's salary. So this is a file the tenant exports from
 * wherever they already track time, and the panel's job is to be honest about
 * the shape it has to be in — the required columns are listed and an example is
 * one click from the clipboard, because the alternative is a rejected file and
 * a person guessing at the header row.
 *
 * Errors come back naming the row and the reason, and are shown verbatim.
 */
import { useRef, useState } from "react";
import { api, ApiError, type EffortImportResult } from "../api";
import { money, num } from "../format";

const EXAMPLE = `date,person,customer_id,feature_id,hours,activity_type,loaded_hourly_rate,note
2026-09-15,Aman,northwind-financial,,3.5,development,110,Customer workflow changes
2026-09-15,Alessio,vertex-health,,2.0,support,125,Deployment support`;

export function HumanEffortImport({ onImported }: { onImported: () => void }) {
  const [open, setOpen] = useState(false);
  const [csv, setCsv] = useState("");
  const [fileName, setFileName] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<EffortImportResult | null>(null);
  const [copied, setCopied] = useState(false);
  const fileInput = useRef<HTMLInputElement>(null);

  const choose = async (file: File | undefined) => {
    if (!file) return;
    setError(null);
    setResult(null);
    setFileName(file.name);
    setCsv(await file.text());
  };

  const load = async () => {
    setBusy(true);
    setError(null);
    try {
      const r = await api.importHumanEffort(csv);
      setResult(r);
      setCsv("");
      setFileName(null);
      if (fileInput.current) fileInput.current.value = "";
      onImported();
    } catch (err) {
      // The server names the row and the reason; that is the whole value of it.
      setError(err instanceof ApiError ? err.message : "Could not import that file.");
    } finally {
      setBusy(false);
    }
  };

  if (!open) {
    return (
      <button type="button" className="effort-open" onClick={() => setOpen(true)}>
        Add human effort
      </button>
    );
  }

  return (
    <section className="effort-import" aria-label="Add human effort">
      <div className="panel-head">
        <h3>Add human effort</h3>
        <button type="button" className="link" onClick={() => setOpen(false)}>
          Close
        </button>
      </div>
      <p className="method-help">
        Hours worked for a customer, with the loaded hourly rate that prices them. Meter has no way
        to fetch this — export it from wherever you track time. Required columns: <code>date</code>,{" "}
        <code>person</code>, <code>customer_id</code>, <code>hours</code>,{" "}
        <code>activity_type</code>, <code>loaded_hourly_rate</code>. Optional:{" "}
        <code>feature_id</code>, <code>note</code>. Dates are <code>YYYY-MM-DD</code>, and{" "}
        <code>activity_type</code> is one of development, support, review, rework, other.
      </p>

      <pre className="effort-example">{EXAMPLE}</pre>
      <span className="inline">
        <button
          type="button"
          className="link"
          onClick={() => {
            navigator.clipboard?.writeText(EXAMPLE).then(
              () => setCopied(true),
              () => setCopied(false),
            );
          }}
        >
          {copied ? "Example copied" : "Copy example"}
        </button>
      </span>

      <div className="inline effort-pick">
        <input
          ref={fileInput}
          type="file"
          accept=".csv,text/csv"
          aria-label="Effort CSV file"
          onChange={(e) => choose(e.target.files?.[0])}
        />
        <button onClick={load} disabled={busy || !csv.trim()}>
          {busy ? "Importing…" : "Import"}
        </button>
      </div>
      {fileName && !result && (
        <p className="muted">
          {fileName} — ready to import. Nothing is stored until you choose Import.
        </p>
      )}

      {error && (
        <p className="error" role="alert">
          {error}
        </p>
      )}
      {result && (
        <p className="muted" role="status">
          Imported {num(result.imported)} row{result.imported === 1 ? "" : "s"} for{" "}
          {num(result.customers)} customer{result.customers === 1 ? "" : "s"} —{" "}
          {num(Math.round(result.hours))} hours, {money(result.cost)}, covering {result.first_date}{" "}
          to {result.last_date}.
        </p>
      )}
    </section>
  );
}
