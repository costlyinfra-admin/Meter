/**
 * The code the Install SDK page shows people to paste.
 *
 * It lives here rather than inline in the page for the same reason the agent
 * prompt does: these are promises about the SDK, and installSnippets.test.ts
 * checks them against sdk/python and sdk/node. The hand-written instructions
 * drifted once already — they were still teaching `wrap(client, feature_id=…)`
 * and `meter.record_anthropic(resp)` after the rewrite removed both — and a
 * page that teaches a method the SDK does not have is worse than no page.
 *
 * Every snippet is a function of the reader's own install: their ingest URL and
 * their application slug, never a placeholder they have to remember to replace.
 */

/** A slug the SDK will accept, normalized the way the server normalizes it. */
export function normalizeSlug(raw: string): string {
  return raw
    .trim()
    .toLowerCase()
    .replace(/[\s_]+/g, "-")
    .replace(/[^a-z0-9-]/g, "")
    .replace(/-{2,}/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 64)
    .replace(/-+$/, "");
}

/**
 * A first suggestion for the slug, from the organization's name.
 *
 * A suggestion, not a decision: an org called "Acme Security" rarely has one
 * application called "acme-security". The field stays editable and the copy
 * says what the slug is for, because it groups every workflow in a codebase
 * and is awkward to change once events carry it.
 */
export function suggestSlug(orgName: string | null | undefined): string {
  return normalizeSlug(orgName || "") || "my-app";
}

/** The environment the SDK actually reads. Checked against the SDK source. */
export const ENV_VARS = [
  {
    name: "METER_INGEST_URL",
    required: true,
    note: "Where the SDK sends usage. Specific to this install.",
  },
  { name: "METER_INGEST_TOKEN", required: true, note: "Secret. Never commit it." },
  {
    name: "METER_APPLICATION",
    required: false,
    note: "Groups every workflow in this codebase under one name. Defaults to “default”.",
  },
  {
    name: "METER_ENVIRONMENT",
    required: false,
    note: "production | staging | development. Defaults to production.",
  },
  {
    name: "METER_RELEASE_VERSION",
    required: false,
    note: "Your build or release ID, so a cost change can be traced to a deploy.",
  },
];

export function envSnippet(ingestUrl: string, slug: string, token?: string | null): string {
  return `METER_INGEST_URL=${ingestUrl}
METER_INGEST_TOKEN=${token ?? "<your token>"}
METER_APPLICATION=${slug}
METER_ENVIRONMENT=production
# METER_RELEASE_VERSION=$GIT_SHA   # optional`;
}

// ---------------------------------------------------------------------------
// 3. One model call
// ---------------------------------------------------------------------------
export const WRAP_PYTHON = `from anthropic import Anthropic
from costlyinfra_meter import Meter

meter = Meter()                       # reads the env vars above
client = meter.wrap(Anthropic(), feature_id="<feature-id>")

# unchanged — this call is metered, and becomes a one-step trace:
resp = client.messages.create(model="claude-sonnet-4-6", messages=[...])`;

export const WRAP_NODE = `import OpenAI from "openai";
import { Meter } from "costlyinfra-meter";

const meter = new Meter();
const client = meter.wrap(new OpenAI(), { featureId: "<feature-id>" });

// unchanged — this call is metered, and becomes a one-step trace:
const resp = await client.chat.completions.create({ model: "gpt-4o", messages: [...] });`;

// ---------------------------------------------------------------------------
// 4. A multi-step run
// ---------------------------------------------------------------------------
export const AGENT_PYTHON = `with meter.agent("resolve-ticket", feature_id="<feature-id>",
                 customer_id=customer_id) as run:
    classification = run.llm("classify", lambda: client.messages.create(...))
    documents      = run.tool("retrieve-documents", retrieve_documents)
    answer         = run.llm("generate-answer", lambda: client.messages.create(...))`;

export const AGENT_NODE = `const answer = await meter.agent(
  "resolve-ticket",
  { featureId: "<feature-id>", customerId },
  async (run) => {
    const classification = await run.llm("classify", () => client.messages.create(...));
    const documents      = await run.tool("retrieve-documents", retrieveDocuments);
    return run.llm("generate-answer", () => client.messages.create(...));
  },
);`;

/** The step kinds a run can record. Each is a real method on AgentRun. */
export const SPAN_KINDS = [
  ["llm", "A model call"],
  ["tool", "A function or API the agent called"],
  ["retrieval", "A search over your own data"],
  ["embedding", "An embedding call"],
  ["guardrail", "A safety or policy check"],
  ["evaluation", "A scoring or judging step"],
];

