/// <reference types="vitest/config" />
import { readFileSync } from "node:fs";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// The app's own version, read from package.json at build time and inlined as a
// constant. Reported to Datadog so an error can be tied to the build it came
// from; importing package.json instead would ship the whole manifest.
const { version } = JSON.parse(
  readFileSync(new URL("./package.json", import.meta.url), "utf8"),
) as { version: string };

export default defineConfig({
  define: { __APP_VERSION__: JSON.stringify(version) },
  plugins: [react()],
  server: {
    // Dev: forward API calls to the FastAPI backend
    // (uvicorn --factory meter.api:create_app --port 8000).
    proxy: {
      "/api": "http://localhost:8000",
    },
  },
  test: {
    globals: true,
    environment: "jsdom",
    setupFiles: ["./src/test/setup.ts"],
  },
});
