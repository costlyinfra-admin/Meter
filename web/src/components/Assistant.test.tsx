import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { Assistant } from "./Assistant";
import { ApiError } from "../api";

const askAssistant = vi.fn();
const assistantMeta = vi.fn();

vi.mock("../api", async () => {
  const actual = await vi.importActual<typeof import("../api")>("../api");
  return {
    ...actual,
    api: {
      askAssistant: (...args: unknown[]) => askAssistant(...args),
      assistantMeta: () => assistantMeta(),
    },
  };
});

/** Open the panel and let it finish loading, so nothing settles mid-assertion. */
async function open() {
  render(
    <MemoryRouter>
      <Assistant />
    </MemoryRouter>,
  );
  fireEvent.click(screen.getByRole("button", { name: /open support assistant/i }));
  await waitFor(() => expect(assistantMeta).toHaveBeenCalled());
}

function ask(question: string) {
  fireEvent.change(screen.getByLabelText(/ask the assistant/i), { target: { value: question } });
  fireEvent.click(screen.getByRole("button", { name: /send question/i }));
}

/** Reveal a reply instantly. These tests are about what an answer says, not how
 *  it arrives; the reveal itself has its own test below. */
function instantReplies(reduce = true) {
  Object.defineProperty(window, "matchMedia", {
    writable: true,
    configurable: true,
    value: (query: string) => ({
      matches: reduce && query.includes("reduce"),
      media: query,
      addEventListener: () => {},
      removeEventListener: () => {},
      addListener: () => {},
      removeListener: () => {},
      onchange: null,
      dispatchEvent: () => false,
    }),
  });
}

beforeEach(() => {
  instantReplies();
  sessionStorage.clear();
  askAssistant.mockReset();
  assistantMeta.mockReset().mockResolvedValue({
    composed: true,
    support_email: "support@costlyinfra.com",
  });
});

afterEach(() => sessionStorage.clear());

