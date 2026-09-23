/**
 * The knowledge base is content, so these tests guard the things that silently
 * rot: dead in-app links, duplicate URLs, and search that stops finding things.
 */
import { describe, expect, it } from "vitest";
import { blockText } from "./blocks";
import { ALL_TOPICS, CATEGORIES, findTopic } from "./content";
import { APP_ROUTES } from "./routes";
import { search } from "./search";
import { MIN_SDK } from "../pages/installPrompt";

// The SDKs as text, so a snippet that no longer compiles fails here rather than
// in a reader's terminal. The handbook shipped `from costlyinfra_meter import
// wrap` and `import { wrap }` for a while; neither has worked since the v2
// rewrite, and nothing noticed.
import PYTHON_SDK from "../../../sdk/python/costlyinfra_meter/__init__.py?raw";
import NODE_SDK from "../../../sdk/node/index.mjs?raw";

const LINKS = /\[[^\]]+\]\(([^)]+)\)/g;

describe("knowledge base content", () => {
  it("has unique slugs, so no topic shadows another", () => {
    const urls = ALL_TOPICS.map(({ category, topic }) => `${category.slug}/${topic.slug}`);
    expect(new Set(urls).size).toBe(urls.length);
    expect(new Set(CATEGORIES.map((c) => c.slug)).size).toBe(CATEGORIES.length);
  });

  it("links only to routes the app really has", () => {
    // A help system that sends people to a 404 is worse than no help system.
    const bad: string[] = [];
    for (const { category, topic } of ALL_TOPICS) {
      const text = [topic.summary, ...topic.blocks.map(blockText)].join(" ");
      for (const [, href] of text.matchAll(LINKS)) {
        if (!href.startsWith("/")) continue; // external links are not ours to check
        const ok = APP_ROUTES.has(href) || (href.startsWith("/help/") && findTopic(...split(href)));
        if (!ok) bad.push(`${category.slug}/${topic.slug} -> ${href}`);
      }
    }
    expect(bad).toEqual([]);
  });

  it("gives every topic a title, a summary and some body", () => {
    for (const { category, topic } of ALL_TOPICS) {
      const where = `${category.slug}/${topic.slug}`;
      expect(topic.title, where).toBeTruthy();
      expect(topic.summary, where).toBeTruthy();
      expect(topic.blocks.length, where).toBeGreaterThan(0);
    }
  });

  it("covers every area of the product", () => {
    // A reader should be able to look up anything the sidebar offers.
    const all = ALL_TOPICS.map(({ topic }) =>
      [topic.title, topic.summary, ...topic.blocks.map(blockText)].join(" "),
    ).join(" ");
    for (const subject of ["Unattributed", "confidence", "SDK", "alert", "discovery", "customer"]) {
      expect(all.toLowerCase(), subject).toContain(subject.toLowerCase());
    }
  });
});

function split(href: string): [string, string] {
  const [, , category, topic] = href.split("/");
  return [category, topic];
}

