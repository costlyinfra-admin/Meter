/**
 * Datadog Browser RUM.
 *
 * Imported for its side effect by `main.tsx`, before React renders, so an error
 * thrown while the app is still mounting is still reported.
 *
 * WHERE THIS RUNS
 * Only in a real browser, only in a production build, and only on the live
 * host. Each of those guards exists for a different reason: `window` is absent
 * if the module is ever evaluated outside a browser, `import.meta.env.PROD` is
 * false under Vitest and `vite dev`, and the hostname check keeps local builds,
 * the `onrender.com` hostname, and any preview deploy out of the production RUM
 * data. A telemetry SDK that quietly starts sending during a test run is a
 * problem nobody notices until the bill or the noise arrives.
 *
 * WHAT IT MUST NOT COLLECT
 * This product's screens carry things that must never leave the browser: a
 * customer's ingest token, their own customers' identifiers, provider API keys.
 * `defaultPrivacyLevel: "mask-user-input"` covers form fields — the API-key
 * input, connector credentials — but it does NOT mask text the page renders,
 * and session replay records the DOM. The two places that display secrets or
 * customer identifiers as text are therefore marked `data-dd-privacy="mask"` at
 * the element (see `Snippet`'s `sensitive` prop and the customer table); that
 * attribute wins over the default for its whole subtree.
 *
 * Nothing here reads request or response bodies, so prompts, model responses,
 * billing details and credentials in flight are outside its reach by
 * construction — RUM records timings and URLs for requests, not payloads.
 */
import { datadogRum } from "@datadog/browser-rum";
import { reactPlugin } from "@datadog/browser-rum-react";

/** Inlined from package.json by Vite — see `define` in vite.config.ts. */
declare const __APP_VERSION__: string;

/** The only hostname that reports. Preview and local builds stay silent. */
export const RUM_HOST = "meter.costlyinfra.com";

/** Guards against a double `init`, which Datadog warns about and ignores. */
let started = false;

/**
 * Start RUM if this is the production site in a browser. Returns whether it
 * actually started, which is what the tests assert on.
 */
export function initObservability(): boolean {
  if (started) return false;
  // Not a browser at all (SSR, a node script, a prerender step).
  if (typeof window === "undefined" || typeof document === "undefined") return false;
  // `vite dev` and Vitest both report PROD false, which covers dev and tests.
  if (!import.meta.env.PROD) return false;
  if (window.location.hostname !== RUM_HOST) return false;

  started = true;
  datadogRum.init({
    applicationId: "60aa2d46-5989-41d9-a3ef-fe9a8841955d",
    // Public by design: a RUM client token can only write, and is visible in
    // any browser that loads the app. It is not a secret and not an API key.
    clientToken: "pub90a1576f7d8bdf1392227803455239e2",
    site: "datadoghq.com",
    service: "meter-web",
    env: "production",
    version: __APP_VERSION__,
    sessionSampleRate: 100,
    sessionReplaySampleRate: 20,
    trackResources: true,
    trackUserInteractions: true,
    trackLongTasks: true,
    defaultPrivacyLevel: "mask-user-input",
    plugins: [reactPlugin({ router: false })],
  });
  return true;
}
