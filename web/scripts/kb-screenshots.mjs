#!/usr/bin/env node
/**
 * Retake the knowledge base's screenshots from the demo tenant.
 *
 *   make demo                      # in one terminal: seeded demo at :5173
 *   npm run kb:screenshots         # in another, from web/
 *
 * Screenshots go stale the moment a screen changes, so they are produced by
 * this script rather than by hand: run it after changing a screen the
 * handbook shows, look at the diff, commit the images.
 *
 * Only the DEMO tenant is ever photographed — its data is made up — so no
 * customer's numbers can end up in public documentation. The script refuses
 * any account but the demo's.
 *
 * It drives a locally installed Chrome over the DevTools protocol with Node's
 * own WebSocket, so it needs nothing installed beyond Chrome. Set CHROME to the
 * browser binary if it is not in the usual macOS place.
 *
 * Each shot is cropped to the panel the topic is about, at twice the pixel
 * density so it stays sharp on a high-resolution screen, and saved as WebP.
 */
import { spawn } from "node:child_process";
import { mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const OUT = join(root, "public", "kb");

function arg(name, fallback) {
  const i = process.argv.indexOf(`--${name}`);
  return i !== -1 && process.argv[i + 1] ? process.argv[i + 1] : fallback;
}

const BASE = arg("url", "http://localhost:5173").replace(/\/$/, "");
const ONLY = arg("only", null); // retake one file, e.g. --only overview.webp
// The seeded demo login (scripts/demo.sh prints it; docs/deploy.md lists it).
const DEMO_EMAIL = "demo@costlyinfra.com";
const DEMO_PASSWORD = "meter-demo";
const CHROME =
  process.env.CHROME ?? "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";

/** Click one of the Overview's breakdown tabs by its label. */
const tab = (label) => `
  [...document.querySelectorAll('[role=tab]')].find((b) => b.textContent === ${JSON.stringify(label)}).click();
`;

/**
 * Every screenshot the handbook uses. `select` is cropped to (the union of the
 * elements, padded); `ready` must be true before the picture is taken; `prepare`
 * runs first, to open a tab or follow a link.
 */
const SHOTS = [
  { file: "overview.webp", path: "/", select: [".kpi-row", ".overview-grid"] },
  { file: "spend-trend.webp", path: "/", select: [".trend-panel"] },
  { file: "budget-forecast.webp", path: "/", select: [".budget-panel"] },
  {
    file: "by-feature.webp",
    path: "/",
    select: [".tabs-wrap", ".feature-bars", ".mini-table-wrap"],
    maxHeight: 760,
  },
  {
    file: "by-product.webp",
    path: "/",
    prepare: tab("By Product"),
    ready: "document.querySelector('.stacked-trend')",
    select: [".tabs-wrap", ".stacked-trend"],
  },
  {
    file: "by-provider.webp",
    path: "/",
    prepare: tab("By Provider"),
    ready: "document.querySelector('.inference-body')",
    select: [".tabs-wrap", ".inference-body"],
  },
  {
    file: "by-developer.webp",
    path: "/",
    prepare: tab("By Developer"),
    ready: "document.querySelector('.stacked-trend')",
    select: [".tabs-wrap", ".inference-body"],
    maxHeight: 900,
  },
  {
    file: "by-customer.webp",
    path: "/",
    prepare: tab("By Customer"),
    ready: "document.querySelector('.stacked-trend')",
    select: [".tabs-wrap", ".inference-body"],
  },
  {
    file: "feature-detail.webp",
    path: "/",
    prepare: "document.querySelector('.feature-row a').click();",
    ready: "location.pathname.startsWith('/features/') && document.querySelector('.detail-meta')",
    select: [".content"],
    maxHeight: 820,
  },
  { file: "features.webp", path: "/features", select: [".content"], maxHeight: 860 },
  { file: "connect-sources.webp", path: "/cost-sources", select: [".content"], maxHeight: 760 },
  { file: "forecast.webp", path: "/forecast", select: [".content"], maxHeight: 820 },
  { file: "recommendations.webp", path: "/optimize", select: [".content"], maxHeight: 820 },
  { file: "traces.webp", path: "/traces", select: [".content"], maxHeight: 700 },
  { file: "alerts.webp", path: "/alerts", select: [".content"], maxHeight: 700 },
  {
    file: "install-sdk.webp",
    path: "/install-sdk",
    // The page prints the address it was opened at. A customer sees the real
    // one, so the picture should too, rather than the demo's localhost.
    prepare: `
      const walk = document.createTreeWalker(document.querySelector(".content"), NodeFilter.SHOW_TEXT);
      while (walk.nextNode()) {
        walk.currentNode.nodeValue = walk.currentNode.nodeValue.replaceAll(location.origin, "https://meter.costlyinfra.com");
      }
    `,
    select: [".content"],
    maxHeight: 760,
  },
];

// Things that float over the page and belong in no screenshot.
const HIDE_CSS = ".assist-fab, .assist-panel, .ask-bubble { display: none !important; }";

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function launch() {
  const profile = await mkdtemp(join(tmpdir(), "meter-kb-"));
  const chrome = spawn(
    CHROME,
    [
      "--headless=new",
      "--remote-debugging-port=0",
      `--user-data-dir=${profile}`,
      "--no-first-run",
      "--no-default-browser-check",
      "--hide-scrollbars",
      "--window-size=1440,1000",
      "about:blank",
    ],
    { stdio: "ignore" },
  );
  // Chrome writes the port it picked here once it is listening.
  for (let i = 0; i < 100; i++) {
    try {
      const [port] = (await readFile(join(profile, "DevToolsActivePort"), "utf8")).split("\n");
      const targets = await (await fetch(`http://127.0.0.1:${port}/json/list`)).json();
      const page = targets.find((t) => t.type === "page");
      if (page) return { chrome, profile, url: page.webSocketDebuggerUrl };
    } catch {
      // not up yet
    }
    await sleep(100);
  }
  chrome.kill();
  throw new Error(`Chrome did not start (${CHROME}). Set CHROME to its binary.`);
}

function connect(url) {
  const ws = new WebSocket(url);
  let next = 1;
  const pending = new Map();
  ws.addEventListener("message", (event) => {
    const msg = JSON.parse(event.data);
    const waiter = pending.get(msg.id);
    if (!waiter) return;
    pending.delete(msg.id);
    if (msg.error) waiter.reject(new Error(msg.error.message));
    else waiter.resolve(msg.result);
  });
  const send = (method, params = {}) =>
    new Promise((resolveSend, reject) => {
      const id = next++;
      pending.set(id, { resolve: resolveSend, reject });
      ws.send(JSON.stringify({ id, method, params }));
    });
  return new Promise((ready, fail) => {
    ws.addEventListener("open", () => ready({ send, close: () => ws.close() }));
    ws.addEventListener("error", fail);
  });
}

async function main() {
  const { chrome, profile, url } = await launch();
  const cdp = await connect(url);
  const evaluate = async (expression) => {
    const { result, exceptionDetails } = await cdp.send("Runtime.evaluate", {
      expression,
      awaitPromise: true,
      returnByValue: true,
    });
    if (exceptionDetails) throw new Error(exceptionDetails.exception?.description ?? expression);
    return result.value;
  };
  const waitFor = async (expression, what, ms = 15000) => {
    const until = Date.now() + ms;
    while (Date.now() < until) {
      if (await evaluate(`Boolean(${expression})`)) return;
      await sleep(150);
    }
    throw new Error(`Timed out waiting for ${what}`);
  };
  const go = async (path) => {
    await cdp.send("Page.navigate", { url: `${BASE}${path}` });
    await sleep(300);
    await waitFor("document.readyState === 'complete'", `${path} to load`);
  };

  try {
    await cdp.send("Page.enable");
    await cdp.send("Runtime.enable");
    await cdp.send("Emulation.setDeviceMetricsOverride", {
      width: 1440,
      height: 1000,
      deviceScaleFactor: 2,
      mobile: false,
    });
    // Light, whatever this machine prefers: the docs show one theme.
    await cdp.send("Emulation.setEmulatedMedia", {
      features: [{ name: "prefers-color-scheme", value: "light" }],
    });

    await go("/login");
    const me = await evaluate(`
      (async () => {
        localStorage.setItem("meter.theme", "light");
        const r = await fetch("/api/auth/login", {
          method: "POST",
          credentials: "include",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(${JSON.stringify({ email: DEMO_EMAIL, password: DEMO_PASSWORD })}),
        });
        return r.ok ? (await r.json()).email : null;
      })()
    `);
    if (me !== DEMO_EMAIL) {
      throw new Error(`Could not sign in as the demo account at ${BASE}. Is \`make demo\` running?`);
    }

    for (const shot of SHOTS) {
      if (ONLY && shot.file !== ONLY) continue;
      await go(shot.path);
      await evaluate(`
        const s = document.createElement("style");
        s.textContent = ${JSON.stringify(HIDE_CSS)};
        document.head.appendChild(s);
      `);
      await waitFor(`document.querySelector(${JSON.stringify(shot.select[0])})`, shot.file);
      // Panels fetch their own data; give them a moment past first paint, then
      // wait until nothing on the page still says it is loading.
      await sleep(900);
      if (shot.prepare) {
        await evaluate(shot.prepare);
        await sleep(400);
      }
      if (shot.ready) await waitFor(shot.ready, `${shot.file} to be ready`);
      await waitFor(
        `!/Loading…|Calculating…/.test(document.querySelector(".content")?.innerText ?? "")`,
        `${shot.file} to finish loading`,
      );
      await sleep(500);

      const box = await evaluate(`
        (() => {
          const rects = ${JSON.stringify(shot.select)}
            .map((sel) => document.querySelector(sel))
            .filter(Boolean)
            .map((el) => el.getBoundingClientRect());
          const pad = 12;
          const x = Math.min(...rects.map((r) => r.left)) - pad;
          const y = Math.min(...rects.map((r) => r.top)) + window.scrollY - pad;
          const right = Math.max(...rects.map((r) => r.right)) + pad;
          const bottom = Math.max(...rects.map((r) => r.bottom)) + window.scrollY + pad;
          return { x: Math.max(0, x), y: Math.max(0, y), width: right - x, height: bottom - y };
        })()
      `);
      if (shot.maxHeight) box.height = Math.min(box.height, shot.maxHeight);
      const { data } = await cdp.send("Page.captureScreenshot", {
        format: "webp",
        quality: 82,
        captureBeyondViewport: true,
        clip: { ...box, scale: 1 },
      });
      const bytes = Buffer.from(data, "base64");
      await writeFile(join(OUT, shot.file), bytes);
      console.log(`✓ ${shot.file}  ${Math.round(box.width)}×${Math.round(box.height)}  ${Math.round(bytes.length / 1024)} KB`);
    }
  } finally {
    cdp.close();
    // Let Chrome finish writing its profile before it is removed.
    const exited = new Promise((r) => chrome.once("exit", r));
    chrome.kill();
    await exited;
    await rm(profile, { recursive: true, force: true, maxRetries: 5, retryDelay: 200 });
  }
}

main().catch((err) => {
  console.error(`✖ ${err.message}`);
  process.exit(1);
});
