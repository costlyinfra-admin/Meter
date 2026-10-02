import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import type { OptimizationAction } from "../api";
import { CalibrationSummary, PredictionCell } from "./PredictionCheck";

function action(over: Partial<OptimizationAction> = {}): OptimizationAction {
  return {
    lever: "prompt_caching",
    applied_on: "2026-03-01",
    projected_monthly: 242.26,
    current_avoidable: null,
    realized_monthly: null,
    status: "measured",
    unit_cost_before: 0.018182,
    unit_cost_now: 0.015408,
    unit_cost_unit: "call",
    bill_agrees: true,
    verification_note: null,
    prediction: {
      savings_type: "measured",
      confidence: "med",
      validation: "untested",
      experiment_id: null,
      reduction: 0.18,
      unit_cost: 0.014909,
      predicted_at: "2026-03-12T15:00:00Z",
    },
    outcome: { reduction: 0.1526, delivered: 0.8476 },
    outcome_note: null,
    ...over,
  };
}

describe("PredictionCell", () => {
  it("puts the frozen prediction beside what the bill shows", () => {
    render(<PredictionCell action={action()} />);
    expect(screen.getByText(/−18% predicted/)).toHaveTextContent("−18% predicted · −15% billed");
    expect(screen.getByText("85% of the saving arrived")).toBeInTheDocument();
    // What the prediction rested on, on hover.
    expect(screen.getByTitle(/Predicted from a measured figure, medium confidence/)).toBeTruthy();
  });

  it("says a cost per unit that rose rose", () => {
    render(<PredictionCell action={action({ outcome: { reduction: -0.2, delivered: -1.1 } })} />);
    expect(screen.getByText(/\+20% billed/)).toBeInTheDocument();
    expect(screen.getByText("cost per unit rose")).toBeInTheDocument();
  });

  it("shows a prediction not yet checked, and why", () => {
    render(
      <PredictionCell
        action={action({
          outcome: null,
          outcome_note: "Checked against the bill from the month after it was applied.",
        })}
      />,
    );
    expect(screen.getByText("−18% predicted")).toBeInTheDocument();
    expect(screen.getByText("not checked yet")).toHaveAttribute(
      "title",
      "Checked against the bill from the month after it was applied.",
    );
  });

  it("has nothing to show for a change applied before predictions were kept", () => {
    render(
      <PredictionCell
        action={action({
          prediction: null,
          outcome: null,
          outcome_note: "Applied before Meter recorded its predictions.",
        })}
      />,
    );
    expect(screen.getByText("—")).toHaveAttribute(
      "title",
      "Applied before Meter recorded its predictions.",
    );
  });
});

describe("CalibrationSummary", () => {
  it("reports each kind of figure apart, and says when there are too few", () => {
    render(
      <CalibrationSummary
        calibration={{
          count: 2,
          median_delivered: 0.66,
          by_savings_type: [
            { savings_type: "tested", count: 1, median_delivered: 0.98 },
            { savings_type: "modeled_ceiling", count: 1, median_delivered: 0.34 },
          ],
          waiting: 1,
          unpredicted: 1,
        }}
      />,
    );
    expect(screen.getByText(/of the predicted saving/)).toHaveTextContent(
      "At the median, 66% of the predicted saving arrived, across 2 changes the bill could check.",
    );
    expect(screen.getByText(/Tested figures/)).toHaveTextContent("Tested figures: 98% (1 change)");
    expect(screen.getByText(/Ceilings/)).toHaveTextContent("34%");
    expect(screen.getByText("Too few changes to call this a pattern yet.")).toBeInTheDocument();
    expect(screen.getByText(/1 more is waiting to be checked/)).toHaveTextContent(
      "1 was applied before Meter recorded predictions",
    );
  });

  it("says plainly when nothing has been checked yet", () => {
    render(
      <CalibrationSummary
        calibration={{
          count: 0,
          median_delivered: null,
          by_savings_type: [],
          waiting: 2,
          unpredicted: 0,
        }}
      />,
    );
    expect(
      screen.getByText("No applied change has been checked against the bill yet."),
    ).toBeInTheDocument();
  });
});
