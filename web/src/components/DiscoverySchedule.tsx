/**
 * Automatic discovery, as one line: when discovery last ran, and the nightly
 * switch. It sits directly above the feature list's own controls, because "is
 * this list current?" is the question someone has when they look at the list.
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

  // Everything below the one line is about turning it ON: the lookback it will
  // use, and what a run costs the customer. Off is the default and the common
  // case, so that state stays a single row.
  return (
    <section className="discovery-schedule-line">
      <span className="muted discovery-last-run">
        {coverage?.last_run_at
          ? `Last updated on ${day(coverage.last_run_at)}`
          : "Discovery has not run yet"}
        {coverage?.last_run_status === "error" ? " — the last run failed" : ""}
      </span>

      <label className="toggle discovery-auto-toggle" htmlFor="auto-discovery">
        <span>Run discovery automatically (nightly)</span>
        <input
          id="auto-discovery"
          type="checkbox"
          checked={schedule.enabled}
          disabled={busy || !schedule.configurable}
          onChange={(e) => save(e.target.checked)}
          // The cost is stated on the control itself rather than in a paragraph
          // nobody reads twice: a run spends the customer's GitHub rate limit
          // and, with BYOK, their model budget.
          title={
            schedule.configurable
              ? "Each run reads merged pull requests against your GitHub rate limit, uses your own model when one is configured, and can raise proposals to review."
              : "Run discovery once first — an automatic run needs an owner and a repository selection."
          }
        />
        <span>{schedule.enabled ? "On" : "Off"}</span>
      </label>

      {error && (
        <p className="error discovery-schedule-error" role="alert">
          {error}
        </p>
      )}

      {schedule.enabled && (
        <span className="discovery-schedule-detail muted">
          <label htmlFor="auto-lookback">Looks back at least</label>
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
          <span>
            Each run reads merged pull requests against your GitHub rate limit and uses your own
            model when one is configured.
            {schedule.next_run_at
              ? ` Next run ${new Date(schedule.next_run_at).toLocaleString()}.`
              : ""}
          </span>
        </span>
      )}
    </section>
  );
}
