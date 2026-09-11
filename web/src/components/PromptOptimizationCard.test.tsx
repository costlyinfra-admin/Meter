import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { api, ApiError, type PromptConsentStatus } from "../api";
import { PromptOptimizationCard } from "./PromptOptimizationCard";

vi.mock("../api", async (importActual) => {
  const actual = await importActual<typeof import("../api")>();
  return {
    ...actual,
    api: {
      promptConsent: vi.fn(),
      promptAudit: vi.fn(),
      grantPromptConsent: vi.fn(),
      withdrawPromptConsent: vi.fn(),
      setPromptFeature: vi.fn(),
    },
  };
});

const MODEL = { source: "meter" as const, provider: "Groq", model: "openai/gpt-oss-120b" };

const NOT_GRANTED: PromptConsentStatus = {
  consent: null,
  current_version: "2026-09-11",
  current_disclosure: MODEL,
  version_outdated: false,
  disclosure_changed: false,
  capturing: false,
  retention_days: 30,
  features: [
    {
      feature_id: "f-1",
      name: "Ticket triage",
      enabled: false,
      enabled_by: null,
      enabled_at: null,
      samples: 0,
    },
  ],
};

const GRANTED: PromptConsentStatus = {
  ...NOT_GRANTED,
  consent: {
    consent_version: "2026-09-11",
    granted_by: "cto@acme.com",
    granted_at: "2026-09-11T10:00:00Z",
    disclosed: MODEL,
  },
  capturing: true,
};

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(api.promptConsent).mockResolvedValue(NOT_GRANTED);
  vi.mocked(api.promptAudit).mockResolvedValue({ events: [] });
});

const agreeBox = () => screen.getByRole("checkbox", { name: /I agree to these terms/ });
const passwordBox = () => screen.getByLabelText("Your password");
const turnOn = () => screen.getByRole("button", { name: /Turn on prompt optimization/ });

