import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { api, type PriceBook } from "../api";
import { PricingPage } from "./PricingPage";

vi.mock("../api", async (importActual) => {
  const actual = await importActual<typeof import("../api")>();
  return { ...actual, api: { pricing: vi.fn() } };
});

const BOOK: PriceBook = {
  version: "2026-09-25",
  families: [
    {
      family: "llama-3.1-70b-instruct",
      label: "Llama 3.1 70B Instruct",
      vendor: "Meta",
      hosts: [
        {
          provider: "deepinfra",
          label: "DeepInfra",
          model: "meta-llama-3.1-70b-instruct",
          input_per_million: "0.35",
          output_per_million: "0.40",
        },
        {
          provider: "together",
          label: "Together AI",
          model: "meta-llama-3.1-70b-instruct",
          input_per_million: "0.88",
          output_per_million: "0.88",
        },
      ],
    },
  ],
  providers: [
    {
      provider: "anthropic",
      label: "Anthropic",
      source_url: "https://claude.com/pricing",
      checked: "2026-09-25",
      vendors: null,
      models: [
        {
          model: "claude-sonnet-4-6",
          input_per_million: "3",
          output_per_million: "15",
          cache_read_per_million: "0.30",
          input_batch_per_million: "1.50",
          output_batch_per_million: "7.50",
          cache_write_5m_per_million: "3.75",
          cache_write_1h_per_million: "6.00",
          cache_read_mult: "0.10",
          min_cacheable_tokens: 1024,
          cache_is_automatic: false,
          downgrade_target: "claude-haiku-4-5",
          open_weights_family: null,
        },
      ],
    },
    {
      provider: "together",
      label: "Together AI",
      source_url: "https://www.together.ai/pricing",
      checked: null,
      vendors: null,
      models: [
        {
          model: "meta-llama-3.1-70b-instruct",
          input_per_million: "0.88",
          output_per_million: "0.88",
          cache_read_per_million: null,
          input_batch_per_million: null,
          output_batch_per_million: null,
          cache_write_5m_per_million: null,
          cache_write_1h_per_million: null,
          cache_read_mult: null,
          min_cacheable_tokens: null,
          cache_is_automatic: null,
          downgrade_target: null,
          open_weights_family: "Llama 3.1 70B Instruct",
        },
      ],
    },
  ],
};

function renderPage() {
  return render(
    <MemoryRouter>
      <PricingPage />
    </MemoryRouter>,
  );
}

