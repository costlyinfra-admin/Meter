/**
 * The public documentation site, generated from the in-app handbook.
 *
 * There is one source of truth for Meter's documentation — `help/content.ts` —
 * and three readers of it: the Knowledge Base screen, the Ask Meter assistant,
 * and this. Nothing here reaches back into the handbook to change it, so adding
 * public docs cannot break either of the other two: a topic is written once and
 * this module decides how it reads to someone who is not logged in.
 *
 * Output is plain static HTML with no JavaScript. Public documentation has to be
 * readable by crawlers — search engines and, increasingly, the assistants people
 * ask about tools before they try them — and a client-rendered page serves those
 * an empty shell.
 *
 * The one thing that genuinely differs from the in-app reading is links. In the
 * app, "open [Connect sources](/cost-sources)" is a router link one click away.
 * On the public site it is a destination behind a login, so it is rewritten to an
 * absolute app URL and marked as such. An unhandled link throws rather than
 * quietly emitting a dead one — see resolveLink.
 *
 * Rendering is pure: it takes content and returns files. Writing them to disk is
 * scripts/build-docs.mjs, so everything here is testable without a filesystem.
 */
import type { Block } from "../help/blocks";
import { CATEGORIES, type Category, type Topic } from "../help/content";
import { APP_ROUTES } from "../help/routes";

/** Where the generated site will live. */
export interface Site {
  /** Path the docs are served under, no trailing slash. */
  base: string;
  /** Public origin, for canonical URLs and the sitemap. */
  origin: string;
  /** The app's origin, where in-app links point. */
  app: string;
}

export const DEFAULT_SITE: Site = {
  base: "/docs",
  origin: "https://costlyinfra.com",
  app: "https://meter.costlyinfra.com",
};

/** One generated file: a path relative to the output directory, and its bytes. */
export interface Page {
  path: string;
  body: string;
}

/** A link in the handbook that the public site cannot resolve. Fatal on purpose:
 *  silently emitting a dead link is how a docs site rots. */
export class UnresolvableLink extends Error {
  constructor(href: string, where: string) {
    super(`${where}: cannot publish link to ${href}`);
    this.name = "UnresolvableLink";
  }
}

