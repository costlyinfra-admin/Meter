/**
 * Forecast — where AI spend is heading, and what is pushing it there.
 *
 * The Overview's budget panel answers "where does this month land?". This page
 * answers the questions a CFO asks next: what about the next three months, which
 * feature is driving it, how sure is the number, and how do I hear about an
 * overrun before it happens rather than after.
 *
 * Every figure is computed on the server (forecast.py) and shown with its
 * method and its range, because a projection without either is a guess wearing
 * a number. Inference and build are drawn as separate segments of each bar —
 * never merged into one series — and the only combined figure is labelled as
 * the total a budget is measured against.
 */
import { useEffect, useLayoutEffect, useRef, useState, type RefObject } from "react";
import { Link } from "react-router-dom";
import { api, ApiError, type Forecast, type ForecastDriver } from "../api";
import { monthLabel } from "../budget";
import { FINE_STEPS, GRID_LEVELS, niceCeil } from "../components/chartAxis";
import { ChartHoverCard, HoverRow } from "../components/ChartHoverCard";
import { compactMoney, money, wholeMoney } from "../format";

/** The query string the "alert me" link carries, which AlertFormPage prefills. */
const ALERT_LINK = "/alerts/new?metric=combined_cost&condition=forecast_budget_pct";

/** What each method means, in words a budget holder can act on. A month can be
 *  made of several — a connector reporting daily, the SDK reporting monthly, a
 *  self-hosted pool booked whole — so each is its own clause. */
const METHOD_TEXT: Record<string, string> = {
  recent_weighted: "spend a connector reports day by day, weighted towards the last seven days",
  month_to_date_average: "spend a connector reports day by day, at its average so far",
  month_to_date_prorata:
    "spend reported only as a monthly total, carried forward at its average so far — there are no daily figures to read a trend from",
  monthly_allocation: "self-hosted capacity, already booked for the whole month",
};

function MethodClauses({ method }: { method: string }) {
  const parts = method.split("+").filter((m) => m && m !== "none");
  if (parts.length === 0) return <>nothing yet — there is no spend this month.</>;
  if (parts.length === 1) return <>{METHOD_TEXT[parts[0]] ?? parts[0]}.</>;
  return (
    <ul>
      {parts.map((m) => (
        <li key={m}>{METHOD_TEXT[m] ?? m}</li>
      ))}
    </ul>
  );
}

function Range({ low, high }: { low: number | null; high: number | null }) {
  if (low === null || high === null) return null;
  if (low === high) return null; // exact: nothing to show a range of
  return (
    <span className="forecast-range muted">
      {compactMoney(low)} – {compactMoney(high)}
    </span>
  );
}

function BudgetStatus({ f }: { f: Forecast }) {
  const om = f.open_month;
  if (!f.has_budget) {
    return (
      <div className="forecast-status">
        <p>
          Set a budget in <Link to="/settings">Settings</Link> to see where each month lands against
          it, and to be warned before it is exceeded.
        </p>
      </div>
    );
  }
  if (om.projected_budget_pct === null) return null;
  const over = om.projected_budget_pct > 100;
  return (
    <div className={`forecast-status ${over ? "forecast-status-over" : ""}`} role="status">
      <p>
        This month is projected to reach{" "}
        <strong>{om.projected_budget_pct.toFixed(0)}% of budget</strong> —{" "}
        {money(om.total_projected)} against {money(om.budget)}.{over && " That is over."}
      </p>
      {/* The point of the page: hear about it before it happens. The budget
          alert that already existed fires on ACTUAL spend, once it is too late
          to act on this month. */}
      <Link to={ALERT_LINK} className="btn">
        Alert me before this happens
      </Link>
    </div>
  );
}

