import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { api, type AlertRule, type AlertSummary } from "../api";
import { AlertsPage } from "./AlertsPage";

vi.mock("../api", async (importActual) => {
  const actual = await importActual<typeof import("../api")>();
  return {
    ...actual,
    api: {
      listAlerts: vi.fn(),
      alertsActivity: vi.fn(),
      markAllAlertsRead: vi.fn(),
      enableAlert: vi.fn(),
    },
  };
});

const SUMMARY: AlertSummary = {
  triggered: 1,
  healthy: 2,
  delivery_errors: 1,
  disabled: 1,
  unread: 3,
};

const RULE: AlertRule = {
  id: "a1",
  name: "Monthly AI budget",
  description: "",
  metric: "combined_cost",
  metric_label: "Combined AI cost",
  scope_type: "organization",
  scope_ref: null,
  scope_label: null,
  condition_type: "budget_pct",
  threshold: 90,
  budget_amount: 25000,
  window: "monthly",
  cooldown: "day",
  recovery_notify: true,
  enabled: true,
  status: "healthy",
  last_observed: 18240,
  last_evaluated_at: "2026-08-17T10:00:00Z",
  last_triggered_at: null,
  next_eval_at: "2026-09-01T00:00:00Z",
  created_by: "cto@acme.com",
  created_at: "2026-08-01T00:00:00Z",
  updated_at: "2026-08-01T00:00:00Z",
  channels: [{ channel: "in_app", label: "In-app" }],
};

const renderPage = () =>
  render(
    <MemoryRouter>
      <AlertsPage />
    </MemoryRouter>,
  );

describe("AlertsPage", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.listAlerts).mockResolvedValue({ rules: [RULE], summary: SUMMARY });
    vi.mocked(api.alertsActivity).mockResolvedValue({ events: [] });
  });

  it("renders the header, summary cards, and the rules table", async () => {
    renderPage();
    expect(screen.getByRole("heading", { name: "Alerts" })).toBeInTheDocument();
    // The rule surfaces once loaded, with its status.
    expect(await screen.findByRole("link", { name: "Monthly AI budget" })).toBeInTheDocument();
    // Summary cards render (the "Delivery errors" label is unique to a card).
    expect(screen.getByText("Delivery errors")).toBeInTheDocument();
  });

  it("filters the table by search text", async () => {
    renderPage();
    await screen.findByRole("link", { name: "Monthly AI budget" });
    fireEvent.change(screen.getByLabelText("Search alerts"), {
      target: { value: "nothing-matches" },
    });
    expect(screen.queryByRole("link", { name: "Monthly AI budget" })).not.toBeInTheDocument();
    expect(screen.getByText("No alerts match your filters.")).toBeInTheDocument();
  });

  it("marks all activity as read", async () => {
    vi.mocked(api.alertsActivity).mockResolvedValue({
      events: [
        {
          id: "e1",
          alert_id: "a1",
          alert_name: "Monthly AI budget",
          event_type: "triggered",
          metric: "combined_cost",
          metric_label: "Combined AI cost",
          condition_type: "exceeds",
          scope_type: "organization",
          scope_ref: null,
          scope_label: null,
          window: "monthly",
          observed_value: 24100,
          threshold: 22500,
          message: null,
          deliveries: [{ channel: "in_app", status: "sent" }],
          occurred_at: "2026-08-16T10:00:00Z",
          read: false,
        },
      ],
    });
    vi.mocked(api.markAllAlertsRead).mockResolvedValue({ marked: 1 });
    renderPage();
    fireEvent.click(await screen.findByRole("tab", { name: "Activity" }));
    fireEvent.click(await screen.findByRole("button", { name: "Mark all as read" }));
    await waitFor(() => expect(api.markAllAlertsRead).toHaveBeenCalled());
  });
});