describe("Provider pricing", () => {
  beforeEach(() => {
    vi.mocked(api.pricing).mockReset();
    vi.mocked(api.pricing).mockResolvedValue(BOOK);
  });

  it("shows each provider's rates, and where they came from", async () => {
    renderPage();
    expect(await screen.findByText("Anthropic")).toBeInTheDocument();
    expect(screen.getByText("claude-sonnet-4-6")).toBeInTheDocument();
    expect(screen.getByText("$3")).toBeInTheDocument();
    expect(screen.getByText("$15")).toBeInTheDocument();
    // The version and the count are the reader's handle on how current it is.
    expect(screen.getByText("2026-09-25")).toBeInTheDocument();
    expect(screen.getAllByRole("link", { name: "source" })).toHaveLength(2);
  });

  it("says when a table was last checked, and when it was not", async () => {
    renderPage();
    expect(
      await screen.findByText(/Checked against the published table on 2026-09-25/),
    ).toBeInTheDocument();
    // An unchecked table says so rather than quietly borrowing the credibility
    // of the ones above it.
    expect(screen.getByText(/Not reconciled against the provider/)).toBeInTheDocument();
  });

  it("writes a missing cache rate as a dash, never as zero", async () => {
    renderPage();
    // Scoped to the provider's own table by its heading: the same model also
    // appears in the open-weights family section below, with fewer columns.
    const heading = await screen.findByRole("heading", { name: "Together AI", level: 2 });
    const section = heading.closest("section");
    const row = within(section!).getByText("meta-llama-3.1-70b-instruct").closest("tr");
    expect(row).not.toBeNull();
    // Everything this host does not offer: both batch columns, both cache
    // writes, the read, the minimum and the mode. "$0" would read as "batch
    // and caching are free here", which is a claim Meter is not making.
    const cells = [...row!.querySelectorAll("td")].map((c) => c.textContent);
    expect(cells.slice(2)).toEqual(["—", "—", "—", "—", "—", "—", "—"]);
    // The rates it DOES have are still there, so this is not an empty row.
    expect(cells.slice(0, 2)).toEqual(["$0.88", "$0.88"]);
  });

  it("shows the cache write as dearer than the input it replaces", async () => {
    renderPage();
    await screen.findByText("claude-sonnet-4-6");
    // $3 to send it, $3.75 to cache it. This is the number that decides
    // whether a caching recommendation is a saving at all.
    expect(screen.getByText("$3.75")).toBeInTheDocument();
    expect(screen.getByText("1,024")).toBeInTheDocument();
  });

  it("filters to the models a reader asked for", async () => {
    renderPage();
    await screen.findByText("claude-sonnet-4-6");
    fireEvent.change(screen.getByLabelText("Filter models"), { target: { value: "llama" } });
    await waitFor(() => expect(screen.queryByText("claude-sonnet-4-6")).not.toBeInTheDocument());
    // The model appears in its host's table and again under its family, so
    // this is getAll: one match would mean the family section had vanished.
    expect(screen.getAllByText("meta-llama-3.1-70b-instruct").length).toBeGreaterThan(0);
    // ...and a provider with nothing left drops out rather than sitting empty.
    expect(screen.queryByText("Anthropic")).not.toBeInTheDocument();
  });

  it("shows batch rates beside standard ones where a batch API exists", async () => {
    renderPage();
    await screen.findByText("claude-sonnet-4-6");
    const row = screen.getByText("claude-sonnet-4-6").closest("tr");
    const cells = [...row!.querySelectorAll("td")].map((c) => c.textContent);
    // input, output, then the same two at the batch rate.
    expect(cells.slice(0, 4)).toEqual(["$3", "$15", "$1.5", "$7.5"]);
  });

  it("shows both cache write tiers, because a caller picks between them", async () => {
    renderPage();
    await screen.findByText("claude-sonnet-4-6");
    const row = screen.getByText("claude-sonnet-4-6").closest("tr");
    const cells = [...row!.querySelectorAll("td")].map((c) => c.textContent);
    // read, then 5-minute, then 1-hour: $0.30, $3.75, $6. An hour costs nearly
    // twice what five minutes does, which is the decision the column exists for.
    expect(cells.slice(4, 7)).toEqual(["$0.3", "$3.75", "$6"]);
  });

  it("lists open weights by who serves them, cheapest first", async () => {
    // Meta sells no inference of its own, so Llama cannot be a provider row.
    // It is a family, and the spread between its hosts is the point.
    renderPage();
    const heading = await screen.findByRole("heading", {
      name: "Open weights, by who serves them",
    });
    const section = heading.closest("section");
    expect(within(section!).getByText(/Llama 3.1 70B Instruct/)).toBeInTheDocument();
    expect(within(section!).getByText(/Meta/)).toBeInTheDocument();
    const hosts = [...section!.querySelectorAll("tbody tr")].map(
      (r) => r.querySelector("th")?.textContent,
    );
    expect(hosts).toEqual(["DeepInfra", "Together AI"]);
  });

  it("puts the providers a reader came for at the top", async () => {
    renderPage();
    await screen.findByText("Anthropic");
    const headings = screen.getAllByRole("heading", { level: 2 }).map((h) => h.textContent);
    expect(headings[0]).toBe("Anthropic");
  });

  it("reports a failure instead of showing an empty table", async () => {
    vi.mocked(api.pricing).mockRejectedValue(new Error("nope"));
    renderPage();
    expect(await screen.findByRole("alert")).toHaveTextContent(/Could not load pricing/);
  });
});
