/**
 * Spend per product, month by month — a stacked bar per month, one segment per
 * product, plus the two residuals.
 *
 * WHY A TOGGLE RATHER THAN ONE STACK OF BOTH KINDS. A segment per product whose
 * height was build + inference would put a blended per-product figure on screen,
 * which is the one thing this product never does (invariant 2). So the chart
 * draws one kind of money at a time and says which: every segment, every axis
 * label and every hover figure is build cost, or inference cost, never a sum of
 * the two.
 *
 * The segments include Unassigned and Unattributed, because a chart of products
 * alone would show a rising total as features get assigned and look like growth
 * that never happened. Both are grey: neither is a product.
 */
import { useId, useState } from "react";
import { type ProductTrendMonth } from "../api";
import { money, wholeMoney } from "../format";
import { GRID_LEVELS, niceCeil } from "./chartAxis";
import { ChartHoverCard, HoverRow } from "./ChartHoverCard";

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

/** The colour ramp, cycled: a customer's product list has no fixed length. */
const RAMP = 6;
const segClass = (i: number) => `product-seg-${(i % RAMP) + 1}`;

// Same frame as the classification chart, so the two read as one family.
const VB_W = 1000;
const VB_H = 200;
const AXIS_W = 54;
const PLOT_TOP = 26;
const PLOT_BOTTOM = 158;
const PAD_RIGHT = 14;
const PLOT_W = VB_W - AXIS_W - PAD_RIGHT;

type Kind = "inference_cost" | "build_cost";

/** One drawable slice of a month's bar. */
type Segment = { key: string; label: string; cls: string; value: number };

function segments(month: ProductTrendMonth, kind: Kind): Segment[] {
  return [
    ...month.products.map((p, i) => ({
      key: p.product_id,
      label: p.name,
      cls: segClass(i),
      value: p[kind],
    })),
    {
      key: "__unassigned",
      label: "Unassigned",
      cls: "product-seg-unassigned",
      value: month.unassigned[kind],
    },
    {
      key: "__unattributed",
      label: "Unattributed",
      cls: "product-seg-unattributed",
      value: month.unattributed[kind],
    },
  ];
}

