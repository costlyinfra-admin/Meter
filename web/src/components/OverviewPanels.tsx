/**
 * The Overview's summary panels: the KPI row, and the three cards beneath it.
 *
 * They live here rather than in Dashboard.tsx because each is a small, self
 * contained reading of data the page already has — keeping them together makes
 * the page component about layout and loading, which is enough for one file.
 *
 * Two rules run through all of them. Build and inference cost are only ever
 * summed to answer "how much in total" or "who do we pay"; every card that
 * shows a feature or a trend keeps them apart. And nothing is drawn that the
 * data does not support: a card with no history shows no sparkline rather than
 * a flat line implying one.
 */
import { useEffect, useState, type ReactNode } from "react";
import { Link } from "react-router-dom";
import type {
  BudgetForecast,
  Dashboard,
  Insight,
  OpenAction,
  ProviderTotal,
  TrendMonth,
} from "../api";
import { FINE_STEPS, GRID_LEVELS, niceCeil } from "./chartAxis";
import { ChartHoverCard, HoverRow } from "./ChartHoverCard";
import { ConnectorMark } from "./ConnectorMark";
import type { SpendSource } from "./ProviderBreakdown";
import { forecastShape, type ForecastPoint, type ForecastShape } from "../budget";
import { compact, compactMoney, money, wholeMoney } from "../format";
import { AskAction } from "./AskMeter";
import { Skeleton } from "./Skeleton";
import { markSeen, seen } from "../once";

/** Remembered per viewer: the first time every dollar is attributed. */
const FULL_COVERAGE = "meter.milestone.full-coverage";
import { ChartLegend, type LegendItem } from "./ChartLegend";
import {
  buildTrendView,
  hasProviderSplit,
  OTHER_KEY,
  providerLabel,
  providerOptions,
  toggled,
  visibleTotal,
  type TrendMeasure,
  type TrendSplit,
  type TrendView,
} from "../spendTrend";

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

function monthLabel(period: string): string {
  return MONTHS[Number(period.slice(5, 7)) - 1];
}

/** A percentage, or an em dash when the base makes one meaningless. */
function share(part: number, whole: number): string {
  if (!whole) return "—";
  const pct = (part / whole) * 100;
  return pct >= 10 ? `${Math.round(pct)}%` : `${pct.toFixed(1)}%`;
}

// ---------------------------------------------------------------------------
// KPI row
// ---------------------------------------------------------------------------

/** A bar per month, scaled to the tallest. Purely a shape — the numbers are
 *  beside it, and this only says whether they have been climbing. */
function Sparkline({ months }: { months: TrendMonth[] }) {
  const values = months.map((m) => m.build_cost + m.inference_cost);
  const max = Math.max(...values, 0);
  if (months.length < 2 || max <= 0) return null;
  return (
    <span className="kpi-spark" aria-hidden>
      {values.map((value, i) => (
        <span key={i} style={{ height: `${Math.max(8, (value / max) * 100)}%` }} />
      ))}
    </span>
  );
}

function Delta({ current, prev, label }: { current: number; prev: number; label: string }) {
  if (prev <= 0) return <span className="muted kpi-note">No prior period to compare</span>;
  const change = ((current - prev) / prev) * 100;
  const up = change >= 0;
  return (
    <span className={`kpi-delta ${up ? "up" : "down"}`}>
      {up ? "▲" : "▼"}{" "}
      {Math.abs(change) >= 10 ? Math.round(Math.abs(change)) : Math.abs(change).toFixed(1)}%
      <span className="muted kpi-note">
        {" "}
        {/* The label carries its own "vs" — "vs last month", "vs prev 3 months". */}
        {label} ({money(prev)})
      </span>
    </span>
  );
}

export interface SavingsSummary {
  potentialMonthly: number;
  realizedMonthly: number;
  realizedAnnual: number;
}

/** Each headline figure is keyed by its value, so a figure that changes — a
 *  new period, a refresh — arrives as a new element and fades in (see
 *  .kpi-value). It never counts up: the numbers in between were never true. */
