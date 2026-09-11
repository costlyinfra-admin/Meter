import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { type ReactNode, useState } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { api, type ConnectorStatus } from "../api";
import { ConnectorRow } from "./ConnectorRow";

vi.mock("../api", async (importActual) => {
  const actual = await importActual<typeof import("../api")>();
  return {
    ...actual,
    api: {
      saveCredential: vi.fn(),
      connectorCredentials: vi.fn(),
      deleteCredential: vi.fn(),
    },
  };
});

// A minimal controlled harness: ConnectorRow's expand is parent-driven (accordion).
function Harness({
  overrides = {},
  onSync,
  detail,
}: {
  overrides?: Partial<ConnectorStatus>;
  onSync?: () => Promise<string>;
  detail?: ReactNode;
}) {
  const [open, setOpen] = useState(false);
  return (
    <ul>
      <ConnectorRow
        connector={{
          type: "anthropic",
          name: "Anthropic",
          category: "inference",
          connected: false,
          ...overrides,
        }}
        onConnected={vi.fn()}
        onSync={onSync}
        detail={detail}
        expanded={open}
        onToggle={() => setOpen((v) => !v)}
      />
    </ul>
  );
}

function row(overrides: Partial<ConnectorStatus> = {}) {
  return <Harness overrides={overrides} />;
}

describe("ConnectorRow", () => {
  beforeEach(() => vi.clearAllMocks());

  it("hides the instructions until Connect is pressed", () => {
    render(row());
    expect(screen.queryByText(/organization Cost & Usage report/)).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Connect" }));
    // Source-specific guide + a setup link appear underneath the row.
    expect(screen.getByText(/organization Cost & Usage report/)).toBeInTheDocument();
    expect(screen.getByText(/create an Admin API key/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /Open provider setup page/ })).toHaveAttribute(
      "href",
      "https://console.anthropic.com/settings/admin-keys",
    );
  });

  it("saves the pasted credential and collapses", async () => {
    vi.mocked(api.saveCredential).mockResolvedValue(undefined);
    render(row());
    fireEvent.click(screen.getByRole("button", { name: "Connect" }));
    fireEvent.change(screen.getByLabelText("Anthropic token"), {
      target: { value: "sk-ant-admin-xyz" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() =>
      expect(api.saveCredential).toHaveBeenCalledWith(
        "anthropic",
        "sk-ant-admin-xyz",
        undefined,
        undefined,
      ),
    );
  });

  it("uses a JSON textarea for Bedrock", () => {
    render(row({ type: "bedrock", name: "Amazon Bedrock (AWS cost)" }));
    fireEvent.click(screen.getByRole("button", { name: "Connect" }));
    const field = screen.getByLabelText("Amazon Bedrock (AWS cost) credentials");
    expect(field.tagName).toBe("TEXTAREA");
    expect(field).toHaveAttribute("placeholder", expect.stringContaining("access_key_id"));
  });

  it("shows Sync now on a connected row and reports the result", async () => {
    const onSync = vi.fn().mockResolvedValue("Pulled $42 of spend.");
    render(<Harness overrides={{ connected: true }} onSync={onSync} />);
    fireEvent.click(screen.getByRole("button", { name: "Sync now" }));
    await waitFor(() => expect(onSync).toHaveBeenCalled());
    expect(await screen.findByText("Pulled $42 of spend.")).toBeInTheDocument();
  });

  it("expands a connected row's detail inline via Configure", () => {
    render(<Harness overrides={{ connected: true }} detail={<p>INLINE DETAIL</p>} />);
    expect(screen.queryByText("INLINE DETAIL")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /Configure/ }));
    expect(screen.getByText("INLINE DETAIL")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /Close/ }));
    expect(screen.queryByText("INLINE DETAIL")).not.toBeInTheDocument();
  });
});

