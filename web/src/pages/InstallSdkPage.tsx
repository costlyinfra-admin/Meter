/**
 * Install SDK — the optional metering hook (design §9.2). A precision tier, not
 * a requirement: connectors already give per-feature cost. The SDK adds exact,
 * per-call inference numbers and is the way to split inference cost per feature
 * when you route calls through one shared API key.
 *
 * Three ways through it: hand the work to a coding agent, do it by hand, or
 * point an OpenTelemetry exporter you already run at Meter. All three end at the
 * same place, so the token step and the verification panel sit outside the
 * tabs — they are what every route needs, whichever you took.
 *
 * Which tab you are on lives in the URL (`?tab=` / `?guide=`), so a link to a
 * particular guide works and the browser's Back button steps between them.
 */
import { useCallback, useEffect, useRef, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { api, ApiError, type Feature, type HookEvent, type OtelTrace } from "../api";
import { useAuth } from "../auth/AuthContext";
import { ConnectorMark } from "../components/ConnectorMark";
import { Snippet } from "../components/Snippet";
import { TabPanel, Tabs } from "../components/Tabs";
import {
  AGENT_NODE,
  AGENT_PYTHON,
  CAPTURE_ENV_VARS,
  CAPTURE_NODE,
  CAPTURE_PYTHON,
  ENV_VARS,
  envSnippet,
  FLUSH_NODE,
  FLUSH_PYTHON,
  MIN_SDK_CAPTURE,
  normalizeSlug,
  OTEL_CONTENT_OFF,
  OTEL_ENV_VARS,
  OTEL_FEATURE_NODE,
  OTEL_FEATURE_PYTHON,
  OTEL_METER_ATTRIBUTES,
  OTEL_READS,
  otelCollectorSnippet,
  otelEnvSnippet,
  RESUME_NODE,
  SPLUNK_CONFIG_FILE,
  SPLUNK_ENV_FILE,
  SPLUNK_ENV_SNIPPET,
  SPLUNK_HOSTS,
  SPLUNK_K8S_SECRET,
  splunkHelmSnippet,
  splunkLinuxSnippet,
  type SplunkHost,
  RESUME_PYTHON,
  SPAN_KINDS,
  suggestSlug,
  WRAP_NODE,
  WRAP_PYTHON,
} from "./installSnippets";
import {
  AGENT_GUIDES,
  agentPromptFor,
  DEFAULT_GUIDE,
  MIN_SDK,
  PLANNED_CLI_COMMAND,
  PLANNED_CLI_STEPS,
  type AgentId,
} from "./installPrompt";

/** The warning mark on a hint block. Decorative: the block already reads as a
 *  caution from its colour and its "Before you start:" lead, and announcing
 *  "alert" before that sentence would only repeat it. */
function AlertIcon() {
  return (
    <svg
      className="hint-icon"
      viewBox="0 0 24 24"
      width="18"
      height="18"
      aria-hidden
      focusable="false"
      fill="none"
      stroke="currentColor"
    >
      <path
        strokeWidth="2"
        strokeLinecap="round"
        strokeLinejoin="round"
        d="M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0Z"
      />
      <path strokeWidth="2" strokeLinecap="round" d="M12 9v4" />
      <path strokeWidth="2" strokeLinecap="round" d="M12 17h.01" />
    </svg>
  );
}

type PrimaryTab = "ai" | "manual" | "otel" | "splunk";
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
  return value === "ai" || value === "manual" || value === "otel" || value === "splunk";
}

function isGuide(value: string | null): value is AgentId {
  return AGENT_GUIDES.some((g) => g.id === value);
}

