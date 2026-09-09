import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { api, ApiError, type Feature } from "../api";
import { AuthProvider } from "../auth/AuthContext";
import { InstallSdkPage } from "./InstallSdkPage";

vi.mock("../api", async (importActual) => {
  const actual = await importActual<typeof import("../api")>();
  return {
    ...actual,
    api: {
      me: vi.fn(),
      createHookToken: vi.fn(),
      listFeatures: vi.fn(),
      recentHookEvent: vi.fn(),
      aiApplications: vi.fn(),
    },
  };
});

const renderPage = (route = "/install-sdk") =>
  render(
    <MemoryRouter initialEntries={[route]}>
      <AuthProvider>
        <InstallSdkPage />
      </AuthProvider>
    </MemoryRouter>,
  );

const feature = (id: string, name: string) => ({ id, name }) as Feature;

/** The tab in a named tablist — the page has two, so they must be told apart. */
const tabIn = (listName: string, name: RegExp | string) =>
  within(screen.getByRole("tablist", { name: listName })).getByRole("tab", { name });

/** The prompt inside one guide's panel. Every panel stays mounted — that is how
 *  switching tabs avoids losing state and shifting layout — so a bare text
 *  query would match all three at once. */
const promptFor = (guide: string) =>
  document.getElementById(`guide-panel-${guide}`)!.querySelector(".agent-prompt")!.textContent!;

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(api.me).mockResolvedValue({ id: "u1", tenant_id: "t-123", email: "cto@acme.com" });
  vi.mocked(api.listFeatures).mockResolvedValue([]);
  vi.mocked(api.recentHookEvent).mockResolvedValue({ event: null });
  vi.mocked(api.aiApplications).mockResolvedValue({
    applications: [],
    from: "2026-08-10",
    to: "2026-09-09",
  });
});

describe("InstallSdkPage — structure", () => {
  it("opens on a guide that works, not on the one that does not exist yet", async () => {
    // Setup CLI is unbuilt. Landing there meant the first thing a new customer
    // saw on this page was a "Coming soon" badge.
    renderPage();
    expect(await screen.findByRole("heading", { name: "Install SDK" })).toBeInTheDocument();

    expect(tabIn("Installation method", "Setup with AI")).toHaveAttribute("aria-selected", "true");
    expect(tabIn("Setup assistant", /Claude Code/)).toHaveAttribute("aria-selected", "true");
    expect(screen.getByRole("heading", { name: "Install with Claude Code" })).toBeVisible();
  });

  it("offers the four assistants in order, each with a logo", async () => {
    renderPage();
    const list = screen.getByRole("tablist", { name: "Setup assistant" });
    const tabs = within(list).getAllByRole("tab");
    // Setup CLI last: the three that work come first.
    expect(tabs.map((t) => t.getAttribute("aria-label"))).toEqual([
      "Claude Code",
      "Cursor",
      "Codex",
      "Setup CLI",
    ]);
    // A mark before every label, all rendered by the app's own icon component.
    for (const tab of tabs) expect(tab.querySelector(".connector-mark")).toBeInTheDocument();
  });

  it("wires the tabs to their panels both ways", async () => {
    renderPage();
    const tab = tabIn("Installation method", "Setup with AI");
    const panel = document.getElementById(tab.getAttribute("aria-controls")!)!;
    expect(panel).toHaveAttribute("role", "tabpanel");
    expect(panel.getAttribute("aria-labelledby")).toBe(tab.id);
  });
});

describe("InstallSdkPage — the Setup CLI tab is a preview, not an instruction", () => {
  it("shows the planned command without offering to copy it", async () => {
    // Reached explicitly now: it is the last guide, not the landing one.
    renderPage("/install-sdk?guide=cli");
    await screen.findByRole("heading", { name: "Install Meter automatically" });

    expect(screen.getByText("Coming soon")).toBeInTheDocument();
    expect(screen.getByText("Planned command")).toBeInTheDocument();

    // The package is not published. Showing it is fine; handing someone a copy
    // button for a command that 404s is not.
    const command = screen.getByText("npx @costlyinfra/meter-setup");
    expect(command).toHaveClass("snippet-disabled");
    expect(command.closest(".snippet-wrap")).toBeNull(); // no Snippet, no Copy

    for (const step of ["Detect the project language and package manager", "Send a test event"]) {
      expect(screen.getByText(new RegExp(step))).toBeInTheDocument();
    }
  });
});