describe("PromptOptimizationCard — before consent", () => {
  it("names who would see prompts before anyone agrees", async () => {
    render(<PromptOptimizationCard />);
    expect(await screen.findByText(/Who sees your prompts/)).toBeInTheDocument();
    expect(screen.getByText("openai/gpt-oss-120b")).toBeInTheDocument();
    expect(screen.getByText(/Groq/)).toBeInTheDocument();
    expect(screen.getByText(/This is Meter's model/)).toBeInTheDocument();
  });

  it("states what is never collected and that withdrawing deletes everything", async () => {
    render(<PromptOptimizationCard />);
    await screen.findByText(/Who sees your prompts/);
    expect(screen.getByText(/tool calls and their results, images and files/)).toBeInTheDocument();
    expect(screen.getByText(/deletes everything collected immediately/)).toBeInTheDocument();
    expect(screen.getByText(/30 days, encrypted/)).toBeInTheDocument();
  });

  it("needs both the agreement and a password", async () => {
    render(<PromptOptimizationCard />);
    await screen.findByText(/Who sees your prompts/);
    expect(turnOn()).toBeDisabled();
    fireEvent.click(agreeBox());
    expect(turnOn()).toBeDisabled();
    fireEvent.change(passwordBox(), { target: { value: "correct horse" } });
    expect(turnOn()).toBeEnabled();
  });

  it("agrees to exactly the terms version and model on screen, then forgets the password", async () => {
    vi.mocked(api.grantPromptConsent).mockResolvedValue(GRANTED);
    render(<PromptOptimizationCard />);
    await screen.findByText(/Who sees your prompts/);
    fireEvent.click(agreeBox());
    fireEvent.change(passwordBox(), { target: { value: "correct horse" } });
    fireEvent.click(turnOn());

    await waitFor(() =>
      expect(api.grantPromptConsent).toHaveBeenCalledWith({
        password: "correct horse",
        accepted_version: "2026-09-11",
        accepted_disclosure: MODEL,
      }),
    );
    expect(await screen.findByText(/agreed by cto@acme.com/)).toBeInTheDocument();
  });

  it("says so when the password is wrong, and does not keep it", async () => {
    vi.mocked(api.grantPromptConsent).mockRejectedValue(
      new ApiError(403, "That password is not correct."),
    );
    render(<PromptOptimizationCard />);
    await screen.findByText(/Who sees your prompts/);
    fireEvent.click(agreeBox());
    fireEvent.change(passwordBox(), { target: { value: "wrong" } });
    fireEvent.click(turnOn());

    expect(await screen.findByText("That password is not correct.")).toBeInTheDocument();
    expect(passwordBox()).toHaveValue("");
  });

  it("cannot be turned on when no model could be named", async () => {
    vi.mocked(api.promptConsent).mockResolvedValue({ ...NOT_GRANTED, current_disclosure: null });
    render(<PromptOptimizationCard />);
    expect(await screen.findByText(/no model is available/)).toBeInTheDocument();
    expect(agreeBox()).toBeDisabled();
    expect(turnOn()).toBeDisabled();
  });
});

describe("PromptOptimizationCard — after consent", () => {
  beforeEach(() => {
    vi.mocked(api.promptConsent).mockResolvedValue(GRANTED);
  });

  it("switches capture per feature", async () => {
    vi.mocked(api.setPromptFeature).mockResolvedValue({
      ...GRANTED,
      features: [{ ...GRANTED.features[0], enabled: true, enabled_by: "cto@acme.com" }],
    });
    render(<PromptOptimizationCard />);
    fireEvent.click(
      await screen.findByRole("checkbox", { name: "Capture prompts for Ticket triage" }),
    );
    await waitFor(() => expect(api.setPromptFeature).toHaveBeenCalledWith("f-1", true));
    await waitFor(() =>
      expect(
        screen.getByRole("checkbox", { name: "Capture prompts for Ticket triage" }),
      ).toBeChecked(),
    );
  });

  it("asks before withdrawing, and says what it destroys", async () => {
    vi.mocked(api.withdrawPromptConsent).mockResolvedValue(NOT_GRANTED);
    render(<PromptOptimizationCard />);
    fireEvent.click(
      await screen.findByRole("button", { name: "Withdraw consent and delete everything" }),
    );
    expect(
      screen.getByText(/destroys the encryption key. It cannot be undone/),
    ).toBeInTheDocument();
    expect(api.withdrawPromptConsent).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("button", { name: "Yes, withdraw and delete" }));
    await waitFor(() => expect(api.withdrawPromptConsent).toHaveBeenCalledTimes(1));
    expect(await screen.findByText(/Who sees your prompts/)).toBeInTheDocument();
  });

  it("pauses and asks again when the model that would see prompts changes", async () => {
    vi.mocked(api.promptConsent).mockResolvedValue({
      ...GRANTED,
      capturing: false,
      disclosure_changed: true,
      current_disclosure: { source: "byok", provider: "OpenAI", model: "gpt-4o" },
    });
    render(<PromptOptimizationCard />);
    expect(await screen.findByText("Capture is paused.")).toBeInTheDocument();
    expect(screen.getByText(/model that would see your prompts has changed/)).toBeInTheDocument();
    expect(screen.getByText("gpt-4o")).toBeInTheDocument();
    expect(screen.getByText(/your organization's own model/)).toBeInTheDocument();
  });

  it("lists activity by who and what, without content", async () => {
    vi.mocked(api.promptAudit).mockResolvedValue({
      events: [
        {
          event: "sample_content_viewed",
          actor: "analyst@acme.com",
          feature_id: "f-1",
          feature_name: "Ticket triage",
          detail: { sample_id: "s-1", prompt_id: "triage", prompt_version: "v7" },
          created_at: "2026-09-11T11:00:00Z",
        },
        {
          event: "samples_purged",
          actor: null,
          feature_id: null,
          feature_name: null,
          detail: { samples_deleted: 3 },
          created_at: "2026-09-10T06:17:00Z",
        },
      ],
    });
    render(<PromptOptimizationCard />);
    expect(
      await screen.findByText("Viewed a captured prompt for Ticket triage"),
    ).toBeInTheDocument();
    expect(screen.getByText(/analyst@acme.com/)).toBeInTheDocument();
    expect(screen.getByText(/^Meter ·/)).toBeInTheDocument();
  });
});
