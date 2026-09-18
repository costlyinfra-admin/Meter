/**
 * Metered AI cost and human cost, month by month, stacked.
 *
 * The By Customer tab used to draw metered spend alone with `TrendChart`. That
 * component takes one series and is shared by three other screens, so widening
 * it to two would complicate all of them for the benefit of one; this is the
 * same bars, the same sizes and the same CSS, with the bar split in two.
 *
 * The two halves are never summed into a single labelled figure without saying
 * what went into it: the legend names both, and the hover gives each on its own
 * line before the total. A stack whose segments are unlabelled is exactly the
 * blended number this product exists to take apart.
 */
import { money } from "../format";

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

interface DeliveryMonth {
  period: string;
  /** Metered, customer-tagged inference. */
  ai: number;
  /** null where no effort was recorded for the month — not 0. */
  human: number | null;
}

type Series = { period: string; amount: number }[];

/** Fold the two series the API returns into one month-keyed list. */
function deliveryMonths(ai: Series, human: Series, effortPresent: boolean): DeliveryMonth[] {
  const humanBy = new Map(human.map((h) => [h.period, h.amount]));
  const periods = [...new Set([...ai.map((a) => a.period), ...humanBy.keys()])].sort();
  const aiBy = new Map(ai.map((a) => [a.period, a.amount]));
  return periods.map((period) => ({
    period,
    ai: aiBy.get(period) ?? 0,
    // A month with no rows inside a period that HAS effort data is a real zero
    // — nobody logged hours that month. With no effort data at all it is null,
    // and the chart draws AI only.
    human: effortPresent ? (humanBy.get(period) ?? 0) : null,
  }));
}

export function DeliveryCostTrend({
  ai,
  human,
  effortPresent,
}: {
  /** The metered-spend trend this chart has always drawn. */
  ai: Series;
  human: Series;
  effortPresent: boolean;
}) {
  const months = deliveryMonths(ai, human, effortPresent);
  if (months.length === 0) return <p className="muted">No data yet.</p>;
  const anyHuman = months.some((m) => m.human !== null);
  const max = Math.max(...months.map((m) => m.ai + (m.human ?? 0)), 1);

  return (
    <>
      <div className="trend-chart">
        {months.map((m) => {
          const human = m.human ?? 0;
          const total = m.ai + human;
          const height = Math.max(3, (total / max) * 100);
          return (
            <div
              className="trend-bar-wrap"
              key={m.period}
              title={
                anyHuman
                  ? `${MONTHS[Number(m.period.slice(5, 7)) - 1]} · ${money(m.ai)} AI + ` +
                    `${money(human)} human = ${money(total)}`
                  : `${MONTHS[Number(m.period.slice(5, 7)) - 1]} · ${money(m.ai)}`
              }
            >
              <span className="trend-value">{money(total)}</span>
              <div className="delivery-bar" style={{ height: `${height}%` }}>
                {/* Human on top, so the AI half keeps the baseline it had
                    before this view gained a second series. */}
                {human > 0 && (
                  <div
                    className="delivery-seg delivery-seg-human"
                    style={{ height: `${(human / total) * 100}%` }}
                  />
                )}
                <div
                  className="delivery-seg delivery-seg-ai"
                  style={{ height: `${total > 0 ? (m.ai / total) * 100 : 100}%` }}
                />
              </div>
              <span className="trend-label">{MONTHS[Number(m.period.slice(5, 7)) - 1]}</span>
            </div>
          );
        })}
      </div>
      <p className="delivery-legend">
        <span className="delivery-key">
          <span className="delivery-swatch delivery-seg-ai" aria-hidden /> Metered AI cost
        </span>
        {anyHuman && (
          <span className="delivery-key">
            <span className="delivery-swatch delivery-seg-human" aria-hidden /> Human cost
          </span>
        )}
      </p>
    </>
  );
}
