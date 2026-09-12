/**
 * The Products page.
 *
 * Its job is to make mapping 20-odd repositories into 5-6 products quick, and to
 * be honest about the one case it cannot decide: a feature built in repositories
 * belonging to two different products is listed for a person, never guessed.
 */
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { api, type Product } from "../api";
import { ProductsPage } from "./ProductsPage";

vi.mock("../api", async (importActual) => {
  const actual = await importActual<typeof import("../api")>();
  return {
    ...actual,
    api: {
      listProducts: vi.fn(),
      productSuggestions: vi.fn(),
      spanningFeatures: vi.fn(),
      createProduct: vi.fn(),
      deleteProduct: vi.fn(),
      setProductRepos: vi.fn(),
      reassignProducts: vi.fn(),
    },
  };
});

const SENTINEL: Product = {
  id: "p1",
  name: "Sentinel",
  description: "",
  repos: ["acme/sentinel-api"],
  feature_count: 3,
};

const renderPage = () =>
  render(
    <MemoryRouter>
      <ProductsPage />
    </MemoryRouter>,
  );

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(api.listProducts).mockResolvedValue({ products: [SENTINEL] });
  vi.mocked(api.productSuggestions).mockResolvedValue({
    suggestions: [{ name: "Beacon", repos: ["acme/beacon-api", "acme/beacon-web"] }],
    unmapped: ["acme/beacon-api", "acme/beacon-web"],
    mapped_count: 1,
  });
  vi.mocked(api.spanningFeatures).mockResolvedValue({ features: [] });
  vi.mocked(api.reassignProducts).mockResolvedValue({
    assigned: 7,
    unassigned: 2,
    spanning: 1,
  });
});

describe("Products", () => {
  it("lists each product with what it holds", async () => {
    renderPage();
    expect(await screen.findByRole("heading", { name: "Sentinel" })).toBeInTheDocument();
    expect(screen.getByText(/3 features · 1 repository/)).toBeInTheDocument();
  });

  it("adds a product", async () => {
    vi.mocked(api.createProduct).mockResolvedValue({ ...SENTINEL, id: "p2", name: "Beacon" });
    renderPage();
    await screen.findByRole("heading", { name: "Sentinel" });

    fireEvent.change(screen.getByLabelText("New product"), { target: { value: "Beacon" } });
    fireEvent.click(screen.getByRole("button", { name: "Add product" }));
    await waitFor(() => expect(api.createProduct).toHaveBeenCalledWith("Beacon"));
  });

  it("maps repositories to a product and reports what that reassigned", async () => {
    vi.mocked(api.setProductRepos).mockResolvedValue({
      product: { ...SENTINEL, repos: ["acme/sentinel-api", "acme/beacon-api"] },
      reassigned: { assigned: 4, unassigned: 1, spanning: 0 },
    });
    renderPage();
    await screen.findByRole("heading", { name: "Sentinel" });

    fireEvent.click(screen.getByLabelText("acme/beacon-api"));
    fireEvent.click(screen.getByRole("button", { name: "Save repositories" }));

    await waitFor(() =>
      expect(api.setProductRepos).toHaveBeenCalledWith("p1", [
        "acme/sentinel-api",
        "acme/beacon-api",
      ]),
    );
    expect(await screen.findByText(/4 features assigned, 1 left unassigned/)).toBeInTheDocument();
  });

  it("offers products guessed from repository names, and creates one on request", async () => {
    vi.mocked(api.createProduct).mockResolvedValue({ ...SENTINEL, id: "p2", name: "Beacon" });
    vi.mocked(api.setProductRepos).mockResolvedValue({
      product: { ...SENTINEL, id: "p2", name: "Beacon" },
      reassigned: { assigned: 2, unassigned: 0, spanning: 0 },
    });
    renderPage();

    const row = (await screen.findByText("Beacon")).closest("tr")!;
    expect(row.textContent).toContain("acme/beacon-api, acme/beacon-web");
    fireEvent.click(within(row).getByRole("button", { name: "Create" }));

    await waitFor(() => expect(api.createProduct).toHaveBeenCalledWith("Beacon"));
    await waitFor(() =>
      expect(api.setProductRepos).toHaveBeenCalledWith("p2", [
        "acme/beacon-api",
        "acme/beacon-web",
      ]),
    );
  });

  it("re-applies the mapping and says what changed", async () => {
    renderPage();
    await screen.findByRole("heading", { name: "Sentinel" });

    fireEvent.click(screen.getByRole("button", { name: "Re-apply repository mapping" }));
    await waitFor(() => expect(api.reassignProducts).toHaveBeenCalled());
    expect(
      await screen.findByText(/7 features assigned, 2 left unassigned, of which 1 span/),
    ).toBeInTheDocument();
  });

  it("lists features that span two products instead of guessing one", async () => {
    vi.mocked(api.spanningFeatures).mockResolvedValue({
      features: [
        {
          feature_id: "f9",
          name: "Shared auth",
          repos: ["acme/sentinel-api", "acme/beacon-api"],
          products: ["Beacon", "Sentinel"],
        },
      ],
    });
    renderPage();
    expect(
      await screen.findByRole("heading", { name: "Features that span more than one product" }),
    ).toBeInTheDocument();
    const row = screen.getByRole("link", { name: "Shared auth" }).closest("tr")!;
    expect(row.textContent).toContain("Beacon or Sentinel");
    expect(screen.getByText(/a guess here would be a number you could not check/)).toBeVisible();
  });

  it("warns that deleting a product keeps its features", async () => {
    renderPage();
    await screen.findByRole("heading", { name: "Sentinel" });

    fireEvent.click(screen.getByRole("button", { name: "Delete" }));
    expect(screen.getByText(/Its features stay, unassigned/)).toBeInTheDocument();
  });
});
