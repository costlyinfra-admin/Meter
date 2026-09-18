import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { AskAction, AskMeterBubble } from "./AskMeter";
import {
  ASK_EVENT,
  AskDetail,
  AskSuggestion,
  CHAT_OPENED_EVENT,
  DISCOVERED_KEY,
  DISMISSED_KEY,
  INVITED_KEY,
  publishOverviewContext,
  resetAskMeter,
} from "../askMeter";

const SUGGESTIONS: AskSuggestion[] = [
  {
    label: "Explain the cost spike",
    detail: "Sep 4 was the costliest day at $1,204 — 3.1x the $388 median day this period.",
    topic: "the cost spike",
    question: "What caused the cost spike on my Overview? Sep 4 was the costliest day.",
  },
  {
    label: "Investigate unattributed spend",
    detail: "$2,480 is not tied to any feature.",
    topic: "unattributed spend",
    question: "$2,480 of my spend is unattributed. Why does spend land there?",
  },
];

function show(path = "/") {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <AskMeterBubble />
    </MemoryRouter>,
  );
}

/** Push the invitation's six-second wait past its end. */
function waitOutTheTimer() {
  act(() => void vi.advanceTimersByTime(6000));
}

/** Everything dispatched at the assistant, in order. */
function asks(): AskDetail[] {
  return sent;
}
let sent: AskDetail[] = [];
const collect = (e: Event) => void sent.push((e as CustomEvent<AskDetail>).detail);

beforeEach(() => {
  vi.useFakeTimers({ shouldAdvanceTime: true });
  localStorage.clear();
  resetAskMeter();
  sent = [];
  window.addEventListener(ASK_EVENT, collect);
});

afterEach(() => {
  window.removeEventListener(ASK_EVENT, collect);
  vi.useRealTimers();
});

describe("the compact bubble", () => {
  it("names the assistant rather than showing a glyph", () => {
    show();
    expect(screen.getByRole("button", { name: "Ask Meter" })).toBeInTheDocument();
  });

  it("opens the existing chat without asking anything", () => {
    show();
    fireEvent.click(screen.getByRole("button", { name: "Ask Meter" }));
    expect(asks()).toEqual([{ question: "", source: "bubble" }]);
  });

  it("stays out of the way once the chat has been found", () => {
    localStorage.setItem(DISCOVERED_KEY, "1");
    show();
    expect(screen.queryByRole("button", { name: "Ask Meter" })).not.toBeInTheDocument();
  });

  it("retires itself the moment the chat is opened by any route", () => {
    show();
    act(() => void window.dispatchEvent(new Event(CHAT_OPENED_EVENT)));
    expect(screen.queryByRole("button", { name: "Ask Meter" })).not.toBeInTheDocument();
  });

  it("can be dismissed, and does not come back on the next visit", () => {
    const first = show();
    fireEvent.click(screen.getByRole("button", { name: /dismiss the ask meter tip/i }));
    expect(screen.queryByRole("button", { name: "Ask Meter" })).not.toBeInTheDocument();
    expect(localStorage.getItem(DISMISSED_KEY)).toBe("1");

    first.unmount();
    show();
    expect(screen.queryByRole("button", { name: "Ask Meter" })).not.toBeInTheDocument();
  });
});

