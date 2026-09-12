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
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
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
  trend: [
    {
      period: "2026-04-01",
      products: [
        { product_id: "p1", name: "Threat Platform", build_cost: 588, inference_cost: 6830 },
        { product_id: "p2", name: "Customer Reporting", build_cost: 142, inference_cost: 1000 },
      ],
      unassigned: { build_cost: 217, inference_cost: 968 },
      unattributed: { build_cost: 0, inference_cost: 0 },
    },
    {
      period: "2026-05-01",
      products: [
        { product_id: "p1", name: "Threat Platform", build_cost: 703, inference_cost: 10342 },
        { product_id: "p2", name: "Customer Reporting", build_cost: 144, inference_cost: 2260 },
      ],
      unassigned: { build_cost: 4509, inference_cost: 7400 },
      unattributed: { build_cost: 30, inference_cost: 1460 },
    },
  ],
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
    // Scoped to the table: the chart's legend names both residuals as well, and
    // this test is about the two rows under the products.
    const table = await screen.findByRole("table");
    const unassigned = within(table).getByText("Unassigned").closest("tr")!;
    const unattributed = within(table).getByText("Unattributed").closest("tr")!;
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

  it("charts each product month by month, including both residuals", async () => {
    renderTab();
    await screen.findByText("Cost by product");
    const legend = screen.getByLabelText("Product legend");
    expect(within(legend).getByText("Threat Platform")).toBeInTheDocument();
    expect(within(legend).getByText("Unassigned")).toBeInTheDocument();
    // Without the residuals the total would climb as features get assigned, and
    // look like growth that never happened.
    expect(within(legend).getByText("Unattributed")).toBeInTheDocument();
    expect(
      screen.getByRole("img", { name: /Inference cost per product, per month/ }),
    ).toBeVisible();
  });

  it("charts one kind of money at a time, never a blended per-product figure", async () => {
    renderTab();
    await screen.findByText("Cost by product");
    // Inference by default; the axis and label say which.
    expect(screen.getByRole("button", { name: "Inference" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );

    fireEvent.click(screen.getByRole("button", { name: "Build" }));
    expect(
      await screen.findByRole("img", { name: /Build cost per product, per month/ }),
    ).toBeVisible();
    expect(screen.getByRole("button", { name: "Build" })).toHaveAttribute("aria-pressed", "true");
  });

  it("says what a product is when there are none yet", async () => {
    vi.mocked(api.productSpend).mockResolvedValue({ ...SPEND, products: [], trend: [] });
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
