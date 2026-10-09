/**
 * Horizontal spend bars with optional sub-rows — the shared bar list used by the
 * Overview's "By provider" and "By developer" breakdowns. Each row shows a labelled
 * amount + share; sub-rows (models under a provider, tools under a developer) render
 * beneath their parent.
 *
 * Click a row to leave it out. It stays in its place, faded, so the reader can
 * see what is missing; the shares of the rest are recomputed over what is still
 * shown, and a quiet "Show all" puts it back. No toolbar: the rows are the
 * control. A chart beside the list can share what is hidden (`hidden` +
 * `onHiddenChange`), so hiding a tool here hides it there too.
 */
import { useState } from "react";
import { money } from "../format";
import { toggled } from "../spendTrend";

type SubBar = { label: string; amount: number; pct: number; meta?: string };
export type Bar = {
  label: string;
  amount: number;
  pct: number;
  models?: SubBar[];
  /** Optional context shown under the label (e.g. a token count). */
  meta?: string;
  /** What identifies the row when it is hidden; defaults to the label. Set it
   *  when a chart beside the list keys the same thing differently. */
  key?: string;
};

const NONE: ReadonlySet<string> = new Set();

export function SpendBars({
  rows,
  verbatim = false,
  format = money,
  hidden: controlledHidden,
  onHiddenChange,
  showShare = true,
  dense = false,
  preview: controlledPreview,
  onPreviewChange,
}: {
  rows: Bar[];
  /** Labels are identifiers the customer chose (a customer id, not a provider
   *  name), so render them exactly as sent rather than title-casing them. */
  verbatim?: boolean;
  /** How to render an amount. Defaults to money, which is what every caller but
   *  one wants; the Customer economics tab can rank by HOURS, and printing
   *  "$40" for forty hours would be a different claim entirely. */
  format?: (value: number) => string;
  hidden?: ReadonlySet<string>;
  onHiddenChange?: (hidden: Set<string>) => void;
  /** Print each row's share beside its amount. Off for measures where a
   *  share means nothing (a cost per user); the bars still scale. */
  showShare?: boolean;
  /** Tighter rows, for a strip that sits above a table rather than a panel. */
  dense?: boolean;
  /** The row being pointed at, shared with a chart beside the list (pass both
   *  props) so pointing at a tool lights up its part of the trend. */
  preview?: string | null;
  onPreviewChange?: (key: string | null) => void;
}) {
  const [ownHidden, setOwnHidden] = useState<ReadonlySet<string>>(NONE);
  const hidden = controlledHidden ?? ownHidden;
  const [ownPreview, setOwnPreview] = useState<string | null>(null);
  const preview = controlledPreview !== undefined ? controlledPreview : ownPreview;
  const setPreview = (key: string | null) =>
    onPreviewChange ? onPreviewChange(key) : setOwnPreview(key);
  const setHidden = (next: Set<string>) =>
    onHiddenChange ? onHiddenChange(next) : setOwnHidden(next);

  const keyOf = (r: Bar) => r.key ?? r.label;
  const off = rows.filter((r) => hidden.has(keyOf(r)));
  // With nothing hidden, the server's share stands: it may be of a total wider
  // than these rows. Once something is left out, shares are of what is shown.
  const shownTotal = rows.reduce((sum, r) => (hidden.has(keyOf(r)) ? sum : sum + r.amount), 0);
  const share = (r: Bar) =>
    off.length === 0 ? r.pct : shownTotal > 0 ? (r.amount / shownTotal) * 100 : 0;

  return (
    <div className="spend-bars">
      <ul className={dense ? "provider-bars dense" : "provider-bars"}>
        {rows.map((r) => {
          const isOff = hidden.has(keyOf(r));
          const pct = share(r);
          return (
            <li
              key={keyOf(r)}
              className={
                isOff
                  ? "provider-bar-row off"
                  : preview !== null && preview !== keyOf(r)
                    ? "provider-bar-row previewed-out"
                    : "provider-bar-row"
              }
            >
              <button
                type="button"
                className="provider-bar-toggle"
                aria-pressed={!isOff}
                title={isOff ? `Show ${r.label}` : `Leave ${r.label} out`}
                onClick={() => setHidden(toggled(hidden, keyOf(r)))}
                onMouseEnter={() => setPreview(isOff ? null : keyOf(r))}
                onMouseLeave={() => setPreview(null)}
                onFocus={() => setPreview(isOff ? null : keyOf(r))}
                onBlur={() => setPreview(null)}
              >
                <span className="provider-bar-head">
                  <span className={verbatim ? "provider-bar-name verbatim" : "provider-bar-name"}>
                    {r.label}
                    {r.meta && <span className="provider-bar-meta"> · {r.meta}</span>}
                  </span>
                  <span className="provider-bar-amt">
                    {format(r.amount)}
                    {!isOff && showShare && ` · ${pct.toFixed(0)}%`}
                  </span>
                </span>
                <span className="provider-bar-track">
                  <span
                    className="provider-bar-fill"
                    style={{ width: isOff ? 0 : `${Math.max(2, pct)}%` }}
                  />
                </span>
              </button>
              {!isOff && r.models && r.models.length > 0 && (
                <ul className="model-subrows">
                  {r.models.map((m) => (
                    <li key={m.label} className="model-subrow">
                      <span className="model-subrow-name">
                        {m.label}
                        {m.meta && <span className="provider-bar-meta"> · {m.meta}</span>}
                      </span>
                      <span className="model-subrow-amt">
                        {money(m.amount)} · {m.pct.toFixed(0)}%
                      </span>
                    </li>
                  ))}
                </ul>
              )}
            </li>
          );
        })}
      </ul>
      {off.length > 0 && (
        <p className="spend-bars-note muted">
          {off.length} left out{showShare ? " · shares are of the rest" : ""} ·{" "}
          <button type="button" className="link" onClick={() => setHidden(new Set())}>
            Show all
          </button>
        </p>
      )}
    </div>
  );
}
