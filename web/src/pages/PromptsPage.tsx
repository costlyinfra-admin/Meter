/**
 * Optimize → Prompts: the captured prompts, and Meter's proposed rewrites.
 *
 * Two screens, shaped like Applications and Features so they read as the same
 * product: a table whose rows open a detail page, and a detail page that shows
 * the prompt and its rewrite side by side.
 *
 * Two rules the UI has to keep visible:
 *
 *   **Nothing here is recommended yet.** A rewrite that has not been tested
 *   against real examples is a suggestion. It says so, and it carries no dollar
 *   figure, because the saving is only real once the tokens have been measured.
 *
 *   **Looking at a prompt is an act.** The text is fetched only when someone
 *   asks for it, and the server records who looked. So the page does not load
 *   content just because it rendered.
 */
import { useCallback, useEffect, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import {
  api,
  ApiError,
  type EvalKey,
  type Evaluation,
  type EvaluationCaseContent,
  type EvaluationEstimate,
  type PromptContent,
  type PromptDetail as PromptDetailData,
  type PromptSummary,
} from "../api";
import { money, num } from "../format";

/** Plain labels for the kinds of change a rewrite may claim. */
const CHANGE_LABELS: Record<string, string> = {
  redundancy: "Repeated instruction",
  examples: "Shorter example",
  cache_prefix: "Cacheable prefix",
  output_format: "Output format",
  unused_instruction: "Unused instruction",
  wording: "Wording",
};

export function PromptsPage() {
  const navigate = useNavigate();
  const [data, setData] = useState<{
    usage_days: number;
    min_samples: number;
    prompts: PromptSummary[];
  } | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api
      .prompts()
      .then(setData)
      .catch((err) => setError(err instanceof ApiError ? err.message : "Could not load prompts."));
  }, []);

  return (
    <div className="content">
      <Link to="/optimize" className="link breadcrumb">
        ← Optimization Copilot
      </Link>
      <div className="dash-head">
        <h1>Prompts</h1>
      </div>
      <p className="muted">
        The prompts your product runs, with what they cost. Meter can propose a cheaper version of
        one and explain every change.
      </p>

      {error && (
        <p className="error" role="alert">
          {error}
        </p>
      )}

      {data === null && !error ? (
        <p className="muted">Loading…</p>
      ) : data && data.prompts.length === 0 ? (
        <div className="empty-state">
          <p className="empty-title">No prompts collected yet</p>
          <p className="muted">
            A prompt appears here once your organization has turned on prompt optimization in{" "}
            <Link to="/settings#privacy" className="link">
              Settings
            </Link>{" "}
            and a developer has switched capture on in the SDK.
          </p>
          <Link className="button-link" to="/install-sdk?tab=manual">
            Install SDK
          </Link>
        </div>
      ) : data ? (
        <>
          <table className="features-table">
            <caption className="sr-only">Captured prompts and what they cost</caption>
            <thead>
              <tr>
                <th>Prompt</th>
                <th>Version</th>
                <th>Feature</th>
                <th className="num" title={`Metered calls in the last ${data.usage_days} days`}>
                  Calls
                </th>
                <th className="num">Cost</th>
                <th className="num" title="Real calls kept for testing a rewrite">
                  Samples
                </th>
                <th>Rewrite</th>
              </tr>
            </thead>
            <tbody>
              {data.prompts.map((p) => (
                <tr
                  key={p.template_id}
                  className="feature-row"
                  onClick={() => navigate(`/optimize/prompts/${p.template_id}`)}
                >
                  <td>
                    <Link
                      to={`/optimize/prompts/${p.template_id}`}
                      onClick={(e) => e.stopPropagation()}
                    >
                      {p.prompt_id}
                    </Link>
                  </td>
                  <td>
                    <code className="slug-tag">{p.prompt_version}</code>
                  </td>
                  <td>
                    <Link
                      to={`/features/${p.feature_id}`}
                      onClick={(e) => e.stopPropagation()}
                      className="link"
                    >
                      {p.feature_name}
                    </Link>
                  </td>
                  <td className="num">{num(p.calls)}</td>
                  <td className="num">{money(p.cost)}</td>
                  <td className="num">
                    {num(p.samples)}
                    {p.samples < data.min_samples && (
                      <span className="muted"> / {data.min_samples}</span>
                    )}
                  </td>
                  <td>
                    {p.candidate_status === "not_evaluated" ? (
                      <span className="badge">Proposed, not tested</span>
                    ) : (
                      <span className="muted">—</span>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          <p className="muted legend">
            Calls and cost are metered traffic over the last {data.usage_days} days, not the
            samples: samples are a small fraction of calls, kept only for testing. A rewrite needs{" "}
            {data.min_samples} samples behind it.
          </p>
        </>
      ) : null}
    </div>
  );
}

export function PromptDetail() {
  const { id = "" } = useParams();
  const [prompt, setPrompt] = useState<PromptDetailData | null>(null);
  const [content, setContent] = useState<PromptContent | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<null | "show" | "suggest" | "discard">(null);
  const [confirmDiscard, setConfirmDiscard] = useState(false);

  const load = useCallback(async () => {
    try {
      setPrompt(await api.prompt(id));
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not load this prompt.");
    }
  }, [id]);

  useEffect(() => {
    void load();
  }, [load]);

  async function run<T>(kind: NonNullable<typeof busy>, fn: () => Promise<T>): Promise<T | null> {
    setBusy(kind);
    setError(null);
    try {
      return await fn();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Something went wrong.");
      return null;
    } finally {
      setBusy(null);
    }
  }

  async function show() {
    const found = await run("show", () => api.promptContent(id));
    if (found) setContent(found);
  }

  async function suggest() {
    const made = await run("suggest", () => api.generatePromptCandidate(id));
    if (made) {
      await load();
      // The rewrite is only worth showing beside the prompt it replaces.
      const found = await api.promptContent(id).catch(() => null);
      if (found) setContent(found);
    }
  }

  async function discard(candidateId: string) {
    const done = await run("discard", () => api.discardPromptCandidate(candidateId));
    if (done) {
      setConfirmDiscard(false);
      setContent(null);
      await load();
    }
  }

  if (error && !prompt) {
    return (
      <div className="content">
        <Link to="/optimize/prompts" className="link breadcrumb">
          ← All prompts
        </Link>
        <p className="error" role="alert">
          {error}
        </p>
      </div>
    );
  }
  if (!prompt) {
    return (
      <div className="content">
        <Link to="/optimize/prompts" className="link breadcrumb">
          ← All prompts
        </Link>
        <p className="muted">Loading…</p>
      </div>
    );
  }

  const enough = prompt.samples >= 20 || prompt.candidate !== null;
  const candidate = prompt.candidate;

  return (
    <div className="content">
      <Link to="/optimize/prompts" className="link breadcrumb">
        ← All prompts
      </Link>

      <h1>{prompt.prompt_id}</h1>
      <p className="detail-meta">
        <code className="slug-tag">{prompt.prompt_version}</code>
        <Link to={`/features/${prompt.feature_id}`} className="link">
          {prompt.feature_name}
        </Link>
        <span className="muted">{num(prompt.calls)} calls</span>
        <span className="muted">{money(prompt.cost)}</span>
        <span className="muted">{num(prompt.samples)} samples held</span>
      </p>

      {error && (
        <p className="error" role="alert">
          {error}
        </p>
      )}

      <section className="detail-section">
        <div className="section-head">
          <div>
            <h2>The prompt</h2>
            <span className="section-sub muted">
              Shown only when you ask. Meter records who looked at a prompt.
            </span>
          </div>
          {!content && (
            <button className="secondary" onClick={() => void show()} disabled={busy !== null}>
              {busy === "show" ? "Loading…" : "Show prompt"}
            </button>
          )}
        </div>

        {content ? (
          <div className={candidate ? "prompt-pair" : ""}>
            <div>
              <span className="chart-title">Current</span>
              <pre className="prompt-text">{content.template}</pre>
            </div>
            {content.candidate && (
              <div>
                <span className="chart-title">Proposed</span>
                <pre className="prompt-text proposed">{content.candidate.template}</pre>
              </div>
            )}
          </div>
        ) : (
          <p className="muted">Hidden.</p>
        )}
      </section>

      <section className="detail-section">
        <div className="section-head">
          <div>
            <h2>Proposed rewrite</h2>
            <span className="section-sub muted">
              A cheaper version of the same prompt, with a reason for every change.
            </span>
          </div>
          {!candidate && (
            <button onClick={() => void suggest()} disabled={!enough || busy !== null}>
              {busy === "suggest" ? "Thinking…" : "Suggest a cheaper prompt"}
            </button>
          )}
        </div>

        {!candidate ? (
          <p className="muted">
            {enough
              ? "No rewrite yet."
              : `${num(prompt.samples)} of 20 samples so far. A rewrite needs enough real calls behind it to be worth trusting.`}
          </p>
        ) : (
          <>
            <p className="detail-meta">
              <span className="badge">Not tested</span>
              <span className="muted">
                written by {candidate.provider} ({candidate.model})
              </span>
              <span className="muted">
                {num(candidate.change_count)} change{candidate.change_count === 1 ? "" : "s"}
              </span>
              <span className="muted">
                {num(candidate.original_chars)} → {num(candidate.candidate_chars)} characters
              </span>
            </p>
            <p className="hint">
              No saving is claimed yet. What a rewrite saves, and whether it answers as well, is
              only known once it has been tested against real examples.
            </p>

            {content?.candidate ? (
              <ul className="prompt-changes">
                {content.candidate.changes.map((change, i) => (
                  <li key={i}>
                    <span className="change-category">
                      {CHANGE_LABELS[change.category] ?? change.category}
                    </span>
                    <p className="change-reason">{change.reason}</p>
                    {(change.before || change.after) && (
                      <p className="change-excerpt">
                        <del>{change.before}</del>
                        {change.after && <ins>{change.after}</ins>}
                      </p>
                    )}
                    {change.expected_effect && (
                      <p className="muted change-effect">
                        The model expects: {change.expected_effect}
                      </p>
                    )}
                  </li>
                ))}
              </ul>
            ) : (
              <p className="muted">Show the prompt to see what changed and why.</p>
            )}

            <div className="settings-actions">
              {confirmDiscard ? (
                <>
                  <button
                    className="danger"
                    onClick={() => void discard(candidate.candidate_id)}
                    disabled={busy !== null}
                  >
                    {busy === "discard" ? "Discarding…" : "Yes, discard it"}
                  </button>
                  <button className="secondary" onClick={() => setConfirmDiscard(false)}>
                    Cancel
                  </button>
                </>
              ) : (
                <button className="secondary" onClick={() => setConfirmDiscard(true)}>
                  Discard this rewrite
                </button>
              )}
            </div>
          </>
        )}
      </section>

      {candidate && <EvaluationPanel templateId={id} onDecided={() => void load()} />}

      <section className="detail-section">
        <div className="section-head">
          <div>
            <h2>Samples held</h2>
            <span className="section-sub muted">
              Real calls kept for testing, deleted 30 days after they arrive.
            </span>
          </div>
        </div>
        {prompt.samples_detail.length === 0 ? (
          <p className="muted">None.</p>
        ) : (
          <table className="mini-table">
            <thead>
              <tr>
                <th>Model</th>
                <th className="num">Tokens in</th>
                <th className="num">Tokens out</th>
                <th className="num">Latency</th>
                <th>Captured</th>
              </tr>
            </thead>
            <tbody>
              {prompt.samples_detail.slice(0, 10).map((s) => (
                <tr key={s.sample_id}>
                  <td>{s.model}</td>
                  <td className="num">{s.tokens_in === null ? "—" : num(s.tokens_in)}</td>
                  <td className="num">{s.tokens_out === null ? "—" : num(s.tokens_out)}</td>
                  <td className="num">{s.latency_ms === null ? "—" : `${num(s.latency_ms)}ms`}</td>
                  <td>{new Date(s.captured_at).toLocaleString()}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>
    </div>
  );
}

/** How long between checks while a run is in flight. Replaying thirty inputs
 *  twice takes minutes, so this is a progress bar, not a spinner. */
const POLL_MS = 3000;

const REASONS: Record<string, string> = {
  no_candidate: "There is no rewrite to test yet.",
  already_running: "An evaluation of this rewrite is already running.",
  no_key: "Testing makes real model calls, so it needs a key for this provider.",
  unpriced_model: "Meter has no rates for this model, so a saving could not be measured.",
  over_cap: "This run would take your organization past its monthly evaluation cap.",
  no_samples: "There are no samples to replay.",
};

/** A per-call price, which is fractions of a cent. The shared money() stops at
 *  cents, where every row of this table would read "$0.00" and the comparison
 *  the table exists for would be invisible. */
const perCall = (value: number) => `$${value.toFixed(value < 0.01 ? 5 : 2)}`;

/**
 * Testing a rewrite: what it would cost, then what it actually measured.
 *
 * Everything here is the customer's own money and the customer's own model, so
 * the panel says what a run will cost before it runs, and afterwards shows the
 * measurement rather than a verdict on its own: the counts, the cost per call
 * either way, and the volume any projected saving was scaled by.
 */
function EvaluationPanel({ templateId, onDecided }: { templateId: string; onDecided: () => void }) {
  const [run, setRun] = useState<Evaluation | null>(null);
  const [estimate, setEstimate] = useState<EvaluationEstimate | null>(null);
  const [keys, setKeys] = useState<EvalKey[]>([]);
  const [cases, setCases] = useState<EvaluationCaseContent[] | null>(null);
  const [apiKey, setApiKey] = useState("");
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    const [latest, plan, keyList] = await Promise.all([
      api.latestEvaluation(templateId).catch(() => null),
      api.evaluationEstimate(templateId).catch(() => null),
      api
        .evalKeys()
        .then((r) => r.keys)
        .catch(() => []),
    ]);
    setRun(latest);
    setEstimate(plan);
    setKeys(keyList);
    return latest;
  }, [templateId]);

  useEffect(() => {
    void load();
  }, [load]);

  // Only while something is actually running: a finished run never polls.
  useEffect(() => {
    if (run?.status !== "running") return;
    const timer = setInterval(() => {
      void api
        .evaluation(run.evaluation_id)
        .then((next) => {
          setRun(next);
          if (next.status !== "running") {
            clearInterval(timer);
            onDecided();
          }
        })
        .catch(() => clearInterval(timer));
    }, POLL_MS);
    return () => clearInterval(timer);
  }, [run?.status, run?.evaluation_id, onDecided]);

  async function act<T>(kind: string, fn: () => Promise<T>): Promise<T | null> {
    setBusy(kind);
    setError(null);
    try {
      return await fn();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Something went wrong.");
      return null;
    } finally {
      setBusy(null);
    }
  }

  const provider = estimate?.provider ?? run?.provider ?? "";
  const needsKey = estimate?.reason === "no_key";
  const spent = estimate?.spent_this_month ?? 0;
  const cap = estimate?.monthly_cap ?? 0;

  return (
    <section className="detail-section">
      <div className="section-head">
        <div>
          <h2>Testing</h2>
          <span className="section-sub muted">
            Real inputs, replayed through both prompts on your own model, then compared.
          </span>
        </div>
        {!run || run.status === "failed" ? (
          <button
            onClick={() =>
              void act("start", () => api.startEvaluation(templateId)).then((started) => {
                if (started) setRun(started);
              })
            }
            disabled={!estimate?.can_run || busy !== null}
          >
            {busy === "start" ? "Starting…" : "Test this rewrite"}
          </button>
        ) : null}
      </div>

      {error && (
        <p className="error" role="alert">
          {error}
        </p>
      )}

      {!run && estimate && (
        <p className="muted">
          {estimate.can_run ? (
            <>
              Replays {estimate.cases} real input{estimate.cases === 1 ? "" : "s"} through both
              prompts on {estimate.provider} ({estimate.model}). Estimated cost{" "}
              <strong>{money(estimate.cost_estimate ?? 0)}</strong> on your account. {money(spent)}{" "}
              of {money(cap)} used this month.
            </>
          ) : (
            (REASONS[estimate.reason ?? ""] ?? "This rewrite cannot be tested yet.")
          )}
        </p>
      )}

      {needsKey && (
        <div className="settings-field">
          <label htmlFor="eval-key">{provider} API key</label>
          <input
            id="eval-key"
            type="password"
            autoComplete="off"
            value={apiKey}
            onChange={(e) => setApiKey(e.target.value)}
          />
          <span className="settings-hint muted">
            A key that can make model calls, which the read-only cost key cannot. Stored encrypted
            and never shown again.
          </span>
          <div className="settings-actions">
            <button
              onClick={() => {
                const entered = apiKey;
                setApiKey("");
                void act("key", () => api.setEvalKey(provider, entered)).then((r) => {
                  if (r) {
                    setKeys(r.keys);
                    void load();
                  }
                });
              }}
              disabled={!apiKey || busy !== null}
            >
              {busy === "key" ? "Saving…" : "Save key"}
            </button>
          </div>
        </div>
      )}

      {run?.status === "running" && (
        <p className="muted" role="status">
          Replaying {run.cases_done} of {run.cases_planned} inputs…
        </p>
      )}

      {run?.status === "failed" && (
        <p className="hint" role="status">
          The run stopped: {run.error || "the provider could not be reached."} The rewrite is
          unchanged and untested.
        </p>
      )}

      {run?.status === "completed" && (
        <>
          <p className="detail-meta">
            <span className={run.decision === "recommended" ? "badge ok" : "badge"}>
              {run.decision === "recommended" ? "Recommended" : "Not recommended"}
            </span>
            <span className="muted">{run.decision_reason}</span>
          </p>

          <table className="mini-table">
            <thead>
              <tr>
                <th>Per call</th>
                <th className="num">Now</th>
                <th className="num">With the rewrite</th>
              </tr>
            </thead>
            <tbody>
              <tr>
                <td>Cost</td>
                <td className="num">{perCall(run.cost_before)}</td>
                <td className="num">{perCall(run.cost_after)}</td>
              </tr>
              <tr>
                <td>Tokens in</td>
                <td className="num">{num(run.tokens_in_before)}</td>
                <td className="num">{num(run.tokens_in_after)}</td>
              </tr>
              <tr>
                <td>Tokens out</td>
                <td className="num">{num(run.tokens_out_before)}</td>
                <td className="num">{num(run.tokens_out_after)}</td>
              </tr>
              <tr>
                <td>Latency</td>
                <td className="num">{num(run.latency_before_ms)}ms</td>
                <td className="num">{num(run.latency_after_ms)}ms</td>
              </tr>
            </tbody>
          </table>

          <p className="muted legend">
            Better on {run.better}, the same on {run.same}, worse on {run.worse} of {run.cases_done}{" "}
            replayed inputs
            {run.check_failures > 0 && `, and ${run.check_failures} came back broken`}. Judged by{" "}
            {run.judge_model || "no judge"}, each case both ways round. The run cost{" "}
            {money(run.spend)}.
          </p>

          {run.decision === "recommended" && (
            <p className="muted">
              At {num(run.calls_30d)} call{run.calls_30d === 1 ? "" : "s"} in the last 30 days, that
              is <strong>{money(run.projected_monthly_saving)}</strong> a month — a ceiling, which
              holds while your traffic looks like the inputs this was measured on.
            </p>
          )}

          <div className="settings-actions">
            {cases ? null : (
              <button
                className="secondary"
                onClick={() =>
                  void act("cases", () => api.evaluationCases(run.evaluation_id)).then((r) => {
                    if (r) setCases(r.cases);
                  })
                }
                disabled={busy !== null}
              >
                {busy === "cases" ? "Loading…" : "Show the answers"}
              </button>
            )}
          </div>

          {cases && (
            <ul className="prompt-changes">
              {cases.map((c) => (
                <li key={c.case_id}>
                  <span className="change-category">{c.verdict}</span>
                  {c.reason && <p className="change-reason">{c.reason}</p>}
                  <div className="prompt-pair">
                    <div>
                      <span className="chart-title">Now</span>
                      <pre className="prompt-text">{c.before}</pre>
                    </div>
                    <div>
                      <span className="chart-title">With the rewrite</span>
                      <pre className="prompt-text proposed">{c.after}</pre>
                    </div>
                  </div>
                  {c.failed_checks.length > 0 && (
                    <p className="change-effect muted">
                      Failed checks: {c.failed_checks.join(", ")}
                    </p>
                  )}
                </li>
              ))}
            </ul>
          )}
        </>
      )}

      {keys.length > 0 && (
        <p className="settings-hint muted">
          Replay keys:{" "}
          {keys.map((k) => `${k.provider} ${k.has_key ? "added" : "not added"}`).join(", ")}.
        </p>
      )}
    </section>
  );
}