export function InstallSdkPage() {
  const { user, loading: authLoading } = useAuth();
  const [params, setParams] = useSearchParams();
  const [token, setToken] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [features, setFeatures] = useState<Feature[]>([]);
  const [nodePm, setNodePm] = useState<NodePm>("npm");
  // The application slug, which groups every workflow this codebase reports.
  // Held raw so the field is typeable ("support " on the way to "support-agent"
  // must not collapse under the cursor); normalized only where it is used.
  const [slugDraft, setSlugDraft] = useState<string>("");
  const [slugTouched, setSlugTouched] = useState(false);
  // A ref as well as state: the suggestion effect must know whether someone has
  // typed WITHOUT re-running every keystroke, and a late-arriving suggestion
  // must never land on top of what they typed.
  const slugEdited = useRef(false);

  const tabParam = params.get("tab");
  const guideParam = params.get("guide");
  const tab: PrimaryTab = isPrimary(tabParam) ? tabParam : "ai";
  const guide: AgentId = isGuide(guideParam) ? guideParam : DEFAULT_GUIDE;

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
  // Where an OpenTelemetry exporter sends spans, for the same reason.
  const otlpUrl = `${window.location.origin}/api/otel/v1/traces`;

  // The agent prompt names this tenant's real features, so the agent tags call
  // sites with ids that exist instead of inventing placeholders.
  useEffect(() => {
    api
      .listFeatures("confirmed")
      .then(setFeatures)
      .catch(() => setFeatures([]));
  }, []);

  // Suggest a slug rather than demanding one. An organization that has already
  // reported traces has the answer in its own data, so reuse the application it
  // is already sending to instead of inventing a second name for it; a first
  // install has nothing to reuse and falls back to the organization's name.
  useEffect(() => {
    // Wait for the session. Suggesting before the org name has loaded would
    // fill the field with the generic fallback and then have to correct it,
    // which reads as the page changing its mind under the cursor.
    if (authLoading) return;
    let live = true;
    void (async () => {
      let suggestion = "";
      try {
        const { applications } = await api.aiApplications();
        suggestion = applications[0]?.slug ?? "";
      } catch {
        // No answer here is not an error — it only means there is nothing to
        // reuse, and the organization's name still gives a suggestion.
      }
      if (!suggestion) suggestion = suggestSlug(user?.org_name);
      // Never overwrite what someone has typed, however slow the fetch was.
      if (live && !slugEdited.current) setSlugDraft(suggestion);
    })();
    return () => {
      live = false;
    };
  }, [authLoading, user?.org_name]);

  const slug = normalizeSlug(slugDraft) || suggestSlug(null);
  const verifyRoute: VerifyRoute = tab === "otel" || tab === "splunk" ? "otel" : "sdk";

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
        The SDK is optional. Use it to track exact costs for each AI call, separate features that
        share an API key, and see the cost of multi-step{" "}
        <Link to="/traces" className="link">
          runs
        </Link>
        .
      </p>
      <p className="muted">
        It sends usage data such as token counts, model, latency, cost, and feature ID. It{" "}
        <strong>never</strong> sends prompts, responses, tool arguments, or retrieved documents,
        unless your organization turns on Prompt optimization and a developer switches capture on.
        Reporting happens in the background and won't affect your application.
      </p>

      <div className="hint hint-with-icon">
        <AlertIcon />
        <span>
          <strong>Before you start:</strong> discover your features first (
          <Link to="/features" className="link">
            Features
          </Link>
          ) — each metered call is tagged with a feature's id, and anything untagged lands in the
          honest <em>Unattributed</em> bucket.
        </span>
      </div>

      <TokenSection
        ingestUrl={ingestUrl}
        token={token}
        error={error}
        onGenerate={() => void generate()}
        slugDraft={slugDraft}
        slug={slug}
        slugTouched={slugTouched}
        onSlug={(value) => {
          slugEdited.current = true;
          setSlugTouched(true);
          setSlugDraft(value);
        }}
      />

      <Tabs
        items={[
          { id: "ai", label: "Setup with AI" },
          { id: "manual", label: "Manual via package manager" },
          { id: "otel", label: "OpenTelemetry" },
          { id: "splunk", label: "Splunk" },
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
                prompt={agentPromptFor(g.id, ingestUrl, features, user?.tenant_id, slug)}
                hasFeatures={features.length > 0}
              />
            )}
          </TabPanel>
        ))}
      </TabPanel>

      <TabPanel id="manual" idPrefix="install" active={tab === "manual"}>
        <ManualGuide nodePm={nodePm} onNodePm={setNodePm} ingestUrl={ingestUrl} slug={slug} />
      </TabPanel>

      <TabPanel id="otel" idPrefix="install" active={tab === "otel"}>
        <OtelGuide otlpUrl={otlpUrl} slug={slug} />
      </TabPanel>

      <TabPanel id="splunk" idPrefix="install" active={tab === "splunk"}>
        <SplunkGuide otlpUrl={otlpUrl} />
      </TabPanel>

      {/* Keyed by route: what counts as "arrived" differs, and a panel that had
          seen SDK events must not carry that green light onto the OTel guide. */}
      <Verification key={verifyRoute} route={verifyRoute} />
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
  slugDraft,
  slug,
  slugTouched,
  onSlug,
}: {
  ingestUrl: string;
  token: string | null;
  error: string | null;
  onGenerate: () => void;
  slugDraft: string;
  slug: string;
  slugTouched: boolean;
  onSlug: (value: string) => void;
}) {
  // The typed text and the slug the SDK will actually send diverge while
  // someone is typing ("Support Agent" -> "support-agent"). Showing what it
  // becomes is kinder than silently rewriting the field under the cursor or
  // rejecting a capital letter — the server normalizes it either way.
  const normalizedDiffers = slugDraft.trim() !== "" && slugDraft.trim() !== slug;
  return (
    <section className="source-section">
      <h2>Name this application, then generate a token</h2>
      <p className="muted">
        Each setup needs an application name and an ingest token. The application groups your usage
        under{" "}
        <Link to="/applications" className="link">
          Applications
        </Link>
        , while the token securely sends usage data to Meter. Use one token per workspace.
      </p>

      <div className="settings-field">
        <label htmlFor="app-slug">Application</label>
        <input
          id="app-slug"
          className="slug-input"
          value={slugDraft}
          onChange={(e) => onSlug(e.target.value)}
          placeholder="support-agent"
          spellCheck={false}
          autoCapitalize="none"
          autoCorrect="off"
        />
        <span className="settings-hint muted">
          {normalizedDiffers ? (
            <>
              The SDK will send <code>{slug}</code>. Slugs are lowercase with dashes, because they
              appear in URLs.
            </>
          ) : slugTouched ? (
            <>Name the app, not your company. You can have several.</>
          ) : (
            <>
              We suggested this name based on your codebase. Change it now if needed—changing it
              later will create a new application.
            </>
          )}
        </span>
      </div>

      {error && (
        <p className="error" role="alert">
          {error}
        </p>
      )}
      {token ? (
        <>
          <Snippet className="token" sensitive>
            {envSnippet(ingestUrl, slug, token)}
          </Snippet>
          <p className="muted">Copy the token now. It is not shown again. Keep it secret.</p>
        </>
      ) : (
        <>
          <Snippet>
            {envSnippet(ingestUrl, slug).replace(
              "METER_INGEST_TOKEN=<your token>",
              'METER_INGEST_TOKEN=…    # click "Generate" to create yours',
            )}
          </Snippet>
          <button className="secondary" onClick={onGenerate}>
            Generate ingest token
          </button>
        </>
      )}
      <p className="muted">
        Meter creates the application the first time an event names it. There is nothing to set up
        here first.
      </p>
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
        Run one command from your project root. Meter detects your runtime, package manager, and AI
        providers, then sets everything up.
      </p>
      <div className="hint hint-with-icon">
        <AlertIcon />
        <span>
          <strong>Not available yet.</strong> The package is not published, so this command will not
          run today and there is no copy button for it. Use an agent guide or the manual route for
          now.
        </span>
      </div>
      <span className="chart-title">Planned command</span>
      {/* Deliberately not a Snippet: a copy button here would hand someone a
          command that does not resolve. Shown, not offered. */}
      <pre className="snippet snippet-disabled" aria-describedby="cli-soon">
        {PLANNED_CLI_COMMAND}
      </pre>
      <p className="muted" id="cli-soon">
        The command takes no token. A secret typed into a shell is saved in its history and visible
        in process listings. The CLI will read the token from your environment, the way the SDK
        does.
      </p>
      <p className="muted">When it ships, it will:</p>
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
        {instruction} The prompt includes the exact packages, your ingest URL, your real feature
        IDs, and the rules the agent must follow. Review the diff before you merge, as you would any
        other change.
      </p>
      {!hasFeatures && (
        <div className="hint hint-with-icon">
          <AlertIcon />
          <span>
            <strong>Confirm your features first.</strong> Without them, the agent has to stop and
            ask you for every feature ID.
          </span>
        </div>
      )}
      <Snippet className="agent-prompt" copyLabel="Copy prompt">
        {prompt}
      </Snippet>
      <p className="muted">
        The prompt tells the agent to read the token from your environment and never to write it in
        code. Nothing secret is in what you just copied.
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
  slug,
}: {
  nodePm: NodePm;
  onNodePm: (pm: NodePm) => void;
  ingestUrl: string;
  slug: string;
}) {
  return (
    <>
      <section className="source-section">
        <h2>1. Install the package</h2>
        <p className="muted">
          Apache-2.0 and no dependencies. Install it into the environment your app runs in: the same
          virtualenv, image, or lockfile as your other dependencies, not your system Python.
        </p>
        <span className="chart-title">Python</span>
        <Snippet>{`python3 -m pip install "costlyinfra-meter>=${MIN_SDK}"

# or, the durable version — add it to your requirements.txt / pyproject.toml:
costlyinfra-meter>=${MIN_SDK}`}</Snippet>
        <p className="muted">
          If pip reports <code>error: externally-managed-environment</code>, you are outside a
          virtualenv. Activate your app's environment and run it again. That message is Python
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
          The Node package is <strong>ESM only</strong>, so use <code>import</code>. In a CommonJS
          project, load it with <code>{`const { Meter } = await import("costlyinfra-meter")`}</code>
          . <code>require()</code> will not work.
        </p>
      </section>

      <section className="source-section">
        <h2>2. Configure the environment</h2>
        <p className="muted">
          Set these where your app keeps its secrets. The SDK does nothing until the URL and token
          are both set, so it is safe to deploy before the token exists.
        </p>
        {/* The placeholder, never the real token. The generated token is shown
            once, above, inside a snippet marked for session-replay masking —
            repeating it here would put a live secret outside that masking. */}
        <Snippet>{envSnippet(ingestUrl, slug)}</Snippet>
        <dl className="env-table">
          {ENV_VARS.map((v) => (
            <div key={v.name}>
              <dt>
                <code>{v.name}</code>
                {v.required && <span className="badge-required">Required</span>}
              </dt>
              <dd className="muted">{v.note}</dd>
            </div>
          ))}
        </dl>
        <p className="muted">
          Add these names to your <code>.env.example</code> with empty values, so the next person
          knows they exist. Never commit a real token.
        </p>
      </section>

      <section className="source-section">
        <h2>3. Wrap your LLM client</h2>
        <p className="muted">
          For a single model call, wrap the client once where you create it and pass the feature the
          calls belong to. Copy a feature's ID from its page under{" "}
          <Link to="/features" className="link">
            Features
          </Link>
          . Every call through the wrapped client is then metered, with no per-call code, and each
          becomes its own one-step trace. The provider is detected from the client. Pass{" "}
          <code>provider=</code> if you have subclassed or wrapped it and detection is wrong.
        </p>
        <span className="chart-title">Python</span>
        <Snippet>{WRAP_PYTHON}</Snippet>
        <span className="chart-title">Node</span>
        <Snippet>{WRAP_NODE}</Snippet>
      </section>

      <section className="source-section">
        <h2>4. Record a multi-step run</h2>
        <p className="muted">
          If one request makes several model calls, or mixes model calls with retrieval and tools,
          wrap the <strong>whole run</strong>. Its steps are recorded as one trace, which is what
          makes <em>“why did this run cost $1.42”</em> answerable and what the{" "}
          <Link to="/traces" className="link">
            Traces
          </Link>{" "}
          page draws.
        </p>
        <span className="chart-title">Python</span>
        <Snippet>{AGENT_PYTHON}</Snippet>
        <span className="chart-title">Node</span>
        <Snippet>{AGENT_NODE}</Snippet>
        <p className="muted">
          Each step returns whatever your function returned, so wrapping it does not change your
          control flow. Leaving the block ends the trace, including on an exception, so a crashed
          agent is recorded as failed instead of looking stuck. Name steps after what they{" "}
          <em>do</em>, like <code>classify</code> or <code>retrieve-documents</code>, not after the
          function that implements them.
        </p>
        <dl className="env-table">
          {SPAN_KINDS.map(([kind, what]) => (
            <div key={kind}>
              <dt>
                <code>run.{kind}(…)</code>
              </dt>
              <dd className="muted">{what}</dd>
            </div>
          ))}
        </dl>
      </section>

      <section className="source-section">
        <h2>5. Queues, workers and short-lived processes</h2>
        <p className="muted">
          If a run continues in a background worker, export the context and resume it there so both
          halves are one trace. The context carries identifiers only, with no prompts and no
          customer data, so it is safe to put on a queue.
        </p>
        <span className="chart-title">Python</span>
        <Snippet>{RESUME_PYTHON}</Snippet>
        <span className="chart-title">Node</span>
        <Snippet>{RESUME_NODE}</Snippet>
        <p className="muted">
          Two cases to watch. <strong>Streaming and async responses</strong> have no usage until
          they finish, so a wrapped client skips them. Record those inside{" "}
          <code>meter.agent(…)</code>, where you control when the step ends. And a{" "}
          <strong>short-lived process</strong>, such as a script, cron job, or Lambda, can exit
          before the background worker sends anything:
        </p>
        <Snippet>{`${FLUSH_PYTHON}\n${FLUSH_NODE}`}</Snippet>
      </section>

      <PromptCaptureGuide />
    </>
  );
}

