import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ThemeToggle } from "./ThemeToggle";
import { STORAGE_KEY } from "../theme";

beforeEach(() => {
  localStorage.clear();
  document.documentElement.removeAttribute("data-theme");
  vi.stubGlobal("matchMedia", () => ({
    matches: false,
    addEventListener: () => {},
    removeEventListener: () => {},
  }));
});

afterEach(() => vi.unstubAllGlobals());

describe("ThemeToggle", () => {
  it("offers the theme you are not in", () => {
    render(<ThemeToggle />);
    expect(screen.getByRole("button", { name: /switch to dark mode/i })).toBeInTheDocument();
  });

  it("switches the document and remembers the choice", () => {
    render(<ThemeToggle />);
    fireEvent.click(screen.getByRole("button", { name: /switch to dark mode/i }));

    expect(document.documentElement.getAttribute("data-theme")).toBe("dark");
    expect(localStorage.getItem(STORAGE_KEY)).toBe("dark");
    // And now offers the way back.
    expect(screen.getByRole("button", { name: /switch to light mode/i })).toBeInTheDocument();
  });

  it("opens in the theme already stored", () => {
    localStorage.setItem(STORAGE_KEY, "dark");
    render(<ThemeToggle />);
    expect(screen.getByRole("button", { name: /switch to light mode/i })).toBeInTheDocument();
    expect(document.documentElement.getAttribute("data-theme")).toBe("dark");
  });

  it("reveals the new theme in a circle from the toggle, where the browser can", () => {
    const startViewTransition = vi.fn((update: () => void) => update());
    Object.assign(document, { startViewTransition });
    try {
      render(<ThemeToggle />);
      fireEvent.click(screen.getByRole("button", { name: /switch to dark mode/i }));
      expect(startViewTransition).toHaveBeenCalledTimes(1);
      // The theme still changes, inside the transition.
      expect(document.documentElement.getAttribute("data-theme")).toBe("dark");
      // And the circle knows where the toggle is.
      expect(document.documentElement.style.getPropertyValue("--reveal-r")).toMatch(/px$/);
    } finally {
      delete (document as { startViewTransition?: unknown }).startViewTransition;
    }
  });

  it("switches at once for anyone who asked for reduced motion", () => {
    const startViewTransition = vi.fn((update: () => void) => update());
    Object.assign(document, { startViewTransition });
    vi.stubGlobal("matchMedia", (query: string) => ({
      matches: query.includes("reduced-motion"),
      addEventListener: () => {},
      removeEventListener: () => {},
    }));
    try {
      render(<ThemeToggle />);
      fireEvent.click(screen.getByRole("button", { name: /switch to dark mode/i }));
      expect(startViewTransition).not.toHaveBeenCalled();
      expect(document.documentElement.getAttribute("data-theme")).toBe("dark");
    } finally {
      delete (document as { startViewTransition?: unknown }).startViewTransition;
    }
  });
});