describe("support assistant", () => {
  it("stays out of the way until it is opened", () => {
    render(
      <MemoryRouter>
        <Assistant />
      </MemoryRouter>,
    );
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: /open support assistant/i })).toBeInTheDocument();
  });

  it("opens on a greeting and a set of common questions", async () => {
    await open();
    expect(screen.getByRole("dialog", { name: /support assistant/i })).toBeInTheDocument();
    expect(screen.getByText(/Ask me anything about your AI usage/)).toBeInTheDocument();
    expect(screen.getByText("Common questions")).toBeInTheDocument();
    // The starters show both halves it can answer from: live data, and how a
    // number is built.
    expect(
      screen.getByRole("button", { name: /Is an agent stuck in a loop/i }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: /When was my data last refreshed/i }),
    ).toBeInTheDocument();
  });

  it("answers a question, grounded in handbook excerpts it sends with it", async () => {
    askAssistant.mockResolvedValue({
      answer: "Build cost is what a feature cost to make.",
      sources: ["concepts/build-vs-inference"],
      answered: true,
      composed: true,
    });
    await open();
    ask("what is build cost?");

    expect(await screen.findByText(/what a feature cost to make/)).toBeInTheDocument();

    const [body] = askAssistant.mock.calls[0];
    expect(body.question).toBe("what is build cost?");
    expect(body.passages.length).toBeGreaterThan(0);
    expect(body.passages[0]).toHaveProperty("text");
    // The screen the user is on travels with the question.
    expect(body.page).toBe("Overview");
  });

  it("answers without citing documentation back at the reader", async () => {
    // Documentation is supporting material. Someone who asked about their own
    // product does not want a reading list under the reply, and a citation made
    // every answer read as a search result.
    askAssistant.mockResolvedValue({
      answer: "They are kept separate.",
      sources: ["concepts/build-vs-inference"],
      answered: true,
      composed: true,
    });
    await open();
    ask("build vs inference?");

    expect(await screen.findByText("They are kept separate.")).toBeInTheDocument();
    expect(
      screen.queryByRole("link", { name: /Build cost vs inference cost/i }),
    ).not.toBeInTheDocument();
    expect(screen.queryByText(/In the handbook/i)).not.toBeInTheDocument();
  });

  it("offers a human when it cannot answer", async () => {
    askAssistant.mockResolvedValue({
      answer: "I can't answer that one right now.",
      sources: [],
      answered: false,
      composed: true,
    });
    await open();
    await waitFor(() => expect(assistantMeta).toHaveBeenCalled());
    ask("do you support SAP?");

    const mail = await screen.findByRole("link", { name: /email support/i });
    expect(mail).toHaveAttribute("href", "mailto:support@costlyinfra.com");
  });

  it("asks a suggested question when one is clicked", async () => {
    askAssistant.mockResolvedValue({
      answer: "Sure.",
      sources: [],
      answered: true,
      composed: true,
    });
    await open();
    fireEvent.click(screen.getByRole("button", { name: /Which traces used the most tokens/i }));

    await waitFor(() => expect(askAssistant).toHaveBeenCalled());
    // Suggestions give way to the conversation once it has started.
    expect(screen.queryByText("Common questions")).not.toBeInTheDocument();
  });

  it("says something useful when the assistant is unreachable", async () => {
    askAssistant.mockRejectedValue(new Error("network"));
    await open();
    ask("hello?");
    expect(
      await screen.findByText(/Something went wrong reaching the assistant/),
    ).toBeInTheDocument();
  });

  it("explains a rate limit rather than showing a raw error", async () => {
    askAssistant.mockRejectedValue(new ApiError(429, "Too many questions"));
    await open();
    ask("hello?");
    expect(await screen.findByText(/give it a minute/)).toBeInTheDocument();
  });

  it("keeps the conversation across a page change", async () => {
    askAssistant.mockResolvedValue({
      answer: "Kept for later.",
      sources: [],
      answered: true,
      composed: true,
    });
    await open();
    ask("remember this?");
    await screen.findByText("Kept for later.");

    // A fresh mount is what navigating away and back looks like to this widget.
    render(
      <MemoryRouter>
        <Assistant />
      </MemoryRouter>,
    );
    const launchers = screen.getAllByRole("button", { name: /open support assistant/i });
    const reopened = launchers[launchers.length - 1];
    fireEvent.click(reopened);
    await waitFor(() => expect(screen.getAllByText("Kept for later.").length).toBe(2));
  });

  it("closes on Escape", async () => {
    await open();
    fireEvent.keyDown(window, { key: "Escape" });
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });
});

describe("support assistant — how a reply arrives", () => {
  it("reveals the answer rather than pasting it whole", async () => {
    // A whole answer appearing at once reads as a lookup. Revealing it reads as
    // a reply — and gives someone a beat to start at the top.
    instantReplies(false);
    vi.useFakeTimers({ shouldAdvanceTime: true });
    askAssistant.mockResolvedValue({
      answer: "Build cost and inference cost are tracked separately, and never added together.",
      sources: [],
      answered: true,
    });
    await open();
    fireEvent.change(screen.getByLabelText(/ask the assistant/i), { target: { value: "how?" } });
    fireEvent.submit(screen.getByRole("button", { name: /send question/i }).closest("form")!);

    // Part-way through, some of it is on screen and the rest is not.
    await vi.advanceTimersByTimeAsync(300);
    const bubble = () => document.querySelectorAll(".assist-msg.bot");
    const partial = bubble()[bubble().length - 1].textContent ?? "";
    expect(partial.length).toBeGreaterThan(2);
    expect(partial).not.toContain("never added together");

    await vi.advanceTimersByTimeAsync(5000);
    expect(screen.getByText(/never added together/)).toBeInTheDocument();
    vi.useRealTimers();
  });

  it("shows it whole for someone who asked for less motion", async () => {
    instantReplies(true);
    askAssistant.mockResolvedValue({ answer: "All of it at once.", sources: [], answered: true });
    await open();
    fireEvent.change(screen.getByLabelText(/ask the assistant/i), { target: { value: "how?" } });
    fireEvent.submit(screen.getByRole("button", { name: /send question/i }).closest("form")!);

    expect(await screen.findByText("All of it at once.")).toBeInTheDocument();
  });
});
