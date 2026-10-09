import { render } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { BrandMark } from "./BrandMark";

beforeEach(() => sessionStorage.clear());
afterEach(() => vi.unstubAllGlobals());

describe("BrandMark", () => {
  it("settles its bars on the first mark of a visit, then never again that visit", () => {
    const first = render(<BrandMark />).container.querySelector("svg")!;
    expect(first).toHaveClass("settle");
    expect(first.querySelectorAll(".brand-bar")).toHaveLength(3);

    const later = render(<BrandMark />).container.querySelector("svg")!;
    expect(later).not.toHaveClass("settle");
    expect(later).toHaveClass("brand-mark");
  });

  it("is simply still where the browser keeps nothing", () => {
    vi.stubGlobal("sessionStorage", {
      getItem: () => {
        throw new Error("blocked");
      },
      setItem: () => {
        throw new Error("blocked");
      },
    });
    const mark = render(<BrandMark />).container.querySelector("svg")!;
    expect(mark).not.toHaveClass("settle");
    expect(mark.querySelectorAll(".brand-bar")).toHaveLength(3);
  });
});