describe("ConnectorRow — several accounts under one connector", () => {
  const ACCOUNTS = [
    {
      id: "c1",
      label: "Acme",
      created_at: "2026-08-01T00:00:00Z",
      updated_at: "2026-08-01T00:00:00Z",
    },
    {
      id: "c2",
      label: "Acme Labs",
      created_at: "2026-09-01T00:00:00Z",
      updated_at: "2026-09-01T00:00:00Z",
    },
  ];

  const openConfigure = async (accounts = ACCOUNTS) => {
    vi.mocked(api.connectorCredentials).mockResolvedValue({ credentials: accounts });
    render(
      <Harness
        overrides={{
          connected: true,
          credential_count: accounts.length,
          supports_multiple: true,
        }}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: /Configure/ }));
    return screen.findByRole("heading", { name: "Accounts" });
  };

  it("lists every stored account, and no secret", async () => {
    // Two Anthropic organisations billed separately are one connector with two
    // keys. Showing one would hide half the bill's source.
    await openConfigure();
    expect(await screen.findByText("Acme")).toBeInTheDocument();
    expect(screen.getByText("Acme Labs")).toBeInTheDocument();
    const panel = document.querySelector(".connector-rotate") as HTMLElement;
    expect(panel.textContent).not.toMatch(/sk-ant|•{3,}|\*{3,}/);
  });

  it("says the accounts are summed, so nobody assumes one wins", async () => {
    await openConfigure();
    expect(await screen.findByText(/2 accounts .* summed/)).toBeInTheDocument();
  });

  it("adds another account without touching the existing ones", async () => {
    vi.mocked(api.saveCredential).mockResolvedValue(undefined as never);
    await openConfigure();
    await screen.findByText("Acme");

    fireEvent.change(screen.getByLabelText("Account name"), { target: { value: "Third org" } });
    fireEvent.change(screen.getByLabelText("Anthropic token"), { target: { value: "sk-ant-3" } });
    fireEvent.click(screen.getByRole("button", { name: /Add token/ }));

    // No credential id: this is an addition, not a replacement.
    await waitFor(() =>
      expect(api.saveCredential).toHaveBeenCalledWith(
        "anthropic",
        "sk-ant-3",
        "Third org",
        undefined,
      ),
    );
  });

  it("replaces one named account, leaving the other alone", async () => {
    vi.mocked(api.saveCredential).mockResolvedValue(undefined as never);
    await openConfigure();
    fireEvent.click(await screen.findByRole("button", { name: "Replace Acme Labs" }));

    // The panel switches to replacing that one, and asks for no new name.
    expect(await screen.findByRole("heading", { name: /Replace token/ })).toBeInTheDocument();
    expect(screen.queryByLabelText("Account name")).not.toBeInTheDocument();

    fireEvent.change(screen.getByLabelText("Anthropic token"), { target: { value: "sk-ant-new" } });
    fireEvent.click(screen.getByRole("button", { name: "Replace" }));

    await waitFor(() =>
      expect(api.saveCredential).toHaveBeenCalledWith("anthropic", "sk-ant-new", undefined, "c2"),
    );
  });

  it("removes one account", async () => {
    vi.mocked(api.deleteCredential).mockResolvedValue(undefined as never);
    await openConfigure();
    fireEvent.click(await screen.findByRole("button", { name: "Remove Acme" }));
    await waitFor(() => expect(api.deleteCredential).toHaveBeenCalledWith("anthropic", "c1"));
  });

  it("will not remove the last account", async () => {
    // That is disconnecting, which discards more than a key — and doing it from
    // a Remove link would be a surprise.
    await openConfigure([ACCOUNTS[0]]);
    expect(await screen.findByRole("button", { name: "Remove Acme" })).toBeDisabled();
  });

  it("never renders a stored credential in the field", async () => {
    await openConfigure();
    const field = screen.getByLabelText("Anthropic token") as HTMLInputElement;
    expect(field.value).toBe("");
    expect(field.type).toBe("password");
  });

  it("puts the accounts panel above the provider's detail", async () => {
    // The detail is a provider's own list of workspaces and keys. On a busy
    // connector it pushed the credential controls off the bottom of the screen.
    vi.mocked(api.connectorCredentials).mockResolvedValue({ credentials: ACCOUNTS });
    render(
      <Harness
        overrides={{ connected: true, supports_multiple: true }}
        detail={<p>Workspace and API key breakdown</p>}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: /Configure/ }));
    await screen.findByRole("heading", { name: "Accounts" });

    const panels = [...document.querySelectorAll(".connector-panel")];
    expect(panels[0]).toHaveClass("connector-rotate");
    expect(panels[1]).toHaveTextContent("Workspace and API key breakdown");
  });

  it("offers replace, not add, where a second key would never be read", async () => {
    // GitHub's sync reads one token. Offering "Add another" would take a key
    // and quietly never fetch with it.
    vi.mocked(api.connectorCredentials).mockResolvedValue({ credentials: [ACCOUNTS[0]] });
    render(
      <Harness
        overrides={{ type: "github", name: "GitHub", connected: true, supports_multiple: false }}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: /Configure/ }));

    expect(await screen.findByRole("heading", { name: /Replace token/ })).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Accounts" })).not.toBeInTheDocument();
    expect(screen.queryByLabelText("Account name")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Replace" })).toBeInTheDocument();
  });

  it("still lets a connector be set up when it has no accounts yet", async () => {
    render(<Harness />);
    fireEvent.click(screen.getByRole("button", { name: /Connect/ }));
    expect(await screen.findByLabelText("Anthropic token")).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Accounts" })).not.toBeInTheDocument();
  });
});
