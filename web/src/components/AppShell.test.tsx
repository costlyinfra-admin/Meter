/**
 * The shape of the sidebar.
 *
 * Two things here break quietly rather than loudly. The active state:
 * "/optimize" is a prefix of "/optimize/prompts", so without an explicit `end`
 * both rows light up at once and the nav stops telling the reader where they
 * are. And the grouping: Reconciliation is inserted at runtime into a section
 * found BY NAME, so renaming a section silently drops it out of the nav.
 */
import { render, screen, within } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { api } from "../api";
import { AuthProvider } from "../auth/AuthContext";
import { AppShell } from "./AppShell";

vi.mock("../api", async (importActual) => {
  const actual = await importActual<typeof import("../api")>();
  return {
    ...actual,
    api: {
      me: vi.fn(),
      logout: vi.fn(),
      reconSettings: vi.fn(),
      alertsSummary: vi.fn(),
    },
  };
});

/** The shell with stub pages: this is about the nav, not the screens. */
const renderAt = (path: string) =>
  render(
    <MemoryRouter initialEntries={[path]}>
      <AuthProvider>
        <Routes>
          <Route element={<AppShell />}>
            <Route path="/optimize" element={<p>recommendations</p>} />
            <Route path="/optimize/prompts" element={<p>prompts</p>} />
          </Route>
        </Routes>
      </AuthProvider>
    </MemoryRouter>,
  );

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(api.me).mockResolvedValue({ id: "u1", tenant_id: "t1", email: "cto@acme.com" });
  vi.mocked(api.reconSettings).mockResolvedValue({
    available: false,
    enabled: false,
    tolerance_abs: 1,
    tolerance_pct: 0.5,
  });
  // The shell asks for the alert badge on mount.
  vi.mocked(api.alertsSummary).mockResolvedValue({
    triggered: 0,
    healthy: 0,
    delivery_errors: 0,
    disabled: 0,
    unread: 0,
  });
});

describe("Sidebar nav", () => {
  it("is grouped the way the product is used", async () => {
    renderAt("/optimize");
    await screen.findByRole("link", { name: "Recommendations" });

    const groups = [...document.querySelectorAll(".nav-group")].map((g) => ({
      section: g.querySelector(".nav-group-label")?.textContent ?? null,
      items: [...g.querySelectorAll("a")].map((a) => a.textContent?.trim()),
    }));

    // Written in sentence case and rendered uppercase by CSS.
    expect(groups).toEqual([
      { section: null, items: ["Overview"] },
      { section: "Analyze", items: ["Applications", "Products", "Features", "Traces"] },
      { section: "Optimize", items: ["Recommendations", "Prompts"] },
      { section: "Monitor", items: ["Alerts"] },
      { section: "Setup", items: ["Connect sources", "Install SDK", "Settings"] },
      { section: "Help", items: ["Knowledge base"] },
    ]);
  });

  it("puts connecting a provider under Setup, because you do it once", async () => {
    renderAt("/optimize");
    const link = await screen.findByRole("link", { name: "Connect sources" });
    // The label changed; the route did not, so no bookmark or link breaks.
    expect(link).toHaveAttribute("href", "/cost-sources");
    const group = link.closest(".nav-group") as HTMLElement;
    expect(within(group).getByText("Setup")).toBeInTheDocument();
  });

  it("puts Reconciliation under Monitor once the module is on", async () => {
    vi.mocked(api.reconSettings).mockResolvedValue({
      available: true,
      enabled: true,
      tolerance_abs: 1,
      tolerance_pct: 0.5,
    });
    renderAt("/optimize");

    // Inserted at runtime into the section named "Monitor". If that section is
    // ever renamed without updating the insertion, the item vanishes and only
    // a test like this one notices.
    const recon = await screen.findByRole("link", { name: "Reconciliation" });
    expect(recon).toHaveAttribute("href", "/reconciliation");
    const group = recon.closest(".nav-group") as HTMLElement;
    expect(within(group).getByText("Monitor")).toBeInTheDocument();
    expect(within(group).getByRole("link", { name: "Alerts" })).toBeInTheDocument();
  });

  it("leaves Reconciliation out while the module is off", async () => {
    renderAt("/optimize");
    await screen.findByRole("link", { name: "Recommendations" });
    expect(screen.queryByRole("link", { name: "Reconciliation" })).not.toBeInTheDocument();
  });

  it("offers Recommendations and Prompts together under Optimize", async () => {
    renderAt("/optimize");
    const rec = await screen.findByRole("link", { name: "Recommendations" });
    expect(rec).toHaveAttribute("href", "/optimize");

    // Both options live under the one heading, not scattered through the nav.
    const group = rec.closest(".nav-group") as HTMLElement;
    expect(within(group).getByText("Optimize")).toBeInTheDocument();
    expect(within(group).getByRole("link", { name: "Prompts" })).toHaveAttribute(
      "href",
      "/optimize/prompts",
    );
  });

  it("highlights the row you are on", async () => {
    renderAt("/optimize");
    const rec = await screen.findByRole("link", { name: "Recommendations" });
    expect(rec.className).toContain("active");
    expect(screen.getByRole("link", { name: "Prompts" }).className).not.toContain("active");
  });

  it("does not leave Recommendations lit while you are on Prompts", async () => {
    renderAt("/optimize/prompts");
    const prompts = await screen.findByRole("link", { name: "Prompts" });
    expect(prompts.className).toContain("active");
    expect(screen.getByRole("link", { name: "Recommendations" }).className).not.toContain("active");
  });
});
