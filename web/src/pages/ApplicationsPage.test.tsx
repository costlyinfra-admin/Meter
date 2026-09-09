/**
 * Applications, list and detail.
 *
 * These pages are deliberately the feature views one level up, so the tests
 * check the things that make them feel the same: a table whose rows open the
 * detail, a search that narrows it, a window that scopes every number and
 * survives in the URL, and numbers that say "—" when there is nothing to know
 * rather than "0" — because free and never-happened are different answers.
 */
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { api, type AiApplication } from "../api";
import { ApplicationDetail, ApplicationsPage } from "./ApplicationsPage";

vi.mock("../api", async (importActual) => {
  const actual = await importActual<typeof import("../api")>();
  return { ...actual, api: { aiApplications: vi.fn(), aiApplication: vi.fn() } };
});

const app = (over: Partial<AiApplication> = {}): AiApplication => ({
  id: "a1",
  name: "Support agent",
  slug: "support-agent",
  description: "Answers customer tickets.",
  owner: "platform",
  runs: 225,
  spend: 4.83,
  tokens: 1_300_000,
  features: 2,
  active: 3,
  stale: 0,
  cost_per_run: 0.02,
  error_rate: 0.044,
  prior_spend: 4,
  prior_runs: 200,
  spend_change: 0.2,
  ...over,
});

const renderList = (route = "/applications") =>
  render(
    <MemoryRouter initialEntries={[route]}>
      <Routes>
        <Route path="/applications" element={<ApplicationsPage />} />
        <Route path="/applications/:id" element={<ApplicationDetail />} />
      </Routes>
    </MemoryRouter>,
  );

const renderDetail = (route = "/applications/a1") => renderList(route);

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(api.aiApplications).mockResolvedValue({
    applications: [app(), app({ id: "a2", name: "Document review", slug: "document-review" })],
    from: "2026-08-10",
    to: "2026-09-09",
  });
  vi.mocked(api.aiApplication).mockResolvedValue(
    app({
      by_feature: [{ feature: "Report generator", spend: 4.83, runs: 225 }],
      by_model: [
        { provider: "anthropic", model: "claude-opus-4-8", spend: 3, calls: 6 },
        { provider: "openai", model: "gpt-4o", spend: 1.83, calls: 108 },
      ],
      releases: [{ release: "2026.9.1", runs: 220, spend: 4.8 }],
    }),
  );
});

describe("Applications — the list", () => {
  it("lists applications in a table, one row each", async () => {
    renderList();
    const table = await screen.findByRole("table");
    const rows = within(table).getAllByRole("row").slice(1); // drop the header
    expect(rows).toHaveLength(2);
    expect(within(rows[0]).getByRole("link", { name: "Support agent" })).toBeInTheDocument();
  });

  it("opens the detail when a row is clicked, not only its link", async () => {
    // The whole row is the target on the Overview's feature table; anything
    // less means the click lands on the row and nothing happens.
    renderList();
    const row = (await screen.findByRole("link", { name: "Support agent" })).closest("tr")!;
    fireEvent.click(row);
    expect(await screen.findByRole("heading", { name: "Support agent" })).toBeInTheDocument();
  });

  it("narrows on search by name and by slug", async () => {
    renderList();
    const search = await screen.findByLabelText("Search applications");

    fireEvent.change(search, { target: { value: "support" } });
    expect(screen.queryByRole("link", { name: "Document review" })).not.toBeInTheDocument();

    // The slug is what the SDK sends, so it is what someone is likely to paste.
    fireEvent.change(search, { target: { value: "document-review" } });
    expect(await screen.findByRole("link", { name: "Document review" })).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Support agent" })).not.toBeInTheDocument();
  });

  it("says a search found nothing, rather than showing an empty table", async () => {
    renderList();
    fireEvent.change(await screen.findByLabelText("Search applications"), {
      target: { value: "zzz" },
    });
    expect(screen.getByText(/No applications match/)).toBeInTheDocument();
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
  });

  it("points at Install SDK when there is nothing to show yet", async () => {
    vi.mocked(api.aiApplications).mockResolvedValue({
      applications: [],
      from: "2026-08-10",
      to: "2026-09-09",
    });
    renderList();
    expect(await screen.findByText("No applications yet")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Install SDK" })).toHaveAttribute(
      "href",
      "/install-sdk",
    );
    // No search box either: there is nothing to search.
    expect(screen.queryByLabelText("Search applications")).not.toBeInTheDocument();
  });

  it("reads its window from the URL and refetches when it changes", async () => {
    renderList("/applications?days=7");
    await waitFor(() => expect(api.aiApplications).toHaveBeenCalledWith({ days: 7 }));

    fireEvent.change(screen.getByLabelText("Time window"), { target: { value: "90" } });
    await waitFor(() => expect(api.aiApplications).toHaveBeenCalledWith({ days: 90 }));
  });

  it("falls back to 30 days when the URL asks for a window that is not offered", async () => {
    renderList("/applications?days=999");
    await waitFor(() => expect(api.aiApplications).toHaveBeenCalledWith({ days: 30 }));
  });

  it("carries the window into the detail link, so the drill-down agrees", async () => {
    renderList("/applications?days=7");
    expect(await screen.findByRole("link", { name: "Support agent" })).toHaveAttribute(
      "href",
      "/applications/a1?days=7",
    );
  });

  it("shows a dash, not a zero, when there were no runs to average", async () => {
    // "$0.00 per run" reads as free. Nothing ran.
    vi.mocked(api.aiApplications).mockResolvedValue({
      applications: [app({ runs: 0, cost_per_run: null, error_rate: null, spend_change: null })],
      from: "2026-08-10",
      to: "2026-09-09",
    });
    renderList();
    const row = (await screen.findByRole("link", { name: "Support agent" })).closest("tr")!;
    expect(within(row).getAllByText("—").length).toBeGreaterThanOrEqual(3);
    expect(within(row).queryByText("$0.00")).not.toBeInTheDocument();
  });

  it("calls out stale runs ahead of running ones", async () => {
    // A stuck agent is the thing worth interrupting someone for; "2 running"
    // beside it would bury it.
    vi.mocked(api.aiApplications).mockResolvedValue({
      applications: [app({ active: 2, stale: 1 })],
      from: "2026-08-10",
      to: "2026-09-09",
    });
    renderList();
    expect(await screen.findByText("1 stale")).toBeInTheDocument();
    expect(screen.queryByText("2 running")).not.toBeInTheDocument();
  });
});