// ---------------------------------------------------------------------------
// Inline syntax — the same tiny dialect render.tsx understands
// ---------------------------------------------------------------------------
const INLINE = /(\*\*[^*]+\*\*|`[^`]+`|\[[^\]]+\]\([^)]+\))/g;

export function escapeHtml(text: string): string {
  return text
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

export type LinkKind = "docs" | "app" | "external";

export interface ResolvedLink {
  href: string;
  kind: LinkKind;
}

/**
 * Where a handbook link points on the public site.
 *
 * `/help/x/y` becomes the published topic, `/help` the docs home, any other app
 * route becomes an absolute URL into the app, and an external URL is left alone.
 * Anything else is a bug in the content and stops the build.
 */
export function resolveLink(
  href: string,
  site: Site,
  topics: Set<string>,
  where: string,
): ResolvedLink {
  if (href === "/help") return { href: `${site.base}/`, kind: "docs" };
  if (href.startsWith("/help/")) {
    const [, , category, topic] = href.split("/");
    const id = `${category}/${topic}`;
    if (!topics.has(id)) throw new UnresolvableLink(href, where);
    return { href: `${site.base}/${id}/`, kind: "docs" };
  }
  if (APP_ROUTES.has(href)) return { href: `${site.app}${href}`, kind: "app" };
  if (/^https?:\/\//.test(href)) return { href, kind: "external" };
  throw new UnresolvableLink(href, where);
}

/** Render **bold**, `code` and [label](/route) to HTML, escaping everything else. */
export function inline(text: string, site: Site, topics: Set<string>, where: string): string {
  return text
    .split(INLINE)
    .map((part) => {
      if (!part) return "";
      if (part.startsWith("**") && part.endsWith("**")) {
        return `<strong>${escapeHtml(part.slice(2, -2))}</strong>`;
      }
      if (part.startsWith("`") && part.endsWith("`")) {
        return `<code>${escapeHtml(part.slice(1, -1))}</code>`;
      }
      const link = /^\[([^\]]+)\]\(([^)]+)\)$/.exec(part);
      if (!link) return escapeHtml(part);
      const [, label, href] = link;
      const { href: to, kind } = resolveLink(href, site, topics, where);
      if (kind === "app") {
        // Named as what it is, so nobody clicks expecting more documentation and
        // lands on a sign-in screen instead.
        return `<a class="applink" href="${escapeHtml(to)}">${escapeHtml(label)}<span class="applink-mark" aria-hidden="true">↗</span><span class="visually-hidden"> — in the Meter app</span></a>`;
      }
      if (kind === "external") {
        return `<a href="${escapeHtml(to)}" target="_blank" rel="noreferrer">${escapeHtml(label)}</a>`;
      }
      return `<a href="${escapeHtml(to)}">${escapeHtml(label)}</a>`;
    })
    .join("");
}

/** One content block as HTML. Mirrors render.tsx block for block. */
export function block(b: Block, site: Site, topics: Set<string>, where: string): string {
  const md = (text: string) => inline(text, site, topics, where);
  switch (b.kind) {
    case "p":
      return `<p>${md(b.text)}</p>`;
    case "list":
      return `<ul>\n${b.items.map((i) => `<li>${md(i)}</li>`).join("\n")}\n</ul>`;
    case "steps":
      return `<ol>\n${b.items.map((i) => `<li>${md(i)}</li>`).join("\n")}\n</ol>`;
    case "code":
      // Code is never inline-parsed: a snippet containing ** or backticks is a
      // snippet, not emphasis.
      return `<pre><code>${escapeHtml(b.text)}</code></pre>`;
    case "note":
      return `<aside class="note">${md(b.text)}</aside>`;
    case "table":
      return [
        `<div class="table-wrap">`,
        `<table>`,
        `<thead><tr>${b.head.map((h) => `<th>${md(h)}</th>`).join("")}</tr></thead>`,
        `<tbody>`,
        ...b.rows.map((row) => `<tr>${row.map((c) => `<td>${md(c)}</td>`).join("")}</tr>`),
        `</tbody>`,
        `</table>`,
        `</div>`,
      ].join("\n");
  }
}

// ---------------------------------------------------------------------------
// Pages
// ---------------------------------------------------------------------------
interface Entry {
  category: Category;
  topic: Topic;
  /** `category/topic` — the handbook's own identifier for a topic. */
  id: string;
  url: string;
}

function entries(categories: Category[], site: Site): Entry[] {
  return categories.flatMap((category) =>
    category.topics.map((topic) => ({
      category,
      topic,
      id: `${category.slug}/${topic.slug}`,
      url: `${site.base}/${category.slug}/${topic.slug}/`,
    })),
  );
}

/** The contents list, repeated on every page: 50-odd topics is small enough to
 *  show whole, and a reader who can see the whole book can navigate it without
 *  a search box. */
function nav(categories: Category[], site: Site, current: string | null): string {
  const sections = categories.map((category) => {
    const items = category.topics
      .map((topic) => {
        const id = `${category.slug}/${topic.slug}`;
        const here = id === current;
        return `<li><a href="${site.base}/${id}/"${here ? ' aria-current="page"' : ""}>${escapeHtml(topic.title)}</a></li>`;
      })
      .join("\n");
    return [
      `<li class="nav-section">`,
      `<a class="nav-category" href="${site.base}/${category.slug}/">${escapeHtml(category.title)}</a>`,
      `<ul>`,
      items,
      `</ul>`,
      `</li>`,
    ].join("\n");
  });
  return `<nav class="toc" aria-label="Documentation">\n<h2>All topics</h2>\n<ul>\n${sections.join("\n")}\n</ul>\n</nav>`;
}

interface Shell {
  /** Becomes the <title>, with the site name appended unless this is the home page. */
  title: string;
  description: string;
  /** URL path this page will be served at, used for the canonical link. */
  path: string;
  /** `category/topic` of the topic being read, for the contents list. */
  current: string | null;
  main: string;
}

function shell(categories: Category[], site: Site, page: Shell): string {
  const canonical = `${site.origin}${page.path}`;
  const home = page.path === `${site.base}/`;
  const title = home ? page.title : `${page.title} · Meter docs`;
  return `<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>${escapeHtml(title)}</title>
