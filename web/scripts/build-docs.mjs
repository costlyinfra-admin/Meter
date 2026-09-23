#!/usr/bin/env node
/**
 * Build the public documentation site from the in-app handbook.
 *
 *   npm run build:docs -- --out ../dist-docs --base /docs
 *
 * All the rendering lives in src/docs/publish.ts, which is typed and tested.
 * This is the part that cannot be: reading arguments, and writing files.
 *
 * It loads the TypeScript through Vite rather than compiling it first, so the
 * generator reads exactly the same source the app does — one module graph, no
 * build artefact that can go stale, and no extra dependency.
 */
import { mkdir, rm, writeFile } from "node:fs/promises";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { createServer } from "vite";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");

function arg(name, fallback) {
  const i = process.argv.indexOf(`--${name}`);
  return i !== -1 && process.argv[i + 1] ? process.argv[i + 1] : fallback;
}

const out = resolve(root, arg("out", "dist-docs"));
const base = arg("base", process.env.DOCS_BASE ?? "/docs").replace(/\/$/, "");
const origin = arg("origin", process.env.DOCS_ORIGIN ?? "https://costlyinfra.com").replace(/\/$/, "");
const app = arg("app", process.env.DOCS_APP_ORIGIN ?? "https://meter.costlyinfra.com").replace(/\/$/, "");

// configFile: false — the app's Vite config carries the React plugin and a dev
// proxy, neither of which has anything to do with rendering HTML from data.
const server = await createServer({
  root,
  configFile: false,
  logLevel: "warn",
  appType: "custom",
  server: { middlewareMode: true },
});

let pages;
try {
  const { publish } = await server.ssrLoadModule("/src/docs/publish.ts");
  pages = publish({ base, origin, app });
} finally {
  await server.close();
}

// Rebuild from empty: a topic that was renamed must not leave its old URL
// behind, still live and still saying the old thing.
await rm(out, { recursive: true, force: true });
for (const page of pages) {
  const file = join(out, page.path);
  await mkdir(dirname(file), { recursive: true });
  await writeFile(file, page.body, "utf8");
}

const html = pages.filter((p) => p.path.endsWith(".html")).length;
console.log(`✓ ${pages.length} files (${html} pages) → ${out}`);
console.log(`  serving at ${origin}${base}/`);
