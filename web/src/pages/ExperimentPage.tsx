/**
 * One test of a recommendation, and what it found (EX-1).
 *
 * The answer, then the whole trade-off the customer chose along — every
 * freshness limit, or both cache lifetimes — then how it was worked out, so the
 * figure is never a black box. A test still waiting for data says how many
 * calls it has, and finishes on its own once there are enough.
 */
import { useCallback, useEffect, useState } from "react";
import { Link, useLocation, useParams } from "react-router-dom";
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

const SOURCES: Record<string, string> = {
  runner: "meter-test",
  promptfoo: "promptfoo, imported with meter-test",
  inspect: "Inspect AI, imported with meter-test",
};

function percent(rate: number): string {
  return `${Math.round(rate * 100)}%`;
}

/** How to run an offline test, with the run's token while it can still be shown. */
function RunInstructions({ exp, token }: { exp: Experiment; token: string | null }) {
  const keyVar = `${(exp.setting.provider ?? "provider").toUpperCase()}_API_KEY`;
  const origin = window.location.origin;
  return (
    <section className="detail-section">
      <div className="section-head">
        <h2>Run it on your machine</h2>
        <span className="section-sub muted">
          Your cases, your keys and your judge stay there. Only numbers come back.
        </span>
      </div>
      {token ? (
        <div className="test-token">
          <p>
            <strong>This test&rsquo;s token, shown once.</strong> It can only fetch this
            test&rsquo;s instructions and send its results, once. Copy it now.
          </p>
          <code className="test-token-value">{token}</code>
        </div>
      ) : (
        <p className="muted">
          The token was shown when this test was started. If you no longer have it, start the test
          again — that cancels this one and issues a new token.
        </p>
      )}
      <pre className="test-commands">
        {[
          'pip install "costlyinfra-meter>=2.5"',
          `export METER_URL=${origin}`,
          `export METER_EXPERIMENT_TOKEN=${token ?? "<the token>"}`,
          `export ${keyVar}=<your key>`,
          "meter-test run --cases cases.jsonl",
        ].join("\n")}
      </pre>
      <ul className="test-method">
        <li>
          <code>cases.jsonl</code> holds one case per line, such as <code>{'{"input": "…"}'}</code>{" "}
          or <code>{'{"system": "…", "messages": [...]}'}</code> — real requests your feature
          handles.
        </li>
        <li>
          Each case runs on {exp.setting.control_model} and on {exp.setting.candidate_model}. A
          judge compares the two answers, both ways round; it is {exp.setting.control_model} unless
          you pass <code>--judge-model</code>.
        </li>
        <li>
          Already use promptfoo or Inspect AI? Send their results instead:{" "}
          <code>meter-test import promptfoo results.json</code> or{" "}
          <code>meter-test import inspect control.json candidate.json</code>.
        </li>
        <li>
          Add <code>--dry-run</code> to see exactly what would be sent, without sending it.
        </li>
        {exp.run_expires_at && <li>The token stops working on {testDate(exp.run_expires_at)}.</li>}
      </ul>
    </section>
  );
}

