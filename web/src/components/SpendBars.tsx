/**
 * Horizontal spend bars with optional sub-rows — the shared bar list used by the
 * Overview's "By provider" and "By developer" breakdowns. Each row shows a labelled
 * amount + share; sub-rows (models under a provider, tools under a developer) render
 * beneath their parent.
 */
import { money } from "../format";

type SubBar = { label: string; amount: number; pct: number; meta?: string };
export type Bar = {
  label: string;
  amount: number;
  pct: number;
  models?: SubBar[];
  /** Optional context shown under the label (e.g. a token count). */
  meta?: string;
};

export function SpendBars({
  rows,
  verbatim = false,
  format = money,
}: {
  rows: Bar[];
  /** Labels are identifiers the customer chose (a customer id, not a provider
   *  name), so render them exactly as sent rather than title-casing them. */
  verbatim?: boolean;
  /** How to render an amount. Defaults to money, which is what every caller but
   *  one wants; the Customer economics tab can rank by HOURS, and printing
   *  "$40" for forty hours would be a different claim entirely. */
  format?: (value: number) => string;
}) {
  return (
    <ul className="provider-bars">
      {rows.map((r) => (
        <li key={r.label} className="provider-bar-row">
          <div className="provider-bar-head">
            <span className={verbatim ? "provider-bar-name verbatim" : "provider-bar-name"}>
              {r.label}
              {r.meta && <span className="provider-bar-meta"> · {r.meta}</span>}
            </span>
            <span className="provider-bar-amt">
              {format(r.amount)} · {r.pct.toFixed(0)}%
            </span>
          </div>
          <div className="provider-bar-track">
            <div className="provider-bar-fill" style={{ width: `${Math.max(2, r.pct)}%` }} />
          </div>
          {r.models && r.models.length > 0 && (
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
      ))}
    </ul>
  );
}
