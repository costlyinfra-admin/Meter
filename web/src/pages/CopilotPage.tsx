/**
 * Optimization Copilot — the tenant-wide Overview (opt spec §21). Answers "where's
 * the money and what do I fix first" across every feature. Measured, modeled and
 * verified savings are three distinct figures — never combined into one number.
 */
import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api, ApiError, type CopilotOverview } from "../api";
import { ConfidenceBadge } from "../components/badges";
import { money } from "../format";

/** Plain labels for billing-only findings — never the word "savings". */
const BILLING_LABELS: Record<string, string> = {
  unclassified_spend: "Spend to review",
  non_production_spend: "Spend to review",
  unattributed_spend: "Visibility opportunity",
  cost_concentration: "Cost concentration",
  cost_growth: "Cost growth",
  missing_cost_control: "Missing cost control",
};

const EFFORT_LABELS: Record<string, string> = {
  very_low: "Very low",
  low: "Low",
  medium: "Medium",
  high: "High",
};

export function CopilotPage() {
  const [data, setData] = useState<CopilotOverview | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      setData(await api.copilotOverview());
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not load the Copilot Overview.");
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  return (
    <div className="content">
      <div className="dash-head">
        <h1>Optimization Copilot</h1>
      </div>
      <p className="muted">
        Where AI money is being wasted, and what to fix first — measured across every feature.
      </p>
      <p className="muted">
        <Link to="/optimize/prompts" className="link">
          Prompts →
        </Link>{" "}
        the prompts your product runs, and cheaper versions of them.
      </p>

      {error && (
        <p className="error" role="alert">
          {error}
        </p>
      )}
      {data === null && !error ? (
        <p className="muted">Loading…</p>
      ) : data ? (
        <>
          {/* Three distinct savings figures — never one blended number. */}
          <div className="copilot-kpis">
            <Kpi
              label="Measured savings"
              value={`${money(data.totals.measured)}/mo`}
              sub="Guaranteed, given your traffic"
              tone="measured"
            />
            <Kpi
              label="Modeled ceiling"
              value={`up to ${money(data.totals.modeled_ceiling)}/mo`}
              sub="Quality-gated — realize with care"
              tone="ceiling"
            />
            <Kpi
              label="Verified savings"
              value={`${money(data.verified_annual_savings)}/yr`}
              sub={`${money(data.verified_monthly_savings)}/mo proven & held`}
              tone="verified"
            />
          </div>

          <section className="detail-section">
            <div className="section-head">
              <h2>Top recommendations</h2>
              <span className="section-sub muted">
                Measured savings first, then what billing data alone can show. Billing findings
                don&apos;t infer prompt, model, caching, quality, user or feature conclusions.
              </span>
            </div>
            {data.top_recommendations.length === 0 && data.billing_opportunities.length === 0 ? (
              <p className="muted">
                {data.has_billing_data
                  ? "No measured opportunities yet — these need request telemetry from the SDK."
                  : "No measured opportunities yet across your features."}
              </p>
            ) : (
              <table className="mini-table">
                <thead>
                  <tr>
                    <th>Opportunity</th>
                    <th>Feature</th>
                    <th className="num">Savings</th>
                    <th>Confidence</th>
                    <th>Effort</th>
                    <th />
                  </tr>
                </thead>
                <tbody>
                  {/* Measured/modelled opportunities: real, quantified savings. */}
                  {data.top_recommendations.map((o) => (
                    <tr key={`${o.feature_id}-${o.lever}`}>
                      <td title={o.evidence}>{o.title}</td>
                      <td>
                        <Link to={`/features/${o.feature_id}`} className="link">
                          {o.feature_name}
                        </Link>
                      </td>
                      <td className="num">
                        {o.savings_type === "modeled_ceiling" && (
                          <span className="opt-ceiling">up to </span>
                        )}
                        {money(o.projected_monthly_savings)}/mo
                      </td>
                      <td>
                        <ConfidenceBadge level={o.confidence} />
                      </td>
                      <td>{EFFORT_LABELS[o.engineering_effort] ?? o.engineering_effort}</td>
                      <td>
                        <Link to={`/features/${o.feature_id}`} className="row-action">
                          Review
                        </Link>
                      </td>
                    </tr>
                  ))}
                  {/* Billing-only findings: observed spend, never counted as savings. */}
                  {data.billing_opportunities.map((o) => (
                    // The calculation stays available on hover — auditable, not crowding.
                    <tr key={o.id} title={o.evidence.calculation}>
                      <td>
                        <span className={`billing-tag billing-tag-${o.type}`}>
                          {BILLING_LABELS[o.type] ?? "Finding"}
                        </span>{" "}
                        {o.title}
                      </td>
                      <td className="muted">
                        {o.evidence.observed_cost != null
                          ? `${money(o.evidence.observed_cost)} observed`
                          : "—"}
                      </td>
                      <td className="num muted">Not quantified</td>
                      <td>
                        <ConfidenceBadge level={o.confidence === "medium" ? "med" : "high"} />
                      </td>
                      <td className="muted">—</td>
                      <td>
                        <Link to={o.action.href} className="row-action">
                          {o.action.label}
                        </Link>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
            {!data.has_sdk_telemetry && data.billing_opportunities.length > 0 && (
              <p className="muted billing-sdk-note">
                Request-, user- and feature-level optimizations (duplicate calls, cacheable
                prefixes, model right-sizing) need request telemetry.{" "}
                <Link to="/install-sdk" className="link">
                  Install the SDK
                </Link>{" "}
                to unlock them.
              </p>
            )}
          </section>

          <div className="copilot-cols">
            <section className="detail-section">
              <div className="section-head">
                <h2>By feature</h2>
                <span className="section-sub muted">Where the money is.</span>
              </div>
              <table className="mini-table">
                <thead>
                  <tr>
                    <th>Feature</th>
                    <th className="num">Measured</th>
                    <th className="num">Modeled</th>
                  </tr>
                </thead>
                <tbody>
                  {data.by_feature
                    .filter((f) => f.measured > 0 || f.modeled_ceiling > 0)
                    .map((f) => (
                      <tr key={f.feature_id}>
                        <td>
                          <Link to={`/features/${f.feature_id}`} className="link">
                            {f.name}
                          </Link>
                        </td>
                        <td className="num">{money(f.measured)}/mo</td>
                        <td className="num muted">
                          {f.modeled_ceiling > 0 ? `up to ${money(f.modeled_ceiling)}/mo` : "—"}
                        </td>
                      </tr>
                    ))}
                </tbody>
              </table>
            </section>

            <section className="detail-section">
              <div className="section-head">
                <h2>By lever</h2>
                <span className="section-sub muted">Where the leverage is.</span>
              </div>
              <table className="mini-table">
                <thead>
                  <tr>
                    <th>Lever</th>
                    <th className="num">Features</th>
                    <th className="num">Savings</th>
                  </tr>
                </thead>
                <tbody>
                  {data.by_lever.map((l) => (
                    <tr key={l.lever}>
                      <td>{l.title}</td>
                      <td className="num">{l.count}</td>
                      <td className="num">
                        {l.savings_type === "modeled_ceiling" && (
                          <span className="opt-ceiling">up to </span>
                        )}
                        {money(l.monthly)}/mo
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </section>
          </div>

          {data.applied.length > 0 && (
            <section className="detail-section">
              <div className="section-head">
                <h2>Applied &amp; verified</h2>
                <span className="section-sub muted">
                  Projected savings reconciled against the measured drop since.
                </span>
              </div>
              <table className="mini-table">
                <thead>
                  <tr>
                    <th>Optimization</th>
                    <th>Feature</th>
                    <th className="num">Projected</th>
                    <th className="num">Realized</th>
                  </tr>
                </thead>
                <tbody>
                  {data.applied.map((a) => (
                    <tr key={`${a.feature_id}-${a.lever}`}>
                      <td>{a.lever.replace(/_/g, " ")}</td>
                      <td>
                        <Link to={`/features/${a.feature_id}`} className="link">
                          {a.feature_name}
                        </Link>
                      </td>
                      <td className="num">{money(a.projected_monthly)}/mo</td>
                      <td className="num">
                        {a.status === "pending" ? (
                          <span className="muted">awaiting next period</span>
                        ) : (
                          <>
                            <strong className="opt-realized">
                              {money(a.realized_monthly ?? 0)}/mo
                            </strong>
                            {a.status === "verified" && (
                              <span className="opt-verified">✓ Verified</span>
                            )}
                          </>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </section>
          )}
        </>
      ) : null}
    </div>
  );
}

function Kpi({
  label,
  value,
  sub,
  tone,
}: {
  label: string;
  value: string;
  sub: string;
  tone: "measured" | "ceiling" | "verified";
}) {
  return (
    <div className={`copilot-kpi kpi-${tone}`}>
      <span className="copilot-kpi-label">{label}</span>
      <span className="copilot-kpi-value">{value}</span>
      <span className="copilot-kpi-sub muted">{sub}</span>
    </div>
  );
}
