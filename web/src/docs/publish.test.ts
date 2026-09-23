/**
 * The public docs are generated, so the things that can go wrong are structural:
 * a link that goes nowhere, content that escapes into markup, a page that exists
 * in the navigation but not on disk. These check those against the real
 * handbook, not a fixture, wherever the real handbook is what is at risk.
 */
import { describe, expect, it } from "vitest";
import { code, note, p, type Block } from "../help/blocks";
import { CATEGORIES, type Category } from "../help/content";
import {
  DEFAULT_SITE,
  UnresolvableLink,
  escapeHtml,
  inline,
  publish,
  renderSite,
  resolveLink,
  type Page,
  type Site,
} from "./publish";

const SITE: Site = DEFAULT_SITE;
const byPath = (pages: Page[]) => new Map(pages.map((page) => [page.path, page.body]));

function fixture(blocks: Block[]): Category[] {
  return [
    {
      slug: "cat",
      title: "A category",
      blurb: "What it covers.",
      topics: [{ slug: "one", title: "One", summary: "The first topic.", blocks }],
    },
  ];
}

const render = (blocks: Block[]) => byPath(renderSite(fixture(blocks))).get("cat/one/index.html")!;

describe("link policy", () => {
  const topics = new Set(["concepts/confidence"]);

  it("sends an in-app route to the app, absolutely", () => {
    expect(resolveLink("/cost-sources", SITE, topics, "x")).toEqual({
      href: "https://meter.costlyinfra.com/cost-sources",
      kind: "app",
    });
  });

  it("keeps a handbook cross-link inside the docs", () => {
    expect(resolveLink("/help/concepts/confidence", SITE, topics, "x")).toEqual({
      href: "/docs/concepts/confidence/",
      kind: "docs",
    });
    expect(resolveLink("/help", SITE, topics, "x").href).toBe("/docs/");
  });

  it("leaves an external link alone", () => {
    expect(resolveLink("https://example.com/a", SITE, topics, "x")).toEqual({
      href: "https://example.com/a",
      kind: "external",
    });
  });

  it("refuses to publish a link to a route that does not exist", () => {
    expect(() => resolveLink("/invented", SITE, topics, "topic")).toThrow(UnresolvableLink);
  });

  it("refuses to publish a cross-link to a topic that does not exist", () => {
    expect(() => resolveLink("/help/concepts/invented", SITE, topics, "topic")).toThrow(
      UnresolvableLink,
    );
  });

  it("names the topic in the failure, so it can be found", () => {
    expect(() => resolveLink("/invented", SITE, topics, "concepts/confidence")).toThrow(
      /concepts\/confidence.*\/invented/,
    );
  });

  it("marks an app link as leaving the docs, in text as well as in glyph", () => {
    const html = inline("open [Connect sources](/cost-sources)", SITE, topics, "x");
    expect(html).toContain('href="https://meter.costlyinfra.com/cost-sources"');
    expect(html).toContain("in the Meter app");
  });

  it("opens an external link in a new tab, without leaking the referrer", () => {
    const html = inline("see [docs](https://example.com)", SITE, topics, "x");
    expect(html).toContain('rel="noreferrer"');
  });
});

describe("rendering", () => {
  it("escapes markup in prose", () => {
    expect(escapeHtml("<script>\"&'")).toBe("&lt;script&gt;&quot;&amp;&#39;");
    expect(render([p("a <b> & c")])).toContain("a &lt;b&gt; &amp; c");
  });

  it("escapes a code block rather than parsing it", () => {
    const html = render([code("if (a < b && c) { /* **not bold** */ }")]);
    expect(html).toContain("a &lt; b &amp;&amp; c");
    expect(html).not.toContain("<strong>");
  });

  it("renders the inline dialect the app renders", () => {
    const html = render([p("**bold** and `code`")]);
    expect(html).toContain("<strong>bold</strong>");
    expect(html).toContain("<code>code</code>");
  });

  it("keeps notes distinguishable from body text", () => {
    expect(render([note("careful")])).toContain('<aside class="note">careful</aside>');
  });

  it("ships no JavaScript — the pages have to read without it", () => {
    for (const page of publish()) {
      if (page.path.endsWith(".html")) expect(page.body, page.path).not.toContain("<script");
    }
  });
});

describe("the site", () => {
  // Rendered on demand, not at collection time: an unpublishable link throws,
  // and a throw here should fail a named test rather than the whole file.
  let cached: Page[] | null = null;
  const site = () => (cached ??= publish());

  it("publishes a page for every topic and every category", () => {
    const paths = new Set(site().map((page) => page.path));
    for (const category of CATEGORIES) {
      expect(paths, category.slug).toContain(`${category.slug}/index.html`);
      for (const topic of category.topics) {
        expect(paths, topic.slug).toContain(`${category.slug}/${topic.slug}/index.html`);
      }
    }
    expect(paths).toContain("index.html");
    expect(paths).toContain("docs.css");
  });

  it("publishes the whole handbook, with nothing held back silently", () => {
    const pages = site();
    const topics = CATEGORIES.reduce((n, c) => n + c.topics.length, 0);
    const published = pages.filter((page) => /^[^/]+\/[^/]+\/index\.html$/.test(page.path)).length;
    expect(published).toBe(topics);
  });

  it("resolves every link the real handbook contains", () => {
    // publish() throws on an unpublishable link, so reaching here is the
    // assertion — but state it, because that is the test's whole purpose.
    expect(() => publish()).not.toThrow();
  });

  it("links only to pages it actually generated", () => {
    const pages = site();
    const paths = new Set(pages.map((page) => page.path));
    const missing: string[] = [];
    for (const page of pages) {
      if (!page.path.endsWith(".html")) continue;
      for (const [, href] of page.body.matchAll(/href="([^"]+)"/g)) {
        if (!href.startsWith(`${SITE.base}/`)) continue;
        const rest = href.slice(SITE.base.length + 1);
        const file = rest === "" ? "index.html" : rest.endsWith("/") ? `${rest}index.html` : rest;
        if (!paths.has(file)) missing.push(`${page.path} -> ${href}`);
      }
    }
    expect(missing).toEqual([]);
  });

  it("gives every page a canonical URL and a description", () => {
    for (const page of site()) {
      if (!page.path.endsWith(".html")) continue;
      expect(page.body, page.path).toMatch(/<link rel="canonical" href="https:\/\/[^"]+">/);
      expect(page.body, page.path).toMatch(/<meta name="description" content="[^"]+">/);
    }
  });

  it("lists every page in the sitemap", () => {
    const pages = site();
    const sitemap = byPath(pages).get("sitemap.xml")!;
    const locs = [...sitemap.matchAll(/<loc>([^<]+)<\/loc>/g)].map(([, url]) => url);
    const htmlPages = pages.filter((page) => page.path.endsWith(".html")).length;
    expect(locs).toHaveLength(htmlPages);
    expect(locs).toContain("https://costlyinfra.com/docs/getting-started/what-meter-does/");
  });

  it("marks the page you are on in the contents", () => {
    const html = byPath(site()).get("concepts/confidence/index.html")!;
    expect(html).toContain('href="/docs/concepts/confidence/" aria-current="page"');
  });

  it("moves with the base path, so /docs is a choice and not a hard-coding", () => {
    const moved = byPath(renderSite(CATEGORIES, { ...SITE, base: "/handbook" }));
    const html = moved.get("concepts/confidence/index.html")!;
    expect(html).toContain('href="/handbook/');
    expect(html).not.toContain('href="/docs/');
  });
});