<meta name="description" content="${escapeHtml(page.description)}">
<link rel="canonical" href="${escapeHtml(canonical)}">
<meta property="og:site_name" content="Meter">
<meta property="og:type" content="${home ? "website" : "article"}">
<meta property="og:title" content="${escapeHtml(title)}">
<meta property="og:description" content="${escapeHtml(page.description)}">
<meta property="og:url" content="${escapeHtml(canonical)}">
<meta name="twitter:card" content="summary">
<link rel="stylesheet" href="${site.base}/docs.css">
</head>
<body>
<a class="visually-hidden skip" href="#main">Skip to content</a>
<header class="site-head">
<a class="wordmark" href="${escapeHtml(site.origin)}/">Meter</a>
<nav class="site-nav" aria-label="Site">
<a href="${site.base}/">Docs</a>
<a href="${escapeHtml(site.app)}/">Sign in</a>
</nav>
</header>
<div class="layout">
<main id="main">
${page.main}
</main>
${nav(categories, site, page.current)}
</div>
<footer class="site-foot">
<p>These pages are generated from the handbook built into Meter, so what you read here is what the product ships.</p>
<p><a href="${escapeHtml(site.origin)}/">costlyinfra.com</a> · <a href="${escapeHtml(site.app)}/">Open Meter</a></p>
</footer>
</body>
</html>
`;
}

function home(categories: Category[], site: Site): string {
  const cards = categories
    .map((category) =>
      [
        `<section class="card">`,
        `<h2><a href="${site.base}/${category.slug}/">${escapeHtml(category.title)}</a></h2>`,
        `<p class="blurb">${escapeHtml(category.blurb)}</p>`,
        `<ul class="plain">`,
        ...category.topics.map(
          (topic) =>
            `<li><a href="${site.base}/${category.slug}/${topic.slug}/">${escapeHtml(topic.title)}</a></li>`,
        ),
        `</ul>`,
        `</section>`,
      ].join("\n"),
    )
    .join("\n");
  return [
    `<div class="lede">`,
    `<h1>Meter documentation</h1>`,
    `<p>How Meter turns a blended AI bill into cost per feature — what it reads, how it attributes spend, and what every number on a screen means.</p>`,
    `</div>`,
    `<div class="cards">`,
    cards,
    `</div>`,
  ].join("\n");
}

function categoryPage(category: Category, site: Site): string {
  return [
    `<nav class="crumbs" aria-label="Breadcrumb"><a href="${site.base}/">Docs</a> <span aria-hidden="true">/</span> <span>${escapeHtml(category.title)}</span></nav>`,
    `<h1>${escapeHtml(category.title)}</h1>`,
    `<p class="blurb">${escapeHtml(category.blurb)}</p>`,
    `<ul class="topic-list">`,
    ...category.topics.map((topic) =>
      [
        `<li>`,
        `<a href="${site.base}/${category.slug}/${topic.slug}/">${escapeHtml(topic.title)}</a>`,
        `<span>${escapeHtml(topic.summary)}</span>`,
        `</li>`,
      ].join(""),
    ),
    `</ul>`,
  ].join("\n");
}

function topicPage(
  entry: Entry,
  site: Site,
  topics: Set<string>,
  prev: Entry | undefined,
  next: Entry | undefined,
): string {
  const { category, topic } = entry;
  const body = topic.blocks.map((b) => block(b, site, topics, entry.id)).join("\n");
  const around = [
    prev
      ? `<a class="prev" href="${prev.url}"><span>Previous</span>${escapeHtml(prev.topic.title)}</a>`
      : "",
    next
      ? `<a class="next" href="${next.url}"><span>Next</span>${escapeHtml(next.topic.title)}</a>`
      : "",
  ].join("\n");
  return [
    `<nav class="crumbs" aria-label="Breadcrumb"><a href="${site.base}/">Docs</a> <span aria-hidden="true">/</span> <a href="${site.base}/${category.slug}/">${escapeHtml(category.title)}</a> <span aria-hidden="true">/</span> <span>${escapeHtml(topic.title)}</span></nav>`,
    `<article>`,
    `<h1>${escapeHtml(topic.title)}</h1>`,
    `<p class="summary">${escapeHtml(topic.summary)}</p>`,
    body,
    `</article>`,
    `<nav class="around" aria-label="More topics">`,
    around,
    `</nav>`,
  ].join("\n");
}

function sitemap(urls: string[], site: Site): string {
  const entries = urls
    .map((url) => `  <url><loc>${escapeHtml(`${site.origin}${url}`)}</loc></url>`)
    .join("\n");
  return `<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n${entries}\n</urlset>\n`;
}

/**
 * The whole site, as files.
 *
 * Takes the categories rather than importing them so a test can render a fixture
 * — including a deliberately broken one — without touching the real handbook.
 */
export function renderSite(categories: Category[], site: Site = DEFAULT_SITE): Page[] {
  const all = entries(categories, site);
  const known = new Set(all.map((e) => e.id));
  const pages: Page[] = [
    {
      path: "index.html",
      body: shell(categories, site, {
        title: "Meter documentation",
        description:
          "How Meter turns a blended AI bill into cost per feature: what it reads, how it attributes spend, and what every number means.",
        path: `${site.base}/`,
        current: null,
        main: home(categories, site),
      }),
    },
  ];

  for (const category of categories) {
    pages.push({
      path: `${category.slug}/index.html`,
      body: shell(categories, site, {
        title: category.title,
        description: category.blurb,
        path: `${site.base}/${category.slug}/`,
        current: null,
        main: categoryPage(category, site),
      }),
    });
  }

  all.forEach((entry, i) => {
    pages.push({
      path: `${entry.id}/index.html`,
      body: shell(categories, site, {
        title: entry.topic.title,
        description: entry.topic.summary,
        path: entry.url,
        current: entry.id,
        main: topicPage(entry, site, known, all[i - 1], all[i + 1]),
      }),
    });
  });

  const urls = [
    `${site.base}/`,
    ...categories.map((c) => `${site.base}/${c.slug}/`),
    ...all.map((e) => e.url),
  ];
  pages.push({ path: "sitemap.xml", body: sitemap(urls, site) });
  pages.push({ path: "docs.css", body: STYLESHEET });
  return pages;
}

/** The real handbook, published. */
export function publish(site: Site = DEFAULT_SITE): Page[] {
  return renderSite(CATEGORIES, site);
}

// ---------------------------------------------------------------------------
// The stylesheet
// ---------------------------------------------------------------------------
/**
 * One small stylesheet, shipped with the site.
 *
 * It borrows the app's palette and type scale rather than importing its CSS: the
 * app's stylesheet is thousands of lines about screens that do not exist here,
 * and a docs page that loads it would inherit a dashboard's layout rules. Fonts
 * are system stacks with the product's faces first — no font CDN, so reading the
 * docs sends a request to nobody but this site.
 */
const STYLESHEET = `:root {
  --bg: #faf9f5;
  --card: #ffffff;
  --ink: #1a1c17;
  --ink-soft: #3f4239;
  --muted: #686c60;
  --line: #e5e3dc;
  --line-strong: #d6d3c9;
  --surface: #f6f4ef;
  --link: #2b7264;
  --link-hover: #1f574c;
  --accent-soft: #f3fad1;
  --snippet-bg: #1a1c17;
  --snippet-ink: #e6ecd6;
  --code-tint: rgba(26, 28, 23, 0.07);
  --font: "Inter", -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
  --font-display: "Space Grotesk", "Inter", -apple-system, BlinkMacSystemFont, sans-serif;
  --mono: ui-monospace, "SF Mono", SFMono-Regular, Menlo, Consolas, monospace;
  color-scheme: light;
}

