/**
 * One test of a recommendation, and what it found (EX-1).
 *
 * The answer, then the whole trade-off the customer chose along — every
 * freshness limit, or both cache lifetimes — then how it was worked out, so the
 * figure is never a black box. A test still waiting for data says how many
 * calls it has, and finishes on its own once there are enough.
 */
import { useCallback, useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { api, ApiError, type Experiment, type ExperimentResult } from "../api";
import { LEVER_TITLES, testDate, testStatus } from "../experimentLabels";
import { money, num } from "../format";

const SAVINGS_TYPE: Record<string, string> = {
  measured: "a measured saving",
  modeled_ceiling: "a ceiling",
  directional: "a rough estimate",
};

function pct(rate: number): string {
  return `${(rate * 100).toFixed(1)}%`;
}

function RepeatsTable({ r }: { r: Extract<ExperimentResult, { kind: "repeats" }> }) {
  return (
    <div className="mini-table-wrap">
      <table className="mini-table">
        <thead>
          <tr>
            <th scope="col">Answers reused for up to</th>
            <th scope="col" className="num">
              Calls a cache would have served
            </th>
            <th scope="col" className="num">
              Share of calls
            </th>
            <th scope="col" className="num">
              Saving this month
            </th>
          </tr>
        </thead>
        <tbody>
          {r.ladder.map((step) => (
            <tr
              key={step.ttl_seconds}
              className={step.ttl_seconds === r.ttl_seconds ? "test-chosen" : undefined}
              aria-current={step.ttl_seconds === r.ttl_seconds ? "true" : undefined}
            >
              <td>
                {step.label}
                {step.ttl_seconds === r.ttl_seconds && (
                  <span className="muted"> · your choice</span>
                )}
              </td>
              <td className="num">
                {step.lower_bound && (
                  <span title="The SDK had to forget some requests early">≥ </span>
                )}
                {num(step.hits)}
              </td>
              <td className="num">{pct(step.hit_rate)}</td>
              <td className="num">{money(step.monthly_saving)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function CachingTable({ r }: { r: Extract<ExperimentResult, { kind: "caching" }> }) {
  return (
    <div className="mini-table-wrap">
      <table className="mini-table">
        <thead>
          <tr>
            <th scope="col">Cache lifetime</th>
            <th scope="col" className="num">
              Cache writes needed
            </th>
            <th scope="col" className="num">
              Saving this month, after writes
            </th>
          </tr>
        </thead>
        <tbody>
          {r.lifetimes.map((l) => (
            <tr
              key={l.cache_ttl}
              className={l.cache_ttl === r.cache_ttl ? "test-chosen" : undefined}
              aria-current={l.cache_ttl === r.cache_ttl ? "true" : undefined}
            >
              <td>
                {l.label}
                {l.cache_ttl === r.cache_ttl && <span className="muted"> · your choice</span>}
              </td>
              <td className="num">{l.cache_writes === null ? "—" : num(l.cache_writes)}</td>
              <td className="num">{money(l.monthly_saving)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function Method({ exp }: { exp: Experiment }) {
  return (
    <section className="detail-section">
      <div className="section-head">
        <h2>How this was worked out</h2>
      </div>
      <ul className="test-method">
        {exp.lever === "duplicate_calls" ? (
          <>
            <li>
              In optimize mode the SDK fingerprints each request on your servers and counts, for
              each freshness limit, how many calls repeated a request first seen no longer ago than
              that. The limit runs from the first call and is never extended by later ones.
            </li>
            <li>
              Each of those calls is priced at the tokens it used, at list price — the same basis as
              the recommendation, so the two can be compared.
            </li>
            <li>
              Identical requests are not proof an answer could be reused: freshness, permissions and
              anything outside the prompt still decide that. That is what the limit you chose stands
              for.
            </li>
          </>
        ) : (
          <>
            <li>
              Caching is a trade: reads are cheaper than sending the prompt, but writing the cache
              costs more. The SDK counts how often the cache would have had to be written at each
              lifetime, from the gaps between your calls.
            </li>
            <li>
              Each lifetime is priced at its own write price and netted against the reads, the same
              way the recommendation is priced.
            </li>
          </>
        )}
        <li>
          Only counts left your servers. No prompt or response was read and no model was called, so
          the test cost nothing.
        </li>
      </ul>
    </section>
  );
}

export function ExperimentPage() {
  const { id = "" } = useParams();
  const [exp, setExp] = useState<Experiment | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    try {
      setExp(await api.experiment(id));
      setError(null);
    } catch (err) {
      setError(
        err instanceof ApiError && err.status === 404
          ? "This test does not exist, or belongs to another organization."
          : "Could not load this test.",
      );
    }
  }, [id]);

  useEffect(() => {
    void load();
  }, [load]);

  async function cancel() {
    setBusy(true);
    try {
      setExp(await api.cancelExperiment(id));
    } finally {
      setBusy(false);
    }
  }

  if (error)
    return (
      <div className="content">
        <p className="error" role="alert">
          {error}
        </p>
      </div>
    );
  if (!exp)
    return (
      <div className="content">
        <p className="muted">Loading…</p>
      </div>
    );

  const status = testStatus(exp);
  const title = LEVER_TITLES[exp.lever] ?? exp.lever;
  const testAgain = `/features/${exp.feature_id}/test/${exp.lever}`;
  return (
    <div className="content test-page">
      <div className="dash-head">
        <div>
          <Link to={`/features/${exp.feature_id}`} className="link breadcrumb">
            ← {exp.feature_name ?? "Back to the feature"}
          </Link>
          <h1>Test: {title}</h1>
          <p className="muted">
            Simulated with {exp.setting_label} · started {testDate(exp.created_at)} by{" "}
            {exp.created_by}
          </p>
        </div>
      </div>

      <div className={`test-outcome ${status.className}`} role="status">
        <span className={`test-badge ${status.className}`}>{status.label}</span>
        <p>{exp.outcome_reason}</p>
      </div>

      {exp.baseline && (
        <p className="test-baseline">
          Before this test Meter estimated <strong>{money(exp.baseline.monthly)}/mo</strong>,{" "}
          {SAVINGS_TYPE[exp.baseline.savings_type] ?? exp.baseline.savings_type}.
          {exp.result && exp.status === "completed" && (
            <>
              {" "}
              Under your setting: <strong>{money(exp.result.monthly_saving)}/mo</strong>.
            </>
          )}
        </p>
      )}

      {exp.result && exp.result.calls > 0 && (
        <section className="detail-section">
          <div className="section-head">
            <h2>What each choice would have saved</h2>
            <span className="section-sub muted">
              {num(exp.result.calls)} calls in{" "}
              {new Date(`${exp.period}T00:00:00`).toLocaleDateString("en-US", {
                month: "long",
                year: "numeric",
              })}
            </span>
          </div>
          {exp.result.kind === "repeats" ? (
            <RepeatsTable r={exp.result} />
          ) : (
            <CachingTable r={exp.result} />
          )}
        </section>
      )}

      <Method exp={exp} />

      <div className="settings-actions">
        {exp.status === "waiting_for_data" ? (
          <>
            <button onClick={() => void load()}>Check again</button>
            <button className="secondary" onClick={() => void cancel()} disabled={busy}>
              Cancel the test
            </button>
          </>
        ) : (
          <Link to={testAgain} className="link">
            Test again with a different setting →
          </Link>
        )}
      </div>

      {exp.history && exp.history.length > 0 && (
        <section className="detail-section">
          <div className="section-head">
            <h2>Earlier tests of this recommendation</h2>
          </div>
          <ul className="test-history">
            {exp.history.map((h) => (
              <li key={h.id}>
                <Link to={`/experiments/${h.id}`} className="link">
                  {testDate(h.created_at)}
                </Link>{" "}
                · {h.setting_label} ·{" "}
                <span className={`test-badge ${testStatus(h).className}`}>
                  {testStatus(h).label}
                </span>
              </li>
            ))}
          </ul>
        </section>
      )}
    </div>
  );
}
