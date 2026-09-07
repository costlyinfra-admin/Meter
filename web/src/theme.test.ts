import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { choose, resolved, STORAGE_KEY, stored, systemTheme, watchSystem } from "./theme";

/** A controllable prefers-color-scheme, which jsdom does not provide. */
function mockSystem(dark: boolean) {
  const listeners: (() => void)[] = [];
  const query = {
    matches: dark,
    addEventListener: (_: string, fn: () => void) => listeners.push(fn),
    removeEventListener: (_: string, fn: () => void) => {
      const at = listeners.indexOf(fn);
      if (at >= 0) listeners.splice(at, 1);
    },
  };
  vi.stubGlobal("matchMedia", () => query);
  return {
    query,
    /** Pretend the OS just switched. */
    fire(nowDark: boolean) {
      query.matches = nowDark;
      listeners.forEach((fn) => fn());
    },
    get listenerCount() {
      return listeners.length;
    },
  };
}

beforeEach(() => {
  localStorage.clear();
  document.documentElement.removeAttribute("data-theme");
});

afterEach(() => vi.unstubAllGlobals());

describe("theme", () => {
  it("has no stored preference until one is chosen", () => {
    mockSystem(false);
    expect(stored()).toBeNull();
  });

  it("follows the operating system when nothing is stored", () => {
    mockSystem(true);
    expect(systemTheme()).toBe("dark");
    expect(resolved()).toBe("dark");
  });

  it("prefers an explicit choice over the operating system", () => {
    mockSystem(true);
    choose("light");
    expect(localStorage.getItem(STORAGE_KEY)).toBe("light");
    expect(resolved()).toBe("light");
    expect(document.documentElement.getAttribute("data-theme")).toBe("light");
  });

  it("hands control back to the system when the choice is cleared", () => {
    mockSystem(true);
    choose("light");
    expect(choose(null)).toBe("dark");
    expect(localStorage.getItem(STORAGE_KEY)).toBeNull();
    expect(document.documentElement.getAttribute("data-theme")).toBe("dark");
  });

  it("ignores a stored value that is not a theme", () => {
    mockSystem(false);
    localStorage.setItem(STORAGE_KEY, "chartreuse");
    expect(stored()).toBeNull();
    expect(resolved()).toBe("light");
  });

  it("switches with the machine while the user has not chosen", () => {
    const system = mockSystem(false);
    const seen: string[] = [];
    watchSystem((theme) => seen.push(theme));

    system.fire(true);
    expect(seen).toEqual(["dark"]);
    expect(document.documentElement.getAttribute("data-theme")).toBe("dark");
  });

  it("leaves a chosen theme alone when the machine switches", () => {
    // Someone who picked light at their desk does not want sunset to overrule it.
    const system = mockSystem(false);
    choose("light");
    const seen: string[] = [];
    watchSystem((theme) => seen.push(theme));

    system.fire(true);
    expect(seen).toEqual([]);
    expect(document.documentElement.getAttribute("data-theme")).toBe("light");
  });

  it("stops listening when unsubscribed", () => {
    const system = mockSystem(false);
    const stop = watchSystem(() => {});
    expect(system.listenerCount).toBe(1);
    stop();
    expect(system.listenerCount).toBe(0);
  });

  it("still themes the page when storage is unavailable", () => {
    mockSystem(false);
    // Replace the whole object rather than spying on its methods. Where jsdom
    // provides a real Storage it is a Proxy, and assigning `getItem` to it
    // writes a storage ENTRY of that name instead of shadowing the method — so
    // a spy silently does nothing and the test passes for the wrong reason.
    const real = Object.getOwnPropertyDescriptor(window, "localStorage");
    const blocked = () => {
      throw new Error("blocked");
    };
    Object.defineProperty(window, "localStorage", {
      configurable: true,
      value: { getItem: blocked, setItem: blocked, removeItem: blocked },
    });

    try {
      expect(choose("dark")).toBe("dark");
      expect(document.documentElement.getAttribute("data-theme")).toBe("dark");
      expect(stored()).toBeNull();
    } finally {
      if (real) Object.defineProperty(window, "localStorage", real);
    }
  });
});
