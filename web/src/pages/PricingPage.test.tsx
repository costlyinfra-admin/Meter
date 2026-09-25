import { fireEvent, render, screen, waitFor } from "@testing-library/react";
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
  providers: [
    {
      provider: "anthropic",
      label: "Anthropic",
      source_url: "https://claude.com/pricing",
      checked: "2026-09-25",
      models: [
        {
          model: "claude-sonnet-4-6",
          input_per_million: "3",
          output_per_million: "15",
          cache_read_per_million: "0.30",
          cache_write_per_million: "3.75",
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
      models: [
        {
          model: "meta-llama-3.1-70b-instruct",
          input_per_million: "0.88",
          output_per_million: "0.88",
          cache_read_per_million: null,
          cache_write_per_million: null,
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
    await screen.findByText("meta-llama-3.1-70b-instruct");
    const row = screen.getByText("meta-llama-3.1-70b-instruct").closest("tr");
    expect(row).not.toBeNull();
    // Four cache columns, all unpriced for this host: read, write, minimum
    // and mode. "$0" would read as "caching is free here", which is a claim
    // Meter is not making — it does not price a cache for this host at all.
    const cells = [...row!.querySelectorAll("td")].map((c) => c.textContent);
    expect(cells.slice(2)).toEqual(["—", "—", "—", "—"]);
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
    expect(screen.getByText("meta-llama-3.1-70b-instruct")).toBeInTheDocument();
    // ...and a provider with nothing left drops out rather than sitting empty.
    expect(screen.queryByText("Anthropic")).not.toBeInTheDocument();
  });

  it("reports a failure instead of showing an empty table", async () => {
    vi.mocked(api.pricing).mockRejectedValue(new Error("nope"));
    renderPage();
    expect(await screen.findByRole("alert")).toHaveTextContent(/Could not load pricing/);
  });
});
