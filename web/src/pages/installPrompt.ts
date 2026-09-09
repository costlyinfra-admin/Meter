/**
 * The prompt a customer hands to their coding agent to wire up the SDK.
 *
 * It is generated rather than written as a static block because the two things
 * that make it work are specific to the reader: the ingest URL of *this*
 * install, and *their* feature ids. An agent given placeholders invents
 * plausible-looking ids, and a wrong id misattributes real money — so the ids
 * are listed, and the prompt says to ask rather than guess when a call site is
 * ambiguous.
 *
 * Everything it claims about the SDK is checked against the SDK: see
 * installPrompt.test.ts, which reads sdk/python and sdk/node.
 */
import type { Feature } from "../api";

/** The lowest SDK version with the queue, batching and retries. */
export const MIN_SDK = "1.0";

function featureList(features: Feature[]): string {
  if (features.length === 0) {
    return `  (No features are confirmed in Meter yet. Ask me for the feature id
  before you wrap anything — do not invent one.)`;
  }
  const width = Math.max(...features.map((f) => f.id.length));
  return features.map((f) => `  ${f.id.padEnd(width)}  ${f.name}`).join("\n");
}

export function agentPrompt(
  ingestUrl: string,
  features: Feature[],
  projectId?: string | null,
  application?: string | null,
): string {
  return `Add Meter metering to this codebase.

Meter reports per-call LLM token counts so we can see what each of our
features costs to run. Your job is to install its SDK and wrap the LLM clients
we already have. Work through it end to end, then summarise what you changed.

FIRST
Work out what you are looking at before you change anything: the language and
runtime, the package manager actually in use (npm, yarn, pnpm, Bun, pip, uv,
poetry — go by the lockfile, not by preference), and where the app reads its
configuration. Install with the manager this repository already uses.

INSTALL
- Python: costlyinfra-meter>=${MIN_SDK} (PyPI) — add it to requirements.txt / pyproject.toml,
  and install it into the virtualenv this app runs in.
- Node: costlyinfra-meter@^${MIN_SDK} (npm). It is ESM-only: import it, never require() it.
It has no dependencies and is Apache-2.0 licensed.

CONFIGURE
Environment variables, set wherever this app already keeps its secrets:
  METER_INGEST_URL=${ingestUrl}
  METER_INGEST_TOKEN=<ask me for this; it is a secret>
  METER_APPLICATION=${application || "<ask me: a short slug naming this app, e.g. support-agent>"}
  METER_ENVIRONMENT=<production | staging | development, matching this deploy>
  METER_RELEASE_VERSION=<optional; your build or release identifier>
${projectId ? `Our Meter project id is ${projectId}, for reference if you need to ask about\nthis install. It is not a credential and the SDK does not read it.\n` : ""}Also add these names to .env.example (or whatever this repository uses to
document its configuration) with EMPTY values, so the next person knows they
exist. Never put a real token in that file, never hardcode it in source, and
never commit it. The SDK is a silent no-op until the URL and token are set, so
this is safe to merge and deploy before the token exists.

${
  application
    ? `The application slug above is already decided — use it exactly as written. It
groups every workflow in this codebase.`
    : `If you do not know the application slug, ASK ME. It groups every workflow in
this codebase and is awkward to change later.`
}

WHAT TO CHANGE — two cases, and most codebases need both

1. A SINGLE MODEL CALL. Wrap the client once where it is constructed. Existing
   calls stay exactly as they are; each becomes its own one-call trace.

     # Python
     from costlyinfra_meter import Meter
     meter = Meter()                       # reads the env vars above
     client = meter.wrap(Anthropic(), feature_id="<feature-id>")

     // Node
     import { Meter } from "costlyinfra-meter";
     const meter = new Meter();
     const client = meter.wrap(new OpenAI(), { featureId: "<feature-id>" });

   wrap() returns a transparent proxy. The provider is detected from the client;
   pass provider="..." / { provider: "..." } if detection is wrong.

2. A MULTI-STEP AGENT OR WORKFLOW. If a request makes several model calls, or
   mixes model calls with retrieval and tool calls, wrap the WHOLE run so the
   steps are recorded as one trace. This is what makes "why did this run cost
   $1.42" answerable.

     # Python
     with meter.agent("resolve-ticket", feature_id="<feature-id>",
                      customer_id=customer_id) as run:
         classification = run.llm("classify", lambda: client.messages.create(...))
         documents      = run.tool("retrieve-documents", retrieve_documents)
         answer         = run.llm("generate-answer", lambda: client.messages.create(...))

     // Node
     const answer = await meter.agent(
       "resolve-ticket",
       { featureId: "<feature-id>", customerId },
       async (run) => {
         const classification = await run.llm("classify", () => client.messages.create(...));
         const documents      = await run.tool("retrieve-documents", retrieveDocuments);
         return run.llm("generate-answer", () => client.messages.create(...));
       },
     );

   Each step returns whatever your function returned, so wrapping a step does
   not change control flow. Besides llm and tool there are retrieval,
   embedding, guardrail and evaluation.

   Name steps after what they DO ("classify", "retrieve-documents"), not after
   the function that implements them.

OUR FEATURES — use these ids, they are real:
${featureList(features)}

If you cannot tell which feature a call belongs to, ask me — do not guess. A
wrong id misattributes real money.

QUEUES AND WORKERS
If a run continues in a background worker, pass the context and resume it, so
both halves are one trace:

  # Python
  context = run.export_context()          # identifiers only; safe to enqueue
  with meter.resume(job.trace_context) as run:
      run.tool("process-document", process_document)

  // Node
  await queue.send({ traceContext: run.exportContext() });
  await meter.resume(job.traceContext, async (run) => run.tool("process", process));

TWO CASES TO WATCH
1. Streaming and async responses have no usage until they finish, so a wrapped
   client skips them. Record those inside meter.agent(...) instead, where you
   control when the step ends.
2. Short-lived processes — a script, a cron job, a Lambda — can exit before the
   background worker has sent anything. Call meter.flush() (await it in Node)
   before the process ends.

RULES — these are not negotiable
- Never send prompt or response content to Meter — not prompts, responses,
  messages, tool arguments, tool results, retrieved documents, error messages
  or stack traces. The SDK has no field for any of it and the server rejects a
  payload that carries one. Do not attempt to add it. What Meter records is
  identity, counts, timing and cost: token counts, the model, latency, the
  feature, and optionally a prompt id and version.
- Metering must never break or slow the request path. Do not await delivery, do
  not add your own retries, and do not put metering in a code path whose failure
  could change what the application returns.
- Do not change prompts, model choices, or any application logic. This task adds
  observability and nothing else.
- If the SDK cannot be wired into some call site cleanly, leave it alone and
  tell me why, rather than restructuring that code to fit.
- Never commit a token, an API key, or any other credential. Never commit
  prompts, model responses, or customer data — not in code, not in fixtures,
  not in tests, not in a commit message.
- Do not make unrelated changes. No opportunistic refactoring, no reformatting
  of files you did not otherwise touch, no dependency upgrades.

WHEN YOU ARE DONE
Run whatever this repository already has — its tests, linter, type checker and
build — and fix anything your change broke. Then list every file you changed
and what you did to it, and say plainly if anything is left unfinished.`;
}

