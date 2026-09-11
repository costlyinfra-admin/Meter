/**
 * Automatic discovery is opt-in, and condensing it to one line must not lose
 * that. The behaviour worth pinning is the refusal to default anything on — a
 * run spends the customer's GitHub rate limit and, with BYOK, their model
 * budget — and that the lookback setting survived the condensing.
 */
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { api, type DiscoverySchedule as Schedule } from "../api";
import { DiscoverySchedule } from "./DiscoverySchedule";

vi.mock("../api", async (importActual) => {
  const actual = await importActual<typeof import("../api")>();
  return {
    ...actual,
    api: {
      discoverySchedule: vi.fn(),
      setDiscoverySchedule: vi.fn(),
      discoveryRuns: vi.fn(),
    },
  };
});

const SCHEDULE: Schedule = {
  configurable: true,
  enabled: false,
  lookback_days: 14,
  next_run_at: null,
  owner: "acme",
  repos: ["acme/core"],
};

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(api.discoverySchedule).mockResolvedValue({ ...SCHEDULE });
  vi.mocked(api.discoveryRuns).mockResolvedValue({
    runs: [],
    coverage: {
      runs: 3,
      covered_from: "2026-01-01",
      covered_to: "2026-05-21",
      last_run_at: "2026-05-21T09:00:00Z",
      last_run_status: "success",
      last_run_trigger: "manual",
    },
  });
});

describe("DiscoverySchedule", () => {
  const toggle = () => screen.findByLabelText(/Run discovery automatically/);

  it("says when discovery last ran, in one line", async () => {
    // The question someone has while looking at the feature list is whether it
    // is current. A paragraph about coverage windows answered a question
    // nobody was asking at that moment.
    render(<DiscoverySchedule />);
    expect(await screen.findByText(/Last updated on May 21, 2026/)).toBeInTheDocument();
  });

  it("is off until someone turns it on, and says what a run costs", async () => {
    render(<DiscoverySchedule />);
    const box = await toggle();
    expect(box).not.toBeChecked();
    expect(api.setDiscoverySchedule).not.toHaveBeenCalled();

    // The cost still has to be stated — a run spends the customer's GitHub rate
    // limit and, with BYOK, their model budget. On the control itself now,
    // rather than in a paragraph the one line replaced.
    expect(box).toHaveAttribute("title", expect.stringContaining("rate limit"));
    expect(box).toHaveAttribute("title", expect.stringContaining("your own model"));
  });

  it("turns on, and only then offers a lookback", async () => {
    render(<DiscoverySchedule />);
    const box = await toggle();
    expect(screen.queryByLabelText(/Looks back at least/)).not.toBeInTheDocument();

    vi.mocked(api.setDiscoverySchedule).mockResolvedValue({
      ...SCHEDULE,
      enabled: true,
      next_run_at: "2026-05-22T09:00:00Z",
    });
    fireEvent.click(box);

    await waitFor(() => expect(api.setDiscoverySchedule).toHaveBeenCalledWith(true, undefined));
    // Condensing the display must not remove the setting.
    expect(await screen.findByLabelText(/Looks back at least/)).toHaveValue("14");
    expect(screen.getByText(/Next run/)).toBeInTheDocument();
    // And the cost is spelled out once it is actually going to be spent.
    expect(screen.getByText(/against your GitHub rate limit/)).toBeInTheDocument();
  });

  it("cannot be scheduled before discovery has a scope to run against", async () => {
    vi.mocked(api.discoverySchedule).mockResolvedValue({
      ...SCHEDULE,
      configurable: false,
      owner: null,
      repos: [],
    });
    vi.mocked(api.discoveryRuns).mockResolvedValue({
      runs: [],
      coverage: {
        runs: 0,
        covered_from: null,
        covered_to: null,
        last_run_at: null,
        last_run_status: null,
        last_run_trigger: null,
      },
    });
    render(<DiscoverySchedule />);

    const box = await toggle();
    expect(box).toBeDisabled();
    expect(box).toHaveAttribute("title", expect.stringContaining("Run discovery once first"));
    expect(screen.getByText(/has not run yet/)).toBeInTheDocument();
  });

  it("says so when the last run failed", async () => {
    vi.mocked(api.discoveryRuns).mockResolvedValue({
      runs: [],
      coverage: {
        runs: 1,
        covered_from: "2026-01-01",
        covered_to: "2026-05-21",
        last_run_at: "2026-05-21T09:00:00Z",
        last_run_status: "error",
        last_run_trigger: "scheduled",
      },
    });
    render(<DiscoverySchedule />);
    // A stale list because the last run failed is exactly what this line is for.
    expect(await screen.findByText(/the last run failed/)).toBeInTheDocument();
  });
});
