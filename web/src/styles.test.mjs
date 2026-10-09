/**
 * Phone-width layout guards, read off the stylesheet itself.
 *
 * jsdom does no layout, so the only way to catch "the page slides sideways on a
 * phone" in a unit test is to assert the rules that stop it. Measured in a real
 * browser at 375px, `document.documentElement.scrollWidth` is 375 on / — it was
 * 917 on /features and 1,147 on / before these rules, because a row of badges,
 * pickers and actions that cannot wrap, or a wide table with no scroller of its
 * own, makes the page as wide as its widest piece.
 */
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";

// Plain JS, not TypeScript, on purpose: this reads a file off disk, and the web
// tsconfig deliberately has no Node types — app code has no business importing
// node:fs. Importing the stylesheet instead does not work either; vitest stubs
// stylesheets out to an empty string, `?raw` included. Vitest's cwd is web/.
const CSS = readFileSync(resolve(process.cwd(), "src/styles.css"), "utf8");

/** The declarations of the first rule with this exact selector. */
function block(selector) {
  const at = CSS.indexOf(`\n${selector} {`);
  expect(at, `no rule for ${selector}`).toBeGreaterThan(-1);
  return CSS.slice(at, CSS.indexOf("}", at));
}

describe("styles.css — nothing on Overview or Features may push the page sideways", () => {
  // Every flex row on /features and /features/:id that holds more than fits a
  // phone. Each one was measured overflowing 375px before it wrapped.
  it.each([
    ".discovery-bar", // org field + two buttons
    ".review-toolbar", // add-a-feature + merge
    ".feature-head", // name, badges, pickers, actions
    ".feature-actions", // the action cluster itself
    ".detail-meta", // status, badges, pickers, active users
    ".opt-item-main", // opportunity title + savings + actions
    ".evidence-item", // signal type, ref, actor, confidence, source
  ])("%s wraps rather than overflowing", (selector) => {
    expect(block(selector)).toContain("flex-wrap: wrap");
  });

  it(".tabs.tabs-wrap wraps rather than overflowing", () => {
    // The Overview tab row: five tabs, the product filter and the feature
    // search box. Measured pushing the page to 1,389px at a 1024px window.
    expect(block(".tabs.tabs-wrap")).toContain("flex-wrap: wrap");
  });

  it("lets the sidebar footer shrink to a phone", () => {
    // Every page: at flex: 0 0 auto the footer was 2px wider than 375px.
    const phone = CSS.slice(CSS.indexOf("@media (max-width: 720px) {"));
    const rule = phone.slice(phone.indexOf("  .sidebar-bottom {"));
    expect(rule.slice(0, rule.indexOf("}"))).toContain("min-width: 0");
  });

  it("keeps .features-table sharing the table look with .mini-table", () => {
    // Easy to break by hand: the two share one rule, and inserting anything
    // between the selector and the block silently strips every table on the
    // app of its border, radius and shadow.
    expect(CSS).toContain("\n.features-table,\n.mini-table {");
  });

  it("scrolls wide tables inside their own card", () => {
    // The table scrolls, not the page — same bargain the Alerts and Pricing
    // tables already make.
    expect(block(".mini-table-wrap")).toContain("overflow-x: auto");
    expect(block(".alerts-table-wrap")).toContain("overflow-x: auto");
    expect(block(".price-table-wrap")).toContain("overflow-x: auto");
  });
});