@media (prefers-color-scheme: dark) {
  :root {
    --bg: #14150f;
    --card: #1c1e17;
    --ink: #f3f2ea;
    --ink-soft: #d2d2c6;
    --muted: #989b8d;
    --line: #2f3229;
    --line-strong: #40443a;
    --surface: #20221a;
    --link: #7fd2bf;
    --link-hover: #a5e3d4;
    --accent-soft: #2c3421;
    --snippet-bg: #0d0e0a;
    --snippet-ink: #e6ecd6;
    --code-tint: rgba(243, 242, 234, 0.1);
    color-scheme: dark;
  }
}

* { box-sizing: border-box; }

body {
  margin: 0;
  background: var(--bg);
  color: var(--ink);
  font-family: var(--font);
  font-size: 16px;
  line-height: 1.65;
  -webkit-font-smoothing: antialiased;
}

a { color: var(--link); text-decoration-thickness: 1px; text-underline-offset: 2px; }
a:hover { color: var(--link-hover); }

.visually-hidden {
  position: absolute;
  width: 1px;
  height: 1px;
  margin: -1px;
  padding: 0;
  overflow: hidden;
  clip: rect(0 0 0 0);
  white-space: nowrap;
  border: 0;
}
.skip:focus {
  position: fixed;
  top: 0.5rem;
  left: 0.5rem;
  width: auto;
  height: auto;
  clip: auto;
  padding: 0.5rem 0.75rem;
  background: var(--card);
  border: 1px solid var(--line-strong);
  border-radius: 8px;
  z-index: 10;
}

