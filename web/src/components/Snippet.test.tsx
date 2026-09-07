import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { Snippet } from "./Snippet";

const writeText = vi.fn();

beforeEach(() => {
  writeText.mockReset().mockResolvedValue(undefined);
  Object.defineProperty(navigator, "clipboard", { value: { writeText }, configurable: true });
});

afterEach(() => vi.useRealTimers());

describe("Snippet", () => {
  it("shows the code and copies exactly what is shown", async () => {
    render(<Snippet>{"npm install costlyinfra-meter"}</Snippet>);
    expect(screen.getByText("npm install costlyinfra-meter")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Copy" }));
    await waitFor(() => expect(writeText).toHaveBeenCalledWith("npm install costlyinfra-meter"));
    await screen.findByText("Copied");
  });

  it("confirms the copy, by name as well as on screen", async () => {
    render(<Snippet>{"x"}</Snippet>);
    fireEvent.click(screen.getByRole("button", { name: "Copy" }));
    // Named "Copied" too: a screen reader gets the same feedback as an eye does.
    expect(await screen.findByRole("button", { name: "Copied" })).toHaveTextContent("Copied");
  });

  it("names what it copies when a page has several blocks", async () => {
    render(<Snippet copyLabel="Copy prompt">{"a prompt"}</Snippet>);
    fireEvent.click(screen.getByRole("button", { name: "Copy prompt" }));
    await waitFor(() => expect(writeText).toHaveBeenCalledWith("a prompt"));
    await screen.findByText("Copied");
  });

  it("says so when the clipboard is unavailable, instead of doing nothing", async () => {
    // Plain HTTP, or a permissions policy that refuses — both are real.
    writeText.mockRejectedValue(new Error("denied"));
    render(<Snippet>{"x"}</Snippet>);

    fireEvent.click(screen.getByRole("button", { name: "Copy" }));
    expect(await screen.findByText("Select and copy")).toBeInTheDocument();
  });

  it("returns to offering a copy after a moment", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    render(<Snippet>{"x"}</Snippet>);
    fireEvent.click(screen.getByRole("button", { name: "Copy" }));
    await screen.findByText("Copied");

    act(() => vi.advanceTimersByTime(2100));
    expect(screen.getByRole("button", { name: "Copy" })).toBeInTheDocument();
  });

  it("keeps the caller's own class on the block", () => {
    render(<Snippet className="agent-prompt">{"x"}</Snippet>);
    expect(document.querySelector("pre")).toHaveClass("snippet", "agent-prompt");
  });
});
