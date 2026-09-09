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