/* --- chrome ------------------------------------------------------------- */
.site-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 1rem;
  padding: 0.9rem 1.5rem;
  border-bottom: 1px solid var(--line);
  background: var(--card);
  position: sticky;
  top: 0;
  z-index: 5;
}
.wordmark {
  font-family: var(--font-display);
  font-weight: 700;
  font-size: 1.15rem;
  letter-spacing: -0.02em;
  color: var(--ink);
  text-decoration: none;
}
.site-nav { display: flex; gap: 1.25rem; font-size: 0.9rem; }
.site-nav a { color: var(--ink-soft); text-decoration: none; }
.site-nav a:hover { color: var(--link); }

/* The contents come after the article in the document and are placed back on
   the left here. On a phone that ordering is the layout: an article opens at its
   first line, not below 60 links to other articles. */
.layout {
  display: grid;
  grid-template-columns: 17rem minmax(0, 1fr);
  gap: 3rem;
  max-width: 78rem;
  margin: 0 auto;
  padding: 2rem 1.5rem 4rem;
  align-items: start;
}
.layout > main { grid-column: 2; grid-row: 1; }
.layout > .toc { grid-column: 1; grid-row: 1; }

/* --- contents ----------------------------------------------------------- */
.toc { position: sticky; top: 4.5rem; max-height: calc(100vh - 6rem); overflow-y: auto; font-size: 0.9rem; }
.toc > h2 {
  font-size: 0.78rem;
  font-weight: 600;
  letter-spacing: 0.06em;
  text-transform: uppercase;
  color: var(--muted);
  margin: 0 0 1rem;
}
@media (min-width: 62.0625rem) {
  /* The heading is for a reader who has scrolled to the list; on desktop the
     list is simply there, and the category names already label it. */
  .toc > h2 { position: absolute; width: 1px; height: 1px; overflow: hidden; clip: rect(0 0 0 0); }
}
.toc ul { list-style: none; margin: 0; padding: 0; }
.toc > ul > li + li { margin-top: 1.25rem; }
.nav-category {
  display: block;
  font-family: var(--font-display);
  font-size: 0.78rem;
  font-weight: 600;
  letter-spacing: 0.06em;
  text-transform: uppercase;
  color: var(--muted);
  text-decoration: none;
  margin-bottom: 0.4rem;
}
.nav-category:hover { color: var(--ink); }
.toc ul ul li a {
  display: block;
  padding: 0.2rem 0.6rem;
  margin-left: -0.6rem;
  border-radius: 8px;
  color: var(--ink-soft);
  text-decoration: none;
}
.toc ul ul li a:hover { background: var(--surface); color: var(--ink); }
.toc a[aria-current="page"] { background: var(--accent-soft); color: var(--ink); font-weight: 600; }

/* --- content ------------------------------------------------------------ */
main { min-width: 0; max-width: 46rem; }
h1 {
  font-family: var(--font-display);
  font-size: 2rem;
  line-height: 1.2;
  letter-spacing: -0.02em;
  margin: 0 0 0.5rem;
}
h2 { font-family: var(--font-display); font-size: 1.25rem; margin: 0 0 0.35rem; }
p { margin: 0 0 1rem; }
.crumbs { font-size: 0.85rem; color: var(--muted); margin-bottom: 1rem; }
.crumbs a { color: var(--muted); }
.summary, .blurb { color: var(--muted); font-size: 1.05rem; margin-bottom: 1.75rem; }

