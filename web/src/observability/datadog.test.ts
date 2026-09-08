/**
 * The guards on RUM, which are the whole point of the module.
 *
 * Nothing here asserts that Datadog works — that is Datadog's job. It asserts
 * that it does NOT start anywhere it should not, because the failure mode is
 * silent: a telemetry SDK that quietly reports from a test run or a preview
 * build is only noticed later, in the data or the bill.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const init = vi.fn();
vi.mock("@datadog/browser-rum", () => ({ datadogRum: { init } }));
vi.mock("@datadog/browser-rum-react", () => ({ reactPlugin: () => ({ name: "react" }) }));

/** Re-import the module so its once-only flag starts fresh each time. */
async function load() {
  vi.resetModules();
  return import("./datadog");
}

/** Pretend to be on `host` — jsdom's location is not writable directly. */
function onHost(host: string) {
  Object.defineProperty(window, "location", {
    configurable: true,
    value: { ...window.location, hostname: host },
  });
}

const realLocation = Object.getOwnPropertyDescriptor(window, "location");

beforeEach(() => {
  init.mockClear();
});

afterEach(() => {
  if (realLocation) Object.defineProperty(window, "location", realLocation);
  vi.unstubAllEnvs();
});

describe("initObservability", () => {
  it("does not start under the test runner, whatever the hostname", async () => {
    // The default state of this very suite: PROD is false. If this ever starts
    // reporting, every test run becomes production telemetry.
    const { initObservability, RUM_HOST } = await load();
    onHost(RUM_HOST);

    expect(initObservability()).toBe(false);
    expect(init).not.toHaveBeenCalled();
  });

  it("does not start on a production build served from another host", async () => {
    // Covers localhost, the onrender.com hostname, and any preview deploy —
    // all of which are production builds of the same bundle.
    vi.stubEnv("PROD", true);
    const { initObservability } = await load();

    for (const host of ["localhost", "meter-a1b2.onrender.com", "staging.costlyinfra.com"]) {
      onHost(host);
      expect(initObservability(), host).toBe(false);
    }
    expect(init).not.toHaveBeenCalled();
  });

  it("starts on the production site, once, with the expected configuration", async () => {
    vi.stubEnv("PROD", true);
    const { initObservability, RUM_HOST } = await load();
    onHost(RUM_HOST);

    expect(initObservability()).toBe(true);
    expect(init).toHaveBeenCalledTimes(1);

    const config = init.mock.calls[0][0];
    expect(config.service).toBe("meter-web");
    expect(config.env).toBe("production");
    expect(config.sessionSampleRate).toBe(100);
    expect(config.sessionReplaySampleRate).toBe(20);
    // Replay records the DOM, so the default has to mask what users type. The
    // two screens that render secrets or customer identifiers as *text* are
    // masked at the element — see Snippet's `sensitive` prop and
    // CustomerBreakdown — because this default does not reach them.
    expect(config.defaultPrivacyLevel).toBe("mask-user-input");
    // The version ties an error to the build it came from.
    expect(config.version).toBeTruthy();
  });

  it("never initialises twice", async () => {
    vi.stubEnv("PROD", true);
    const { initObservability, RUM_HOST } = await load();
    onHost(RUM_HOST);

    expect(initObservability()).toBe(true);
    expect(initObservability()).toBe(false);
    expect(initObservability()).toBe(false);
    expect(init).toHaveBeenCalledTimes(1);
  });
});
