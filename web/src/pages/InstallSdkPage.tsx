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
        <strong>never</strong> sends prompts, responses, tool arguments, or retrieved documents.
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
    </>
  );
}

// ---------------------------------------------------------------------------
// Verification — shared by both routes
// ---------------------------------------------------------------------------
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
