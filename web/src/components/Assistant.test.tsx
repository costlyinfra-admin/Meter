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

beforeEach(() => {
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
    expect(screen.getByText(/I'm the Meter assistant/)).toBeInTheDocument();
    expect(screen.getByText("Common questions")).toBeInTheDocument();
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

  it("links every answer back to the handbook topics behind it", async () => {
    askAssistant.mockResolvedValue({
      answer: "They are kept separate.",
      sources: ["concepts/build-vs-inference"],
      answered: true,
      composed: true,
    });
    await open();
    ask("build vs inference?");

    const link = await screen.findByRole("link", { name: /Build cost vs inference cost/i });
    expect(link).toHaveAttribute("href", "/help/concepts/build-vs-inference");
  });

  it("offers a human when the handbook has no answer", async () => {
    askAssistant.mockResolvedValue({
      answer: "The handbook doesn't cover that.",
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
    fireEvent.click(screen.getByRole("button", { name: /How do I connect a provider/i }));

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
