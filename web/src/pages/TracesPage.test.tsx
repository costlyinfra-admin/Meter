import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api, type AiTrace, type AiTracePage } from "../api";
import { TraceDetail } from "./TraceDetail";
import { TracesPage } from "./TracesPage";

vi.mock("../api", async (importActual) => {
  const actual = await importActual<typeof import("../api")>();
  return { ...actual, api: { aiTraces: vi.fn(), aiTrace: vi.fn() } };
});

function trace(over: Partial<AiTrace> = {}): AiTrace {
  return {
    id: "uuid-1",
    trace_id: "t-1",
    operation_name: "resolve-ticket",
    status: "success",
    live_status: "success",
    started_at: new Date(Date.now() - 60_000).toISOString(),
    ended_at: new Date().toISOString(),
    duration_ms: 18_400,
    last_activity_at: new Date().toISOString(),
    last_heartbeat_at: null,
    current_span_id: null,
    total_cost: 0.042,
    total_tokens: 10_674,
    span_count: 7,
    llm_calls: 2,
    environment: "production",
    release_version: "2026.9.1",
    customer_ref: null,
    application: { id: "a1", name: "Support agent", slug: "support-agent" },
    feature: { id: "f1", name: "AI threat triage" },
    ...over,
  };
}

function page(traces: AiTrace[]): AiTracePage {
  return { traces, total: traces.length, limit: 50, offset: 0, stale_after_minutes: 10 };
}

let search = "";
function Probe() {
  search = useLocation().search;
  return null;
}

function renderTraces(url = "/traces") {
  search = "";
  return render(
    <MemoryRouter initialEntries={[url]}>
      <TracesPage />
      <Probe />
    </MemoryRouter>,
  );
}

