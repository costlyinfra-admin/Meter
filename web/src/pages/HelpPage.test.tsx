import { fireEvent, render, screen, within } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { describe, expect, it } from "vitest";
import { CATEGORIES } from "../help/content";
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

  it("shows a topic's screenshot, described for those who cannot see it", () => {
    renderHelp("/help/getting-started/first-dashboard");
    const img = screen.getByRole("img", { name: /The Overview: headline figures/ });
    expect(img).toHaveAttribute("src", "/kb/overview.webp");
    expect(img).toHaveAttribute("loading", "lazy");
    // A click opens it at full size.
    expect(img.closest("a")).toHaveAttribute("href", "/kb/overview.webp");
    expect(screen.getByText("The Overview for the demo organization.")).toBeInTheDocument();
  });

  it("marks each heading with its category's icon, which screen readers skip", () => {
    renderHelp("/help/alerts/creating");
    const heading = screen.getByRole("heading", { name: "Creating an alert" });
    const mark = heading.querySelector(".kb-mark")!;
    expect(mark).toHaveAttribute("aria-hidden");
    expect(mark.querySelector("svg")).not.toBeNull();
    // Every chapter card on the front page carries its chapter's mark too.
    renderHelp();
    const marks = document.querySelectorAll(".kb-chapter .kb-mark");
    expect(marks.length).toBe(CATEGORIES.length);
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

  it("searches from the front page, and shows the results in place of the chapters", () => {
    renderHelp();
    fireEvent.change(screen.getByLabelText(/Search the knowledge base/i), {
      target: { value: "unattributed" },
    });
    const results = screen.getByRole("region", { name: "Search results" });
    expect(within(results).getByText(/topics?$/)).toBeInTheDocument();
    expect(within(results).getByText("The Unattributed bucket")).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Chapters" })).not.toBeInTheDocument();
  });

  it("searches from a topic, and replaces the contents with the results", () => {
    renderHelp("/help/concepts/confidence");
    fireEvent.change(screen.getByLabelText(/Search the knowledge base/i), {
      target: { value: "unattributed" },
    });
    const nav = screen.getByRole("navigation", { name: /contents/i });
    expect(within(nav).getByText(/topics?$/)).toBeInTheDocument();
    expect(within(nav).getByText("The Unattributed bucket")).toBeInTheDocument();
  });

  it("opens with a short path for a first read", () => {
    renderHelp();
    const start = screen.getByRole("heading", { name: "Start here" }).closest("section")!;
    const links = within(start).getAllByRole("link");
    expect(links.map((l) => l.getAttribute("href"))).toEqual([
      "/help/getting-started/what-meter-does",
      "/help/getting-started/setup",
      "/help/getting-started/first-dashboard",
    ]);
  });

  it("says how long a topic takes to read and where it sits in its chapter", () => {
    renderHelp("/help/concepts/unattributed");
    expect(screen.getByText(/\d+ min read/)).toBeInTheDocument();
    expect(screen.getByText("Topic 3 of 5")).toBeInTheDocument();
  });

  it("draws a diagram with a caption and a description a screen reader can reach", () => {
    renderHelp("/help/concepts/reconciliation");
    const art = screen.getByRole("img", { name: /authoritative on dollars/i });
    expect(art.closest("figure")!.querySelector("figcaption")).toHaveTextContent(
      "a lost event lowers attribution",
    );
  });

  it("renders *emphasis* as emphasis, never as asterisks", () => {
    renderHelp("/help/concepts/confidence");
    const article = document.querySelector(".kb-article")!;
    expect(article.querySelector("em")).toHaveTextContent("lowest");
    expect(article.textContent).not.toContain("*lowest*");
  });

  it("says so when nothing matches", () => {
    renderHelp();
    fireEvent.change(screen.getByLabelText(/Search the knowledge base/i), {
      target: { value: "zzzznothing" },
    });
    expect(screen.getByText("No topics match.")).toBeInTheDocument();
  });
});