function MonthCard({
  label,
  inference,
  build,
  buildIsActual,
  total,
  budget,
  pct,
}: {
  label: string;
  inference: { projected: number | null; low: number | null; high: number | null };
  build: number | null;
  buildIsActual: boolean;
  total: number | null;
  budget: number | null;
  pct: number | null;
}) {
  return (
    <div className="forecast-card">
      <div className="forecast-card-month">{label}</div>
      <dl>
        <dt>Inference</dt>
        <dd>
          {inference.projected === null ? (
            <span className="muted">—</span>
          ) : (
            <>
              <strong>{money(inference.projected)}</strong>
              <Range low={inference.low} high={inference.high} />
            </>
          )}
        </dd>
        <dt>Build {buildIsActual && <span className="muted">(to date)</span>}</dt>
        <dd>{build === null ? <span className="muted">—</span> : money(build)}</dd>
        {budget !== null && (
          <>
            <dt>Total vs budget</dt>
            <dd>
              {total === null ? (
                <span className="muted">—</span>
              ) : (
                <>
                  {money(total)} <span className="muted">/ {compactMoney(budget)}</span>
                  {pct !== null && (
                    <span className={`forecast-pct ${pct > 100 ? "forecast-pct-over" : ""}`}>
                      {" "}
                      {pct.toFixed(0)}%
                    </span>
                  )}
                </>
              )}
            </dd>
          </>
        )}
      </dl>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Chart
// ---------------------------------------------------------------------------
// Drawn at the width it is shown at, so one viewBox unit is one pixel and the
// labels come out at the Overview's size on any screen. A chart this wide on a
// fixed grid would scale its text with it: oversized on a desktop, unreadable
// on a phone. The fallback is only for the first paint and for tests.
const FALLBACK_W = 640;
const VB_H = 240;
const AXIS_W = 56;
const PAD_RIGHT = 8;
const TOP = 12;
const BOTTOM = 214; // baseline; month labels sit below it
/** The Overview bar's on-screen width, so a bar here looks like a bar there. */
const MAX_BAR_W = 38;

function useWidth<T extends HTMLElement>(): [RefObject<T>, number] {
  const ref = useRef<T>(null);
  const [width, setWidth] = useState(0);
  // Measured once before the first paint, so the chart never depends on the
  // observer to appear; the observer only follows later resizes.
  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    setWidth(Math.round(el.getBoundingClientRect().width));
    if (typeof ResizeObserver === "undefined") return;
    const ro = new ResizeObserver(([entry]) => setWidth(Math.round(entry.contentRect.width)));
    ro.observe(el);
    return () => ro.disconnect();
  }, []);
  return [ref, width];
}

interface Bar {
  month: string;
  kind: "actual" | "open" | "projected";
  build: number;
  inference: number;
  /** Where the open month's inference had got to by today. */
  inferenceSoFar?: number;
  high: number | null;
  low: number | null;
  budget: number | null;
}

function bars(f: Forecast): Bar[] {
  const out: Bar[] = f.history.map((h) => ({
    month: h.month,
    kind: "actual",
    build: h.build,
    inference: h.inference,
    high: null,
    low: null,
    budget: null,
  }));
  const om = f.open_month;
  out.push({
    month: om.month,
    kind: "open",
    build: om.build.actual,
    inference: om.inference.projected ?? om.inference.actual,
    inferenceSoFar: om.inference.actual,
    high: om.inference.high === null ? null : om.inference.high + om.build.actual,
    low: om.inference.low === null ? null : om.inference.low + om.build.actual,
    budget: om.budget,
  });
  for (const m of f.months) {
    if (m.inference.projected === null) continue;
    const b = m.build.projected ?? 0;
    out.push({
      month: m.month,
      kind: "projected",
      build: b,
      inference: m.inference.projected,
      high: m.inference.high === null ? null : m.inference.high + b,
      low: m.inference.low === null ? null : m.inference.low + b,
      budget: m.budget,
    });
  }
  return out;
}