// ---------------------------------------------------------------------------
// 5. Queues, workers and short-lived processes
// ---------------------------------------------------------------------------
export const RESUME_PYTHON = `# where the run starts — identifiers only, safe to put on a queue
queue.send({"trace_context": run.export_context()})

# in the worker, later, possibly on another machine
with meter.resume(job["trace_context"]) as run:
    run.tool("process-document", process_document)`;

export const RESUME_NODE = `await queue.send({ traceContext: run.exportContext() });

await meter.resume(job.traceContext, async (run) => run.tool("process", process));`;

export const FLUSH_PYTHON = `meter.flush()          # Python: blocks briefly, returns whether the queue drained`;

export const FLUSH_NODE = `await meter.flush();   // Node: await it before the process exits`;

// ---------------------------------------------------------------------------
// OpenTelemetry — an exporter you already run, pointed at Meter
// ---------------------------------------------------------------------------

/**
 * Trace-specific variables, on purpose. The generic OTEL_EXPORTER_OTLP_ENDPOINT
 * would send metrics and logs here too, and Meter only receives traces; it also
 * has the exporter append `/v1/traces` itself, which turns a pasted full URL
 * into a 404. The space after "Bearer" is written %20 because the spec parses
 * these values as W3C baggage, where a bare space is not allowed.
 */
export function otelEnvSnippet(otlpUrl: string, slug: string): string {
  return `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT=${otlpUrl}
OTEL_EXPORTER_OTLP_TRACES_HEADERS=Authorization=Bearer%20<your token>
OTEL_EXPORTER_OTLP_TRACES_PROTOCOL=http/protobuf
OTEL_SERVICE_NAME=${slug}
OTEL_RESOURCE_ATTRIBUTES=deployment.environment.name=production`;
}

export const OTEL_ENV_VARS = [
  {
    name: "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
    required: true,
    note: "Where spans go. The full URL, exactly as shown.",
  },
  {
    name: "OTEL_EXPORTER_OTLP_TRACES_HEADERS",
    required: true,
    note: "Your ingest token. Secret. Never commit it.",
  },
  {
    name: "OTEL_SERVICE_NAME",
    required: false,
    note: "Becomes the application in Meter. Defaults to “default”.",
  },
  {
    name: "OTEL_RESOURCE_ATTRIBUTES",
    required: false,
    note: "deployment.environment.name sets the environment; service.version the release.",
  },
];

export function otelCollectorSnippet(otlpUrl: string): string {
  return `exporters:
  otlphttp/meter:
    traces_endpoint: ${otlpUrl}
    headers:
      Authorization: "Bearer \${env:METER_INGEST_TOKEN}"
    compression: gzip

service:
  pipelines:
    traces:
      exporters: [otlphttp/meter]   # alongside the exporters you already have`;
}

export const OTEL_CONTENT_OFF = `# OpenLLMetry / Traceloop
TRACELOOP_TRACE_CONTENT=false

# OpenInference (Arize Phoenix, and its LangChain and LlamaIndex instrumentors)
OPENINFERENCE_HIDE_INPUTS=true
OPENINFERENCE_HIDE_OUTPUTS=true

# OpenTelemetry's own GenAI instrumentations: off by default, keep it off
OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=false`;

export const OTEL_FEATURE_PYTHON = `# pip install opentelemetry-processor-baggage
from opentelemetry import baggage, context
from opentelemetry.processor.baggage import BaggageSpanProcessor

# once, where you set up tracing: copy meter.* baggage onto every span
provider.add_span_processor(BaggageSpanProcessor(lambda key: key.startswith("meter.")))

# per run: every span started inside, including the model calls your
# instrumentation creates, carries the feature
token = context.attach(baggage.set_baggage("meter.feature_id", "<feature-id>"))
try:
    resolve_ticket(ticket)
finally:
    context.detach(token)`;

export const OTEL_FEATURE_NODE = `// npm install @opentelemetry/baggage-span-processor
import { context, propagation } from "@opentelemetry/api";
import { BaggageSpanProcessor } from "@opentelemetry/baggage-span-processor";

// once: add to spanProcessors where you construct your tracer provider
const meterBaggage = new BaggageSpanProcessor((key) => key.startsWith("meter."));

// per run
const bag = propagation.createBaggage({ "meter.feature_id": { value: "<feature-id>" } });
await context.with(propagation.setBaggage(context.active(), bag), () => resolveTicket(ticket));`;

