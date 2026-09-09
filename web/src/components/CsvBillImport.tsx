/**
 * Importing a bill for a platform that publishes an invoice but no API.
 *
 * Two steps, deliberately. Choosing a file previews it — which columns were
 * matched to what, the total, the date range, and every row that was skipped
 * with the reason — and writes nothing. Only "Import" commits. These files are
 * downloaded by hand, so there is no schedule to hurry, and an import that
 * surprises someone is an import that should have been previewed.
 *
 * The column matching is best-effort by design (a bill can call the cost column
 * anything), so the preview is also where it is corrected: every matched column
 * is a dropdown over the file's real headers. Fixing a wrong guess should not
 * mean editing the file and starting again.
 */
import { useRef, useState } from "react";
import { api, ApiError, type InfraImportReport, type InfraProvider } from "../api";
import { money } from "../format";

/** The meanings we map, in the order they matter to someone checking them. */
const MEANINGS: { key: string; label: string; required?: boolean }[] = [
  { key: "date", label: "Date", required: true },
  { key: "amount", label: "Amount", required: true },
  { key: "service", label: "Service" },
  { key: "tag", label: "Feature tag" },
  { key: "usage_type", label: "Usage type" },
  { key: "currency", label: "Currency" },
  { key: "region", label: "Region" },
  { key: "account", label: "Account" },
];

/** Every column header in the uploaded file, for the correction dropdowns. */
function headersOf(csv: string): string[] {
  const first = csv.split(/\r?\n/, 1)[0] ?? "";
  if (!first.trim()) return [];
  // Good enough for a header row: quoted headers containing commas are rare,
  // and a wrong split here only affects which options are offered, never what
  // is parsed — the backend does the real reading.
  return first
    .split(",")
    .map((h) => h.trim().replace(/^"|"$/g, ""))
    .filter(Boolean);
}

export function CsvBillImport({
  provider,
  onImported,
}: {
  provider: InfraProvider;
  onImported: () => void;
}) {
  const [csv, setCsv] = useState<string | null>(null);
  const [fileName, setFileName] = useState<string | null>(null);
  const [report, setReport] = useState<InfraImportReport | null>(null);
  const [mapping, setMapping] = useState<Record<string, string>>({});
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [done, setDone] = useState<string | null>(null);
  const fileInput = useRef<HTMLInputElement>(null);

  const headers = csv ? headersOf(csv) : [];

  async function preview(text: string, override: Record<string, string>) {
    setBusy(true);
    setError(null);
    try {
      const next = await api.importInfrastructureCsv(provider.type, text, {
        dryRun: true,
        mapping: Object.keys(override).length ? override : undefined,
      });
      setReport(next);
      setMapping(next.mapping);
    } catch (err) {
      setReport(null);
      setError(err instanceof ApiError ? err.message : "Could not read that file.");
    } finally {
      setBusy(false);
    }
  }

  async function onFile(file: File) {
    setDone(null);
    setFileName(file.name);
    const text = await file.text();
    setCsv(text);
    setMapping({});
    await preview(text, {});
  }

  async function remap(meaning: string, header: string) {
    const next = { ...mapping, [meaning]: header };
    if (!header) delete next[meaning];
    setMapping(next);
    if (csv) await preview(csv, next);
  }

  async function commit() {
    if (!csv) return;
    setBusy(true);
    setError(null);
    try {
      const result = await api.importInfrastructureCsv(provider.type, csv, { mapping });
      setDone(
        `Imported ${result.items ?? result.rows_imported} line ` +
          `${(result.items ?? result.rows_imported) === 1 ? "item" : "items"}: ` +
          `${money(result.infrastructure ?? result.total)} of infrastructure cost` +
          (result.from ? `, ${result.from} to ${result.to}.` : "."),
      );
      setCsv(null);
      setReport(null);
      setFileName(null);
      if (fileInput.current) fileInput.current.value = "";
      onImported();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Import failed.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="csv-import">
      <p className="muted">{provider.note}</p>
      <p className="muted csv-import-why">
        {provider.name} publishes an invoice you can download but no cost API to read it from.
        Rather than estimate your spend from a price list, we read the real numbers out of the file
        — so download the bill and drop it here.
      </p>

      <label className="csv-import-file">
        <span className="csv-import-file-label">
          {fileName ?? "Choose the downloaded bill (.csv)"}
        </span>
        <input
          ref={fileInput}
          type="file"
          accept=".csv,text/csv"
          aria-label={`${provider.name} bill CSV`}
          onChange={(e) => {
            const file = e.target.files?.[0];
            if (file) onFile(file);
          }}
        />
      </label>

      {error && (
        <p className="error" role="alert">
          {error}
        </p>
      )}
      {done && <p className="muted csv-import-done">{done}</p>}

      {report && (
        <div className="csv-import-preview">
          <p className="csv-import-headline">
            Found <strong>{report.rows_imported}</strong> line{" "}
            {report.rows_imported === 1 ? "item" : "items"} totalling{" "}
            <strong>{money(report.total)}</strong>
            {report.currency !== "USD" && report.currency !== "mixed" && ` ${report.currency}`}
            {report.from && (
              <>
                , {report.from} to {report.to}
              </>
            )}
            . Nothing has been imported yet.
          </p>

          <table className="data-table csv-import-mapping">
            <thead>
              <tr>
                <th scope="col">We read</th>
                <th scope="col">From column</th>
              </tr>
            </thead>
            <tbody>
              {MEANINGS.map((m) => (
                <tr key={m.key}>
                  <td>
                    {m.label}
                    {m.required && <span className="csv-import-required"> (required)</span>}
                  </td>
                  <td>
                    <select
                      aria-label={`Column for ${m.label}`}
                      value={mapping[m.key] ?? ""}
                      disabled={busy}
                      onChange={(e) => remap(m.key, e.target.value)}
                    >
                      <option value="">— not used —</option>
                      {headers.map((h) => (
                        <option key={h} value={h}>
                          {h}
                        </option>
                      ))}
                    </select>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>

          {report.warnings.length > 0 && (
            <ul className="csv-import-warnings">
              {report.warnings.map((w) => (
                <li key={w}>{w}</li>
              ))}
            </ul>
          )}

          <div className="csv-import-actions">
            <button onClick={commit} disabled={busy || report.rows_imported === 0}>
              {busy ? "Working…" : `Import ${report.rows_imported} line items`}
            </button>
            <button
              className="secondary"
              disabled={busy}
              onClick={() => {
                setCsv(null);
                setReport(null);
                setFileName(null);
                if (fileInput.current) fileInput.current.value = "";
              }}
            >
              Cancel
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