describe("the contextual invitation", () => {
  it("waits six seconds on the Overview before expanding", () => {
    show();
    act(() => void publishOverviewContext(SUGGESTIONS));

    act(() => void vi.advanceTimersByTime(5000));
    expect(screen.queryByText(/worth investigating/i)).not.toBeInTheDocument();

    act(() => void vi.advanceTimersByTime(1000));
    expect(screen.getByText(/i found a few things worth investigating/i)).toBeInTheDocument();
  });

  it("stays compact on every other page", () => {
    show("/features");
    act(() => void publishOverviewContext(SUGGESTIONS));
    waitOutTheTimer();
    expect(screen.queryByText(/worth investigating/i)).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Ask Meter" })).toBeInTheDocument();
  });

  it("says nothing when the Overview has nothing to point at", () => {
    show();
    act(() => void publishOverviewContext([]));
    waitOutTheTimer();
    expect(screen.queryByText(/worth investigating/i)).not.toBeInTheDocument();
  });

  it("never opens the chat by itself", () => {
    show();
    act(() => void publishOverviewContext(SUGGESTIONS));
    waitOutTheTimer();
    expect(asks()).toEqual([]);
  });

  it("writes its copy from the live dashboard, not from a fixture", () => {
    show();
    act(() => void publishOverviewContext(SUGGESTIONS));
    waitOutTheTimer();

    // The lead names only what was actually found.
    expect(
      screen.getByText("Ask me about the cost spike, or unattributed spend."),
    ).toBeInTheDocument();
    // The detail under each prompt is the page's own sentence and figure.
    expect(screen.getByText(SUGGESTIONS[0].detail)).toBeInTheDocument();
    expect(screen.getByText("$2,480 is not tied to any feature.")).toBeInTheDocument();
  });

  it("hands the suggestion straight to the existing chat", () => {
    show();
    act(() => void publishOverviewContext(SUGGESTIONS));
    waitOutTheTimer();

    fireEvent.click(screen.getByRole("button", { name: /explain the cost spike/i }));
    expect(asks()).toEqual([{ question: SUGGESTIONS[0].question, source: "invitation" }]);
  });

  it("expands once per reader, not once per visit", () => {
    const first = show();
    act(() => void publishOverviewContext(SUGGESTIONS));
    waitOutTheTimer();
    expect(screen.getByText(/worth investigating/i)).toBeInTheDocument();
    expect(localStorage.getItem(INVITED_KEY)).toBe("1");
    first.unmount();

    show();
    act(() => void publishOverviewContext(SUGGESTIONS));
    waitOutTheTimer();
    expect(screen.queryByText(/worth investigating/i)).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Ask Meter" })).toBeInTheDocument();
  });

  it("falls back to the compact bubble when the offer is declined", () => {
    show();
    act(() => void publishOverviewContext(SUGGESTIONS));
    waitOutTheTimer();

    fireEvent.click(screen.getByRole("button", { name: /dismiss the ask meter suggestions/i }));
    expect(screen.queryByText(/worth investigating/i)).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Ask Meter" })).toBeInTheDocument();
    // Declining the offer is not dismissing the assistant.
    expect(localStorage.getItem(DISMISSED_KEY)).toBeNull();
  });

  it("is announced as a labelled region, with every prompt reachable", async () => {
    show();
    act(() => void publishOverviewContext(SUGGESTIONS));
    waitOutTheTimer();

    const region = screen.getByRole("region", { name: /worth investigating/i });
    expect(region).toBeInTheDocument();
    await waitFor(() =>
      expect(document.activeElement).toBe(screen.getByText(/worth investigating/i)),
    );
    expect(screen.getAllByRole("button", { name: /explain|investigate|dismiss/i })).toHaveLength(3);
  });

  it("anchors to the launcher instead of covering it", () => {
    const { container } = show();
    act(() => void publishOverviewContext(SUGGESTIONS));
    waitOutTheTimer();
    // Above the 56px launcher and its 24px inset, in the same corner: the
    // styles put it at bottom 96px / right 24px and draw the tail downwards.
    expect(container.querySelector(".ask-bubble")).toHaveClass("ask-bubble-expanded");
  });
});

describe("an inline Ask Meter", () => {
  it("carries the question for the thing it sits beside", () => {
    render(<AskAction source="insights" question="Which insight should I act on first?" />);
    fireEvent.click(screen.getByRole("button", { name: "Ask Meter" }));
    expect(asks()).toEqual([
      { question: "Which insight should I act on first?", source: "insights" },
    ]);
  });
});
