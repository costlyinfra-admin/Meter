import { describe, expect, it } from "vitest";
import otelSource from "../../../backend/meter/otel.py?raw";
import {
  SPLUNK_CONTENT_PATTERN,
  SPLUNK_ENV_SNIPPET,
  splunkHelmSnippet,
  splunkLinuxSnippet,
} from "./installSnippets";

const URL_ = "https://meter.example/api/otel/v1/traces";

/** The prefixes otel.py treats as content, read from the backend source itself,
 *  so the Collector config cannot quietly fall behind what Meter discards. */
function contentPrefixes(): string[] {
  const block = otelSource.match(/_CONTENT_PREFIXES = \(([\s\S]*?)\n\)/);
  if (!block) throw new Error("_CONTENT_PREFIXES not found in backend/meter/otel.py");
  return [...block[1].matchAll(/"([^"]+)"/g)].map((m) => m[1]);
}

describe("Splunk Collector config", () => {
  it("strips every attribute Meter would otherwise discard, before it leaves", () => {
    const pattern = new RegExp(SPLUNK_CONTENT_PATTERN);
    const prefixes = contentPrefixes();
    expect(prefixes.length).toBeGreaterThan(10);
    for (const prefix of prefixes) {
      expect(pattern.test(`${prefix}.0.content`), prefix).toBe(true);
      expect(pattern.test(prefix), prefix).toBe(true);
    }
  });

  it("leaves alone every attribute Meter reads", () => {
    const pattern = new RegExp(SPLUNK_CONTENT_PATTERN);
    for (const key of [
      "gen_ai.usage.input_tokens",
      "gen_ai.usage.output_tokens",
      "gen_ai.usage.cache_read_input_tokens",
      "gen_ai.request.model",
      "gen_ai.response.model",
      "gen_ai.system",
      "gen_ai.operation.name",
      "llm.token_count.prompt",
      "llm.model_name",
      "openinference.span.kind",
      "meter.feature_id",
      "service.name",
    ]) {
      expect(pattern.test(key), key).toBe(false);
    }
  });

  it("adds a Meter pipeline instead of redefining the Splunk one", () => {
    for (const config of [splunkLinuxSnippet(URL_), splunkHelmSnippet(URL_)]) {
      expect(config).toContain("traces/meter:");
      expect(config).toContain("exporters: [otlp_http/meter]");
      // A bare `traces:` pipeline would replace what goes to Splunk.
      expect(config).not.toMatch(/^\s*traces:\s*$/m);
      expect(config).toContain(URL_);
    }
  });

  it("strips content inside Meter's pipeline, before the exporter", () => {
    for (const config of [splunkLinuxSnippet(URL_), splunkHelmSnippet(URL_)]) {
      const line = config.split("\n").find((l) => l.trim().startsWith("processors: ["))!;
      expect(line).toContain("attributes/meter_no_content");
      expect(line).toContain("filter/meter_no_span_events");
    }
  });

  it("reads the token from the environment, never from the config file", () => {
    for (const config of [splunkLinuxSnippet(URL_), splunkHelmSnippet(URL_)]) {
      expect(config).toContain("${env:METER_INGEST_TOKEN}");
      expect(config).not.toContain("<your token>");
    }
    expect(SPLUNK_ENV_SNIPPET).toContain("METER_INGEST_TOKEN=<your token>");
  });
});
