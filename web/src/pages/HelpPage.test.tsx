import { fireEvent, render, screen, within } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { describe, expect, it } from "vitest";
import { HelpPage } from "./HelpPage";

function renderHelp(path = "/help") {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <Routes>
        <Route path="/help" element={<HelpPage />} />
        <Route path="/help/:category/:topic" element={<HelpPage />} />
      </Routes>
    </MemoryRouter>,
  );
}

describe("HelpPage", () => {
  it("opens on a table of contents covering every category", () => {
    renderHelp();
    expect(screen.getByRole("heading", { name: /Knowledge base/i })).toBeInTheDocument();
    for (const category of ["Getting started", "Core concepts", "Troubleshooting"]) {
      expect(screen.getAllByText(category).length).toBeGreaterThan(0);
    }
  });

  it("renders a topic when one is addressed directly", () => {
    renderHelp("/help/concepts/unattributed");
    expect(screen.getByRole("heading", { name: "The Unattributed bucket" })).toBeInTheDocument();
    expect(screen.getByText(/Where honest gaps go/)).toBeInTheDocument();
  });

  it("renders inline formatting as elements, not raw markup", () => {
    renderHelp("/help/concepts/build-vs-inference");
    const article = document.querySelector(".kb-article")!;
    expect(article.querySelector("strong")).not.toBeNull();
    expect(article.textContent).not.toContain("**"); // the syntax never reaches the reader
  });

  it("turns an in-app reference into a real link", () => {
    renderHelp("/help/getting-started/setup");
    const link = screen.getAllByRole("link", { name: "Features" })[0];
    expect(link).toHaveAttribute("href", "/features");
  });

  it("marks the current topic in the contents", () => {
    renderHelp("/help/concepts/confidence");
    const nav = screen.getByRole("navigation", { name: /contents/i });
    expect(within(nav).getByRole("link", { current: "page" })).toHaveTextContent("Confidence");
  });

  it("reads straight through: every topic offers the next one", () => {
    renderHelp("/help/getting-started/what-meter-does");
    expect(screen.getByText(/Setting up →/)).toBeInTheDocument();
  });

  it("searches, and replaces the contents with the results", () => {
    renderHelp();
    fireEvent.change(screen.getByLabelText(/Search the knowledge base/i), {
      target: { value: "unattributed" },
    });
    const nav = screen.getByRole("navigation", { name: /contents/i });
    expect(within(nav).getByText(/topics?$/)).toBeInTheDocument();
    expect(within(nav).getByText("The Unattributed bucket")).toBeInTheDocument();
  });

  it("says so when nothing matches", () => {
    renderHelp();
    fireEvent.change(screen.getByLabelText(/Search the knowledge base/i), {
      target: { value: "zzzznothing" },
    });
    expect(screen.getByText("No topics match.")).toBeInTheDocument();
  });
});