/**
 * How a developer turns on prompt capture. Shown only once the organization has
 * agreed to Prompt optimization: before that, teaching a switch that would send
 * nothing is noise, and it reads like an invitation to collect prompts that
 * nobody has consented to.
 */
function PromptCaptureGuide() {
  const [consented, setConsented] = useState(false);

  useEffect(() => {
    let live = true;
    Promise.resolve()
      .then(() => api.promptConsent())
      .then((status) => {
        if (live) setConsented(Boolean(status?.consent));
      })
      .catch(() => {
        // Not knowing is the same as not consented: the section stays hidden.
      });
    return () => {
      live = false;
    };
  }, []);

  if (!consented) return null;
  return (
    <section className="source-section">
      <h2>6. Prompt optimization (optional)</h2>
      <p className="muted">
        Your organization has turned on Prompt optimization. To let a wrapped client send prompt
        samples, switch capture on and name the prompt. Samples are sent only for features switched
        on in{" "}
        <Link to="/settings#privacy" className="link">
          Settings
        </Link>
        , and only text: never tool calls, tool results, images or files. Requires version{" "}
        {MIN_SDK_CAPTURE} or later.
      </p>
      <span className="chart-title">Python</span>
      <Snippet>{CAPTURE_PYTHON}</Snippet>
      <span className="chart-title">Node</span>
      <Snippet>{CAPTURE_NODE}</Snippet>
      <dl className="env-table">
        {CAPTURE_ENV_VARS.map((v) => (
          <div key={v.name}>
            <dt>
              <code>{v.name}</code>
            </dt>
            <dd className="muted">{v.note}</dd>
          </div>
        ))}
      </dl>
      <p className="muted">
        Change the prompt version whenever the prompt changes, so a saving can be checked against
        the version that earned it. Pass <code>redact</code> to scrub known identifiers before a
        sample leaves. Capture works for Anthropic <code>messages.create</code> and OpenAI{" "}
        <code>chat.completions.create</code> through <code>wrap</code>.
      </p>
    </section>
  );
}

