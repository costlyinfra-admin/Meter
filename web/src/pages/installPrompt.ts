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

export function agentPrompt(ingestUrl: string, features: Feature[]): string {
  return `Add Meter metering to this codebase.

Meter reports per-call LLM token counts so we can see what each of our
features costs to run. Your job is to install its SDK and wrap the LLM clients
we already have. Work through it end to end, then summarise what you changed.

INSTALL
- Python: costlyinfra-meter>=${MIN_SDK} (PyPI) — add it to requirements.txt / pyproject.toml,
  and install it into the virtualenv this app runs in.
- Node: costlyinfra-meter@^${MIN_SDK} (npm). It is ESM-only: import it, never require() it.
It has no dependencies and is Apache-2.0 licensed.

CONFIGURE
Two environment variables, set wherever this app already keeps its secrets:
  METER_INGEST_URL=${ingestUrl}
  METER_INGEST_TOKEN=<ask me for this; it is a secret>
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
  tell me why, rather than restructuring that code to fit.`;
}
