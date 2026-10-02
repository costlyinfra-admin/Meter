/**
 * Prediction vs outcome (EX-5): what Meter predicted when a change was applied,
 * and how much of it the bill shows arriving.
 *
 * The prediction is that a unit of the feature's work — a call, or a million
 * input tokens — gets cheaper by the saving's share of the feature's spend when
 * the change was applied. The bill's answer is how far its cost per unit fell
 * since. Both are feature-wide, so anything else that changed the feature moves
 * them too; one change proves little, and the pattern across changes is what a
 * buyer should read.
 */
import type { ActionPrediction, Calibration, OptimizationAction } from "../api";
import { shortDate } from "../format";

/** "−18%" for a fall, "+20%" for a rise. */
function change(reduction: number): string {
  const pct = Math.round(Math.abs(reduction) * 100);
  return `${reduction >= 0 ? "−" : "+"}${pct}%`;
}

function delivered(share: number): string {
  return `${Math.round(share * 100)}%`;
}

const KIND_WORDS: Record<string, string> = {
  tested: "a tested figure",
  measured: "a measured figure",
  modeled_ceiling: "a ceiling (“up to”)",
  directional: "a rule-of-thumb estimate",
};

const CONFIDENCE_WORDS: Record<string, string> = { high: "high", med: "medium", low: "low" };

function basis(p: ActionPrediction): string {
  const kind = (p.savings_type && KIND_WORDS[p.savings_type]) ?? "Meter's figure";
  const conf = p.confidence ? `, ${CONFIDENCE_WORDS[p.confidence]} confidence` : "";
  const when = p.predicted_at ? `, frozen ${shortDate(p.predicted_at)}` : "";
  return `Predicted from ${kind}${conf}${when}.`;
}

/** One applied change: predicted fall in cost per unit, and the bill's. */
export function PredictionCell({ action }: { action: OptimizationAction }) {
  const p = action.prediction;
  const o = action.outcome;
  if (!p) {
    return (
      <span className="muted" title={action.outcome_note ?? undefined}>
        —
      </span>
    );
  }
  if (!o) {
    return (
      <span className="prediction-cell">
        <span title={basis(p)}>{change(p.reduction)} predicted</span>
        <span className="muted prediction-note" title={action.outcome_note ?? undefined}>
          not checked yet
        </span>
      </span>
    );
  }
  return (
    <span className="prediction-cell" title={basis(p)}>
      <span>
        {change(p.reduction)} predicted · <strong>{change(o.reduction)} billed</strong>
      </span>
      {o.delivered !== null && (
        <span className={o.delivered < 0 ? "prediction-note prediction-rose" : "prediction-note"}>
          {o.delivered < 0
            ? "cost per unit rose"
            : `${delivered(o.delivered)} of the saving arrived`}
        </span>
      )}
    </span>
  );
}

const KIND_TITLES: Record<string, string> = {
  tested: "Tested figures",
  measured: "Measured figures",
  modeled_ceiling: "Ceilings (“up to”)",
};

/** Across changes: how far Meter's own predictions can be trusted. */
export function CalibrationSummary({ calibration }: { calibration: Calibration }) {
  const c = calibration;
  const others = [
    c.waiting > 0 && `${c.waiting} more ${c.waiting === 1 ? "is" : "are"} waiting to be checked`,
    c.unpredicted > 0 &&
      `${c.unpredicted} ${c.unpredicted === 1 ? "was" : "were"} applied before Meter recorded predictions`,
  ].filter(Boolean);
  return (
    <div className="calibration">
      <h3>Meter&rsquo;s predictions against your bill</h3>
      {c.count === 0 || c.median_delivered === null ? (
        <p className="muted">No applied change has been checked against the bill yet.</p>
      ) : (
        <>
          <p>
            At the median, <strong>{delivered(c.median_delivered)}</strong> of the predicted saving
            arrived, across {c.count} {c.count === 1 ? "change" : "changes"} the bill could check.
          </p>
          <ul className="calibration-kinds">
            {c.by_savings_type.map((k) => (
              <li key={k.savings_type}>
                {KIND_TITLES[k.savings_type] ?? k.savings_type}:{" "}
                <strong>{k.median_delivered === null ? "—" : delivered(k.median_delivered)}</strong>{" "}
                <span className="muted">
                  ({k.count} {k.count === 1 ? "change" : "changes"})
                </span>
              </li>
            ))}
          </ul>
          {c.count < 3 && <p className="muted">Too few changes to call this a pattern yet.</p>}
        </>
      )}
      <p className="muted settings-hint">
        {others.length > 0 && `${others.join("; ")}. `}
        Checked on cost per unit of work — per call, or per million input tokens — from your
        provider&rsquo;s billing, before and after each change. Anything else that changed a feature
        moves it too. Removing repeated calls is not scored this way: it makes fewer calls, not
        cheaper ones.
      </p>
    </div>
  );
}