describe("TracesPage", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.aiTraces).mockResolvedValue(page([trace()]));
  });
  afterEach(() => vi.useRealTimers());

  it("lists runs with their economics", async () => {
    renderTraces();
    expect(await screen.findByRole("heading", { name: "Traces" })).toBeInTheDocument();
    expect(screen.getByText("Request-level evidence behind your AI spend.")).toBeInTheDocument();

    const row = (await screen.findByText("resolve-ticket")).closest("tr") as HTMLElement;
    expect(within(row).getByText("Support agent")).toBeInTheDocument();
    expect(within(row).getByText("AI threat triage")).toBeInTheDocument();
    expect(within(row).getByText("$0.04")).toBeInTheDocument();
    expect(within(row).getByText("10,674")).toBeInTheDocument();
  });

  it("keeps the selected tab in the URL so a view can be linked", async () => {
    renderTraces();
    fireEvent.click(await screen.findByRole("tab", { name: "Running now" }));
    await waitFor(() => expect(search).toBe("?tab=running"));
  });

  it("opens straight onto a tab named in the URL", async () => {
    renderTraces("/traces?tab=explore");
    expect(await screen.findByRole("tab", { name: "Explore" })).toHaveAttribute(
      "aria-selected",
      "true",
    );
  });

  it("asks the server to re-sort rather than sorting a single page", async () => {
    renderTraces();
    await screen.findByText("resolve-ticket");
    fireEvent.change(screen.getByLabelText(/Sort/), { target: { value: "expensive" } });

    await waitFor(() =>
      expect(api.aiTraces).toHaveBeenLastCalledWith(expect.objectContaining({ sort: "expensive" })),
    );
  });

  it("shows the install path when nothing has reported yet", async () => {
    vi.mocked(api.aiTraces).mockResolvedValue(page([]));
    renderTraces();

    expect(await screen.findByText("No traces received")).toBeInTheDocument();
    expect(
      screen.getByText("Run an instrumented AI workflow to see request-level cost here."),
    ).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Install SDK" })).toHaveAttribute(
      "href",
      "/install-sdk",
    );
  });

  it("says so plainly when no agent is active", async () => {
    vi.mocked(api.aiTraces).mockResolvedValue(page([]));
    renderTraces("/traces?tab=running");
    expect(await screen.findByText("No agent runs are active.")).toBeInTheDocument();
  });

  // Running now must show BOTH: a stale agent is the one someone opened this
  // tab to find, and filtering it out would defeat the point.
  it("shows running and stale agents together, each labelled", async () => {
    vi.mocked(api.aiTraces).mockResolvedValue(
      page([
        trace({
          id: "r",
          trace_id: "r",
          status: "running",
          live_status: "running",
          current_span_id: "answer",
        }),
        trace({
          id: "s",
          trace_id: "s",
          operation_name: "extract-obligations",
          status: "running",
          live_status: "stale",
        }),
      ]),
    );
    renderTraces("/traces?tab=running");

    expect(await screen.findByText("Running")).toBeInTheDocument();
    expect(screen.getByText("Stale")).toBeInTheDocument();
    // The server is asked for everything stored as running; the label is derived.
    expect(api.aiTraces).toHaveBeenCalledWith(expect.objectContaining({ trace_status: "running" }));
  });

  it("communicates status by word, not only by colour", async () => {
    vi.mocked(api.aiTraces).mockResolvedValue(
      page([trace({ live_status: "error", status: "error" })]),
    );
    renderTraces();
    // Someone who cannot distinguish the hues still reads "Error".
    expect(await screen.findByText("Error")).toBeInTheDocument();
  });

  it("polls Running now, and stops when it unmounts", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const { unmount } = renderTraces("/traces?tab=running");
    await waitFor(() => expect(api.aiTraces).toHaveBeenCalledTimes(1));

    await vi.advanceTimersByTimeAsync(5100);
    await waitFor(() => expect(api.aiTraces).toHaveBeenCalledTimes(2));

    unmount();
    await vi.advanceTimersByTimeAsync(15000);
    // No further requests after the page is gone.
    expect(api.aiTraces).toHaveBeenCalledTimes(2);
  });

  it("does not poll while the document is hidden", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    renderTraces("/traces?tab=running");
    await waitFor(() => expect(api.aiTraces).toHaveBeenCalledTimes(1));

    const spy = vi.spyOn(document, "visibilityState", "get").mockReturnValue("hidden");
    await vi.advanceTimersByTimeAsync(15000);
    // Polling a hidden tab spends the customer's API budget on nothing.
    expect(api.aiTraces).toHaveBeenCalledTimes(1);
    spy.mockRestore();
  });

  it("does not poll the history tab at all", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    renderTraces("/traces?tab=all");
    await waitFor(() => expect(api.aiTraces).toHaveBeenCalledTimes(1));
    await vi.advanceTimersByTimeAsync(20000);
    expect(api.aiTraces).toHaveBeenCalledTimes(1);
  });

  it("keeps the last good data when a poll fails", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    renderTraces("/traces?tab=running");
    expect(await screen.findByText("resolve-ticket")).toBeInTheDocument();

    vi.mocked(api.aiTraces).mockRejectedValue(new Error("network"));
    await vi.advanceTimersByTimeAsync(5100);

    // A dropped connection must not blank a table someone is reading.
    expect(screen.getByText("resolve-ticket")).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("groups by a dimension in Explore and keeps it in the URL", async () => {
    vi.mocked(api.aiTraces).mockResolvedValue(
      page([
        trace({ id: "1", total_cost: 3 }),
        trace({
          id: "2",
          total_cost: 1,
          application: { id: "a2", name: "Document review", slug: "document-review" },
        }),
      ]),
    );
    renderTraces("/traces?tab=explore");

    expect(await screen.findByText("Support agent")).toBeInTheDocument();
    expect(screen.getByText("Document review")).toBeInTheDocument();

    fireEvent.change(screen.getByLabelText(/Group by/), { target: { value: "environment" } });
    await waitFor(() => expect(search).toContain("group=environment"));
  });
});

// --- detail ---------------------------------------------------------------
function renderDetail(id = "t-1") {
  return render(
    <MemoryRouter initialEntries={[`/traces/${id}`]}>
      <Routes>
        <Route path="/traces/:id" element={<TraceDetail />} />
      </Routes>
    </MemoryRouter>,
  );
}

function span(over: Partial<AiTrace["spans"] extends (infer S)[] | undefined ? S : never> = {}) {
  return {
    external_span_id: "s1",
    parent_span_id: null,
    parent_missing: false,
    span_kind: "llm" as const,
    operation_name: "generate-answer",
    provider: "anthropic",
    model: "claude-sonnet-4-6",
    tokens_in: 8400,
    tokens_out: 920,
    cache_read_tokens: 6100,
    cache_write_tokens: 1200,
    reasoning_tokens: null,
    amount: 0.031,
    latency_ms: 6400,
    status: "success" as const,
    prompt_id: "answer-ticket",
    prompt_version: "5.0",
    started_at: new Date(Date.now() - 10_000).toISOString(),
    ended_at: new Date().toISOString(),
    ...over,
  };
}

