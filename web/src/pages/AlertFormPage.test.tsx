/**
 * The alert form's dependency on a real budget, and its request-level metrics.
 *
 * A budget-percentage rule has no denominator without one, so the form must
 * refuse it and point at the place to fix it — never quietly measure against a
 * number the customer did not set.
 *
 * The run-level metrics are not counted in dollars, so the form must not label
 * their thresholds with a "$", and an application-scoped rule must offer the
 * organization's real applications rather than a free-text box.
 */
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { api, type AlertMeta } from "../api";
import { AlertFormPage } from "./AlertFormPage";
import { AuthProvider } from "../auth/AuthContext";

vi.mock("../api", async (importActual) => {
  const actual = await importActual<typeof import("../api")>();
  return {
    ...actual,
    api: {
      me: vi.fn(),
      alertsMeta: vi.fn(),
      getSettings: vi.fn(),
      listFeatures: vi.fn(),
      aiApplications: vi.fn(),
      createAlert: vi.fn(),
      updateAlert: vi.fn(),
      getAlert: vi.fn(),
    },
  };
});

const META: AlertMeta = {
  metrics: [
    { value: "inference_cost", label: "Inference cost" },
    { value: "combined_cost", label: "Combined AI cost" },
  ],
  scopes: ["organization", "feature"],
  conditions: ["exceeds", "increase_pct", "budget_pct"],
  windows: ["daily", "monthly"],
  cooldowns: ["none", "day"],
  channels: ["in_app", "email"],
  valid_conditions: {
    inference_cost: ["exceeds", "increase_pct", "budget_pct"],
    combined_cost: ["exceeds", "increase_pct", "budget_pct"],
  },
  valid_scopes: { inference_cost: ["organization"], combined_cost: ["organization"] },
  metric_units: { inference_cost: "money", combined_cost: "money" },
  templates: [
    {
      id: "monthly_budget",
      label: "Monthly AI spend exceeds budget",
      requires_budget: true,
      rule: { name: "Over budget", condition_type: "budget_pct", threshold: 100 },
    },
    {
      id: "daily_spike",
      label: "Daily inference cost spikes by 30%",
      rule: { name: "Spike", condition_type: "increase_pct", threshold: 30 },
    },
  ],
  has_budget: false,
  budget_required_message:
    "This alert measures spend against your organization's AI budget, and no budget is " +
    "configured. Set one in Settings -> Budgets, then save this alert.",
  budget_conditions: ["budget_pct"],
};

function renderForm() {
  return render(
    <MemoryRouter>
      <AuthProvider>
        <AlertFormPage />
      </AuthProvider>
    </MemoryRouter>,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(api.me).mockResolvedValue({ id: "u1", tenant_id: "t1", email: "cto@acme.com" });
  vi.mocked(api.getSettings).mockResolvedValue({
    org_name: "Acme",
    timezone: "UTC",
    currency: "USD",
    customer_id_storage: "hashed",
    store_prompts: false,
    data_retention: "indefinite",
    trace_retention_days: 30,
    agent_stale_after_minutes: 10,
    content_capture: "disabled",
  });
  vi.mocked(api.listFeatures).mockResolvedValue([]);
  vi.mocked(api.aiApplications).mockResolvedValue({
    applications: [],
    from: "2026-08-10",
    to: "2026-09-09",
  });
  vi.mocked(api.alertsMeta).mockResolvedValue({ ...META });
});

async function chooseBudgetCondition() {
  await screen.findByLabelText("Condition");
  fireEvent.change(screen.getByLabelText("Condition"), { target: { value: "budget_pct" } });
}