describe("knowledge base search", () => {
  it("ignores queries too short to mean anything", () => {
    expect(search("")).toEqual([]);
    expect(search("a")).toEqual([]);
  });

  it("ranks a title match above a passing mention", () => {
    const hits = search("unattributed");
    expect(hits.length).toBeGreaterThan(1); // the term appears in several topics
    expect(hits[0].topic.title).toMatch(/Unattributed/i); // ...but the topic ABOUT it wins
  });

  it("finds topics by their body, not just their title", () => {
    const hits = search("webhook");
    expect(hits.some((h) => h.topic.slug === "delivery")).toBe(true);
  });

  it("returns a readable snippet with each hit", () => {
    const hits = search("cache");
    expect(hits.length).toBeGreaterThan(0);
    for (const hit of hits) {
      expect(hit.snippet.length).toBeGreaterThan(0);
      // A reader should never see the inline syntax in a result.
      expect(hit.snippet).not.toContain("**");
      expect(hit.snippet).not.toMatch(/\]\(\//);
    }
  });

  it("finds nothing for a term the book does not cover", () => {
    expect(search("kubernetes helm chart")).toEqual([]);
  });
});

describe("invoice reconciliation topics", () => {
  const category = CATEGORIES.find((c) => c.slug === "reconciliation")!;

  it("is documented as its own section", () => {
    expect(category).toBeDefined();
    expect(category.topics.length).toBeGreaterThan(4);
  });

  it("tells the two reconciliations apart, in both directions", () => {
    // "Reconciliation" names two different things in this product. A reader
    // who lands on either one must be pointed at the other.
    const internal = findTopic("concepts", "reconciliation")!;
    const invoice = findTopic("reconciliation", "what-it-is")!;
    expect(text(internal.topic)).toContain("/help/reconciliation/what-it-is");
    expect(text(invoice.topic)).toContain("/help/concepts/reconciliation");
  });

  it("states the promises the module actually makes", () => {
    const body = category.topics.map(text).join(" ").toLowerCase();
    expect(body).toContain("never changes your cost data");
    expect(body).toContain("never converts currencies");
    expect(body).toContain("opt-in");
    // The tolerance defaults, which a reader will act on.
    expect(body).toContain("$1.00");
    expect(body).toContain("0.5%");
  });

  it("documents every status the module can end in", () => {
    const body = category.topics.map(text).join(" ");
    for (const status of [
      "Matched",
      "Within tolerance",
      "Discrepancy",
      "Incomplete data",
      "Failed",
    ]) {
      expect(body).toContain(status);
    }
  });

  it("documents every confidence level, since the distinction is the point", () => {
    const body = category.topics.map(text).join(" ");
    for (const level of ["Confirmed", "Possible", "Unknown"]) {
      expect(body).toContain(level);
    }
  });
});

/** A topic's prose, with the inline syntax left in — these assertions are
 *  about links as much as words. */
function text(topic: { blocks: { kind: string }[] }): string {
  return JSON.stringify(topic.blocks);
}

describe("the SDK snippets", () => {
  const snippets = findTopic("sdk", "installing")!
    .topic.blocks.filter((b) => b.kind === "code")
    .map((b) => (b.kind === "code" ? b.text : ""));
  const python = snippets.find((s) => s.startsWith("# Python"))!;
  const node = snippets.find((s) => s.startsWith("// Node"))!;

  it("imports only names the Python package exports", () => {
    const exported = /__all__ = \[([^\]]+)\]/.exec(PYTHON_SDK)![1];
    for (const [, names] of python.matchAll(/from costlyinfra_meter import (.+)/g)) {
      for (const name of names.split(",").map((n) => n.trim())) {
        expect(exported, name).toContain(`"${name}"`);
      }
    }
  });

  it("imports only names the Node package exports", () => {
    for (const [, names] of node.matchAll(/import \{([^}]+)\} from "costlyinfra-meter"/g)) {
      for (const name of names.split(",").map((n) => n.trim())) {
        expect(NODE_SDK, name).toMatch(new RegExp(`export (class|function|const) ${name}\\b`));
      }
    }
  });

  it("wraps the way the SDK supports — through a Meter, in both languages", () => {
    // Python also has a module-level wrap(), but it takes the meter as its
    // second argument, so `wrap(Anthropic(), feature_id=...)` raises TypeError.
    // Node has no module-level wrap at all. The method is the documented path
    // because it is the one that exists in both.
    expect(python).toContain("meter = Meter()");
    expect(python).toMatch(/meter\.wrap\(/);
    expect(node).toContain("new Meter()");
    expect(node).toMatch(/meter\.wrap\(/);
    expect(PYTHON_SDK).toMatch(/def wrap\(self, client/);
    expect(NODE_SDK).toMatch(/\n {2}wrap\(client/);
  });

  it("asks for the version the rest of the product asks for", () => {
    const note = findTopic("sdk", "installing")!.topic.blocks.find((b) => b.kind === "note")!;
    expect(note.kind === "note" && note.text).toContain(`costlyinfra-meter>=${MIN_SDK}`);
  });
});
