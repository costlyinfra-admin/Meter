import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { api, type InfraProvider, type InfraSummary } from "../api";
import { InfrastructureSources } from "./InfrastructureSources";

vi.mock("../api", async (importActual) => {
  const actual = await importActual<typeof import("../api")>();
  return {
    ...actual,
    api: {
      infraProviders: vi.fn(),
      infraSummary: vi.fn(),
      syncInfrastructure: vi.fn(),
      saveCredential: vi.fn(),
    },
  };
});

function provider(over: Partial<InfraProvider> = {}): InfraProvider {
  return {
    type: "aws",
    name: "Amazon Web Services",
    short: "AWS",
    status: "available",
    note: "Reads AWS Cost Explorer — the whole bill, read-only.",
    connected: false,
    last_sync: null,
    config: null,
    ...over,
  };
}

const CONNECTED = provider({
  connected: true,
  config: {
    tag: "feature",
    metric: "UnblendedCost",
    granularity: "DAILY",
    region: "us-east-1",
    group_by: ["SERVICE", "TAG"],
  },
  last_sync: {
    status: "success",
    started_at: "2026-09-07T02:00:00Z",
    finished_at: "2026-09-07T02:00:30Z",
    items: 412,
    amount: 5200,
    error_message: null,
  },
});

function summary(over: Partial<InfraSummary> = {}): InfraSummary {
  return {
    provider: "aws",
    month: "2026-09-01",
    total: 5200,
    attributed: 3200,
    unattributed: 2000,
    excluded: 0,
    rows: 412,
    by_category: [{ category: "infrastructure", amount: 5200 }],
    services: [{ service: "Amazon Relational Database Service", amount: 5200, attributed: 3200 }],
    ...over,
  };
}

const REGISTRY = [
  provider(),
  provider({ type: "azure_cloud", name: "Microsoft Azure", short: "Azure", status: "coming_soon" }),
  provider({ type: "gcp", name: "Google Cloud Platform", short: "GCP", status: "coming_soon" }),
];

