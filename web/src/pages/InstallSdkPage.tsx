/**
 * Install SDK — the optional metering hook (design §9.2). A precision tier, not
 * a requirement: connectors already give per-feature cost. The SDK adds exact,
 * per-call inference numbers and is the way to split inference cost per feature
 * when you route calls through one shared API key.
 *
 * Two ways through it: hand the work to a coding agent, or do it by hand. Both
 * end at the same place, so the token step and the verification panel sit
 * outside the tabs — they are what every route needs, whichever you took.
 *
 * Which tab you are on lives in the URL (`?tab=` / `?guide=`), so a link to a
 * particular guide works and the browser's Back button steps between them.
 */
import { useCallback, useEffect, useRef, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { api, ApiError, type Feature, type HookEvent } from "../api";
import { useAuth } from "../auth/AuthContext";
import { ConnectorMark } from "../components/ConnectorMark";
import { Snippet } from "../components/Snippet";
import { TabPanel, Tabs } from "../components/Tabs";
import {
  AGENT_GUIDES,
  agentPromptFor,
  MIN_SDK,
  PLANNED_CLI_COMMAND,
  PLANNED_CLI_STEPS,
  type AgentId,
} from "./installPrompt";

type PrimaryTab = "ai" | "manual";
type NodePm = "npm" | "yarn" | "pnpm" | "bun";

/** Install commands, one per manager. All four resolve the same package. */
const NODE_INSTALL: Record<NodePm, string> = {
  npm: "npm install costlyinfra-meter",
  yarn: "yarn add costlyinfra-meter",
  pnpm: "pnpm add costlyinfra-meter",
  bun: "bun add costlyinfra-meter",
};

const NODE_PMS: NodePm[] = ["npm", "yarn", "pnpm", "bun"];

/** How often the verification panel asks whether an event has arrived. */
const POLL_MS = 5000;

function isPrimary(value: string | null): value is PrimaryTab {
  return value === "ai" || value === "manual";
}

function isGuide(value: string | null): value is AgentId {
  return AGENT_GUIDES.some((g) => g.id === value);
}

export function InstallSdkPage() {
  const { user } = useAuth();
  const [params, setParams] = useSearchParams();
  const [token, setToken] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [features, setFeatures] = useState<Feature[]>([]);
  const [nodePm, setNodePm] = useState<NodePm>("npm");

  const tabParam = params.get("tab");
  const guideParam = params.get("guide");
  const tab: PrimaryTab = isPrimary(tabParam) ? tabParam : "ai";
  const guide: AgentId = isGuide(guideParam) ? guideParam : "cli";

  // Replace rather than push: flicking between tabs should not fill the back
  // button, while a link straight to one still lands where it says.
  const select = (next: Partial<{ tab: PrimaryTab; guide: AgentId }>) => {
    const merged = new URLSearchParams(params);
    if (next.tab) merged.set("tab", next.tab);
    if (next.guide) merged.set("guide", next.guide);
    setParams(merged, { replace: true });
  };

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

  async function generate() {
    setError(null);
    try {
      setToken((await api.createHookToken()).token);
    } catch {
      setError("Could not generate an ingest token. Try again.");
    }
  }

  return (
    <div className="content install-page">
      <div className="dash-head">
        <h1>Install SDK</h1>
      </div>

      <p className="muted">
        Optional precision upgrade — connectors already give you per-feature cost, so this is never
        required to go live. Use the SDK when you want exact, per-call inference numbers, or when
        you route calls through <strong>one shared API key</strong> (a provider's cost API can't
        tell your features apart, but the SDK can). It reports only token counts and a{" "}
        <code>feature_id</code> — <strong>never your prompts or responses</strong> — on a background
        thread, and is a no-op until configured, so it can't break your request path.
      </p>

      <div className="hint">
        <strong>Before you start:</strong> discover your features first (
        <Link to="/features" className="link">
          Features
        </Link>
        ) — each metered call is tagged with a feature's id, and anything untagged lands in the
        honest <em>Unattributed</em> bucket.
      </div>

      <TokenSection
        ingestUrl={ingestUrl}
        token={token}
        error={error}
        onGenerate={() => void generate()}
      />

      <Tabs
        items={[
          { id: "ai", label: "Setup with AI" },
          { id: "manual", label: "Manual via package manager" },
        ]}
        active={tab}
        onChange={(id) => select({ tab: id })}
        idPrefix="install"
        label="Installation method"
        className="install-tabs"
      />

      <TabPanel id="ai" idPrefix="install" active={tab === "ai"}>
        <Tabs
          items={AGENT_GUIDES.map((g) => ({
            id: g.id,
            accessibleLabel: g.label,
            label: (
              <>
                <ConnectorMark type={g.logo} name={g.label} />
                <span>{g.label}</span>
              </>
            ),
          }))}
          active={guide}
          onChange={(id) => select({ guide: id })}
          idPrefix="guide"
          label="Setup assistant"
          className="guide-tabs"
        />
        {AGENT_GUIDES.map((g) => (
          <TabPanel key={g.id} id={g.id} idPrefix="guide" active={guide === g.id}>
            {g.id === "cli" ? (
              <CliGuide heading={g.heading} />
            ) : (
              <AgentGuidePanel
                heading={g.heading}
                instruction={g.instruction}
                prompt={agentPromptFor(g.id, ingestUrl, features, user?.tenant_id)}
                hasFeatures={features.length > 0}
              />
            )}
          </TabPanel>
        ))}
      </TabPanel>

      <TabPanel id="manual" idPrefix="install" active={tab === "manual"}>
        <ManualGuide nodePm={nodePm} onNodePm={setNodePm} ingestUrl={ingestUrl} />
      </TabPanel>

      <Verification />
    </div>
  );
}

// ---------------------------------------------------------------------------
// The token, needed whichever route you take
// ---------------------------------------------------------------------------
function TokenSection({
  ingestUrl,
  token,
  error,
  onGenerate,
}: {
  ingestUrl: string;
  token: string | null;
  error: string | null;
  onGenerate: () => void;
}) {
  return (
    <section className="source-section">
      <h2>Generate your ingest token</h2>
      <p className="muted">
        One token per workspace. It authorizes the SDK to send usage to Meter. Set these{" "}
        <strong>two</strong> environment variables where your app runs (your <code>.env</code>,
        secrets manager, or deploy config) — both routes below need them.
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
          <button className="secondary" onClick={onGenerate}>
            Generate ingest token
          </button>
        </>
      )}
    </section>
  );
}

