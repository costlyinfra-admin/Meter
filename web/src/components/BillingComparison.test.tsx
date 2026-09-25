/**
 * Reconciliation → Connected providers.
 *
 * What this screen must never do is the substance of these tests: call a
 * missing measurement a variance, show a variance on a dimension only one side
 * records, or leave someone without billing access with nothing to click.
 */
import { fireEvent, render as renderBare, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { BillingComparison } from "./BillingComparison";
import { api, type BillingBreakdown, type BillingProvider } from "../api";

vi.mock("../api", async (importActual) => {
  const actual = await importActual<typeof import("../api")>();
  return { ...actual, api: { billingComparison: vi.fn(), billingBreakdown: vi.fn() } };
});

const render = (ui: React.ReactElement) => renderBare(<MemoryRouter>{ui}</MemoryRouter>);

const provider = (over: Partial<BillingProvider> = {}): BillingProvider => ({
  provider: "anthropic",
  name: "Anthropic",
  connected: true,
  supported: true,
  status: "matched",
  provider_reported: 1000,
  tracked: 999.8,
  variance: 0.2,
  variance_pct: 0.02,
  estimated: false,
  billing_updated_at: new Date().toISOString(),
  currency: "USD",
  mixed_currency: false,
  ...over,
});

const comparison = (...providers: BillingProvider[]) => ({
  period: "2026-05-01",
  providers,
  tolerance: { absolute: 0.5, percent: 0.5 },
});

const breakdown = (over: Partial<BillingBreakdown> = {}): BillingBreakdown => ({
  period: "2026-05-01",
  provider: "anthropic",
  by_model: [
    {
      model: "claude-opus-5",
      provider_reported: 600,
      tracked: 400,
      variance: 200,
      variance_pct: 33.3,
    },
  ],
  provider_only: {
    by_day: [{ day: "2026-05-03", provider_reported: 120 }],
    by_account: [{ account: "Production", provider_reported: 900 }],
  },
  ...over,
});

beforeEach(() => {
  vi.mocked(api.billingComparison).mockResolvedValue(comparison(provider()));
  vi.mocked(api.billingBreakdown).mockResolvedValue(breakdown());
});

const rowFor = async (name: string) => (await screen.findByText(name)).closest("tr")!;

describe("the provider table", () => {
  it("shows both numbers, the variance and how fresh the billing data is", async () => {
    render(<BillingComparison />);
    const row = await rowFor("Anthropic");
    expect(within(row).getByText("Matched")).toBeInTheDocument();
    expect(within(row).getByText("$999.80")).toBeInTheDocument();
    expect(within(row).getByText("$1,000.00")).toBeInTheDocument();
    expect(within(row).getByText("just now")).toBeInTheDocument();
  });

  it("names the billing period it is comparing", async () => {
    render(<BillingComparison />);
    expect(await screen.findByText("2026-05")).toBeInTheDocument();
  });

  it("marks an open month as estimated, so a gap is not read as a discrepancy", async () => {
    vi.mocked(api.billingComparison).mockResolvedValue(comparison(provider({ estimated: true })));
    render(<BillingComparison />);
    expect(await screen.findByText(/estimated/)).toBeInTheDocument();
  });

  it("says the comparison changes nothing", async () => {
    render(<BillingComparison />);
    expect(await screen.findByText(/never changes it/)).toBeInTheDocument();
  });
});

describe("states that are not a variance", () => {
  it("does not show a variance figure when nothing was metered", async () => {
    // The state every customer is in before the SDK is installed. A variance
    // column here would say their bill is 100% wrong.
    vi.mocked(api.billingComparison).mockResolvedValue(
      comparison(
        provider({ status: "no_metered_data", tracked: 0, variance: 800, variance_pct: 100 }),
      ),
    );
    render(<BillingComparison />);
    const row = await rowFor("Anthropic");
    expect(within(row).getByText("No metered data")).toBeInTheDocument();
    expect(within(row).queryByText(/100.0%/)).not.toBeInTheDocument();
    expect(screen.getByText(/install the metering SDK/i)).toBeInTheDocument();
  });

  it("offers Connect billing data when billing access is missing", async () => {
    vi.mocked(api.billingComparison).mockResolvedValue(
      comparison(provider({ status: "billing_access_required", provider_reported: 0, tracked: 0 })),
    );
    render(<BillingComparison />);
    const row = await rowFor("Anthropic");
    expect(within(row).getByRole("link", { name: "Connect billing data" })).toHaveAttribute(
      "href",
      "/cost-sources",
    );
  });

  it("explains that a self-hosted provider has no bill to check against", async () => {
    vi.mocked(api.billingComparison).mockResolvedValue(
      comparison(
        provider({ provider: "ollama", name: "ollama", status: "not_supported", supported: false }),
      ),
    );
    render(<BillingComparison />);
    expect(await screen.findByText("Not supported")).toBeInTheDocument();
    expect(screen.getByText(/no billing API can confirm/i)).toBeInTheDocument();
  });

  it("only offers a breakdown where there is a variance to break down", async () => {
    vi.mocked(api.billingComparison).mockResolvedValue(
      comparison(
        provider({ status: "matched" }),
        provider({
          provider: "openai",
          name: "OpenAI",
          status: "variance",
        }),
      ),
    );
    render(<BillingComparison />);
    await screen.findByText("OpenAI");
    expect(screen.getAllByRole("button", { name: /Show breakdown/ })).toHaveLength(1);
  });
});

describe("the breakdown", () => {
  const varied = () =>
    comparison(provider({ status: "variance", tracked: 600, variance: 400, variance_pct: 40 }));

  it("compares by model, which is the dimension both sides record", async () => {
    vi.mocked(api.billingComparison).mockResolvedValue(varied());
    render(<BillingComparison />);
    fireEvent.click(await screen.findByRole("button", { name: /Show breakdown/ }));

    const row = await rowFor("claude-opus-5");
    expect(within(row).getByText("$400.00")).toBeInTheDocument();
    expect(within(row).getByText("$600.00")).toBeInTheDocument();
    expect(within(row).getByText("$200.00")).toBeInTheDocument();
  });

  it("shows day and account as provider-reported only, with no variance", async () => {
    // Meter's metered rows carry neither, so a variance column beside them
    // would be half a comparison presented as a whole one.
    vi.mocked(api.billingComparison).mockResolvedValue(varied());
    render(<BillingComparison />);
    fireEvent.click(await screen.findByRole("button", { name: /Show breakdown/ }));

    expect(await screen.findByText(/Provider-reported only/)).toBeInTheDocument();
    expect(screen.getByText(/cannot be compared/)).toBeInTheDocument();
    expect(screen.getByText(/Production — \$900\.00/)).toBeInTheDocument();
    expect(screen.getByText(/2026-05-03 — \$120\.00/)).toBeInTheDocument();
  });

  it("closes again", async () => {
    vi.mocked(api.billingComparison).mockResolvedValue(varied());
    render(<BillingComparison />);
    fireEvent.click(await screen.findByRole("button", { name: /Show breakdown/ }));
    fireEvent.click(await screen.findByRole("button", { name: /Hide breakdown/ }));
    await waitFor(() => expect(screen.queryByText("claude-opus-5")).not.toBeInTheDocument());
  });
});

describe("when there is nothing to compare", () => {
  it("says no billing data is available yet, and points at connecting some", async () => {
    vi.mocked(api.billingComparison).mockResolvedValue(comparison());
    render(<BillingComparison />);
    expect(await screen.findByText("No billing data available yet")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Connect billing data" })).toBeInTheDocument();
  });

  it("keeps statement import available as the fallback", async () => {
    vi.mocked(api.billingComparison).mockResolvedValue(comparison());
    render(<BillingComparison />);
    expect(await screen.findByRole("link", { name: /import a statement/i })).toHaveAttribute(
      "href",
      "/reconciliation/import",
    );
  });
});
