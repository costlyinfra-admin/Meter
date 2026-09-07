import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { api } from "../api";
import { InstallSdkPage } from "./InstallSdkPage";

vi.mock("../api", async (importActual) => {
  const actual = await importActual<typeof import("../api")>();
  return {
    ...actual,
    api: { createHookToken: vi.fn(), listFeatures: vi.fn() },
  };
});

const renderPage = () =>
  render(
    <MemoryRouter>
      <InstallSdkPage />
    </MemoryRouter>,
  );

describe("InstallSdkPage", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.listFeatures).mockResolvedValue([]);
  });

  it("renders the install instructions and the snippet", async () => {
    renderPage();
    expect(screen.getByRole("heading", { name: "Install SDK" })).toBeInTheDocument();
    // Published on PyPI and npm.
    expect(screen.getByText(/python3 -m pip install "costlyinfra-meter/)).toBeInTheDocument();
    expect(screen.getByText(/npm install costlyinfra-meter/)).toBeInTheDocument();
    // Both required env vars are documented (the URL was previously missing).
    expect(screen.getAllByText(/METER_INGEST_URL=/).length).toBeGreaterThan(0);
    await waitFor(() => expect(api.listFeatures).toHaveBeenCalled());
  });

  it("warns about the two ways a plain install goes wrong", async () => {
    // Both are things a real user hits: PEP 668 outside a virtualenv, and
    // require() against an ESM-only package.
    renderPage();
    expect(screen.getByText(/externally-managed-environment/)).toBeInTheDocument();
    expect(screen.getByText(/ESM only/)).toBeInTheDocument();
    await waitFor(() => expect(api.listFeatures).toHaveBeenCalled());
  });

  it("generates an ingest token on demand", async () => {
    vi.mocked(api.createHookToken).mockResolvedValue({ token: "ingest_abc123" });
    renderPage();
    fireEvent.click(screen.getByRole("button", { name: "Generate ingest token" }));
    await waitFor(() => expect(api.createHookToken).toHaveBeenCalled());
    expect(await screen.findByText(/ingest_abc123/)).toBeInTheDocument();
  });

  it("offers a prompt naming this workspace's real features", async () => {
    vi.mocked(api.listFeatures).mockResolvedValue([
      { id: "feat-123", name: "AI threat triage" } as never,
    ]);
    renderPage();

    expect(await screen.findByText(/feat-123\s+AI threat triage/)).toBeInTheDocument();
    // Confirmed features only: a proposed one has no id worth metering against.
    expect(api.listFeatures).toHaveBeenCalledWith("confirmed");
  });

  it("copies the prompt to the clipboard", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", { value: { writeText }, configurable: true });
    renderPage();

    fireEvent.click(screen.getByRole("button", { name: /copy prompt/i }));
    await waitFor(() => expect(writeText).toHaveBeenCalled());
    expect(writeText.mock.calls[0][0]).toContain("Add Meter metering to this codebase.");
    expect(await screen.findByRole("button", { name: /copied/i })).toBeInTheDocument();
  });

  it("says so when there is nothing to attribute to yet", async () => {
    renderPage();
    expect(await screen.findByText(/Confirm your features first/)).toBeInTheDocument();
  });
});
