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
import { api, ApiError, type ExperimentInput, type Opportunity } from "../api";
import { CACHE_CHOICES, FRESHNESS_CHOICES, LEVER_TITLES } from "../experimentLabels";
import { money } from "../format";

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
            Repeated requests can be tested, and prompt caching where the provider offers a choice
            of cache lifetime. Testing other recommendations on your own data comes next.
          </p>
        </div>
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