export function ProductTrend({ trend }: { trend: ProductTrendMonth[] }) {
  const [kind, setKind] = useState<Kind>("inference_cost");
  const [hover, setHover] = useState<number | null>(null);
  const clipId = useId();

  if (trend.length === 0) return <p className="muted">No data yet.</p>;

  const totals = trend.map((m) => segments(m, kind).reduce((sum, s) => sum + s.value, 0));
  const max = Math.max(...totals, 0);
  const ceil = niceCeil(max);
  const y = (v: number) => PLOT_BOTTOM - (v / ceil) * (PLOT_BOTTOM - PLOT_TOP);

  // A whole year of slots, so a three-month range does not draw three
  // billboards across the card — the same rule the classification chart uses.
  const slots = Math.max(trend.length, 12);
  const slotW = PLOT_W / slots;
  const barW = Math.min(slotW * 0.62, 40);

  const bars = trend.map((m, i) => {
    const cx = AXIS_W + i * slotW + slotW / 2;
    const total = totals[i];
    const height = total > 0 ? Math.max(2, PLOT_BOTTOM - y(total)) : 0;
    return { m, i, cx, x: cx - barW / 2, total, height, top: PLOT_BOTTOM - height };
  });

  // The legend names every product once, from the first month — the series are
  // in the same order every month, so any month would do.
  const legend = segments(trend[0], kind);

  return (
    <div>
      <div className="trend-toggle" role="group" aria-label="Which cost">
        <button
          type="button"
          className={kind === "inference_cost" ? "active" : ""}
          aria-pressed={kind === "inference_cost"}
          onClick={() => setKind("inference_cost")}
        >
          Inference
        </button>
        <button
          type="button"
          className={kind === "build_cost" ? "active" : ""}
          aria-pressed={kind === "build_cost"}
          onClick={() => setKind("build_cost")}
        >
          Build
        </button>
      </div>

      {max <= 0 ? (
        <p className="muted">
          No {kind === "build_cost" ? "build" : "inference"} cost in this period.
        </p>
      ) : (
        <div className="trend-line-wrap" onMouseLeave={() => setHover(null)}>
          <svg
            className="trend-line-svg"
            viewBox={`0 0 ${VB_W} ${VB_H}`}
            role="img"
            aria-label={`${kind === "build_cost" ? "Build" : "Inference"} cost per product, per month`}
          >
            <defs>
              {bars.map((b) => (
                <clipPath key={b.m.period} id={`${clipId}-${b.i}`}>
                  <path
                    d={`M${b.x} ${PLOT_BOTTOM} V${b.top + 4} a4 4 0 0 1 4 -4 h${barW - 8} a4 4 0 0 1 4 4 V${PLOT_BOTTOM} Z`}
                  />
                </clipPath>
              ))}
            </defs>

            {GRID_LEVELS.map((f) => (
              <g key={f}>
                <line
                  className="trend-grid-line"
                  x1={AXIS_W}
                  y1={y(f * ceil)}
                  x2={VB_W - PAD_RIGHT}
                  y2={y(f * ceil)}
                />
                <text
                  className="trend-axis-label"
                  x={AXIS_W - 8}
                  y={y(f * ceil) + 4}
                  textAnchor="end"
                >
                  {wholeMoney(f * ceil)}
                </text>
              </g>
            ))}

            {bars.map((b) => {
              let stacked = 0;
              return (
                <g
                  key={b.m.period}
                  className={
                    hover === null || hover === b.i ? "trend-bar-group" : "trend-bar-group dim"
                  }
                >
                  <g clipPath={`url(#${clipId}-${b.i})`}>
                    {segments(b.m, kind).map((s) => {
                      if (s.value <= 0 || b.total <= 0) return null;
                      const h = (s.value / b.total) * b.height;
                      const rectY = PLOT_BOTTOM - stacked - h;
                      stacked += h;
                      return (
                        <rect
                          key={s.key}
                          className={`trend-seg-fill ${s.cls}`}
                          x={b.x}
                          y={rectY}
                          width={barW}
                          height={h}
                        />
                      );
                    })}
                  </g>
                </g>
              );
            })}

            {bars.map((b) => (
              <text
                key={`tick-${b.m.period}`}
                className="trend-line-tick"
                x={b.cx}
                y={PLOT_BOTTOM + 18}
                textAnchor="middle"
              >
                {MONTHS[Number(b.m.period.slice(5, 7)) - 1]}
              </text>
            ))}

            {bars.map((b) => (
              <rect
                key={`hit-${b.m.period}`}
                x={b.cx - slotW / 2}
                y={0}
                width={slotW}
                height={PLOT_BOTTOM}
                fill="transparent"
                onMouseEnter={() => setHover(b.i)}
              />
            ))}
          </svg>

          {hover !== null && (
            <ChartHoverCard
              pct={(bars[hover].cx / VB_W) * 100}
              title={`${MONTHS[Number(trend[hover].period.slice(5, 7)) - 1]} ${trend[hover].period.slice(0, 4)}`}
              total={money(bars[hover].total)}
            >
              <ul className="trend-hover-list">
                {segments(trend[hover], kind)
                  .filter((s) => s.value > 0)
                  .map((s) => (
                    <HoverRow
                      key={s.key}
                      swatch={s.cls}
                      label={s.label}
                      value={money(s.value)}
                      muted={s.key.startsWith("__")}
                    />
                  ))}
              </ul>
            </ChartHoverCard>
          )}
        </div>
      )}

      <ul className="trend-legend" aria-label="Product legend">
        {legend.map((s) => (
          <li key={s.key} className="trend-legend-item">
            <span className={`trend-legend-swatch ${s.cls}`} aria-hidden />
            {s.label}
          </li>
        ))}
      </ul>
    </div>
  );
}