function ForecastChart({ f }: { f: Forecast }) {
  const [wrapRef, measured] = useWidth<HTMLDivElement>();
  // Which month the pointer is over; null is "not hovering".
  const [hover, setHover] = useState<number | null>(null);
  const data = bars(f);
  if (data.length === 0) return null;

  const vbW = measured || FALLBACK_W;

  const top = Math.max(
    ...data.map((d) => Math.max(d.build + d.inference, d.high ?? 0, d.budget ?? 0)),
  );
  const ceiling = niceCeil(top || 1, FINE_STEPS);
  const slot = (vbW - AXIS_W - PAD_RIGHT) / data.length;
  const barW = Math.min(slot * 0.55, MAX_BAR_W);
  const centre = (i: number) => AXIS_W + i * slot + slot / 2;
  const y = (v: number) => BOTTOM - (v / ceiling) * (BOTTOM - TOP);
  const hovered = hover === null ? null : data[hover];

  return (
    <div className="trend-line-wrap" ref={wrapRef} onMouseLeave={() => setHover(null)}>
      <svg
        className="trend-svg forecast-svg"
        viewBox={`0 0 ${vbW} ${VB_H}`}
        role="img"
        aria-label={`Monthly AI spend: ${f.history.length} months actual, then this month and the next ${f.months.length} projected.`}
      >
        {GRID_LEVELS.map((level) => (
          <g key={level}>
            <line
              className="trend-grid-line"
              x1={AXIS_W}
              y1={y(level * ceiling)}
              x2={vbW - PAD_RIGHT}
              y2={y(level * ceiling)}
            />
            <text
              className="trend-axis-label"
              x={AXIS_W - 6}
              y={y(level * ceiling) + 4}
              textAnchor="end"
            >
              {wholeMoney(level * ceiling)}
            </text>
          </g>
        ))}

        {data.map((d, i) => {
          const bx = centre(i) - barW / 2;
          const buildTop = y(d.build);
          const totalTop = y(d.build + d.inference);
          const soFarTop = d.inferenceSoFar !== undefined ? y(d.build + d.inferenceSoFar) : null;
          const dim = hover !== null && hover !== i;
          return (
            <g
              key={d.month}
              className={`trend-bar-group forecast-bar forecast-bar-${d.kind}${dim ? " dim" : ""}`}
            >
              {/* Inference on top of build: two segments, never one (invariant 2).
                  Same order, colours and corner as the Overview's spend trend. */}
              <g className="bar-grow" style={{ animationDelay: `${Math.min(i, 24) * 25}ms` }}>
                <rect
                  className="trend-bar-run"
                  x={bx}
                  y={totalTop}
                  width={barW}
                  height={Math.max(buildTop - totalTop, 0)}
                  rx={2}
                />
                <rect
                  className="trend-bar-build"
                  x={bx}
                  y={buildTop}
                  width={barW}
                  height={Math.max(BOTTOM - buildTop, 0)}
                />
                {/* The open month: solid to where spend has reached today, lighter
                  above it for the part that is still a projection. */}
                {soFarTop !== null && (
                  <rect
                    className="trend-bar-run forecast-seg-sofar"
                    x={bx}
                    y={soFarTop}
                    width={barW}
                    height={Math.max(buildTop - soFarTop, 0)}
                  />
                )}
              </g>
              {d.high !== null && d.low !== null && d.high !== d.low && (
                <g className="forecast-range-whisker">
                  <line x1={centre(i)} x2={centre(i)} y1={y(d.high)} y2={y(d.low)} />
                  <line x1={centre(i) - 4} x2={centre(i) + 4} y1={y(d.high)} y2={y(d.high)} />
                  <line x1={centre(i) - 4} x2={centre(i) + 4} y1={y(d.low)} y2={y(d.low)} />
                </g>
              )}
              {d.budget !== null && (
                <line
                  className="budget-line"
                  x1={centre(i) - slot / 2}
                  x2={centre(i) + slot / 2}
                  y1={y(d.budget)}
                  y2={y(d.budget)}
                />
              )}
              <text className="trend-axis-label" x={centre(i)} y={BOTTOM + 18} textAnchor="middle">
                {/* Ten months on a phone leave about 24px each: the initial fits, the
                    name does not, and the hover card still gives it in full. */}
                {slot < 32 ? monthLabel(d.month).slice(0, 1) : monthLabel(d.month)}
              </text>
            </g>
          );
        })}

        {/* One invisible band per month, so a short bar is as easy to hit as a
            tall one and the pointer never falls between two of them. */}
        {data.map((d, i) => (
          <rect
            key={`hit-${d.month}`}
            x={centre(i) - slot / 2}
            y={0}
            width={slot}
            height={BOTTOM}
            fill="transparent"
            onMouseEnter={() => setHover(i)}
          />
        ))}
      </svg>

      {hovered && <ForecastHover bar={hovered} pct={(centre(hover!) / vbW) * 100} />}

      <div className="trend-key forecast-key">
        <span className="trend-key-item">
          <span className="trend-key-swatch build" aria-hidden /> Build
        </span>
        <span className="trend-key-item">
          <span className="trend-key-swatch run" aria-hidden /> Inference
        </span>
        <span className="trend-key-item">
          <span className="trend-key-swatch run forecast-swatch-projected" aria-hidden /> Projected
        </span>
        {f.has_budget && (
          <span className="trend-key-item">
            <span className="forecast-swatch-budget" aria-hidden /> Budget
          </span>
        )}
        <span className="trend-key-item">
          <span className="forecast-swatch-range" aria-hidden /> 80% range
        </span>
      </div>
    </div>
  );
}

