/**
 * Feature drill-down (design §9.3). A single review-period filter at the top
 * (the same presets as the Overview, preserved in the URL) scopes every cost on
 * the page — developer/build cost, inference cost, and the optimization anchor —
 * so the totals reconcile with the Overview's feature row. Engineering activity
 * (commits / PRs / files) and the evidence trail are all-time, and labelled so.
 */
import { useCallback, useEffect, useMemo, useState } from "react";
import { Link, useParams, useSearchParams } from "react-router-dom";
import {
  api,
  ApiError,
  type FeatureDetail as Detail,
  type FeatureInference,
  type FeatureOpportunities,
  type Opportunity,
  type OptimizationAction,
  type Product,
  type RangeKind,
  type ReviewRange,
} from "../api";
import { CategoryBadge, ConfidenceBadge, ProductBadge } from "../components/badges";
import { CategoryPicker } from "../components/CategoryPicker";
import { ProductPicker } from "../components/ProductPicker";
import { PeriodSelector } from "../components/PeriodSelector";
import { TrendChart } from "../components/TrendChart";
import { compact, money, num } from "../format";

/** Series colours — the --chart-1..6 ramp from styles.css, led by lime because
 *  lime is the data colour on costlyinfra.com. Kept in sync by hand: these are
 *  inline SVG/style fills, which cannot read a CSS custom property. */
const MODEL_COLORS = ["#ddf859", "#2b7264", "#92661c", "#857ab8", "#e76740", "#22241e", "#686c60"];

const NAMED_KINDS: RangeKind[] = [
  "this_month",
  "last_month",
  "last_3_months",
  "last_6_months",
  "last_12_months",
];

/** Read the review range from the URL (?range=… or ?start=&end=), default this month. */
function rangeFromParams(sp: URLSearchParams): ReviewRange {
  const start = sp.get("start");
  const end = sp.get("end");
  if (start && end) return { kind: "custom", start, end };
  const k = sp.get("range") as RangeKind | null;
  if (k && NAMED_KINDS.includes(k)) return { kind: k };
  return { kind: "this_month" };
}

/** The URL params for a range (mirrors the API's rangeQuery param names). */
function paramsForRange(r: ReviewRange): Record<string, string> {
  if (r.kind === "custom") return r.start && r.end ? { start: r.start, end: r.end } : {};
  return { range: r.kind };
}