describe("AlertFormPage — budget-backed conditions", () => {
  it("blocks a budget condition when the organization has no budget", async () => {
    renderForm();
    await chooseBudgetCondition();

    expect(await screen.findByText(/no budget is configured/i)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /Set a budget/ })).toHaveAttribute(
      "href",
      "/settings#budgets",
    );
  });

  it("will not save that rule, and does not call the API", async () => {
    renderForm();
    await chooseBudgetCondition();
    fireEvent.change(screen.getByLabelText("Alert name"), { target: { value: "Over budget" } });

    const save = screen.getByRole("button", { name: /Create alert|Save/ });
    expect(save).toBeDisabled();
    fireEvent.click(save);
    await waitFor(() => expect(api.createAlert).not.toHaveBeenCalled());
  });

  it("offers no budget field: the denominator is the stored budget, not a typed one", async () => {
    renderForm();
    await chooseBudgetCondition();
    expect(screen.queryByLabelText(/Monthly budget/)).not.toBeInTheDocument();
  });

  it("disables the templates that need a budget, and says why", async () => {
    renderForm();
    const template = await screen.findByRole("button", { name: /Monthly AI spend exceeds budget/ });
    expect(template).toBeDisabled();
    expect(template).toHaveAttribute("title", expect.stringContaining("Settings"));
    // The templates that do not need one stay available.
    expect(screen.getByRole("button", { name: /Daily inference cost spikes/ })).not.toBeDisabled();
  });

  it("allows the same rule once a budget exists", async () => {
    vi.mocked(api.alertsMeta).mockResolvedValue({ ...META, has_budget: true });
    renderForm();
    await chooseBudgetCondition();
    fireEvent.change(screen.getByLabelText("Alert name"), { target: { value: "Over budget" } });

    expect(screen.queryByText(/no budget is configured/i)).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Create alert|Save/ })).not.toBeDisabled();
    expect(
      screen.getByRole("button", { name: /Monthly AI spend exceeds budget/ }),
    ).not.toBeDisabled();
  });

  it("leaves non-budget conditions alone when there is no budget", async () => {
    renderForm();
    await screen.findByLabelText("Condition");
    fireEvent.change(screen.getByLabelText("Condition"), { target: { value: "increase_pct" } });
    fireEvent.change(screen.getByLabelText("Alert name"), { target: { value: "Spike" } });

    expect(screen.queryByText(/no budget is configured/i)).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Create alert|Save/ })).not.toBeDisabled();
  });
});

const RUN_META: AlertMeta = {
  ...META,
  metrics: [
    ...META.metrics,
    { value: "agent_runtime", label: "Longest agent runtime (minutes)" },
    { value: "cache_hit_rate", label: "Prompt cache hit rate (%)" },
    { value: "agent_steps", label: "Steps in a single run" },
  ],
  scopes: ["organization", "feature", "application"],
  conditions: ["exceeds", "increase_pct", "budget_pct", "falls_below"],
  valid_conditions: {
    ...META.valid_conditions,
    agent_runtime: ["exceeds"],
    cache_hit_rate: ["falls_below"],
    agent_steps: ["exceeds"],
  },
  valid_scopes: {
    ...META.valid_scopes,
    agent_runtime: ["organization", "application", "feature"],
    cache_hit_rate: ["organization", "application", "feature"],
    agent_steps: ["organization", "application", "feature"],
  },
  metric_units: {
    ...META.metric_units,
    agent_runtime: "minutes",
    cache_hit_rate: "percent",
    agent_steps: "steps",
  },
};

describe("AlertFormPage — request-level metrics", () => {
  beforeEach(() => {
    vi.mocked(api.alertsMeta).mockResolvedValue({ ...RUN_META });
    vi.mocked(api.aiApplications).mockResolvedValue({
      applications: [
        { id: "app-1", name: "Support agent", slug: "support-agent" },
        { id: "app-2", name: "Billing agent", slug: "billing-agent" },
      ] as never,
      from: "2026-08-10",
      to: "2026-09-09",
    });
  });

  it("labels the threshold in the metric's own units, not dollars", async () => {
    renderForm();
    await screen.findByLabelText("Metric");
    expect(screen.getByLabelText(/Threshold/)).toBeInTheDocument();
    expect(screen.getByText("Threshold ($)")).toBeInTheDocument();

    fireEvent.change(screen.getByLabelText("Metric"), { target: { value: "agent_runtime" } });
    expect(await screen.findByText("Threshold (minutes)")).toBeInTheDocument();
    expect(screen.queryByText("Threshold ($)")).not.toBeInTheDocument();

    fireEvent.change(screen.getByLabelText("Metric"), { target: { value: "cache_hit_rate" } });
    expect(await screen.findByText("Threshold (%)")).toBeInTheDocument();
  });

  it("switches to the only condition a metric supports", async () => {
    renderForm();
    await screen.findByLabelText("Metric");
    fireEvent.change(screen.getByLabelText("Metric"), { target: { value: "cache_hit_rate" } });

    const condition = screen.getByLabelText("Condition") as HTMLSelectElement;
    // Cache efficiency going UP is not an alert; the form must not leave
    // "exceeds" selected from the previous metric.
    expect(condition.value).toBe("falls_below");
    expect([...condition.options].map((o) => o.value)).toEqual(["falls_below"]);
  });

  it("offers the organization's real applications for an application scope", async () => {
    renderForm();
    await screen.findByLabelText("Metric");
    fireEvent.change(screen.getByLabelText("Metric"), { target: { value: "agent_steps" } });
    fireEvent.change(screen.getByLabelText("Scope"), { target: { value: "application" } });

    const picker = (await screen.findByLabelText("Application to scope to")) as HTMLSelectElement;
    expect([...picker.options].map((o) => o.textContent)).toEqual([
      "Select an application…",
      "Support agent",
      "Billing agent",
    ]);

    fireEvent.change(picker, { target: { value: "app-2" } });
    expect(picker.value).toBe("app-2");
  });

  it("clears an application scope when the metric no longer allows it", async () => {
    renderForm();
    await screen.findByLabelText("Metric");
    fireEvent.change(screen.getByLabelText("Metric"), { target: { value: "agent_steps" } });
    fireEvent.change(screen.getByLabelText("Scope"), { target: { value: "application" } });
    fireEvent.change(await screen.findByLabelText("Application to scope to"), {
      target: { value: "app-1" },
    });

    // Inference cost has no application dimension. Carrying the reference over
    // would save a rule scoped to something the metric cannot be measured by.
    fireEvent.change(screen.getByLabelText("Metric"), { target: { value: "inference_cost" } });
    expect(screen.queryByLabelText("Application to scope to")).not.toBeInTheDocument();
    expect((screen.getByLabelText("Scope") as HTMLSelectElement).value).toBe("organization");
  });
});

