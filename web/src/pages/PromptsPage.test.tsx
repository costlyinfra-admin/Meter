/**
 * Prompts, list and detail.
 *
 * The things these screens must not get wrong: usage comes from metering rather
 * than from the handful of samples, a rewrite is never called recommended
 * before it has been tested, and prompt text is fetched only when someone asks
 * for it, because that read is audited.
 */
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { api, type PromptDetail as PromptDetailData, type PromptSummary } from "../api";
import { PromptDetail, PromptsPage } from "./PromptsPage";

vi.mock("../api", async (importActual) => {
  const actual = await importActual<typeof import("../api")>();
  return {
    ...actual,
    api: {
      prompts: vi.fn(),
      prompt: vi.fn(),
      promptContent: vi.fn(),
      generatePromptCandidate: vi.fn(),
      discardPromptCandidate: vi.fn(),
    },
  };
});

const summary = (over: Partial<PromptSummary> = {}): PromptSummary => ({
  template_id: "t1",
  feature_id: "f1",
  feature_name: "Ticket triage",
  prompt_id: "classify-alert",
  prompt_version: "v7",
  template_bytes: 820,
  last_seen_at: "2026-09-11T10:00:00Z",
  samples: 24,
  calls: 4200,
  cost: 31.5,
  candidate_id: null,
  candidate_status: null,
  ...over,
});

const detail = (over: Partial<PromptDetailData> = {}): PromptDetailData => ({
  ...summary(),
  samples_detail: [
    {
      sample_id: "s1",
      provider: "anthropic",
      model: "claude-sonnet-4-6",
      tokens_in: 1200,
      tokens_out: 12,
      latency_ms: 850,
      captured_at: "2026-09-11T10:00:00Z",
    },
  ],
  candidate: null,
  ...over,
});

const CANDIDATE = {
  candidate_id: "c1",
  change_count: 1,
  original_chars: 820,
  candidate_chars: 540,
  provider: "Groq",
  model: "openai/gpt-oss-120b",
  status: "not_evaluated" as const,
  created_by: "cto@acme.com",
  created_at: "2026-09-11T11:00:00Z",
};

const CONTENT = {
  template_id: "t1",
  template: "You classify {ticket}. Answer in one word.",
  candidate: {
    candidate_id: "c1",
    template: "Classify {ticket}. One word.",
    changes: [
      {
        category: "redundancy",
        before: "Answer in one word.",
        after: "One word.",
        reason: "The same instruction, shorter.",
        expected_effect: "Fewer input tokens per call.",
      },
    ],
  },
};

const renderList = () =>
  render(
    <MemoryRouter initialEntries={["/optimize/prompts"]}>
      <Routes>
        <Route path="/optimize/prompts" element={<PromptsPage />} />
        <Route path="/optimize/prompts/:id" element={<PromptDetail />} />
      </Routes>
    </MemoryRouter>,
  );

const renderDetail = () =>
  render(
    <MemoryRouter initialEntries={["/optimize/prompts/t1"]}>
      <Routes>
        <Route path="/optimize/prompts/:id" element={<PromptDetail />} />
      </Routes>
    </MemoryRouter>,
  );

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(api.prompts).mockResolvedValue({
    usage_days: 30,
    min_samples: 20,
    prompts: [summary()],
  });
  vi.mocked(api.prompt).mockResolvedValue(detail());
  vi.mocked(api.promptContent).mockResolvedValue(CONTENT);
});