// ---------------------------------------------------------------------------
// Setup with AI
// ---------------------------------------------------------------------------
function CliGuide({ heading }: { heading: string }) {
  return (
    <section className="source-section">
      <div className="guide-head">
        <h2>{heading}</h2>
        <span className="badge-soon">Coming soon</span>
      </div>
      <p className="muted">
        Run one command from your project root. Meter will detect your runtime, package manager, and
        supported AI providers.
      </p>
      <div className="hint">
        <strong>Not available yet.</strong> The command below is what it will be — the package is
        not published, so there is nothing to run today and no copy button for it. Use one of the
        agent guides, or the manual route, in the meantime.
      </div>
      <span className="chart-title">Planned command</span>
      {/* Deliberately not a Snippet: a copy button here would hand someone a
          command that does not resolve. Shown, not offered. */}
      <pre className="snippet snippet-disabled" aria-describedby="cli-soon">
        {PLANNED_CLI_COMMAND}
      </pre>
      <p className="muted" id="cli-soon">
        No token goes on that command line. A secret typed into a shell ends up in its history and
        in any process listing on the machine — the CLI will read it the way the SDK does, from the
        environment.
      </p>
      <p className="muted">It will:</p>
      <ul className="cli-steps muted">
        {PLANNED_CLI_STEPS.map((step) => (
          <li key={step}>{step}</li>
        ))}
      </ul>
    </section>
  );
}

function AgentGuidePanel({
  heading,
  instruction,
  prompt,
  hasFeatures,
}: {
  heading: string;
  instruction: string;
  prompt: string;
  hasFeatures: boolean;
}) {
  return (
    <section className="source-section">
      <h2>{heading}</h2>
      <p className="muted">
        {instruction} It carries everything the agent needs — the exact packages, this workspace's
        ingest URL, your real feature ids, and the rules it must not break. Read the diff before you
        merge it, as you would any other change.
      </p>
      {!hasFeatures && (
        <div className="hint">
          <strong>Confirm your features first.</strong> The prompt is far more useful once it can
          name them — otherwise the agent has to stop and ask you for every id.
        </div>
      )}
      <Snippet className="agent-prompt" copyLabel="Copy prompt">
        {prompt}
      </Snippet>
      <p className="muted">
        The prompt tells the agent to read the token from your environment, never to write it into
        the code — so nothing secret is in what you just copied.
      </p>
    </section>
  );
}

