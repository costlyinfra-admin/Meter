/**
 * The page's own code samples, checked against the SDK that has to run them.
 *
 * installPrompt.test.ts already does this for the agent prompt, and its header
 * says why: "so the prompt cannot quietly drift the way the hand-written
 * install instructions did." Those instructions had drifted — the page taught
 * `wrap(client, feature_id=…)` and `meter.record_anthropic(resp)` after the
 * rewrite removed both, so anyone following it by hand got a TypeError and an
 * AttributeError. This file closes that gap.
 */
import { describe, expect, it } from "vitest";
import {
  AGENT_NODE,
  AGENT_PYTHON,
  ENV_VARS,
  envSnippet,
  FLUSH_NODE,
  FLUSH_PYTHON,
  normalizeSlug,
  RESUME_NODE,
  RESUME_PYTHON,
  SPAN_KINDS,
  suggestSlug,
  WRAP_NODE,
  WRAP_PYTHON,
} from "./installSnippets";

import PYTHON_SDK from "../../../sdk/python/costlyinfra_meter/__init__.py?raw";
import NODE_SDK from "../../../sdk/node/index.mjs?raw";
// The server's own normalizer. The page's copy has to agree with it, or a slug
// the page accepts is silently rewritten to a different application on ingest.
import APPLICATIONS_PY from "../../../backend/meter/applications.py?raw";

const PY = [WRAP_PYTHON, AGENT_PYTHON, RESUME_PYTHON, FLUSH_PYTHON].join("\n");
const NODE = [WRAP_NODE, AGENT_NODE, RESUME_NODE, FLUSH_NODE].join("\n");