describe("Applications — the detail", () => {
  it("leads with a breadcrumb back to the list", async () => {
    renderDetail();
    expect(await screen.findByRole("link", { name: "← All applications" })).toHaveAttribute(
      "href",
      "/applications",
    );
  });

  it("shows the slug verbatim, because it is what goes in METER_APPLICATION", async () => {
    // Rendered in a <code>, not a badge: badges capitalize, and
    // "Support-Agent" is not the slug.
    renderDetail();
    const slug = await screen.findByText("support-agent");
    expect(slug.tagName).toBe("CODE");
    expect(getComputedStyle(slug).textTransform).not.toBe("capitalize");
  });

  it("breaks the spend down by feature, by model and by release", async () => {
    renderDetail();
    expect(await screen.findByRole("heading", { name: "Cost by feature" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Cost by model" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Recent releases" })).toBeInTheDocument();
    expect(screen.getByText("Report generator")).toBeInTheDocument();
    expect(screen.getByText("claude-opus-4-8")).toBeInTheDocument();
    expect(screen.getByText("2026.9.1")).toBeInTheDocument();
  });

  it("computes each row's share of the total it belongs to", async () => {
    renderDetail();
    // Models are a share of the model total (3 + 1.83), not of the run total.
    expect(await screen.findByText("62.1%")).toBeInTheDocument();
    expect(screen.getByText("37.9%")).toBeInTheDocument();
  });

  it("hides the releases section when nothing reported a release", async () => {
    vi.mocked(api.aiApplication).mockResolvedValue(app({ by_feature: [], by_model: [] }));
    renderDetail();
    await screen.findByRole("heading", { name: "Cost by feature" });
    expect(screen.queryByRole("heading", { name: "Recent releases" })).not.toBeInTheDocument();
  });

  it("says a section is empty rather than drawing an empty table", async () => {
    vi.mocked(api.aiApplication).mockResolvedValue(app({ by_feature: [], by_model: [] }));
    renderDetail();
    expect(await screen.findByText("No attributed runs in this period.")).toBeInTheDocument();
    expect(screen.getByText("No model calls in this period.")).toBeInTheDocument();
  });

  it("scopes every number to the window in the URL", async () => {
    renderDetail("/applications/a1?days=90");
    await waitFor(() => expect(api.aiApplication).toHaveBeenCalledWith("a1", 90));

    fireEvent.change(screen.getByLabelText("Time window"), { target: { value: "7" } });
    await waitFor(() => expect(api.aiApplication).toHaveBeenCalledWith("a1", 7));
  });

  it("reports a load failure instead of showing a blank page", async () => {
    vi.mocked(api.aiApplication).mockRejectedValue(new Error("boom"));
    renderDetail();
    expect(await screen.findByRole("alert")).toHaveTextContent(/Could not load this application/);
  });
});
