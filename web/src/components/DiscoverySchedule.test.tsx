/**
 * Automatic discovery is opt-in, and the card has to say what it costs.
 *
 * The behaviour worth pinning is the refusal to default anything on: a run
 * spends the customer's GitHub rate limit and, with BYOK, their model budget,
 * and raises proposals someone has to review.
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
  it("reports what discovery has covered, from the runs themselves", async () => {
    render(<DiscoverySchedule />);
    expect(await screen.findByText(/Discovery has covered/)).toBeInTheDocument();
    expect(screen.getByText(/across 3 runs/)).toBeInTheDocument();
  });

  it("is off until someone turns it on, and says what a run costs", async () => {
    render(<DiscoverySchedule />);
    const toggle = await screen.findByLabelText("Run discovery automatically");
    expect(toggle).not.toBeChecked();
    expect(api.setDiscoverySchedule).not.toHaveBeenCalled();

    // The cost is stated beside the switch, not buried somewhere else.
    expect(screen.getByText(/against your rate limit/)).toBeInTheDocument();
    expect(screen.getByText(/raise new feature proposals/)).toBeInTheDocument();
  });

  it("turns on, and only then offers a lookback", async () => {
    render(<DiscoverySchedule />);
    const toggle = await screen.findByLabelText("Run discovery automatically");
    expect(screen.queryByLabelText(/looks back at least/)).not.toBeInTheDocument();

    vi.mocked(api.setDiscoverySchedule).mockResolvedValue({
      ...SCHEDULE,
      enabled: true,
      next_run_at: "2026-05-22T09:00:00Z",
    });
    fireEvent.click(toggle);

    await waitFor(() => expect(api.setDiscoverySchedule).toHaveBeenCalledWith(true, undefined));
    expect(await screen.findByLabelText(/looks back at least/)).toHaveValue("14");
    // Enabling schedules the next run rather than firing one now.
    expect(screen.getByText(/Next run:/)).toBeInTheDocument();
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

    expect(await screen.findByLabelText("Run discovery automatically")).toBeDisabled();
    expect(screen.getByText(/Run discovery once first/)).toBeInTheDocument();
    expect(screen.getByText(/has not completed a run yet/)).toBeInTheDocument();
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
    expect(await screen.findByText(/which failed/)).toBeInTheDocument();
  });
});
