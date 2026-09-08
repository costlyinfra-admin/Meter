/**
 * Install SDK — the optional metering hook (design §9.2). A precision tier, not
 * a requirement: connectors already give per-feature cost. The SDK adds exact,
 * per-call inference numbers and is the way to split inference cost per feature
 * when you route calls through one shared API key. Detailed, follow-along setup.
 */
import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api, type Feature } from "../api";
import { Snippet } from "../components/Snippet";
import { agentPrompt } from "./installPrompt";

export function InstallSdkPage() {
  const [token, setToken] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [features, setFeatures] = useState<Feature[]>([]);

  // The endpoint the SDK posts to — shown so setup is copy-paste for THIS install.
  const ingestUrl = `${window.location.origin}/api/hook/events`;

  // The agent prompt names this tenant's real features, so the agent tags call
  // sites with ids that exist instead of inventing placeholders.
  useEffect(() => {
    api
      .listFeatures("confirmed")
      .then(setFeatures)
      .catch(() => setFeatures([]));
  }, []);

  const prompt = agentPrompt(ingestUrl, features);

  async function generate() {
    setError(null);
    try {
      setToken((await api.createHookToken()).token);
    } catch {
      setError("Could not generate an ingest token. Try again.");
    }
  }

  return (
    <div className="content">
      <div className="dash-head">
        <h1>Install SDK</h1>
      </div>

      <p className="muted">
        Optional precision upgrade — connectors already give you per-feature cost, so this is never
        required to go live. Use the SDK when you want exact, per-call inference numbers, or when
        you route calls through <strong>one shared API key</strong> (a provider's cost API can't
        tell your features apart, but the SDK can). It reports only token counts and a{" "}
        <code>feature_id</code> — <strong>never your prompts or responses</strong> — on a background
        thread, and is a no-op until configured, so it can't break your request path. Metered cost
        is reconciled against your provider bill; it sharpens the picture, never replaces the bill.
      </p>

      <div className="hint">
        <strong>Before you start:</strong> discover your features first (
        <Link to="/features" className="link">
          Features
        </Link>
        ) — each metered call is tagged with a feature's id, and anything untagged lands in the
        honest <em>Unattributed</em> bucket.
      </div>

      <section className="source-section">
        <h2>1. Generate your ingest token</h2>
        <p className="muted">
          One token per workspace. It authorizes the SDK to send usage to Meter. Set these{" "}
          <strong>two</strong> environment variables where your app runs (your <code>.env</code>,
          secrets manager, or deploy config):
        </p>
        {error && (
          <p className="error" role="alert">
            {error}
          </p>
        )}
        {token ? (
          <>
            <Snippet className="token" sensitive>{`METER_INGEST_URL=${ingestUrl}
METER_INGEST_TOKEN=${token}`}</Snippet>
            <p className="muted">Copy the token now — it isn't shown again. Keep it secret.</p>
          </>
        ) : (
          <>
            <Snippet>{`METER_INGEST_URL=${ingestUrl}
METER_INGEST_TOKEN=…    # click "Generate" to create yours`}</Snippet>
            <button className="secondary" onClick={generate}>
              Generate ingest token
            </button>
          </>
        )}
      </section>

      <section className="source-section">
        <h2>Shortcut: hand steps 2 and 3 to your coding agent</h2>
        <p className="muted">
          Copy the prompt below into Claude Code, Cursor, or whichever agent works in your
          repository. It carries everything the agent needs — the exact packages, this workspace's
          ingest URL, your real feature ids, and the rules it must not break — so it can find your
          LLM clients and wire them up itself. Read the diff before you merge it, as you would any
          other change.
          {features.length === 0 && (
            <>
              {" "}
              <strong>
                Confirm your features first — the prompt is far more useful once it can name them.
              </strong>
            </>
          )}
        </p>
        <Snippet className="agent-prompt" copyLabel="Copy prompt">
          {prompt}
        </Snippet>
        <p className="muted">
          The prompt tells the agent to read the token from your environment, never to write it into
          the code. Set it where the app already keeps its secrets.
        </p>
      </section>

      <section className="source-section">
        <h2>2. Install the SDK</h2>
        <p className="muted">
          Apache-2.0, no dependencies. Install it into the environment your app actually runs in —
          the same virtualenv, image or lockfile as the rest of your dependencies, not your laptop's
          system Python.
        </p>
        <span className="chart-title">Python</span>
        <Snippet>{`python3 -m pip install "costlyinfra-meter>=1.0"

# or, the durable version — add it to your requirements.txt / pyproject.toml:
costlyinfra-meter>=1.0`}</Snippet>
        <p className="muted">
          If pip answers <code>error: externally-managed-environment</code>, you are outside a
          virtualenv — activate your app's environment and run it again. That message is Python
          protecting the system install, not a problem with the package.
        </p>
        <span className="chart-title">Node</span>
        <Snippet>{`npm install costlyinfra-meter`}</Snippet>
        <p className="muted">
          The Node package is <strong>ESM only</strong>: use <code>import</code>. In a CommonJS
          project, load it with <code>{`const { wrap } = await import("costlyinfra-meter")`}</code>{" "}
          — <code>require()</code> will not work.
        </p>
      </section>

      <section className="source-section">
        <h2>3. Wrap your LLM client</h2>
        <p className="muted">
          Wrap the client you already use, once, with the feature the calls belong to (copy a
          feature's id from its page under{" "}
          <Link to="/features" className="link">
            Features
          </Link>
          ). Every call through it is then metered automatically, with latency — no per-call code.
          The provider is auto-detected.
        </p>
        <span className="chart-title">Python</span>
        <Snippet>{`from anthropic import Anthropic
from costlyinfra_meter import wrap

client = wrap(Anthropic(), feature_id="<feature-id>")   # reads the env vars above

# unchanged — this call is metered automatically:
resp = client.messages.create(model="claude-sonnet-4-6", messages=[...])`}</Snippet>
        <span className="chart-title">Node</span>
        <Snippet>{`import OpenAI from "openai";
import { wrap } from "costlyinfra-meter";

const client = wrap(new OpenAI(), { featureId: "<feature-id>" });

// unchanged — this call is metered automatically:
const resp = await client.chat.completions.create({ model: "gpt-4o", messages: [...] });`}</Snippet>
        <p className="muted">
          Optional: pass <code>metadata</code> at wrap time (e.g.{" "}
          <code>{`metadata={ "environment": "prod" }`}</code>) and it is attached to every call
          through that client. For cost <em>per customer</em>, where the value changes call to call,
          wrap per request with that customer's id, or record the call explicitly.
        </p>
        <div className="hint">
          <strong>Two cases the wrapper doesn't cover.</strong> Streaming and async responses are
          skipped, so record those yourself — and for that you need a meter of your own rather than
          the one <code>wrap()</code> makes internally:
          <Snippet>{`from costlyinfra_meter import Meter

meter = Meter(feature_id="<feature-id>")     # reads the same two env vars
meter.record_anthropic(resp)                 # or meter.record_openai(resp)

# In a short-lived process — a script, a job, a Lambda — the background worker
# may not get to send before the process ends. Flush before you exit:
meter.flush()`}</Snippet>
          In Node the equivalents are <code>meter.recordAnthropic(resp)</code>,{" "}
          <code>meter.recordOpenAI(resp)</code> and <code>await meter.flush()</code>.
        </div>
      </section>

      <section className="source-section">
        <h2>4. Verify it's working</h2>
        <p className="muted">
          Run your app so it makes a real model call, then open that feature under{" "}
          <Link to="/features" className="link">
            Features
          </Link>{" "}
          — within a minute or two you'll see its inference cost update, with{" "}
          <em>metered (hook)</em> as the source and an average latency. If nothing appears: confirm
          both env vars are set in the running process, and that the <code>feature_id</code> matches
          a real feature. The SDK fails silently by design, so a missing token never errors — it
          simply doesn't report.
        </p>
      </section>
    </div>
  );
}