describe("InfrastructureSources", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.infraProviders).mockResolvedValue(REGISTRY);
    vi.mocked(api.infraSummary).mockResolvedValue(summary());
  });

  it("lists AWS as connectable and Azure/GCP as coming soon", async () => {
    render(<InfrastructureSources />);

    expect(await screen.findByText("Amazon Web Services")).toBeInTheDocument();
    expect(screen.getByText("Microsoft Azure")).toBeInTheDocument();
    expect(screen.getByText("Google Cloud Platform")).toBeInTheDocument();
    // AWS can be connected; the other two are shown, not offered.
    expect(screen.getByRole("button", { name: "Connect" })).toBeInTheDocument();
    expect(screen.getAllByText("Coming soon")).toHaveLength(2);
  });

  it("shows the AWS setup steps, including the permission and the Bedrock rule", async () => {
    render(<InfrastructureSources />);
    fireEvent.click(await screen.findByRole("button", { name: "Connect" }));

    expect(screen.getByText(/ce:GetCostAndUsage/)).toBeInTheDocument();
    expect(screen.getByText(/Cost allocation tags/)).toBeInTheDocument();
    expect(screen.getByText(/billing data lags/)).toBeInTheDocument();
    expect(screen.getByText(/never added to infrastructure totals/)).toBeInTheDocument();
  });

  it("saves the credential through the shared encrypted-credential path", async () => {
    vi.mocked(api.saveCredential).mockResolvedValue(undefined);
    render(<InfrastructureSources />);
    fireEvent.click(await screen.findByRole("button", { name: "Connect" }));

    const blob = '{"access_key_id":"AKIA","secret_access_key":"s"}';
    fireEvent.change(screen.getByLabelText(/Amazon Web Services credentials/), {
      target: { value: blob },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => expect(api.saveCredential).toHaveBeenCalledWith("aws", blob));
  });

  it("reports connection state and when it last synced", async () => {
    vi.mocked(api.infraProviders).mockResolvedValue([CONNECTED]);
    render(<InfrastructureSources />);

    expect(await screen.findByText("Connected")).toBeInTheDocument();
    expect(screen.getByText(/Last synced/)).toBeInTheDocument();
  });

  it("surfaces a failed sync on the card instead of looking connected and fine", async () => {
    vi.mocked(api.infraProviders).mockResolvedValue([
      provider({
        connected: true,
        last_sync: {
          status: "error",
          started_at: "2026-09-07T02:00:00Z",
          finished_at: "2026-09-07T02:00:01Z",
          items: 0,
          amount: 0,
          error_message: "AWS rejected the credentials.",
        },
      }),
    ]);
    render(<InfrastructureSources />);

    expect(await screen.findByText(/Last sync failed/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /Configure/ }));
    expect(await screen.findByText("AWS rejected the credentials.")).toBeInTheDocument();
  });

  it("syncs on demand and says what came back", async () => {
    vi.mocked(api.infraProviders).mockResolvedValue([CONNECTED]);
    vi.mocked(api.syncInfrastructure).mockResolvedValue({
      provider: "aws",
      items: 412,
      infrastructure: 5200,
      excluded: 0,
      attributed: 3200,
      unattributed: 2000,
      by_category: { infrastructure: 5200 },
    });
    render(<InfrastructureSources />);

    fireEvent.click(await screen.findByRole("button", { name: "Sync now" }));
    await waitFor(() => expect(api.syncInfrastructure).toHaveBeenCalledWith("aws"));
    expect(await screen.findByText(/Read 412 line items/)).toBeInTheDocument();
  });

  it("says how much Bedrock spend was left out, and why", async () => {
    vi.mocked(api.infraProviders).mockResolvedValue([CONNECTED]);
    vi.mocked(api.infraSummary).mockResolvedValue(summary({ excluded: 900 }));
    render(<InfrastructureSources />);

    fireEvent.click(await screen.findByRole("button", { name: /Configure/ }));
    // The excluded number is stated, not hidden — someone comparing this against
    // the AWS console has to be able to see where the difference went.
    expect(await screen.findByText(/\$900/)).toBeInTheDocument();
    expect(screen.getByText(/never counted twice/)).toBeInTheDocument();
  });

  it("breaks the month down by service and by category", async () => {
    vi.mocked(api.infraProviders).mockResolvedValue([CONNECTED]);
    vi.mocked(api.infraSummary).mockResolvedValue(
      summary({
        by_category: [
          { category: "infrastructure", amount: 5200 },
          { category: "self_hosted", amount: 800 },
        ],
      }),
    );
    render(<InfrastructureSources />);
    fireEvent.click(await screen.findByRole("button", { name: /Configure/ }));

    expect(await screen.findByText("Amazon Relational Database Service")).toBeInTheDocument();
    expect(screen.getByText("Self-hosted models")).toBeInTheDocument();
    // The tag driving attribution is shown, so an empty Attributed column is
    // explainable rather than mysterious.
    expect(screen.getByText(/Attributing by the/)).toBeInTheDocument();
  });

  it("explains an empty month instead of showing a blank panel", async () => {
    vi.mocked(api.infraProviders).mockResolvedValue([CONNECTED]);
    vi.mocked(api.infraSummary).mockResolvedValue(
      summary({ total: 0, attributed: 0, unattributed: 0, rows: 0, by_category: [], services: [] }),
    );
    render(<InfrastructureSources />);
    fireEvent.click(await screen.findByRole("button", { name: /Configure/ }));

    expect(await screen.findByText(/No infrastructure line items/)).toBeInTheDocument();
  });

  it("re-reads the card after a failed sync, so its state is not stale", async () => {
    vi.mocked(api.infraProviders)
      .mockResolvedValueOnce([CONNECTED])
      .mockResolvedValue([
        provider({
          connected: true,
          last_sync: {
            status: "error",
            started_at: "2026-09-07T03:00:00Z",
            finished_at: "2026-09-07T03:00:01Z",
            items: 0,
            amount: 0,
            error_message: "AWS rejected the credentials.",
          },
        }),
      ]);
    vi.mocked(api.syncInfrastructure).mockRejectedValue(new Error("AWS rejected the credentials."));
    render(<InfrastructureSources />);

    expect(await screen.findByText(/Last synced/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Sync now" }));

    // The failure is reported AND the card stops claiming the older success.
    expect(await screen.findByText("AWS rejected the credentials.")).toBeInTheDocument();
    await waitFor(() => expect(screen.getByText(/Last sync failed/)).toBeInTheDocument());
    expect(screen.queryByText(/Last synced/)).not.toBeInTheDocument();
  });
});
