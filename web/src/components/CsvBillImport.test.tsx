import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { api, type InfraImportReport, type InfraProvider } from "../api";
import { CsvBillImport } from "./CsvBillImport";

vi.mock("../api", async (importActual) => {
  const actual = await importActual<typeof import("../api")>();
  return { ...actual, api: { importInfrastructureCsv: vi.fn() } };
});

const PROVIDER: InfraProvider = {
  type: "redis_cloud",
  name: "Redis Cloud",
  short: "Redis",
  status: "available",
  ingest: "csv",
  note: "Import the cost report you download from Redis Cloud.",
  connected: false,
  last_sync: null,
  config: null,
};

const CSV = "Subscription,Usage Date,Cost (USD)\nprod,2026-05-01,142.50\n";

function report(over: Partial<InfraImportReport> = {}): InfraImportReport {
  return {
    provider: "redis_cloud",
    dry_run: true,
    rows_read: 3,
    rows_imported: 2,
    rows_skipped: 1,
    mapping: { date: "Usage Date", amount: "Cost (USD)", tag: "Subscription" },
    unmapped_columns: [],
    warnings: ["1 row skipped: zero amount"],
    total: 230.75,
    currency: "USD",
    from: "2026-05-01",
    to: "2026-05-02",
    ...over,
  };
}

/** Selecting a file, the way the browser hands it over. */
async function choose(text = CSV) {
  const input = screen.getByLabelText(/bill CSV/i) as HTMLInputElement;
  const file = new File([text], "bill.csv", { type: "text/csv" });
  // jsdom's File has no .text() in some versions; make it deterministic.
  Object.defineProperty(file, "text", { value: () => Promise.resolve(text) });
  fireEvent.change(input, { target: { files: [file] } });
  return input;
}

describe("CsvBillImport", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.importInfrastructureCsv).mockResolvedValue(report());
  });

  it("previews the file without importing anything", async () => {
    render(<CsvBillImport provider={PROVIDER} onImported={() => {}} />);
    await choose();

    await waitFor(() =>
      expect(api.importInfrastructureCsv).toHaveBeenCalledWith("redis_cloud", CSV, {
        dryRun: true,
        mapping: undefined,
        costColumn: undefined,
      }),
    );
    // The preview says so in as many words: a number on screen that has already
    // been written is a different thing from one that has not.
    expect(await screen.findByText(/Nothing has been imported yet/)).toBeInTheDocument();
    expect(screen.getByText("$231")).toBeInTheDocument();
  });

  it("shows which column it read each value from", async () => {
    render(<CsvBillImport provider={PROVIDER} onImported={() => {}} />);
    await choose();

    expect(await screen.findByLabelText("Column for Amount")).toHaveValue("Cost (USD)");
    expect(screen.getByLabelText("Column for Date")).toHaveValue("Usage Date");
    expect(screen.getByLabelText("Column for Feature tag")).toHaveValue("Subscription");
  });

  it("lets the customer correct a column we matched wrongly", async () => {
    render(<CsvBillImport provider={PROVIDER} onImported={() => {}} />);
    await choose("Date,List Price,Charge\n2026-05-01,99.00,42.00\n");
    await screen.findByLabelText("Column for Amount");

    fireEvent.change(screen.getByLabelText("Column for Amount"), {
      target: { value: "List Price" },
    });

    // Correcting re-previews rather than importing — still nothing written.
    await waitFor(() =>
      expect(api.importInfrastructureCsv).toHaveBeenLastCalledWith(
        "redis_cloud",
        expect.any(String),
        expect.objectContaining({
          dryRun: true,
          mapping: expect.objectContaining({ amount: "List Price" }),
        }),
      ),
    );
  });

  it("surfaces skipped rows rather than quietly importing fewer", async () => {
    render(<CsvBillImport provider={PROVIDER} onImported={() => {}} />);
    await choose();
    expect(await screen.findByText("1 row skipped: zero amount")).toBeInTheDocument();
  });

  it("imports only when the customer commits, and says what landed", async () => {
    const onImported = vi.fn();
    render(<CsvBillImport provider={PROVIDER} onImported={onImported} />);
    await choose();
    const button = await screen.findByRole("button", { name: /Import 2 line items/ });

    vi.mocked(api.importInfrastructureCsv).mockResolvedValueOnce(
      report({ dry_run: false, items: 2, infrastructure: 230.75 }),
    );
    fireEvent.click(button);

    await waitFor(() => expect(onImported).toHaveBeenCalled());
    expect(await screen.findByText(/Imported 2 line items/)).toBeInTheDocument();
    expect(screen.getByText(/2026-05-01 to 2026-05-02/)).toBeInTheDocument();
    // The last call was the real one, not a preview.
    const calls = vi.mocked(api.importInfrastructureCsv).mock.calls;
    expect(calls[calls.length - 1][2]).toMatchObject({ mapping: expect.any(Object) });
  });

  it("explains an unreadable file instead of failing silently", async () => {
    const { ApiError } = await vi.importActual<typeof import("../api")>("../api");
    vi.mocked(api.importInfrastructureCsv).mockRejectedValue(
      new ApiError(400, "Could not identify an amount column. The file has: Date, Notes."),
    );
    render(<CsvBillImport provider={PROVIDER} onImported={() => {}} />);
    await choose("Date,Notes\n2026-05-01,hi\n");

    expect(await screen.findByRole("alert")).toHaveTextContent(/an amount column/);
    expect(screen.queryByRole("button", { name: /^Import/ })).not.toBeInTheDocument();
  });

  it("says why this provider needs a file at all", async () => {
    render(<CsvBillImport provider={PROVIDER} onImported={() => {}} />);
    // Otherwise it reads as a worse connector rather than a deliberate choice.
    expect(screen.getByText(/no cost API to read it from/)).toBeInTheDocument();
    expect(screen.getByText(/rather than estimate your spend/i)).toBeInTheDocument();
  });

  it("cancels back to the empty state", async () => {
    render(<CsvBillImport provider={PROVIDER} onImported={() => {}} />);
    await choose();
    fireEvent.click(await screen.findByRole("button", { name: "Cancel" }));

    expect(screen.queryByText(/Nothing has been imported yet/)).not.toBeInTheDocument();
    expect(screen.getByText(/Choose the downloaded bill/)).toBeInTheDocument();
  });
});

