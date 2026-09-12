/**
 * The By-Product tab.
 *
 * The thing this screen must not do is blur its two residual rows together.
 * "Unassigned" is spend on features nobody has put in a product — the customer
 * can fix it. "Unattributed" is spend with no feature at all, part of which is
 * the gap between a provider's bill and what the SDK metered: there is no row to
 * move, so it can never belong to a product. One number is an action; the other
 * is a fact.
 */
import { render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { api, type ProductSpend } from "../api";
import { ProductBreakdown } from "./ProductBreakdown";

vi.mock("../api", async (importActual) => {
  const actual = await importActual<typeof import("../api")>();
  return { ...actual, api: { productSpend: vi.fn() } };
});

const SPEND: ProductSpend = {
  start: "2026-05",
  end: "2026-05",
  months: 1,
  products: [
    {
      product_id: "p1",
      name: "Threat Platform",
      build_cost: 635,
      inference_cost: 10219.2,
      feature_count: 5,
      repos: ["acme-security/platform", "acme-security/detections"],
      confidence: "high",
    },
    {
      product_id: "p2",
      name: "Customer Reporting",
      build_cost: 143.62,
      inference_cost: 2260,
      feature_count: 2,
      repos: ["acme-security/reporting"],
      confidence: "med",
    },
  ],
  unassigned: {
    build_cost: 4576.93,
    inference_cost: 7523.2,
    feature_count: 6,
    spanning_count: 2,
  },
  unattributed: { build_cost: 30, inference_cost: 1460 },
  totals: { build_cost: 5385.55, inference_cost: 21462.4 },
};

const renderTab = () =>
  render(
    <MemoryRouter>
      <ProductBreakdown range={{ kind: "this_month" }} />
    </MemoryRouter>,
  );

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(api.productSpend).mockResolvedValue(SPEND);
});

describe("Cost by product", () => {
  it("shows build and inference as separate columns, never one blended number", async () => {
    renderTab();
    const row = (await screen.findByRole("link", { name: "Threat Platform" })).closest("tr")!;
    expect(within(row).getByText("$635")).toBeInTheDocument();
    expect(within(row).getByText("$10,219")).toBeInTheDocument();
    // A blended total would be $10,854 — it must appear nowhere.
    expect(screen.queryByText("$10,854")).not.toBeInTheDocument();
  });

  it("says how many features a product holds, and which repositories it is built in", async () => {
    renderTab();
    const row = (await screen.findByRole("link", { name: "Threat Platform" })).closest("tr")!;
    expect(within(row).getByText("5")).toBeInTheDocument();
    expect(row.textContent).toContain("acme-security/platform, acme-security/detections");
  });

  it("keeps Unassigned and Unattributed apart, with different explanations", async () => {
    renderTab();
    const unassigned = (await screen.findByText("Unassigned")).closest("tr")!;
    const unattributed = screen.getByText("Unattributed").closest("tr")!;
    expect(unassigned).not.toBe(unattributed);

    // Assignable: says what it is and offers somewhere to go.
    expect(within(unassigned).getByText("$4,577")).toBeInTheDocument();
    expect(
      within(unassigned).getByRole("link", { name: "features with no product" }),
    ).toBeVisible();
    expect(unassigned.textContent).toContain("2 span more than one");

    // Not assignable, and worded exactly as the Overview words it.
    expect(within(unattributed).getByText("$30.00")).toBeInTheDocument();
    expect(unattributed.textContent).toContain("spend not yet mapped to a feature");
  });

  it("explains that a provider's unmetered spend can never belong to a product", async () => {
    renderTab();
    await screen.findByText("Cost by product");
    expect(
      screen.getByText(/difference between a provider's bill and what your SDK metered/),
    ).toBeInTheDocument();
  });

  it("says what a product is when there are none yet", async () => {
    vi.mocked(api.productSpend).mockResolvedValue({ ...SPEND, products: [] });
    renderTab();
    expect(await screen.findByText("No products yet")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Set up products" })).toHaveAttribute(
      "href",
      "/products",
    );
  });

  it("says so when the breakdown cannot be loaded", async () => {
    vi.mocked(api.productSpend).mockRejectedValue(new Error("nope"));
    renderTab();
    await waitFor(() =>
      expect(screen.getByText("Couldn't load product spend.")).toBeInTheDocument(),
    );
  });
});
