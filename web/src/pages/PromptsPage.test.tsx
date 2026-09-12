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
import {
  api,
  ApiError,
  type Evaluation,
  type EvaluationEstimate,
  type PromptDetail as PromptDetailData,
  type PromptSummary,
} from "../api";
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
      latestEvaluation: vi.fn(),
      evaluationEstimate: vi.fn(),
      evalKeys: vi.fn(),
      setEvalKey: vi.fn(),
      removeEvalKey: vi.fn(),
      startEvaluation: vi.fn(),
      evaluation: vi.fn(),
      evaluationCases: vi.fn(),
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

const ESTIMATE: EvaluationEstimate = {
  can_run: true,
  reason: null,
  candidate_id: "c1",
  cases: 24,
  provider: "anthropic",
  model: "claude-sonnet-4-6",
  priced: true,
  has_key: true,
  cost_estimate: 0.0288,
  spent_this_month: 1.2,
  monthly_cap: 25,
  min_cases_for_decision: 20,
};

const RUN: Evaluation = {
  evaluation_id: "e1",
  candidate_id: "c1",
  template_id: "t1",
  status: "completed",
  decision: "recommended",
  decision_reason: "No broken answers. Better on 6, same on 18, worse on 0 of 24.",
  cases_planned: 24,
  cases_done: 24,
  better: 6,
  same: 18,
  worse: 0,
  check_failures: 0,
  cost_before: 0.000944,
  cost_after: 0.000508,
  tokens_in_before: 1180,
  tokens_in_after: 604,
  tokens_out_before: 86,
  tokens_out_after: 41,
  latency_before_ms: 930,
  latency_after_ms: 610,
  spend: 0.0348,
  provider: "anthropic",
  model: "claude-sonnet-4-6",
  judge_model: "openai/gpt-oss-120b",
  error: "",
  started_by: "cto@acme.com",
  started_at: "2026-09-11T10:00:00Z",
  finished_at: "2026-09-11T10:04:00Z",
  calls_30d: 4200,
  projected_monthly_saving: 1.83,
  cases: [],
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
  // The testing panel loads whenever a rewrite exists: nothing tested yet.
  vi.mocked(api.latestEvaluation).mockRejectedValue(new ApiError(404, "No evaluation yet"));
  vi.mocked(api.evaluationEstimate).mockResolvedValue(ESTIMATE);
  vi.mocked(api.evalKeys).mockResolvedValue({ keys: [] });
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
    expect(await screen.findByText("Not tested")).toBeInTheDocument();
  });

  it("names a rewrite that passed testing, rather than showing nothing", async () => {
    vi.mocked(api.prompts).mockResolvedValue({
      usage_days: 30,
      min_samples: 20,
      prompts: [summary({ candidate_id: "c1", candidate_status: "recommended" })],
    });
    renderList();
    expect(await screen.findByText("Recommended")).toBeInTheDocument();
  });

  it("names one that failed testing too, so a bad result is not silence", async () => {
    vi.mocked(api.prompts).mockResolvedValue({
      usage_days: 30,
      min_samples: 20,
      prompts: [summary({ candidate_id: "c1", candidate_status: "not_recommended" })],
    });
    renderList();
    expect(await screen.findByText("Not recommended")).toBeInTheDocument();
  });

  it("says when a test is still running", async () => {
    vi.mocked(api.prompts).mockResolvedValue({
      usage_days: 30,
      min_samples: 20,
      prompts: [summary({ candidate_id: "c1", candidate_status: "evaluating" })],
    });
    renderList();
    expect(await screen.findByText("Testing")).toBeInTheDocument();
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

describe("A rewrite that has been tested", () => {
  it("stops calling itself untested, and drops the no-saving caution", async () => {
    vi.mocked(api.prompt).mockResolvedValue(
      detail({ candidate: { ...CANDIDATE, status: "recommended" } }),
    );
    vi.mocked(api.latestEvaluation).mockResolvedValue(RUN);
    renderDetail();
    await screen.findByRole("heading", { name: "Proposed rewrite" });
    expect(screen.queryByText("Not tested")).not.toBeInTheDocument();
    expect(screen.queryByText(/No saving is claimed yet/)).not.toBeInTheDocument();
    // The verdict appears on the rewrite itself, not only down in the panel.
    expect(screen.getAllByText("Recommended").length).toBeGreaterThan(1);
  });
});

describe("Testing a rewrite", () => {
  const withCandidate = () =>
    vi.mocked(api.prompt).mockResolvedValue(detail({ candidate: CANDIDATE }));

  it("says what a run will cost and what is left under the cap", async () => {
    withCandidate();
    renderDetail();
    expect(await screen.findByRole("heading", { name: "Testing" })).toBeInTheDocument();
    const panel = screen.getByRole("heading", { name: "Testing" }).closest("section")!;
    expect(panel.textContent).toContain("Replays 24 real inputs");
    expect(panel.textContent).toContain("$0.03");
    expect(panel.textContent).toContain("of $25.00 used this month");
  });

  it("asks for a key that can make model calls, and never echoes it back", async () => {
    withCandidate();
    vi.mocked(api.evaluationEstimate).mockResolvedValue({
      ...ESTIMATE,
      can_run: false,
      reason: "no_key",
      has_key: false,
    });
    vi.mocked(api.setEvalKey).mockResolvedValue({
      keys: [{ provider: "anthropic", has_key: true, added_by: "cto@acme.com", added_at: "x" }],
    });
    renderDetail();
    expect(await screen.findByText(/needs a key for this provider/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Test this rewrite" })).toBeDisabled();

    fireEvent.change(screen.getByLabelText("anthropic API key"), {
      target: { value: "sk-secret" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save key" }));
    await waitFor(() => expect(api.setEvalKey).toHaveBeenCalledWith("anthropic", "sk-secret"));
    expect(screen.getByLabelText("anthropic API key")).toHaveValue("");
  });

  it("reports progress while it replays", async () => {
    withCandidate();
    vi.mocked(api.startEvaluation).mockResolvedValue({
      ...RUN,
      status: "running",
      decision: null,
      cases_done: 3,
    });
    renderDetail();
    fireEvent.click(await screen.findByRole("button", { name: "Test this rewrite" }));
    expect(await screen.findByText(/Replaying 3 of 24 inputs/)).toBeInTheDocument();
  });

  it("shows the measurement at a precision where the two columns differ", async () => {
    withCandidate();
    vi.mocked(api.latestEvaluation).mockResolvedValue(RUN);
    renderDetail();
    expect(await screen.findByText("Recommended")).toBeInTheDocument();
    // A per-call cost is fractions of a cent: rounded to cents both rows would
    // read $0.00 and the comparison would say nothing.
    expect(screen.getByText("$0.00094")).toBeInTheDocument();
    expect(screen.getByText("$0.00051")).toBeInTheDocument();
    expect(screen.getByText(/Better on 6, the same on 18, worse on 0 of 24/)).toBeInTheDocument();
    expect(screen.getByText(/each case both ways round/)).toBeInTheDocument();
  });

  it("scales the saving by the volume it was measured against", async () => {
    withCandidate();
    vi.mocked(api.latestEvaluation).mockResolvedValue(RUN);
    renderDetail();
    const line = await screen.findByText(/4,200 calls in the last 30 days/);
    expect(line).toHaveTextContent("$1.83");
    expect(line).toHaveTextContent(/ceiling/);
  });

  it("does not claim a saving when the rewrite is not recommended", async () => {
    withCandidate();
    vi.mocked(api.latestEvaluation).mockResolvedValue({
      ...RUN,
      decision: "not_recommended",
      decision_reason: "3 case(s) came back broken.",
      check_failures: 3,
      better: 1,
      worse: 4,
    });
    renderDetail();
    expect(await screen.findByText("Not recommended")).toBeInTheDocument();
    expect(screen.getByText("3 case(s) came back broken.")).toBeInTheDocument();
    expect(screen.queryByText(/a month/)).not.toBeInTheDocument();
  });

  it("says a failed run left the rewrite untested", async () => {
    withCandidate();
    vi.mocked(api.latestEvaluation).mockResolvedValue({
      ...RUN,
      status: "failed",
      decision: null,
      error: "The provider refused a replay (401): ***",
    });
    renderDetail();
    expect(await screen.findByText(/The rewrite is unchanged and untested/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Test this rewrite" })).toBeInTheDocument();
  });

  it("fetches the replayed answers only when asked, because that read is audited", async () => {
    withCandidate();
    vi.mocked(api.latestEvaluation).mockResolvedValue(RUN);
    vi.mocked(api.evaluationCases).mockResolvedValue({
      cases: [
        {
          case_id: "k1",
          before: "phishing, because the domain is a lookalike",
          after: "phishing",
          verdict: "better",
          reason: "Same verdict, fewer words.",
          failed_checks: [],
        },
      ],
    });
    renderDetail();
    await screen.findByText("Recommended");
    expect(api.evaluationCases).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("button", { name: "Show the answers" }));
    await waitFor(() => expect(api.evaluationCases).toHaveBeenCalledWith("e1"));
    expect(await screen.findByText("phishing, because the domain is a lookalike")).toBeVisible();
  });
});
