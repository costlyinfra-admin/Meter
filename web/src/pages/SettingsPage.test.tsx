import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { api, type OrgSettings } from "../api";
import { SettingsPage } from "./SettingsPage";

vi.mock("../api", async (importActual) => {
  const actual = await importActual<typeof import("../api")>();
  return {
    ...actual,
    api: {
      getSettings: vi.fn(),
      updateSettings: vi.fn(),
      getBudget: vi.fn(),
      setBudget: vi.fn(),
      removeBudget: vi.fn(),
      connectors: vi.fn(),
      discoveryLlm: vi.fn(),
      discoveryLlmProviders: vi.fn(),
      saveDiscoveryLlm: vi.fn(),
      testDiscoveryLlm: vi.fn(),
      setDiscoveryLlmEnabled: vi.fn(),
      removeDiscoveryLlm: vi.fn(),
    },
  };
});

const SETTINGS: OrgSettings = {
  org_name: "Transilience AI",
  timezone: "America/Los_Angeles",
  currency: "USD",
  customer_id_storage: "hashed",
  store_prompts: false,
  data_retention: "indefinite",
  trace_retention_days: 30,
  agent_stale_after_minutes: 10,
  content_capture: "disabled",
};

function renderPage(route = "/settings") {
  return render(
    <MemoryRouter initialEntries={[route]}>
      <SettingsPage />
    </MemoryRouter>,
  );
}

/** Open a Settings tab the way a person would. Panels stay mounted but hidden,
 *  and accessible queries ignore hidden content — so every test that reaches
 *  past the first tab has to click its way there, same as a user. */
async function openTab(name: RegExp | string) {
  fireEvent.click(await screen.findByRole("tab", { name }));
}

function setupMocks() {
  vi.clearAllMocks();
  vi.mocked(api.getSettings).mockResolvedValue({ ...SETTINGS });
  // Budgets card loads alongside the page; default to "none set".
  vi.mocked(api.getBudget).mockResolvedValue({ budget: null });
  vi.mocked(api.updateSettings).mockImplementation(async (p) => ({ ...SETTINGS, ...p }));
  // The BYOK card loads alongside the page; default to "not configured".
  vi.mocked(api.discoveryLlm).mockResolvedValue({
    configured: false,
    enabled: false,
    has_key: false,
  });
  vi.mocked(api.discoveryLlmProviders).mockResolvedValue({
    providers: [
      { value: "groq", base_url: "https://api.groq.com/openai/v1" },
      { value: "custom", base_url: "" },
    ],
    default_model: "llama-3.3-70b-versatile",
  });
}