export function FeatureDetail() {
  const { id = "" } = useParams();
  const [searchParams, setSearchParams] = useSearchParams();
  // Keyed on the query string so `range`'s identity is stable between renders
  // (the effect below and the child sections depend on it).
  const spString = searchParams.toString();
  const range = useMemo(() => rangeFromParams(new URLSearchParams(spString)), [spString]);
  const setRange = (r: ReviewRange) => setSearchParams(paramsForRange(r), { replace: true });

  const [detail, setDetail] = useState<Detail | null>(null);
  // One feature, so one fetch: the picker needs the customer's product list.
  const [products, setProducts] = useState<Product[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      setError(null);
      setDetail(await api.featureDetail(id, range));
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not load this feature.");
    }
  }, [id, range]);

  useEffect(() => {
    api
      .listProducts()
      .then((d) => setProducts(d.products))
      .catch(() => setProducts([]));
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const sources = detail?.inference_sources ?? [];
  const inferenceLabel = sources.includes("self_host")
    ? "self-hosted (allocated)"
    : sources.includes("hook")
      ? "hook-metered"
      : "connector-derived";

  return (
    <div className="content">
      <Link to="/" className="link breadcrumb">
        ← All features
      </Link>

      {/* Review-period filter — scopes every cost on this page, mirroring the
          Overview's presets and preserved in the URL. */}
      <div className="period-controls detail-period">
        <span className="muted period-label">Showing costs for</span>
        <PeriodSelector
          value={range}
          onChange={setRange}
          resolved={
            detail ? { start: detail.start.slice(0, 7), end: detail.end.slice(0, 7) } : undefined
          }
        />
      </div>

      {error && (
        <p className="error" role="alert">
          {error}
        </p>
      )}
      {detail === null && !error ? (
        <p className="muted">Loading…</p>
      ) : detail ? (
        <div>
          <h1>{detail.name}</h1>
          {detail.description && <p className="muted">{detail.description}</p>}
          <p className="detail-meta">
            <span className="badge">{detail.status}</span>
            <ProductBadge product={detail.product_name} source={detail.product_source} />
            <CategoryBadge category={detail.category} source={detail.category_source} />
            <ProductPicker
              value={detail.product_id}
              products={products}
              onChange={async (productId) => {
                await api.setFeatureProduct(detail.feature_id, productId);
                await load();
              }}
            />
            <CategoryPicker
              value={detail.category}
              onChange={async (category) => {
                await api.setFeatureCategory(detail.feature_id, category);
                await load();
              }}
            />
            {detail.headline.active_users != null && (
              <span className="muted">
                {num(detail.headline.active_users)} active users this period
              </span>
            )}
            {detail.headline.avg_latency_ms != null && (
              <span className="muted" title="Average latency of metered (SDK) calls">
                {num(detail.headline.avg_latency_ms)} ms avg latency
              </span>
            )}
          </p>

          {/* ---- Developer cost ---- */}
          <section className="detail-section">
            <div className="section-head">
              <h2>Developer cost</h2>
              <div className="section-stats">
                <span>
                  <strong>{money(detail.build_total)}</strong> in period
                </span>
                <span>
                  <strong>{num(detail.build_contributors)}</strong> contributor
                  {detail.build_contributors === 1 ? "" : "s"}
                </span>
              </div>
            </div>
            {detail.build_by_developer.length === 0 ? (
              <p className="muted">No build cost in this period.</p>
            ) : (
              <>
                <table className="mini-table">
                  <thead>
                    <tr>
                      <th>Developer</th>
                      <th>Tool</th>
                      <th className="num">Amount</th>
                      <th className="num">Commits</th>
                      <th className="num">PRs</th>
                      <th className="num">Files</th>
                    </tr>
                  </thead>
                  <tbody>
                    {detail.build_by_developer.map((d, i) => (
                      <tr key={i}>
                        <td>{d.developer_id}</td>
                        <td>{d.tool.replace("_", " ")}</td>
                        <td className="num">{money(d.amount)}</td>
                        <td className="num">{num(d.commits)}</td>
                        <td className="num">{num(d.prs)}</td>
                        <td className="num">{num(d.files_changed)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
                <p className="muted legend">
                  Amount is for the selected period; commits, PRs, and files are all-time
                  engineering activity.
                </p>
              </>
            )}
          </section>

          {/* ---- Inference cost ---- */}
          <InferenceSection
            featureId={detail.feature_id}
            range={range}
            sourceLabel={inferenceLabel}
          />

          {/* ---- Optimization opportunities ---- */}
          <OptimizationSection featureId={detail.feature_id} range={range} />

          {/* ---- Evidence trail (all-time) ---- */}
          <section className="evidence-trail">
            <h2>Evidence trail</h2>
            <p className="muted">
              The all-time signals behind this feature — every number above traces back to these.
            </p>
            {detail.evidence.length === 0 ? (
              <p className="muted">No signals recorded.</p>
            ) : (
              <ul className="evidence-list">
                {detail.evidence.map((s, i) => (
                  <li key={i} className="evidence-item">
                    <span className="evidence-type">{s.signal_type}</span>
                    <span className="evidence-ref">{s.external_ref}</span>
                    {s.actor && <span className="muted">by {s.actor}</span>}
                    {s.confidence && <ConfidenceBadge level={s.confidence} />}
                    {s.source && <span className="muted">via {s.source}</span>}
                  </li>
                ))}
              </ul>
            )}
          </section>
        </div>
      ) : null}
    </div>
  );
}

function monthLabel(iso: string): string {
  return new Date(`${iso}T00:00:00`).toLocaleString("en-US", { month: "short", year: "numeric" });
}

// Lever → friendly title, for the Applied-optimizations table (whose rows carry
// only a lever). Opportunity cards use the `title` the API now provides.
const LEVER_TITLES: Record<string, string> = {
  duplicate_calls: "Duplicate calls",
  prompt_caching: "Prompt caching",
  provider_switch: "Cheaper provider",
  model_rightsizing: "Model right-sizing",
};

const EFFORT_LABELS: Record<string, string> = {
  very_low: "Very low effort",
  low: "Low effort",
  medium: "Medium effort",
  high: "High effort",
};

function OptimizationSection({ featureId, range }: { featureId: string; range: ReviewRange }) {
  const [data, setData] = useState<FeatureOpportunities | null>(null);
  const [failed, setFailed] = useState(false);

  const load = useCallback(async () => {
    try {
      setFailed(false);
      setData(await api.featureOpportunities(featureId, range));
    } catch {
      setFailed(true);
    }
  }, [featureId, range]);

  useEffect(() => {
    load();
  }, [load]);

  return (
    <section className="detail-section">
      <div className="section-head">
        <div>
          <h2>Optimization opportunities</h2>
          <span className="section-sub muted">
            Measured findings from your traffic, plus directional estimates from usage.
          </span>
        </div>
      </div>

      {failed ? (
        <p className="muted">Could not load optimization opportunities.</p>
      ) : data === null ? (
        <p className="muted">Loading…</p>
      ) : (
        <>
          <MeasuredGroup
            featureId={featureId}
            opps={data.opportunities.filter((o) => o.savings_type !== "directional")}
            totals={data.totals}
            cacheUtilization={data.cache_utilization}
            actions={data.actions}
            onChange={load}
          />
          <DirectionalGroup
            opps={data.opportunities.filter((o) => o.savings_type === "directional")}
            total={data.totals.directional}
          />
        </>
      )}
    </section>
  );
}

function MeasuredGroup({
  featureId,
  opps,
  totals,
  cacheUtilization,
  actions,
  onChange,
}: {
  featureId: string;
  opps: Opportunity[];
  totals: FeatureOpportunities["totals"];
  cacheUtilization: number | null;
  actions: OptimizationAction[];
  onChange: () => Promise<void>;
}) {
  const actionByLever = new Map(actions.map((a) => [a.lever, a]));
  return (
    <div className="opt-group">
      <div className="opt-group-head">
        <div>
          <h3 className="opt-group-title">
            Measured <span className="opt-tag opt-tag-measured">grounded in measured usage</span>
          </h3>
          {cacheUtilization != null && (
            <span className="section-sub muted">
              {Math.round(cacheUtilization * 100)}% of input is already cached.
            </span>
          )}
        </div>
        {totals.measured > 0 ? (
          <div className="savings-headline">
            <span className="savings-label">Measured savings</span>
            <span className="savings-month">{money(totals.measured)}/mo</span>
            {totals.modeled_ceiling > 0 && (
              <span className="savings-year muted">
                + up to {money(totals.modeled_ceiling)}/mo modeled
              </span>
            )}
          </div>
        ) : totals.modeled_ceiling > 0 ? (
          <div className="savings-headline">
            <span className="savings-label">Modeled ceiling</span>
            <span className="savings-month">up to {money(totals.modeled_ceiling)}/mo</span>
          </div>
        ) : null}
      </div>

      {opps.length === 0 ? (
        <div className="opt-nudge">
          <p>
            No measured opportunities yet. Install the metering SDK with <code>optimize=True</code>{" "}
            to turn the estimates below into measured, per-call findings.
          </p>
          <Link to="/install-sdk" className="link">
            Install SDK →
          </Link>
        </div>
      ) : (
        <ul className="opt-list">
          {opps.map((o) => (
            <MeasuredRow
              key={o.lever}
              opp={o}
              featureId={featureId}
              action={actionByLever.get(o.lever) ?? null}
              onChange={onChange}
            />
          ))}
        </ul>
      )}

      <AppliedActions actions={actions} />
    </div>
  );
}

function MeasuredRow({
  opp,
  featureId,
  action,
  onChange,
}: {
  opp: Opportunity;
  featureId: string;
  action: OptimizationAction | null;
  onChange: () => Promise<void>;
}) {
  const [busy, setBusy] = useState(false);
  const applied = action != null;
  const ceiling = opp.savings_type === "modeled_ceiling";

  async function toggle() {
    setBusy(true);
    try {
      if (applied) await api.unapplyOpportunity(featureId, opp.lever);
      else await api.applyOpportunity(featureId, opp.lever, opp.projected_monthly_savings);
      await onChange();
    } finally {
      setBusy(false);
    }
  }

  return (
    <li className="opt-item">
      <div className="opt-item-main">
        <div className="opt-item-lever">
          <strong>{opp.title}</strong>
          <span title={opp.confidence_reason}>
            <ConfidenceBadge level={opp.confidence} />
          </span>
          <span className={`opt-effort effort-${opp.engineering_effort}`}>
            {EFFORT_LABELS[opp.engineering_effort] ?? opp.engineering_effort}
          </span>
          {applied && (
            <span className="opt-applied-chip">✓ Applied {monthLabel(action.applied_on)}</span>
          )}
        </div>
        <div className="opt-item-actions">
          <span className="opt-item-savings">
            {ceiling && <span className="opt-ceiling">up to </span>}
            {money(opp.projected_monthly_savings)}/mo
          </span>
          <button className="opt-apply-btn" onClick={toggle} disabled={busy}>
            {applied ? "Undo" : "Mark as applied"}
          </button>
        </div>
      </div>
      <p className="opt-item-evidence">{opp.evidence}</p>
      {opp.fix && <p className="opt-item-fix muted">{opp.fix}</p>}
      <details className="opt-trail">
        <summary>How to apply &amp; verify</summary>
        <dl className="opt-guidance">
          <dt>Validate</dt>
          <dd>{opp.validation_guidance}</dd>
          <dt>Meter verifies</dt>
          <dd>{opp.verification}</dd>
        </dl>
      </details>
      {opp.trail.length > 0 && (
        <details className="opt-trail">
          <summary>Evidence trail ({opp.trail.length})</summary>
          <ul className="evidence-list">
            {opp.trail.map((t, i) => (
              <li key={i} className="evidence-item">
                <span className="evidence-type">{t.model}</span>
                {t.fingerprint && <span className="evidence-ref">{t.fingerprint}…</span>}
                {t.note && <span className="muted">{t.note}</span>}
                {t.call_count != null && <span className="muted">{num(t.call_count)} repeats</span>}
                {t.prefix_tokens != null && (
                  <span className="muted">
                    {num(t.prefix_tokens)}-tok prefix · {num(t.calls ?? 0)} calls
                    {t.cached != null && ` · ${num(t.cached)} cached`}
                  </span>
                )}
              </li>
            ))}
          </ul>
        </details>
      )}
    </li>
  );
}

function AppliedActions({ actions }: { actions: OptimizationAction[] }) {
  if (actions.length === 0) return null;
  return (
    <div className="opt-applied">
      <h4 className="opt-applied-title">Applied optimizations</h4>
      <span className="section-sub muted">
        Projected savings frozen at apply time, reconciled against the measured drop since.
      </span>
      <table className="mini-table">
        <thead>
          <tr>
            <th>Optimization</th>
            <th>Applied</th>
            <th className="num">Projected</th>
            <th className="num">Realized</th>
          </tr>
        </thead>
        <tbody>
          {actions.map((a) => (
            <tr key={a.lever}>
              <td>{LEVER_TITLES[a.lever] ?? a.lever}</td>
              <td>{monthLabel(a.applied_on)}</td>
              <td className="num">{money(a.projected_monthly)}/mo</td>
              <td className="num">
                {a.status === "pending" ? (
                  <span className="muted">awaiting next period</span>
                ) : (
                  <>
                    <strong className="opt-realized">{money(a.realized_monthly ?? 0)}/mo</strong>
                    {a.status === "verified" && <span className="opt-verified">✓ Verified</span>}
                  </>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function DirectionalGroup({ opps, total }: { opps: Opportunity[]; total: number }) {
  return (
    <div className="opt-group opt-group-estimated">
      <div className="opt-group-head">
        <div>
          <h3 className="opt-group-title">
            Estimated <span className="opt-tag">directional estimate</span>
          </h3>
          <span className="section-sub muted">
            Rules of thumb from this feature&apos;s usage — not guaranteed savings.
          </span>
        </div>
        {total > 0 && (
          <div className="savings-headline">
            <span className="savings-label">Estimated savings</span>
            <span className="savings-month">{money(total)}/mo</span>
          </div>
        )}
      </div>
      {opps.length === 0 ? (
        <p className="muted">No estimated opportunities for this period.</p>
      ) : (
        <table className="mini-table">
          <thead>
            <tr>
              <th>Opportunity</th>
              <th className="num">Potential savings</th>
              <th>Confidence</th>
            </tr>
          </thead>
          <tbody>
            {opps.map((o) => (
              <tr key={o.lever} className={o.overlaps ? "opt-overlapped" : ""}>
                <td title={o.evidence}>
                  {o.title}
                  {o.overlaps && <span className="muted"> · measured as {o.overlaps}</span>}
                </td>
                <td className="num">{money(o.projected_monthly_savings)}/mo</td>
                <td>
                  <ConfidenceBadge level={o.confidence} />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}

function InferenceSection({
  featureId,
  range,
  sourceLabel,
}: {
  featureId: string;
  range: ReviewRange;
  sourceLabel: string;
}) {
  const [data, setData] = useState<FeatureInference | null>(null);
  const [hover, setHover] = useState<number | null>(null);

  useEffect(() => {
    let active = true;
    setData(null);
    setHover(null);
    api
      .featureInference(featureId, range)
      .then((d) => active && setData(d))
      .catch(() => active && setData({ start: "", end: "", total: 0, by_model: [], trend: [] }));
    return () => {
      active = false;
    };
  }, [featureId, range]);

  return (
    <section className="detail-section">
      <div className="section-head">
        <div>
          <h2>Inference cost</h2>
          <span className="section-sub muted">
            {money(data?.total ?? 0)} in period · {sourceLabel}
          </span>
        </div>
      </div>

      {data === null ? (
        <p className="muted">Loading…</p>
      ) : (
        <div className="inference-body">
          <div className="inference-col">
            <span className="chart-title">Trend</span>
            <TrendChart trend={data.trend} />
          </div>
          <div className="inference-col">
            <span className="chart-title">By model</span>
            {data.by_model.length === 0 ? (
              <p className="muted">No inference cost in this period.</p>
            ) : (
              <div className="pie-wrap">
                <DonutPie models={data.by_model} hover={hover} setHover={setHover} />
                <PieCaption models={data.by_model} index={hover ?? 0} />
              </div>
            )}
          </div>
        </div>
      )}
    </section>
  );
}

function DonutPie({
  models,
  hover,
  setHover,
}: {
  models: FeatureInference["by_model"];
  hover: number | null;
  setHover: (i: number | null) => void;
}) {
  let filled = 0;
  return (
    <svg viewBox="0 0 36 36" className="donut" role="img" aria-label="Inference cost by model">
      {models.map((m, i) => {
        const offset = 25 - filled; // start at 12 o'clock, then continue clockwise
        filled += m.pct;
        return (
          <circle
            key={m.model}
            cx="18"
            cy="18"
            r="15.9155"
            fill="none"
            stroke={MODEL_COLORS[i % MODEL_COLORS.length]}
            strokeWidth={hover === i ? 5 : 3.5}
            strokeDasharray={`${m.pct} ${100 - m.pct}`}
            strokeDashoffset={offset}
            onMouseEnter={() => setHover(i)}
            onMouseLeave={() => setHover(null)}
            style={{ cursor: "pointer", transition: "stroke-width 0.1s" }}
          />
        );
      })}
    </svg>
  );
}

function PieCaption({ models, index }: { models: FeatureInference["by_model"]; index: number }) {
  const m = models[Math.min(index, models.length - 1)];
  if (!m) return null;
  const color = MODEL_COLORS[Math.min(index, models.length - 1) % MODEL_COLORS.length];
  return (
    <div className="pie-caption">
      <span className="pie-cap-model">
        <span className="swatch" style={{ background: color }} />
        {m.model}
      </span>
      <span className="pie-cap-cost">
        {money(m.amount)} · {Math.round(m.pct)}%
      </span>
      <span className="muted">{compact(m.requests)} requests</span>
    </div>
  );
}
