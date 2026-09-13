import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { api, ApiError } from "./api";
import { App } from "./App";
import { AuthProvider } from "./auth/AuthContext";

vi.mock("./api", async (importActual) => {
  const actual = await importActual<typeof import("./api")>();
  return {
    ...actual,
    api: {
      me: vi.fn(),
      connectors: vi.fn(),
      logout: vi.fn(),
      login: vi.fn(),
      signup: vi.fn(),
      listFeatures: vi.fn(),
      dashboard: vi.fn(),
      budgetForecast: vi.fn(),
      alertsSummary: vi.fn(),
      reconSettings: vi.fn(),
    },
  };
});

function renderApp() {
  return render(
    <MemoryRouter initialEntries={["/"]}>
      <AuthProvider>
        <App />
      </AuthProvider>
    </MemoryRouter>,
  );
}

describe("App routing", () => {
  beforeEach(() => vi.clearAllMocks());

  it("redirects signed-out users to the login page", async () => {
    vi.mocked(api.me).mockRejectedValue(new ApiError(401, "Not authenticated"));
    renderApp();
    expect(await screen.findByRole("heading", { name: "Sign in" })).toBeInTheDocument();
    // One-click demo entry is offered on the login screen.
    expect(screen.getByRole("button", { name: "View the demo" })).toBeInTheDocument();
  });

  it("sends authenticated users into the app shell on the Overview", async () => {
    vi.mocked(api.me).mockResolvedValue({ id: "u1", tenant_id: "t1", email: "cto@acme.com" });
    // The Overview asks for a budget forecast on mount; this route test only
    // cares that the shell renders, so an empty answer is enough.
    vi.mocked(api.budgetForecast).mockRejectedValue(new ApiError(404, "no budget"));
    // Reconciliation is opt-in; the shell asks and this organization has it off.
    vi.mocked(api.reconSettings).mockResolvedValue({
      available: true,
      enabled: false,
      tolerance_abs: 1,
      tolerance_pct: 0.5,
    });
    vi.mocked(api.alertsSummary).mockResolvedValue({
      triggered: 0,
      healthy: 0,
      delivery_errors: 0,
      disabled: 0,
      unread: 0,
    });
    vi.mocked(api.dashboard).mockResolvedValue({
      period: "2026-05-01",
      start: "2026-05-01",
      end: "2026-05-01",
      months: 1,
      features: [],
      unattributed: { build_cost: 0, inference_cost: 0 },
      highlights: { most_expensive: null, optimization: null, highest_cost_per_user: null },
      insights: [],
      actions: [],
      trend: [],
      providers: [],
      data_updated_at: null,
      inference_updated_at: null,
      build_updated_at: null,
      totals: {
        build_cost: 0,
        inference_cost: 0,
        estimated_inference: 0,
        prev_build_cost: 0,
        prev_inference_cost: 0,
        tokens_in: 0,
        tokens_out: 0,
      },
    });
    renderApp();
    // The Overview page + the sidebar nav both render (proves the shell).
    expect(await screen.findByRole("heading", { name: "Overview" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Connect sources" })).toBeInTheDocument();
  });
});
