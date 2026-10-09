import { act, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { toast } from "../toast";
import { TOAST_MS, Toaster } from "./Toaster";

describe("Toaster", () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  it("shows a confirmation politely, then lets it go", () => {
    render(<Toaster />);
    act(() => toast("Alert saved"));
    const region = screen.getByRole("status");
    expect(region).toHaveAttribute("aria-live", "polite");
    expect(region).toHaveTextContent("Alert saved");

    // On its way out first (the stylesheet fades it), then gone.
    act(() => vi.advanceTimersByTime(TOAST_MS));
    expect(screen.getByText("Alert saved")).toHaveClass("leaving");
    act(() => vi.advanceTimersByTime(300));
    expect(screen.queryByText("Alert saved")).toBeNull();
  });

  it("shows one of each, however often the same thing is confirmed", () => {
    render(<Toaster />);
    act(() => {
      toast("Anthropic connected");
      toast("Anthropic connected");
      toast("Alert created");
    });
    expect(screen.getAllByText("Anthropic connected")).toHaveLength(1);
    expect(screen.getByText("Alert created")).toBeInTheDocument();
  });
});