// ---------------------------------------------------------------------------
// OpenTelemetry
// ---------------------------------------------------------------------------
function OtelGuide({ otlpUrl, slug }: { otlpUrl: string; slug: string }) {
  return (
    <>
      <section className="source-section">
        <h2>1. Point your exporter at Meter</h2>
        <p className="muted">
          If your app already emits OpenTelemetry traces, from OpenLLMetry, OpenInference, or
          OpenTelemetry's own GenAI instrumentation, there is nothing to install. Add Meter as a
          trace exporter using the application name and token above.
        </p>
        {/* The placeholder, never the real token: the generated one is shown
            once, above, inside the snippet marked for session-replay masking. */}
        <Snippet>{otelEnvSnippet(otlpUrl, slug)}</Snippet>
        <dl className="env-table">
          {OTEL_ENV_VARS.map((v) => (
            <div key={v.name}>
              <dt>
                <code>{v.name}</code>
                {v.required && <span className="badge-required">Required</span>}
              </dt>
              <dd className="muted">{v.note}</dd>
            </div>
          ))}
        </dl>
        <p className="muted">
          Use the <code>TRACES</code> variables, not the general{" "}
          <code>OTEL_EXPORTER_OTLP_ENDPOINT</code>. Meter only receives traces, and the general one
          would send your metrics and logs here too. Write the space after <code>Bearer</code> as{" "}
          <code>%20</code>.
        </p>

        <span className="chart-title">Already running a Collector?</span>
        <Snippet>{otelCollectorSnippet(otlpUrl)}</Snippet>
        <p className="muted">
          Add Meter next to the exporters you have. Nothing else in your pipeline changes.
        </p>
      </section>

      <section className="source-section">
        <h2>2. Turn content capture off</h2>
        <p className="muted">
          Meter stores token counts, model, timing and cost. It <strong>never</strong> stores
          prompts, responses, tool arguments or retrieved documents. Anything like that in a span is
          discarded the moment it arrives, and your exporter is told so. But discarded on arrival
          still means it crossed the network, so switch it off where it starts:
        </p>
        <Snippet>{OTEL_CONTENT_OFF}</Snippet>
      </section>

      <section className="source-section">
        <h2>3. Attribute runs to a feature</h2>
        <p className="muted">
          If a whole service is one feature, say so once. Copy the feature's ID from its page under{" "}
          <Link to="/features" className="link">
            Features
          </Link>
          :
        </p>
        <Snippet>{`OTEL_RESOURCE_ATTRIBUTES=deployment.environment.name=production,meter.feature_id=<feature-id>`}</Snippet>
        <p className="muted">
          If one service serves several features, set the feature per run with{" "}
          <strong>baggage</strong>. OpenTelemetry does not copy a span's attributes onto its
          children, and the spans for your model calls are made by your instrumentation library, not
          by you. Baggage is copied onto every span started inside it, including those.
        </p>
        <span className="chart-title">Python</span>
        <Snippet>{OTEL_FEATURE_PYTHON}</Snippet>
        <span className="chart-title">Node</span>
        <Snippet>{OTEL_FEATURE_NODE}</Snippet>
        <p className="muted">
          A run with no feature still counts. Its cost lands in the <em>Unattributed</em> bucket
          rather than disappearing.
        </p>
        <dl className="env-table">
          {OTEL_METER_ATTRIBUTES.map(([name, what]) => (
            <div key={name}>
              <dt>
                <code>{name}</code>
              </dt>
              <dd className="muted">{what}</dd>
            </div>
          ))}
        </dl>
      </section>

      <section className="source-section">
        <h2>4. What Meter reads</h2>
        <p className="muted">
          A fixed list, and nothing else. An attribute not on it, and every span event, is never
          read, so a convention that starts carrying content tomorrow is already excluded.
        </p>
        <dl className="env-table">
          {OTEL_READS.map(([source, becomes]) => (
            <div key={source}>
              <dt>
                <code>{source}</code>
              </dt>
              <dd className="muted">{becomes}</dd>
            </div>
          ))}
        </dl>
        <p className="muted">
          Model calls are priced with the same rates as the SDK and reconciled against your provider
          bill the same way. A run instrumented both ways is still one run.
        </p>
      </section>
    </>
  );
}

