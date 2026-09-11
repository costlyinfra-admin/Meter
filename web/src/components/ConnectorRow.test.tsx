import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { type ReactNode, useState } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { api, type ConnectorStatus } from "../api";
import { ConnectorRow } from "./ConnectorRow";

vi.mock("../api", async (importActual) => {
  const actual = await importActual<typeof import("../api")>();
  return { ...actual, api: { saveCredential: vi.fn() } };
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
      expect(api.saveCredential).toHaveBeenCalledWith("anthropic", "sk-ant-admin-xyz"),
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

describe("ConnectorRow — replacing a stored credential", () => {
  const open = async () => {
    fireEvent.click(screen.getByRole("button", { name: /Configure/ }));
    return screen.findByRole("heading", { name: /Replace token/ });
  };

  it("puts replace above the provider's detail, not below it", async () => {
    // The detail is a provider's own list of workspaces, keys or accounts. On a
    // busy connector it is long enough to push the credential form off the
    // bottom of the screen, which is the thing someone opened Configure for.
    render(
      <Harness overrides={{ connected: true }} detail={<p>Workspace and API key breakdown</p>} />,
    );
    fireEvent.click(screen.getByRole("button", { name: /Configure/ }));
    await screen.findByRole("heading", { name: /Replace token/ });

    const panels = [...document.querySelectorAll(".connector-panel")];
    expect(panels[0]).toHaveClass("connector-rotate");
    expect(panels[1]).toHaveTextContent("Workspace and API key breakdown");
  });

  it("offers a replace form on a connected row", async () => {
    // The whole point: before this, a connected connector had no way back to
    // its credential, so a rotated or leaked key could not be changed at all.
    render(<Harness overrides={{ connected: true }} />);
    await open();
    expect(screen.getByLabelText("Anthropic token")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Replace" })).toBeInTheDocument();
  });

  it("never renders the stored credential, masked or otherwise", async () => {
    render(<Harness overrides={{ connected: true, credential_set_at: "2026-08-01T00:00:00Z" }} />);
    const panel = (await open()).closest(".connector-panel") as HTMLElement;

    const field = screen.getByLabelText("Anthropic token") as HTMLInputElement;
    // Empty, not pre-filled with a placeholder row of dots that cannot be
    // edited — there is no route that returns a secret to fill it with.
    expect(field.value).toBe("");
    expect(field.type).toBe("password");
    expect(panel.textContent).not.toMatch(/•{3,}|\*{3,}/);
  });

  it("says when the current credential was set, so rotation is checkable", async () => {
    const sixtyDaysAgo = new Date(Date.now() - 60 * 86_400_000).toISOString();
    render(<Harness overrides={{ connected: true, credential_set_at: sixtyDaysAgo }} />);
    await open();
    expect(screen.getByText(/Current token set 2 months ago/)).toBeInTheDocument();
  });

  it("sends only the new secret, and keeps nothing after saving", async () => {
    vi.mocked(api.saveCredential).mockResolvedValue(undefined as never);
    render(<Harness overrides={{ connected: true }} />);
    await open();

    fireEvent.change(screen.getByLabelText("Anthropic token"), {
      target: { value: "  sk-ant-new  " },
    });
    fireEvent.click(screen.getByRole("button", { name: "Replace" }));

    await waitFor(() => expect(api.saveCredential).toHaveBeenCalledWith("anthropic", "sk-ant-new"));
    // The panel closes and the field is cleared, so a secret is not left sitting
    // in a form for the next person at the keyboard.
    await waitFor(() =>
      expect(screen.queryByRole("heading", { name: /Replace token/ })).not.toBeInTheDocument(),
    );
  });

  it("will not submit an empty replacement", async () => {
    // An accidental Save must never blank out a working connector.
    render(<Harness overrides={{ connected: true }} />);
    await open();
    expect(screen.getByRole("button", { name: "Replace" })).toBeDisabled();

    fireEvent.change(screen.getByLabelText("Anthropic token"), { target: { value: "   " } });
    expect(screen.getByRole("button", { name: "Replace" })).toBeDisabled();
  });

  it("calls a multi-line credential what it is, rather than a token", async () => {
    // Bedrock takes a service-account style blob, not a token. The word is
    // derived from the guide so twenty of them do not have to spell it out.
    render(<Harness overrides={{ type: "bedrock", name: "Amazon Bedrock", connected: true }} />);
    fireEvent.click(screen.getByRole("button", { name: /Configure/ }));
    expect(await screen.findByRole("heading", { name: /Replace credentials/ })).toBeInTheDocument();
    expect(screen.getByLabelText("Amazon Bedrock credentials")).toBeInTheDocument();
  });
});