describe("Prompts list", () => {
  it("shows metered calls and cost, not the sample count, as the usage", async () => {
    renderList();
    const row = (await screen.findByRole("link", { name: "classify-alert" })).closest("tr")!;
    expect(within(row).getByText("4,200")).toBeInTheDocument();
    expect(within(row).getByText("$31.50")).toBeInTheDocument();
    expect(within(row).getByText("24")).toBeInTheDocument();
    expect(screen.getByText(/metered traffic over the last 30 days/)).toBeInTheDocument();
  });

  it("shows how far a prompt is from having enough samples", async () => {
    vi.mocked(api.prompts).mockResolvedValue({
      usage_days: 30,
      min_samples: 20,
      prompts: [summary({ samples: 6 })],
    });
    renderList();
    const row = (await screen.findByRole("link", { name: "classify-alert" })).closest("tr")!;
    expect(row.textContent).toContain("6");
    expect(row.textContent).toContain("/ 20");
  });

  it("marks a rewrite as proposed but untested", async () => {
    vi.mocked(api.prompts).mockResolvedValue({
      usage_days: 30,
      min_samples: 20,
      prompts: [summary({ candidate_id: "c1", candidate_status: "not_evaluated" })],
    });
    renderList();
    expect(await screen.findByText("Proposed, not tested")).toBeInTheDocument();
  });

  it("says how prompts get here when there are none", async () => {
    vi.mocked(api.prompts).mockResolvedValue({ usage_days: 30, min_samples: 20, prompts: [] });
    renderList();
    expect(await screen.findByText("No prompts collected yet")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Settings" })).toHaveAttribute(
      "href",
      "/settings#privacy",
    );
  });

  it("opens a prompt from its row", async () => {
    renderList();
    fireEvent.click(await screen.findByRole("link", { name: "classify-alert" }));
    expect(await screen.findByRole("heading", { name: "The prompt" })).toBeInTheDocument();
  });
});

describe("Prompt detail", () => {
  it("does not fetch the prompt text until someone asks", async () => {
    renderDetail();
    await screen.findByRole("heading", { name: "classify-alert" });
    expect(api.promptContent).not.toHaveBeenCalled();
    expect(screen.getByText("Hidden.")).toBeInTheDocument();
    expect(screen.getByText(/Meter records who looked at a prompt/)).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Show prompt" }));
    expect(await screen.findByText("You classify {ticket}. Answer in one word.")).toBeVisible();
    expect(api.promptContent).toHaveBeenCalledWith("t1");
  });

  it("will not ask for a rewrite until there are enough samples", async () => {
    vi.mocked(api.prompt).mockResolvedValue(detail({ samples: 6 }));
    renderDetail();
    const button = await screen.findByRole("button", { name: "Suggest a cheaper prompt" });
    expect(button).toBeDisabled();
    expect(screen.getByText(/6 of 20 samples so far/)).toBeInTheDocument();
  });

  it("asks for a rewrite and shows it beside the prompt, with its reasons", async () => {
    vi.mocked(api.generatePromptCandidate).mockResolvedValue(CANDIDATE);
    vi.mocked(api.prompt)
      .mockResolvedValueOnce(detail())
      .mockResolvedValue(detail({ candidate: CANDIDATE }));
    renderDetail();
    fireEvent.click(await screen.findByRole("button", { name: "Suggest a cheaper prompt" }));

    await waitFor(() => expect(api.generatePromptCandidate).toHaveBeenCalledWith("t1"));
    expect(await screen.findByText("Classify {ticket}. One word.")).toBeVisible();
    expect(screen.getByText("Repeated instruction")).toBeInTheDocument();
    expect(screen.getByText("The same instruction, shorter.")).toBeInTheDocument();
    expect(screen.getByText(/The model expects: Fewer input tokens per call/)).toBeInTheDocument();
  });

  it("claims no saving, and says the rewrite is untested", async () => {
    vi.mocked(api.prompt).mockResolvedValue(detail({ candidate: CANDIDATE }));
    renderDetail();
    expect(await screen.findByText("Not tested")).toBeInTheDocument();
    expect(screen.getByText(/No saving is claimed yet/)).toBeInTheDocument();
    expect(screen.getByText(/written by Groq \(openai\/gpt-oss-120b\)/)).toBeInTheDocument();
  });

  it("asks before discarding a rewrite", async () => {
    vi.mocked(api.prompt).mockResolvedValue(detail({ candidate: CANDIDATE }));
    vi.mocked(api.discardPromptCandidate).mockResolvedValue({ discarded: true });
    renderDetail();
    fireEvent.click(await screen.findByRole("button", { name: "Discard this rewrite" }));
    expect(api.discardPromptCandidate).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("button", { name: "Yes, discard it" }));
    await waitFor(() => expect(api.discardPromptCandidate).toHaveBeenCalledWith("c1"));
  });

  it("says so when the server refuses", async () => {
    vi.mocked(api.generatePromptCandidate).mockRejectedValue(
      new (await import("../api")).ApiError(400, "The model found nothing to change."),
    );
    renderDetail();
    fireEvent.click(await screen.findByRole("button", { name: "Suggest a cheaper prompt" }));
    expect(await screen.findByText("The model found nothing to change.")).toBeInTheDocument();
  });
});