// ---------------------------------------------------------------------------
// Splunk Observability Cloud
// ---------------------------------------------------------------------------
function SplunkGuide({ otlpUrl }: { otlpUrl: string }) {
  const [host, setHost] = useState<SplunkHost>("linux");
  return (
    <>
      <section className="source-section">
        <h2>1. Add Meter to your Splunk Collector</h2>
        <p className="muted">
          If you send traces to Splunk Observability Cloud, they already pass through the Splunk
          Distribution of the OpenTelemetry Collector. Add Meter there as a second destination. What
          goes to Splunk does not change, and nothing changes in your application.
        </p>
        <div className="pm-switch" role="group" aria-label="Where your Collector runs">
          {SPLUNK_HOSTS.map(([id, label]) => (
            <button
              key={id}
              type="button"
              className={id === host ? "pm-option active" : "pm-option"}
              aria-pressed={id === host}
              onClick={() => setHost(id)}
            >
              {label}
            </button>
          ))}
        </div>
        {/* Placeholders only: the generated token is shown once, above, inside
            the snippet marked for session-replay masking. */}
        {host === "linux" ? (
          <>
            <p className="muted">
              Put your ingest token in <code>{SPLUNK_ENV_FILE}</code>, next to your Splunk token:
            </p>
            <Snippet>{SPLUNK_ENV_SNIPPET}</Snippet>
            <p className="muted">
              Then add these blocks to <code>{SPLUNK_CONFIG_FILE}</code>, alongside what is already
              there, and restart the Collector.
            </p>
            <Snippet>{splunkLinuxSnippet(otlpUrl)}</Snippet>
          </>
        ) : (
          <>
            <p className="muted">Store your ingest token as a secret where the Collector runs:</p>
            <Snippet>{SPLUNK_K8S_SECRET}</Snippet>
            <p className="muted">
              Then add this to your values for the <code>splunk-otel-collector</code> Helm chart.
              The chart merges it into its defaults, so your Splunk pipelines stay as they are.
            </p>
            <Snippet>{splunkHelmSnippet(otlpUrl)}</Snippet>
          </>
        )}
        <p className="muted">
          This copies traces your apps send over OTLP. If some send Jaeger or Zipkin to the
          Collector instead, add those receivers to the <code>traces/meter</code> pipeline too.
        </p>
      </section>

      <section className="source-section">
        <h2>2. Meter's copy leaves without content</h2>
        <p className="muted">
          You may keep prompts and responses in Splunk. The <code>traces/meter</code> pipeline is
          Meter's alone: before anything leaves for Meter, it deletes prompt, response, tool and
          exception attributes, and drops span events, which is where AI instrumentation puts
          message content. Meter would discard all of that on arrival anyway. This way it is never
          sent.
        </p>
      </section>

      <section className="source-section">
        <h2>3. Attribute runs to a feature</h2>
        <p className="muted">
          Splunk's AI instrumentation follows OpenTelemetry's GenAI conventions, so Meter reads its
          spans directly. Set <code>meter.feature_id</code> the same way as on the{" "}
          <Link to="/install-sdk?tab=otel" className="link">
            OpenTelemetry
          </Link>{" "}
          tab: once for a whole service, or per run with baggage. Runs with no feature still count,
          in the <em>Unattributed</em> bucket.
        </p>
      </section>
    </>
  );
}