describe("SettingsPage", () => {
  beforeEach(setupMocks);

  it("opens on Organization, with the other sections behind their tabs", async () => {
    renderPage();
    expect(await screen.findByRole("heading", { name: "Organization" })).toBeInTheDocument();
    expect(screen.getByLabelText("Organization name")).toHaveValue("Transilience AI");
    expect(screen.getByLabelText("Time zone")).toHaveValue("America/Los_Angeles");
    expect(screen.getByLabelText("Default currency")).toHaveValue("USD");

    // Four tabs, one selected.
    const tabs = screen.getAllByRole("tab");
    expect(tabs.map((t) => t.textContent)).toEqual([
      "Organization",
      "Budgets",
      "Bring your own key",
      "Privacy & data",
    ]);
    expect(tabs[0]).toHaveAttribute("aria-selected", "true");

    // The other panels are mounted — so drafts survive — but not on screen, and
    // not reachable by role, until their tab is opened.
    expect(screen.getByLabelText("Data retention")).not.toBeVisible();
    expect(screen.queryByRole("heading", { name: "Privacy & data" })).not.toBeInTheDocument();

    await openTab("Privacy & data");
    expect(screen.getByLabelText("Customer identifiers")).toHaveValue("hashed");
    expect(screen.getByLabelText("Data retention")).toHaveValue("indefinite");
  });

  it("keeps an unsaved edit when you leave a tab and come back", async () => {
    renderPage();
    const input = await screen.findByLabelText("Organization name");
    fireEvent.change(input, { target: { value: "Half typed" } });

    await openTab("Privacy & data");
    await openTab("Organization");
    // Panels are hidden, not unmounted, so the draft survives.
    expect(screen.getByLabelText("Organization name")).toHaveValue("Half typed");
    expect(api.getSettings).toHaveBeenCalledTimes(1);
  });

  it("opens the tab named by the URL fragment, so /settings#budgets lands there", async () => {
    // The Alerts form links straight here when a rule needs a budget.
    renderPage("/settings#budgets");
    expect(await screen.findByRole("heading", { name: "Budgets" })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "Budgets" })).toHaveAttribute("aria-selected", "true");
  });

  it("does NOT render Connected sources, Account, or a Sign out button", async () => {
    renderPage();
    await screen.findByRole("heading", { name: "Organization" });
    expect(screen.queryByText("Connected sources")).not.toBeInTheDocument();
    expect(screen.queryByText("Account")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /sign out/i })).not.toBeInTheDocument();
    // No provider/connector UI leaks in.
    expect(api.connectors).not.toHaveBeenCalled();
  });

  it("edits and saves the organization name", async () => {
    renderPage();
    const input = await screen.findByLabelText("Organization name");
    fireEvent.change(input, { target: { value: "Acme Security" } });
    fireEvent.click(screen.getByRole("button", { name: "Save changes" }));

    await waitFor(() =>
      expect(api.updateSettings).toHaveBeenCalledWith(
        expect.objectContaining({ org_name: "Acme Security" }),
      ),
    );
    expect(await screen.findByText("Saved ✓")).toBeInTheDocument();
  });

  it("saves privacy preferences together", async () => {
    renderPage();
    await openTab("Privacy & data");
    fireEvent.click(screen.getByLabelText("Store prompt content")); // Off -> On
    fireEvent.change(screen.getByLabelText("Data retention"), { target: { value: "90d" } });
    fireEvent.click(screen.getByRole("button", { name: "Save changes" }));

    await waitFor(() =>
      expect(api.updateSettings).toHaveBeenCalledWith(
        expect.objectContaining({ store_prompts: true, data_retention: "90d" }),
      ),
    );
  });
});