export function KpiRow({
  data,
  savings,
  savingsFailed,
  deltaLabel,
  onShowSource,
}: {
  data: Dashboard;
  /** Loaded separately, so a slow Optimize calculation never holds up the page. */
  savings: SavingsSummary | null;
  savingsFailed: boolean;
  deltaLabel: string;
  /** Opens the By Provider tab on one half of the build/run split. */
  onShowSource: (source: SpendSource) => void;
}) {
  const totalSpend = data.totals.build_cost + data.totals.inference_cost;
  const prevSpend = data.totals.prev_build_cost + data.totals.prev_inference_cost;
  const unattributed = data.unattributed.build_cost + data.unattributed.inference_cost;
  const coverage = totalSpend > 0 ? ((totalSpend - unattributed) / totalSpend) * 100 : 0;
  // A milestone, marked once: the first time this viewer sees every dollar tied
  // to a feature, a single sheen passes along the coverage bar. After that it
  // is just a full bar — a moment, not a habit.
  const full = totalSpend > 0 && unattributed <= 0;
  const [milestone, setMilestone] = useState(false);
  useEffect(() => {
    if (full && !seen(FULL_COVERAGE)) {
      markSeen(FULL_COVERAGE);
      setMilestone(true);
    }
  }, [full]);

  return (
    <section className="kpi-row" aria-label="Headline figures">
      <article className="kpi-card">
        <h2 className="kpi-label">Total AI spend</h2>
        <div className="kpi-main">
          <span className="kpi-value" key={money(totalSpend)}>
            {money(totalSpend)}
          </span>
          <Sparkline months={data.trend} />
        </div>
        <Delta current={totalSpend} prev={prevSpend} label={deltaLabel} />
        {/* Build and inference are added here only to answer "how much in
            total"; the split is right beneath, and never blended below. Each
            half opens the breakdown that explains it. */}
        <span className="muted kpi-note">
          <button
            type="button"
            className="kpi-note-link"
            onClick={() => onShowSource("build")}
            title="Show build cost by tool and by developer"
          >
            {money(data.totals.build_cost)} build
          </button>{" "}
          ·{" "}
          <button
            type="button"
            className="kpi-note-link"
            onClick={() => onShowSource("inference")}
            title="Show inference (run) cost by provider, model and workspace"
          >
            {money(data.totals.inference_cost)} run
          </button>
        </span>
        {data.totals.estimated_inference > 0 && (
          <span className="muted kpi-note" title="Recent usage the provider has not billed yet">
            incl. ~{money(data.totals.estimated_inference)} estimated
          </span>
        )}
      </article>

      <article className="kpi-card">
        <h2 className="kpi-label">Potential savings</h2>
        {savings ? (
          <>
            <div className="kpi-main">
              <span className="kpi-value" key={money(savings.potentialMonthly)}>
                {money(savings.potentialMonthly)}
                <span className="kpi-unit"> / mo</span>
              </span>
            </div>
            <span className="muted kpi-note">
              {share(savings.potentialMonthly, totalSpend)} of total spend
            </span>
          </>
        ) : (
          <Pending failed={savingsFailed} />
        )}
        <Link className="kpi-link" to="/optimize">
          View opportunities →
        </Link>
      </article>

      <article className="kpi-card">
        <h2 className="kpi-label">Savings realized</h2>
        {savings ? (
          <>
            <div className="kpi-main">
              <span className="kpi-value" key={money(savings.realizedMonthly)}>
                {money(savings.realizedMonthly)}
                <span className="kpi-unit"> / mo</span>
              </span>
            </div>
            <span className="muted kpi-note">
              {money(savings.realizedAnnual)} annualized — verified, not projected
            </span>
          </>
        ) : (
          <Pending failed={savingsFailed} />
        )}
        <Link className="kpi-link" to="/optimize">
          View savings →
        </Link>
      </article>

      <article className="kpi-card">
        <h2 className="kpi-label">Total tokens</h2>
        <div className="kpi-main">
          <span className="kpi-value" key={data.totals.tokens_in + data.totals.tokens_out}>
            {compact(data.totals.tokens_in + data.totals.tokens_out)}
          </span>
        </div>
        <span className="muted kpi-note">
          {compact(data.totals.tokens_in)} in · {compact(data.totals.tokens_out)} out
        </span>
      </article>

      <article className="kpi-card">
        <h2 className="kpi-label">Attribution coverage</h2>
        <div className="kpi-main">
          <span className="kpi-value" key={coverage.toFixed(1)}>
            {totalSpend > 0 ? `${coverage.toFixed(1)}%` : "—"}
          </span>
        </div>
        <span className={milestone ? "kpi-bar sheen" : "kpi-bar"} aria-hidden>
          <span style={{ width: `${Math.max(0, Math.min(100, coverage))}%` }} />
        </span>
        <span className={milestone ? "muted kpi-note milestone-note" : "muted kpi-note"}>
          {unattributed > 0
            ? `${money(unattributed)} unattributed (${share(unattributed, totalSpend)})`
            : "Every dollar is tied to a feature"}
        </span>
        {unattributed > 0 && (
          <Link className="kpi-link" to="/cost-sources">
            Resolve unattributed →
          </Link>
        )}
      </article>
    </section>
  );
}

/** The savings cards before their own request lands, or after it fails. Never a
 *  zero — an unknown figure and a figure of nothing are different answers. */
