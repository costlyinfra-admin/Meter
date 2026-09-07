/**
 * Automatic discovery — the opt-in nightly re-run, and what it has covered.
 *
 * Off by default, and deliberately so: a run reads the customer's GitHub (their
 * rate limit), calls their own LLM when BYOK is configured (their money), and
 * can raise feature proposals a person then has to review. None of that should
 * start happening because someone opened a page. So the card states the cost
 * plainly next to the switch, and shows what discovery has actually covered so
 * the choice can be made against a real picture rather than a guess.
 */
import { useEffect, useState } from "react";
import { api, ApiError, type DiscoveryCoverage, type DiscoverySchedule as Schedule } from "../api";

/** Lookbacks offered for an automatic run. Short on purpose — an incremental run
 *  only has to reach past the previous one. */
const LOOKBACKS = [
  { days: 7, label: "7 days" },
  { days: 14, label: "14 days" },
  { days: 30, label: "30 days" },
  { days: 90, label: "90 days" },
];

/**
 * A date, in words.
 *
 * Coverage is recorded as plain calendar dates; a timestamp is a real instant.
 * A bare "2025-06-01" parses as UTC midnight, so formatting it in the browser's
 * zone renders it as 31 May anywhere west of Greenwich — a day out, on the exact
 * figure the reader is here to check. Date-only strings are therefore read in
 * UTC; instants are shown in local time, which is what they mean.
 */
function day(iso: string): string {
  const dateOnly = iso.length === 10;
  return new Date(dateOnly ? `${iso}T00:00:00Z` : iso).toLocaleDateString("en-US", {
    day: "numeric",
    month: "short",
    year: "numeric",
    ...(dateOnly ? { timeZone: "UTC" } : {}),
  });
}

export function DiscoverySchedule() {
  const [schedule, setSchedule] = useState<Schedule | null>(null);
  const [coverage, setCoverage] = useState<DiscoveryCoverage | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api
      .discoverySchedule()
      .then(setSchedule)
      .catch(() => setSchedule(null));
    api
      .discoveryRuns()
      .then((r) => setCoverage(r.coverage))
      .catch(() => setCoverage(null));
  }, []);

  async function save(enabled: boolean, lookbackDays?: number) {
    setBusy(true);
    setError(null);
    try {
      setSchedule(await api.setDiscoverySchedule(enabled, lookbackDays));
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not save the schedule.");
    } finally {
      setBusy(false);
    }
  }

  if (!schedule) return null;

  return (
    <section className="settings-card discovery-schedule">
      <h2>Keeping discovery up to date</h2>

      {coverage && coverage.runs > 0 ? (
        <p className="muted settings-lead">
          Discovery has covered {day(coverage.covered_from!)} – {day(coverage.covered_to!)} across{" "}
          {coverage.runs} run{coverage.runs === 1 ? "" : "s"}
          {coverage.last_run_at ? `, most recently on ${day(coverage.last_run_at)}` : ""}
          {coverage.last_run_status === "error" ? " — which failed" : ""}.
        </p>
      ) : (
        <p className="muted settings-lead">
          Discovery has not completed a run yet. Run it above, then it can be kept up to date
          automatically.
        </p>
      )}

      {error && (
        <p className="error" role="alert">
          {error}
        </p>
      )}

      <div className="settings-field settings-field-inline">
        <label htmlFor="auto-discovery">Run discovery automatically</label>
        <label className="toggle">
          <input
            id="auto-discovery"
            type="checkbox"
            checked={schedule.enabled}
            disabled={busy || !schedule.configurable}
            onChange={(e) => save(e.target.checked)}
          />
          <span>{schedule.enabled ? "On" : "Off"}</span>
        </label>
        <span className="settings-hint muted">
          {schedule.configurable
            ? "Once a night, fetching only what has been merged since the last run."
            : "Run discovery once first — an automatic run needs an owner and a repository selection."}
        </span>
      </div>

      {schedule.enabled && (
        <div className="settings-field">
          <label htmlFor="auto-lookback">Each run looks back at least</label>
          <select
            id="auto-lookback"
            value={schedule.lookback_days}
            disabled={busy}
            onChange={(e) => save(true, Number(e.target.value))}
          >
            {LOOKBACKS.map((l) => (
              <option key={l.days} value={l.days}>
                {l.label}
              </option>
            ))}
          </select>
          <span className="settings-hint muted">
            A run reaches back to whichever is earlier: this, or just past the previous run — so a
            gap after downtime is still collected.
          </span>
        </div>
      )}

      <p className="muted settings-hint discovery-cost-note">
        Each run reads merged pull requests from GitHub against your rate limit, and uses your own
        model when one is configured in Settings. It can also raise new feature proposals for
        someone to review, so nothing is turned on here by default.
        {schedule.enabled && schedule.next_run_at
          ? ` Next run: ${new Date(schedule.next_run_at).toLocaleString()}.`
          : ""}
      </p>
    </section>
  );
}