/** The Overview's hover card, with what this month's bar is made of. */
function ForecastHover({ bar, pct }: { bar: Bar; pct: number }) {
  const label =
    bar.kind === "actual"
      ? monthLabel(bar.month)
      : `${monthLabel(bar.month)} · ${bar.kind === "open" ? "this month" : "projected"}`;
  return (
    <ChartHoverCard pct={pct} title={label} total={money(bar.build + bar.inference)}>
      <ul className="trend-hover-list">
        <HoverRow
          swatch="trend-key-swatch build"
          label={bar.kind === "open" ? "Build to date" : "Build"}
          value={money(bar.build)}
        />
        {bar.inferenceSoFar !== undefined && (
          <HoverRow
            swatch="trend-key-swatch run"
            label="Inference so far"
            value={money(bar.inferenceSoFar)}
          />
        )}
        <HoverRow
          swatch={
            bar.kind === "actual"
              ? "trend-key-swatch run"
              : "trend-key-swatch run forecast-swatch-projected"
          }
          label={bar.kind === "actual" ? "Inference" : "Inference projected"}
          value={money(bar.inference)}
        />
        {bar.low !== null && bar.high !== null && bar.high !== bar.low && (
          <HoverRow
            label="Total, 80% range"
            value={`${money(bar.low)} – ${money(bar.high)}`}
            muted
          />
        )}
        {bar.budget !== null && <HoverRow label="Budget" value={money(bar.budget)} muted />}
      </ul>
    </ChartHoverCard>
  );
}

// ---------------------------------------------------------------------------
// Drivers
// ---------------------------------------------------------------------------
function DriverName({ d }: { d: ForecastDriver }) {
  if (d.feature_id === null) {
    // Unattributed is a row, never hidden — but it is not a feature to open.
    return <span title="Spend no feature has claimed yet">{d.name}</span>;
  }
  return (
    <Link to={`/features/${d.feature_id}`} className="link">
      {d.name}
    </Link>
  );
}

function Change({ d }: { d: ForecastDriver }) {
  if (d.change === null) return <span className="muted">—</span>;
  const up = d.change > 0;
  return (
    <span className={`delta ${up ? "delta-up" : "delta-down"}`}>
      {up ? "▲" : "▼"} {money(Math.abs(d.change))}
      {d.change_pct !== null && ` (${Math.abs(d.change_pct).toFixed(0)}%)`}
    </span>
  );
}

