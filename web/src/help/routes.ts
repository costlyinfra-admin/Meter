/**
 * The in-app destinations the handbook is allowed to link to.
 *
 * It lives here, rather than inside a test, because two things need it and they
 * must agree: content.test.ts fails the build when a topic links somewhere the
 * app does not serve, and the public docs generator (src/docs/publish.ts)
 * rewrites these same links to absolute app URLs. A second private copy of the
 * list would rot the moment a route was added.
 *
 * Kept beside App.tsx by hand — there is no route table to read at build time,
 * because the routes are JSX. The test is what keeps it honest.
 */
export const APP_ROUTES = new Set([
  "/",
  "/optimize",
  "/optimize/prompts",
  "/products",
  "/applications",
  "/traces",
  "/cost-sources",
  "/features",
  "/install-sdk",
  "/alerts",
  "/settings",
  "/help",
  // Opt-in module: the route always exists, and refuses unless it is enabled.
  "/reconciliation",
]);