describe("InstallSdkPage — keyboard", () => {
  it("moves between assistants with the arrow keys, and wraps", async () => {
    renderPage();
    const list = screen.getByRole("tablist", { name: "Setup assistant" });

    fireEvent.keyDown(list, { key: "ArrowRight" });
    expect(tabIn("Setup assistant", /Cursor/)).toHaveAttribute("aria-selected", "true");

    fireEvent.keyDown(list, { key: "ArrowLeft" });
    expect(tabIn("Setup assistant", /Claude Code/)).toHaveAttribute("aria-selected", "true");

    // Left from the first wraps to the last, as the tab pattern expects.
    fireEvent.keyDown(list, { key: "ArrowLeft" });
    expect(tabIn("Setup assistant", /Setup CLI/)).toHaveAttribute("aria-selected", "true");
  });

  it("jumps to the ends with Home and End", async () => {
    renderPage();
    const list = screen.getByRole("tablist", { name: "Setup assistant" });

    fireEvent.keyDown(list, { key: "End" });
    expect(tabIn("Setup assistant", /Setup CLI/)).toHaveAttribute("aria-selected", "true");
    fireEvent.keyDown(list, { key: "Home" });
    expect(tabIn("Setup assistant", /Claude Code/)).toHaveAttribute("aria-selected", "true");
  });

  it("is one tab stop: only the selected tab is reachable by Tab", async () => {
    renderPage();
    const tabs = within(screen.getByRole("tablist", { name: "Setup assistant" })).getAllByRole(
      "tab",
    );
    expect(tabs.filter((t) => t.getAttribute("tabindex") === "0")).toHaveLength(1);
    expect(tabs[0]).toHaveAttribute("tabindex", "0");
  });
});

describe("InstallSdkPage — the URL carries the tab", () => {
  it("opens the guide named in the query string", async () => {
    renderPage("/install-sdk?tab=ai&guide=cursor");
    expect(await screen.findByRole("heading", { name: "Install with Cursor" })).toBeVisible();
    expect(tabIn("Setup assistant", /Cursor/)).toHaveAttribute("aria-selected", "true");
  });

  it("falls back to the defaults when the query string is nonsense", async () => {
    renderPage("/install-sdk?tab=sideways&guide=emacs");
    expect(tabIn("Installation method", "Setup with AI")).toHaveAttribute("aria-selected", "true");
    expect(tabIn("Setup assistant", /Claude Code/)).toHaveAttribute("aria-selected", "true");
  });

  it("opens the manual route directly", async () => {
    renderPage("/install-sdk?tab=manual");
    expect(await screen.findByRole("heading", { name: "1. Install the package" })).toBeVisible();
  });
});

describe("InstallSdkPage — manual route", () => {
  it("shows one package manager's command at a time", async () => {
    renderPage("/install-sdk?tab=manual");
    await screen.findByRole("heading", { name: "1. Install the package" });

    expect(screen.getByText("npm install costlyinfra-meter")).toBeInTheDocument();
    expect(screen.queryByText("pnpm add costlyinfra-meter")).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "pnpm" }));
    expect(screen.getByText("pnpm add costlyinfra-meter")).toBeInTheDocument();
    expect(screen.queryByText("npm install costlyinfra-meter")).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "bun" }));
    expect(screen.getByText("bun add costlyinfra-meter")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "bun" })).toHaveAttribute("aria-pressed", "true");
  });

  it("keeps the Python instructions and their two known failure modes", async () => {
    renderPage("/install-sdk?tab=manual");
    await screen.findByRole("heading", { name: "1. Install the package" });
    expect(screen.getByText(/python3 -m pip install "costlyinfra-meter/)).toBeInTheDocument();
    expect(screen.getByText(/externally-managed-environment/)).toBeInTheDocument();
    expect(screen.getByText(/ESM only/)).toBeInTheDocument();
  });
});