ul, ol { margin: 0 0 1rem; padding-left: 1.25rem; }
li { margin-bottom: 0.4rem; }
li::marker { color: var(--muted); }

code {
  font-family: var(--mono);
  font-size: 0.875em;
  background: var(--code-tint);
  padding: 0.1em 0.35em;
  border-radius: 5px;
}
pre {
  background: var(--snippet-bg);
  color: var(--snippet-ink);
  padding: 1rem 1.1rem;
  border-radius: 12px;
  overflow-x: auto;
  margin: 0 0 1.25rem;
}
pre code { background: none; padding: 0; font-size: 0.85rem; color: inherit; }

.note {
  border-left: 3px solid var(--line-strong);
  background: var(--surface);
  padding: 0.85rem 1.1rem;
  border-radius: 0 10px 10px 0;
  color: var(--ink-soft);
  margin: 0 0 1.25rem;
}

.table-wrap { overflow-x: auto; margin: 0 0 1.25rem; }
/* min-width, so a narrow screen scrolls the table sideways rather than
   squeezing three columns into four characters each. */
table { border-collapse: collapse; width: 100%; min-width: 28rem; font-size: 0.92rem; }
th, td { text-align: left; padding: 0.55rem 0.8rem; border-bottom: 1px solid var(--line); vertical-align: top; }
th { font-size: 0.78rem; letter-spacing: 0.04em; text-transform: uppercase; color: var(--muted); }

.applink-mark { font-size: 0.8em; margin-left: 0.15em; opacity: 0.7; }

/* --- home and category listings ----------------------------------------- */
.lede { margin-bottom: 2rem; }
.lede p { color: var(--muted); font-size: 1.1rem; max-width: 40rem; }
main:has(.cards) { max-width: none; }
.cards { display: grid; grid-template-columns: repeat(auto-fill, minmax(17rem, 1fr)); gap: 1rem; }
.card {
  background: var(--card);
  border: 1px solid var(--line);
  border-radius: 14px;
  padding: 1.1rem 1.25rem;
}
.card h2 a { color: var(--ink); text-decoration: none; }
.card h2 a:hover { color: var(--link); }
.card .blurb { font-size: 0.9rem; margin-bottom: 0.75rem; }
ul.plain { list-style: none; padding: 0; margin: 0; font-size: 0.92rem; }
ul.plain li { margin-bottom: 0.25rem; }

.topic-list { list-style: none; padding: 0; }
.topic-list li { padding: 0.8rem 0; border-top: 1px solid var(--line); margin: 0; }
.topic-list li a { display: block; font-weight: 600; }
.topic-list li span { display: block; color: var(--muted); font-size: 0.92rem; }

/* --- previous / next ---------------------------------------------------- */
.around { display: flex; gap: 1rem; margin-top: 3rem; border-top: 1px solid var(--line); padding-top: 1.25rem; }
.around a {
  flex: 1 1 0;
  border: 1px solid var(--line);
  border-radius: 12px;
  padding: 0.75rem 1rem;
  text-decoration: none;
  color: var(--ink);
  background: var(--card);
}
.around a:hover { border-color: var(--line-strong); }
.around .next { text-align: right; }
.around span { display: block; font-size: 0.75rem; letter-spacing: 0.05em; text-transform: uppercase; color: var(--muted); }

.site-foot {
  border-top: 1px solid var(--line);
  padding: 2rem 1.5rem;
  color: var(--muted);
  font-size: 0.88rem;
  text-align: center;
}
.site-foot p { margin: 0 0 0.35rem; }

@media (max-width: 62rem) {
  .layout { grid-template-columns: minmax(0, 1fr); gap: 2rem; padding-top: 1.5rem; }
  .layout > main, .layout > .toc { grid-column: 1; grid-row: auto; }
  .toc {
    position: static;
    max-height: none;
    border-top: 1px solid var(--line);
    padding-top: 1.5rem;
  }
  h1 { font-size: 1.6rem; }
}
`;