function OfflineResults({
  exp,
  r,
}: {
  exp: Experiment;
  r: Extract<ExperimentResult, { kind: "offline" }>;
}) {
  const saving =
    r.control.cost_per_call && r.candidate.cost_per_call !== null
      ? 1 - r.candidate.cost_per_call / r.control.cost_per_call
      : null;
  return (
    <section className="detail-section">
      <div className="section-head">
        <h2>What the test found</h2>
        <span className="section-sub muted">
          {num(r.compared)} cases compared
          {r.control_errors > 0 && ` (${num(r.control_errors)} left out: the current model failed)`}
        </span>
      </div>
      <ul className="test-verdicts">
        <li>
          <strong>{num(r.better)}</strong> better
        </li>
        <li>
          <strong>{num(r.same)}</strong> the same
        </li>
        <li>
          <strong>{num(r.worse)}</strong> worse
        </li>
        <li>
          <strong>{num(r.broken)}</strong> broken
        </li>
        {r.unjudged > 0 && (
          <li>
            <strong>{num(r.unjudged)}</strong> not judged
          </li>
        )}
      </ul>
      <div className="mini-table-wrap">
        <table className="mini-table">
          <thead>
            <tr>
              <th scope="col">Model</th>
              <th scope="col" className="num">
                Cost per call
              </th>
              <th scope="col" className="num">
                Typical latency
              </th>
              <th scope="col" className="num">
                Slowest 5%
              </th>
            </tr>
          </thead>
          <tbody>
            {[r.control, r.candidate].map((arm, i) => (
              <tr
                key={arm.model}
                aria-current={i === 1 ? "true" : undefined}
                className={i === 1 ? "test-chosen" : undefined}
              >
                <td>
                  {arm.model}
                  <span className="muted"> · {i === 0 ? "current" : "tested"}</span>
                </td>
                <td className="num">
                  {arm.cost_per_call === null ? "—" : `$${arm.cost_per_call.toFixed(4)}`}
                </td>
                <td className="num">
                  {arm.latency_ms.p50 === null ? "—" : `${num(arm.latency_ms.p50)} ms`}
                </td>
                <td className="num">
                  {arm.latency_ms.p95 === null ? "—" : `${num(arm.latency_ms.p95)} ms`}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {exp.outcome === "passed" && saving !== null && (
        <p className="test-baseline">
          {percent(saving)} less per call, applied to the {money(r.control_monthly_spend)} this
          feature spent on {r.control.model} this month:{" "}
          <strong>{money(r.monthly_saving)}/mo</strong>, counted as <em>tested</em>.
        </p>
      )}
      <p className="muted">
        Rule: no broken answers, up to {percent(r.rule.loss_margin)} more cases worse than better,
        at least {num(r.rule.min_cases)} cases, and cheaper per call.
        {r.rule.relaxed && <strong> This rule was loosened from Meter&rsquo;s default.</strong>}
      </p>
      <p className="muted">
        Results from {SOURCES[exp.results_source ?? "runner"] ?? exp.results_source}
        {exp.judge_model && `, judged by ${exp.judge_model}`}.
        {exp.test_cost != null &&
          ` Running it cost about ${money(exp.test_cost)} at list price — already part of your provider bill.`}
      </p>
    </section>
  );
}

function Method({ exp }: { exp: Experiment }) {
  return (
    <section className="detail-section">
      <div className="section-head">
        <h2>How this was worked out</h2>
      </div>
      <ul className="test-method">
        {exp.mode === "offline" ? (
          <>
            <li>
              The test ran on your machine: your cases, through both models, with your keys.{" "}
              {exp.results_source === "promptfoo"
                ? "Your own promptfoo assertions decided each answer: passing where the current model failed counts as better, and the reverse as worse."
                : exp.results_source === "inspect"
                  ? "Your own Inspect scorer decided each answer: a higher score than the current model's counts as better, a lower one as worse."
                  : "A judge you chose compared each pair of answers twice, in both orders, and only agreement counted."}
            </li>
            <li>
              Meter received numbers only — tokens, latency, whether a call failed or broke a check,
              and the verdict — and priced the tokens itself from its price book.
            </li>
            <li>
              A sample is not your traffic, so a passed test counts as <em>tested</em>, kept apart
              from measured savings, until the change is applied and your bill confirms it.
            </li>
          </>
        ) : exp.lever === "duplicate_calls" ? (
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
        {exp.mode !== "offline" && (
          <li>
            Only counts left your servers. No prompt or response was read and no model was called,
            so the test cost nothing.
          </li>
        )}
      </ul>
    </section>
  );
}

export function ExperimentPage() {
  const { id = "" } = useParams();
  const [exp, setExp] = useState<Experiment | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  // The run's token arrives only with the navigation that created the test,
  // and is never fetched again: reload the page and it is gone, by design.
  const location = useLocation();
  const token = (location.state as { token?: string } | null)?.token ?? null;

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
            {exp.mode === "offline" ? "Tested" : "Simulated"} with {exp.setting_label} · started{" "}
            {testDate(exp.created_at)} by {exp.created_by}
          </p>
        </div>
      </div>

      <div className={`test-outcome ${status.className}`} role="status">
        <span className={`test-badge ${status.className}`}>{status.label}</span>
        <p>
          {exp.status === "waiting_for_results"
            ? "Run the test below. The result appears here as soon as it is sent."
            : exp.outcome_reason}
        </p>
      </div>

      {exp.status === "waiting_for_results" && <RunInstructions exp={exp} token={token} />}

      {exp.baseline && (
        <p className="test-baseline">
          Before this test Meter estimated <strong>{money(exp.baseline.monthly)}/mo</strong>
          {exp.mode === "offline" && exp.setting.control_model
            ? ` for ${exp.setting.control_model}, `
            : ", "}
          {SAVINGS_TYPE[exp.baseline.savings_type] ?? exp.baseline.savings_type}.
          {exp.result && exp.status === "completed" && (
            <>
              {" "}
              Under your setting: <strong>{money(exp.result.monthly_saving)}/mo</strong>.
            </>
          )}
        </p>
      )}

      {exp.result?.kind === "offline" && <OfflineResults exp={exp} r={exp.result} />}

      {exp.result && exp.result.kind !== "offline" && exp.result.calls > 0 && (
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
        {exp.status === "waiting_for_data" || exp.status === "waiting_for_results" ? (
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