describe("InstallSdkPage — the token", () => {
  it("mints one on request and marks it for masking in session replay", async () => {
    vi.mocked(api.createHookToken).mockResolvedValue({ token: "hk_live_secret" });
    renderPage();

    fireEvent.click(await screen.findByRole("button", { name: "Generate ingest token" }));
    const shown = await screen.findByText(/METER_INGEST_TOKEN=hk_live_secret/);
    expect(shown.closest("[data-dd-privacy='mask']")).not.toBeNull();
  });

  it("says so when the token cannot be minted", async () => {
    vi.mocked(api.createHookToken).mockRejectedValue(new ApiError(500, "boom"));
    renderPage();
    fireEvent.click(await screen.findByRole("button", { name: "Generate ingest token" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(/Could not generate/);
  });
});

describe("InstallSdkPage — generated prompts carry no secrets", () => {
  it("never puts the ingest token in a prompt, even after minting one", async () => {
    vi.mocked(api.createHookToken).mockResolvedValue({ token: "hk_live_secret" });
    vi.mocked(api.listFeatures).mockResolvedValue([feature("f-1", "AI threat triage")]);
    renderPage("/install-sdk?guide=claude-code");

    fireEvent.click(await screen.findByRole("button", { name: "Generate ingest token" }));
    await screen.findByText(/METER_INGEST_TOKEN=hk_live_secret/);

    const prompt = promptFor("claude-code");
    expect(prompt).not.toContain("hk_live_secret");
    // It tells the agent to ask for the token rather than carrying one.
    expect(prompt).toContain("METER_INGEST_TOKEN=<ask me for this; it is a secret>");
    // The real feature id is there, because a guessed one misattributes money.
    expect(prompt).toContain("f-1");
  });

  it("uses only environment variables the SDK actually reads", async () => {
    vi.mocked(api.listFeatures).mockResolvedValue([feature("f-1", "AI threat triage")]);
    renderPage();
    await screen.findByRole("heading", { name: "Install SDK" });
    for (const guide of ["claude-code", "cursor", "codex"]) {
      const prompt = promptFor(guide);
      expect(prompt).toContain("METER_INGEST_URL");
      expect(prompt).toContain("METER_INGEST_TOKEN");
      // Neither of these exists. METER_TOKEN is not what the SDK reads, and
      // there is no feature-id environment variable at all — feature_id is an
      // argument to wrap()/Meter().
      expect(prompt).not.toMatch(/METER_TOKEN\b/);
      expect(prompt).not.toContain("METER_FEATURE_ID");
    }
  });

  it("gives each assistant its own closing instructions", async () => {
    renderPage();
    await screen.findByRole("heading", { name: "Install SDK" });
    expect(promptFor("cursor")).toMatch(/Show me the changes you propose/);
    expect(promptFor("codex")).toMatch(/report every file you changed/);
    expect(promptFor("claude-code")).toMatch(/Read the repository before you edit it/);
  });
});

describe("InstallSdkPage — verification", () => {
  it("waits, then reports what arrived", async () => {
    vi.mocked(api.recentHookEvent).mockResolvedValue({ event: null });
    renderPage();
    expect(await screen.findByText(/Waiting for your first Meter event/)).toBeInTheDocument();

    vi.mocked(api.recentHookEvent).mockResolvedValue({
      event: {
        feature_id: "f-1",
        feature_name: "AI threat triage",
        provider: "anthropic",
        model: "claude-sonnet-4-6",
        received_at: "2026-05-21T09:00:00Z",
        requests: 3,
      },
    });
    // Polling picks it up without a reload.
    expect(await screen.findByText("Events received", {}, { timeout: 8000 })).toBeInTheDocument();
    expect(screen.getByText("AI threat triage")).toBeInTheDocument();
    expect(screen.getByText("claude-sonnet-4-6")).toBeInTheDocument();
  }, 15000);

  it("marks the wait with an hourglass, not a status light", async () => {
    // A pulsing dot reads as a status light, and a light that is not green
    // reads as a fault — the wrong thing to say to someone whose install is
    // fine and whose app simply has not made a model call yet.
    vi.mocked(api.recentHookEvent).mockResolvedValue({ event: null });
    renderPage();
    await screen.findByText(/Waiting for your first Meter event/);

    const state = screen.getByText(/Waiting for your first Meter event/).closest("p")!;
    expect(state.querySelector(".verify-hourglass")).not.toBeNull();
    expect(state.querySelector(".verify-dot")).toBeNull();
  });

  it("keeps the hourglass out of the accessibility tree", async () => {
    // The sentence beside it already says what is happening; announcing a
    // decorative timer as well would only interrupt it.
    vi.mocked(api.recentHookEvent).mockResolvedValue({ event: null });
    renderPage();
    await screen.findByText(/Waiting for your first Meter event/);
    expect(document.querySelector(".verify-hourglass")).toHaveAttribute("aria-hidden");
  });

  it("drops the hourglass once events arrive", async () => {
    // Nothing is being waited for any more.
    vi.mocked(api.recentHookEvent).mockResolvedValue({
      event: {
        feature_id: "f-1",
        feature_name: "AI threat triage",
        provider: "anthropic",
        model: "claude-sonnet-4-6",
        received_at: "2026-05-21T09:00:00Z",
        requests: 3,
      },
    });
    renderPage();
    await screen.findByText("Events received");
    expect(document.querySelector(".verify-hourglass")).toBeNull();
    expect(document.querySelector(".verify-dot.ok")).not.toBeNull();
  });

  it("names the Unattributed bucket rather than showing a blank feature", async () => {
    vi.mocked(api.recentHookEvent).mockResolvedValue({
      event: {
        feature_id: null,
        feature_name: null,
        provider: "openai",
        model: null,
        received_at: "2026-05-21T09:00:00Z",
        requests: 1,
      },
    });
    renderPage();
    const verify = (await screen.findByText("Events received")).closest("section")!;
    expect(within(verify).getByText("Unattributed")).toBeInTheDocument();
  });

  it("survives the tab switching that happens above it", async () => {
    vi.mocked(api.recentHookEvent).mockResolvedValue({
      event: {
        feature_id: "f-1",
        feature_name: "AI threat triage",
        provider: "anthropic",
        model: "claude-sonnet-4-6",
        received_at: "2026-05-21T09:00:00Z",
        requests: 3,
      },
    });
    renderPage();
    await screen.findByText("Events received");

    fireEvent.click(tabIn("Installation method", "Manual via package manager"));
    // The panel lives outside the tabs, so switching cannot reset it.
    expect(screen.getByText("Events received")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "1. Install the package" })).toBeVisible();
  });

  it("stops polling on an expired session instead of hammering a 401", async () => {
    vi.mocked(api.recentHookEvent).mockRejectedValue(new ApiError(401, "Not authenticated"));
    renderPage();
    await waitFor(() => expect(api.recentHookEvent).toHaveBeenCalledTimes(1));
    await new Promise((r) => setTimeout(r, 200));
    expect(api.recentHookEvent).toHaveBeenCalledTimes(1);
    expect(screen.getByText(/Waiting for your first Meter event/)).toBeInTheDocument();
  });
});

describe("InstallSdkPage — the application slug", () => {
  const slugField = () => screen.findByLabelText("Application");

  it("reuses the application this organization already reports to", async () => {
    // A second service being instrumented should join the app that exists, not
    // start a parallel one under a slightly different name.
    vi.mocked(api.aiApplications).mockResolvedValue({
      applications: [{ id: "a1", name: "Support agent", slug: "support-agent" }] as never,
      from: "2026-08-10",
      to: "2026-09-09",
    });
    renderPage();
    await waitFor(async () => expect(await slugField()).toHaveValue("support-agent"));
  });

  it("falls back to the organization's name on a first install", async () => {
    vi.mocked(api.me).mockResolvedValue({
      id: "u1",
      tenant_id: "t-123",
      email: "cto@acme.com",
      org_name: "Acme Security",
    });
    renderPage();
    await waitFor(async () => expect(await slugField()).toHaveValue("acme-security"));
  });

  it("shows what a typed name becomes, rather than rewriting under the cursor", async () => {
    renderPage();
    const field = await slugField();
    fireEvent.change(field, { target: { value: "Support Agent" } });
    // The field keeps what was typed...
    expect(field).toHaveValue("Support Agent");
    // ...and the page says what the SDK will actually send.
    expect(screen.getByText("support-agent")).toBeInTheDocument();
  });

  it("writes the chosen slug into the env snippet and the agent prompt", async () => {
    renderPage("/install-sdk?guide=claude-code");
    fireEvent.change(await slugField(), { target: { value: "doc-review" } });

    await waitFor(() =>
      expect(screen.getAllByText(/METER_APPLICATION=doc-review/).length).toBeGreaterThan(0),
    );
    const prompt = promptFor("claude-code");
    expect(prompt).toContain("METER_APPLICATION=doc-review");
    // And the agent is told not to ask about it, because it is already decided.
    expect(prompt).not.toMatch(/If you do not know the application slug, ASK ME/);
  });

  it("never sends an empty or malformed slug to the snippets", async () => {
    renderPage();
    fireEvent.change(await slugField(), { target: { value: "!!!" } });
    await waitFor(() =>
      expect(screen.getAllByText(/METER_APPLICATION=my-app/).length).toBeGreaterThan(0),
    );
  });
});

describe("InstallSdkPage — the manual route teaches the SDK that exists", () => {
  const manual = async () => {
    renderPage("/install-sdk?tab=manual");
    await screen.findByRole("heading", { name: "1. Install the package" });
    return document.getElementById("install-panel-manual")!.textContent!;
  };

  it("shows the current entry points, not the ones the rewrite removed", async () => {
    const text = await manual();
    expect(text).toContain("meter.wrap(");
    expect(text).toContain("meter.agent(");
    expect(text).toContain("meter.resume(");
    for (const gone of [
      "record_anthropic",
      "record_openai",
      "recordAnthropic",
      "recordOpenAI",
      "from costlyinfra_meter import wrap",
    ]) {
      expect(text).not.toContain(gone);
    }
  });

  it("teaches the multi-step run, which is the reason to instrument at all", async () => {
    const text = await manual();
    expect(text).toContain("Record a multi-step run");
    expect(text).toContain("run.llm(");
    expect(text).toContain("run.tool(");
    expect(text).toContain("run.export_context()");
    expect(text).toContain("meter.flush()");
  });

  it("offers no field that could carry prompt content", async () => {
    // `metadata=` was on the old page as a wrap() argument. It is not in the
    // event contract, and showing it invites someone to put a prompt in it.
    expect(await manual()).not.toContain("metadata");
  });

  it("keeps the live token out of every snippet but the masked one", async () => {
    vi.mocked(api.createHookToken).mockResolvedValue({ token: "hk_live_secret" });
    renderPage("/install-sdk?tab=manual");
    fireEvent.click(await screen.findByRole("button", { name: "Generate ingest token" }));

    const shown = await screen.findByText(/METER_INGEST_TOKEN=hk_live_secret/);
    expect(shown.closest("[data-dd-privacy='mask']")).not.toBeNull();
    // Exactly one place shows it, and that place is masked. The manual step
    // shows the placeholder instead.
    expect(screen.getAllByText(/METER_INGEST_TOKEN=hk_live_secret/)).toHaveLength(1);
    expect(document.getElementById("install-panel-manual")!.textContent).not.toContain(
      "hk_live_secret",
    );
  });
});
