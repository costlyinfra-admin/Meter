/**
 * A legend you can click: each entry hides or shows its series.
 *
 * Buttons, not spans, so a keyboard reaches them and a screen reader hears
 * whether each one is on (aria-pressed). A hidden entry keeps its place and
 * goes hollow and struck through, so the reader can see what is missing from
 * the chart rather than wondering where it went.
 */
import type { CSSProperties } from "react";

export interface LegendItem {
  key: string;
  label: string;
  /** Swatch colour, for a filled square. */
  color?: string;
  /** Or a class, for a swatch drawn another way (a dashed line, say). */
  swatchClass?: string;
  /** A figure beside the label, such as the series' total for the range. */
  value?: string;
}

export function ChartLegend({
  items,
  hidden,
  onToggle,
  onShowAll,
  label,
  onPreview,
}: {
  items: LegendItem[];
  hidden: ReadonlySet<string>;
  onToggle: (key: string) => void;
  onShowAll: () => void;
  /** What the legend controls, for assistive tech: "Spend trend series". */
  label: string;
  /** Pointing at an entry (or tabbing to it) previews it: the chart fades the
   *  rest, so you see what a click would isolate. Null when it leaves. */
  onPreview?: (key: string | null) => void;
}) {
  const anyHidden = items.some((item) => hidden.has(item.key));
  return (
    <div className="chart-legend" role="group" aria-label={label}>
      {items.map((item) => {
        const off = hidden.has(item.key);
        const style: CSSProperties | undefined = item.color
          ? ({ "--swatch": item.color } as CSSProperties)
          : undefined;
        return (
          <button
            key={item.key}
            type="button"
            className={`chart-legend-item${off ? " off" : ""}`}
            aria-pressed={!off}
            title={off ? `Show ${item.label}` : `Hide ${item.label}`}
            onClick={() => onToggle(item.key)}
            onMouseEnter={() => onPreview?.(off ? null : item.key)}
            onMouseLeave={() => onPreview?.(null)}
            onFocus={() => onPreview?.(off ? null : item.key)}
            onBlur={() => onPreview?.(null)}
          >
            <span
              className={`chart-legend-swatch ${item.swatchClass ?? "fill"}`}
              style={style}
              aria-hidden
            />
            <span className="chart-legend-label">{item.label}</span>
            {item.value && <span className="chart-legend-value">{item.value}</span>}
          </button>
        );
      })}
      {anyHidden && (
        <button type="button" className="link chart-legend-reset" onClick={onShowAll}>
          Show all
        </button>
      )}
    </div>
  );
}
