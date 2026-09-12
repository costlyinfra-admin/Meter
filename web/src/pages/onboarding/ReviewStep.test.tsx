import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { api, type Feature } from "../../api";
import { ReviewStep } from "./ReviewStep";

vi.mock("../../api", async (importActual) => {
  const actual = await importActual<typeof import("../../api")>();
  return {
    ...actual,
    api: {
      listFeatures: vi.fn(),
      runDiscovery: vi.fn(),
      discoveryRepos: vi.fn(),
      discoveryScope: vi.fn(),
      addFeature: vi.fn(),
      renameFeature: vi.fn(),
      deleteFeature: vi.fn(),
      splitFeature: vi.fn(),
      mergeFeatures: vi.fn(),
      setFeatureCategory: vi.fn(),
      featureCategories: vi.fn(),
      listProducts: vi.fn(),
      setFeatureProduct: vi.fn(),
      // ReviewStep now renders the discovery freshness line above its toolbar.
      discoverySchedule: vi.fn(),
      setDiscoverySchedule: vi.fn(),
      discoveryRuns: vi.fn(),
    },
  };
});

const summary = (over: Partial<import("../../api").DiscoverySummary>) => ({
  owner: "acme",
  prs: 0,
  repos: [] as string[],
  repos_with_prs: [] as string[],
  prs_by_repo: {} as Record<string, number>,
  repos_scanned: 0,
  proposals: 0,
  ...over,
});

const THREAT: Feature = {
  id: "f1",
  name: "Threat",
  description: "",
  status: "proposed",
  discovery_confidence: "high",
  category: "chat",
  category_source: "discovery",
  product_id: null,
  product_name: null,
  product_source: null,
  signals: [
    { id: "s1", signal_type: "pr", external_ref: "acme/core#1", confidence: "high" },
    { id: "s2", signal_type: "pr", external_ref: "acme/core#2", confidence: "high" },
    { id: "s3", signal_type: "branch", external_ref: "feature/threat-*", confidence: "high" },
  ],
};