describe("Feature discovery model (BYOK)", () => {
  beforeEach(setupMocks);

  // The panel is hidden until its tab is open, so these render straight to it.

  it("says Meter's model is in use until one is configured", async () => {
    renderPage("/settings#byok");
    expect(await screen.findByText("Feature discovery model")).toBeInTheDocument();
    expect(screen.getByText("Using Meter's model")).toBeInTheDocument();
  });

  it("saves a configuration and never renders the key afterwards", async () => {
    vi.mocked(api.saveDiscoveryLlm).mockResolvedValue({
      configured: true,
      enabled: true,
      has_key: true,
      provider: "groq",
      model: "llama-3.3-70b-versatile",
      base_url: "https://api.groq.com/openai/v1",
    });
    renderPage("/settings#byok");
    await screen.findByText("Feature discovery model");

    fireEvent.change(screen.getByLabelText("Model"), {
      target: { value: "llama-3.3-70b-versatile" },
    });
    fireEvent.change(screen.getByLabelText("API key"), { target: { value: "gsk_secret_value" } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() =>
      expect(api.saveDiscoveryLlm).toHaveBeenCalledWith(
        expect.objectContaining({ provider: "groq", api_key: "gsk_secret_value" }),
      ),
    );
    expect(await screen.findByText("Using your model")).toBeInTheDocument();
    // The secret is gone from the DOM the moment it is saved.
    expect(document.body.textContent).not.toContain("gsk_secret_value");
    expect(screen.queryByDisplayValue("gsk_secret_value")).toBeNull();
  });

  it("edits without re-entering the key", async () => {
    vi.mocked(api.discoveryLlm).mockResolvedValue({
      configured: true,
      enabled: true,
      has_key: true,
      provider: "groq",
      model: "old-model",
      base_url: "https://api.groq.com/openai/v1",
    });
    vi.mocked(api.saveDiscoveryLlm).mockResolvedValue({
      configured: true,
      enabled: true,
      has_key: true,
      provider: "groq",
      model: "new-model",
      base_url: "https://api.groq.com/openai/v1",
    });
    renderPage("/settings#byok");
    fireEvent.click(await screen.findByRole("button", { name: "Edit" }));

    // The field advertises that a key is stored, and holds no value.
    const key = screen.getByLabelText("API key") as HTMLInputElement;
    expect(key.value).toBe("");
    expect(key.placeholder).toMatch(/leave blank to keep it/i);

    fireEvent.change(screen.getByLabelText("Model"), { target: { value: "new-model" } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => expect(api.saveDiscoveryLlm).toHaveBeenCalled());
    // No api_key sent, so the server keeps the stored one.
    expect(vi.mocked(api.saveDiscoveryLlm).mock.calls[0][0]).not.toHaveProperty("api_key");
  });

  it("reports a failed connection test", async () => {
    vi.mocked(api.testDiscoveryLlm).mockResolvedValue({
      ok: false,
      error: "401: invalid api key: ***",
    });
    renderPage("/settings#byok");
    await screen.findByText("Feature discovery model");
    fireEvent.click(screen.getByRole("button", { name: "Test connection" }));

    expect(await screen.findByText(/401: invalid api key/)).toBeInTheDocument();
    expect(document.body.textContent).toContain("***"); // redacted, as the server sent it
  });

  it("can switch back to Meter's model without discarding the config", async () => {
    vi.mocked(api.discoveryLlm).mockResolvedValue({
      configured: true,
      enabled: true,
      has_key: true,
      provider: "groq",
      model: "m",
      base_url: "u",
    });
    vi.mocked(api.setDiscoveryLlmEnabled).mockResolvedValue({
      configured: true,
      enabled: false,
      has_key: true,
      provider: "groq",
      model: "m",
      base_url: "u",
    });
    renderPage("/settings#byok");
    fireEvent.click(await screen.findByRole("button", { name: "Use Meter's model" }));

    await waitFor(() => expect(api.setDiscoveryLlmEnabled).toHaveBeenCalledWith(false));
    expect(await screen.findByText("Using Meter's model")).toBeInTheDocument();
    // Still configured, so it can be switched back on.
    expect(screen.getByRole("button", { name: "Use my model" })).toBeInTheDocument();
  });
});

describe("Budgets", () => {
  beforeEach(setupMocks);

  // Same as BYOK: render with the fragment that opens this tab.

  it("says there is no budget rather than showing a plausible default", async () => {
    renderPage("/settings#budgets");
    expect(await screen.findByRole("heading", { name: "Budgets" })).toBeInTheDocument();
    expect(await screen.findByText(/No budget is set/)).toBeInTheDocument();
    // Nothing prefilled: an amount in the box would read as a budget somebody set.
    expect(screen.getByLabelText(/Budget amount/)).toHaveValue(null);
    expect(screen.queryByRole("button", { name: "Remove budget" })).not.toBeInTheDocument();
  });

  it("sets a budget, then offers to remove it", async () => {
    vi.mocked(api.setBudget).mockResolvedValue({
      budget: {
        amount: 50000,
        cadence: "annual",
        currency: "USD",
        effective_from: "2026-01-01",
        updated_at: "2026-05-01T00:00:00Z",
        updated_by: "cto@acme.com",
      },
    });
    renderPage("/settings#budgets");
    await screen.findByRole("heading", { name: "Budgets" });

    fireEvent.change(screen.getByLabelText(/Budget amount/), { target: { value: "50000" } });
    fireEvent.change(screen.getByLabelText("Applies"), { target: { value: "annual" } });
    fireEvent.change(screen.getByLabelText("Effective from"), {
      target: { value: "2026-01-01" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Set budget" }));

    await waitFor(() =>
      expect(api.setBudget).toHaveBeenCalledWith({
        amount: 50000,
        cadence: "annual",
        effective_from: "2026-01-01",
      }),
    );
    expect(await screen.findByText(/Current budget: \$50,000 per year/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Remove budget" })).toBeInTheDocument();
  });

  it("refuses an amount of zero without asking the server", async () => {
    renderPage("/settings#budgets");
    await screen.findByRole("heading", { name: "Budgets" });
    fireEvent.change(screen.getByLabelText(/Budget amount/), { target: { value: "0" } });
    fireEvent.click(screen.getByRole("button", { name: "Set budget" }));

    expect(await screen.findByText(/greater than 0/)).toBeInTheDocument();
    expect(api.setBudget).not.toHaveBeenCalled();
  });

  it("removes the budget and goes back to saying there is none", async () => {
    vi.mocked(api.getBudget).mockResolvedValue({
      budget: {
        amount: 12000,
        cadence: "monthly",
        currency: "USD",
        effective_from: "2026-01-01",
        updated_at: null,
        updated_by: null,
      },
    });
    vi.mocked(api.removeBudget).mockResolvedValue({ budget: null });
    renderPage("/settings#budgets");

    expect(await screen.findByText(/Current budget: \$12,000 per month/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Remove budget" }));

    await waitFor(() => expect(api.removeBudget).toHaveBeenCalled());
    expect(await screen.findByText(/No budget is set/)).toBeInTheDocument();
  });
});

describe("Request-level settings", () => {
  it("saves trace retention and the stale threshold with the rest of privacy", async () => {
    vi.mocked(api.updateSettings).mockResolvedValue({
      ...SETTINGS,
      trace_retention_days: 90,
      agent_stale_after_minutes: 30,
    });
    renderPage();
    fireEvent.click(await screen.findByRole("tab", { name: /Privacy/ }));

    const retention = await screen.findByLabelText("Trace retention");
    expect(retention).toHaveValue("30");
    fireEvent.change(retention, { target: { value: "90" } });
    fireEvent.change(screen.getByLabelText("Agent stale threshold"), { target: { value: "30" } });
    fireEvent.click(screen.getByRole("button", { name: "Save changes" }));

    await waitFor(() =>
      expect(api.updateSettings).toHaveBeenCalledWith(
        expect.objectContaining({ trace_retention_days: 90, agent_stale_after_minutes: 30 }),
      ),
    );
  });

  it("says plainly that trace retention does not touch cost totals", async () => {
    renderPage();
    fireEvent.click(await screen.findByRole("tab", { name: /Privacy/ }));
    // Someone shortening retention needs to know their bill history survives.
    expect(
      await screen.findByText(/deleting traces never changes what a month cost/i),
    ).toBeInTheDocument();
  });

  it("explains that a stale agent has not failed", async () => {
    renderPage();
    fireEvent.click(await screen.findByRole("tab", { name: /Privacy/ }));
    expect(await screen.findByText(/has not failed and may still finish/i)).toBeInTheDocument();
  });

  it("shows content capture as disabled, with no way to enable it", async () => {
    renderPage();
    fireEvent.click(await screen.findByRole("tab", { name: /Privacy/ }));

    expect(await screen.findByText("Content capture")).toBeInTheDocument();
    expect(screen.getByText("Disabled")).toBeInTheDocument();
    // Stated, not offered: there is no control, so nothing can turn it on.
    expect(screen.queryByLabelText("Content capture")).not.toBeInTheDocument();
    expect(screen.queryByText("Redacted")).not.toBeInTheDocument();
    expect(screen.queryByText("Full")).not.toBeInTheDocument();
  });

  it("says what Meter records instead of content", async () => {
    renderPage();
    fireEvent.click(await screen.findByRole("tab", { name: /Privacy/ }));
    expect(
      await screen.findByText(/records prompt identity, version, tokens and cost/i),
    ).toBeInTheDocument();
  });
});