function Drivers({ drivers }: { drivers: ForecastDriver[] }) {
  if (drivers.length === 0) return null;
  return (
    <section className="detail-section">
      <div className="section-head">
        <h2>What is driving it</h2>
        <span className="section-sub muted">
          Each feature&rsquo;s projected month against last month, biggest increase first.
        </span>
      </div>
      <div className="mini-table-wrap">
        <table className="mini-table">
          <thead>
            <tr>
              <th scope="col">Feature</th>
              <th scope="col" className="num">
                Last month
              </th>
              <th scope="col" className="num">
                Projected this month
              </th>
              <th scope="col" className="num">
                Change
              </th>
              <th
                scope="col"
                className="num"
                title="Measured and modelled savings Optimize has already found"
              >
                Savings found
              </th>
            </tr>
          </thead>
          <tbody>
            {drivers.map((d) => (
              <tr key={d.feature_id ?? "unattributed"}>
                <td>
                  <DriverName d={d} />
                </td>
                <td className="num">{money(d.prior_month)}</td>
                <td className="num">
                  {d.projected === null ? <span className="muted">—</span> : money(d.projected)}
                </td>
                <td className="num">
                  <Change d={d} />
                </td>
                <td className="num">
                  {d.identified_savings ? (
                    <Link to={`/features/${d.feature_id}`} className="link">
                      {money(d.identified_savings)}/mo
                    </Link>
                  ) : (
                    <span className="muted">—</span>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}

// ---------------------------------------------------------------------------
// Page
// ---------------------------------------------------------------------------
export function ForecastPage() {
  const [f, setF] = useState<Forecast | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let live = true;
    api
      .forecast()
      .then((r) => live && setF(r))
      .catch(
        (err) =>
          live && setError(err instanceof ApiError ? err.message : "Could not load the forecast."),
      );
    return () => {
      live = false;
    };
  }, []);

  return (
    <div className="content">
      <div className="dash-head">
        <div>
          <h1>Forecast</h1>
          <p className="muted dash-sub">
            Where your AI spend is heading over the next three months, what is driving it, and how
            sure each number is.
          </p>
        </div>
      </div>

      {error && (
        <p className="error" role="alert">
          {error}
        </p>
      )}

      {f && (
        <>
          <p className="section-sub muted">
            As of{" "}
            {new Date(f.as_of + "T00:00:00").toLocaleDateString(undefined, { dateStyle: "long" })}
            {f.as_of_is_fixed && " (the demo's fixed date)"} — day {f.open_month.observed_days} of{" "}
            {f.open_month.days_in_month}.
          </p>

          <BudgetStatus f={f} />

          <div className="forecast-cards">
            <MonthCard
              label={`${monthLabel(f.open_month.month)} (this month)`}
              inference={f.open_month.inference}
              build={f.open_month.build.actual}
              buildIsActual
              total={f.open_month.total_projected}
              budget={f.open_month.budget}
              pct={f.open_month.projected_budget_pct}
            />
            {f.months.map((m) => (
              <MonthCard
                key={m.month}
                label={monthLabel(m.month)}
                inference={m.inference}
                build={m.build.projected}
                buildIsActual={false}
                total={m.total_projected}
                budget={m.budget}
                pct={m.projected_budget_pct}
              />
            ))}
          </div>

          {f.horizon.status === "insufficient" && (
            <p className="section-sub muted">
              The next three months need at least three full months of spend to draw a trend
              through, and there {f.horizon.history_months === 1 ? "is" : "are"}{" "}
              {f.horizon.history_months}. They will fill in as history builds up.
            </p>
          )}

          <section className="detail-section">
            <div className="section-head">
              <h2>Monthly spend</h2>
              <span className="section-sub muted">
                The last six months as billed, then this month and the next three as projected.
              </span>
            </div>
            <ForecastChart f={f} />
          </section>

          <Drivers drivers={f.drivers} />

          <section className="detail-section forecast-method">
            <div className="section-head">
              <h2>How this is worked out</h2>
            </div>
            <ul>
              <li>
                <strong>This month</strong> is projected from{" "}
                <MethodClauses method={f.open_month.inference.method} />
                {f.open_month.inference.low === null && f.open_month.inference.projected !== null
                  ? " There is no range: that needs daily figures to measure how much spend varies."
                  : " The range is where 8 months in 10 would land, given how much daily spend has varied."}
              </li>
              <li>
                <strong>The next three months</strong> follow a straight-line trend through up to
                six months of history
                {f.horizon.carried_open_month && ", including where this month is heading"}. The
                range widens the further out it looks. Seasonal patterns are not modelled.
              </li>
              <li>
                <strong>Build cost</strong> is billed monthly with no daily detail, so this
                month&rsquo;s figure is what has been billed so far, not a projection.
              </li>
              <li>
                Inference and build are forecast separately. They are added together only where a
                figure is compared with a budget, which covers both.
              </li>
            </ul>
          </section>
        </>
      )}
    </div>
  );
}
