/**
 * The agent prompt is a set of promises about the SDK. These check them against
 * the SDK itself, so the prompt cannot quietly drift the way the hand-written
 * install instructions did.
 */
import { describe, expect, it } from "vitest";
import { MIN_SDK, agentPrompt } from "./installPrompt";
import type { Feature } from "../api";

// The SDKs themselves, read as text. Loading the real files is the whole point:
// a claim in the prompt that no longer matches the code fails here.
import PYTHON_SDK from "../../../sdk/python/costlyinfra_meter/__init__.py?raw";
import NODE_SDK from "../../../sdk/node/index.mjs?raw";
import PYTHON_MANIFEST from "../../../sdk/python/pyproject.toml?raw";
import NODE_MANIFEST_TEXT from "../../../sdk/node/package.json?raw";

const NODE_MANIFEST = JSON.parse(NODE_MANIFEST_TEXT);

const feature = (id: string, name: string) => ({ id, name }) as Feature;
const FEATURES = [feature("f-threat-triage", "AI threat triage"), feature("f-reports", "Reports")];

const prompt = () => agentPrompt("https://meter.example.com/api/hook/events", FEATURES);

describe("the coding-agent prompt", () => {
  it("names the packages that are actually published", () => {
    const text = prompt();
    expect(PYTHON_MANIFEST).toContain('name = "costlyinfra-meter"');
    expect(NODE_MANIFEST.name).toBe("costlyinfra-meter");
    expect(text).toContain("costlyinfra-meter");
  });

  it("does not ask for a version the SDK has not reached", () => {
    const shipped = /version = "(\d+)\.(\d+)/.exec(PYTHON_MANIFEST)!;
    const [major, minor] = MIN_SDK.split(".").map(Number);
    expect(Number(shipped[1])).toBeGreaterThanOrEqual(major);
    if (Number(shipped[1]) === major) expect(Number(shipped[2])).toBeGreaterThanOrEqual(minor);
    // The two SDKs are released together, so their versions must agree.
    expect(NODE_MANIFEST.version).toBe(/version = "([\d.]+)"/.exec(PYTHON_MANIFEST)![1]);
  });

  // The prompt tells a coding agent which methods to call. If it names one the
  // SDK does not have, the agent writes code that cannot work — so the prompt
  // is checked against the SDK source, not against what we remember shipping.
  it("only mentions Python entry points that exist", () => {
    for (const name of [
      "def wrap(",
      "class Meter",
      "def agent(",
      "def resume(",
      "def llm(",
      "def tool(",
      "def export_context(",
      "def flush(",
    ]) {
      expect(PYTHON_SDK).toContain(name);
    }
    const text = prompt();
    expect(text).toContain("from costlyinfra_meter import Meter");
    expect(text).toContain("meter.wrap(");
    expect(text).toContain("meter.agent(");
    expect(text).toContain("run.export_context()");
    // And must not name anything the rewrite removed.
    for (const gone of ["record_anthropic", "record_openai", "meter.record("]) {
      expect(text).not.toContain(gone);
    }
  });

  it("only mentions Node entry points that exist", () => {
    for (const name of [
      "export class Meter",
      "export class AgentRun",
      "async agent(",
      "async resume(",
      "llm(",
      "tool(",
      "exportContext(",
      "async flush(",
    ]) {
      expect(NODE_SDK).toContain(name);
    }
    const text = prompt();
    expect(text).toContain('import { Meter } from "costlyinfra-meter"');
    expect(text).toContain("meter.agent(");
    expect(text).toContain("run.exportContext()");
    for (const gone of ["recordAnthropic", "recordOpenAI", 'new Meter("<feature-id>")']) {
      expect(text).not.toContain(gone);
    }
  });

  it("is right that the Node package is ESM only", () => {
    // If this ever gains a CommonJS entry point, the prompt's warning is wrong.
    expect(NODE_MANIFEST.type).toBe("module");
    expect(NODE_MANIFEST.main).toMatch(/\.mjs$/);
    expect(prompt()).toContain("ESM-only");
  });

  it("uses the environment variables the SDK reads", () => {
    for (const name of ["METER_INGEST_URL", "METER_INGEST_TOKEN"]) {
      expect(PYTHON_SDK).toContain(name);
      expect(NODE_SDK).toContain(name);
      expect(prompt()).toContain(name);
    }
  });

  it("carries this install's ingest URL, not a placeholder", () => {
    expect(prompt()).toContain("METER_INGEST_URL=https://meter.example.com/api/hook/events");
  });

  it("lists real feature ids so the agent has nothing to invent", () => {
    const text = prompt();
    expect(text).toContain("f-threat-triage");
    expect(text).toContain("AI threat triage");
    expect(text).toContain("f-reports");
  });

  it("tells the agent to ask when there are no features to name", () => {
    const text = agentPrompt("https://x/api/hook/events", []);
    expect(text).toMatch(/do not invent one/i);
  });

  it("names the application the reader chose, instead of telling the agent to ask", () => {
    const text = agentPrompt("https://x/api/hook/events", FEATURES, null, "support-agent");
    expect(text).toContain("METER_APPLICATION=support-agent");
    expect(text).toContain("already decided");
    expect(text).not.toMatch(/If you do not know the application slug, ASK ME/);
  });

  it("still tells the agent to ask when no application has been chosen", () => {
    expect(prompt()).toMatch(/If you do not know the application slug, ASK ME/);
  });

  it("never puts the ingest token in the text it hands over", () => {
    // The prompt goes into a third-party tool; the secret does not travel with it.
    expect(prompt()).toMatch(/METER_INGEST_TOKEN=<ask me/);
  });

  it("states the invariants that make metering safe to merge", () => {
    const text = prompt();
    expect(text).toMatch(/Never send prompt or response content/i);
    // The ban must be specific enough that an agent knows what it covers.
    expect(text).toMatch(/tool arguments/i);
    expect(text).toMatch(/stack traces/i);
    expect(text).toMatch(/never break or slow the request path/i);
    expect(text).toMatch(/flush\(\)/);
  });
});