describe("ReviewStep", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    // Component loads the saved scope on mount; default to "nothing saved".
    vi.mocked(api.discoveryScope).mockResolvedValue({ owner: null, repos: [] });
    // The product picker on each proposal reads one shared product list.
    vi.mocked(api.listProducts).mockResolvedValue({ products: [] });
    // The freshness line renders nothing without a schedule, which is fine for
    // every test here — they are about the feature list, not the schedule.
    vi.mocked(api.discoverySchedule).mockResolvedValue(null as never);
    vi.mocked(api.discoveryRuns).mockRejectedValue(new Error("not under test"));
    // The category picker fetches its vocabulary on mount.
    vi.mocked(api.featureCategories).mockResolvedValue({
      categories: [
        { value: "ui", label: "UI" },
        { value: "auth", label: "Auth" },
      ],
    });
  });

  it("runs discovery and renders proposals with evidence + confidence", async () => {
    vi.mocked(api.listFeatures).mockResolvedValueOnce([]).mockResolvedValue([THREAT]);
    vi.mocked(api.runDiscovery).mockResolvedValue(
      summary({
        prs: 3,
        repos: ["acme/core"],
        repos_with_prs: ["acme/core"],
        repos_scanned: 2,
        proposals: 1,
      }),
    );

    render(<ReviewStep />);
    expect(await screen.findByText("No features discovered yet")).toBeInTheDocument();

    fireEvent.change(screen.getByLabelText("GitHub organization"), { target: { value: "acme" } });
    fireEvent.click(screen.getByRole("button", { name: "Analyze last 90 days" }));

    expect(await screen.findByText(/Analyzed: 3 merged PRs/)).toBeInTheDocument();
    expect(await screen.findByText("Threat")).toBeInTheDocument();
    expect(screen.getByText("high confidence")).toBeInTheDocument();
    expect(screen.getByText("acme/core#1")).toBeInTheDocument();
    expect(screen.getByText("branch: feature/threat-*")).toBeInTheDocument();
  });

  it("lists org repositories and analyzes only the selected scope", async () => {
    vi.mocked(api.listFeatures).mockResolvedValue([]);
    vi.mocked(api.discoveryRepos).mockResolvedValue({
      owner: "transilienceai",
      repos: ["transilienceai/mcs", "transilienceai/docs"],
    });
    vi.mocked(api.runDiscovery).mockResolvedValue(
      summary({
        owner: "transilienceai",
        prs: 5,
        repos: ["transilienceai/mcs"],
        repos_scanned: 2,
        proposals: 2,
      }),
    );

    render(<ReviewStep />);
    fireEvent.change(await screen.findByLabelText("GitHub organization"), {
      target: { value: "transilienceai" },
    });
    fireEvent.click(screen.getByRole("button", { name: "List repositories" }));

    // Repos appear as checkboxes; select just "mcs".
    const mcs = await screen.findByText("transilienceai/mcs");
    fireEvent.click(mcs);
    fireEvent.click(screen.getByRole("button", { name: "Analyze 1 selected" }));

    await waitFor(() =>
      expect(api.runDiscovery).toHaveBeenCalledWith("transilienceai", ["transilienceai/mcs"]),
    );
    expect(await screen.findByText(/Repository: transilienceai\/mcs/)).toBeInTheDocument();
  });

  it("explains when no repositories are accessible (token/owner issue)", async () => {
    vi.mocked(api.listFeatures).mockResolvedValue([]);
    vi.mocked(api.runDiscovery).mockResolvedValue(summary({ owner: "cloudoku-training" }));

    render(<ReviewStep />);
    fireEvent.change(await screen.findByLabelText("GitHub organization"), {
      target: { value: "cloudoku-training" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Analyze last 90 days" }));

    expect(await screen.findByText(/No repositories accessible/)).toBeInTheDocument();
  });

  it("explains when repos are found but have no merged PRs", async () => {
    vi.mocked(api.listFeatures).mockResolvedValue([]);
    vi.mocked(api.runDiscovery).mockResolvedValue(
      summary({ owner: "cloudoku-training", repos_scanned: 1 }),
    );

    render(<ReviewStep />);
    fireEvent.change(await screen.findByLabelText("GitHub organization"), {
      target: { value: "cloudoku-training" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Analyze last 90 days" }));

    expect(await screen.findByText(/no merged PRs in the last 90 days/)).toBeInTheDocument();
  });

  it("deletes a proposal", async () => {
    vi.mocked(api.listFeatures).mockResolvedValue([THREAT]);
    vi.mocked(api.deleteFeature).mockResolvedValue(undefined);

    render(<ReviewStep />);
    fireEvent.click(await screen.findByRole("button", { name: "Delete" }));
    await waitFor(() => expect(api.deleteFeature).toHaveBeenCalledWith("f1"));
  });

  it("adds a manual feature", async () => {
    vi.mocked(api.listFeatures).mockResolvedValue([THREAT]);
    vi.mocked(api.addFeature).mockResolvedValue({ ...THREAT, id: "f2", name: "Manual" });

    render(<ReviewStep />);
    fireEvent.change(await screen.findByLabelText("New feature name"), {
      target: { value: "Manual" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Add" }));
    await waitFor(() => expect(api.addFeature).toHaveBeenCalledWith("Manual"));
  });

  it("lets someone correct the category guess, and remembers it", async () => {
    vi.mocked(api.listFeatures).mockResolvedValue([{ ...THREAT, category: "ui" }]);
    vi.mocked(api.setFeatureCategory).mockResolvedValue({ ...THREAT, category: "auth" });
    vi.mocked(api.featureCategories).mockResolvedValue({
      categories: [
        { value: "ui", label: "UI" },
        { value: "auth", label: "Auth" },
      ],
    });
    render(<ReviewStep />);

    // The badge, not the picker's <option> of the same name.
    expect(await screen.findByText("UI", { selector: ".badge" })).toBeInTheDocument();
    const picker = await screen.findByLabelText("Feature type");
    await waitFor(() => expect(picker).not.toBeDisabled());
    fireEvent.change(picker, { target: { value: "auth" } });
    await waitFor(() => expect(api.setFeatureCategory).toHaveBeenCalledWith("f1", "auth"));
  });
});