// ---------------------------------------------------------------------------
// Verification — shared by every route
// ---------------------------------------------------------------------------
/** Which kind of arrival the panel waits for. SDK events and OTLP traces are
 *  counted separately, so neither route is credited with the other's work. */
type VerifyRoute = "sdk" | "otel";
/**
 * A running hourglass, for the state where Meter is listening and nothing has
 * arrived yet.
 *
 * A pulsing dot reads as a status light, and a status light that is not green
 * reads as a fault — which is exactly the wrong thing to say to someone whose
 * install is fine and whose app simply has not made a model call yet. An
 * hourglass says "waiting", which is the truth.
 *
 * Drawn inline rather than as an emoji so it inherits `currentColor` in both
 * themes and animates: the sand drains from the top bulb into the bottom one,
 * and the glass turns over to start again. `aria-hidden` because the sentence
 * beside it already says what is happening — a screen reader announcing a
 * decorative timer would only interrupt it.
 */
function Hourglass() {
  return (
    <svg className="verify-hourglass" viewBox="0 0 20 24" aria-hidden focusable="false">
      <defs>
        {/* The sand is the bulb shape, revealed through a moving window: the
            top's window slides down as it empties, the bottom's slides up as
            it fills. Clipping keeps the sand inside the glass at every frame,
            which scaling the triangles would not. */}
        <clipPath id="hg-top-clip">
          <rect className="hg-drain" x="0" y="3" width="20" height="9" />
        </clipPath>
        <clipPath id="hg-bottom-clip">
          <rect className="hg-fill" x="0" y="21" width="20" height="9" />
        </clipPath>
      </defs>

      <polygon className="hg-sand" points="5,4 15,4 10,12" clipPath="url(#hg-top-clip)" />
      <polygon className="hg-sand" points="5,20 15,20 10,12" clipPath="url(#hg-bottom-clip)" />
      <line className="hg-stream" x1="10" y1="12" x2="10" y2="19" />

      <path className="hg-glass" d="M5 4 L15 4 L10 12 Z M5 20 L15 20 L10 12 Z" />
      <line className="hg-glass" x1="4" y1="3" x2="16" y2="3" />
      <line className="hg-glass" x1="4" y1="21" x2="16" y2="21" />
    </svg>
  );
}

