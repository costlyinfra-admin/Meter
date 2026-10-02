/**
 * "Test this" — choose the assumption to test a recommendation under (EX-1).
 *
 * Meter priced the recommendation under its own assumption: answers may be
 * reused for ten minutes, the prompt cache lives five. Here the customer says
 * what is true for them, and a simulation from the SDK's counters works out
 * what the recommendation is worth then. No prompt is read, no model is
 * called, nothing is spent.
 */
import { useEffect, useState, type FormEvent } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { api, ApiError, type ExperimentInput, type OfflineOptions, type Opportunity } from "../api";
import { CACHE_CHOICES, FRESHNESS_CHOICES, LEVER_TITLES } from "../experimentLabels";
import { money } from "../format";

/**
 * Model right-sizing (EX-2): which model to replace, which to test in its
 * place, and the rule. The test itself runs on the customer's machine.
 */
function RightSizingForm({ featureId, opp }: { featureId: string; opp: Opportunity }) {
  const navigate = useNavigate();
  const [options, setOptions] = useState<OfflineOptions | null>(null);
  const [failed, setFailed] = useState(false);
  const [control, setControl] = useState("");
  const [candidate, setCandidate] = useState("");
  const [marginPct, setMarginPct] = useState(10);
  const [minCases, setMinCases] = useState(20);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api
      .experimentOptions(featureId)
      .then((o) => {
        setOptions(o);
        if (o.controls.length) {
          setControl(o.controls[0].model);
          setCandidate(o.controls[0].default_candidate);
        }
        setMarginPct(Math.round(o.rule.loss_margin * 100));
        setMinCases(o.rule.min_cases);
      })
      .catch(() => setFailed(true));
  }, [featureId]);

  if (failed)
    return (
      <p className="error" role="alert">
        Could not load the models this feature can test.
      </p>
    );
  if (!options) return <p className="muted">Loading…</p>;
  if (options.controls.length === 0)
    return (
      <div className="settings-card">
        <p>None of this feature&rsquo;s models can be tested yet.</p>
        <p className="muted">
          The test runner calls Anthropic and OpenAI models. Other providers come later.
        </p>
      </div>
    );

  const current = options.controls.find((c) => c.model === control) ?? options.controls[0];
  const loosened = marginPct / 100 > options.rule.loss_margin || minCases < options.rule.min_cases;

  async function submit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const exp = await api.startExperiment(featureId, {
        lever: "model_rightsizing",
        control_model: control,
        candidate_model: candidate,
        loss_margin: marginPct / 100,
        min_cases: minCases,
      });
      // The run's token travels with this navigation only; it is never
      // fetched again.
      navigate(`/experiments/${exp.id}`, { state: { token: exp.token } });
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not start the test.");
      setBusy(false);
    }
  }

  return (
    <form className="settings-card" onSubmit={submit}>
      <p>
        Meter estimated up to <strong>{money(opp.projected_monthly_savings)}/mo</strong> if a
        cheaper model holds up for this feature. Whether it does is a question about answers, so the
        test runs where they are: on your machine, on your own cases, with your own keys and a judge
        you choose. Only numbers come back to Meter.
      </p>

      <div className="settings-field">
        <label htmlFor="t-control">Model to replace</label>
        <select
          id="t-control"
          value={control}
          onChange={(e) => {
            setControl(e.target.value);
            const next = options.controls.find((c) => c.model === e.target.value);
            if (next) setCandidate(next.default_candidate);
          }}
        >
          {options.controls.map((c) => (
            <option key={c.model} value={c.model}>
              {c.model} — {money(c.monthly_spend)} this month
            </option>
          ))}
        </select>
      </div>
      <div className="settings-field">
        <label htmlFor="t-candidate">Cheaper model to test in its place</label>
        <select id="t-candidate" value={candidate} onChange={(e) => setCandidate(e.target.value)}>
          {current.candidates.map((c) => (
            <option key={c.model} value={c.model}>
              {c.model} — {Math.round(c.save_fraction * 100)}% cheaper at your mix
              {c.model === current.default_candidate ? " (recommended)" : ""}
            </option>
          ))}
        </select>
      </div>

      <fieldset className="test-choices">
        <legend>The rule</legend>
        <p className="settings-hint muted">
          The cheaper model passes if no answer comes back broken, it is not worse more often than
          it is better beyond the allowance, there are enough cases, and it costs less per call.
        </p>
        <div className="settings-field settings-field-inline">
          <label htmlFor="t-margin">Allowance for worse answers (%)</label>
          <input
            id="t-margin"
            type="number"
            min={Math.round(options.rule.loss_margin_range[0] * 100)}
            max={Math.round(options.rule.loss_margin_range[1] * 100)}
            value={marginPct}
            onChange={(e) => setMarginPct(Number(e.target.value))}
          />
        </div>
        <div className="settings-field settings-field-inline">
          <label htmlFor="t-cases">Cases needed, at least</label>
          <input
            id="t-cases"
            type="number"
            min={options.rule.min_cases_range[0]}
            max={options.rule.min_cases_range[1]}
            value={minCases}
            onChange={(e) => setMinCases(Number(e.target.value))}
          />
        </div>
        {loosened && (
          <p className="opt-item-test-note" role="note">
            Looser than Meter&rsquo;s rule ({Math.round(options.rule.loss_margin * 100)}%, at least{" "}
            {options.rule.min_cases} cases). That is allowed, and the result will say so wherever it
            appears.
          </p>
        )}
      </fieldset>

      <p className="settings-hint muted">
        Running it calls both models once per case, plus the judge twice, on your provider account.
        Meter shows what it cost afterwards; it is part of your provider bill either way.
      </p>

      <div className="settings-actions">
        <button type="submit" disabled={busy || !candidate}>
          {busy ? "Starting…" : "Start the test"}
        </button>
        {error && (
          <span className="error" role="alert">
            {error}
          </span>
        )}
      </div>
    </form>
  );
}

