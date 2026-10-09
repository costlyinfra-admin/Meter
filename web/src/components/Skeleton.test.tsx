import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { Skeleton } from "./Skeleton";

describe("Skeleton", () => {
  it("still says what it is waiting for, to anyone who cannot see the shapes", () => {
    const { container } = render(<Skeleton />);
    expect(screen.getByText("Loading…")).toHaveClass("sr-only");
    expect(container.firstChild).toHaveAttribute("aria-busy", "true");
    // The shapes are decoration only.
    for (const shape of container.querySelectorAll(".skeleton-block")) {
      expect(shape.closest("[aria-hidden]")).not.toBeNull();
    }
  });

  it("is not a status, so a page's real status message is the one found", () => {
    render(<Skeleton />);
    expect(screen.queryByRole("status")).toBeNull();
  });

  it("takes the shape of what is coming", () => {
    const page = render(<Skeleton variant="page" />).container;
    expect(page.querySelectorAll(".skeleton-card")).toHaveLength(4);
    const chart = render(<Skeleton variant="chart" label="Calculating…" />).container;
    expect(chart.querySelector(".skeleton-chart")).not.toBeNull();
    expect(screen.getByText("Calculating…")).toBeInTheDocument();
  });
});
