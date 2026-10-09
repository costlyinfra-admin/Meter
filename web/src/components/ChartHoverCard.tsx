/**
 * The card that follows a hovered point on a chart.
 *
 * Extracted from the classification trend chart so the Overview's spend trend
 * and budget forecast get the same behaviour rather than a second version of it:
 * it appears the instant the pointer crosses a slot, slides along with a short
 * transition, and flips before it can run off the right-hand edge.
 *
 * This is deliberately not a native SVG `<title>`. Those wait about a second
 * before the browser draws them, cannot be styled, and cannot hold a breakdown.
 */
import type { CSSProperties, ReactNode } from "react";

/** One line in the card: a label, an amount, and optionally a colour swatch. */
export function HoverRow({
  label,
  value,
  swatch,
  swatchStyle,
  muted,
}: {
  label: string;
  value: string;
  /** Class for the swatch that ties the row to its mark on the chart. */
  swatch?: string;
  /** Or an inline colour, for series whose colour is chosen at runtime. */
  swatchStyle?: CSSProperties;
  muted?: boolean;
}) {
  return (
    <li>
      {(swatch || swatchStyle) && (
        <span className={`trend-legend-swatch ${swatch ?? ""}`} style={swatchStyle} aria-hidden />
      )}
      <span className={`trend-hover-name${muted ? " muted" : ""}`}>{label}</span>
      <span className="trend-hover-amt">{value}</span>
    </li>
  );
}

export function ChartHoverCard({
  pct,
  title,
  total,
  children,
}: {
  /** Where the highlighted point sits, as a percentage of the chart's width. */
  pct: number;
  title: string;
  /** The headline figure, or omitted when the card's rows are the whole story. */
  total?: string;
  children?: ReactNode;
}) {
  // Past about two-thirds across, a card that opens rightwards would hang off
  // the panel, so it opens the other way instead.
  const flip = pct > 60;
  return (
    <div
      className={`trend-hover-card${flip ? " flip" : ""}`}
      style={{ left: `${pct}%` }}
      role="status"
    >
      <div className="trend-hover-head">
        <span className="trend-hover-date">{title}</span>
        {total && <span className="trend-hover-total">{total}</span>}
      </div>
      {children}
    </div>
  );
}