// ---------------------------------------------------------------------------
// The four guides on Install SDK
// ---------------------------------------------------------------------------
/** Which guide a reader is on. Also the value carried in the URL. */
export type AgentId = "cli" | "claude-code" | "cursor" | "codex";

export interface AgentGuide {
  id: AgentId;
  label: string;
  /** ConnectorMark type — resolves to a logo in src/logos/. */
  logo: string;
  heading: string;
  /** Where to paste it. Shown above the prompt. */
  instruction: string;
}

export const AGENT_GUIDES: AgentGuide[] = [
  {
    id: "cli",
    label: "Setup CLI",
    logo: "meter-cli",
    heading: "Install Meter automatically",
    instruction: "Run one command from your project root.",
  },
  {
    id: "claude-code",
    label: "Claude Code",
    logo: "anthropic",
    heading: "Install with Claude Code",
    instruction: "Open Claude Code at your project root and paste this prompt.",
  },
  {
    id: "cursor",
    label: "Cursor",
    logo: "cursor",
    heading: "Install with Cursor",
    instruction: "Open the repository in Cursor, start an Agent chat, and paste this prompt.",
  },
  {
    id: "codex",
    label: "Codex",
    logo: "openai",
    heading: "Install with Codex",
    instruction: "Open the repository in Codex and paste this prompt.",
  },
];

/**
 * Each agent works differently enough to be worth a closing paragraph, and no
 * more. The body above is the same for all of them — it is the part that has to
 * stay true to the SDK, and one copy of it is one thing to keep honest.
 */
const AGENT_NOTES: Record<Exclude<AgentId, "cli">, string> = {
  "claude-code": `HOW I WANT YOU TO WORK
Read the repository before you edit it, and tell me what you found — which LLM
clients exist and where — before you start changing them. Then work through the
task above end to end.`,
  cursor: `HOW I WANT YOU TO WORK
Show me the changes you propose before you apply them, or summarise them
immediately after, file by file. Stay inside the task above: no refactoring of
code you were not asked to touch, and no reformatting of untouched files. When
you are finished, build the project and confirm it still builds.`,
  codex: `HOW I WANT YOU TO WORK
You may read and modify this repository and install dependencies, using the
package manager it already uses — do not switch it. When you are finished, run
the test suite and report every file you changed and why. Do not commit
secrets, and do not make changes unrelated to this task.`,
};

/** The full prompt for one guide. `cli` has no prompt — it is a command. */
export function agentPromptFor(
  id: Exclude<AgentId, "cli">,
  ingestUrl: string,
  features: Feature[],
  projectId?: string | null,
  application?: string | null,
): string {
  return `${agentPrompt(ingestUrl, features, projectId, application)}\n\n${AGENT_NOTES[id]}`;
}

/**
 * The command the setup CLI will be, once it exists.
 *
 * Shown on the page as a PLANNED command, never as something to run: the
 * package is not published, so a copy button here would hand someone a command
 * that fails. It also carries no token — a secret on a command line ends up in
 * shell history and in any process listing on the machine.
 */
export const PLANNED_CLI_COMMAND = "npx @costlyinfra/meter-setup";

/** What the CLI is intended to do. Written as intent, not as a promise. */
export const PLANNED_CLI_STEPS = [
  "Detect the project language and package manager",
  "Find supported LLM SDKs and call sites",
  "Install the appropriate Meter package",
  "Configure Meter without committing secrets",
  "Send a test event to verify the installation",
];
