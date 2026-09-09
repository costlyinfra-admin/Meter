import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
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
    scope: "us-east-1",
    scope_label: "region",
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
  provider({ type: "azure_cloud", name: "Microsoft Azure", short: "Azure" }),
  provider({ type: "gcp", name: "Google Cloud Platform", short: "GCP" }),
  provider({ type: "digitalocean", name: "DigitalOcean", short: "DigitalOcean" }),
  provider({ type: "mongodb_atlas", name: "MongoDB Atlas", short: "Atlas" }),
  provider({ type: "cloudflare", name: "Cloudflare", short: "Cloudflare" }),
  provider({ type: "snowflake", name: "Snowflake", short: "Snowflake" }),
];

describe("InfrastructureSources", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.infraProviders).mockResolvedValue(REGISTRY);
    vi.mocked(api.infraSummary).mockResolvedValue(summary());
  });

  it("offers every cloud and platform as connectable", async () => {
    render(<InfrastructureSources />);

    for (const name of [
      "Amazon Web Services",
      "Microsoft Azure",
      "Google Cloud Platform",
      "DigitalOcean",
      "MongoDB Atlas",
      "Cloudflare",
      "Snowflake",
    ]) {
      expect(await screen.findByText(name)).toBeInTheDocument();
    }
    // Every one is connectable — nothing is listed but withheld.
    expect(screen.getAllByRole("button", { name: "Connect" })).toHaveLength(REGISTRY.length);
    expect(screen.queryByText("Coming soon")).not.toBeInTheDocument();
  });

  /** The Connect button on one provider's row. */
  async function connect(name: string) {
    const row = (await screen.findByText(name)).closest("li") as HTMLElement;
    fireEvent.click(within(row).getByRole("button", { name: "Connect" }));
    return row;
  }

  it("shows the AWS setup steps, including the permission and the Bedrock rule", async () => {
    render(<InfrastructureSources />);
    await connect("Amazon Web Services");

    expect(screen.getByText(/ce:GetCostAndUsage/)).toBeInTheDocument();
    expect(screen.getByText(/Cost allocation tags/)).toBeInTheDocument();
    expect(screen.getByText(/billing data lags/)).toBeInTheDocument();
    expect(screen.getByText(/never added to infrastructure totals/)).toBeInTheDocument();
  });

  it("saves the credential through the shared encrypted-credential path", async () => {
    vi.mocked(api.saveCredential).mockResolvedValue(undefined);
    render(<InfrastructureSources />);
    await connect("Amazon Web Services");

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

  // Each cloud bills one model service another connector owns. The panel must
  // name THAT service — "some spend was excluded" explains nothing to someone
  // reconciling this page against their cloud console.
  it.each([
    ["aws", "Amazon Bedrock"],
    ["azure_cloud", "Azure OpenAI"],
    ["gcp", "Vertex AI"],
  ])("says how much %s spend was left out, and which service it was", async (type, service) => {
    vi.mocked(api.infraProviders).mockResolvedValue([provider({ ...CONNECTED, type })]);
    vi.mocked(api.infraSummary).mockResolvedValue(summary({ excluded: 900 }));
    render(<InfrastructureSources />);

    fireEvent.click(await screen.findByRole("button", { name: /Configure/ }));
    const note = await screen.findByText(/recorded but not counted here/);
    expect(note).toHaveTextContent("$900");
    expect(note).toHaveTextContent(service);
    expect(note).toHaveTextContent(/never counted twice/);
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

  it("shows the Azure setup steps, including the role and the Azure OpenAI rule", async () => {
    render(<InfrastructureSources />);
    await connect("Microsoft Azure");

    expect(screen.getByText(/Cost Management Reader/)).toBeInTheDocument();
    expect(screen.getByText(/cost data lags/)).toBeInTheDocument();
    expect(screen.getByText(/never added to infrastructure totals/)).toBeInTheDocument();
  });

  it("is honest that the GCP export has no history before it is switched on", async () => {
    render(<InfrastructureSources />);
    await connect("Google Cloud Platform");

    // The one thing that will surprise someone connecting GCP: empty history.
    expect(screen.getByText(/not backfilled/)).toBeInTheDocument();
    expect(screen.getByText(/BigQuery Data Viewer/)).toBeInTheDocument();
    // And why it works this way at all, rather than like the other two clouds.
    expect(screen.getByText(/Google publishes no cost API/)).toBeInTheDocument();
  });

  it("syncs each cloud through the same control", async () => {
    vi.mocked(api.infraProviders).mockResolvedValue([
      provider({ ...CONNECTED, type: "gcp", name: "Google Cloud Platform" }),
    ]);
    vi.mocked(api.syncInfrastructure).mockResolvedValue({
      provider: "gcp",
      items: 88,
      infrastructure: 1200,
      excluded: 0,
      attributed: 800,
      unattributed: 400,
      by_category: { infrastructure: 1200 },
    });
    render(<InfrastructureSources />);

    fireEvent.click(await screen.findByRole("button", { name: "Sync now" }));
    await waitFor(() => expect(api.syncInfrastructure).toHaveBeenCalledWith("gcp"));
    expect(await screen.findByText(/Read 88 line items/)).toBeInTheDocument();
  });

  it.each([
    ["DigitalOcean", /READ scope only/],
    ["MongoDB Atlas", /Organization Billing Viewer/],
    ["Cloudflare", /Billing → Read/],
    ["Snowflake", /ORGADMIN/],
  ])("shows %s's setup steps and the least privilege it needs", async (name, permission) => {
    render(<InfrastructureSources />);
    await connect(name);
    expect(screen.getByText(permission)).toBeInTheDocument();
  });

  it("warns that Cloudflare has no history to backfill", async () => {
    // A subscription describes its current period, so a first sync looks empty
    // for past months. Someone should learn that here, not from a blank chart.
    render(<InfrastructureSources />);
    await connect("Cloudflare");
    expect(screen.getByText(/no historical series to backfill/)).toBeInTheDocument();
  });

  it("explains why Snowflake reads currency rather than credits", async () => {
    render(<InfrastructureSources />);
    await connect("Snowflake");
    expect(screen.getByText(/estimate rather than your bill/)).toBeInTheDocument();
  });

  it("does not claim spend was excluded for a platform nothing else counts", async () => {
    // Cloudflare Workers AI is inference, but no other connector reads it, so it
    // is counted here. The excluded note must stay away.
    vi.mocked(api.infraProviders).mockResolvedValue([
      provider({ ...CONNECTED, type: "cloudflare", name: "Cloudflare" }),
    ]);
    vi.mocked(api.infraSummary).mockResolvedValue(
      summary({
        excluded: 0,
        by_category: [
          { category: "infrastructure", amount: 5200 },
          { category: "inference", amount: 120 },
        ],
      }),
    );
    render(<InfrastructureSources />);
    fireEvent.click(await screen.findByRole("button", { name: /Configure/ }));

    expect(await screen.findByText("Inference")).toBeInTheDocument();
    expect(screen.queryByText(/recorded but not counted here/)).not.toBeInTheDocument();
  });
});
