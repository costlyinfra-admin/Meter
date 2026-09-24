/**
 * Settings → Coding agents.
 *
 * The properties that matter here are about a credential and about consent: the
 * token is shown once and never recoverable, revoking is possible and informed,
 * and someone reading the panel learns where their data goes before they decide
 * to send it there.
 */
import { fireEvent, render as renderBare, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { McpTokensCard } from "./McpTokensCard";
import { api, type McpActivity, type McpToken } from "../api";

vi.mock("../api", async () => {
  const actual = await vi.importActual<typeof import("../api")>("../api");
  return {
    ...actual,
    api: {
      mcpTokens: vi.fn(),
      createMcpToken: vi.fn(),
      revokeMcpToken: vi.fn(),
      mcpActivity: vi.fn(),
    },
  };
});

/** The card links into the handbook, so it needs a router around it. */
const render = (ui: React.ReactElement) => renderBare(<MemoryRouter>{ui}</MemoryRouter>);

const token = (over: Partial<McpToken> = {}): McpToken => ({
  id: "tok-1",
  label: "Office laptop",
  created_at: "2026-09-20T10:00:00Z",
  last_used_at: "2026-09-23T10:00:00Z",
  revoked_at: null,
  active: true,
  recent_calls: 14,
  ...over,
});

const activity = (over: Partial<McpActivity> = {}): McpActivity => ({
  at: "2026-09-23T10:00:00Z",
  tool: "get_cost_summary",
  arguments: '{"group_by": "feature"}',
  outcome: "ok",
  duration_ms: 12,
  transport: "http",
  token_label: "Office laptop",
  ...over,
});

beforeEach(() => {
  vi.mocked(api.mcpTokens).mockResolvedValue([]);
  vi.mocked(api.mcpActivity).mockResolvedValue([]);
  vi.mocked(api.revokeMcpToken).mockResolvedValue(undefined);
});

describe("before anything is connected", () => {
  it("says what an agent can and cannot do, and where the data goes", async () => {
    render(<McpTokensCard />);
    await screen.findByText(/No tokens yet/);

    // Read-only, no content, and — the part Meter's own promise does not cover —
    // that the numbers leave for whatever model the agent runs on.
    expect(screen.getByText(/read-only/i)).toBeInTheDocument();
    expect(screen.getByText(/never receives prompts or responses/i)).toBeInTheDocument();
    expect(screen.getByText(/sent to whichever model that agent runs on/i)).toBeInTheDocument();
    // ...and the long version, in the handbook the public docs also publish.
    expect(screen.getByRole("link", { name: /coding agents/i })).toHaveAttribute(
      "href",
      "/help/trust/coding-agents",
    );
  });

  it("does not offer a connection command until there is something to connect with", async () => {
    render(<McpTokensCard />);
    await screen.findByText(/No tokens yet/);
    expect(screen.queryByText(/claude mcp add/)).not.toBeInTheDocument();
  });
});

describe("creating a token", () => {
  it("shows it once, and says that is the only time", async () => {
    vi.mocked(api.createMcpToken).mockResolvedValue({
      id: "tok-1",
      label: "Office laptop",
      created_at: "2026-09-23T10:00:00Z",
      token: "mtr_mcp_secret-value",
    });
    vi.mocked(api.mcpTokens).mockResolvedValue([token()]);

    render(<McpTokensCard />);
    await screen.findByText(/No tokens yet|Office laptop/);
    fireEvent.change(screen.getByLabelText(/Add a token/), { target: { value: "Office laptop" } });
    fireEvent.click(screen.getByRole("button", { name: /Create token/ }));

    expect(await screen.findByText("mtr_mcp_secret-value")).toBeInTheDocument();
    expect(screen.getByText(/only time it is shown/i)).toBeInTheDocument();
  });

  it("stops showing it once dismissed, because it cannot be shown again", async () => {
    vi.mocked(api.createMcpToken).mockResolvedValue({
      id: "tok-1",
      label: "laptop",
      created_at: "2026-09-23T10:00:00Z",
      token: "mtr_mcp_secret-value",
    });
    vi.mocked(api.mcpTokens).mockResolvedValue([token()]);

    render(<McpTokensCard />);
    fireEvent.change(screen.getByLabelText(/Add a token/), { target: { value: "laptop" } });
    fireEvent.click(screen.getByRole("button", { name: /Create token/ }));
    await screen.findByText("mtr_mcp_secret-value");

    fireEvent.click(screen.getByRole("button", { name: "Done" }));
    expect(screen.queryByText("mtr_mcp_secret-value")).not.toBeInTheDocument();
  });

  it("will not create an unlabelled token", async () => {
    render(<McpTokensCard />);
    await screen.findByText(/No tokens yet/);
    // One per machine only helps if you can tell the machines apart.
    expect(screen.getByRole("button", { name: /Create token/ })).toBeDisabled();
  });

  it("reports a failure instead of pretending it worked", async () => {
    const { ApiError } = await vi.importActual<typeof import("../api")>("../api");
    vi.mocked(api.createMcpToken).mockRejectedValue(new ApiError(400, "Label is too long."));
    render(<McpTokensCard />);
    fireEvent.change(screen.getByLabelText(/Add a token/), { target: { value: "x" } });
    fireEvent.click(screen.getByRole("button", { name: /Create token/ }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Label is too long.");
  });
});

describe("living with tokens", () => {
  it("shows what you need in order to decide whether to revoke one", async () => {
    vi.mocked(api.mcpTokens).mockResolvedValue([token()]);
    render(<McpTokensCard />);

    const row = (await screen.findByText("Office laptop")).closest("tr")!;
    // "Is anything still using this?" is the question, every time.
    expect(within(row).getByText("14")).toBeInTheDocument();
    expect(within(row).getByText("Active")).toBeInTheDocument();
  });

  it("revokes one and reloads", async () => {
    vi.mocked(api.mcpTokens)
      .mockResolvedValueOnce([token()])
      .mockResolvedValue([token({ active: false, revoked_at: "2026-09-23T11:00:00Z" })]);

    render(<McpTokensCard />);
    fireEvent.click(await screen.findByRole("button", { name: "Revoke" }));

    expect(api.revokeMcpToken).toHaveBeenCalledWith("tok-1");
    await waitFor(() => expect(screen.getByText(/Revoked/)).toBeInTheDocument());
    expect(screen.queryByRole("button", { name: "Revoke" })).not.toBeInTheDocument();
  });

  it("offers a connection command once a token exists", async () => {
    vi.mocked(api.mcpTokens).mockResolvedValue([token()]);
    render(<McpTokensCard appOrigin="https://meter.costlyinfra.com" />);
    const command = await screen.findByText(/claude mcp add/);
    expect(command).toHaveTextContent("https://meter.costlyinfra.com/api/mcp");
    // Never the token itself: this snippet is meant to be copied and pasted
    // into a shell, and a real credential in it would end up in history.
    expect(command).toHaveTextContent("<your token>");
  });

  it("does not offer a command when every token is revoked", async () => {
    vi.mocked(api.mcpTokens).mockResolvedValue([token({ active: false })]);
    render(<McpTokensCard />);
    await screen.findByText("Office laptop");
    expect(screen.queryByText(/claude mcp add/)).not.toBeInTheDocument();
  });
});

describe("the activity trail", () => {
  it("lists what an agent asked for", async () => {
    vi.mocked(api.mcpTokens).mockResolvedValue([token()]);
    vi.mocked(api.mcpActivity).mockResolvedValue([activity()]);
    render(<McpTokensCard />);

    const entry = await screen.findByText(/get_cost_summary/);
    expect(entry).toHaveTextContent('{"group_by": "feature"}');
    expect(entry).toHaveTextContent("Office laptop");
  });

  it("marks a call that failed", async () => {
    vi.mocked(api.mcpTokens).mockResolvedValue([token()]);
    vi.mocked(api.mcpActivity).mockResolvedValue([activity({ outcome: "error" })]);
    render(<McpTokensCard />);
    expect(await screen.findByText(/failed/)).toBeInTheDocument();
  });

  it("says what the trail records, and what it does not, when it is empty", async () => {
    vi.mocked(api.mcpTokens).mockResolvedValue([token()]);
    render(<McpTokensCard />);
    expect(
      await screen.findByText(/what it asked for, never what it was told/i),
    ).toBeInTheDocument();
  });
});