describe("AlertFormPage — started from the Forecast page", () => {
  // The Forecast page's "alert me before this happens" link arrives with the
  // metric and condition in the query string, so the reader lands on a ready
  // rule rather than a blank form they have to reconstruct.
  const FORECAST_META: AlertMeta = {
    ...META,
    conditions: ["exceeds", "increase_pct", "budget_pct", "forecast_budget_pct"],
    valid_conditions: {
      inference_cost: ["exceeds", "increase_pct", "budget_pct", "forecast_budget_pct"],
      combined_cost: ["exceeds", "increase_pct", "budget_pct", "forecast_budget_pct"],
    },
    has_budget: true,
    budget_conditions: ["budget_pct", "forecast_budget_pct"],
  };

  function renderAt(url: string) {
    return render(
      <MemoryRouter initialEntries={[url]}>
        <AuthProvider>
          <AlertFormPage />
        </AuthProvider>
      </MemoryRouter>,
    );
  }

  it("prefills a projected-overspend rule from the link", async () => {
    vi.mocked(api.alertsMeta).mockResolvedValue(FORECAST_META);
    renderAt("/alerts/new?metric=combined_cost&condition=forecast_budget_pct");
    const condition = (await screen.findByLabelText("Condition")) as HTMLSelectElement;
    await waitFor(() => expect(condition.value).toBe("forecast_budget_pct"));
    expect((screen.getByLabelText("Metric") as HTMLSelectElement).value).toBe("combined_cost");
    expect((screen.getByLabelText("Alert name") as HTMLInputElement).value).toBe(
      "Projected to exceed budget",
    );
  });

  it("ignores names it does not know rather than trusting the URL", async () => {
    // Checked on what is SUBMITTED, not on what a <select> displays: a select
    // given a value it has no option for just shows its first option, so the
    // screen would look right while the form held the junk.
    vi.mocked(api.alertsMeta).mockResolvedValue(FORECAST_META);
    vi.mocked(api.createAlert).mockResolvedValue({ id: "a1" } as never);
    renderAt("/alerts/new?metric=drop_tables&condition=whatever");
    await screen.findByLabelText("Condition");
    fireEvent.change(screen.getByLabelText("Alert name"), { target: { value: "Spend" } });
    fireEvent.click(screen.getByRole("button", { name: /Create alert|Save/ }));
    await waitFor(() => expect(api.createAlert).toHaveBeenCalled());
    const sent = vi.mocked(api.createAlert).mock.calls[0][0];
    expect(sent.metric).toBe("inference_cost");
    expect(sent.condition_type).toBe("exceeds");
  });

  it("needs a budget for the projected rule, like the actual one", async () => {
    vi.mocked(api.alertsMeta).mockResolvedValue({ ...FORECAST_META, has_budget: false });
    renderAt("/alerts/new?metric=combined_cost&condition=forecast_budget_pct");
    expect(await screen.findByText(/no budget is configured/i)).toBeInTheDocument();
  });
});