/** Meter's own span attributes: the only place these are read from. */
export const OTEL_METER_ATTRIBUTES = [
  ["meter.feature_id", "The feature a run's cost belongs to. Without it, cost is Unattributed."],
  [
    "meter.customer_id",
    "Your own reference for the customer the run served, for cost per customer.",
  ],
  ["meter.application", "Overrides the service name, when one service hosts several applications."],
  [
    "meter.prompt_id",
    "A prompt's name, to compare what its versions cost. Never the prompt itself.",
  ],
];

/** What Meter reads from a span. Everything else, span events included, is not read. */
export const OTEL_READS = [
  ["service.name", "Application"],
  ["gen_ai.system", "Provider"],
  ["gen_ai.response.model, else gen_ai.request.model", "Model"],
  ["gen_ai.usage.input_tokens and output_tokens", "Tokens, and from them cost"],
  ["Span status", "Whether the step and its run succeeded"],
  ["The span with no parent", "The run: its name, start and end"],
];

// ---------------------------------------------------------------------------
// Splunk Observability Cloud — a second destination in the Collector it runs
// ---------------------------------------------------------------------------

/** Where the Splunk Collector keeps its configuration and environment on Linux. */
export const SPLUNK_CONFIG_FILE = "/etc/otel/collector/agent_config.yaml";
export const SPLUNK_ENV_FILE = "/etc/otel/collector/splunk-otel-collector.conf";

export const SPLUNK_HOSTS = [
  ["linux", "Linux host"],
  ["k8s", "Kubernetes (Helm)"],
] as const;
export type SplunkHost = (typeof SPLUNK_HOSTS)[number][0];

/**
 * Keys that carry prompt, response, tool or exception content, as one regular
 * expression for the Collector's attributes processor. Anchored at the start so
 * it matches by prefix, which is the same rule otel.py uses to spot content; a
 * test reads otel.py and holds the two lists together.
 */
export const SPLUNK_CONTENT_PATTERN =
  "^(gen_ai\\.(prompt|completion|input\\.messages|output\\.messages|content|system_instructions|tool\\.call\\.(arguments|result))|llm\\.(input_messages|output_messages|prompts|prompt_template)|input\\.value|output\\.value|retrieval\\.documents|tool\\.parameters|traceloop\\.entity\\.(input|output)|exception\\.(message|stacktrace))";

/** Scoped to span events: an unscoped condition would drop whole spans. */
export const SPLUNK_SPAN_EVENT_CONDITION = `IsMatch(spanevent.name, ".*")`;

/**
 * The blocks Meter adds. Its own `traces/meter` pipeline, so the processors
 * that strip content touch only Meter's copy; what goes to Splunk is left
 * exactly as it was.
 */
function splunkBlocks(otlpUrl: string): string {
  return `exporters:
  otlp_http/meter:
    traces_endpoint: ${otlpUrl}
    headers:
      Authorization: "Bearer \${env:METER_INGEST_TOKEN}"

processors:
  # Meter's copy only. What you send to Splunk is untouched.
  attributes/meter_no_content:
    actions:
      - pattern: '${SPLUNK_CONTENT_PATTERN}'
        action: delete
  filter/meter_no_span_events:
    error_mode: ignore
    trace_conditions:
      - '${SPLUNK_SPAN_EVENT_CONDITION}'

service:
  pipelines:
    traces/meter:
      receivers: [otlp]
      processors: [memory_limiter, attributes/meter_no_content, filter/meter_no_span_events, batch]
      exporters: [otlp_http/meter]`;
}

export const SPLUNK_ENV_SNIPPET = `# ${SPLUNK_ENV_FILE}
METER_INGEST_TOKEN=<your token>`;

export function splunkLinuxSnippet(otlpUrl: string): string {
  return `# ${SPLUNK_CONFIG_FILE}: add these alongside what is already there
${splunkBlocks(otlpUrl)}`;
}

export const SPLUNK_K8S_SECRET = `kubectl create secret generic meter \\
  --namespace <collector namespace> \\
  --from-literal=ingest-token=<your token>`;

export function splunkHelmSnippet(otlpUrl: string): string {
  const nested = splunkBlocks(otlpUrl)
    .split("\n")
    .map((line) => (line ? `    ${line}` : line))
    .join("\n");
  return `# values for the splunk-otel-collector chart
agent:
  extraEnvs:
    - name: METER_INGEST_TOKEN
      valueFrom:
        secretKeyRef:
          name: meter
          key: ingest-token
  config:
${nested}`;
}
