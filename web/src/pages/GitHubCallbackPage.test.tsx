import { StrictMode } from "react";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { api, ApiError, type GitHubAppInstallation } from "../api";
import { GitHubAppOption, GITHUB_RETURN_KEY } from "../components/GitHubAppOption";
import { GitHubCallbackPage } from "./GitHubCallbackPage";

vi.mock("../api", async (importActual) => {
  const actual = await importActual<typeof import("../api")>();
  return {
    ...actual,
    api: { githubApp: vi.fn(), githubAppVerify: vi.fn(), githubAppConnect: vi.fn() },
  };
});

const ACME: GitHubAppInstallation = {
  installation_id: 77,
  account: "acme",
  account_type: "Organization",
  repository_selection: "selected",
  claim: "claim-77",
};

function renderCallback(query: string) {
  return render(
    <StrictMode>
      <MemoryRouter initialEntries={[`/github/callback?${query}`]}>
        <Routes>
          <Route path="/github/callback" element={<GitHubCallbackPage />} />
          <Route path="/features" element={<p>Features page</p>} />
          <Route path="/onboarding-here" element={<p>Where I started</p>} />
        </Routes>
      </MemoryRouter>
    </StrictMode>,
  );
}

describe("GitHubCallbackPage", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    sessionStorage.clear();
  });

  it("checks the code once, and connects only when the admin confirms", async () => {
    vi.mocked(api.githubAppVerify).mockResolvedValue({ installations: [ACME] });
    vi.mocked(api.githubAppConnect).mockResolvedValue({ account: "acme", connection: null });
    sessionStorage.setItem(GITHUB_RETURN_KEY, "/onboarding-here");
    renderCallback("code=abc&installation_id=77&setup_action=install");

    const button = await screen.findByRole("button", { name: "Connect acme" });
    // StrictMode runs effects twice; the code is single-use, so it is sent once.
    expect(api.githubAppVerify).toHaveBeenCalledTimes(1);
    expect(api.githubAppVerify).toHaveBeenCalledWith("abc", 77);
    expect(screen.getByText(/selected repositories/)).toBeInTheDocument();
    // Nothing is stored by arriving here — a crafted link must not connect anything.
    expect(api.githubAppConnect).not.toHaveBeenCalled();

    fireEvent.click(button);
    await screen.findByText("Where I started");
    expect(api.githubAppConnect).toHaveBeenCalledWith("claim-77");
  });

  it("lets the admin choose when they can see several organizations", async () => {
    vi.mocked(api.githubAppVerify).mockResolvedValue({
      installations: [ACME, { ...ACME, installation_id: 78, account: "acme-labs", claim: "c78" }],
    });
    vi.mocked(api.githubAppConnect).mockResolvedValue({ account: "acme-labs", connection: null });
    renderCallback("code=abc");
    expect(await screen.findByText(/Which GitHub organization/)).toBeInTheDocument();
    expect(api.githubAppVerify).toHaveBeenCalledWith("abc", undefined);
    fireEvent.click(screen.getByRole("button", { name: "Connect acme-labs" }));
    // No saved starting point: back to Features.
    await screen.findByText("Features page");
    expect(api.githubAppConnect).toHaveBeenCalledWith("c78");
  });

  it("never returns anywhere but a page inside Meter", async () => {
    vi.mocked(api.githubAppVerify).mockResolvedValue({ installations: [ACME] });
    vi.mocked(api.githubAppConnect).mockResolvedValue({ account: "acme", connection: null });
    sessionStorage.setItem(GITHUB_RETURN_KEY, "//evil.example/steal");
    renderCallback("code=abc");
    fireEvent.click(await screen.findByRole("button", { name: "Connect acme" }));
    await screen.findByText("Features page");
  });

  it("says what the server refused, and stores nothing", async () => {
    vi.mocked(api.githubAppVerify).mockRejectedValue(
      new ApiError(400, "Your GitHub account can't see that installation of Meter."),
    );
    renderCallback("code=abc&installation_id=99");
    expect(await screen.findByRole("alert")).toHaveTextContent("can't see that installation");
    expect(screen.queryByRole("button", { name: /Connect/ })).toBeNull();
  });

  it("explains a request waiting for an owner's approval", async () => {
    renderCallback("setup_action=request");
    expect(
      await screen.findByText(/sent your request to your organization's owners/),
    ).toBeVisible();
    expect(api.githubAppVerify).not.toHaveBeenCalled();
  });

  it("says so when GitHub sent no sign-in back", async () => {
    renderCallback("installation_id=77&setup_action=install");
    expect(await screen.findByRole("alert")).toHaveTextContent("didn't send a sign-in");
    expect(api.githubAppVerify).not.toHaveBeenCalled();
  });
});

describe("GitHubAppOption", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    sessionStorage.clear();
  });

  it("is absent where the deployment has not set it up", async () => {
    vi.mocked(api.githubApp).mockResolvedValue({
      configured: false,
      install_url: null,
      connection: null,
    });
    const { container } = render(<GitHubAppOption connected={false} />);
    await waitFor(() => expect(api.githubApp).toHaveBeenCalled());
    expect(container).toBeEmptyDOMElement();
  });

  it("sends the admin to GitHub, remembering where they started", async () => {
    vi.mocked(api.githubApp).mockResolvedValue({
      configured: true,
      install_url: "https://github.com/apps/meter/installations/new",
      connection: null,
    });
    const assign = vi.fn();
    vi.stubGlobal("location", { ...window.location, assign, pathname: "/features" });
    try {
      render(<GitHubAppOption connected={false} />);
      fireEvent.click(await screen.findByRole("button", { name: "Connect with GitHub" }));
      expect(assign).toHaveBeenCalledWith("https://github.com/apps/meter/installations/new");
      expect(sessionStorage.getItem(GITHUB_RETURN_KEY)).toBe("/features");
      // The token form is still the other way in.
      expect(screen.getByText("or paste a personal access token")).toBeInTheDocument();
    } finally {
      vi.unstubAllGlobals();
    }
  });

  it("shows which organization is connected and where to change its repositories", async () => {
    vi.mocked(api.githubApp).mockResolvedValue({
      configured: true,
      install_url: "https://github.com/apps/meter/installations/new",
      connection: {
        installation_id: 77,
        account: "acme",
        manage_url: "https://github.com/apps/meter/installations/77",
      },
    });
    render(<GitHubAppOption connected />);
    expect(await screen.findByText("acme")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /Change repositories on GitHub/ })).toHaveAttribute(
      "href",
      "https://github.com/apps/meter/installations/77",
    );
  });
});