describe("the Install SDK page's code samples", () => {
  it("only calls Python methods the SDK actually has", () => {
    for (const name of [
      "class Meter",
      "    def wrap(",
      "    def agent(",
      "    def resume(",
      "    def llm(",
      "    def tool(",
      "    def export_context(",
      "    def flush(",
    ]) {
      expect(PYTHON_SDK).toContain(name);
    }
    expect(PY).toContain("from costlyinfra_meter import Meter");
    expect(PY).toContain("meter.wrap(");
    expect(PY).toContain("meter.agent(");
    expect(PY).toContain("meter.resume(");
    expect(PY).toContain("run.export_context()");
    expect(PY).toContain("meter.flush()");
  });

  it("only calls Node methods the SDK actually has", () => {
    for (const name of ["export class Meter", "async agent(", "async resume(", "async flush("]) {
      expect(NODE_SDK).toContain(name);
    }
    expect(NODE).toContain('import { Meter } from "costlyinfra-meter"');
    expect(NODE).toContain("meter.wrap(");
    expect(NODE).toContain("meter.agent(");
    expect(NODE).toContain("meter.resume(");
    expect(NODE).toContain("run.exportContext()");
    expect(NODE).toContain("await meter.flush()");
  });

  it("names no entry point the rewrite removed", () => {
    // Each of these was on the page and is gone from the SDK. Following the old
    // page by hand raised AttributeError on the first metered call.
    for (const gone of [
      "record_anthropic",
      "record_openai",
      "recordAnthropic",
      "recordOpenAI",
      "from costlyinfra_meter import wrap",
      'import { wrap } from "costlyinfra-meter"',
    ]) {
      expect(`${PY}\n${NODE}`).not.toContain(gone);
      expect(`${PYTHON_SDK}\n${NODE_SDK}`).not.toContain(`def ${gone}(`);
    }
  });

  it("never offers a Meter field that would carry prompt or response content", () => {
    // The milestone's whole guarantee. `metadata=` was on the page as a wrap()
    // argument; it is not in the event contract, and showing it invites
    // someone to put a prompt in it.
    const all = `${PY}\n${NODE}`;
    expect(all).not.toContain("metadata");

    // The samples DO contain `messages=[...]` — that is the customer's own
    // provider call, shown unchanged, which is the point. What must never
    // happen is content reaching Meter, so check the lines that call it.
    // What is ASSIGNED from a step is the customer's own variable —
    // `documents = run.tool(...)` is fine and idiomatic. What is PASSED to
    // Meter is the thing to police, so look only inside the parentheses.
    const args = all
      .split("\n")
      .map((line) => /\b(?:meter|run)\.[a-zA-Z_]+\((.*)$/.exec(line)?.[1])
      .filter((a): a is string => a !== undefined);
    expect(args.length).toBeGreaterThan(6); // the check is looking at something
    for (const arg of args) {
      for (const field of ["prompt", "messages", "response", "content", "documents", "result"]) {
        expect(arg).not.toMatch(new RegExp(`\\b${field}\\s*[=:]`));
      }
    }
  });

  it("constructs Meter the way the SDK does — no positional feature id", () => {
    // `Meter("<feature-id>")` looks reasonable and silently does nothing:
    // every constructor argument is keyword-only.
    expect(PYTHON_SDK).toMatch(/def __init__\(\s*self,\s*\*,/);
    expect(PY).toContain("Meter()");
    expect(PY).not.toMatch(/Meter\(["']/);
    expect(NODE).not.toMatch(/new Meter\(["']/);
  });

  it("passes agent() the arguments Node's signature expects", () => {
    // agent(operationName, options, callback) — a sample that passed the
    // options object as the callback would throw at the first run.
    expect(NODE_SDK).toContain("async agent(operationName, options, callback)");
    expect(AGENT_NODE).toMatch(/meter\.agent\(\s*\n?\s*"resolve-ticket",\s*\n?\s*\{/);
    expect(AGENT_NODE).toContain("async (run) =>");
  });

  it("lists only step kinds that are real methods on a run", () => {
    for (const [kind] of SPAN_KINDS) {
      expect(PYTHON_SDK).toContain(`    def ${kind}(`);
      expect(NODE_SDK).toMatch(new RegExp(`\\n  (?:async )?${kind}\\(`));
    }
  });

  it("documents every environment variable the SDK reads, and no others", () => {
    for (const { name } of ENV_VARS) {
      expect(PYTHON_SDK).toContain(name);
      expect(NODE_SDK).toContain(name);
    }
    // And the reverse: an env var the SDK reads but the page never mentions is
    // a setting a customer cannot discover.
    const read = [...PYTHON_SDK.matchAll(/METER_[A-Z_]+/g)].map((m) => m[0]);
    for (const name of new Set(read)) {
      expect(ENV_VARS.map((v) => v.name)).toContain(name);
    }
  });

  it("writes the reader's own ingest URL and slug into the env snippet", () => {
    const snippet = envSnippet("https://meter.example.com/api/hook/events", "support-agent");
    expect(snippet).toContain("METER_INGEST_URL=https://meter.example.com/api/hook/events");
    expect(snippet).toContain("METER_APPLICATION=support-agent");
    // No token unless one has been generated — and the placeholder is obvious.
    expect(snippet).toContain("METER_INGEST_TOKEN=<your token>");
  });

  it("shows a real token only once the reader has generated one", () => {
    expect(envSnippet("https://x/api/hook/events", "app", "mtr_secret")).toContain(
      "METER_INGEST_TOKEN=mtr_secret",
    );
  });
});

describe("the application slug", () => {
  it("normalizes the way the server does", () => {
    // Mirrors normalize_slug() in backend/meter/applications.py: lowercase,
    // spaces and underscores to dashes, everything else dropped, no repeats,
    // no leading or trailing dash.
    expect(APPLICATIONS_PY).toContain("def normalize_slug(");
    expect(normalizeSlug("Support Agent")).toBe("support-agent");
    expect(normalizeSlug("support_agent")).toBe("support-agent");
    expect(normalizeSlug("  Support   Agent!!  ")).toBe("support-agent");
    expect(normalizeSlug("--support--agent--")).toBe("support-agent");
    expect(normalizeSlug("Ünïcodé")).toBe("ncod");
  });

  it("never produces a slug the server would reject", () => {
    // 64 characters is the server's MAX_SLUG, and a slug may not end in a dash
    // — so truncation must not leave one behind.
    expect(APPLICATIONS_PY).toContain("MAX_SLUG = 64");
    const long = normalizeSlug("a".repeat(70));
    expect(long.length).toBe(64);
    const truncatedOnADash = normalizeSlug(`${"a".repeat(63)}-bcdef`);
    expect(truncatedOnADash.endsWith("-")).toBe(false);
    expect(/^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$/.test(truncatedOnADash)).toBe(true);
  });

  it("suggests something usable even when there is nothing to derive from", () => {
    expect(suggestSlug("Acme Security")).toBe("acme-security");
    expect(suggestSlug("")).toBe("my-app");
    expect(suggestSlug(null)).toBe("my-app");
    // A name with nothing slug-able left in it must not yield an empty slug.
    expect(suggestSlug("!!!")).toBe("my-app");
  });
});