describe("a FOCUS export", () => {
  const FOCUS_PROVIDER: InfraProvider = { ...PROVIDER, type: "focus", name: "FOCUS export" };

  const focusReport = (over = {}) =>
    report({
      provider: "focus",
      focus: {
        cost_column: "BilledCost",
        cost_columns_available: ["BilledCost", "EffectiveCost"],
        by_category: { Usage: 100, Tax: 14.8, Credit: -25 },
        corrections: 0,
        extensions: [],
        ...over,
      },
    });

  beforeEach(() => {
    vi.mocked(api.importInfrastructureCsv).mockResolvedValue(focusReport());
  });

  it("says it recognised the format rather than guessed at it", async () => {
    render(<CsvBillImport provider={FOCUS_PROVIDER} onImported={() => {}} />);
    await choose();
    expect(await screen.findByText(/FinOps FOCUS/)).toBeInTheDocument();
  });

  it("shows what the charge categories total, so tax is not read as service cost", async () => {
    render(<CsvBillImport provider={FOCUS_PROVIDER} onImported={() => {}} />);
    await choose();

    await screen.findByText("Tax");
    expect(screen.getByText("Usage")).toBeInTheDocument();
    expect(screen.getByText("Credit")).toBeInTheDocument();
    // A credit is negative money; showing it as a positive would be worse than
    // not showing it at all.
    expect(screen.getByText(/-\$25/)).toBeInTheDocument();
  });

  it("lets a customer read the amortized cost instead, and re-previews", async () => {
    render(<CsvBillImport provider={FOCUS_PROVIDER} onImported={() => {}} />);
    await choose();

    const select = await screen.findByLabelText(/Which FOCUS cost to read/i);
    vi.mocked(api.importInfrastructureCsv).mockResolvedValue(
      focusReport({ cost_column: "EffectiveCost" }),
    );
    fireEvent.change(select, { target: { value: "EffectiveCost" } });

    await waitFor(() =>
      expect(api.importInfrastructureCsv).toHaveBeenLastCalledWith("focus", CSV, {
        dryRun: true,
        mapping: expect.anything(),
        costColumn: "EffectiveCost",
      }),
    );
  });

  it("imports with the cost column the customer chose, not the default", async () => {
    render(<CsvBillImport provider={FOCUS_PROVIDER} onImported={() => {}} />);
    await choose();
    const select = await screen.findByLabelText(/Which FOCUS cost to read/i);
    vi.mocked(api.importInfrastructureCsv).mockResolvedValue(
      focusReport({ cost_column: "EffectiveCost" }),
    );
    fireEvent.change(select, { target: { value: "EffectiveCost" } });
    // Wait for the re-preview to land, not merely to start. While one is in
    // flight the button reads "Working…" and is disabled, so both finding it
    // by name and clicking it would be racing the request.
    fireEvent.click(await screen.findByRole("button", { name: /Import 2 line items/ }));
    await waitFor(() =>
      expect(api.importInfrastructureCsv).toHaveBeenLastCalledWith("focus", CSV, {
        mapping: expect.anything(),
        costColumn: "EffectiveCost",
      }),
    );
  });

  it("offers no choice when the file carries only one cost", async () => {
    vi.mocked(api.importInfrastructureCsv).mockResolvedValue(
      focusReport({ cost_columns_available: ["BilledCost"] }),
    );
    render(<CsvBillImport provider={FOCUS_PROVIDER} onImported={() => {}} />);
    await choose();
    expect(await screen.findByLabelText(/Which FOCUS cost to read/i)).toBeDisabled();
  });

  it("stays out of the way for a bill that is not FOCUS", async () => {
    vi.mocked(api.importInfrastructureCsv).mockResolvedValue(report());
    render(<CsvBillImport provider={PROVIDER} onImported={() => {}} />);
    await choose();
    await screen.findByText(/Nothing has been imported yet/);
    expect(screen.queryByText(/FinOps FOCUS/)).not.toBeInTheDocument();
  });
});