export function TestRecommendationPage() {
  const { featureId = "", lever = "" } = useParams();
  const navigate = useNavigate();
  const [opp, setOpp] = useState<Opportunity | null | undefined>(undefined);
  const [loadFailed, setLoadFailed] = useState(false);
  const [ttl, setTtl] = useState(600);
  // On by default: the safe answer to "may one customer's answer be served to
  // another" is no, and loosening it should be a choice someone makes.
  const [scopedOnly, setScopedOnly] = useState(true);
  const [cacheTtl, setCacheTtl] = useState<"5m" | "1h">("1h");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api
      .featureOpportunities(featureId)
      .then((d) => setOpp(d.opportunities.find((o) => o.lever === lever) ?? null))
      .catch(() => setLoadFailed(true));
  }, [featureId, lever]);

  async function submit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    const body: ExperimentInput =
      lever === "duplicate_calls"
        ? { lever, ttl_seconds: ttl, scoped_only: scopedOnly }
        : { lever, cache_ttl: cacheTtl };
    try {
      const exp = await api.startExperiment(featureId, body);
      navigate(`/experiments/${exp.id}`);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not start the test.");
      setBusy(false);
    }
  }

  const title = opp?.title ?? LEVER_TITLES[lever] ?? "Recommendation";
  return (
    <div className="content test-page">
      <div className="dash-head">
        <div>
          <Link to={`/features/${featureId}`} className="link breadcrumb">
            ← Back to the feature
          </Link>
          <h1>Test: {title}</h1>
        </div>
      </div>

      {loadFailed ? (
        <p className="error" role="alert">
          Could not load this recommendation.
        </p>
      ) : opp === undefined ? (
        <p className="muted">Loading…</p>
      ) : opp === null || !opp.testable ? (
        <div className="settings-card">
          <p>This recommendation cannot be tested yet.</p>
          <p className="muted">
            Repeated requests can be tested, prompt caching where the provider offers a choice of
            cache lifetime, and model right-sizing on Anthropic and OpenAI models. Other
            recommendations come next.
          </p>
        </div>
      ) : lever === "model_rightsizing" ? (
        <RightSizingForm featureId={featureId} opp={opp} />
      ) : (
        <form className="settings-card" onSubmit={submit}>
          {opp.validation === "simulated" && opp.experiment ? (
            // The figure on the card is already the last test's, not Meter's
            // default — say so, rather than attach it to the wrong assumption.
            <p>
              Your last test put this at <strong>{money(opp.projected_monthly_savings)}/mo</strong>,
              with {opp.experiment.setting_label}. Choose another setting to see what it is worth
              then.
            </p>
          ) : (
            <p>
              Meter estimated <strong>{money(opp.projected_monthly_savings)}/mo</strong>{" "}
              {lever === "duplicate_calls"
                ? "assuming an answer can be reused for 10 minutes."
                : "with the provider's default 5-minute cache."}{" "}
              Tell it what is true for you, and it works out what this is worth then.
            </p>
          )}

          {lever === "duplicate_calls" ? (
            <>
              <fieldset className="test-choices">
                <legend>How old may a cached answer be?</legend>
                {FRESHNESS_CHOICES.map((c) => (
                  <label key={c.seconds} className="test-choice">
                    <input
                      type="radio"
                      name="ttl"
                      checked={ttl === c.seconds}
                      onChange={() => setTtl(c.seconds)}
                    />
                    <span>
                      <strong>{c.label}</strong>
                      <span className="muted">{c.hint}</span>
                    </span>
                  </label>
                ))}
              </fieldset>
              <label className="test-choice">
                <input
                  type="checkbox"
                  checked={scopedOnly}
                  onChange={(e) => setScopedOnly(e.target.checked)}
                />
                <span>
                  <strong>Only reuse an answer for the same customer</strong>
                  <span className="muted">
                    Counts only repeats the SDK tagged with a <code>customer_id</code> or{" "}
                    <code>cache_scope</code>. Leave this on if one customer&rsquo;s answer must
                    never be served to another.
                  </span>
                </span>
              </label>
            </>
          ) : (
            <fieldset className="test-choices">
              <legend>How long should the cache live?</legend>
              {CACHE_CHOICES.map((c) => (
                <label key={c.ttl} className="test-choice">
                  <input
                    type="radio"
                    name="cache-ttl"
                    checked={cacheTtl === c.ttl}
                    onChange={() => setCacheTtl(c.ttl)}
                  />
                  <span>
                    <strong>{c.label}</strong>
                    <span className="muted">{c.hint}</span>
                  </span>
                </label>
              ))}
            </fieldset>
          )}

          <p className="settings-hint muted">
            This is a simulation. Meter reads no prompts or responses and calls no model, so it
            costs nothing. It uses the counts the metering SDK already sends in optimize mode
            (Python or Node SDK 2.4 or later).
          </p>

          <div className="settings-actions">
            <button type="submit" disabled={busy}>
              {busy ? "Running…" : "Run the simulation"}
            </button>
            {/* Beside the button, where the person who clicked it is looking. */}
            {error && (
              <span className="error" role="alert">
                {error}
              </span>
            )}
          </div>
        </form>
      )}
    </div>
  );
}
