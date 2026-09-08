import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { Snippet } from "../components/Snippet";

/**
 * Session replay records the DOM. `defaultPrivacyLevel: "mask-user-input"` masks
 * what a user types, not what the page renders — so anything sensitive shown as
 * TEXT has to be masked at the element. These are the two places that do.
 */
describe("session-replay masking", () => {
  it("marks a secret-bearing snippet, and leaves ordinary ones alone", () => {
    const { container, rerender } = render(<Snippet>npm install costlyinfra-meter</Snippet>);
    expect(container.querySelector("[data-dd-privacy]")).toBeNull();

    rerender(<Snippet sensitive>METER_INGEST_TOKEN=secret-value</Snippet>);
    const wrap = container.querySelector(".snippet-wrap")!;
    expect(wrap.getAttribute("data-dd-privacy")).toBe("mask");
    expect(screen.getByText(/secret-value/)).toBeInTheDocument();
  });
});
