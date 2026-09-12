import "@testing-library/jest-dom/vitest";
import { configure } from "@testing-library/react";

// Testing Library waits one second by default before deciding an element is
// never going to appear. That is plenty on a developer's machine — this suite's
// biggest spec runs 36 tests in about half a second — but CI runs every file in
// parallel on a slower box, where a render can lose that race and fail a test
// that has nothing wrong with it.
//
// This only changes how long a FAILING assertion waits before giving up: one
// that is going to pass still resolves in milliseconds, so the green path costs
// nothing. A genuinely missing element takes longer to report, which is the
// right trade against a suite that goes red for the wrong reason.
configure({ asyncUtilTimeout: 5000 });

// jsdom has no layout, so it implements no scrolling. Components that keep a
// view pinned to the newest content call this; stub it rather than making the
// components defensive about a gap that only exists in tests.
Element.prototype.scrollIntoView = Element.prototype.scrollIntoView ?? (() => {});

// The jsdom build here exposes `localStorage` as a bare object with none of the
// Storage methods on it (`sessionStorage` is real; this one is not). Anything
// that remembers a preference needs a working one, so install a minimal
// in-memory Storage when the environment has not provided a real one.
if (typeof window.localStorage?.getItem !== "function") {
  const store = new Map<string, string>();
  Object.defineProperty(window, "localStorage", {
    configurable: true,
    value: {
      getItem: (key: string) => store.get(key) ?? null,
      setItem: (key: string, value: string) => void store.set(key, String(value)),
      removeItem: (key: string) => void store.delete(key),
      clear: () => store.clear(),
      key: (index: number) => [...store.keys()][index] ?? null,
      get length() {
        return store.size;
      },
    },
  });
}
