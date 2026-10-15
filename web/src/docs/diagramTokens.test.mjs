/**
 * The diagrams' stylesheet (help/figures.css) is read by two pages: the app,
 * whose tokens live in styles.css, and the public docs, whose tokens live in
 * docs/publish.ts. A colour the diagrams use that either side does not define
 * renders as nothing — a missing fill is a missing shape, silently — so every
 * token the diagrams reach for must exist in both, in both themes.
 *
 * Plain JS reading files off disk, like styles.test.mjs: vitest stubs imported
 * stylesheets to an empty string, `?raw` included. Vitest's cwd is web/.
 */
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";

const read = (path) => readFileSync(resolve(process.cwd(), path), "utf8");
const DIAGRAMS = read("src/help/figures.css");
const APP = read("src/styles.css");
const DOCS = read("src/docs/publish.ts");

const used = [...new Set([...DIAGRAMS.matchAll(/var\((--[a-z0-9-]+)/g)].map((m) => m[1]))];

/** The custom properties a block of CSS declares. */
const declared = (css) => new Set([...css.matchAll(/(--[a-z0-9-]+):/g)].map((m) => m[1]));

/** The text of the first rule opened by `opener`, up to its closing brace. */
function rule(css, opener) {
  const at = css.indexOf(opener);
  expect(at, `no ${opener}`).toBeGreaterThan(-1);
  return css.slice(at, css.indexOf("\n}", at));
}

describe("the diagrams' tokens", () => {
  it("are found at all — the scan is reading the right file", () => {
    expect(used).toContain("--chart-1");
    expect(used.length).toBeGreaterThan(15);
  });

  it.each(used)("%s is defined by the app", (token) => {
    expect(declared(APP).has(token)).toBe(true);
  });

  it.each(used)("%s is defined by the public docs, in daylight", (token) => {
    expect(declared(rule(DOCS, "const STYLESHEET = `:root {")).has(token)).toBe(true);
  });

  it("change colour with the docs' dark theme wherever the app's do", () => {
    const appDark = declared(rule(APP, ':root[data-theme="dark"] {'));
    const docsDark = declared(rule(DOCS, "@media (prefers-color-scheme: dark) {"));
    for (const token of used) {
      if (appDark.has(token)) expect(docsDark.has(token), token).toBe(true);
    }
  });
});