function Pending({ failed }: { failed: boolean }) {
  return (
    <div className="kpi-main">
      <span className="kpi-value muted">—</span>
      <span className="muted kpi-note">{failed ? "Unavailable" : "Calculating…"}</span>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Key insights
// ---------------------------------------------------------------------------
const INSIGHT_TONE: Record<string, string> = {
  spike: "warn",
  trend: "warn",
  "trend-down": "good",
  pace: "warn",
  "pace-down": "good",
  waste: "warn",
  governance: "warn",
  coverage: "warn",
  resource: "warn",
  concentration: "info",
  efficiency: "info",
  split: "info",
  cache: "info",
};

/** A mark per kind of finding, so the list can be read by shape before it is
 *  read by word. Same family as the navigation icons: one 20x20 grid, one
 *  stroke weight, currentColor — the tone comes from the row. */
const INSIGHT_PATHS: Record<string, string> = {
  // A jagged peak: one day far above the rest.
  spike: "M2.5 13.5 6 8l3 3.5L12.5 4l5 9.5",
  // Arrows for direction of travel.
  trend: "M2.5 14 8 8.5l3 3 6.5-6.5 M13 5h4.5v4.5",
  "trend-down": "M2.5 6 8 11.5l3-3 6.5 6.5 M13 15h4.5v-4.5",
  // A clock: a projection, which is about time not size.
  pace: "M10 4.5a5.5 5.5 0 1 1 0 11 5.5 5.5 0 0 1 0-11Z M10 7v3.2l2.2 1.3",
  "pace-down": "M10 4.5a5.5 5.5 0 1 1 0 11 5.5 5.5 0 0 1 0-11Z M10 7v3.2l2.2 1.3",
  // A warning: spend to look at.
  waste: "M10 3.6 17.5 16.4H2.5L10 3.6Z M10 8.3v3.1 M10 13.6h.01",
  // A tag with no string: spend not tied to a feature.
  governance: "M9.4 3.5H16v6.6L10.6 16.5 3.5 9.4 9.4 3.5Z M12.9 6.9h.01",
  coverage: "M9.4 3.5H16v6.6L10.6 16.5 3.5 9.4 9.4 3.5Z M12.9 6.9h.01",
  // A key: this is about API keys and workspaces.
  resource: "M12.8 4.5a3.2 3.2 0 1 1-2.9 4.5L4 15v2.5h2.5v-2h2v-2h2l1.4-1.4a3.2 3.2 0 0 1 .9-7.6Z",
  // Bars, one taller: one feature dominating.
  concentration: "M4 16.5v-4 M8 16.5v-9 M12 16.5v-6 M16 16.5v-11",
  // Two people: cost per active user.
  efficiency:
    "M7.5 9.5a2.4 2.4 0 1 1 0-4.8 2.4 2.4 0 0 1 0 4.8Z M3 16.2c0-2.2 2-3.6 4.5-3.6" +
    "s4.5 1.4 4.5 3.6 M13.5 5.1a2.4 2.4 0 0 1 0 4.4 M14.5 12.9c1.6.5 2.5 1.7 2.5 3.3",
  // A circle with a wedge: two shares of one whole.
  split: "M10 3.5a6.5 6.5 0 1 0 6.5 6.5H10V3.5Z",
  // Stacked discs: cached reads.
  cache:
    "M10 3.5c3.6 0 6.5 1 6.5 2.2S13.6 8 10 8 3.5 6.9 3.5 5.7 6.4 3.5 10 3.5Z" +
    " M3.5 5.7v8.6c0 1.2 2.9 2.2 6.5 2.2s6.5-1 6.5-2.2V5.7 M3.5 10c0 1.2 2.9 2.2 6.5 2.2" +
    "s6.5-1 6.5-2.2",
};

const FALLBACK_PATH = "M10 4.5a5.5 5.5 0 1 1 0 11 5.5 5.5 0 0 1 0-11Z M10 7.2v3.4 M10 13h.01";

function InsightIcon({ kind }: { kind: string }) {
  return (
    <svg
      viewBox="0 0 20 20"
      width="16"
      height="16"
      aria-hidden
      className="insight-icon"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.5"
      strokeLinecap="round"
      strokeLinejoin="round"
    >
      <path d={INSIGHT_PATHS[kind] ?? FALLBACK_PATH} />
    </svg>
  );
}

export function KeyInsights({ insights }: { insights: Insight[] }) {
  if (insights.length === 0) return null;
  return (
    <section className="panel insights-panel" aria-label="Key insights">
      <div className="panel-head">
        <h2>Key insights</h2>
        <AskAction
          source="insights"
          question="Walk me through the key insights on my Overview. Which one should I act on first, and why?"
        />
      </div>
      {/* Four is where spreading the rows down the panel reads as a roomy list
          rather than as two items adrift in a tall box. */}
      <ul className={`insight-list ${insights.length >= 4 ? "spread" : ""}`}>
        {insights.map((insight, i) => (
          <li key={i} className={`insight-item tone-${INSIGHT_TONE[insight.kind] ?? "info"}`}>
            <InsightIcon kind={insight.kind} />
            <span className="insight-body">
              <span className="insight-text">{insight.text}</span>
              {insight.detail && <span className="muted insight-detail">{insight.detail}</span>}
            </span>
          </li>
        ))}
      </ul>
    </section>
  );
}

// ---------------------------------------------------------------------------
// Spend trend
// ---------------------------------------------------------------------------
// A fixed viewBox: the panel is fluid, so drawing to a constant grid keeps the
// bars and the axis in step at any width.
const VB_W = 320;
const VB_H = 172;
const AXIS_W = 40; // room for the dollar labels
const PLOT_TOP = 8;
const PLOT_BOTTOM = 150; // baseline; month labels sit below it

/** A row of pill buttons, one of which is chosen. */
function Segmented<T extends string>({
  label,
  value,
  options,
  onChange,
}: {
  label: string;
  value: T;
  options: { value: T; label: string }[];
  onChange: (value: T) => void;
}) {
  return (
    <div className="trend-toggle chart-seg" role="group" aria-label={label}>
      {options.map((option) => (
        <button
          key={option.value}
          type="button"
          className={option.value === value ? "active" : undefined}
          aria-pressed={option.value === value}
          onClick={() => onChange(option.value)}
        >
          {option.label}
        </button>
      ))}
    </div>
  );
}

function trendChartLabel(
  measure: TrendMeasure,
  split: TrendSplit,
  provider: string | null,
): string {
  const what =
    measure === "cost"
      ? split === "kind"
        ? "Build and inference cost per month"
        : "Cost per month by provider"
      : split === "kind"
        ? "Tokens per month by type"
        : "Tokens per month by provider";
  return provider === null ? what : `${what}, ${providerLabel(provider)} only`;
}

function emptyTrendText(measure: TrendMeasure, provider: string | null): string {
  if (provider !== null) return `Nothing from ${providerLabel(provider)} in this period.`;
  return measure === "cost" ? "No spend in this period." : "No tokens recorded in this period.";
}

export function SpendTrend({
  trend,
  provider = null,
  onProviderChange,
  linkedMonth = null,
  onHoverMonth,
}: {
  trend: TrendMonth[];
  /** One vendor to narrow the chart to, or null for everything. Owned by the
   *  page, because the provider list beside the chart sets it too. */
  provider?: string | null;
  onProviderChange?: (provider: string | null) => void;
  /** The month the pointer is over in the budget chart beside this one, so
   *  hovering either chart lights up the same month in both. */
  linkedMonth?: number | null;
  onHoverMonth?: (month: number | null) => void;
}) {
  // Which month the pointer is over. Null is "not hovering", which is why the
  // dimming and the card both key off it rather than off a separate flag.
  const [hover, setLocalHover] = useState<number | null>(null);
  const setHover = (month: number | null) => {
    setLocalHover(month);
    onHoverMonth?.(month);
  };
  // What the bars dim around: this chart's own pointer, else the linked one.
  // The card opens only under the pointer, never for the other chart.
  const focus = hover ?? linkedMonth;
  // The series a legend entry is pointing at: the others fade until it leaves.
  const [preview, setPreview] = useState<string | null>(null);
  const [measure, setMeasure] = useState<TrendMeasure>("cost");
  const [split, setSplit] = useState<TrendSplit>("kind");
  // Hidden series belong to the view they were hidden in: a different measure,
  // split or filter has different series, and carrying "anthropic is hidden"
  // over into the build/inference view would hide nothing and confuse.
  const viewKey = `${measure}|${split}|${provider ?? ""}`;
  const [hiddenIn, setHiddenIn] = useState<{ view: string; keys: Set<string> }>({
    view: viewKey,
    keys: new Set(),
  });
  const hidden = hiddenIn.view === viewKey ? hiddenIn.keys : EMPTY_SET;
  const setHidden = (keys: Set<string>) => setHiddenIn({ view: viewKey, keys });

  const canSlice = hasProviderSplit(trend);
  const options = canSlice ? providerOptions(trend) : [];
  // A vendor the window no longer contains (the range changed under it) is
  // dropped rather than left filtering the chart down to nothing.
  const filter = provider !== null && options.includes(provider) ? provider : null;

  const view = buildTrendView(trend, measure, canSlice ? split : "kind", filter);
  const totals = view.values.map((row) => visibleTotal(row, view.series, hidden));
  const rawMax = Math.max(...view.values.map((row) => row.reduce((a, b) => a + b, 0)), 0);
  const max = Math.max(...totals, 0);
  const ceiling = niceCeil(max);
  const y = (value: number) => PLOT_BOTTOM - (value / ceiling) * (PLOT_BOTTOM - PLOT_TOP);
  const fmt = measure === "cost" ? money : compact;
  const axisFmt = measure === "cost" ? wholeMoney : compact;

  const plotW = VB_W - AXIS_W - 6;
  const slot = plotW / Math.max(trend.length, 1);
  const barW = Math.min(slot * 0.55, 28);
  const centre = (i: number) => AXIS_W + i * slot + slot / 2;

  const hovered = hover === null ? null : trend[hover];
  const legend: LegendItem[] = view.series.map((s, i) => ({
    key: s.key,
    label: s.label,
    color: s.color,
    value: fmt(view.values.reduce((sum, row) => sum + row[i], 0)),
  }));

  return (
    <section className="panel trend-panel" aria-label="Spend trend">
      <div className="panel-head">
        <h2>Spend trend</h2>
        <AskAction
          source="trend"
          question="What is driving the shape of my spend trend over these months — build, inference, or both?"
        />
      </div>

      <div className="trend-controls">
        <Segmented
          label="Measure"
          value={measure}
          options={[
            { value: "cost", label: "Cost" },
            { value: "tokens", label: "Tokens" },
          ]}
          onChange={setMeasure}
        />
        {canSlice && (
          <Segmented
            label="Split by"
            value={split}
            options={[
              { value: "kind", label: measure === "cost" ? "Build & inference" : "Token type" },
              { value: "provider", label: "Provider" },
            ]}
            onChange={setSplit}
          />
        )}
        {canSlice && options.length > 1 && (
          <select
            className="trend-filter"
            aria-label="Provider filter"
            value={filter ?? ""}
            onChange={(e) => onProviderChange?.(e.target.value || null)}
          >
            <option value="">All providers</option>
            {options.map((name) => (
              <option key={name} value={name}>
                {providerLabel(name)}
              </option>
            ))}
          </select>
        )}
      </div>

      {rawMax <= 0 ? (
        <p className="muted trend-empty">{emptyTrendText(measure, filter)}</p>
      ) : max <= 0 ? (
        // Something to draw, but every series is switched off. Saying so beats
        // an empty frame that reads as "nothing was spent".
        <p className="muted trend-empty">
          Every series is hidden. Click one in the legend below to bring it back.
        </p>
      ) : (
        <div className="trend-line-wrap" onMouseLeave={() => setHover(null)}>
          <svg
            className="trend-svg"
            viewBox={`0 0 ${VB_W} ${VB_H}`}
            role="img"
            aria-label={trendChartLabel(measure, canSlice ? split : "kind", filter)}
          >
            {GRID_LEVELS.map((level) => (
              <g key={level}>
                <line
                  className="trend-grid-line"
                  x1={AXIS_W}
                  y1={y(level * ceiling)}
                  x2={VB_W - 6}
                  y2={y(level * ceiling)}
                />
                <text
                  className="trend-axis-label"
                  x={AXIS_W - 6}
                  y={y(level * ceiling) + 3}
                  textAnchor="end"
                >
                  {axisFmt(level * ceiling)}
                </text>
              </g>
            ))}

            {trend.map((month, i) => {
              const x = centre(i) - barW / 2;
              // Each visible series stacked on the last, first series at the
              // bottom — parts of the month, never one blended number.
              const shown = view.series
                .map((s, j) => ({ s, value: view.values[i][j] }))
                .filter(({ s, value }) => !hidden.has(s.key) && value > 0);
              let base = 0;
              return (
                <g
                  key={month.period}
                  className={
                    focus === null || focus === i ? "trend-bar-group" : "trend-bar-group dim"
                  }
                >
                  <g className="bar-grow" style={{ animationDelay: `${Math.min(i, 24) * 25}ms` }}>
                    {shown.map(({ s, value }, j) => {
                      const h = Math.max((value / ceiling) * (PLOT_BOTTOM - PLOT_TOP), 1);
                      const top = j === shown.length - 1;
                      const rect = (
                        <rect
                          key={s.key}
                          className={
                            preview !== null && preview !== s.key
                              ? `trend-bar-${s.key} seg-dim`
                              : `trend-bar-${s.key}`
                          }
                          style={{ fill: s.color }}
                          x={x}
                          y={y(base + value)}
                          width={barW}
                          height={h}
                          rx={top ? 2 : 0}
                        />
                      );
                      base += value;
                      return rect;
                    })}
                  </g>
                  <text
                    className="trend-axis-label"
                    x={centre(i)}
                    y={PLOT_BOTTOM + 14}
                    textAnchor="middle"
                  >
                    {monthLabel(month.period)}
                  </text>
                </g>
              );
            })}

            {/* One invisible band per month, so a short bar is as easy to hit as
                a tall one and the pointer never falls between two of them. */}
            {trend.map((month, i) => (
              <rect
                key={`hit-${month.period}`}
                x={centre(i) - slot / 2}
                y={0}
                width={slot}
                height={PLOT_BOTTOM}
                fill="transparent"
                onMouseEnter={() => setHover(i)}
              />
            ))}
          </svg>

          {hovered && (
            <ChartHoverCard
              pct={(centre(hover!) / VB_W) * 100}
              title={
                filter === null
                  ? monthLabel(hovered.period)
                  : `${monthLabel(hovered.period)} · ${providerLabel(filter)}`
              }
              total={fmt(totals[hover!])}
            >
              <ul className="trend-hover-list">
                {view.series.map((s, j) =>
                  hidden.has(s.key) ? null : (
                    <HoverRow
                      key={s.key}
                      swatchStyle={{ background: s.color }}
                      label={s.label}
                      value={fmt(view.values[hover!][j])}
                    />
                  ),
                )}
                {/* Split by vendor adds a vendor's build and run together, as
                    the provider list does. The month's own split is still here. */}
                {measure === "cost" && split === "provider" && canSlice && (
                  <VendorKindRows month={hovered} filter={filter} view={view} hidden={hidden} />
                )}
              </ul>
            </ChartHoverCard>
          )}
        </div>
      )}

      {rawMax > 0 && (
        <ChartLegend
          label="Spend trend series"
          items={legend}
          hidden={hidden}
          onToggle={(key) => setHidden(toggled(hidden, key))}
          onShowAll={() => setHidden(new Set())}
          onPreview={setPreview}
        />
      )}
    </section>
  );
}

const EMPTY_SET: ReadonlySet<string> = new Set();

/** The build and inference behind the visible vendors in one month. */
function VendorKindRows({
  month,
  filter,
  view,
  hidden,
}: {
  month: TrendMonth;
  filter: string | null;
  view: TrendView;
  hidden: ReadonlySet<string>;
}) {
  const named = new Set(view.series.map((s) => s.key));
  const otherHidden = hidden.has(OTHER_KEY);
  let build = 0;
  let run = 0;
  for (const row of month.by_provider ?? []) {
    if (filter !== null && row.provider !== filter) continue;
    const key = named.has(row.provider) ? row.provider : OTHER_KEY;
    if (key === OTHER_KEY ? otherHidden : hidden.has(key)) continue;
    build += row.build_cost;
    run += row.inference_cost;
  }
  return (
    <>
      <HoverRow label="of which build" value={money(build)} muted />
      <HoverRow label="of which inference" value={money(run)} muted />
    </>
  );
}

// ---------------------------------------------------------------------------
// Budget & forecast
// ---------------------------------------------------------------------------
// Same 320-unit width as the spend trend above it, so the two charts in this
// column are drawn to one grid — but half the height. This one carries a single
// line and a reference level, and the card below it has figures to fit in.
const BF_VB_W = 320;
const BF_VB_H = 104;
const BF_AXIS_W = 44;
const BF_PLOT_TOP = 12;
const BF_PLOT_BOTTOM = 78;
/** Three levels, not the trend chart's five: at this height five would crowd. */
const BF_GRID_LEVELS = [0, 0.5, 1] as const;

/** Cumulative spend against the budget: solid where it happened, dashed where
 *  it is a projection. A closed period draws no dashes at all. */
function BudgetChart({
  shape,
  forecast,
  trend,
  linkedMonth = null,
  onHoverMonth,
}: {
  shape: ForecastShape;
  forecast: BudgetForecast;
  /** The same months the line is drawn from, for the hover card's per-month split. */
  trend: TrendMonth[];
  /** The month hovered in the spend trend; see SpendTrend. */
  linkedMonth?: number | null;
  onHoverMonth?: (month: number | null) => void;
}) {
  const [hover, setLocalHover] = useState<number | null>(null);
  const setHover = (month: number | null) => {
    setLocalHover(month);
    onHoverMonth?.(month);
  };
  // The guide follows either chart's pointer; the card only this one's.
  const focus = hover ?? (linkedMonth !== null && linkedMonth < shape.months ? linkedMonth : null);
  // Lines switched off from the legend. The axis scales to what is still shown,
  // so hiding a far-off budget lets the spend line use the whole height.
  const [hidden, setHidden] = useState<ReadonlySet<string>>(EMPTY_SET);
  const shows = (key: string) => !hidden.has(key);

  const over = forecast.variance !== null && forecast.variance > 0;
  const legend: LegendItem[] = [
    { key: "actual", label: "Spent", swatchClass: "line actual" },
    ...(shape.projected
      ? [
          {
            key: "forecast",
            label: "Forecast",
            swatchClass: `line dashed ${over ? "over" : "under"}`,
          },
        ]
      : []),
    ...(shape.optimizedTail
      ? [{ key: "optimized", label: "With savings", swatchClass: "line dashed optimized" }]
      : []),
    ...(forecast.budget !== null
      ? [{ key: "budget", label: "Budget", swatchClass: "line budget" }]
      : []),
  ];

  // What was spent always counts towards the scale, hidden or not: the hover
  // marker sits on it, and must never land above the top of the chart.
  const top = Math.max(
    forecast.actual,
    shows("forecast") ? (forecast.forecast ?? 0) : 0,
    shows("optimized") ? (forecast.forecast_optimized ?? 0) : 0,
    shows("budget") ? (forecast.budget ?? 0) : 0,
  );
  const ceiling = niceCeil(top, FINE_STEPS);
  const px = (x: number) => BF_AXIS_W + (x / shape.months) * (BF_VB_W - BF_AXIS_W - 8);
  const py = (y: number) => BF_PLOT_BOTTOM - (y / ceiling) * (BF_PLOT_BOTTOM - BF_PLOT_TOP);
  const path = (points: ForecastPoint[]) =>
    points.map((p, i) => `${i === 0 ? "M" : "L"}${px(p.x)} ${py(p.y)}`).join(" ");

  const lastActual = shape.actual[shape.actual.length - 1];
  const end = shape.projected?.[1] ?? null;
  const optimizedEnd = shape.optimizedTail?.[1] ?? null;

  const slot = (BF_VB_W - BF_AXIS_W - 8) / shape.months;
  // The cumulative point for month i — index 0 of shape.actual is the origin.
  const pointAt = (i: number) => shape.actual[i + 1];
  const isFinal = (i: number) => i === shape.months - 1;

  return (
    <>
      <div className="trend-line-wrap" onMouseLeave={() => setHover(null)}>
        <svg
          className="trend-svg budget-svg"
          viewBox={`0 0 ${BF_VB_W} ${BF_VB_H}`}
          role="img"
          aria-label={budgetChartLabel(forecast)}
        >
          {BF_GRID_LEVELS.map((level) => (
            <g key={level}>
              <line
                className="trend-grid-line"
                x1={BF_AXIS_W}
                y1={py(level * ceiling)}
                x2={BF_VB_W - 8}
                y2={py(level * ceiling)}
              />
              <text
                className="trend-axis-label"
                x={BF_AXIS_W - 6}
                y={py(level * ceiling) + 3}
                textAnchor="end"
              >
                {wholeMoney(level * ceiling)}
              </text>
            </g>
          ))}

          {/* The budget, drawn across the whole plot so it reads as a ceiling
            rather than as another series. Absent when there is no budget. */}
          {forecast.budget !== null && shows("budget") && (
            <g>
              <line
                className="budget-line"
                x1={BF_AXIS_W}
                y1={py(forecast.budget)}
                x2={BF_VB_W - 8}
                y2={py(forecast.budget)}
              />
              <text className="budget-line-label" x={BF_AXIS_W + 4} y={py(forecast.budget) - 5}>
                Budget {compactMoney(forecast.budget)}
              </text>
            </g>
          )}

          {shows("actual") && (
            <path className="budget-actual line-draw" d={path(shape.actual)} pathLength={1} />
          )}

          {shape.optimizedTail && optimizedEnd && shows("optimized") && (
            <>
              <path className="budget-optimized" d={path(shape.optimizedTail)} />
              <circle
                className="budget-dot optimized"
                cx={px(optimizedEnd.x)}
                cy={py(optimizedEnd.y)}
                r={3}
              />
            </>
          )}

          {shape.projected && end && shows("forecast") && (
            <>
              <path
                className={`budget-projected ${over ? "over" : "under"}`}
                d={path(shape.projected)}
              />
              <circle
                className={`budget-dot ${over ? "over" : "under"}`}
                cx={px(end.x)}
                cy={py(end.y)}
                r={3}
              />
            </>
          )}

          {/* Where the actuals stop and the projection starts. */}
          {shows("actual") && (
            <circle
              className="budget-dot actual"
              cx={px(lastActual.x)}
              cy={py(lastActual.y)}
              r={3}
            />
          )}

          {/* A guide down to the hovered month, so the card and the line agree. */}
          {focus !== null && (
            <line
              className="trend-line-guide"
              x1={px(pointAt(focus).x)}
              y1={BF_PLOT_TOP - 4}
              x2={px(pointAt(focus).x)}
              y2={BF_PLOT_BOTTOM}
            />
          )}
          {focus !== null && (
            <circle
              className="budget-dot actual"
              cx={px(pointAt(focus).x)}
              cy={py(pointAt(focus).y)}
              r={4}
            />
          )}

          {shape.labels.map((label, i) => (
            <text
              className="trend-axis-label"
              key={`${label}-${i}`}
              x={BF_AXIS_W + (i + 0.5) * slot}
              y={BF_PLOT_BOTTOM + 13}
              textAnchor="middle"
            >
              {label}
            </text>
          ))}

          {/* Contiguous invisible bands: one month each, the full height of the
            plot, so the card opens the moment the pointer enters a month. */}
          {shape.labels.map((label, i) => (
            <rect
              key={`hit-${label}-${i}`}
              x={BF_AXIS_W + i * slot}
              y={0}
              width={slot}
              height={BF_PLOT_BOTTOM}
              fill="transparent"
              onMouseEnter={() => setHover(i)}
            />
          ))}
        </svg>

        {hover !== null && (
          <ChartHoverCard
            pct={(px(pointAt(hover).x) / BF_VB_W) * 100}
            title={`${shape.labels[hover]} · cumulative`}
            total={money(pointAt(hover).y)}
          >
            <ul className="trend-hover-list">
              <HoverRow
                swatch="trend-key-swatch build"
                label="Build this month"
                value={money(trend[hover]?.build_cost ?? 0)}
              />
              <HoverRow
                swatch="trend-key-swatch run"
                label="Inference this month"
                value={money(trend[hover]?.inference_cost ?? 0)}
              />
              {/* The final month is where the projection lives, so its card is the
                one that carries the forecast and the budget it is measured on. */}
              {isFinal(hover) && forecast.forecast !== null && forecast.status !== "closed" && (
                <HoverRow label="Forecast at month end" value={money(forecast.forecast)} />
              )}
              {isFinal(hover) && forecast.forecast_optimized !== null && (
                <HoverRow label="With savings" value={money(forecast.forecast_optimized)} />
              )}
              {isFinal(hover) && forecast.budget !== null && (
                <HoverRow label="Budget" value={money(forecast.budget)} muted />
              )}
            </ul>
          </ChartHoverCard>
        )}
      </div>
      <ChartLegend
        label="Budget chart lines"
        items={legend}
        hidden={hidden}
        onToggle={(key) => setHidden(toggled(hidden, key))}
        onShowAll={() => setHidden(EMPTY_SET)}
      />
    </>
  );
}

function budgetChartLabel(f: BudgetForecast): string {
  const spend =
    f.status === "closed"
      ? `Final spend ${money(f.actual)}.`
      : `${money(f.actual)} spent so far, forecast ${f.forecast === null ? "unavailable" : money(f.forecast)}.`;
  const budget =
    f.budget === null
      ? "No budget is configured."
      : `Budget ${money(f.budget)}${f.variance_pct === null ? "" : `, ${varianceWords(f)}`}.`;
  const optimized =
    f.forecast_optimized === null
      ? ""
      : ` With identified savings, ${money(f.forecast_optimized)}.`;
  return `Cumulative spend against budget. ${spend} ${budget}${optimized}`;
}

function budgetLineTitle(f: BudgetForecast): string {
  const detail = f.budget_detail;
  if (!detail || detail.fully_covered) {
    return `Budget for this period: ${money(f.budget)}`;
  }
  // A prorated figure is rarely the round number the customer typed in, so the
  // tooltip says where it came from rather than leaving them to work it out.
  return (
    `Budget for this period: ${money(f.budget)} — ${detail.covered_days} of ` +
    `${detail.window_days} days covered by the ${detail.method} budget, ` +
    `from ${detail.covered_start}.`
  );
}

/** "17% over budget" / "39% under budget". Rounded once, in one place. */
function varianceWords(f: BudgetForecast): string {
  const pct = Math.abs(Math.round(f.variance_pct ?? 0));
  return `${pct}% ${(f.variance ?? 0) > 0 ? "over" : "under"} budget`;
}

/** The card's chrome, so every state below is the same box in the same place. */
function BudgetShell({ children, status }: { children: ReactNode; status?: ReactNode }) {
  return (
    <section className="panel budget-panel" aria-label="Budget and forecast">
      <div className="panel-head">
        <h2>Budget &amp; forecast</h2>
        <AskAction
          source="forecast"
          question="How does Meter forecast my spend against budget, and what should I do if the forecast is over?"
        />
        {status}
      </div>
      {children}
    </section>
  );
}

/**
 * Where the period lands against the organization's budget.
 *
 * Every figure here comes from the server, which reads the stored budget and the
 * observed daily spend. This component chooses between states and draws; it does
 * not compute money. When there is no budget it says so and offers the place to
 * set one, rather than showing a plausible number nobody agreed to.
 */
export function BudgetForecastPanel({
  trend,
  forecast,
  failed,
  linkedMonth = null,
  onHoverMonth,
}: {
  trend: TrendMonth[];
  /** Null while the forecast request is in flight. */
  forecast: BudgetForecast | null;
  failed: boolean;
  /** The month hovered in the spend trend, lit here too. */
  linkedMonth?: number | null;
  onHoverMonth?: (month: number | null) => void;
}) {
  if (failed) {
    return (
      <BudgetShell>
        <p className="muted budget-empty">
          Budget and forecast are unavailable right now. Refresh to try again.
        </p>
      </BudgetShell>
    );
  }

  if (!forecast) {
    return (
      <BudgetShell>
        <Skeleton variant="chart" label="Calculating…" />
      </BudgetShell>
    );
  }

  // No budget: the one state that is a call to action rather than a reading.
  if (forecast.budget === null) {
    return (
      <BudgetShell>
        <p className="budget-headline">No budget configured</p>
        <p className="muted budget-empty">
          Set a monthly or annual AI budget and this card will track the period against it, forecast
          where it lands, and show what the identified savings would change.
        </p>
        <Link className="kpi-link budget-cta" to="/settings#budgets">
          Set a budget →
        </Link>
      </BudgetShell>
    );
  }

  const shape = forecastShape(trend, forecast);
  const closed = forecast.status === "closed";

  // Open, but nothing observed to project from. The budget and what has been
  // spent are still real, so they are still shown.
  if (forecast.forecast === null) {
    return (
      <BudgetShell>
        <p className="budget-headline">Forecast unavailable</p>
        <p className="muted budget-empty">
          No daily spend has been recorded for this month yet, so there is nothing to project from.{" "}
          {money(forecast.actual)} of a {compactMoney(forecast.budget)} budget is spent.
        </p>
      </BudgetShell>
    );
  }

  return (
    <BudgetShell
      status={
        <span
          className={`budget-status ${(forecast.variance ?? 0) > 0 ? "over" : "under"}`}
          title={
            closed
              ? "The period is over; this compares final spend with the budget."
              : "Where the period is forecast to land against the budget."
          }
        >
          {varianceWords(forecast)}
        </span>
      }
    >
      <p className="budget-headline">
        {closed ? "Final spend: " : "Forecast: "}
        <strong key={closed ? forecast.actual : forecast.forecast}>
          {compactMoney(closed ? forecast.actual : forecast.forecast)}
        </strong>
      </p>

      {shape && (
        <BudgetChart
          shape={shape}
          forecast={forecast}
          trend={trend}
          linkedMonth={linkedMonth}
          onHoverMonth={onHoverMonth}
        />
      )}

      <dl className="budget-stats">
        <div>
          <dt>Budget</dt>
          <dd title={budgetLineTitle(forecast)}>{compactMoney(forecast.budget)}</dd>
        </div>
        <div>
          <dt>{closed ? "Final" : "Spent so far"}</dt>
          <dd title={money(forecast.actual)}>{compactMoney(forecast.actual)}</dd>
        </div>
        <div>
          <dt>{closed ? "Variance" : "With savings"}</dt>
          <dd
            title={
              closed
                ? money(forecast.variance)
                : forecast.forecast_optimized === null
                  ? "No identified savings for this period."
                  : `${money(forecast.identified_savings)} of identified savings applied to the forecast.`
            }
          >
            {closed
              ? compactMoney(forecast.variance)
              : forecast.forecast_optimized === null
                ? "—"
                : compactMoney(forecast.forecast_optimized)}
          </dd>
        </div>
      </dl>

      <p className="muted budget-foot">{budgetFootnote(forecast)}</p>
    </BudgetShell>
  );
}

/** One sentence saying what the reader should take from the card. */
function budgetFootnote(f: BudgetForecast): string {
  if (f.status === "closed") {
    return (
      `This period closed on ${f.window_end}. Final spend was ` +
      `${varianceWords(f)} — no forecast applies.`
    );
  }
  const basis =
    f.method === "recent_weighted"
      ? `weighted towards the last week of ${f.observed_days} days observed`
      : `the average of ${f.observed_days} days observed so far`;
  if (f.forecast_optimized !== null && f.budget !== null) {
    if ((f.variance ?? 0) > 0 && f.forecast_optimized <= f.budget) {
      return `Applying identified savings would bring forecasted spend within budget. Projected from ${basis}.`;
    }
    if ((f.variance ?? 0) > 0) {
      return `Identified savings would not be enough to bring this period within budget. Projected from ${basis}.`;
    }
  }
  return `This period is forecast to land within budget. Projected from ${basis}.`;
}

// ---------------------------------------------------------------------------
// Provider spend
// ---------------------------------------------------------------------------
/** How many vendors the panel shows before it asks to be opened. Three answers
 *  "who are we mostly paying"; the rest is a follow-up question. */
const PROVIDERS_SHOWN = 3;

function ProviderRow({
  provider,
  selected,
  onSelect,
}: {
  provider: ProviderTotal;
  selected: boolean;
  onSelect?: (provider: string | null) => void;
}) {
  const body = (
    <>
      <ConnectorMark type={provider.provider} name={provider.provider} />
      <span className="provider-name">{providerLabel(provider.provider)}</span>
      <span className="provider-amount">{money(provider.amount)}</span>
      <span className="muted provider-share">{Math.round(provider.share)}%</span>
      <span className="provider-bar" aria-hidden>
        <span style={{ width: `${provider.share}%` }} />
      </span>
    </>
  );
  if (!onSelect) return <li>{body}</li>;
  // Clicking a vendor narrows the spend trend to it; clicking it again widens
  // the trend back out. The row is the control, so it is a button.
  return (
    <li>
      <button
        type="button"
        className={`provider-row${selected ? " selected" : ""}`}
        aria-pressed={selected}
        title={
          selected
            ? "Show every provider in the spend trend"
            : `Show only ${providerLabel(provider.provider)} in the spend trend`
        }
        onClick={() => onSelect(selected ? null : provider.provider)}
      >
        {body}
      </button>
    </li>
  );
}

export function ProviderSpendPanel({
  providers,
  selected = null,
  onSelect,
}: {
  providers: ProviderTotal[];
  /** The vendor the spend trend is narrowed to, if any. */
  selected?: string | null;
  onSelect?: (provider: string | null) => void;
}) {
  const [open, setOpen] = useState(false);
  const shown = providers.slice(0, PROVIDERS_SHOWN);
  const rest = providers.slice(PROVIDERS_SHOWN);
  const restTotal = rest.reduce((sum, p) => sum + p.amount, 0);
  const restShare = rest.reduce((sum, p) => sum + p.share, 0);

  return (
    <section className="panel provider-panel" aria-label="Provider spend">
      <div className="panel-head">
        <h2>Provider spend</h2>
        <Link className="panel-link" to="/cost-sources">
          View all →
        </Link>
      </div>
      {providers.length === 0 ? (
        <p className="muted">No provider spend in this period.</p>
      ) : (
        <>
          <ul className="provider-list">
            {shown.map((provider) => (
              <ProviderRow
                key={provider.provider}
                provider={provider}
                selected={provider.provider === selected}
                onSelect={onSelect}
              />
            ))}
          </ul>
          {rest.length > 0 && (
            <>
              {/* Rendered whether open or not, so the browser has something to
                  animate down and a page search still finds a vendor in it. */}
              <div className={`provider-more ${open ? "open" : ""}`}>
                <ul className="provider-list">
                  {rest.map((provider) => (
                    <ProviderRow
                      key={provider.provider}
                      provider={provider}
                      selected={provider.provider === selected}
                      onSelect={onSelect}
                    />
                  ))}
                </ul>
              </div>
              <button
                type="button"
                className="link provider-toggle"
                aria-expanded={open}
                onClick={() => setOpen((was) => !was)}
              >
                <span className="provider-toggle-chevron" aria-hidden>
                  ›
                </span>
                {open
                  ? "Show fewer"
                  : `${rest.length} more · ${money(restTotal)} (${Math.round(restShare)}%)`}
              </button>
            </>
          )}
        </>
      )}
    </section>
  );
}

// ---------------------------------------------------------------------------
// Open actions
// ---------------------------------------------------------------------------
export function OpenActions({ actions }: { actions: OpenAction[] }) {
  return (
    <section className="panel actions-panel" aria-label="Open actions">
      <div className="panel-head">
        <h2>Open actions</h2>
        <AskAction
          source="actions"
          question="What are the open actions on my Overview asking me to do, and what happens to my numbers if I do them?"
        />
      </div>
      {actions.length === 0 ? (
        // An empty list is a real answer here, not a blank state.
        <p className="muted">Nothing needs attention.</p>
      ) : (
        <ul className="action-list">
          {actions.map((action) => (
            <li key={action.kind}>
              <Link to={action.href} className={`action-item tone-${action.tone}`}>
                <span className="action-dot" aria-hidden />
                <span className="action-body">
                  <span className="action-title">{action.title}</span>
                  <span className="muted action-detail">{action.detail}</span>
                </span>
                <span className="action-chevron" aria-hidden>
                  ›
                </span>
              </Link>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