// ---------------------------------------------------------------------------
// Manual
// ---------------------------------------------------------------------------
function ManualGuide({
  nodePm,
  onNodePm,
  ingestUrl,
}: {
  nodePm: NodePm;
  onNodePm: (pm: NodePm) => void;
  ingestUrl: string;
}) {
  return (
    <>
      <section className="source-section">
        <h2>1. Install the package</h2>
        <p className="muted">
          Apache-2.0, no dependencies. Install it into the environment your app actually runs in —
          the same virtualenv, image or lockfile as the rest of your dependencies, not your laptop's
          system Python.
        </p>
        <span className="chart-title">Python</span>
        <Snippet>{`python3 -m pip install "costlyinfra-meter>=${MIN_SDK}"

# or, the durable version — add it to your requirements.txt / pyproject.toml:
costlyinfra-meter>=${MIN_SDK}`}</Snippet>
        <p className="muted">
          If pip answers <code>error: externally-managed-environment</code>, you are outside a
          virtualenv — activate your app's environment and run it again. That message is Python
          protecting the system install, not a problem with the package.
        </p>

        <span className="chart-title">Node</span>
        <div className="pm-switch" role="group" aria-label="Package manager">
          {NODE_PMS.map((pm) => (
            <button
              key={pm}
              type="button"
              className={pm === nodePm ? "pm-option active" : "pm-option"}
              aria-pressed={pm === nodePm}
              onClick={() => onNodePm(pm)}
            >
              {pm}
            </button>
          ))}
        </div>
        <Snippet>{NODE_INSTALL[nodePm]}</Snippet>
        <p className="muted">
          The Node package is <strong>ESM only</strong>: use <code>import</code>. In a CommonJS
          project, load it with <code>{`const { wrap } = await import("costlyinfra-meter")`}</code>{" "}
          — <code>require()</code> will not work.
        </p>
      </section>

      <section className="source-section">
        <h2>2. Configure the environment</h2>
        <p className="muted">
          The same two variables from the token step above, set where your app already keeps its
          secrets. The SDK is a silent no-op until both are present, so deploying before the token
          exists is safe.
        </p>
        <Snippet>{`METER_INGEST_URL=${ingestUrl}
METER_INGEST_TOKEN=<your token>`}</Snippet>
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
      </section>

      <section className="source-section">
        <h2>4. Instrument what the wrapper skips</h2>
        <p className="muted">
          Streaming and async responses are not metered by <code>wrap()</code>. Record those
          yourself — and for that you need a meter of your own rather than the one{" "}
          <code>wrap()</code> makes internally:
        </p>
        <Snippet>{`from costlyinfra_meter import Meter

meter = Meter(feature_id="<feature-id>")     # reads the same two env vars
meter.record_anthropic(resp)                 # or meter.record_openai(resp)

# In a short-lived process — a script, a job, a Lambda — the background worker
# may not get to send before the process ends. Flush before you exit:
meter.flush()`}</Snippet>
        <p className="muted">
          In Node the equivalents are <code>meter.recordAnthropic(resp)</code>,{" "}
          <code>meter.recordOpenAI(resp)</code> and <code>await meter.flush()</code>.
        </p>
      </section>
    </>
  );
}

// ---------------------------------------------------------------------------
// Verification — shared by both routes
// ---------------------------------------------------------------------------
function Verification() {
  const [event, setEvent] = useState<HookEvent | null>(null);
  const [failed, setFailed] = useState(false);
  const timer = useRef<ReturnType<typeof setTimeout>>();

  const stop = useCallback(() => clearTimeout(timer.current), []);

  useEffect(() => {
    let live = true;

    async function check() {
      try {
        const { event: found } = await api.recentHookEvent();
        if (!live) return;
        setFailed(false);
        if (found) {
          setEvent(found); // arrived — stop asking
          return;
        }
      } catch (err) {
        if (!live) return;
        setFailed(true);
        // A dead session will never start answering, so stop rather than
        // hammering an endpoint that is going to 401 forever.
        if (err instanceof ApiError && err.status === 401) return;
      }
      if (live) timer.current = setTimeout(() => void check(), POLL_MS);
    }

    void check();
    return () => {
      live = false;
      stop();
    };
  }, [stop]);

  return (
    <section className="source-section verify-section">
      <h2>Test instrumentation</h2>
      {event ? (
        <>
          <p className="verify-state ok" role="status">
            <span className="verify-dot ok" aria-hidden /> Events received
          </p>
          <dl className="verify-facts">
            <div>
              <dt>Feature</dt>
              <dd>{event.feature_name ?? "Unattributed"}</dd>
            </div>
            <div>
              <dt>Provider</dt>
              <dd>{event.provider}</dd>
            </div>
            <div>
              <dt>Model</dt>
              <dd>{event.model ?? "—"}</dd>
            </div>
            <div>
              <dt>Last received</dt>
              <dd>{new Date(event.received_at).toLocaleString()}</dd>
            </div>
          </dl>
          <p className="muted">
            Metered cost is reconciled against your provider bill, so it sharpens the picture rather
            than replacing it. Open the feature under{" "}
            <Link to="/features" className="link">
              Features
            </Link>{" "}
            to see its inference cost with <em>metered (hook)</em> as the source.
          </p>
        </>
      ) : (
        <>
          <p className="verify-state" role="status">
            <span className="verify-dot" aria-hidden /> Waiting for your first Meter event…
          </p>
          <p className="muted">
            Run your app so it makes a real model call. This updates on its own — no need to reload.
            If nothing arrives: check both environment variables are set{" "}
            <em>in the running process</em>, and that the <code>feature_id</code> matches a real
            feature. The SDK fails silently by design, so a missing token never errors — it simply
            does not report.
          </p>
          {failed && (
            <p className="muted" role="status">
              Could not reach Meter just now — still trying.
            </p>
          )}
        </>
      )}
    </section>
  );
}
