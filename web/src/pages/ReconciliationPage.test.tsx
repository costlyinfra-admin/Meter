import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { api, ApiError } from "../api";
import { ReconciliationPage } from "./ReconciliationPage";

vi.mock("../api", async (importActual) => {
  const actual = await importActual<typeof import("../api")>();
  return {
    ...actual,
    api: {
      reconSettings: vi.fn(),
      saveReconSettings: vi.fn(),
      reconPreview: vi.fn(),
      reconImport: vi.fn(),
      reconImports: vi.fn(),
      removeReconImport: vi.fn(),
      reconRuns: vi.fn(),
      reconRun: vi.fn(),
      runReconciliation: vi.fn(),
      reconReportUrl: (id: string) => `/api/reconciliation/runs/${id}/report.csv`,
    },
  };
});

const ENABLED = {
  available: true,
  enabled: true,
  tolerance_abs: 1,
  tolerance_pct: 0.5,
  providers: ["anthropic", "openai"],
};

function renderPage(path = "/reconciliation") {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <Routes>
        <Route path="/reconciliation" element={<ReconciliationPage />} />
        <Route path="/reconciliation/:view" element={<ReconciliationPage />} />
        <Route path="/reconciliation/runs/:runId" element={<ReconciliationPage />} />
      </Routes>
    </MemoryRouter>,
  );
}

const RUN = {
  id: "r1",
  import_id: "i1",
  provider: "anthropic",
  provider_account: "ws1",
  period_start: "2026-05-01",
  period_end: "2026-05-31",
  currency: "USD",
  status: "discrepancy" as const,
  tolerance_abs: 1,
  tolerance_pct: 0.5,
  provider_usage: 283,
  provider_credits: -10,
  provider_tax: 22.64,
  provider_fees: 3,
  provider_total: 298.64,
  tracked_usage: 100,
  usage_difference: 183,
  usage_difference_pct: 64.66,
  unmatched_provider_count: 1,
  unmatched_tracked_count: 0,
  created_by: "cfo@acme.com",
  created_at: "2026-06-01T09:00:00",
  completed_at: "2026-06-01T09:00:02",
  failure_reason: null,
};

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(api.reconSettings).mockResolvedValue(ENABLED);
  vi.mocked(api.reconRuns).mockResolvedValue([]);
  vi.mocked(api.reconImports).mockResolvedValue([]);
  // Importing routes straight to the new run's detail, which loads it.
  vi.mocked(api.reconRun).mockResolvedValue(RUN);
});