function Verification({ route }: { route: VerifyRoute }) {
  const [event, setEvent] = useState<HookEvent | null>(null);
  const [run, setRun] = useState<OtelTrace | null>(null);
  const [failed, setFailed] = useState(false);
  const timer = useRef<ReturnType<typeof setTimeout>>();

  const stop = useCallback(() => clearTimeout(timer.current), []);

  useEffect(() => {
    let live = true;

    async function check() {
      try {
        if (route === "otel") {
          const { trace: found } = await api.recentOtelTrace();
          if (!live) return;
          setFailed(false);
          if (found) {
            setRun(found); // arrived — stop asking
            return;
          }
        } else {
          const { event: found } = await api.recentHookEvent();
          if (!live) return;
          setFailed(false);
          if (found) {
            setEvent(found); // arrived — stop asking
            return;
          }
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
  }, [stop, route]);

  if (route === "otel") {
    return (
      <section className="source-section verify-section">
        <h2>Test instrumentation</h2>
        {run ? (
          <>
            <p className="verify-state ok" role="status">
              <span className="verify-dot ok" aria-hidden /> Spans received
            </p>
            <dl className="verify-facts">
              <div>
                <dt>Application</dt>
                <dd>{run.application}</dd>
              </div>
              <div>
                <dt>Run</dt>
                <dd>{run.operation_name}</dd>
              </div>
              <div>
                <dt>Steps</dt>
                <dd>{run.span_count}</dd>
              </div>
              <div>
                <dt>Last received</dt>
                <dd>{new Date(run.received_at).toLocaleString()}</dd>
              </div>
            </dl>
            <p className="muted">
              Open{" "}
              <Link to="/traces" className="link">
                Traces
              </Link>{" "}
              to see the run, each of its steps, and what they cost.
            </p>
          </>
        ) : (
          <>
            <p className="verify-state" role="status">
              <Hourglass /> Waiting for your first OpenTelemetry trace…
            </p>
            <p className="muted">
              Run something your instrumentation traces. Exporters send in batches, so allow a few
              seconds. If nothing arrives, look at your exporter's own log: a <code>401</code> means
              the token header is wrong, and a <code>404</code> usually means the general{" "}
              <code>OTEL_EXPORTER_OTLP_ENDPOINT</code> was set to the full URL, so the path was
              added twice.
            </p>
            {failed && (
              <p className="muted" role="status">
                Could not reach Meter just now. Still trying.
              </p>
            )}
          </>
        )}
      </section>
    );
  }

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
            Metered cost is reconciled against your provider bill, so it refines your numbers rather
            than replacing them. Open the feature under{" "}
            <Link to="/features" className="link">
              Features
            </Link>{" "}
            to see its inference cost with <em>metered (hook)</em> as the source.
          </p>
        </>
      ) : (
        <>
          <p className="verify-state" role="status">
            <Hourglass /> Waiting for your first Meter event…
          </p>
          <p className="muted">
            Run your app so it makes a real model call. This page updates on its own, so there is no
            need to reload. If nothing arrives, check that both environment variables are set{" "}
            <em>in the running process</em> and that the <code>feature_id</code> matches a real
            feature. The SDK fails silently by design, so a missing token never errors. It simply
            does not report.
          </p>
          {failed && (
            <p className="muted" role="status">
              Could not reach Meter just now. Still trying.
            </p>
          )}
        </>
      )}
    </section>
  );
}
