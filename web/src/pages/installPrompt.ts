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
Two environment variables, set wherever this app already keeps its secrets:
  METER_INGEST_URL=${ingestUrl}
  METER_INGEST_TOKEN=<ask me for this; it is a secret>
${projectId ? `Our Meter project id is ${projectId}, for reference if you need to ask about\nthis install. It is not a credential and the SDK does not read it.\n` : ""}Also add METER_INGEST_URL and METER_INGEST_TOKEN to .env.example (or whatever
this repository uses to document its configuration) with EMPTY values, so the
next person knows they exist. Never put a real token in that file.
Never hardcode the token in source and never commit it. The SDK is a silent
no-op until both variables are set, so this is safe to merge and deploy before
the token exists.

WHAT TO CHANGE
Find every place this codebase constructs an LLM client — Anthropic, OpenAI,
Google GenAI, or any OpenAI-compatible client — and wrap it once, where it is
constructed, with the feature its calls belong to. Do not add per-call code.

  # Python
  from costlyinfra_meter import wrap
  client = wrap(Anthropic(), feature_id="<feature-id>")

  // Node
  import { wrap } from "costlyinfra-meter";
  const client = wrap(new OpenAI(), { featureId: "<feature-id>" });

wrap() returns a transparent proxy. Existing calls stay exactly as they are and
are metered automatically, with latency. The provider is detected from the
client; pass provider="..." / { provider: "..." } only if detection is wrong.

OUR FEATURES — use these ids, they are real:
${featureList(features)}

If one client serves several features, wrap it at each call site instead of
once at construction. If you cannot tell which feature a call belongs to, ask
me — do not guess. A wrong id misattributes real money.

TWO CASES wrap() DOES NOT COVER
1. Streaming and async responses are skipped. Record those explicitly, with a
   meter of your own:
     # Python
     from costlyinfra_meter import Meter
     meter = Meter(feature_id="<feature-id>")
     meter.record_anthropic(resp)   # or meter.record_openai(resp)
     // Node
     import { Meter } from "costlyinfra-meter";
     const meter = new Meter("<feature-id>");
     meter.recordAnthropic(resp);   // or meter.recordOpenAI(resp)
2. Short-lived processes — a script, a cron job, a Lambda — can exit before the
   background worker has sent anything. Call meter.flush() (await it in Node)
   before the process ends.

OPTIONAL
metadata passed at wrap time is attached to every call through that client:
  wrap(client, feature_id="...", metadata={"environment": "prod"})
  wrap(client, { featureId: "...", metadata: { environment: "prod" } })
For cost per customer, where the value changes from call to call, either build
the wrapped client per request with that customer's id, or record the call
explicitly with meter.record_*(resp, metadata={"customer_id": ...}).

RULES — these are not negotiable
- Never send prompt or response text to Meter. The SDK sends token counts,
  the model name, latency and the feature id. Keep it that way.
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
): string {
  return `${agentPrompt(ingestUrl, features, projectId)}\n\n${AGENT_NOTES[id]}`;
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
