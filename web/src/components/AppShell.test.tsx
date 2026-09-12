/**
 * The shape of the sidebar.
 *
 * Optimize is two destinations now, and the thing that breaks silently is the
 * active state: "/optimize" is a prefix of "/optimize/prompts", so without an
 * explicit `end` both rows light up at once and the nav stops telling the
 * reader where they are.
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
