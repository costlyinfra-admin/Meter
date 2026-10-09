/**
 * Stacked bars, one per month, with a legend you can click.
 *
 * The one stacked trend the Overview's tabs share: products, delivery cost,
 * build cost by tool and by developer. Each series can be switched off from the
 * legend; the axis then rescales to what is left, the hover card lists only
 * what is shown, and "Show all" puts everything back.
 *
 * It draws whatever series it is given and adds nothing up beyond them. Which
 * series may share a stack is the caller's decision, and every caller here
 * keeps build and inference out of the same one (invariant 2).
 */
import { useId, useState } from "react";
import { type StackSeries, visibleSum } from "../chartSeries";
import { money, wholeMoney } from "../format";
import { toggled } from "../spendTrend";
import { GRID_LEVELS, niceCeil } from "./chartAxis";
import { ChartHoverCard, HoverRow } from "./ChartHoverCard";
import { ChartLegend } from "./ChartLegend";

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

// The classification chart's frame, so the tabs' charts read as one family.
const VB_W = 1000;
const VB_H = 200;
const AXIS_W = 54;
const PLOT_TOP = 26;
const PLOT_BOTTOM = 158;
const PAD_RIGHT = 14;
const PLOT_W = VB_W - AXIS_W - PAD_RIGHT;

const NONE: ReadonlySet<string> = new Set();

export function StackedTrend({
  periods,
  series,
  ariaLabel,
  legendLabel,
  format = money,
  axisFormat = wholeMoney,
  emptyText = "No data yet.",
  hidden: controlledHidden,
  onHiddenChange,
  preview: controlledPreview,
  onPreviewChange,
}: {
  /** "YYYY-MM-01", one per bar. */
  periods: string[];
  /** Bottom of the stack first. */
  series: StackSeries[];
  ariaLabel: string;
  /** What the legend controls, for assistive tech. */
  legendLabel: string;
  format?: (value: number) => string;
  axisFormat?: (value: number) => string;
  emptyText?: string;
  /** Pass both to share what is hidden with something else on the page (the
   *  list beside the chart); omit both and the chart keeps its own. */
  hidden?: ReadonlySet<string>;
  onHiddenChange?: (hidden: Set<string>) => void;
  /** The series being pointed at, here or in the list beside the chart; the
   *  rest fade so it stands out. Pass both to share it. */
  preview?: string | null;
  onPreviewChange?: (key: string | null) => void;
}) {
  const [ownHidden, setOwnHidden] = useState<ReadonlySet<string>>(NONE);
  const [hover, setHover] = useState<number | null>(null);
  const clipId = useId();
  const hidden = controlledHidden ?? ownHidden;
  const [ownPreview, setOwnPreview] = useState<string | null>(null);
  const preview = controlledPreview !== undefined ? controlledPreview : ownPreview;
  const setPreview = (key: string | null) =>
    onPreviewChange ? onPreviewChange(key) : setOwnPreview(key);
  const setHidden = (next: Set<string>) =>
    onHiddenChange ? onHiddenChange(next) : setOwnHidden(next);

  const anything = series.some((s) => s.values.some((v) => v > 0));
  if (periods.length === 0 || !anything) return <p className="muted">{emptyText}</p>;

  const totals = periods.map((_, i) => visibleSum(series, i, hidden));
  const max = Math.max(...totals, 0);
  const ceil = niceCeil(max);
  const y = (v: number) => PLOT_BOTTOM - (v / ceil) * (PLOT_BOTTOM - PLOT_TOP);

  // A whole year of slots, so a three-month range does not draw three
  // billboards across the card.
  const slots = Math.max(periods.length, 12);
  const slotW = PLOT_W / slots;
  const barW = Math.min(slotW * 0.62, 40);
  const bars = periods.map((period, i) => {
    const cx = AXIS_W + i * slotW + slotW / 2;
    const height = totals[i] > 0 ? Math.max(2, PLOT_BOTTOM - y(totals[i])) : 0;
    return { period, i, cx, x: cx - barW / 2, height, top: PLOT_BOTTOM - height };
  });
  const monthOf = (period: string) => MONTHS[Number(period.slice(5, 7)) - 1];

  return (
    <div className="stacked-trend">
      {max <= 0 ? (
        // Something to draw, but every series is switched off. Saying so beats
        // an empty frame that reads as "nothing was spent".
        <p className="muted trend-empty">
          Every series is hidden. Click one in the legend below to bring it back.
        </p>
      ) : (
        <div className="trend-line-wrap" onMouseLeave={() => setHover(null)}>
          <svg
            className="trend-line-svg"
            viewBox={`0 0 ${VB_W} ${VB_H}`}
            role="img"
            aria-label={ariaLabel}
          >
            <defs>
              {bars.map((b) => (
                <clipPath key={b.period} id={`${clipId}-${b.i}`} className="bar-clip">
                  {/* Rounded top, square feet — the segments are clipped to it. */}
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
                  {axisFormat(f * ceil)}
                </text>
              </g>
            ))}

            {bars.map((b) => {
              let stacked = 0;
              return (
                <g
                  key={b.period}
                  className={
                    hover === null || hover === b.i ? "trend-bar-group" : "trend-bar-group dim"
                  }
                >
                  {/* Rises from the baseline on first draw, one bar after
                      another (see .bar-grow). */}
                  <g
                    clipPath={`url(#${clipId}-${b.i})`}
                    className="bar-grow"
                    style={{ animationDelay: `${Math.min(b.i, 24) * 25}ms` }}
                  >
                    {series.map((s) => {
                      const value = s.values[b.i];
                      if (hidden.has(s.key) || value <= 0 || totals[b.i] <= 0) return null;
                      const h = (value / totals[b.i]) * b.height;
                      const rectY = PLOT_BOTTOM - stacked - h;
                      stacked += h;
                      return (
                        <rect
                          key={s.key}
                          className={
                            preview !== null && preview !== s.key
                              ? "stacked-seg seg-dim"
                              : "stacked-seg"
                          }
                          data-series={s.key}
                          style={{ fill: s.color }}
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
                key={`tick-${b.period}`}
                className="trend-line-tick"
                x={b.cx}
                y={PLOT_BOTTOM + 18}
                textAnchor="middle"
              >
                {monthOf(b.period)}
              </text>
            ))}

            {/* One invisible band per month, so a short bar is as easy to hit
                as a tall one. */}
            {bars.map((b) => (
              <rect
                key={`hit-${b.period}`}
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
              title={`${monthOf(periods[hover])} ${periods[hover].slice(0, 4)}`}
              total={format(totals[hover])}
            >
              <ul className="trend-hover-list">
                {series
                  .filter((s) => !hidden.has(s.key) && s.values[hover] > 0)
                  .map((s) => (
                    <HoverRow
                      key={s.key}
                      swatchStyle={{ background: s.color }}
                      label={s.label}
                      value={format(s.values[hover])}
                      muted={s.muted}
                    />
                  ))}
              </ul>
            </ChartHoverCard>
          )}
        </div>
      )}

      <ChartLegend
        label={legendLabel}
        items={series.map((s) => ({
          key: s.key,
          label: s.label,
          color: s.color,
          value: format(s.values.reduce((sum, v) => sum + v, 0)),
        }))}
        hidden={hidden}
        onToggle={(key) => setHidden(toggled(hidden, key))}
        onShowAll={() => setHidden(new Set())}
        onPreview={setPreview}
      />
    </div>
  );
}