describe("Reconciliation — disabled", () => {
  it("offers to turn the module on and shows none of it until then", async () => {
    vi.mocked(api.reconSettings).mockResolvedValue({ ...ENABLED, enabled: false });
    renderPage();

    expect(
      await screen.findByRole("button", { name: "Enable reconciliation" }),
    ).toBeInTheDocument();
    expect(screen.queryByRole("tab", { name: /Import a statement/ })).not.toBeInTheDocument();
    // Nothing is fetched for a module that is off.
    expect(api.reconRuns).not.toHaveBeenCalled();
  });

  it("says so when the installation itself has it turned off", async () => {
    vi.mocked(api.reconSettings).mockResolvedValue({
      ...ENABLED,
      enabled: false,
      available: false,
    });
    renderPage();

    expect(await screen.findByText(/disabled for this installation/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Enable reconciliation" })).toBeDisabled();
  });

  it("enables it on request", async () => {
    vi.mocked(api.reconSettings).mockResolvedValue({ ...ENABLED, enabled: false });
    vi.mocked(api.saveReconSettings).mockResolvedValue(ENABLED);
    renderPage();

    fireEvent.click(await screen.findByRole("button", { name: "Enable reconciliation" }));
    await waitFor(() => expect(api.saveReconSettings).toHaveBeenCalledWith({ enabled: true }));
    expect(await screen.findByRole("tab", { name: /Import a statement/ })).toBeInTheDocument();
  });
});

describe("Reconciliation — summary", () => {
  it("says what to do when nothing has been reconciled", async () => {
    renderPage();
    expect(await screen.findByText("No statement reconciled yet")).toBeInTheDocument();
  });

  it("lists each run with its usage comparison and status", async () => {
    vi.mocked(api.reconRuns).mockResolvedValue([RUN]);
    renderPage();

    const row = (await screen.findByText(/2026-05-01/)).closest("tr")!;
    expect(within(row).getByText("$283")).toBeInTheDocument(); // provider usage
    expect(within(row).getByText("$100")).toBeInTheDocument(); // tracked
    expect(within(row).getByText("$183")).toBeInTheDocument(); // difference
    expect(within(row).getByText("64.66%")).toBeInTheDocument();
    expect(within(row).getByText("Discrepancy")).toBeInTheDocument();
  });

  it("survives a summary that will not load", async () => {
    vi.mocked(api.reconRuns).mockRejectedValue(new ApiError(500, "boom"));
    renderPage();
    expect(await screen.findByText(/Could not load reconciliation runs/)).toBeInTheDocument();
  });
});

describe("Reconciliation — import", () => {
  const PREVIEW = {
    headers: ["Date", "Model", "Cost"],
    suggested_mapping: { service_date: "Date", model: "Model", usage_subtotal: "Cost" },
    mapping: { service_date: "Date", model: "Model", usage_subtotal: "Cost" },
    missing_required: [],
    field_help: { service_date: "Date the usage was incurred", usage_subtotal: "Usage charge" },
    row_count: 3,
    accepted_count: 2,
    rejected_count: 1,
    currencies: ["USD"],
    period_start: "2026-05-01",
    period_end: "2026-05-31",
    usage_subtotal: 283,
    credits: -10,
    tax: 22.64,
    fees: 3,
    billed_total: 298.64,
    rows: [
      {
        row_number: 1,
        service_date: "2026-05-01",
        provider_account: "",
        api_key_ref: "",
        model: "claude-sonnet-4-6",
        usage_category: "usage",
        quantity: 1000,
        usage_subtotal: 283,
        credit: 0,
        tax: 0,
        fee: 0,
        adjustment: 0,
        billed_amount: 283,
        currency: "USD",
        status: "ok",
        errors: [],
      },
    ],
    rejected_rows: [
      {
        row_number: 3,
        service_date: null,
        provider_account: "",
        api_key_ref: "",
        model: "",
        usage_category: "",
        quantity: null,
        usage_subtotal: 0,
        credit: 0,
        tax: 0,
        fee: 0,
        adjustment: 0,
        billed_amount: 0,
        currency: "USD",
        status: "rejected",
        errors: ["No usable date"],
      },
    ],
    checksum: "abc",
  };

  function uploadFile(text = "Date,Model,Cost\n2026-05-01,c,283\n") {
    const input = screen.getByLabelText(/Billing export/i);
    const file = new File([text], "may.csv", { type: "text/csv" });
    // jsdom's File has no .text() in this environment; the component awaits it.
    Object.defineProperty(file, "text", { value: () => Promise.resolve(text) });
    fireEvent.change(input, { target: { files: [file] } });
  }

  it("previews the file before anything is stored", async () => {
    vi.mocked(api.reconPreview).mockResolvedValue(PREVIEW);
    renderPage("/reconciliation/import");
    await screen.findByText("Import a billing statement");

    uploadFile();
    await waitFor(() => expect(api.reconPreview).toHaveBeenCalled());

    // The categories are shown apart, which is the whole point.
    expect(await screen.findByText("Usage subtotal")).toBeInTheDocument();
    expect(screen.getAllByText("Tax").length).toBeGreaterThan(0);
    expect(screen.getByText("Credits")).toBeInTheDocument();
    // And nothing has been imported yet.
    expect(api.reconImport).not.toHaveBeenCalled();
  });

  it("shows the rows it cannot read rather than dropping them quietly", async () => {
    vi.mocked(api.reconPreview).mockResolvedValue(PREVIEW);
    renderPage("/reconciliation/import");
    await screen.findByText("Import a billing statement");
    uploadFile();

    expect(await screen.findByText(/Row 3: No usable date/)).toBeInTheDocument();
  });

  it("lets a column be remapped, and re-previews with the change", async () => {
    vi.mocked(api.reconPreview).mockResolvedValue(PREVIEW);
    renderPage("/reconciliation/import");
    await screen.findByText("Import a billing statement");
    uploadFile();
    await screen.findByText("Columns");

    const selects = screen.getAllByRole("combobox");
    const dateSelect = selects.find((s) => (s as HTMLSelectElement).value === "Date")!;
    fireEvent.change(dateSelect, { target: { value: "Model" } });

    await waitFor(() => expect(api.reconPreview).toHaveBeenCalledTimes(2));
    expect(vi.mocked(api.reconPreview).mock.calls[1][1]).toMatchObject({ service_date: "Model" });
  });

  it("refuses to import while a required column is unmapped", async () => {
    vi.mocked(api.reconPreview).mockResolvedValue({
      ...PREVIEW,
      missing_required: ["usage_subtotal"],
    });
    renderPage("/reconciliation/import");
    await screen.findByText("Import a billing statement");
    uploadFile();

    expect(await screen.findByText(/Map usage_subtotal before importing/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Import and reconcile" })).toBeDisabled();
  });

  it("imports and reconciles in one step", async () => {
    vi.mocked(api.reconPreview).mockResolvedValue(PREVIEW);
    vi.mocked(api.reconImport).mockResolvedValue({ id: "i1" } as never);
    vi.mocked(api.runReconciliation).mockResolvedValue(RUN);
    renderPage("/reconciliation/import");
    await screen.findByText("Import a billing statement");
    uploadFile();
    await screen.findByRole("button", { name: "Import and reconcile" });

    fireEvent.click(screen.getByRole("button", { name: "Import and reconcile" }));
    await waitFor(() => expect(api.runReconciliation).toHaveBeenCalledWith("i1"));
  });

  it("reports a failed import instead of pretending it worked", async () => {
    vi.mocked(api.reconPreview).mockResolvedValue(PREVIEW);
    vi.mocked(api.reconImport).mockRejectedValue(
      new ApiError(400, "That file is not a statement."),
    );
    renderPage("/reconciliation/import");
    await screen.findByText("Import a billing statement");
    uploadFile();
    await screen.findByRole("button", { name: "Import and reconcile" });

    fireEvent.click(screen.getByRole("button", { name: "Import and reconcile" }));
    expect(await screen.findByText("That file is not a statement.")).toBeInTheDocument();
  });

  it("reports an unreadable file", async () => {
    vi.mocked(api.reconPreview).mockRejectedValue(new ApiError(400, "The file is empty."));
    renderPage("/reconciliation/import");
    await screen.findByText("Import a billing statement");
    uploadFile("");
    expect(await screen.findByText("The file is empty.")).toBeInTheDocument();
  });
});

describe("Reconciliation — detail", () => {
  const DETAIL = {
    ...RUN,
    matches: [
      {
        strategy: "account_date_model",
        dimensions: { day: "2026-05-01" },
        provider_amount: 100,
        tracked_amount: 100,
        difference: 0,
        difference_pct: 0,
        classification: "matched",
        explanation: "They agree exactly.",
        confidence: "confirmed" as const,
        evidence: ["statement row 1: 100"],
      },
      {
        strategy: "unmatched_provider",
        dimensions: { day: "2026-05-02" },
        provider_amount: 183,
        tracked_amount: 0,
        difference: 183,
        difference_pct: null,
        classification: "provider_usage_missing_from_meter",
        explanation: "The provider billed this and Meter has no record of it.",
        confidence: "possible" as const,
        evidence: ["no tracked row for this day"],
      },
      {
        strategy: "unmatched_provider",
        dimensions: { row: 3 },
        provider_amount: 22.64,
        tracked_amount: 0,
        difference: 0,
        difference_pct: null,
        classification: "provider_tax",
        explanation: "A tax line, excluded from usage.",
        confidence: "confirmed" as const,
        evidence: ["row 3 category 'tax' = 22.64"],
      },
    ],
    breakdown: { by_model: [{ key: "claude-sonnet-4-6", usage: 283, lines: 2 }] },
  };

  it("keeps the financial categories apart and says why", async () => {
    vi.mocked(api.reconRun).mockResolvedValue(DETAIL);
    renderPage("/reconciliation/runs/r1");

    expect(await screen.findByText("Provider usage subtotal")).toBeInTheDocument();
    // "Tax" is both a figure and a classification here — both are wanted.
    expect(screen.getAllByText("Tax").length).toBeGreaterThan(0);
    expect(screen.getByText("Credits and discounts")).toBeInTheDocument();
    expect(screen.getByText("Provider invoice total")).toBeInTheDocument();
    expect(screen.getByText(/Tax, credits and fees are shown because/)).toBeInTheDocument();
  });

  it("marks an inferred cause as possible, not confirmed", async () => {
    vi.mocked(api.reconRun).mockResolvedValue(DETAIL);
    renderPage("/reconciliation/runs/r1");

    const row = (await screen.findByText(/no record of it/)).closest("tr")!;
    expect(within(row).getByText("Possible")).toBeInTheDocument();
    // And shows what it is based on.
    expect(within(row).getByText("no tracked row for this day")).toBeInTheDocument();
  });

  it("filters the comparisons", async () => {
    vi.mocked(api.reconRun).mockResolvedValue(DETAIL);
    renderPage("/reconciliation/runs/r1");
    await screen.findByText("They agree exactly.");

    fireEvent.change(screen.getByLabelText("Show"), { target: { value: "differences" } });
    expect(screen.queryByText("They agree exactly.")).not.toBeInTheDocument();
    expect(screen.getByText(/no record of it/)).toBeInTheDocument();
  });

  it("offers the report export for this run", async () => {
    vi.mocked(api.reconRun).mockResolvedValue(DETAIL);
    renderPage("/reconciliation/runs/r1");

    const link = await screen.findByRole("link", { name: "Export report" });
    expect(link).toHaveAttribute("href", "/api/reconciliation/runs/r1/report.csv");
  });

  it("shows a failed calculation as a state, with its reason", async () => {
    vi.mocked(api.reconRun).mockResolvedValue({
      ...DETAIL,
      status: "failed" as const,
      failure_reason: "The import has no usable dated rows.",
    });
    renderPage("/reconciliation/runs/r1");

    expect(await screen.findByText(/This run failed/)).toBeInTheDocument();
    expect(screen.getByText(/no usable dated rows/)).toBeInTheDocument();
  });

  it("says so when the run is not this organization's", async () => {
    vi.mocked(api.reconRun).mockRejectedValue(new ApiError(404, "No such run"));
    renderPage("/reconciliation/runs/someone-elses");
    expect(await screen.findByText(/not available/)).toBeInTheDocument();
  });
});

describe("Reconciliation — import history", () => {
  const IMPORTED = {
    id: "i1",
    provider: "anthropic",
    provider_account: null,
    filename: "may.csv",
    checksum: "abc",
    status: "committed" as const,
    currency: "USD",
    period_start: "2026-05-01",
    period_end: "2026-05-31",
    imported_by: "cfo@acme.com",
    imported_at: "2026-06-01T09:00:00",
    row_count: 42,
    rejected_count: 1,
    validation_errors: [],
    removed_at: null,
    run_count: 2,
  };

  it("lists what was imported, by whom, and how many runs came from it", async () => {
    vi.mocked(api.reconImports).mockResolvedValue([IMPORTED]);
    renderPage("/reconciliation/history");

    const row = (await screen.findByText("may.csv")).closest("tr")!;
    expect(within(row).getByText("cfo@acme.com")).toBeInTheDocument();
    expect(within(row).getByText("42")).toBeInTheDocument();
    expect(within(row).getByText("committed")).toBeInTheDocument();
  });

  it("confirms before removing an import", async () => {
    vi.mocked(api.reconImports).mockResolvedValue([IMPORTED]);
    vi.mocked(api.removeReconImport).mockResolvedValue({ ...IMPORTED, status: "removed" });
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);
    renderPage("/reconciliation/history");

    fireEvent.click(await screen.findByRole("button", { name: "Remove" }));
    expect(confirm).toHaveBeenCalled();
    expect(api.removeReconImport).not.toHaveBeenCalled(); // declined, so nothing happened

    confirm.mockReturnValue(true);
    fireEvent.click(screen.getByRole("button", { name: "Remove" }));
    await waitFor(() => expect(api.removeReconImport).toHaveBeenCalledWith("i1"));
    confirm.mockRestore();
  });

  it("shows an empty history plainly", async () => {
    renderPage("/reconciliation/history");
    expect(await screen.findByText("No statement has been imported yet.")).toBeInTheDocument();
  });
});