describe("TraceDetail", () => {
  beforeEach(() => vi.clearAllMocks());
  afterEach(() => vi.useRealTimers());

  it("shows the run's economics and where it came from", async () => {
    vi.mocked(api.aiTrace).mockResolvedValue(trace({ spans: [span()] }));
    renderDetail();

    expect(await screen.findByRole("heading", { name: "resolve-ticket" })).toBeInTheDocument();
    expect(screen.getByText("Total cost")).toBeInTheDocument();
    expect(screen.getByText("$0.04")).toBeInTheDocument();
    expect(screen.getByText(/Support agent/)).toBeInTheDocument();
    expect(screen.getByText(/2026\.9\.1/)).toBeInTheDocument();
  });

  it("nests a child under its parent", async () => {
    vi.mocked(api.aiTrace).mockResolvedValue(
      trace({
        spans: [
          span({ external_span_id: "root", span_kind: "workflow", operation_name: "resolve" }),
          span({ external_span_id: "child", parent_span_id: "root" }),
        ],
      }),
    );
    renderDetail();

    const rows = await screen.findAllByRole("button", { expanded: false });
    // The child is rendered after its parent, and indented.
    expect(rows[0]).toHaveTextContent("resolve");
    expect(rows[1]).toHaveTextContent("generate-answer");
  });

  it("shows a span whose parent never arrived rather than dropping it", async () => {
    vi.mocked(api.aiTrace).mockResolvedValue(
      trace({
        spans: [
          span({ external_span_id: "orphan", parent_span_id: "never", parent_missing: true }),
        ],
      }),
    );
    renderDetail();

    // It happened and it cost money; hiding it would understate the run.
    expect(await screen.findByText("generate-answer")).toBeInTheDocument();
    expect(screen.getByText("parent missing")).toBeInTheDocument();
  });

  it("expands a span to its token and prompt identity", async () => {
    vi.mocked(api.aiTrace).mockResolvedValue(trace({ spans: [span()] }));
    renderDetail();
    fireEvent.click(await screen.findByRole("button", { expanded: false }));

    expect(screen.getByText("Input tokens")).toBeInTheDocument();
    expect(screen.getByText("8,400")).toBeInTheDocument();
    expect(screen.getByText("Cache read tokens")).toBeInTheDocument();
    expect(screen.getByText("answer-ticket v5.0")).toBeInTheDocument();
  });

  it("says why there is no prompt content to show", async () => {
    vi.mocked(api.aiTrace).mockResolvedValue(trace({ spans: [span()] }));
    renderDetail();
    fireEvent.click(await screen.findByRole("button", { expanded: false }));

    // Its absence is a guarantee, not a gap to file a bug about.
    expect(screen.getByText(/never prompt or response content/)).toBeInTheDocument();
  });

  it("renders no prompt, response or exception text anywhere", async () => {
    vi.mocked(api.aiTrace).mockResolvedValue(trace({ spans: [span({ status: "error" })] }));
    const { container } = renderDetail();
    await screen.findByText("generate-answer");

    const text = container.textContent ?? "";
    for (const banned of ["Traceback", "stack trace", "prompt:", "assistant:", "user:"]) {
      expect(text.toLowerCase()).not.toContain(banned.toLowerCase());
    }
  });

  it("shows live progress for a run still going", async () => {
    vi.mocked(api.aiTrace).mockResolvedValue(
      trace({
        status: "running",
        live_status: "running",
        ended_at: null,
        duration_ms: null,
        current_span_id: "answer",
        spans: [span({ status: "running", ended_at: null })],
      }),
    );
    renderDetail();

    expect(await screen.findByText(/Currently running/)).toBeInTheDocument();
    expect(screen.getByText("Runtime")).toBeInTheDocument();
  });

  it("stops refreshing a finished trace", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    vi.mocked(api.aiTrace).mockResolvedValue(trace({ spans: [span()] }));
    renderDetail();
    await waitFor(() => expect(api.aiTrace).toHaveBeenCalledTimes(1));

    await vi.advanceTimersByTimeAsync(20000);
    // Nothing about a completed run can change.
    expect(api.aiTrace).toHaveBeenCalledTimes(1);
  });

  it("explains a trace that cannot be loaded", async () => {
    const { ApiError } = await vi.importActual<typeof import("../api")>("../api");
    vi.mocked(api.aiTrace).mockRejectedValue(new ApiError(404, "Not found"));
    renderDetail("missing");

    expect(await screen.findByRole("alert")).toHaveTextContent("Not found");
    expect(screen.getByRole("link", { name: /Back to traces/ })).toBeInTheDocument();
  });
});
