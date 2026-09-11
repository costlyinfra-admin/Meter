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
